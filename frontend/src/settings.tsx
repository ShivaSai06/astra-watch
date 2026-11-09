import { useEffect, useState } from "react";
import { ApiError, send } from "./api";
import { usePoll } from "./hooks";
import { Section } from "./machine";
import { ago, fmtBytes, fmtDur, fmtTime } from "./ui";

const THRESH: [string, string][] = [
  ["mem_warn", "Memory warning (%)"], ["mem_crit", "Memory critical (%)"],
  ["cpu_warn", "CPU warning (%)"], ["cpu_crit", "CPU critical (%)"],
  ["disk_free_warn", "Storage warning when free below (%)"], ["disk_free_crit", "Storage critical when free below (%)"],
  ["swap_growth_mb", "Swap growth in 30 min (MB)"], ["restart_count", "Container restarts in 15 min"],
  ["agent_grace_seconds", "Mac shown as Stale after no report for (s)"], ["agent_notify_minutes", "Mac shown as Offline, and notified, after (min)"],
  ["vitest_workers", "Test workers left running"], ["docker_vm_gb", "Docker VM memory warning (GB)"], ["log_size_mb", "Container log warning (MB)"],
];
const TOP: [string, string][] = [
  ["refresh_interval", "Collection interval (s): server sampling, alert checks, Mac agent reports"], ["persist_seconds", "Problem must last (s) before notifying"],
  ["cpu_persist_seconds", "CPU must stay high for (s)"], ["cooldown_minutes", "Notification cooldown (min)"],
  ["retention_hours", "Metric history kept (h, max 24)"],
];

function Row({ title, desc, state, good }: { title: string; desc: string; state: string; good?: boolean | null }) {
  return (
    <div className="setting-row">
      <div><div className="t">{title}</div><div className="d">{desc}</div></div>
      <span className={`status ${good == null ? "" : good ? "good" : "attention"}`}>{state}</span>
    </div>
  );
}

export function SettingsPage() {
  const { data, err, reload } = usePoll<any>("/api/settings");
  const [form, setForm] = useState<any>(null);
  const [msg, setMsg] = useState("");
  useEffect(() => { if (data && !form) setForm(data.settings); }, [data, form]);
  if (!data || !form) return <><h1>Settings</h1>{err && <div className="banner">{err}</div>}</>;
  const tg = data.telegram, ag = data.agent_state, ret = data.retention, ra = data.remote_access, wd = data.watchdog;
  const save = async () => {
    try { await send("PUT", "/api/settings", { ...form }); setMsg("Saved."); reload(); } catch (e) { setMsg(e instanceof ApiError ? e.message : String(e)); }
  };
  const test = async () => {
    setMsg("Sending…");
    try { const r = await send<{ ok: boolean; error?: string }>("POST", "/api/telegram/test"); setMsg(r.ok ? "Test message sent. Check Telegram." : r.error ?? "Failed"); reload(); } catch (e) { setMsg(String(e)); }
  };
  const setTop = (k: string, v: number) => setForm({ ...form, [k]: v });
  const setTh = (k: string, v: number) => setForm({ ...form, thresholds: { ...form.thresholds, [k]: v } });
  const macOk = !!ag?.ts && !ag.clean_stop;
  return (
    <>
      <h1>Settings</h1>
      <p className="sub">The basics are here. Secrets are never shown; they are set on the server with <span className="mono">watcherctl</span>.</p>

      <h2>Connections</h2>
      <section className="panel">
        <Row title="Private access" desc={ra.connected ? "This page was reached through Tailscale over HTTPS. It is not exposed to the public internet." : "Not through Tailscale (direct local connection)."}
          state={ra.funnel ? "Public (Funnel on)" : ra.connected ? "Private" : "Local"} good={!ra.funnel} />
        <Row title="Telegram alerts" desc={tg.configured ? `Connected to one authorised chat. Last test: ${tg.last_test ? `${tg.last_test.status} ${ago(tg.last_test.ts)}` : "none"}.` : "Not set up. Run watcherctl setup-telegram on the server."}
          state={tg.configured ? "Connected" : "Not set up"} good={tg.configured} />
        <Row title="Mac agent" desc={ag?.ts ? `${ag.host ?? "Mac"} · version ${ag.version ?? "?"} · last heard ${ago(ag.ts)}` : "Never connected. Run watcherctl pair-agent."}
          state={ag?.ts ? (ag.clean_stop ? "Stopped on purpose" : "Reporting") : "Never connected"} good={ag?.ts ? macOk || !!ag.clean_stop : false} />
        <Row title="Outside watchdog" desc="Astra Watch cannot report its own failure, so an outside service can alert you when its regular pings stop."
          state={wd?.configured ? (wd.last_ok ? `Active · ${ago(wd.last_ok)}` : "Waiting for first ping") : "Not set up"} good={wd?.configured ? !!wd.last_ok : null} />
      </section>

      <h2>Notifications</h2>
      <section className="panel">
        <div className="setting-row">
          <label><input type="checkbox" checked={form.recovery_notifications} onChange={e => setForm({ ...form, recovery_notifications: e.target.checked })} /><b>Tell me when a problem is fixed</b><div className="d" style={{ marginLeft: 26 }}>Sends a Telegram message when something returns to normal.</div></label>
        </div>
        <div className="setting-row">
          <label style={{ flex: 1, minWidth: 220 }}>
            <span className="t">How often this page refreshes (seconds)</span>
            <input type="number" value={form.dashboard_refresh_seconds} onChange={e => setTop("dashboard_refresh_seconds", Number(e.target.value))} style={{ maxWidth: 160, marginTop: 6 }} />
          </label>
        </div>
        <div className="setting-row">
          <div><div className="t">Send a test notification</div><div className="d">Checks that Telegram messages reach you.</div></div>
          <button className="btn" onClick={test} disabled={!tg.configured}>Send test</button>
        </div>
        <div className="setting-row">
          <span className="muted" role="status">{msg}</span>
          <button className="btn primary" onClick={save}>Save settings</button>
        </div>
      </section>

      <h2>Advanced</h2>
      <section className="panel">
        <Section title="Alert thresholds" hint="when a measurement counts as a problem">
          <div className="form">
            {THRESH.map(([k, l]) => <label key={k}>{l}<input type="number" step="any" value={form.thresholds[k]} onChange={e => setTh(k, Number(e.target.value))} /></label>)}
          </div>
        </Section>
        <Section title="Timing and retention" hint="collection interval, cooldowns, how long data is kept">
          <div className="form">
            {TOP.map(([k, l]) => <label key={k}>{l}<input type="number" value={form[k]} onChange={e => setTop(k, Number(e.target.value))} /></label>)}
          </div>
          <dl className="kv" style={{ marginTop: 20 }}>
            <dt>Measurements kept</dt><dd>{ret.hours} h (hard limit {ret.max_hours} h)</dd>
            <dt>Incident history and audit</dt><dd>{ret.history_days} days</dd>
            <dt>Oldest measurement</dt><dd>{ret.oldest ? fmtTime(ret.oldest) : "none yet"}</dd>
            <dt>Database size</dt><dd>{fmtBytes(ret.db_bytes)}</dd>
            <dt>Last cleanup</dt><dd>{ret.last_prune ? ago(ret.last_prune.ts) : "at startup"}</dd>
          </dl>
          <p style={{ marginTop: 16 }}><button className="btn primary" onClick={save}>Save settings</button> <span className="muted" role="status">{msg}</span></p>
        </Section>
        <Section title="Access and agents" hint="Tailscale identity, paired agents, transport">
          <dl className="kv">
            <dt>HTTPS</dt><dd>{ra.https ? "Enabled" : "Not detected on this connection"}</dd>
            <dt>Access</dt><dd>{ra.access}{ra.owner_restricted ? ", limited to one authorised Tailscale identity" : " (owner identity not set)"}</dd>
            <dt>Funnel</dt><dd>{ra.funnel ? "ACTIVE: this request came from the public internet" : "Disabled"}</dd>
            <dt>Telegram transport</dt><dd>{tg.mode}{tg.mode === "webhook" && !tg.webhook_secret_set ? " (secret missing)" : ""}</dd>
            <dt>Paired agents</dt><dd>{data.agents.length ? data.agents.map((a: any) => `${a.id}${a.revoked ? " (revoked)" : ""}`).join(", ") : "none. Run watcherctl pair-agent."}</dd>
          </dl>
        </Section>
        <Section title="Astra Watch itself" hint="version, memory, recent notifications">
          <dl className="kv">
            <dt>Version</dt><dd>v{data.version} · up {fmtDur(data.uptime)}</dd>
            <dt>Its own memory / CPU</dt><dd>{data.self?.rss ? `${fmtBytes(data.self.rss)} · ${data.self.cpu}%` : "–"}</dd>
          </dl>
          <h3>Recent notifications</h3>
          <div className="tablewrap"><table><tbody>
            {data.notifications.length === 0 && <tr><td className="muted">None yet.</td></tr>}
            {data.notifications.map((n: any) => <tr key={n.id}><td>{fmtTime(n.ts)}</td><td>{n.kind}</td><td><span className={`tag ${n.status === "sent" ? "ok" : "crit"}`}>{n.status}</span></td><td className="muted">{n.detail}</td></tr>)}
          </tbody></table></div>
        </Section>
      </section>
    </>
  );
}
