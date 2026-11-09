import { ReactNode, useState } from "react";
import { Container, Detail, Overview, Proc } from "./api";
import { MACHINE_OS, Pill, ResourceRows, dockerLine } from "./home";
import { usePoll } from "./hooks";
import { MachineChart, METRICS, Range, RangeTabs } from "./charts";
import { ConnectionLine, MACHINE_NAME, Stat, Icon, StopDialog, ago, fmtBytes, fmtDur, fmtMem, fmtPct } from "./ui";

const FRIENDLY: Record<string, string> = {
  vitest: "Vitest test workers", jest: "Jest test workers", node: "Node.js", java: "Java", python: "Python", maven: "Maven builds",
  gradle: "Gradle builds", "docker-vm": "Docker Desktop", claude: "Claude Code", vite: "Dev servers", build: "Build tools",
  browser: "Web browser", other: "Other apps",
};

export function Section({ title, hint, children, open = false }: { title: string; hint?: string; children: ReactNode; open?: boolean }) {
  return (
    <details className="fold" open={open || undefined}>
      <summary><span>{title}</span>{hint && <small>{hint}</small>}</summary>
      <div className="fold-body">{children}</div>
    </details>
  );
}

/** Four independent graphs for ONE machine. Every request carries the machine name, so series can never mix. */
function History({ machine, hours }: { machine: "mac" | "hetzner"; hours: number }) {
  const [range, setRange] = useState<Range>("1h");
  const metrics = METRICS;
  return (
    <>
      <div className="toolbar">
        <span className="muted">{MACHINE_NAME[machine]} measurements only.</span>
        <RangeTabs value={range} onChange={setRange} hours={hours} label="Time range" />
      </div>
      <div className="charts">
        {metrics.map(x => <div key={x.key} className="panel pad chart-card"><h4 className="chart-title">{x.title}</h4><MachineChart machine={machine} metric={x.key} range={range} height={130} /></div>)}
      </div>
    </>
  );
}

function ProcTable({ rows, title, onStop, showContainer }: { rows: Proc[]; title: string; onStop?: (p: Proc) => void; showContainer?: boolean }) {
  return (
    <>
      <h3>{title}</h3>
      <div className="tablewrap"><table>
        <thead><tr><th>Process</th><th className="num">PID</th><th className="num">CPU</th><th className="num">Memory</th><th>Context</th>{onStop && <th />}</tr></thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={6} className="muted">No data yet.</td></tr>}
          {rows.map(p => (
            <tr key={p.pid}>
              <td>{p.name}{p.label && !["other", "browser"].includes(p.label) && <> <span className="tag">{p.label}</span></>}</td>
              <td className="num mono">{p.pid}</td><td className="num">{p.cpu.toFixed(0)}%</td><td className="num">{fmtMem(p.rss)}</td>
              <td className="muted">{p.why || (showContainer && p.container_name) || p.parent || ""}{p.growing && " · memory keeps growing"}</td>
              {onStop && <td className="num">{p.eligible && <button className="btn small danger" onClick={() => onStop(p)} aria-label={`Stop ${p.name} ${p.pid}`}>Stop…</button>}</td>}
            </tr>
          ))}
        </tbody>
      </table></div>
    </>
  );
}

function ContainerTable({ rows, onStop }: { rows: Container[]; onStop: (c: Container) => void }) {
  const sorted = [...rows].sort((a, b) => b.mem - a.mem);
  return (
    <div className="tablewrap"><table>
      <thead><tr><th>Container</th><th>Status</th><th className="num">CPU</th><th className="num">Memory</th><th className="num">Restarts</th><th>Details</th><th /></tr></thead>
      <tbody>
        {sorted.map(c => (
          <tr key={c.id}>
            <td><b>{c.name}</b><div className="muted mono">{c.short ?? c.id.slice(0, 12)} · {c.image}</div></td>
            <td><span className={`tag ${c.running ? (c.health === "unhealthy" ? "crit" : "ok") : ""}`}>{c.running ? (c.health && c.health !== "none" ? c.health : "running") : c.status}</span></td>
            <td className="num">{c.running ? `${c.cpu.toFixed(0)}%` : "–"}</td><td className="num">{c.running ? fmtMem(c.mem) : "–"}</td>
            <td className={`num ${c.restarts >= 3 ? "state warning" : ""}`}>{c.restarts ?? "–"}</td>
            <td className="muted">
              {c.restart_policy && <>policy {c.restart_policy}</>}{c.ports?.length ? <> · {c.ports.slice(0, 2).join(", ")}</> : null}
              {c.mounts?.length ? <> · {c.mounts.length} mount{c.mounts.length > 1 ? "s" : ""}</> : null}
              {c.log_size != null && <> · log {fmtBytes(c.log_size)}</>}
            </td>
            <td className="num">{c.running && <button className="btn small danger" onClick={() => onStop(c)} aria-label={`Stop ${c.name}`}>Stop…</button>}</td>
          </tr>
        ))}
      </tbody>
    </table></div>
  );
}

/** "What is using it": plain-English list of the biggest users. Size, not blame. */
function TopUsers({ d, mac }: { d: Detail; mac: boolean }) {
  let rows: { name: string; detail: string }[] = [];
  if (mac && d.procs?.groups) {
    rows = Object.entries(d.procs.groups).sort((a, b) => b[1].rss - a[1].rss).slice(0, 4)
      .map(([k, g]) => ({ name: FRIENDLY[k] ?? k, detail: `${fmtMem(g.rss)} of memory in ${g.count} process${g.count === 1 ? "" : "es"}` }));
  } else if (!mac && d.container_list?.list) {
    rows = d.container_list.list.filter(c => c.running).sort((a, b) => b.mem - a.mem).slice(0, 4)
      .map(c => ({ name: c.name, detail: `${fmtMem(c.mem)} of memory, ${c.cpu.toFixed(0)}% processor` }));
  }
  if (!rows.length) return <p className="muted">No breakdown available yet.</p>;
  return (
    <>
      <ul className="users">{rows.map(r => <li key={r.name}><b>{r.name}</b><span className="muted">{r.detail}</span></li>)}</ul>
      <p className="foot">The largest users right now. Being large does not mean something is wrong.</p>
    </>
  );
}

function MachineView({ machine, hours }: { machine: "mac" | "hetzner"; hours: number }) {
  const { data: d, err, reload } = usePoll<Detail>(`/api/machine/${machine}`);
  const [stop, setStop] = useState<any>(null);
  if (err && !d) return <div className="banner">{err}</div>;
  if (!d) return <p className="muted">Loading…</p>;
  const s = d.sample;
  const mac = machine === "mac";
  const x = s?.extra ?? {};
  const pressure: Record<number, string> = { 1: "Normal", 2: "Elevated", 4: "Critical" };
  const done = () => { setStop(null); reload(); };
  const stopProc = (p: Proc) => setStop({ machine, kind: "process", pid: p.pid, start: (p as any).start });
  const docker = mac
    ? (d.procs?.docker_running ? `Docker Desktop is running${d.containers != null ? ` with ${d.containers} container${d.containers === 1 ? "" : "s"}` : ""}.` : "Docker Desktop is not running.")
    : dockerLine(d);
  const issues = d.alerts.length;
  return (
    <>
      <section className="panel pad">
        <div className="machine-head">
          <span className="machine-ico"><Icon name={mac ? "laptop" : "server"} size={26} /></span>
          <div className="machine-id"><h2>{MACHINE_NAME[machine]}</h2><p>{MACHINE_OS[machine]}{s?.mem_total ? ` · ${fmtMem(s.mem_total)} RAM` : ""}{d.agent?.version ? ` · agent ${d.agent.version}` : ""}</p></div>
          <Pill s={d} />
        </div>
        <div className="machine-sub">
          {issues > 0
            ? <div className="issues" style={{ margin: "0 -22px 6px" }}>{d.alerts.map(a => (
                <a key={a.id} className={`issue ${a.severity}`} href={`#/activity/${a.id}`}><span className={`dot ${a.severity}`} /><span className="issue-title">{a.title}</span><span className="issue-meta">See what to do</span></a>))}</div>
            : <p style={{ marginBottom: 6 }}>{d.status.headline}</p>}
          <ConnectionLine connection={d.connection} last={d.last_seen} />
        </div>
        {d.resources.length > 0 ? <ResourceRows rows={d.resources} dim={d.connection !== "connected"} /> : <p className="muted">No data yet.</p>}
        {docker && <p className="foot" style={{ marginTop: 16 }}><b>Docker</b> · {docker}</p>}
      </section>

      <h2>History</h2>
      <History machine={machine} hours={hours} />

      <h2>What is using it</h2>
      <section className="panel pad"><TopUsers d={d} mac={mac} /></section>

      <h2>Technical details</h2>
      <section className="panel">
        <Section title="Applications and processes" hint="CPU and memory per process, PIDs, Stop buttons">
          {d.procs ? (
            <>
              {mac && Object.keys(d.procs.groups ?? {}).length > 0 && (
                <div className="tablewrap"><table>
                  <thead><tr><th>Type</th><th className="num">Processes</th><th className="num">Memory</th><th className="num">CPU</th><th>Notes</th></tr></thead>
                  <tbody>{Object.entries(d.procs.groups ?? {}).sort((a, b) => b[1].rss - a[1].rss).map(([k, g]) => (
                    <tr key={k}><td>{FRIENDLY[k] ?? k}</td><td className="num">{g.count}</td><td className="num">{fmtMem(g.rss)}</td><td className="num">{g.cpu.toFixed(0)}%</td>
                      <td className="muted">{g.orphans ? `${g.orphans} without a parent process` : ""}{g.idle ? `${g.orphans ? " · " : ""}${g.idle} idle` : ""}</td></tr>))}
                  </tbody></table></div>
              )}
              <ProcTable title="Using the most processor" rows={d.procs.top_cpu} showContainer={!mac} onStop={mac ? stopProc : undefined} />
              <ProcTable title="Using the most memory" rows={d.procs.top_mem} showContainer={!mac} onStop={mac ? stopProc : undefined} />
              {mac && (d.procs.dev ?? []).length > 0 && <ProcTable title="Development processes" rows={d.procs.dev!.slice(0, 20)} onStop={stopProc} />}
            </>
          ) : <p className="muted">No process data yet.</p>}
        </Section>
        <Section title="Docker containers" hint="status, health checks, restarts, memory, Stop buttons">
          {d.container_list?.list?.length
            ? <ContainerTable rows={mac ? d.container_list.list.map(c => ({ ...c, short: c.id.slice(0, 12), restarts: c.restarts ?? 0, ports: c.ports ?? [], mounts: c.mounts ?? [] })) : (d.container_list!.list as Container[])}
                onStop={c => setStop({ machine, kind: "container", id: c.id })} />
            : <div className="muted">No containers reported.</div>}
        </Section>
        <Section title="Memory, load and disk" hint="pressure, swap, load average, disk activity">
          {s && (
            <dl className="stats">
              <Stat label="Processor in use" value={fmtPct(s.cpu)} sub={mac && x.cpu_idle != null ? `${Math.round(x.cpu_idle)}% idle` : ""} />
              <Stat label="Load average" value={s.load1.toFixed(2)} sub={`${s.load5.toFixed(2)} · ${s.load15.toFixed(2)}`} note="Tasks waiting for the processor, over 1, 5 and 15 minutes. Only worth a look if it stays above the number of cores." />
              <Stat label={mac ? "Installed memory" : "Total memory"} value={fmtMem(s.mem_total)} />
              <Stat label="Memory in use" value={fmtMem(s.mem_used)} sub={fmtPct(s.mem_pct)} />
              {mac && <Stat label="Memory pressure" value={pressure[x.pressure_level] ?? "–"} note="macOS's own verdict on memory. This matters more than the percentage used." />}
              {mac && <Stat label="Compressed memory" value={fmtMem(x.compressed)} note="Memory squeezed to save space. Normal." />}
              <Stat label="Swap" value={fmtMem(s.swap_used)} sub={s.swap_total ? `of ${fmtMem(s.swap_total)}` : ""} note="Disk space used as extra memory. A little is normal." />
              <Stat label={mac ? "Internal SSD" : "Disk"} value={`${fmtBytes(s.disk_total - s.disk_free)} used`} sub={`${fmtBytes(s.disk_free)} free`} />
              {mac && <Stat label="Thermal state" value={x.thermal ? (x.thermal.state === "nominal" ? "Normal" : x.thermal.state === "throttled" ? "Throttled" : "Not available") : "–"} />}
              {!mac && <Stat label="Disk activity" value={x.io_read_mbs != null ? `${x.io_read_mbs} / ${x.io_write_mbs} MB/s` : "–"} sub={x.io_util != null ? `${Math.round(x.io_util)}% busy` : "read / write"} />}
              {!mac && <Stat label="Uptime" value={x.uptime ? fmtDur(x.uptime) : "–"} />}
              {mac && d.procs?.docker_vm_rss != null && <Stat label="Docker VM memory" value={fmtMem(d.procs.docker_vm_rss)} />}
            </dl>
          )}
        </Section>
        {(d.docker_df || (d.storage && d.storage.dirs.length > 0)) && (
          <Section title="Storage breakdown" hint="Docker images, volumes, logs and large folders">
            {d.docker_df && (
              <>
                <dl className="stats">
                  {d.docker_df.images_total != null && <Stat label="Docker images" value={fmtBytes(d.docker_df.images_total)} />}
                  {d.docker_df.volumes_total != null && <Stat label="Docker volumes" value={fmtBytes(d.docker_df.volumes_total)} />}
                  {d.docker_df.build_cache_total != null && <Stat label="Build cache" value={fmtBytes(d.docker_df.build_cache_total)} />}
                  {d.docker_df.vm_disk_file != null && <Stat label="Docker VM disk file" value={fmtBytes(d.docker_df.vm_disk_file)} />}
                  {d.logs && <Stat label="Container logs" value={fmtBytes(Object.values(d.logs.sizes).reduce((a, b) => a + b, 0))} />}
                </dl>
                {d.docker_df.top_volumes?.length > 0 && (
                  <div className="tablewrap" style={{ marginTop: 10 }}><table><thead><tr><th>Largest volumes</th><th className="num">Size</th></tr></thead>
                    <tbody>{d.docker_df.top_volumes.slice(0, 6).map((v: any) => <tr key={v.name}><td className="mono">{v.name}</td><td className="num">{fmtBytes(v.size)}</td></tr>)}</tbody></table></div>
                )}
              </>
            )}
            {d.storage && d.storage.dirs.length > 0 && (
              <>
                <h3>Large development folders</h3>
                <div className="tablewrap"><table><tbody>{d.storage.dirs.slice(0, 10).map(r => <tr key={r.name}><td>{r.name}</td><td className="num">{fmtBytes(r.size)}</td></tr>)}</tbody></table></div>
                <p className="foot">Measured a few times a day at low priority, never continuously.</p>
              </>
            )}
          </Section>
        )}
      </section>
      <p className="foot">Last measurements received {ago(d.last_update)}.</p>
      <StopDialog request={stop} onClose={done} />
    </>
  );
}

export function MachinesPage({ selected }: { selected?: string }) {
  const machine: "mac" | "hetzner" = selected === "hetzner" ? "hetzner" : "mac";
  const { data: ov } = usePoll<Overview>("/api/overview");
  const tab = (m: "mac" | "hetzner") => (
    <a key={m} href={`#/machines/${m}`} aria-current={machine === m ? "page" : undefined}>
      <span className={`dot ${ov?.[m].status.level === "good" ? "good" : ov?.[m].status.level === "critical" ? "critical" : ov?.[m].status.level ? "attention" : ""}`} />{MACHINE_NAME[m]}
    </a>
  );
  return (
    <>
      <div className="toolbar"><h1>Machines</h1><nav className="seg" aria-label="Choose a machine">{tab("mac")}{tab("hetzner")}</nav></div>
      <MachineView key={machine} machine={machine} hours={ov?.retention.hours ?? 24} />
    </>
  );
}
