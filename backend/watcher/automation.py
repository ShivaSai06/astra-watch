"""Automation policy: what Astra Watch may do, and a dry-run simulator.

This stage ships the *policy model and dry-run only*. Nothing in this module can stop, restart or delete
anything: `simulate()` returns decisions with executed=False and writes an audit line, nothing more, and
AUTONOMOUS_EXECUTION_AVAILABLE is a code constant (not a setting), so no config edit can turn it on.

Modes
  explain     investigate and recommend only (Stop buttons are refused)
  assisted    recommend and wait for a human to confirm (the behaviour before this feature; the default)
  autonomous  reserved. Accepted in the file format, but downgraded to 'assisted' while execution is unavailable.

The policy file is owner-only (0600, owned by the service user) and is edited with `watcherctl automation-*`.
The dashboard can only read it. If the file is group/world accessible, owned by someone else or malformed,
it is ignored and the safe default is used.
"""
from __future__ import annotations

import copy
import fnmatch
import json
import os
import re
import time
from pathlib import Path

from .config import write_private

AUTONOMOUS_EXECUTION_AVAILABLE = False      # code constant on purpose: not configurable
MODES = ("explain", "assisted", "autonomous")
NAME_RE = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9_.*-]{0,63}$")
BUILTIN_PROTECTED = ("immich*",)             # cannot be removed or overridden
ACTIONS_ALLOWED = ("stop_container",)         # no delete / prune / rollback exists in this module

DEFAULT_LIMITS = {"cpu_pct": 80.0, "mem_host_pct": 40.0, "sustained_seconds": 600, "cooldown_minutes": 60, "max_attempts": 2}
LIMIT_BOUNDS = {"cpu_pct": (20, 400), "mem_host_pct": (5, 95), "sustained_seconds": (120, 3600),
                "cooldown_minutes": (10, 1440), "max_attempts": (1, 5)}

DEFAULT_POLICY: dict = {
    "mode": "assisted", "allowlist": [], "denylist": [], "defaults": dict(DEFAULT_LIMITS), "services": {},
}


class PolicyError(ValueError):
    pass


def validate(data: dict) -> dict:
    """Return a cleaned copy or raise PolicyError. Strict: unknown keys are rejected."""
    if not isinstance(data, dict):
        raise PolicyError("policy must be an object")
    unknown = set(data) - set(DEFAULT_POLICY)
    if unknown:
        raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
    out = copy.deepcopy(DEFAULT_POLICY)
    mode = data.get("mode", "assisted")
    if mode not in MODES:
        raise PolicyError("mode must be explain, assisted or autonomous")
    out["mode"] = mode
    for key in ("allowlist", "denylist"):
        vals = data.get(key, [])
        if not isinstance(vals, list) or len(vals) > 100:
            raise PolicyError(f"{key} must be a list of at most 100 names")
        for v in vals:
            if not isinstance(v, str) or not NAME_RE.match(v):
                raise PolicyError(f"invalid name in {key}: {str(v)[:40]!r}")
        out[key] = sorted(set(v.lower() for v in vals))
    out["defaults"] = _limits(data.get("defaults", {}), DEFAULT_LIMITS)
    svc = data.get("services", {})
    if not isinstance(svc, dict) or len(svc) > 100:
        raise PolicyError("services must be an object")
    for name, lim in svc.items():
        if not isinstance(name, str) or not NAME_RE.match(name) or "*" in name:
            raise PolicyError(f"invalid service name {str(name)[:40]!r}")
        out["services"][name.lower()] = _limits(lim, out["defaults"])
    for a in out["allowlist"]:
        if "*" in a:
            raise PolicyError("allowlist entries must be exact container names (no wildcards)")
        if is_denied(a, out):
            raise PolicyError(f"{a} is protected (deny list or built-in protection) and cannot be allow-listed")
    return out


def _limits(lim, base: dict) -> dict:
    if not isinstance(lim, dict):
        raise PolicyError("limits must be an object")
    out = dict(base)
    for k, v in lim.items():
        if k not in LIMIT_BOUNDS:
            raise PolicyError(f"unknown limit {k}")
        lo, hi = LIMIT_BOUNDS[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise PolicyError(f"{k} must be between {lo} and {hi}")
        out[k] = v
    return out


def is_denied(name: str, policy: dict) -> bool:
    n = name.lower().lstrip("/")
    return any(fnmatch.fnmatch(n, p) for p in (*BUILTIN_PROTECTED, *policy.get("denylist", [])))


def check_file_permissions(path: Path) -> str | None:
    """None if the policy file is safe to trust, else a reason."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if st.st_uid != os.getuid():
        return "owned by a different user"
    if st.st_mode & 0o077:
        return "readable or writable by group/others (must be 0600)"
    return None


class PolicyStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> tuple[dict, str | None]:
        """(policy, problem). On any problem the safe default is returned and `problem` explains why."""
        bad = check_file_permissions(self.path)
        if bad:
            return validate({}), f"Policy file ignored: {bad}"
        if not self.path.exists():
            return validate({}), None
        try:
            return validate(json.loads(self.path.read_text())), None
        except (OSError, ValueError) as e:
            return validate({}), f"Policy file ignored: {e}"

    def save(self, policy: dict) -> dict:
        clean = validate(policy)
        write_private(self.path, json.dumps(clean, indent=2, sort_keys=True))
        return clean


def effective_mode(policy: dict) -> tuple[str, str]:
    mode = policy["mode"]
    if mode == "autonomous" and not AUTONOMOUS_EXECUTION_AVAILABLE:
        return "assisted", "Autonomous mode is not available in this version, so Astra Watch behaves as Assisted."
    return mode, ""


def limits_for(name: str, policy: dict) -> dict:
    return policy["services"].get(name.lower(), policy["defaults"])


# ---------------------------------------------------------------------------- dry run
def decide(c: dict, policy: dict, host_mem_pct: float, mem_total: int, sustained_ok, cooldown_ok: bool) -> dict:
    """One container's dry-run decision. Never executes anything."""
    name = c["name"]
    lim = limits_for(name, policy)
    reasons: list[str] = []
    protected = is_denied(name, policy)
    allowed = name.lower() in policy["allowlist"]
    mem_pct = 100.0 * c.get("mem", 0) / mem_total if mem_total else 0
    over_cpu, over_mem = c.get("cpu", 0) >= lim["cpu_pct"], mem_pct >= lim["mem_host_pct"]
    d = {"name": name, "id": c["id"], "protected": protected, "allowlisted": allowed, "cpu": round(c.get("cpu", 0)),
         "mem_host_pct": round(mem_pct, 1), "over_threshold": bool(over_cpu or over_mem), "would_stop": False,
         "executed": False, "reasons": reasons, "policy": None}
    if protected:
        reasons.append("Protected: on the deny list, never stopped automatically.")
        return d
    if not allowed:
        reasons.append("Not on the allow list, so Astra Watch would only recommend.")
        return d
    if not (over_cpu or over_mem):
        reasons.append("Within its CPU and memory limits.")
        return d
    reasons.append("Over its limit now: " + ", ".join(
        x for x in (f"CPU {c.get('cpu', 0):.0f}% ≥ {lim['cpu_pct']:.0f}%" if over_cpu else "",
                    f"memory {mem_pct:.0f}% of host ≥ {lim['mem_host_pct']:.0f}%" if over_mem else "") if x))
    if host_mem_pct < 85:
        reasons.append("Host is not under memory pressure, so stopping it would not help (safety check).")
        return d
    if not sustained_ok:
        reasons.append(f"Not over the limit for the full {lim['sustained_seconds']:.0f}s required.")
        return d
    if not cooldown_ok:
        reasons.append("In cooldown after a previous simulated action.")
        return d
    d["would_stop"] = True
    d["policy"] = f"allow list + limits ({lim['cpu_pct']:.0f}% CPU / {lim['mem_host_pct']:.0f}% memory for {lim['sustained_seconds']:.0f}s)"
    reasons.append("All checks passed: in Autonomous mode this container would be stopped gracefully (dry run: nothing was done).")
    return d


class Automation:
    def __init__(self, db, settings_fn, policy_path: Path):
        self.db, self.store = db, PolicyStore(policy_path)
        self.settings_fn = settings_fn

    def policy(self) -> tuple[dict, str | None]:
        return self.store.load()

    def mode(self) -> str:
        return effective_mode(self.policy()[0])[0]

    def _sustained(self, c_id: str, lim: dict, cpu_attr: str, now: float) -> bool:
        window = lim["sustained_seconds"]
        rows = self.db.query("SELECT data FROM snapshots WHERE machine='hetzner' AND kind='containers' AND ts>=? ORDER BY ts",
                             (now - window,))
        if len(rows) < max(2, window // 90):                       # not enough history to prove it
            return False
        for r in rows:
            c = next((x for x in json.loads(r["data"])["list"] if x["id"] == c_id), None)
            if not c or not c.get("running"):
                return False
        return True

    def simulate(self, now: float | None = None, record: bool = False) -> dict:
        now = now or time.time()
        policy, problem = self.policy()
        mode, note = effective_mode(policy)
        snap = self.db.one("SELECT ts,data FROM snapshots WHERE machine='hetzner' AND kind='containers' ORDER BY ts DESC LIMIT 1")
        host = self.db.one("SELECT mem_pct,mem_total FROM samples WHERE machine='hetzner' ORDER BY ts DESC LIMIT 1")
        decisions: list[dict] = []
        if snap and host and now - snap["ts"] < 600:
            for c in json.loads(snap["data"])["list"]:
                if not c.get("running"):
                    continue
                lim = limits_for(c["name"], policy)
                breach = (c.get("cpu", 0) >= lim["cpu_pct"] or 100.0 * c.get("mem", 0) / max(host["mem_total"], 1) >= lim["mem_host_pct"])
                allowed = c["name"].lower() in policy["allowlist"] and not is_denied(c["name"], policy)
                sustained = self._sustained(c["id"], lim, "", now) if (allowed and breach) else False
                last = self.db.kv_get(f"dryrun:{c['name']}", 0)
                cool = now - last >= lim["cooldown_minutes"] * 60
                d = decide(c, policy, host["mem_pct"] or 0, host["mem_total"], sustained, cool)
                decisions.append(d)
                if record and d["would_stop"]:
                    self.db.kv_set(f"dryrun:{c['name']}", now)
                    self.db.audit("automation", "dry_run.would_stop", "hetzner", c["name"], "simulated",
                                  "; ".join(d["reasons"])[:400])
        order = {True: 0, False: 1}
        decisions.sort(key=lambda d: (order[d["would_stop"]], not d["over_threshold"], d["name"]))
        return {"mode": mode, "requested_mode": policy["mode"], "mode_note": note, "problem": problem,
                "autonomous_available": AUTONOMOUS_EXECUTION_AVAILABLE, "dry_run": True,
                "allowlist": policy["allowlist"], "denylist": sorted(set(policy["denylist"]) | set(BUILTIN_PROTECTED)),
                "builtin_protected": list(BUILTIN_PROTECTED), "defaults": policy["defaults"], "services": policy["services"],
                "allowed_actions": list(ACTIONS_ALLOWED), "decisions": decisions}

    def tick(self, now: float | None = None) -> None:
        """Cheap: does nothing unless something is on the allow list."""
        policy, problem = self.policy()
        if problem or not policy["allowlist"]:
            return
        self.simulate(now, record=True)
