"""Configuration, secrets and settings.

Three separate things live on disk (never in the monitoring database):
  * secrets.env   (0600)  Telegram token/chat id, webhook secret, Tailscale owner login, approval key
  * agents.json   (0600)  SHA-256 hashes of Mac agent tokens (the token itself is shown once)
  * settings.json         Non-secret, owner-editable thresholds and intervals
"""
from __future__ import annotations

import copy
import json
import os
import stat
import threading
from pathlib import Path

RETENTION_HARD_MAX_HOURS = 24

DEFAULT_SETTINGS: dict = {
    "public_url": "https://astra.example.ts.net",
    # Three different clocks (see docs/monitoring.md):
    "dashboard_refresh_seconds": 30,  # how often the BROWSER re-reads cached data (10-300). Never triggers collection.
    "refresh_interval": 30,          # BACKEND collection + alert evaluation, and how often the Mac agent reports (15-300)
    "persist_seconds": 180,          # a problem must persist this long before notification (60-600)
    "cpu_persist_seconds": 300,      # sustained CPU window
    "recover_seconds": 120,          # must stay clear this long before "recovered"
    "cooldown_minutes": 30,          # no repeat notification for the same problem within this time
    "recovery_notifications": True,
    "retention_hours": 24,           # may only be lowered, never above 24
    "thresholds": {
        "mem_warn": 85, "mem_crit": 95,
        "cpu_warn": 85, "cpu_crit": 95,
        "disk_free_warn": 20, "disk_free_crit": 10,       # percent free
        "swap_growth_mb": 500,                            # growth within 30 min
        "swap_used_crit": 90,                             # percent of swap used
        "restart_count": 3,                               # restarts within 15 min
        "container_cpu": 80,                              # percent of one core... of host share
        "container_mem_host_pct": 40,                     # % of host RAM
        "agent_grace_seconds": 120,                       # no report for this long -> "Stale"
        "agent_notify_minutes": 15,                       # no report for this long -> "Offline" + one notification (sleep tolerant)
        "vitest_workers": 4,
        "node_procs": 12,
        "docker_vm_gb": 3.0,
        "log_size_mb": 500,
        "self_rss_mb": 250,
        "self_cpu": 25,
    },
}

# (min, max) bounds for validation of every editable value
BOUNDS: dict = {
    "dashboard_refresh_seconds": (10, 300),
    "refresh_interval": (15, 300),
    "persist_seconds": (60, 900),
    "cpu_persist_seconds": (60, 1800),
    "recover_seconds": (30, 900),
    "cooldown_minutes": (1, 1440),
    "retention_hours": (1, RETENTION_HARD_MAX_HOURS),
    "mem_warn": (50, 99), "mem_crit": (60, 100),
    "cpu_warn": (30, 100), "cpu_crit": (40, 100),
    "disk_free_warn": (2, 80), "disk_free_crit": (1, 60),
    "swap_growth_mb": (50, 8192),
    "swap_used_crit": (30, 100),
    "restart_count": (2, 50),
    "container_cpu": (20, 400),
    "container_mem_host_pct": (5, 95),
    "agent_grace_seconds": (30, 3600),
    "agent_notify_minutes": (1, 240),
    "vitest_workers": (2, 100),
    "node_procs": (3, 200),
    "docker_vm_gb": (0.5, 64),
    "log_size_mb": (10, 100000),
    "self_rss_mb": (50, 4000),
    "self_cpu": (5, 100),
}


class Paths:
    def __init__(self, base: str | None = None):
        base = base or os.environ.get("WATCHER_HOME") or str(Path.home() / "pi-watcher")
        self.base = Path(base)
        self.config = Path(os.environ.get("WATCHER_CONFIG_DIR", self.base / "config"))
        self.data = Path(os.environ.get("WATCHER_DATA_DIR", self.base / "data"))
        self.static = Path(os.environ.get("WATCHER_STATIC_DIR", self.base / "current" / "static"))
        self.secrets = self.config / "secrets.env"
        self.agents = self.config / "agents.json"
        self.settings = self.config / "settings.json"
        self.automation = self.config / "automation.json"
        self.db = self.data / "watcher.sqlite3"

    def ensure(self) -> None:
        self.config.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config, 0o700)
        os.chmod(self.data, 0o700)


def write_private(path: Path, text: str) -> None:
    """Atomic write with 0600 permissions."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def save_env_file(path: Path, values: dict[str, str]) -> None:
    body = "# Astra Watch secrets. Mode 0600. Never commit.\n"
    body += "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
    write_private(path, body)


class Settings:
    """Thread-safe, validated settings store."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULT_SETTINGS)
        self.reload()

    def reload(self) -> None:
        with self._lock:
            data = copy.deepcopy(DEFAULT_SETTINGS)
            if self.paths.settings.exists():
                try:
                    stored = json.loads(self.paths.settings.read_text())
                    self._merge(data, stored, strict=False)
                except (OSError, ValueError):
                    pass
            self._data = data

    @staticmethod
    def _merge(dst: dict, src: dict, strict: bool) -> None:
        for key, val in src.items():
            if key == "thresholds" and isinstance(val, dict):
                for tk, tv in val.items():
                    if tk not in dst["thresholds"]:
                        if strict:
                            raise ValueError(f"unknown threshold {tk}")
                        continue
                    dst["thresholds"][tk] = Settings._check(tk, tv)
            elif key in dst and key != "thresholds":
                dst[key] = Settings._check(key, val)
            elif strict:
                raise ValueError(f"unknown setting {key}")

    @staticmethod
    def _check(key: str, val):
        if key == "recovery_notifications":
            if not isinstance(val, bool):
                raise ValueError("recovery_notifications must be true/false")
            return val
        if key == "public_url":
            if not isinstance(val, str) or not val.startswith("https://") or len(val) > 200:
                raise ValueError("public_url must be an https:// URL")
            return val.rstrip("/")
        if key in BOUNDS:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"{key} must be a number")
            lo, hi = BOUNDS[key]
            if not lo <= val <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            return val
        raise ValueError(f"unknown setting {key}")

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def thresholds(self) -> dict:
        with self._lock:
            return dict(self._data["thresholds"])

    def update(self, patch: dict) -> dict:
        with self._lock:
            new = copy.deepcopy(self._data)
            self._merge(new, patch, strict=True)
            t = new["thresholds"]
            if t["mem_crit"] <= t["mem_warn"] or t["cpu_crit"] <= t["cpu_warn"]:
                raise ValueError("critical thresholds must be above warning thresholds")
            if t["disk_free_crit"] >= t["disk_free_warn"]:
                raise ValueError("disk critical free-% must be below warning free-%")
            self._data = new
            self.paths.config.mkdir(parents=True, exist_ok=True)
            write_private(self.paths.settings, json.dumps(new, indent=2))
            return copy.deepcopy(new)


class Secrets:
    """Reads secrets.env on demand so `watcherctl` changes apply without restart."""

    def __init__(self, paths: Paths):
        self.paths = paths

    def all(self) -> dict[str, str]:
        env = load_env_file(self.paths.secrets)
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_USER_ID", "TELEGRAM_WEBHOOK_SECRET",
                  "TELEGRAM_MODE", "TAILSCALE_OWNER_LOGIN", "APPROVAL_HMAC_KEY", "HEARTBEAT_URL"):
            if k in os.environ and k not in env:
                env[k] = os.environ[k]
        return env

    def get(self, key: str, default: str = "") -> str:
        return self.all().get(key, default)

    def telegram_configured(self) -> bool:
        s = self.all()
        return bool(s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID") and s.get("TELEGRAM_USER_ID"))

    def hmac_key(self) -> bytes:
        s = self.all()
        key = s.get("APPROVAL_HMAC_KEY")
        if not key:
            import secrets as _s
            key = _s.token_hex(32)
            s["APPROVAL_HMAC_KEY"] = key
            save_env_file(self.paths.secrets, load_env_file(self.paths.secrets) | {"APPROVAL_HMAC_KEY": key})
        return key.encode()
