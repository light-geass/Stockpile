"""Deterministic agents + judge. Pure Python, no LLM. Always available as the fallback engine.
Bull and Bear each score 0-100 from fixed rules (weights sum to 100). Missing data earns no points."""
import etf
import ipo
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
    pos, neg = news_tone(n)
    if pos > neg:
        add(10, f"Headline tone leans positive ({n.get('positive', 0)} vs {n.get('negative', 0)})", "news.positive")
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
    pos, neg = news_tone(n)
    if neg > pos:  # score_bear reads neg>pos; news_tone returns (positive, negative) for both scorers
        add(10, f"Headline tone leans negative ({n.get('negative', 0)} vs {n.get('positive', 0)})", "news.negative")
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


def news_tone(n):
    """(positive, negative) headline weight: age-weighted counts when present, raw counts otherwise.
    A fresh headline outweighs one repeated all week — the stale-news reversal risk the plan targets."""
    wpos, wneg = n.get("weighted_positive"), n.get("weighted_negative")
    return (wpos, wneg) if wpos is not None and wneg is not None else (n.get("positive", 0), n.get("negative", 0))


def leadership(b):
    pos, rv = _v(b, "range_52w.position_pct"), _v(b, "technicals.rvol")
    return (pos is not None and pos >= 60) or (rv is not None and rv >= 3)


def apply_guards(j, b):
    """Python-side caps. Flags are precomputed onto the bundle; this never fetches."""
    notes = list(j.get("guards") or [])
    if j.get("verdict") != "BUY":
        j["guards"] = notes
        return j
    if not etf.allows_buy(b):
        j["verdict"] = "WATCH"
        notes.append("BUY downgraded: ETF overlay rejected equity-breakout rules")
    if ipo.force_watch(b, b.get("sme_watch_until_checklist", True)):
        j["verdict"] = "WATCH"
        notes.append("BUY downgraded: IPO/SME checklist incomplete")
    if (b.get("events") or {}).get("blocks_buy"):
        j["verdict"] = "WATCH"
        notes.append("BUY downgraded: scheduled event inside holding window")
    if (b.get("regime") or {}).get("label") == "risk_off" and b.get("regime_guard", True):
        j["verdict"] = "WATCH"
        notes.append("BUY downgraded: risk-off index regime")
    n, wr = b.get("news") or {}, (b.get("technicals") or {}).get("window_return_pct") or 0
    if n.get("stale_positive") and wr > 5:
        j["verdict"] = "WATCH"
        notes.append("BUY downgraded: stale repeated positive news after a run-up")
    if j["verdict"] != "BUY":
        j["confidence"] = min(j.get("confidence") or 6, 6)
    j["guards"] = notes
    return j


def evaluate_etf(b):
    """ETFs are not stocks. Breakout/RVOL/52-week judgement rules describe a single company's momentum and
    mean something different (or nothing) for a basket, so an ETF is judged on what actually decides its return:
    tracking error, liquidity, and premium/discount to NAV. Missing data is a gap and caps the verdict at WATCH."""
    e = b.get("etf") or {}
    why, ok = [], True
    prem, td, adv = e.get("premium_pct"), e.get("tracking_diff_20d"), e.get("adv")
    if prem is not None:
        why.append(f"Premium/discount to NAV {prem:+.2f}%" + (" (expensive to buy)" if abs(prem) > 1.5 else ""))
        ok &= abs(prem) <= 1.5
    if td is not None:
        why.append(f"20-day tracking difference {td:+.2f}%")
        ok &= abs(td) <= 3
    if adv is not None:
        why.append(f"Average daily volume {adv:,.0f}")
        ok &= adv >= 50_000
    if e.get("expense_ratio") is not None:
        why.append(f"Expense ratio {e['expense_ratio']}")
    gaps = [g for g in (e.get("gaps") or [])]
    verdict = "WATCH" if gaps else ("BUY" if ok else "AVOID")
    notes = ["ETF judged on tracking, liquidity and NAV premium, not equity breakout rules"]
    if gaps:
        notes.append("ETF overlay incomplete: " + ", ".join(gaps))
    j = {"winner": "ETF overlay", "verdict": verdict, "confidence": 6 if verdict == "WATCH" else 8 if ok else 3,
         "net": 0, "rationale": ("; ".join(why) or "No ETF overlay data available") + ".",
         "key_catalyst": why[0] if why else "No ETF overlay data in the feed",
         "bull_score": None, "bear_score": None, "evidence": [], "guards": notes}
    return {"scores": {"bull": {"score": None, "reasons": []}, "bear": {"score": None, "reasons": []},
                       "technicals": {"score": None}, "fundamentals": {"score": None}, "news": {"score": None}},
            "verdict": j, "engine": "deterministic_etf", "model": None, "fallback_reason": None,
            "validation": {"passed": True, "issues": [], "guards": notes}, "news_interpretation": None}


def judge(bull, bear, b):
    net = bull - bear
    verdict = "BUY" if (net >= 25 and leadership(b)) else "AVOID" if net <= -15 else "WATCH"
    conf = max(1, min(10, round(4 + net / 15)))
    conf = max(conf, 7) if verdict == "BUY" else min(conf, 6)
    return apply_guards({"winner": "Bull" if net > 0 else "Bear", "verdict": verdict, "confidence": conf, "net": net}, b)


def evaluate_deterministic(b, facts):
    if (b.get("asset_type") or "").upper() == "ETF":
        return evaluate_etf(b)
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


# ---- three shadow scores (news/trend, chart pattern, overall) ----
# Computed in Python only. The LLM panel may read them as non-citable context but never changes them,
# and no verdict/alert rule consumes them yet. Abstention is explicit (score None), never a fake 0.
_OVERALL_W = (0.6, 0.4)  # news_trend, pattern


def _news_trend(b):
    n = b.get("news") or {}
    tot = n.get("positive", 0) + n.get("negative", 0)
    news = None if tot == 0 else round(n["positive"] / tot * 100)
    t = b.get("technicals") or {}
    trend = {"up": 85, "sideways": 50, "down": 15}.get(t.get("trend"))
    reasons = ([f"Headline tone {news}/100 ({n['positive']}+ vs {n['negative']}-)"] if news is not None else []) + \
              ([f"Trend {t['trend']} maps to {trend}/100"] if trend is not None else [])
    parts = [x for x in (news, trend) if x is not None]
    if not parts:
        return None, False, ["No headline tone or trend data."]
    score = round(sum(parts) / len(parts))
    wr = t.get("window_return_pct")
    if wr is not None:
        adj = 5 if wr > 0 else -5 if wr < 0 else 0
        score = max(0, min(100, score + adj))
        reasons.append(f"~1-month return {wr:+.1f}% adjusts by {adj:+d}")
    return score, True, reasons


def _pattern_score(chart):
    if not chart or not chart.get("available"):
        return None, False, ["Chart data unavailable (insufficient or missing candles)."]
    scored = [(p, round(50 * p.get("confidence", 0) * (1 if p.get("direction") == "bullish" else -1)))
              for p in chart.get("patterns", []) if p.get("status") == "confirmed"
              and p.get("direction") in ("bullish", "bearish")]
    if not scored:
        return None, True, [f"Chart stage abstained: {chart.get('status')}."]
    net = sum(d for _, d in scored)
    return max(0, min(100, 50 + net)), True, [f"{p['name']} ({p['direction']}) contributes {d:+d}" for p, d in scored]


def _overall(nt, pat):
    parts = [x for x in ((nt[0], _OVERALL_W[0]), (pat[0], _OVERALL_W[1])) if x[0] is not None]
    if not parts:
        return None, False, ["Both components unavailable: no overall score."], None
    if len(parts) == 1:
        return parts[0][0], True, ["One component available; overall equals it."], "single_component"
    return (round(sum(s * w for s, w in parts)), True,
            [f"{_OVERALL_W[0]}*news_trend + {_OVERALL_W[1]}*pattern"],
            f"{_OVERALL_W[0]}*news_trend + {_OVERALL_W[1]}*pattern")


def scores3(b):
    """The plan's three-score block (higher = more bullish, 0-100). Shadow mode: verdicts and alerts
    never read it. Each component carries score/available/reasons; overall carries its formula."""
    nt = _news_trend(b)
    pat = _pattern_score(b.get("chart"))
    ov = _overall(nt, pat)
    return {"schema_version": 1,
            "news_trend": {"score": nt[0], "available": nt[1], "reasons": nt[2]},
            "pattern": {"score": pat[0], "available": pat[1], "reasons": pat[2]},
            "overall": {"score": ov[0], "available": ov[1], "reasons": ov[2], "formula": ov[3]}}
