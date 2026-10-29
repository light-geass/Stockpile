import requests

from fyers_client import FyersClient, FyersError, RateLimiter
from tests.conftest import FakeHTTP, Resp

QUOTE_OK = {"s": "ok", "code": 200, "d": [{"n": "NSE:SBIN-EQ", "s": "ok", "v": {
    "lp": 800.5, "open_price": 790, "high_price": 805, "low_price": 788, "prev_close_price": 785, "chp": 1.97, "volume": 1234567, "tt": 1760000000}},
    {"n": "NSE:BAD-EQ", "s": "error", "v": {}}]}


def client(queue, tok="TOK"):
    http = FakeHTTP(queue)
    lim = RateLimiter(per_second=1000, per_minute=1000, per_day=1000, sleep=lambda s: None)
    return FyersClient("APP-100", lambda: tok, lim, http), http


def test_quotes_parse_and_auth_header():
    c, http = client([Resp(QUOTE_OK)])
    q = c.quotes(["NSE:SBIN-EQ", "NSE:BAD-EQ"])
    assert list(q) == ["NSE:SBIN-EQ"] and q["NSE:SBIN-EQ"]["live"] == 800.5 and q["NSE:SBIN-EQ"]["quote_epoch"] == 1760000000
    _, url, kw = http.calls[0]
    assert url == "https://api-t1.fyers.in/data/quotes" and kw["headers"]["Authorization"] == "APP-100:TOK"
    assert kw["params"]["symbols"] == "NSE:SBIN-EQ,NSE:BAD-EQ"


def test_quotes_chunked_by_50():
    c, http = client([Resp({"s": "ok", "d": []}), Resp({"s": "ok", "d": []})])
    c.quotes([f"NSE:S{i}-EQ" for i in range(75)])
    assert len(http.calls) == 2


def test_error_kinds():
    for resp, kind in [(Resp({"s": "error", "code": -16, "message": "Could not authenticate"}), "auth_expired"),
                       (Resp({"s": "error", "code": 429, "message": "limit"}, status=429), "rate_limited"),
                       (Resp(None, raise_json=True, status=502), "unavailable"),
                       (Resp({"s": "error", "code": -99, "message": "no permission for app"}), "permission")]:
        c, _ = client([resp])
        try:
            c.quotes(["NSE:X-EQ"])
            assert False
        except FyersError as e:
            assert e.kind == kind


def test_network_failure_and_missing_token():
    c, _ = client([requests.ConnectionError("x")])
    try:
        c.quotes(["a"]); assert False
    except FyersError as e:
        assert e.kind == "unavailable"
    c, http = client([], tok=None)
    try:
        c.quotes(["a"]); assert False
    except FyersError as e:
        assert e.kind == "auth_expired" and http.calls == []


def test_history_params_and_parse():
    c, http = client([Resp({"s": "ok", "candles": [[1700000000, 1, 2, 0.5, 1.5, 1000], [1700086400, 1, 2, 0.5, 1.6, 900]]})])
    out = c.daily_candles("NSE:SBIN-EQ")
    assert out[0] == {"t": 1700000000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1000}
    p = http.calls[0][2]["params"]
    assert http.calls[0][1].endswith("/data/history") and p["resolution"] == "D" and p["date_format"] == "1" and p["cont_flag"] == "1"


def test_rate_limiter_spaces_calls_and_caps_minute_and_day():
    t = [0.0]
    sleeps = []

    def sleep(x):
        sleeps.append(x); t[0] += x
    lim = RateLimiter(per_second=4, per_minute=3, per_day=5, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        lim.acquire()
    assert all(abs(s - 0.25) < 1e-6 for s in sleeps[:2])
    lim.acquire()  # 4th call must wait for the 60s window
    assert max(sleeps) > 50
    lim.acquire()
    try:
        lim.acquire(); assert False
    except FyersError as e:
        assert e.kind == "rate_limited"
