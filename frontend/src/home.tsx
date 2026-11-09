import { ActivityItem, Overview, Resource, Summary } from "./api";
import { MachineMonitor } from "./charts";
import { usePoll } from "./hooks";
import { ConnectionLine, Icon, MACHINE_NAME, Meter, ago, fmtBytes, fmtMem } from "./ui";

export const MACHINE_OS: Record<string, string> = { mac: "macOS", hetzner: "Ubuntu" };

/** Pill = the machine's state in one word. Health and connection stay separate facts; a pill never says "Online" for a machine with a problem. */
export function statePill(s: Pick<Summary, "connection" | "status">): { cls: string; text: string } {
  const l = s.status.level;
  if (l === "good") return { cls: "good", text: "Online" };
  if (l === "critical") return { cls: "critical", text: "Critical" };
  if (l === "attention") return { cls: "attention", text: "Needs attention" };
  if (s.connection === "offline") return { cls: "critical", text: "Offline" };
  if (s.connection === "stale") return { cls: "attention", text: "Stale" };
  return { cls: "", text: "Not reporting" };
}
export const Pill = ({ s }: { s: Pick<Summary, "connection" | "status"> }) => { const p = statePill(s); return <span className={`pill ${p.cls}`}>{p.text}</span>; };

export function ResourceRows({ rows, dim }: { rows: Resource[]; dim?: boolean }) {
  return (
    <ul className={`res ${dim ? "dim" : ""}`}>
      {rows.map(r => (
        <li key={r.key} className={r.level}>
          <div className="res-top"><b>{r.label}</b><span>{r.value}</span></div>
          <Meter pct={r.pct} level={r.level} />
          <p>{r.text}</p>
        </li>
      ))}
    </ul>
  );
}

/** One sentence about Docker, kept out of the bar list because containers have no percentage. */
export function dockerLine(s: Pick<Summary, "machine" | "containers" | "unhealthy_containers">): string | null {
  if (s.containers == null) return s.machine === "mac" ? null : "No container information yet.";
  const n = s.containers, bad = s.unhealthy_containers ?? 0;
  const base = `${n} container${n === 1 ? "" : "s"} running.`;
  return bad > 0 ? `${base} ${bad} ${bad === 1 ? "is" : "are"} failing its health check.` : `${base} No unhealthy containers reported.`;
}

function Gauge({ label, pct, detail, level }: { label: string; pct: number | null; detail?: string; level: string }) {
  return (
    <div className="gauge">
      <span className="gauge-label">{label}</span>
      <span className="gauge-val">{pct == null ? "–" : `${Math.round(pct)}%`}{detail && <small>{detail}</small>}</span>
      <Meter pct={pct} level={level === "good" ? "" : level} />
    </div>
  );
}

export function MachineCard({ s, href, hours = 24 }: { s: Summary; href: string; hours?: number }) {
  const name = MACHINE_NAME[s.machine];
  const lv = (k: string) => s.resources.find(r => r.key === k)?.level ?? "good";
  const used = s.disk_total != null && s.disk_free != null ? s.disk_total - s.disk_free : null;
  const dim = s.connection !== "connected";
  const issues = s.alerts.length;
  const docker = dockerLine(s);
  return (
    <section className="panel machine" aria-label={name}>
      <div className="machine-head">
        <span className="machine-ico"><Icon name={s.machine === "mac" ? "laptop" : "server"} size={26} /></span>
        <div className="machine-id">
          <h3><a href={href}>{name}</a></h3>
          <p>{MACHINE_OS[s.machine]}{s.mem_total ? ` · ${fmtMem(s.mem_total)} RAM` : ""}</p>
        </div>
        <Pill s={s} />
        <a className="go" href={href} aria-label={`${name} details`}><Icon name="chevron" size={18} /></a>
      </div>
      <ConnectionLine connection={s.connection} last={s.last_seen} />
      {issues > 0 && <p className="machine-note">{issues} active {issues === 1 ? "issue" : "issues"}. {s.status.headline}</p>}
      {dim && issues === 0 && <p className="machine-note">{s.status.headline}</p>}
      <div className={`gauges ${dim ? "dim" : ""}`}>
        <Gauge label="CPU" pct={s.cpu} level={lv("cpu")} />
        <Gauge label="Memory" pct={s.mem_pct} detail={s.mem_used != null && s.mem_total != null ? `${fmtMem(s.mem_used)} / ${fmtMem(s.mem_total)}` : undefined} level={lv("memory")} />
        <Gauge label="Storage" pct={used != null && s.disk_total ? (100 * used) / s.disk_total : null} detail={used != null && s.disk_total != null ? `${fmtBytes(used)} / ${fmtBytes(s.disk_total)}` : undefined} level={lv("storage")} />
      </div>
      <MachineMonitor machine={s.machine} hours={hours} />
      {docker && <p className="foot">Docker · {docker}</p>}
    </section>
  );
}

const icoFor = (i: ActivityItem) => i.state === "recovered" ? "ok" : i.state === "active" ? (i.severity === "critical" ? "bad" : "warn") : "info";

export function HomePage() {
  const { data, err } = usePoll<Overview>("/api/overview");
  if (!data) return <><h1 className="sr">Infrastructure Overview</h1>{err ? <div className="banner">{err}</div> : <p className="muted">Loading…</p>}</>;
  const o = data.overall;
  const containers = (data.mac.containers ?? 0) + (data.hetzner.containers ?? 0);
  const now = new Date();
  const word = { good: "Operational", attention: "Needs attention", critical: "Critical", offline: "Degraded", unknown: "Not reporting" }[o.level];
  const first = data.attention[0];
  const sub = o.level === "good" ? "All systems are healthy" : first ? `${first.headline}${data.attention.length > 1 ? ` (+${data.attention.length - 1} more)` : ""}` : o.text;
  return (
    <>
      {err && <div className="banner">{err}</div>}
      <header className="page-top">
        <p className="date">{now.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" })}</p>
        <h1>Infrastructure Overview</h1>
        <p className="sub">Live health and resource use for each machine. Each graph shows one machine only.</p>
      </header>

      <section className="strip panel" aria-label="Infrastructure health" aria-live="polite">
        <div className={`strip-state ${o.level}`}>
          <span className="state-ico"><Icon name={o.level === "good" ? "check" : "alert"} size={22} /></span>
          <div><b>{word}</b><span>{sub}</span></div>
        </div>
        <div className="strip-fact"><Icon name="machines" size={26} /><div><b>{o.machines ?? 2}</b><span>Machines · {o.healthy ?? 0} healthy</span></div></div>
        <div className="strip-fact"><Icon name="box" size={26} /><div><b>{containers}</b><span>Containers</span></div></div>
        <div className="strip-fact"><Icon name="alert" size={26} /><div><b className={(o.critical_incidents ?? 0) > 0 ? "bad" : (o.active_incidents ?? 0) > 0 ? "warn" : ""}>{o.active_incidents ?? 0}</b><span>Active incidents{(o.critical_incidents ?? 0) > 0 ? ` · ${o.critical_incidents} critical` : ""}</span></div></div>
        <div className="strip-fact"><Icon name="clock" size={26} /><div><span>Last update</span><b className="sm">{o.last_report ? new Date(o.last_report * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never"}</b></div></div>
      </section>

      <div className="machines">
        <MachineCard s={data.mac} href="#/machines/mac" hours={data.retention.hours} />
        <MachineCard s={data.hetzner} href="#/machines/hetzner" hours={data.retention.hours} />
      </div>

      <div className="lower">
        <section className="panel pad" aria-label="Incidents">
          <div className="panel-head"><h2><Icon name="alert" size={20} />Incidents</h2><a href="#/activity">View all <Icon name="arrow" size={14} /></a></div>
          {data.attention.length === 0 ? (
            <div className="calm"><span className="state-ico ok"><Icon name="check" size={22} /></span><b>No active incidents</b><span>Your infrastructure is running smoothly.</span></div>
          ) : (
            <ul className="feed">
              {data.attention.map(a => (
                <li key={a.id} className={a.severity}>
                  <span className={`fico ${a.severity === "critical" ? "bad" : "warn"}`}><Icon name="alert" size={14} /></span>
                  <div><a href={`#/activity/${a.id}`}><b>{a.headline}</b></a><span>{a.what}</span></div>
                  <time>{MACHINE_NAME[a.machine]} · {ago(a.since)}</time>
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className="panel pad" aria-label="Recent activity">
          <div className="panel-head"><h2><Icon name="activity" size={20} />Recent activity</h2><a href="#/activity">View all <Icon name="arrow" size={14} /></a></div>
          {data.recent.length === 0 ? <p className="muted">Nothing has happened in the last {data.retention.history_days} days.</p> : (
            <ul className="feed quiet">
              {data.recent.slice(0, 6).map(i => (
                <li key={i.id}>
                  <span className={`fico ${icoFor(i)}`}><Icon name={i.state === "recovered" ? "check" : i.state === "active" ? "alert" : "info"} size={14} /></span>
                  <div><a href={`#/activity/${i.id}`}><b>{i.title}</b></a><span>{MACHINE_NAME[i.machine]}{i.state === "recovered" ? " · recovered" : i.state === "active" ? " · ongoing" : " · ended"}</span></div>
                  <time>{ago(i.last_ts)}</time>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}
