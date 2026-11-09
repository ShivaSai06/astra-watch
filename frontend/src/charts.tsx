import { useState } from "react";
import { usePoll } from "./hooks";

export type Metric = "cpu" | "memory" | "storage" | "swap";
export type Range = "1h" | "6h" | "24h" | "7d" | "30d";
export const RANGES: { key: Range; label: string; secs: number; supported: boolean }[] = [
  { key: "1h", label: "1h", secs: 3600, supported: true }, { key: "6h", label: "6h", secs: 21600, supported: true },
  { key: "24h", label: "24h", secs: 86400, supported: true }, { key: "7d", label: "7d", secs: 7 * 86400, supported: false },
  { key: "30d", label: "30d", secs: 30 * 86400, supported: false },
];
export const METRICS: { key: Metric; label: string; title: string }[] = [
  { key: "cpu", label: "CPU", title: "CPU utilization" }, { key: "memory", label: "Memory", title: "Memory utilization" },
  { key: "storage", label: "Storage", title: "Storage utilization" }, { key: "swap", label: "Swap", title: "Swap usage" },
];

interface HistoryPoint { ts: number; cpu: number | null; mem: number | null; swap: number | null; disk_free: number | null; disk_total: number | null }
export interface History { machine: string; range: string; points: HistoryPoint[]; available_seconds: number | null }

const GB = 1024 ** 3;
function value(m: Metric, p: HistoryPoint): number | null {
  if (m === "cpu") return p.cpu;
  if (m === "memory") return p.mem;
  if (m === "swap") return p.swap == null ? null : p.swap / GB;
  return p.disk_total ? 100 * (1 - (p.disk_free ?? 0) / p.disk_total) : null;
}

export function RangeTabs({ value, onChange, hours, label }: { value: Range; onChange: (r: Range) => void; hours: number; label: string }) {
  return (
    <div className="seg small" role="group" aria-label={label}>
      {RANGES.map(r => {
        const off = !r.supported || r.secs > hours * 3600;
        return <button key={r.key} aria-pressed={value === r.key} disabled={off} onClick={() => onChange(r.key)}
          title={off ? `Not available: raw measurements are kept for ${hours} hours` : undefined}>{r.label}</button>;
      })}
    </div>
  );
}

export function MetricTabs({ value, onChange, label, metrics = METRICS.slice(0, 3) }: { value: Metric; onChange: (m: Metric) => void; label: string; metrics?: typeof METRICS }) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {metrics.map(m => <button key={m.key} role="tab" aria-selected={value === m.key} onClick={() => onChange(m.key)}>{m.label}</button>)}
    </div>
  );
}

const hhmm = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

/** Smooth area chart for ONE machine and ONE metric. It plots only what was measured: gaps stay gaps. */
export function MachineChart({ machine, metric, range, height = 150 }: { machine: "mac" | "hetzner"; metric: Metric; range: Range; height?: number }) {
  const { data, err } = usePoll<History>(`/api/history?machine=${machine}&range=${range}`);
  const def = METRICS.find(m => m.key === metric)!;
  const box = { height };
  if (err && !data) return <div className="chart-empty" style={box}>Could not load history.</div>;
  if (!data) return <div className="chart-empty" style={box}>Loading…</div>;
  const pts = data.points.map(p => ({ t: p.ts, v: value(metric, p) })).filter((p): p is { t: number; v: number } => p.v != null);
  if (pts.length < 2) return <div className="chart-empty" style={box}>Collecting data. Not enough measurements in this range yet.</div>;
  const secs = RANGES.find(r => r.key === range)!.secs;
  const now = Date.now() / 1000;
  const t0 = Math.min(pts[0].t, now - secs), t1 = Math.max(pts[pts.length - 1].t, now);
  const pct = metric !== "swap";
  const hi = pct ? 100 : Math.max(1, Math.ceil(Math.max(...pts.map(p => p.v)) * 1.2 * 4) / 4);
  const W = 600, H = 100;
  const x = (t: number) => ((t - t0) / Math.max(t1 - t0, 1)) * W;
  const y = (v: number) => H - (Math.min(v, hi) / hi) * H;
  const gap = Math.max(secs / 180 * 3, 180);
  const runs: { t: number; v: number }[][] = [];
  pts.forEach((p, i) => { if (i === 0 || p.t - pts[i - 1].t > gap) runs.push([p]); else runs[runs.length - 1].push(p); });
  const line = (r: { t: number; v: number }[]) => {
    if (r.length === 1) return `M${x(r[0].t) - 1},${y(r[0].v)} L${x(r[0].t) + 1},${y(r[0].v)}`;
    let d = `M${x(r[0].t).toFixed(1)},${y(r[0].v).toFixed(1)}`;
    for (let i = 0; i < r.length - 1; i++) {                       // Catmull-Rom → cubic Bézier, clamped so it never overshoots 0..hi
      const p0 = r[i - 1] ?? r[i], p1 = r[i], p2 = r[i + 1], p3 = r[i + 2] ?? r[i + 1];
      const c = (a: number) => Math.max(0, Math.min(hi, a));
      d += ` C${(x(p1.t) + (x(p2.t) - x(p0.t)) / 6).toFixed(1)},${y(c(p1.v + (p2.v - p0.v) / 6)).toFixed(1)} ${(x(p2.t) - (x(p3.t) - x(p1.t)) / 6).toFixed(1)},${y(c(p2.v - (p3.v - p1.v) / 6)).toFixed(1)} ${x(p2.t).toFixed(1)},${y(p2.v).toFixed(1)}`;
    }
    return d;
  };
  const ticks = [0, 1, 2, 3, 4].map(i => t0 + ((t1 - t0) * i) / 4);
  const yl = pct ? [100, 75, 50, 25, 0].map(v => `${v}%`) : [hi, hi * .75, hi * .5, hi * .25, 0].map(v => `${+v.toFixed(2)} GB`);
  const last = pts[pts.length - 1].v;
  const fmt = (v: number) => (pct ? `${Math.round(v)}%` : `${v.toFixed(2)} GB`);
  const have = data.available_seconds ?? 0;
  return (
    <figure className="chart" aria-label={`${def.title}, ${machine === "mac" ? "MacBook Air" : "Hetzner server"}: now ${fmt(last)}`}>
      <div className="chart-body" style={box}>
        <ol className="yaxis" aria-hidden="true">{yl.map(l => <li key={l}>{l}</li>)}</ol>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={`${def.title}: now ${fmt(last)}`}>
          {[0, 25, 50, 75, 100].map(g => <line key={g} className="grid" x1="0" x2={W} y1={g} y2={g} />)}
          {runs.map((r, i) => (
            <g key={i}>
              {r.length > 1 && <path d={`${line(r)} L${x(r[r.length - 1].t)},${H} L${x(r[0].t)},${H} Z`} fill="var(--chart)" opacity=".16" />}
              <path d={line(r)} fill="none" stroke="var(--chart)" strokeWidth="2" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
            </g>
          ))}
        </svg>
      </div>
      <div className="xaxis" aria-hidden="true">{ticks.map((t, i) => <span key={i}>{hhmm(t)}</span>)}</div>
      {have < secs * 0.8 && <figcaption>Only {have >= 5400 ? `${(have / 3600).toFixed(1)} h` : `${Math.max(1, Math.round(have / 60))} min`} of measurements exist so far. Nothing is filled in.</figcaption>}
    </figure>
  );
}

/** Tabs + range + chart for one machine, used on the Overview. */
export function MachineMonitor({ machine, hours }: { machine: "mac" | "hetzner"; hours: number }) {
  const [metric, setMetric] = useState<Metric>("cpu");
  const [range, setRange] = useState<Range>("1h");
  const t = METRICS.find(m => m.key === metric)!;
  return (
    <div className="monitor">
      <div className="monitor-bar">
        <MetricTabs value={metric} onChange={setMetric} label={`${machine === "mac" ? "MacBook Air" : "Hetzner server"} metric`} />
        <RangeTabs value={range} onChange={setRange} hours={hours} label={`${machine === "mac" ? "MacBook Air" : "Hetzner server"} time range`} />
      </div>
      <h4 className="chart-title">{t.title}</h4>
      <MachineChart machine={machine} metric={metric} range={range} />
    </div>
  );
}
