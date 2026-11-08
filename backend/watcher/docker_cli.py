"""Bounded Docker CLI access for the Hetzner host.

Security model: the ONLY mutating command this module can run is `docker stop -t N <64-hex id>`.
Everything else is read-only inspection. IDs are validated against ^[0-9a-f]{64}$ before use and
commands are executed with an argument vector (never a shell), so nothing from the network can
inject arguments.

Privilege note: membership of the `docker` group is root-equivalent on the host. This process
therefore enforces the allowlist in code; it does not rely on Docker permissions for safety.
"""
from __future__ import annotations

import json
import re
import subprocess

FULL_ID = re.compile(r"^[0-9a-f]{64}$")
DOCKER = "docker"


class DockerError(Exception):
    pass


def valid_id(cid: str) -> bool:
    return bool(isinstance(cid, str) and FULL_ID.match(cid))


def _run(args: list[str], timeout: int = 20) -> str:
    try:
        p = subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise DockerError("docker CLI not found") from e
    except subprocess.TimeoutExpired as e:
        raise DockerError(f"docker {args[0]} timed out") from e
    if p.returncode != 0:
        raise DockerError((p.stderr or p.stdout or "docker error").strip()[:300])
    return p.stdout


def list_ids() -> list[str]:
    out = _run(["ps", "-aq", "--no-trunc"])
    return [x for x in out.split() if valid_id(x)]


def inspect_many(ids: list[str]) -> list[dict]:
    ids = [i for i in ids if valid_id(i)]
    if not ids:
        return []
    return json.loads(_run(["inspect", *ids], timeout=30))


def summarize(c: dict) -> dict:
    """Reduce `docker inspect` JSON to the fields the dashboard needs (no env vars, no cmd)."""
    st = c.get("State", {})
    cfg = c.get("Config", {})
    hc = c.get("HostConfig", {})
    labels = cfg.get("Labels") or {}
    ports = []
    for cport, binds in (c.get("NetworkSettings", {}).get("Ports") or {}).items():
        if binds:
            for b in binds:
                ports.append(f"{b.get('HostIp') or '0.0.0.0'}:{b.get('HostPort')}->{cport}")
        else:
            ports.append(cport)
    mounts = [{"type": m.get("Type"), "name": m.get("Name") or m.get("Source"),
               "dest": m.get("Destination"), "rw": m.get("RW")} for m in c.get("Mounts", [])]
    return {
        "id": c["Id"], "short": c["Id"][:12], "name": c["Name"].lstrip("/"),
        "image": cfg.get("Image"), "image_id": c.get("Image"),
        "status": st.get("Status"), "running": bool(st.get("Running")),
        "health": (st.get("Health") or {}).get("Status", "none"),
        "restarts": c.get("RestartCount", 0), "started": st.get("StartedAt"),
        "exit_code": st.get("ExitCode"), "finished": st.get("FinishedAt"), "oom": bool(st.get("OOMKilled")),
        "restart_policy": (hc.get("RestartPolicy") or {}).get("Name") or "no",
        "mem_limit": hc.get("Memory") or 0,
        "ports": ports, "mounts": mounts,
        "project": labels.get("com.docker.compose.project"),
        "service": labels.get("com.docker.compose.service"),
        "depends_on": labels.get("com.docker.compose.depends_on", ""),
        "networks": sorted((c.get("NetworkSettings", {}).get("Networks") or {}).keys()),
        "log_path": c.get("LogPath"),
    }


def containers() -> list[dict]:
    return [summarize(c) for c in inspect_many(list_ids())]


def stop(cid: str, grace: int = 30) -> str:
    """Graceful stop of exactly one container by full ID. Never `kill`, `rm`, `prune` or `down`."""
    if not valid_id(cid):
        raise DockerError("invalid container id")
    if not 1 <= int(grace) <= 120:
        raise DockerError("invalid grace period")
    return _run(["stop", "-t", str(int(grace)), cid], timeout=int(grace) + 30)


def system_df() -> dict:
    """Image / volume / build-cache storage. Verbose for per-volume sizes; call rarely."""
    raw = _run(["system", "df", "-v", "--format", "{{json .}}"], timeout=90)
    data = json.loads(raw)
    images = data.get("Images") or []
    volumes = data.get("Volumes") or []
    cache = data.get("BuildCache") or []

    def size(x):  # docker prints sizes like "1.2GB"
        return parse_size(x)
    vols = sorted(({"name": v.get("Name"), "size": size(v.get("Size", "0B")), "links": v.get("Links")}
                   for v in volumes), key=lambda v: v["size"], reverse=True)
    return {
        "images_total": sum(size(i.get("Size", "0B")) for i in images),
        "images_count": len(images),
        "volumes_total": sum(v["size"] for v in vols),
        "volumes_count": len(vols),
        "top_volumes": vols[:8],
        "build_cache_total": sum(size(c.get("Size", "0B")) for c in cache),
        "build_cache_count": len(cache),
    }


_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3}


def parse_size(s: str) -> int:
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]+)?\s*$", str(s))
    if not m:
        return 0
    return int(float(m.group(1)) * _UNITS.get((m.group(2) or "B").upper(), 1))


def log_sizes() -> dict[str, int]:
    """Container json-log sizes. /var/lib/docker/containers is root-only, so use a throw-away
    read-only helper container from an image that already exists locally (no pull, no network)."""
    images = _run(["images", "--format", "{{.Repository}}:{{.Tag}}"]).split()
    helper = next((i for i in images if i.startswith("nginx:alpine") or i.startswith("alpine:")), None)
    if not helper:
        raise DockerError("no local alpine/busybox-based image available for log sizing")
    out = _run(["run", "--rm", "--network", "none", "--read-only", "--memory", "32m", "--pids-limit", "20",
                "--cap-drop", "ALL", "-v", "/var/lib/docker/containers:/c:ro", "--entrypoint", "sh",
                helper, "-c", "for f in /c/*/*-json.log; do stat -c '%s %n' \"$f\"; done"], timeout=60)
    res = {}
    for line in out.splitlines():
        size, _, path = line.partition(" ")
        m = re.search(r"/c/([0-9a-f]{64})/", path)
        if m and size.isdigit():
            res[m.group(1)] = int(size)
    return res
