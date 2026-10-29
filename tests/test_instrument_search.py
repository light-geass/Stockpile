import pytest

import data_sources as ds
from instrument_search import (CatalogueError, InstrumentCatalogue, MAX_RESULTS, NSE_CM_URL,
                               parse_master, rank)
from tests.conftest import FakeHTTP, LOCAL, Resp


def row(symbol, name, short, strike="-1", isin="INE000A01001"):
    """One master row in FYERS column order (17 fields); only the columns we read carry real values."""
    r = [""] * 17
    r[1], r[5], r[9], r[13], r[15] = name, isin, symbol, short, strike
    return ",".join(r)


MASTER = "\n".join([
    row("NSE:TCS-EQ", "TATA CONSULTANCY SERVICES LIMITED", "TCS"),
    row("NSE:TATAMOTORS-EQ", "TATA MOTORS LIMITED", "TATAMOTORS"),
    row("NSE:TATAPOWER-EQ", "TATA POWER COMPANY LIMITED", "TATAPOWER"),
    row("NSE:NIFTYBEES-EQ", "NIPPON INDIA ETF NIFTY BEES", "NIFTYBEES"),
    row("NSE:GOLDBEES-EQ", "NIPPON INDIA ETF GOLD BEES", "GOLDBEES"),
    row("NSE:TCS-EQ-TEST", "TATA CONSULTANCY SERVICES LIMITED", "TCS"),      # duplicate listing row
    row("NSE:RELIANCE-EQ", "RELIANCE INDUSTRIES LIMITED", "RELIANCE"),
    row("NSE-XX", "BROKEN SYMBOL", "BROKEN"),
    row("NSE:TCS24NOVFUT", "TCS FUT 27 NOV 24", "TCS", strike="3500"),      # derivative → skipped
    "short,row",
])


def test_parse_master_normalizes_and_skips_derivatives_and_junk():
    out = parse_master(MASTER)
    syms = [i["symbol"] for i in out]
    assert "TCS" in syms and "NIFTYBEES" in syms and "RELIANCE" in syms
    assert "BROKEN" not in syms
    tcs = next(i for i in out if i["provider_symbol"] == "NSE:TCS-EQ")
    assert tcs == {"symbol": "TCS", "name": "TATA CONSULTANCY SERVICES LIMITED", "exchange": "NSE",
                   "asset_type": "EQUITY", "provider": "fyers", "provider_symbol": "NSE:TCS-EQ",
                   "currency": "INR", "isin": "INE000A01001"}


def test_parse_master_rejects_empty_input():
    with pytest.raises(CatalogueError):
        parse_master("")
    with pytest.raises(CatalogueError):
        parse_master("a,b,c\n")


def test_rank_order_is_exact_then_prefix_then_name_then_substring():
    recs = parse_master(MASTER)
    assert [i["symbol"] for i in rank(recs, "tcs")][0] == "TCS"
    assert {i["symbol"] for i in rank(recs, "tata")} == {"TATAMOTORS", "TATAPOWER", "TCS"}  # TCS name contains TATA
    assert [i["symbol"] for i in rank(recs, "TATA M")] == ["TATAMOTORS"]
    assert [i["symbol"] for i in rank(recs, "bees")] == ["GOLDBEES", "NIFTYBEES"]
    assert rank(recs, "zzzz") == []
    assert len(rank(recs, "a", limit=2)) <= 2


def test_rank_is_case_and_whitespace_insensitive_and_escapes_nothing():
    recs = parse_master(MASTER)
    assert rank(recs, "  reliance  ")[0]["symbol"] == "RELIANCE"
    assert rank(recs, "<script>") == []


def test_catalogue_downloads_once_then_serves_cache():
    http = FakeHTTP([Resp(text=MASTER)])
    cat = InstrumentCatalogue(http=http)
    assert cat.search("tcs")[0]["provider_symbol"] == "NSE:TCS-EQ"
    assert cat.search("relia")[0]["symbol"] == "RELIANCE"
    assert len(http.calls) == 1 and http.calls[0][1] == NSE_CM_URL
    assert cat.status() == {"loaded": True, "instruments": 6, "stale": False, "error": None}


def test_catalogue_refreshes_after_ttl():
    now = [0.0]
    http = FakeHTTP([Resp(text=MASTER), Resp(text=row("NSE:NEWCO-EQ", "NEW CO LIMITED", "NEWCO"))])
    cat = InstrumentCatalogue(http=http, ttl=100, clock=lambda: now[0])
    cat.search("tcs")
    now[0] = 50
    cat.search("tcs")
    assert len(http.calls) == 1
    now[0] = 101
    cat.search("newco")
    assert len(http.calls) == 2
    assert cat.get("NSE:NEWCO-EQ")["symbol"] == "NEWCO"


def test_catalogue_keeps_last_known_good_when_refresh_fails():
    now = [0.0]
    http = FakeHTTP([Resp(text=MASTER), Resp(status=500, text="down")])
    cat = InstrumentCatalogue(http=http, ttl=10, clock=lambda: now[0])
    cat.search("tcs")
    now[0] = 11
    assert cat.search("tcs")[0]["symbol"] == "TCS"
    assert cat.status()["stale"] is True and cat.status()["error"] == "unavailable"


def test_catalogue_raises_when_nothing_cached_and_provider_down():
    http = FakeHTTP([Resp(status=503, text="nope")])
    cat = InstrumentCatalogue(http=http)
    with pytest.raises(CatalogueError):
        cat.search("tcs")
    assert cat.ready() is False
    assert cat.get("NSE:TCS-EQ") is None          # lookup never explodes
    with pytest.raises(CatalogueError):
        cat.refresh(force=True)


def test_catalogue_network_exception_is_a_catalogue_error():
    import requests
    http = FakeHTTP([requests.ConnectionError("boom")])
    with pytest.raises(CatalogueError):
        InstrumentCatalogue(http=http).search("tcs")


def test_get_rejects_malformed_or_unknown_symbols():
    cat = InstrumentCatalogue(http=FakeHTTP(), records=parse_master(MASTER))
    assert cat.get("NSE:TCS-EQ")["symbol"] == "TCS"
    assert cat.get("nse:tcs-eq")["symbol"] == "TCS"
    for bad in ("NSE:TOTALLYMADEUP-EQ", "TCS", None, 42, "http://evil/x", "NSE:TCS"):
        assert cat.get(bad) is None
    assert cat.get(" NSE:TCS-EQ ")["symbol"] == "TCS"


def test_get_if_fresh_never_triggers_a_download():
    http = FakeHTTP([Resp(text=MASTER)])
    cat = InstrumentCatalogue(http=http)
    assert cat.get_if_fresh("NSE:TCS-EQ") is None
    assert http.calls == []


def test_search_short_query_does_not_download():
    http = FakeHTTP([Resp(text=MASTER)])
    cat = InstrumentCatalogue(http=http)
    assert cat.search("t") == []
    assert http.calls == []


def test_max_results_is_bounded():
    recs = [{"symbol": f"AAA{i}", "name": f"AAA {i} LIMITED", "provider_symbol": f"NSE:AAA{i}-EQ",
             "exchange": "NSE"} for i in range(50)]
    assert len(rank(recs, "AAA")) == MAX_RESULTS


# ---------- API ----------
def test_search_requires_min_length_and_returns_normalized_fields(mk):
    cat = InstrumentCatalogue(http=FakeHTTP(), records=parse_master(MASTER))
    app, _ = mk(deps={"catalogue": cat})
    c = app.test_client()
    assert c.get("/api/instruments/search", base_url=LOCAL).json == {"results": [], "min_query": 2}
    assert c.get("/api/instruments/search?q=t", base_url=LOCAL).json["results"] == []
    d = c.get("/api/instruments/search?q=tcs", base_url=LOCAL).json
    assert d["results"][0] == {"symbol": "TCS", "name": "TATA CONSULTANCY SERVICES LIMITED", "exchange": "NSE",
                               "asset_type": "EQUITY", "provider": "fyers", "provider_symbol": "NSE:TCS-EQ"}
    assert "isin" not in d["results"][0] and d["stale"] is False
    assert c.get("/api/instruments/search?q=" + "a" * 41, base_url=LOCAL).status_code == 400


def test_search_reports_unavailable_catalogue_safely(mk):
    cat = InstrumentCatalogue(http=FakeHTTP([Resp(status=500, text="<html>err</html>")]))
    app, _ = mk(deps={"catalogue": cat})
    r = app.test_client().get("/api/instruments/search?q=tcs", base_url=LOCAL)
    assert r.status_code == 503 and r.json["error"] == "catalogue_unavailable"
    assert "<html>" not in r.json["message"]


def test_analyze_selected_rejects_spoofed_instrument(mk):
    cat = InstrumentCatalogue(http=FakeHTTP(), records=parse_master(MASTER))
    app, _ = mk(deps={"catalogue": cat})
    c = app.test_client()
    hdr = {"X-CSRF-Token": _csrf(c)}
    for body in ({}, {"provider_symbol": ""}, {"provider_symbol": 42}, {"provider_symbol": "NSE:MADEUP-EQ"},
                 {"provider_symbol": "NSE:TCS-EQ&url=http://evil"}, {"provider_symbol": "x" * 61}, ["nse:tcs-eq"]):
        r = c.post("/api/analyze-selected", json=body, headers=hdr, base_url=LOCAL)
        assert r.status_code == 400, body
    assert app.extensions["pipeline"].running is False


def test_analyze_selected_without_csrf_is_blocked(mk):
    cat = InstrumentCatalogue(http=FakeHTTP(), records=parse_master(MASTER))
    app, _ = mk(deps={"catalogue": cat})
    c = app.test_client()
    r = c.post("/api/analyze-selected", json={"provider_symbol": "NSE:TCS-EQ"}, base_url=LOCAL)
    assert r.status_code == 403
    assert app.extensions["pipeline"].running is False


def _csrf(client):
    import re
    html = client.get("/", base_url=LOCAL).get_data(as_text=True)
    return re.search(r'name="csrf" content="([^"]+)"', html).group(1)


def test_status_exposes_search_state(mk):
    cat = InstrumentCatalogue(http=FakeHTTP(), records=parse_master(MASTER))
    app, _ = mk(deps={"catalogue": cat})
    d = app.test_client().get("/api/status", base_url=LOCAL).json
    assert d["scope"] == "universe" and d["search"]["ready"] is True and d["search"]["stale"] is False
    assert d["search"]["min_query"] == 2


def test_yf_symbol_mapping_never_guesses():
    assert ds.yf_symbol_for("NSE:TCS-EQ") == "TCS.NS"
    assert ds.yf_symbol_for("NSE:BAJAJ-AUTO-EQ") == "BAJAJ-AUTO.NS"
    assert ds.yf_symbol_for("NSE:M&M-EQ") == "M&M.NS"
    assert ds.yf_symbol_for("NSE:XYZ-BE-EQ") == "XYZ-BE.NS"     # T2T series is still the same equity
    assert ds.yf_symbol_for("NSE:NIFTYBEES-EQ") == "NIFTYBEES.NS"  # -EQ is a series, not an ETF marker
    assert ds.yf_symbol_for("NSE:XYZ-ETF-EQ") is None
    assert ds.yf_symbol_for("BSE:TCS-A") is None
    assert ds.yf_symbol_for(None) is None
