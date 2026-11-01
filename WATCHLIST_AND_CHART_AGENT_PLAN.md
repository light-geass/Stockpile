# Watchlists and Chart-Pattern Agent — Implementation Plan
**Status:** Implemented — Phase 1 (Supabase watchlists + one-time `universe.json` import, which is now deleted from the codebase). Phases 3–4 (chart stage, three scores) are still to do.  
**Prepared:** 2026-11-01  
**Project:** Signal Desk  
**Scope:** Add user-managed watchlists backed by Supabase Postgres; add a conservative, explainable chart-analysis support stage for Bull/Bear/Judge. Preserve the existing stock-search behavior and analysis-only safety posture.

---

## 1. Executive summary
Both requested capabilities are feasible within this codebase.

1. **Watchlists:** persist user-created lists in Supabase Postgres, not Render’s ephemeral filesystem. Add CRUD endpoints and a sidebar/list selector; runs analyze only the server-loaded selected list. Preserve the existing stock search and focused-analysis behavior.
2. **Chart support stage:** analyze the FYERS OHLCV already fetched by this project. No chart-pattern or market-data external API is part of this feature. Implement deterministic Python rules with explicit definitions and candle-level evidence.
3. **Conservative findings:** the chart stage is advisory evidence for Bull, Bear, and Judge—not an independent decision-maker or a new authority that Judge must follow. Emit only findings that satisfy strict, testable criteria; otherwise return `no reliable pattern identified` or `insufficient data`, with no guessed pattern or forced score.
4. **Score separation:** preserve the requested three scores (news/trends, chart patterns, combined overall) with transparent, versioned calculations. Judge may consider the chart evidence as one input but must still weigh the Bull/Bear cases and existing safety rules. Show overall in the collapsed row and components/reasons when expanded.

“Pro trader quality” is an engineering objective, not a claim that code can reproduce every expert’s subjective chart reading. The plan therefore defines a conservative, explainable, backtested system and forbids unsupported certainty.

---

## 2. Current project findings
- `pipeline.py` currently describes eight stages: Scout, Technician, Fundamentalist, Newsdesk, Bull, Bear, Judge, Messenger.
- `FyersClient.daily_candles()` already retrieves daily OHLCV history; `intraday_candles()` retrieves completed 5-minute candles during market hours. Both use FYERS `/history` and the shared client-side rate limiter.
- `Pipeline._technicals()` requests daily candles for each analyzed stock and currently converts those into RVOL, SMA/trend, return, swing levels and range metrics. Pattern analysis can consume the same candle data, avoiding a duplicate market-data request.
- Live universe runs load `universe.json`; focused search runs use a canonical instrument returned by the cached FYERS symbol catalogue. The search API and focused flow are independent of the universe list and should remain unchanged.
- The Flask app already has CSRF checks on POSTs, Host checks, a same-origin UI API, and a `Store` class backed by SQLite for audit history. Watchlist writes should follow those protections and use Supabase Postgres through a server-side PostgreSQL connection. Existing audit SQLite remains local/ephemeral unless separately migrated.
- Result details currently include deterministic or LLM panel score reasons, headline evidence, analyst fields, and technical/range details. The compact row and expanded detail are built in `Pipeline._row()` and `dashboard.html::renderRow()`.
- The existing “Bull/Bear/Judge” LLM path is one structured Groq call per stock that role-plays the panel, with grounding validation and deterministic fallback. Add chart findings as optional, grounded evidence to this panel. The Judge should not treat the chart stage as an instruction or allow it to dominate conflicting/stronger evidence.
- `universe.json` grouped stocks by user-defined buckets. It has been **retired**: its 24 stocks (with their Large/Mid/Small buckets) are imported once as **Watchlist 1** by `watchlists.ensure_seeded()` in `watchlists.py`, and runs now load their stocks from Supabase Postgres.

---

## 3. Research conclusion: chart analysis feasibility and options
### 3.1 Is a chart-analysis support stage feasible?

**Yes.** The existing pipeline retrieves ordered FYERS OHLCV bars, so local deterministic analysis can operate directly on candles without image recognition or a third-party API. It should run alongside the Technician stage and provide optional, structured evidence to Bull/Bear/Judge.

The stage must not become a ninth decision-maker or a command for Judge. It reports its own findings and uncertainty; Bull and Bear assess the relevance and limitations, and Judge weighs that evidence with the rest of the case. Lack of a detected pattern is a valid result, not a reason to invent one.

To make this as rigorous as practical:
- Define a limited pattern catalog with objective, documented criteria and trend context.
- Use completed bars; separate forming, confirmed, and invalidated states.
- Attach dates, levels, pivots, and confirmation criteria to each finding.
- Apply conservative thresholds and return no finding when evidence is ambiguous.
- Validate detectors on time-ordered historical samples; report false positives and limitations.
- Never equate detector confidence with probability of profit or claim that a chart formation guarantees direction.

### 3.2 Data and external services
**No external API will be used for chart patterns.** Use the current FYERS candle adapter for market data and local deterministic code for chart analysis. Do not add a second quote/history provider, a chart-pattern SaaS, a web-scraped source, or an image-analysis endpoint as a dependency.

TA-Lib may be evaluated as an optional local open-source helper for standard candlestick labels, subject to install/build checks; it is not required for geometric patterns and cannot substitute for documented, validated project rules. Sources: [TA-Lib](https://ta-lib.org/), [TA-Lib Python wrapper](https://github.com/TA-Lib/ta-lib-python). For MVP, prefer a small pure-Python detector set to keep Windows and Render deployments reproducible.

Supabase is the selected managed PostgreSQL datastore for watchlists. The Flask server will connect using `DATABASE_URL` stored only in Render’s private environment configuration. Do not use Supabase browser/anon keys or service-role keys for this server-to-database connection. Supabase is a storage service, not an analysis or chart-pattern API.

### 3.3 Conservative detector roadmap
**Initial catalog (after objective definitions and fixtures):**
- Candle anatomy/context and a small set of well-defined candlestick patterns.
- Confirmed swing structure (higher highs/lows, lower highs/lows) only when pivots are established.
- Support/resistance zones derived from repeated, separated pivots; display tolerance and source bars.
- Breakout/breakdown only on a completed close beyond a validated level; volume may be a confirmation factor, not proof.

**Later, only after tests/backtests:** double top/bottom, triangles, head-and-shoulders variants, and channels. Each needs explicit tolerances, trend context, invalidation and confirmation rules.

Every finding includes status (`forming`, `confirmed`, `invalidated`), timeframe, dates, key levels/pivots, detector version, and evidence. If a rule is not met confidently, omit the pattern and return a neutral/no-reliable-pattern status. Do not force patterns from a fixed checklist.

### 3.4 Inputs and data considerations
- Use the existing FYERS daily candles initially, preserving current provider/rate-limit handling.
- Analyze completed bars only. The existing daily API may include the current session candle; explicitly filter incomplete/current-session bars when appropriate, or label them provisional. Do not allow today’s partial candle to create a confirmed pattern.
- Confirm candle order and duplicates, require a minimum valid history, reject null/non-finite OHLCV, and guard zero-range/zero-volume bars.
- Use daily timeframe for MVP to match current technical analysis. Intraday patterns can be a later selectable timeframe and should have separate score calibration.
- Reuse the returned daily candle data rather than making a second history request. If refactoring is needed, return candles alongside technicals through an internal structure, but never expose a huge raw candle payload in the dashboard/audit response by default.
- FYERS’ production behavior, data coverage, and exact history limits remain subject to the app’s existing live-integration caveat; the code path exists, but prior project verification states live candle calls have not been tested against the real account.

---

## 4. Product requirements and behavior
### 4.1 Watchlist behavior
- A left-side navigation panel lists named watchlists (e.g. Watchlist 1, Watchlist 2) and an **+ New watchlist** action.
- Selecting a list displays its stocks and marks it as the active list.
- A stock can be added to the active watchlist from the existing autocomplete search selection. The current **Analyze selected** action continues to work exactly as today; search behavior, result count, keyboard behavior, and server-side instrument validation must not change.
- Provide removal of a stock from the active list. Editing watchlist names is desirable; deletion should ask for confirmation or be undoable. Deleting the final list should either be prevented or immediately create/select a default list.
- The primary **Run analysis** control gets a selector for the chosen watchlist. Running it analyzes that list only, via the existing Scout shortlist/debate path. The request must contain a stable watchlist ID, not arbitrary client-supplied symbols; the server loads and validates its saved contents.
- A list with no stocks should block the run with a clear message. Missing/invalid/stale instruments should be reported as row-level warnings and skipped safely, or rejected before the run with an explicit summary; never silently substitute the whole universe.
- Keep focused **Analyze selected** as a one-stock run with its existing no-Telegram behavior. Decide whether adding to a watchlist is an explicit separate action in the search result/selected-stock area so existing semantics are unambiguous.
- `universe.json` has been deleted from the codebase. Its contents are a constant (`watchlists.UNIVERSE_SEED`) imported once as an editable **Watchlist 1** on the first start against an empty database; the migration marker plus a pre-existing-list check mean it never re-runs or overwrites user lists. No run falls back to it (`POST /api/start` without an id uses the first stored list).

### 4.2 Chart support and three scores
- Show **Chart Analyst** as a technical evidence stage, clearly distinguished from the decision agents.
- Return a compact structured `chart_analysis`: availability, timeframe, detector version, findings, evidence, and limitations.
- The chart stage may inform **Bull, Bear, and Judge**, but it is never an independent verdict stage. Judge receives the chart findings as one evidence source alongside news/trends, fundamentals, technician metrics, and Bull/Bear arguments. The prompt and deterministic logic must explicitly prevent chart output from overriding other evidence automatically.
- If no pattern passes its confidence/confirmation gates, return `patterns: []`, `status: no_reliable_pattern` (or `insufficient_data`), and no fabricated explanation. Do not infer likely patterns from weak similarities or ask an LLM to fill gaps.
- Keep scores distinct: (1) news/trends, (2) chart-pattern evidence, (3) deterministic overall combination. Define their scale/direction, inputs, weights, missing-data behavior, and version before implementation. Do not silently impute missing scores.
- Show overall score prominently; show component scores, reasons, pattern evidence, and limitations in expanded details. Bull/Bear should cite relevant chart evidence and explain whether it strengthens, weakens, or does not change their cases.
- Keep existing verdict and alert thresholds unchanged in the first release. Run new scores in shadow mode and calibrate before any policy change. Judge may consider overall score, but must remain a balanced arbiter rather than a pass-through of Chart Analyst.

---

## 5. Proposed implementation phases
### Phase 0 — database, security, and contracts
1. Use Supabase Postgres for watchlist data. Add a PostgreSQL driver and read `DATABASE_URL` server-side; do not use Supabase client keys. Keep secrets out of source control and logs.
2. Create versioned SQL migrations for watchlists/items and a one-time `universe.json` seed marker. Migrations must be safe to run on every Render deploy and must never overwrite existing user-managed rows.
3. Decide whether there is one operator-owned dataset or per-user data. Current app has no user/account model, so MVP is a single protected dashboard’s shared lists; do not pretend lists are user-isolated.
4. Define DB connection lifecycle/pooling for the single Render instance, SSL requirements, connection failure messaging, and backup/export/restore procedure. Fail safely rather than silently falling back to in-memory or local storage.
5. Define score schemas, detector confidence/abstention rules, evidence contracts, and no-verdict-change shadow policy before pipeline changes.

### Phase 1 — Supabase watchlist persistence and API
1. Add PostgreSQL tables for lists and items, stable IDs, unique instrument-per-list constraints, timestamps, indexes, foreign keys, and schema migration versioning.
2. Seed/import `universe.json` once in a transaction and record completion; later deploys must never reseed over user edits.
3. Add CSRF-protected CRUD endpoints. Resolve add-stock symbols against the existing FYERS catalogue on the server. Bound list/item sizes and validate all input.
4. Make each selected-list run resolve its instruments from Supabase server-side. Keep universe fallback explicit and only for legacy requests; never substitute it for a missing/empty selected list.
5. Capture list ID/name and run scope in pipeline state/audit. Preserve focused search behavior unchanged.

### Phase 2 — sidebar and selected-list run UI
1. Add a responsive sidebar listing lists, active-list state, counts, creation/rename/deletion, and item removal.
2. Reuse the current search selection; provide **Add to watchlist** separately from **Analyze selected**.
3. Add the watchlist dropdown beside **Run analysis**; display selected name/count; disable run on an empty list with a clear explanation.
4. Preserve autocomplete/search behavior and keyboard interaction. On narrow screens, collapse the sidebar into a drawer.
5. Use the existing same-origin API helper, CSRF, text-only DOM rendering, and CSP constraints.

### Phase 3 — chart support module and grounded evidence
1. Reuse daily candles fetched for Technician; do not make a second data request.
2. Implement pure `chart_patterns.py` normalization/detection code and a conservative initial detector catalog. No external chart-pattern API. Optional TA-Lib only if deploy compatibility is verified; pure Python remains the supported baseline.
3. Add synthetic fixtures, edge/near-miss cases, minimum-data requirements, and explicit abstention. Detector returns no reliable finding if criteria are not all satisfied.
4. Add Chart Analyst as a technical evidence stage in pipeline/UI; report status and limitations without presenting it as an independent verdict agent.
5. Add chart findings as optional grounded facts available to Bull, Bear, and Judge. Update prompts/validators so model output cannot invent detections, numeric confidence, levels, or score values.

### Phase 4 — scores and balanced Judge integration
1. Specify news/trend score inputs and chart score mapping independently. Include reasons and availability flags.
2. Cap correlated findings; neutralize conflicting patterns; score only findings that pass defined quality/confirmation rules. Abstention is not a low-confidence bullish/bearish score.
3. Compute the overall score deterministically in Python with formula/version and explicit missing-component handling.
4. Provide the three scores and grounded reasons to Bull/Bear/Judge. The LLM may reason about evidence but may not calculate/alter scores or assert unsupported findings.
5. Show only overall in the collapsed result; expanded view exposes components, formula, evidence, and limitations.
6. Keep verdicts/Telegram rules unchanged during shadow rollout. Evaluate balanced decision quality before considering any later threshold change.

### Phase 5 — validation, rollout, and calibration
1. Unit test storage migrations, first-run seeding, CRUD, symbol validation, run scope isolation, empty lists, duplicate entries, deletes, and concurrent writes.
2. Add pipeline tests proving a watchlist run analyzes only that list, does not fall back to `universe.json`, does not affect focused search, and preserves current Messenger/Telegram safety rules unless the user explicitly changes policy.
3. Add chart unit fixtures for each accepted pattern, near-misses, invalid/partial candles, insufficient history, ties, gaps, and current incomplete candles.
4. Run historical walk-forward tests with time-ordered data, no look-ahead, split-adjustment awareness, survivorship-bias caveats, and trading friction if assessing predictive utility. Compare against a simple baseline and report false-positive rates, not just apparent accuracy.
5. Run the full existing pytest suite and manual/headless UI checks; verify mobile layout, keyboard accessibility, CSP/CSRF behavior, and app startup with the chosen dependency set.
6. Roll out with existing verdict/alert rules unchanged. Only after calibration decide whether score 3 affects verdicts, confidence, or notifications; document changes and thresholds separately.

---

## 6. Supabase storage and API contracts
### Watchlist object
```json
{
  "id": "stable-server-generated-id",
  "name": "Watchlist 1",
  "items": [
    {
      "provider_symbol": "NSE:TCS-EQ",
      "symbol": "TCS",
      "name": "Tata Consultancy Services"
    }
  ],
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

The browser must not be trusted for `name`, exchange, asset type, or provider symbol validity. Add-stock requests should name a `provider_symbol` from a current catalogue entry; the server resolves authoritative fields just as `/api/analyze-selected` does. The Flask backend uses `DATABASE_URL` for Supabase Postgres; no Supabase key is sent to the browser.

### Suggested endpoints (final names can follow existing conventions)

- `GET /api/watchlists` — list watchlists, active/default ID, counts.
- `POST /api/watchlists` — create a list.
- `PATCH /api/watchlists/<id>` — rename.
- `DELETE /api/watchlists/<id>` — delete with final-list protection.
- `POST /api/watchlists/<id>/items` — add a catalogue-validated instrument.
- `DELETE /api/watchlists/<id>/items/<encoded-provider-symbol>` — remove item.
- Extend `POST /api/start` to accept `watchlist_id`; server resolves its stocks and starts the corresponding run. Without an id the first stored list is used (never a file on disk).

Do not expose a bulk endpoint that accepts arbitrary unverified symbols. Enforce a configurable maximum stocks per watchlist and maximum run size to protect FYERS rate limits.

---

## 7. Score contract proposal
Use a stable result schema, for example:

```json
{
  "score_schema_version": 1,
  "scores": {
    "news_trend": {"score": 0, "available": true, "reasons": []},
    "pattern": {"score": 0, "available": true, "reasons": []},
    "overall": {"score": 0, "available": true, "formula": "0.5*news_trend+0.5*pattern"}
  }
}
```

The zero values above are schema examples, not a recommendation that missing/neutral evidence should be scored as zero. Define score direction clearly (e.g. higher = more bullish) and retain separate bear evidence. The current engine uses separate Bull/Bear points; do not force-fit those to the proposed three-score contract without specifying whether score 1 is bullishness, risk-adjusted setup quality, or a signed signal.

**Important design decision:** the phrase “overall score = 1 * 2” is ambiguous. If it means multiplication, a 0–100 × 0–100 product is difficult to interpret and changes scale. Prefer a normalized weighted combination or a clearly defined interaction formula. Decide and document it before implementation; do not implement literal multiplication without a product requirement.

A chart pattern result should have bounded data such as:

```json
{
  "timeframe": "1D",
  "detector_version": "1",
  "available": true,
  "patterns": [
    {
      "name": "double_bottom",
      "direction": "bullish",
      "status": "forming",
      "confidence": 0.0,
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "evidence": {"pivot_prices": [], "neckline": null, "confirmation": "close_above_neckline"}
    }
  ],
  "limitations": []
}
```

The score and evidence should be recomputable from source candles and detector version. Watchlist state lives in Supabase Postgres; audit results currently live in SQLite unless a separate audit migration is approved.

---

## 8. Risks and mitigations
| Risk | Mitigation |
|---|---|
| Watchlist data lost or reset on deploy | Store in Supabase Postgres, not Render’s ephemeral filesystem; use migrations, one-time seed marker, and periodic export/restore checks. |
| Supabase credentials exposed or over-privileged | Keep `DATABASE_URL` only in Render server environment; never expose it in HTML/JS or logs; use a least-privilege DB role if practical and rotate credentials if exposed. |
| Database unavailable during a run | Fail visibly and safely; do not silently switch to local/in-memory storage or substitute the universe. |
| Arbitrary/injected symbols submitted by browser | Resolve instruments against the existing FYERS catalogue on the server; store canonical provider symbols. |
| Many watchlist stocks exceed rate budgets | Cap list/run size, retain quote batching, preserve per-candle request budget, show expected scope and rate-limit failures clearly. |
| A watchlist is mistaken for the whole universe | Show selected list name/count in run controls, state, footer, and audit record. No silent fallback. |
| Partial candles create false patterns | Use completed bars for confirmations; mark live-session readings provisional. |
| Pattern rules overfit or fire too often | Document rules, test near-misses, walk-forward validation, shadow scoring, version detector parameters. |
| Duplicate/conflicting patterns inflate scores | Group correlated signals, cap per-family contribution, expose conflict/neutralization, calibrate historical false positives. |
| LLM hallucinates technical claims | Deterministic pattern results, evidence IDs, grounding validation; LLM may explain but cannot create detections or scores. |
| Optional TA-Lib package is difficult on Windows/deploy | Pure-Python detectors are the supported baseline; adopt the optional package only after install/build checks. |
| Chart detector produces unsupported/ambiguous claims | Strict rule gates, abstention, evidence-backed outputs, near-miss tests, and no LLM-generated detections. |
| Overall score is misunderstood as probability | Label it as an internal directional/setup score, not a probability or expected return; retain educational disclaimer. |
| New score unexpectedly changes alerts | Keep verdicts/Telegram rules unchanged in shadow phase; require separate approval and calibrated thresholds before behavior changes. |

---

## 9. Acceptance criteria
### Watchlists
- You can create multiple named watchlists and see them in a sidebar.
- You can add a stock from existing search to the selected list, view list membership, and remove a stock.
- You can choose a watchlist at **Run analysis** and the run analyzes only its stocks.
- The existing search/autocomplete and **Analyze selected** behavior are unchanged.
- Existing `universe.json` installs seed Supabase once, and user watchlists survive Render app redeploys as long as the Supabase project/database remains available and is not deleted or reset.
- Empty, deleted, oversized, or invalid-symbol lists fail safely and visibly.

### Chart agent and scores
- The new chart stage uses the same valid OHLCV candles already fetched for technicals and makes no duplicate candle request.
- Every emitted pattern has deterministic criteria and inspectable evidence; unsupported patterns are never invented.
- Forming vs confirmed status is visible; insufficient/bad candle data becomes an explicit gap.
- Bull/Bear receive chart findings and score reasons as grounded evidence.
- Expanded stock details show news/trend score, pattern score, overall score, score reasons, and identified chart patterns; compact row shows only overall among the new three.
- In MVP/shadow rollout, existing verdict and alert behavior is unchanged.
- All tests pass, including existing search/security/focused-run tests, and the real FYERS candle behavior is clearly marked as verified or still unverified.

---

## 10. Research sources
- [TA-Lib official site — indicators, candlestick pattern recognition, BSD license](https://ta-lib.org/)
- [TA-Lib Python wrapper](https://github.com/TA-Lib/ta-lib-python)
- [Supabase database overview](https://supabase.com/docs/guides/database/overview)
- [Supabase database backups](https://supabase.com/docs/guides/platform/backups)
- [FYERS API product page](https://fyers.in/products/api/)
- [FYERS Python SDK package page and historical-data example](https://pypi.org/project/fyers-apiv3/)
- [Project's FYERS client and candle adapter](fyers_client.py)
- [Project pipeline and current agent sequencing](pipeline.py)
- [Project score implementation](scoring.py)
- [Project API/security and current focused analysis](app.py)
- [Project dashboard UI](dashboard.html)
- [Project watchlist store](watchlists.py) (contains the retired universe list, imported once)

**Storage and deployment caveat:** configure the Supabase project and `DATABASE_URL` as a server-side Render environment variable; this plan does not itself wire the current app to Postgres. Confirm the chosen Supabase plan’s backup/recovery and inactivity behavior, and keep periodic exports for important watchlists. No external chart-analysis API is used; chart findings are computed locally from FYERS candles.

---

## 11. Recommended build order
1. Provision Supabase Postgres; add the PostgreSQL driver, server-side `DATABASE_URL` config, migrations, and connectivity/error tests.
2. Implement watchlist CRUD and one-time `universe.json` seed in Supabase; verify data survives repeated Render deploys.
3. Add sidebar, add/remove membership controls, and selected-watchlist analysis while preserving search and focused analysis.
4. Build the local conservative chart-analysis support module and grounded evidence path; no external pattern API.
5. Add three scores in shadow mode and show transparent components/reasons; keep Judge balanced and current verdict/alert behavior unchanged.
6. Run historical validation/backtests and deployment tests; revisit verdict influence only after evidence supports it.
