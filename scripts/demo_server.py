#!/usr/bin/env python3
"""Run Astra Watch locally on SYNTHETIC data, for screenshots and for trying the dashboard.

Nothing here touches a real machine: the data is generated, the database lives in a throw-away
temp directory, no collection loops run, and Telegram is not configured.

    .venv/bin/python scripts/demo_server.py [--port 8300] [--mac connected|stale|offline]

A tiny middleware adds the headers `tailscale serve` would add (a fake demo login, https) so the
Settings page shows the Tailscale status block. Real deployments never use that middleware.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["WATCHER_NO_LOOPS"] = "1"
os.environ.setdefault("WATCHER_STATIC_DIR", str(Path(__file__).resolve().parents[1] / "frontend" / "dist"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import uvicorn  # noqa: E402

from watcher.config import Paths, save_env_file  # noqa: E402
from watcher.main import create_app  # noqa: E402

GB = 1_000_000_000
DEMO_LOGIN = "demo@example.com"


def sample(db, machine, ts, cpu, mem_pct, mem_total, swap_used, swap_total, disk_total, disk_free, extra):
    db.execute(
        "INSERT INTO samples(machine,ts,cpu,load1,load5,load15,mem_total,mem_used,mem_pct,swap_total,swap_used,"
        "disk_total,disk_free,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (machine, ts, cpu, cpu / 40, cpu / 45, cpu / 50, mem_total, int(mem_total * mem_pct / 100), mem_pct,
         swap_total, swap_used, disk_total, disk_free, json.dumps(extra)))


def snap(db, machine, kind, data, ts):
    db.execute("INSERT INTO snapshots(machine,ts,kind,data) VALUES(?,?,?,?)", (machine, ts, kind, json.dumps(data)))


def seed(st, mac_age: float):
    db, now = st.db, time.time()
    for i in range(24 * 60, -1, -1):                       # 24 h, one point a minute
        ts = now - i * 60
        wave = math.sin(i / 55.0)
        sample(db, "hetzner", ts, 14 + 6 * wave + (i % 7), 52 + 4 * wave + (36 if i < 14 else 0), 16 * GB, 0, 4 * GB,
               320 * GB, 190 * GB - i * 2_000_000, {"uptime": 19 * 86400, "io_read_mbs": 1.2, "io_write_mbs": 3.4, "io_util": 6,
                                                      "containers": 6, "containers_total": 6})
        if ts < now - mac_age:
            sample(db, "mac", ts, 22 + 14 * math.sin(i / 20.0) + (i % 5), 64 + 6 * math.sin(i / 70.0), 16 * GB,
                   (1.1 + 0.2 * wave) * GB, 3 * GB, 494 * GB, 161 * GB,
                   {"pressure_level": 1, "compressed": 2.4 * GB, "cpu_idle": 71, "cores": 10, "thermal": {"state": "nominal"}})
    mac_ts = now - mac_age
    st.hub.last_seen = mac_ts
    st.db.kv_set("mac_agent_state", {"ts": mac_ts, "clean_stop": False, "host": "demo-macbook", "version": "demo", "agent": "agt_demo"})

    def proc(pid, name, cpu, rss, label=None, **kw):
        return {"pid": pid, "ppid": 1, "name": name, "exe": f"/usr/bin/{name}", "cpu": cpu, "rss": rss * 1_000_000,
                "label": label, "eligible": False, **kw}

    snap(db, "mac", "procs", {
        "top_cpu": [proc(4101, "node", 41.0, 910, "Node.js dev server"), proc(2210, "Code Helper", 18.5, 740),
                    proc(3320, "docker-vm", 9.0, 2400, "Docker VM")],
        "top_mem": [proc(3320, "docker-vm", 9.0, 2400, "Docker VM"), proc(2210, "Code Helper", 18.5, 740),
                    proc(4101, "node", 41.0, 910, "Node.js dev server")],
        "dev": [proc(4101, "node", 41.0, 910, "Node.js dev server")],
        "groups": {"node": {"count": 6, "rss": 1_900_000_000, "cpu": 47.0, "orphans": 0, "idle": 2},
                   "docker-vm": {"count": 1, "rss": 2_400_000_000, "cpu": 9.0, "orphans": 0, "idle": 0}},
        "docker_vm_rss": 2_400_000_000, "docker_running": True}, mac_ts)
    snap(db, "mac", "containers", {"list": []}, mac_ts)

    def ctr(i, name, image, cpu, mem_mb, **kw):
        base = {"id": f"{i:x}" * 64, "short": (f"{i:x}" * 12), "name": name, "image": image, "status": "running", "running": True,
                "health": "healthy", "restarts": 0, "restart_policy": "unless-stopped", "ports": [], "mounts": [],
                "cpu": cpu, "mem": mem_mb * 1_000_000, "log_size": 4_000_000}
        base["id"] = (f"{i:x}" * 64)[:64]
        return base | kw

    containers = [
        ctr(1, "demo-web", "nginx:1.27-alpine", 0.6, 28, ports=["8080->80"]),
        ctr(2, "demo-api", "demo/api:2.3", 7.5, 410, ports=["8000->8000"]),
        ctr(3, "demo-db", "postgres:16", 3.1, 920, mounts=[{"type": "volume", "name": "demo_pgdata", "dest": "/var/lib/postgresql/data"}]),
        ctr(4, "demo-photos", "demo/photo-library:1.9", 4.2, 1900),
        ctr(5, "demo-search-indexer", "demo/indexer:0.4", 63.0, 5400, log_size=640_000_000),
        ctr(6, "demo-cache", "redis:7-alpine", 0.4, 22),
    ]
    snap(db, "hetzner", "containers", {"list": containers}, now)
    snap(db, "hetzner", "procs", {"top_cpu": [proc(900, "demo-indexer", 63.0, 5400, container="demo-search-indexer")],
                                  "top_mem": [proc(900, "demo-indexer", 63.0, 5400, container="demo-search-indexer")]}, now)
    snap(db, "hetzner", "docker_df", {"images_total": 6.2 * GB, "volumes_total": 11.4 * GB, "build_cache_total": 1.1 * GB,
                                      "top_volumes": [{"name": "demo_pgdata", "size": 5.1 * GB}, {"name": "demo_photos", "size": 4.8 * GB}]}, now)
    snap(db, "hetzner", "logs", {"sizes": {c["id"]: c["log_size"] for c in containers}}, now)
    # let the real rule engine turn the last minutes of high memory into a genuine incident
    for k in range(24, -1, -1):
        st.engine.tick(now - k * 30)


class DemoTailscale:
    """Adds the headers `tailscale serve` would add, with a fake identity."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            h = [(k, v) for k, v in scope["headers"] if k not in (b"tailscale-user-login", b"x-forwarded-proto")]
            h += [(b"tailscale-user-login", DEMO_LOGIN.encode()), (b"x-forwarded-proto", b"https")]
            scope = dict(scope, headers=h)
        await self.app(scope, receive, send)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--mac", choices=["connected", "stale", "offline"], default="connected")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="astra-demo-"))
    paths = Paths(str(tmp)); paths.ensure()
    save_env_file(paths.secrets, {"TAILSCALE_OWNER_LOGIN": DEMO_LOGIN})
    app = create_app(paths)
    app.state.st.agents.pair("demo-macbook")                   # the token is discarded; nothing can use it
    app.state.st.engine.service_probe = lambda port: True      # demo has no nginx
    seed(app.state.st, {"connected": 8, "stale": 300, "offline": 3 * 3600}[a.mac])
    print(f"Astra Watch demo (synthetic data) on http://127.0.0.1:{a.port}  [data in {tmp}]")
    uvicorn.run(DemoTailscale(app), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
