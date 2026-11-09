# Monitoring and alerting

## What is measured

| Measure | Meaning | Example |
|---|---|---|
| **CPU usage** | Share of processor time in use. On the server it is computed from `/proc/stat` ticks between two samples. | 14 % is relaxed; 95 % for five minutes is a problem. |
| **Memory** | RAM in use compared with total (cache that can be dropped is not counted as "used"). | 14.1 GB of 16 GB = 88 %. |
| **Swap** | Disk space the OS uses as slow emergency memory. Some swap is normal; swap that keeps *growing* means RAM is short. | Swap grows by 500 MB in 30 minutes: warning. |
| **Disk usage** | Free space on the main disk. | Warning below 20 % free, critical below 10 %. A separate rule warns if free space is shrinking fast. |
| **Processes** | Top processes by CPU and memory. On the Mac, development workloads are grouped (Node, test workers, Docker VM). | "6 Node processes, 1.9 GB". |
| **Docker containers** | Status, health, restarts, CPU, memory and log size for every container. | A container restarting 3 times in 15 minutes raises an alert. |

## Alert thresholds

Defaults live in `backend/watcher/config.py` and can be changed in **Settings** (values are range-checked):

| Rule | Warning | Critical |
|---|---|---|
| Memory | 85 % | 95 % |
| CPU (sustained) | 85 % | 95 % |
| Disk free | below 20 % | below 10 % |
| Swap | growth 500 MB in 30 min | 90 % of swap used |
| Container restarts | 3 in 15 min | restarting loop |
| Container log size | 500 MB | |
| Mac not reporting | Stale after 120 s | Offline after 15 min (notified) |

## The four clocks (do not confuse them)

| What | Default | Where | What it does | Example |
|---|---|---|---|---|
| **Dashboard refresh** | 30 s | Settings: *Dashboard refresh* | How often the **browser** re-reads data that is already stored. It never starts a collection. | You open the page and leave it; it re-reads every 30 s. Ten open tabs cost ten cheap database reads, not ten scans. |
| **Collection interval** | 30 s | Settings: *Collection interval* | How often the **server** samples itself, and how often the Mac agent is told to report. The alert rules run right after each collection. | At 12:00:00 and 12:00:30 the server reads `/proc` and Docker, stores a row, then re-checks every rule. |
| **Alert persistence** | 180 s (CPU 300 s) | *Problem must last…* | A problem must be present this long before it becomes an alert, so a brief spike is ignored. | Memory hits 91 % for 40 s then drops: nothing happens. It stays above 85 % for 3 minutes: an incident opens. |
| **Notification cooldown** | 30 min | *Notification cooldown* | Stops repeat Telegram messages for the same problem. | A flapping problem that reopens after 10 minutes does not message you again until 30 minutes have passed. |

Alert evaluation is part of the collection loop, so there is no separate "alert interval": rules are checked every collection. Changing the collection interval changes how quickly a problem can be *noticed*; persistence decides how long it must last before you are *told*.

Before this was documented, a single setting (`refresh_interval`) was labelled "Refresh interval" although it only controlled collection and agent reporting. The browser polled on its own fixed timers (10 to 30 s). They are now separate: `dashboard_refresh_seconds` (browser) and `refresh_interval` (collection).

### Actual backend timings

- Server host sample: every 30 s (`refresh_interval`, 15 to 300 s allowed).
- Process list: every 60 s. Container metadata: every 60 s. Container log sizes: every 15 min.
- Mac agent report: every 30 s (it follows the server's `refresh_interval`, clamped to 15 to 120 s).
- Browser: every 30 s (`dashboard_refresh_seconds`, 10 to 300 s allowed).

## Problem persistence, step by step

1. A rule sees a problem: it is stored as *pending* with the time it was first seen.
2. Each collection the rule is evaluated again. If it disappears, the pending record is deleted. No alert, no message.
3. If it is still there after the persistence time, an **incident** is created (state *active*) and shown on the dashboard.
4. The notifier sends one Telegram message for that incident.
5. When the problem is gone for 120 s the incident becomes *recovered* and a single "cleared" message is sent (if recovery notifications are on).

## Notification deduplication

- An incident is identified by *rule + machine + subject* (for example `restart_loop` + `hetzner` + container id). While it is active, further evaluations only update it; they never create another one.
- Each incident is sent once (`notify_state = sent`). Severity escalation (warning to critical) can send one more message.
- A cooldown suppresses a second message for the same problem within 30 minutes.
- Refreshing or opening the dashboard cannot create incidents or messages: the API only reads. This is covered by the test `test_overview_does_not_collect_or_create_incidents`.

## Healthy is not the same as reporting

Astra Watch tracks two separate questions. **Health** asks whether the numbers it has are good. **Connection** asks whether those numbers are recent. A machine can be perfectly healthy and still *Stale*, for example a sleeping MacBook whose last report looked fine. A machine can also be *Connected* and unhealthy. The dashboard never merges the two: stale values are greyed and labelled "not current", and silence is never presented as "all good".

## Device status: Connected, Stale, Offline

Every device card shows a connection state, the time it was **last seen** and whether the numbers are current.

| State | Rule (Mac) | What the dashboard does |
|---|---|---|
| **Connected** | A report arrived in the last 120 s (`agent_grace_seconds`, four missed 30 s reports) | Numbers are current. |
| **Stale** | No report for 120 s up to 15 min | Values greyed, labelled "not current". |
| **Offline** | No report for 15 min or more (`agent_notify_minutes`) | Last values kept, labelled with last-seen time; one Telegram notification (unless the agent was stopped cleanly). |

The server uses the same idea with its own thresholds (stale after the larger of 90 s and four collection intervals, offline after five times that).

What happens on a Mac:

- **Awake, screen locked:** the agent keeps running and reporting, Tailscale stays up. Connected.
- **Asleep:** the process is suspended, so reports stop. After 2 minutes it shows Stale, after 15 minutes Offline. Old metrics are always labelled as not current.
- **Shut down:** the agent tries to send a last "clean stop" report when launchd terminates it (it only gets through if the network is still up). No new metrics arrive, the card turns Stale then Offline, and no alert is sent because the stop was clean.
- **Wake / boot:** launchd starts (or the OS resumes) the agent, Tailscale reconnects, the agent authenticates with the token in the Keychain (same pairing, same agent id), the next report is accepted and the card returns to Connected. History is never deleted when a device disconnects.

The server keeps monitoring itself whether or not the Mac is present: its collection loop, Docker checks, rule engine and Telegram notifier are separate from the agent endpoint.

## 24-hour retention

All history is deleted once older than 24 hours (a hard cap; Settings can only lower it). Cleanup runs at startup and every hour. Example: at 15:00 on Tuesday, the oldest chart point is about 15:00 on Monday. Incidents that recovered more than 24 hours ago are removed too. The database therefore stays small (under a few MB for two machines).

## Incident explanations and history

Each incident is explained in plain English (what happened, why it matters, likely cause, numbered steps, how to verify, whether Astra Watch acted). A cause is labelled **Confirmed** only when something was directly observed, such as Docker reporting an out-of-memory kill. A "largest user" is always **Inferred**. Memory is shown in binary units (an 8 GiB Mac reads "8 GB"); storage stays decimal like Finder.

Every incident records lifecycle events (detected, investigated, recommended, notified, action, verified, recovered). Retention is deliberately separate:

| Data | Kept |
|---|---|
| Raw metrics, process and container snapshots | 24 hours (hard cap) |
| Incident events and the action audit trail | 30 days |

## Automation policy (dry run only)

`watcherctl automation-*` edits an owner-only policy file; the dashboard only displays it. Modes: Explain (recommend only), Assisted (a human confirms every stop, the default), Autonomous (reserved: autonomous execution is a code constant and is **not available**). Containers must be explicitly allow-listed to be considered, the deny list always wins and `immich*` is permanently protected. The dry run records what *would* happen after sustained-limit, host-pressure and cooldown checks; it never stops anything.

## External watchdog

Astra Watch runs on the server it watches, so it cannot report its own death. When `HEARTBEAT_URL` is set (`watcherctl set-heartbeat <https url>`), it sends an outbound ping about once a minute, **only while the latest server sample is fresh and collection is succeeding**. A hosted dead-man's-switch (Healthchecks.io was chosen: free hobbyist tier, built-in Telegram integration, plain HTTP ping) alerts you from outside when pings stop. Nothing is exposed inbound; the URL is a secret kept in `secrets.env`.
