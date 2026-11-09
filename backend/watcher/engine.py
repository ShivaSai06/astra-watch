"""Problem-detection engine.

Rules look at the latest data and return *findings* ("this is wrong right now").
The lifecycle layer turns findings into incidents using duration-based persistence:

    finding appears -> pending (timer starts)
    pending for >= persist seconds -> active incident (this is what gets a notification)
    finding absent for >= recover seconds -> recovered

So a single-sample spike never alerts. Facts (measured numbers) and inferences (likely cause +
confidence) are kept in separate fields so the UI can label them honestly.
"""
from __future__ import annotations

import json
import secrets
import socket
import time
from dataclasses import dataclass, field

from .db import DB

RANK = {"warning": 1, "critical": 2}
LABEL_TEXT = {
    "vitest": "Vitest test workers", "jest": "Jest test workers", "node": "Node.js processes",
    "java": "Java processes", "python": "Python processes", "maven": "Maven builds", "gradle": "Gradle builds",
    "docker-vm": "the Docker Desktop virtual machine", "claude": "Claude Code", "vite": "Vite/dev servers",
    "build": "build tools", "browser": "browser processes", "other": "other processes",
}
GB = 1e9


GIB = 1024 ** 3


def mem(b) -> str:
    """Memory/swap/RSS in binary units, shown as 'GB' like Activity Monitor (8 GiB RAM -> '8 GB')."""
    v = (b or 0) / GIB
    return f"{v:.0f} GB" if v >= 1 and abs(v - round(v)) < 0.05 else f"{v:.1f} GB"


def _cap(s: str) -> str:
    """Upper-case the first letter only (str.capitalize() would turn 'Claude Code' into 'Claude code')."""
    return s[:1].upper() + s[1:]


def gb(b) -> str:
    """Disk / storage sizes in decimal units, like Finder and `df -H`."""
    return f"{(b or 0) / GB:.1f} GB"


@dataclass
class Finding:
    rule: str
    machine: str
    severity: str
    title: str
    subject: str = ""
    values: dict = field(default_factory=dict)     # raw numbers
    summary: dict = field(default_factory=dict)    # label -> display string (used in Telegram)
    facts: list = field(default_factory=list)      # measured statements
    cause: str = ""                                # inferred
    confidence: str = ""                           # high | medium | low
    impact: str = ""
    recommendation: str = ""
    targets: list = field(default_factory=list)    # [{kind, ref, label}] eligible stop candidates
    persist: float | None = None                   # override persistence seconds

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.machine}|{self.subject}"


class Engine:
    def __init__(self, db: DB, settings, hub=None):
        self.db = db
        self.settings = settings
        self.hub = hub
        self.self_state: dict = {}          # filled by main loop: rss, cpu, db_size, collector_errors
        self.collector_errors: dict[str, int] = {"hetzner": 0}
        self.service_probe = self._probe_tcp

    # ---------------------------------------------------------------- data access
    def latest(self, machine: str) -> dict | None:
        r = self.db.one("SELECT * FROM samples WHERE machine=? ORDER BY ts DESC LIMIT 1", (machine,))
        if not r:
            return None
        d = dict(r)
        d["extra"] = json.loads(d["extra"]) if d["extra"] else {}
        return d

    def hist(self, machine: str, seconds: float, now: float | None = None) -> list[dict]:
        now = now or time.time()
        rows = self.db.query("SELECT ts,cpu,mem_pct,swap_used,swap_total,disk_free,disk_total FROM samples "
                             "WHERE machine=? AND ts>=? ORDER BY ts", (machine, now - seconds))
        return [dict(r) for r in rows]

    def snap(self, machine: str, kind: str, max_age: float = 300) -> dict | None:
        r = self.db.one("SELECT ts,data FROM snapshots WHERE machine=? AND kind=? ORDER BY ts DESC LIMIT 1",
                        (machine, kind))
        if not r or time.time() - r["ts"] > max_age:
            return None
        d = json.loads(r["data"])
        d["_ts"] = r["ts"]
        return d

    # ---------------------------------------------------------------- evaluation
    def evaluate(self, now: float | None = None) -> list[Finding]:
        now = now or time.time()
        t = self.settings.thresholds()
        out: list[Finding] = []
        for machine in ("mac", "hetzner"):
            s = self.latest(machine)
            fresh = s and now - s["ts"] < max(120, 4 * self.settings.get()["refresh_interval"])
            if machine == "mac":
                out += self.r_agent(s, now, t)
            if not fresh:
                continue
            out += self.r_memory(machine, s, t)
            out += self.r_cpu(machine, now, t)
            out += self.r_disk(machine, s, t)
            out += self.r_swap(machine, s, now, t)
            out += self.r_disk_growth(machine, now, t)
        out += self.r_hetzner_containers(t, now)
        out += self.r_hetzner_services()
        out += self.r_mac_dev(t)
        out += self.r_self(t)
        out += self.r_monitor_failure()
        return out

    # ---------------------------------------------------------------- contributors
    def _mac_groups(self):
        p = self.snap("mac", "procs")
        if not p:
            return None, []
        groups = sorted(p.get("groups", {}).items(), key=lambda kv: kv[1].get("rss", 0), reverse=True)
        return p, groups

    def _mac_targets(self, p, label=None, n=3) -> list[dict]:
        procs = {x["pid"]: x for k in ("dev", "top_mem", "top_cpu") for x in p.get(k, [])}
        cand = [x for x in procs.values() if x.get("eligible") and (label is None or x.get("label") == label)]
        cand.sort(key=lambda x: x.get("rss", 0), reverse=True)
        return [{"kind": "process", "ref": {"pid": x["pid"], "start": x["start"]},
                 "label": f"{x['name']} {mem(x.get('rss', 0))} (PID {x['pid']})"} for x in cand[:n]]

    def _mac_cause(self, total_mem: int):
        p, groups = self._mac_groups()
        if not p or not groups:
            return "", "", [], {}, []
        label, g = groups[0]
        share = g.get("rss", 0) / total_mem if total_mem else 0
        name = LABEL_TEXT.get(label, label)
        cause = f"{name} ({g.get('count', 0)} process{'es' if g.get('count', 0) != 1 else ''}, {mem(g.get('rss', 0))})"
        if label in ("vitest", "jest") and g.get("count", 0) > 1:
            cause = f"Several {name} are still running ({g.get('count')} workers, {mem(g.get('rss', 0))})"
            if g.get("orphans", 0):
                cause += f"; {g['orphans']} have no parent process left"
        conf = "high" if share >= 0.30 else "medium" if share >= 0.15 else "low"
        return (cause, conf, self._mac_targets(p, label if label != "docker-vm" else None),
                {"Main contributor": _cap(LABEL_TEXT.get(label, label).replace("the ", ""))},
                [f"{LABEL_TEXT.get(lb, lb)}: {mem(gg.get('rss', 0))} in {gg.get('count', 0)} process(es)"
                 for lb, gg in groups[:4]])

    def _hz_containers(self):
        c = self.snap("hetzner", "containers", max_age=600)
        return c

    def _hz_cause(self, mem_total: int, by="mem"):
        c = self._hz_containers()
        if not c:
            return "", "", [], {}, []
        rows = [x for x in c["list"] if x.get("running")]
        rows.sort(key=lambda x: x.get(by, 0), reverse=True)
        if not rows:
            return "", "", [], {}, []
        top = rows[0]
        if by == "mem":
            share = top.get("mem", 0) / mem_total if mem_total else 0
            cause = f"Container {top['name']} is using {mem(top.get('mem', 0))} ({share * 100:.0f}% of RAM)"
            conf = "high" if share >= 0.25 else "medium" if share >= 0.12 else "low"
            if conf == "low":
                cause = "No single container dominates; memory is spread across many containers"
        else:
            cause = f"Container {top['name']} is using {top.get('cpu', 0):.0f}% of a CPU core"
            conf = "high" if top.get("cpu", 0) >= 60 else "medium" if top.get("cpu", 0) >= 30 else "low"
        targets = [{"kind": "container", "ref": {"id": top["id"]},
                    "label": f"{top['name']} {mem(top.get('mem', 0)) if by == 'mem' else str(round(top.get('cpu', 0))) + '% CPU'}"}] \
            if conf != "low" else []
        facts = [f"{x['name']}: {mem(x.get('mem', 0))}, {x.get('cpu', 0):.0f}% CPU" for x in rows[:4]]
        return cause, conf, targets, {"Main contributor": top["name"]}, facts

    # ---------------------------------------------------------------- rules
    def r_memory(self, machine, s, t) -> list[Finding]:
        pct = s["mem_pct"] or 0
        ex = s["extra"]
        swap = s["swap_used"] or 0
        sev = None
        if machine == "mac":
            level = ex.get("pressure_level", 1)       # 1 normal, 2 warn, 4 critical
            if pct >= t["mem_crit"] or level >= 4:
                sev = "critical"
            elif pct >= t["mem_warn"] and (level >= 2 or swap >= 1.5 * GB):
                sev = "warning"
        else:
            if pct >= t["mem_crit"]:
                sev = "critical"
            elif pct >= t["mem_warn"]:
                sev = "warning"
        if not sev:
            return []
        f = Finding("memory", machine, sev, "High memory consumption detected")
        f.values = {"mem_pct": pct, "swap_used": swap}
        f.summary = {"Memory used": f"{pct:.0f}%", "Swap used": mem(swap)}
        if machine == "mac":
            f.summary["Memory pressure"] = {1: "normal", 2: "elevated", 4: "critical"}.get(ex.get("pressure_level", 1), "?")
            f.facts = [f"Used {mem(s['mem_used'])} of {mem(s['mem_total'])} (apps + system + compressed)",
                       f"Compressed memory {mem(ex.get('compressed', 0))}; swap {mem(swap)}"]
            cause, conf, targets, summ, more = self._mac_cause(s["mem_total"])
            f.impact = "Your Mac may become slow or unresponsive as it relies on swap."
            f.recommendation = "Inspect and stop unused development workers or containers."
        else:
            f.facts = [f"Used {mem(s['mem_used'])} of {mem(s['mem_total'])} (cache excluded; {mem(ex.get('mem_avail', 0))} available)",
                       f"Swap {mem(swap)} of {mem(s['swap_total'])}"]
            cause, conf, targets, summ, more = self._hz_cause(s["mem_total"])
            f.impact = "Containers may be slowed or killed by the kernel (out-of-memory) and new workloads may not start."
            f.recommendation = "Inspect container memory on the Hetzner page and stop what you do not need."
        f.cause, f.confidence, f.targets = cause, conf, targets
        f.summary.update(summ)
        f.facts += more
        return [f]

    def r_cpu(self, machine, now, t) -> list[Finding]:
        rows = self.hist(machine, 120, now)[-3:]
        if len(rows) < 2:
            return []
        avg = sum(r["cpu"] or 0 for r in rows) / len(rows)
        sev = "critical" if avg >= t["cpu_crit"] else "warning" if avg >= t["cpu_warn"] else None
        if not sev:
            return []
        f = Finding("cpu", machine, sev, "Sustained CPU overload", persist=self.settings.get()["cpu_persist_seconds"])
        f.values = {"cpu": avg}
        f.summary = {"CPU used": f"{avg:.0f}%"}
        f.facts = [f"Average CPU over the last {len(rows)} samples: {avg:.0f}%"]
        f.impact = "Everything on the machine runs slower; builds, tests and services will lag."
        f.recommendation = "Inspect the top processes and stop what you do not need."
        if machine == "mac":
            p = self.snap("mac", "procs")
            if p and p.get("top_cpu"):
                top = p["top_cpu"][0]
                f.cause = f"{top['name']} is using {top['cpu']:.0f}% CPU"
                f.confidence = "high" if top["cpu"] >= 60 else "medium"
                f.summary["Main contributor"] = top["name"]
                f.facts += [f"{x['name']}: {x['cpu']:.0f}% CPU" for x in p["top_cpu"][:4]]
                if top.get("eligible"):
                    f.targets = [{"kind": "process", "ref": {"pid": top["pid"], "start": top["start"]},
                                  "label": f"{top['name']} {top['cpu']:.0f}% CPU (PID {top['pid']})"}]
        else:
            cause, conf, targets, summ, more = self._hz_cause(0, by="cpu")
            f.cause, f.confidence, f.targets = cause, conf, targets
            f.summary.update(summ)
            f.facts += more
        return [f]

    def r_disk(self, machine, s, t) -> list[Finding]:
        if not s["disk_total"]:
            return []
        free_pct = 100.0 * s["disk_free"] / s["disk_total"]
        sev = "critical" if free_pct < t["disk_free_crit"] else "warning" if free_pct < t["disk_free_warn"] else None
        if not sev:
            return []
        f = Finding("disk", machine, sev, "Low disk space")
        f.values = {"free_pct": free_pct, "free": s["disk_free"]}
        f.summary = {"Free space": f"{gb(s['disk_free'])} ({free_pct:.0f}%)", "Disk size": gb(s["disk_total"])}
        f.facts = [f"{gb(s['disk_total'] - s['disk_free'])} used of {gb(s['disk_total'])}"]
        f.impact = ("Other services may fail if storage is exhausted." if machine == "hetzner"
                    else "macOS slows down and can't create swap or caches if the SSD fills up.")
        f.recommendation = "Inspect the largest storage consumers; nothing will be deleted for you."
        df = self.snap(machine, "docker_df", max_age=7200)
        logs = self.snap(machine, "logs", max_age=7200)
        parts = []
        if df:
            parts.append(("Docker images", df.get("images_total", 0)))
            parts.append(("Docker volumes", df.get("volumes_total", 0)))
            parts.append(("Docker build cache", df.get("build_cache_total", 0)))
        if logs:
            parts.append(("Container logs", sum(logs.get("sizes", {}).values())))
        dev = self.snap(machine, "storage", max_age=86400) if machine == "mac" else None
        if dev:
            parts += [(d["name"], d["size"]) for d in dev.get("dirs", [])[:4]]
        parts.sort(key=lambda x: x[1], reverse=True)
        if parts and parts[0][1] > 0:
            f.cause = f"The largest measured consumer is {parts[0][0]} ({gb(parts[0][1])})"
            f.confidence = "medium"
            f.summary["Largest consumer"] = f"{parts[0][0]} ({gb(parts[0][1])})"
            f.facts += [f"{n}: {gb(v)}" for n, v in parts[:5]]
        else:
            f.cause = "Not yet determined (storage breakdown not collected yet)"
            f.confidence = "low"
        return [f]

    def r_swap(self, machine, s, now, t) -> list[Finding]:
        out = []
        total = s["swap_total"] or 0
        if total and 100.0 * s["swap_used"] / total >= t["swap_used_crit"] and total > 0:
            f = Finding("swap_full", machine, "critical", "Swap is almost full")
            f.summary = {"Swap used": f"{mem(s['swap_used'])} of {mem(total)}"}
            f.impact = "The system may start killing processes or freeze when swap runs out."
            f.recommendation = "Reduce memory use now: stop unused workloads."
            out.append(f)
        rows = self.hist(machine, 1800, now)
        if len(rows) >= 8 and rows[-1]["ts"] - rows[0]["ts"] >= 1200:
            vals = [r["swap_used"] or 0 for r in rows]
            growth = vals[-1] - min(vals[: max(1, len(vals) // 3)])
            steps = [b - a for a, b in zip(vals, vals[1:])]
            nondec = sum(1 for d in steps if d >= -50e6) / max(len(steps), 1)
            if growth >= t["swap_growth_mb"] * 1e6 and nondec >= 0.7:
                f = Finding("swap_growth", machine, "warning", "Swap usage is growing steadily")
                f.values = {"growth": growth}
                f.summary = {"Swap growth (30 min)": f"+{growth / 1e6:.0f} MB", "Swap used": mem(vals[-1])}
                f.facts = [f"Swap rose from {mem(min(vals[: max(1, len(vals) // 3)]))} to {mem(vals[-1])} in about 30 minutes"]
                f.cause = "Memory demand is higher than physical RAM and pages keep moving to disk"
                f.confidence = "medium"
                f.impact = "Performance degrades progressively; swapping makes everything slower."
                f.recommendation = "Find what is growing (top memory processes) and stop what you do not need."
                if machine == "mac":
                    _, _, tg, summ, _ = self._mac_cause(s["mem_total"])
                    f.targets = tg
                    f.summary.update(summ)
                out.append(f)
        return out

    def r_disk_growth(self, machine, now, t) -> list[Finding]:
        rows = self.hist(machine, 6 * 3600, now)
        if len(rows) < 20 or rows[-1]["ts"] - rows[0]["ts"] < 3 * 3600:
            return []
        first = sum(r["disk_free"] for r in rows[:5]) / 5
        last = sum(r["disk_free"] for r in rows[-5:]) / 5
        span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
        rate = (first - last) / span_h  # bytes consumed per hour
        if rate <= 0 or last / rate > 24:
            return []
        f = Finding("disk_growth", machine, "warning", "Disk is filling quickly")
        hours = last / rate
        f.summary = {"Filling at": f"{rate / 1e6:.0f} MB/hour", "Full in about": f"{hours:.0f} hours"}
        f.facts = [f"Free space dropped {gb(first - last)} in {span_h:.1f} h"]
        f.cause = "Something is writing data continuously (logs, images, caches or volumes)"
        f.confidence = "medium"
        f.impact = "Other services may fail when storage runs out."
        f.recommendation = "Inspect the largest storage consumers on the machine page."
        return [f]

    def r_agent(self, s, now, t) -> list[Finding]:
        last = (self.hub.last_seen if self.hub else 0) or (s["ts"] if s else 0)
        if self.hub and s:
            last = max(self.hub.last_seen, s["ts"])
        if not last:
            return []                                    # never connected: nothing to alert on
        state = self.db.kv_get("mac_agent_state", {})
        if state.get("clean_stop") and state.get("ts", 0) >= last - 1:
            return []                                    # owner stopped the agent on purpose
        age = now - last
        if age < t["agent_notify_minutes"] * 60:
            return []
        f = Finding("agent_down", "mac", "warning", "Mac agent disconnected", persist=0)
        f.summary = {"Last heard from": f"{age / 60:.0f} minutes ago"}
        f.facts = [f"No heartbeat for {age / 60:.0f} minutes"]
        f.cause = "The Mac is asleep, offline or shut down, or the agent was stopped"
        f.confidence = "medium"
        f.impact = "The Mac cannot be monitored until the agent reconnects."
        f.recommendation = "Wake the Mac or check that the agent is running (start-agent)."
        return [f]

    def r_hetzner_containers(self, t, now) -> list[Finding]:
        c = self._hz_containers()
        s = self.latest("hetzner")
        if not c or not s:
            return []
        out = []
        mem_total = s["mem_total"] or 1
        host_crit = s["mem_pct"] >= t["mem_crit"]
        old = self.db.one("SELECT data FROM snapshots WHERE machine='hetzner' AND kind='containers' AND ts<=? "
                          "ORDER BY ts DESC LIMIT 1", (now - 900,))
        old_restarts = {x["id"]: x.get("restarts", 0) for x in json.loads(old["data"])["list"]} if old else {}
        stopped_by_us = {r["target"] for r in self.db.query("SELECT target FROM audit WHERE action='stop.execute' AND outcome='stopped'")}
        for x in c["list"]:
            name = x["name"]
            # --- restart loop
            delta = x.get("restarts", 0) - old_restarts.get(x["id"], x.get("restarts", 0))
            if x.get("status") == "restarting" or delta >= t["restart_count"]:
                sev = "critical" if delta >= 2 * t["restart_count"] or x.get("status") == "restarting" else "warning"
                f = Finding("restart_loop", "hetzner", sev, f"Container {name} keeps restarting", subject=x["id"][:12])
                f.summary = {"Container": name, "Restarts (15 min)": str(delta)}
                f.facts = [f"Restart count rose by {delta} in 15 minutes (total {x.get('restarts', 0)})",
                           f"Exit code {x.get('exit_code')}" + (", killed for out-of-memory" if x.get("oom") else "")]
                f.cause = "Killed for running out of memory" if x.get("oom") else "The application inside keeps crashing on start"
                f.confidence = "high" if x.get("oom") else "medium"
                f.impact = f"The service {name} is unstable or unavailable."
                f.recommendation = "Check the container logs; stop it if it is not needed."
                f.persist = 0
                f.targets = [{"kind": "container", "ref": {"id": x["id"]}, "label": name}]
                out.append(f)
            # --- unhealthy
            if x.get("running") and x.get("health") == "unhealthy":
                f = Finding("unhealthy", "hetzner", "warning", f"Container {name} is unhealthy", subject=x["id"][:12])
                f.summary = {"Container": name, "Health": "unhealthy"}
                f.facts = [f"Docker health check for {name} is failing"]
                f.cause = "The service inside is not answering its health check"
                f.confidence = "medium"
                f.impact = f"{name} may be unavailable to users."
                f.recommendation = "Inspect the container logs."
                out.append(f)
            # --- unexpectedly down
            if (not x.get("running") and x.get("status") == "exited" and x.get("exit_code") not in (0, None)
                    and x.get("restart_policy") in ("always", "unless-stopped") and name not in stopped_by_us
                    and x.get("finished_ago", 1e9) < 86400):
                f = Finding("service_down", "hetzner", "critical", f"Container {name} is down", subject=x["id"][:12])
                f.summary = {"Container": name, "Exit code": str(x.get("exit_code"))}
                f.facts = [f"Exited with code {x.get('exit_code')} {x.get('finished_ago', 0) / 60:.0f} min ago"]
                f.cause = "The process exited unexpectedly"
                f.confidence = "medium"
                f.impact = f"The service {name} is not running."
                f.recommendation = "Check its logs on the server; it is not restarted automatically by this app."
                out.append(f)
            if not x.get("running"):
                continue
            # --- CPU hog
            if x.get("cpu", 0) >= t["container_cpu"]:
                f = Finding("container_cpu", "hetzner", "warning", f"Container {name} is using a lot of CPU", subject=x["id"][:12],
                            persist=self.settings.get()["cpu_persist_seconds"])
                f.summary = {"Container": name, "CPU": f"{x['cpu']:.0f}% of one core"}
                f.facts = [f"{name} is using {x['cpu']:.0f}% of a CPU core"]
                f.cause = f"{name} is busy"
                f.confidence = "high"
                f.impact = "Other services on this 2-core server will slow down."
                f.recommendation = "Inspect what the workload is doing; stop it if unneeded."
                f.targets = [{"kind": "container", "ref": {"id": x["id"]}, "label": f"{name} {x['cpu']:.0f}% CPU"}]
                out.append(f)
            # --- memory hog
            share = 100.0 * x.get("mem", 0) / mem_total
            limit = x.get("mem_limit") or 0
            near_limit = limit and x.get("mem", 0) >= 0.9 * limit
            if share >= t["container_mem_host_pct"] or near_limit:
                f = Finding("container_mem", "hetzner", "critical" if host_crit else "warning",
                            f"Container {name} is using a lot of memory", subject=x["id"][:12])
                f.summary = {"Container": name, "Memory": f"{mem(x['mem'])} ({share:.0f}% of RAM)"}
                f.facts = [f"{name}: {mem(x['mem'])}" + (f" of its {mem(limit)} limit" if limit else "")]
                f.cause = "The workload is holding a large share of RAM" if not near_limit else "The container is near its own memory limit"
                f.confidence = "high"
                f.impact = "Less memory is available for other workloads; out-of-memory kills become likelier."
                f.recommendation = "Inspect the workload; stop it if it is not needed."
                f.targets = [{"kind": "container", "ref": {"id": x["id"]}, "label": f"{name} {mem(x['mem'])}"}]
                out.append(f)
        # --- logs growing large
        logs = self.snap("hetzner", "logs", max_age=7200)
        if logs:
            names = {x["id"]: x["name"] for x in c["list"]}
            for cid, size in logs.get("sizes", {}).items():
                if size >= t["log_size_mb"] * 1e6:
                    nm = names.get(cid, cid[:12])
                    f = Finding("log_size", "hetzner", "warning", f"Container {nm} has very large logs", subject=cid[:12])
                    f.summary = {"Container": nm, "Log size": f"{size / 1e6:.0f} MB"}
                    f.facts = [f"{nm} json log file is {size / 1e6:.0f} MB"]
                    f.cause = "The container writes a lot of log output and logs are not rotated"
                    f.confidence = "medium"
                    f.impact = "Logs consume disk space until the container is recreated or rotation is configured."
                    f.recommendation = "Configure log rotation (max-size) for this container; nothing is deleted automatically."
                    out.append(f)
        return out

    def _probe_tcp(self, port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            return False

    def r_hetzner_services(self) -> list[Finding]:
        if not self.latest("hetzner"):
            return []
        out = []
        for port, name in ((443, "nginx (HTTPS)"), (80, "nginx (HTTP)")):
            if not self.service_probe(port):
                f = Finding("service_probe", "hetzner", "critical", f"{name} is not accepting connections",
                            subject=str(port), persist=60)
                f.summary = {"Service": name, "Port": str(port)}
                f.facts = [f"TCP connect to 127.0.0.1:{port} failed"]
                f.cause = "The web server is stopped or crashed"
                f.confidence = "high"
                f.impact = "All your websites on this server are unreachable."
                f.recommendation = "Check nginx on the server (systemctl status nginx)."
                out.append(f)
        return out

    def r_mac_dev(self, t) -> list[Finding]:
        p = self.snap("mac", "procs")
        s = self.latest("mac")
        if not p or not s:
            return []
        out = []
        groups = p.get("groups", {})
        total = s["mem_total"] or 1
        for lbl in ("vitest", "jest"):
            g = groups.get(lbl)
            if g and g.get("count", 0) >= t["vitest_workers"] and (g.get("orphans", 0) or g.get("idle", 0) >= t["vitest_workers"] // 2 or g.get("rss", 0) > 0.15 * total):
                crit = g.get("rss", 0) > 0.30 * total
                f = Finding("dev_accumulation", "mac", "critical" if crit else "warning",
                            f"{LABEL_TEXT[lbl]} left running", subject=lbl)
                f.summary = {"Workers": str(g["count"]), "Memory held": mem(g["rss"]),
                             "Main contributor": _cap(LABEL_TEXT[lbl])}
                f.facts = [f"{g['count']} {lbl} processes, {mem(g['rss'])} total, {g.get('orphans', 0)} orphaned, {g.get('idle', 0)} idle"]
                f.cause = f"Several {lbl.capitalize()} workers are still running after their test run"
                f.confidence = "high" if g.get("orphans", 0) else "medium"
                f.impact = "Available RAM is decreasing and macOS leans more on swap."
                f.recommendation = "Inspect the worker processes and stop unused ones."
                f.targets = self._mac_targets(p, lbl)
                out.append(f)
        g = groups.get("node")
        if g and g.get("count", 0) >= t["node_procs"] and g.get("rss", 0) > 0.12 * total:
            f = Finding("dev_accumulation", "mac", "warning", "Node.js processes are accumulating", subject="node")
            f.summary = {"Node processes": str(g["count"]), "Memory held": mem(g["rss"])}
            f.facts = [f"{g['count']} node processes holding {mem(g['rss'])}; {g.get('idle', 0)} idle"]
            f.cause = "Development servers or tools were started and never closed"
            f.confidence = "medium"
            f.impact = "RAM is being consumed by processes you may no longer need."
            f.recommendation = "Review the node processes and stop the ones you are not using."
            f.targets = self._mac_targets(p, "node")
            out.append(f)
        vm = p.get("docker_vm_rss", 0)
        if vm >= t["docker_vm_gb"] * GB:
            ct = self.snap("mac", "containers", max_age=600)
            running = [c for c in (ct or {}).get("list", []) if c.get("running")]
            f = Finding("docker_vm", "mac", "warning", "Docker is consuming a lot of memory", subject="vm")
            f.summary = {"Docker VM memory": mem(vm), "Running containers": str(len(running)) if ct else "unknown"}
            f.facts = [f"Docker Desktop VM resident memory {mem(vm)}"] + [f"container {c['name']}" for c in running[:5]]
            f.cause = "Several containers are running at the same time" if len(running) > 1 else "The Docker VM holds memory it was given"
            f.confidence = "medium"
            f.impact = "Available RAM is decreasing on your 8 GB Mac."
            f.recommendation = "Inspect container resource usage and stop unnecessary containers."
            f.targets = [{"kind": "container", "ref": {"id": c["id"]}, "label": c["name"]} for c in running[:3]]
            out.append(f)
        builds = [x for x in p.get("dev", []) if x.get("label") in ("maven", "gradle", "build") and x.get("cpu", 0) >= 25]
        if len(builds) >= 2:
            f = Finding("multi_build", "mac", "warning", "Several builds are running at once", subject="builds")
            f.summary = {"Builds": str(len(builds)), "CPU": f"{sum(b['cpu'] for b in builds):.0f}%"}
            f.facts = [f"{b['name']} ({b.get('label')}) {b['cpu']:.0f}% CPU" for b in builds[:4]]
            f.cause = "More than one build/test tool is running concurrently"
            f.confidence = "medium"
            f.impact = "CPU and RAM are shared, so all builds finish slower."
            f.recommendation = "Let one finish, or stop the one you do not need."
            f.targets = self._mac_targets({"dev": builds}, None)
            out.append(f)
        growing = [x for x in p.get("top_mem", []) if x.get("growing")]
        if growing:
            x = growing[0]
            f = Finding("mem_growth", "mac", "warning", f"{x['name']} memory keeps growing", subject=x["name"][:30])
            f.summary = {"Process": x["name"], "Memory now": mem(x["rss"]), "Growth": f"+{x.get('growth', 0) / 1e6:.0f} MB"}
            f.facts = [f"{x['name']} RSS has risen steadily by {x.get('growth', 0) / 1e6:.0f} MB over the last ~20 minutes"]
            f.cause = "A possible memory leak or an ever-growing cache"
            f.confidence = "low"
            f.impact = "It will keep taking memory until it is restarted."
            f.recommendation = "Restart it when convenient."
            f.targets = self._mac_targets({"dev": growing}, None, 1)
            out.append(f)
        return out

    def r_self(self, t) -> list[Finding]:
        st = self.self_state
        out = []
        if st.get("rss", 0) > t["self_rss_mb"] * 1e6 or st.get("cpu", 0) > t["self_cpu"] or st.get("db_size", 0) > 300e6:
            f = Finding("self_resource", "hetzner", "warning", "The monitor itself is using unexpected resources", subject="self")
            f.summary = {"Watcher memory": f"{st.get('rss', 0) / 1e6:.0f} MB", "Watcher CPU": f"{st.get('cpu', 0):.0f}%",
                         "Database": f"{st.get('db_size', 0) / 1e6:.0f} MB"}
            f.facts = list(f.summary.values())
            f.cause = "Unexpected load or growth inside the monitoring app"
            f.confidence = "medium"
            f.impact = "The watcher competes with your workloads for scarce resources."
            f.recommendation = "Restart the watcher service (systemctl --user restart infra-watcher)."
            out.append(f)
        return out

    def r_monitor_failure(self) -> list[Finding]:
        if self.collector_errors.get("hetzner", 0) >= 3:
            f = Finding("monitor_failure", "hetzner", "critical", "Monitoring failure: server metrics cannot be collected", subject="collector", persist=60)
            f.summary = {"Failed collections": str(self.collector_errors["hetzner"])}
            f.facts = [f"{self.collector_errors['hetzner']} collection cycles in a row failed"]
            f.cause = "Docker or /proc access is failing"
            f.confidence = "medium"
            f.impact = "Problems on the server may go undetected."
            f.recommendation = "Check the watcher service logs."
            return [f]
        return []

    # ---------------------------------------------------------------- lifecycle
    def apply(self, findings: list[Finding], now: float | None = None) -> None:
        now = now or time.time()
        s = self.settings.get()
        keys = {f.key: f for f in findings}
        active = {f"{r['rule']}|{r['machine']}|{r['subject']}": dict(r)
                  for r in self.db.query("SELECT * FROM incidents WHERE state='active'")}
        pending = {r["key"]: r["since"] for r in self.db.query("SELECT key,since FROM pending")}

        for key, f in keys.items():
            payload = self._payload(f)
            if key in active:
                inc = active[key]
                if RANK.get(f.severity, 0) > RANK.get(inc["severity"], 0):
                    self.db.event(inc["id"], "escalated", f"Now {f.severity}", machine=f.machine, rule=f.rule,
                                  severity=f.severity, title=f.title, ts=now)
                self.db.execute(
                    "UPDATE incidents SET severity=?, title=?, measured=?, cause=?, confidence=?, impact=?, recommendation=?,"
                    " targets=?, last_seen=?, clear_since=NULL WHERE id=?",
                    (f.severity, f.title, *payload, now, inc["id"]))
                continue
            since = pending.get(key)
            if since is None:
                self.db.execute("INSERT INTO pending(key,since) VALUES(?,?)", (key, now))
                since = now
            persist = f.persist if f.persist is not None else s["persist_seconds"]
            if now - since >= persist:
                iid = "inc_" + secrets.token_hex(5)
                self.db.execute(
                    "INSERT INTO incidents(id,rule,machine,subject,severity,state,title,measured,cause,confidence,impact,"
                    "recommendation,targets,first_seen,active_since,last_seen) VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?,?)",
                    (iid, f.rule, f.machine, f.subject, f.severity, f.title, *payload, since, now, now))
                self.db.execute("DELETE FROM pending WHERE key=?", (key,))
                ev = dict(machine=f.machine, rule=f.rule, severity=f.severity, title=f.title, ts=now)
                self.db.event(iid, "detected", f"Lasted {now - since:.0f}s before it counted as a problem", **ev)
                self.db.event(iid, "investigated", (f.cause or "No clear cause found") +
                              (f" [{f.confidence} confidence]" if f.confidence else ""), **ev)
                self.db.event(iid, "recommended", f.recommendation, **ev)
        for key in list(pending):
            if key not in keys:
                self.db.execute("DELETE FROM pending WHERE key=?", (key,))
        for key, inc in active.items():
            if key in keys:
                continue
            clear = inc["clear_since"] or now
            if not inc["clear_since"]:
                self.db.execute("UPDATE incidents SET clear_since=? WHERE id=?", (now, inc["id"]))
            if now - clear >= s["recover_seconds"]:
                self.db.execute("UPDATE incidents SET state='recovered', recovered_at=?, last_seen=? WHERE id=?",
                                (now, now, inc["id"]))
                ev = dict(machine=inc["machine"], rule=inc["rule"], severity=inc["severity"], title=inc["title"], ts=now)
                self.db.event(inc["id"], "verified", f"Measurement stayed back under the limit for {s['recover_seconds']:.0f}s", **ev)
                self.db.event(inc["id"], "recovered", "Alert cleared", **ev)

    @staticmethod
    def _payload(f: Finding):
        measured = json.dumps({"values": f.values, "summary": f.summary, "facts": f.facts})
        return (measured, f.cause, f.confidence, f.impact, f.recommendation, json.dumps(f.targets))

    def tick(self, now: float | None = None) -> list[Finding]:
        findings = self.evaluate(now)
        self.apply(findings, now)
        return findings
