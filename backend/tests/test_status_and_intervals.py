"""Device status (Connected / Stale / Offline), dashboard-vs-collection intervals and Tailscale status."""
import time

import pytest

from test_security_api import agent_payload, client, csrf_headers  # noqa: F401 (client is a fixture)
from watcher import main
from watcher.config import save_env_file

TS = {"x-forwarded-for": "100.64.0.9", "tailscale-user-login": "me@example.com"}


def post(c, **over):
    return c.post("/agent/v1/metrics", json=agent_payload(**over), headers={"Authorization": "Bearer " + c.token})


def age_mac(c, seconds):
    c.st.db.execute("UPDATE samples SET ts=ts-? WHERE machine='mac'", (seconds,))
    c.st.hub.last_seen = time.time() - seconds


def mac(c):
    return c.get("/api/overview").json()["mac"]


# ---------------------------------------------------------------- connection states
@pytest.mark.parametrize("age,state", [(0, "connected"), (60, "connected"), (130, "stale"), (600, "stale"),
                                       (14 * 60, "stale"), (16 * 60, "offline"), (6 * 3600, "offline")])
def test_connection_state_thresholds(client, age, state):
    post(client)
    age_mac(client, age)
    m = mac(client)
    assert m["connection"] == state and m["fresh"] == (state == "connected")
    assert m["health"] == (state if state != "connected" else "healthy")        # stale data is never "healthy"


def test_connection_state_function():
    assert main.connection_state(None, 120, 900) == "unknown"
    assert main.connection_state(119, 120, 900) == "connected"
    assert main.connection_state(120, 120, 900) == "stale"
    assert main.connection_state(900, 120, 900) == "offline"


def test_offline_keeps_last_values_and_last_seen(client):
    post(client)
    age_mac(client, 3 * 3600)
    m = mac(client)
    assert m["health"] == "offline" and m["mem_pct"] == 62.5 and m["cpu"] == 20      # last reported values preserved
    assert m["last_seen"] and m["age_seconds"] > 3 * 3600 - 5


def test_mac_wake_reuses_same_pairing_and_returns_to_connected(client):
    post(client)
    age_mac(client, 3 * 3600)
    assert mac(client)["health"] == "offline"
    before = client.st.db.one("SELECT COUNT(*) c FROM samples WHERE machine='mac'")["c"]
    assert post(client).status_code == 200                                          # same token = existing pairing
    m = mac(client)
    assert m["connection"] == "connected" and m["fresh"] and m["health"] == "healthy"
    assert client.st.db.one("SELECT COUNT(*) c FROM samples WHERE machine='mac'")["c"] == before + 1   # history kept
    assert [a["revoked"] for a in client.st.agents.list()] == [False]


def test_hetzner_status_does_not_depend_on_mac(client):
    from conftest import add_sample
    add_sample(client.st.db, "hetzner", time.time())
    post(client)
    age_mac(client, 6 * 3600)
    ov = client.get("/api/overview").json()
    assert ov["mac"]["health"] == "offline" and ov["hetzner"]["connection"] == "connected"


# ---------------------------------------------------------------- browser refresh is read-only
def test_overview_does_not_collect_or_create_incidents(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("browser request triggered collection")
    for name in ("host", "processes", "containers"):
        if hasattr(client.st.collector, name):
            monkeypatch.setattr(client.st.collector, name, boom)
    monkeypatch.setattr(main, "collect_hetzner_once", boom)
    post(client, sample=agent_payload()["sample"] | {"mem_pct": 99})
    for _ in range(20):                                   # a browser refreshing over and over
        for url in ("/api/overview", "/api/machine/mac", "/api/machine/hetzner", "/api/alerts", "/api/settings"):
            assert client.get(url).status_code == 200
    assert client.st.db.one("SELECT COUNT(*) c FROM incidents")["c"] == 0
    assert client.st.db.one("SELECT COUNT(*) c FROM notifications")["c"] == 0
    assert client.st.tg.sent == []


def test_intervals_are_separate_settings(client):
    s = client.get("/api/settings").json()["settings"]
    assert s["dashboard_refresh_seconds"] == 30 and s["refresh_interval"] == 30
    assert client.get("/api/overview").json()["dashboard_refresh_seconds"] == 30
    h = csrf_headers(client)
    assert client.put("/api/settings", json={"dashboard_refresh_seconds": 60}, headers=h).status_code == 200
    assert client.get("/api/overview").json()["dashboard_refresh_seconds"] == 60
    assert client.st.settings.get()["refresh_interval"] == 30                          # collection untouched
    assert client.put("/api/settings", json={"dashboard_refresh_seconds": 1}, headers=h).status_code == 422
    r = post(client)
    assert r.json()["interval"] == 30                                                  # agent follows collection interval only


# ---------------------------------------------------------------- Tailscale status / auth
def test_remote_access_status_reports_tailscale_and_no_cloudflare(client):
    save_env_file(client.st.paths.secrets, client.st.secrets.all() | {"TAILSCALE_OWNER_LOGIN": "me@example.com"})
    r = client.get("/api/settings", headers={**TS, "x-forwarded-proto": "https"}).json()
    ra = r["remote_access"]
    assert ra["method"] == "Tailscale" and ra["connected"] and ra["https"] and ra["owner_restricted"] and not ra["funnel"]
    assert ra["access"] == "Private tailnet"
    assert "cloudflare_access" not in r and "cloudflare" not in str(r).lower()


def test_tailscale_owner_only_and_funnel_blocked(client):
    save_env_file(client.st.paths.secrets, client.st.secrets.all() | {"TAILSCALE_OWNER_LOGIN": "me@example.com"})
    assert client.get("/api/overview", headers=TS).status_code == 200
    assert client.get("/api/overview", headers={**TS, "tailscale-user-login": "eve@example.com"}).status_code == 403
    assert client.get("/api/overview", headers={**TS, "tailscale-funnel-request": "?1"}).status_code == 403
    assert client.get("/api/overview", headers={"x-forwarded-for": "100.64.0.9"}).status_code == 403
    # state-changing calls still need CSRF even for the owner
    assert client.put("/api/settings", json={"cooldown_minutes": 5}, headers=TS).status_code == 403


def test_tailscale_header_ignored_from_non_loopback_peer(client):
    from watcher import security
    assert not security.tailscale_owner_ok("203.0.113.5", {"tailscale-user-login": "me@example.com"}, "me@example.com")
