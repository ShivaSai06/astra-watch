import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Overview, get, setRefreshSeconds } from "./api";
import { useRefreshMs } from "./pages";
import { AlertsPage, MachinePage, OverviewPage, SettingsPage } from "./pages";
import "./styles.css";

function useRoute() {
  const [h, setH] = useState(location.hash || "#/");
  useEffect(() => { const f = () => setH(location.hash || "#/"); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return h.replace(/^#/, "").split("/").filter(Boolean);
}

function App() {
  const [seg0, seg1] = useRoute();
  const [ov, setOv] = useState<Overview | null>(null);
  const ms = useRefreshMs();
  useEffect(() => {
    const l = () => get<Overview>("/api/overview").then(o => { setOv(o); setRefreshSeconds(o.dashboard_refresh_seconds); }).catch(() => {});
    l(); const i = setInterval(l, ms); return () => clearInterval(i);
  }, [ms]);
  const crit = (ov?.mac.alerts.length ?? 0) + (ov?.hetzner.alerts.length ?? 0);
  const anyCrit = [...(ov?.mac.alerts ?? []), ...(ov?.hetzner.alerts ?? [])].some(a => a.severity === "critical");
  const cur = seg0 ?? "";
  const link = (href: string, key: string, label: string, extra?: React.ReactNode) =>
    <a href={href} aria-current={cur === key ? "page" : undefined}>{label}{extra}</a>;
  let page;
  switch (cur) {
    case "mac": page = <MachinePage machine="mac" />; break;
    case "hetzner": page = <MachinePage machine="hetzner" />; break;
    case "alerts": page = <AlertsPage selected={seg1} />; break;
    case "settings": page = <SettingsPage />; break;
    default: page = <OverviewPage />;
  }
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">Astra Watch<small>Mac · Server</small></div>
        <nav className="nav" aria-label="Main">
          {link("#/", "", "Overview")}
          {link("#/mac", "mac", "My Mac")}
          {link("#/hetzner", "hetzner", "Server")}
          {link("#/alerts", "alerts", "Alerts", crit > 0 && <span className={`badge ${anyCrit ? "" : "warn"}`} aria-label={`${crit} active`}>{crit}</span>)}
          {link("#/settings", "settings", "Settings")}
        </nav>
      </aside>
      <main className="main">{page}</main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
