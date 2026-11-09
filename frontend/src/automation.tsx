import { AutomationView } from "./api";
import { usePoll } from "./hooks";
import { Section } from "./machine";
import { fmtTime } from "./ui";

const MODES: [string, string, string][] = [
  ["explain", "Explain", "Investigates and recommends only. Stop buttons are switched off."],
  ["assisted", "Assisted", "Recommends, and waits for you to confirm every stop."],
  ["autonomous", "Autonomous", "Would act on its own for allow-listed services. Not available in this version."],
];

export function AutomationPage() {
  const { data: a, err } = usePoll<AutomationView>("/api/automation");
  return (
    <>
      <h1>Automation</h1>
      <p className="sub">What Astra Watch is allowed to do, and what it is not.</p>
      {err && <div className="banner">{err}</div>}
      {a && (
        <>
          <section className="guard" style={{ marginTop: 24 }} aria-live="polite">
            <b>Nothing happens without you</b>
            <span>Astra Watch will not stop, restart or delete anything on its own. Every stop needs your explicit confirmation.</span>
          </section>
          {a.problem && <div className="banner" style={{ marginTop: 16 }}>{a.problem}. The safe default is being used.</div>}

          <section className="panel can" style={{ marginTop: 16 }}>
            <div><h3>It can</h3><ul><li>Watch memory, processor, storage and containers</li><li>Explain what is wrong and what to do</li><li>Notify you on Telegram</li></ul></div>
            <div><h3>It needs your confirmation to</h3><ul><li>Stop a container</li><li>Stop a process on your Mac</li></ul></div>
            <div><h3>It cannot</h3><ul><li>Restart or delete anything</li><li>Remove volumes, images or data</li><li>Act on its own. That is switched off for good</li></ul></div>
          </section>

          <h2>Protected services</h2>
          <section className="panel pad">
            <p className="muted" style={{ marginBottom: 10 }}>These are never stopped automatically. <span className="mono">immich*</span> is protected permanently.</p>
            <p className="chips">{a.denylist.map(n => <span key={n} className="tag">{n}{a.builtin_protected.includes(n) ? " · built in" : ""}</span>)}</p>
          </section>

          <h2>Has anything been done automatically?</h2>
          <section className="panel pad">
            <p><b>No.</b> Astra Watch has never stopped anything on its own.</p>
            {a.last_action && <p className="muted" style={{ marginTop: 6 }}>Last stop you approved: {a.last_action.target} on {fmtTime(a.last_action.ts)}.</p>}
          </section>

          <h2>Current mode</h2>
          <section className="panel">
            {MODES.map(([k, l, d]) => (
              <div key={k} className="setting-row" aria-current={a.mode === k ? "true" : undefined}>
                <div><div className="t">{l} {a.mode === k && <span className="tag ok">Current</span>}{k === "autonomous" && <span className="tag">Unavailable</span>}</div><div className="d">{d}</div></div>
              </div>))}
          </section>
          {a.mode_note && <p className="foot">{a.mode_note}</p>}
          <p className="foot">This page is read-only. The mode and policy are changed on the server with <span className="mono">watcherctl automation-mode</span> and the other <span className="mono">automation-*</span> commands.</p>

          <h2>Technical details</h2>
          <section className="panel">
            <Section title="Allow list and resource limits" hint="policy values used by the practice run">
              <h3>Allow list (practice runs only)</h3>
              {a.allowlist.length ? <p className="chips">{a.allowlist.map(n => <span key={n} className="tag ok">{n}</span>)}</p>
                : <p className="muted">Empty. No service is eligible, so no practice run happens.</p>}
              <h3>Resource limits</h3>
              <div className="tablewrap"><table>
                <thead><tr><th>Service</th><th className="num">CPU</th><th className="num">Memory of host</th><th className="num">Must last</th><th className="num">Cooldown</th><th className="num">Max tries</th></tr></thead>
                <tbody>
                  {[["Default", a.defaults] as const, ...Object.entries(a.services)].map(([n, l]) => (
                    <tr key={n}><td>{n}</td><td className="num">{l.cpu_pct}%</td><td className="num">{l.mem_host_pct}%</td><td className="num">{l.sustained_seconds}s</td><td className="num">{l.cooldown_minutes} min</td><td className="num">{l.max_attempts}</td></tr>))}
                </tbody>
              </table></div>
            </Section>
            <Section title="Practice run (dry run)" hint="what it would decide right now. Nothing is executed">
              <div className="tablewrap"><table>
                <thead><tr><th>Container</th><th className="num">CPU</th><th className="num">Memory of host</th><th>Decision</th></tr></thead>
                <tbody>
                  {a.decisions.length === 0 && <tr><td colSpan={4} className="muted">No recent container data.</td></tr>}
                  {a.decisions.slice(0, 12).map(d => (
                    <tr key={d.id}><td>{d.name}</td><td className="num">{d.cpu}%</td><td className="num">{d.mem_host_pct}%</td>
                      <td>{d.would_stop ? <span className="tag warn">Would stop (practice)</span> : d.protected ? <span className="tag">Protected</span> : <span className="tag ok">Leave alone</span>}
                        <div className="muted">{d.reasons[d.reasons.length - 1]}</div></td></tr>))}
                </tbody>
              </table></div>
            </Section>
            <Section title={`Audit history (last ${a.history_days} days)`} hint="stop requests and practice runs">
              <div className="tablewrap"><table><tbody>
                {a.history.length === 0 && <tr><td className="muted">No stop requests or practice runs yet.</td></tr>}
                {a.history.map((h, i) => <tr key={i}><td>{fmtTime(h.ts)}</td><td>{h.action.replace("stop.", "stop ").replace("dry_run.", "practice: ")}</td><td>{h.machine}</td><td>{h.target}</td><td>{h.outcome}</td><td className="muted">{h.actor}</td></tr>)}
              </tbody></table></div>
            </Section>
          </section>
        </>
      )}
    </>
  );
}
