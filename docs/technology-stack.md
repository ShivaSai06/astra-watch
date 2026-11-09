# Technology stack

Only technologies that appear in this repository are listed. Frontend versions are pinned in `frontend/package.json`; backend dependencies are declared in `backend/requirements*.txt`.

## Backend

| Technology | What Astra Watch uses it for | Why it is useful |
|---|---|---|
| **Python 3.12** | The backend and the Mac agent | Reads `/proc` and runs system commands with very little code. Ubuntu ships it, so the server needs no extra runtime. |
| **FastAPI** | The dashboard API, the agent API and the Telegram hook in `main.py` | Typed request validation rejects malformed or oversized agent payloads before they reach the logic. |
| **Uvicorn** | Runs the FastAPI app as a single process on `127.0.0.1:8200` | Light enough for a server that also runs other workloads. |
| **SQLite (WAL mode)** | Samples, snapshots, incidents, approvals and the audit log | One file, no database server to run or secure, and enough for 24 hours of two machines. |
| **Python standard library** | `urllib` for Telegram calls, `hashlib`/`hmac` for tokens and approvals, `asyncio` for background loops | Keeps runtime dependencies to two packages (`fastapi`, `uvicorn`), so there is little to patch. |

## Measuring machines and containers

| Technology | What Astra Watch uses it for | Why it is useful |
|---|---|---|
| **Linux `/proc`, `/sys` and cgroups** | CPU, memory, load, disk I/O and per-container CPU and memory on the server (`collect_linux.py`) | Reading files is cheap and needs no extra agent on the server. |
| **Docker CLI** | Container list, health, restarts, disk use and log sizes (`docker_cli.py`); `docker stop` after confirmation | The workloads already run in Docker. Almost everything is read-only inspection, which keeps the blast radius small. |
| **macOS system tools** | The Mac agent reads memory pressure, `ps`, disk and thermal state | These are the numbers a developer actually cares about on a Mac, available without installing anything. |

## Agent and device integration

| Technology | What Astra Watch uses it for | Why it is useful |
|---|---|---|
| **HTTPS with bearer tokens** | The Mac agent pushes metrics to `/agent/v1/metrics` and long-polls `/agent/v1/commands` | Outbound-only from the laptop, so the Mac needs no open port. |
| **launchd** | Starts the agent at login and restarts it if it crashes | The native macOS way to keep a per-user background job alive. |
| **macOS Keychain** | Stores the agent token | The token is not left in a plain file on the laptop. |
| **systemd user services** | Run the backend (`infra-watcher`) and Tailscale daemon without root, with memory and CPU caps | Automatic restart and resource limits protect the other workloads on the server. |

## Networking, authentication and security

| Technology | What Astra Watch uses it for | Why it is useful |
|---|---|---|
| **Tailscale** | A private WireGuard network between the owner's devices | A monitoring dashboard reveals what runs on your machines and should not be on the public internet. With Tailscale there is no open port to attack and no public login page. |
| **Tailscale Serve** | HTTPS on the `*.ts.net` name, proxying to the loopback backend, and adding the signed-in identity header | The backend never handles TLS or a public interface, and it can check *who* is calling. |
| **Tailscale identity check** | `security.py` accepts only the one configured owner login | Being on the tailnet is not enough; a device shared with someone else is still refused. |
| **CSRF tokens, security headers, CSP** | Protect every state-changing request | Stops another web page from triggering a stop or changing settings through the owner's browser. |
| **SHA-256 token hashes and HMAC approvals** | Agent tokens are stored hashed; stop confirmations are bound to the exact target | A leaked database cannot reveal agent tokens, and an approval cannot be reused on another container. |

## Notifications

| Technology | What Astra Watch uses it for | Why it is useful |
|---|---|---|
| **Telegram Bot API** | Incident and recovery messages, plus Stop and Confirm buttons (`notify.py`, `telegram.py`) | Push alerts reach a phone with no app to build. The backend polls for taps, so no public webhook URL is required. |
| **Optional external heartbeat** | An outbound ping while collection is healthy (`heartbeat.py`) | A monitor cannot report its own death; an outside service can notice when the pings stop. |

## Frontend

| Technology | What Astra Watch uses it for | Why it is useful |
|---|---|---|
| **React 18** | The dashboard pages (`frontend/src`) | Components map well to machine cards, incident lists and settings forms. |
| **TypeScript** | Types for every API response (`api.ts`) | A change in the backend response shape fails at build time instead of showing a blank card. |
| **Vite** | Builds the dashboard to static files in `dist/` | The backend serves those files, so production needs no Node process. |
| **Hand-written SVG charts and CSS** | The time-series graphs and layout (`charts.tsx`, `styles.css`) | No charting library to ship for four simple line charts. |
| **Hash-based routing** | Routes like `#/machines/mac` | Deep links and refreshes work with the backend serving a single page. |

## Testing, build and delivery

| Technology | What Astra Watch uses it for | Why it is useful |
|---|---|---|
| **pytest** | Backend tests for rules, security, status handling and the agent | Covers the behaviours that matter most, such as "opening the dashboard never creates incidents". |
| **Vitest, Testing Library, jsdom** | Frontend component and URL tests | Fast checks of the UI without a browser. |
| **Shell deploy scripts** | `deploy/deploy.sh` tests, builds, uploads a timestamped release, activates it and rolls back if the health check fails | Repeatable releases that can be undone. |
| **Headless Chrome and a demo server** | Generates the documentation screenshots from synthetic data | Screenshots are real renders, and no production data is involved. |
| **Mermaid** | Diagrams in these docs | Diagrams live in text and render on GitHub. |
| **GitHub Actions and Pages** | Publish the static landing page in `site/` only | Free hosting of a page that contains no application code. |

## Not used

Prometheus, Grafana, Kubernetes, Redis, a message queue, an ORM and a hosted monitoring service are **not** part of Astra Watch. An earlier iteration used Cloudflare for access; it was replaced by Tailscale.
