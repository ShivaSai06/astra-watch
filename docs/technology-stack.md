# Technology stack

Only technologies actually used in this repository are listed. Versions are read from `package.json`, `requirements*.txt` and the deployed server.

| Technology | Version | What it is / where it is used | Why | Responsibility |
|---|---|---|---|---|
| **Python** | 3.12 on the server, 3.12+ for development | Backend and Mac agent | Ubuntu ships it, great for reading `/proc`, small code | All logic |
| **FastAPI** | 0.162 (server) | Web framework in `backend/watcher/main.py` | Typed request validation, simple routing | Dashboard API, agent API, Telegram hooks |
| **Pydantic** | 2.x (comes with FastAPI) | Request models | Rejects malformed or oversized agent payloads | Input validation |
| **Uvicorn** | 0.54 | ASGI server in the systemd unit | Light, single process | Serves HTTP on `127.0.0.1:8200` |
| **SQLite** | built into Python | `data/watcher.sqlite3` | Zero admin, one file, enough for 24 h of two machines | Samples, incidents, approvals, audit |
| **Docker CLI** | 27.x on the server | Called by `docker_cli.py` | Existing workloads already run in Docker | Read-only inspection, `docker stop` on approval |
| **Tailscale** | 1.104 (userspace `tailscaled`) | `infra-tailscaled` service, `tailscale serve` | Private network + HTTPS certificate without opening ports | Reachability, HTTPS, owner identity header |
| **systemd user services** | Ubuntu 24.04 | `infra-watcher`, `infra-tailscaled` | Restart on failure, resource caps, no root | Process supervision |
| **launchd** | macOS | `com.astra-watch.agent` plist | Native way to run a per-user agent at login | Keeps the Mac agent alive |
| **macOS Keychain** | macOS | `security` CLI in `agent/` | OS secret store, token not in a plain file | Stores the agent token |
| **Telegram Bot API** | HTTPS API | `telegram.py` / `notify.py`, standard library `urllib` | Push alerts to a phone with buttons, no app to build | Notifications and Stop/Confirm taps |
| **React** | 18.3 | `frontend/src` | Familiar, small app | Dashboard UI |
| **TypeScript** | 5.6 | `frontend/src` | Catches API-shape mistakes at build | Type safety |
| **Vite** | 5.4 | Frontend build | Fast build to static files | Produces `dist/` that the backend serves |
| **pytest** | 9.x | `backend/tests` | Standard Python tests | Engine, security, agent, status tests |
| **Mermaid** | rendered by GitHub | Diagrams in docs | Diagrams in plain text | Documentation only |
| **GitHub Actions / Pages** | n/a | `.github/workflows/pages.yml` | Free static hosting for the landing page | Publishes `site/` only |
| **Plain HTML + CSS** | n/a | `site/` landing page | No framework needed for a static page | Public project page |

## Not used

Prometheus, Grafana, Kubernetes, Redis, Cloudflare, a message queue and an ORM are **not** part of Astra Watch. Cloudflare Tunnel and Access were used in an earlier iteration and have been removed in favour of Tailscale.

## Dependencies on purpose kept small

Backend runtime dependencies are just `fastapi` and `uvicorn`. The Mac agent uses only the Python standard library. The frontend has two runtime dependencies (`react`, `react-dom`).
