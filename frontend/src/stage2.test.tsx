import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActivityDetailPage, ExplainBody, Lifecycle } from "./activity";
import { AutomationPage } from "./automation";
import { Explain, Summary } from "./api";
import { HomePage, MachineCard } from "./home";
import { App } from "./main";
import { fmtBytes, fmtMem } from "./ui";
import { MachineChart } from "./charts";

afterEach(() => vi.unstubAllGlobals());

function mockApi(routes: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const key = Object.keys(routes).find(k => url.startsWith(k));
    return key ? { ok: true, status: 200, json: async () => routes[key] } : { ok: false, status: 404, statusText: "nf", json: async () => ({ detail: "nf" }) };
  }));
}

const summary = (over: Partial<Summary> = {}): Summary => ({
  machine: "mac", health: "healthy", connection: "connected", last_update: Date.now() / 1000, last_seen: Date.now() / 1000 - 5, age_seconds: 5, fresh: true,
  cpu: 20, mem_pct: 50, mem_total: 8 * 1024 ** 3, mem_used: 4 * 1024 ** 3, swap_used: 0, swap_total: 2 * 1024 ** 3, disk_free: 5e10, disk_total: 2e11,
  containers: null, alerts: [], agent: null,
  status: { level: "good", label: "Good", headline: "Your Mac looks fine. Nothing needs your attention.", watching: 0 },
  resources: [{ key: "memory", label: "Memory", level: "good", pct: 50, value: "4 GB of 8 GB in use", text: "Plenty of room." }], ...over,
});

const explain = (over: Partial<Explain> = {}): Explain => ({
  headline: "Your Mac is running low on memory", what: "91% of your Mac's memory is in use.", why: "The Mac can feel sluggish.",
  cause: { text: "Largest identified memory user: Claude Code", kind: "inferred", confidence: "medium", evidence: ["Claude Code 2.4 GB"], note: "This is the largest user we measured. That does not prove it caused the problem." },
  steps: ["Open Activity Monitor.", "Click the Memory tab.", "Quit apps you do not need."], verify: ["Memory Pressure is green."],
  glossary: [{ term: "swap", meaning: "Swap is disk space used as overflow." }], measurements: ["Memory used: 91%"],
  automatic_action: "Astra Watch took no automatic action. Nothing was stopped or changed.", duration_seconds: 600, ...over,
});

describe("units", () => {
  it("shows an 8 GiB Mac as 8 GB, not 8.6 GB", () => {
    expect(fmtMem(8 * 1024 ** 3)).toBe("8 GB");
    expect(fmtMem(8_589_934_592)).toBe("8 GB");
    expect(fmtMem(7.1 * 1024 ** 3)).toBe("7.1 GB");
    expect(fmtBytes(8_589_934_592)).toBe("8.6 GB");          // storage stays decimal
  });
});

describe("health and connection are separate", () => {
  it("a connected machine with a critical problem is Critical, not Good", () => {
    render(<MachineCard s={summary({ alerts: [{ id: "i", severity: "critical", title: "x" }], status: { level: "critical", label: "Critical", headline: "Your Mac is running low on memory", watching: 0 } })} href="#/machines/mac" />);
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.queryByText("Good")).not.toBeInTheDocument();
    expect(screen.getByText(/Connected · last report/)).toBeInTheDocument();
  });
  it("a stale machine says its values are not current and is never Good", () => {
    render(<MachineCard s={summary({ connection: "stale", status: { level: "unknown", label: "Not reporting", headline: "Your Mac has missed its recent reports. Values shown are the last received, not current.", watching: 0 } })} href="#/machines/mac" />);
    expect(screen.getByText("Stale", { selector: ".pill" })).toBeInTheDocument();
    expect(screen.queryByText("Online")).not.toBeInTheDocument();
    expect(screen.getByText(/Stale · last report/)).toHaveTextContent("not current");
  });
  it("an offline machine is labelled Offline", () => {
    render(<MachineCard s={summary({ connection: "offline", status: { level: "offline", label: "Offline", headline: "Your Mac is offline.", watching: 0 } })} href="#/machines/mac" />);
    expect(screen.getByText("Offline", { selector: ".pill" })).toBeInTheDocument();
  });
});

describe("Home", () => {
  it("answers: is everything OK, what is wrong, what to do", async () => {
    mockApi({ "/api/overview": {
      mac: summary({ status: { level: "attention", label: "Needs attention", headline: "Your Mac is running low on memory", watching: 0 } }),
      hetzner: summary({ machine: "hetzner", containers: 24, unhealthy_containers: 0 }), retention: { hours: 24, max_hours: 24, history_days: 30, oldest: null, db_bytes: 1 },
      dashboard_refresh_seconds: 30, overall: { level: "attention", label: "Needs attention", text: "One thing needs your attention. Your Mac is running low on memory.", machines: 2, healthy: 1, need_attention: 1, critical_incidents: 0, not_reporting: 0, active_incidents: 1, last_report: Date.now() / 1000 - 5 },
      attention: [{ id: "inc_1", machine: "mac", severity: "warning", headline: "Your Mac is running low on memory", what: "91% in use.", since: Date.now() / 1000 - 600 }],
      recent: [{ id: "inc_0", machine: "hetzner", rule: "cpu", severity: "warning", title: "Sustained CPU overload", state: "recovered", first_ts: 1, last_ts: 2, events: 5 }],
    } });
    render(<HomePage />);
    expect(await screen.findByText("Your Mac is running low on memory", { selector: ".strip span" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Incidents" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Your Mac is running low on memory/ })).toHaveAttribute("href", "#/activity/inc_1");
    expect(screen.getByText(/Machines · 1 healthy/)).toBeInTheDocument();
    expect(screen.getByText("Last update")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recent activity" })).toBeInTheDocument();
    expect(screen.getByText("Sustained CPU overload")).toBeInTheDocument();
  });
});

describe("Incident explanation", () => {
  it("labels an inferred cause and never calls the largest user the cause", () => {
    render(<ExplainBody ex={explain()} />);
    expect(screen.getByText(/Inferred · medium confidence/)).toBeInTheDocument();
    expect(screen.queryByText("Confirmed")).not.toBeInTheDocument();
    expect(screen.getByText(/does not prove it caused/)).toBeInTheDocument();
    expect(screen.getByText(/Largest identified memory user/)).toBeInTheDocument();
  });
  it("labels a confirmed cause", () => {
    render(<ExplainBody ex={explain({ cause: { text: "Docker reports an out-of-memory kill", kind: "confirmed", confidence: "high", evidence: [], note: "" } })} />);
    expect(screen.getByText("Confirmed")).toBeInTheDocument();
  });
  it("shows numbered steps, a verify section and whether Astra Watch acted", () => {
    render(<ExplainBody ex={explain()} />);
    expect(screen.getAllByRole("listitem").some(li => li.textContent === "Click the Memory tab.")).toBe(true);
    expect(document.querySelector("ol.steps")?.children.length).toBe(3);
    expect(screen.getByText("How to check it is fixed")).toBeInTheDocument();
    expect(screen.getByText(/took no automatic action/)).toBeInTheDocument();
    expect(screen.getByText("Terms explained")).toBeInTheDocument();
  });
  it("shows the lifecycle from Detected to Recovered", () => {
    render(<Lifecycle stages={["detected", "investigated", "recommended", "action", "verified", "recovered"].map(k => ({ key: k, done: k !== "action", skipped: k === "action" }))} />);
    const items = within(screen.getByRole("list", { name: "Incident progress" })).getAllByRole("listitem").map(li => li.textContent);
    expect(items.map(t => t?.replace("✓", "").replace(" (none needed)", ""))).toEqual(["Detected", "Investigated", "Recommended", "Action taken", "Verified", "Recovered"]);
  });
  it("loads an incident page from the API", async () => {
    mockApi({ "/api/activity/inc_1": {
      incident: { id: "inc_1", machine: "mac", rule: "memory", severity: "warning", state: "active", title: "High memory", first_seen: 1, active_since: 1, recovered_at: null, targets: [] },
      explain: explain(), events: [{ ts: 1, kind: "detected", detail: "x" }, { ts: 2, kind: "notified", detail: "Telegram message sent" }],
      stages: [{ key: "detected", done: true, skipped: false }],
    } });
    render(<ActivityDetailPage id="inc_1" />);
    expect(await screen.findByRole("heading", { name: "Your Mac is running low on memory" })).toBeInTheDocument();
    expect(screen.getByText("You were notified")).toBeInTheDocument();
  });
});

describe("Automation page", () => {
  it("is read-only and makes clear nothing happens without the owner", async () => {
    mockApi({ "/api/automation": {
      mode: "assisted", requested_mode: "assisted", mode_note: "", problem: null, autonomous_available: false, dry_run: true, allowlist: [],
      denylist: ["immich*"], builtin_protected: ["immich*"], defaults: { cpu_pct: 80, mem_host_pct: 40, sustained_seconds: 600, cooldown_minutes: 60, max_attempts: 2 },
      services: {}, allowed_actions: ["stop_container"], decisions: [], history: [], last_action: null, history_days: 30, editable_here: false } });
    render(<AutomationPage />);
    expect(await screen.findByText("Nothing happens without you")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/immich\* · built in/)).toBeInTheDocument();
    expect(screen.getByText(/never stopped anything on its own/i)).toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
    expect(screen.queryAllByRole("spinbutton")).toHaveLength(0);
  });
});

describe("Navigation", () => {
  it("offers Overview, Machines, Activity, Automation and Settings", () => {
    mockApi({});
    render(<App />);
    const nav = within(screen.getByRole("navigation", { name: "Main" })).getAllByRole("link").map(a => a.textContent);
    expect(nav).toEqual(["Overview", "Machines", "Activity", "Automation", "Settings"]);
  });
});

describe("Overview plain English", () => {
  it("says 24 containers running with none unhealthy, once", async () => {
    mockApi({ "/api/overview": {
      mac: summary(), hetzner: summary({ machine: "hetzner", containers: 24, unhealthy_containers: 0 }),
      retention: { hours: 24, max_hours: 24, history_days: 30, oldest: null, db_bytes: 1 }, dashboard_refresh_seconds: 30,
      overall: { level: "good", label: "All good", text: "Everything is running normally.", machines: 2, healthy: 2, need_attention: 0, critical_incidents: 0, not_reporting: 0, active_incidents: 0, last_report: Date.now() / 1000 },
      attention: [], recent: [] } });
    render(<HomePage />);
    expect(await screen.findByText("All systems are healthy")).toBeInTheDocument();
    expect(screen.getByText("No active incidents")).toBeInTheDocument();
    expect(screen.getByText(/24 containers running. No unhealthy containers reported./)).toBeInTheDocument();
  });
  it("does not repeat the problem inside the machine card", () => {
    render(<MachineCard s={summary({ alerts: [{ id: "i", severity: "warning", title: "Low disk space" }], status: { level: "attention", label: "Needs attention", headline: "Your Mac is running low on storage", watching: 0 } })} href="#/machines/mac" />);
    expect(screen.getByText(/1 active issue/)).toBeInTheDocument();
    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(screen.queryByText("Online")).not.toBeInTheDocument();
  });
});

describe("Per-machine charts", () => {
  const pts = (cpu: number) => Array.from({ length: 12 }, (_, i) => ({ ts: Date.now() / 1000 - (12 - i) * 60, cpu, mem: cpu, swap: 0, disk_free: 5e10, disk_total: 2e11 }));
  it("each machine requests and plots only its own history", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      const m = /machine=(\w+)/.exec(url)![1];
      return { ok: true, status: 200, json: async () => ({ machine: m, range: "1h", available_seconds: 720, points: pts(m === "mac" ? 11 : 77) }) };
    }));
    render(<><MachineChart machine="mac" metric="cpu" range="1h" /><MachineChart machine="hetzner" metric="cpu" range="1h" /></>);
    const charts = await screen.findAllByRole("img");
    expect(charts[0]).toHaveAccessibleName(/MacBook Air|CPU utilization: now 11%/);
    expect(charts.map(c => c.getAttribute("aria-label"))).toEqual(["CPU utilization: now 11%", "CPU utilization: now 77%"]);
    expect(calls.some(u => u.includes("machine=mac&range=1h"))).toBe(true);
    expect(calls.some(u => u.includes("machine=hetzner&range=1h"))).toBe(true);
    expect(screen.getAllByText(/Only .* of measurements exist so far/).length).toBe(2);   // honest about missing history
  });
  it("shows an honest empty state and never invents a line", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ machine: "mac", range: "1h", available_seconds: null, points: [] }) })));
    render(<MachineChart machine="mac" metric="memory" range="1h" />);
    expect(await screen.findByText(/Not enough measurements/)).toBeInTheDocument();
    expect(document.querySelector("svg path")).toBeNull();
  });
  it("disables 7d and 30d because raw data is kept for 24 hours", async () => {
    const { RangeTabs } = await import("./charts");
    render(<RangeTabs value="1h" onChange={() => {}} hours={24} label="r" />);
    expect(screen.getByRole("button", { name: "7d" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "30d" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "24h" })).toBeEnabled();
  });
});
