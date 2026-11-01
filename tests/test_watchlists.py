"""Watchlist store, API and run scope. No database is contacted: the store is faked (API/pipeline) or the
SQL layer is replaced with an in-memory cursor that records statements (store)."""
import re
import time

import pytest

import instrument_search
from tests.conftest import LOCAL, FakeHTTP
from tests.test_instrument_search import MASTER
from tests.test_pipeline import FakeProvider, FakeGdelt, FakeYF, FakeTG
from watchlists import WatchlistError, WatchlistStore, dsn_uri


def csrf(client):
    html = client.get("/", base_url=LOCAL).get_data(as_text=True)
    return re.search(r'name="csrf" content="([^"]+)"', html).group(1)


def hdr(tok, **extra):
    return {"X-CSRF-Token": tok, **extra}


@pytest.fixture
def api(mk):
    cat = instrument_search.InstrumentCatalogue(http=FakeHTTP(), records=instrument_search.parse_master(MASTER))
    app, _ = mk({"FYERS_APP_ID": "A-1", "FYERS_APP_SECRET": "s", "FYERS_ACCESS_TOKEN": "env-token-1234567"},
                {"catalogue": cat, "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": FakeTG()})
    return app, app.test_client(), app.extensions["watchlists"]


def test_lists_are_seeded_with_the_retired_universe(api):
    _, c, _ = api
    d = c.get("/api/watchlists", base_url=LOCAL).json
    assert [w["name"] for w in d["watchlists"]] == ["Watchlist 1"]
    assert d["watchlists"][0]["count"] == 24
    item = d["watchlists"][0]["items"][0]
    assert item == {"provider_symbol": "NSE:RELIANCE-EQ", "symbol": "RELIANCE", "name": "Reliance Industries",
                    "exchange": "NSE", "asset_type": "EQUITY"}


def test_create_rename_delete_lists(api):
    _, c, _ = api
    tok = csrf(c)
    r = c.post("/api/watchlists", json={"name": "  Momentum   ideas "}, headers=hdr(tok), base_url=LOCAL)
    assert r.status_code == 201 and r.json["watchlist"]["name"] == "Momentum ideas"
    wid = r.json["watchlist"]["id"]
    r = c.patch(f"/api/watchlists/{wid}", json={"name": "Quality"}, headers=hdr(tok), base_url=LOCAL)
    assert r.status_code == 200 and r.json["watchlist"]["name"] == "Quality"
    r = c.delete(f"/api/watchlists/{wid}", headers=hdr(tok), base_url=LOCAL)
    assert r.status_code == 200 and [w["name"] for w in r.json["watchlists"]] == ["Watchlist 1"]


def test_last_list_cannot_be_deleted(api):
    _, c, _ = api
    r = c.delete("/api/watchlists/1", headers=hdr(csrf(c)), base_url=LOCAL)
    assert r.status_code == 409 and r.json["error"] == "last_list"


def test_mutations_require_csrf(api):
    _, c, _ = api
    assert c.post("/api/watchlists", json={"name": "X"}, base_url=LOCAL).status_code == 403
    assert c.patch("/api/watchlists/1", json={"name": "X"}, base_url=LOCAL).status_code == 403
    assert c.delete("/api/watchlists/1/items/NSE:TCS-EQ", base_url=LOCAL).status_code == 403


def test_bad_names_are_rejected(api):
    _, c, _ = api
    tok = csrf(c)
    for body in ({}, {"name": ""}, {"name": "   "}, {"name": 42}, {"name": "x" * 80}, ["n"]):
        assert c.post("/api/watchlists", json=body, headers=hdr(tok), base_url=LOCAL).status_code == 400


def test_add_and_remove_a_catalogue_validated_stock(api):
    _, c, _ = api
    tok = csrf(c)
    wid = c.post("/api/watchlists", json={"name": "Banks"}, headers=hdr(tok), base_url=LOCAL).json["watchlist"]["id"]
    r = c.post(f"/api/watchlists/{wid}/items", json={"provider_symbol": "nse:tcs-eq"}, headers=hdr(tok), base_url=LOCAL)
    assert r.status_code == 201
    item = r.json["watchlist"]["items"][0]
    # Fields come from the catalogue, never from the browser.
    assert item["symbol"] == "TCS" and item["name"] == "TATA CONSULTANCY SERVICES LIMITED" and item["in_catalogue"]
    assert c.post(f"/api/watchlists/{wid}/items", json={"provider_symbol": "NSE:TCS-EQ"},
                  headers=hdr(tok), base_url=LOCAL).status_code == 409           # duplicate
    for body in ({}, {"provider_symbol": "NSE:MADEUP-EQ"}, {"provider_symbol": "x" * 61}, ["nse:tcs-eq"]):
        assert c.post(f"/api/watchlists/{wid}/items", json=body, headers=hdr(tok), base_url=LOCAL).status_code == 400
    r = c.delete(f"/api/watchlists/{wid}/items/NSE%3ATCS-EQ", headers=hdr(tok), base_url=LOCAL)
    assert r.status_code == 200 and r.json["watchlist"]["items"] == []
    assert c.delete(f"/api/watchlists/{wid}/items/NSE%3ATCS-EQ", headers=hdr(tok), base_url=LOCAL).status_code == 404


def test_store_is_unavailable_without_a_dsn_and_fails_closed():
    st = WatchlistStore("")
    with pytest.raises(WatchlistError) as e:
        st.lists()
    assert e.value.kind == "unavailable" and st.pool is None


def test_dsn_uri_escapes_a_password_pasted_with_raw_characters():
    """Supabase shows `postgresql://postgres:PASSWORD@db...`; a password containing @ ? / or # must still parse,
    and the password itself is never guessed (a missing one is left alone so it fails visibly)."""
    uri = dsn_uri("postgresql://postgres:pa?ss@wo/rd@db.abc.supabase.co:5432/postgres")
    assert uri.startswith("postgresql://postgres:pa%3Fss%40wo%2Frd@db.abc.supabase.co:5432/postgres?sslmode=require")
    assert dsn_uri("").strip() == "" and "sslmode" not in dsn_uri("not-a-uri")


class _Cursor:
    """Records SQL. fetchone() answers from a queue; a WatchlistError raised by _FakeDB escapes the transaction."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        return self.db.execute(sql, params)

    def executemany(self, sql, rows):
        return self.db.executemany(sql, rows)

    def fetchone(self):
        return self.db.answers.pop(0)

    @property
    def connection(self):
        return self.db


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *a):
        return False


class _FakeDB:
    """SQL recorder. answers: what each fetchone() returns, in call order (marker, list-exists, RETURNING id, count)."""

    def __init__(self, marker=None, list_exists=None, list_id=7):
        self.sql = []
        self.answers = [marker, list_exists, (list_id,), None]

    def execute(self, sql, params=None):
        self.sql.append((sql.strip().split("\n")[0], params))
        return self

    def executemany(self, sql, rows):
        self.sql.append((sql.strip().split("\n")[0], list(rows)))
        return self

    def cursor(self):
        return _Cursor(self)

    def transaction(self):
        return _Tx(self)

    def fetchone(self):
        return self.answers.pop(0)


def _store_with(**kw):
    st = WatchlistStore("postgresql://u:p@h/db")
    st.pool = _FakeDB(**kw)
    return st


def test_seed_runs_once_and_only_into_an_empty_database():
    st = _store_with(marker=(1,))                              # migration marker already present
    st.seed()
    assert len(st.pool.sql) == 1 and "app_migrations" in st.pool.sql[0][0]   # no list is touched again

    st = _store_with()                                         # marker absent; no lists exist
    st.seed()
    inserts = [p for sql, p in st.pool.sql if sql.startswith("INSERT INTO watchlist_items")][0]
    assert len(inserts) == 24 and inserts[0][:4] == (7, "NSE:RELIANCE-EQ", "RELIANCE", "Reliance Industries")
    assert "INSERT INTO app_migrations" in st.pool.sql[-1][0]

    st = _store_with(list_exists=(1,))                         # lists exist: never reseed, just mark
    st.seed()
    assert not [s for s, _ in st.pool.sql if s.startswith("INSERT INTO watchlist_items")]


def test_seed_rolls_back_when_an_insert_fails():
    class Boom(_FakeDB):
        def executemany(self, sql, rows):                           # nothing is written; the tx must not commit
            raise RuntimeError("boom")
    st = WatchlistStore("postgresql://u:p@h/db")
    st.pool = Boom()
    with pytest.raises(RuntimeError):
        st.seed()


# ---------- run scope ----------
def _cat():
    return instrument_search.InstrumentCatalogue(http=FakeHTTP(), records=instrument_search.parse_master(MASTER))


def test_run_analyses_only_the_selected_watchlist(mk):
    deps = {"catalogue": _cat(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": FakeTG()}
    app, _ = mk({"FYERS_APP_ID": "A-1", "FYERS_APP_SECRET": "s", "FYERS_ACCESS_TOKEN": "env-token-1234567"}, deps)
    store = app.extensions["watchlists"]
    store.create("Pairs")
    store.add_item(2, {"provider_symbol": "NSE:TCS-EQ", "symbol": "TCS", "name": "T", "exchange": "NSE"})
    store.add_item(2, {"provider_symbol": "NSE:ITC-EQ", "symbol": "ITC", "name": "I", "exchange": "NSE"})
    p = app.extensions["pipeline"]
    p.run("live", watchlist_id=2)
    st = p.snapshot()
    assert st["status"] == "done" and st["scope"] == "watchlist"
    assert st["watchlist"] == {"id": 2, "name": "Pairs", "count": 2}
    assert sorted(v["symbol"] for v in st["verdicts"]) == ["ITC", "TCS"]
    assert "Pairs" in st["footer"]["text"] and st["kpis"]["universe"] == 2


def test_run_without_an_id_uses_the_first_list_only(mk):
    deps = {"catalogue": _cat(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": FakeTG()}
    app, _ = mk({"FYERS_APP_ID": "A-1", "FYERS_APP_SECRET": "s", "FYERS_ACCESS_TOKEN": "env-token-1234567"}, deps)
    app.extensions["pipeline"].run("live")
    st = app.extensions["pipeline"].snapshot()
    assert st["watchlist"]["name"] == "Watchlist 1" and st["kpis"]["in_debate"] == 4


def test_empty_or_unknown_list_fails_visibly_and_never_falls_back(mk):
    deps = {"catalogue": _cat(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": FakeTG()}
    app, _ = mk({"FYERS_APP_ID": "A-1", "FYERS_APP_SECRET": "s", "FYERS_ACCESS_TOKEN": "env-token-1234567"}, deps)
    p = app.extensions["pipeline"]
    p.run("live", watchlist_id=99)
    st = p.snapshot()
    assert st["status"] == "error" and "no longer exists" in st["message"] and st["verdicts"] == []


def test_empty_list_is_refused_before_any_fyers_call(mk):
    calls = []

    class Spy(FakeProvider):
        def quotes(self, syms):
            calls.append(list(syms))
            return super().quotes(syms)
    deps = {"catalogue": _cat(), "fyers": Spy(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": FakeTG()}
    app, _ = mk({"FYERS_APP_ID": "A-1", "FYERS_APP_SECRET": "s", "FYERS_ACCESS_TOKEN": "env-token-1234567"}, deps)
    app.extensions["watchlists"].create("Empty")
    p = app.extensions["pipeline"]
    p.run("live", watchlist_id=2)
    st = p.snapshot()
    assert st["status"] == "error" and "is empty" in st["message"] and calls == [] and st["verdicts"] == []


def test_api_start_requires_a_watchlist_and_resolves_it_server_side(mk):
    deps = {"catalogue": _cat(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": FakeTG()}
    app, _ = mk({"FYERS_APP_ID": "A-1", "FYERS_APP_SECRET": "s", "FYERS_ACCESS_TOKEN": "env-token-1234567"}, deps)
    app.extensions["watchlists"].create("Two")
    c = app.test_client()
    r = c.post("/api/start", json={"mode": "live", "watchlist_id": "2"}, headers=hdr(csrf(c)), base_url=LOCAL)
    assert r.status_code == 202 and r.json["watchlist_id"] == "2" and r.json["scope"] == "watchlist"
    p = app.extensions["pipeline"]
    for _ in range(2000):
        if not p.running and p.state["status"] != "running":
            break
        time.sleep(0.02)
    assert app.test_client().get("/api/status", base_url=LOCAL).json["watchlist"]["name"] == "Two"

