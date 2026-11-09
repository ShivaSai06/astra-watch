# Security

This page separates **network access** (who can reach the server at all) from **application authentication** (who the app lets in once connected). Both are needed.

## Layer 1: network access (Tailscale)

- **Private tailnet.** Astra Watch is reachable only from devices signed in to the owner's Tailscale network. The backend itself listens on `127.0.0.1`, so there is no public port.
- **Tailscale Serve.** `tailscale serve` receives HTTPS on the machine's `*.ts.net` name and forwards to the local backend.
- **HTTPS certificates.** Tailscale issues and renews the certificate for that name. Traffic is also encrypted by WireGuard between devices.
- **Funnel is disabled.** Funnel would expose the service to the public internet. It is off, and the backend additionally rejects any request carrying the `Tailscale-Funnel-Request` header.
- The live dashboard is **private and is not published anywhere**. The public GitHub Pages site is a separate static page.

## Layer 2: application authentication

### Dashboard: Tailscale identity + CSRF

- `tailscale serve` authenticates the peer and adds `Tailscale-User-Login`. The backend accepts a request only if: it came from loopback (i.e. from the local `tailscale serve`), the login equals the single configured owner login, and no Funnel or other proxy headers are present. Otherwise `403`.
- This **tailnet identity restriction** means other people or devices in the same tailnet (for example one shared with a friend) are still refused.
- If no owner login is configured the dashboard fails closed: only a direct loopback connection (SSH port-forward) works.
- **CSRF protection:** every state-changing request (`PUT /api/settings`, stop actions, test notification) needs a `csrf` cookie plus matching `X-CSRF-Token` header and a same-origin `Origin`. The cookie is `HttpOnly`, `SameSite=Strict`.
- Strict security headers and a restrictive Content-Security-Policy are set on every response; interactive API docs are disabled.

### Mac agent: bearer token

Each agent has its own token. The server stores only a SHA-256 **hash** (`agents.json`, mode 0600); the Mac stores the token in the **Keychain**. Tokens can be revoked or rotated with `watcherctl`. Failed attempts are rate-limited per IP. This is independent of the dashboard login.

### Telegram: separate authentication

Telegram messages are accepted only from the allow-listed numeric user id **and** private chat id. In webhook mode an additional secret header is required. Production uses polling. Anyone else who messages the bot is ignored and logged in the audit table.

## Controlled actions

- **Allowlisted stop actions.** The only mutating operations are `docker stop` on a validated 64-hex container id, and, on the Mac, SIGTERM to a process whose identity (pid and start time) is re-verified. There is no kill, delete, exec or shell.
- **Confirmation tokens.** A stop is *prepared* first, producing a random single-use token valid for 120 seconds and bound by HMAC to the exact target, machine and requester. Confirming spends it. A token cannot be reused or applied to a different container.
- Every prepare, confirm, cancel and rejection is written to the audit log.
- The server user is in the Docker group, which is powerful. Safety relies on the allowlisted code paths above.

## Secret management

| Secret | Where it lives | Never in |
|---|---|---|
| Telegram bot token, chat/user id | `secrets.env` (0600) on the server | repo, database, logs, API responses |
| Tailscale owner login, approval HMAC key | `secrets.env` | repo, API responses |
| Agent token (plain) | macOS Keychain; shown once at pairing | server (only its hash is kept) |
| Tailscale node state | `~/pi-watcher/tailscale/` on the server | repo |
| Deploy target and SSH key path | `deploy/local.env` (git-ignored) | repo |

Secrets are entered with hidden prompts via `watcherctl` on the server. The Telegram token is scrubbed from error messages. `test_no_secrets_in_api_responses` checks that no secret appears in API output.

## Public repository precautions

- `.gitignore` excludes `.env`, `secrets.env`, `deploy/local.env`, `*.token`, `*.db`, `data/`, `backups/` and logs.
- Documentation uses placeholder names (`astra.example.ts.net`, `user@your-server`).
- Screenshots come from `scripts/demo_server.py`, which generates synthetic data in a throw-away directory.
- The GitHub Pages workflow publishes only the `site/` folder.
- Before publishing, the working tree and history are scanned for tokens, IP addresses, emails and private paths.

## Known limitations

- A single owner identity; no multi-user roles.
- Trust in Tailscale's control plane for identity and certificates.
- The Docker-group caveat above.
