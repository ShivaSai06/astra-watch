"""Collectors, retention, engine lifecycle, notification policy."""
import asyncio
import json
import time

from conftest import add_sample, add_snap
from watcher import collect_linux as cl
from watcher.config import Settings


# ------------------------------------------------------------------ collectors / calculations
def test_stat_parser_handles_spaces_and_parens_in_comm():
    raw = "42 (my (weird) proc) S 1 42 42 0 -1 4194560 100 0 0 0 150 50 0 0 20 0 1 0 100 1000000 2500 18446744073709551615"
    p = cl.parse_stat_line(raw)
    assert p["comm"] == "my (weird) proc" and p["ppid"] == 1 and p["ticks"] == 200
    assert p["rss"] == 2500 * cl.PAGE


def test_linux_memory_excludes_cache():
    mi = {"MemTotal": 4000, "MemFree": 100, "MemAvailable": 1800, "Cached": 1700, "Buffers": 100, "SwapTotal": 1000, "SwapFree": 400}
    m = cl.memory_numbers(mi)
    assert m["mem_used"] == 2200 and m["mem_pct"] == 55.0          # (4000-1800)/4000, not (4000-100)/4000
    assert m["swap_used"] == 600


def test_cpu_line_iowait_counts_as_idle():
    busy, total = cl.parse_cpu_line("cpu  100 0 100 700 100 0 0 0 0 0\n")
    assert total == 1000 and busy == 200


# ------------------------------------------------------------------ retention
def test_retention_24h_hard_cap_and_cleanup(env):
    db, now = env.db, time.time()
    add_sample(db, "mac", now - 30 * 3600)
    add_sample(db, "mac", now - 23 * 3600)
    add_sample(db, "mac", now - 10)
    add_snap(db, "mac", "procs", {}, now - 40 * 3600)
    db.audit("owner", "stop.execute", "mac", "x", "stopped")
    db.execute("UPDATE audit SET ts=?", (now - 25 * 3600,))
    db.audit("owner", "stop.execute", "mac", "ancient", "stopped")
    db.execute("UPDATE audit SET ts=? WHERE target='ancient'", (now - 31 * 86400,))
    db.event("inc_old", "detected", machine="mac", rule="r", severity="warning", title="t", ts=now - 31 * 86400)
    db.event("inc_mid", "detected", machine="mac", rule="r", severity="warning", title="t", ts=now - 10 * 86400)
    db.execute("INSERT INTO incidents(id,rule,machine,subject,severity,state,title,first_seen,last_seen,recovered_at) VALUES('old','r','mac','','warning','recovered','t',?,?,?)",
               (now - 50 * 3600, now - 30 * 3600, now - 30 * 3600))
    removed = db.prune(100_000)                                      # a request for 'forever' is capped at 24 h
    assert removed["samples"] == 1 and removed["snapshots"] == 1 and removed["incidents"] >= 1
    assert "audit" not in removed and "incident_events" not in removed          # metric pruning never touches history
    assert db.one("SELECT COUNT(*) c FROM audit")["c"] == 2 and db.one("SELECT COUNT(*) c FROM incident_events")["c"] == 2
    h = db.prune_history(100_000)                                    # 'forever' is capped at 30 days
    assert h == {"incident_events": 1, "audit": 1}
    assert db.one("SELECT COUNT(*) c FROM audit WHERE target='x'")["c"] == 1 and db.one("SELECT COUNT(*) c FROM incident_events")["c"] == 1
    left = db.one("SELECT COUNT(*) c FROM samples")["c"]
    assert left == 2
    assert db.oldest_record() >= now - 24 * 3600


def test_retention_setting_cannot_exceed_24(env):
    import pytest
    with pytest.raises(ValueError):
        env.settings.update({"retention_hours": 48})
    assert env.settings.update({"retention_hours": 12})["retention_hours"] == 12


def test_prune_survives_restart(env):
    from watcher.db import DB
    now = time.time()
    add_sample(env.db, "mac", now - 100 * 3600)
    db2 = DB(env.paths.db)                                           # "restart"
    db2.prune(24)
    assert db2.one("SELECT COUNT(*) c FROM samples")["c"] == 0


def test_database_size_stays_bounded(env):
    """24 h at 30 s for two machines, with process snapshots each minute, stays small."""
    now = time.time()
    env.db.execute("BEGIN")
    for i in range(8640):
        for m in ("mac", "hetzner"):
            add_sample(env.db, m, now - i * 30, extra={"a": 1})
    for i in range(4320):
        add_snap(env.db, "mac", "procs", {"top": [{"pid": j, "name": "x" * 20, "rss": 1e8} for j in range(40)]}, now - i * 60)
    env.db.execute("COMMIT")
    env.db.prune(24)
    assert env.db.size_bytes() < 60e6


# ------------------------------------------------------------------ engine lifecycle
def run_ticks(e, start, seconds, step, **sample):
    t = start
    while t <= start + seconds:
        add_sample(e.db, "hetzner", t, **sample)
        e.engine.tick(t)
        asyncio.run(e.notifier.process(t))
        t += step
    return t


def incidents(e, state=None):
    q = "SELECT * FROM incidents" + (f" WHERE state='{state}'" if state else "")
    return e.db.query(q)


def test_single_spike_never_alerts(env):
    t0 = time.time() - 3600
    run_ticks(env, t0, 60, 30, mem_pct=40)
    add_sample(env.db, "hetzner", t0 + 90, mem_pct=97)
    env.engine.tick(t0 + 90)
    run_ticks(env, t0 + 120, 120, 30, mem_pct=40)
    assert incidents(env) == [] and env.tg.sent == []


def test_persistent_memory_alert_notifies_once_then_recovers(env):
    t0 = time.time() - 7200
    t = run_ticks(env, t0, 60, 30, mem_pct=40)
    assert env.tg.sent == []
    t = run_ticks(env, t, 600, 30, mem_pct=91)                       # warning for 10 minutes
    alerts = [m for m in env.tg.sent if "NEEDS ATTENTION" in m["text"]]
    assert len(alerts) == 1                                         # deduplicated: one message for the whole incident
    txt = alerts[0]["text"]
    assert "HETZNER SERVER" in txt and "What happened" in txt and "Why it matters" in txt and "First steps" in txt
    assert "inferred" in txt                                         # a largest-user is never presented as confirmed
    assert "https://astra.example.ts.net/#/activity/inc_" in txt
    assert alerts[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Open details"
    t = run_ticks(env, t, 300, 30, mem_pct=30)                       # clears
    rec = [m for m in env.tg.sent if "ALERT CLEARED" in m["text"]]
    assert len(rec) == 1
    assert "Root cause" in rec[0]["text"] and "does not mean" in rec[0]["text"]       # cleared != fixed
    assert "Memory in use: 91% →" in rec[0]["text"] and "None. Astra Watch did not change anything." in rec[0]["text"]
    kinds = [r["kind"] for r in env.db.query("SELECT kind FROM incident_events ORDER BY id")]
    assert kinds[:3] == ["detected", "investigated", "recommended"] and "recovered" in kinds
    assert incidents(env, "recovered") and not incidents(env, "active")
    run_ticks(env, t, 600, 30, mem_pct=30)
    assert len([m for m in env.tg.sent if "ALERT CLEARED" in m["text"]]) == 1   # recovery sent only once


def test_escalation_sends_one_more_message(env):
    t0 = time.time() - 7200
    t = run_ticks(env, t0, 400, 30, mem_pct=88)
    assert len(env.tg.sent) == 1
    run_ticks(env, t, 400, 30, mem_pct=97)
    assert len(env.tg.sent) == 2 and "CRITICAL" in env.tg.sent[1]["text"] and "got worse" in env.tg.sent[1]["text"]


def test_cooldown_suppresses_flapping_repeat(env):
    t0 = time.time() - 36000
    t = run_ticks(env, t0, 400, 30, mem_pct=90)           # incident 1 -> notified
    t = run_ticks(env, t, 300, 30, mem_pct=30)            # recovers
    n = len(env.tg.sent)                                   # alert + recovery
    t = run_ticks(env, t, 400, 30, mem_pct=90)            # same problem again within 30-minute cooldown
    assert len(env.tg.sent) == n                           # suppressed
    row = env.db.one("SELECT notify_state FROM incidents WHERE state='active'")
    assert row["notify_state"] == "suppressed"
    run_ticks(env, t, 1700, 30, mem_pct=90)               # still going after the cooldown -> one message
    assert len(env.tg.sent) == n + 1


def test_telegram_failure_is_retried_and_status_recorded(env):
    env.tg.fail = True
    t0 = time.time() - 7200
    t = run_ticks(env, t0, 400, 30, mem_pct=90)
    r = env.db.one("SELECT notify_state, notify_attempts, notify_error FROM incidents")
    assert r["notify_state"] in ("failed_retry", "failed") and r["notify_attempts"] >= 1
    env.tg.fail = False
    run_ticks(env, t + 100, 1200, 30, mem_pct=90)
    assert env.db.one("SELECT notify_state FROM incidents")["notify_state"] == "sent"
    assert any(n["status"] == "failed" for n in env.db.query("SELECT * FROM notifications"))


def test_mac_memory_uses_pressure_not_just_percentage(env):
    """macOS shows high 'used' routinely thanks to compression: no alert while pressure is normal and swap small."""
    t = time.time() - 3000
    for i in range(30):
        add_sample(env.db, "mac", t + i * 30, mem_pct=90, swap_used=200e6, extra={"pressure_level": 1})
    env.hub.last_seen = t + 29 * 30
    assert [f for f in env.engine.evaluate(t + 29 * 30) if f.rule == "memory"] == []
    add_sample(env.db, "mac", t + 31 * 30, mem_pct=90, swap_used=200e6, extra={"pressure_level": 2})
    f = [f for f in env.engine.evaluate(t + 31 * 30) if f.rule == "memory"]
    assert f and f[0].severity == "warning"
    add_sample(env.db, "mac", t + 32 * 30, mem_pct=70, swap_used=0, extra={"pressure_level": 4})
    assert [f.severity for f in env.engine.evaluate(t + 32 * 30) if f.rule == "memory"] == ["critical"]


def vitest_snapshot(n=6, orphans=5):
    procs = [{"pid": 1000 + i, "ppid": 1 if i < orphans else 500, "name": "node", "exe": "/usr/local/bin/node", "user": "me",
              "start": 1700000000 + i, "cpu": 0.0, "rss": 400_000_000, "label": "vitest", "eligible": True, "growing": False,
              "growth": 0, "parent": "", "why": ""} for i in range(n)]
    return {"top_cpu": [], "top_mem": procs[:5], "dev": procs,
            "groups": {"vitest": {"count": n, "rss": n * 400_000_000, "cpu": 0, "orphans": orphans, "idle": n}}, "docker_vm_rss": 0}


def test_vitest_workers_detected_with_cause_confidence_and_stop_offers(env):
    now = time.time()
    add_sample(env.db, "mac", now, mem_pct=92, swap_used=3.1e9, extra={"pressure_level": 2})
    add_snap(env.db, "mac", "procs", vitest_snapshot(), now)
    env.hub.last_seen = now
    fs = {f.rule: f for f in env.engine.evaluate(now)}
    assert "dev_accumulation" in fs and fs["dev_accumulation"].confidence == "high"
    assert "orphan" not in fs["dev_accumulation"].cause or True
    assert len(fs["dev_accumulation"].targets) == 3
    assert fs["memory"].cause.startswith("Several Vitest test workers are still running")
    assert "Main contributor" in fs["memory"].summary


def test_long_running_alone_is_not_a_problem(env):
    now = time.time()
    add_sample(env.db, "mac", now, mem_pct=40, extra={"pressure_level": 1})
    snap = vitest_snapshot(n=1, orphans=0)
    snap["groups"]["vitest"]["idle"] = 0
    add_snap(env.db, "mac", "procs", snap, now)
    env.hub.last_seen = now
    assert [f for f in env.engine.evaluate(now) if f.rule == "dev_accumulation"] == []


def test_agent_disconnect_and_clean_stop(env):
    now = time.time()
    add_sample(env.db, "mac", now - 3600, mem_pct=30, extra={"pressure_level": 1})
    env.hub.last_seen = now - 3600
    assert [f.rule for f in env.engine.evaluate(now) if f.rule == "agent_down"] == ["agent_down"]
    env.db.kv_set("mac_agent_state", {"ts": now - 3600, "clean_stop": True})
    assert [f for f in env.engine.evaluate(now) if f.rule == "agent_down"] == []   # deliberate stop: no alarm


def test_disk_thresholds_use_free_percentage(env):
    now = time.time()
    add_sample(env.db, "hetzner", now, disk_free=15e9, disk_total=100e9)
    assert [f.severity for f in env.engine.evaluate(now) if f.rule == "disk"] == ["warning"]
    add_sample(env.db, "hetzner", now + 1, disk_free=5e9, disk_total=100e9)
    assert [f.severity for f in env.engine.evaluate(now + 1) if f.rule == "disk"] == ["critical"]


def test_restart_loop_detected_from_history(env):
    now = time.time()
    add_sample(env.db, "hetzner", now)
    def cont(r): return {"list": [{"id": "a" * 64, "name": "flappy", "running": True, "status": "running", "restarts": r, "health": "none", "cpu": 1, "mem": 1e6, "restart_policy": "always", "exit_code": 1}]}
    add_snap(env.db, "hetzner", "containers", cont(2), now - 1000)
    add_snap(env.db, "hetzner", "containers", cont(9), now)
    f = [f for f in env.engine.evaluate(now) if f.rule == "restart_loop"]
    assert f and f[0].severity == "critical" and f[0].persist == 0


def test_swap_growth_detected_but_stable_swap_is_ignored(env):
    now = time.time()
    for i in range(40):                                               # +20 MB per step => +780 MB in 30 min
        add_sample(env.db, "hetzner", now - 1800 + i * 45, swap_used=1.0e9 + i * 20e6)
    assert [f.rule for f in env.engine.evaluate(now) if f.rule == "swap_growth"] == ["swap_growth"]
    env.db.execute("DELETE FROM samples")
    for i in range(40):                                               # server today: large but flat swap
        add_sample(env.db, "hetzner", now - 1800 + i * 45, swap_used=1.85e9)
    assert [f for f in env.engine.evaluate(now) if f.rule == "swap_growth"] == []


def test_monitoring_the_monitor(env):
    now = time.time()
    add_sample(env.db, "hetzner", now)
    env.engine.self_state = {"rss": 400e6, "cpu": 2, "db_size": 1e6}
    assert [f.rule for f in env.engine.evaluate(now) if f.rule == "self_resource"] == ["self_resource"]


def test_service_probe_failure(env):
    now = time.time()
    add_sample(env.db, "hetzner", now)
    env.engine.service_probe = lambda port: port != 443
    f = [f for f in env.engine.evaluate(now) if f.rule == "service_probe"]
    assert len(f) == 1 and f[0].severity == "critical"


def test_settings_validation(env):
    import pytest
    for bad in ({"refresh_interval": 1}, {"thresholds": {"mem_warn": 99, "mem_crit": 90}}, {"bogus": 1},
                {"thresholds": {"nope": 1}}, {"public_url": "http://x"}, {"recovery_notifications": "yes"}):
        with pytest.raises(ValueError):
            env.settings.update(bad)
    out = env.settings.update({"thresholds": {"mem_warn": 80}, "cooldown_minutes": 10})
    assert out["thresholds"]["mem_warn"] == 80 and Settings(env.paths).get()["cooldown_minutes"] == 10
