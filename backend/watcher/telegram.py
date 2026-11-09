"""Telegram Bot API client (stdlib only) and message formatting.

The bot token is read from secrets.env at call time and is never logged or stored in the DB.
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request

from .config import Secrets

API = "https://api.telegram.org"


class TelegramError(Exception):
    pass


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


class Telegram:
    def __init__(self, secrets: Secrets):
        self.secrets = secrets

    def configured(self) -> bool:
        return self.secrets.telegram_configured()

    def call(self, method: str, payload: dict | None = None, timeout: int = 15) -> dict:
        token = self.secrets.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise TelegramError("Telegram is not configured")
        req = urllib.request.Request(
            f"{API}/bot{token}/{method}", data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https host
                body = json.load(r)
        except urllib.error.HTTPError as e:
            try:
                desc = json.load(e).get("description", "")
            except Exception:
                desc = ""
            raise TelegramError(_scrub(f"HTTP {e.code}: {desc}", token)) from None
        except Exception as e:
            raise TelegramError(_scrub(f"{type(e).__name__}: {e}", token)) from None
        if not body.get("ok"):
            raise TelegramError(_scrub(str(body.get("description", "unknown error")), token))
        return body["result"]

    # -- high level ------------------------------------------------------
    def send(self, text: str, buttons: list[list[dict]] | None = None, chat_id: str | None = None) -> dict:
        payload = {"chat_id": chat_id or self.secrets.get("TELEGRAM_CHAT_ID"), "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return self.call("sendMessage", payload)

    def edit(self, chat_id, message_id, text: str, buttons: list[list[dict]] | None = None) -> dict:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True,
                   "reply_markup": {"inline_keyboard": buttons or []}}
        return self.call("editMessageText", payload)

    def answer(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190],
                                              "show_alert": alert})
        except TelegramError:
            pass

    def get_me(self) -> dict:
        return self.call("getMe")

    def set_webhook(self, url: str, secret: str) -> dict:
        return self.call("setWebhook", {"url": url, "secret_token": secret,
                                        "allowed_updates": ["message", "callback_query"],
                                        "drop_pending_updates": True})

    def delete_webhook(self) -> dict:
        return self.call("deleteWebhook", {"drop_pending_updates": True})

    def webhook_info(self) -> dict:
        return self.call("getWebhookInfo")

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 10)


# ---------------------------------------------------------------- formatting

def E(s) -> str:
    """Escape for Telegram HTML. Quotes are left alone (they only matter inside attributes, which we never build)."""
    return html.escape(str(s), quote=False)


MACHINE_LABEL = {"mac": "MY MAC", "hetzner": "HETZNER SERVER"}
SEV_ICON = {"critical": "🔴", "warning": "🟠", "recovered": "🟢"}


def fmt_gb(b: float) -> str:
    return f"{b / 1e9:.1f} GB"


SEV_WORD = {"critical": "CRITICAL", "warning": "NEEDS ATTENTION"}
BRAND = "<b>ASTRA WATCH</b>"


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _duration(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} min" if m < 90 else f"{m / 60:.1f} h"


def alert_message(inc: dict, base_url: str, escalated: bool = False, ex: dict | None = None) -> str:
    """Short and actionable: what happened, why it matters, likely contributor (labelled), first steps, link."""
    from . import explain as _ex
    ex = ex or _ex.explain(inc, inc.get("last_seen") or 0)
    word = SEV_WORD.get(inc["severity"], inc["severity"].upper())
    machine = MACHINE_LABEL.get(inc["machine"], inc["machine"].upper())
    lines = [BRAND, "", f"{SEV_ICON.get(inc['severity'], '')} <b>{word} · {machine}</b>" + (" (got worse)" if escalated else ""),
             f"<b>{E(ex['headline'])}</b>", "",
             f"<b>What happened</b>\n{E(_clip(ex['what'], 260))}", "",
             f"<b>Why it matters</b>\n{E(_clip(ex['why'], 200))}"]
    c = ex["cause"]
    if c.get("text"):
        tag = "confirmed" if c["kind"] == "confirmed" else "inferred" + (f", {c['confidence']} confidence" if c.get("confidence") else "")
        lines += ["", f"<b>Likely cause</b> ({tag})", E(_clip(c["text"], 200))]
        if c.get("note"):
            lines.append(f"<i>{E(_clip(c['note'], 120))}</i>")
    steps = ex["steps"][:3]
    if steps:
        lines += ["", "<b>First steps</b>"] + [f"{i}. {E(_clip(t, 150))}" for i, t in enumerate(steps, 1)]
    lines += ["", f"Full details and every step: {base_url}/#/activity/{inc['id']}"]
    return "\n".join(lines)


def recovery_message(inc: dict, base_url: str, duration_s: float, improved: list[str] | None = None,
                     actions: list[str] | None = None) -> str:
    """Says the alert cleared. It does NOT claim the root cause is fixed unless an approved action was taken."""
    from . import explain as _ex
    machine = MACHINE_LABEL.get(inc["machine"], inc["machine"].upper())
    ex = _ex.explain(inc, inc.get("recovered_at") or 0)
    _, root = _ex.root_cause_status(inc, actions)
    lines = [BRAND, "", f"🟢 <b>ALERT CLEARED · {machine}</b>", f"<b>{E(ex['headline'])}</b>", "This has cleared.", "",
             f"<b>How long</b>\nIt lasted about {_duration(duration_s)}."]
    if improved:
        lines += ["", "<b>What improved</b>"] + [E(x) for x in improved]
    lines += ["", "<b>Automatic action</b>\n" + E("; ".join(actions) if actions else "None. Astra Watch did not change anything."),
              "", "<b>Root cause</b>", E(_clip(root, 230)), "", f"Details: {base_url}/#/activity/{inc['id']}"]
    return "\n".join(lines)


def buttons_for(inc: dict, offers: list[dict], base_url: str) -> list[list[dict]]:
    rows = [[{"text": "Open details", "url": f"{base_url}/#/activity/{inc['id']}"}]]
    for o in offers[:3]:
        rows.append([{"text": f"Stop: {o['label']}"[:60], "callback_data": f"o:{o['id']}"}])
    return rows


SAFE = re.compile(r"[^\w .:/@+\-]")


def clean(text: str, n: int = 80) -> str:
    return SAFE.sub("", str(text))[:n]
