"""Shadow three-score block (scores3): news/trend, chart pattern, overall. Pure Python, no network.
Higher = more bullish (0-100); None = the component abstains. A missing/abstained component is never
fabricated into a fake number, and the overall reweights or collapses to the one component that exists."""
from scoring import scores3
from tests.test_llm import live_bundle


def chart(status="ok", available=True, patterns=()):
    return {"timeframe": "1D", "detector_version": "1", "available": available, "status": status,
            "patterns": list(patterns), "summary": "Detected: demo.", "limitations": []}


def pat(name, direction, status="confirmed", confidence=0.6):
    return {"name": name, "direction": direction, "status": status, "confidence": confidence,
            "start_date": "2026-09-01", "end_date": "2026-10-29", "levels": [100.0], "evidence": {}}


def test_news_trend_combines_headline_tone_and_trend_with_momentum():
    """live_bundle: tone 2+ vs 0- -> 100, trend 'up' -> 85, avg 92, +5 momentum from +8.5% monthly return."""
    nt = scores3(live_bundle())["news_trend"]
    assert nt["score"] == 97 and nt["available"] is True and len(nt["reasons"]) == 3


def test_news_trend_abstains_without_components():
    b = live_bundle()
    b["technicals"]["trend"] = None
    b["news"] = {"total": 0, "positive": 0, "negative": 0, "neutral": 0, "recent": []}
    s3 = scores3(b)
    assert s3["news_trend"] == {"score": None, "available": False,
                                "reasons": ["No headline tone or trend data."]}
    assert s3["overall"]["score"] is None and s3["overall"]["available"] is False  # nothing to combine


def test_pattern_score_is_directional_and_capped():
    b = live_bundle()
    b["chart"] = chart(patterns=[pat("breakout", "bullish", confidence=0.65),
                                 pat("breakdown", "bearish", confidence=0.65)])
    assert scores3(b)["pattern"]["score"] == 50  # +32 and -32 cancel around the neutral midpoint
    b["chart"] = chart(patterns=[pat("swing_structure", "bullish", confidence=0.7)])
    assert scores3(b)["pattern"]["score"] == 85  # 50 + round(50 * 0.7)
    b["chart"] = chart(patterns=[pat("breakout", "bullish", confidence=0.9) for _ in range(5)])
    assert scores3(b)["pattern"]["score"] == 100  # clamped, never above 100


def test_forming_patterns_are_not_scored():
    b = live_bundle()
    b["chart"] = chart(patterns=[pat("breakout", "bullish", status="forming", confidence=0.5)])
    p = scores3(b)["pattern"]
    assert p["score"] is None and p["available"] is True  # abstained on scored patterns, not a fake 0


def test_abstention_is_not_a_score():
    b = live_bundle()
    b["chart"] = chart(status="no_reliable_pattern", patterns=[])
    p = scores3(b)["pattern"]
    assert p["score"] is None and p["available"] is True and "abstained" in p["reasons"][0]


def test_missing_chart_is_unavailable_and_overall_collapses_to_news_trend():
    s3 = scores3(live_bundle())  # no "chart" key at all
    assert s3["pattern"] == {"score": None, "available": False,
                             "reasons": ["Chart data unavailable (insufficient or missing candles)."]}
    assert s3["overall"]["score"] == s3["news_trend"]["score"]
    assert s3["overall"]["formula"] == "single_component"


def test_overall_uses_documented_formula():
    b = live_bundle()
    b["chart"] = chart(patterns=[pat("breakout", "bullish", confidence=0.65)])
    s3 = scores3(b)
    nt, p = s3["news_trend"]["score"], s3["pattern"]["score"]
    assert nt == 97 and p == 82
    ov = s3["overall"]
    assert ov["score"] == round(0.6 * nt + 0.4 * p)
    assert ov["formula"] == "0.6*news_trend + 0.4*pattern" and ov["available"] is True


def test_scores3_schema_version_and_structure():
    s3 = scores3(live_bundle())
    assert s3["schema_version"] == 1
    assert set(s3) == {"schema_version", "news_trend", "pattern", "overall"}
    for k in ("news_trend", "pattern"):
        assert set(s3[k]) == {"score", "available", "reasons"} and isinstance(s3[k]["available"], bool)
    assert "formula" in s3["overall"]
