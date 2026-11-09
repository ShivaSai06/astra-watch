"""watcherctl — owner-only admin CLI. Secrets are entered with hidden prompts on the server itself.

    python -m watcher.ctl status
    python -m watcher.ctl setup-telegram        # token (hidden) -> send /start to your bot -> confirm ids
    python -m watcher.ctl set-webhook           # register https://<public_url>/telegram/webhook
    python -m watcher.ctl use-polling           # no public endpoint; bot is polled instead
    python -m watcher.ctl test-telegram
    python -m watcher.ctl pair-agent [name]     # prints a one-time Mac agent token
    python -m watcher.ctl revoke-agent <id>
    python -m watcher.ctl rotate-agent <id>     # revoke + pair a replacement
    python -m watcher.ctl tailscale-owner <email>   # allow only this tailnet login via `tailscale serve`
    python -m watcher.ctl backup
"""
from __future__ import annotations

import getpass
import secrets as pysecrets
import sys
import time

from .config import Paths, Secrets, Settings, load_env_file, save_env_file
from .security import AgentRegistry
from .telegram import Telegram, TelegramError


def _set(paths: Paths, **kv):
    env = load_env_file(paths.secrets)
    env.update({k: v for k, v in kv.items() if v is not None})
    paths.ensure()
    save_env_file(paths.secrets, env)


def cmd_status(paths: Paths, _args):
    sec = Secrets(paths)
    s = sec.all()
    tg = Telegram(sec)
    print("Telegram configured :", tg.configured(), "| mode:", s.get("TELEGRAM_MODE", "webhook"))
    print("Webhook secret set  :", bool(s.get("TELEGRAM_WEBHOOK_SECRET")))
    print("Tailscale owner set :", bool(s.get("TAILSCALE_OWNER_LOGIN")))
    for a in AgentRegistry(paths).list():
        print(f"Agent {a['id']} ({a['name']}) revoked={a['revoked']}")
    if tg.configured():
        try:
            info = tg.webhook_info()
            print("Webhook url         :", info.get("url") or "(none)", "| pending:", info.get("pending_update_count"),
                  "| last error:", info.get("last_error_message", "-"))
        except TelegramError as e:
            print("Telegram API error  :", e)


def cmd_setup_telegram(paths: Paths, _args):
    token = getpass.getpass("Telegram bot token from @BotFather (hidden): ").strip()
    sec = Secrets(paths)
    _set(paths, TELEGRAM_BOT_TOKEN=token)
    tg = Telegram(sec)
    try:
        me = tg.get_me()
    except TelegramError as e:
        _set(paths, TELEGRAM_BOT_TOKEN="")
        sys.exit(f"Token rejected by Telegram: {e}")
    print(f"Bot found: @{me.get('username')}")
    print("Now open Telegram and send /start to your bot. Waiting up to 120 s ...")
    try:
        tg.delete_webhook()
    except TelegramError:
        pass
    end, offset, found = time.time() + 120, 0, None
    while time.time() < end and not found:
        try:
            for u in tg.get_updates(offset, 10):
                offset = u["update_id"] + 1
                m = u.get("message")
                if m and m["chat"]["type"] == "private":
                    found = (m["from"]["id"], m["chat"]["id"], m["from"].get("first_name", ""))
        except TelegramError as e:
            sys.exit(f"Telegram error: {e}")
    if not found:
        sys.exit("No /start received; nothing was saved as authorised. Run the command again.")
    uid, cid, first = found
    ok = input(f"Authorise Telegram user id {uid} ({first}) in private chat {cid}? [y/N] ").strip().lower()
    if ok != "y":
        sys.exit("Aborted. No authorised user stored.")
    _set(paths, TELEGRAM_USER_ID=str(uid), TELEGRAM_CHAT_ID=str(cid),
         TELEGRAM_WEBHOOK_SECRET=load_env_file(paths.secrets).get("TELEGRAM_WEBHOOK_SECRET") or pysecrets.token_urlsafe(32),
         TELEGRAM_MODE=load_env_file(paths.secrets).get("TELEGRAM_MODE", "polling"))
    tg.send("<b>PERSONAL INFRASTRUCTURE WATCHER</b>\n\n✅ Telegram connected. You are the only authorised user.")
    print("Saved. Mode is 'polling' until you run set-webhook (needs the public HTTPS URL working).")


def cmd_set_webhook(paths: Paths, _args):
    sec = Secrets(paths)
    s = sec.all()
    url = Settings(paths).get()["public_url"] + "/telegram/webhook"
    tg = Telegram(sec)
    tg.set_webhook(url, s["TELEGRAM_WEBHOOK_SECRET"])
    _set(paths, TELEGRAM_MODE="webhook")
    print("Webhook registered:", url)


def cmd_use_polling(paths: Paths, _args):
    Telegram(Secrets(paths)).delete_webhook()
    _set(paths, TELEGRAM_MODE="polling")
    print("Webhook removed; the watcher now polls Telegram (works without a public endpoint).")


def cmd_tailscale_owner(paths: Paths, args):
    if not args or "@" not in args[0]:
        sys.exit("usage: tailscale-owner <tailnet login email>")
    _set(paths, TAILSCALE_OWNER_LOGIN=args[0].strip().lower())
    print("Dashboard access via Tailscale Serve is now limited to", args[0].strip().lower())


def cmd_test_telegram(paths: Paths, _args):
    Telegram(Secrets(paths)).send("<b>PERSONAL INFRASTRUCTURE WATCHER</b>\n\n✅ Test notification from watcherctl.")
    print("Sent.")


def cmd_pair_agent(paths: Paths, args):
    name = args[0] if args else "mac"
    agent_id, token = AgentRegistry(paths).pair(name)
    print(f"Agent id : {agent_id}\nToken    : {token}\n\nThis token is shown ONCE. Put it on the Mac with:  ./agent/install.sh  (it prompts for it, hidden).")


def cmd_revoke_agent(paths: Paths, args):
    print("revoked" if args and AgentRegistry(paths).revoke(args[0]) else "unknown agent id")


def cmd_rotate_agent(paths: Paths, args):
    if not args or not AgentRegistry(paths).revoke(args[0]):
        sys.exit("unknown agent id")
    cmd_pair_agent(paths, [])


def cmd_backup(paths: Paths, _args):
    from .config import Paths as P
    from .db import DB
    import shutil, os
    d = paths.base / "backups"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    DB(paths.db).checkpoint_backup(d / f"watcher-{stamp}.sqlite3")
    shutil.copytree(paths.config, d / f"config-{stamp}")
    for p in d.glob("watcher-*.sqlite3"):
        if time.time() - p.stat().st_mtime > 24 * 3600:
            p.unlink()
    print("Backup written to", d)


COMMANDS = {"status": cmd_status, "setup-telegram": cmd_setup_telegram, "set-webhook": cmd_set_webhook,
            "use-polling": cmd_use_polling, "test-telegram": cmd_test_telegram, "pair-agent": cmd_pair_agent,
            "revoke-agent": cmd_revoke_agent, "rotate-agent": cmd_rotate_agent, "tailscale-owner": cmd_tailscale_owner,
            "backup": cmd_backup}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](Paths(), sys.argv[2:])


if __name__ == "__main__":
    main()
