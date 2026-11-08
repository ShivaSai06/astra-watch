#!/usr/bin/env python3
"""Personal Infrastructure Watcher — Mac agent.

Standard library only (works with the system Python 3.9+). Needs no administrator rights.

Privacy: process *command lines are read locally only to classify a workload* (e.g. "this node
process is a Vitest worker"). Only the classification label, executable path, resource numbers and
PID are sent. Environment variables, arguments and file contents are never collected or sent.

It never stops anything on its own: the only write action is a narrowly scoped stop request that
the server forwards after YOU confirmed it (dashboard or Telegram), and it re-verifies the exact
process/container identity before sending a polite SIGTERM / `docker stop`. No force-kill.
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

VERSION = "1.0.0"
HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(HOME, ".config", "infra-watcher")
STATE_DIR = os.path.join(HOME, ".local", "state", "infra-watcher")
CONF_FILE = os.path.join(CONF_DIR, "agent.json")
KEYCHAIN_SERVICE = "infra-watcher-agent"
ME = os.environ.get("USER") or os.path.basename(HOME)
GB = 1e9

PROTECTED_NAMES = {
    "WindowServer", "kernel_task", "launchd", "loginwindow", "Finder", "Dock", "SystemUIServer", "coreaudiod",
    "mds", "mds_stores", "mdworker", "mdworker_shared", "configd", "powerd", "opendirectoryd", "securityd", "trustd",
    "distnoted", "cfprefsd", "UserEventAgent", "bluetoothd", "airportd", "syslogd", "logd", "notifyd", "diskarbitrationd",
    "fseventsd", "runningboardd", "sharingd", "ControlCenter", "NotificationCenter", "Spotlight", "kextd", "sudo", "login",
    "sshd", "zsh", "bash", "fish", "sh", "tmux", "Terminal", "iTerm2", "ssh-agent",
}
PROTECTED_PREFIXES = ("/System/", "/usr/libexec/", "/usr/sbin/", "/sbin/", "/Library/Apple/", "/bin/",
                      "/usr/bin/", "/Applications/Utilities/")
# Workload classes that may be offered for stopping (user-owned dev tooling only).
ELIGIBLE_LABELS = {"vitest", "jest", "node", "java", "python", "maven", "gradle", "vite", "build"}

log = logging.getLogger("agent")


# ----------------------------------------------------------------------------- config / secrets
def load_config():
    with open(CONF_FILE) as fh:
        return json.load(fh)


def read_token():
    """Token lives in the macOS Keychain (native secret store); falls back to a 0600 file."""
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    p = os.path.join(CONF_DIR, "token")
    if os.path.exists(p):
        return open(p).read().strip()
    return os.environ.get("WATCHER_AGENT_TOKEN", "")


# ----------------------------------------------------------------------------- native macOS metrics
class Mach:
    """host_statistics via libSystem: no subprocess, no privileges."""

    class VMStat64(ctypes.Structure):
        _fields_ = [("free_count", ctypes.c_uint32), ("active_count", ctypes.c_uint32),
                    ("inactive_count", ctypes.c_uint32), ("wire_count", ctypes.c_uint32),
                    ("zero_fill_count", ctypes.c_uint64), ("reactivations", ctypes.c_uint64),
                    ("pageins", ctypes.c_uint64), ("pageouts", ctypes.c_uint64), ("faults", ctypes.c_uint64),
                    ("cow_faults", ctypes.c_uint64), ("lookups", ctypes.c_uint64), ("hits", ctypes.c_uint64),
                    ("purges", ctypes.c_uint64), ("purgeable_count", ctypes.c_uint32),
                    ("speculative_count", ctypes.c_uint32), ("decompressions", ctypes.c_uint64),
                    ("compressions", ctypes.c_uint64), ("swapins", ctypes.c_uint64), ("swapouts", ctypes.c_uint64),
                    ("compressor_page_count", ctypes.c_uint32), ("throttled_count", ctypes.c_uint32),
                    ("external_page_count", ctypes.c_uint32), ("internal_page_count", ctypes.c_uint32),
                    ("total_uncompressed_pages_in_compressor", ctypes.c_uint64)]

    def __init__(self):
        self.lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        self.lib.mach_host_self.restype = ctypes.c_uint32
        self.host = self.lib.mach_host_self()
        self.page = os.sysconf("SC_PAGE_SIZE")
        self._last_cpu = None

    def cpu(self):
        """(user+system+nice share, idle share) in percent since the previous call."""
        info = (ctypes.c_uint32 * 4)()
        count = ctypes.c_uint32(4)
        rc = self.lib.host_statistics(self.host, 3, ctypes.byref(info), ctypes.byref(count))
        if rc != 0:
            raise OSError("host_statistics failed")
        cur = list(info)                                  # user, system, idle, nice
        prev, self._last_cpu = self._last_cpu, cur
        if not prev:
            return 0.0, 100.0
        d = [c - p for c, p in zip(cur, prev)]
        total = sum(d)
        if total <= 0:
            return 0.0, 100.0
        idle = 100.0 * d[2] / total
        return round(100.0 - idle, 1), round(idle, 1)

    def vm(self):
        st = self.VMStat64()
        count = ctypes.c_uint32(ctypes.sizeof(st) // 4)
        rc = self.lib.host_statistics64(self.host, 4, ctypes.byref(st), ctypes.byref(count))
        if rc != 0:
            raise OSError("host_statistics64 failed")
        return st


def sh(args, timeout=8):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def memory_numbers(mach: Mach):
    """macOS 'used' = App memory + Wired + Compressed (what Activity Monitor calls Memory Used).
    File cache and purgeable memory are *not* counted, because macOS gives them back on demand."""
    total = int(sh(["sysctl", "-n", "hw.memsize"]) or 0)
    v = mach.vm()
    pg = mach.page
    app = max(v.internal_page_count - v.purgeable_count, 0) * pg
    wired = v.wire_count * pg
    comp = v.compressor_page_count * pg
    used = min(app + wired + comp, total)
    swap_total = swap_used = 0
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", sh(["sysctl", "-n", "vm.swapusage"]))
    if m:
        swap_total, swap_used = int(float(m.group(1)) * 1048576), int(float(m.group(2)) * 1048576)
    level = int(sh(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip() or 1)
    return {
        "mem_total": total, "mem_used": used, "mem_pct": round(100.0 * used / total, 1) if total else 0.0,
        "swap_total": swap_total, "swap_used": swap_used,
        "extra": {"pressure_level": level, "compressed": comp, "app_mem": app, "wired": wired,
                  "available": max(total - used, 0), "pageins": v.pageins, "pageouts": v.pageouts},
    }


def disk_numbers():
    path = "/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/"
    st = os.statvfs(path)
    return {"disk_total": st.f_blocks * st.f_frsize, "disk_free": st.f_bavail * st.f_frsize}


def thermal():
    out = sh(["pmset", "-g", "therm"])
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
    if m:
        lim = int(m.group(1))
        return {"cpu_speed_limit": lim, "state": "throttled" if lim < 100 else "nominal"}
    return {"state": "nominal" if out else "unavailable"}


# ----------------------------------------------------------------------------- processes
LSTART = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(\w{3} \w{3}\s+\d+ \d\d:\d\d:\d\d \d{4})\s+([\d.]+)\s+(\d+)\s+(.*)$")


def parse_lstart(s: str) -> int:
    try:
        return int(time.mktime(time.strptime(re.sub(r"\s+", " ", s.strip()), "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return 0


def ps_all():
    """Resource facts for every process. `comm` on macOS is the executable path (no arguments)."""
    out = sh(["ps", "-axo", "pid=,ppid=,user=,lstart=,%cpu=,rss=,comm="], timeout=10)
    rows = []
    for line in out.splitlines():
        m = LSTART.match(line)
        if not m:
            continue
        pid, ppid, user, lstart, cpu, rss, exe = m.groups()
        rows.append({"pid": int(pid), "ppid": int(ppid), "user": user, "start": parse_lstart(lstart),
                     "cpu": float(cpu), "rss": int(rss) * 1024, "exe": exe.strip(),
                     "name": os.path.basename(exe.strip())})
    return rows


DEV_PATTERNS = [
    ("vitest", re.compile(r"vitest|tinypool")), ("jest", re.compile(r"jest-worker|/jest\b|\bjest\b")),
    ("maven", re.compile(r"plexus-classworlds|\bmvn\b|maven")), ("gradle", re.compile(r"gradle")),
    ("vite", re.compile(r"\bvite\b|next dev|next-server|react-scripts|webpack serve|webpack-dev-server|nodemon|ts-node-dev")),
    ("build", re.compile(r"\btsc\b|webpack|esbuild|rollup|turbo|\bnpm (run )?build|\bpnpm (run )?build")),
    ("claude", re.compile(r"claude-code|/claude\b|\bclaude$")),
]


def classify_all(rows):
    """Attach a coarse label to each process. Command lines are read here and discarded."""
    cmds = {}
    for line in sh(["ps", "-axo", "pid=,command="], timeout=10).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            cmds[int(parts[0])] = parts[1].lower()
    for r in rows:
        cmd, name, exe = cmds.get(r["pid"], ""), r["name"], r["exe"]
        label = "other"
        if "Virtualization.VirtualMachine" in exe or "com.docker.virtualization" in exe:
            label = "docker-vm"
        elif exe.startswith("/Applications/Docker.app") or "com.docker" in exe:
            label = "docker-app"
        elif name == "claude" or "claude-code" in cmd or "@anthropic-ai" in cmd:
            label = "claude"
        elif name in ("node", "bun", "deno") or name.startswith("node"):
            label = "node"
            for lbl, pat in DEV_PATTERNS:
                if pat.search(cmd):
                    label = lbl
                    break
        elif name == "java" or name.startswith("java"):
            label = "java"
            for lbl in ("maven", "gradle"):
                if dict(DEV_PATTERNS)[lbl].search(cmd):
                    label = lbl
        elif name.lower().startswith("python"):
            label = "python"
        elif re.search(r"Google Chrome|Safari|Firefox|Brave|Arc\.app|Microsoft Edge", exe):
            label = "browser"
        r["label"] = label
    return rows


def ancestors(rows):
    by = {r["pid"]: r for r in rows}
    out, pid = set(), os.getpid()
    while pid in by and pid not in out and pid > 1:
        out.add(pid)
        pid = by[pid]["ppid"]
    return out


def eligible(r, protect):
    if r["user"] != ME or r["pid"] <= 1 or r["pid"] in protect:
        return False
    if r["name"] in PROTECTED_NAMES or r["exe"].startswith(PROTECTED_PREFIXES):
        return False
    return r["label"] in ELIGIBLE_LABELS


class ProcessTracker:
    """Per-process memory history to spot steady growth and idle workers (not mere long runtime)."""

    def __init__(self):
        self.hist = {}
        self.idle = collections.Counter()

    def update(self, rows, now):
        alive = set()
        for r in rows:
            key = (r["pid"], r["start"])
            alive.add(key)
            if r["cpu"] < 1.0:
                self.idle[key] += 1
            else:
                self.idle[key] = 0
            r["idle_samples"] = self.idle[key]
            if r["rss"] > 300e6:
                h = self.hist.setdefault(key, collections.deque(maxlen=60))
                h.append((now, r["rss"]))
        for k in list(self.hist):
            if k not in alive:
                del self.hist[k]
        for k in list(self.idle):
            if k not in alive:
                del self.idle[k]
        for r in rows:
            r["growing"], r["growth"] = False, 0
            h = self.hist.get((r["pid"], r["start"]))
            if h and len(h) >= 20 and h[-1][0] - h[0][0] >= 900:
                vals = [v for _, v in h]
                growth = vals[-1] - vals[0]
                steps = [b - a for a, b in zip(vals, vals[1:])]
                mono = sum(1 for d in steps if d >= -20e6) / len(steps)
                if growth >= 400e6 and mono >= 0.85:
                    r["growing"], r["growth"] = True, growth


def process_snapshot(tracker, now):
    rows = classify_all(ps_all())
    tracker.update(rows, now)
    protect = ancestors(rows)
    by_pid = {r["pid"]: r for r in rows}
    for r in rows:
        r["eligible"] = eligible(r, protect)
        p = by_pid.get(r["ppid"])
        r["parent"] = p["name"] if p else ""
        r["why"] = ""
        if r["label"] in ("vitest", "jest") and r["ppid"] == 1:
            r["why"] = "Worker has no parent process left (orphaned)"
        elif r["label"] in ELIGIBLE_LABELS and r.get("idle_samples", 0) >= 4 and r["rss"] > 200e6:
            r["why"] = f"Idle for several minutes while holding {r['rss'] / GB:.1f} GB"
        elif r["growing"]:
            r["why"] = f"Memory grew {r['growth'] / 1e6:.0f} MB steadily"

    def slim(r):
        return {k: r[k] for k in ("pid", "ppid", "name", "exe", "user", "start", "cpu", "rss", "label", "eligible",
                                  "growing", "growth", "parent", "why")} | {"name": r["name"][:80], "exe": r["exe"][:200]}

    groups = {}
    for r in rows:
        if r["label"] in ("other", "browser", "docker-app"):
            continue
        g = groups.setdefault(r["label"], {"count": 0, "rss": 0, "cpu": 0.0, "orphans": 0, "idle": 0})
        g["count"] += 1
        g["rss"] += r["rss"]
        g["cpu"] = round(g["cpu"] + r["cpu"], 1)
        g["orphans"] += 1 if r["ppid"] == 1 and r["label"] in ELIGIBLE_LABELS else 0
        g["idle"] += 1 if r.get("idle_samples", 0) >= 2 and r["label"] in ELIGIBLE_LABELS else 0
    vm = sum(r["rss"] for r in rows if r["label"] == "docker-vm")
    dev = sorted((r for r in rows if r["label"] not in ("other", "browser", "docker-app")), key=lambda r: r["rss"], reverse=True)
    return {
        "top_cpu": [slim(r) for r in sorted(rows, key=lambda r: r["cpu"], reverse=True)[:10]],
        "top_mem": [slim(r) for r in sorted(rows, key=lambda r: r["rss"], reverse=True)[:10]],
        "dev": [slim(r) for r in dev[:40]], "groups": groups, "docker_vm_rss": vm, "count": len(rows),
        "docker_running": any(r["label"] in ("docker-vm", "docker-app") for r in rows),
    }


# ----------------------------------------------------------------------------- docker (Docker Desktop)
DOCKER_CANDIDATES = ["/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                     "/Applications/Docker.app/Contents/Resources/bin/docker"]


def docker_bin():
    return shutil.which("docker") or next((p for p in DOCKER_CANDIDATES if os.path.exists(p)), None)


def docker(args, timeout=10):
    b = docker_bin()
    if not b:
        return ""
    return sh([b, *args], timeout=timeout)


def to_bytes(s):
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", s.strip())
    if not m:
        return 0
    return int(float(m.group(1)) * {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KIB": 1024, "MIB": 1024 ** 2,
                                    "GIB": 1024 ** 3}[m.group(2).upper()])


def docker_containers():
    out = docker(["ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}"])
    lst = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 4 and re.fullmatch(r"[0-9a-f]{64}", p[0]):
            lst.append({"id": p[0], "name": p[1], "image": p[2], "running": p[3] == "running", "status": p[3]})
    if not lst:
        return {"list": []}
    meta = docker(["inspect", "--format", "{{.Id}} {{.Image}} {{.State.StartedAt}}", *[c["id"] for c in lst]])
    info = {}
    for line in meta.splitlines():
        p = line.split()
        if len(p) == 3:
            info[p[0]] = p
    stats = {}
    if any(c["running"] for c in lst):
        for line in docker(["stats", "--no-stream", "--no-trunc", "--format", "{{.ID}}\t{{.CPUPerc}}\t{{.MemUsage}}"], 20).splitlines():
            p = line.split("\t")
            if len(p) == 3:
                stats[p[0]] = (float(p[1].strip("%") or 0), to_bytes(p[2].split("/")[0]))
    for c in lst:
        i = info.get(c["id"])
        if i:
            c["image_id"], c["started"] = i[1], i[2]
        c["cpu"], c["mem"] = stats.get(c["id"], (0.0, 0))
    return {"list": lst}


def docker_df():
    out = docker(["system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"], 60)
    res = {}
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 3:
            key = {"Images": "images_total", "Containers": "containers_total", "Local Volumes": "volumes_total",
                   "Build Cache": "build_cache_total"}.get(p[0])
            if key:
                res[key] = to_bytes(p[1])
    raw = os.path.join(HOME, "Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw")
    if os.path.exists(raw):
        res["vm_disk_file"] = os.stat(raw).st_blocks * 512
    return res


DEV_DIRS = ["~/Library/Caches", "~/.npm", "~/.gradle", "~/.m2", "~/.cache", "~/Library/Developer",
            "~/Library/Containers/com.docker.docker", "~/.docker", "~/Library/pnpm", "~/.pnpm-store"]


def storage_scan(scan_projects):
    """Occasional (every ~12 h), background-priority `du` of a fixed list of cache/dev dirs. Never recursive over $HOME."""
    dirs = []
    for d in DEV_DIRS:
        p = os.path.expanduser(d)
        if os.path.isdir(p):
            out = sh(["taskpolicy", "-b", "du", "-sk", p], timeout=90).split()
            if out and out[0].isdigit():
                dirs.append({"name": d, "size": int(out[0]) * 1024})
    if scan_projects:
        base = os.path.expanduser(scan_projects)
        if os.path.isdir(base):
            for line in sh(["taskpolicy", "-b", "du", "-sk", "-d", "1", base], timeout=180).splitlines():
                p = line.split(None, 1)
                if len(p) == 2 and p[0].isdigit() and p[1].strip() != base:
                    dirs.append({"name": "project: " + os.path.basename(p[1].strip()), "size": int(p[0]) * 1024})
    dirs.sort(key=lambda d: d["size"], reverse=True)
    return {"dirs": dirs[:20]}


# ----------------------------------------------------------------------------- stop commands
def ps_one(pid):
    out = sh(["ps", "-p", str(pid), "-o", "pid=,ppid=,user=,lstart=,%cpu=,rss=,comm="])
    for line in out.splitlines():
        m = LSTART.match(line)
        if m:
            pid_, ppid, user, lstart, _, rss, exe = m.groups()
            return {"pid": int(pid_), "ppid": int(ppid), "user": user, "start": parse_lstart(lstart), "exe": exe.strip(),
                    "name": os.path.basename(exe.strip())}
    return None


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = sh(["ps", "-p", str(pid), "-o", "state="]).strip()
    return bool(state) and not state.startswith("Z")


def stop_process(t):
    try:
        pid = int(t["pid"])
    except (KeyError, ValueError, TypeError):
        return {"status": "failed", "message": "bad request"}
    cur = ps_one(pid)
    if not cur:
        return {"status": "gone", "message": "That process is no longer running."}
    if cur["start"] != t.get("start") or cur["exe"] != t.get("exe") or cur["user"] != t.get("user"):
        return {"status": "changed", "message": "The PID now belongs to a different process; nothing was stopped."}
    rows = classify_all(ps_all())
    me = next((r for r in rows if r["pid"] == pid), None)
    if not me or not eligible(me, ancestors(rows)):
        return {"status": "unauthorized", "message": f"{cur['name']} is protected or not an eligible development process."}
    log.info("stopping pid=%s name=%s (SIGTERM)", pid, cur["name"])
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"status": "stopped", "message": f"{cur['name']} had already exited."}
    except PermissionError:
        return {"status": "failed", "message": "Permission denied; not owned by you."}
    for _ in range(40):
        time.sleep(0.25)
        if not alive(pid):
            return {"status": "stopped", "message": f"{cur['name']} (PID {pid}) exited after a graceful stop request."}
    return {"status": "failed", "message": f"{cur['name']} is still running after SIGTERM. It was NOT force-killed; stop it manually if needed."}


def stop_container(t):
    cid = t.get("id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", cid or ""):
        return {"status": "failed", "message": "bad container id"}
    meta = docker(["inspect", "--format", "{{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.Running}}", cid]).split()
    if len(meta) != 4:
        return {"status": "gone", "message": "That container no longer exists (or Docker is not running)."}
    if meta[0] != cid or meta[1] != t.get("image_id") or meta[2] != t.get("started"):
        return {"status": "changed", "message": "The container was restarted or replaced; nothing was stopped."}
    if meta[3] != "true":
        return {"status": "gone", "message": "The container is already stopped."}
    docker(["stop", "-t", "20", cid], timeout=60)
    after = docker(["inspect", "--format", "{{.State.Running}}", cid]).strip()
    if after == "false":
        return {"status": "stopped", "message": f"Container {t.get('name', cid[:12])} stopped. Volumes untouched."}
    return {"status": "failed", "message": "The container is still running after docker stop."}


# ----------------------------------------------------------------------------- agent
class Agent:
    def __init__(self, conf, token):
        self.url = conf["url"].rstrip("/")
        self.token = token
        self.interval = int(conf.get("interval", 30))
        self.scan_projects = conf.get("scan_projects", "~/Workspace")
        self.mach = Mach()
        self.tracker = ProcessTracker()
        self.stop_evt = threading.Event()
        self.last = {"containers": 0, "docker_df": 0, "storage": 0}
        self.start_t = time.time()
        self.fail_since = None
        self.notified_outage = False

    # -- HTTP --------------------------------------------------------------
    def request(self, method, path, body=None, timeout=15):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
            "User-Agent": f"infra-watcher-agent/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:   # certificate verification is on by default
            return json.load(r)

    # -- collection --------------------------------------------------------
    def collect(self, clean_stop=False, full=False):
        now = time.time()
        cpu, idle = self.mach.cpu()
        mem = memory_numbers(self.mach)
        l1, l5, l15 = os.getloadavg()
        extra = mem.pop("extra")
        extra.update({"cpu_idle": idle, "cores": os.cpu_count(), "thermal": thermal() if (full or int(now) % 60 < self.interval) else None,
                      "uptime": now - self._boot()})
        procs = process_snapshot(self.tracker, now)
        payload = {"v": 1, "ts": now, "host": os.uname().nodename[:80], "version": VERSION, "clean_stop": clean_stop,
                   "sample": {"cpu": cpu, "load1": l1, "load5": l5, "load15": l15, **mem, **disk_numbers(), "extra": extra},
                   "procs": procs}
        if procs.get("docker_running") and (full or now - self.last["containers"] >= 60):
            self.last["containers"] = now
            payload["containers"] = docker_containers()
            extra["containers"] = sum(1 for c in payload["containers"]["list"] if c["running"])
        if procs.get("docker_running") and (full or now - self.last["docker_df"] >= 1800):
            self.last["docker_df"] = now
            payload["docker_df"] = docker_df()
        if (full or (now - self.start_t > 600 and now - self.last["storage"] >= 12 * 3600)) and not clean_stop:
            self.last["storage"] = now
            payload["storage"] = storage_scan(self.scan_projects)
        if extra.get("thermal") is None:
            extra.pop("thermal")
        return payload

    @staticmethod
    def _boot():
        m = re.search(r"sec = (\d+)", sh(["sysctl", "-n", "kern.boottime"]))
        return int(m.group(1)) if m else time.time()

    # -- loops -------------------------------------------------------------
    def metrics_loop(self):
        while not self.stop_evt.is_set():
            t0 = time.time()
            try:
                payload = self.collect()
                resp = self.request("POST", "/agent/v1/metrics", payload)
                self.interval = max(15, min(int(resp.get("interval", self.interval)), 120))
                self.fail_since, self.notified_outage = None, False
            except urllib.error.HTTPError as e:
                log.warning("server rejected metrics: HTTP %s", e.code)
                if e.code in (401, 403):
                    self.stop_evt.wait(300)
                self._outage()
            except Exception as e:
                log.warning("send failed (%s); will retry", type(e).__name__)
                self._outage()
            self.stop_evt.wait(max(1, self.interval - (time.time() - t0)))

    def _outage(self):
        """If the dashboard (and so Telegram) is unreachable for a while, say so locally on the Mac."""
        self.fail_since = self.fail_since or time.time()
        if not self.notified_outage and time.time() - self.fail_since > 600:
            self.notified_outage = True
            sh(["osascript", "-e", 'display notification "The monitoring server has been unreachable for 10 minutes." '
                                   'with title "Infrastructure Watcher"'])

    def command_loop(self):
        backoff = 5
        while not self.stop_evt.is_set():
            try:
                resp = self.request("GET", "/agent/v1/commands?wait=20", timeout=35)
                backoff = 5
                for cmd in resp.get("commands", []):
                    self.run_command(cmd)
            except urllib.error.HTTPError as e:
                self.stop_evt.wait(300 if e.code in (401, 403) else backoff)
            except Exception:
                self.stop_evt.wait(backoff)
                backoff = min(backoff * 2, 120)

    def run_command(self, cmd):
        typ, target = cmd.get("type"), cmd.get("target") or {}
        log.info("command %s received", typ)
        if typ == "stop_process":
            res = stop_process(target)
        elif typ == "stop_container":
            res = stop_container(target)
        else:
            res = {"status": "failed", "message": "unsupported command"}
        log.info("command %s -> %s", typ, res["status"])
        try:
            self.request("POST", f"/agent/v1/commands/{cmd['id']}/result", res)
        except Exception as e:
            log.warning("could not report result: %s", type(e).__name__)

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self.stop_evt.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop_evt.set())
        threading.Thread(target=self.command_loop, daemon=True).start()
        log.info("agent %s started, server=%s interval=%ss", VERSION, self.url, self.interval)
        self.metrics_loop()
        try:                                                    # tell the server this was a deliberate stop
            self.request("POST", "/agent/v1/metrics", self.collect(clean_stop=True), timeout=5)
        except Exception:
            pass
        log.info("agent stopped")


def setup_logging(verbose):
    os.makedirs(STATE_DIR, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(os.path.join(STATE_DIR, "agent.log"), maxBytes=256_000, backupCount=2)]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="collect one sample and print it (nothing is sent)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if a.once:
        ag = Agent({"url": "https://invalid.example"}, "x")
        ag.mach.cpu(); time.sleep(1.5)
        print(json.dumps(ag.collect(full=True), indent=2))
        return
    setup_logging(a.verbose)
    conf = load_config()
    token = read_token()
    if not token:
        sys.exit("No agent token found. Re-run install.sh.")
    Agent(conf, token).run()


if __name__ == "__main__":
    main()
