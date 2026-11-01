import socket
import time
from datetime import datetime, timedelta, timezone

import pytest

import evidence as ev
import watchlists as wl
from fyers_client import FyersError
from tests.conftest import FakeHTTP, FakeWatchlists, rows as db_rows

IST = ev.IST


class FakeTG:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, text):
        self.sent.append(text)
        return (True, None) if self.ok else (False, "boom")


def run(app, mode, watchlist=None):
    p = app.extensions["pipeline"]
    p.run(mode, watchlist=watchlist)
    return p.snapshot()


def test_live_without_connection_is_a_clear_error(mk):
    app, _ = mk(deps={"lists": FakeWatchlists()})
    st = run(app, "live")
    assert st["status"] == "error" and "not connected" in st["message"].lower()


def test_run_without_a_configured_watchlist_store_says_so(mk):
    """No database and no in-memory fallback: the run fails visibly instead of analysing an invented list."""
    app, _ = mk(live_env(), {"fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF})
    st = run(app, "live")
    assert st["status"] == "error" and "not configured" in st["message"].lower()


def test_watchlist_run_analyses_only_that_list(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")
    lists = FakeWatchlists(seed=False)
    lists.create("Mine")
    lists.add_instrument("list-1", symbol="TCS", name="Tata Consultancy Services", provider_symbol="NSE:TCS-EQ")
    lists.add_instrument("list-1", symbol="ITC", name="ITC Limited", provider_symbol="NSE:ITC-EQ")
    tg = FakeTG()
    app, _ = mk({**live_env(), "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"},
                {"lists": lists, "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": tg})
    st = run(app, "live", {"id": "list-1", "name": "Mine"})
    assert st["status"] == "done" and st["scope"] == "watchlist"
    assert st["watchlist"] == {"id": "list-1", "name": "Mine", "count": 2}
    assert {v["symbol"] for v in st["verdicts"]} == {"TCS", "ITC"}   # never the seeded default list
    assert st["kpis"]["universe"] == 2
    assert 'Mine' in st["footer"]["text"]


def test_empty_or_unknown_watchlist_blocks_the_run(mk):
    lists = FakeWatchlists(seed=False)
    lists.create("Empty")
    app, _ = mk(live_env(), {"lists": lists, "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF})
    st = run(app, "live", {"id": "list-1"})
    assert st["status"] == "error" and "no stocks" in st["message"].lower()
    st = run(app, "live", {"id": "nope"})
    assert st["status"] == "error" and "no longer exists" in st["message"]


def test_legacy_run_without_an_id_uses_the_first_stored_list(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")
    app, _ = mk(live_env(), {"lists": FakeWatchlists(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF})
    st = run(app, "live")
    seeded = {e["symbol"] for e in wl.seed_entries()}
    assert st["status"] == "done" and st["watchlist"]["name"] == wl.UNIVERSE_SEED_NAME
    assert {v["symbol"] for v in st["verdicts"]} <= seeded


def test_second_run_while_running_rejected(mk):
    app, _ = mk()
    p = app.extensions["pipeline"]
    p.running = True
    assert p.start("live") is False


def test_failed_telegram_is_recorded_not_fatal(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")
    deps = {"lists": FakeWatchlists(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF,
            "telegram": FakeTG(ok=False)}
    app, _ = mk({**live_env(), "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, deps)
    st = run(app, "live")
    assert st["status"] == "done" and any(v["alert"]["status"] == "failed" for v in st["verdicts"])


# ---------- live path with fully mocked providers ----------
class FakeProvider:
    def __init__(self, fail=None):
        self.fail, self.now = fail, int(time.time())

    def quotes(self, syms):
        if self.fail:
            raise self.fail
        return {s: {"live": 500.0 + i, "day_open": 480.0, "day_high": 505.0 + i, "day_low": 479.0, "prev_close": 480.0 + 0.1 * i,
                    "day_change_pct": 3.0 + i * 0.1, "volume": 6_000_000, "quote_epoch": self.now} for i, s in enumerate(syms)}

    def daily_candles(self, sym, days=366):
        today = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        for k in range(250, 0, -1):
            d = today - timedelta(days=k)
            px = 300 + (250 - k) * 0.8
            out.append({"t": int(d.timestamp()), "o": px, "h": px + 3, "l": px - 3, "c": px, "v": 1_000_000})
        return out

    def intraday_candles(self, sym, calendar_days=14, resolution="5"):
        return []


class FakeGdelt:
    def search(self, q, **k):
        return [{"title": f"{q} wins record order", "url": "https://x.example/a", "domain": "x.example", "seendate": "20261028T040000Z"}], None


class FakeYF:
    class Ticker:
        def __init__(self, s):
            self.news, self.recommendations = [], None
            self.info = {"recommendationKey": "buy", "numberOfAnalystOpinions": 12, "targetMeanPrice": 620.0,
                         "targetLowPrice": 500.0, "targetHighPrice": 700.0, "sector": "X", "longName": s}


def live_env():
    return {"FYERS_APP_ID": "APP-100", "FYERS_APP_SECRET": "sec", "FYERS_ACCESS_TOKEN": "env-token-1234567"}


def test_live_end_to_end_with_mocks_sends_one_alert_per_day(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")
    tg = FakeTG()
    deps = {"lists": FakeWatchlists(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": tg}
    app, _ = mk({**live_env(), "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, deps)
    st = run(app, "live")
    assert st["status"] == "done" and st["kpis"]["in_debate"] == 12
    assert not st["footer"]["synthetic"] and "real NSE stocks" in st["footer"]["text"]
    assert any("GROQ_API_KEY not set" in w for w in st["warnings"])
    buys = [v for v in st["verdicts"] if v["verdict"] == "BUY"]
    assert buys and all(v["freshness"] == "live" and v["alert"]["status"] == "sent" for v in buys)
    n = len([m for m in tg.sent if "BUY SIGNAL" in m])
    assert n == len(buys) and any("Daily summary" in m for m in tg.sent)
    st2 = run(app, "live")
    assert len([m for m in tg.sent if "BUY SIGNAL" in m]) == n





def test_live_market_closed_blocks_alerts(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "closed")
    tg = FakeTG()
    app, _ = mk({**live_env(), "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"},
                {"lists": FakeWatchlists(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": tg})
    st = run(app, "live")
    assert st["status"] == "done" and not [m for m in tg.sent if "BUY SIGNAL" in m]
    assert any("Market is closed" in w for w in st["warnings"])
    assert all(v["freshness"] == "last_close" for v in st["verdicts"])


def test_live_expired_token_aborts_gracefully_and_marks_expired(mk):
    app, _ = mk(live_env(), {"lists": FakeWatchlists(), "fyers": FakeProvider(FyersError("auth_expired", "x", -16)),
                             "gdelt": FakeGdelt(), "yf": FakeYF})
    st = run(app, "live")
    assert st["status"] == "error" and "expired" in st["message"].lower()
    assert app.extensions["auth"].status()["state"] == "expired"


def test_live_rate_limit_aborts_with_message(mk):
    app, _ = mk(live_env(), {"lists": FakeWatchlists(),
                             "fyers": FakeProvider(FyersError("rate_limited", "FYERS rate limit hit")),
                             "gdelt": FakeGdelt(), "yf": FakeYF})
    st = run(app, "live")
    assert st["status"] == "error" and "rate limit" in st["message"].lower()


def test_yfinance_failure_degrades_to_data_gaps(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")

    class Boom:
        class Ticker:
            def __init__(self, s):
                raise RuntimeError("yahoo down")
    app, _ = mk(live_env(), {"lists": FakeWatchlists(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": Boom})
    st = run(app, "live")
    assert st["status"] == "done" and all(any("analyst" in g for g in v["gaps"]) for v in st["verdicts"])


def test_fundamentals_nan_and_empty_info_are_reported_not_leaked():
    import data_sources as ds
    nan = float("nan")

    class NanYF:
        class Ticker:
            def __init__(self, s):
                self.recommendations = None
                self.info = {"recommendationKey": "buy", "numberOfAnalystOpinions": nan,
                             "targetMeanPrice": nan, "targetLowPrice": None, "targetHighPrice": nan}

    class EmptyYF:
        class Ticker:
            def __init__(self, s):
                self.recommendations = None
                self.info = {}
    an, meta, gaps, tk = ds.fetch_fundamentals("ITC.NS", NanYF)
    assert an["target_mean"] is None and an["num_analysts"] is None and an["consensus"] == "buy"
    an2, _, gaps2, _ = ds.fetch_fundamentals("ITC.NS", EmptyYF)
    assert "analyst.yfinance_empty" in gaps2 and an2["target_mean"] is None


def test_universe_seed_reproduces_the_retired_universe_file():
    """universe.json is gone from the codebase; the one-time import must still map every stock to a verified yfinance
    ticker (`NSE:SYM-EQ` -> `SYM.NS`, None for non-equity series) and keep its Large/Mid/Small buckets."""
    import data_sources as ds
    entries = [wl.to_entry(e) for e in wl.seed_entries()]
    assert len(entries) == 24 and all(e["yf"] for e in entries)
    assert {e["cap_segment"] for e in entries} == {"Large", "Mid", "Small"}
    for e in entries:
        assert e["yf"] == ds.yf_symbol_for(e["fyers"])
    itc = next(e for e in entries if e["symbol"] == "ITC")
    assert itc["yf"] == "ITC.NS" and itc["fyers"] == "NSE:ITC-EQ"


def test_no_module_still_loads_a_universe_file():
    """Regression guard: universe.json was deleted, so any leftover loader would crash a live run. Only the one-time
    import in watchlists.py may mention it; nothing may read a stock list from disk."""
    import pathlib
    here = pathlib.Path(__file__).resolve()
    root = here.parent.parent
    assert not (root / "universe.json").exists()
    modules = [p for p in root.glob("*.py") if p.name != "watchlists.py"]
    hits = [p.name for p in modules if "load_universe" in p.read_text(encoding="utf-8")
            or "json.loads" in p.read_text(encoding="utf-8") and "universe.json" in p.read_text(encoding="utf-8")]
    assert hits == [], hits


def test_focused_run_for_a_covered_stock_reports_coverage(mk, monkeypatch):
    """ITC has analyst coverage, so the Fundamentalist card must not read 0/1 for it."""
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")
    seen = []

    class YF:
        class Ticker:
            def __init__(self, s):
                seen.append(s)
                self.recommendations = None
                self.info = {"recommendationKey": "buy", "numberOfAnalystOpinions": 14, "targetMeanPrice": 470.0}
    app, _ = mk(live_env(), {"lists": FakeWatchlists(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": YF})
    app.extensions["pipeline"].run("live", {**FOCUSED, "symbol": "ITC", "provider_symbol": "NSE:ITC-EQ"})
    st = app.extensions["pipeline"].snapshot()
    assert seen == ["ITC.NS"]                              # never a bare 'ITC' or None
    assert st["agents"]["fundamentalist"]["stats"]["covered"] == "1/1"
    gaps = st["verdicts"][0]["gaps"]
    assert "analyst.no_coverage" not in gaps and "analyst.not_mapped" not in gaps  # only the mix is missing here
    assert "analyst.buy_pct" in gaps


def test_fundamentals_survive_a_blocked_info_endpoint():
    """The deployed failing case: `.info` is empty/raises on a throttled cloud IP while the report endpoints work.
    Before the fallback this reported 'Covered 0/1' for TCS, which is covered by dozens of analysts."""
    import data_sources as ds
    class Blocked:
        class Ticker:
            def __init__(self, s):
                self.recommendations = None
            @property
            def info(self):
                raise ValueError("empty info")
            @property
            def recommendations_summary(self):
                raise RuntimeError("no summary")
            @property
            def analyst_price_targets(self):
                return {"current": 2502.6, "high": 3266.0, "low": 1840.0, "mean": 2398.0}
    an, meta, gaps, _ = ds.fetch_fundamentals("TCS.NS", Blocked)
    assert ds.analyst_covered(an) and an["_reason"] == "fetched"
    assert an["target_mean"] == 2398.0 and an["target_low"] == 1840.0
    assert "analyst.yfinance_empty" not in gaps
    assert "analyst.yfinance_empty" not in gaps


class _EmptyTicker:
    recommendations = None
    info = {}
    recommendations_summary = None
    analyst_price_targets = {}
    def __init__(self, s):
        pass


def test_fully_blocked_yahoo_is_not_reported_as_no_coverage():
    """'No analyst data' and 'Yahoo gave this server nothing' must not be confused in the UI."""
    class Blocked:
        Ticker = _EmptyTicker
    import data_sources as ds
    an, _, gaps, _ = ds.fetch_fundamentals("TCS.NS", Blocked)
    assert an["_reason"] == "yfinance_empty" and not ds.analyst_covered(an)
    assert "analyst.yfinance_empty" in gaps and "analyst.no_coverage" not in gaps


def test_no_analyst_coverage_is_explained_not_silent():
    """Regression: a small cap showed 'Covered 0/1' with an EMPTY gap list, which looks like a bug.
    Yahoo returning no consensus/target is 'not covered' and must say so."""
    class NoCoverage:
        class Ticker:
            def __init__(self, s):
                self.recommendations = None
                self.info = {"longName": "ITI Ltd", "sector": "Technology", "currentPrice": 215.95}
    import data_sources as ds
    an, meta, gaps, tk = ds.fetch_fundamentals("ITI.NS", NoCoverage)
    assert an["_reason"] == "no_analyst_coverage" and "analyst.no_coverage" in gaps
    assert ds.analyst_covered(an) is False and ds.analyst_covered({**an, "buy_pct": 50.0}) is True
    assert meta["name"] == "ITI Ltd"


def test_yfinance_proxy_is_opt_in(monkeypatch):
    import app as appmod
    for k in ("YFINANCE_PROXY", "PROXY_URL", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    appmod._route_yfinance_proxy(object())
    assert "HTTP_PROXY" not in __import__("os").environ
    monkeypatch.setenv("YFINANCE_PROXY", "http://user:pass@10.0.0.1:8080")
    appmod._route_yfinance_proxy(object())
    import os
    assert os.environ["HTTPS_PROXY"] == "http://user:pass@10.0.0.1:8080"
    for k in ("HTTP_PROXY", "HTTPS_PROXY"):  # do not leak the dead proxy into other tests
        monkeypatch.delenv(k, raising=False)


def test_agent_order_preserved_in_api(mk):
    from tests.conftest import LOCAL
    app, _ = mk()
    keys = list(app.test_client().get("/api/status", base_url=LOCAL).json["agents"])
    assert keys == ["scout", "technician", "fundamentalist", "newsdesk", "bull", "bear", "judge", "messenger"]


# ---------- focused run (search & select) ----------
import instrument_search  # noqa: E402
from tests.test_instrument_search import MASTER  # noqa: E402

FOCUSED = {"symbol": "NIFTYBEES", "name": "NIPPON INDIA ETF NIFTY BEES", "exchange": "NSE", "asset_type": "EQUITY",
           "provider": "fyers", "provider_symbol": "NSE:NIFTYBEES-EQ", "currency": "INR", "isin": "INF204KB14I2"}


def focused_env():
    return {**live_env(), "TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}


def test_focused_run_analyses_only_the_selected_instrument_without_alerts(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")
    tg = FakeTG()
    deps = {"lists": FakeWatchlists(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": tg}
    app, _ = mk(focused_env(), deps)
    p = app.extensions["pipeline"]
    p.run("live", dict(FOCUSED))
    st = p.snapshot()
    assert st["status"] == "done" and st["scope"] == "focused"
    assert [v["symbol"] for v in st["verdicts"]] == ["NIFTYBEES"]
    assert st["kpis"]["universe"] == 1 and st["kpis"]["in_debate"] == 1
    assert st["target"]["provider_symbol"] == "NSE:NIFTYBEES-EQ"
    assert "not from a watchlist" in st["footer"]["text"]
    assert tg.sent == []                                   # focused runs never message Telegram
    assert st["verdicts"][0]["alert"]["status"] == "skipped"
    assert db_rows(app.extensions["store"], "SELECT COUNT(*) FROM verdicts")[0][0] == 1


def test_focused_run_is_recorded_with_its_scope(mk):
    app, _ = mk(live_env(), {"fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF})
    app.extensions["pipeline"].run("live", dict(FOCUSED))
    import json
    mode, summary = db_rows(app.extensions["store"], "SELECT mode, summary FROM runs")[-1]
    assert mode == "focused"
    meta = json.loads(summary)
    assert meta["scope"] == "focused" and meta["target"]["symbol"] == "NIFTYBEES"


def test_focused_run_without_known_yf_mapping_reports_a_gap_not_a_guess(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")
    seen = []

    class SpyYF:
        class Ticker:
            def __init__(self, s):
                seen.append(s)
                raise AssertionError("must not fetch an unmapped ticker")

    app, _ = mk(live_env(), {"lists": FakeWatchlists(), "fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": SpyYF})
    app.extensions["pipeline"].run("live", {**FOCUSED, "provider_symbol": "NSE:XYZ-ETF-EQ"})
    st = app.extensions["pipeline"].snapshot()
    assert seen == [] and st["status"] == "done"
    assert "analyst.not_mapped" in st["verdicts"][0]["gaps"]


def test_focused_run_without_a_quote_is_a_clear_error(mk):
    class NoQuote(FakeProvider):
        def quotes(self, syms):
            return {}

    app, _ = mk(live_env(), {"fyers": NoQuote(), "gdelt": FakeGdelt(), "yf": FakeYF})
    app.extensions["pipeline"].run("live", dict(FOCUSED))
    st = app.extensions["pipeline"].snapshot()
    assert st["status"] == "error" and "no live quote" in st["message"].lower()


def test_focused_run_without_fyers_connection_is_a_clear_error(mk):
    app, _ = mk()
    app.extensions["pipeline"].run("live", dict(FOCUSED))
    st = app.extensions["pipeline"].snapshot()
    assert st["status"] == "error" and "not connected" in st["message"].lower()


def test_focused_run_technical_failure_degrades_to_gaps(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")

    class NoCandles(FakeProvider):
        def daily_candles(self, sym, days=366):
            raise FyersError("invalid", "no historical data")

    app, _ = mk(live_env(), {"lists": FakeWatchlists(), "fyers": NoCandles(), "gdelt": FakeGdelt(), "yf": FakeYF})
    app.extensions["pipeline"].run("live", dict(FOCUSED))
    st = app.extensions["pipeline"].snapshot()
    assert st["status"] == "done"
    assert any(g.startswith("technicals(") for g in st["verdicts"][0]["gaps"])


def test_focused_run_permission_error_on_candles_aborts(mk):
    class NoPerm(FakeProvider):
        def daily_candles(self, sym, days=366):
            raise FyersError("permission", "no historical data")

    app, _ = mk(live_env(), {"lists": FakeWatchlists(), "fyers": NoPerm(), "gdelt": FakeGdelt(), "yf": FakeYF})
    app.extensions["pipeline"].run("live", dict(FOCUSED))
    st = app.extensions["pipeline"].snapshot()
    assert st["status"] == "error" and "historical data" in st["message"].lower()


def test_analyze_selected_api_starts_a_focused_run_and_status_reports_scope(mk):
    import re
    cat = instrument_search.InstrumentCatalogue(http=FakeHTTP(), records=instrument_search.parse_master(MASTER))
    tg = FakeTG()
    app, _ = mk(focused_env(), {"lists": FakeWatchlists(), "catalogue": cat, "fyers": FakeProvider(),
                                "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": tg})
    from tests.conftest import LOCAL
    c = app.test_client()
    tok = re.search(r'name="csrf" content="([^"]+)"', c.get("/", base_url=LOCAL).get_data(as_text=True)).group(1)
    r = c.post("/api/analyze-selected", json={"provider_symbol": "nse:niftybees-eq"},
               headers={"X-CSRF-Token": tok}, base_url=LOCAL)
    assert r.status_code == 202 and r.json["scope"] == "focused"
    assert r.json["target"]["provider_symbol"] == "NSE:NIFTYBEES-EQ"
    p = app.extensions["pipeline"]
    # The run is on a thread; wait for its RESULT, not just for running=False (that flag is cleared in the
    # worker's finally block, so it can still be True before the thread has started).
    for _ in range(2000):
        if not p.running and p.state["status"] != "running":
            break
        time.sleep(0.02)
    st = c.get("/api/status", base_url=LOCAL).json
    assert st["scope"] == "focused" and st["target"]["symbol"] == "NIFTYBEES"
    assert st["status"] == "done", st.get("message")
    assert [v["symbol"] for v in st["verdicts"]] == ["NIFTYBEES"] and tg.sent == []


def test_analyze_selected_returns_busy_while_running(mk):
    import re
    cat = instrument_search.InstrumentCatalogue(http=FakeHTTP(), records=instrument_search.parse_master(MASTER))
    app, _ = mk(deps={"catalogue": cat})
    from tests.conftest import LOCAL
    c = app.test_client()
    tok = re.search(r'name="csrf" content="([^"]+)"', c.get("/", base_url=LOCAL).get_data(as_text=True)).group(1)
    app.extensions["pipeline"].running = True
    r = c.post("/api/analyze-selected", json={"provider_symbol": "NSE:NIFTYBEES-EQ"},
               headers={"X-CSRF-Token": tok}, base_url=LOCAL)
    assert r.status_code == 409 and r.json["error"] == "busy"


def test_fyers_quotes_exposes_the_returned_symbol():
    from fyers_client import FyersClient
    from tests.conftest import FakeHTTP, Resp
    http = FakeHTTP([Resp({"s": "ok", "code": 200, "d": [{"n": "NSE:NIFTYBEES-EQ", "s": "ok",
                                                           "v": {"lp": 250.0, "tt": 1_700_000_000}}]})])
    q = FyersClient("APP", lambda: "tok", http=http).quotes(["NSE:NIFTYBEES-EQ"])
    assert q["NSE:NIFTYBEES-EQ"]["symbol"] == "NSE:NIFTYBEES-EQ"
