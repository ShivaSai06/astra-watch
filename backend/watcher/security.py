"""Authentication, CSRF and rate limiting.

Three independent trust domains, each with its own credential:
  1. Dashboard (browser)   -> Cloudflare Access JWT (verified here) + CSRF for state changes
  2. Mac agent             -> per-agent bearer token, stored only as a SHA-256 hash
  3. Telegram webhook      -> secret header + authorised user id + authorised private chat id
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.request
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


# ------------------------------------------------------------------ Cloudflare Access


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class CloudflareAccess:
    """Verifies the Cf-Access-Jwt-Assertion header (RS256) against the team's published keys."""

    def __init__(self):
        self._jwks: tuple[float, dict] | None = None

    def _keys(self, team: str) -> dict:
        if self._jwks and time.time() - self._jwks[0] < 3600:
            return self._jwks[1]
        url = f"https://{team}.cloudflareaccess.com/cdn-cgi/access/certs"
        with urllib.request.urlopen(url, timeout=8) as r:  # noqa: S310 - fixed https host
            keys = {k["kid"]: k for k in json.load(r)["keys"]}
        self._jwks = (time.time(), keys)
        return keys

    def verify(self, token: str, team: str, aud: str, owner_email: str = "", keys: dict | None = None,
               now: float | None = None) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        try:
            h, p, s = token.split(".")
            header = json.loads(_b64d(h))
            claims = json.loads(_b64d(p))
        except Exception as e:  # malformed
            raise PermissionError("malformed token") from e
        if header.get("alg") != "RS256":
            raise PermissionError("unsupported algorithm")
        keys = keys if keys is not None else self._keys(team)
        jwk = keys.get(header.get("kid"))
        if not jwk:
            raise PermissionError("unknown signing key")
        pub = rsa.RSAPublicNumbers(int.from_bytes(_b64d(jwk["e"]), "big"),
                                   int.from_bytes(_b64d(jwk["n"]), "big")).public_key()
        try:
            pub.verify(_b64d(s), f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except Exception as e:
            raise PermissionError("bad signature") from e
        t = now or time.time()
        if claims.get("exp", 0) < t or claims.get("nbf", 0) > t + 60:
            raise PermissionError("token expired")
        auds = claims.get("aud")
        auds = [auds] if isinstance(auds, str) else (auds or [])
        if aud not in auds:
            raise PermissionError("wrong audience")
        if claims.get("iss") != f"https://{team}.cloudflareaccess.com":
            raise PermissionError("wrong issuer")
        if owner_email and claims.get("email", "").lower() != owner_email.lower():
            raise PermissionError("not the owner")
        return claims


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


PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-real-ip", "forwarded", "cf-access-jwt-assertion")


def is_direct_loopback(client_host: str | None, headers) -> bool:
    """True only for a request made straight to 127.0.0.1 (e.g. through an SSH port-forward),
    with none of the headers a reverse proxy / Cloudflare would add."""
    if client_host not in ("127.0.0.1", "::1"):
        return False
    return not any(h in headers for h in PROXY_HEADERS)
