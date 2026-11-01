"""Conservative, explainable daily chart findings.

The module is deliberately provider-neutral: it consumes completed OHLCV bars
and abstains when the history is too short or a rule is ambiguous.
"""
from datetime import datetime, timezone
from math import isfinite

DETECTOR_VERSION = "1"
MIN_BARS = 30


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def normalize(candles):
    """Return sorted, de-duplicated valid candles with finite OHLCV values."""
    out = {}
    for c in candles or []:
        if not isinstance(c, dict) or not _number(c.get("t")):
            continue
        vals = {k: c.get(k) for k in ("o", "h", "l", "c", "v")}
        if not all(_number(v) for v in vals.values()):
            continue
        if vals["h"] < max(vals["o"], vals["c"]) or vals["l"] > min(vals["o"], vals["c"]):
            continue
        if vals["h"] < vals["l"] or vals["v"] < 0:
            continue
        out[int(c["t"])] = {"t": int(c["t"]), **vals}
    return [out[t] for t in sorted(out)]


def _date(c):
    return datetime.fromtimestamp(c["t"], timezone.utc).date().isoformat()


def _finding(name, direction, status, start, end, evidence, reason):
    return {"name": name, "direction": direction, "status": status, "confidence": None,
            "start_date": start, "end_date": end, "evidence": evidence, "reason": reason,
            "detector_version": DETECTOR_VERSION}


def analyze(candles):
    """Analyze daily completed candles and return a stable, abstaining result."""
    bars = normalize(candles)
    result = {"timeframe": "1D", "detector_version": DETECTOR_VERSION, "available": len(bars) >= MIN_BARS,
              "patterns": [], "limitations": []}
    if len(bars) < MIN_BARS:
        result["limitations"].append(f"need at least {MIN_BARS} valid completed daily candles")
        return result

    last = bars[-1]
    prev = bars[-2]
    body = abs(last["c"] - last["o"])
    spread = last["h"] - last["l"]
    if spread > 0 and body / spread <= 0.1:
        result["patterns"].append(_finding("doji", "neutral", "confirmed", _date(last), _date(last),
                                           {"range": round(spread, 4), "body": round(body, 4)},
                                           "Small real body relative to the completed candle range."))
    if (prev["c"] < prev["o"] and last["c"] > last["o"] and
            last["o"] <= prev["c"] and last["c"] >= prev["o"]):
        result["patterns"].append(_finding("bullish_engulfing", "bullish", "confirmed", _date(prev), _date(last),
                                           {"prior_open": prev["o"], "prior_close": prev["c"],
                                            "open": last["o"], "close": last["c"]},
                                           "The completed bullish body fully engulfs the prior bearish body."))
    if (prev["c"] > prev["o"] and last["c"] < last["o"] and
            last["o"] >= prev["c"] and last["c"] <= prev["o"]):
        result["patterns"].append(_finding("bearish_engulfing", "bearish", "confirmed", _date(prev), _date(last),
                                           {"prior_open": prev["o"], "prior_close": prev["c"],
                                            "open": last["o"], "close": last["c"]},
                                           "The completed bearish body fully engulfs the prior bullish body."))

    pivots = []
    for i in range(2, len(bars) - 2):
        window = bars[i - 2:i + 3]
        if bars[i]["h"] == max(x["h"] for x in window):
            pivots.append(("high", i, bars[i]["h"]))
        if bars[i]["l"] == min(x["l"] for x in window):
            pivots.append(("low", i, bars[i]["l"]))
    highs = [p for p in pivots if p[0] == "high"]
    lows = [p for p in pivots if p[0] == "low"]
    if len(highs) >= 2 and len(lows) >= 2:
        hdir = "higher" if highs[-1][2] > highs[-2][2] else "lower" if highs[-1][2] < highs[-2][2] else "flat"
        ldir = "higher" if lows[-1][2] > lows[-2][2] else "lower" if lows[-1][2] < lows[-2][2] else "flat"
        if hdir == ldir and hdir in ("higher", "lower"):
            direction = "bullish" if hdir == "higher" else "bearish"
            result["patterns"].append(_finding("confirmed_swing_structure", direction, "confirmed",
                                               _date(bars[min(highs[-2][1], lows[-2][1])]), _date(last),
                                               {"highs": [highs[-2][2], highs[-1][2]],
                                                "lows": [lows[-2][2], lows[-1][2]],
                                                "pivot_window": 2},
                                               f"Recent confirmed swing highs and lows are {hdir}."))

    for kind, points in (("support", lows), ("resistance", highs)):
        if len(points) < 2:
            continue
        tolerance = max(last["c"] * 0.01, 0.01)
        if abs(points[-1][2] - points[-2][2]) <= tolerance:
            level = round((points[-1][2] + points[-2][2]) / 2, 4)
            result["patterns"].append(_finding(kind + "_zone", "neutral", "confirmed",
                                               _date(bars[points[-2][1]]), _date(bars[points[-1][1]]),
                                               {"level": level, "tolerance": round(tolerance, 4),
                                                "touches": [points[-2][2], points[-1][2]]},
                                               f"Two separated pivot touches define a {kind} zone."))

    prior = bars[-21:-1]
    resistance = max(x["h"] for x in prior)
    support = min(x["l"] for x in prior)
    avg_volume = sum(x["v"] for x in prior) / len(prior)
    if avg_volume > 0 and last["c"] > resistance and last["v"] >= avg_volume * 1.2:
        result["patterns"].append(_finding("confirmed_breakout", "bullish", "confirmed", _date(prior[0]), _date(last),
                                           {"close": last["c"], "level": resistance,
                                            "volume": last["v"], "average_volume": round(avg_volume, 4),
                                            "volume_multiple": round(last["v"] / avg_volume, 2)},
                                           "A completed close cleared the prior range high with volume confirmation."))
    elif avg_volume > 0 and last["c"] < support and last["v"] >= avg_volume * 1.2:
        result["patterns"].append(_finding("confirmed_breakdown", "bearish", "confirmed", _date(prior[0]), _date(last),
                                           {"close": last["c"], "level": support,
                                            "volume": last["v"], "average_volume": round(avg_volume, 4),
                                            "volume_multiple": round(last["v"] / avg_volume, 2)},
                                           "A completed close broke the prior range low with volume confirmation."))
    if not result["patterns"]:
        result["limitations"].append("no reliable pattern met the confirmation rules")
    return result
