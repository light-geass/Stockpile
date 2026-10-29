"""Deterministic agents + judge. Pure Python, no LLM. Always available as the fallback engine.
Bull and Bear each score 0-100 from fixed rules (weights sum to 100). Missing data earns no points."""
from grounding import fid


def _v(b, path):
    cur = b
    for p in path.split("."):
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur


def _fmt(x, nd=1):
    return f"{x:.{nd}f}"


def score_bull(b, facts):
    pts, why = 0, []

    def add(n, text, path):
        nonlocal pts
        pts += n
        why.append({"points": round(n, 1), "text": text, "evidence": [i for i in [fid(facts, path)] if i]})

    rv = _v(b, "technicals.rvol")
    if rv is not None and rv > 0:
        add(min(rv, 4) / 4 * 25, f"RVOL {_fmt(rv, 2)}x shows participation", "technicals.rvol")
    pos = _v(b, "range_52w.position_pct")
    if pos is not None and pos >= 85:
        add(15, f"Trades at {_fmt(pos)}% of its 52-week range (breakout zone)", "range_52w.position_pct")
    pv = _v(b, "technicals.price_vs_sma_pct")
    if pv is not None and pv > 0 and _v(b, "technicals.trend") == "up":
        add(15, f"{_fmt(pv)}% above a rising 10-day SMA", "technicals.price_vs_sma_pct")
    dr = _v(b, "technicals.day_range_position_pct")
    if dr is not None and dr >= 70:
        add(10, f"Holding the top of the day range ({_fmt(dr)}%)", "technicals.day_range_position_pct")
    up = _v(b, "analyst.upside_pct")
    if up is not None and up >= 10:
        add(10, f"Analyst mean target implies {_fmt(up)}% upside", "analyst.upside_pct")
    bp = _v(b, "analyst.buy_pct")
    if bp is not None and bp >= 80:
        add(10, f"{_fmt(bp)}% of analysts rate it a buy", "analyst.buy_pct")
    n = b.get("news") or {}
    if n.get("positive", 0) > n.get("negative", 0):
        add(10, f"Headline tone leans positive ({n['positive']} vs {n['negative']})", "news.positive")
    wr = _v(b, "technicals.window_return_pct")
    if wr is not None and wr > 0:
        add(5, f"Up {_fmt(wr)}% over about a month", "technicals.window_return_pct")
    return min(100, round(pts)), why


def score_bear(b, facts):
    pts, why = 0, []

    def add(n, text, path):
        nonlocal pts
        pts += n
        why.append({"points": n, "text": text, "evidence": [i for i in [fid(facts, path)] if i]})

    rv = _v(b, "technicals.rvol")
    if rv is not None and rv < 1:
        add(15, f"Thin participation: RVOL {_fmt(rv, 2)}x", "technicals.rvol")
    pos = _v(b, "range_52w.position_pct")
    if pos is not None and pos < 30:
        add(15, f"Sits at {_fmt(pos)}% of its 52-week range (near lows)", "range_52w.position_pct")
    pv = _v(b, "technicals.price_vs_sma_pct")
    if (pv is not None and pv < 0) or _v(b, "technicals.trend") == "down":
        add(15, "Below its 10-day SMA or in a downtrend", "technicals.price_vs_sma_pct")
    up = _v(b, "analyst.upside_pct")
    if up is not None and up <= 0:
        add(10, f"No analyst headroom ({_fmt(up)}% to mean target)", "analyst.upside_pct")
    bp = _v(b, "analyst.buy_pct")
    if bp is not None and bp < 55:
        add(10, f"Low analyst conviction ({_fmt(bp)}% buy)", "analyst.buy_pct")
    ph = _v(b, "range_52w.pct_from_high")
    if ph is not None and ph <= -20:
        add(10, f"{_fmt(abs(ph))}% below the 52-week high", "range_52w.pct_from_high")
    sp = _v(b, "analyst.sell_pct")
    if sp is not None and sp >= 20:
        add(10, f"{_fmt(sp)}% of analysts rate it a sell", "analyst.sell_pct")
    n = b.get("news") or {}
    if n.get("negative", 0) > n.get("positive", 0):
        add(10, f"Headline tone leans negative ({n['negative']} vs {n['positive']})", "news.negative")
    dr = _v(b, "technicals.day_range_position_pct")
    if dr is not None and dr < 30:
        add(5, f"Weak close in the day range ({_fmt(dr)}%)", "technicals.day_range_position_pct")
    return min(100, round(pts)), why


def sub_scores(b):
    """Technicals / Fundamentals / News panel scores (0-100) or None if no data."""
    def avg(parts):
        parts = [p for p in parts if p is not None]
        return round(sum(parts) / len(parts)) if parts else None
    clip = lambda x: max(0, min(100, x))
    t = avg([
        None if _v(b, "technicals.rvol") is None else clip(_v(b, "technicals.rvol") / 3 * 100),
        None if _v(b, "range_52w.position_pct") is None else _v(b, "range_52w.position_pct"),
        None if _v(b, "technicals.day_range_position_pct") is None else _v(b, "technicals.day_range_position_pct"),
        None if _v(b, "technicals.trend") is None else {"up": 85, "sideways": 50, "down": 15}[_v(b, "technicals.trend")]])
    f = avg([None if _v(b, "analyst.buy_pct") is None else _v(b, "analyst.buy_pct"),
             None if _v(b, "analyst.upside_pct") is None else clip(50 + _v(b, "analyst.upside_pct") * 2.5)])
    n = b.get("news") or {}
    tot = n.get("positive", 0) + n.get("negative", 0)
    ns = None if tot == 0 else round(n["positive"] / tot * 100)
    return {"technicals": t, "fundamentals": f, "news": ns}


def leadership(b):
    pos, rv = _v(b, "range_52w.position_pct"), _v(b, "technicals.rvol")
    return (pos is not None and pos >= 60) or (rv is not None and rv >= 3)


def judge(bull, bear, b):
    net = bull - bear
    verdict = "BUY" if (net >= 25 and leadership(b)) else "AVOID" if net <= -15 else "WATCH"
    conf = max(1, min(10, round(4 + net / 15)))
    conf = max(conf, 7) if verdict == "BUY" else min(conf, 6)
    return {"winner": "Bull" if net > 0 else "Bear", "verdict": verdict, "confidence": conf, "net": net}


def evaluate_deterministic(b, facts):
    bs, bw = score_bull(b, facts)
    es, ew = score_bear(b, facts)
    j = judge(bs, es, b)
    top = sorted(bw if j["net"] > 0 else ew, key=lambda r: -r["points"])
    lead = top[0] if top else None
    rationale = ("; ".join(r["text"] for r in top[:3]) or "Not enough data for a conviction call") + "."
    j.update(rationale=rationale, key_catalyst=lead["text"] if lead else "No clear catalyst in the data",
             bull_score=bs, bear_score=es,
             evidence=sorted({i for r in top[:3] for i in r["evidence"]}))
    subs = sub_scores(b)
    return {"scores": {"bull": {"score": bs, "reasons": bw}, "bear": {"score": es, "reasons": ew},
                       **{k: {"score": v} for k, v in subs.items()}},
            "verdict": j, "engine": "deterministic", "model": None, "fallback_reason": None,
            "validation": {"passed": True, "issues": []}, "news_interpretation": None}
