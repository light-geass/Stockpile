"""Fact table with evidence IDs + numeric/ID verification of model text."""
import re

FACT_SPEC = [  # (path, label, unit)
    ("price.live", "Live price", "INR"), ("price.day_change_pct", "Day change", "%"),
    ("price.prev_close", "Previous close", "INR"), ("price.day_high", "Day high", "INR"),
    ("price.day_low", "Day low", "INR"), ("price.volume", "Day volume", "shares"),
    ("range_52w.high", "52-week high", "INR"), ("range_52w.low", "52-week low", "INR"),
    ("range_52w.pct_from_high", "Distance from 52-week high", "%"),
    ("range_52w.position_pct", "Position in 52-week range", "%"),
    ("technicals.rvol", "Relative volume (RVOL)", "x"), ("technicals.rvol_method", "RVOL method", ""),
    ("technicals.price_vs_sma_pct", "Price vs 10-day SMA", "%"), ("technicals.trend", "Trend", ""),
    ("technicals.window_return_pct", "~1-month return", "%"), ("technicals.swing_high", "Swing high (1M)", "INR"),
    ("technicals.swing_low", "Swing low (1M)", "INR"),
    ("technicals.day_range_position_pct", "Position in day range", "%"),
    ("analyst.consensus", "Analyst consensus", ""), ("analyst.num_analysts", "Analyst count", ""),
    ("analyst.buy_pct", "Analysts buy", "%"), ("analyst.hold_pct", "Analysts hold", "%"),
    ("analyst.sell_pct", "Analysts sell", "%"), ("analyst.target_mean", "Mean analyst target", "INR"),
    ("analyst.upside_pct", "Upside to mean target", "%"),
    ("news.total", "Relevant headlines", ""), ("news.positive", "Positive headlines", ""),
    ("news.negative", "Negative headlines", ""), ("freshness", "Data freshness", ""),
    ("market_session", "Market session", ""),
    ("technicals.atr14_pct", "ATR(14) of price", "%"),
    ("technicals.overnight_gap_median_pct", "Median overnight gap", "%"),
    ("technicals.overnight_gap_p5_pct", "Overnight gap 5% tail", "%"),
    ("technicals.stop_level", "Stop level", "INR"),
    ("regime.label", "Index regime", ""), ("regime.ret_20d", "Index 20-day return", "%"),
    ("etf.premium_pct", "ETF premium/discount to NAV", "%"),
    ("etf.nav_source", "ETF NAV basis (live iNAV vs EOD)", ""),
    ("etf.tracking_diff_20d", "ETF 20-day tracking difference", "%"),
    ("etf.adv", "ETF average daily volume", "shares"),
    ("etf.expense_ratio", "ETF expense ratio", "%"),
    ("ipo.listing_date", "IPO listing date", ""), ("ipo.age_days", "Days since listing", ""),
    ("ipo.vs_issue_pct", "Price vs issue price", "%"), ("ipo.sme", "SME board listing", ""),
    ("ipo.checklist_complete", "IPO risk checklist complete", ""),
    ("events.next_date", "Next scheduled event", ""),
    ("prediction.1.p_up", "P(+1d up)", ""), ("prediction.5.p_up", "P(+5d up)", ""),
    ("prediction.20.p_up", "P(+20d up)", ""),
]
STRUCTURAL = {0, 1, 52, 100}


def _get(b, path):
    cur = b
    for p in path.split("."):
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur


def build_facts(bundle):
    """Returns (facts, news_items, index). facts: [{id,path,label,value,unit}], news ids N1.. (suspicious excluded).
    Chart patterns from the deterministic Chart Analyst stage become citable facts: the model may discuss
    them but can never invent detections, levels or confidence."""
    facts = []
    for path, label, unit in FACT_SPEC:
        v = _get(bundle, path)
        if v is not None:
            facts.append({"id": f"E{len(facts) + 1}", "path": path, "label": label, "value": v, "unit": unit})
    for i, it in enumerate((bundle.get("events") or {}).get("items") or []):
        d, kind = it.get("date"), it.get("kind") or "event"
        if not d:
            continue
        label = {"results": "Results on", "ex_dividend": "Ex-dividend on", "dividend": "Dividend on",
                 "board": "Board meeting on"}.get(kind, "Corporate event on")
        facts.append({"id": f"E{len(facts) + 1}", "path": f"events.items.{i}.date",
                      "label": f"{label} {d}", "value": d, "unit": ""})
    chart = bundle.get("chart") or {}
    if chart.get("available"):
        facts.append({"id": f"E{len(facts) + 1}", "path": "chart.status", "label": "Chart pattern status",
                      "value": chart["status"], "unit": ""})
        for p in chart.get("patterns", []):
            name = p["name"].replace("_", " ")
            facts.append({"id": f"E{len(facts) + 1}", "path": f"chart.pattern.{p['name']}",
                          "label": f"Chart pattern: {name}", "value": f"{p['status']} {p['direction']}", "unit": ""})
            for lv in p.get("levels", [])[:2]:
                facts.append({"id": f"E{len(facts) + 1}", "path": f"chart.level.{p['name']}",
                              "label": f"Chart {name} level", "value": round(float(lv), 2), "unit": "INR"})
    news = []
    for it in (bundle.get("news") or {}).get("recent", []):
        if it.get("suspicious"):
            continue
        news.append({"id": f"N{len(news) + 1}", "title": it["title"], "published_at": it.get("published_at"),
                     "domain": it.get("domain"), "sentiment": it.get("sentiment"), "type": "headline_only"})
    index = {f["id"]: f for f in facts}
    index.update({n["id"]: n for n in news})
    return facts, news, index


def fid(facts, path):
    for f in facts:
        if f["path"] == path:
            return f["id"]
    return None


_NUM = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")
_ID = re.compile(r"\b[EN]\d+\b")


_LABEL = re.compile(r"\b\d+[- ]?(?:day|week|month|year|session)s?\b", re.I)  # window labels like "10-day", "52-week"


def numbers_in(text):
    t = _LABEL.sub(" ", _ID.sub(" ", text or ""))
    out = []
    for m in _NUM.finditer(t):
        raw = m.group().replace(",", "")
        try:
            out.append((float(raw), len(raw.split(".")[1]) if "." in raw else 0))
        except ValueError:
            pass
    return out


def _match(num, nd, allowed):
    for a in allowed:
        for cand in (a, abs(a)):
            if abs(round(cand, nd) - num) < 1e-9 or abs(abs(num) - round(abs(cand), nd)) < 1e-9:
                return True
    return False


def verify_statement(text, cited_ids, index, facts):
    """Every number in `text` must trace to a fact value, a cited headline's own numbers, or a structural constant.
    Every cited ID must exist. Returns list of issue strings."""
    issues = []
    for i in cited_ids:
        if i not in index:
            issues.append(f"unknown evidence id {i}")
    allowed = set(STRUCTURAL)
    for f in facts:
        v = f.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            allowed.add(float(v))
        elif isinstance(v, str):
            allowed |= {n for n, _ in numbers_in(v)}
    for i in cited_ids:
        it = index.get(i)
        if it and "title" in it:
            allowed |= {n for n, _ in numbers_in(it["title"])}
    for num, nd in numbers_in(text):
        if not _match(num, nd, allowed):
            issues.append(f"untraceable number {num:g}")
    return issues
