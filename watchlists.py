"""Watchlists in Supabase Postgres (server-side DATABASE_URL; no Supabase key ever reaches the browser).

The schema is created idempotently at boot and lists are stored in a stable order (position), so a run can
resolve its stocks server-side from a watchlist id alone. The retired `universe.json` contents are seeded once
as Watchlist 1: a marker row plus the "no lists yet" check mean a redeploy can never re-import or overwrite
user edits.
"""
import logging
import re
import threading
from urllib.parse import quote

log = logging.getLogger("watchlists")

# scheme://user:password@host:port/database — the last '@' before '/', '/?' or end, so an unescaped '@' or '?'
# inside the password (a common copy/paste) still yields a valid conninfo.
_DSN = re.compile(r"^\s*([a-z][a-z0-9+.]*)://([^:@/?\s]*):(.*)@([^/?#\s]+)(/[^\s]*)?$", re.I | re.S)


def dsn_uri(value, default_sslmode="require"):
    """Normalise SUPABASE_DATABASE_URL into a libpq URI: the password is URL-escaped in place and TLS is
    required unless the query already says otherwise. A value without a password is returned unchanged, so
    a broken setting fails visibly instead of being patched with a guess."""
    m = _DSN.match(value or "")
    if not m or not m.group(3):
        return (value or "").strip()
    scheme, user, pwd, host, rest = m.groups()
    return f"{scheme}://{user}:{quote(pwd, safe='')}@{host}{rest or '/' + user}?sslmode={default_sslmode}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlists(id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS watchlist_items(id BIGSERIAL PRIMARY KEY, watchlist_id BIGINT NOT NULL
    REFERENCES watchlists(id) ON DELETE CASCADE, provider_symbol TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT NOT NULL,
  exchange TEXT NOT NULL DEFAULT 'NSE', asset_type TEXT, position INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(watchlist_id, provider_symbol));
CREATE INDEX IF NOT EXISTS ix_watchlist_items_list ON watchlist_items(watchlist_id, position, id);
CREATE TABLE IF NOT EXISTS app_migrations(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""

# Tables created by an earlier (pre-rename) version lack `position`; that shape was never released with
# data anyone depends on beyond the same universe import, so columns are added back cheaply (IF NOT EXISTS
# semantics via catalog check) instead of dropping anything.
MIGRATIONS = [
    ("add_watchlists_position",
     "ALTER TABLE watchlists ADD COLUMN IF NOT EXISTS position INTEGER NOT NULL DEFAULT 0"),
    ("backfill_watchlists_position",
     "WITH o AS (SELECT id, ROW_NUMBER() OVER (ORDER BY id) - 1 AS p FROM watchlists) "
     "UPDATE watchlists SET position = o.p FROM o WHERE watchlists.id = o.id AND watchlists.position = 0"),
    ("add_watchlist_items_sector_cap_segment",
     "ALTER TABLE watchlist_items ADD COLUMN IF NOT EXISTS sector TEXT, ADD COLUMN IF NOT EXISTS cap_segment TEXT"),
    ("backfill_watchlist_items_nse_defaults",
     "UPDATE watchlist_items SET exchange = 'NSE', asset_type = 'EQUITY' WHERE exchange IS NULL"),
]

# The retired universe.json, verbatim: (bucket, symbol, name).
UNIVERSE_SEED = [
    ("Large", "RELIANCE", "Reliance Industries"), ("Large", "TCS", "Tata Consultancy Services"),
    ("Large", "HDFCBANK", "HDFC Bank"), ("Large", "INFY", "Infosys"),
    ("Large", "ICICIBANK", "ICICI Bank"), ("Large", "SBIN", "State Bank of India"),
    ("Large", "ITC", "ITC Limited"), ("Large", "LT", "Larsen & Toubro"),
    ("Mid", "POLYCAB", "Polycab India"), ("Mid", "MPHASIS", "Mphasis"), ("Mid", "VOLTAS", "Voltas"),
    ("Mid", "CUMMINSIND", "Cummins India"), ("Mid", "PERSISTENT", "Persistent Systems"),
    ("Mid", "COFORGE", "Coforge"), ("Mid", "ASHOKLEY", "Ashok Leyland"), ("Mid", "BHARATFORG", "Bharat Forge"),
    ("Small", "IRCON", "Ircon International"), ("Small", "NBCC", "NBCC India"), ("Small", "HFCL", "HFCL"),
    ("Small", "RAILTEL", "RailTel Corporation"), ("Small", "GRSE", "Garden Reach Shipbuilders"),
    ("Small", "CDSL", "Central Depository Services"), ("Small", "ITI", "ITI Limited"),
    ("Small", "TITAGARH", "Titagarh Rail Systems"),
]
SEED_MIGRATION = "seed_universe_watchlist_1"

_LIST_SQL = """SELECT l.id, l.name, l.created_at, l.updated_at,
  COALESCE(json_agg(json_build_object('provider_symbol', i.provider_symbol, 'symbol', i.symbol, 'name', i.name,
    'exchange', i.exchange, 'asset_type', i.asset_type) ORDER BY i.position, i.id)
    FILTER (WHERE i.id IS NOT NULL), '[]') AS items
  FROM watchlists l LEFT JOIN watchlist_items i ON i.watchlist_id = l.id"""


class WatchlistError(Exception):
    """kind: unavailable · empty · too_many · not_found · duplicate · last_list"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class WatchlistStore:
    def __init__(self, dsn, max_items=50):
        self.dsn, self.max_items = dsn, max_items
        self.lock = threading.Lock()
        self.pool = None  # one shared connection: the app runs single-process (WEB_CONCURRENCY=1)
        self.error = None
        self._seeded = False

    # ---------- connection / schema ----------
    def connect(self):
        """Open the shared connection and apply the schema. Never raises: a DB outage is reported, not fatal."""
        with self.lock:
            if self.pool is not None:
                return
            try:
                import psycopg
                self.pool = psycopg.connect(dsn_uri(self.dsn), autocommit=True, connect_timeout=10)
                self.pool.execute(SCHEMA)
                for name, sql in MIGRATIONS:  # each recorded step runs at most once
                    if self.pool.execute("SELECT 1 FROM app_migrations WHERE name=%s", (name,)).fetchone():
                        continue
                    self.pool.execute(sql)
                    self.pool.execute("INSERT INTO app_migrations(name) VALUES(%s)", (name,))
                self.error = None
                log.info("watchlist store ready")
            except Exception as e:  # bad DSN, DNS, auth, network, driver missing
                self.pool, self.error = None, type(e).__name__
                log.warning("watchlist store unavailable (%s)", type(e).__name__)

    def _sql(self):
        if self.pool is None:
            self.connect()
        if self.pool is None:
            raise WatchlistError("unavailable", "Watchlist storage (Supabase) is unreachable. Check SUPABASE_DATABASE_URL.")
        return self.pool

    # ---------- one-time universe import ----------
    def seed(self):
        """Import the retired universe as Watchlist 1 exactly once, only into an empty database."""
        if self._seeded:
            return
        if self._sql().execute("SELECT 1 FROM app_migrations WHERE name=%s", (SEED_MIGRATION,)).fetchone():
            self._seeded = True
            return
        cur = self.pool.cursor()
        with cur:
            with cur.connection.transaction():
                cur.execute("SELECT 1 FROM watchlists LIMIT 1")
                if cur.fetchone() is None:
                    cur.execute("INSERT INTO watchlists(name, position) VALUES('Watchlist 1', 0) RETURNING id")
                    wid = cur.fetchone()[0]
                    cur.executemany(
                        "INSERT INTO watchlist_items(watchlist_id, provider_symbol, symbol, name, position) "
                        "VALUES(%s,%s,%s,%s,%s)",
                        [(wid, f"NSE:{s}-EQ", s, n, p) for p, (_, s, n) in enumerate(UNIVERSE_SEED)])
                cur.execute("INSERT INTO app_migrations(name) VALUES(%s)", (SEED_MIGRATION,))
        self._seeded = True

    # ---------- reads ----------
    @staticmethod
    def _lid(list_id):
        """Pass a list id through untouched: the column is BIGINT in this repo's schema but UUID in the
        pre-release table already holding the user's data — casting would destroy the value."""
        return str(list_id)

    def lists(self):
        """Every list with its items, in display order, plus its stock count."""
        self.seed()
        rows = self._sql().execute(_LIST_SQL + " GROUP BY l.id ORDER BY l.position, l.id").fetchall()
        return [{"id": str(r[0]), "name": r[1], "count": len(r[4]), "created_at": r[2].isoformat(),
                 "updated_at": r[3].isoformat(), "items": r[4]} for r in rows]

    def get(self, list_id):
        self.seed()
        r = self._sql().execute(_LIST_SQL + " WHERE l.id=%s GROUP BY l.id", (self._lid(list_id),)).fetchone()
        if r is None:
            raise WatchlistError("not_found", "That watchlist no longer exists.")
        return {"id": str(r[0]), "name": r[1], "count": len(r[4]), "created_at": r[2].isoformat(),
                "updated_at": r[3].isoformat(), "items": r[4]}

    def first_id(self):
        self.seed()
        r = self._sql().execute("SELECT id FROM watchlists ORDER BY position, id LIMIT 1").fetchone()
        return r[0] if r else None

    # ---------- writes ----------
    def create(self, name):
        self.seed()
        r = self._sql().execute("INSERT INTO watchlists(name, position) "
                                "SELECT %s, COALESCE(MAX(position)+1, 0) FROM watchlists RETURNING id", (name,)).fetchone()
        return self.get(r[0])

    def rename(self, list_id, name):
        r = self._sql().execute("UPDATE watchlists SET name=%s, updated_at=now() WHERE id=%s RETURNING id",
                                (name, self._lid(list_id))).fetchone()
        if r is None:
            raise WatchlistError("not_found", "That watchlist no longer exists.")
        return self.get(r[0])

    def delete(self, list_id):
        """Deleting the final list is refused: a run must always have a list to resolve its stocks from."""
        if self._sql().execute("SELECT COUNT(*) FROM watchlists").fetchone()[0] <= 1:
            raise WatchlistError("last_list", "Keep at least one watchlist; rename or empty this one instead.")
        if self._sql().execute("DELETE FROM watchlists WHERE id=%s RETURNING id",
                               (self._lid(list_id),)).fetchone() is None:
            raise WatchlistError("not_found", "That watchlist no longer exists.")

    def add_item(self, list_id, instrument):
        """instrument is a catalogue record; only its canonical fields are stored."""
        if not self._sql().execute("SELECT 1 FROM watchlists WHERE id=%s", (self._lid(list_id),)).fetchone():
            raise WatchlistError("not_found", "That watchlist no longer exists.")
        try:
            with self.pool.transaction():
                self.pool.execute(
                    "INSERT INTO watchlist_items(watchlist_id, provider_symbol, symbol, name, exchange, asset_type, position) "
                    "SELECT %s, %s, %s, %s, %s, %s, COALESCE(MAX(position)+1, 0) FROM watchlist_items WHERE watchlist_id=%s",
                    (self._lid(list_id), instrument["provider_symbol"], instrument["symbol"], instrument["name"],
                     instrument.get("exchange") or "NSE", instrument.get("asset_type"), self._lid(list_id)))
                n = self.pool.execute("SELECT COUNT(*) FROM watchlist_items WHERE watchlist_id=%s",
                                      (self._lid(list_id),)).fetchone()[0]
                if n > self.max_items:  # undo the insert by raising inside the transaction
                    raise WatchlistError("too_many", f"A watchlist holds at most {self.max_items} stocks.")
        except WatchlistError:
            raise
        except Exception as e:
            if type(e).__name__ == "UniqueViolation":
                raise WatchlistError("duplicate", f"{instrument['symbol']} is already in this watchlist.")
            raise WatchlistError("unavailable", f"Could not save the stock ({type(e).__name__}).")
        return self.get(list_id)

    def remove_item(self, list_id, provider_symbol):
        if not self._sql().execute("DELETE FROM watchlist_items WHERE watchlist_id=%s AND provider_symbol=%s RETURNING id",
                                   (self._lid(list_id), provider_symbol)).fetchone():
            raise WatchlistError("not_found", "That stock is not in the watchlist.")
        return self.get(list_id)
