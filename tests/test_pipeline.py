import socket
import time
from datetime import datetime, timedelta, timezone

import pytest

import evidence as ev
import data_sources as ds
from fyers_client import FyersError
from tests.conftest import FakeHTTP, rows as db_rows

IST = ev.IST


class FakeTG:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, text):
        self.sent.append(text)
        return (True, None) if self.ok else (False, "boom")


def run(app, mode):
    p = app.extensions["pipeline"]
    p.run(mode)
    return p.snapshot()


def test_live_without_connection_is_a_clear_error(mk):
    app, _ = mk()
    st = run(app, "live")
    assert st["status"] == "error" and "not connected" in st["message"].lower()


def test_second_run_while_running_rejected(mk):
    app, _ = mk()
    p = app.extensions["pipeline"]
    p.running = True
    assert p.start("live") is False


def test_failed_telegram_is_recorded_not_fatal(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")
    deps = {"fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": FakeTG(ok=False)}
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
    deps = {"fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": tg}
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
                {"fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": tg})
    st = run(app, "live")
    assert st["status"] == "done" and not [m for m in tg.sent if "BUY SIGNAL" in m]
    assert any("Market is closed" in w for w in st["warnings"])
    assert all(v["freshness"] == "last_close" for v in st["verdicts"])


def test_live_expired_token_aborts_gracefully_and_marks_expired(mk):
    app, _ = mk(live_env(), {"fyers": FakeProvider(FyersError("auth_expired", "x", -16)), "gdelt": FakeGdelt(), "yf": FakeYF})
    st = run(app, "live")
    assert st["status"] == "error" and "expired" in st["message"].lower()
    assert app.extensions["auth"].status()["state"] == "expired"


def test_live_rate_limit_aborts_with_message(mk):
    app, _ = mk(live_env(), {"fyers": FakeProvider(FyersError("rate_limited", "FYERS rate limit hit")), "gdelt": FakeGdelt(), "yf": FakeYF})
    st = run(app, "live")
    assert st["status"] == "error" and "rate limit" in st["message"].lower()


def test_yfinance_failure_degrades_to_data_gaps(mk, monkeypatch):
    monkeypatch.setattr(ev, "market_session", lambda now=None: "open")

    class Boom:
        class Ticker:
            def __init__(self, s):
                raise RuntimeError("yahoo down")
    app, _ = mk(live_env(), {"fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": Boom})
    st = run(app, "live")
    assert st["status"] == "done" and all(any("analyst" in g for g in v["gaps"]) for v in st["verdicts"])


def test_fundamentals_nan_and_empty_info_are_reported_not_leaked():
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
    deps = {"fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": FakeYF, "telegram": tg}
    app, _ = mk(focused_env(), deps)
    p = app.extensions["pipeline"]
    p.run("live", dict(FOCUSED))
    st = p.snapshot()
    assert st["status"] == "done" and st["scope"] == "focused"
    assert [v["symbol"] for v in st["verdicts"]] == ["NIFTYBEES"]
    assert st["kpis"]["universe"] == 1 and st["kpis"]["in_debate"] == 1
    assert st["target"]["provider_symbol"] == "NSE:NIFTYBEES-EQ"
    assert "not from universe.json" in st["footer"]["text"]
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

    app, _ = mk(live_env(), {"fyers": FakeProvider(), "gdelt": FakeGdelt(), "yf": SpyYF})
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

    app, _ = mk(live_env(), {"fyers": NoCandles(), "gdelt": FakeGdelt(), "yf": FakeYF})
    app.extensions["pipeline"].run("live", dict(FOCUSED))
    st = app.extensions["pipeline"].snapshot()
    assert st["status"] == "done"
    assert any(g.startswith("technicals(") for g in st["verdicts"][0]["gaps"])


def test_focused_run_permission_error_on_candles_aborts(mk):
    class NoPerm(FakeProvider):
        def daily_candles(self, sym, days=366):
            raise FyersError("permission", "no historical data")

    app, _ = mk(live_env(), {"fyers": NoPerm(), "gdelt": FakeGdelt(), "yf": FakeYF})
    app.extensions["pipeline"].run("live", dict(FOCUSED))
    st = app.extensions["pipeline"].snapshot()
    assert st["status"] == "error" and "historical data" in st["message"].lower()


def test_analyze_selected_api_starts_a_focused_run_and_status_reports_scope(mk):
    import re
    cat = instrument_search.InstrumentCatalogue(http=FakeHTTP(), records=instrument_search.parse_master(MASTER))
    tg = FakeTG()
    app, _ = mk(focused_env(), {"catalogue": cat, "fyers": FakeProvider(), "gdelt": FakeGdelt(),
                                "yf": FakeYF, "telegram": tg})
    from tests.conftest import LOCAL
    c = app.test_client()
    tok = re.search(r'name="csrf" content="([^"]+)"', c.get("/", base_url=LOCAL).get_data(as_text=True)).group(1)
    r = c.post("/api/analyze-selected", json={"provider_symbol": "nse:niftybees-eq"},
               headers={"X-CSRF-Token": tok}, base_url=LOCAL)
    assert r.status_code == 202 and r.json["scope"] == "focused"
    assert r.json["target"]["provider_symbol"] == "NSE:NIFTYBEES-EQ"
    p = app.extensions["pipeline"]
    for _ in range(200):
        if not p.running:
            break
        time.sleep(0.02)
    st = c.get("/api/status", base_url=LOCAL).json
    assert st["scope"] == "focused" and st["target"]["symbol"] == "NIFTYBEES"
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
