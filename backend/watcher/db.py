"""SQLite storage with strict retention.

Only monitoring history lives here. No secrets, no environment variables, no process arguments.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY, machine TEXT NOT NULL, ts REAL NOT NULL,
  cpu REAL, load1 REAL, load5 REAL, load15 REAL,
  mem_total INTEGER, mem_used INTEGER, mem_pct REAL,
  swap_total INTEGER, swap_used INTEGER,
  disk_total INTEGER, disk_free INTEGER,
  extra TEXT
);
CREATE INDEX IF NOT EXISTS ix_samples ON samples(machine, ts);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY, machine TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(machine, kind, ts);
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, rule TEXT NOT NULL, machine TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL, state TEXT NOT NULL,
  title TEXT NOT NULL, measured TEXT, cause TEXT, confidence TEXT, impact TEXT, recommendation TEXT,
  targets TEXT, first_seen REAL NOT NULL, active_since REAL, last_seen REAL NOT NULL, recovered_at REAL,
  notify_state TEXT NOT NULL DEFAULT 'none', notified_at REAL, notified_severity TEXT,
  notify_attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL, notify_error TEXT,
  recovery_notified INTEGER NOT NULL DEFAULT 0, clear_since REAL
);
CREATE INDEX IF NOT EXISTS ix_inc_state ON incidents(state);
CREATE INDEX IF NOT EXISTS ix_inc_key ON incidents(rule, machine, subject);
CREATE TABLE IF NOT EXISTS pending (
  key TEXT PRIMARY KEY, since REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
  id TEXT PRIMARY KEY, incident_id TEXT, machine TEXT NOT NULL, kind TEXT NOT NULL,
  target TEXT NOT NULL, label TEXT, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, machine TEXT NOT NULL, kind TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT NOT NULL, summary TEXT, source TEXT NOT NULL, actor TEXT,
  created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
  mac TEXT NOT NULL, chat_id TEXT, message_id INTEGER, result TEXT, finished REAL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT, action TEXT NOT NULL,
  machine TEXT, target TEXT, outcome TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit ON audit(ts);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, incident_id TEXT, kind TEXT NOT NULL,
  status TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_notif ON notifications(ts);
CREATE TABLE IF NOT EXISTS incident_events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, incident_id TEXT NOT NULL, machine TEXT, rule TEXT, severity TEXT,
  title TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_ev_inc ON incident_events(incident_id, ts);
CREATE INDEX IF NOT EXISTS ix_ev_ts ON incident_events(ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT, msg TEXT
);
"""

# Raw metrics and short-lived working data: 24 h hard cap (prune()).
RETENTION_COLUMNS = {
    "samples": "ts", "snapshots": "ts", "notifications": "ts", "logs": "ts",
    "offers": "created", "approvals": "created",
}
# Incident history and the action audit trail: 30 days (prune_history()). Kept apart from the metric
# retention above so changing one can never change the other.
HISTORY_RETENTION_DAYS = 30
HISTORY_COLUMNS = {"incident_events": "ts", "audit": "ts"}


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        new = not self.path.exists()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if new:
            # must be set before the first table is created
            self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        try:
            import os
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- kv -------------------------------------------------------------
    def kv_get(self, key: str, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (key,))
        return json.loads(r["v"]) if r else default

    def kv_set(self, key: str, value) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (key, json.dumps(value)))

    # ---- audit / logs ---------------------------------------------------
    def audit(self, actor: str, action: str, machine: str | None = None, target: str | None = None,
              outcome: str | None = None, detail: str | None = None) -> None:
        self.execute("INSERT INTO audit(ts,actor,action,machine,target,outcome,detail) VALUES(?,?,?,?,?,?,?)",
                     (time.time(), actor, action, machine, target, outcome, (detail or "")[:500]))

    def log(self, level: str, msg: str) -> None:
        self.execute("INSERT INTO logs(ts,level,msg) VALUES(?,?,?)", (time.time(), level, msg[:500]))

    # ---- retention ------------------------------------------------------
    def prune(self, hours: float, now: float | None = None) -> dict[str, int]:
        """Delete everything older than `hours` (capped at 24). Returns rows removed per table."""
        hours = min(float(hours), 24.0)
        cutoff = (now or time.time()) - hours * 3600
        removed: dict[str, int] = {}
        with self._lock:
            for table, col in RETENTION_COLUMNS.items():
                removed[table] = self.conn.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)).rowcount
            # incidents: remove once their whole life is older than the cutoff
            removed["incidents"] = self.conn.execute(
                "DELETE FROM incidents WHERE state='recovered' AND COALESCE(recovered_at,last_seen) < ?", (cutoff,)
            ).rowcount
            removed["incidents"] += self.conn.execute(
                "DELETE FROM incidents WHERE last_seen < ?", (cutoff,)).rowcount
            removed["pending"] = self.conn.execute("DELETE FROM pending WHERE since < ?", (cutoff,)).rowcount
            self.conn.execute("PRAGMA incremental_vacuum(2000)")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.kv_set("last_prune", {"ts": time.time(), "hours": hours, "removed": removed})
        return removed

    def prune_history(self, days: float = HISTORY_RETENTION_DAYS, now: float | None = None) -> dict[str, int]:
        """Delete incident events and audit rows older than `days` (capped at 30). Metrics are not touched."""
        days = min(float(days), float(HISTORY_RETENTION_DAYS))
        cutoff = (now or time.time()) - days * 86400
        with self._lock:
            return {t: self.conn.execute(f"DELETE FROM {t} WHERE {c} < ?", (cutoff,)).rowcount
                    for t, c in HISTORY_COLUMNS.items()}

    def event(self, incident_id: str, kind: str, detail: str = "", *, machine=None, rule=None, severity=None,
              title=None, ts: float | None = None) -> None:
        """Append one lifecycle event. Rows carry the incident's name so history outlives the 24 h incident row."""
        if machine is None:
            r = self.one("SELECT machine,rule,severity,title FROM incidents WHERE id=?", (incident_id,))
            if r:
                machine, rule, severity, title = r["machine"], r["rule"], r["severity"], r["title"]
        self.execute("INSERT INTO incident_events(ts,incident_id,machine,rule,severity,title,kind,detail) VALUES(?,?,?,?,?,?,?,?)",
                     (ts or time.time(), incident_id, machine, rule, severity, title, kind, (detail or "")[:500]))

    def oldest_record(self) -> float | None:
        vals = []
        for table, col in RETENTION_COLUMNS.items():
            r = self.one(f"SELECT MIN({col}) AS m FROM {table}")
            if r and r["m"] is not None:
                vals.append(r["m"])
        return min(vals) if vals else None

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def checkpoint_backup(self, dest: Path) -> None:
        """Consistent online copy of the small database."""
        import sqlite3 as _s
        with self._lock:
            out = _s.connect(dest)
            try:
                self.conn.backup(out)
            finally:
                out.close()
