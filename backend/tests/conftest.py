import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["WATCHER_NO_LOOPS"] = "1"
import pytest
from watcher.config import Paths, Secrets, Settings
from watcher.db import DB
from watcher.actions import Actions, MacHub
from watcher.engine import Engine
from watcher.notify import Notifier
from watcher import telegram as tg


class FakeTG(tg.Telegram):
    """Records every message instead of calling api.telegram.org."""
    def __init__(self, secrets):
        super().__init__(secrets)
        self.sent, self.edits, self.answers, self.fail = [], [], [], False
    def call(self, method, payload=None, timeout=15):
        if self.fail:
            raise tg.TelegramError("simulated outage")
        if method == "sendMessage":
            self.sent.append(payload); return {"message_id": len(self.sent)}
        if method == "editMessageText":
            self.edits.append(payload); return {}
        if method == "answerCallbackQuery":
            self.answers.append(payload); return True
        return {}


@pytest.fixture
def env(tmp_path):
    paths = Paths(str(tmp_path)); paths.ensure()
    from watcher.config import save_env_file
    save_env_file(paths.secrets, {"TELEGRAM_BOT_TOKEN": "123:TESTTOKEN", "TELEGRAM_CHAT_ID": "555",
                                  "TELEGRAM_USER_ID": "555", "TELEGRAM_WEBHOOK_SECRET": "whsec"})
    settings, secrets, db = Settings(paths), Secrets(paths), DB(paths.db)
    hub = MacHub()
    actions = Actions(db, lambda: b"k" * 32, hub)
    fake = FakeTG(secrets)
    n = Notifier(db, settings, secrets, fake, actions)
    eng = Engine(db, settings, hub)
    eng.service_probe = lambda port: True
    class E: pass
    e = E(); e.paths, e.settings, e.secrets, e.db, e.hub, e.actions, e.tg, e.notifier, e.engine = paths, settings, secrets, db, hub, actions, fake, n, eng
    return e


def add_sample(db, machine, ts, cpu=10, mem_pct=40, swap_used=0, mem_total=8_000_000_000, disk_free=100e9, disk_total=200e9, extra=None, swap_total=4_000_000_000):
    db.execute("INSERT INTO samples(machine,ts,cpu,load1,load5,load15,mem_total,mem_used,mem_pct,swap_total,swap_used,disk_total,disk_free,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (machine, ts, cpu, 1, 1, 1, mem_total, int(mem_total * mem_pct / 100), mem_pct, swap_total, swap_used, disk_total, disk_free, json.dumps(extra or {})))

def add_snap(db, machine, kind, data, ts=None):
    db.execute("INSERT INTO snapshots(machine,ts,kind,data) VALUES(?,?,?,?)", (machine, ts or time.time(), kind, json.dumps(data)))
