"""Conservative deterministic chart analysis over completed daily candles. Pure Python, no network.
A finding is emitted only when every objective gate passes; otherwise the stage abstains explicitly:
`no_reliable_pattern` (data is fine, nothing passed) or `insufficient_data` (too few valid bars).
Raw candles never leave this module — only the small, serialisable `chart` summary does."""

from datetime import datetime

from evidence import IST

VERSION = "1"
MIN_BARS = 60        # completed daily bars required before any pattern is considered
PIVOT_SPAN = 5       # bars either side of a pivot must be strictly lower/higher
ZONE_TOL = 0.015     # 1.5% groups repeated pivot levels into one support/resistance zone
ZONE_TOUCHES = 2     # pivots needed to validate a zone (a single touch is ignored)
MAX_PATTERNS = 3     # strongest findings only; the stage never dumps a catalogue
CONF = {"swing": 0.7, "zone": 0.6, "breakout": 0.65, "candle": 0.55}

LIMITS = [
    "Completed daily bars only; intraday patterns are out of scope.",
    "Detector rules are deterministic and versioned; confidence is rule strength, not profit probability.",
    "Support/resistance zones require repeated pivots within 1.5%; single touches are ignored.",
]


def _r(x, n=2):
    return round(float(x), n)


def _date(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")


def _valid(c):
    try:
        return (all(isinstance(c[k], (int, float)) and c[k] == c[k] for k in ("t", "o", "h", "l", "c"))
                and c["h"] > c["l"] and c["h"] >= max(c["o"], c["c"]) and c["l"] <= min(c["o"], c["c"])
                and (c.get("v") or 0) > 0)
    except (TypeError, KeyError, ValueError):
        return False


def _clean(candles, now, session):
    """Ordered completed bars only: dedupe by timestamp, drop null/non-finite and zero-range/zero-volume
    bars, and drop today's live bar while the market is open (a partial candle must never confirm a pattern)."""
    seen, out = set(), []
    for c in sorted(candles, key=lambda c: c["t"]):
        if c["t"] in seen or not _valid(c):
            continue
        seen.add(c["t"])
        if session == "open" and datetime.fromtimestamp(c["t"], IST).date() == now.date():
            continue
        out.append(c)
    return out


def _fractals(candles):
    """Strict swing pivots: the bar's high/low must exceed every bar within PIVOT_SPAN on both sides."""
    highs, lows = [], []
    for i in range(PIVOT_SPAN, len(candles) - PIVOT_SPAN):
        w = candles[i - PIVOT_SPAN:i + PIVOT_SPAN + 1]
        if all(candles[i]["h"] > c["h"] for c in w if c is not candles[i]):
            highs.append((i, candles[i]["h"]))
        if all(candles[i]["l"] < c["l"] for c in w if c is not candles[i]):
            lows.append((i, candles[i]["l"]))
    return highs, lows


def _swing(candles, highs, lows):
    """Confirmed swing structure: the last two highs AND lows must trend the same way."""
    if len(highs) < 2 or len(lows) < 2:
        return None
    up = highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]
    down = highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]
    if not (up or down):
        return None
    return {"name": "swing_structure", "direction": "bullish" if up else "bearish", "status": "confirmed",
            "confidence": CONF["swing"], "start_date": _date(candles[highs[-2][0]]["t"]),
            "end_date": _date(candles[highs[-1][0]]["t"]),
            "levels": [_r(highs[-1][1]), _r(lows[-1][1])],
            "evidence": {"swing_highs": [[_date(candles[i]["t"]), _r(v)] for i, v in highs[-2:]],
                         "swing_lows": [[_date(candles[i]["t"]), _r(v)] for i, v in lows[-2:]]}}


def _zones(candles, highs, lows):
    """Support/resistance zones from repeated, separated pivots within ZONE_TOL of a running mean level."""
    out = []
    for name, pivs in (("resistance_zone", highs), ("support_zone", lows)):
        clusters = []
        for i, lv in pivs:
            for cl in clusters:
                if abs(cl["level"] - lv) / cl["level"] <= ZONE_TOL:
                    cl["level"] = (cl["level"] * cl["touches"] + lv) / (cl["touches"] + 1)
                    cl["touches"] += 1
                    cl["end_date"] = _date(candles[i]["t"])
                    cl["pivots"].append([_date(candles[i]["t"]), _r(lv)])
                    break
            else:
                clusters.append({"level": lv, "touches": 1, "start_date": _date(candles[i]["t"]),
                                 "end_date": _date(candles[i]["t"]),
                                 "pivots": [[_date(candles[i]["t"]), _r(lv)]]})
        for cl in clusters:
            if cl["touches"] >= ZONE_TOUCHES:
                out.append({"name": name, "direction": "bullish" if name == "support_zone" else "bearish",
                            "status": "confirmed", "confidence": CONF["zone"],
                            "start_date": cl["start_date"], "end_date": cl["end_date"],
                            "levels": [_r(cl["level"])],
                            "evidence": {"touches": cl["touches"], "pivots": cl["pivots"]}})
    return out


def _breakouts(candles, live, zones):
    """Breakout/breakdown only beyond a VALIDATED zone level. Confirmed needs a completed close beyond;
    a live quote beyond with no such close is merely forming."""
    out, last = [], candles[-1]
    closes = [c["c"] for c in candles]
    for z in zones:
        lv = z["levels"][0]
        closed = last["c"] > lv if z["name"] == "resistance_zone" else last["c"] < lv
        beyond = all(c <= lv for c in closes[:-1]) if z["name"] == "resistance_zone" else all(c >= lv for c in closes[:-1])
        if not beyond:
            continue
        forming = live is not None and (live > lv if z["name"] == "resistance_zone" else live < lv) and not closed
        if not closed and not forming:
            continue
        out.append({"name": "breakout" if z["name"] == "resistance_zone" else "breakdown",
                    "direction": "bullish" if z["name"] == "resistance_zone" else "bearish",
                    "status": "confirmed" if closed else "forming",
                    "confidence": CONF["breakout"] if closed else _r(CONF["breakout"] - 0.15),
                    "start_date": z["end_date"], "end_date": _date(last["t"]), "levels": [lv],
                    "evidence": {"level_source": z["name"], "touches": z["evidence"]["touches"],
                                 "confirmation": "close_beyond_level" if closed else "live_price_beyond_level"}})
    return out


def _candle(candles):
    """A small, well-defined set on the last completed bar: doji, hammer, shooting star, engulfing."""
    out = []
    if len(candles) < 2:
        return out
    a, b = candles[-2], candles[-1]
    body, rng = abs(b["c"] - b["o"]), b["h"] - b["l"]
    if rng <= 0:
        return out
    def pat(name, direction, level, rule):
        return {"name": name, "direction": direction, "status": "confirmed", "confidence": CONF["candle"],
                "start_date": _date(b["t"]), "end_date": _date(b["t"]), "levels": [_r(level)],
                "evidence": {"rule": rule}}
    if body <= rng * 0.1:
        out.append(pat("doji", "neutral", b["c"], "body <= 10% of range"))
    else:
        up_shadow, dn_shadow = b["h"] - max(b["c"], b["o"]), min(b["c"], b["o"]) - b["l"]
        if dn_shadow >= body * 2 and up_shadow <= body:
            out.append(pat("hammer", "bullish", b["l"], "lower shadow >= 2x body"))
        elif up_shadow >= body * 2 and dn_shadow <= body:
            out.append(pat("shooting_star", "bearish", b["h"], "upper shadow >= 2x body"))
    abody = abs(a["c"] - a["o"])
    bull, prev_bear = b["c"] > b["o"], a["c"] < a["o"]
    if abody > 0 and bull and prev_bear and b["c"] >= a["o"] and b["o"] <= a["c"] and body > abody:
        out.append(pat("bullish_engulfing", "bullish", b["c"], "body engulfs the previous bearish bar"))
    elif abody > 0 and not bull and a["c"] > a["o"] and b["c"] <= a["o"] and b["o"] >= a["c"] and body > abody:
        out.append(pat("bearish_engulfing", "bearish", b["h"], "body engulfs the previous bullish bar"))
    return out


def analyze(candles, live=None, session="closed", now=None):
    """Return (chart, gaps). `chart` is the compact report; gaps names missing data explicitly."""
    n = now or datetime.now(IST)
    bars = _clean(candles or [], n, session)
    base = {"timeframe": "1D", "detector_version": VERSION, "patterns": [], "limitations": LIMITS}
    if len(bars) < MIN_BARS:
        return ({**base, "available": False, "status": "insufficient_data",
                 "summary": f"Only {len(bars)} valid completed daily bars; {MIN_BARS} are required."},
                ["chart.insufficient_history"])
    highs, lows = _fractals(bars)
    pats = [p for p in (_swing(bars, highs, lows),) if p]
    zones = _zones(bars, highs, lows)
    pats += zones + _breakouts(bars, live, zones) + _candle(bars)
    pats = sorted(pats, key=lambda p: -p["confidence"])[:MAX_PATTERNS]
    if not pats:
        return ({**base, "available": True, "status": "no_reliable_pattern",
                 "summary": "No reliable pattern identified; the stage abstains rather than guess."}, [])
    names = ", ".join(f"{p['name'].replace('_', ' ')} ({p['status']})" for p in pats)
    return ({**base, "available": True, "status": "ok", "patterns": pats, "summary": f"Detected: {names}."}, [])
