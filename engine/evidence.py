"""Pure-Python indicators + provider-neutral evidence bundle. No LLM, no network."""
from datetime import date, datetime, time as dtime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
SMA_PERIOD = 10
OPEN_T, CLOSE_T = dtime(9, 15), dtime(15, 30)
NEAR_CLOSE, CAS_END = dtime(15, 0), dtime(15, 35)
CAS_FROM = date(2026, 8, 3)  # NSE CAS live date (circular CMTR76170 / SEBI)
# Official weekday holidays: NSE circular NSE/CMTR/71775 (12 Dec 2025).
FALLBACK_HOLIDAYS = {
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31), date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28), date(2026, 6, 26), date(2026, 9, 14),
    date(2026, 10, 2), date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25),
}
_holiday_state = {"dates": set(FALLBACK_HOLIDAYS), "source": "fallback_nse_cmtr71775", "gap": True}


def now_ist():
    return datetime.now(IST)


def holiday_state():
    return {**_holiday_state, "dates": set(_holiday_state["dates"])}


def set_holidays(dates, source, gap=False):
    _holiday_state.update(dates=set(dates), source=source, gap=bool(gap))


def load_holidays(http=None, store=None, year=None):
    """Fetch NSE holiday-master once; cache; fall back to the circular list with an explicit gap."""
    year = year or now_ist().year
    cached = store.kv_get("nse_holidays") if store else None
    if cached and cached.get("year") == year and cached.get("dates"):
        set_holidays([date.fromisoformat(x) for x in cached["dates"]], cached.get("source", "cache"),
                     cached.get("gap", False))
        return holiday_state()
    dates, source, gap = _fetch_holidays(http, year)
    set_holidays(dates, source, gap)
    if store:
        store.kv_set("nse_holidays", {"year": year, "dates": [d.isoformat() for d in sorted(dates)],
                                     "source": source, "gap": gap})
    return holiday_state()


def _fetch_holidays(http, year):
    if http is None:
        return set(FALLBACK_HOLIDAYS), "fallback_nse_cmtr71775", True
    hdr = {"User-Agent": "Mozilla/5.0 signal-desk/1.0", "Accept": "application/json"}
    try:
        http.get("https://www.nseindia.com", timeout=12, headers=hdr)
        r = http.get("https://www.nseindia.com/api/holiday-master?type=trading", timeout=12,
                     headers={**hdr, "Referer": "https://www.nseindia.com/"})
        data = r.json() if getattr(r, "status_code", 0) == 200 else None
    except Exception:
        return set(FALLBACK_HOLIDAYS), "fallback_nse_cmtr71775", True
    rows = data.get("CM") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        return set(FALLBACK_HOLIDAYS), "fallback_nse_cmtr71775", True
    out = set()
    for row in rows:
        raw = (row.get("tradingDate") or row.get("date") or "") if isinstance(row, dict) else ""
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                d = datetime.strptime(raw[:11].strip(), fmt).date()
            except ValueError:
                continue
            if d.year == year:
                out.add(d)
            break
    return (out, "nse_holiday_master", False) if out else (set(FALLBACK_HOLIDAYS), "fallback_nse_cmtr71775", True)


def market_session(now=None, holidays=None):
    """NSE cash session. Holidays from load_holidays(); CAS window from 15:00 IST (fills may be auction prices)."""
    n = now or now_ist()
    hol = _holiday_state["dates"] if holidays is None else holidays
    if n.weekday() >= 5 or n.date() in hol:
        return "closed"
    t = n.time()
    if t < OPEN_T:
        return "closed"
    if t < NEAR_CLOSE:
        return "open"
    end = CAS_END if n.date() >= CAS_FROM else CLOSE_T
    return "cas" if t <= end else "closed"


def session_warnings(session, holidays_meta=None):
    out = []
    meta = holidays_meta or _holiday_state
    if meta.get("gap"):
        out.append(f"Exchange holiday list is {meta.get('source')}; live NSE calendar was unavailable.")
    if session == "cas":
        out.append("Near the close: a cash entry may fill at Closing-Auction (CAS) prices "
                   "(from 3 Aug 2026, CTS ends 15:15 IST; CAS 15:15–15:35). Non-CAS names still VWAP to 15:30.")
    if session == "closed":
        out.append("Market is closed (scheduled NSE hours and holiday calendar): data is last-session, so no alerts fire.")
    return out


def freshness(source_synthetic, session, quote_epoch, retrieved, max_age):
    if source_synthetic:
        return "synthetic"
    if quote_epoch is None:  # exchange gives no trustworthy quote time: never claim stale
        return "unknown"
    age = retrieved.timestamp() - quote_epoch
    if session == "open":
        return "live" if age <= max_age else "stale"
    return "last_close"


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def trend_label(price, closes, n=SMA_PERIOD):
    cur = sma(closes, n)
    prev = sma(closes[:-5], n) if len(closes) >= n + 5 else None
    if cur is None or price is None:
        return None
    rising = prev is not None and cur > prev
    falling = prev is not None and cur < prev
    if price > cur and rising:
        return "up"
    if price < cur and falling:
        return "down"
    return "sideways"


def _day(c):
    return datetime.fromtimestamp(c["t"], IST).date()


def intraday_rvol(candles, now=None, sessions=10):
    """Time-matched RVOL: today's cumulative volume up to the latest completed bar vs the average cumulative
    volume of prior sessions up to the same time of day. Returns (rvol, None) or (None, reason)."""
    n = now or now_ist()
    by_day = {}
    for c in candles:
        by_day.setdefault(_day(c), []).append(c)
    today = n.date()
    if today not in by_day:
        return None, "no_intraday_bars_today"
    tb = by_day[today]
    cutoff = datetime.fromtimestamp(max(c["t"] for c in tb), IST).time()
    today_cum = sum(c["v"] for c in tb)
    prior = []
    for d in sorted(k for k in by_day if k < today)[-sessions:]:
        cum = sum(c["v"] for c in by_day[d] if datetime.fromtimestamp(c["t"], IST).time() <= cutoff)
        if cum > 0:
            prior.append(cum)
    if len(prior) < 3:
        return None, "insufficient_prior_sessions"
    return round(today_cum / (sum(prior) / len(prior)), 2), None


def technicals_from_daily(daily, quote, intraday=None, session="closed", now=None):
    """daily: list of candle dicts (oldest first). Returns (technicals, range_52w, gaps)."""
    gaps = []
    n = now or now_ist()
    candles = [c for c in daily if None not in (c["h"], c["l"], c["c"], c["v"])]
    today_in = bool(candles) and _day(candles[-1]) == n.date() and session == "open"
    hist = candles[:-1] if today_in else candles  # completed sessions only
    live = quote.get("live")
    closes = [c["c"] for c in hist]
    tech = {"rvol": None, "rvol_method": None, "price_vs_sma_pct": None, "sma_period": SMA_PERIOD,
            "window_return_pct": None, "swing_high": None, "swing_low": None,
            "day_range_position_pct": None, "trend": None, "atr14_pct": None,
            "overnight_gap_median_pct": None, "overnight_gap_p5_pct": None,
            "stop_level": None, "entry_zone": None}
    win = hist[-22:]  # ~1 month of sessions
    if len(win) >= SMA_PERIOD and live:
        s = sma([c["c"] for c in hist] + ([live] if today_in else []), SMA_PERIOD)
        tech["price_vs_sma_pct"] = _r((live - s) / s * 100) if s else None
        tech["trend"] = trend_label(live, closes + ([live] if today_in else []))
        tech["window_return_pct"] = _r((live - win[0]["c"]) / win[0]["c"] * 100)
        tech["swing_high"] = _r(max(c["h"] for c in win))
        tech["swing_low"] = _r(min(c["l"] for c in win))
    else:
        gaps += ["technicals.price_vs_sma_pct", "technicals.trend", "technicals.window_return_pct",
                 "technicals.swing_high", "technicals.swing_low"]
    hi, lo = quote.get("day_high"), quote.get("day_low")
    if live is not None and hi is not None and lo is not None and hi > lo:
        tech["day_range_position_pct"] = _r((live - lo) / (hi - lo) * 100)
    else:
        gaps.append("technicals.day_range_position_pct")
    # RVOL
    rv = None
    if session == "open" and intraday:
        rv, why = intraday_rvol(intraday, n)
        if rv is not None:
            tech["rvol"], tech["rvol_method"] = rv, "intraday_time_matched"
        else:
            gaps.append(f"technicals.rvol_intraday({why})")
    if tech["rvol"] is None:
        vols = [c["v"] for c in hist[:-1][-20:]] if not today_in else [c["v"] for c in hist[-20:]]
        today_vol = quote.get("volume") if today_in or session == "open" else (hist[-1]["v"] if hist else None)
        if today_vol is not None and len(vols) >= 5 and sum(vols) > 0:
            tech["rvol"] = round(today_vol / (sum(vols) / len(vols)), 2)
            tech["rvol_method"] = ("full_day_volume_partial_session_UNADJUSTED" if session == "open"
                                   else "full_day_volume_vs_avg_prior_days")
        else:
            gaps.append("technicals.rvol")
    # 52-week range from <=366d of daily candles
    rng = {"high": None, "low": None, "pct_from_high": None, "position_pct": None, "basis": "daily_candles_<=366d"}
    allc = candles + ([] if today_in else [])
    if len(allc) >= 100 and live:
        h = max(max(c["h"] for c in allc), live)
        l = min(min(c["l"] for c in allc), live)
        rng.update(high=_r(h), low=_r(l), pct_from_high=_r((live - h) / h * 100),
                   position_pct=_r((live - l) / (h - l) * 100) if h > l else None)
    else:
        gaps.append("range_52w")
    atr = _atr(hist, 14)
    if atr and live:
        tech["atr14_pct"] = _r(atr / live * 100)
    else:
        gaps.append("technicals.atr14_pct")
    med, p5 = _gap_stats(hist)
    tech["overnight_gap_median_pct"], tech["overnight_gap_p5_pct"] = med, p5
    if med is None:
        gaps.append("technicals.overnight_gap")
    tech["stop_level"], tech["entry_zone"] = risk_levels(live, tech["atr14_pct"], None)
    return tech, rng, gaps


def _atr(candles, n=14):
    if len(candles) < n + 1:
        return None
    tr = []
    for i in range(-n, 0):
        c, p = candles[i], candles[i - 1]
        tr.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    return sum(tr) / n


def _gap_stats(candles):
    gaps = [(b["o"] - a["c"]) / a["c"] * 100 for a, b in zip(candles, candles[1:]) if a.get("c") and b.get("o") is not None]
    if len(gaps) < 10:
        return None, None
    gaps.sort()
    return _r(gaps[len(gaps) // 2]), _r(gaps[int(0.05 * (len(gaps) - 1))])


def risk_levels(live, atr_pct, chart=None):
    stop = round(live * (1 - max(atr_pct or 0, 1.5) / 100), 2) if live else None
    for p in (chart or {}).get("patterns") or []:
        if p.get("name") == "support_zone" and p.get("levels") and live and p["levels"][0] < live:
            stop = p["levels"][0]
            break
    zone = None
    if live and atr_pct:
        w = live * atr_pct / 100 / 2
        zone = [round(live - w, 2), round(live + w, 2)]
    return stop, zone


def attach_risk(bundle):
    t = bundle.get("technicals") or {}
    t["stop_level"], t["entry_zone"] = risk_levels(
        (bundle.get("price") or {}).get("live"), t.get("atr14_pct"), bundle.get("chart"))
    bundle["technicals"] = t
    return bundle


def empty_analyst(reason="not_fetched"):
    return {"consensus": None, "num_analysts": None, "buy_pct": None, "hold_pct": None, "sell_pct": None,
            "target_mean": None, "target_low": None, "target_high": None, "upside_pct": None,
            "limitation": "Only analyst targets/recommendations are available from this feed; no valuation "
                          "ratios (P/E, ROE, margins). Targets are not a valuation analysis.",
            "_reason": reason}


def assemble_bundle(entry, quote, technicals, rng, analyst, news, source, retrieved, gaps, synthetic=False,
                    max_age=300, session=None):
    session = session or market_session(retrieved.astimezone(IST))
    live, prev = quote.get("live"), quote.get("prev_close")
    chp = quote.get("day_change_pct")
    if chp is None and live is not None and prev:
        chp = _r((live - prev) / prev * 100)
    an = dict(analyst)
    if an.get("target_mean") and live:
        an["upside_pct"] = _r((an["target_mean"] - live) / live * 100)
    # FYERS passes no quote_epoch at all (its `tt` is a fixed daily value, not the quote time), so qts shows only
    # what the provider actually gave us. freshness() then returns 'unknown' instead of a false 'stale'.
    ep = quote.get("quote_epoch")
    qts = datetime.fromtimestamp(ep, timezone.utc).isoformat() if ep else quote.get("quote_timestamp")
    b = {
        "symbol": entry["symbol"], "name": entry.get("name", entry["symbol"]),
        "cap_segment": entry.get("cap_segment"), "sector": entry.get("sector"), "synthetic": synthetic,
        "asset_type": entry.get("asset_type") or "EQUITY",
        "screened_as": entry.get("screened_as"),
        "provider_symbol": entry.get("fyers") or entry.get("provider_symbol"),
        "source": source, "quote_timestamp": qts, "retrieved_at": retrieved.isoformat(),
        "market_session": session,
        "quote_age_sec": (round(retrieved.timestamp() - ep) if ep else None),
        "freshness": freshness(synthetic, session, ep, retrieved, max_age),
        "price": {"live": live, "day_open": quote.get("day_open"), "day_high": quote.get("day_high"),
                  "day_low": quote.get("day_low"), "prev_close": prev, "day_change_pct": chp,
                  "volume": quote.get("volume")},
        "range_52w": rng, "technicals": technicals, "analyst": an, "news": news,
        "data_gaps": sorted(set(gaps)),
    }
    for k, v in list(b["price"].items()):
        if v is None:
            b["data_gaps"].append(f"price.{k}")
    for k in ("consensus", "num_analysts", "buy_pct", "target_mean", "upside_pct"):
        if an.get(k) is None:
            b["data_gaps"].append(f"analyst.{k}")
    b["data_gaps"] = sorted(set(b["data_gaps"]))
    return b
