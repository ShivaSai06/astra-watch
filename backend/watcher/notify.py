"""Notification policy + Telegram bot interaction.

Policy: one notification per continuing incident, escalation (warning -> critical) sends one more,
a cooldown suppresses flapping repeats of the same problem, recovery sends one "cleared" message.
Delivery is retried with backoff and the status is recorded on the incident.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import time

from . import explain
from . import telegram as tg
from .actions import RESULTS, ActionError, Actions
from .db import DB

MAX_ATTEMPTS = 5


class Notifier:
    def __init__(self, db: DB, settings, secrets, telegram: tg.Telegram, actions: Actions):
        self.db, self.settings, self.secrets, self.tg, self.actions = db, settings, secrets, telegram, actions

    def base_url(self) -> str:
        return self.settings.get()["public_url"]

    def _inc(self, row) -> dict:
        d = dict(row)
        d["measured"] = json.loads(d["measured"] or "{}")
        d["targets"] = json.loads(d["targets"] or "[]")
        return d

    def _log(self, incident_id, kind, status, detail=""):
        self.db.execute("INSERT INTO notifications(ts,incident_id,kind,status,detail) VALUES(?,?,?,?,?)",
                        (time.time(), incident_id, kind, status, detail[:300]))

    async def process(self, now: float | None = None) -> None:
        now = now or time.time()
        cooldown = self.settings.get()["cooldown_minutes"] * 60
        configured = self.tg.configured()
        for row in self.db.query("SELECT * FROM incidents WHERE state='active'"):
            inc = self._inc(row)
            if not configured:
                if inc["notify_state"] in ("none", "suppressed"):
                    self.db.execute("UPDATE incidents SET notify_state='unconfigured' WHERE id=?", (inc["id"],))
                continue
            if inc["notify_state"] == "unconfigured":
                inc["notify_state"] = "none"
            escalated = bool(inc["notify_state"] == "sent" and inc["notified_severity"]
                             and _rank(inc["severity"]) > _rank(inc["notified_severity"]))
            if inc["notify_state"] in ("none", "suppressed", "failed_retry") or escalated:
                if inc["next_attempt"] and now < inc["next_attempt"]:
                    continue
                if not escalated:
                    last = self.db.one("SELECT MAX(notified_at) AS t FROM incidents WHERE rule=? AND machine=? AND subject=? "
                                       "AND id!=? AND notified_at IS NOT NULL", (inc["rule"], inc["machine"], inc["subject"], inc["id"]))
                    if last and last["t"] and now - last["t"] < cooldown:
                        if inc["notify_state"] != "suppressed":
                            self.db.execute("UPDATE incidents SET notify_state='suppressed' WHERE id=?", (inc["id"],))
                            self._log(inc["id"], "alert", "suppressed", "cooldown")
                        continue
                await self._send_alert(inc, bool(escalated), now)
        if configured:
            for row in self.db.query("SELECT * FROM incidents WHERE state='recovered' AND notify_state='sent' AND recovery_notified=0"):
                inc = self._inc(row)
                if not self.settings.get()["recovery_notifications"]:
                    self.db.execute("UPDATE incidents SET recovery_notified=2 WHERE id=?", (inc["id"],))
                    continue
                try:
                    acts = [r["detail"].replace("You approved ", "").strip() for r in self.db.query(
                        "SELECT detail FROM incident_events WHERE incident_id=? AND kind='action'", (inc["id"],))]
                    sample = self.db.one("SELECT * FROM samples WHERE machine=? ORDER BY ts DESC LIMIT 1", (inc["machine"],))
                    improved = explain.recovery_facts(inc, dict(sample) if sample else None)
                    text = tg.recovery_message(inc, self.base_url(), (inc["recovered_at"] or now) - (inc["active_since"] or now),
                                               improved, acts)
                    await asyncio.to_thread(self.tg.send, text)
                    self.db.execute("UPDATE incidents SET recovery_notified=1 WHERE id=?", (inc["id"],))
                    self._log(inc["id"], "recovery", "sent")
                    self.db.event(inc["id"], "notified", "Recovery message sent", ts=now)
                except tg.TelegramError as e:
                    self._log(inc["id"], "recovery", "failed", str(e))
                    if now - (inc["recovered_at"] or now) > 3600:
                        self.db.execute("UPDATE incidents SET recovery_notified=2 WHERE id=?", (inc["id"],))

    async def _send_alert(self, inc: dict, escalated: bool, now: float) -> None:
        offers = []
        for t in inc["targets"][:3]:
            oid = self.actions.create_offer(inc["id"], inc["machine"], t["kind"], t["ref"], tg.clean(t["label"], 50))
            offers.append({"id": oid, "label": tg.clean(t["label"], 50)})
        ex = explain.explain(inc, now)
        text = tg.alert_message(inc, self.base_url(), escalated, ex)
        buttons = tg.buttons_for(inc, offers, self.base_url())
        try:
            await asyncio.to_thread(self.tg.send, text, buttons)
        except tg.TelegramError as e:
            n = inc["notify_attempts"] + 1
            state = "failed" if n >= MAX_ATTEMPTS else "failed_retry"
            self.db.execute("UPDATE incidents SET notify_attempts=?, notify_state=?, next_attempt=?, notify_error=? WHERE id=?",
                            (n, state, now + min(30 * 2 ** n, 900), str(e)[:200], inc["id"]))
            self._log(inc["id"], "alert", "failed", str(e))
            return
        self.db.execute("UPDATE incidents SET notify_state='sent', notified_at=?, notified_severity=?, notify_error=NULL, "
                        "next_attempt=NULL WHERE id=?", (now, inc["severity"], inc["id"]))
        self._log(inc["id"], "escalation" if escalated else "alert", "sent")
        self.db.event(inc["id"], "notified", "Telegram message sent" + (" (escalation)" if escalated else ""), ts=now)

    # ------------------------------------------------------------------ inbound bot traffic
    def authorised(self, user_id, chat_id, chat_type="private") -> bool:
        s = self.secrets.all()
        return (chat_type == "private" and str(user_id) == s.get("TELEGRAM_USER_ID", "x")
                and str(chat_id) == s.get("TELEGRAM_CHAT_ID", "y"))

    async def handle_update(self, upd: dict) -> None:
        """Entry point for both the webhook and the polling fallback. Never executes anything except
        the approval flow; text commands are limited to /start, /status and /help."""
        if "callback_query" in upd:
            await self._callback(upd["callback_query"])
        elif "message" in upd:
            msg = upd["message"]
            frm, chat = msg.get("from", {}), msg.get("chat", {})
            if not self.authorised(frm.get("id"), chat.get("id"), chat.get("type")):
                self.db.audit(f"tg:{frm.get('id')}", "telegram.rejected", outcome="unauthorised sender")
                return
            text = (msg.get("text") or "").strip().split("@")[0]
            if text.startswith("/status"):
                await asyncio.to_thread(self.tg.send, self._status_text())
            elif text.startswith("/start") or text.startswith("/help"):
                await asyncio.to_thread(
                    self.tg.send, "<b>ASTRA WATCH</b>\n\nI send alerts here. Tap Stop on an alert to "
                                  "start a two-step confirmation. Commands: /status")

    def _status_text(self) -> str:
        rows = self.db.query("SELECT * FROM incidents WHERE state='active' ORDER BY severity DESC, first_seen")
        if not rows:
            return "<b>ASTRA WATCH</b>\n\n🟢 No active problems.\n" + self.base_url()
        lines = ["<b>ASTRA WATCH</b>", "", f"{len(rows)} active problem(s):"]
        for r in rows[:8]:
            lines.append(f"• {tg.SEV_WORD.get(r['severity'], r['severity'])} · {tg.MACHINE_LABEL.get(r['machine'], r['machine'])}: {tg.E(r['title'])}")
        lines += ["", self.base_url() + "/#/activity"]
        return "\n".join(lines)

    async def _callback(self, cq: dict) -> None:
        cid = cq.get("id", "")
        frm = cq.get("from", {})
        msg = cq.get("message") or {}
        chat = msg.get("chat", {})
        if not self.authorised(frm.get("id"), chat.get("id"), chat.get("type")):
            self.db.audit(f"tg:{frm.get('id')}", "telegram.rejected", outcome="unauthorised callback")
            await asyncio.to_thread(self.tg.answer, cid, "Not authorised", True)
            return
        actor, chat_id, mid = str(frm["id"]), str(chat["id"]), msg.get("message_id")
        data = cq.get("data", "")
        kind, _, arg = data.partition(":")
        if kind == "o" and 0 < len(arg) <= 40:
            await self._step1(cid, arg, actor, chat_id)
        elif kind == "c" and 0 < len(arg) <= 60:
            await self._step2(cid, arg, actor, chat_id, mid)
        elif kind == "x" and 0 < len(arg) <= 60:
            self.actions.cancel(arg, actor)
            await asyncio.to_thread(self.tg.answer, cid, "Cancelled")
            await self._safe_edit(chat_id, mid, "Stop request cancelled. Nothing was changed.")
        else:
            await asyncio.to_thread(self.tg.answer, cid, "Unknown action", True)

    async def _step1(self, cb_id: str, offer_id: str, actor: str, chat_id: str) -> None:
        offer = self.actions.get_offer(offer_id)
        if not offer:
            await asyncio.to_thread(self.tg.answer, cb_id, "This alert is too old", True)
            return
        try:
            appr = await self.actions.prepare(offer["machine"], offer["kind"], offer["target"], "telegram", actor, chat_id)
        except ActionError as e:
            await asyncio.to_thread(self.tg.answer, cb_id, e.args[0][:150], True)
            await asyncio.to_thread(self.tg.send, self.result_text({"status": e.code, "message": str(e), "ts": time.time(),
                                                                    "machine": offer["machine"]}, offer["label"]))
            return
        await asyncio.to_thread(self.tg.answer, cb_id, "Please confirm")
        d = appr["details"]
        who = tg.MACHINE_LABEL.get(appr["machine"], appr["machine"])
        ident = (f"Container: <b>{tg.E(d['name'])}</b>\nContainer ID: <code>{d['short']}</code>" if "short" in d
                 else f"Process: <b>{tg.E(d['name'])}</b>\nPID: <code>{d['pid']}</code>\nExecutable: <code>{tg.E(str(d.get('exe', '')))}</code>")
        why = offer["label"]
        text = "\n".join(["<b>CONFIRM STOP</b>", "", f"Machine: <b>{who}</b>", ident, f"Flagged because: {tg.E(why)}", "",
                          "<b>Expected impact:</b>", *[tg.E("• " + i) for i in d["impact"]], "",
                          "Graceful stop only. Nothing is deleted. Expires in 2 minutes."])
        buttons = [[{"text": "✅ Confirm Stop", "callback_data": f"c:{appr['token']}"},
                    {"text": "Cancel", "callback_data": f"x:{appr['token']}"}]]
        await asyncio.to_thread(self.tg.send, text, buttons, chat_id)

    async def _step2(self, cb_id: str, token: str, actor: str, chat_id: str, message_id) -> None:
        await asyncio.to_thread(self.tg.answer, cb_id, "Working…")
        await self._safe_edit(chat_id, message_id, "Stopping… please wait.")
        res = await self.actions.execute(token, actor, chat_id, "telegram")
        row = self.db.one("SELECT target FROM approvals WHERE id=?", (token,))
        name = ""
        if row:
            t = json.loads(row["target"])
            name = t.get("name") or str(t.get("pid", ""))
        await self._safe_edit(chat_id, message_id, self.result_text(res, name))

    async def _safe_edit(self, chat_id, message_id, text):
        try:
            await asyncio.to_thread(self.tg.edit, chat_id, message_id, text)
        except tg.TelegramError:
            try:
                await asyncio.to_thread(self.tg.send, text, None, chat_id)
            except tg.TelegramError:
                pass

    def result_text(self, res: dict, name: str = "") -> str:
        icon = "✅" if res["status"] == "stopped" else "⚠️"
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(res.get("ts", time.time())))
        mach = tg.MACHINE_LABEL.get(res.get("machine") or "", "")
        return "\n".join([f"{icon} <b>{tg.E(RESULTS.get(res['status'], res['status']))}</b>", "",
                          *( [f"{mach}: {tg.E(name)}"] if name or mach else []), tg.E(res.get("message", "")), "", when])


def _rank(sev: str) -> int:
    return {"warning": 1, "critical": 2}.get(sev, 0)


def webhook_secret_ok(header: str | None, expected: str) -> bool:
    return bool(header and expected and hmac.compare_digest(header, expected))
