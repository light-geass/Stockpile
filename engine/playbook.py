"""Overnight playbook: near-close filters + next-open / next-close settlement horizons."""
from datetime import time as dtime

from events import blocks_buy
from outcomes import DISCLAIMER
from regime import caps_buy
from sizing import guidance

WINDOW = (dtime(15, 0), dtime(15, 20))


def in_window(now):
    t = now.time()
    return now.weekday() < 5 and WINDOW[0] <= t <= WINDOW[1]


def row(bundle, result, settings):
    tech, price = bundle.get("technicals") or {}, bundle.get("price") or {}
    ev = bundle.get("events") or {}
    blocked, item = blocks_buy(ev.get("items"), getattr(settings, "event_guard_days", 1))
    thin = (price.get("volume") or 0) < 100_000
    reasons = []
    if thin:
        reasons.append("thin liquidity")
    if (tech.get("atr14_pct") or 0) > 6:
        reasons.append("wide overnight-gap / ATR")
    if blocked:
        reasons.append(f"event {(item or {}).get('date')}")
    if caps_buy(bundle.get("regime")):
        reasons.append("risk-off regime")
    ok = not reasons and result["verdict"]["verdict"] in ("BUY", "WATCH")
    size = guidance(price.get("live"), tech.get("stop_level"), getattr(settings, "risk_budget_inr", 10000))
    return {"symbol": bundle["symbol"], "name": bundle.get("name"), "eligible": ok, "reasons": reasons,
            "verdict": result["verdict"]["verdict"], "confidence": result["verdict"]["confidence"],
            "entry_zone": tech.get("entry_zone"), "stop": tech.get("stop_level"),
            "gap_median": tech.get("overnight_gap_median_pct"), "gap_p5": tech.get("overnight_gap_p5_pct"),
            "settle": ["next_open", "next_close"], "sizing": size,
            "prediction": bundle.get("prediction"), "disclaimer": DISCLAIMER}


def build(bundles, results, settings, now):
    rows = [row(b, results[b["symbol"]], settings) for b in bundles if b["symbol"] in results]
    return {"active": in_window(now), "session": bundles[0].get("market_session") if bundles else None,
            "disclaimer": DISCLAIMER, "rows": rows,
            "note": "Close-entry playbook. Fills from 15:00 IST may occur at Closing-Auction (CAS) prices."}
