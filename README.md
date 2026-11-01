# Signal Desk — multi-agent Indian stock analysis dashboard

Eight agents (Scout, Technician, Fundamentalist, Newsdesk, Bull, Bear, Judge, Messenger) screen NSE stocks, debate each one,
and call **BUY / WATCH / AVOID**. BUY signals can be sent to Telegram. **Analysis only: no order or trading endpoint exists in this code.**

**Search & select:** search any listed NSE stock, ETF or recently listed IPO by name or symbol, pick it from the autocomplete,
and press **Analyze selected** to run the same agents on that one instrument.

**Watchlists:** every run analyses one saved watchlist. Lists live in Supabase Postgres (not on the server's disk), so they
survive redeploys. Add stocks from the search box with **+ Watchlist**, remove them with `×`, and pick the list to run from
the **Run watchlist** selector. On first start against an empty database, the old built-in 24-stock list is imported once as
**Watchlist 1**; `universe.json` no longer exists in the codebase.

## 1. Try it now (Demo mode — no keys, no ngrok, no internet)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                                          # http://127.0.0.1:5000
```
Open http://127.0.0.1:5000, keep **Demo** selected, press **Run analysis**.
Demo uses **fictional companies with synthetic numbers** (`demo_data/`, regenerate with `python tools/make_demo_data.py`).
It makes zero network calls (a test enforces this) and never messages Telegram unless you set `ALLOW_DEMO_TELEGRAM=true`.

## 2. Setup checklist for live data

1. `cp .env.example .env` and fill in `FYERS_APP_ID`, `FYERS_APP_SECRET`, `FYERS_REDIRECT_URI` (step 4) and
   `SUPABASE_DATABASE_URL` (the Postgres *connection string*, see §7b). Optional: `GROQ_API_KEY`, Telegram vars.
2. `python app.py` (binds **127.0.0.1:5000**, debug off).
3. Install and authenticate ngrok, then in a second terminal: `ngrok http 5000`.
4. Copy the HTTPS forwarding domain. Set `FYERS_REDIRECT_URI=https://<your-domain>/auth/fyers/callback` in `.env` **and** register the identical URL in the FYERS API dashboard. Restart `python app.py`.
5. Open **`https://<your-domain>/`** (not 127.0.0.1). Unlock with the access key (`DASHBOARD_ACCESS_KEY`, or the auto-generated one in `.local/dashboard_access_key`).
6. Click **Connect FYERS**, log in at FYERS, get redirected back. The status pill turns green.
7. Choose **Live**, press **Run analysis**.

### ngrok install / auth
Install from https://ngrok.com/download (or `brew install ngrok`, `choco install ngrok`), create a free account, then run
`ngrok config add-authtoken <YOUR_TOKEN>` once. Then `ngrok http 5000`. The exact callback URL must match **character for character**
in FYERS settings and `.env` (scheme, host, path, no trailing slash differences).

### Provider-side restrictions — what I verified and what I could not
* FYERS docs (fyers.in/api-docs/v3) describe the flow and the redirect URL field but **do not state which domains are accepted**. I could not confirm that `*.ngrok-free.app` / `*.ngrok.app` domains pass FYERS validation; **try it and see**.
* A FYERS community thread reported that the app-creation form rejected localhost-style URLs lacking a subdomain; that is the reason this project is built around an HTTPS tunnel. That report may be outdated.
* Free ngrok domains change on every restart unless you claim a static domain; each change needs a new FYERS registration and `.env` edit. ngrok's free tier may also show a browser interstitial ("Visit Site") the first time.
* Create a **non-trading** (data-only) FYERS app: no order permission, no static-IP requirement.
* Access tokens expire daily around **06:30 IST**; reconnect each day. (The refresh token is deliberately not stored.)
* FYERS Standard-plan data limits (docs): 5/s, 50/min, 5000/day; repeated per-minute breaches can block the day. The client caps itself at 4/s, 40/min, 4500/day (`FYERS_PER_MINUTE_CAP`, `FYERS_DAILY_CAP`).

## 3. How authentication is secured
* Flow follows the official docs and the `fyers-apiv3` 3.2.0 SDK source: `generate-authcode` → browser login → `validate-authcode` with `appIdHash = sha256(app_id:app_secret)`, exchanged **server-side**. The callback reads `auth_code` (FYERS' own `code=200` is a status, not the auth code).
* OAuth `state`: 256-bit random, session-bound, **single-use, 10-minute expiry**, constant-time compare, checked *before* any exchange.
* Login must start on the redirect host so the session cookie and state match; starting on 127.0.0.1 shows a pointer instead.
* Token stored in `.local/fyers_token.json` (gitignored, mode 0600). Never sent to the browser, logged, or written to SQLite. Secrets are read only from env.
* **Tunnel protection:** requests via the tunnel host (or carrying `X-Forwarded-*` headers) must unlock with the access key (rate-limited, constant-time compare). Only `/healthz`, `/unlock` and the OAuth callback are public; the callback is useless without a matching state.
* Host-header allow-list (loopback + tunnel host). Forwarded headers are **not** trusted for host/scheme (no ProxyFix); the `Secure` cookie flag is derived from the configured redirect URI.
* CSRF token + Origin check on POSTs; CSP with nonce, `frame-ancestors 'none'`, no-store caching; debug off; werkzeug access logs silenced and all log lines redacted (`code`, `auth_code`, `state`, tokens, `gsk_…`, Telegram bot tokens).
* Residual risk: the callback URL with the one-time code sits briefly in your browser history/ngrok's inspector (`localhost:4040`). The code is single-use and short-lived.

## 4. Pipeline and engines
* **Scout** (Python): one FYERS quotes call for the selected watchlist, keeps the top-N movers per bucket
  (`SHORTLIST_PER_BUCKET`, default 4). Buckets are Large/Mid/Small where a stock has one (imported from the old list); a
  stock added from search has no bucket and forms its own group, so custom lists still shortlist. Stocks FYERS returns no
  quote for are named in a warning and skipped — the list is never substituted with something else.
* **Technician** (FYERS candles): RVOL, 10-day SMA position/trend, ~1-month return, swing levels, 52-week range (from ≤366 daily candles). During open hours RVOL is **time-matched intraday** (5-min bars); if that is impossible it falls back to full-day volume and is **labelled** `…UNADJUSTED` / `full_day_volume…`.
* **Fundamentalist** (yfinance): analyst consensus, buy/hold/sell mix, mean target and upside. **Only** analyst data: no P/E, ROE or margins, and coverage of small caps is often missing (shown as data gaps). It reads `.info` first and, when Yahoo returns nothing for the server, falls back to the analyst report endpoints. **Yahoo blocks datacentre IPs** (Render included) on `.info`, so on a cloud host set `YFINANCE_PROXY` (or `PROXY_URL`) to a residential proxy; unset, nothing changes.
* **Newsdesk**: Yahoo/yfinance headlines, then **Google News RSS** (no key, unthrottled) when those are thin, then GDELT DOC (last resort: it 429s aggressively). Deduped, company-name filtered, prompt-injection-flagged items withheld from the model. **Headline-only**; tone is a transparent keyword heuristic.
* **Bull / Bear / Judge**: if `GROQ_API_KEY` is set, **one** Groq call per stock (`openai/gpt-oss-120b`, JSON-schema structured output) *role-plays* the panel; it is **not** independent models. Output is validated in Python (schema, evidence IDs, every number traceable to a fact or cited headline, BUY needs ≥2 evidence IDs and momentum/range confirmation, BUY downgraded if the deterministic engine says AVOID). Failure of any kind (bad key, 429, timeout, malformed or ungrounded output) falls back to the deterministic rule engine (`scoring.py`: Bull/Bear 0–100 points; BUY = net ≥ 25 with leadership; AVOID = net ≤ −15; else WATCH). Retries are bounded; a hard rate limit or bad key stops further calls for the run.
* **Messenger / alert rules (Python, never the model):** BUY, confidence ≥ `CONFIDENCE_THRESHOLD`, quote fresh, market open (scheduled NSE hours; **exchange holidays are not modelled**), validation passed, not synthetic, not already alerted for that symbol today. Closed-market runs display results but send nothing.
* **Quote freshness:** FYERS' `tt` field is a fixed daily epoch (its own sample shows 00:00 UTC for a 20:07 IST quote), so it is **not** a quote time. It is therefore never used to judge staleness — otherwise every quote read ~5.5 h old, raising a false "check your PC clock" warning and blocking every alert. With no trustworthy provider timestamp, freshness is reported as `unknown`, and the alert rule above refuses to auto-send on an unverified quote. `ALERT_REQUIRE_OPEN_MARKET=false` does **not** override this: the rule is deliberately independent of it.
* "Confidence x/10" is an **internal conviction score, not a probability of profit.** The Telegram text follows the specified template exactly, so that label lives in the UI and daily summary.
* Audit trail in `data/audit.sqlite3` (runs, verdicts with evidence bundles, alert delivery status), scrubbed of secrets.
  A watchlist run is stored with `scope='watchlist'` plus the list id/name; a focused run with `scope='focused'`.

## 5. Tests (`python -m pytest`) — what they do and do not prove
132 tests, all with mocked HTTP/PostgreSQL: OAuth URL/state/exchange/denial/failure/expiry, token-file permissions, tunnel lock/unlock/CSRF/host checks, secret redaction, FYERS client parsing/errors/rate limiter, Groq retry/circuit/validation/grounding/downgrade, Telegram template/escaping/dedupe/token non-leak, offline demo, end-to-end watchlist run with fake providers, symbol-master parsing/ranking/cache TTL/stale fallback, search API bounds and hostile input, spoofed-instrument rejection, focused runs (one target, scope tagging, no Telegram, data-gap degradation), watchlists (migrations, one-time import guard, CRUD, duplicates, caps, last-list protection, empty list blocks a run, database down is an error not a fallback, CSRF on every write), and a source scan proving no order endpoints.
**Verified against the live internet on 2026-10-29:** the FYERS symbol master (2,724 cash instruments, strike column now `-1.0`), yfinance analyst data for NSE tickers, Google News RSS headlines, and the LLM grounding path with a fake Groq reply. **Still not tested: any live call to FYERS quotes/candles, the real Groq API, Telegram, or a GDELT success** (GDELT was returning 429 page-wide during that check). The search UI was exercised in a headless Chrome against a local symbol-master file and fake providers; the real master download, its current column layout and the `-EQ` quote/candle calls for a searched symbol remain **unverified** until you run them. Treat live integration as **unverified** until you run it. In particular unverified: FYERS acceptance of your tunnel redirect URL, FYERS symbol names in the imported list (`NSE:<SYM>-EQ`; check the symbol master), the live Supabase connection and the one-time import against your real project (all tests use an in-memory fake of the driver), Groq strict-schema acceptance for your key/model (the client degrades to `json_object` on a 400 and still validates), and current yfinance field names (all reads are null-safe).

## 5b. Search & select (focused analysis)
* **Where the list comes from:** the FYERS public NSE cash-market symbol master (`INSTRUMENT_CATALOGUE_URL`, default
  `https://public.fyers.in/sym_details/NSE_CM.csv`). It is downloaded once per `INSTRUMENT_CATALOGUE_TTL` (default 6 h) in a
  background thread and parsed in memory; **every keystroke is answered locally** — a search never calls FYERS. If a refresh
  fails, the last valid copy keeps serving and results are labelled as cached.
* The master is headerless; the parser reads name (col 1), ISIN (5), provider symbol (9), short symbol (13) and strike (15),
  keeps cash instruments only (strike marker `-1` **or** `-1.0` — FYERS changed it to the float form, which silently
  emptied the catalogue until it was accepted — and an `NSE:…-EQ` symbol) and **drops malformed rows instead of guessing**.
  This column layout comes from FYERS community posts, not a versioned spec — verify it against the live file after a schema change.
* Only `NSE:<SYMBOL>-EQ` cash instruments are searchable, and an IPO becomes searchable once it appears in a refreshed master.
  Upcoming/unlisted IPOs have no quote or price history and are out of scope. The master carries no reliable ETF flag, so
  every hit is labelled `EQUITY`; analyst data for ETFs is usually reported as a gap.
* Selection is server-validated: the browser sends only `provider_symbol`, and name/exchange/type are re-read from the cached
  master. Unknown, malformed or over-long symbols are rejected; labels are rendered as text, never HTML.
* A focused run analyses **only** that instrument (Scout retrieves one quote instead of screening the universe) and reuses the
  same Technician / Fundamentalist / Newsdesk / Bull / Bear / Judge code and deterministic fallback. To avoid unexpected
  messages from a one-off lookup, **focused runs never send Telegram**; the verdict is still written to the audit trail and
  the run row is tagged `focused` with its target.
* yfinance tickers are derived only from a verified mapping (`NSE:X-EQ` → `X.NS`); anything else is reported as
  `analyst.not_mapped` rather than guessed.
* Settings: `INSTRUMENT_CATALOGUE_URL`, `INSTRUMENT_CATALOGUE_TTL`. Search is bounded to 2–40 characters, 8 results, and a
  per-session throttle; responses go through the same access-key, host and CSRF checks as the rest of the JSON API.

## 6. Troubleshooting
| Symptom | Cause / fix |
|---|---|
| "Invalid or expired state" | Login not started on the tunnel host, cookies blocked, or >10 min elapsed. Start again from `https://<domain>/`. |
| FYERS rejects redirect URL | URL differs from the registered one, or the domain is refused by FYERS. |
| Status "Expired" | Daily token expiry. Click Reconnect. |
| Focused run: "That instrument is not in the current NSE symbol list" | The cached master is empty/stale after a failed refresh. Restart the app or wait for the next refresh. |
| Live run: "rate limit" | Wait for the minute window; lower `SHORTLIST_PER_BUCKET`. |
| "FYERS rate limit hit (HTTP 429)" right after Connect FYERS on Render | Several fallback instances / an old instance may hold a *different* token for the same FYERS app, and only one session is live at a time. Set `WEB_CONCURRENCY=1`, enable **Pre-Deploy / single instance**, then **deploy first and Connect FYERS last**. |
| Status flips to "Expired" / "Unavailable" after clicking Analyze selected | The stored token is no longer accepted by FYERS (a newer login or an app change invalidated it) or FYERS is rate-limiting. The run checks `/profile` before any data call and reports which one; click Connect FYERS and retry. |
| A 429 is shown as "non-JSON response" | Fixed: the HTTP status is now checked before the body is parsed. |
| "quotes are older than 300s ... check your PC clock" | Fixed: FYERS `tt` is not a quote timestamp — see *Quote freshness* in section 4. |
| Fundamentalist shows **Covered 0/1 with no gaps**, on a stock that clearly has analysts (TCS, ITC) | Yahoo's `.info` endpoint was empty for this server. The app now falls back to the analyst report endpoints and, if those are empty too, says *"Yahoo returned no data to this server"* instead of implying the stock has no coverage. Persistent on a cloud host → set `YFINANCE_PROXY` (see below) or use a provider with a keyed API. |
| Fundamentalist: "No analyst coverage" | Genuinely none on Yahoo (common for small caps such as ITI). Correct behaviour, stated as a data gap. |
| Many `analyst.*` / `news.*` gaps | yfinance/Google News/GDELT had no coverage or were throttled. Shown honestly, not filled. |
| LLM fallback `grounding_failed` naming a number like `10` | The model wrote its own bull/bear score as a number, which is not evidence. The prompt now forbids that; any other untraceable number is still rejected after one corrective retry. |
| Bull/Bear/Judge always fall back | Check the warning: `no_api_key`, `auth_error`, `rate_limited`, `invalid_output` or `grounding_failed`. Each names the exact gate that failed. |
| Watchlist bar says "Watchlist storage is not configured" | `SUPABASE_DATABASE_URL` is empty or is the project URL (`https://…`). Use the Postgres connection string (Supabase → Project settings → Database). A password with `?`, `#`, `/`, `@` or `:` in it is escaped automatically, so it can be pasted as-is. |
| Watchlist bar says "database is unreachable" / `could not translate host name` | The direct host `db.<project>.supabase.co` publishes **IPv6 only**, so a network without IPv6 cannot reach it. Use the **Session pooler** string instead: `postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require` (IPv4). Also check the password, that the project is not paused, and the server log. |
| "has no stocks. Add some first" | Runs analyse one list only. Add stocks with **+ Watchlist** or pick another list. |
| "Keep at least one watchlist" | The last list cannot be deleted; create another first. |
| Search says "Symbol list is temporarily unavailable" | The symbol master could not be downloaded and nothing was cached. Check the server log and network access to `public.fyers.in`. |
| Focused run: "No verified yfinance listing" | The instrument is not a plain `-EQ` equity (e.g. ETF series). Analyst fields are shown as gaps. |

## 6b. Watchlists (Supabase Postgres)
* **Storage:** `watchlists.py` talks to PostgreSQL directly with a server-side connection string, read from
  `DATABASE_URL`, `SUPABASE_DATABASE_URL` or `DATABASE_CONNECTION_STRING` (first one that is a `postgresql://` URL). The URL
  is treated as a secret — redacted from logs and never sent to the browser. The Supabase anon/service_role keys are **not** used.
* **Triggers:** the store bumps `watchlists.updated_at` itself. Triggers left by a draft of this feature (`trg_watchlists_touch`,
  `trg_watchlist_items_touch`, `trg_touch_watchlist`) are dropped by the migrations: one read a nonexistent column and the
  other recursed on its own UPDATE.
* **Schema:** created/updated on first use by idempotent forward-only migrations (`watchlist_migrations`, `watchlists`,
  `watchlist_items` with a unique `(watchlist, provider_symbol)` constraint and a foreign key cascade). Safe on every deploy.
* **One-time import:** if the database has no watchlist at all, the old built-in 24-stock list is inserted as
  **Watchlist 1** inside one transaction guarded by an advisory lock and a migration marker. Existing user lists are never
  touched and the import never re-runs — that is why `universe.json` could be deleted from the codebase.
* **Runs** analyse exactly one list. `POST /api/start` takes a `watchlist_id`; the server loads its stocks itself and
  refuses an empty list (409 `empty`) — it never falls back to another list. If the database is down, the run fails with a
  visible error instead of an in-memory substitute.
* **API** (all CSRF-protected, same access-key/host checks as the rest of the JSON API):
  `GET /api/watchlists` (sidebar + `default_id`), `GET/POST /api/watchlists`, `PATCH/DELETE /api/watchlists/<id>`,
  `POST /api/watchlists/<id>/items` and `DELETE /api/watchlists/<id>/items/<provider-symbol>`. Add-stock accepts only a
  `provider_symbol` and re-reads name/exchange/type from the cached FYERS master, so the browser cannot inject instruments.
  Limits: `WATCHLIST_MAX_ITEMS` (default 100) stocks per list, 50 lists, the last list cannot be deleted.
* **Not user-isolated:** there is one dashboard and no account model, so every list is shared by whoever can open the app.
* `/api/analyze-selected` is unchanged: a one-off analysis of a searched instrument that is never added unless you press
  **+ Watchlist**.

## 7. Layout
`app.py` routes+security · `fyers_auth.py` OAuth/token store · `fyers_client.py` read-only data client · `instrument_search.py` NSE symbol-master catalogue/search · `watchlists.py` Postgres watchlist store (schema, one-time import, CRUD) · `data_sources.py` symbol mapping/yfinance/demo loader · `evidence.py` indicators+bundle · `news.py` · `grounding.py` evidence IDs/number checks · `scoring.py` deterministic engine · `llm.py` Groq panel+alert rules · `pipeline.py` orchestration · `notifier.py` Telegram · `storage.py` SQLite audit · `dashboard.html` UI.

*Not investment advice. Educational software; no warranty.*
