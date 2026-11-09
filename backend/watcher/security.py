"""Authentication, CSRF and rate limiting.

Three independent trust domains, each with its own credential:
  1. Dashboard (browser)   -> Tailscale identity (set by `tailscale serve`, checked here) + CSRF for state changes
  2. Mac agent             -> per-agent bearer token, stored only as a SHA-256 hash
  3. Telegram webhook      -> secret header + authorised user id + authorised private chat id
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque

from .config import Paths, write_private

# ------------------------------------------------------------------ rate limiting


class RateLimiter:
    """Sliding-window limiter, in memory (single process, single owner)."""

    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, per_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > per_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 2000:  # bound memory
                for k in [k for k, v in self._hits.items() if not v][:500]:
                    self._hits.pop(k, None)
            return True


# ------------------------------------------------------------------ agent tokens


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AgentRegistry:
    """agents.json: {agent_id: {name, hash, created, revoked}}. Plaintext token is never stored."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self._cache: tuple[float, dict] | None = None

    def _load(self) -> dict:
        p = self.paths.agents
        if not p.exists():
            return {}
        mtime = p.stat().st_mtime
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        data = json.loads(p.read_text())
        self._cache = (mtime, data)
        return data

    def pair(self, name: str = "mac") -> tuple[str, str]:
        """Create a new agent identity. Returns (agent_id, token). Token is shown once by the caller."""
        data = dict(self._load())
        agent_id = "agt_" + secrets.token_hex(4)
        token = f"{agent_id}.{secrets.token_urlsafe(32)}"
        data[agent_id] = {"name": name, "hash": hash_token(token), "created": time.time(), "revoked": False}
        write_private(self.paths.agents, json.dumps(data, indent=2))
        self._cache = None
        return agent_id, token

    def revoke(self, agent_id: str) -> bool:
        data = dict(self._load())
        if agent_id not in data:
            return False
        data[agent_id]["revoked"] = True
        write_private(self.paths.agents, json.dumps(data, indent=2))
        self._cache = None
        return True

    def verify(self, token: str | None) -> str | None:
        """Constant-time check. Returns agent_id or None."""
        if not token or "." not in token or len(token) > 200:
            return None
        agent_id = token.split(".", 1)[0]
        rec = self._load().get(agent_id)
        if not rec or rec.get("revoked"):
            return None
        return agent_id if hmac.compare_digest(rec["hash"], hash_token(token)) else None

    def list(self) -> list[dict]:
        return [{"id": k, "name": v.get("name"), "created": v.get("created"), "revoked": v.get("revoked", False)}
                for k, v in self._load().items()]


# ------------------------------------------------------------------ CSRF


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(cookie: str | None, header: str | None, origin: str | None, host: str | None) -> bool:
    """Double-submit token plus Origin check. Both must pass for browser state-changing calls."""
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        return False
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc != (host or ""):
            return False
    return True


# Headers a reverse proxy adds. A request carrying any of them is never treated as a direct local connection.
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "cf-ray")


def tailscale_owner_ok(client_host: str | None, headers, owner_login: str | None) -> bool:
    """True for a request proxied by the local `tailscale serve` on behalf of the configured owner.

    tailscaled sets Tailscale-User-Login itself (strips any client-supplied copy) after authenticating the
    WireGuard peer. Funnel (public internet) requests never carry it, and are rejected explicitly anyway."""
    if not owner_login or client_host not in ("127.0.0.1", "::1"):
        return False
    if "tailscale-funnel-request" in headers or any(h in headers for h in ("cf-connecting-ip", "cf-ray")):
        return False
    login = (headers.get("tailscale-user-login") or "").strip().lower()
    return bool(login) and hmac.compare_digest(login.encode(), owner_login.strip().lower().encode())


def is_direct_loopback(client_host: str | None, headers) -> bool:
    """True only for a request made straight to 127.0.0.1 (e.g. through an SSH port-forward),
    with none of the headers a reverse proxy would add."""
    if client_host not in ("127.0.0.1", "::1"):
        return False
    return not any(h in headers for h in PROXY_HEADERS)
