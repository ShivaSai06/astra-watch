"""Authentication, CSRF, rate limiting, API behaviour and Mac agent ingestion."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import FakeTG
from watcher import security
from watcher.config import Paths, save_env_file
from watcher.main import create_app


def test_csrf_double_submit_and_origin():
    assert security.csrf_ok("t", "t", "https://astra.example.ts.net", "astra.example.ts.net")
    assert not security.csrf_ok("t", "x", None, "h") and not security.csrf_ok(None, None, None, "h")
    assert not security.csrf_ok("t", "t", "https://evil.example", "astra.example.ts.net")


def test_rate_limiter():
    rl = security.RateLimiter()
    assert all(rl.allow("k", 3, 60) for _ in range(3)) and not rl.allow("k", 3, 60) and rl.allow("other", 3, 60)


def test_agent_tokens_hashed_revocable(tmp_path):
    p = Paths(str(tmp_path)); p.ensure()
    reg = security.AgentRegistry(p)
    aid, tok = reg.pair("mac")
    assert reg.verify(tok) == aid and reg.verify(tok + "x") is None and reg.verify(None) is None
    assert tok not in p.agents.read_text()                                          # only the hash is stored
    assert oct(p.agents.stat().st_mode & 0o777) == "0o600"
    reg.revoke(aid)
    assert reg.verify(tok) is None


@pytest.fixture
def client(tmp_path):
    p = Paths(str(tmp_path)); p.ensure()
    save_env_file(p.secrets, {"TELEGRAM_BOT_TOKEN": "1:T", "TELEGRAM_CHAT_ID": "555", "TELEGRAM_USER_ID": "555",
                              "TELEGRAM_WEBHOOK_SECRET": "whsec"})
    app = create_app(p)
    st = app.state.st
    st.tg = FakeTG(st.secrets); st.notifier.tg = st.tg
    aid, token = st.agents.pair("mac")
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        c.st, c.token = st, token
        yield c


def csrf_headers(c):
    tok = c.get("/api/session").json()["csrf"]
    return {"X-CSRF-Token": tok}


def agent_payload(**over):
    p = {"v": 1, "ts": time.time(), "host": "mac", "version": "1", "sample": {"cpu": 20, "mem_total": 8_000_000_000, "mem_used": 5_000_000_000,
         "mem_pct": 62.5, "swap_total": 2_000_000_000, "swap_used": 500_000_000, "disk_total": 245_000_000_000, "disk_free": 50_000_000_000,
         "extra": {"pressure_level": 1}}}
    p.update(over); return p


def test_healthz_is_public_and_minimal(client):
    r = client.get("/healthz", headers={"cf-connecting-ip": "1.2.3.4"})
    assert r.status_code == 200 and set(r.json()) == {"status", "version"}


def test_dashboard_fails_closed_through_proxy_without_tailscale_identity(client):
    assert client.get("/api/overview").status_code == 200                           # direct loopback / SSH tunnel
    assert client.get("/api/overview", headers={"cf-connecting-ip": "9.9.9.9"}).status_code == 403
    assert client.get("/api/overview", headers={"x-forwarded-for": "9.9.9.9"}).status_code == 403
    # a forged Tailscale header is useless until an owner login is configured
    assert client.get("/api/overview", headers={"x-forwarded-for": "9.9.9.9", "tailscale-user-login": "me@example.com"}).status_code == 403


def test_state_changing_calls_require_csrf(client):
    assert client.put("/api/settings", json={"cooldown_minutes": 5}).status_code == 403
    assert client.post("/api/stop/prepare", json={"machine": "hetzner", "kind": "container", "id": "a" * 64}).status_code == 403
    h = csrf_headers(client)
    assert client.put("/api/settings", json={"cooldown_minutes": 5}, headers=h).status_code == 200
    assert client.put("/api/settings", json={"cooldown_minutes": 5}, headers={**h, "Origin": "https://evil.example"}).status_code == 403
    assert client.put("/api/settings", json={"refresh_interval": 1}, headers=h).status_code == 422


def test_agent_endpoints_need_a_valid_token(client):
    assert client.post("/agent/v1/metrics", json=agent_payload()).status_code == 401
    assert client.post("/agent/v1/metrics", json=agent_payload(), headers={"Authorization": "Bearer agt_x.bad"}).status_code == 401
    r = client.post("/agent/v1/metrics", json=agent_payload(), headers={"Authorization": "Bearer " + client.token})
    assert r.status_code == 200


def test_agent_payload_validation_and_size(client):
    h = {"Authorization": "Bearer " + client.token}
    bad = agent_payload(); bad["sample"]["cpu"] = 500
    assert client.post("/agent/v1/metrics", json=bad, headers=h).status_code == 422
    assert client.post("/agent/v1/metrics", content=b"x" * 600_000, headers={**h, "content-type": "application/json"}).status_code == 413


def test_mac_data_flows_to_overview_and_goes_offline(client):
    h = {"Authorization": "Bearer " + client.token}
    client.post("/agent/v1/metrics", json=agent_payload(), headers=h)
    ov = client.get("/api/overview").json()["mac"]
    assert ov["connection"] == "connected" and ov["health"] == "healthy" and ov["mem_pct"] == 62.5
    client.st.db.execute("UPDATE samples SET ts=ts-1000"); client.st.hub.last_seen = time.time() - 1000
    assert client.get("/api/overview").json()["mac"]["health"] == "offline"
    assert client.get("/api/overview").json()["hetzner"]["health"] == "unknown"


def test_revoked_agent_is_locked_out(client):
    aid = client.token.split(".")[0]
    client.st.agents.revoke(aid)
    assert client.post("/agent/v1/metrics", json=agent_payload(), headers={"Authorization": "Bearer " + client.token}).status_code == 401


def test_telegram_webhook_requires_secret_and_authorised_sender(client):
    upd = {"update_id": 1, "message": {"from": {"id": 555}, "chat": {"id": 555, "type": "private"}, "text": "/status"}}
    assert client.post("/telegram/webhook", json=upd).status_code == 403
    assert client.post("/telegram/webhook", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    assert client.post("/telegram/webhook", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "whsec", "cf-connecting-ip": "149.154.1.1"}).status_code == 200
    stranger = {"update_id": 2, "callback_query": {"id": "1", "from": {"id": 6}, "data": "c:abc", "message": {"message_id": 1, "chat": {"id": 6, "type": "private"}}}}
    assert client.post("/telegram/webhook", json=stranger, headers={"X-Telegram-Bot-Api-Secret-Token": "whsec"}).status_code == 200
    time.sleep(0.3)
    assert client.st.db.one("SELECT COUNT(*) c FROM audit WHERE action='telegram.rejected'")["c"] >= 1
    assert any("No active problems" in m["text"] for m in client.st.tg.sent)


def test_telegram_test_notification(client):
    h = csrf_headers(client)
    r = client.post("/api/telegram/test", headers=h)
    assert r.json() == {"ok": True} and "Test notification" in client.st.tg.sent[-1]["text"]


def test_no_secrets_in_api_responses(client):
    save_env_file(client.st.paths.secrets, client.st.secrets.all() | {"TAILSCALE_OWNER_LOGIN": "secretowner@example.com"})
    body = json.dumps([client.get(u).json() for u in ("/api/settings", "/api/overview", "/api/alerts", "/api/audit")])
    for secret in ("TESTTOKEN", "1:T", "whsec", client.token, client.token.split(".", 1)[1], "secretowner@example.com"):
        assert secret not in body


def test_history_and_alerts_endpoints(client):
    h = {"Authorization": "Bearer " + client.token}
    for i in range(5):
        client.post("/agent/v1/metrics", json=agent_payload(), headers=h)
    r = client.get("/api/history?machine=mac&range=1h").json()
    assert r["points"] and client.get("/api/history?machine=mac&range=999h").status_code == 400
    assert client.get("/api/alerts").json() == {"alerts": []}
    assert client.get("/api/overview").json()["retention"]["max_hours"] == 24


def test_docs_and_openapi_disabled(client):
    assert client.get("/docs").status_code in (404, 403) and client.get("/openapi.json").status_code in (404, 403)


def test_tailscale_serve_owner_only(client):
    save_env_file(client.st.paths.secrets, client.st.secrets.all() | {"TAILSCALE_OWNER_LOGIN": "Me@Example.com"})
    ok = {"x-forwarded-for": "100.64.0.9", "tailscale-user-login": "me@example.com"}
    assert client.get("/api/overview", headers=ok).status_code == 200
    assert client.get("/api/overview", headers={**ok, "tailscale-user-login": "other@example.com"}).status_code == 403
    assert client.get("/api/overview", headers={"x-forwarded-for": "100.64.0.9"}).status_code == 403          # no identity
    assert client.get("/api/overview", headers={**ok, "tailscale-funnel-request": "?1"}).status_code == 403    # Funnel/public
    assert client.get("/api/overview", headers={**ok, "cf-connecting-ip": "9.9.9.9"}).status_code == 403
