"""Run orchestration + live state for the dashboard. One run at a time."""
import copy
import logging
import threading
import time
from datetime import datetime, timezone

import data_sources as ds
import evidence as ev
import llm
import news as newsmod
import watchlists as wl
from fyers_client import FyersClient, FyersError
from grounding import build_facts
from notifier import Telegram, format_buy, format_summary
from security import redact
from storage import Store

log = logging.getLogger("pipeline")
AGENTS = [
    ("scout", "Scout", "Screens the watchlist by day move"),
    ("technician", "Technician", "RVOL, trend, 52-week range"),
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
    def __init__(self, settings, auth, store: Store, deps=None, lists=None):
        self.s, self.auth, self.store = settings, auth, store
        self.deps = deps or {}
        # Watchlists live in PostgreSQL; a test suite injects its own store through deps['lists'].
        self.lists = lists or self.deps.get("lists") or wl.store_from_settings(settings)
        self.lock = threading.Lock()
        self.running = False
        self._yf_map = {}
        self._reset()

    # ---------- state ----------
    def _reset(self, scope="watchlist", target=None, watchlist=None):
        self.state = {"status": "idle", "mode": "live", "scope": scope, "target": target,
                      "watchlist": watchlist,
                      "message": "Ready. Pick a watchlist and press Run analysis to start.",
                      "agents": {k: {"name": n, "role": r, "status": "offline", "stats": {}} for k, n, r in AGENTS},
                      "kpis": {"universe": 0, "in_debate": 0, "buys": 0, "top_pick": None},
                      "verdicts": [], "warnings": [], "footer": None, "engine": None, "pulled_at": None}

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
    def start(self, mode, target=None, watchlist=None):
        """target → focused single-instrument run; watchlist={id,name} → that list's stocks; neither → the first list."""
        with self.lock:
            if self.running:
                return False
            self.running = True
            target = copy.deepcopy(target) if target is not None else None  # caller may mutate its dict
            watchlist = dict(watchlist) if watchlist is not None else None
        threading.Thread(target=self._thread, args=(mode, target, watchlist), daemon=True).start()
        return True

    def _thread(self, mode, target=None, watchlist=None):
        try:
            self.run(mode, target, watchlist)
        finally:
            with self.lock:
                self.running = False

    def run(self, mode="live", target=None, watchlist=None):
        """Blocking run. Never raises: failures land in state['status']='error' with a safe message.

        target set → only that instrument is analysed (scope='focused'), Messenger/Telegram is skipped entirely.
        watchlist={id,name} → the stocks stored in that list (scope='watchlist'): it is loaded and validated on the
        server and there is no fallback to another list. watchlist=None → the first stored list (legacy callers).
        """
        focused = target is not None
        scope = "focused" if focused else "watchlist"
        brief = ({k: target.get(k) for k in ("symbol", "name", "exchange", "asset_type", "provider_symbol")}
                 if focused else None)
        # Mark running and flip the state in ONE critical section: start() has already set running=True on the
        # caller's thread, so a fast run could otherwise clear it before the state said "running" (a poller then
        # saw running=False with status='running' for ever).
        self._reset(scope, brief, dict(watchlist) if (watchlist and not focused) else None)
        with self.lock:
            self.running = True
            self.state.update(status="running", mode="live", message="Run started.")
        run_id = self.store.start_run(scope)
        engine_label, uni_n, short_n, summary = None, 0, 0, {}
        try:
            bundles, uni_n = self._collect_focused(target) if focused else self._collect_watchlist(watchlist)
            short_n = len(bundles)
            self._debate(bundles, run_id)
            if focused:
                self._record_focused(bundles, run_id)
            else:
                self._message(bundles, run_id)
            engine_label = self.state["engine"]
            self._finish(bundles, uni_n)
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
                                   "scope": scope, "target": brief, "watchlist": snap.get("watchlist")})
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

    # ---------- shared agent stages (watchlist scan and focused run use the same code) ----------
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
                out[e["symbol"]] = ev.technicals_from_daily(daily, quotes[e["fyers"]], intra, session)
            except FyersError as err:  # _fyers_call already re-raised rate limits/auth as RunAbort
                out[e["symbol"]] = (None, None, [f"technicals({err.kind})"])
        rv = [t[0]["rvol"] for t in out.values() if t[0] and t[0].get("rvol")]
        self._agent("technician", "done", analyzed=len(out),
                    **{"avg RVOL" if len(out) > 1 else "RVOL": f"{sum(rv) / len(rv):.2f}x" if rv else "n/a"})
        return out

    def _fundamentals(self, entries, quotes):
        """yfinance analyst data. The ticker comes only from the verified map (never guessed); a series with no
        mapping (ETF, foreign listing) becomes a data gap. Universe and focused runs use the same rule."""
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
        self._yf_map = yf_map
        return out

    def _news(self, entries, fundamentals):
        self._agent("newsdesk", "working")
        gd = self.deps.get("gdelt") or newsmod.Gdelt(self.s.gdelt_min_interval)
        gn = self.deps.get("gnews", newsmod.google_news)
        out = {e["symbol"]: newsmod.build_news(e, gd, fundamentals[e["symbol"]][3], gnews=gn) for e in entries}
        self._agent("newsdesk", "done", headlines=sum(n[0]["total"] for n in out.values()),
                    tone=f"{sum(n[0]['positive'] for n in out.values())}+ / {sum(n[0]['negative'] for n in out.values())}-")
        return out

    def _warn_market(self, session, bundles):
        stale = [b["symbol"] for b in bundles if b["freshness"] == "stale"]
        if stale:
            ages = sorted(b["quote_age_sec"] for b in bundles if b.get("quote_age_sec") is not None)
            self._warn(f"{len(stale)} quotes are older than {self.s.quote_max_age_sec}s while the market is scheduled open "
                       f"(median age {ages[len(ages)//2] if ages else '?'}s). Check your PC clock sync; alerts are blocked for stale quotes.")
        if session != "open":
            self._warn("Market is closed (scheduled NSE hours; holidays not modelled): data is last-session, so no alerts fire.")

    def _collect_watchlist(self, watchlist):
        """Scout over one stored watchlist. Universe/focused behaviour of the other stages is unchanged."""
        try:
            self._agent("scout", "working")
            if watchlist and watchlist.get("id"):
                saved = self.lists.get(watchlist.get("id"))
            else:  # legacy start without an id: use the first stored list, never a hard-coded one
                uid = self.lists.first_id()
                if not uid:
                    raise wl.WatchlistError("empty", "No watchlist exists yet. Create one and add stocks first.")
                saved = self.lists.get(uid)
            self._set(watchlist={"id": saved["id"], "name": saved["name"], "count": len(saved["items"])})
            if not saved["items"]:
                self._agent("scout", "error")
                raise RunAbort(f"Watchlist \"{saved['name']}\" has no stocks. Add some and run again.")
            entries = [wl.to_entry(i) for i in saved["items"]]
        except wl.WatchlistError as e:
            self._agent("scout", "error")
            raise RunAbort(str(e))
        name = self.state["watchlist"]["name"]
        provider = self._provider()   # connect check first: a storage problem is fixed after it, not instead of it
        quotes = self._fyers_call(provider.quotes, [e["fyers"] for e in entries])
        short = ds.screen(entries, quotes, self.s.shortlist_per_bucket)
        retrieved = datetime.now(timezone.utc)
        self._agent("scout", "done", scanned=len(quotes), shortlisted=len(short))
        self._set(pulled_at=ev.now_ist().strftime("%d %b %Y, %H:%M IST"), message="Live run in progress.")
        missing = [e["symbol"] for e in entries if e["fyers"] not in quotes]
        if missing:
            self._warn(f"FYERS returned no quote for {len(missing)} of {len(entries)} stocks in \"{name}\": "
                       f"{', '.join(missing[:12])}{'…' if len(missing) > 12 else ''}. They are skipped, not substituted.")
        if not short:
            raise RunAbort("No stocks in this watchlist passed the Scout screen (no valid quotes).")
        session = ev.market_session()
        tech = self._technicals(provider, short, quotes, session)
        fund = self._fundamentals(short, quotes)
        nws = self._news(short, fund)
        bundles = [self._bundle(e, quotes[e["fyers"]], tech[e["symbol"]], fund[e["symbol"]], nws[e["symbol"]],
                                self._yf_map[e["symbol"]], retrieved, session) for e in short]
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
                 "news_query": target.get("name") or target["symbol"]}
        quotes = {entry["fyers"]: q or {}}
        if not q or q.get("live") is None:
            self._agent("scout", "error")
            raise RunAbort(f"FYERS returned no live quote for {entry['symbol']} ({entry['fyers']}).")
        retrieved = datetime.now(timezone.utc)
        self._agent("scout", "done", scanned=1, selected=entry["symbol"])
        self._set(pulled_at=ev.now_ist().strftime("%d %b %Y, %H:%M IST"), message="Focused analysis in progress.")
        session = ev.market_session()
        tech = self._technicals(provider, [entry], quotes, session)
        fund = self._fundamentals([entry], quotes)
        nws = self._news([entry], fund)
        bundle = self._bundle(entry, q, tech[entry["symbol"]], fund[entry["symbol"]], nws[entry["symbol"]],
                              self._yf_map[entry["symbol"]], retrieved, session)
        self._warn_market(session, [bundle])
        if self._yf_map[entry["symbol"]] is None:
            self._warn("No verified yfinance listing for this instrument: analyst data is reported as a gap, not guessed.")
        return [bundle], 1

    def _bundle(self, entry, quote, tech, fund, news_block, yf_sym, retrieved, session):
        """Assemble one evidence bundle; a failed Technician slot degrades to explicit nulls, never an exception.
        yf_sym is the verified yfinance ticker (None = not mapped); `source` still names the configured feed."""
        t, rng, tg = tech
        if t is None:
            t = {"rvol": None, "rvol_method": None, "price_vs_sma_pct": None, "sma_period": 10, "window_return_pct": None,
                 "swing_high": None, "swing_low": None, "day_range_position_pct": None, "trend": None}
            rng = {"high": None, "low": None, "pct_from_high": None, "position_pct": None, "basis": "unavailable"}
        an, _meta, fg, _ticker = fund
        nb, ng = news_block
        src = {"price": "fyers", "candles": "fyers", "analyst": "yfinance",
               "news": "google_news+yfinance+gdelt" if yf_sym else "google_news+gdelt"}
        return ev.assemble_bundle(entry, quote, t, rng, an, nb, src, retrieved, list(tg or []) + list(fg) + list(ng),
                                 max_age=self.s.quote_max_age_sec, session=session)

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
        bs = [r["verdict"]["bull_score"] for r in results.values()]
        es = [r["verdict"]["bear_score"] for r in results.values()]
        buys = sum(r["verdict"]["verdict"] == "BUY" for r in results.values())
        self._set(engine=engine)
        self._agent("bull", "done", cases=len(bs), **{"avg score": round(sum(bs) / len(bs))}); self._pause()
        self._agent("bear", "done", cases=len(es), **{"avg score": round(sum(es) / len(es))}); self._pause()
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
                "detail": {"scores": {k: {"score": s.get("score"), "point": s.get("point"),
                                         "reasons": [x["text"] for x in s.get("reasons", [])]} for k, s in r["scores"].items()},
                           "news": [{"title": n["title"], "domain": n.get("domain"), "url": n.get("url"),
                                     "sentiment": n.get("sentiment"), "published_at": n.get("published_at")}
                                    for n in b["news"]["recent"] if not n.get("suspicious")],
                           "news_note": r.get("news_interpretation"),
                           "rvol_method": b["technicals"].get("rvol_method"),
                           "analyst": {k: b["analyst"].get(k) for k in ("consensus", "num_analysts", "buy_pct", "target_mean", "upside_pct")},
                           "analyst_reason": b["analyst"].get("_reason"),
                           "range_52w": b["range_52w"], "limitation": b["analyst"].get("limitation")}}

    def _finish(self, bundles, uni_n):
        rows = self.state["verdicts"]
        buys = [r for r in rows if r["verdict"] == "BUY"]
        top = max(rows, key=lambda r: (r["verdict"] == "BUY", r["confidence"], r["bull"] - r["bear"])) if rows else None
        focused = self.state["scope"] == "focused"
        text = (f"Focused analysis of {bundles[0]['symbol']} (selected, not from a watchlist)" if focused
                else f"Built from {len(bundles)} real NSE stocks in \"{(self.state.get('watchlist') or {}).get('name', 'watchlist')}\"")
        self._set(kpis={"universe": uni_n, "in_debate": len(rows), "buys": len(buys),
                        "top_pick": {"symbol": top["symbol"], "confidence": top["confidence"], "verdict": top["verdict"]} if top else None},
                  footer={"text": text, "pulled_at": self.state["pulled_at"], "synthetic": False})
