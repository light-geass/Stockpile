"""Provider-neutral data layer: watchlist entries, symbol mapping, scout screening, yfinance fundamentals."""
import logging
import re

from evidence import empty_analyst

log = logging.getLogger("datasources")

_NSE_EQ = re.compile(r"^NSE:(.+)-EQ$")  # greedy: hyphens belong to the ticker (BAJAJ-AUTO, M&M)
# Trailing series tokens yfinance uses for non-equity NSE instruments ("BE" is a normal equity series).
_YF_NON_EQ = {"ETF", "MF", "IETF", "SM", "IV", "RR", "E1", "P1", "X1", "XT", "N1", "N2", "W1", "T1"}


def yf_symbol_for(fyers_symbol):
    """Map a canonical FYERS NSE cash symbol to a yfinance ticker only when nothing is guessed.

    `NSE:TCS-EQ` â†’ `TCS.NS`. Non-equity series (ETF etc.) return None so the caller reports a data gap
    instead of fetching an unrelated instrument; `fyers_client.quotes()` also returns the exchange series.
    """
    m = _NSE_EQ.match(str(fyers_symbol or "").strip().upper())
    if not m:
        return None
    series = m.group(1).rsplit("-", 1)[-1]
    if series in _YF_NON_EQ:
        return None
    return f"{m.group(1)}.NS"


def entry_from_item(item):
    """A stored watchlist item becomes a run entry. The provider symbol is the stored canonical one; the
    yfinance ticker is the same verified mapping a focused run uses (None = reported as a data gap)."""
    fyers = item["provider_symbol"]
    return {"symbol": item["symbol"], "name": item.get("name") or item["symbol"], "sector": None,
            "cap_segment": "Watchlist", "asset_type": item.get("asset_type"),
            "fyers": fyers, "yf": yf_symbol_for(fyers), "news_query": item.get("name") or item["symbol"]}


def screen_watchlist(entries, quotes, per_list):
    """Scout for a watchlist: keep the top-N by day change, skipping stocks with no valid quote. There are no
    cap buckets any more, so the per-bucket limit applies to the whole list."""
    rows = [(q["day_change_pct"], e) for e in entries
            for q in [quotes.get(e["fyers"])] if q and q.get("live") and q.get("day_change_pct") is not None]
    rows.sort(key=lambda r: r[0], reverse=True)
    return [e for _, e in rows[:per_list]]


def _num(v):
    """yfinance can return NaN (a float, not None) for a missing field; treat it as missing."""
    try:
        return None if v is None or v != v else v
    except Exception:
        return v


def fetch_fundamentals(yf_symbol, yf_module=None):
    """Fundamentalist: yfinance metadata + analyst consensus/targets. Everything null-safe. Returns (analyst, meta, gaps, ticker)."""
    gaps, meta = [], {}
    t = None
    try:
        yf = yf_module
        if yf is None:
            import yfinance as yf
        t = yf.Ticker(yf_symbol)
        info = t.info or {}
        if not info:  # Yahoo blocks/throttles some server IPs here (notably cloud hosts like Render)
            raise ValueError("empty info")
    except Exception as e:  # network / library failures must never crash a run
        log.warning("yfinance .info unavailable for %s (%s); trying the analyst-only endpoints",
                    yf_symbol, type(e).__name__)
        info = info if "info" in locals() else {}  # a missing summary is not a block
        an, gaps = _analyst_fallback(t, gaps, info)
        if an["_reason"] in ("yfinance_blocked", "yfinance_empty"):
            log.warning("yfinance analyst endpoints were empty too for %s", yf_symbol)
        return an, _meta(info), gaps, t
    return _fundamentals_from_info(t, info, gaps), _meta(info), gaps, t


def _meta(info):
    return {"sector": info.get("sector"), "name": info.get("longName") or info.get("shortName")}


def _fundamentals_from_info(t, info, gaps):
    """Normal path: yfinance `.info` (Fastly-backed). Returns the analyst dict."""
    an = empty_analyst("fetched")
    an["consensus"] = _num(info.get("recommendationKey"))
    an["num_analysts"] = _num(info.get("numberOfAnalystOpinions"))
    # No sell-side coverage on Yahoo (e.g. small caps like ITI): that is "not covered", not a failure,
    # and must be visible as a reason instead of an empty gap list.
    if (an["consensus"] in (None, "none") and an["num_analysts"] is None
            and _num(info.get("targetMeanPrice")) is None):
        an["_reason"] = "no_analyst_coverage"
        gaps.append("analyst.no_coverage")
    an["target_mean"] = _num(info.get("targetMeanPrice"))
    an["target_low"] = _num(info.get("targetLowPrice"))
    an["target_high"] = _num(info.get("targetHighPrice"))
    _add_breakdown(t, an, gaps)
    if an["consensus"] in (None, "none"):
        an["consensus"] = None
    return an


def _analyst_fallback(t, gaps, info=None):
    """When `.info` is blocked (or came back empty), take what it did give us and then fill the rest from the
    report endpoints, which are a different backend on Yahoo. Without this the Fundamentalist reported 0/1 for
    covered stocks on a throttled cloud host."""
    an = empty_analyst("yfinance_blocked")  # distinct from no_analyst_coverage: Yahoo gave the SERVER nothing
    if t is None:
        return an, gaps + ["analyst.yfinance_unavailable"]
    info = info or {}
    an["consensus"] = _num(info.get("recommendationKey"))
    an["num_analysts"] = _num(info.get("numberOfAnalystOpinions"))
    an["target_mean"] = _num(info.get("targetMeanPrice"))
    got = False
    try:
        getter = getattr(t, "get_analyst_price_targets", None) or (lambda: t.analyst_price_targets)
        tgt = getter() or {}
        an["target_mean"], an["target_low"], an["target_high"] = (_num(tgt.get("mean")), _num(tgt.get("low")),
                                                                  _num(tgt.get("high")))
    except Exception as e:  # only reachable when the endpoint itself fails, not when it is absent
        log.warning("analyst price targets unavailable: %s", type(e).__name__)
    try:
        summ = t.recommendations_summary
        if summ is not None and len(summ):
            row = summ.iloc[0]
            n = sum(int(_num(row.get(k)) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
            an["num_analysts"] = n or None
            got = got or bool(n)
    except Exception as e:
        log.warning("analyst summary unavailable: %s", type(e).__name__)
    _add_breakdown(t, an, gaps)
    got = got or an["buy_pct"] is not None or an["target_mean"] is not None or an["consensus"] is not None
    # A partial answer is still coverage (e.g. only the target mean); only a fully empty reply has no coverage.
    if not got:
        an["_reason"] = "yfinance_empty"
        gaps.append("analyst.yfinance_empty")
        return an, gaps
    an["_reason"] = "fetched"
    if an["num_analysts"] and an["consensus"] is None and an["buy_pct"] is not None:
        an["consensus"] = "buy" if an["buy_pct"] >= 60 else "hold" if an["buy_pct"] >= 40 else "sell"
    return an, gaps


def _add_breakdown(t, an, gaps):
    """Latest period's strongBuy/buy/hold/sell/strongSell -> buy%/hold%/sell%."""
    try:
        rec = t.recommendations
        if rec is not None and len(rec):
            row = rec.iloc[0]
            sb, b, h, s, ss = (float(_num(row.get(k, 0)) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
            tot = sb + b + h + s + ss
            if tot > 0:
                an["buy_pct"], an["hold_pct"] = round((sb + b) / tot * 100, 1), round(h / tot * 100, 1)
                an["sell_pct"] = round((s + ss) / tot * 100, 1)
    except Exception:
        gaps.append("analyst.recommendation_breakdown")


def analyst_covered(an):
    """True when at least one analyst figure is present. Shared by the agent stats and by tests so the
    meaning of "Covered N/M" cannot drift away from what the Fundamentalist actually collected."""
    return bool(an.get("target_mean") or an.get("consensus") or an.get("buy_pct") is not None)
