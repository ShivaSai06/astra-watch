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

export type Health = "healthy" | "warning" | "critical" | "disconnected" | "unknown";
export interface AlertRef { id: string; severity: string; title: string }
export interface Summary {
  machine: "mac" | "hetzner"; health: Health; connection: string; last_update: number | null;
  cpu: number | null; mem_pct: number | null; mem_total: number | null; mem_used: number | null;
  swap_used: number | null; swap_total: number | null; disk_free: number | null; disk_total: number | null;
  containers: number | null; alerts: AlertRef[]; agent: { host?: string; version?: string } | null;
}
export interface Retention { hours: number; max_hours: number; oldest: number | null; db_bytes: number }
export interface Overview { mac: Summary; hetzner: Summary; retention: Retention }

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
