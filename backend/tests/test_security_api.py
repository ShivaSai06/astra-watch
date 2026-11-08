"""Authentication, CSRF, rate limiting, API behaviour and Mac agent ingestion."""
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from conftest import FakeTG
from watcher import security
from watcher.config import Paths, save_env_file
from watcher.main import create_app


def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(key, claims, kid="k1", alg="RS256"):
    h = b64(json.dumps({"alg": alg, "kid": kid}).encode()); p = b64(json.dumps(claims).encode())
    sig = key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{b64(sig)}"


def jwks(key, kid="k1"):
    n = key.public_key().public_numbers()
    return {kid: {"kid": kid, "n": b64(n.n.to_bytes(256, "big")), "e": b64(n.e.to_bytes(3, "big"))}}


def test_cloudflare_access_jwt_verification():
    key = rsa.generate_private_key(65537, 2048)
    cf, now = security.CloudflareAccess(), time.time()
    good = {"aud": ["AUD1"], "iss": "https://me.cloudflareaccess.com", "exp": now + 600, "email": "owner@x.com"}
    assert cf.verify(make_jwt(key, good), "me", "AUD1", "owner@x.com", keys=jwks(key))["email"] == "owner@x.com"
    for bad, why in (({**good, "aud": ["OTHER"]}, "audience"), ({**good, "exp": now - 5}, "expired"),
                     ({**good, "iss": "https://evil.cloudflareaccess.com"}, "issuer"), ({**good, "email": "eve@x.com"}, "owner")):
        with pytest.raises(PermissionError):
            cf.verify(make_jwt(key, bad), "me", "AUD1", "owner@x.com", keys=jwks(key))
    other = rsa.generate_private_key(65537, 2048)
    with pytest.raises(PermissionError):                                           # wrong signing key
        cf.verify(make_jwt(other, good), "me", "AUD1", keys=jwks(key))
    with pytest.raises(PermissionError):                                           # alg=none / HS256 downgrade
        cf.verify(make_jwt(key, good, alg="HS256"), "me", "AUD1", keys=jwks(key))
    with pytest.raises(PermissionError):
        cf.verify("not.a.jwt", "me", "AUD1", keys=jwks(key))


def test_csrf_double_submit_and_origin():
    assert security.csrf_ok("t", "t", "https://monitor.example.com", "monitor.example.com")
    assert not security.csrf_ok("t", "x", None, "h") and not security.csrf_ok(None, None, None, "h")
    assert not security.csrf_ok("t", "t", "https://evil.example", "monitor.example.com")


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


def test_dashboard_fails_closed_through_proxy_when_access_not_configured(client):
    assert client.get("/api/overview").status_code == 200                           # direct loopback / SSH tunnel
    assert client.get("/api/overview", headers={"cf-connecting-ip": "9.9.9.9"}).status_code == 403   # via Cloudflare: refused
    assert client.get("/api/overview", headers={"x-forwarded-for": "9.9.9.9"}).status_code == 403


def test_dashboard_requires_valid_access_token_when_configured(client):
    save_env_file(client.st.paths.secrets, client.st.secrets.all() | {"CF_ACCESS_TEAM_DOMAIN": "me", "CF_ACCESS_AUD": "AUD1"})
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"Cf-Access-Jwt-Assertion": "junk"}).status_code == 403


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


def test_mac_data_flows_to_overview_and_goes_disconnected(client):
    h = {"Authorization": "Bearer " + client.token}
    client.post("/agent/v1/metrics", json=agent_payload(), headers=h)
    ov = client.get("/api/overview").json()["mac"]
    assert ov["connection"] == "connected" and ov["health"] == "healthy" and ov["mem_pct"] == 62.5
    client.st.db.execute("UPDATE samples SET ts=ts-1000"); client.st.hub.last_seen = time.time() - 1000
    assert client.get("/api/overview").json()["mac"]["health"] == "disconnected"
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
    save_env_file(client.st.paths.secrets, client.st.secrets.all() | {"CF_ACCESS_AUD": "SECRETAUD", "CF_ACCESS_TEAM_DOMAIN": "teamx"})
    body = json.dumps([client.get(u).json() for u in ("/api/settings", "/api/overview", "/api/alerts", "/api/audit")])
    for secret in ("TESTTOKEN", "1:T", "whsec", client.token, client.token.split(".", 1)[1], "SECRETAUD"):
        assert secret not in body


def test_history_and_alerts_endpoints(client):
    h = {"Authorization": "Bearer " + client.token}
    for i in range(5):
        client.post("/agent/v1/metrics", json=agent_payload(), headers=h)
    r = client.get("/api/history?machine=mac&range=1h").json()
    assert r["points"] and client.get("/api/history?machine=mac&range=999h").status_code == 400
    assert client.get("/api/alerts").json() == {"alerts": []}
    assert client.get("/api/overview").json()["retention"]["max_hours"] == 72


def test_docs_and_openapi_disabled(client):
    assert client.get("/docs").status_code in (404, 403) and client.get("/openapi.json").status_code in (404, 403)
