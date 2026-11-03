"""Forward outcomes: pure math + settle job + accuracy report + technical-only replay.

Settlement is anchored on the verdict's decision day (`asof`): bar 0 after that day is the entry session.
A row is stored once all `horizon` sessions did trade; an ambiguous decision day (market closed, weekend,
holiday, or no candle) is skipped rather than guessed, so nothing is silently conditioned on bad entries.
"""
import json
import statistics
from datetime import datetime

from evidence import IST, _day, _r
from grounding import build_facts
from scoring import evaluate_deterministic, scores3

DISCLAIMER = ("Hit rates and expected ranges are measured history, not a promise of future gain. "
              "Analysis only; no orders are placed. Not investment advice.")
SURVIVORSHIP = "Replay and live universes are today's symbol master; delisted names are absent."
HORIZONS = (1, 5, 20, 60)
BUCKETS = ((1, 4, "1-4"), (5, 6, "5-6"), (7, 8, "7-8"), (9, 10, "9-10"))
# Performance slices of a session (IST). Env-driven sample windows are refused on top of these.
SESSIONS = {"open": (9, 15, 14, 59), "cas": (15, 0, 15, 35)}
_WEEKEND = {5, 6}


def pct(a, b):
    """(a − b) / b × 100, or None when either side is missing or b is zero."""
    return _r((a - b) / b * 100, 4) if a is not None and b else None


def _bars_after(candles, asof):
    return [c for c in candles or [] if _day(c) > asof]


def settle_window(candles, asof, horizon):
    """Entry = the open of the first session after `asof`; exit = the close `horizon` sessions later.
    Returns None until the window is complete (a partial window would silently become a shorter horizon)."""
    fwd = _bars_after(candles, asof)
    if len(fwd) < horizon:
        return None
    window, entry = fwd[:horizon], fwd[0].get("o")
    if entry is None:
        return None
    last = window[-1].get("c")
    highs = [c["h"] for c in window if c.get("h") is not None]
    lows = [c["l"] for c in window if c.get("l") is not None]
    ret = pct(last, entry)
    return {"entry_price": entry, "next_open": entry, "close_px": last,
            "gap_pct": pct(_prev_close(candles, asof), entry),
            "mfe_pct": pct(max(highs), entry) if highs else None,
            "mae_pct": pct(min(lows), entry) if lows else None,
            "ret_pct": ret, "hit": None if ret is None else int(ret > 0)}


def _prev_close(candles, asof):
    before = [c for c in candles or [] if _day(c) <= asof]
    return before[-1].get("c") if before else None


def _asof(bundle):
    raw = bundle.get("retrieved_at") or bundle.get("quote_timestamp")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(IST).date()
    except (AttributeError, ValueError):
        return None


def _load_bundle(raw):
    try:
        b = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return None
    return b if isinstance(b, dict) else None


def settle_pending(store, provider, horizons=HORIZONS, benchmark="NIFTYBEES", limit=400, holidays=None):
    """Idempotent. One candle request per provider symbol (benchmark included). Returns {settled, skipped, errors}."""
    stats = {"settled": 0, "skipped": 0, "errors": []}
    cache, holidays = {}, holidays or set()
    pending = store.verdicts_for_settle(limit, horizons)

    def candles(sym):
        if sym not in cache:
            try:
                cache[sym] = provider.daily_candles(sym, days=420)
            except Exception as e:  # one unreachable symbol must not abort the whole job
                cache[sym] = e
        if isinstance(cache[sym], Exception):
            raise cache[sym]
        return cache[sym]

    def settle(verdict_id, symbol, hist, asof):
        if not _traded(hist, asof, holidays):
            return False
        wrote = False
        for h in horizons:
            if store.outcome_get(verdict_id, h) is None and (row := settle_window(hist, asof, h)) is not None:
                store.outcome_put(verdict_id, h, row)
                wrote = True
        return wrote

    for vid, symbol, bundle in pending:
        entry_bundle = _load_bundle(bundle)
        asof = _asof(entry_bundle) if entry_bundle else None
        if not entry_bundle or not asof or not (entry_bundle.get("price") or {}).get("live"):
            # No decision-time price means no entry: never substitute today's close (that is look-ahead).
            stats["skipped"] += 1
            continue
        try:
            if settle(vid, symbol, candles(entry_bundle.get("provider_symbol") or f"NSE:{symbol}-EQ"), asof):
                stats["settled"] += 1
            else:
                stats["skipped"] += 1
        except Exception:
            stats["errors"].append(vid)
    # NIFTYBEES is itself a settleable instrument; settling it as a benchmark is a bonus, never a failure.
    try:
        for vid, _s, b in store.benchmark_verdicts(benchmark, horizons):
            bundle = _load_bundle(b)
            if bundle and (asof := _asof(bundle)):
                settle(vid, benchmark, candles(bundle.get("provider_symbol") or f"NSE:{benchmark}-EQ"), asof)
    except Exception:
        pass
    return stats


def _traded(candles, asof, holidays):
    """A decision day must be a legitimate trading day with a candle, or the forward window is meaningless."""
    known = {_day(c) for c in candles or []}
    return asof in known and asof.weekday() not in _WEEKEND and asof not in holidays


def _pctile(xs, p):
    """Linear-interpolation percentile on an already-sorted list."""
    i = (len(xs) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def _median(xs):
    return statistics.median(xs) if xs else None


def _col(rows, key):
    return [r[key] for r in rows if r.get(key) is not None]


def summarize(rows):
    """Descriptive stats for one slice. `n` counts everything; each stat counts its own non-null cells."""
    rets, hits = sorted(_col(rows, "ret_pct")), _col(rows, "hit")
    maes = sorted(_col(rows, "mae_pct"))
    return {"n": len(rows),
            "hit_rate": round(sum(hits) / len(hits), 4) if hits else None,
            "mean_return": round(statistics.fmean(rets), 4) if rets else None,
            "median_return": round(_median(rets), 4) if rets else None,
            "p10": _r(_pctile(rets, 0.1), 4) if rets else None,
            "p90": _r(_pctile(rets, 0.9), 4) if rets else None,
            "median_mae": round(_median(maes), 4) if maes else None,
            "mean_mae": round(statistics.fmean(maes), 4) if maes else None}


def _bucket(conf):
    return next((name for lo, hi, name in BUCKETS if lo <= (conf or 0) <= hi), BUCKETS[0][2])


def _session(row):
    """Performance slices must not pool an opening-bell entry with a closing-auction entry."""
    started = row.get("started_at") or ""
    try:
        t = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(IST)
    except (AttributeError, ValueError):
        return "unknown"
    if t.weekday() in _WEEKEND:
        return "unknown"
    hhmm = (t.hour, t.minute)
    return next((name for name, (h0, m0, h1, m1) in SESSIONS.items() if (h0, m0) <= hhmm <= (h1, m1)), "unknown")


def _group(rows, key):
    out = {}
    for r in rows:
        out.setdefault(key(r), []).append(r)
    return out


def _slices(rows):
    groups = _group(rows, lambda r: (r.get("verdict"), _bucket(r.get("confidence")), r["horizon"],
                                     r.get("engine") or "deterministic"))

    return [{"verdict": k[0], "confidence_bucket": k[1], "horizon": k[2], "engine": k[3], **summarize(v),
             "top_gainer_n": sum(1 for x in v if x.get("screened_as") == "top_gainer")}
            for k, v in sorted(groups.items())]


def _rolling(rows):
    months = _group([r for r in rows if r.get("month")], lambda r: (r["month"], r["horizon"]))
    return [{"month": k[0], "horizon": k[1], **summarize(v)} for k, v in sorted(months.items())][-9:]


def _benchmark_excess(rows, horizons):
    """Median return minus the benchmark's median return, same horizon, same month. Gap when either side is thin."""
    bench = _group([r for r in rows if r.get("is_benchmark")], lambda r: (r["month"], r["horizon"]))
    own = [r for r in rows if not r.get("is_benchmark")]
    out = {}
    for h in horizons:
        months = _group([r for r in own if r["horizon"] == h], lambda r: r["month"])
        diffs = [m - br for k, v in months.items() if (m := _median(sorted(_col(v, "ret_pct")))) is not None
                 and (br := _median(sorted(_col(bench.get((k, h), []), "ret_pct")))) is not None]
        out[h] = {"excess_median": round(_median(diffs), 4) if diffs else None, "months": len(diffs)}
    return out


def range_table(store, horizons=HORIZONS):
    """Measured p10–p90 returns, keyed for per-row lookup. Thin slices fall back to the horizon total."""
    live = [r for r in store.outcomes_joined() if not r.get("is_benchmark")]
    grouped = _group(live, lambda r: (r.get("verdict"), _bucket(r.get("confidence")),
                                      r.get("engine") or "deterministic", r["horizon"]))
    return {"horizons": list(horizons),
            "by_horizon": {h: summarize(v) for h, v in _group(live, lambda r: r["horizon"]).items()},
            "by_slice": {k: summarize(v) for k, v in grouped.items()}}


def lookup_range(table, verdict, confidence, engine, horizons=HORIZONS):
    """JSON-ready expected range per horizon. Empty history → omitted keys, never a fake 0% band."""
    if not table:
        return {}
    engine = engine or "deterministic"
    out = {}
    for h in horizons:
        s = table.get("by_slice", {}).get((verdict, _bucket(confidence), engine, h))
        if not s or (s.get("n") or 0) < 5:
            s = table.get("by_horizon", {}).get(h)
        if s and s.get("n"):
            out[str(h)] = {"p10": s.get("p10"), "p90": s.get("p90"),
                           "median": s.get("median_return"), "n": s["n"]}
    return out


def accuracy_report(store, horizons=HORIZONS, benchmark="NIFTYBEES"):
    """Measured history per verdict × confidence bucket × horizon × engine, plus benchmark and trend."""
    rows = store.outcomes_joined()
    live = [r for r in rows if not r.get("is_benchmark")]
    return {"disclaimer": DISCLAIMER, "survivorship": SURVIVORSHIP,
            "horizons": list(horizons), "benchmark_symbol": benchmark,
            "benchmark": {h: summarize(v) for h, v in _group([r for r in rows if r.get("is_benchmark")],
                                                               lambda r: r["horizon"]).items()},
            "benchmark_source": next((r.get("engine") for r in rows if r.get("is_benchmark")), None),
            "overall": {h: summarize(v) for h, v in _group(live, lambda r: r["horizon"]).items()},
            "slices": _slices(live), "rolling": _rolling(live),
            "excess_vs_benchmark": _benchmark_excess(rows, horizons),
            "sessions": {s: summarize(v) for s, v in _group(live, _session).items()},
            "n_settled": len(live)}


def replay_deterministic(candles, quote_key="c", session="closed", symbol="REPLAY"):
    """Walk completed bars and re-run the deterministic engine as-of each one, so a baseline backtest exists on
    day one. Technical features only: news and analyst data are not point-in-time available, so they are
    explicit gaps rather than today's values leaking backwards. Each row carries the settled horizons computed
    from the same candle series, which is what makes a replay cheaper than waiting weeks of live runs."""
    import evidence as ev
    out, empty_news = [], {"total": 0, "positive": 0, "negative": 0, "neutral": 0, "recent": []}
    candles = list(candles or [])
    for i in range(60, len(candles)):
        hist, last = candles[:i], candles[i - 1]
        quote = {"live": last[quote_key], "day_high": last["h"], "day_low": last["l"], "volume": last.get("v"),
                 "prev_close": hist[-2]["c"] if len(hist) > 1 else last["c"]}
        at = datetime.fromtimestamp(last["t"], IST)
        tech, rng, gaps = ev.technicals_from_daily(hist, quote, None, session, now=at)
        bundle = ev.assemble_bundle({"symbol": symbol, "name": symbol, "screened_as": "replay"}, quote, tech, rng,
                                    ev.empty_analyst("replay"), empty_news,
                                    {"price": "replay", "candles": "replay"}, at,
                                    gaps + ["news.items", "analyst.replay"], session=session)
        bundle["replay"] = True
        facts, _, _ = build_facts(bundle)
        r = evaluate_deterministic(bundle, facts)
        r["scores3"] = scores3(bundle)
        asof = _day(last)
        out.append({"verdict_id": None, "symbol": symbol, "asof": asof.isoformat(),
                    "started_at": at.astimezone(IST).isoformat(), "month": asof.isoformat()[:7],
                    "verdict": r["verdict"]["verdict"], "confidence": r["verdict"]["confidence"],
                    "engine": "deterministic_replay", "screened_as": "replay", "bundle": bundle,
                    "horizons": {h: w for h in HORIZONS if (w := settle_window(candles, asof, h)) is not None}})
    return out


def replay_report(candles, symbol="REPLAY", horizons=HORIZONS):
    """Accuracy of the deterministic engine on a historical candle window. NOT a substitute for live history:
    the universe is today's symbol master (survivorship) and only technical features existed as-of."""
    rows = replay_deterministic(candles, symbol=symbol)
    flat, by_h = [], {h: [] for h in horizons}
    for r in rows:
        for h, w in r["horizons"].items():
            row = {"symbol": symbol, "verdict": r["verdict"], "confidence": r["confidence"],
                   "engine": r["engine"], "horizon": h, "started_at": r["started_at"], "month": r["month"],
                   "screened_as": "replay", **w}
            flat.append(row)
            if h in by_h:
                by_h[h].append(row)
    return {"disclaimer": DISCLAIMER, "survivorship": SURVIVORSHIP,
            "limitation": ("Replay uses today's symbol master and as-of technical features only; news, analyst "
                           "coverage and events were not point-in-time available, so they are gaps here."),
            "symbol": symbol, "bars": len(candles or []), "verdicts": len(rows), "horizons": list(horizons),
            "overall": {h: summarize(v) for h, v in by_h.items()},
            "slices": _slices(flat), "rolling": _rolling(flat)}
