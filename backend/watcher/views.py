"""Read-only view models for the dashboard: plain-English health, resource lines and incident history.

Everything here reads rows that already exist (SQLite). Nothing collects, scans or sends.
Health (is the machine OK?) and connection (is it reporting?) are deliberately separate.
"""
from __future__ import annotations

import json

from . import explain
from .engine import mem

LEVELS = {"good": "Good", "attention": "Needs attention", "critical": "Critical", "offline": "Offline", "unknown": "Not reporting"}


def parse_incident(row) -> dict:
    d = dict(row)
    d["measured"] = json.loads(d.get("measured") or "{}")
    d["targets"] = json.loads(d.get("targets") or "[]")
    return d


def machine_status(db, machine: str, connection: str, now: float) -> dict:
    """{level,label,headline,watching}: connection problems win (old data is never 'Good'), then incidents."""
    rows = active_incidents(db, machine)
    watching = db.one("SELECT COUNT(*) c FROM pending WHERE key LIKE ?", (f"%|{machine}|%",))["c"]
    name = "Your Mac" if machine == "mac" else "The server"
    if connection == "unknown":
        return {"level": "unknown", "label": LEVELS["unknown"], "headline": f"{name} has not reported yet.", "watching": 0}
    if connection == "offline":
        why = "It may be asleep or shut down." if machine == "mac" else "The collector has stopped reporting."
        return {"level": "offline", "label": LEVELS["offline"], "headline": f"{name} is offline. {why} Values shown are the last received, not current.", "watching": 0}
    if connection == "stale":
        return {"level": "unknown", "label": LEVELS["unknown"], "headline": f"{name} has missed its recent reports. Values shown are the last received, not current.", "watching": 0}
    if rows:
        crit = any(r["severity"] == "critical" for r in rows)
        head = explain.explain(rows[0], now)["headline"]
        more = f" (+{len(rows) - 1} more)" if len(rows) > 1 else ""
        return {"level": "critical" if crit else "attention", "label": LEVELS["critical" if crit else "attention"],
                "headline": head + more, "watching": watching}
    if watching:
        return {"level": "good", "label": LEVELS["good"],
                "headline": f"{name} looks fine. Astra Watch is keeping an eye on {watching} possible issue{'s' if watching != 1 else ''}.",
                "watching": watching}
    return {"level": "good", "label": LEVELS["good"], "headline": f"{name} looks fine. Nothing needs your attention.", "watching": 0}


def overall(statuses: dict, attention_items: list | None = None, counts: dict | None = None) -> dict:
    order = ["critical", "attention", "offline", "unknown", "good"]
    levels = [s["level"] for s in statuses.values()]
    worst = next(l for l in order if l in levels)
    bad = [m for m, s in statuses.items() if s["level"] in ("critical", "attention")]
    names = {"mac": "Your Mac", "hetzner": "Your server"}
    items = attention_items or []
    if worst == "good":
        text = "Everything is running normally."
        if any(s.get("watching") for s in statuses.values()):
            text += " Astra Watch is keeping an eye on a possible issue."
    elif bad:
        n = len(items) or len(bad)
        lead = "One thing needs your attention." if n == 1 else f"{n} things need your attention."
        text = f"{lead} {items[0]['headline']}." if items else f"{lead} {' and '.join(names[m] for m in bad)}."
        if len(items) > 1:
            text = text.rstrip(".") + f", and {len(items) - 1} more."
        text = text.replace("..", ".")
    elif worst == "offline":
        text = "Everything that is reporting looks fine, but a machine is offline."
    else:
        text = "Some machines have not reported recently, so their health is not known right now."
    label = {"good": "All good", "attention": "Needs attention", "critical": "Critical", "offline": "Offline", "unknown": "Not reporting"}[worst]
    return {"level": worst, "label": label, "text": text, **(counts or {})}


_RANK = {"unknown": 0, "good": 1, "attention": 2, "critical": 3}
_RULE_KEY = {"memory": "memory", "cpu": "cpu", "disk": "storage", "disk_growth": "storage", "swap_full": "swap", "swap_growth": "swap"}


def _lvl(pct: float | None, warn: float, crit: float) -> str:
    if pct is None:
        return "unknown"
    return "critical" if pct >= crit else "attention" if pct >= warn else "good"


def active_incidents(db, machine: str) -> list[dict]:
    return [parse_incident(r) for r in db.query(
        "SELECT * FROM incidents WHERE state='active' AND machine=? ORDER BY (severity='critical') DESC, first_seen", (machine,))]


def resource_lines(machine: str, s: dict | None, t: dict, incs: list[dict] | None = None) -> list[dict]:
    """Four plain-English lines: memory, processor, storage, overflow memory.

    Levels use the SAME rules as the alert engine (macOS memory uses pressure, Linux does not), and an active
    incident for a resource can only raise its level, never be contradicted by reassuring text.
    """
    if not s:
        return []
    ex = s.get("extra") or {}
    mac = machine == "mac"
    out: list[dict] = []
    pct = s["mem_pct"] or 0
    used, total = mem(s["mem_used"]), mem(s["mem_total"])
    swap = s["swap_used"] or 0
    if mac:
        pl = ex.get("pressure_level", 1)
        pname = {1: "normal", 2: "elevated", 4: "critical"}.get(pl, "normal")
        lvl = "critical" if (pct >= t["mem_crit"] or pl >= 4) else "attention" if (pct >= t["mem_warn"] and (pl >= 2 or swap >= 1.5 * 1024 ** 3)) else "good"
        if lvl == "good":
            note = f"Memory pressure is {pname}." + (" High use is normal for macOS, which keeps memory busy on purpose." if pct >= t["mem_warn"] else "")
        else:
            note = f"Memory pressure is {pname}. " + ("The Mac may start to slow down." if lvl == "attention" else "Expect slowdowns.")
    else:
        lvl = _lvl(pct, t["mem_warn"], t["mem_crit"])
        avail = ex.get("mem_avail")
        note = (f"{mem(avail)} is still available. " if avail else "") + {"good": "Memory is fine.", "attention": "Memory is getting tight.",
                                                                          "critical": "Memory is almost full; programs may be killed."}[lvl]
    out.append({"key": "memory", "label": "Memory", "level": lvl, "pct": pct, "value": f"{used} of {total} used", "text": note.strip()})

    cpu = s["cpu"] or 0
    out.append({"key": "cpu", "label": "Processor", "level": "good", "pct": cpu, "value": f"{cpu:.0f}% busy",
                "text": "Processor usage is normal." if cpu < t["cpu_warn"] else "Busy right now. It only counts as a problem if it stays this high for several minutes."})

    if s["disk_total"]:
        free_pct = 100.0 * s["disk_free"] / s["disk_total"]
        lvl = "critical" if free_pct < t["disk_free_crit"] else "attention" if free_pct < t["disk_free_warn"] else "good"
        out.append({"key": "storage", "label": "Storage", "level": lvl, "pct": 100 - free_pct,
                    "value": f"{s['disk_free'] / 1e9:.0f} GB available of {s['disk_total'] / 1e9:.0f} GB",
                    "text": {"good": "Storage is fine.", "attention": "Storage is getting low. Free some space soon.",
                             "critical": "Storage is almost full. Apps may start failing."}[lvl]})
    if s["swap_total"]:
        sp = 100.0 * s["swap_used"] / s["swap_total"]
        lvl = "critical" if sp >= t["swap_used_crit"] else "good"
        out.append({"key": "swap", "label": "Overflow memory (swap)", "level": lvl, "pct": sp,
                    "value": f"{mem(s['swap_used'])} of {mem(s['swap_total'])} used",
                    "text": ("This alone is not a problem." if swap else "None in use.") if lvl == "good"
                    else "Almost full. The system may freeze or close programs."})
    for inc in incs or []:                       # active problems can only raise a line, never be contradicted
        key = _RULE_KEY.get(inc["rule"])
        sev = "critical" if inc["severity"] == "critical" else "attention"
        for r in out:
            if r["key"] == key and _RANK[sev] > _RANK[r["level"]]:
                r["level"] = sev
                r["text"] = {"cpu": "Sustained high usage is slowing the machine down.",
                             "swap": "Swap keeps growing, which means memory demand is above what the machine has.",
                             "storage": "Storage is getting low. Free some space soon.",
                             "memory": "Memory is under strain."}[key]
    return out


def attention(db, now: float, limit: int = 6) -> list[dict]:
    """Active problems with a one-line plain-English summary, worst first."""
    rows = [parse_incident(r) for r in db.query(
        "SELECT * FROM incidents WHERE state='active' ORDER BY (severity='critical') DESC, first_seen DESC LIMIT ?", (limit,))]
    out = []
    for r in rows:
        ex = explain.explain(r, now)
        out.append({"id": r["id"], "machine": r["machine"], "severity": r["severity"], "headline": ex["headline"],
                    "what": ex["what"], "since": r["active_since"] or r["first_seen"]})
    return out


# ---------------------------------------------------------------------------- activity (30-day history)
def activity_list(db, limit: int = 100) -> list[dict]:
    rows = db.query(
        "SELECT incident_id, MIN(ts) AS first_ts, MAX(ts) AS last_ts, SUM(kind='recovered') AS rec, COUNT(*) AS n "
        "FROM incident_events GROUP BY incident_id ORDER BY first_ts DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        last = db.one("SELECT machine,rule,severity,title FROM incident_events WHERE incident_id=? ORDER BY ts DESC, id DESC LIMIT 1",
                      (r["incident_id"],))
        inc = db.one("SELECT state,severity,title FROM incidents WHERE id=?", (r["incident_id"],))
        state = "recovered" if r["rec"] else ("active" if inc and inc["state"] == "active" else "ended")
        out.append({"id": r["incident_id"], "machine": last["machine"], "rule": last["rule"],
                    "severity": (inc["severity"] if inc else last["severity"]), "title": (inc["title"] if inc else last["title"]),
                    "state": state, "first_ts": r["first_ts"], "last_ts": r["last_ts"], "events": r["n"]})
    return out


STAGES = ["detected", "investigated", "recommended", "action", "verified", "recovered"]


def activity_detail(db, incident_id: str, now: float) -> dict | None:
    events = [dict(e) for e in db.query("SELECT ts,kind,detail FROM incident_events WHERE incident_id=? ORDER BY ts, id", (incident_id,))]
    row = db.one("SELECT * FROM incidents WHERE id=?", (incident_id,))
    if not row and not events:
        return None
    if row:
        inc = parse_incident(row)
        actions = [e["detail"].replace("You approved ", "") for e in events if e["kind"] == "action"]
        ex = explain.explain(inc, now, actions or None)
    else:                                   # incident row aged out (24 h) but the 30-day history remains
        e0 = db.one("SELECT machine,rule,severity,title FROM incident_events WHERE incident_id=? ORDER BY id DESC LIMIT 1", (incident_id,))
        inc = {"id": incident_id, **dict(e0), "state": "recovered" if any(e["kind"] == "recovered" for e in events) else "ended",
               "targets": [], "measured": {}, "first_seen": events[0]["ts"], "active_since": events[0]["ts"], "recovered_at": None}
        ex = {"headline": inc["title"], "what": "This is an older incident. The detailed measurements are kept for 24 hours; the timeline is kept for 30 days.",
              "why": "", "cause": None, "steps": [], "verify": [], "glossary": [], "measurements": [], "automatic_action": explain.AUTO_NONE,
              "duration_seconds": int((events[-1]["ts"] - events[0]["ts"])), "severity": inc["severity"], "machine": inc["machine"], "rule": inc["rule"],
              "id": incident_id, "state": inc["state"]}
    reached = {e["kind"] for e in events}
    reached |= {"action"} if "action_verified" in reached else set()
    return {"incident": {k: inc.get(k) for k in ("id", "machine", "rule", "severity", "state", "title", "first_seen", "active_since",
                                                    "recovered_at", "notify_state", "notified_at", "recovery_notified", "targets")},
            "explain": ex, "events": events,
            "stages": [{"key": k, "done": k in reached, "skipped": k == "action" and k not in reached} for k in STAGES]}
