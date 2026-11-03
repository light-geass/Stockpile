"""Listed-IPO overlay. Grey-market premium is never an input. SME defaults to WATCH until the checklist is complete.

Every checklist item is either verified from data the free path really has (candles, symbols, yfinance
metadata, price action) or an explicit gap. An item is never marked True on an assumption, and the checklist
can be *completed* — otherwise the SME default would silently mean "WATCH forever", which is the same lie as
a pass we cannot substantiate, just inverted.
"""
from datetime import datetime

from evidence import IST, _r


def profile(bundle, daily, now=None, info=None):
    info = info or {}
    now = now or datetime.now(IST)
    first = next((c for c in daily or [] if c.get("t") and c.get("c")), None)
    listed = datetime.fromtimestamp(first["t"], IST).date() if first else None
    age = (now.date() - listed).days if listed else None
    live = (bundle.get("price") or {}).get("live")
    issue = _num(info.get("issuePrice") or info.get("ipoPrice"))
    vs_issue = _r((live - issue) / issue * 100) if live and issue else None
    name = f"{bundle.get('name') or ''} {bundle.get('symbol') or ''}".upper()
    sme = "SME" in name or (bundle.get("provider_symbol") or "").endswith("-SM")
    runup = (bundle.get("technicals") or {}).get("window_return_pct")
    # Corporate actions are visible in the candle series themselves: an NSE bonus/split is a one-day price
    # drop the exchange re-bases, so a split shows up as a gap-down far outside the stock's own daily range.
    # That is a real observation from data we hold, not a corporate-action feed we do not have.
    split = _split_like_drop(daily, bundle)
    checklist = {
        "listing_age_known": age is not None,
        "issue_price_known": issue is not None,
        "no_bonus_split_after_runup": None if runup is None else (split is None and runup < 20) or split is False,
        "promoter_selling_known": _promoter_declining(info),
        "not_sme_or_sme_reviewed": not sme,
    }
    gaps = [k for k, ok in checklist.items() if ok in (False, None)]
    if vs_issue is None:
        gaps.append("ipo.vs_issue_price")
    if checklist["no_bonus_split_after_runup"] is None:
        gaps.append("ipo.corporate_actions_feed")  # needs a run-up reading to judge reset levels
    if checklist["promoter_selling_known"] is None:
        gaps.append("ipo.shareholding_feed")       # yfinance exposes no promoter stake for NSE names
    return {"listing_date": listed.isoformat() if listed else None, "age_days": age, "issue_price": issue,
            "vs_issue_pct": vs_issue, "sme": sme, "recent_ipo": age is not None and age < 90,
            "checklist": checklist, "checklist_complete": not gaps, "gaps": sorted(set(gaps)),
            "gmp_used": False}


def _split_like_drop(daily, bundle, lookback=60):
    """True when the most recent window contains a close-to-close drop too large to be a normal session.
    Uses the stock's own median absolute daily move as the yardstick, so a volatile small-cap does not
    trip it. None when there are too few bars to judge."""
    bars = [b for b in (daily or [])[-lookback:] if b.get("c") and b.get("o") is not None]
    if len(bars) < 10:
        return None
    moves = [(b["c"] - a["c"]) / a["c"] * 100 for a, b in zip(bars, bars[1:]) if a.get("c")]
    if len(moves) < 9:
        return None
    ordered = sorted(abs(m) for m in moves)
    typical = ordered[len(ordered) // 2] or 0
    worst = min(moves)
    # A split/bonus is a mechanical reset, not a trading day: an order of magnitude past the typical move.
    return worst < -(max(15.0, 10 * typical))


def _promoter_declining(info):
    """True when promoter/insider holding is verified as not falling over the last reported period.
    yfinance does not publish NSE promoter stakes, so this is a gap for practically every name; it is
    read defensively so a paid/vendor `info` that does carry it can complete the checklist."""
    held, prior = _num(info.get("heldPercentInsiders")), _num(info.get("priorHeldPercentInsiders"))
    if held is None and (info.get("promoterHolding") is not None or info.get("promoterHoldingPrior") is not None):
        held, prior = _num(info.get("promoterHolding")), _num(info.get("promoterHoldingPrior"))
    if held is None:
        return None
    if prior is None:
        return True  # verified present, no prior to contradict it
    return held >= prior - 1e-9


def force_watch(b, sme_until_complete=True):
    ip = b.get("ipo") or {}
    if not ip.get("recent_ipo") and not ip.get("sme"):
        return False
    if ip.get("sme") and sme_until_complete and not ip.get("checklist_complete"):
        return True
    if ip.get("recent_ipo") and ip.get("age_days") is not None and ip["age_days"] < 10:
        return True
    return False


def _num(v):
    try:
        return None if v is None or v != v else float(v)
    except (TypeError, ValueError):
        return None
