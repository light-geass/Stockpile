"""SQLite audit trail: runs, verdicts, alert deliveries. Everything is scrubbed of secrets before writing."""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

from security import scrub_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, mode TEXT,
  engine TEXT, status TEXT, universe_n INT, shortlisted_n INT, summary TEXT);
CREATE TABLE IF NOT EXISTS verdicts(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INT, symbol TEXT, verdict TEXT,
  confidence INT, engine TEXT, fallback_reason TEXT, alert_eligible INT, blocked TEXT, bundle TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INT, symbol TEXT, dedupe_key TEXT,
  status TEXT, error TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_alert_key ON alerts(dedupe_key, status);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path, extra_secrets=()):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.secrets = list(extra_secrets)
        with self.lock:
            self.db.executescript(SCHEMA)

    def _j(self, o):
        return json.dumps(scrub_obj(o, self.secrets), default=str)

    def start_run(self, mode):
        with self.lock:
            c = self.db.execute("INSERT INTO runs(started_at,mode,status) VALUES(?,?,?)", (_now(), mode, "running"))
            self.db.commit()
            return c.lastrowid

    def finish_run(self, run_id, status, engine, universe_n, shortlisted_n, summary):
        with self.lock:
            self.db.execute("UPDATE runs SET finished_at=?,status=?,engine=?,universe_n=?,shortlisted_n=?,summary=? WHERE id=?",
                            (_now(), status, engine, universe_n, shortlisted_n, self._j(summary), run_id))
            self.db.commit()

    def add_verdict(self, run_id, bundle, result, alert):
        v = result["verdict"]
        with self.lock:
            self.db.execute("INSERT INTO verdicts(run_id,symbol,verdict,confidence,engine,fallback_reason,alert_eligible,blocked,bundle,result)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (run_id, bundle["symbol"], v["verdict"], v["confidence"], result["engine"], result.get("fallback_reason"),
                             int(alert["eligible"]), self._j(alert["blocked_reasons"]), self._j(bundle), self._j(result)))
            self.db.commit()

    @staticmethod
    def dedupe_key(symbol, trade_date, verdict="BUY"):
        return hashlib.sha256(f"{symbol}|{trade_date}|{verdict}".encode()).hexdigest()[:24]

    def already_sent(self, key):
        with self.lock:
            return self.db.execute("SELECT 1 FROM alerts WHERE dedupe_key=? AND status='sent'", (key,)).fetchone() is not None

    def log_alert(self, run_id, symbol, key, status, error=None):
        with self.lock:
            self.db.execute("INSERT INTO alerts(run_id,symbol,dedupe_key,status,error,created_at) VALUES(?,?,?,?,?,?)",
                            (run_id, symbol, key, status, scrub_obj(error, self.secrets), _now()))
            self.db.commit()
