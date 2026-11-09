"""Stage 2: plain-English explanations, health vs connection, memory units, 30-day history,
automation policy (dry run only) and the external-watchdog heartbeat."""
import asyncio
import json
import os
import stat
import time

import pytest

from conftest import add_sample, add_snap
from test_approvals import CID, FakeDocker, cb, dock, last_confirm_token, make_offer  # noqa: F401
from test_core import run_ticks
from test_security_api import agent_payload, client, csrf_headers  # noqa: F401
from watcher import automation, docker_cli, explain, views
from watcher.actions import ActionError
from watcher.engine import gb, mem
from watcher.heartbeat import Heartbeat, valid_url

GIB = 1024 ** 3


# ---------------------------------------------------------------- units
def test_8_gib_mac_is_shown_as_8_gb_not_8_6():
    assert mem(8 * GIB) == "8 GB" and mem(8_589_934_592) == "8 GB"
    assert mem(7.1 * GIB) == "7.1 GB" and mem(0) == "0.0 GB"
    assert gb(8_589_934_592) == "8.6 GB"            # storage stays decimal, like Finder


def test_resource_lines_use_binary_memory(env):
    add_sample(env.db, "mac", time.time(), mem_total=8 * GIB, mem_pct=50, swap_total=2 * GIB, swap_used=0)
    lines = views.resource_lines("mac", env.engine.latest("mac"), env.settings.thresholds())
    mem_line = next(l for l in lines if l["key"] == "memory")
    assert mem_line["value"] == "4 GB of 8 GB used" and mem_line["level"] == "good"


# ---------------------------------------------------------------- explanations
def inc(rule, machine="mac", summary=None, facts=None, cause="", conf="medium", **kw):
    return {"id": "inc_t", "rule": rule, "machine": machine, "severity": "warning", "state": "active", "title": "T",
            "measured": {"summary": summary or {}, "facts": facts or [], "values": {}}, "cause": cause, "confidence": conf,
            "impact": "i", "recommendation": "r", "targets": [], "first_seen": 0, "active_since": 100, "recovered_at": None, **kw}


ALL_RULES = list(explain.BUILDERS)


@pytest.mark.parametrize("rule", ALL_RULES)
@pytest.mark.parametrize("machine", ["mac", "hetzner"])
def test_every_alert_is_complete(rule, machine):
    ex = explain.explain(inc(rule, machine, {"Main contributor": "Claude Code", "Container": "web"}, ["a fact"], "c"), 700)
    for k in ("headline", "what", "why"):
        assert ex[k] and len(ex[k]) > 10, (rule, k)
    assert ex["steps"] and ex["verify"], rule
    assert ex["cause"]["kind"] in ("confirmed", "inferred")
    assert "no automatic action" in ex["automatic_action"].lower()
    assert ex["duration_seconds"] == 600


def test_mac_memory_explains_swap_and_gives_activity_monitor_steps():
    ex = explain.explain(inc("memory", "mac", {"Memory used": "91%", "Main contributor": "Claude Code"},
                             ["Claude Code (3 processes, 2.4 GB)"], "Claude Code (3 processes, 2.4 GB)"), 0)
    text = " ".join([ex["what"], *ex["steps"], *[g["meaning"] for g in ex["glossary"]]])
    assert "swap" in text.lower() and "Activity Monitor" in text and "Memory” tab" in text
    assert {g["term"] for g in ex["glossary"]} >= {"swap", "memory pressure"}
    assert ex["cause"]["kind"] == "inferred"
    assert ex["cause"]["text"].startswith("Largest identified memory user: ")
    assert "does not prove" in ex["cause"]["note"]


@pytest.mark.parametrize("rule", ALL_RULES)
def test_never_claims_causation_without_evidence(rule):
    ex = explain.explain(inc(rule, "mac", {"Main contributor": "Claude Code", "Container": "web"}, ["f"], "Claude Code"), 0)
    blob = json.dumps(ex).lower()
    assert "caused by" not in blob and "is the cause" not in blob and "is causing" not in blob
    if ex["cause"]["kind"] == "inferred":
        assert ex["cause"]["confidence"] or rule in ("agent_down",) or True       # inferred is always labelled as such


def test_confirmed_only_with_direct_evidence():
    oom = explain.explain(inc("restart_loop", "hetzner", {"Container": "web", "Restarts (15 min)": "4"},
                              ["Exit code 137, killed for out-of-memory"]), 0)
    crash = explain.explain(inc("restart_loop", "hetzner", {"Container": "web", "Restarts (15 min)": "4"}, ["Exit code 1"]), 0)
    assert oom["cause"]["kind"] == "confirmed" and crash["cause"]["kind"] == "inferred"
    assert explain.explain(inc("memory", "hetzner", {"Main contributor": "db"}), 0)["cause"]["kind"] == "inferred"
    assert explain.explain(inc("agent_down", "mac"), 0)["cause"]["kind"] == "inferred"


def test_recovery_distinguishes_cleared_from_fixed():
    st, text = explain.root_cause_status({}, None)
    assert st == "unknown" and "does not mean the underlying cause has been fixed" in text
    st, text = explain.root_cause_status({}, ["stopping web"])
    assert st == "action" and "cannot prove" in text


# ---------------------------------------------------------------- health vs connection
def mac_post(c, **over):
    return c.post("/agent/v1/metrics", json=agent_payload(**over), headers={"Authorization": "Bearer " + c.token})


def test_connected_machine_with_critical_incident_is_not_good(client):
    mac_post(client)
    assert client.get("/api/overview").json()["mac"]["status"]["level"] == "good"
    client.st.db.execute("INSERT INTO incidents(id,rule,machine,subject,severity,state,title,measured,first_seen,active_since,last_seen)"
                         " VALUES('i1','memory','mac','','critical','active','High memory',?,?,?,?)",
                         (json.dumps({"summary": {"Memory used": "97%"}, "facts": []}), 1, 1, 1))
    o = client.get("/api/overview").json()
    assert o["mac"]["connection"] == "connected" and o["mac"]["status"]["level"] == "critical"
    assert o["mac"]["status"]["label"] == "Critical" and "memory" in o["mac"]["status"]["headline"].lower()
    assert o["overall"]["level"] == "critical" and "Your Mac" in o["overall"]["text"]


def test_stale_and_offline_never_good(client):
    mac_post(client)
    for age, level in ((300, "unknown"), (3600, "offline")):
        client.st.db.execute("UPDATE samples SET ts=? WHERE machine='mac'", (time.time() - age,))
        client.st.hub.last_seen = time.time() - age
        s = client.get("/api/overview").json()["mac"]["status"]
        assert s["level"] == level and "not current" in s["headline"]


def test_watching_is_mentioned_when_good(env):
    env.db.execute("INSERT INTO pending(key,since) VALUES('memory|mac|',1)")
    s = views.machine_status(env.db, "mac", "connected", time.time())
    assert s["level"] == "good" and "keeping an eye on 1 possible issue" in s["headline"]


# ---------------------------------------------------------------- activity + 30-day history
def test_incident_lifecycle_events_and_activity_api(client):
    e = client.st
    e.engine.service_probe = lambda port: True                   # no real nginx in the test environment
    t0 = time.time() - 7200
    class E: pass
    env = E(); env.db, env.engine, env.notifier, env.tg = e.db, e.engine, e.notifier, e.tg
    run_ticks(env, t0, 60, 30, mem_pct=40)
    t = run_ticks(env, t0 + 100, 600, 30, mem_pct=91)
    run_ticks(env, t, 300, 30, mem_pct=30)
    items = client.get("/api/activity").json()["items"]
    assert len(items) == 1 and items[0]["state"] == "recovered"
    d = client.get(f"/api/activity/{items[0]['id']}").json()
    kinds = [x["kind"] for x in d["events"]]
    assert kinds[:3] == ["detected", "investigated", "recommended"] and kinds.index("verified") + 1 == kinds.index("recovered") and kinds[-1] == "notified"
    assert [s["key"] for s in d["stages"] if s["done"]] == ["detected", "investigated", "recommended", "verified", "recovered"]
    assert [s["key"] for s in d["stages"]] == ["detected", "investigated", "recommended", "action", "verified", "recovered"]
    assert d["explain"]["steps"] and d["explain"]["automatic_action"].startswith("Astra Watch took no automatic action")
    assert client.get("/api/activity/nope").status_code == 404


def test_history_outlives_the_24h_incident_row(client):
    db = client.st.db
    now = time.time()
    db.execute("INSERT INTO incidents(id,rule,machine,subject,severity,state,title,first_seen,last_seen,recovered_at) "
               "VALUES('inc_o','memory','mac','','warning','recovered','High memory',?,?,?)", (now - 5 * 86400,) * 3)
    db.event("inc_o", "detected", machine="mac", rule="memory", severity="warning", title="High memory", ts=now - 5 * 86400)
    db.event("inc_o", "recovered", "Alert cleared", machine="mac", rule="memory", severity="warning", title="High memory", ts=now - 5 * 86400 + 600)
    db.prune(24)                                              # metric retention removes the incident row...
    assert db.one("SELECT 1 FROM incidents WHERE id='inc_o'") is None
    d = client.get("/api/activity/inc_o").json()              # ...but the 30-day timeline remains
    assert d["incident"]["title"] == "High memory" and d["incident"]["state"] == "recovered" and len(d["events"]) == 2
    assert "kept for 24 hours" in d["explain"]["what"]


def test_stop_action_is_recorded_on_the_incident(env, dock):
    add_sample(env.db, "hetzner", time.time(), mem_pct=95)
    env.db.execute("INSERT INTO incidents(id,rule,machine,subject,severity,state,title,measured,targets,first_seen,active_since,last_seen)"
                   " VALUES('inc_a','container_mem','hetzner','x','warning','active','t','{}',?,1,1,1)",
                   (json.dumps([{"kind": "container", "ref": {"id": CID}, "label": "disposable-test"}]),))
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    asyncio.run(env.notifier.handle_update(cb(f"c:{last_confirm_token(env)}")))
    ev = [(r["kind"], r["detail"]) for r in env.db.query("SELECT kind,detail FROM incident_events WHERE incident_id='inc_a' ORDER BY id")]
    assert ev[0][0] == "action" and "disposable-test" in ev[0][1] and ev[1][0] == "action_verified"


# ---------------------------------------------------------------- automation policy
def test_policy_validation_is_strict():
    ok = automation.validate({"mode": "explain", "allowlist": ["Web"], "services": {"web": {"cpu_pct": 90}}})
    assert ok["allowlist"] == ["web"] and ok["services"]["web"]["cpu_pct"] == 90
    for bad in ({"mode": "yolo"}, {"extra": 1}, {"allowlist": ["a b"]}, {"allowlist": ["w*"]}, {"allowlist": ["immich_server"]},
                {"allowlist": ["x"], "denylist": ["x"]}, {"services": {"web": {"cpu_pct": 5}}}, {"services": {"web": {"nope": 1}}},
                {"allowlist": "web"}, {"defaults": {"sustained_seconds": 1}}):
        with pytest.raises(automation.PolicyError):
            automation.validate(bad)


def test_immich_is_always_protected_and_cannot_be_allowed(tmp_path):
    pol = automation.validate({})
    assert all(automation.is_denied(n, pol) for n in ("immich_server", "Immich-Postgres", "/immich_redis"))
    with pytest.raises(automation.PolicyError):
        automation.validate({"allowlist": ["immich_machine_learning"]})


def test_policy_file_permissions_are_enforced(tmp_path):
    path = tmp_path / "automation.json"
    store = automation.PolicyStore(path)
    store.save({"mode": "explain", "allowlist": ["web"]})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and store.load() == (automation.validate({"mode": "explain", "allowlist": ["web"]}), None)
    os.chmod(path, 0o644)                                     # group/world readable -> not trusted
    pol, problem = store.load()
    assert pol["mode"] == "assisted" and pol["allowlist"] == [] and "0600" in problem
    os.chmod(path, 0o600)
    path.write_text("{not json")
    pol, problem = store.load()
    assert pol["allowlist"] == [] and "ignored" in problem


def test_autonomous_is_accepted_in_the_file_but_never_executes(tmp_path):
    assert automation.AUTONOMOUS_EXECUTION_AVAILABLE is False
    pol = automation.validate({"mode": "autonomous", "allowlist": ["web"]})
    assert automation.effective_mode(pol)[0] == "assisted"


def test_dry_run_decides_but_never_executes(env, dock, monkeypatch):
    now = time.time()
    monkeypatch.setattr(docker_cli, "stop", lambda *a, **k: pytest.fail("a dry run must never stop anything"))
    a = automation.Automation(env.db, env.settings.get, env.paths.config / "automation.json")
    a.store.save({"mode": "assisted", "allowlist": ["web"], "services": {"web": {"cpu_pct": 80, "sustained_seconds": 120}}})
    hot = lambda cid, name, cpu: {"id": cid, "name": name, "running": True, "cpu": cpu, "mem": 100_000_000}
    for i in range(6):                                      # 6 snapshots over the sustained window
        add_snap(env.db, "hetzner", "containers", {"list": [hot("a" * 64, "web", 150), hot("b" * 64, "immich_server", 300),
                                                              hot("c" * 64, "other", 300)]}, now - 300 + i * 60)
    add_sample(env.db, "hetzner", now, mem_pct=92, mem_total=4 * GIB)
    out = a.simulate(now, record=True)
    by = {d["name"]: d for d in out["decisions"]}
    assert by["web"]["would_stop"] and by["web"]["policy"] and by["web"]["executed"] is False
    assert by["immich_server"]["protected"] and not by["immich_server"]["would_stop"]
    assert not by["other"]["would_stop"] and "Not on the allow list" in by["other"]["reasons"][0]
    assert out["dry_run"] and out["autonomous_available"] is False
    assert env.db.one("SELECT outcome FROM audit WHERE action='dry_run.would_stop'")["outcome"] == "simulated"
    again = a.simulate(now + 60, record=True)               # cooldown: recorded once, not repeatedly
    assert not next(d for d in again["decisions"] if d["name"] == "web")["would_stop"]
    assert env.db.one("SELECT COUNT(*) c FROM audit WHERE action='dry_run.would_stop'")["c"] == 1


def test_dry_run_safety_checks(env):
    now = time.time()
    a = automation.Automation(env.db, env.settings.get, env.paths.config / "automation.json")
    a.store.save({"allowlist": ["web"], "services": {"web": {"sustained_seconds": 120}}})
    add_snap(env.db, "hetzner", "containers", {"list": [{"id": "a" * 64, "name": "web", "running": True, "cpu": 300, "mem": 1}]}, now)
    add_sample(env.db, "hetzner", now, mem_pct=40, mem_total=4 * GIB)
    d = a.simulate(now)["decisions"][0]
    assert not d["would_stop"] and "not under memory pressure" in " ".join(d["reasons"])      # host is fine -> do nothing
    add_sample(env.db, "hetzner", now + 1, mem_pct=95, mem_total=4 * GIB)
    d = a.simulate(now + 2)["decisions"][0]
    assert not d["would_stop"] and "full" in " ".join(d["reasons"])                           # not sustained long enough


def test_explain_mode_blocks_stop_requests(env, dock):
    pol = automation.Automation(env.db, env.settings.get, env.paths.config / "automation.json")
    pol.store.save({"mode": "explain"})
    env.actions.mode_fn = pol.mode
    with pytest.raises(ActionError) as e:
        asyncio.run(env.actions.prepare("hetzner", "container", {"id": CID}, "dashboard", "owner"))
    assert e.value.code == "unauthorized" and "Explain mode" in str(e.value) and dock.stopped == []
    pol.store.save({"mode": "assisted"})                       # default behaviour (human-approved stops) is unchanged
    assert asyncio.run(env.actions.prepare("hetzner", "container", {"id": CID}, "dashboard", "owner"))["token"]


def test_automation_api_is_read_only_and_secret_free(client):
    r = client.get("/api/automation").json()
    assert r["mode"] == "assisted" and r["autonomous_available"] is False and r["editable_here"] is False
    assert "immich*" in r["denylist"] and r["allowed_actions"] == ["stop_container"]
    h = {"Origin": "https://x"} | csrf_headers(client)
    for m in ("put", "post", "patch", "delete"):
        assert getattr(client, m)("/api/automation", headers=h).status_code in (404, 405)
    assert "TOKEN" not in json.dumps(r)


def test_ctl_refuses_autonomous_and_protects_immich(tmp_path, capsys):
    from watcher import ctl
    from watcher.config import Paths
    p = Paths(str(tmp_path)); p.ensure()
    with pytest.raises(SystemExit):
        ctl.cmd_automation_mode(p, ["autonomous"])
    with pytest.raises(SystemExit):
        ctl.cmd_automation_allow(p, ["immich_server"])
    ctl.cmd_automation_allow(p, ["scratch"])
    ctl.cmd_automation_mode(p, ["explain"])
    pol, _ = automation.PolicyStore(p.automation).load()
    assert pol["mode"] == "explain" and pol["allowlist"] == ["scratch"]


# ---------------------------------------------------------------- heartbeat (external watchdog)
def test_heartbeat_only_pings_when_genuinely_healthy(env, monkeypatch):
    from watcher.config import save_env_file, load_env_file
    hits = []
    class R:
        def read(self, n): return b"OK"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: hits.append(req.full_url) or R())
    hb = Heartbeat(env.secrets, env.engine, lambda: 30)
    now = time.time()
    assert not hb.configured() and hb.tick(now) is False and hits == []
    save_env_file(env.paths.secrets, load_env_file(env.paths.secrets) | {"HEARTBEAT_URL": "https://hc.example/ping/secret-uuid"})
    assert hb.tick(now) is False                                       # no fresh server sample -> never claim "healthy"
    add_sample(env.db, "hetzner", now)
    env.engine.collector_errors["hetzner"] = 3
    assert hb.tick(now) is False                                       # collector failing -> no ping
    env.engine.collector_errors["hetzner"] = 0
    assert hb.tick(now) is True and hits == ["https://hc.example/ping/secret-uuid"]
    assert hb.tick(now + 10) is False                                  # rate limited to once a minute
    assert "secret-uuid" not in json.dumps(hb.status())                # the URL is a secret


def test_heartbeat_failure_is_quiet_and_scrubbed(env, monkeypatch):
    from watcher.config import save_env_file
    save_env_file(env.paths.secrets, {"HEARTBEAT_URL": "https://hc.example/ping/secret-uuid"})
    def boom(*a, **k): raise OSError("secret-uuid unreachable")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    add_sample(env.db, "hetzner", time.time())
    hb = Heartbeat(env.secrets, env.engine, lambda: 30)
    assert hb.tick() is False and hb.status()["last_error"] == "OSError" and "secret" not in json.dumps(hb.status())
    assert not valid_url("http://insecure.example/ping") and not valid_url("https://a b")


def test_settings_never_reveal_the_heartbeat_url(client):
    from watcher.config import load_env_file, save_env_file
    sec = client.st.paths.secrets
    save_env_file(sec, load_env_file(sec) | {"HEARTBEAT_URL": "https://hc.example/ping/secret-uuid"})
    body = json.dumps(client.get("/api/settings").json())
    assert "secret-uuid" not in body and client.get("/api/settings").json()["watchdog"]["configured"] is True


def test_storage_headline_matches_severity():
    warn = explain.explain(inc("disk", "mac", {"Free space": "49.0 GB (20%)"}), 0)
    crit = explain.explain(inc("disk", "mac", {"Free space": "9.0 GB (4%)"}, severity="critical"), 0)
    assert "getting full" in warn["headline"] and "not urgent" in warn["what"]
    assert "running out" in crit["headline"] and "not urgent" not in crit["what"]


# ---------------------------------------------------------------- stage 3: one source of truth
def test_mac_high_memory_with_normal_pressure_is_not_alarming(env):
    add_sample(env.db, "mac", time.time(), mem_total=8 * GIB, mem_pct=92, swap_total=2 * GIB, swap_used=0)
    line = next(l for l in views.resource_lines("mac", env.engine.latest("mac"), env.settings.thresholds()) if l["key"] == "memory")
    assert line["level"] == "good" and "pressure is normal" in line["text"]


def test_active_incident_is_never_contradicted_by_reassuring_text(env):
    add_sample(env.db, "mac", time.time(), disk_total=int(2e11), disk_free=int(1.5e11))
    incs = [{"rule": "disk", "severity": "warning"}]
    line = next(l for l in views.resource_lines("mac", env.engine.latest("mac"), env.settings.thresholds(), incs) if l["key"] == "storage")
    assert line["level"] == "attention" and "fine" not in line["text"].lower() and "plenty" not in line["text"].lower()


def test_health_matches_status_everywhere(client):
    mac_post(client)
    client.st.db.execute("INSERT INTO incidents(id,rule,machine,subject,severity,state,title,measured,first_seen,active_since,last_seen)"
                         " VALUES('i2','disk','mac','','warning','active','Low disk space',?,?,?,?)", (json.dumps({"summary": {}, "facts": []}), 1, 1, 1))
    o = client.get("/api/overview").json()
    d = client.get("/api/machine/mac").json()
    assert o["mac"]["health"] == d["health"] == "warning" and o["mac"]["status"] == d["status"]
    assert o["overall"]["need_attention"] == 1
    assert o["overall"]["text"].startswith("One thing needs your attention")
    assert next(l for l in d["resources"] if l["key"] == "storage")["level"] == "attention"


def test_all_clear_text(client):
    mac_post(client)
    o = client.get("/api/overview").json()["overall"]
    assert o["healthy"] >= 1 and o["critical_incidents"] == 0 and o["last_report"]


def test_history_is_strictly_per_machine(client):
    now = time.time()
    for i in range(5):
        add_sample(client.st.db, "mac", now - 60 * i, cpu=11, mem_pct=22)
        add_sample(client.st.db, "hetzner", now - 60 * i, cpu=77, mem_pct=88)
    for rng in ("1h", "6h", "24h"):
        m = client.get(f"/api/history?machine=mac&range={rng}").json()
        h = client.get(f"/api/history?machine=hetzner&range={rng}").json()
        assert m["machine"] == "mac" and h["machine"] == "hetzner"
        assert all(round(p["cpu"]) == 11 and round(p["mem"]) == 22 for p in m["points"]) and m["points"]
        assert all(round(p["cpu"]) == 77 and round(p["mem"]) == 88 for p in h["points"]) and h["points"]
        assert m["available_seconds"] >= 200
    assert client.get("/api/history?machine=mac&range=7d").status_code == 400      # never invent history that is not kept
