"""Real-process tests of the Mac agent (macOS only). Only disposable child processes are ever signalled."""
import importlib.util
import os
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Mac agent tests need macOS")
spec = importlib.util.spec_from_file_location("mac_agent", os.path.join(os.path.dirname(__file__), "..", "..", "agent", "mac_agent.py"))
ma = importlib.util.module_from_spec(spec); spec.loader.exec_module(ma)


@pytest.fixture
def sleeper():
    procs = []
    def start(code="import time; time.sleep(300)"):
        p = subprocess.Popen(["/usr/bin/python3", "-c", code]); procs.append(p); time.sleep(0.5); return p
    yield start
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


def target_for(pid):
    cur = ma.ps_one(pid)
    return {"pid": pid, "start": cur["start"], "exe": cur["exe"], "user": cur["user"], "name": cur["name"]}


def test_graceful_stop_of_disposable_process(sleeper):
    p = sleeper()
    res = ma.stop_process(target_for(p.pid))
    assert res["status"] == "stopped", res
    p.wait(timeout=5)
    assert p.returncode == -signal.SIGTERM                       # ended by a polite SIGTERM, not SIGKILL


def test_pid_reuse_or_changed_identity_is_refused(sleeper):
    p = sleeper()
    t = target_for(p.pid); t["start"] += 100                     # different start time => "different process"
    assert ma.stop_process(t)["status"] == "changed" and p.poll() is None
    t = target_for(p.pid); t["exe"] = "/usr/bin/other"
    assert ma.stop_process(t)["status"] == "changed" and p.poll() is None


def test_missing_process_reports_gone():
    assert ma.stop_process({"pid": 999999, "start": 1, "exe": "x", "user": "x"})["status"] == "gone"


def test_system_and_protected_processes_refused():
    t = target_for(1)                                            # launchd
    res = ma.stop_process(t)
    assert res["status"] in ("unauthorized", "changed") and ma.alive(1)
    rows = ma.classify_all(ma.ps_all())
    prot = ma.ancestors(rows)
    assert all(not ma.eligible(r, prot) for r in rows if r["name"] in ma.PROTECTED_NAMES or r["exe"].startswith(ma.PROTECTED_PREFIXES))
    assert not any(ma.eligible(r, prot) for r in rows if r["pid"] in (os.getpid(), os.getppid()))   # test runner + its parent


def test_agent_never_force_kills_a_stubborn_process(sleeper):
    p = sleeper("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)")
    res = ma.stop_process(target_for(p.pid))
    assert res["status"] == "failed" and "NOT force-killed" in res["message"]
    assert p.poll() is None                                      # still alive: we respected "no force-kill by default"


def test_classification_never_exposes_arguments(sleeper):
    p = sleeper("import time; SECRET='hunter2'; time.sleep(300)")
    snap = ma.process_snapshot(ma.ProcessTracker(), time.time())
    blob = repr(snap)
    assert "hunter2" not in blob and "time.sleep" not in blob
    mine = next(x for x in snap["dev"] if x["pid"] == p.pid)
    assert mine["label"] == "python" and mine["eligible"] and set(mine) >= {"pid", "ppid", "start", "exe"}


def test_vitest_style_workers_are_classified_from_their_command_line(sleeper):
    p = sleeper("import time; time.sleep(300)  # vitest/dist/workers/forks.js")
    # python -c puts the code in argv, so the classifier sees 'vitest'; the label is still python for non-node runtimes.
    rows = ma.classify_all(ma.ps_all())
    assert next(r for r in rows if r["pid"] == p.pid)["label"] == "python"
    assert ma.DEV_PATTERNS[0][1].search("node /x/node_modules/vitest/dist/workers/forks.js")
    assert ma.DEV_PATTERNS[0][1].search("node /x/node_modules/tinypool/dist/entry/process.js")


def test_memory_calculation_matches_native_tools():
    m = ma.Mach(); mem = ma.memory_numbers(m)
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    assert 0 < mem["mem_pct"] < 100 and mem["mem_used"] <= mem["mem_total"]
    page = int(out.split("page size of ")[1].split()[0])
    wired = int([l for l in out.splitlines() if "wired down" in l][0].split(":")[1].strip(" .")) * page
    assert abs(mem["extra"]["wired"] - wired) < 200e6            # same numbers vm_stat reports (they move slightly between calls)
    cpu, idle = m.cpu(); time.sleep(1.0); cpu, idle = m.cpu()
    assert 0 <= cpu <= 100 and abs(cpu + idle - 100) < 0.2


def test_stop_container_rejects_malformed_ids():
    assert ma.stop_container({"id": "abc; rm -rf /"})["status"] == "failed"
    assert ma.stop_container({"id": "--all"})["status"] == "failed"
