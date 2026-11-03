"""Chart Analyst detector tests: every accepted pattern, near-misses, abstention, and hygiene rules.
All fixtures are synthetic, time-ordered daily candles; no network, no provider."""
from datetime import datetime, timedelta

import chart_patterns as cp


def candles(bars, start="2026-09-01"):
    """bars: list of (o,h,l,c[,v]) oldest-first; returns candle dicts on consecutive sessions."""
    d0 = datetime.strptime(start, "%Y-%m-%d")
    out = []
    for i, b in enumerate(bars):
        out.append({"t": int((d0 + timedelta(days=i)).timestamp()),
                    "o": b[0], "h": b[1], "l": b[2], "c": b[3], "v": b[4] if len(b) > 4 else 1_000_000})
    return out


def flat(n=66, px=100.0):
    return candles([(px, px + 1, px - 1, px) for _ in range(n)])


def zigzag(n=72, base=200.0, rise=1.5, amp=4.0, period=12, offset=6):
    """Triangle wave with an upward drift: strict swing highs at m=offset, swing lows at m=0 of each cycle."""
    return candles([(base + rise * k + amp * (m if m < 6 else 12 - m) - 0.5,
                     base + rise * k + amp * (m if m < 6 else 12 - m) + 1,
                     base + rise * k + amp * (m if m < 6 else 12 - m) - 1,
                     base + rise * k + amp * (m if m < 6 else 12 - m))
                    for k, m in ((k, k % period) for k in range(n))])


def test_insufficient_history_abstains():
    chart, gaps = cp.analyze(flat(n=59))
    assert chart["status"] == "insufficient_data" and chart["available"] is False and chart["patterns"] == []
    assert "chart.insufficient_history" in gaps


def test_invalid_bars_are_dropped_and_can_abstain():
    bars = flat(n=62)
    bars[10].update(h=None)                       # null high
    bars[20].update(h=bars[20]["l"])              # zero range
    bars[30].update(c=float("nan"))               # non-finite close
    bars[40]["v"] = 0                             # zero volume
    chart, _ = cp.analyze(bars)
    assert chart["status"] == "insufficient_data"  # 62 - 4 = 58 < MIN_BARS


def test_uptrend_swing_structure_confirmed():
    chart, _ = cp.analyze(zigzag())
    names = [p["name"] for p in chart["patterns"]]
    assert chart["status"] == "ok" and "swing_structure" in names
    sw = next(p for p in chart["patterns"] if p["name"] == "swing_structure")
    assert sw["direction"] == "bullish" and sw["status"] == "confirmed" and len(sw["levels"]) == 2


def test_downtrend_swing_structure_confirmed():
    bars = zigzag()
    rev = []
    for c in bars:
        rev.append({"t": c["t"], "o": 500 - c["o"], "h": 500 - c["l"], "l": 500 - c["h"], "c": 500 - c["c"], "v": c["v"]})
    chart, _ = cp.analyze(rev)
    sw = next((p for p in chart["patterns"] if p["name"] == "swing_structure"), None)
    assert sw is not None and sw["direction"] == "bearish"


def test_zone_and_breakout_confirmed():
    """Repeated highs near 105 form a validated resistance zone; a completed close beyond it is a breakout."""
    bars = flat(n=64, px=100.0)
    for i in (10, 22, 34, 46):
        bars[i].update(h=105.5, c=104.5, o=104.0)       # four pivots within tolerance
        bars[i + 1].update(h=105.4, c=104.4, o=104.1)
    bars[-2].update(o=104.0, h=105.2, l=103.5, c=104.6)
    bars[-1].update(o=104.6, h=110.0, l=104.2, c=109.5)  # completed close beyond the zone
    chart, _ = cp.analyze(bars)
    br = next((p for p in chart["patterns"] if p["name"] == "breakout"), None)
    assert br is not None and br["status"] == "confirmed" and br["direction"] == "bullish"
    zone = next((p for p in chart["patterns"] if p["name"] == "resistance_zone"), None)
    assert zone is not None and zone["evidence"]["touches"] >= cp.ZONE_TOUCHES


def test_breakout_forming_without_completed_close():
    bars = flat(n=64, px=100.0)
    for i in (10, 22, 34, 46):
        bars[i].update(h=105.5, c=104.5, o=104.0)
    chart, _ = cp.analyze(bars, live=107.0)  # live quote beyond, last close below
    br = next((p for p in chart["patterns"] if p["name"] == "breakout"), None)
    assert br is not None and br["status"] == "forming" and br["confidence"] < cp.CONF["breakout"]


def test_near_miss_close_does_not_breakout():
    bars = flat(n=64, px=100.0)
    for i in (10, 22, 34, 46):
        bars[i].update(h=105.5, c=104.5, o=104.0)
    bars[-1].update(o=104.0, h=105.7, l=104.0, c=105.4)  # close just below the zone level: not beyond
    chart, _ = cp.analyze(bars)
    assert not any(p["name"] in ("breakout", "breakdown") for p in chart["patterns"])


def test_single_touch_zone_is_ignored():
    bars = flat(n=64, px=100.0)
    bars[10].update(h=110.0, c=109.0, o=108.0)  # one lonely pivot: no validated zone
    bars[-1].update(o=109.0, h=115.0, l=108.5, c=114.0)
    chart, _ = cp.analyze(bars)
    assert not any(p["name"] in ("resistance_zone", "breakout") for p in chart["patterns"])


def test_candle_patterns_hammer_and_star():
    hammer = flat(n=64)
    hammer[-1].update(o=100.0, h=101.2, l=97.8, c=101.0)   # lower shadow 2.2 >= 2x body 1
    chart, _ = cp.analyze(hammer)
    assert any(p["name"] == "hammer" and p["direction"] == "bullish" for p in chart["patterns"])
    star = flat(n=64)
    star[-1].update(o=100.0, h=103.0, l=98.6, c=99.0)        # upper shadow 3 >= 2x body 1, lower shadow 0.4
    chart, _ = cp.analyze(star)
    assert any(p["name"] == "shooting_star" and p["direction"] == "bearish" for p in chart["patterns"])


def test_candle_patterns_engulfing_and_doji():
    eng = flat(n=64)
    eng[-2].update(o=102.0, h=102.5, l=99.5, c=100.0)       # bearish bar, body 2
    eng[-1].update(o=99.5, h=103.0, l=99.0, c=102.5)        # bullish bar engulfs it, body 3
    chart, _ = cp.analyze(eng)
    assert any(p["name"] == "bullish_engulfing" for p in chart["patterns"])
    doji = flat(n=64)
    doji[-1].update(o=100.0, h=101.0, l=99.0, c=100.05)     # body 0.05 <= 10% of range 2
    chart, _ = cp.analyze(doji)
    assert any(p["name"] == "doji" and p["direction"] == "neutral" for p in chart["patterns"])


def test_todays_partial_bar_is_excluded_when_market_open():
    bars = flat(n=60)
    now = datetime.now(cp.IST)
    bars[-1]["t"] = int(now.replace(hour=9, minute=30).timestamp())  # today's live bar
    chart, _ = cp.analyze(bars, session="open", now=now)
    assert chart["status"] == "insufficient_data"  # 60 - 1 = 59 < MIN_BARS
    chart, _ = cp.analyze(bars, session="closed", now=now)  # closed: today's bar is a completed session
    assert chart["status"] != "insufficient_data"


def test_noise_abstains_without_fabricated_patterns():
    import random
    rnd = random.Random(7)
    bars = []
    px = 500.0
    for i in range(70):
        px += rnd.uniform(-3, 3)
        bars.append((px - 0.5, px + rnd.uniform(0.5, 2.0), px - rnd.uniform(0.5, 2.0), px))
    chart, _ = cp.analyze(candles(bars))
    for p in chart["patterns"]:
        assert p["name"] in ("swing_structure", "resistance_zone", "support_zone", "breakout",
                             "breakdown", "doji", "hammer", "shooting_star", "bullish_engulfing", "bearish_engulfing")
        assert p["status"] in ("confirmed", "forming") and 0 <= p["confidence"] <= 1 and p["levels"]


def test_deterministic_and_versioned():
    bars = zigzag()
    a, _ = cp.analyze(bars)
    b, _ = cp.analyze(bars)
    assert a == b and a["detector_version"] == cp.VERSION and a["timeframe"] == "1D"


def test_breakdown_confirmed_below_support():
    bars = flat(n=64, px=100.0)
    for i in (10, 22, 34, 46):
        bars[i].update(l=94.5, c=95.5, o=95.8)       # repeated lows form support
        bars[i + 1].update(l=94.4, c=95.4, o=95.7)
    bars[-1].update(o=95.0, h=95.2, l=90.0, c=90.5)   # completed close below the zone
    chart, _ = cp.analyze(bars)
    br = next((p for p in chart["patterns"] if p["name"] == "breakdown"), None)
    assert br is not None and br["status"] == "confirmed" and br["direction"] == "bearish"
