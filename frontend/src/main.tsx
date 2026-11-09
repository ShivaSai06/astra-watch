import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Overview, get, setRefreshSeconds } from "./api";
import { ActivityPage } from "./activity";
import { AutomationPage } from "./automation";
import { HomePage } from "./home";
import { useRefreshMs } from "./hooks";
import { Icon, ago } from "./ui";
import { MachinesPage } from "./machine";
import { SettingsPage } from "./settings";
import "./styles.css";
import { stripTrackingParams } from "./url";

stripTrackingParams();

function useRoute() {
  const [h, setH] = useState(location.hash || "#/");
  useEffect(() => { const f = () => setH(location.hash || "#/"); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return h.replace(/^#/, "").split("/").filter(Boolean);
}

export function App() {
  const [seg0, seg1] = useRoute();
  const [ov, setOv] = useState<Overview | null>(null);
  const ms = useRefreshMs();
  useEffect(() => {
    const l = () => get<Overview>("/api/overview").then(o => { setOv(o); setRefreshSeconds(o.dashboard_refresh_seconds); }).catch(() => {});
    l(); const i = setInterval(l, ms); return () => clearInterval(i);
  }, [ms]);
  const n = ov?.overall.active_incidents ?? (ov?.mac.alerts.length ?? 0) + (ov?.hetzner.alerts.length ?? 0);
  const online = ov ? [ov.mac, ov.hetzner].filter(s => s.connection === "connected").length : 0;
  const anyCrit = (ov?.overall.critical_incidents ?? 0) > 0;
  // Old links (#/mac, #/hetzner, #/alerts) keep working.
  const legacy: Record<string, string> = { alerts: "activity", mac: "machines", hetzner: "machines" };
  const cur = legacy[seg0 ?? ""] ?? seg0 ?? "";
  const machine = seg0 === "mac" || seg0 === "hetzner" ? seg0 : seg1;
  const link = (href: string, key: string, label: string, icon: string, extra?: React.ReactNode) =>
    <a href={href} aria-current={cur === key ? "page" : undefined}><Icon name={icon} />{label}{extra}</a>;
  let page;
  switch (cur) {
    case "machines": page = <MachinesPage selected={machine} />; break;
    case "activity": page = <ActivityPage selected={seg1} />; break;
    case "automation": page = <AutomationPage />; break;
    case "settings": page = <SettingsPage />; break;
    default: page = <HomePage />;
  }
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><Icon name="logo" size={30} /><div>Astra Watch<span>Infrastructure Monitoring</span></div></div>
        <nav className="nav" aria-label="Main">
          {link("#/", "", "Overview", "overview")}
          {link("#/machines", "machines", "Machines", "machines")}
          {link("#/activity", "activity", "Activity", "activity", n > 0 && <span className={`badge ${anyCrit ? "" : "warn"}`} aria-label={`${n} active`}>{n}</span>)}
          {link("#/automation", "automation", "Automation", "automation")}
          {link("#/settings", "settings", "Settings", "settings")}
        </nav>
        <div className="side-foot" aria-live="polite">
          <span className={`dot ${online === 2 ? "good" : online === 0 ? "critical" : "attention"}`} />
          <div><b>{ov ? `${online} / 2 systems online` : "Connecting…"}</b>{ov && <span>Last update: {ago(ov.overall.last_report ?? null)}</span>}</div>
        </div>
      </aside>
      <main className="main">{page}</main>
    </div>
  );
}
const root = document.getElementById("root");
if (root) createRoot(root).render(<StrictMode><App /></StrictMode>);
