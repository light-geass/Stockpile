"""Run orchestration + live state for the dashboard. One run at a time."""
import copy
import logging
import threading
import time
from datetime import datetime, timezone

import data_sources as ds
import chart_patterns as cp
import etf
import events
import evidence as ev
import ipo
import llm
import news as newsmod
import playbook
import predict
import regime
from fyers_client import FyersClient, FyersError
from grounding import build_facts
from notifier import Telegram, format_buy, format_summary
import outcomes
from outcomes import DISCLAIMER
from security import redact
from sizing import guidance
from storage import Store
from watchlists import WatchlistError

log = logging.getLogger("pipeline")
AGENTS = [
    ("scout", "Scout", "Screens the watchlist by day move"),
    ("technician", "Technician", "RVOL, trend, 52-week range"),
    ("chart", "Chart Analyst", "Patterns from completed daily candles"),
    ("fundamentalist", "Fundamentalist", "Analyst consensus and targets"),
    ("newsdesk", "Newsdesk", "Fresh headlines and tone"),
    ("bull", "Bull", "Argues the case to buy"),
    ("bear", "Bear", "Argues the case against"),
    ("judge", "Judge", "Weighs both, calls the verdict"),
    ("messenger", "Messenger", "Sends BUY alerts"),
]


class RunAbort(Exception):
    pass


class Pipeline:
    def __init__(self, settings, auth, store: Store, deps=None, watchlists=None):
        self.s, self.auth, self.store = settings, auth, store
        self.deps, self.watchlists = deps or {}, watchlists
        self.lock = threading.Lock()
        self.running = False
        self._yf = {}  # symbol → {ticker, info}: the one yfinance fetch reused by analyst, events, ETF and IPO
        self._ranges = None
        self._reset()

    # ---------- state ----------
    def _reset(self, scope="watchlist", target=None, watchlist=None):
        self.state = {"status": "idle", "mode": "live", "scope": scope, "target": target, "watchlist": watchlist,
                      "message": "Ready. Press Run analysis to start.",
                      "agents": {k: {"name": n, "role": r, "status": "offline", "stats": {}} for k, n, r in AGENTS},
                      "kpis": {"universe": 0, "in_debate": 0, "buys": 0, "top_pick": None},
                      "verdicts": [], "warnings": [], "footer": None, "engine": None, "pulled_at": None,
                      "playbook": None, "disclaimer": DISCLAIMER}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def _agent(self, key, status=None, **stats):
        with self.lock:
            a = self.state["agents"][key]
            if status:
                a["status"] = status
            a["stats"].update(stats)

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def _warn(self, msg):
        with self.lock:
            if msg not in self.state["warnings"]:
                self.state["warnings"].append(redact(msg, self.s.secret_values()))

    def _pause(self):
        if self.s.agent_delay:
            time.sleep(self.s.agent_delay)

    # ---------- control ----------
    def start(self, mode, target=None, watchlist_id=None):
        """watchlist_id → scan that stored list; a canonical instrument dict → focused single-name run."""
        with self.lock:
            if self.running:
                return False
            self.running = True
            target = copy.deepcopy(target) if target is not None else None  # caller may mutate its dict
        threading.Thread(target=self._thread, args=(mode, target, watchlist_id), daemon=True).start()
        return True

    def _thread(self, mode, target=None, watchlist_id=None):
        try:
            self.run(mode, target, watchlist_id)
        finally:
            with self.lock:
                self.running = False

    def run(self, mode="live", target=None, watchlist_id=None):
        """Blocking run. Never raises: failures land in state['status']='error' with a safe message.

        watchlist_id=None uses the first stored watchlist. With a canonical instrument dict, only that instrument
        is analysed (scope='focused') and the Messenger/Telegram stage is skipped entirely.
        """
        focused = target is not None
        scope = "focused" if focused else "watchlist"
        brief = ({k: target.get(k) for k in ("symbol", "name", "exchange", "asset_type", "provider_symbol")}
                 if focused else None)
        wl = None
        if not focused:
            try:
                wl = self.watchlists.get(watchlist_id) if watchlist_id is not None else \
                    self.watchlists.get(self.watchlists.first_id())
            except WatchlistError as e:
                self._reset(scope, None, None)
                self._set(status="error", message=str(e))
                return
            brief = {"id": wl["id"], "name": wl["name"], "count": wl["count"]}
        # Mark running and flip the state in ONE critical section: start() has already set running=True on the
        # caller's thread, so a fast run could otherwise clear it before the state said "running" (a poller then
        # saw running=False with status='running' for ever).
        self._reset(scope, brief, brief if not focused else None)
        with self.lock:
            self.running = True
            self.state.update(status="running", mode="live", message="Run started.")
        run_id = self.store.start_run(scope)
        self._bench = None  # per-run memo, reset before the first provider request
        self._ranges = None
        engine_label, uni_n, short_n, summary = None, 0, 0, {}
        try:
            ev.load_holidays((self.deps or {}).get("http"), self.store)
            bundles, uni_n = self._collect_focused(target) if focused else self._collect_live(wl)
            short_n = len(bundles)
            self._enrich(bundles, self._daily)
            self._debate(bundles, run_id)
            scored = self._scored(bundles)  # the benchmark bundle is a reference row, never a decision
            self._set(playbook=playbook.build(scored, self.results, self.s, ev.now_ist()))
            if focused:
                self._record_focused(scored, run_id)
            else:
                self._message(scored, run_id)
            engine_label = self.state["engine"]
            self._finish(scored, uni_n)
            # Last on purpose: a provider hiccup on the reference row must not fail a completed run.
            self._record_benchmark([b for b in bundles if b.get("benchmark")], run_id)
            self._set(status="done", message="Focused analysis complete." if focused else "Run complete.")
        except RunAbort as e:
            with self.lock:
                self.state.update(status="error", message=redact(str(e), self.s.secret_values()))
                for a in self.state["agents"].values():
                    if a["status"] == "working":
                        a["status"] = "error"
        except Exception as e:  # last-resort guard: a bug must never kill the server
            log.error("run crashed: %s", redact(f"{type(e).__name__}: {e}", self.s.secret_values()))
            self._set(status="error", message="Run failed unexpectedly. Check the server log.")
        finally:
            snap = self.snapshot()
            self.store.finish_run(run_id, snap["status"], engine_label, uni_n, short_n,
                                  {"kpis": snap["kpis"], "warnings": snap["warnings"],
                                   "scope": scope, "target": brief if focused else None, "watchlist": brief if not focused else None})
            with self.lock:
                self.running = False

    def _fyers(self):
        if "fyers" in self.deps:
            return self.deps["fyers"]
        from fyers_client import RateLimiter
        return FyersClient(self.s.fyers_app_id, self.auth.get_token,
                           RateLimiter(per_minute=self.s.fyers_per_minute_cap, per_day=self.s.fyers_daily_cap))

    def _fyers_call(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except FyersError as e:
            if e.kind == "auth_expired":
                self.auth.mark_expired()
                raise RunAbort("FYERS token missing or expired. Click Connect FYERS and run again.")
            if e.kind == "unavailable":
                self.auth.mark_unavailable()
            if e.kind in ("rate_limited", "unavailable", "permission"):
                raise RunAbort(e.message)
            raise

    # ---------- shared agent stages (watchlist run and focused run use the same code) ----------
    def _provider(self):
        if not self.auth.get_token():
            self._agent("scout", "error")
            raise RunAbort("FYERS is not connected. Open the dashboard through your HTTPS tunnel and click Connect FYERS.")
        p = self._fyers()
        if hasattr(p, "profile"):
            ok, why = p.profile()
            if not ok and why == "auth_expired":
                # The stored token looks valid locally but FYERS no longer accepts it: make the pill say so.
                self.auth.mark_expired()
                self._agent("scout", "error")
                raise RunAbort("FYERS replaced this session (a newer login or an app change invalidated the stored token). "
                               "Click Connect FYERS and run again.")
            if not ok and why == "rate_limited":
                self._agent("scout", "error")
                raise RunAbort("FYERS is rate-limiting this server right now. Wait a minute, then run again.")
        return p

    def _technicals(self, provider, entries, quotes, session):
        self._agent("technician", "working")
        out = {}
        for e in entries:
            try:
                daily = self._fyers_call(provider.daily_candles, e["fyers"])
                intra = self._fyers_call(provider.intraday_candles, e["fyers"]) if session == "open" else None
                technicals, rng, gaps = ev.technicals_from_daily(daily, quotes[e["fyers"]], intra, session)
                out[e["symbol"]] = (technicals, rng, gaps, daily)  # candles ride along for the Chart Analyst
            except FyersError as err:  # _fyers_call already re-raised rate limits/auth as RunAbort
                out[e["symbol"]] = (None, None, [f"technicals({err.kind})"], None)
        rv = [t[0]["rvol"] for t in out.values() if t[0] and t[0].get("rvol")]
        self._agent("technician", "done", analyzed=len(out),
                    **{"avg RVOL" if len(out) > 1 else "RVOL": f"{sum(rv) / len(rv):.2f}x" if rv else "n/a"})
        return out

    def _chart(self, entries, tech, quotes, session):
        """Chart Analyst: evidence stage, not a decision agent. Reuses the Technician's completed daily
        candles — never a second market-data request. Patterns never override the Judge on their own."""
        self._agent("chart", "working")
        out = {}
        for e in entries:
            t = tech[e["symbol"]]
            daily = t[3] if t and t[0] is not None else None
            out[e["symbol"]] = (cp.analyze(daily, quotes[e["fyers"]].get("live"), session)
                                if daily is not None else (None, ["chart.unavailable"]))
        found = sum(1 for c, _ in out.values() if c and c["status"] == "ok")
        self._agent("chart", "done", analyzed=len(out), patterns=found)
        return out

    def _fundamentals(self, entries, quotes):
        """yfinance analyst data. The ticker comes only from the verified map (never guessed); a series with no
        mapping (ETF, foreign listing) becomes a data gap. Watchlist and focused runs use the same rule."""
        self._agent("fundamentalist", "working")
        out, yf_map = {}, {}
        for e in entries:
            if "yf" not in e:  # focused entries are built from a searched symbol, not a stored watchlist item
                e["yf"] = ds.yf_symbol_for(e["fyers"])
            yf_map[e["symbol"]] = e["yf"]
            sym = yf_map[e["symbol"]]
            out[e["symbol"]] = (ds.fetch_fundamentals(sym, self.deps.get("yf")) if sym else
                                 (ev.empty_analyst("not_mapped"), {}, ["analyst.not_mapped"], None))
        ups = [a["target_mean"] / quotes[e["fyers"]]["live"] * 100 - 100 for e in entries
               for a in [out[e["symbol"]][0]] if a.get("target_mean") and quotes[e["fyers"]].get("live")]
        cov = sum(1 for a, *_ in out.values() if ds.analyst_covered(a))
        nocov = sum(1 for a, *_ in out.values() if a.get("_reason") == "no_analyst_coverage")
        self._agent("fundamentalist", "done", covered=f"{cov}/{len(entries)}",
                    **{"avg upside": f"{sum(ups) / len(ups):+.1f}%" if ups else "n/a"},
                    **({"note": f"{nocov} without analyst coverage"} if nocov else {}))
        self._yf = {e["symbol"]: {"ticker": t, "info": meta or {}} for e in entries
                    for _a, meta, _g, t in [out[e["symbol"]]]}
        return out

    def _news(self, entries, fundamentals):
        self._agent("newsdesk", "working")
        gd = self.deps.get("gdelt") or newsmod.Gdelt(self.s.gdelt_min_interval)
        gn = self.deps.get("gnews", newsmod.google_news)
        # store= lets build_news fingerprint headlines across runs; without it the stale-news flag is dead code.
        out = {e["symbol"]: newsmod.build_news(e, gd, fundamentals[e["symbol"]][3], gnews=gn, store=self.store)
               for e in entries}
        stale = sum(1 for n in out.values() if n[0].get("stale_positive"))
        self._agent("newsdesk", "done", headlines=sum(n[0]["total"] for n in out.values()),
                    tone=f"{sum(n[0]['positive'] for n in out.values())}+ / {sum(n[0]['negative'] for n in out.values())}-",
                    **({"stale positive": stale} if stale else {}))
        return out

    def _warn_market(self, session, bundles):
        stale = [b["symbol"] for b in bundles if b["freshness"] == "stale"]
        if stale:
            ages = sorted(b["quote_age_sec"] for b in bundles if b.get("quote_age_sec") is not None)
            self._warn(f"{len(stale)} quotes are older than {self.s.quote_max_age_sec}s while the market is scheduled open "
                       f"(median age {ages[len(ages)//2] if ages else '?'}s). Check your PC clock sync; alerts are blocked for stale quotes.")
        for w in ev.session_warnings(session):
            self._warn(w)

    def _collect_live(self, wl):
        """Scan exactly the selected watchlist (resolved from the DB by the server, never from the browser)."""
        entries = [ds.entry_from_item(i) for i in wl["items"]]
        if not entries:
            raise RunAbort(f"Watchlist \"{wl['name']}\" is empty. Add stocks to it before running.")
        provider = self._provider()
        self._agent("scout", "working")
        quotes = self._fyers_call(provider.quotes, [e["fyers"] for e in entries])
        short = ds.screen_watchlist(entries, quotes, self.s.shortlist_per_bucket)
        for e in short:
            e["screened_as"] = "top_gainer"
        retrieved = datetime.now(timezone.utc)
        self._agent("scout", "done", scanned=len(quotes), shortlisted=len(short))
        self._set(pulled_at=ev.now_ist().strftime("%d %b %Y, %H:%M IST"),
                  message=f"Run in progress · {wl['name']}.")
        if len(quotes) < len(entries):
            self._warn(f"FYERS returned quotes for {len(quotes)} of {len(entries)} stocks in \"{wl['name']}\"; the rest are skipped.")
        if not short:
            raise RunAbort("No stocks passed the Scout screen (no valid quotes).")
        session = ev.market_session()
        tech = self._technicals(provider, short, quotes, session)
        chart = self._chart(short, tech, quotes, session)
        fund = self._fundamentals(short, quotes)
        nws = self._news(short, fund)
        # One candle fetch per symbol (line 4 of the tuple) is reused by the Chart Analyst, IPO overlay and settle job.
        self._daily = {e["symbol"]: tech[e["symbol"]][3] for e in short if tech[e["symbol"]] is not None}
        bundles = [self._bundle(e, quotes[e["fyers"]], tech[e["symbol"]], chart[e["symbol"]],
                                fund[e["symbol"]], nws[e["symbol"]], self._ticker(e), retrieved, session)
                   for e in short]
        bundles += self._benchmark_bundles(retrieved, session)  # reference row for excess returns
        self._warn_market(session, bundles)
        return bundles, len(entries)

    # ---------- focused (search & select) ----------
    def _collect_focused(self, target):
        """One selected instrument, no Scout screening. Returns ([bundle], 1)."""
        provider = self._provider()
        self._agent("scout", "working")
        quotes = self._fyers_call(provider.quotes, [target["provider_symbol"]])
        q = quotes.get(target["provider_symbol"])
        # Prefer the series FYERS actually returned (it also covers -EQ/-BE/-ETF, which the request does not).
        entry = {"symbol": target["symbol"], "name": target.get("name") or target["symbol"], "sector": None,
                 "cap_segment": "Selected", "asset_type": target.get("asset_type"),
                 "fyers": (q or {}).get("symbol") or target["provider_symbol"],
                 "news_query": target.get("name") or target["symbol"], "screened_as": "selected"}
        quotes = {entry["fyers"]: q or {}}
        if not q or q.get("live") is None:
            self._agent("scout", "error")
            raise RunAbort(f"FYERS returned no live quote for {entry['symbol']} ({entry['fyers']}).")
        retrieved = datetime.now(timezone.utc)
        self._agent("scout", "done", scanned=1, selected=entry["symbol"])
        self._set(pulled_at=ev.now_ist().strftime("%d %b %Y, %H:%M IST"), message="Focused analysis in progress.")
        session = ev.market_session()
        tech = self._technicals(provider, [entry], quotes, session)
        chart = self._chart([entry], tech, quotes, session)
        fund = self._fundamentals([entry], quotes)
        nws = self._news([entry], fund)
        self._daily = {entry["symbol"]: tech[entry["symbol"]][3] if tech[entry["symbol"]] is not None else None}
        bundle = self._bundle(entry, q, tech[entry["symbol"]], chart[entry["symbol"]],
                              fund[entry["symbol"]], nws[entry["symbol"]], self._ticker(entry), retrieved, session)
        self._warn_market(session, [bundle])
        if self._ticker(entry) is None:
            self._warn("No verified yfinance listing for this instrument: analyst data is reported as a gap, not guessed.")
        return [bundle], 1

    @staticmethod
    def _scored(bundles):
        """Verdict rows only: the benchmark reference row is recorded but never debated or alerted."""
        return [b for b in bundles if not b.get("benchmark")]

    def _record_benchmark(self, bundles, run_id):
        """Persist the benchmark's close so settle_pending() can compute a same-horizon reference return."""
        result = {"engine": "benchmark", "scores3": None,
                  "verdict": {"verdict": "BENCHMARK", "confidence": 0}, "fallback_reason": None}
        alert = {"eligible": False, "blocked_reasons": ["benchmark reference row; never scored or alerted"]}
        for b in bundles:
            self.store.add_verdict(run_id, b, result, alert)

    def _ticker(self, entry):
        return (self._yf.get(entry["symbol"]) or {}).get("ticker")

    def _benchmark_candles(self):
        """One request per run, shared by the regime, the ETF tracking check and the reference row."""
        if getattr(self, "_bench", None) is None:
            try:
                self._bench = self._fyers_call(self._fyers().daily_candles, f"NSE:{self.s.benchmark_symbol}-EQ") or []
            except Exception as e:
                self._warn(f"Index regime unavailable ({type(e).__name__}); regime facts are a gap.")
                self._bench = []
        return self._bench

    def _benchmark_bundles(self, retrieved, session):
        """Record the benchmark's close as a close-only verdict, so Phase-1 accuracy has a same-horizon reference
        (excess return) without a second data source. Never screened, scored or alerted; skipped if already held."""
        symbol = self.s.benchmark_symbol
        if any(symbol in (wl or {}).values() for wl in (getattr(self, "bundles", None) or {}).values()):
            return []  # the user already analysed it: its own verdict is the reference row
        try:
            daily = self._benchmark_candles()
            if not daily:
                return []
            # No live quote exists for this reference row, so its price is the last completed close.
            quote = {"live": daily[-1].get("c"), "day_high": daily[-1].get("h"), "day_low": daily[-1].get("l"),
                     "volume": daily[-1].get("v"), "prev_close": daily[-2].get("c") if len(daily) > 1 else None}
            tech, rng, gaps = ev.technicals_from_daily(daily, quote, None, "closed")
            b = ev.assemble_bundle({"symbol": symbol, "name": f"{symbol} (benchmark)", "asset_type": "ETF",
                                    "screened_as": "benchmark"}, quote, tech, rng, ev.empty_analyst("benchmark"),
                                   {"total": 0, "positive": 0, "negative": 0, "neutral": 0, "recent": []},
                                   {"price": "fyers", "candles": "fyers"}, retrieved,
                                   gaps + ["analyst.benchmark"], session="closed")
            return [{**b, "benchmark": True}]
        except Exception:
            return []

    def _bundle(self, entry, quote, tech, chart, fund, news_block, yf_sym, retrieved, session):
        """Assemble one evidence bundle; a failed Technician slot degrades to explicit nulls, never an exception.
        yf_sym is the verified yfinance ticker (None = not mapped); `source` still names the configured feed."""
        t, rng, tg = tech[:3]
        if t is None:
            t = {"rvol": None, "rvol_method": None, "price_vs_sma_pct": None, "sma_period": 10, "window_return_pct": None,
                 "swing_high": None, "swing_low": None, "day_range_position_pct": None, "trend": None,
                 "atr14_pct": None, "overnight_gap_median_pct": None, "overnight_gap_p5_pct": None,
                 "stop_level": None, "entry_zone": None}
            rng = {"high": None, "low": None, "pct_from_high": None, "position_pct": None, "basis": "unavailable"}
        ch, cg = chart
        an, _meta, fg, _ticker = fund
        nb, ng = news_block
        src = {"price": "fyers", "candles": "fyers", "analyst": "yfinance",
               "news": "google_news+yfinance+gdelt" if yf_sym else "google_news+gdelt"}
        bundle = ev.assemble_bundle(entry, quote, t, rng, an, nb, src, retrieved,
                                   list(tg or []) + list(cg or []) + list(fg) + list(ng),
                                   max_age=self.s.quote_max_age_sec, session=session)
        bundle["is_benchmark"] = bool(entry.get("screened_as") == "benchmark")
        if ch:
            bundle["chart"] = ch
        ev.attach_risk(bundle)
        return bundle

    def _enrich(self, bundles, daily=None):
        """Forward-looking overlay: events, regime, ETF/IPO, optional calibrated probabilities.
        One benchmark candle request feeds both the regime and the ETF tracking check; the yfinance handles
        already fetched for the analyst panel feed the event calendar and the ETF/IPO metadata (no second fetch).
        `daily` is the Technician's candle cache (symbol → candles) so the IPO listing age needs no new request."""
        http = self.deps.get("http")
        bench = self._benchmark_candles()
        reg = regime.from_candles(bench)
        trained = {}
        if self.s.model_enabled:
            try:
                trained = {m["horizon"]: m for m in self.store.shipped_models()}
            except Exception:
                trained = {}
        through = ev.now_ist().date().isoformat()
        for b in bundles:
            yf = self._yf.get(b["symbol"]) or {}
            items, gaps = events.collect(b["symbol"], yf.get("ticker"), http)
            blocked, nxt = events.blocks_buy(items, self.s.event_guard_days)
            nxt = nxt or (items[0] if items else None)
            b["events"] = {"items": items, "blocks_buy": blocked, "next_date": (nxt or {}).get("date")}
            b["regime"], b["regime_guard"] = reg, self.s.regime_guard
            b["sme_watch_until_checklist"] = self.s.sme_watch_until_checklist
            asset = (b.get("asset_type") or "EQUITY").upper()
            extra = []
            if asset == "ETF":
                b["etf"] = etf.profile(b, bench, yf.get("info"))
                extra = b["etf"]["gaps"]
            else:
                # Catalogue never labels IPO; listing age comes from the Technician's candles.
                p = ipo.profile(b, (daily or {}).get(b["symbol"]), info=yf.get("info"))
                if p.get("recent_ipo") or p.get("sme"):
                    b["ipo"] = p
                    extra = p["gaps"]
            if reg.get("vix_gap"):
                extra.append("regime.vix")
            b["data_gaps"] = sorted(set(b.get("data_gaps") or []) | set(gaps) | set(extra))
            predict.attach(b, trained, through)

    def _record_focused(self, bundles, run_id):
        """Persist the verdict for the audit trail. Focused runs never send Telegram in v1."""
        self._agent("messenger", "working")
        rows = []
        for b in bundles:
            r = self.results[b["symbol"]]
            alert = llm.alert_eligibility(b, r, self.s)
            reason = "focused run: notifications disabled"
            self.store.add_verdict(run_id, b, r, alert)
            rows.append(self._row(b, r, alert, "skipped", reason))
        self._set(verdicts=rows)
        self._agent("messenger", "done", sent=0, engine="off")

    # ---------- debate ----------
    def _debate(self, bundles, run_id):
        client = None
        if self.s.groq_configured:
            client = self.deps.get("groq") or llm.GroqClient(self.s.groq_api_key, self.s.groq_model,
                                                             self.s.groq_max_attempts, self.s.groq_timeout)
        else:
            self._warn("GROQ_API_KEY not set: using the deterministic scoring engine instead of the LLM panel.")
        for k in ("bull", "bear", "judge"):
            self._agent(k, "working")
        results = {}
        for b in bundles:
            r = llm.evaluate(b, self.s, client)
            fr = r.get("fallback_reason")
            if fr and fr != "no_api_key":
                iss = (r["validation"].get("issues") or [""])[0]
                self._warn(f"{b['symbol']}: LLM fallback to deterministic engine ({fr}). {iss}".strip())
            results[b["symbol"]] = r
            self._pause()
        self.results = results
        n_groq = sum(r["engine"] == "groq" for r in results.values())
        engine = ("deterministic" if n_groq == 0 else f"groq:{self.s.groq_model}" if n_groq == len(results)
                  else f"mixed ({n_groq} LLM / {len(results) - n_groq} deterministic)")
        # ETF rows carry no bull/bear score by design (they are judged on their overlay), so average only
        # rows that actually produced a number.
        bs = [s for r in results.values() if (s := r["verdict"].get("bull_score")) is not None]
        es = [s for r in results.values() if (s := r["verdict"].get("bear_score")) is not None]
        buys = sum(r["verdict"]["verdict"] == "BUY" for r in results.values())
        self._set(engine=engine)
        self._agent("bull", "done", cases=len(bs), **{"avg score": round(sum(bs) / len(bs)) if bs else "n/a"}); self._pause()
        self._agent("bear", "done", cases=len(es), **{"avg score": round(sum(es) / len(es)) if es else "n/a"}); self._pause()
        self._agent("judge", "done", verdicts=len(results), buys=buys)
        self.bundles = {b["symbol"]: b for b in bundles}

    # ---------- messenger ----------
    def _message(self, bundles, run_id):
        self._agent("messenger", "working")
        tg = self.deps.get("telegram") or (Telegram(self.s.telegram_bot_token, self.s.telegram_chat_id)
                                           if self.s.telegram_ready else None)
        today = ev.now_ist().strftime("%Y-%m-%d")
        sent, rows, buys = 0, [], []
        for b in bundles:
            r = self.results[b["symbol"]]
            alert = llm.alert_eligibility(b, r, self.s)
            status, reason = "none", None
            if r["verdict"]["verdict"] == "BUY":
                buys.append((b["symbol"], r["verdict"]["confidence"]))
                key = Store.dedupe_key(b["symbol"], today)
                if not alert["eligible"]:
                    status, reason = "blocked", "; ".join(alert["blocked_reasons"])
                elif tg is None:
                    status, reason = "skipped", "Telegram disabled or not configured"
                elif self.store.already_sent(key):
                    status, reason = "duplicate", "already alerted today"
                else:
                    ok, err = tg.send(format_buy(b, r))
                    status, reason = ("sent", None) if ok else ("failed", err)
                    sent += ok
                self.store.log_alert(run_id, b["symbol"], key, status, reason)
            self.store.add_verdict(run_id, b, r, alert)
            rows.append(self._row(b, r, alert, status, reason))
        if tg is not None:
            skey = Store.dedupe_key("SUMMARY", today, "SUMMARY")
            if not self.store.already_sent(skey):
                ok, err = tg.send(format_summary(today, self.state["engine"], buys, len(bundles)))
                self.store.log_alert(run_id, "SUMMARY", skey, "sent" if ok else "failed", err)
        rows.sort(key=lambda x: ({"BUY": 0, "WATCH": 1, "AVOID": 2}[x["verdict"]], -x["confidence"]))
        self._set(verdicts=rows)
        self._agent("messenger", "done", sent=sent, engine=("telegram" if tg else "off"))

    def _row(self, b, r, alert, status, reason):
        v = r["verdict"]
        facts, news, _ = build_facts(b)
        return {"symbol": b["symbol"], "name": b["name"], "cap": b["cap_segment"], "sector": b.get("sector"),
                "asset_type": b.get("asset_type"),
                "verdict": v["verdict"], "confidence": v["confidence"], "winner": v["winner"],
                "why": v["rationale"], "catalyst": v["key_catalyst"], "bull": v["bull_score"], "bear": v["bear_score"],
                "price": b["price"]["live"], "chg": b["price"]["day_change_pct"], "engine": r["engine"],
                "fallback": r.get("fallback_reason"), "freshness": b["freshness"], "quote_age": b.get("quote_age_sec"), "synthetic": False,
                "gaps": b["data_gaps"], "alert": {"status": status, "reason": reason},
                "guards": r["validation"].get("guards", []),
                "future": self._future(b, r),
                "detail": {"scores": {k: {"score": s.get("score"), "point": s.get("point"),
                                         "reasons": [x["text"] for x in s.get("reasons", [])]} for k, s in r["scores"].items()},
                           "scores3": r.get("scores3"), "chart": b.get("chart"),
                           "news": [{"title": n["title"], "domain": n.get("domain"), "url": n.get("url"),
                                     "sentiment": n.get("sentiment"), "published_at": n.get("published_at")}
                                    for n in b["news"]["recent"] if not n.get("suspicious")],
                           "news_note": r.get("news_interpretation"),
                           "rvol_method": b["technicals"].get("rvol_method"),
                           "analyst": {k: b["analyst"].get(k) for k in ("consensus", "num_analysts", "buy_pct", "target_mean", "upside_pct")},
                           "analyst_reason": b["analyst"].get("_reason"),
                           "range_52w": b["range_52w"], "limitation": b["analyst"].get("limitation")}}

    def _future(self, b, r=None):
        """What is knowable about the future: probabilities (never a promise), calendar, regime, risk levels."""
        t, ev_block = b.get("technicals") or {}, b.get("events") or {}
        v = (r or {}).get("verdict") or {}
        return {"prediction": b.get("prediction"), "events": (ev_block.get("items") or [])[:5],
                "next_event": ev_block.get("next_date"), "regime": b.get("regime"),
                "entry_zone": t.get("entry_zone"), "stop": t.get("stop_level"),
                "sizing": guidance(b["price"].get("live"), t.get("stop_level"), self.s.risk_budget_inr),
                "expected_range": outcomes.lookup_range(self._range_table(), v.get("verdict"), v.get("confidence"),
                                                        r.get("engine") if r else None, self.s.horizons),
                "etf": b.get("etf"), "ipo": b.get("ipo")}

    def _range_table(self):
        if self._ranges is None:
            self._ranges = outcomes.range_table(self.store, self.s.horizons)
        return self._ranges

    def _finish(self, bundles, uni_n):
        rows = self.state["verdicts"]
        buys = [r for r in rows if r["verdict"] == "BUY"]
        # ETF rows have no bull/bear scores; treat a missing score as 0 so the sort never compares None.
        top = max(rows, key=lambda r: (r["verdict"] == "BUY", r["confidence"],
                                       (r.get("bull") or 0) - (r.get("bear") or 0))) if rows else None
        focused = self.state["scope"] == "focused"
        wl = self.state.get("watchlist") or {}
        text = (f"Focused analysis of {bundles[0]['symbol']} (selected, not from a watchlist)" if focused
                else f"{wl.get('name', 'Watchlist')} · {len(bundles)} real NSE stocks analysed")
        self._set(kpis={"universe": uni_n, "in_debate": len(rows), "buys": len(buys),
                        "top_pick": {"symbol": top["symbol"], "confidence": top["confidence"], "verdict": top["verdict"]} if top else None},
                  footer={"text": text, "pulled_at": self.state["pulled_at"], "synthetic": False})
