"""Index regime from completed daily candles. VIX is an explicit gap unless a free series is supplied."""
from evidence import _day, _r, now_ist, sma

SMA_FAST, SMA_SLOW, RET_N = 50, 200, 20


def from_candles(candles, live=None, vix=None):
    """`live` is appended only when it is the current day's price: a last-close quote is already the final candle,
    and appending it twice flattens the 20-day return."""
    candles = [c for c in candles or [] if c.get("c") is not None]
    closes = [c["c"] for c in candles]
    if live is not None and (not candles or _day(candles[-1]) < now_ist().date()):
        closes = closes + [live]
    px = closes[-1] if closes else live
    s50, s200 = sma(closes, SMA_FAST), sma(closes, SMA_SLOW)
    base = closes[-1 - RET_N] if len(closes) > RET_N else None
    ret = _r((px - base) / base * 100) if px and base else None
    if px is None or s50 is None:
        label, why = "unknown", "insufficient index history"
    elif s200 is not None and px < s200 and ret is not None and ret < 0:
        label, why = "risk_off", "index below 200-day SMA with a negative 20-day return"
    elif px > s50 and (ret is None or ret >= 0):
        label, why = "risk_on", "index above 50-day SMA"
    else:
        label, why = "neutral", "mixed trend/return"
    return {"label": label, "why": why, "sma50": _r(s50, 2), "sma200": _r(s200, 2),
            "ret_20d": ret, "price": _r(px, 2), "vix": vix, "vix_gap": vix is None,
            "vix_note": None if vix is not None else "India VIX needs a licensed NSE feed; no free series is wired in.",
            "source": "NIFTYBEES candles (index ETF proxy)"}


def caps_buy(reg):
    return bool(reg) and reg.get("label") == "risk_off"
