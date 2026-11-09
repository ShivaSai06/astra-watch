import { useEffect, useRef, useState } from "react";
import { ApiError, Prepared, StopResult, send } from "./api";

export const fmtBytes = (b?: number | null) => {
  if (b == null) return "–";
  if (b >= 1e12) return (b / 1e12).toFixed(2) + " TB";
  if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
  if (b >= 1e6) return Math.round(b / 1e6) + " MB";
  return Math.round(b / 1e3) + " KB";
};
/** Memory/swap/process sizes in binary units shown as "GB" (like Activity Monitor): an 8 GiB Mac reads "8 GB". */
export const fmtMem = (b?: number | null) => {
  if (b == null) return "–";
  const GiB = 1024 ** 3, MiB = 1024 ** 2;
  if (b >= GiB) { const v = b / GiB; return (Math.abs(v - Math.round(v)) < 0.05 ? String(Math.round(v)) : v.toFixed(1)) + " GB"; }
  if (b >= MiB) return Math.round(b / MiB) + " MB";
  return Math.round(b / 1024) + " KB";
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
export const CONNECTION_LABEL = { connected: "Connected", stale: "Stale", offline: "Offline", unknown: "Unknown" } as const;
export const MACHINE_NAME: Record<string, string> = { mac: "MacBook Air", hetzner: "Hetzner server" };
export const MACHINE_SHORT: Record<string, string> = { mac: "Your Mac", hetzner: "Your server" };

const ICONS: Record<string, string> = {
  overview: "M3 11.5 12 4l9 7.5M5.5 10v9.5h13V10",
  machines: "M4 4h16v6H4zM4 14h16v6H4zM7.5 7h.1M7.5 17h.1",
  activity: "M3 12h4l3-7 4 14 3-7h4",
  automation: "M12 3 3 8l9 5 9-5zM3 12.5l9 5 9-5M3 17l9 5 9-5",
  settings: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19 12a7 7 0 0 0-.1-1.3l2-1.5-2-3.4-2.3.9a7 7 0 0 0-2.2-1.3L14 3h-4l-.4 2.4a7 7 0 0 0-2.2 1.3l-2.3-.9-2 3.4 2 1.5a7 7 0 0 0 0 2.6l-2 1.5 2 3.4 2.3-.9a7 7 0 0 0 2.2 1.3L10 21h4l.4-2.4a7 7 0 0 0 2.2-1.3l2.3.9 2-3.4-2-1.5c.1-.4.1-.9.1-1.3z",
  chevron: "M9 6l6 6-6 6",
  arrow: "M5 12h14M13 6l6 6-6 6",
  check: "M5 12.5l4.5 4.5L19 7.5",
  alert: "M12 4 2.5 20h19zM12 10v4.5M12 17.2v.1",
  info: "M12 11v5.5M12 7.6v.1M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z",
  box: "M12 3 4 7.5v9l8 4.5 8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9",
  clock: "M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z",
  laptop: "M5 6h14v9H5zM2.5 19h19",
  server: "M4 4h16v6H4zM4 14h16v6H4zM7.5 7h.1M7.5 17h.1M11 7h5M11 17h5",
  logo: "M12 3 3 8v8l9 5 9-5V8zM12 12 3 8M12 12l9-4M12 12v9",
};
export function Icon({ name, size = 20 }: { name: keyof typeof ICONS | string; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={ICONS[name]} /></svg>;
}

/** Health badge. Health (is it OK?) is shown separately from connection (is it reporting?). */
export function StatusBadge({ level, label }: { level: string; label: string }) {
  return <span className={`status ${level}`}>{label}</span>;
}

/** Last-report line: connection is its own fact, never folded into health. */
export function ConnectionLine({ connection, last }: { connection: keyof typeof CONNECTION_LABEL; last: number | null }) {
  return <p className={`conn ${connection}`}><span className={`dot ${connection}`} />{CONNECTION_LABEL[connection]} · last report {ago(last)}{connection !== "connected" ? " · values shown are the last received, not current" : ""}</p>;
}

export function Meter({ pct, level = "good" }: { pct: number | null; level?: string }) {
  const v = Math.max(0, Math.min(100, pct ?? 0));
  return <div className="meter" aria-hidden="true"><i className={level} style={{ width: `${v}%` }} /></div>;
}

export function Stat({ label, value, sub, note }: { label: string; value: string; sub?: string; note?: string }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{value} {sub && <small>{sub}</small>}</dd>
      {note && <p>{note}</p>}
    </div>
  );
}

/** Restrained area chart: one line, a faint fill, two guide lines. Min/max are printed as text, not drawn as axes. */
export function LineChart({ title, points, unit = "%", fmt, max }: {
  title: string; points: { t: number; v: number | null }[]; unit?: string; fmt?: (v: number) => string; max?: number;
}) {
  const vals = points.filter(p => p.v != null) as { t: number; v: number }[];
  const f = fmt ?? ((v: number) => `${Math.round(v)}${unit}`);
  if (vals.length < 2) return <div className="chart"><h4>{title}</h4><div className="empty-chart">Collecting data…</div></div>;
  const t0 = vals[0].t, t1 = vals[vals.length - 1].t;
  const hi = max ?? Math.max(...vals.map(p => p.v), 1) * 1.1;
  const lo = max ? 0 : Math.min(...vals.map(p => p.v)) * 0.9;
  const x = (t: number) => ((t - t0) / Math.max(t1 - t0, 1)) * 300;
  const y = (v: number) => 92 - ((v - lo) / Math.max(hi - lo, 1e-9)) * 86;
  const d = vals.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const last = vals[vals.length - 1].v;
  return (
    <div className="chart">
      <h4><span>{title}</span><span>{f(last)} now · range {f(Math.min(...vals.map(p => p.v)))}–{f(Math.max(...vals.map(p => p.v)))}</span></h4>
      <svg viewBox="0 0 300 96" preserveAspectRatio="none" role="img" aria-label={`${title}: now ${f(last)}`}>
        <line className="grid" x1="0" x2="300" y1="6" y2="6" /><line className="grid" x1="0" x2="300" y1="92" y2="92" />
        <path d={`${d} L300,92 L0,92 Z`} fill="var(--chart)" opacity=".09" />
        <path d={d} fill="none" stroke="var(--chart)" strokeWidth="1.8" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
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
              <dt>Machine</dt><dd>{MACHINE_NAME[prep!.machine] ?? prep!.machine}</dd>
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
