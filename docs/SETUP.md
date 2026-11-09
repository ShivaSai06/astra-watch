# Astra Watch — setup guide

The release is deployed by `deploy/deploy.sh` to `~/pi-watcher` on the server (service `infra-watcher`, listening on
`127.0.0.1:8200` only). The remaining one-time steps need **your** accounts; nothing below requires pasting secrets into chat.

Server shell: `ssh user@your-server`
Helper on the server: `~/pi-watcher/bin/watcherctl <command>`

## 1. Telegram (alerts + Stop buttons)
1. In Telegram, message **@BotFather** → `/newbot` → keep the token.
2. On the server: `~/pi-watcher/bin/watcherctl setup-telegram`
   - paste the token at the hidden prompt, then send `/start` to your new bot; confirm the user/chat id it shows.
3. Test: `~/pi-watcher/bin/watcherctl test-telegram` (or Settings → Send test notification).
   Until step 4 the bot is polled (works without any public URL, including Stop approvals).

## 2. Private remote access with Tailscale
Astra Watch is never exposed to the public internet. The server runs Tailscale in userspace mode as the user service `infra-tailscaled` (no root needed) and `tailscale serve` proxies HTTPS from your `*.ts.net` name to `127.0.0.1:8200`.
1. Create a free Tailscale account and install Tailscale on the devices you will browse from (laptop, phone).
2. On the server, run `tailscaled` as a user service with its own state directory and socket under `~/pi-watcher/tailscale/`, then `tailscale up` (authenticate in the browser; do not paste auth keys into files in the repo).
3. In the Tailscale admin console enable **MagicDNS** and **HTTPS certificates**.
4. Publish the backend privately: `tailscale serve --bg http://127.0.0.1:8200` (use `--socket` to point at the userspace daemon). **Do not run `tailscale funnel`**; Funnel would make it public.
5. Restrict the dashboard to your identity: `~/pi-watcher/bin/watcherctl tailscale-owner <your tailnet login>`.
6. Tell the app its URL (used for links in alerts): set `public_url` in `settings.json` to your `https://<machine>.<tailnet>.ts.net` address.
7. Check: Settings shows *Remote access: Tailscale, Connection: Connected, HTTPS: Enabled, Access: Private tailnet, Funnel: Disabled*.

Telegram runs in polling mode (`watcherctl use-polling`), so no public webhook is needed.

## 3. Mac agent
`./agent/install.sh` asks for your dashboard URL (the `https://...ts.net` address) and stores the agent token in the macOS Keychain. The Mac must be signed in to the same tailnet.
Agent commands: `watcher-agent start | stop | status | logs | enable | disable | once | uninstall`. `enable` installs the launchd job (starts at login, restarts if it crashes).
Pair, revoke or rotate tokens on the server with `watcherctl pair-agent | revoke-agent <id> | rotate-agent <id>`.

## 4. Independent "server is down" alert
If the whole server is off, nothing on it can message you, and the dashboard is private so public uptime checkers cannot reach it. As a local extra, the Mac agent shows a macOS notification if the dashboard is unreachable for 10 minutes. This remains a limitation: there is no off-server alerting.

## Operations
- Deploy / update: `./deploy/deploy.sh` (tests → build → upload → activate; **auto-rollback** if the health check fails)
- Roll back: `./deploy/rollback.sh`
- Stop only the dashboard: `systemctl --user stop infra-watcher` (touches nothing else)
- Logs: `journalctl --user -u infra-watcher` (warnings only); app diagnostics live 24 h in the DB.
- Backups: daily, automatic, in `~/pi-watcher/backups` (DB copies older than 24 h are deleted; config copies keep 7).
- Retention: 24 h hard cap, enforced at startup and hourly; Settings can only lower it.

## Privilege notes
The watcher runs as your (non-root) deploy user, which is in the `docker` group. That group is root-equivalent on the host, so safety comes from
code: the only mutating Docker call is `docker stop -t N <64-hex id>` behind a two-step, single-use, HMAC-bound approval; everything
else is read-only inspection. The log-size helper starts a short-lived read-only `nginx:alpine` container every 15 min.
Resource caps (systemd): 300 MB RAM, 60 % of one CPU, nice 10.
