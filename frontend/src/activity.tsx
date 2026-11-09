import { Fragment, useState } from "react";
import { ActivityDetail, ActivityItem, AuditRow, AutomationView, Explain } from "./api";
import { usePoll } from "./hooks";
import { Icon, MACHINE_NAME, StatusBadge, StopDialog, ago, fmtDur, fmtTime } from "./ui";

const STAGE_LABEL: Record<string, string> = {
  detected: "Detected", investigated: "Investigated", recommended: "Recommended", action: "Action taken", verified: "Verified", recovered: "Recovered",
};
const EVENT_LABEL: Record<string, string> = {
  detected: "Detected", investigated: "Investigated", recommended: "Recommended", notified: "You were notified", escalated: "Got worse",
  action: "Action taken (approved by you)", action_verified: "Action verified", action_failed: "Action did not work", verified: "Verified", recovered: "Recovered",
};

export function CauseBadge({ kind, confidence }: { kind: "confirmed" | "inferred"; confidence?: string }) {
  return kind === "confirmed"
    ? <span className="tag ok">Confirmed</span>
    : <span className="tag warn">Inferred{confidence ? ` · ${confidence} confidence` : ""}</span>;
}

export function ExplainBody({ ex }: { ex: Explain }) {
  return (
    <div className="explain">
      <h2>What happened</h2>
      <p>{ex.what}</p>
      {ex.measurements.length > 0 && <p className="muted">What was measured: {ex.measurements.join(" · ")} · lasting {fmtDur(ex.duration_seconds)} so far</p>}

      {ex.why && <><h2>Why it matters</h2><p>{ex.why}</p></>}

      {ex.cause && (
        <>
          <h2>Likely cause <CauseBadge kind={ex.cause.kind} confidence={ex.cause.confidence} /></h2>
          <p>{ex.cause.text}</p>
          {ex.cause.note && <p className="note">{ex.cause.note}</p>}
          {ex.cause.evidence.length > 0 && (
            <details className="fold small"><summary><span>Evidence</span></summary>
              <ul className="facts">{ex.cause.evidence.map((e, i) => <li key={i}>{e}</li>)}</ul></details>
          )}
        </>
      )}

      {ex.steps.length > 0 && <><h2>What you can do</h2><ol className="steps">{ex.steps.map((s, i) => <li key={i}>{s}</li>)}</ol></>}
      {ex.verify.length > 0 && <><h2>How to check it is fixed</h2><ul>{ex.verify.map((s, i) => <li key={i}>{s}</li>)}</ul></>}

      <h2>Did Astra Watch do anything?</h2>
      <p>{ex.automatic_action}</p>

      {ex.glossary.length > 0 && (
        <details className="fold small"><summary><span>Terms explained</span></summary>
          <dl className="kv">{ex.glossary.map(g => <Fragment key={g.term}><dt>{g.term}</dt><dd>{g.meaning}</dd></Fragment>)}</dl></details>
      )}
    </div>
  );
}

export function Lifecycle({ stages }: { stages: ActivityDetail["stages"] }) {
  return (
    <ol className="stages" aria-label="Incident progress">
      {stages.map(s => (
        <li key={s.key} className={s.done ? "done" : s.skipped ? "skipped" : ""}>
          <span className="pip" aria-hidden="true">{s.done ? "✓" : ""}</span>
          <span>{STAGE_LABEL[s.key]}{s.skipped ? <small> (none needed)</small> : null}</span>
        </li>
      ))}
    </ol>
  );
}

function stateText(i: { state: string }, recoveredAt?: number | null) {
  return i.state === "active" ? "Ongoing" : i.state === "recovered" ? `Back to normal${recoveredAt ? ` ${ago(recoveredAt)}` : ""}` : "Ended";
}

export function ActivityDetailPage({ id }: { id: string }) {
  const { data, err, reload } = usePoll<ActivityDetail>(`/api/activity/${id}`);
  const [stop, setStop] = useState<any>(null);
  const back = <a className="back" href="#/activity">← Activity</a>;
  if (err) return <>{back}<h1>Incident</h1><div className="banner">{err}</div></>;
  if (!data) return <>{back}<p className="muted">Loading…</p></>;
  const { incident: i, explain: ex } = data;
  const level = i.state === "active" ? (i.severity === "critical" ? "critical" : "attention") : "good";
  return (
    <>
      {back}
      <div className="page-head"><h1>{ex.headline}</h1><StatusBadge level={level} label={stateText(i, i.recovered_at)} /></div>
      <p className="sub">{MACHINE_NAME[i.machine]} · {i.severity} · started {fmtTime(i.active_since ?? i.first_seen)}</p>
      <Lifecycle stages={data.stages} />
      <ExplainBody ex={ex} />
      {i.state === "active" && i.targets.length > 0 && (
        <>
          <h2>Stop something (asks you to confirm first)</h2>
          <p className="chips">{i.targets.map((t, k) => (
            <button key={k} className="btn small danger"
              onClick={() => setStop({ machine: i.machine, kind: t.kind, ...(t.kind === "container" ? { id: t.ref.id } : { pid: t.ref.pid, start: t.ref.start }) })}>
              Stop {t.label}…</button>))}</p>
        </>
      )}
      <h2>Timeline</h2>
      <ul className="timeline">{data.events.map((e, k) => (
        <li key={k}><time>{fmtTime(e.ts)}</time><span><b>{EVENT_LABEL[e.kind] ?? e.kind}</b>{e.detail && <span className="muted"> {e.detail}</span>}</span></li>))}</ul>
      <StopDialog request={stop} onClose={() => { setStop(null); reload(); }} />
    </>
  );
}

function Row({ i }: { i: ActivityItem }) {
  const live = i.state === "active";
  return (
    <a className={`issue ${live ? i.severity : "recovered"}`} href={`#/activity/${i.id}`}>
      <span className={`dot ${live ? i.severity : "good"}`} />
      <span className="issue-title">{i.title}</span>
      <span className="issue-meta">{MACHINE_NAME[i.machine]} · {live ? `ongoing since ${ago(i.first_ts)}` : i.state === "recovered" ? `back to normal ${ago(i.last_ts)}` : `ended ${ago(i.last_ts)}`} · {fmtTime(i.first_ts)}</span>
      <span className="chev"><Icon name="chevron" size={18} /></span>
    </a>
  );
}

function ActionRows({ rows, empty }: { rows: AuditRow[]; empty: string }) {
  if (rows.length === 0) return <div className="panel empty">{empty}</div>;
  return (
    <div className="panel issues">
      {rows.map((h, k) => (
        <div key={k} className="issue" style={{ cursor: "default" }}>
          <span className={`dot ${h.outcome === "stopped" ? "good" : "attention"}`} />
          <span className="issue-title">{h.outcome === "stopped" ? "Stopped" : "Stop attempted:"} {h.target}</span>
          <span className="issue-meta">{h.machine ? MACHINE_NAME[h.machine] : ""} · {fmtTime(h.ts)}{h.outcome && h.outcome !== "stopped" ? ` · ${h.outcome}` : ""}</span>
        </div>
      ))}
    </div>
  );
}

export function ActivityPage({ selected }: { selected?: string }) {
  const { data, err } = usePoll<{ items: ActivityItem[]; history_days: number }>("/api/activity");
  const { data: auto } = usePoll<AutomationView>("/api/automation");
  if (selected) return <ActivityDetailPage id={selected} />;
  const active = data?.items.filter(i => i.state === "active") ?? [];
  const past = data?.items.filter(i => i.state !== "active") ?? [];
  const executed = (auto?.history ?? []).filter(h => h.action === "stop.execute");
  const byAstra = executed.filter(h => h.actor === "automation");
  const byMe = executed.filter(h => h.actor !== "automation");
  return (
    <>
      <h1>Activity</h1>
      <p className="sub">Every problem Astra Watch has found, from first sign to recovery. Nothing here is ever stopped automatically.</p>
      {err && <div className="banner">{err}</div>}
      <h2>Active problems</h2>
      {data && active.length === 0 ? <div className="panel empty">No active problems. Everything is running normally.</div> : <div className="panel issues">{active.map(i => <Row key={i.id} i={i} />)}</div>}
      <h2>Recovered{data ? ` (last ${data.history_days} days)` : ""}</h2>
      {data && past.length === 0 ? <div className="panel empty">Nothing has gone wrong in the last {data.history_days} days.</div> : <div className="panel issues">{past.map(i => <Row key={i.id} i={i} />)}</div>}
      <h2>Actions you took</h2>
      <ActionRows rows={byMe} empty="You have not stopped anything from Astra Watch." />
      <h2>Actions Astra Watch took</h2>
      <ActionRows rows={byAstra} empty="None. Astra Watch has never stopped or changed anything on its own." />
    </>
  );
}
