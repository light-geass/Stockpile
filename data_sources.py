"""Provider-neutral data layer: universe, symbol mapping, scout screening, yfinance fundamentals."""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from evidence import empty_analyst

log = logging.getLogger("datasources")

BASE = Path(__file__).resolve().parent
_NSE_EQ = re.compile(r"^NSE:(.+)-EQ$")  # greedy: hyphens belong to the ticker (BAJAJ-AUTO, M&M)
# Trailing series tokens yfinance uses for non-equity NSE instruments ("BE" is a normal equity series).
_YF_NON_EQ = {"ETF", "MF", "IETF", "SM", "IV", "RR", "E1", "P1", "X1", "XT", "N1", "N2", "W1", "T1"}


def yf_symbol_for(fyers_symbol):
    """Map a canonical FYERS NSE cash symbol to a yfinance ticker only when nothing is guessed.

    `NSE:TCS-EQ` → `TCS.NS`. Non-equity series (ETF etc.) return None so the caller reports a data gap
    instead of fetching an unrelated instrument; `fyers_client.quotes()` also returns the exchange series.
    """
    m = _NSE_EQ.match(str(fyers_symbol or "").strip().upper())
    if not m:
        return None
    series = m.group(1).rsplit("-", 1)[-1]
    if series in _YF_NON_EQ:
        return None
    return f"{m.group(1)}.NS"


def load_universe(path=None):
    d = json.loads(Path(path or BASE / "universe.json").read_text(encoding="utf-8"))
    out = []
    for bucket in ("large", "mid", "small"):
        for e in d.get(bucket, []):
            e = dict(e)
            e["cap_segment"] = bucket.capitalize()
            sym = e["symbol"]
            e["fyers"] = f"NSE:{sym}-EQ"              # provider-specific ids live ONLY here
            e["yf"] = yf_symbol_for(e["fyers"])        # same verified mapping as a focused run
            out.append(e)
    return out





def screen(universe, quotes, per_bucket):
    """Scout: deterministic Python screen. Keep top-N by day change in each bucket; skip stocks with no valid quote."""
    shortlisted = []
    for bucket in ("Large", "Mid", "Small"):
        rows = []
        for e in universe:
            if e["cap_segment"] != bucket:
                continue
            q = quotes.get(e["fyers"])
            if q and q.get("live") and q.get("day_change_pct") is not None:
                rows.append((q["day_change_pct"], e))
        rows.sort(key=lambda r: r[0], reverse=True)
        shortlisted += [e for _, e in rows[:per_bucket]]
    return shortlisted


def _num(v):
    """yfinance can return NaN (a float, not None) for a missing field; treat it as missing."""
    try:
        return None if v is None or v != v else v
    except Exception:
        return v


def fetch_fundamentals(yf_symbol, yf_module=None):
    """Fundamentalist: yfinance metadata + analyst consensus/targets. Everything null-safe. Returns (analyst, meta, gaps, ticker)."""
    gaps = []
    an = empty_analyst("fetched")
    meta = {}
    t = None
    try:
        yf = yf_module
        if yf is None:
            import yfinance as yf
        t = yf.Ticker(yf_symbol)
        info = t.info or {}
    except Exception as e:  # network / library failures must never crash a run
        log.warning("yfinance fetch failed for %s: %s", yf_symbol, type(e).__name__)
        return empty_analyst("yfinance_unavailable"), meta, ["analyst.yfinance_unavailable"], t
    if not info:
        # Yahoo returns an empty payload when it blocks/throttles a client: report it instead of a silent None.
        log.warning("yfinance returned no info for %s (blocked or unknown ticker)", yf_symbol)
        gaps.append("analyst.yfinance_empty")
    meta = {"sector": info.get("sector"), "name": info.get("longName") or info.get("shortName")}
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
    if an["consensus"] in (None, "none"):
        an["consensus"] = None
    return an, meta, gaps, t


def analyst_covered(an):
    """True when at least one analyst figure is present. Shared by the agent stats and by tests so the
    meaning of "Covered N/M" cannot drift away from what the Fundamentalist actually collected."""
    return bool(an.get("target_mean") or an.get("consensus") or an.get("buy_pct") is not None)
