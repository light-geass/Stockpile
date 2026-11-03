"""FYERS market-data adapter (read-only). Endpoints verified against the official fyers-apiv3 SDK 3.2.0
source and the docs: GET https://api-t1.fyers.in/data/quotes, GET .../data/history.
NO order/trading endpoints are implemented anywhere in this project."""
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import requests

from fyers_auth import EXPIRED_CODES, IST

DATA_BASE = "https://api-t1.fyers.in/data"
PROFILE_URL = "https://api-t1.fyers.in/api/v3/profile"


class FyersError(Exception):
    def __init__(self, kind, message, code=None):
        super().__init__(message)
        self.kind, self.message, self.code = kind, message, code


class RateLimiter:
    """Conservative client-side limiter: min gap between calls, per-minute window, per-day cap.
    FYERS Standard plan: 5/s, 50/min, 5000/day for data APIs; >3 per-minute breaches/day => blocked for the day."""

    def __init__(self, per_second=4, per_minute=40, per_day=4500, clock=time.monotonic, sleep=time.sleep, max_wait=75):
        self.gap = 1.0 / per_second
        self.per_minute, self.per_day = per_minute, per_day
        self.clock, self.sleep, self.max_wait = clock, sleep, max_wait
        self.calls = deque()
        self.last = None
        self.today = 0
        self.lock = threading.Lock()

    def acquire(self):
        waited = 0.0
        while True:
            with self.lock:
                if self.today >= self.per_day:
                    raise FyersError("rate_limited", "Local daily FYERS request budget reached.")
                now = self.clock()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                need = 0.0
                if self.last is not None:
                    need = max(need, self.gap - (now - self.last))
                if len(self.calls) >= self.per_minute:
                    need = max(need, 60 - (now - self.calls[0]) + 0.05)
                if need <= 0:
                    self.last = now
                    self.calls.append(now)
                    self.today += 1
                    return
            if waited + need > self.max_wait:
                raise FyersError("rate_limited", "Waiting for the FYERS rate window took too long.")
            self.sleep(need)
            waited += need


def _kind(code, msg):
    m = (msg or "").lower()
    if code in EXPIRED_CODES or "token" in m and ("expired" in m or "invalid" in m):
        return "auth_expired"
    if code == 429 or code == -429 or "limit" in m:
        return "rate_limited"
    if "permission" in m or "not allowed" in m or "unauthori" in m:
        return "permission"
    return "api_error"


class FyersClient:
    def __init__(self, app_id, token_fn, limiter=None, http=requests, timeout=20):
        self.app_id, self.token_fn, self.http, self.timeout = app_id, token_fn, http, timeout
        self.limiter = limiter or RateLimiter()

    def _get(self, path, params):
        tok = self.token_fn()
        if not tok:
            raise FyersError("auth_expired", "No valid FYERS token. Connect FYERS first.")
        self.limiter.acquire()
        headers = {"Authorization": f"{self.app_id}:{tok}", "Content-Type": "application/json", "version": "3"}
        try:
            r = self.http.get(f"{DATA_BASE}{path}", params=params, headers=headers, timeout=self.timeout)
        except requests.RequestException:
            raise FyersError("unavailable", "FYERS data API unreachable.")
        # Status first: rate-limit replies are often an HTML/empty body, so parsing earlier reports the wrong cause.
        if r.status_code == 429:
            raise FyersError("rate_limited", "FYERS rate limit hit (HTTP 429; 5 req/s, 50/min on the Standard plan). "
                                             "Wait a minute and run again, or redeploy and click Connect FYERS.", 429)
        try:
            data = r.json()
        except ValueError:
            raise FyersError("unavailable", f"FYERS returned a non-JSON response (HTTP {r.status_code}).")
        if not isinstance(data, dict) or data.get("s") == "error" or (data.get("code") not in (200, None)):
            code, msg = (data or {}).get("code"), (data or {}).get("message", "")
            kind = _kind(code, msg)
            hint = {"permission": " Check the app has Market Data permission.",
                    "auth_expired": " Token expired or invalid; reconnect FYERS."}.get(kind, "")
            raise FyersError(kind, f"FYERS error {code}: {msg}.{hint}".strip(), code)
        return data

    # ---- quotes: up to 50 symbols/request on Standard plan ----
    def quotes(self, fyers_symbols):
        out = {}
        for i in range(0, len(fyers_symbols), 50):
            chunk = fyers_symbols[i:i + 50]
            data = self._get("/quotes", {"symbols": ",".join(chunk)})
            for item in data.get("d", []) or []:
                if item.get("s") != "ok":
                    continue
                v = item.get("v") or {}
                out[item.get("n")] = {
                    "symbol": item.get("n"),
                    "live": v.get("lp"), "day_open": v.get("open_price"), "day_high": v.get("high_price"),
                    "day_low": v.get("low_price"), "prev_close": v.get("prev_close_price"),
                    "day_change_pct": v.get("chp"), "volume": v.get("volume"),
                    "quote_epoch": quote_epoch(v.get("tt")),
                }
        return out

    # ---- candles ----
    def profile(self):
        """Cheap liveness check (GET /profile, not a data-API call, so it does not use the quote rate budget).
        A stored token can be rejected by FYERS (new login, app change/redeploy) while still looking valid locally."""
        tok = self.token_fn()
        if not tok:
            return False, "no_token"
        try:
            r = self.http.get(f"{PROFILE_URL}", headers={"Authorization": f"{self.app_id}:{tok}", "version": "3"},
                              timeout=10)
        except requests.RequestException:
            return False, "unreachable"
        if r.status_code == 429:
            return False, "rate_limited"
        try:
            data = r.json()
        except ValueError:
            return False, "unreachable"
        if isinstance(data, dict) and data.get("s") == "ok" and data.get("code") in (200, None):
            return True, None
        msg = str((data or {}).get("message", "")) if isinstance(data, dict) else ""
        code = (data or {}).get("code") if isinstance(data, dict) else None
        return False, "auth_expired" if _kind(code, msg) == "auth_expired" else "unavailable"

    def daily_candles(self, sym, days=366):
        today = datetime.now(IST).date()
        data = self._get("/history", {"symbol": sym, "resolution": "D", "date_format": "1",
                                      "range_from": (today - timedelta(days=days - 1)).isoformat(),
                                      "range_to": today.isoformat(), "cont_flag": "1"})
        return _candles(data)

    def intraday_candles(self, sym, calendar_days=14, resolution="5"):
        now = int(time.time())
        data = self._get("/history", {"symbol": sym, "resolution": resolution, "date_format": "0",
                                      "range_from": str(now - calendar_days * 86400),
                                      "range_to": str(now - 60 - int(resolution) * 60),  # completed candles only
                                      "cont_flag": "1"})
        return _candles(data)


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def quote_epoch(tt):
    """FYERS sends `tt` as a plain epoch. Its own sample payload shows `tt` as the day's 00:00 UTC while the quote
    was taken at 20:07 IST, so `tt` is NOT a reliable quote time and must never drive 'stale': a wrong reading
    produced a false 'quotes are 28000s old, check your PC clock' alert and blocked every BUY."""
    return _int(tt)


def _candles(data):
    out = []
    for c in data.get("candles", []) or []:
        if isinstance(c, (list, tuple)) and len(c) >= 6:
            out.append({"t": int(c[0]), "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
    return out
