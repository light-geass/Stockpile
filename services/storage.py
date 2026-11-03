"""SQLite audit trail: runs, verdicts, alert deliveries, forward outcomes. Secrets are scrubbed first."""
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
  confidence INT, engine TEXT, fallback_reason TEXT, alert_eligible INT, blocked TEXT, bundle TEXT, result TEXT,
  scores3 TEXT, detector_version TEXT, asset_type TEXT, screened_as TEXT, is_benchmark INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INT, symbol TEXT, dedupe_key TEXT,
  status TEXT, error TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_alert_key ON alerts(dedupe_key, status);
CREATE TABLE IF NOT EXISTS outcomes(verdict_id INT NOT NULL, horizon INT NOT NULL, entry_price REAL,
  next_open REAL, close_px REAL, gap_pct REAL, mfe_pct REAL, mae_pct REAL, ret_pct REAL, hit INT,
  settled_at TEXT, PRIMARY KEY(verdict_id, horizon));
CREATE TABLE IF NOT EXISTS experiments(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, horizon INT,
  model TEXT, n INT, brier REAL, hit_rate REAL, baseline_brier REAL, baseline_hit REAL, shipped INT,
  notes TEXT, metrics TEXT);
CREATE INDEX IF NOT EXISTS ix_verdicts_symbol ON verdicts(symbol, id);
CREATE TABLE IF NOT EXISTS news_seen(fingerprint TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT, hits INT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, updated_at TEXT);
"""
_VERDICT_EXTRAS = (("scores3", "TEXT"), ("detector_version", "TEXT"), ("asset_type", "TEXT"), ("screened_as", "TEXT"),
                   ("is_benchmark", "INT DEFAULT 0"))


def _now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path, extra_secrets=()):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.secrets = list(extra_secrets)
        with self.lock:
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(verdicts)")}
            for name, spec in _VERDICT_EXTRAS:
                if name not in cols:
                    self.db.execute(f"ALTER TABLE verdicts ADD COLUMN {name} {spec}")
            self.db.commit()

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
        chart = bundle.get("chart") or {}
        with self.lock:
            c = self.db.execute(
                "INSERT INTO verdicts(run_id,symbol,verdict,confidence,engine,fallback_reason,alert_eligible,blocked,"
                "bundle,result,scores3,detector_version,asset_type,screened_as,is_benchmark)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, bundle["symbol"], v["verdict"], v["confidence"], result["engine"], result.get("fallback_reason"),
                 int(alert["eligible"]), self._j(alert["blocked_reasons"]), self._j(bundle), self._j(result),
                 self._j(result.get("scores3")), chart.get("detector_version"),
                 bundle.get("asset_type"), bundle.get("screened_as"), int(bool(bundle.get("benchmark")))))
            self.db.commit()
            return c.lastrowid

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

    def kv_get(self, k):
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
            return None if row is None else json.loads(row[0])

    def kv_set(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv(k,v,updated_at) VALUES(?,?,?)", (k, json.dumps(v, default=str), _now()))
            self.db.commit()

    def remember_headline(self, fp, when):
        with self.lock:
            row = self.db.execute("SELECT hits FROM news_seen WHERE fingerprint=?", (fp,)).fetchone()
            if row:
                self.db.execute("UPDATE news_seen SET last_seen=?, hits=hits+1 WHERE fingerprint=?", (when, fp))
                hits = row[0] + 1
            else:
                self.db.execute("INSERT INTO news_seen(fingerprint,first_seen,last_seen,hits) VALUES(?,?,?,1)",
                                (fp, when, when))
                hits = 1
            self.db.commit()
            return hits

    def verdicts_for_settle(self, limit=400, horizons=(1, 5, 20, 60)):
        """Only verdicts that still owe at least one horizon. Fully settled rows cost no provider request,
        so the nightly job does not re-download a year of candles for the whole history every evening."""
        horizons = tuple(horizons)
        sql = (f"SELECT v.id,v.symbol,v.bundle FROM verdicts v WHERE "
               f"(SELECT COUNT(*) FROM outcomes o WHERE o.verdict_id=v.id AND o.horizon IN ({','.join('?' * len(horizons))}))"
               f" < ? ORDER BY v.id DESC LIMIT ?")
        with self.lock:
            rows = self.db.execute(sql, (*horizons, len(horizons), limit)).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]  # fully materialised: the caller writes while iterating

    def benchmark_verdicts(self, symbol, horizons=(1, 5, 20, 60)):
        """Same contract as verdicts_for_settle, for the benchmark instrument only."""
        horizons = tuple(horizons)
        sql = (f"SELECT v.id,v.symbol,v.bundle FROM verdicts v WHERE v.symbol=? AND "
               f"(SELECT COUNT(*) FROM outcomes o WHERE o.verdict_id=v.id AND o.horizon IN ({','.join('?' * len(horizons))}))"
               f" < ? ORDER BY v.id DESC LIMIT 400")
        with self.lock:
            rows = self.db.execute(sql, (symbol, *horizons, len(horizons))).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]

    def outcome_get(self, verdict_id, horizon):
        with self.lock:
            row = self.db.execute("SELECT * FROM outcomes WHERE verdict_id=? AND horizon=?",
                                  (verdict_id, horizon)).fetchone()
            return dict(row) if row else None

    def outcome_put(self, verdict_id, horizon, row):
        cols = ("verdict_id", "horizon", "entry_price", "next_open", "close_px", "gap_pct",
                "mfe_pct", "mae_pct", "ret_pct", "hit", "settled_at")
        row = {"verdict_id": verdict_id, "horizon": horizon, "settled_at": _now(), **row}
        with self.lock:
            self.db.execute(
                f"INSERT OR REPLACE INTO outcomes({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                [row.get(c) for c in cols])
            self.db.commit()

    def outcomes_joined(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT o.*, v.verdict, v.confidence, v.engine, v.symbol, v.screened_as, v.bundle, r.started_at, "
                "substr(COALESCE(r.started_at,''),1,7) AS month, COALESCE(v.is_benchmark,0) AS is_benchmark "
                "FROM outcomes o JOIN verdicts v ON v.id=o.verdict_id LEFT JOIN runs r ON r.id=v.run_id "
                "WHERE o.close_px IS NOT NULL").fetchall()
            return [dict(r) for r in rows]

    def log_experiment(self, horizon, model, n, brier, hit_rate, baseline_brier, baseline_hit, shipped, notes, metrics):
        with self.lock:
            self.db.execute(
                "INSERT INTO experiments(created_at,horizon,model,n,brier,hit_rate,baseline_brier,baseline_hit,shipped,notes,metrics) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), horizon, model, n, brier, hit_rate, baseline_brier, baseline_hit, int(shipped), notes, self._j(metrics)))
            self.db.commit()

    def shipped_models(self):
        """The newest shipped trial per horizon, with its ledger metrics merged in (the pickled model lives inside
        `metrics`). Newest-per-horizon matters: an older shipped row must never shadow a newer retrain."""
        with self.lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT * FROM experiments WHERE shipped=1 ORDER BY id DESC").fetchall()]
        newest, out = set(), []
        for r in rows:
            if r["horizon"] in newest:
                continue
            newest.add(r["horizon"])
            try:
                m = json.loads(r.get("metrics") or "{}")
            except ValueError:
                m = {}
            out.append({**m, "horizon": r["horizon"], "n": r["n"], "brier": r["brier"],
                        "baseline_brier": r["baseline_brier"], "base_rate": m.get("base_rate"),
                        "shipped": True, "trained_at": r.get("created_at")})
        return out
