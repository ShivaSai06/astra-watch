"""Two-step, single-use, identity-bound stop approvals (shared by Dashboard and Telegram).

Flow:  prepare()  -> pending approval (2 min, HMAC-bound to exact target + action + actor)
       execute()  -> atomically consume token, revalidate the live target, gracefully stop, verify.

Nothing in this module is reachable without an explicit prepare()+execute() pair, and nothing
here ever kills, removes, prunes or restarts anything.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

from . import docker_cli
from .db import DB

APPROVAL_TTL = 120          # seconds a confirmation stays valid
MAC_FRESH_SECONDS = 90      # agent must have reported within this long to accept a stop
MAC_PICKUP_SECONDS = 25     # agent must pick the command up within this long, else it is discarded
MAC_RESULT_SECONDS = 45

RESULTS = {
    "stopped": "Successfully stopped",
    "failed": "Stop failed",
    "gone": "Target no longer exists",
    "changed": "Target changed; approval rejected",
    "mac_offline": "Mac agent offline",
    "hetzner_unavailable": "Hetzner unavailable",
    "expired": "Approval expired",
    "used": "Approval already used",
    "invalid": "Approval invalid",
    "unauthorized": "Not authorised",
}


class ActionError(Exception):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or RESULTS.get(code, code))
        self.code = code


@dataclass
class MacHub:
    """In-memory rendezvous between dashboard/Telegram approvals and the polling Mac agent.
    Commands are never persisted, so a stale stop request can never be replayed later."""
    last_seen: float = 0.0
    commands: dict = field(default_factory=dict)       # cmd_id -> dict
    results: dict = field(default_factory=dict)        # cmd_id -> asyncio.Future
    wake: asyncio.Event | None = None

    def online(self) -> bool:
        return time.time() - self.last_seen < MAC_FRESH_SECONDS

    def _event(self) -> asyncio.Event:
        if self.wake is None:
            self.wake = asyncio.Event()
        return self.wake

    async def dispatch(self, payload: dict) -> dict:
        if not self.online():
            raise ActionError("mac_offline")
        cmd_id = secrets.token_urlsafe(12)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.results[cmd_id] = fut
        self.commands[cmd_id] = {"id": cmd_id, "created": time.time(), "delivered": False, **payload}
        self._event().set()
        try:
            # Wait for pickup first; an undelivered command is discarded rather than left queued.
            deadline = time.time() + MAC_PICKUP_SECONDS
            while time.time() < deadline and not self.commands.get(cmd_id, {}).get("delivered"):
                await asyncio.sleep(0.25)
            if not self.commands.get(cmd_id, {}).get("delivered"):
                raise ActionError("mac_offline", "Mac agent did not pick up the request in time; nothing was stopped.")
            return await asyncio.wait_for(fut, MAC_RESULT_SECONDS)
        except asyncio.TimeoutError:
            raise ActionError("failed", "Mac agent did not report a result in time; verify on the Mac.") from None
        finally:
            self.commands.pop(cmd_id, None)
            self.results.pop(cmd_id, None)

    async def poll(self, wait: float) -> list[dict]:
        self.last_seen = time.time()
        end = time.time() + wait
        while True:
            fresh = [c for c in self.commands.values()
                     if not c["delivered"] and time.time() - c["created"] < MAC_PICKUP_SECONDS]
            if fresh:
                for c in fresh:
                    c["delivered"] = True
                return [{k: v for k, v in c.items() if k not in ("created", "delivered")} for c in fresh]
            remaining = end - time.time()
            if remaining <= 0:
                return []
            ev = self._event()
            ev.clear()
            try:
                await asyncio.wait_for(ev.wait(), min(remaining, 5))
            except asyncio.TimeoutError:
                pass
            self.last_seen = time.time()

    def finish(self, cmd_id: str, result: dict) -> bool:
        fut = self.results.get(cmd_id)
        if fut and not fut.done():
            fut.set_result(result)
            return True
        return False


def _bind(key: bytes, row: dict) -> str:
    msg = "|".join([row["id"], row["machine"], row["kind"], row["action"],
                    json.dumps(row["target"], sort_keys=True, separators=(",", ":")),
                    str(int(row["expires"])), str(row.get("actor") or ""), str(row.get("chat_id") or "")])
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


class Actions:
    def __init__(self, db: DB, hmac_key_fn, hub: MacHub):
        self.db = db
        self._key = hmac_key_fn
        self.hub = hub

    # ------------------------------------------------------------ offers
    def create_offer(self, incident_id: str, machine: str, kind: str, target: dict, label: str) -> str:
        oid = secrets.token_urlsafe(8)
        self.db.execute("INSERT INTO offers(id,incident_id,machine,kind,target,label,created) VALUES(?,?,?,?,?,?,?)",
                        (oid, incident_id, machine, kind, json.dumps(target), label, time.time()))
        return oid

    def get_offer(self, oid: str) -> dict | None:
        r = self.db.one("SELECT * FROM offers WHERE id=?", (oid,))
        if not r:
            return None
        d = dict(r)
        d["target"] = json.loads(d["target"])
        return d

    # ------------------------------------------------------------ prepare
    async def prepare(self, machine: str, kind: str, ref: dict, source: str, actor: str,
                      chat_id: str | None = None) -> dict:
        """Look the target up server-side (never trust client-supplied identity), then mint an approval."""
        if machine == "hetzner" and kind == "container":
            target, details = await asyncio.to_thread(self._hetzner_target, ref.get("id", ""))
        elif machine == "mac" and kind in ("process", "container"):
            target, details = self._mac_target(kind, ref)
        else:
            raise ActionError("invalid", "Unsupported stop target")
        now = time.time()
        row = {"id": secrets.token_urlsafe(16), "machine": machine, "kind": kind, "action": "stop",
               "target": target, "actor": actor, "chat_id": chat_id, "expires": now + APPROVAL_TTL}
        row["mac"] = _bind(self._key(), row)
        self.db.execute(
            "INSERT INTO approvals(id,machine,kind,action,target,summary,source,actor,created,expires,state,mac,chat_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], machine, kind, "stop", json.dumps(target, sort_keys=True), json.dumps(details), source, actor,
             now, row["expires"], "pending", row["mac"], chat_id))
        self.db.audit(actor, "stop.prepare", machine, target.get("name") or str(target.get("pid")), "pending", source)
        return {"token": row["id"], "expires": row["expires"], "machine": machine, "kind": kind,
                "target": target, "details": details}

    def _hetzner_target(self, cid: str):
        if not docker_cli.valid_id(cid):
            raise ActionError("invalid", "Container id must be the full 64-character id")
        try:
            allc = docker_cli.containers()
        except docker_cli.DockerError as e:
            raise ActionError("hetzner_unavailable", str(e)) from e
        c = next((x for x in allc if x["id"] == cid), None)
        if not c:
            raise ActionError("gone")
        if not c["running"]:
            raise ActionError("gone", f"{c['name']} is not running.")
        target = {"id": c["id"], "name": c["name"], "image_id": c["image_id"], "started": c["started"]}
        siblings = [x["name"] for x in allc if x["running"] and x["id"] != cid and c["project"]
                    and x["project"] == c["project"]]
        dependents = [x["name"] for x in allc if x["running"] and c["project"] and x["project"] == c["project"]
                      and c["service"] and c["service"] in (x["depends_on"] or "")]
        vols = [m["name"] for m in c["mounts"] if m["type"] == "volume"]
        impact = [f"{c['name']} will stop. Its image, container and {len(vols)} volume(s) are kept; nothing is deleted."]
        if c["ports"]:
            impact.append("Published ports go away: " + ", ".join(c["ports"][:4]))
        if dependents:
            impact.append("Services that depend on it: " + ", ".join(dependents))
        elif siblings:
            impact.append(f"Same project ({c['project']}): " + ", ".join(siblings[:6]) + " may stop working properly.")
        if c["restart_policy"] in ("always",):
            impact.append("Restart policy is 'always': Docker will start it again if the Docker service restarts.")
        elif c["restart_policy"] == "unless-stopped":
            impact.append("Restart policy is 'unless-stopped': it stays stopped until you start it yourself.")
        details = {"name": c["name"], "id": c["id"], "short": c["short"], "image": c["image"], "status": c["status"],
                   "health": c["health"], "restart_policy": c["restart_policy"], "ports": c["ports"],
                   "volumes": vols, "project": c["project"], "dependents": dependents, "impact": impact}
        return target, details

    def _mac_target(self, kind: str, ref: dict):
        if not self.hub.online():
            raise ActionError("mac_offline")
        snap = self.db.one("SELECT data FROM snapshots WHERE machine='mac' AND kind=? ORDER BY ts DESC LIMIT 1",
                           ("procs" if kind == "process" else "containers",))
        if not snap:
            raise ActionError("gone", "No recent Mac data.")
        data = json.loads(snap["data"])
        if kind == "process":
            pool = {}
            for key in ("top_cpu", "top_mem", "dev"):
                for p in data.get(key, []):
                    pool[p["pid"]] = p
            p = pool.get(int(ref.get("pid", -1)))
            if not p:
                raise ActionError("gone")
            if not p.get("eligible"):
                raise ActionError("unauthorized", f"{p['name']} is a protected or system process and cannot be stopped from here.")
            target = {"pid": p["pid"], "start": p["start"], "exe": p["exe"], "name": p["name"],
                      "user": p["user"], "ppid": p["ppid"]}
            impact = [f"{p['name']} (PID {p['pid']}) receives a polite stop request (SIGTERM). Unsaved work in it may be lost.",
                      f"Started by parent PID {p['ppid']}" + (f" ({p.get('parent')})" if p.get("parent") else "") + ".",
                      "It is NOT force-killed: if it ignores the request, it keeps running and you are told."]
            details = {"name": p["name"], "pid": p["pid"], "exe": p["exe"], "ppid": p["ppid"], "cpu": p.get("cpu"),
                       "rss": p.get("rss"), "label": p.get("label"), "why": p.get("why"), "impact": impact}
            return target, details
        cid = ref.get("id", "")
        if not docker_cli.valid_id(cid):
            raise ActionError("invalid", "Container id must be the full 64-character id")
        c = next((x for x in data.get("list", []) if x["id"] == cid), None)
        if not c or not c.get("running"):
            raise ActionError("gone")
        target = {"id": c["id"], "name": c["name"], "image_id": c.get("image_id", ""), "started": c.get("started", "")}
        details = {"name": c["name"], "id": c["id"], "short": c["id"][:12], "image": c.get("image"),
                   "impact": [f"Container {c['name']} on your Mac stops gracefully (docker stop). Volumes are kept."]}
        return target, details

    # ------------------------------------------------------------ execute
    async def execute(self, token: str, actor: str, chat_id: str | None = None, source: str = "dashboard") -> dict:
        row = self.db.one("SELECT * FROM approvals WHERE id=?", (token,))
        if not row:
            return self._done(None, "invalid", "Unknown approval", actor)
        row = dict(row)
        row["target"] = json.loads(row["target"])
        # 1. integrity + binding + authorisation
        if not hmac.compare_digest(_bind(self._key(), row), row["mac"]):
            return self._done(row, "invalid", "Approval integrity check failed", actor)
        if str(row.get("actor") or "") != str(actor) or (row.get("chat_id") or None) != (chat_id or None):
            return self._done(row, "unauthorized", "Approval belongs to a different user or chat", actor, mark=False)
        # 2. single-use, atomic
        now = time.time()
        cur = self.db.execute("UPDATE approvals SET state='used' WHERE id=? AND state='pending' AND expires>?",
                              (token, now))
        if cur.rowcount != 1:
            if row["state"] == "used":
                return self._done(row, "used", None, actor, mark=False)
            self.db.execute("UPDATE approvals SET state='expired' WHERE id=? AND state='pending'", (token,))
            return self._done(row, "expired", None, actor, mark=False)
        # 3. revalidate the live target and perform
        try:
            if row["machine"] == "hetzner":
                res = await self._stop_hetzner(row["target"])
            else:
                res = await self._stop_mac(row)
        except ActionError as e:
            res = {"status": e.code, "message": str(e)}
        except Exception as e:  # never leak internals
            res = {"status": "failed", "message": f"Unexpected error: {type(e).__name__}"}
        return self._done(row, res["status"], res.get("message"), actor, extra=res)

    def _done(self, row, status: str, message: str | None, actor: str, mark: bool = True, extra=None) -> dict:
        res = {"status": status, "title": RESULTS.get(status, status), "message": message or RESULTS.get(status, status),
               "ts": time.time(), "target": (row or {}).get("target"), "machine": (row or {}).get("machine")}
        if row and mark:
            self.db.execute("UPDATE approvals SET result=?, finished=? WHERE id=?", (json.dumps(res), res["ts"], row["id"]))
        tname = ((row or {}).get("target") or {}).get("name") or str(((row or {}).get("target") or {}).get("pid"))
        self.db.audit(actor, "stop.execute", (row or {}).get("machine"), tname, status, message)
        return res

    async def _stop_hetzner(self, t: dict) -> dict:
        try:
            try:
                cur = await asyncio.to_thread(docker_cli.inspect_many, [t["id"]])
            except docker_cli.DockerError as e:
                if "No such" in str(e):
                    raise ActionError("gone") from None
                raise ActionError("hetzner_unavailable", str(e)) from None
            if not cur:
                raise ActionError("gone")
            c = docker_cli.summarize(cur[0])
            if c["id"] != t["id"] or c["name"] != t["name"] or c["image_id"] != t["image_id"] or c["started"] != t["started"]:
                raise ActionError("changed", f"{t['name']} was restarted or replaced since you were asked; nothing was stopped.")
            if not c["running"]:
                raise ActionError("gone", f"{t['name']} is already stopped.")
            try:
                await asyncio.to_thread(docker_cli.stop, t["id"], 30)
            except docker_cli.DockerError as e:
                raise ActionError("failed", f"docker stop failed: {e}") from None
            after = await asyncio.to_thread(docker_cli.inspect_many, [t["id"]])
            if after and not docker_cli.summarize(after[0])["running"]:
                return {"status": "stopped", "message": f"{t['name']} stopped gracefully. Container and volumes are untouched."}
            raise ActionError("failed", f"{t['name']} is still running after docker stop.")
        except docker_cli.DockerError as e:
            raise ActionError("hetzner_unavailable", str(e)) from None

    async def _stop_mac(self, row: dict) -> dict:
        res = await self.hub.dispatch({"type": f"stop_{row['kind']}", "approval": row["id"],
                                       "target": row["target"], "expires": row["expires"]})
        status = res.get("status", "failed")
        if status not in RESULTS:
            status = "failed"
        return {"status": status, "message": res.get("message", RESULTS[status])}

    def cancel(self, token: str, actor: str) -> bool:
        cur = self.db.execute("UPDATE approvals SET state='cancelled' WHERE id=? AND state='pending' AND actor=?",
                              (token, str(actor)))
        if cur.rowcount:
            self.db.audit(actor, "stop.cancel", None, None, "cancelled")
        return bool(cur.rowcount)
