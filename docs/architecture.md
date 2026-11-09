# Architecture

This describes what Astra Watch actually does today, not an idealised design.

## The big picture

```mermaid
flowchart LR
    subgraph MAC["MacBook"]
        AG["Mac agent<br/>Python, launchd<br/>token in Keychain"]
    end

    subgraph TAIL["Tailscale private network (tailnet)"]
        direction TB
        TS["tailscale serve<br/>HTTPS, adds identity header"]
    end

    subgraph HZ["Hetzner server"]
        direction TB
        subgraph BE["Backend (FastAPI, 127.0.0.1:8200)"]
            API["Agent API and<br/>dashboard API"]
            COL["Collector<br/>/proc, /sys, Docker CLI"]
            ENG["Rule engine<br/>and notifier"]
        end
        DB[("SQLite<br/>measurements, incidents, audit")]
        DK["Docker workloads<br/>(containers)"]
    end

    BR["Browser<br/>(laptop or phone)"]
    TG["Telegram"]

    AG -- "1. POST metrics every 30 s<br/>(bearer token)" --> TS
    BR -- "HTTPS: loads page, polls API" --> TS
    TS -- "proxy to loopback" --> API
    COL -- "reads, never changes" --> DK
    API -- "write samples" --> DB
    COL -- "write samples" --> DB
    DB -- "read" --> API
    ENG -- "evaluate after each collection" --> DB
    ENG -- "2. alert / recovery message" --> TG
    TG -- "3. Stop and Confirm taps<br/>(polled by backend)" --> ENG
    ENG -. "approved stop" .-> DK
```

**Reading the diagram.** The solid arrows into `tailscale serve` are the only paths in from outside the server, and both stay inside the tailnet. The backend listens on loopback, so it cannot be reached any other way. The MacBook *pushes* its measurements to the backend. The backend *pulls* the Hetzner server's own measurements, since it runs on that machine. Telegram is reached by outbound requests only: the backend sends alerts and polls for button taps, so no inbound port is opened. The dotted arrow is the one write action, a confirmed `docker stop`.

There are three programs: the **Mac agent**, the **backend** (which also collects the server's own metrics and serves the dashboard) and the **React dashboard** (static files served by the backend). SQLite, Tailscale and Telegram are the supporting pieces.

Inside the backend (`backend/watcher/`): `main.py` (routes and loops), `collect_linux.py` and `docker_cli.py` (collection), `engine.py` (rules), `explain.py` (plain-English incident text), `views.py` (read-only dashboard models), `notify.py` and `telegram.py` (messages), `automation.py` (policy and dry run), `heartbeat.py` (optional outbound watchdog ping), `security.py` (auth, CSRF), `actions.py` (stop approvals), `db.py` (SQLite and retention) and `ctl.py` (the `watcherctl` admin CLI).

## 1. MacBook monitoring agent

`agent/mac_agent.py` is a single Python file using only the standard library. It runs under **launchd** (`RunAtLoad` and `KeepAlive`), so it starts at login and is restarted if it crashes. Every 30 seconds it reads CPU, memory, swap, disk, load and top processes (plus Docker Desktop containers if Docker runs), and sends them to the backend with `POST /agent/v1/metrics`. It also long-polls `GET /agent/v1/commands` so the backend can ask it to stop a process after you approve.

Its token lives in the **macOS Keychain** (service name chosen at install). The agent reads process command lines locally to classify them (for example "Node dev server") but only sends labels, executable paths and numbers.

## 2. Hetzner monitoring backend

`backend/watcher/` is a FastAPI application run by Uvicorn as a **systemd user service** (`infra-watcher`). It listens on `127.0.0.1:8200` only, with a 300 MB memory cap and 60 % CPU cap. One process does everything: HTTP API, collection, rule engine, Telegram. Background loops are `asyncio` tasks (`collect_loop`, `maintenance_loop`, `telegram_poll_loop`).

## 3. System metrics collection

The server's own metrics come from `collect_linux.py`, which reads `/proc` and `/sys` directly (CPU ticks, `/proc/meminfo`, load, disk free, disk I/O) rather than running shell commands. Every **30 seconds** (`refresh_interval`) `collect_loop` takes one sample and stores it. Process lists refresh every 60 seconds.

## 4. Docker metrics collection

`docker_cli.py` calls the `docker` command line with read-only subcommands (list, inspect, df). Container metadata refreshes every 60 s; per-container CPU and memory come from cgroup files each cycle, so no `docker stats` stream is held open. Container log sizes are measured every 15 minutes. The one write operation is `docker stop` (see section 12).

## 5. Metrics ingestion and authentication

The Mac's `POST /agent/v1/metrics` is checked by `agent_auth`: the bearer token is hashed (SHA-256) and compared with `agents.json`, which stores only hashes. Failed attempts are rate-limited per IP, the payload is validated (CPU 0–100 and so on) and capped at about 512 KB. The backend stamps each sample with its **own** clock, so a Mac with a wrong clock cannot poison history.

## 6. SQLite data storage

One SQLite file (WAL mode). Main tables:

| Table | Holds |
|---|---|
| `samples` | one row per collection per machine: CPU, load, memory, swap, disk |
| `snapshots` | JSON blobs: process lists, container lists, Docker disk usage |
| `incidents` | alerts: state (active / recovered), cause, impact, recommendation |
| `pending` | problems seen but not yet persistent enough to alert |
| `approvals`, `audit` | stop confirmations and a log of who did what |
| `notifications`, `logs`, `kv` | delivery log, app diagnostics, small key/value state |

Secrets are **not** in the database; they live in `secrets.env` (mode 0600) beside it.

## 7. Alert evaluation and persistence

After each collection the rule engine (`engine.py`) evaluates every rule against recent samples. A problem first becomes *pending*. Only if it is still present after the persistence time (default 180 s; CPU 300 s) does it become an *active incident*. It recovers after staying clear for 120 s. Details and examples: [monitoring.md](monitoring.md).

## 8. Telegram notification delivery

`notify.py` sends one message per new incident through the Telegram Bot API, with the measured values, likely cause and Stop buttons where applicable. Deduplication, a cooldown and one recovery message are covered in [monitoring.md](monitoring.md). In production the bot is **polled** (`TELEGRAM_MODE=polling`): the backend asks Telegram for button taps, so no public URL is needed.

## 9. Tailscale private networking

Tailscale builds a private WireGuard network (a "tailnet") between your own devices. The server runs `tailscaled` as a user service in userspace mode (`infra-tailscaled`), so no root or kernel changes are needed. Only devices signed in to your tailnet can even reach the server. Funnel, Tailscale's feature for exposing a service to the public internet, is **disabled**.

## 10. Tailscale Serve and HTTPS

`tailscale serve` takes HTTPS connections on the machine's `*.ts.net` name (with a certificate Tailscale issues) and proxies them to `http://127.0.0.1:8200`. The backend therefore never speaks TLS itself and is never bound to a public interface.

## 11. Dashboard access authentication

Being on the tailnet is not enough for the dashboard API. `tailscale serve` adds a `Tailscale-User-Login` header with the identity it authenticated. The backend (`security.tailscale_owner_ok`) accepts a request only if all of these hold: the peer is loopback, the header equals the one configured owner login, and there is no Funnel header. Anything else gets `403`. State-changing calls also need a CSRF token. Details: [security.md](security.md).

## 12. Controlled container stop operations

Nothing is ever stopped automatically. Stopping is two steps: **prepare** (the backend validates the target and returns a single-use token valid for 120 s, bound to the exact container id by an HMAC) and **confirm** (the token is spent). Only `docker stop` on a validated 64-hex container id is allowed; there is no kill, rm or exec. On the Mac, the agent re-checks the process identity and sends SIGTERM, never SIGKILL. Every step is written to the audit table.

## 13. 24-hour retention and cleanup

All time-based data is deleted once it is older than the retention window. The window is 24 hours and cannot be raised above that (settings may only lower it). Cleanup runs at service start and then hourly; a daily database backup is kept for at most 24 hours, config backups for 7 copies.

## A metric's journey

**Mac metric: from the laptop to a graph**

1. The agent samples CPU, memory, swap, disk and top processes on the MacBook.
2. It sends them with `POST /agent/v1/metrics` over Tailscale, with its bearer token.
3. `tailscale serve` terminates HTTPS and proxies the call to the backend on loopback.
4. `agent_auth` hashes the token and compares it with the stored hash, validates the payload and stamps it with the **server's** clock.
5. The backend inserts a row into `samples` (numbers) and `snapshots` (process list) and records `last_seen`.
6. The rule engine evaluates the thresholds. Nothing is wrong yet, so no incident exists.
7. Meanwhile the browser polls `GET /api/overview`. `views.py` reads the latest rows, works out *connection* (how recent `last_seen` is) separately from *health* (what the numbers say), and returns JSON.
8. The dashboard draws the card and graph. It never asked anything to collect.

**Hetzner metric**: step 1 is the backend's `collect_loop` reading `/proc`, `/sys` and Docker itself every 30 s, and steps 2 to 5 are internal. Steps 6 to 8 are identical.

**From a metric to a notification**

1. Memory on the MacBook crosses 85 %. The engine records a *pending* problem with its start time.
2. Each collection re-checks it. If it clears, the pending row is deleted and nothing happens.
3. After 180 s it is still there: an **incident** is created (*active*) and shown in Activity.
4. `explain.py` builds the explanation (what, why, likely cause, steps) when it is read or sent.
5. `notify.py` sends one Telegram message, and records delivery on the incident.
6. When the measurement stays under the limit for 120 s, the incident becomes *recovered* and one recovery message is sent.
7. If you tap Stop, the backend polls the tap, asks for confirmation (single-use token, 120 s), and only then runs `docker stop` or tells the Mac agent to send SIGTERM. Each step is audited.

## Data flow

**Mac to server:** agent samples locally, then `POST /agent/v1/metrics` over Tailscale, token verified, rows inserted into `samples` and `snapshots`, `last_seen` updated.

**Server to itself:** `collect_loop` reads `/proc` and Docker, inserts rows, then runs `engine.tick` and the notifier. This does not depend on the Mac.

**Alert to phone:** engine opens an incident, the notifier sends one Telegram message, you may tap Stop, the backend polls that tap, asks you to confirm, then runs `docker stop`.

**Browser:** the page fetches `/api/overview` and friends every 30 s. These endpoints read what is already in SQLite; they do not collect anything.

## Request / response flow (dashboard)

```mermaid
sequenceDiagram
    participant B as Browser (on tailnet)
    participant T as tailscale serve
    participant A as Backend (127.0.0.1:8200)
    participant D as SQLite
    B->>T: GET /api/overview (HTTPS)
    T->>A: GET /api/overview + Tailscale-User-Login
    A->>A: loopback? owner login matches? no Funnel header?
    A->>D: read latest rows (no collection)
    D-->>A: cached samples, incidents
    A-->>T: JSON (connection state, values, age)
    T-->>B: HTTPS response
```

## Security boundaries

1. **Network:** only your tailnet reaches the server; Funnel off; backend on loopback.
2. **Dashboard user:** one Tailscale identity, plus CSRF on writes.
3. **Mac agent:** its own bearer token (hash on server, Keychain on Mac), revocable.
4. **Telegram:** secret header (webhook mode only), allow-listed user id and private chat id.
5. **Actions:** two-step, single-use, HMAC-bound approval; allowlisted operations only.

## Honest limitations

- The server user is in the `docker` group, which is root-equivalent on that host. Safety comes from the code paths above, so review them for your own setup.
- Two machines are hard-wired (`mac`, `hetzner`); this is not a general fleet monitor.
- Data older than 24 hours is gone by design; there is no long-term history.
- If the server itself is off, nothing can alert you; an external uptime check is recommended (see SETUP).
