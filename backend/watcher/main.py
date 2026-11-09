"""FastAPI application: dashboard API, Mac agent endpoints, Telegram webhook, collection loops."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import docker_cli, security
from . import telegram as tg
from .actions import Actions, ActionError, MacHub
from .collect_linux import LinuxCollector, parse_stat_line
from .config import Paths, Secrets, Settings
from .db import DB
from .automation import Automation, is_denied
from .engine import Engine
from .heartbeat import Heartbeat
from . import views
from .notify import Notifier, webhook_secret_ok

VERSION = "1.1.0"
MAX_BODY = 512 * 1024
HISTORY_DAYS = 30


# ------------------------------------------------------------------ agent payload models
class Sample(BaseModel):
    cpu: float = Field(ge=0, le=100)
    load1: float = 0
    load5: float = 0
    load15: float = 0
    mem_total: int = Field(gt=0)
    mem_used: int = Field(ge=0)
    mem_pct: float = Field(ge=0, le=100)
    swap_total: int = Field(ge=0, default=0)
    swap_used: int = Field(ge=0, default=0)
    disk_total: int = Field(ge=0, default=0)
    disk_free: int = Field(ge=0, default=0)
    extra: dict = Field(default_factory=dict)


class Proc(BaseModel):
    pid: int
    ppid: int = 0
    name: str = Field(max_length=80)
    exe: str = Field(default="", max_length=200)
    user: str = Field(default="", max_length=40)
    start: int = 0
    cpu: float = 0
    rss: int = 0
    label: str = Field(default="other", max_length=24)
    eligible: bool = False
    growing: bool = False
    growth: int = 0
    parent: str = Field(default="", max_length=80)
    why: str = Field(default="", max_length=200)


class Procs(BaseModel):
    top_cpu: list[Proc] = Field(default_factory=list, max_length=15)
    top_mem: list[Proc] = Field(default_factory=list, max_length=15)
    dev: list[Proc] = Field(default_factory=list, max_length=60)
    groups: dict[str, dict] = Field(default_factory=dict)
    docker_vm_rss: int = 0
    count: int = 0


class Payload(BaseModel):
    v: int = 1
    ts: float
    host: str = Field(default="", max_length=80)
    version: str = Field(default="", max_length=20)
    sample: Sample
    procs: Optional[Procs] = None
    containers: Optional[dict] = None
    docker_df: Optional[dict] = None
    storage: Optional[dict] = None
    clean_stop: bool = False


class StopPrepare(BaseModel):
    machine: str
    kind: str
    id: str = Field(default="", max_length=80)
    pid: int = 0
    start: int = 0


class StopToken(BaseModel):
    token: str = Field(max_length=80)


class CmdResult(BaseModel):
    status: str = Field(max_length=30)
    message: str = Field(default="", max_length=300)


# ------------------------------------------------------------------ app state
class State:
    def __init__(self, paths: Paths):
        paths.ensure()
        self.paths = paths
        self.settings = Settings(paths)
        self.secrets = Secrets(paths)
        self.db = DB(paths.db)
        self.hub = MacHub()
        k = self.secrets.hmac_key()
        self.automation = Automation(self.db, self.settings.get, paths.automation)
        self.actions = Actions(self.db, lambda: k, self.hub, mode_fn=self.automation.mode,
                               protected_fn=lambda n: is_denied(n, self.automation.policy()[0]))
        self.tg = tg.Telegram(self.secrets)
        self.notifier = Notifier(self.db, self.settings, self.secrets, self.tg, self.actions)
        self.engine = Engine(self.db, self.settings, self.hub)
        self.heartbeat = Heartbeat(self.secrets, self.engine, lambda: self.settings.get()["refresh_interval"])
        self.agents = security.AgentRegistry(paths)
        self.limiter = security.RateLimiter()
        self.collector = LinuxCollector()
        self.started = time.time()
        self.docker_cache: dict = {"ts": 0, "list": []}
        self.cache_ts = {"procs": 0.0, "containers": 0.0, "docker_df": 0.0, "logs": 0.0}
        self.last_self = None
        self.tasks: list[asyncio.Task] = []


def create_app(paths: Paths | None = None) -> FastAPI:
    st = State(paths or Paths())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st.db.prune(st.settings.get()["retention_hours"])            # enforce on every start
        st.db.prune_history()                                        # 30-day incident events + audit (separate from metrics)
        backfill_events(st)
        if not os.environ.get("WATCHER_NO_LOOPS"):          # tests drive the engine directly
            st.tasks = [asyncio.create_task(_guard(st, collect_loop, "collect")),
                        asyncio.create_task(_guard(st, maintenance_loop, "maintenance")),
                        asyncio.create_task(_guard(st, telegram_poll_loop, "telegram-poll"))]
        yield
        for t in st.tasks:
            t.cancel()

    app = FastAPI(title="Personal Infrastructure Watcher", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.st = st

    # ---------------------------------------------------------------- middleware
    @app.middleware("http")
    async def headers(request: Request, call_next):
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                   "img-src 'self' data:; frame-ancestors 'none'")
        if request.url.path.startswith(("/api", "/agent")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------------------------------------------------------------- auth dependencies
    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    async def owner(request: Request) -> str:
        if not st.limiter.allow("dash:" + client_ip(request), 240, 60):
            raise HTTPException(429, "Too many requests")
        sec = st.secrets.all()
        ts_owner = sec.get("TAILSCALE_OWNER_LOGIN")
        if ts_owner and security.tailscale_owner_ok(request.client.host if request.client else None, request.headers, ts_owner):
            return ts_owner
        # Fail closed: anything else must be a direct loopback connection (e.g. an SSH port-forward).
        if security.is_direct_loopback(request.client.host if request.client else None, request.headers):
            return "owner"
        raise HTTPException(403, "Access denied: open the dashboard through your private Tailscale address.")

    async def csrf(request: Request, who: str = Depends(owner)) -> str:
        if not security.csrf_ok(request.cookies.get("csrf"), request.headers.get("x-csrf-token"),
                                request.headers.get("origin"), request.headers.get("host")):
            raise HTTPException(403, "CSRF check failed")
        return who

    async def agent_auth(request: Request) -> str:
        ip = client_ip(request)
        if not st.limiter.allow("agentfail-check:" + ip, 120, 60):
            raise HTTPException(429, "Too many requests")
        auth = request.headers.get("authorization", "")
        agent = st.agents.verify(auth[7:] if auth.lower().startswith("bearer ") else None)
        if not agent:
            st.limiter.allow("agentfail:" + ip, 10, 300)
            if not st.limiter.allow("agentfail-block:" + ip, 20, 300):
                raise HTTPException(429, "Too many failures")
            raise HTTPException(401, "invalid agent token")
        return agent

    # ---------------------------------------------------------------- public
    @app.get("/healthz")
    async def healthz():
        # Deliberately minimal and free of secrets; used by the external uptime monitor.
        db_ok = True
        try:
            st.db.one("SELECT 1")
        except Exception:
            db_ok = False
        return JSONResponse({"status": "ok" if db_ok else "degraded", "version": VERSION},
                            status_code=200 if db_ok else 503)

    # ---------------------------------------------------------------- Mac agent
    @app.post("/agent/v1/metrics")
    async def agent_metrics(p: Payload, agent: str = Depends(agent_auth)):
        if not st.limiter.allow("agent-metrics:" + agent, 30, 60):
            raise HTTPException(429, "Too many requests")
        now = time.time()
        if abs(p.ts - now) > 600:
            p.ts = now        # a skewed Mac clock must not poison history
        ts = now
        s = p.sample
        st.db.execute(
            "INSERT INTO samples(machine,ts,cpu,load1,load5,load15,mem_total,mem_used,mem_pct,swap_total,swap_used,"
            "disk_total,disk_free,extra) VALUES('mac',?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, s.cpu, s.load1, s.load5, s.load15, s.mem_total, s.mem_used, s.mem_pct, s.swap_total, s.swap_used,
             s.disk_total, s.disk_free, json.dumps(s.extra)[:4000]))
        for kind, data in (("procs", p.procs.model_dump() if p.procs else None), ("containers", p.containers),
                           ("docker_df", p.docker_df), ("storage", p.storage)):
            if data is not None:
                st.db.execute("INSERT INTO snapshots(machine,ts,kind,data) VALUES('mac',?,?,?)",
                              (ts, kind, json.dumps(data)[:200000]))
        st.hub.last_seen = ts
        st.db.kv_set("mac_agent_state", {"ts": ts, "clean_stop": p.clean_stop, "host": p.host, "version": p.version, "agent": agent})
        return {"ok": True, "server_time": ts, "interval": st.settings.get()["refresh_interval"]}

    @app.get("/agent/v1/commands")
    async def agent_commands(wait: int = 20, agent: str = Depends(agent_auth)):
        if not st.limiter.allow("agent-poll:" + agent, 60, 60):
            raise HTTPException(429, "Too many requests")
        cmds = await st.hub.poll(min(max(wait, 0), 25))
        return {"commands": cmds}

    @app.post("/agent/v1/commands/{cmd_id}/result")
    async def agent_result(cmd_id: str, r: CmdResult, agent: str = Depends(agent_auth)):
        if len(cmd_id) > 40 or not st.hub.finish(cmd_id, r.model_dump()):
            raise HTTPException(404, "unknown or expired command")
        return {"ok": True}

    # ---------------------------------------------------------------- Telegram webhook
    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        # Independent of the dashboard login: authenticated by secret header, then sender + chat id checks.
        if not st.limiter.allow("tg:" + client_ip(request), 120, 60):
            raise HTTPException(429, "Too many requests")
        expected = st.secrets.get("TELEGRAM_WEBHOOK_SECRET")
        if not webhook_secret_ok(request.headers.get("x-telegram-bot-api-secret-token"), expected):
            raise HTTPException(403, "forbidden")
        try:
            upd = await request.json()
        except Exception:
            raise HTTPException(400, "bad request") from None
        asyncio.create_task(_safe_handle(st, upd))
        return {"ok": True}

    # ---------------------------------------------------------------- dashboard API
    @app.get("/api/session")
    async def session(request: Request, response: Response, who: str = Depends(owner)):
        tok = request.cookies.get("csrf") or security.new_csrf()
        response.set_cookie("csrf", tok, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
        return {"csrf": tok, "user": who, "version": VERSION}

    @app.get("/api/overview")
    async def overview(_: str = Depends(owner)):
        # Reads cached rows from SQLite only. It never triggers a collection, a Docker call or a process scan.
        sums = {m: machine_summary(st, m) for m in ("mac", "hetzner")}
        att = views.attention(st.db, time.time())
        lv = [sums[m]["status"]["level"] for m in sums]
        counts = {"machines": len(sums), "healthy": lv.count("good"),
                  "need_attention": sum(1 for l in lv if l in ("attention", "critical")),
                  "not_reporting": sum(1 for l in lv if l in ("offline", "unknown")),
                  "critical_incidents": sum(1 for a in att if a["severity"] == "critical"),
                  "active_incidents": len(att),
                  "last_report": max([sums[m]["last_seen"] for m in sums if sums[m]["last_seen"]], default=None)}
        return sums | {
            "overall": views.overall({m: sums[m]["status"] for m in sums}, att, counts),
            "recent": views.activity_list(st.db, 12),
            "attention": att,
            "retention": retention_info(st), "server_time": time.time(),
            "dashboard_refresh_seconds": st.settings.get()["dashboard_refresh_seconds"]}

    @app.get("/api/machine/{machine}")
    async def machine_detail(machine: str, _: str = Depends(owner)):
        if machine not in ("mac", "hetzner"):
            raise HTTPException(404)
        out = machine_summary(st, machine)
        sample = st.engine.latest(machine)
        out["sample"] = sample
        for kind, key in (("procs", "procs"), ("containers", "container_list"), ("docker_df", "docker_df"),
                          ("logs", "logs"), ("storage", "storage")):
            out[key] = st.engine.snap(machine, kind, max_age=10 ** 9)
        if machine == "hetzner":
            logs = (out.get("logs") or {}).get("sizes", {})
            for c in (out.get("container_list") or {}).get("list", []):
                c["log_size"] = logs.get(c["id"])
        return out

    @app.get("/api/history")
    async def history(machine: str, range: str = "1h", _: str = Depends(owner)):
        if machine not in ("mac", "hetzner") or range not in ("1h", "6h", "24h"):
            raise HTTPException(400, "bad parameters")
        secs = {"1h": 3600, "6h": 21600, "24h": 86400}[range]
        now = time.time()
        bucket = max(secs / 180, 10)
        rows = st.db.query(
            "SELECT CAST((ts-?)/? AS INT) AS b, AVG(ts) AS ts, AVG(cpu) AS cpu, AVG(mem_pct) AS mem, AVG(swap_used) AS swap,"
            " AVG(disk_free) AS disk_free, AVG(disk_total) AS disk_total, AVG(json_extract(extra,'$.containers')) AS containers,"
            " AVG(load1) AS load1 FROM samples WHERE machine=? AND ts>=? GROUP BY b ORDER BY b",
            (now - secs, bucket, machine, now - secs))
        first = st.db.one("SELECT MIN(ts) m FROM samples WHERE machine=? AND ts>=?", (machine, now - secs))["m"]
        # Only this machine's own samples are ever returned. `available_seconds` says how much of the range really exists.
        return {"machine": machine, "range": range, "points": [dict(r) for r in rows],
                "available_seconds": None if first is None else round(now - first)}

    @app.get("/api/alerts")
    async def alerts(state: str = "all", _: str = Depends(owner)):
        q = "SELECT * FROM incidents"
        if state == "active":
            q += " WHERE state='active'"
        q += " ORDER BY (state='active') DESC, first_seen DESC LIMIT 200"
        out = []
        for r in st.db.query(q):
            d = dict(r)
            for k in ("measured", "targets"):
                d[k] = json.loads(d[k] or ("{}" if k == "measured" else "[]"))
            d["notified"] = d["notify_state"] == "sent"
            out.append(d)
        return {"alerts": out}

    @app.get("/api/activity")
    async def activity(_: str = Depends(owner)):
        return {"items": views.activity_list(st.db, 100), "history_days": HISTORY_DAYS}

    @app.get("/api/activity/{incident_id}")
    async def activity_one(incident_id: str, _: str = Depends(owner)):
        d = views.activity_detail(st.db, incident_id[:40], time.time())
        if not d:
            raise HTTPException(404, "Unknown incident")
        return d

    @app.get("/api/automation")
    async def automation(_: str = Depends(owner)):
        # Read-only by design: the policy is edited with `watcherctl automation-*` on the server.
        sim = st.automation.simulate()
        hist = [dict(r) for r in st.db.query(
            "SELECT ts,actor,action,machine,target,outcome,detail FROM audit WHERE action LIKE 'stop.%' OR action LIKE 'dry_run.%' "
            "ORDER BY ts DESC LIMIT 100")]
        last = next((h for h in hist if h["action"] == "stop.execute" and h["outcome"] == "stopped"), None)
        return sim | {"history": hist, "last_action": last, "history_days": HISTORY_DAYS, "editable_here": False}

    @app.get("/api/audit")
    async def audit(_: str = Depends(owner)):
        return {"actions": [dict(r) for r in st.db.query(
            "SELECT * FROM audit WHERE action LIKE 'stop.%' OR action LIKE 'telegram.%' ORDER BY ts DESC LIMIT 200")],
            "notifications": [dict(r) for r in st.db.query("SELECT * FROM notifications ORDER BY ts DESC LIMIT 50")]}

    @app.get("/api/settings")
    async def get_settings(request: Request, _: str = Depends(owner)):
        return settings_view(st, request)

    @app.put("/api/settings")
    async def put_settings(patch: dict, request: Request, _: str = Depends(csrf)):
        try:
            st.settings.update(patch)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        st.db.audit("owner", "settings.update", outcome="ok", detail=",".join(patch.keys()))
        return settings_view(st, request)

    @app.post("/api/telegram/test")
    async def telegram_test(_: str = Depends(csrf)):
        if not st.limiter.allow("tgtest", 3, 60):
            raise HTTPException(429, "Slow down")
        if not st.tg.configured():
            return {"ok": False, "error": "Telegram is not configured. Run: watcherctl setup-telegram"}
        try:
            await asyncio.to_thread(
                st.tg.send, "<b>ASTRA WATCH</b>\n\n✅ Test notification.\nTelegram is connected.\n\n"
                            f"{st.settings.get()['public_url']}")
            st.db.execute("INSERT INTO notifications(ts,incident_id,kind,status,detail) VALUES(?,?,?,?,?)",
                          (time.time(), None, "test", "sent", ""))
            return {"ok": True}
        except tg.TelegramError as e:
            st.db.execute("INSERT INTO notifications(ts,incident_id,kind,status,detail) VALUES(?,?,?,?,?)",
                          (time.time(), None, "test", "failed", str(e)[:200]))
            return {"ok": False, "error": str(e)}

    @app.post("/api/stop/prepare")
    async def stop_prepare(body: StopPrepare, request: Request, _: str = Depends(csrf)):
        if not st.limiter.allow("stopprep", 20, 60):
            raise HTTPException(429, "Slow down")
        ref = {"id": body.id} if body.id else {"pid": body.pid, "start": body.start}
        try:
            return await st.actions.prepare(body.machine, body.kind, ref, "dashboard", "owner")
        except ActionError as e:
            raise HTTPException(409, {"code": e.code, "message": str(e)}) from None

    @app.post("/api/stop/confirm")
    async def stop_confirm(body: StopToken, _: str = Depends(csrf)):
        if not st.limiter.allow("stopconf", 10, 60):
            raise HTTPException(429, "Slow down")
        res = await st.actions.execute(body.token, "owner", None, "dashboard")
        return res

    @app.post("/api/stop/cancel")
    async def stop_cancel(body: StopToken, _: str = Depends(csrf)):
        return {"cancelled": st.actions.cancel(body.token, "owner")}

    # ---------------------------------------------------------------- static SPA
    static = st.paths.static
    if static.exists():
        if (static / "assets").exists():
            app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{full_path:path}")
        async def spa(full_path: str, request: Request, _: str = Depends(owner)):
            if full_path.startswith(("api/", "agent/", "telegram/")):
                raise HTTPException(404)
            f = (static / full_path).resolve()
            if full_path and f.is_file() and static.resolve() in f.parents:
                return FileResponse(f)
            return FileResponse(static / "index.html")
    return app


# ------------------------------------------------------------------ helpers
def connection_state(age: float | None, grace: float, offline_after: float) -> str:
    """connected: reported within `grace`; stale: missed reports but not yet long enough to call it gone
    (a sleeping laptop); offline: silent for `offline_after` or more. No data at all -> unknown."""
    if age is None:
        return "unknown"
    if age < grace:
        return "connected"
    return "stale" if age < max(offline_after, grace) else "offline"


def machine_summary(st: State, machine: str) -> dict:
    now = time.time()
    s = st.engine.latest(machine)
    t = st.settings.thresholds()
    interval = st.settings.get()["refresh_interval"]
    incs = st.db.query("SELECT id,severity,title FROM incidents WHERE state='active'  AND machine=?", (machine,))
    last = s["ts"] if s else None
    if machine == "mac" and last:
        last = max(last, st.hub.last_seen)
    if machine == "mac":
        grace, offline_after = t["agent_grace_seconds"], t["agent_notify_minutes"] * 60
    else:
        grace = max(90, 4 * interval)
        offline_after = 5 * grace
    conn = connection_state(None if last is None else now - last, grace, offline_after)
    state = st.db.kv_get("mac_agent_state", {}) if machine == "mac" else {}
    status = views.machine_status(st.db, machine, conn, now)     # the single source of truth for health
    health = {"good": "healthy", "attention": "warning", "critical": "critical", "offline": "offline"}.get(status["level"], "stale" if conn == "stale" else "unknown")
    cont = st.engine.snap(machine, "containers", max_age=900)
    return {
        "machine": machine, "health": health, "connection": conn, "last_update": s["ts"] if s else None,
        "last_seen": last, "age_seconds": None if last is None else round(now - last, 1),
        "fresh": conn == "connected", "server_time": now,
        "cpu": s["cpu"] if s else None, "mem_pct": s["mem_pct"] if s else None,
        "mem_total": s["mem_total"] if s else None, "mem_used": s["mem_used"] if s else None,
        "swap_used": s["swap_used"] if s else None, "swap_total": s["swap_total"] if s else None,
        "disk_free": s["disk_free"] if s else None, "disk_total": s["disk_total"] if s else None,
        "containers": len([c for c in cont["list"] if c.get("running")]) if cont else None,
        "alerts": [dict(i) for i in incs],
        "status": status,
        "resources": views.resource_lines(machine, s, t, views.active_incidents(st.db, machine)),
        "unhealthy_containers": len([c for c in cont["list"] if c.get("running") and c.get("health") == "unhealthy"]) if cont else None,
        "agent": ({"host": state.get("host"), "version": state.get("version"), "clean_stop": state.get("clean_stop")}
                  if machine == "mac" else None),
    }


def retention_info(st: State) -> dict:
    oldest = st.db.oldest_record()
    return {"hours": st.settings.get()["retention_hours"], "max_hours": 24, "oldest": oldest, "history_days": HISTORY_DAYS,
            "db_bytes": st.db.size_bytes(), "last_prune": st.db.kv_get("last_prune")}


def remote_access(sec: dict, request: Request) -> dict:
    """What THIS request tells us about how the dashboard is being reached (no shelling out to tailscale)."""
    h = request.headers
    via = security.tailscale_owner_ok(request.client.host if request.client else None, h, sec.get("TAILSCALE_OWNER_LOGIN"))
    return {"method": "Tailscale", "connected": via, "owner_restricted": bool(sec.get("TAILSCALE_OWNER_LOGIN")),
            "https": request.url.scheme == "https" or h.get("x-forwarded-proto") == "https" or via,
            "funnel": "tailscale-funnel-request" in h, "access": "Private tailnet"}


def settings_view(st: State, request: Request) -> dict:
    sec = st.secrets.all()
    last = st.db.one("SELECT ts,status,detail FROM notifications WHERE kind='test' ORDER BY ts DESC LIMIT 1")
    return {
        "settings": st.settings.get(),
        "telegram": {"configured": st.tg.configured(), "mode": sec.get("TELEGRAM_MODE", "webhook"),
                     "webhook_secret_set": bool(sec.get("TELEGRAM_WEBHOOK_SECRET")),
                     "last_test": dict(last) if last else None},
        "remote_access": remote_access(sec, request),
        "watchdog": st.heartbeat.status(),
        "agents": st.agents.list(),
        "agent_state": st.db.kv_get("mac_agent_state", {}),
        "retention": retention_info(st),
        "self": st.engine.self_state,
        "uptime": time.time() - st.started,
        "version": VERSION,
        "notifications": [dict(r) for r in st.db.query("SELECT * FROM notifications ORDER BY ts DESC LIMIT 10")],
    }


# ------------------------------------------------------------------ background loops
async def _guard(st: State, fn, name: str):
    while True:
        try:
            await fn(st)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                st.db.log("error", f"{name} crashed: {type(e).__name__}: {e}")
            except Exception:
                pass
            await asyncio.sleep(10)


async def _safe_handle(st: State, upd: dict):
    try:
        await st.notifier.handle_update(upd)
    except Exception as e:
        st.db.log("error", f"telegram handler: {type(e).__name__}")


def _iso_age(iso: str | None) -> float:
    try:
        if not iso or iso.startswith("0001"):
            return 1e9
        return time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00").split(".")[0] + "+00:00").timestamp()
    except Exception:
        return 1e9


def collect_hetzner_once(st: State) -> None:
    c = st.collector
    now = time.time()
    host = c.host()
    extra = host.pop("extra")
    # --- containers (metadata every 60 s, resource stats every cycle from cgroups)
    if now - st.cache_ts["containers"] >= 60 or not st.docker_cache["list"]:
        try:
            st.docker_cache = {"ts": now, "list": docker_cli.containers()}
            st.cache_ts["containers"] = now
            st.collector_docker_ok = True
        except docker_cli.DockerError as e:
            st.docker_cache["error"] = str(e)
            st.collector_docker_ok = False
    lst = st.docker_cache["list"]
    stats = c.container_stats([x["id"] for x in lst if x["running"]])
    extra["containers"] = sum(1 for x in lst if x["running"])
    extra["containers_total"] = len(lst)
    st.db.execute(
        "INSERT INTO samples(machine,ts,cpu,load1,load5,load15,mem_total,mem_used,mem_pct,swap_total,swap_used,disk_total,"
        "disk_free,extra) VALUES('hetzner',?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, host["cpu"], host["load1"], host["load5"], host["load15"], host["mem_total"], host["mem_used"], host["mem_pct"],
         host["swap_total"], host["swap_used"], host["disk_total"], host["disk_free"], json.dumps(extra)))
    if now - st.cache_ts["procs"] >= 60:
        st.cache_ts["procs"] = now
        procs = c.processes()
        names = {x["id"]: x["name"] for x in lst}
        for p in procs["top_cpu"] + procs["top_mem"]:
            if p.get("container"):
                p["container_name"] = names.get(p["container"], p["container"][:12])
        st.db.execute("INSERT INTO snapshots(machine,ts,kind,data) VALUES('hetzner',?,'procs',?)", (now, json.dumps(procs)))
        merged = []
        for x in lst:
            y = {k: v for k, v in x.items() if k not in ("log_path", "networks")}
            y.update(stats.get(x["id"], {"cpu": 0, "mem": 0}))
            y["finished_ago"] = _iso_age(x.get("finished")) if not x["running"] else 0
            merged.append(y)
        st.db.execute("INSERT INTO snapshots(machine,ts,kind,data) VALUES('hetzner',?,'containers',?)",
                      (now, json.dumps({"list": merged})))
    else:
        # keep latest container snapshot's live numbers fresh for the engine between full snapshots
        pass
    if now - st.cache_ts["logs"] >= 900:
        st.cache_ts["logs"] = now
        try:
            sizes = docker_cli.log_sizes()
            st.db.execute("INSERT INTO snapshots(machine,ts,kind,data) VALUES('hetzner',?,'logs',?)",
                          (now, json.dumps({"sizes": sizes})))
        except docker_cli.DockerError as e:
            st.db.log("warn", f"log sizing unavailable: {e}")
    if now - st.cache_ts["docker_df"] >= 1800:
        st.cache_ts["docker_df"] = now
        try:
            st.db.execute("INSERT INTO snapshots(machine,ts,kind,data) VALUES('hetzner',?,'docker_df',?)",
                          (now, json.dumps(docker_cli.system_df())))
        except (docker_cli.DockerError, ValueError, KeyError) as e:
            st.db.log("warn", f"docker df unavailable: {type(e).__name__}")


async def collect_loop(st: State):
    # prime counters so the first real sample has meaningful deltas
    st.collector.host()
    st.collector.processes()
    st.cache_ts["procs"] = 0.0
    await asyncio.sleep(3)
    prev = None
    while True:
        started = time.time()
        try:
            await asyncio.to_thread(collect_hetzner_once, st)
            st.engine.collector_errors["hetzner"] = 0
        except Exception as e:
            st.engine.collector_errors["hetzner"] += 1
            st.db.log("error", f"collect failed: {type(e).__name__}: {e}")
        try:
            me = parse_stat_line(Path("/proc/self/stat").read_text()) if Path("/proc/self/stat").exists() else None
            if me:
                cpu = 0.0
                if prev:
                    cpu = 100.0 * (me["ticks"] - prev[0]) / os.sysconf("SC_CLK_TCK") / max(started - prev[1], 1)
                prev = (me["ticks"], started)
                st.engine.self_state = {"rss": me["rss"], "cpu": round(cpu, 2), "db_size": st.db.size_bytes()}
            await asyncio.to_thread(st.engine.tick)
            await st.notifier.process()
            await asyncio.to_thread(st.automation.tick)          # dry run only; no-op unless something is allow-listed
            await asyncio.to_thread(st.heartbeat.tick)           # outbound ping to the external watchdog, if configured
        except Exception as e:
            st.db.log("error", f"engine failed: {type(e).__name__}: {e}")
        await asyncio.sleep(max(5, st.settings.get()["refresh_interval"] - (time.time() - started)))


async def maintenance_loop(st: State):
    last_backup = 0.0
    while True:
        await asyncio.sleep(3600)
        removed = await asyncio.to_thread(st.db.prune, st.settings.get()["retention_hours"])
        await asyncio.to_thread(st.db.prune_history)
        if time.time() - last_backup > 86400:
            last_backup = time.time()
            await asyncio.to_thread(backup, st)


def backup(st: State) -> Path:
    """Copy the small DB (online-consistent) and config. DB backups older than 24 h are removed so the
    retention limit also holds for backups."""
    d = st.paths.base / "backups"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = d / f"watcher-{stamp}.sqlite3"
    st.db.checkpoint_backup(dest)
    os.chmod(dest, 0o600)
    cfg = d / f"config-{stamp}"
    shutil.copytree(st.paths.config, cfg)
    for p in list(d.glob("watcher-*.sqlite3")):
        if time.time() - p.stat().st_mtime > 24 * 3600:
            p.unlink()
    for p in sorted(d.glob("config-*"))[:-7]:
        shutil.rmtree(p, ignore_errors=True)
    return dest


async def telegram_poll_loop(st: State):
    """Fallback transport: getUpdates long-polling (no public endpoint needed). Only active in polling mode."""
    offset = 0
    while True:
        if st.secrets.get("TELEGRAM_MODE") != "polling" or not st.tg.configured():
            await asyncio.sleep(10)
            continue
        try:
            updates = await asyncio.to_thread(st.tg.get_updates, offset, 20)
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
                await _safe_handle(st, u)
        except tg.TelegramError:
            await asyncio.sleep(10)


def backfill_events(st: State) -> None:
    """Incidents created before event history existed get a single 'detected' event so they appear in Activity."""
    for r in st.db.query("SELECT id,machine,rule,severity,title,active_since,first_seen,state,recovered_at FROM incidents "
                         "WHERE id NOT IN (SELECT DISTINCT incident_id FROM incident_events)"):
        st.db.event(r["id"], "detected", "Recorded before event history existed", machine=r["machine"], rule=r["rule"],
                    severity=r["severity"], title=r["title"], ts=r["active_since"] or r["first_seen"])
        if r["state"] == "recovered" and r["recovered_at"]:
            st.db.event(r["id"], "recovered", "Alert cleared", machine=r["machine"], rule=r["rule"], severity=r["severity"],
                        title=r["title"], ts=r["recovered_at"])
