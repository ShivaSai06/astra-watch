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

## 2. Cloudflare Tunnel + Access (`monitor.example.com`)
The server has no passwordless sudo, so nginx was not touched. The tunnel runs as a user service and only makes outbound connections.
1. Cloudflare Zero Trust → Networks → Tunnels → **Create tunnel** (Cloudflared) → copy the token.
2. On the server: `install -m 600 /dev/stdin ~/pi-watcher/config/cloudflared.token` (paste token, Ctrl-D), then
   `~/pi-watcher/bin/cloudflared` is already installed? If not: see "cloudflared binary" below. Start it:
   `systemctl --user enable --now infra-cloudflared`
3. Tunnel → Public hostname: `monitor.example.com` → HTTP → `127.0.0.1:8200`.
   (If a DNS record for `monitor` already exists, Cloudflare asks you to replace it. This hostname is new, so nothing else is affected.)
4. Zero Trust → Access → Applications → **Add self-hosted** `monitor.example.com`, policy *Allow* → your email only.
   Add two **Bypass** policies on *separate path-scoped applications* (they authenticate themselves):
   `monitor.example.com/agent/*`, `monitor.example.com/telegram/*`, and `monitor.example.com/healthz` (for the uptime monitor).
5. Copy the Access application's **AUD tag**, then on the server: `~/pi-watcher/bin/watcherctl cf-access`
   (team name, AUD, your email). From then on the API verifies Cloudflare's signed token itself.
   Until then remote access is refused (fail-closed); only the SSH tunnel works.
6. Tell the app its public URL (used for alert links and the webhook), then switch Telegram to the webhook:
   `echo '{"public_url": "https://monitor.example.com"}' > ~/pi-watcher/config/settings.json`, restart the service, and run
   `~/pi-watcher/bin/watcherctl set-webhook` — this switches Telegram from polling to a webhook with a secret header.

## 3. Mac agent → public URL
Currently the agent reaches the server through an SSH port-forward (`ssh -N -L 8200:127.0.0.1:8200 …`). After step 2:
`./agent/install.sh` (URL `https://monitor.example.com`, token prompt is hidden; it already holds a paired token, say "n" to keep it),
then `watcher-agent stop && watcher-agent start`. Optional auto-start at login: `watcher-agent enable`.
Agent commands: `start | stop | status | logs | enable | disable | once | uninstall`.
Revoke/rotate: `watcherctl revoke-agent <id>` / `rotate-agent <id>`.

## 4. Independent "server is down" alert
If the whole server is off, nothing on it can message you. Use a free external monitor:
**UptimeRobot** (free) → New monitor → HTTPS → `https://monitor.example.com/healthz` → 1-minute/5-minute interval →
Alert contacts → add **Telegram** (it links to your account via its own bot) → done.
Test by running `systemctl --user stop infra-watcher` for ~6 minutes and restarting it; you should get Down and Up messages.
As a local extra, the Mac agent shows a macOS notification if the dashboard is unreachable for 10 minutes.

## Operations
- Deploy / update: `./deploy/deploy.sh` (tests → build → upload → activate; **auto-rollback** if the health check fails)
- Roll back: `./deploy/rollback.sh`
- Stop only the dashboard: `systemctl --user stop infra-watcher` (touches nothing else)
- Logs: `journalctl --user -u infra-watcher` (warnings only); app diagnostics live 72 h in the DB.
- Backups: daily, automatic, in `~/pi-watcher/backups` (DB copies older than 72 h are deleted; config copies keep 7).
- Retention: 72 h hard cap, enforced at startup and hourly; Settings can only lower it.

## Privilege notes
The watcher runs as your (non-root) deploy user, which is in the `docker` group. That group is root-equivalent on the host, so safety comes from
code: the only mutating Docker call is `docker stop -t N <64-hex id>` behind a two-step, single-use, HMAC-bound approval; everything
else is read-only inspection. The log-size helper starts a short-lived read-only `nginx:alpine` container every 15 min.
Resource caps (systemd): 300 MB RAM, 60 % of one CPU, nice 10.

## cloudflared binary
`curl -L -o ~/pi-watcher/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod 755 ~/pi-watcher/bin/cloudflared`
