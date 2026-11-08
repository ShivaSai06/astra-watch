"""Lightweight Linux collector for the Hetzner host. Reads /proc and cgroup v2 directly.

No commands are spawned for CPU/memory/disk/process data, so collection is nearly free.
Process *arguments are never read* (only the kernel `comm` name).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

PROC = Path(os.environ.get("WATCHER_PROC", "/proc"))
CGROUP = Path(os.environ.get("WATCHER_CGROUP", "/sys/fs/cgroup"))
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
CONTAINER_RE = re.compile(r"docker-([0-9a-f]{64})\.scope")


def read_meminfo(text: str | None = None) -> dict[str, int]:
    text = text if text is not None else (PROC / "meminfo").read_text()
    out = {}
    for line in text.splitlines():
        k, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[k] = int(parts[0]) * (1024 if len(parts) > 1 else 1)
    return out


def memory_numbers(mi: dict[str, int]) -> dict:
    """Linux: 'used' excludes reclaimable cache (uses MemAvailable), unlike naive total-free."""
    total = mi.get("MemTotal", 0)
    avail = mi.get("MemAvailable", mi.get("MemFree", 0))
    used = max(total - avail, 0)
    swap_total = mi.get("SwapTotal", 0)
    swap_used = max(swap_total - mi.get("SwapFree", 0), 0)
    return {
        "mem_total": total, "mem_used": used, "mem_avail": avail,
        "mem_pct": round(100.0 * used / total, 1) if total else 0.0,
        "cache": mi.get("Cached", 0) + mi.get("Buffers", 0),
        "swap_total": swap_total, "swap_used": swap_used,
    }


def parse_cpu_line(text: str | None = None) -> tuple[int, int]:
    """Returns (busy_ticks, total_ticks). iowait counts as idle."""
    text = text if text is not None else (PROC / "stat").read_text()
    for line in text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)
            total = sum(v[:8]) if len(v) >= 8 else sum(v)
            return total - idle, total
    return 0, 0


def parse_stat_line(raw: str):
    """Parse /proc/<pid>/stat safely (comm may contain spaces and parens)."""
    lp, rp = raw.find("("), raw.rfind(")")
    if lp < 0 or rp < 0:
        return None
    comm = raw[lp + 1:rp]
    rest = raw[rp + 2:].split()
    if len(rest) < 22:
        return None
    return {"comm": comm, "state": rest[0], "ppid": int(rest[1]),
            "ticks": int(rest[11]) + int(rest[12]), "rss": int(rest[21]) * PAGE}


class LinuxCollector:
    def __init__(self, data_root: str = "/"):
        self.data_root = data_root
        self._last_cpu: tuple[int, int] | None = None
        self._last_proc: dict[int, int] = {}
        self._last_proc_ts = 0.0
        self._last_cg: dict[str, tuple[float, int]] = {}
        self._last_disk: tuple[float, int, int, int] | None = None

    # ---- host ---------------------------------------------------------
    def host(self) -> dict:
        busy, total = parse_cpu_line()
        cpu = 0.0
        if self._last_cpu and total > self._last_cpu[1]:
            cpu = 100.0 * (busy - self._last_cpu[0]) / (total - self._last_cpu[1])
        self._last_cpu = (busy, total)
        l1, l5, l15 = os.getloadavg()
        mem = memory_numbers(read_meminfo())
        st = os.statvfs(self.data_root)
        disk_total = st.f_blocks * st.f_frsize
        disk_free = st.f_bavail * st.f_frsize
        out = {"cpu": round(max(0.0, min(cpu, 100.0)), 1), "load1": l1, "load5": l5, "load15": l15,
               "disk_total": disk_total, "disk_free": disk_free, **mem}
        extra = {"cores": os.cpu_count(), "cache": mem["cache"], "mem_avail": mem["mem_avail"]}
        try:
            extra["uptime"] = float((PROC / "uptime").read_text().split()[0])
        except OSError:
            pass
        try:
            psi = (PROC / "pressure" / "memory").read_text().splitlines()[0]
            m = re.search(r"avg60=([\d.]+)", psi)
            if m:
                extra["psi_mem60"] = float(m.group(1))
        except OSError:
            pass
        extra.update(self._disk_io())
        out["extra"] = extra
        return out

    def _disk_io(self) -> dict:
        """Aggregate whole-disk read/write throughput from /proc/diskstats (physical disks only)."""
        try:
            rd = wr = ticks = 0
            for line in (PROC / "diskstats").read_text().splitlines():
                f = line.split()
                name = f[2]
                if re.match(r"^(sd[a-z]+|vd[a-z]+|nvme\d+n\d+|xvd[a-z]+)$", name):
                    rd += int(f[5]); wr += int(f[9]); ticks += int(f[12])
            now = time.time()
            res = {}
            if self._last_disk:
                dt = now - self._last_disk[0]
                if dt > 0:
                    res = {"io_read_mbs": round((rd - self._last_disk[1]) * 512 / dt / 1e6, 2),
                           "io_write_mbs": round((wr - self._last_disk[2]) * 512 / dt / 1e6, 2),
                           "io_util": round(min(100.0, (ticks - self._last_disk[3]) / dt / 10.0), 1)}
            self._last_disk = (now, rd, wr, ticks)
            return res
        except (OSError, ValueError, IndexError):
            return {}

    # ---- processes ------------------------------------------------------
    def processes(self, top: int = 10) -> dict:
        now = time.time()
        elapsed = now - self._last_proc_ts if self._last_proc_ts else 0
        cur: dict[int, int] = {}
        rows = []
        for entry in os.scandir(PROC):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                info = parse_stat_line((PROC / entry.name / "stat").read_text())
            except (OSError, ValueError):
                continue
            if not info:
                continue
            cur[pid] = info["ticks"]
            cpu = 0.0
            if elapsed > 0 and pid in self._last_proc:
                cpu = 100.0 * (info["ticks"] - self._last_proc[pid]) / CLK_TCK / elapsed
            rows.append({"pid": pid, "name": info["comm"], "cpu": round(cpu, 1), "rss": info["rss"]})
        self._last_proc, self._last_proc_ts = cur, now
        by_cpu = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:top]
        by_mem = sorted(rows, key=lambda r: r["rss"], reverse=True)[:top]
        for r in {id(x): x for x in by_cpu + by_mem}.values():
            r["container"] = self.container_of(r["pid"])
        return {"top_cpu": by_cpu, "top_mem": by_mem, "count": len(rows)}

    def container_of(self, pid: int) -> str | None:
        try:
            m = CONTAINER_RE.search((PROC / str(pid) / "cgroup").read_text())
            return m.group(1) if m else None
        except OSError:
            return None

    # ---- container cgroup stats ----------------------------------------
    def container_stats(self, ids: list[str]) -> dict[str, dict]:
        """CPU % (of one core) and memory working-set per container, from cgroup v2 files."""
        now = time.time()
        out: dict[str, dict] = {}
        for cid in ids:
            base = CGROUP / "system.slice" / f"docker-{cid}.scope"
            try:
                usage = None
                for line in (base / "cpu.stat").read_text().splitlines():
                    if line.startswith("usage_usec"):
                        usage = int(line.split()[1])
                        break
                mem = int((base / "memory.current").read_text())
                inactive = 0
                try:
                    for line in (base / "memory.stat").read_text().splitlines():
                        if line.startswith("inactive_file "):
                            inactive = int(line.split()[1])
                            break
                except OSError:
                    pass
                swap = 0
                try:
                    swap = int((base / "memory.swap.current").read_text())
                except OSError:
                    pass
            except (OSError, ValueError):
                continue
            cpu = 0.0
            prev = self._last_cg.get(cid)
            if prev and usage is not None and now > prev[0]:
                cpu = 100.0 * (usage - prev[1]) / 1e6 / (now - prev[0])
            if usage is not None:
                self._last_cg[cid] = (now, usage)
            out[cid] = {"cpu": round(max(cpu, 0.0), 1), "mem": max(mem - inactive, 0), "swap": swap}
        for gone in set(self._last_cg) - set(ids):
            self._last_cg.pop(gone, None)
        return out

    def self_stats(self) -> dict:
        """RSS and CPU of this very process, for the 'monitor the monitor' rule."""
        pid = os.getpid()
        info = parse_stat_line((PROC / str(pid) / "stat").read_text())
        return {"rss": info["rss"] if info else 0, "ticks": info["ticks"] if info else 0, "ts": time.time()}
