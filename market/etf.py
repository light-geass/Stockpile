"""ETF overlay: tracking, liquidity, premium/discount. Equity breakout rules do not apply.

Premium/discount is measured against a real NAV, in this order of preference:
  1. an intraday iNAV supplied by the provider (or a paid INAV feed) — the only values that price a live premium;
  2. yfinance's `navPrice`, which is EOD, so it is labelled as such and flagged (`premium_is_eod`) rather than
     passed off as an intraday premium;
  3. nothing — an explicit `etf.premium_inav` gap.
"""
from evidence import _r


def _nav(info, bundle):
    """Return (nav, source). Never invents a NAV; `None` means the premium is a gap."""
    info = info or {}
    live_nav = _num(info.get("intradayNav") or info.get("inav") or (bundle.get("etf_nav") or {}).get("value"))
    if live_nav and live_nav > 0:
        return live_nav, "inav"
    eod = _num(info.get("navPrice") or info.get("nav"))
    if eod and eod > 0:
        return eod, "nav_eod"
    return None, None


def profile(bundle, bench_candles=None, info=None):
    """info is a yfinance .info dict when present. Missing fields become explicit gaps, never guesses."""
    info = info or {}
    gaps, t = [], bundle.get("technicals") or {}
    live = (bundle.get("price") or {}).get("live")
    nav, nav_src = _nav(info, bundle)
    prem = _r((live - nav) / nav * 100) if live and nav else None
    if prem is None:
        gaps.append("etf.premium_inav")
    exp = _num(info.get("annualReportExpenseRatio") or info.get("expenseRatio"))
    if exp is None:
        gaps.append("etf.expense_ratio")
    adv = _num(info.get("averageDailyVolume10Day") or info.get("averageVolume"))
    td = tracking_diff(bundle, bench_candles)
    if td is None:
        gaps.append("etf.tracking_difference")
    hi, lo = (bundle.get("price") or {}).get("day_high"), (bundle.get("price") or {}).get("day_low")
    spread = _r((hi - lo) / live * 100) if live and hi and lo and live else None
    return {"premium_pct": prem, "nav": nav, "nav_source": nav_src,
            "premium_is_eod": nav_src == "nav_eod", "expense_ratio": exp, "adv": adv, "tracking_diff_20d": td,
            "spread_proxy_pct": spread, "gaps": gaps, "gap": bool(gaps)}


def tracking_diff(bundle, bench_candles):
    wr = (bundle.get("technicals") or {}).get("window_return_pct")
    if wr is None or not bench_candles or len(bench_candles) < 22:
        return None
    a, b = bench_candles[-22]["c"], bench_candles[-1]["c"]
    if not a:
        return None
    return _r(wr - (b - a) / a * 100)


def allows_buy(b):
    if (b.get("asset_type") or "").upper() != "ETF":
        return True
    e = b.get("etf") or {}
    if e.get("premium_pct") is not None and abs(e["premium_pct"]) > 1.5:
        return False
    # An EOD NAV against a live price is not a tradeable premium: it can be a whole day stale, so it may
    # downgrade (the caller sees the flag) but it may never approve a BUY on its own.
    if e.get("premium_is_eod"):
        return False
    if e.get("adv") is not None and e["adv"] < 50_000:
        return False
    if e.get("tracking_diff_20d") is not None and abs(e["tracking_diff_20d"]) > 3:
        return False
    return not (e.get("gap") and e.get("premium_pct") is None and e.get("adv") is None)


def _num(v):
    try:
        return None if v is None or v != v else float(v)
    except (TypeError, ValueError):
        return None
