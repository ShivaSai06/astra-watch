import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { ApiError, Container, Detail, Incident, Overview, Proc, Summary, get, getRefreshMs, send, subscribeRefresh } from "./api";
import { LineChart, Meter, STATE_LABEL, Stat, StopDialog, ago, fmtBytes, fmtDur, fmtPct, fmtTime } from "./ui";

export const useRefreshMs = () => useSyncExternalStore(subscribeRefresh, getRefreshMs);

/** Re-reads a cached API endpoint every dashboard_refresh_seconds. These endpoints never trigger collection. */
export function usePoll<T>(path: string) {
  const ms = useRefreshMs();
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => get<T>(path).then(d => { setData(d); setErr(""); }).catch(e => setErr(e instanceof ApiError ? e.message : String(e))), [path]);
  useEffect(() => { setData(null); load(); const i = setInterval(load, ms); return () => clearInterval(i); }, [load, ms]);
  return { data, err, reload: load };
}

function verdict(s: Summary): string {
  if (s.health === "unknown") return "No data yet.";
  if (s.health === "stale") return `Not reporting. Last update ${ago(s.last_update)}; the values below are the last ones received, not current. ${s.machine === "mac" ? "The Mac may be asleep." : ""}`;
  if (s.health === "offline") return `Offline. Last seen ${ago(s.last_seen)}; the values below are the last ones received, not current. ${s.machine === "mac" ? "The Mac is asleep, shut down or the agent is stopped." : "The collector has stopped reporting."}`;
  if (!s.alerts.length) return "Everything looks normal.";
  return s.alerts.length === 1 ? s.alerts[0].title + "." : `${s.alerts.length} problems: ${s.alerts[0].title.toLowerCase()} and more.`;
}

function SystemCard({ s, name, href }: { s: Summary; name: string; href: string }) {
  const free = s.disk_total ? (100 * (s.disk_free ?? 0)) / s.disk_total : null;
  return (
    <section className={`card ${s.health}`} aria-label={name}>
      <div className="card-head"><h3><a className="cover" href={href}>{name}</a></h3><span className={`state ${s.health}`}>{STATE_LABEL[s.health]}</span></div>
      <p className="verdict">{verdict(s)}</p>
      <dl className="stats">
        <Stat label="CPU" value={fmtPct(s.cpu)} meter={<Meter pct={s.cpu} warn={85} crit={95} />} />
        <Stat label="Memory" value={fmtPct(s.mem_pct)} sub={`${fmtBytes(s.mem_used)} of ${fmtBytes(s.mem_total)}`} meter={<Meter pct={s.mem_pct} />} />
        <Stat label="Swap" value={fmtBytes(s.swap_used)} sub={s.swap_total ? `of ${fmtBytes(s.swap_total)}` : ""} meter={<Meter pct={s.swap_total ? (100 * (s.swap_used ?? 0)) / s.swap_total : 0} warn={70} crit={90} />} />
        <Stat label="Storage free" value={fmtBytes(s.disk_free)} sub={free != null ? `${Math.round(free)}% free` : ""} meter={<Meter pct={free == null ? null : 100 - free} invert />} />
        {s.machine === "hetzner" && <Stat label="Docker containers" value={s.containers == null ? "–" : String(s.containers)} sub="running" />}
        <Stat label="Connection" value={({ connected: "Connected", stale: "Stale", offline: "Offline", unknown: "Unknown" } as const)[s.connection]} sub={s.connection === "connected" ? "data is current" : "data is not current"} />
      </dl>
      {s.alerts.map(a => <a key={a.id} className="alert-line" href={`#/alerts/${a.id}`}><span className={`dot ${a.severity}`} />{a.title}</a>)}
      <div className="foot">Last seen: {s.last_update ? `${ago(s.last_update)} (${fmtTime(s.last_update)})` : "never"}{s.fresh ? "" : " · showing last reported values"}</div>
    </section>
  );
}

export function OverviewPage() {
  const { data, err } = usePoll<Overview>("/api/overview");
  return (
    <>
      <h1>Overview</h1>
      <p className="sub">Is everything healthy? What is causing a problem? What can be stopped?</p>
      {err && <div className="banner">{err}</div>}
      {data && (
        <>
          <div className="grid2">
            <SystemCard s={data.mac} name="My Mac" href="#/mac" />
            <SystemCard s={data.hetzner} name="Server" href="#/hetzner" />
          </div>
          <p className="foot">History kept for {data.retention.hours} h (maximum {data.retention.max_hours} h). Oldest record: {data.retention.oldest ? fmtTime(data.retention.oldest) : "none yet"}.</p>
        </>
      )}
    </>
  );
}

const RANGES = [["1h", "Last hour"], ["24h", "24 hours"]] as const;

function Trends({ machine }: { machine: string }) {
  const [range, setRange] = useState<"1h" | "24h">("1h");
  const { data } = usePoll<{ points: any[] }>(`/api/history?machine=${machine}&range=${range}`);
  const pts = (k: string) => (data?.points ?? []).map(p => ({ t: p.ts, v: p[k] as number | null }));
  const free = (data?.points ?? []).map(p => ({ t: p.ts, v: p.disk_free != null ? p.disk_free / 1e9 : null }));
  return (
    <>
      <div className="toolbar"><h2>Trends</h2>
        <div className="seg" role="group" aria-label="Time range">
          {RANGES.map(([k, l]) => <button key={k} aria-pressed={range === k} onClick={() => setRange(k)}>{l}</button>)}
        </div>
      </div>
      <div className="charts">
        <LineChart title="CPU" points={pts("cpu")} max={100} />
        <LineChart title="Memory used" points={pts("mem")} max={100} />
        <LineChart title="Swap used" points={pts("swap").map(p => ({ ...p, v: p.v == null ? null : p.v / 1e9 }))} unit=" GB" fmt={v => v.toFixed(2) + " GB"} />
        <LineChart title="Free disk" points={free} fmt={v => v.toFixed(1) + " GB"} />
        <LineChart title="Running containers" points={pts("containers")} fmt={v => v.toFixed(0)} />
      </div>
    </>
  );
}

function ProcTable({ rows, title, onStop, showContainer }: { rows: Proc[]; title: string; onStop?: (p: Proc) => void; showContainer?: boolean }) {
  return (
    <>
      <h2>{title}</h2>
      <div className="tablewrap"><table>
        <thead><tr><th>Process</th><th className="num">PID</th><th className="num">CPU</th><th className="num">Memory</th><th>Context</th>{onStop && <th />}</tr></thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={6} className="muted">No data yet.</td></tr>}
          {rows.map(p => (
            <tr key={p.pid}>
              <td>{p.name}{p.label && !["other", "browser"].includes(p.label) && <> <span className="tag">{p.label}</span></>}</td>
              <td className="num mono">{p.pid}</td><td className="num">{p.cpu.toFixed(0)}%</td><td className="num">{fmtBytes(p.rss)}</td>
              <td className="muted">{p.why || (showContainer && p.container_name) || p.parent || ""}{p.growing && " · memory keeps growing"}</td>
              {onStop && <td className="num">{p.eligible && <button className="btn small danger" onClick={() => onStop(p)} aria-label={`Stop ${p.name} ${p.pid}`}>Stop…</button>}</td>}
            </tr>
          ))}
        </tbody>
      </table></div>
    </>
  );
}

function ContainerTable({ rows, onStop }: { rows: Container[]; onStop: (c: Container) => void }) {
  const sorted = [...rows].sort((a, b) => b.mem - a.mem);
  return (
    <div className="tablewrap"><table>
      <thead><tr><th>Container</th><th>Status</th><th className="num">CPU</th><th className="num">Memory</th><th className="num">Restarts</th><th>Details</th><th /></tr></thead>
      <tbody>
        {sorted.map(c => (
          <tr key={c.id}>
            <td><b>{c.name}</b><div className="muted mono">{c.short ?? c.id.slice(0, 12)} · {c.image}</div></td>
            <td><span className={`tag ${c.running ? (c.health === "unhealthy" ? "crit" : "ok") : ""}`}>{c.running ? (c.health && c.health !== "none" ? c.health : "running") : c.status}</span></td>
            <td className="num">{c.running ? `${c.cpu.toFixed(0)}%` : "–"}</td><td className="num">{c.running ? fmtBytes(c.mem) : "–"}</td>
            <td className={`num ${c.restarts >= 3 ? "state warning" : ""}`}>{c.restarts ?? "–"}</td>
            <td className="muted">
              {c.restart_policy && <>policy {c.restart_policy}</>}{c.ports?.length ? <> · {c.ports.slice(0, 2).join(", ")}</> : null}
              {c.mounts?.length ? <> · {c.mounts.length} mount{c.mounts.length > 1 ? "s" : ""}</> : null}
              {c.log_size != null && <> · log {fmtBytes(c.log_size)}</>}
            </td>
            <td className="num">{c.running && <button className="btn small danger" onClick={() => onStop(c)} aria-label={`Stop ${c.name}`}>Stop…</button>}</td>
          </tr>
        ))}
      </tbody>
    </table></div>
  );
}

export function MachinePage({ machine }: { machine: "mac" | "hetzner" }) {
  const { data: d, err, reload } = usePoll<Detail>(`/api/machine/${machine}`);
  const [stop, setStop] = useState<any>(null);
  const s = d?.sample;
  const mac = machine === "mac";
  const x = s?.extra ?? {};
  const pressure: Record<number, string> = { 1: "Normal", 2: "Elevated", 4: "Critical" };
  const done = () => { setStop(null); reload(); };
  return (
    <>
      <h1>{mac ? "My Mac" : "Server"}</h1>
      <p className="sub">{d ? <><span className={`state ${d.health}`}>{STATE_LABEL[d.health]}</span> · {verdict(d)} Last update {ago(d.last_update)}.</> : "Loading…"}</p>
      {err && <div className="banner">{err}</div>}
      {d && s && (
        <>
          <dl className="stats card" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(170px,1fr))" }}>
            <Stat label="CPU usage" value={fmtPct(s.cpu)} sub={mac && x.cpu_idle != null ? `${Math.round(x.cpu_idle)}% idle` : ""} />
            <Stat label="Load average" value={s.load1.toFixed(2)} sub={`${s.load5.toFixed(2)} · ${s.load15.toFixed(2)}`} />
            <Stat label={mac ? "Physical RAM" : "Total RAM"} value={fmtBytes(s.mem_total)} />
            <Stat label="Available RAM" value={fmtBytes(s.mem_total - s.mem_used)} sub={fmtPct(100 - s.mem_pct) + " free"} meter={<Meter pct={s.mem_pct} />} />
            {mac && <Stat label="Memory pressure" value={pressure[x.pressure_level] ?? "–"} />}
            {mac && <Stat label="Compressed memory" value={fmtBytes(x.compressed)} />}
            <Stat label="Swap" value={fmtBytes(s.swap_used)} sub={s.swap_total ? `of ${fmtBytes(s.swap_total)}` : ""} />
            <Stat label={mac ? "Internal SSD" : "Disk"} value={`${fmtBytes(s.disk_total - s.disk_free)} used`} sub={`${fmtBytes(s.disk_free)} free`} meter={<Meter pct={(100 * (s.disk_total - s.disk_free)) / s.disk_total} warn={80} crit={90} />} />
            {mac && <Stat label="Thermal" value={x.thermal ? (x.thermal.state === "nominal" ? "Normal" : x.thermal.state === "throttled" ? "Throttled" : "Not available") : "–"} />}
            {!mac && <Stat label="Disk I/O" value={x.io_read_mbs != null ? `${x.io_read_mbs} / ${x.io_write_mbs} MB/s` : "–"} sub={x.io_util != null ? `${Math.round(x.io_util)}% busy` : "read / write"} />}
            {!mac && <Stat label="Uptime" value={x.uptime ? fmtDur(x.uptime) : "–"} />}
          </dl>

          <Trends machine={machine} />

          {mac && d.procs && (
            <>
              <h2>Development workloads</h2>
              <div className="tablewrap"><table>
                <thead><tr><th>Type</th><th className="num">Processes</th><th className="num">Memory</th><th className="num">CPU</th><th>Notes</th></tr></thead>
                <tbody>
                  {Object.entries(d.procs.groups ?? {}).sort((a, b) => b[1].rss - a[1].rss).map(([k, g]) => (
                    <tr key={k}><td>{k === "docker-vm" ? "Docker virtual machine" : k}</td><td className="num">{g.count}</td><td className="num">{fmtBytes(g.rss)}</td><td className="num">{g.cpu.toFixed(0)}%</td>
                      <td className="muted">{g.orphans ? `${g.orphans} without a parent process` : ""}{g.idle ? `${g.orphans ? " · " : ""}${g.idle} idle` : ""}</td></tr>
                  ))}
                  {!Object.keys(d.procs.groups ?? {}).length && <tr><td colSpan={5} className="muted">No development processes running.</td></tr>}
                </tbody>
              </table></div>
              <ProcTable title="Top CPU processes" rows={d.procs.top_cpu} onStop={p => setStop({ machine, kind: "process", pid: p.pid, start: (p as any).start })} />
              <ProcTable title="Top memory processes" rows={d.procs.top_mem} onStop={p => setStop({ machine, kind: "process", pid: p.pid, start: (p as any).start })} />
              {(d.procs.dev ?? []).length > 0 && <ProcTable title="Development processes" rows={d.procs.dev!.slice(0, 20)} onStop={p => setStop({ machine, kind: "process", pid: p.pid, start: (p as any).start })} />}
            </>
          )}

          <h2>{mac ? "Docker" : "Docker containers"}</h2>
          {mac && <p className="muted">{d.procs?.docker_running ? `Docker Desktop is running. VM memory ${fmtBytes(d.procs?.docker_vm_rss)}.` : "Docker Desktop is not running."}</p>}
          {d.container_list?.list?.length
            ? <ContainerTable rows={mac ? d.container_list.list.map(c => ({ ...c, short: c.id.slice(0, 12), restarts: c.restarts ?? 0, ports: c.ports ?? [], mounts: c.mounts ?? [] })) : (d.container_list!.list as Container[])}
                onStop={c => setStop({ machine, kind: "container", id: c.id })} />
            : <div className="empty">No containers reported.</div>}

          {d.docker_df && (
            <>
              <h2>Storage used by Docker</h2>
              <dl className="stats card" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(170px,1fr))" }}>
                {d.docker_df.images_total != null && <Stat label="Images" value={fmtBytes(d.docker_df.images_total)} />}
                {d.docker_df.volumes_total != null && <Stat label="Volumes" value={fmtBytes(d.docker_df.volumes_total)} />}
                {d.docker_df.build_cache_total != null && <Stat label="Build cache" value={fmtBytes(d.docker_df.build_cache_total)} />}
                {d.docker_df.vm_disk_file != null && <Stat label="Docker VM disk file" value={fmtBytes(d.docker_df.vm_disk_file)} />}
                {d.logs && <Stat label="Container logs" value={fmtBytes(Object.values(d.logs.sizes).reduce((a, b) => a + b, 0))} />}
              </dl>
              {d.docker_df.top_volumes?.length > 0 && (
                <div className="tablewrap" style={{ marginTop: 10 }}><table><thead><tr><th>Largest volumes</th><th className="num">Size</th></tr></thead>
                  <tbody>{d.docker_df.top_volumes.slice(0, 6).map((v: any) => <tr key={v.name}><td className="mono">{v.name}</td><td className="num">{fmtBytes(v.size)}</td></tr>)}</tbody></table></div>
              )}
            </>
          )}
          {d.storage && d.storage.dirs.length > 0 && (
            <>
              <h2>Large development directories</h2>
              <div className="tablewrap"><table><tbody>{d.storage.dirs.slice(0, 10).map(r => <tr key={r.name}><td>{r.name}</td><td className="num">{fmtBytes(r.size)}</td></tr>)}</tbody></table></div>
              <p className="foot">Measured a few times a day at low priority, never continuously.</p>
            </>
          )}

          {!mac && d.procs && (
            <>
              <ProcTable title="Top processes by CPU" rows={d.procs.top_cpu} showContainer />
              <ProcTable title="Top processes by memory" rows={d.procs.top_mem} showContainer />
            </>
          )}
        </>
      )}
      <StopDialog request={stop} onClose={done} />
    </>
  );
}

const sevOrder = (i: Incident) => (i.state === "active" ? 0 : 1);

export function AlertsPage({ selected }: { selected?: string }) {
  const { data, err } = usePoll<{ alerts: Incident[] }>("/api/alerts");
  const [stop, setStop] = useState<any>(null);
  const list = [...(data?.alerts ?? [])].sort((a, b) => sevOrder(a) - sevOrder(b) || b.first_seen - a.first_seen);
  useEffect(() => { if (selected && data) document.getElementById(selected)?.scrollIntoView({ block: "center" }); }, [selected, data]);
  return (
    <>
      <h1>Alerts</h1>
      <p className="sub">Problems that lasted long enough to matter. Nothing here is ever stopped automatically.</p>
      {err && <div className="banner">{err}</div>}
      {data && list.length === 0 && <div className="empty">No alerts in the last 24 hours.</div>}
      {list.map(a => {
        const state = a.state === "active" ? a.severity : "recovered";
        return (
          <article key={a.id} id={a.id} className={`alert ${state} ${selected === a.id ? "sel" : ""}`}>
            <h3><span className={`dot ${a.state === "active" ? a.severity : "healthy"}`} />{a.title}</h3>
            <div className="muted">{a.machine === "mac" ? "My Mac" : "Server"} · detected {fmtTime(a.first_seen)} · {a.state === "active" ? <b>{a.severity}</b> : `recovered ${ago(a.recovered_at)}`}</div>
            <dl className="kv">
              <dt>Measured</dt><dd>{Object.entries(a.measured.summary ?? {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "–"}
                {a.measured.facts && <ul className="facts">{a.measured.facts.map((f, i) => <li key={i}>{f}</li>)}</ul>}</dd>
              <dt>Likely cause</dt><dd>{a.cause || "Not determined"} {a.confidence && <span className="tag">{a.confidence} confidence · inferred</span>}</dd>
              <dt>Impact</dt><dd>{a.impact}</dd>
              <dt>Recommended action</dt><dd>{a.recommendation}</dd>
              <dt>Notification</dt><dd>{a.notify_state === "sent" ? `Sent ${ago(a.notified_at)}` : a.notify_state === "suppressed" ? "Held back (cooldown)" : a.notify_state === "unconfigured" ? "Telegram not set up" : a.notify_state.startsWith("failed") ? "Delivery failed" : "Pending"}
                {a.state === "recovered" && (a.recovery_notified === 1 ? " · recovery sent" : "")}</dd>
              <dt>Recovered</dt><dd>{a.state === "recovered" ? `Yes, ${fmtTime(a.recovered_at)}` : "No, still active"}</dd>
            </dl>
            {a.state === "active" && a.targets.length > 0 && (
              <p>{a.targets.map((t, i) => (
                <button key={i} className="btn small danger" style={{ marginRight: 8 }}
                  onClick={() => setStop({ machine: a.machine, kind: t.kind, ...(t.kind === "container" ? { id: t.ref.id } : { pid: t.ref.pid, start: t.ref.start }) })}>
                  Stop {t.label}…</button>))}</p>
            )}
          </article>
        );
      })}
      <StopDialog request={stop} onClose={() => setStop(null)} />
    </>
  );
}

const THRESH: [string, string, string][] = [
  ["mem_warn", "Memory warning (%)", "number"], ["mem_crit", "Memory critical (%)", "number"],
  ["cpu_warn", "CPU warning (%)", "number"], ["cpu_crit", "CPU critical (%)", "number"],
  ["disk_free_warn", "Disk warning when free below (%)", "number"], ["disk_free_crit", "Disk critical when free below (%)", "number"],
  ["swap_growth_mb", "Swap growth in 30 min (MB)", "number"], ["restart_count", "Container restarts in 15 min", "number"],
  ["agent_grace_seconds", "Mac shown as Stale after no report for (s)", "number"], ["agent_notify_minutes", "Mac shown as Offline, and notified, after (min)", "number"], ["vitest_workers", "Test workers left running", "number"],
  ["docker_vm_gb", "Docker VM memory warning (GB)", "number"], ["log_size_mb", "Container log warning (MB)", "number"],
];
const TOP: [string, string][] = [
  ["dashboard_refresh_seconds", "Dashboard refresh (s): how often this page re-reads data"],
  ["refresh_interval", "Collection interval (s): server sampling, alert checks, Mac agent reports"], ["persist_seconds", "Problem must last (s) before notifying"],
  ["cpu_persist_seconds", "CPU must stay high for (s)"], ["cooldown_minutes", "Notification cooldown (min)"],
  ["retention_hours", "History kept (h, max 24)"],
];

export function SettingsPage() {
  const { data, err, reload } = usePoll<any>("/api/settings");
  const [form, setForm] = useState<any>(null);
  const [msg, setMsg] = useState("");
  useEffect(() => { if (data && !form) setForm(data.settings); }, [data, form]);
  if (!data || !form) return <><h1>Settings</h1>{err && <div className="banner">{err}</div>}</>;
  const tg = data.telegram, ag = data.agent_state, ret = data.retention, ra = data.remote_access;
  const save = async () => {
    try { await send("PUT", "/api/settings", { ...form }); setMsg("Saved."); reload(); } catch (e) { setMsg(e instanceof ApiError ? e.message : String(e)); }
  };
  const test = async () => {
    setMsg("Sending…");
    try { const r = await send<{ ok: boolean; error?: string }>("POST", "/api/telegram/test"); setMsg(r.ok ? "Test message sent. Check Telegram." : r.error ?? "Failed"); reload(); } catch (e) { setMsg(String(e)); }
  };
  const setTop = (k: string, v: number) => setForm({ ...form, [k]: v });
  const setTh = (k: string, v: number) => setForm({ ...form, thresholds: { ...form.thresholds, [k]: v } });
  return (
    <>
      <h1>Settings</h1>
      <p className="sub">One owner. Secrets are never shown here; they are set on the server with <span className="mono">watcherctl</span>.</p>
      <h2>Remote access</h2>
      <div className="card"><dl className="kv" style={{ margin: 0 }}>
        <dt>Remote access</dt><dd>{ra.method}</dd>
        <dt>Connection</dt><dd>{ra.connected ? "Connected (this page was reached through Tailscale)" : "Not through Tailscale (direct local connection)"}</dd>
        <dt>HTTPS</dt><dd>{ra.https ? "Enabled" : "Not detected on this connection"}</dd>
        <dt>Access</dt><dd>{ra.access}{ra.owner_restricted ? ", limited to one authorised Tailscale identity" : " (owner identity not set)"}</dd>
        <dt>Funnel</dt><dd>{ra.funnel ? "ACTIVE: this request came from the public internet" : "Disabled"}</dd>
      </dl></div>
      <h2>Telegram</h2>
      <div className="card"><dl className="kv" style={{ margin: 0 }}>
        <dt>Connection</dt><dd>{tg.configured ? "Connected to one authorised chat" : "Not set up. Run watcherctl setup-telegram on the server."}</dd>
        <dt>Transport</dt><dd>{tg.mode}{tg.mode === "webhook" && !tg.webhook_secret_set ? " (secret missing)" : ""}</dd>
        <dt>Last test</dt><dd>{tg.last_test ? `${tg.last_test.status} ${ago(tg.last_test.ts)}` : "none"}</dd>
      </dl><p><button className="btn primary" onClick={test} disabled={!tg.configured}>Send test notification</button> <span className="muted" role="status">{msg}</span></p></div>
      <h2>Mac agent</h2>
      <div className="card"><dl className="kv" style={{ margin: 0 }}>
        <dt>Status</dt><dd>{ag?.ts ? `${ag.clean_stop ? "Stopped on purpose" : "Reporting"} · last heard ${ago(ag.ts)}` : "Never connected"}</dd>
        <dt>Host / version</dt><dd>{ag?.host ?? "–"} · {ag?.version ?? "–"}</dd>
        <dt>Paired agents</dt><dd>{data.agents.length ? data.agents.map((a: any) => `${a.id}${a.revoked ? " (revoked)" : ""}`).join(", ") : "none. Run watcherctl pair-agent."}</dd>
      </dl></div>
      <h2>Monitoring and notifications</h2>
      <div className="form">
        {TOP.map(([k, l]) => <label key={k}>{l}<input type="number" value={form[k]} onChange={e => setTop(k, Number(e.target.value))} /></label>)}
        <label style={{ alignContent: "end" }}><span><input type="checkbox" checked={form.recovery_notifications} onChange={e => setForm({ ...form, recovery_notifications: e.target.checked })} /> Send recovery notifications</span></label>
      </div>
      <h2>Alert thresholds</h2>
      <div className="form">
        {THRESH.map(([k, l]) => <label key={k}>{l}<input type="number" step="any" value={form.thresholds[k]} onChange={e => setTh(k, Number(e.target.value))} /></label>)}
      </div>
      <p><button className="btn primary" onClick={save}>Save settings</button> <span className="muted" role="status">{msg}</span></p>
      <h2>Retention and the watcher itself</h2>
      <div className="card"><dl className="kv" style={{ margin: 0 }}>
        <dt>Retention</dt><dd>{ret.hours} h (hard limit {ret.max_hours} h)</dd>
        <dt>Oldest record</dt><dd>{ret.oldest ? fmtTime(ret.oldest) : "none yet"}</dd>
        <dt>Database size</dt><dd>{fmtBytes(ret.db_bytes)}</dd>
        <dt>Last cleanup</dt><dd>{ret.last_prune ? ago(ret.last_prune.ts) : "at startup"}</dd>
        <dt>Watcher memory / CPU</dt><dd>{data.self?.rss ? `${fmtBytes(data.self.rss)} · ${data.self.cpu}%` : "–"}</dd>
        <dt>Uptime / version</dt><dd>{fmtDur(data.uptime)} · v{data.version}</dd>
      </dl></div>
      <h2>Recent notifications</h2>
      <div className="tablewrap"><table><tbody>
        {data.notifications.length === 0 && <tr><td className="muted">None yet.</td></tr>}
        {data.notifications.map((n: any) => <tr key={n.id}><td>{fmtTime(n.ts)}</td><td>{n.kind}</td><td><span className={`tag ${n.status === "sent" ? "ok" : "crit"}`}>{n.status}</span></td><td className="muted">{n.detail}</td></tr>)}
      </tbody></table></div>
      <ActionHistory />
    </>
  );
}

function ActionHistory() {
  const { data } = usePoll<{ actions: any[] }>("/api/audit");
  return (
    <>
      <h2>Stop history</h2>
      <div className="tablewrap"><table><tbody>
        {!data?.actions.length && <tr><td className="muted">No stop actions in the last 24 hours.</td></tr>}
        {data?.actions.map(a => <tr key={a.id}><td>{fmtTime(a.ts)}</td><td>{a.action.replace("stop.", "")}</td><td>{a.machine}</td><td>{a.target}</td><td>{a.outcome}</td></tr>)}
      </tbody></table></div>
    </>
  );
}
