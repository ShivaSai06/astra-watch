"""Stop approvals (Telegram + dashboard), identity binding, replay/expiry/tamper protection."""
import asyncio
import json
import time

import pytest

from conftest import add_sample, add_snap
from watcher import docker_cli
from watcher.actions import ActionError

REAL_STOP = docker_cli.stop          # the genuine validator, captured before any monkeypatching
CID = "ab" * 32
USER = {"id": 555}
CHAT = {"id": 555, "type": "private"}


class FakeDocker:
    """Minimal in-memory Docker used instead of the real daemon for logic tests."""
    def __init__(self):
        self.c = {CID: self.make(CID, "disposable-test", "run1")}
        self.stopped, self.down = [], False
    @staticmethod
    def make(cid, name, started, running=True):
        return {"Id": cid, "Name": "/" + name, "Image": "sha256:img1", "RestartCount": 0, "LogPath": "",
                "Config": {"Image": "alpine", "Labels": {"com.docker.compose.project": "proj", "com.docker.compose.service": "web"}},
                "State": {"Status": "running" if running else "exited", "Running": running, "StartedAt": started, "Health": {"Status": "healthy"}},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "Memory": 0},
                "Mounts": [{"Type": "volume", "Name": "vol1", "Destination": "/data", "RW": True}],
                "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}, "Networks": {"n": {}}}}
    def list_ids(self):
        if self.down: raise docker_cli.DockerError("Cannot connect to the Docker daemon")
        return list(self.c)
    def inspect_many(self, ids):
        if self.down: raise docker_cli.DockerError("Cannot connect to the Docker daemon")
        if any(i not in self.c for i in ids): raise docker_cli.DockerError("Error: No such object: " + ids[0])
        return [self.c[i] for i in ids]
    def stop(self, cid, grace=30):
        self.stopped.append(cid); self.c[cid]["State"].update(Running=False, Status="exited")


@pytest.fixture
def dock(monkeypatch):
    d = FakeDocker()
    monkeypatch.setattr(docker_cli, "list_ids", d.list_ids)
    monkeypatch.setattr(docker_cli, "inspect_many", d.inspect_many)
    monkeypatch.setattr(docker_cli, "stop", d.stop)
    return d


def cb(data, user=USER, chat=CHAT, mid=9):
    return {"update_id": 1, "callback_query": {"id": "cbid", "from": user, "data": data, "message": {"message_id": mid, "chat": chat}}}


def make_offer(env, kind="container", ref=None):
    return env.actions.create_offer("inc_x", "hetzner", kind, ref or {"id": CID}, "disposable-test 1.0 GB")


def last_confirm_token(env):
    for m in reversed(env.tg.sent):
        if "reply_markup" in m and "CONFIRM STOP" in m["text"]:
            return m["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]


def test_full_telegram_two_step_flow_stops_and_verifies(env, dock):
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    msg = env.tg.sent[-1]["text"]                                    # step 1: confirmation screen with exact identity
    assert "CONFIRM STOP" in msg and "disposable-test" in msg and CID[:12] in msg and "HETZNER SERVER" in msg
    assert "volume(s) are kept" in msg and dock.stopped == []        # nothing happened yet
    token = last_confirm_token(env)
    asyncio.run(env.notifier.handle_update(cb(f"c:{token}")))          # step 2
    assert dock.stopped == [CID]
    assert "Successfully stopped" in env.tg.edits[-1]["text"]
    assert env.db.one("SELECT outcome FROM audit WHERE action='stop.execute'")["outcome"] == "stopped"
    # replay of the same confirmation must do nothing
    asyncio.run(env.notifier.handle_update(cb(f"c:{token}")))
    assert dock.stopped == [CID] and "already used" in env.tg.edits[-1]["text"]


def test_unauthorised_sender_or_chat_is_ignored(env, dock):
    oid = make_offer(env)
    before = len(env.tg.sent)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}", user={"id": 999})))                    # wrong user
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}", chat={"id": 777, "type": "private"})))  # wrong chat
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}", chat={"id": 555, "type": "group"})))    # not private
    assert len(env.tg.sent) == before and env.db.one("SELECT COUNT(*) c FROM approvals")["c"] == 0
    assert env.db.one("SELECT COUNT(*) c FROM audit WHERE action='telegram.rejected'")["c"] == 3


def test_another_user_cannot_use_my_token(env, dock):
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    token = last_confirm_token(env)
    env.secrets.paths.secrets.write_text(env.secrets.paths.secrets.read_text().replace("555", "556", 2))  # pretend ids changed
    asyncio.run(env.notifier.handle_update(cb(f"c:{token}", user={"id": 556}, chat={"id": 556, "type": "private"})))
    assert dock.stopped == []


def test_confirmation_expires_after_two_minutes(env, dock, monkeypatch):
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    token = last_confirm_token(env)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 121)          # two minutes and a second later
    asyncio.run(env.notifier.handle_update(cb(f"c:{token}")))
    assert dock.stopped == [] and "Approval expired" in env.tg.edits[-1]["text"]


def test_altered_or_forged_callbacks_rejected(env, dock):
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    token = last_confirm_token(env)
    for bad in ("c:" + token[:-1] + "X", "c:" + "A" * 22, "c:", "c:" + "A" * 300, "z:123", "stop all", "c:../etc/passwd; rm -rf /"):
        asyncio.run(env.notifier.handle_update(cb(bad)))
    assert dock.stopped == []
    # database tampering of the bound target is detected by the HMAC
    env.db.execute("UPDATE approvals SET target=? WHERE id=?", (json.dumps({"id": "cd" * 32, "name": "production-db", "image_id": "x", "started": "y"}), token))
    asyncio.run(env.notifier.handle_update(cb(f"c:{token}")))
    assert dock.stopped == [] and "integrity" in env.tg.edits[-1]["text"].lower()


def test_target_changed_since_prompt_rejects(env, dock):
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    token = last_confirm_token(env)
    dock.c[CID]["State"]["StartedAt"] = "run2"                         # container was restarted after the prompt
    asyncio.run(env.notifier.handle_update(cb(f"c:{token}")))
    assert dock.stopped == [] and "Target changed; approval rejected" in env.tg.edits[-1]["text"]


def test_target_vanished(env, dock):
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    token = last_confirm_token(env)
    del dock.c[CID]
    asyncio.run(env.notifier.handle_update(cb(f"c:{token}")))
    assert "Target no longer exists" in env.tg.edits[-1]["text"]


def test_hetzner_unavailable(env, dock):
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    token = last_confirm_token(env)
    dock.down = True
    asyncio.run(env.notifier.handle_update(cb(f"c:{token}")))
    assert "Hetzner unavailable" in env.tg.edits[-1]["text"] and dock.stopped == []


def test_cancel_button(env, dock):
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    token = last_confirm_token(env)
    asyncio.run(env.notifier.handle_update(cb(f"x:{token}")))
    asyncio.run(env.notifier.handle_update(cb(f"c:{token}")))
    assert dock.stopped == []


def test_verification_failure_is_reported_not_hidden(env, dock, monkeypatch):
    monkeypatch.setattr(docker_cli, "stop", lambda cid, grace=30: dock.stopped.append(cid))   # "succeeds" but keeps running
    oid = make_offer(env)
    asyncio.run(env.notifier.handle_update(cb(f"o:{oid}")))
    asyncio.run(env.notifier.handle_update(cb(f"c:{last_confirm_token(env)}")))
    assert "Stop failed" in env.tg.edits[-1]["text"]


def test_invalid_container_id_never_reaches_docker(env, dock):
    with pytest.raises(ActionError):
        asyncio.run(env.actions.prepare("hetzner", "container", {"id": "abc; rm -rf /"}, "dashboard", "owner"))
    with pytest.raises(docker_cli.DockerError):
        REAL_STOP("--all")
    with pytest.raises(docker_cli.DockerError):
        REAL_STOP(CID, grace=9999)
    with pytest.raises(docker_cli.DockerError):
        REAL_STOP(CID[:12])                                          # short ids are refused: full 64-hex only


def test_dashboard_flow_and_dependents_shown(env, dock):
    d2 = FakeDocker.make("cd" * 32, "web2", "r", True)
    d2["Config"]["Labels"] = {"com.docker.compose.project": "proj", "com.docker.compose.service": "api",
                              "com.docker.compose.depends_on": "web:service_started:false"}
    dock.c["cd" * 32] = d2
    appr = asyncio.run(env.actions.prepare("hetzner", "container", {"id": CID}, "dashboard", "owner"))
    assert "web2" in appr["details"]["dependents"] and appr["details"]["volumes"] == ["vol1"]
    assert any("depend on it" in i for i in appr["details"]["impact"])
    res = asyncio.run(env.actions.execute(appr["token"], "owner", None, "dashboard"))
    assert res["status"] == "stopped" and dock.stopped == [CID]
    again = asyncio.run(env.actions.execute(appr["token"], "owner", None, "dashboard"))
    assert again["status"] == "used"


# --------------------------------------------------------------------- Mac
def mac_proc(**kw):
    p = {"pid": 4242, "ppid": 1, "name": "node", "exe": "/usr/local/bin/node", "user": "me", "start": 1700000000, "cpu": 0.0,
         "rss": 900_000_000, "label": "vitest", "eligible": True, "growing": False, "growth": 0, "parent": "", "why": "orphaned"}
    p.update(kw); return p


def test_mac_offline_refuses_and_queues_nothing(env):
    add_snap(env.db, "mac", "procs", {"top_cpu": [], "top_mem": [mac_proc()], "dev": []})
    with pytest.raises(ActionError) as e:
        asyncio.run(env.actions.prepare("mac", "process", {"pid": 4242}, "telegram", "555", "555"))
    assert e.value.code == "mac_offline" and env.hub.commands == {}


def test_mac_protected_process_cannot_be_offered(env):
    env.hub.last_seen = time.time()
    add_snap(env.db, "mac", "procs", {"top_cpu": [], "top_mem": [mac_proc(pid=77, name="WindowServer", eligible=False, exe="/System/Library/x")], "dev": []})
    with pytest.raises(ActionError) as e:
        asyncio.run(env.actions.prepare("mac", "process", {"pid": 77}, "dashboard", "owner"))
    assert e.value.code == "unauthorized"


def fake_agent(env, result):
    """Plays the Mac agent: long-polls, receives the command, reports a result."""
    async def run():
        seen = []
        for _ in range(40):
            cmds = await env.hub.poll(0.5)
            if cmds:
                seen += cmds
                env.hub.finish(cmds[0]["id"], result)
                break
        return seen
    return run()


def test_mac_stop_dispatches_exact_identity_and_reports_result(env):
    env.hub.last_seen = time.time()
    add_snap(env.db, "mac", "procs", {"top_cpu": [], "top_mem": [mac_proc()], "dev": []})

    async def scenario():
        appr = await env.actions.prepare("mac", "process", {"pid": 4242, "start": 1700000000}, "telegram", "555", "555")
        agent = asyncio.create_task(fake_agent(env, {"status": "stopped", "message": "node (PID 4242) exited"}))
        res = await env.actions.execute(appr["token"], "555", "555", "telegram")
        return res, await agent
    res, seen = asyncio.run(scenario())
    assert res["status"] == "stopped"
    t = seen[0]["target"]
    assert seen[0]["type"] == "stop_process" and t["pid"] == 4242 and t["start"] == 1700000000 and t["exe"] == "/usr/local/bin/node"


def test_mac_agent_reports_changed_target(env):
    env.hub.last_seen = time.time()
    add_snap(env.db, "mac", "procs", {"top_cpu": [], "top_mem": [mac_proc()], "dev": []})

    async def scenario():
        appr = await env.actions.prepare("mac", "process", {"pid": 4242}, "dashboard", "owner")
        agent = asyncio.create_task(fake_agent(env, {"status": "changed", "message": "PID reused"}))
        res = await env.actions.execute(appr["token"], "owner", None, "dashboard")
        await agent
        return res
    assert asyncio.run(scenario())["status"] == "changed"


def test_stale_stop_request_is_not_replayed_after_agent_returns(env, monkeypatch):
    import watcher.actions as A
    monkeypatch.setattr(A, "MAC_PICKUP_SECONDS", 1)
    env.hub.last_seen = time.time()
    add_snap(env.db, "mac", "procs", {"top_cpu": [], "top_mem": [mac_proc()], "dev": []})

    async def scenario():
        appr = await env.actions.prepare("mac", "process", {"pid": 4242}, "dashboard", "owner")
        res = await env.actions.execute(appr["token"], "owner", None, "dashboard")   # agent never polls
        late = await env.hub.poll(0.1)                                               # it reconnects afterwards
        return res, late
    res, late = asyncio.run(scenario())
    assert res["status"] == "mac_offline" and late == [] and env.hub.commands == {}
