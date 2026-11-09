"""Plain-English explanations for incidents.

The engine stores *measurements* (numbers, a likely contributor, a confidence). This module turns a stored incident
into text a non-technical person can act on: what happened, why it matters, how sure we are, numbered steps and how
to check that it is fixed. It is computed when an incident is read or sent, so it needs no extra storage and
improving the wording never needs a data migration.

Honesty rules (also covered by tests):
  * A cause is "confirmed" only when something was directly observed (for example Docker reporting an
    out-of-memory kill, or a failed connection). A "largest user" is never called the cause.
  * Technical terms are explained the first time they appear (glossary).
"""
from __future__ import annotations

import re

GLOSSARY = {
    "swap": "Swap is disk space the computer uses as overflow when memory is full. It works, but it is much slower than memory.",
    "memory pressure": "Memory pressure is macOS's own rating of how hard memory is being squeezed: green is fine, yellow is tight, red is critical.",
    "cpu": "The CPU is the processor, the part that does the computing. At 100% it has no spare capacity.",
    "container": "A container is a packaged app running on the server (via Docker). Stopping one stops that app, not the whole server.",
    "restart loop": "A restart loop means an app keeps crashing and being started again, over and over.",
    "health check": "A health check is a small test Docker runs on an app to see whether it still answers.",
    "oom": "Out-of-memory (OOM) means the system ran out of memory and forcibly closed a program to protect itself.",
    "log": "Logs are text files apps write as they run. Without limits they can grow until they fill the disk.",
}

AUTO_NONE = "Astra Watch took no automatic action. Nothing was stopped or changed."


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def who(machine: str) -> str:
    return "your Mac" if machine == "mac" else "the server"


def _s(inc: dict) -> dict:
    return (inc.get("measured") or {}).get("summary") or {}


def _facts(inc: dict) -> list:
    return list((inc.get("measured") or {}).get("facts") or [])


def _contributor(inc: dict) -> str:
    c = _s(inc).get("Main contributor") or ""
    return c


MAC_ACTIVITY_MONITOR = [
    "Press Command + Space, type “Activity Monitor” and press Return.",
    "Click the “Memory” tab at the top of the window.",
    "Click the “Memory” column heading so the biggest users are listed first.",
    "For anything you no longer need (browser windows, Docker, test runners, editors), save your work, select it and click the ✕ button at the top, then choose Quit.",
]
MAC_CPU_STEPS = [
    "Press Command + Space, type “Activity Monitor” and press Return.",
    "Click the “CPU” tab, then click the “% CPU” heading to list the busiest apps first.",
    "If something you do not need is using a lot, save your work, select it, click ✕ and choose Quit.",
]
SERVER_CONTAINERS = [
    "Open the Server page in Astra Watch and expand “Docker containers”.",
    "Look at the Memory and CPU columns; the busiest containers are listed first.",
    "Decide which ones you do not need right now. Do not stop anything other services depend on.",
    "Click “Stop…” next to a container you can spare. You will be asked to confirm, it stops gracefully and nothing is deleted.",
]


def _base(inc: dict, now: float) -> dict:
    end = inc.get("recovered_at") or now
    start = inc.get("active_since") or inc.get("first_seen") or now
    return {
        "id": inc.get("id"), "rule": inc.get("rule"), "machine": inc.get("machine"),
        "severity": inc.get("severity"), "state": inc.get("state"),
        "duration_seconds": max(0, int(end - start)),
        "measurements": [f"{k}: {v}" for k, v in _s(inc).items()],
        "automatic_action": AUTO_NONE, "glossary": [], "steps": [], "verify": [],
    }


def _cause(text: str, kind: str, confidence: str, evidence: list, note: str = "") -> dict:
    return {"text": text, "kind": kind, "confidence": confidence or "", "evidence": evidence, "note": note}


def _g(*terms):
    return [{"term": t, "meaning": GLOSSARY[t]} for t in terms]


def explain(inc: dict, now: float, actions: list | None = None) -> dict:
    """Return the structured explanation for one incident row (measured already parsed)."""
    out = _base(inc, now)
    rule, machine = inc.get("rule"), inc.get("machine")
    s, facts = _s(inc), _facts(inc)
    me = who(machine)
    conf = inc.get("confidence") or ""
    inferred_note = "This is the largest user we measured. That does not prove it caused the problem."
    builder = BUILDERS.get(rule)
    if builder:
        builder(out, inc, s, facts, me, conf, inferred_note)
    else:
        out.update(headline=inc.get("title", "A problem was detected"), what=inc.get("title", "") + ".",
                   why=inc.get("impact") or "", cause=_cause(inc.get("cause") or "Not determined", "inferred", conf, facts),
                   steps=[inc.get("recommendation") or "Open the machine page for details."],
                   verify=[f"This alert clears by itself once {me} is back to normal."])
    if actions:
        out["automatic_action"] = "; ".join(actions)
    return out


# ---------------------------------------------------------------------------- builders
def _memory(o, inc, s, facts, me, conf, note):
    pct = s.get("Memory used", "?")
    contrib = _contributor(inc)
    if inc["machine"] == "mac":
        o.update(headline="Your Mac is running low on memory",
                 what=f"{pct} of your Mac's memory is in use. When memory runs short, macOS moves some data onto the SSD "
                      "(this is called swap) to keep apps running. That is slower than normal memory.",
                 why="The Mac can feel sluggish, apps may freeze for a moment, and if it gets worse macOS may force apps to close.")
        o["steps"] = MAC_ACTIVITY_MONITOR + ["Look at the “Memory Pressure” graph at the bottom: it should turn green."]
        o["verify"] = ["In Activity Monitor, Memory Pressure is green.", "Astra Watch shows My Mac as Good within a few minutes."]
        o["glossary"] = _g("swap", "memory pressure")
    else:
        o.update(headline="The server is running low on memory",
                 what=f"{pct} of the server's memory is in use. All containers share it.",
                 why="If memory runs out, Linux force-closes a program to protect itself (an out-of-memory kill), and new containers may fail to start.")
        o["steps"] = SERVER_CONTAINERS
        o["verify"] = ["Memory on the Server page falls well below the warning level.", "Astra Watch shows the server as Good."]
        o["glossary"] = _g("container", "oom")
    o["cause"] = _cause(f"Largest identified memory user: {contrib}" if contrib else (inc.get("cause") or "Not determined"),
                        "inferred", conf, facts, note if contrib else "")


def _cpu(o, inc, s, facts, me, conf, note):
    c = s.get("CPU used", "?")
    contrib = _contributor(inc)
    o.update(headline=f"{_cap(me)} has been working very hard",
             what=f"The processor has averaged {c} for several minutes. At 100% it has no spare capacity.",
             why="Everything runs slower: builds, tests, websites and apps take longer to respond.",
             glossary=_g("cpu"))
    o["cause"] = _cause(f"Busiest right now: {contrib}" if contrib else (inc.get("cause") or "Not determined"), "inferred", conf, facts,
                        note if contrib else "")
    o["steps"] = MAC_CPU_STEPS if inc["machine"] == "mac" else SERVER_CONTAINERS
    o["verify"] = ["The CPU graph on the machine page drops well below the warning level.", "Astra Watch shows it as Good."]


def _disk(o, inc, s, facts, me, conf, note):
    free = s.get("Free space", "?")
    crit = inc.get("severity") == "critical"
    o.update(headline=f"{_cap(me)} is running out of storage space" if crit else f"{_cap(me)}'s storage is getting full",
             what=f"Only {free} of storage is left." if crit else f"{free} of storage is left. That is lower than recommended, but it is not urgent yet.",
             why="When storage is full, apps crash, updates fail and the computer cannot save files." if inc["machine"] == "mac"
                 else "When storage is full, containers and databases stop working and may be damaged.",
             glossary=[])
    o["cause"] = _cause(inc.get("cause") or "Not determined", "inferred", conf, facts,
                        "This lists the biggest measured storage users. It does not mean they should be deleted.")
    if inc["machine"] == "mac":
        o["steps"] = ["Open the Apple menu → System Settings → General → Storage.",
                      "Look at the bar to see which categories are largest (Developer, Documents, Applications).",
                      "Empty the Trash and remove large files or old downloads you no longer need.",
                      "If Docker is large, open Docker Desktop and review old images and containers you no longer use."]
    else:
        o["steps"] = ["Open the Server page and expand “Technical details” → “Storage used by Docker”.",
                      "Check which of images, volumes, build cache or container logs is largest.",
                      "Decide with care: never delete a volume unless you are sure its data is not needed. Astra Watch will not delete anything for you.",
                      "For very large container logs, add log rotation to that container’s settings."]
    o["verify"] = ["Free space is above the warning level on the machine page.", "Astra Watch shows the machine as Good."]


def _swap_full(o, inc, s, facts, me, conf, note):
    o.update(headline=f"{_cap(me)}'s overflow memory is almost full",
             what=f"The swap space is nearly used up ({s.get('Swap used', '?')}). Swap is disk space the computer uses when memory is full.",
             why="Once swap is full the system may freeze or force-close programs.", glossary=_g("swap"),
             cause=_cause("Memory demand is higher than available memory", "inferred", conf, facts))
    o["steps"] = MAC_ACTIVITY_MONITOR if inc["machine"] == "mac" else SERVER_CONTAINERS
    o["verify"] = ["Swap use falls on the machine page.", "Astra Watch shows it as Good."]


def _swap_growth(o, inc, s, facts, me, conf, note):
    o.update(headline=f"{_cap(me)} is using more and more overflow memory",
             what=f"Swap use has grown by {str(s.get('Swap growth (30 min)', 'a lot')).lstrip('+')} in the last 30 minutes. Swap is disk space used when memory is full.",
             why="Things slow down gradually as more data is pushed to the disk.", glossary=_g("swap"))
    contrib = _contributor(inc)
    o["cause"] = _cause(f"Largest identified memory user: {contrib}" if contrib else "Memory demand is higher than available memory",
                        "inferred", conf, facts, note if contrib else "")
    o["steps"] = MAC_ACTIVITY_MONITOR if inc["machine"] == "mac" else SERVER_CONTAINERS
    o["verify"] = ["Swap use stops growing and starts to fall.", "Astra Watch shows it as Good."]


def _disk_growth(o, inc, s, facts, me, conf, note):
    o.update(headline="Storage is filling up quickly",
             what=f"Free space is shrinking at {s.get('Filling at', 'a fast rate')}, so it could be full in about {s.get('Full in about', 'a day')}.",
             why="Once full, apps and containers start failing.",
             cause=_cause("Something is writing data continuously (logs, images, caches or volumes)", "inferred", conf, facts,
                          "We can see that space is shrinking, not which program is responsible."))
    o["steps"] = ["Open the machine page and expand “Technical details”, then look at the storage sections.",
                  "Find the largest item and decide whether it is expected to grow.",
                  "Nothing is deleted for you; free space or add log rotation yourself."]
    o["verify"] = ["The “Free disk” graph flattens or rises.", "Astra Watch shows the machine as Good."]


def _agent_down(o, inc, s, facts, me, conf, note):
    o.update(headline="Your Mac has stopped reporting",
             what=f"Astra Watch has not heard from your Mac for {s.get('Last heard from', 'a while')}.",
             why="While the Mac is silent, Astra Watch cannot tell whether it has problems. The values shown are the last ones received, not current.",
             cause=_cause("The Mac is probably asleep, shut down or offline. A stopped agent is also possible.", "inferred", conf, facts,
                          "A sleeping laptop looks exactly like this, so it is often not a fault."))
    o["steps"] = ["If you closed the lid or shut the Mac down, no action is needed. It reconnects when it wakes up.",
                  "Open the Mac and make sure it is awake and connected to the internet.",
                  "If it is awake, open Terminal and run: launchctl list | grep astra (the agent should be listed)."]
    o["verify"] = ["The Mac card on Home says “Connected” with a recent report time."]


def _restart_loop(o, inc, s, facts, me, conf, note):
    name = s.get("Container", "A container")
    oom = "out-of-memory" in " ".join(facts)
    o.update(headline=f"{name} keeps crashing and restarting",
             what=f"{name} has restarted {s.get('Restarts (15 min)', 'many')} times in 15 minutes.",
             why=f"{name} is unstable or unavailable, and the repeated restarts use CPU.",
             glossary=_g("restart loop", "oom") if oom else _g("restart loop"))
    o["cause"] = (_cause("Docker reports it was killed for running out of memory", "confirmed", "high", facts) if oom else
                  _cause("The application inside keeps failing when it starts", "inferred", conf, facts,
                         "The exit code tells us it crashed, not why."))
    o["steps"] = ["Open the Server page → “Docker containers” and find this container.",
                  "Check its Restarts and Status columns.",
                  "(Advanced) over SSH, read the last lines of its log with: docker logs --tail 50 <name>",
                  "If you do not need it right now, use “Stop…” to stop the restarting. You will be asked to confirm."]
    o["verify"] = ["The container’s restart count stops increasing for at least 15 minutes."]


def _unhealthy(o, inc, s, facts, me, conf, note):
    name = s.get("Container", "A container")
    o.update(headline=f"{name} is failing its health check",
             what=f"Docker runs a small test against {name} and the test is failing.",
             why=f"{name} may not be answering requests.", glossary=_g("health check"),
             cause=_cause(f"Docker’s own health check for {name} is failing", "confirmed", "high", facts,
                          "That the check fails is measured; why it fails is not known."))
    o["steps"] = ["Open the Server page → “Docker containers” and check its status.",
                  "(Advanced) over SSH: docker logs --tail 50 <name>",
                  "Restart or fix the app if it is yours; stop it if you do not need it."]
    o["verify"] = ["The container shows “healthy” again."]


def _service_down(o, inc, s, facts, me, conf, note):
    name = s.get("Container", "A container")
    o.update(headline=f"{name} has stopped unexpectedly",
             what=f"{name} exited with code {s.get('Exit code', '?')} and is not running.",
             why=f"Anything that relies on {name} is not working.",
             cause=_cause("The program inside exited on its own", "inferred", conf, facts, "The exit code says it ended, not why."))
    o["steps"] = ["(Advanced) over SSH: docker logs --tail 50 <name> to see why it stopped.",
                  "Start it again from the server if it should be running. Astra Watch does not restart containers."]
    o["verify"] = ["The container shows as running on the Server page."]


def _container_cpu(o, inc, s, facts, me, conf, note):
    name = s.get("Container", "A container")
    o.update(headline=f"{name} is using a lot of processor power",
             what=f"{name} has been using {s.get('CPU', 'a lot of CPU')} for several minutes.",
             why="The server has two processors, so one busy container slows the others.", glossary=_g("cpu"),
             cause=_cause(f"{name} itself is busy (measured)", "confirmed", "high", facts,
                          "We can see it is busy. Whether that work is useful or stuck is not known."))
    o["steps"] = SERVER_CONTAINERS
    o["verify"] = ["The container’s CPU falls on the Server page."]


def _container_mem(o, inc, s, facts, me, conf, note):
    name = s.get("Container", "A container")
    near = "limit" in " ".join(facts)
    o.update(headline=f"{name} is using a lot of memory",
             what=f"{name} is holding {s.get('Memory', 'a large share of memory')}.",
             why="Less is left for other containers, and out-of-memory kills become more likely.", glossary=_g("container", "oom"),
             cause=_cause(f"{name} is holding a large share of memory (measured)", "confirmed" if near else "inferred", "high", facts,
                          "" if near else "A large share is not necessarily a fault; some apps use a lot of memory by design."))
    o["steps"] = SERVER_CONTAINERS
    o["verify"] = ["The container’s memory falls on the Server page."]


def _log_size(o, inc, s, facts, me, conf, note):
    name = s.get("Container", "A container")
    o.update(headline=f"{name} has very large log files",
             what=f"Its log files have grown to {s.get('Log size', 'a large size')}.",
             why="Logs use up disk space and the server’s storage is limited.", glossary=_g("log"),
             cause=_cause(f"{name} writes a lot of log output and the logs are not being trimmed", "confirmed", "high", facts))
    o["steps"] = ["Decide whether the logs are useful to keep.",
                  "Add log rotation (max-size) to this container’s configuration so logs stay small.",
                  "Astra Watch will not delete the logs for you."]
    o["verify"] = ["The log size on the Server page stops growing."]


def _service_probe(o, inc, s, facts, me, conf, note):
    o.update(headline="The server’s web server is not answering",
             what=f"{s.get('Service', 'The web server')} on port {s.get('Port', '?')} refused a test connection.",
             why="Websites hosted on this server are unreachable.",
             cause=_cause("Connections to this service are being refused (measured)", "confirmed", "high", facts,
                          "Why it stopped is not known; check whether nginx crashed."))
    o["steps"] = ["(Advanced) over SSH: systemctl status nginx", "Start it with: sudo systemctl start nginx (if it is stopped)."]
    o["verify"] = ["The alert clears once the port answers again."]


def _dev_accum(o, inc, s, facts, me, conf, note):
    m = re.search(r"(\d+) orphaned", " ".join(facts))
    orphans = bool(m and int(m.group(1)) > 0)
    o.update(headline="Leftover development processes are using memory",
             what=f"{inc.get('title', 'Development tools')} are still running and holding {s.get('Memory held', 'memory')}.",
             why="That memory is not available to your apps, and macOS leans on slower overflow memory.", glossary=_g("swap"),
             cause=_cause(inc.get("cause") or "Tools were started and never closed", "confirmed" if orphans else "inferred", conf, facts,
                          "" if orphans else "We can see they are running, not that they are finished."))
    o["steps"] = ["Check that no test run or build is still in progress.",
                  *MAC_ACTIVITY_MONITOR[:3],
                  "Quit the leftover processes (look for node, vitest or jest), or use the Stop button on this page."]
    o["verify"] = ["The process count drops on the My Mac page.", "Astra Watch shows My Mac as Good."]


def _docker_vm(o, inc, s, facts, me, conf, note):
    o.update(headline="Docker is using a lot of your Mac’s memory",
             what=f"Docker Desktop is holding {s.get('Docker VM memory', 'a lot of memory')} with {s.get('Running containers', 'several')} containers running.",
             why="That memory is not available to other apps.", glossary=_g("container"),
             cause=_cause(inc.get("cause") or "Containers are using memory", "inferred", conf, facts))
    o["steps"] = ["Open Docker Desktop and look at the Containers tab.", "Stop containers you are not using (select → Stop).",
                  "If Docker still holds the memory, quit and reopen Docker Desktop."]
    o["verify"] = ["Docker VM memory drops on the My Mac page."]


def _multi_build(o, inc, s, facts, me, conf, note):
    o.update(headline="Several builds are running at once",
             what=f"{s.get('Builds', 'Several')} build tools are running together, using {s.get('CPU', 'a lot of')} of processor time.",
             why="They compete for the same CPU and memory, so all of them finish slower.",
             cause=_cause("More than one build is running at the same time (measured)", "confirmed", "medium", facts))
    o["steps"] = ["Let one finish first, or stop the build you do not need (use Activity Monitor → CPU tab)."]
    o["verify"] = ["Only one build remains and CPU use falls."]


def _mem_growth(o, inc, s, facts, me, conf, note):
    name = s.get("Process", "A program")
    o.update(headline=f"{name} keeps using more memory",
             what=f"{name} is now using {s.get('Memory now', 'a lot')}, up {s.get('Growth', 'steadily')} over the last 20 minutes.",
             why="It will keep taking memory until it is restarted.",
             cause=_cause("A possible memory leak or an ever-growing cache", "inferred", "low", facts,
                          "Steady growth is a hint, not proof of a leak."))
    o["steps"] = ["Save your work in that program.", "Quit and reopen it. Its memory should drop back to normal."]
    o["verify"] = ["Its memory no longer climbs after reopening."]


def _self(o, inc, s, facts, me, conf, note):
    o.update(headline="Astra Watch itself is using more resources than expected",
             what="The monitoring app is using more memory, CPU or database space than it should.",
             why="It competes with your workloads on a small server.",
             cause=_cause("Unexpected load or growth inside the monitoring app", "inferred", conf, facts))
    o["steps"] = ["(Advanced) over SSH: systemctl --user restart infra-watcher", "If it returns, check the app’s log for repeated errors."]
    o["verify"] = ["The numbers on the Settings page return to normal."]


def _monitor_failure(o, inc, s, facts, me, conf, note):
    o.update(headline="Astra Watch cannot read the server’s metrics",
             what=f"{s.get('Failed collections', 'Several')} collection attempts in a row failed.",
             why="Problems on the server could go unnoticed while this lasts.",
             cause=_cause("Reading system information or Docker is failing", "inferred", conf, facts))
    o["steps"] = ["(Advanced) over SSH: journalctl --user -u infra-watcher -n 50", "Check that Docker is running: systemctl status docker"]
    o["verify"] = ["The alert clears after a successful collection."]


BUILDERS = {
    "memory": _memory, "cpu": _cpu, "disk": _disk, "swap_full": _swap_full, "swap_growth": _swap_growth,
    "disk_growth": _disk_growth, "agent_down": _agent_down, "restart_loop": _restart_loop, "unhealthy": _unhealthy,
    "service_down": _service_down, "container_cpu": _container_cpu, "container_mem": _container_mem,
    "log_size": _log_size, "service_probe": _service_probe, "dev_accumulation": _dev_accum, "docker_vm": _docker_vm,
    "multi_build": _multi_build, "mem_growth": _mem_growth, "self_resource": _self, "monitor_failure": _monitor_failure,
}


# ---------------------------------------------------------------------------- recovery wording
IMPROVE = {
    "memory": ("mem_pct", "Memory in use", "%"), "cpu": ("cpu", "Processor use", "%"),
}


def recovery_facts(inc: dict, sample: dict | None) -> list[str]:
    """What improved, from the values at the time of the problem versus now. Only states what was measured."""
    out = []
    v = (inc.get("measured") or {}).get("values") or {}
    if not sample:
        return out
    if inc["rule"] == "memory" and "mem_pct" in v:
        out.append(f"Memory in use: {v['mem_pct']:.0f}% → {sample.get('mem_pct') or 0:.0f}%")
    elif inc["rule"] == "cpu" and "cpu" in v:
        out.append(f"Processor use: {v['cpu']:.0f}% → {sample.get('cpu') or 0:.0f}%")
    elif inc["rule"] == "disk" and "free_pct" in v and sample.get("disk_total"):
        out.append(f"Free storage: {v['free_pct']:.0f}% → {100.0 * sample['disk_free'] / sample['disk_total']:.0f}%")
    return out


def root_cause_status(inc: dict, actions: list | None) -> tuple[str, str]:
    """('fixed'|'unknown', sentence). 'Alert cleared' only means the measurement dropped below the limit."""
    if actions:
        return "action", ("You approved an action (" + "; ".join(actions) + "). The reading is back to normal, which suggests it helped, "
                          "but Astra Watch cannot prove the underlying cause is gone.")
    return "unknown", ("Not determined. The alert cleared because the reading went back under the limit. "
                       "That does not mean the underlying cause has been fixed, so it may come back.")
