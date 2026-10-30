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
]
STRUCTURAL = {0, 1, 52, 100}


def _get(b, path):
    cur = b
    for p in path.split("."):
        cur = cur.get(p) if isinstance(cur, dict) else None
    return cur


def build_facts(bundle):
    """Returns (facts, news_items, index). facts: [{id,path,label,value,unit}], news ids N1.. (suspicious excluded)."""
    facts = []
    for path, label, unit in FACT_SPEC:
        v = _get(bundle, path)
        if v is not None:
            facts.append({"id": f"E{len(facts) + 1}", "path": path, "label": label, "value": v, "unit": unit})
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


def verify_statement(text, cited_ids, index, facts, extra=()):
    """Every number in `text` must trace to a fact value, a cited headline's own numbers, or a structural constant.
    `extra` is for numbers the caller itself supplied (e.g. the panel's own bull/bear scores); they are accepted
    at exact value only — never rounded — so this cannot be used to smuggle in an approximation. Every cited ID
    must exist. Returns list of issue strings."""
    issues = []
    for i in cited_ids:
        if i not in index:
            issues.append(f"unknown evidence id {i}")
    allowed = {float(f["value"]) for f in facts if isinstance(f["value"], (int, float)) and not isinstance(f["value"], bool)}
    allowed |= STRUCTURAL
    exact = set()
    for v in (extra or ()):
        try:
            exact.add(float(v))
        except (TypeError, ValueError):
            pass
    for i in cited_ids:
        it = index.get(i)
        if it and "title" in it:
            allowed |= {n for n, _ in numbers_in(it["title"])}
    for num, nd in numbers_in(text):
        if num in exact and nd == 0:
            continue
        if not _match(num, nd, allowed):
            issues.append(f"untraceable number {num:g}")
    return issues
