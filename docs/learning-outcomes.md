# What you can learn from Astra Watch

Astra Watch is small enough to read end to end, but it contains real versions of problems that larger monitoring systems also face. Each section below names the idea and where to look.

| Topic | What to study | Where |
|---|---|---|
| A lightweight agent | A single-file, standard-library program that samples a laptop, runs under launchd, and uses the Keychain for its token | `agent/mac_agent.py`, `agent/install.sh` |
| Collecting metrics from different machines | The Mac agent *pushes* numbers; the server *samples itself* by reading `/proc`. Two sources, one stored format | `collect_linux.py`, `POST /agent/v1/metrics` in `main.py` |
| A backend for monitoring data | One `samples` table for numbers, JSON `snapshots` for lists, validated and size-capped input, server-side timestamps | `db.py`, `main.py` |
| Heartbeat and connectivity | `last_seen` decides *Connected*, *Stale* or *Offline*. A machine's health and its connection are modelled separately | `views.py`, `docs/monitoring.md` |
| Collection vs dashboard refresh | The server collects on one clock; the browser re-reads stored data on another. Opening ten tabs cannot cause ten scans | `refresh_interval`, `dashboard_refresh_seconds`, `hooks.ts` |
| Visualising time series | Range selection, one machine per graph, gaps where a machine was silent, hand-drawn SVG | `frontend/src/charts.tsx`, `/api/history` |
| Incident detection and recovery | Pending, active and recovered states; persistence before alerting; recovery only after a quiet period; stable incident identity | `engine.py` |
| Monitoring Docker containers | Read-only inspection, restart-loop detection, log-size checks, cgroup reads instead of a `docker stats` stream | `docker_cli.py`, `engine.py` |
| Operational notifications | One message per incident, a cooldown against flapping, retried delivery, one recovery message | `notify.py`, `telegram.py` |
| Private infrastructure access | Loopback-only backend, Tailscale Serve for HTTPS, an identity check on top of network access, CSRF for writes | `security.py`, `docs/security.md` |
| Safe operational actions | Allowlist, single-use HMAC-bound approvals, an audit trail, protected services, a dry-run-only automation policy | `actions.py`, `automation.py` |
| Explaining problems honestly | A cause is "confirmed" only if observed, otherwise "inferred", never presented as certain | `explain.py` |
| Building and deploying several parts | Python backend, React build, a launchd agent, systemd units, versioned releases with automatic rollback | `deploy/`, `frontend/`, `agent/` |
| Testing monitoring logic | Tests for the claims that matter, for example that reading the dashboard cannot create incidents or leak secrets | `backend/tests/`, `frontend/src/*.test.tsx` |

## Suggested reading order

1. [README](../README.md) and the screenshots, to see the product.
2. [architecture.md](architecture.md), following one metric from the Mac to a graph.
3. `engine.py` and [monitoring.md](monitoring.md), to see how numbers become incidents.
4. [security.md](security.md) with `security.py` and `actions.py`.
5. `scripts/demo_server.py`, which shows how to run everything on synthetic data.

## What I learned while building Astra Watch

**Silence is not the same as health.** Showing the last numbers a machine sent is misleading when a MacBook goes to sleep: the card would still look fine. So I keep *health* and *connection* separate, grey out values that are not current, and show the last-seen time. The Mac needs a clear rule for what happens when it sleeps, shuts down and wakes.

**Collecting and refreshing are different jobs.** One setting used to be labelled "Refresh interval" but only controlled collection, while the browser polled on its own timers. That was confusing. I split them into a collection interval, a dashboard refresh interval, alert persistence and a notification cooldown, and documented all four in [monitoring.md](monitoring.md). The dashboard also became strictly read-only so that viewing it cannot start work, and there is a test for that.

**A spike is not an incident.** Alerting on a single high reading would be noisy, so a problem must persist before it counts and must stay clear for a quiet period before it is declared recovered. That also meant giving incidents a stable identity (rule, machine and subject) so one problem sends one message.

**Be honest about causes.** A "largest memory user" is not necessarily the cause. I added a confidence label so that only directly observed facts, such as Docker reporting an out-of-memory kill, are called confirmed. Everything else is called inferred, and the wording says so.

**Private access is two problems.** Keeping the backend off the public internet was solved with Tailscale. Deciding *who* on the tailnet may use it is separate, so the backend also checks one authorised identity. I moved to Tailscale from an earlier Cloudflare-based setup, which removed the public entry point entirely.

**Make the dangerous action hard to do wrongly.** The server user can use Docker, which is powerful. So the only mutating operation is `docker stop` on a validated container id, behind a prepared, single-use, expiring approval, with an audit trail and a list of protected services. The automation policy is *dry run only*, with autonomous execution fixed off in code, so a policy can be explored without anything ever acting on its own.

**A monitor can fail too.** Astra Watch runs on the server it watches, so it cannot tell me when that server is down. I added an optional outbound heartbeat that is sent only when collection is actually healthy, and I have kept this limitation visible rather than hiding it.

**Small dependencies are a feature.** The backend needs two Python packages and the Mac agent needs none, which made deployment to the server (vendored wheels, no package install on the host) simple and left less to keep patched.

**Documentation screenshots need their own data.** A real dashboard contains real hostnames and workloads, so I wrote a demo server that generates synthetic data in a throwaway database so the screenshots are genuine renders and the repository exposes nothing.
