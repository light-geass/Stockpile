# Data sources — free path, gaps, and what paid would buy

No fetcher in this codebase is allowed to invent data. Every source below degrades to an **explicit gap**
(an entry in `bundle["data_gaps"]`, e.g. `analyst.target_mean`, `etf.premium_inav`, `regime.vix`) and the
verdict caps at WATCH rather than guessing. This file records what runs free, what it costs you in fidelity,
and what a paid feed would measurably improve.

## Free path (the default — everything in the roadmap runs on this)

| Data | Source | Code | Known limitation |
|---|---|---|---|
| Quotes (live price, OHLC, volume) | FYERS `/quotes` | `fyers_client.py` | `tt` is not a reliable quote time, so freshness is reported as `unknown` rather than falsely `stale`. |
| Daily / intraday candles | FYERS `/history` | `fyers_client.py` | Completed bars only. Daily bar `t` is the bar **start**, so a decision day is anchored by date, not timestamp. |
| Benchmark + regime | NIFTYBEES daily candles | `regime.py` | NIFTYBEES is an ETF proxy for the index. No India VIX — that needs a licensed NSE feed, so `regime.vix` is a gap. |
| Trades today | FYERS history/replay as-of engine | `outcomes.py` | Replay is technical-features-only; news/analyst/events were not point-in-time available. |
| Analyst consensus & targets | yfinance `.info` / recommendations | `data_sources.py` | Yahoo blocks datacentre IPs; optional `YFINANCE_PROXY` exists. Recommendations only — no valuation ratios. |
| Headlines | yfinance news → Google News RSS → GDELT | `news.py` | Headline-only lexicon sentiment, not full-text NLP. Google News matches loosely, so titles are relevance-filtered. |
| Corporate events (results, ex-dividend) | yfinance calendar + NSE `event-calendar` JSON | `events.py` | NSE pages change without notice; any failure is a gap, never a fabricated meeting. |
| Exchange holidays | NSE holiday master, cached in SQLite | `evidence.py` | Falls back to the published CMTR circular list, marked `gap: true` in the status payload. |
| Symbol master / asset typing | FYERS `NSE_CM.csv` | `instrument_search.py` | Excludes delisted names (survivorship), so replay universe is today's master. |
| ETF metadata (NAV, expense, ADV) | yfinance `.info` (`intradayNav` / `navPrice`) | `etf.py` | Premium is measured against a real iNAV when the feed exposes one; otherwise against `navPrice`, which is **EOD** — labelled `nav_source=nav_eod` / `premium_is_eod` and it can never approve a BUY. No NAV at all is the `etf.premium_inav` gap. |
| IPO issue price / listing age | yfinance `.info` + first candle | `ipo.py` | Listing age and price-vs-issue come from candles. A bonus/split reset is detected from the candle series itself (a close-to-close drop ≥ 10× the stock's own median daily move). Promoter holding is the remaining gap unless an `info`/vendor field supplies it. **Grey-market premium is never an input** — unregulated, no legal recourse. |
| Probability model | sklearn, optional | `predict.py` | Trains only on accrued Phase-1 outcomes (≥ 200 rows/horizon). Ships only if out-of-sample Brier beats the base rate on identical purged folds. Retrain on demand via `POST /api/model/retrain` — with `MODEL_ENABLED=false` it still ledgers every trial but publishes nothing. |

## Paid unlocks (documented, deliberately not implemented)

Each entry states what it would measurably improve. None is required for the current system to run honestly.

### 1. NSE paid EOD + order-trade + real-time feeds
**Would improve:** overnight-gap modelling and liquidity floors.
Currently the overnight-gap distribution is built from daily open/close and the liquidity floor is a crude
volume threshold. Real tick/order-book data gives true bid-ask spread, depth at the close, and intraday fill
quality — which is exactly what the 15:00–15:20 overnight playbook window depends on.

### 2. Licensed point-in-time fundamentals
**Would improve:** replay fidelity and the model's feature set.
Replay currently loses news, analyst coverage and events because none of them are point-in-time available
free. A PIT vendor makes replay a real backtest instead of a technical-only approximation, which is the single
biggest accuracy risk in the current design.

### 3. Option-chain IV
**Would improve:** expected-move estimates.
Today "expected range" is a measured distribution of past returns. Market-implied IV from the current option
chain would give the forward-looking expected move around events, which is a genuinely forward-looking
signal rather than an extrapolation of history.

### 4. Vendor corporate-actions feed
**Would improve:** the IPO and ETF checked flags.
`ipo.corporate_actions_feed` (bonus/split history) and `etf.tracking_diff_20d` (dividend-adjusted) both
currently degrade to gaps because the free data does not adjust consistently. A clean CA feed removes two
permanent gaps and makes the SME checklist completable.

### 5. Exchange-licensed announcement stream
**Would improve:** event-guard latency.
`events.py` polls on demand. A licensed announcement push feed would let the event guard react *before* a
scheduled earnings release, instead of blocking a BUY after detection.

## Rules that hold regardless of source

- Never fabricate a value to fill a gap.
- Every accuracy number is **measured history, not a promise** — the UI and the API both carry that disclaimer.
- No order placement, no automated trading, no F&O, no grey-market signals.
- Probability output is a fact on the bundle, never a BUY trigger; thresholds move only on Phase-1 evidence.
