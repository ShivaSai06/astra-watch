let csrf = "";

async function ensureSession() {
  if (csrf) return;
  const r = await fetch("/api/session", { credentials: "same-origin" });
  if (!r.ok) throw new ApiError(r.status, await detail(r));
  csrf = (await r.json()).csrf;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function detail(r: Response): Promise<string> {
  try {
    const j = await r.json();
    if (typeof j.detail === "string") return j.detail;
    if (j.detail?.message) return j.detail.message;
    return JSON.stringify(j.detail ?? j);
  } catch { return r.statusText; }
}

export async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new ApiError(r.status, await detail(r));
  return r.json();
}

export async function send<T>(method: "POST" | "PUT", path: string, body?: unknown): Promise<T> {
  await ensureSession();
  const r = await fetch(path, {
    method, credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new ApiError(r.status, await detail(r));
  return r.json();
}

export type Health = "healthy" | "warning" | "critical" | "stale" | "offline" | "unknown";

// How often the browser re-reads cached data. The server sets it (dashboard_refresh_seconds, default 30 s).
let refreshMs = 30000;
const listeners = new Set<() => void>();
export const getRefreshMs = () => refreshMs;
export const subscribeRefresh = (f: () => void) => { listeners.add(f); return () => { listeners.delete(f); }; };
export function setRefreshSeconds(sec: number) {
  const ms = Math.max(10, sec) * 1000;
  if (ms !== refreshMs) { refreshMs = ms; listeners.forEach(f => f()); }
}
export interface AlertRef { id: string; severity: string; title: string }
export type Level = "good" | "attention" | "critical" | "offline" | "unknown";
export interface Status { level: Level; label: string; headline: string; watching: number }
export interface Resource { key: string; label: string; level: "good" | "attention" | "critical" | "unknown"; pct: number; value: string; text: string }
export interface Attention { id: string; machine: string; severity: string; headline: string; what: string; since: number }
export interface ActivityItem { id: string; machine: string; rule: string; severity: string; title: string; state: "active" | "recovered" | "ended"; first_ts: number; last_ts: number; events: number }
export interface Cause { text: string; kind: "confirmed" | "inferred"; confidence: string; evidence: string[]; note: string }
export interface Explain {
  headline: string; what: string; why: string; cause: Cause | null; steps: string[]; verify: string[];
  glossary: { term: string; meaning: string }[]; measurements: string[]; automatic_action: string; duration_seconds: number;
}
export interface IncidentEvent { ts: number; kind: string; detail: string }
export interface ActivityDetail {
  incident: { id: string; machine: string; rule: string; severity: string; state: string; title: string; first_seen: number; active_since: number;
    recovered_at: number | null; notify_state?: string; notified_at?: number | null; recovery_notified?: number; targets: { kind: string; ref: any; label: string }[] };
  explain: Explain; events: IncidentEvent[]; stages: { key: string; done: boolean; skipped: boolean }[];
}
export interface Decision { name: string; id: string; protected: boolean; allowlisted: boolean; cpu: number; mem_host_pct: number; over_threshold: boolean; would_stop: boolean; executed: boolean; reasons: string[]; policy: string | null }
export interface AuditRow { ts: number; actor: string; action: string; machine: string | null; target: string | null; outcome: string | null; detail: string | null }
export interface AutomationView {
  mode: "explain" | "assisted" | "autonomous"; requested_mode: string; mode_note: string; problem: string | null; autonomous_available: boolean;
  dry_run: boolean; allowlist: string[]; denylist: string[]; builtin_protected: string[]; defaults: Record<string, number>;
  services: Record<string, Record<string, number>>; allowed_actions: string[]; decisions: Decision[]; history: AuditRow[];
  last_action: AuditRow | null; history_days: number; editable_here: boolean;
}
export interface Summary {
  machine: "mac" | "hetzner"; health: Health; connection: "connected" | "stale" | "offline" | "unknown"; last_update: number | null;
  last_seen: number | null; age_seconds: number | null; fresh: boolean;
  cpu: number | null; mem_pct: number | null; mem_total: number | null; mem_used: number | null;
  swap_used: number | null; swap_total: number | null; disk_free: number | null; disk_total: number | null;
  containers: number | null; unhealthy_containers?: number | null; alerts: AlertRef[]; agent: { host?: string; version?: string } | null;
  status: Status; resources: Resource[];
}
export interface Retention { hours: number; max_hours: number; history_days: number; oldest: number | null; db_bytes: number }
export interface Overview {
  mac: Summary; hetzner: Summary; retention: Retention; dashboard_refresh_seconds: number;
  overall: { level: Level; label: string; text: string; machines?: number; healthy?: number; need_attention?: number; not_reporting?: number; critical_incidents?: number; active_incidents?: number; last_report?: number | null };
  attention: Attention[]; recent: ActivityItem[];
}

export interface Proc {
  pid: number; ppid: number; name: string; exe?: string; cpu: number; rss: number; label?: string; eligible?: boolean;
  growing?: boolean; why?: string; container?: string | null; container_name?: string; parent?: string;
}
export interface Container {
  id: string; short: string; name: string; image: string; status: string; running: boolean; health: string;
  restarts: number; restart_policy: string; ports: string[]; mounts: { type: string; name: string; dest: string }[];
  cpu: number; mem: number; log_size?: number | null; project?: string;
}
export interface Sample {
  cpu: number; load1: number; load5: number; load15: number; mem_total: number; mem_used: number; mem_pct: number;
  swap_total: number; swap_used: number; disk_total: number; disk_free: number; ts: number;
  extra: Record<string, any>;
}
export interface Detail extends Summary {
  sample: Sample | null;
  procs: { top_cpu: Proc[]; top_mem: Proc[]; dev?: Proc[]; groups?: Record<string, { count: number; rss: number; cpu: number; orphans: number; idle: number }>; docker_vm_rss?: number; docker_running?: boolean } | null;
  container_list: { list: Container[] } | null;
  docker_df: Record<string, any> | null;
  logs: { sizes: Record<string, number> } | null;
  storage: { dirs: { name: string; size: number }[] } | null;
}
export interface Incident {
  id: string; rule: string; machine: string; severity: string; state: string; title: string;
  measured: { summary?: Record<string, string>; facts?: string[] }; cause: string; confidence: string; impact: string;
  recommendation: string; targets: { kind: string; ref: any; label: string }[]; first_seen: number; active_since: number;
  last_seen: number; recovered_at: number | null; notify_state: string; notified_at: number | null; recovery_notified: number;
}
export interface Prepared {
  token: string; expires: number; machine: string; kind: string;
  details: { name: string; short?: string; pid?: number; exe?: string; image?: string; impact: string[]; volumes?: string[]; why?: string; ports?: string[] };
}
export interface StopResult { status: string; title: string; message: string; ts: number }
