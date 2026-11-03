# Forward-Looking Prediction & Reliability Roadmap

## Summary — the honest reframe
No method can promise "this stock WILL gain tomorrow" — and this plan will not fake one. What it builds instead is the professional version of the same goal: **calibrated probabilities + audited accuracy + risk-defined exits**, improving through a closed loop:

> record every signal at decision time → settle vs real forward returns → see which rules actually make money → add forward-looking evidence (events, regime, volatility) → retrain/recalibrate under strict no-leakage rules → verify out-of-sample → repeat.

Your 3:20 PM case is handled explicitly: it becomes a dedicated **overnight playbook** (close-entry filters + next-open/next-close settlement + event guard), and the platform will warn when a near-close entry may execute at Closing-Auction (CAS) prices.

## What the code shows today (verified)
- `scoring.py` `judge()`: BUY needs net ≥ 25 + `leadership()` — inputs (RVOL, 52w position, SMA10 trend, analyst upside, headline tone) are all **backward-looking**; `scores3` shadow block is computed but consumed by no rule.
- `data_sources.py` `screen_watchlist()` shortlists **top-N by day_change_pct** → the system systematically studies today's biggest gainers, then `score_bull` rewards them for being up (chase bias).
- `news.py`: every headline counts equally regardless of age/repetition; research (Tetlock, RFS 2011) shows stale repeated news precedes **reversals** — exactly the retail-chased pattern this platform can produce.
- `evidence.py` `market_session()`: scheduled Mon–Fri 09:15–15:30 only — no holiday list, no CAS awareness; `fyers_client.py` history candles = bar start, completed bars only (verified with FYERS docs).
- `storage.py`: full `bundle`+`result` JSON is already persisted per verdict — the ideal substrate for a forward-outcome engine; **no outcomes table exists**.
- `instrument_search.py` `parse_master()`: every NSE cash instrument labelled "EQUITY"; ETF/IPO not distinguished.

---

## Phase 0 — Foundations (small, unblocks everything)
1. **Audit the shadow scores**: `storage.add_verdict` also persists `scores3` + `chart.detector_version` (SQLite migration in `SCHEMA`) so future calibration can learn from today's shadow signals.
2. **Asset typing**: `instrument_search.parse_master` classifies ETF (name/series heuristics) vs EQUITY; recent-IPO detection is a data gap until Phase 2. `asset_type` flows through `pipeline._bundle`/`_row`.
3. **Session honesty** in `evidence.py`: NSE holiday list (fetched once, cached; explicit gap if unavailable); warning when a run starts inside 15:00–15:30 that entry may fill at auction (CAS) price — current 2026 timings verified during implementation.
4. **Measurement hygiene**: keep `screen_watchlist` as-is, but Phase-1 stats separate "analysed because top gainer" rows so hit rates aren't silently conditioned on the bias.

## Phase 1 — The accuracy flywheel (top priority)
New `outcomes.py` (pure math) + settle job + `/api/accuracy` + dashboard Accuracy card.
1. **Schema**: `outcomes(verdict_id, horizon, entry_price, next_open, close_1d, close_5d, close_20d, close_60d, gap_pct, mfe_pct, mae_pct, settled_at)`. Entry = bundle live price (already stored per verdict). Horizons fixed: 1, 5, 20, 60 trading days.
2. **Settle job** (idempotent; nightly 19:00 IST + on-demand): reuses `FyersClient.daily_candles`; computes forward returns; **MFE/MAE from daily highs/lows** — this directly answers "what if it crashes minutes later".
3. **Accuracy card** (dashboard + `/api/accuracy`): per verdict × confidence bucket × horizon × engine: N, hit rate, median/mean/p10/p90 return, median MAE; benchmark = NIFTYBEES same-horizon (index symbol verified against FYERS master during impl, NIFTYBEES fallback); rolling 3-month trend. This is "how much gain to expect" — as measured history, not a promise.
4. **Replay accelerator** (optional): historical replay of `evaluate_deterministic` over as-of candle windows to bootstrap a baseline backtest immediately; technical features only (news is not point-in-time available) — limitation shown in UI.

## Phase 2 — Forward-looking evidence (future scope, not past moves)
1. **Event calendar**: new `events.py` fetches NSE corporate announcements / board meetings / financial results (free pages, defensive parsing, injected `http` per codebase pattern) → new E* facts via `grounding.FACT_SPEC` ("results on DATE", "ex-dividend DATE"). Both the LLM panel and Python guards gain future knowledge.
2. **Event-risk guard** (Python-side in `llm.alert_eligibility` + `scoring.judge` path, default `EVENT_GUARD_DAYS=1`): BUY blocked/downgraded when a scheduled event falls inside the holding window — the "many reasons a stock crashes" mitigation.
3. **News quality** in `news.py`: hours-age weighting + cross-run repetition fingerprints (small hash table) → "stale news" flag; stale-positive news after a run-up becomes a downgrade trigger.
4. **Regime facts**: new `regime.py` — index vs 50/200-day SMA, 20-day index return, VIX if a free source holds (else gap); risk-off regime caps BUYs (same pattern as the existing `leadership()` guard).
5. **Volatility/risk facts** in `evidence.technicals_from_daily`: ATR14%, overnight-gap distribution (median, 5% tail), ATR/zone-based stop level and entry zone (from `chart_patterns` findings).
6. **ETF path** (`etf.py`): premium/discount vs iNAV (source permitting, else gap), tracking difference vs index via FYERS candles, liquidity (ADV, spread proxy), expense ratio (yfinance/AMC, gap if absent). ETF rows stop being scored by equity judgement rules.
7. **IPO path** (`ipo.py`): listing age, price vs issue price, SME flag, SEBI-advisory-derived risk checklist (exaggerated-claims pattern: bonus/split after run-up, promoter selling); grey-market premium is **never an input** (unregulated, no legal protection); SME default verdict = WATCH until checklist complete (config flag).

## Phase 3 — Calibrated probability engine (the honest "prediction")
1. New `predict.py` (sklearn as **optional** dependency): logistic / gradient-boosting per horizon on Phase-1 outcomes + bundle facts → P(close > entry) at 1d/5d/20d.
2. **Non-negotiable discipline**: purged walk-forward CV with gap = horizon (`TimeSeriesSplit(gap=...)`), calibration fit on data disjoint from training (sklearn calibration guide), min 200 samples/fold, **experiment ledger table** logging every trial (overfitting stays visible, per Bailey et al. PBO), feature-drift checks, weekly retrain.
3. **Ship gate**: a model is used only if its out-of-sample Brier score / hit rate beats the Phase-1 deterministic baseline on identical folds; otherwise it stays a dashboard experiment.
4. Model output enters the bundle as a fact ("P(+5d up) = 62%, calibrated, trained-through DATE, N=…"), never auto-BUY; thresholds move only when Phase-1 evidence supports it.

## Phase 4 — Decision layer & UX
1. Verdict rows gain: P(up) per horizon with interval, expected range (p10–p90), event flags, regime label, entry zone/level, stop level, position-size guidance (new `sizing.py`: risk budget ÷ (entry − stop)); explicit disclaimer text everywhere.
2. **Overnight playbook view** (15:00–15:20 IST): liquidity + gap-stats + no-next-day-events + regime filters; settles as next-open/next-close via Phase 1.
3. `dashboard.html`: Accuracy page, per-row "future" panel (probabilities, calendar, regime), asset-type-aware rendering.
4. `app.py` new endpoints: `/api/accuracy`, `/api/outcomes/settle` (same CSRF/secret gating as existing APIs).
5. New `config.Settings` fields (env-driven, defaults shown): `event_guard_days=1`, `regime_guard=True`, `model_enabled=False`, `settle_hour_ist=19`, `benchmark_symbol=NIFTYBEES`, `horizons="1,5,20,60"`.

## Phase 5 — Free vs paid data (documented, no commitment)
- **Free path (default, everything above runs on it)**: FYERS candles/quotes, NSE public corporate-filing pages, yfinance metadata, NIFTYBEES benchmark.
- **Paid unlocks** documented in `DATA_SOURCES.md` (not implemented): NSE paid EOD/order-trade & real-time feeds (deeper gap/liquidity modelling, faster announcements), licensed point-in-time fundamentals, option-chain IV (true market-implied expected moves), vendor corporate-actions feeds — each entry states what it would measurably improve.

## Non-goals
No order placement / automated trading; no F&O or intraday scalping; no grey-market-premium signals; no guaranteed-gain claims — UI states probabilities and audited history only.

## Test plan
- Unit tests (existing conftest fake pattern, no network): `outcomes.py`/`regime.py`/`sizing.py` golden math; guard tests (event window, regime cap, stale-news downgrade); ETF/IPO branch tests.
- Calibration sanity: synthetic perfectly-calibrated predictor → Brier ≈ base rate; leakage test asserting train/test gap ≥ horizon.
- Integration: settle-job idempotency, `/api/accuracy` shape, holiday/CAS warnings, config parsing; dashboard render smoke for Accuracy card and future panel.

## Assumptions & risks
- Single-user SQLite sufficient at current scale; FYERS access continues; NSE pages may change → every fetcher degrades to explicit gaps, never wrong data.
- Survivorship bias: replay universe is today's symbol master — stated on the Accuracy page.
- Phase 3 needs accrued real verdicts (weeks of runs) before the ship-gate can be honest; replay accelerates but with technical-features-only caveat.
