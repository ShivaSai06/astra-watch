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

E = html.escape
MACHINE_LABEL = {"mac": "MY MAC", "hetzner": "HETZNER SERVER"}
SEV_ICON = {"critical": "🔴", "warning": "🟠", "recovered": "🟢"}


def fmt_gb(b: float) -> str:
    return f"{b / 1e9:.1f} GB"


def alert_message(inc: dict, base_url: str, escalated: bool = False) -> str:
    """Concise: title, key numbers, main contributor, impact, action, link."""
    m = inc.get("measured") or {}
    lines = ["<b>PERSONAL INFRASTRUCTURE WATCHER</b>", ""]
    sev = inc["severity"].upper()
    lines.append(f"{SEV_ICON.get(inc['severity'], '')} <b>{sev} — {MACHINE_LABEL.get(inc['machine'], inc['machine'].upper())}</b>"
                 + (" (escalated)" if escalated else ""))
    lines += ["", E(inc["title"]) + ".", ""]
    for label, val in (m.get("summary") or {}).items():
        lines.append(f"{E(str(label))}: {E(str(val))}")
    if inc.get("cause"):
        conf = inc.get("confidence")
        lines.append(f"Likely cause: {E(inc['cause'])}" + (f" ({E(conf)} confidence)" if conf else ""))
    if inc.get("impact"):
        lines += ["", "<b>Impact:</b>", E(inc["impact"])]
    if inc.get("recommendation"):
        lines += ["", "<b>Recommended action:</b>", E(inc["recommendation"])]
    lines += ["", "<b>Open dashboard:</b>", f"{base_url}/#/alerts/{inc['id']}"]
    return "\n".join(lines)


def recovery_message(inc: dict, base_url: str, duration_s: float) -> str:
    mins = max(1, round(duration_s / 60))
    return "\n".join([
        "<b>PERSONAL INFRASTRUCTURE WATCHER</b>", "",
        f"🟢 <b>RECOVERED — {MACHINE_LABEL.get(inc['machine'], inc['machine'].upper())}</b>", "",
        E(inc["title"]) + " has cleared.", f"Lasted about {mins} min.", "",
        f"{base_url}/#/alerts/{inc['id']}"])


def buttons_for(inc: dict, offers: list[dict], base_url: str) -> list[list[dict]]:
    rows = [[{"text": "Review Dashboard", "url": f"{base_url}/#/alerts/{inc['id']}"}]]
    for o in offers[:3]:
        rows.append([{"text": f"Stop: {o['label']}"[:60], "callback_data": f"o:{o['id']}"}])
    return rows


SAFE = re.compile(r"[^\w .:/@+\-]")


def clean(text: str, n: int = 80) -> str:
    return SAFE.sub("", str(text))[:n]
