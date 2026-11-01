"""Watchlists: PostgreSQL store contract, one-time universe.json import, and the CSRF-protected CRUD API.

The store tests run against a small in-memory fake of the psycopg connection/cursor, so migrations, the seed guard,
seeding, CRUD and the "no fallback" error paths are all exercised without a live Supabase project. The API tests use
the app's real routes with the same fake-store contract (tests.conftest.FakeWatchlists)."""
import re

import pytest

import config
import watchlists as wl
from tests.conftest import FakeWatchlists, LOCAL
from tests.test_instrument_search import MASTER

VALID_URL = "postgresql://user:pw@db.example.supabase.co:5432/postgres?sslmode=require"


# ---------------- connection string handling ----------------
def test_bare_project_url_is_reported_as_misconfigured_not_used():
    # The dashboard used to hand the app `https://<project>.supabase.co`, which is not a database URL.
    s = wl.WatchlistStore("https://jrnwkokiowtjggtdufxd.supabase.co")
    assert s.configured() is False and s.status()["error"] == "invalid_url"
    with pytest.raises(wl.WatchlistError) as e:
        s.lists()
    assert e.value.kind == "not_configured"


def test_missing_database_url_is_reported_as_not_configured():
    s = wl.WatchlistStore("")
    assert s.configured() is False and s.status()["error"] == "not_configured"


def test_settings_accept_both_variable_names_and_redact_the_url(mk, monkeypatch):
    for k in ("DATABASE_URL", "SUPABASE_DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SUPABASE_DATABASE_URL", VALID_URL)
    s = config.Settings.from_env()
    assert s.database_url == VALID_URL and s.db_ready is True
    assert VALID_URL in s.secret_values()          # never logged in clear text
    monkeypatch.setenv("DATABASE_URL", VALID_URL + "&x=1")
    assert config.Settings.from_env().database_url == VALID_URL + "&x=1"   # DATABASE_URL wins


# ---------------- store against a fake driver ----------------
class FakeCursor:
    def __init__(self, db):
        self.db, self.result, self.rowcount = db, [], 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.db.log.append((sql, params))
        self.db.run(self, sql, params or ())

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db.commits += 1

    def rollback(self):
        self.db.rollbacks += 1

    def close(self):
        pass


class FakePostgres:
    """Just enough SQL to run watchlists.WatchlistStore: it dispatches on the tables each statement touches."""

    def __init__(self):
        self.log, self.commits, self.rollbacks, self.closed = [], 0, 0, 0
        self.lists, self.items, self.markers = [], [], set()
        self.seeded_gate = True     # advisory lock granted
        self.fail_connect = None

    def connect(self):
        if self.fail_connect:
            raise self.fail_connect
        return FakeConn(self)

    def run(self, cur, sql, p):
        s = " ".join(sql.split()).lower()
        cur.result, cur.rowcount = [], 0
        if s.startswith(("create table", "create index", "create unique index", "alter table", "drop trigger",
                         "create or replace function", "do $$")):
            return
        if "pg_try_advisory" in s:
            cur.result = [(self.seeded_gate,)]; return
        if s.startswith("select 1 from watchlist_migrations"):
            cur.result = [(1,)] if p[0] in self.markers else []; return
        if s.startswith("insert into watchlist_migrations"):
            self.markers.add(p[0]); cur.rowcount = 1; return
        if s.startswith("select count(*) from watchlists"):
            cur.result = [(len(self.lists),)]; return
        if s.startswith("insert into watchlists"):
            self.lists.append((p[0], p[1]))          # id (uuid), name — the store only reads these
            self.items.append([])
            cur.rowcount = 1
            return
        if s.startswith("select id from watchlists order by"):
            cur.result = [(self.lists[0][0],)] if self.lists else []; return
        if s.startswith("select w.id, w.name, count"):
            cur.result = [(l[0], l[1], len(it), None) for l, it in zip(self.lists, self.items)]; return
        if s.startswith("select id, name, created_at"):
            cur.result = [(l[0], l[1], None, None) for l in self.lists if l[0] == p[0]]; return
        if s.startswith("select provider_symbol, symbol"):
            idx = _index(self.lists, p[0])
            cur.result = [(x["provider_symbol"], x["symbol"], x["name"], x["sector"], x["cap_segment"], x["position"])
                          for x in (self.items[idx] if idx is not None else [])]; return
        if s.startswith("select id from watchlists"):
            cur.result = [(l[0],) for l in self.lists if l[0] == p[0]]; return
        if s.startswith("select count(*) from watchlist_items"):
            idx = _index(self.lists, p[0])
            cur.result = [(len(self.items[idx]) if idx is not None else 0,)]; return
        if s.startswith("insert into watchlist_items"):
            idx = _index(self.lists, p[1])
            lst = self.items[idx]
            if any(x["provider_symbol"] == p[2] for x in lst):
                raise Exception('duplicate key value violates unique constraint "watchlist_items_watchlist_id_provider_sym_key"')
            lst.append({"provider_symbol": p[2], "symbol": p[3], "name": p[4], "sector": p[5],
                        "cap_segment": p[6], "position": p[7]})
            cur.rowcount = 1; return
        if s.startswith("update watchlists set name"):
            idx = _index(self.lists, p[1])
            if idx is not None:
                self.lists[idx] = (p[1], p[0]); cur.rowcount = 1
            return
        if s.startswith("update watchlists set updated_at"):
            return
        if s.startswith("delete from watchlists where id"):
            idx = _index(self.lists, p[0])
            if idx is not None:
                del self.lists[idx], self.items[idx]; cur.rowcount = 1
            return
        if s.startswith("delete from watchlist_items"):
            idx = _index(self.lists, p[0])
            lst = self.items[idx] if idx is not None else []
            before = len(lst)
            if idx is not None:
                self.items[idx] = [x for x in lst if x["provider_symbol"] != p[1]]
            cur.rowcount = before - len(self.items[idx] if idx is not None else []); return
        raise AssertionError(f"unexpected SQL: {sql}")


def _index(lists, uid, default=None):
    """The store addresses lists by uid; this fake keys its item lists by position."""
    for i, l in enumerate(lists):
        if l[0] == uid:
            return i
    return default


@pytest.fixture
def pg(monkeypatch):
    fake = FakePostgres()
    store = wl.WatchlistStore(VALID_URL)
    # A module-level factory is patched so every store created in the test (readers/writers alike) hits the fake.
    monkeypatch.setattr(wl, "_open_connection", lambda url, timeout: fake.connect())
    return store, fake


def _list_of(fake, uid):
    return fake.items[_index(fake.lists, uid)]


def test_seed_imports_universe_once_and_never_overwrites(pg):
    store, fake = pg
    # A fresh database: no lists and no migration marker yet.
    assert store.ensure_seeded() == 24
    assert store.ensure_seeded() == 0                    # marker present: never a second import
    assert len(fake.items[0]) == 24 and fake.items[0][0]["symbol"] == "RELIANCE"
    assert fake.lists[0][1] == wl.UNIVERSE_SEED_NAME
    rows = store.lists()
    assert [r["name"] for r in rows] == [wl.UNIVERSE_SEED_NAME] and rows[0]["count"] == 24
    got = store.get(rows[0]["id"])
    assert got["items"][0]["cap_segment"] == "Large"
    assert got["items"][0]["provider_symbol"] == "NSE:RELIANCE-EQ"


def test_seed_is_skipped_when_the_database_already_has_lists(pg):
    store, fake = pg
    fresh = wl.WatchlistStore(VALID_URL)
    uid = store.create("Mine")["id"]                 # a database that already has user lists
    fresh.ensure_seeded()
    assert [l[1] for l in fake.lists] == ["Mine"]    # no duplicate "Watchlist 1"
    assert _list_of(fake, uid) == []
    assert wl._SEED_KEY in fake.markers              # the import is marked done, so it never retries
    assert fresh.ensure_seeded() == 0


def test_seed_backs_off_when_another_worker_holds_the_lock(pg):
    store, fake = pg
    fake.seeded_gate = False
    assert store.ensure_seeded() == 0
    assert fake.lists == []


def test_crud_round_trip(pg):
    store, fake = pg
    w0 = store.create("Seed holder")                     # CRUD is tested below, not the import
    assert store.get(w0["id"])["items"] == []
    w = store.create("  Bank   stocks ")
    assert w["name"] == "Bank stocks" and w["items"] == []
    instrument = {"symbol": "HDFCBANK", "name": "HDFC Bank Limited", "provider_symbol": "nse:hdfcbank-eq",
                  "sector": "Financials"}
    w = store.add_item(w["id"], instrument, cap_segment="Large")
    assert w["items"][0]["provider_symbol"] == "NSE:HDFCBANK-EQ" and w["items"][0]["cap_segment"] == "Large"
    with pytest.raises(wl.WatchlistError) as e:
        store.add_item(w["id"], instrument)
    assert e.value.kind == "duplicate"
    w = store.rename(w["id"], "Banks")
    assert w["name"] == "Banks"
    w = store.remove_item(w["id"], "nse:hdfcbank-eq")
    assert w["items"] == []
    with pytest.raises(wl.WatchlistError) as e:
        store.remove_item(w["id"], "NSE:HDFCBANK-EQ")
    assert e.value.kind == "not_found"
    assert store.delete(w["id"]) is True
    with pytest.raises(wl.WatchlistError):
        store.get(w["id"])
    assert store.get(w0["id"])["items"] == []


def test_last_watchlist_cannot_be_deleted_and_items_are_capped(pg):
    _, fake = pg
    store = wl.WatchlistStore(VALID_URL, max_items=1)
    w = store.create("Only")
    with pytest.raises(wl.WatchlistError) as e:
        store.delete(w["id"])
    assert e.value.kind == "limit"
    store.add_item(w["id"], {"symbol": "TCS", "name": "TCS", "provider_symbol": "NSE:TCS-EQ"})
    with pytest.raises(wl.WatchlistError) as e:
        store.add_item(w["id"], {"symbol": "INFY", "name": "Infosys", "provider_symbol": "NSE:INFY-EQ"})
    assert e.value.kind == "full"


def test_names_and_instruments_are_validated():
    for bad in ("", "   ", None, 5, "x" * (wl.MAX_NAME + 1)):
        with pytest.raises(wl.WatchlistError) as e:
            wl.validate_name(bad)
        assert e.value.kind == "invalid"
    for bad in ({}, {"symbol": "X"}, {"provider_symbol": "NSE:X-EQ", "symbol": "X"}):
        with pytest.raises(wl.WatchlistError):
            wl.normalise_instrument(bad)
    assert wl.normalise_instrument({"symbol": "X", "name": "X Ltd", "provider_symbol": "NSE:X-EQ"},
                                   "Mega")["cap_segment"] is None


def test_connect_failure_is_reported_and_redacted(monkeypatch):
    def boom(url, timeout):
        raise wl.WatchlistError("unavailable", "Watchlist database is unreachable (OperationalError: timeout).")
    monkeypatch.setattr(wl, "_open_connection", boom)
    with pytest.raises(wl.WatchlistError) as e:
        wl.WatchlistStore(VALID_URL).lists()
    assert e.value.kind == "unavailable" and "timeout" in str(e.value)


def test_to_entry_keeps_the_verified_yfinance_mapping():
    e = wl.to_entry({"symbol": "ITC", "name": "ITC Limited", "provider_symbol": "NSE:ITC-EQ", "cap_segment": "Large",
                     "sector": "FMCG"})
    assert e["fyers"] == "NSE:ITC-EQ" and e["yf"] == "ITC.NS" and e["cap_segment"] == "Large"
    etf = wl.to_entry({"symbol": "NIFTYBEES", "name": "NIFTY BEES", "provider_symbol": "NSE:NIFTYBEES-ETF"})
    assert etf["yf"] is None and etf["cap_segment"] is None


# ---------------- API ----------------
def csrf(client):
    return re.search(r'name="csrf" content="([^"]+)"',
                     client.get("/", base_url=LOCAL).get_data(as_text=True)).group(1)


def test_watchlist_api_requires_csrf(mk):
    app, _ = mk(deps={"lists": FakeWatchlists()})
    c = app.test_client()
    assert c.post("/api/watchlists", json={"name": "X"}, base_url=LOCAL).status_code == 403


def test_watchlist_api_crud(mk):
    import time
    import instrument_search as ins
    cat = ins.InstrumentCatalogue(records=ins.parse_master(MASTER))
    app, _ = mk(deps={"lists": FakeWatchlists(), "catalogue": cat})
    for _ in range(200):  # create_app downloads the master in a background thread; let it finish first
        if cat.ready():
            break
        time.sleep(0.01)
    c = app.test_client()
    h = {"X-CSRF-Token": csrf(c)}

    d = c.get("/api/watchlists", base_url=LOCAL).json
    assert d["ready"] is True and d["watchlists"][0]["name"] == wl.UNIVERSE_SEED_NAME and d["default_id"] == "list-1"

    r = c.post("/api/watchlists", json={"name": "Momentum"}, headers=h, base_url=LOCAL)
    assert r.status_code == 201 and r.json["name"] == "Momentum" and r.json["id"] == "list-2"

    r = c.post("/api/watchlists/list-2/items", json={"provider_symbol": "nse:tcs-eq"}, headers=h, base_url=LOCAL)
    assert r.status_code == 201 and r.json["items"][0]["symbol"] == "TCS"

    r = c.post("/api/watchlists/list-2/items", json={"provider_symbol": "NSE:TCS-EQ"}, headers=h, base_url=LOCAL)
    assert r.status_code == 409 and r.json["error"] == "duplicate"

    r = c.post("/api/watchlists/list-2/items", json={"provider_symbol": "NSE:NOTREAL-EQ"}, headers=h, base_url=LOCAL)
    assert r.status_code == 400 and r.json["error"] == "unknown_instrument"

    r = c.post("/api/watchlists/list-2/items", json={"symbol": "TCS"}, headers=h, base_url=LOCAL)
    assert r.status_code == 400          # the browser may not submit symbols, names or exchanges

    r = c.patch("/api/watchlists/list-2", json={"name": "Momo"}, headers=h, base_url=LOCAL)
    assert r.status_code == 200 and r.json["name"] == "Momo"

    r = c.delete("/api/watchlists/list-2/items/NSE%3ATCS-EQ", headers=h, base_url=LOCAL)
    assert r.status_code == 200 and r.json["items"] == []

    r = c.delete("/api/watchlists/list-2", headers=h, base_url=LOCAL)
    assert r.status_code == 200 and r.json == {"deleted": True}

    r = c.delete("/api/watchlists/list-1", headers=h, base_url=LOCAL)
    assert r.status_code == 409 and r.json["error"] == "limit"      # never delete the last list


def test_start_requires_a_non_empty_watchlist(mk):
    lists = FakeWatchlists(seed=False)
    lists.create("Empty")
    app, _ = mk(deps={"lists": lists})
    c = app.test_client()
    h = {"X-CSRF-Token": csrf(c)}
    r = c.post("/api/start", json={"mode": "live", "watchlist_id": "list-1"}, headers=h, base_url=LOCAL)
    assert r.status_code == 409 and r.json["error"] == "empty" and "no stocks" in r.json["message"]
    r = c.post("/api/start", json={"mode": "live", "watchlist_id": "missing"}, headers=h, base_url=LOCAL)
    assert r.status_code == 404
    r = c.post("/api/start", json={"mode": "live", "watchlist_id": 12}, headers=h, base_url=LOCAL)
    assert r.status_code == 400 and r.json["error"] == "bad_watchlist"


def test_watchlists_endpoint_reports_an_unavailable_database(mk):
    lists = FakeWatchlists(seed=False)
    lists.error = "unavailable"
    app, _ = mk(deps={"lists": lists})
    c = app.test_client()
    r = c.get("/api/watchlists", base_url=LOCAL)
    assert r.status_code == 503 and r.json["ready"] is False and r.json["error"] == "unavailable"
