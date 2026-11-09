import { useEffect, useRef, useState } from "react";
import { ApiError, Prepared, StopResult, send } from "./api";

export const fmtBytes = (b?: number | null) => {
  if (b == null) return "–";
  if (b >= 1e12) return (b / 1e12).toFixed(2) + " TB";
  if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
  if (b >= 1e6) return Math.round(b / 1e6) + " MB";
  return Math.round(b / 1e3) + " KB";
};
export const fmtPct = (v?: number | null) => (v == null ? "–" : `${Math.round(v)}%`);
export const fmtTime = (ts?: number | null) => (ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "medium" }) : "never");
export function ago(ts?: number | null) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
export const fmtDur = (s: number) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
export const STATE_LABEL: Record<string, string> = {
  healthy: "Healthy", warning: "Warning", critical: "Critical", stale: "Stale", offline: "Offline", unknown: "Unknown",
};

export function Meter({ pct, warn = 85, crit = 95, invert = false }: { pct: number | null; warn?: number; crit?: number; invert?: boolean }) {
  const v = Math.max(0, Math.min(100, pct ?? 0));
  const level = invert ? (v >= 100 - 10 ? "critical" : v >= 100 - 20 ? "warning" : "") : v >= crit ? "critical" : v >= warn ? "warning" : "";
  return <div className="meter" aria-hidden="true"><i className={level} style={{ width: `${v}%` }} /></div>;
}

export function Stat({ label, value, sub, meter }: { label: string; value: string; sub?: string; meter?: React.ReactNode }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{value} {sub && <small>{sub}</small>}</dd>
      {meter}
    </div>
  );
}

/** Minimal SVG line chart: no axes clutter, min/max printed as text. */
export function LineChart({ title, points, unit = "%", fmt, max }: {
  title: string; points: { t: number; v: number | null }[]; unit?: string; fmt?: (v: number) => string; max?: number;
}) {
  const vals = points.filter(p => p.v != null) as { t: number; v: number }[];
  const f = fmt ?? ((v: number) => `${Math.round(v)}${unit}`);
  if (vals.length < 2) return <div className="chart"><h4>{title}</h4><div className="muted" style={{ height: 90, display: "grid", placeItems: "center" }}>Collecting data…</div></div>;
  const t0 = vals[0].t, t1 = vals[vals.length - 1].t;
  const hi = max ?? Math.max(...vals.map(p => p.v), 1) * 1.1;
  const lo = max ? 0 : Math.min(...vals.map(p => p.v)) * 0.9;
  const x = (t: number) => ((t - t0) / Math.max(t1 - t0, 1)) * 300;
  const y = (v: number) => 80 - ((v - lo) / Math.max(hi - lo, 1e-9)) * 76;
  const d = vals.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const last = vals[vals.length - 1].v;
  return (
    <div className="chart">
      <h4><span>{title}</span><span>{f(last)} now · {f(Math.min(...vals.map(p => p.v)))}–{f(Math.max(...vals.map(p => p.v)))}</span></h4>
      <svg viewBox="0 0 300 84" preserveAspectRatio="none" role="img" aria-label={`${title}: now ${f(last)}`}>
        <path d={d} fill="none" stroke="var(--accent)" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  );
}

/** Two-step stop dialog (prepare → show exact target + impact → explicit confirm → verified result). */
export function StopDialog({ request, onClose }: {
  request: { machine: string; kind: string; id?: string; pid?: number; start?: number } | null; onClose: () => void;
}) {
  const [prep, setPrep] = useState<Prepared | null>(null);
  const [err, setErr] = useState("");
  const [res, setRes] = useState<StopResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [left, setLeft] = useState(0);
  const first = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    setPrep(null); setErr(""); setRes(null);
    if (!request) return;
    send<Prepared>("POST", "/api/stop/prepare", request).then(setPrep).catch(e => setErr(e instanceof ApiError ? e.message : String(e)));
  }, [request]);
  useEffect(() => {
    if (!prep || res) return;
    const i = setInterval(() => setLeft(Math.max(0, Math.round(prep.expires - Date.now() / 1000))), 500);
    return () => clearInterval(i);
  }, [prep, res]);
  useEffect(() => { first.current?.focus(); }, [prep, err, res]);
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h); return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  if (!request) return null;

  const confirm = async () => {
    if (!prep) return;
    setBusy(true);
    try { setRes(await send<StopResult>("POST", "/api/stop/confirm", { token: prep.token })); }
    catch (e) { setErr(e instanceof ApiError ? e.message : String(e)); }
    setBusy(false);
  };
  const cancel = () => { if (prep && !res) send("POST", "/api/stop/cancel", { token: prep.token }).catch(() => {}); onClose(); };
  const d = prep?.details;
  return (
    <div className="overlay" role="dialog" aria-modal="true" aria-labelledby="stop-title">
      <div className="modal">
        <h3 id="stop-title">{res ? res.title : "Confirm stop"}</h3>
        {err && <div className="result bad">{err}</div>}
        {!prep && !err && <p className="muted">Checking the target…</p>}
        {d && !res && (
          <>
            <dl className="kv">
              <dt>Machine</dt><dd>{prep!.machine === "mac" ? "My Mac" : "Hetzner Server"}</dd>
              <dt>{prep!.kind === "process" ? "Process" : "Container"}</dt><dd><b>{d.name}</b></dd>
              {d.pid != null ? <><dt>PID</dt><dd className="mono">{d.pid}</dd><dt>Executable</dt><dd className="mono">{d.exe}</dd></>
                : <><dt>Container ID</dt><dd className="mono">{d.short}</dd><dt>Image</dt><dd>{d.image}</dd></>}
              {d.why && <><dt>Flagged because</dt><dd>{d.why}</dd></>}
            </dl>
            <h2 style={{ marginTop: 16 }}>What will happen</h2>
            <ul>{d.impact.map(i => <li key={i}>{i}</li>)}</ul>
            <p className="muted">A graceful stop only. Nothing is deleted or force-killed, and it is not restarted for you. Valid for {left}s.</p>
          </>
        )}
        {res && <div className={`result ${res.status === "stopped" ? "ok" : "bad"}`}>{res.message}<div className="muted">{fmtTime(res.ts)}</div></div>}
        <div className="actions">
          {res || err ? <button ref={first} className="btn" onClick={onClose}>Close</button> : (
            <>
              <button ref={first} className="btn" onClick={cancel}>Cancel</button>
              <button className="btn danger" disabled={!prep || busy || left === 0} onClick={confirm}>{busy ? "Stopping…" : "Confirm stop"}</button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
