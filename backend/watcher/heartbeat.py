"""Outbound heartbeat for an external watchdog (dead-man's switch).

Astra Watch runs on the server it watches, so it cannot report its own death. Instead it pings a hosted
heartbeat URL (for example a Healthchecks.io check) while it is genuinely working; if the pings stop, the
hosted service sends the alert from outside the server.

  * Disabled until HEARTBEAT_URL is set in secrets.env (`watcherctl set-heartbeat`).
  * Outbound HTTPS only. No inbound port, nothing is exposed.
  * Pings only when the latest server sample is fresh and collection is not failing, so "process is
    running" is never mistaken for "server is healthy".
  * The URL is a secret: never logged, never returned by the API.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

MIN_INTERVAL = 60


def valid_url(url: str) -> bool:
    return url.startswith("https://") and len(url) <= 300 and " " not in url


class Heartbeat:
    def __init__(self, secrets, engine, interval_fn):
        self.secrets, self.engine, self.interval_fn = secrets, engine, interval_fn
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self._last_try = 0.0

    def configured(self) -> bool:
        return valid_url(self.secrets.get("HEARTBEAT_URL"))

    def healthy(self, now: float) -> bool:
        s = self.engine.latest("hetzner")
        fresh = bool(s) and now - s["ts"] < max(120, 3 * self.interval_fn())
        return fresh and self.engine.collector_errors.get("hetzner", 0) == 0

    def tick(self, now: float | None = None) -> bool:
        """Ping if due and healthy. Returns True if a ping was sent successfully."""
        now = now or time.time()
        if not self.configured() or now - self._last_try < MIN_INTERVAL or not self.healthy(now):
            return False
        self._last_try = now
        url = self.secrets.get("HEARTBEAT_URL")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="GET", headers={"User-Agent": "astra-watch"}),
                                        timeout=8) as r:  # noqa: S310 - https enforced by valid_url
                r.read(64)
            self.last_ok, self.last_error = now, None
            return True
        except Exception as e:  # never include the URL (it is a secret)
            self.last_error = type(e).__name__
            return False

    def status(self) -> dict:
        return {"configured": self.configured(), "last_ok": self.last_ok, "last_error": self.last_error}
