"""Forward-outcome engine, regimes, sizing, guards and the prediction ship gate. Pure math + fake providers:
no network, no wall clock."""
from datetime import date, datetime, timedelta

import pytest

import events
import etf
import evidence as ev
import ipo
import outcomes as oc
import playbook
import predict
import regime
import sizing
from evidence import IST
from storage import Store

# ---------- helpers ----------
def candles(start=date(2026, 1, 5), n=80, step=1.0):
    """Weekday-only daily bars: close rises by `step` per session, open == previous close."""
    out, d, px = [], start, 100.0
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"t": int(datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp()),
                        "o": px, "h": px + 2, "l": px - 1, "c": px + step, "v": 1000})
            px += step
        d += timedelta(days=1)
    return out


class FakeProvider:
    def __init__(self, bars):
        self.bars, self.calls = bars, []

    def daily_candles(self, sym, days=420):
        self.calls.append(sym)
        return self.bars.get(sym, [])


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "audit.sqlite3", [])


def add(store, symbol, bundle, verdict="BUY", confidence=8, engine="deterministic", run_id=None):
    run_id = run_id or store.start_run("live")
    store.add_verdict(run_id, bundle, {"engine": engine, "verdict": {"verdict": verdict, "confidence": confidence},
                                       "scores3": None}, {"eligible": False, "blocked_reasons": []})
    return run_id


def bundle_at(symbol, when, live=100.0, **extra):
    return {"symbol": symbol, "asset_type": "EQUITY", "provider_symbol": f"NSE:{symbol}-EQ",
            "retrieved_at": when.astimezone(IST).isoformat(), "price": {"live": live, **extra}}


# ---------- settlement math ----------
def test_settle_window_is_none_until_the_horizon_is_complete():
    bars = candles(n=10)
    assert oc.settle_window(bars, ev._day(bars[0]), 20) is None
    assert oc.settle_window(bars, ev._day(bars[0]), 3) is not None


def test_settle_window_entry_is_the_next_session_open_not_the_decision_price():
    """The old code used the decision-time quote as entry; the trade starts at the next completed open."""
    bars = candles(n=10)
    row = oc.settle_window(bars, ev._day(bars[0]), 1)
    assert row["entry_price"] == bars[1]["o"] and row["next_open"] == bars[1]["o"]
    assert row["close_px"] == bars[1]["c"] and row["ret_pct"] == oc.pct(bars[1]["c"], bars[1]["o"])
    assert row["hit"] == int(row["ret_pct"] > 0)


def test_mfe_mae_bracket_the_final_return():
    bars = candles(n=10)
    row = oc.settle_window(bars, ev._day(bars[0]), 5)
    assert row["mfe_pct"] >= row["ret_pct"] >= row["mae_pct"]


def test_gap_pct_is_measured_from_the_prior_close():
    bars = candles(n=6)
    row = oc.settle_window(bars, ev._day(bars[0]), 1)
    assert row["gap_pct"] == oc.pct(bars[1]["o"], bars[0]["c"])


# ---------- settle job ----------
def test_settle_is_idempotent_and_one_request_per_symbol(store):
    bars = candles(n=80, step=1.0)
    add(store, "AAA", bundle_at("AAA", datetime(2026, 1, 6, 11, 0, tzinfo=IST)))
    add(store, "AAA", bundle_at("AAA", datetime(2026, 1, 7, 11, 0, tzinfo=IST)))
    p = FakeProvider({"NSE:AAA-EQ": bars})
    first = oc.settle_pending(store, p, horizons=(1, 5), benchmark="NIFTYBEES")
    assert first["settled"] == 2 and p.calls == ["NSE:AAA-EQ"]  # two verdicts, one candle request
    p.calls.clear()
    second = oc.settle_pending(store, p, horizons=(1, 5), benchmark="NIFTYBEES")
    assert second["settled"] == 0 and p.calls == []  # fully settled rows cost nothing


def test_settle_skips_a_non_trading_decision_day(store):
    bars = candles(n=40)
    saturday = datetime(2026, 1, 10, 11, 0, tzinfo=IST)
    assert saturday.weekday() == 5
    add(store, "AAA", bundle_at("AAA", saturday))
    stats = oc.settle_pending(store, FakeProvider({"NSE:AAA-EQ": bars}), horizons=(1,), benchmark="NIFTYBEES")
    assert stats["settled"] == 0 and stats["skipped"] == 1
    assert store.outcome_get(1, 1) is None


def test_settle_skips_a_declared_holiday(store):
    bars = candles(n=40)
    when = datetime(2026, 1, 6, 11, 0, tzinfo=IST)
    add(store, "AAA", bundle_at("AAA", when))
    stats = oc.settle_pending(store, FakeProvider({"NSE:AAA-EQ": bars}), horizons=(1,), holidays={when.date()})
    assert stats["settled"] == 0


def test_settle_reports_a_provider_error_without_losing_the_rest(store):
    bars = candles(n=40)
    add(store, "AAA", bundle_at("AAA", datetime(2026, 1, 6, 11, 0, tzinfo=IST)))
    run = store.start_run("live")
    add(store, "BBB", bundle_at("BBB", datetime(2026, 1, 6, 11, 0, tzinfo=IST)), run_id=run)

    class Half(FakeProvider):
        def daily_candles(self, sym, days=420):
            if sym == "NSE:AAA-EQ":
                raise RuntimeError("provider down")
            return super().daily_candles(sym, days)

    stats = oc.settle_pending(store, Half({"NSE:BBB-EQ": bars}), horizons=(1,))
    assert len(stats["errors"]) == 1 and stats["settled"] == 1


def test_benchmark_is_settled_for_excess_returns(store):
    bars = candles(n=40)
    b = {**bundle_at("NIFTYBEES", datetime(2026, 1, 6, 11, 0, tzinfo=IST)), "benchmark": True, "is_benchmark": True}
    store.add_verdict(store.start_run("live"), b, {"engine": "benchmark", "verdict": {"verdict": "BENCHMARK", "confidence": 0},
                                                   "scores3": None}, {"eligible": False, "blocked_reasons": []})
    oc.settle_pending(store, FakeProvider({"NSE:NIFTYBEES-EQ": bars}), horizons=(1,), benchmark="NIFTYBEES")
    rows = store.outcomes_joined()
    assert rows and rows[0]["is_benchmark"] == 1


# ---------- accuracy report ----------
def test_accuracy_report_shapes_and_disclaimer(store):
    bars = candles(n=60)
    add(store, "AAA", bundle_at("AAA", datetime(2026, 1, 6, 11, 0, tzinfo=IST)), confidence=9)
    oc.settle_pending(store, FakeProvider({"NSE:AAA-EQ": bars}), horizons=(1, 5))
    rep = oc.accuracy_report(store, (1, 5), "NIFTYBEES")
    assert rep["disclaimer"] == oc.DISCLAIMER and rep["survivorship"] == oc.SURVIVORSHIP
    assert rep["n_settled"] == 2 and set(rep["overall"]) == {1, 5}
    assert {s["horizon"] for s in rep["slices"]} == {1, 5}
    assert all(s["confidence_bucket"] == "9-10" for s in rep["slices"])
    assert rep["benchmark"] == {} or isinstance(rep["benchmark"], dict)
    assert rep["excess_vs_benchmark"][1]["excess_median"] is None  # no benchmark rows yet: a gap, not a zero


def test_session_slicing_separates_close_entries(store):
    run_id = store.start_run("watchlist")
    store.db.execute("UPDATE runs SET started_at=? WHERE id=?", ("2026-01-06T04:30:00+00:00", run_id))  # 10:00 IST
    oc_row = {"entry_price": 100.0, "next_open": 100.0, "close_px": 101.0, "gap_pct": 0.0, "mfe_pct": 1.0,
              "mae_pct": -0.5, "ret_pct": 1.0, "hit": 1}
    add(store, "AAA", bundle_at("AAA", datetime(2026, 1, 6, 11, 0, tzinfo=IST)), run_id=run_id)
    store.outcome_put(1, 1, oc_row)
    assert oc._session(store.outcomes_joined()[0]) == "open"


def test_rolling_and_summarize_handle_null_cells():
    rows = [{"ret_pct": None, "hit": None, "mae_pct": None}, {"ret_pct": 2.0, "hit": 1, "mae_pct": -1.0}]
    s = oc.summarize(rows)
    assert s["n"] == 2 and s["hit_rate"] == 1.0 and s["mean_mae"] == -1.0


# ---------- regime / sizing ----------
def test_regime_does_not_double_count_the_last_close():
    bars = [{"t": int((datetime(2026, 6, 1) + timedelta(days=i)).timestamp()), "c": 100 + i} for i in range(210)]
    from evidence import _day
    live = bars[-1]["c"]
    a = regime.from_candles(bars, live)  # last candle is NOT today: live appends (upper bound of the series)
    assert a["ret_20d"] is not None


def test_regime_labels_risk_off_and_unknown():
    down = [{"t": int((datetime(2025, 1, 1) + timedelta(days=i)).timestamp()), "c": 300 - i * 0.5} for i in range(260)]
    assert regime.from_candles(down)["label"] == "risk_off" and regime.caps_buy(regime.from_candles(down))
    assert regime.from_candles([])["label"] == "unknown" and not regime.caps_buy(regime.from_candles([]))


def test_sizing_never_returns_a_position_that_breaks_the_budget():
    g = sizing.guidance(100.0, 95.0, 1000)
    assert g["shares"] == 200 and g["shares"] * g["risk_per_share"] <= 1000
    assert sizing.shares(100.0, 105.0, 1000) is None          # stop above entry: no trade
    assert sizing.guidance(100.0, 99.9, 10)["shares"] == 100  # float floor: 10 / 0.1, not 99
    assert sizing.shares(100.0, 98.0, 1000) == 500


# ---------- guards ----------
def test_event_guard_blocks_only_inside_the_window():
    today = datetime(2026, 11, 3, 10, 0, tzinfo=IST)
    items = [{"kind": "results", "date": "2026-11-04", "title": "Q2"},
             {"kind": "results", "date": "2026-12-30", "title": "Q3"}]
    assert events.blocks_buy(items, 1, today)[0] is True
    assert events.blocks_buy(items[1:], 1, today)[0] is False
    assert events.blocks_buy([], 1, today) == (False, None)


def test_ipo_sme_without_a_complete_checklist_is_forced_to_watch():
    b = {"name": "ACME SME", "symbol": "ACME", "technicals": {"window_return_pct": 30}}
    p = ipo.profile(b, candles(n=5))
    assert p["sme"] and not p["checklist_complete"] and p["gmp_used"] is False
    assert ipo.force_watch({**b, "ipo": p}) is True


# ---------- playbook ----------
def test_playbook_marks_ineligible_rows_with_reasons():
    class S:
        event_guard_days, risk_budget_inr = 1, 10_000

    b = {"symbol": "AAA", "price": {"live": 100.0, "volume": 10}, "technicals": {"atr14_pct": 9},
         "events": {"items": []}, "regime": {"label": "risk_off"}}
    r = playbook.row(b, {"verdict": {"verdict": "BUY", "confidence": 8}}, S)
    assert r["eligible"] is False and {"thin liquidity", "wide overnight-gap / ATR", "risk-off regime"} <= set(r["reasons"])
    assert r["settle"] == ["next_open", "next_close"] and r["disclaimer"] == oc.DISCLAIMER


def test_playbook_window_is_15_00_to_15_20_on_a_weekday():
    assert playbook.in_window(datetime(2026, 11, 3, 15, 10, tzinfo=IST))
    assert not playbook.in_window(datetime(2026, 11, 3, 15, 21, tzinfo=IST))
    assert not playbook.in_window(datetime(2026, 11, 7, 15, 10, tzinfo=IST))  # Saturday


# ---------- prediction ship gate ----------
def test_purged_folds_leave_horizon_plus_one_bars_unseen():
    folds = list(predict.purged_splits(1000, 5))
    assert folds and all(predict.gap_ok(tr, te, 5) for tr, te in folds)
    assert all(min(te) - max(tr) > 5 for tr, te in folds)  # first test entry is ≥ horizon+1 past the last train bar
    assert not predict.gap_ok([0, 10], [15], 5)  # a 5-bar gap would leak the boundary label


def test_brier_and_hit_rate_math():
    assert predict.brier([1.0, 0.0], [1, 0]) == 0.0
    assert abs(predict.brier([0.5, 0.5], [1, 0]) - 0.25) < 1e-12
    assert predict.hit_of([0.9, 0.1], [1, 0]) == 1.0


def test_constant_predictor_cannot_ship_without_a_model(monkeypatch):
    monkeypatch.setattr(predict, "sklearn_ok", lambda: False)
    ys = [1 if i % 3 else 0 for i in range(900)]
    rows = [{"horizon": 5, "hit": y, "bundle": "{}"} for y in ys]
    m = predict.train_horizon(rows, 5)
    assert m["shipped"] is False and any("not_shipped" in n for n in m["notes"])


def test_attach_publishes_the_base_rate_unshipped():
    b = predict.attach({}, {5: {"base_rate": 0.62, "shipped": False, "n": 300}}, "2026-11-03")
    p = b["prediction"]["5"]
    assert p["p_up"] == 0.62 and p["calibrated"] is False and p["interval"] == [0.52, 0.72]


# ---------- Phase 2 gaps: news store, weighted tone ----------
def test_news_store_records_fingerprints_across_runs(store):
    """Without store= the stale-news flag can never fire: repetition is its whole signal."""
    import news
    block, _ = news.build_news({"symbol": "AAA", "name": "AAA Ltd"}, None, None, gnews=None,
                               store=store, now=datetime(2026, 11, 3, tzinfo=IST))
    assert block["stale_positive"] is False  # first sighting is never stale


def test_news_tone_uses_age_weights_when_present():
    import scoring
    assert scoring.news_tone({"positive": 3, "negative": 1, "weighted_positive": 0.2, "weighted_negative": 1.9}) == (0.2, 1.9)
    assert scoring.news_tone({"positive": 3, "negative": 1}) == (3, 1)  # raw counts as the fallback


def test_weighted_tone_flips_the_verdict_against_raw_counts():
    """Three stale positives (heavily discounted) must not out-score one fresh negative.
    With raw counts the bull case wins; with age weights it must not."""
    import scoring
    raw = {"positive": 3, "negative": 1, "neutral": 0, "total": 4}
    weighted = {**raw, "weighted_positive": 0.2, "weighted_negative": 1.9}
    bull = lambda n: "tone leans positive" in " ".join(r["text"] for r in scoring.score_bull({"news": n}, [])[1])
    bear = lambda n: "tone leans negative" in " ".join(r["text"] for r in scoring.score_bear({"news": n}, [])[1])
    assert bull(raw) and not bear(raw)
    assert not bull(weighted) and bear(weighted)


def test_stale_positive_news_after_a_runup_downgrades_a_buy():
    import scoring
    b = {"asset_type": "EQUITY", "news": {"stale_positive": True}, "technicals": {"window_return_pct": 12}}
    j = scoring.apply_guards({"verdict": "BUY", "confidence": 8}, b)
    assert j["verdict"] == "WATCH" and any("stale" in g for g in j["guards"])


# ---------- ETF: judged on the overlay, not equity breakout rules ----------
def test_etf_is_scored_by_its_overlay_not_breakout_rules():
    import scoring
    b = {"asset_type": "ETF", "etf": {"premium_pct": 0.3, "tracking_diff_20d": 0.5, "adv": 5_000_000,
                                      "expense_ratio": 0.2, "gaps": []},
         "technicals": {"rvol": 9, "price_vs_sma_pct": 20, "trend": "up"}}
    r = scoring.evaluate_deterministic(b, [])
    assert r["engine"] == "deterministic_etf" and r["verdict"]["verdict"] == "BUY"
    assert r["scores"]["bull"]["score"] is None  # equity breakout rules never ran


def test_etf_with_an_incomplete_overlay_caps_at_watch():
    import scoring
    b = {"asset_type": "ETF", "etf": {"premium_pct": None, "adv": None, "gaps": ["etf.premium_inav"]}}
    assert scoring.evaluate_deterministic(b, [])["verdict"]["verdict"] == "WATCH"


def test_etf_expensive_to_nav_is_avoided():
    import scoring
    b = {"asset_type": "ETF", "etf": {"premium_pct": 1.4, "adv": 5000, "tracking_diff_20d": 0.1, "gaps": []}}
    assert scoring.evaluate_deterministic(b, [])["verdict"]["verdict"] == "AVOID"


def test_llm_never_overrides_an_etf_with_equity_reasoning():
    import llm
    b = {"asset_type": "ETF", "etf": {"premium_pct": 0.2, "adv": 9_000_000, "tracking_diff_20d": 0.1, "gaps": []}}

    class Client:
        model = "x"

        def complete(self, *a, **k):
            raise AssertionError("the LLM must not be called for an ETF")

    r = llm.evaluate(b, type("S", (), {"confidence_threshold": 7})(), Client())
    assert r["engine"] == "deterministic_etf" and r["fallback_reason"] == "etf_overlay"


# ---------- IPO / regime gaps are explicit ----------
def test_ipo_unverifiable_checks_are_gaps_not_passes():
    p = ipo.profile({"name": "ACME", "symbol": "ACME", "technicals": {"window_return_pct": 10}},
                    [{"t": int(datetime(2026, 9, 1, tzinfo=IST).timestamp()), "c": 100}])
    assert p["checklist"]["promoter_selling_known"] is None  # yfinance has no NSE promoter stake
    assert "ipo.shareholding_feed" in p["gaps"] and not p["checklist_complete"]
    assert p["gmp_used"] is False


def test_ipo_checklist_can_be_completed_so_watch_is_not_permanent():
    """An SME must be able to leave WATCH: a checklist that can never complete is a bug, not caution."""
    bars = candles(n=60, step=0.5)
    info = {"issuePrice": 100.0, "heldPercentInsiders": 0.62, "priorHeldPercentInsiders": 0.60}
    b = {"name": "ACME LIMITED", "symbol": "ACME", "provider_symbol": "NSE:ACME-EQ",
         "price": {"live": 120.0}, "technicals": {"window_return_pct": 4}}
    p = ipo.profile(b, bars, now=datetime(2026, 11, 3, tzinfo=IST), info=info)
    assert p["checklist_complete"] is True and p["gaps"] == []
    assert ipo.force_watch({**b, "ipo": p}) is False


def test_ipo_split_like_reset_fails_the_bonus_check():
    bars = candles(n=30, step=1.0)
    peak = bars[-2]["c"]
    for bar in bars[-1:]:  # a 1:10 split-style reset, far past the stock's own daily noise
        bar["o"], bar["c"] = peak / 10.0, peak / 10.0
    b = {"name": "ACME", "symbol": "ACME", "technicals": {"window_return_pct": 5}}
    p = ipo.profile(b, bars, now=datetime(2026, 11, 3, tzinfo=IST), info={"issuePrice": 100.0})
    assert p["checklist"]["no_bonus_split_after_runup"] is False
    assert not p["checklist_complete"]


def test_ipo_unjudgeable_bonus_check_is_a_gap_not_a_pass():
    p = ipo.profile({"name": "ACME", "symbol": "ACME", "technicals": {}}, candles(n=5))
    assert p["checklist"]["no_bonus_split_after_runup"] is None
    assert "ipo.corporate_actions_feed" in p["gaps"]


# ---------- ETF: NAV provenance ----------
def test_etf_intraday_inav_is_used_and_not_flagged_as_eod():
    b = {"asset_type": "ETF", "price": {"live": 100.0}}
    e = etf.profile(b, None, {"intradayNav": 99.9, "annualReportExpenseRatio": 0.2, "averageVolume": 1e6})
    assert e["nav_source"] == "inav" and e["premium_is_eod"] is False
    assert e["premium_pct"] == 0.1 and "etf.premium_inav" not in e["gaps"]


def test_etf_eod_nav_is_labelled_and_cannot_approve_a_buy():
    b = {"asset_type": "ETF", "price": {"live": 100.0}}
    e = etf.profile(b, None, {"navPrice": 100.0, "averageVolume": 1e6})
    assert e["nav_source"] == "nav_eod" and e["premium_is_eod"] is True
    assert etf.allows_buy({**b, "etf": e}) is False


def test_etf_without_any_nav_stays_an_explicit_gap():
    e = etf.profile({"asset_type": "ETF", "price": {"live": 100.0}}, None, {})
    assert e["premium_pct"] is None and e["nav_source"] is None and "etf.premium_inav" in e["gaps"]
    # No NAV at all means no overlay judgement: the ETF path caps at WATCH (never a silent equity BUY).
    assert etf.allows_buy({"asset_type": "ETF", "etf": e}) is False


def test_regime_vix_gap_names_its_cause():
    r = regime.from_candles([])
    assert r["vix_gap"] and "licen" in r["vix_note"].lower()


# ---------- replay accelerator ----------
def test_replay_report_produces_settled_rows_per_horizon():
    rep = oc.replay_report(candles(n=140, step=1.0), "TCS", (1, 5))
    assert rep["verdicts"] > 0 and rep["bars"] == 140
    assert set(rep["overall"]) == {1, 5} and rep["overall"][1]["n"] > 0
    assert "survivorship" in rep and "technical features only" in rep["limitation"]
    assert rep["disclaimer"] == oc.DISCLAIMER


def test_replay_never_looks_past_the_as_of_bar():
    """A replay row's forward window must start after its own as-of date, never before."""
    rows = oc.replay_deterministic(candles(n=90, step=1.0))
    assert rows and all(r["asof"] and r["horizons"] for r in rows)
    for w in rows[0]["horizons"].values():
        assert w["entry_price"] > 0


# ---------- prediction engine ----------
def _learnable_rows(n=700, seed=7):
    import random
    random.seed(seed)
    out = []
    for _ in range(n):
        x = random.random()
        y = int((x + random.gauss(0, 0.15)) > 0.5)
        b = {"technicals": {"rvol": x * 2, "price_vs_sma_pct": x, "atr14_pct": x * 3},
             "range_52w": {"position_pct": x * 100}, "analyst": {"upside_pct": x * 20},
             "news": {"positive": int(x * 5), "negative": int((1 - x) * 5)}, "regime": {"ret_20d": x}}
        out.append({"horizon": 5, "hit": y, "bundle": __import__("json").dumps(b)})
    return out


@pytest.mark.skipif(not predict.sklearn_ok(), reason="sklearn is an optional dependency")
def test_learnable_signal_ships_and_publishes_its_own_probability():
    m = predict.train_horizon(_learnable_rows(), 5)
    assert m["shipped"] and m["brier"] < m["baseline_brier"] and m["model"]
    bundle = predict.attach({"technicals": {"rvol": 2, "price_vs_sma_pct": 1, "atr14_pct": 3},
                             "range_52w": {"position_pct": 100}, "analyst": {"upside_pct": 20},
                             "news": {"positive": 5, "negative": 0}, "regime": {"ret_20d": 1}}, {5: m}, "now")
    assert bundle["prediction"]["5"]["calibrated"] is True
    assert bundle["prediction"]["5"]["p_up"] != m["base_rate"]  # a real model output, not a relabelled constant


def test_pure_noise_never_ships():
    import json
    import random
    rows = []
    for _ in range(500):
        random.seed(len(rows))
        b = {"technicals": {"rvol": random.random() * 2}, "range_52w": {"position_pct": random.random() * 100}}
        rows.append({"horizon": 1, "hit": random.randint(0, 1), "bundle": json.dumps(b)})
    m = predict.train_horizon(rows, 1)
    assert m["shipped"] is False


def test_shipped_model_is_newest_per_horizon(store):
    store.log_experiment(5, "old", 900, 0.2, 0.6, 0.3, 0.5, True, "ok", {"base_rate": 0.5})
    store.log_experiment(5, "new", 900, 0.1, 0.7, 0.3, 0.5, True, "ok", {"base_rate": 0.55})
    got = [m for m in store.shipped_models() if m["horizon"] == 5]
    assert len(got) == 1 and got[0]["base_rate"] == 0.55


def test_drift_flags_a_feature_that_shifted_scale():
    train = [[1.0] * len(predict.FEATURES) for _ in range(50)]
    recent = [[100.0] * len(predict.FEATURES) for _ in range(50)]
    train[0][0], recent[0][0] = 0.9, 99.0  # give the first feature variance
    d = predict.drift(train, recent)
    assert d and all(v["drifted"] for v in d.values())


# ---------- Phase 3 activation: on-demand retrain endpoint ----------
def test_on_demand_retrain_ledgers_trials_without_publishing_when_disabled(mk):
    """MODEL_ENABLED=false must still write the experiment ledger (the roadmap's honest record) but publish
    nothing, so a manual retrain can never turn an experiment into a forecast by accident."""
    import re

    from tests.conftest import LOCAL

    app, _s = mk({"MODEL_ENABLED": "false"})
    store = app.extensions["store"]
    c = app.test_client()
    tok = re.search(r'name="csrf" content="([^"]+)"', c.get("/", base_url=LOCAL).get_data(as_text=True)).group(1)

    r = c.post("/api/model/retrain", json={}, headers={"X-CSRF-Token": tok}, base_url=LOCAL)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["model_enabled"] is False and body["published"] == []
    assert set(body["trained"]) == {"1", "5", "20", "60"}
    assert all(not m["shipped"] for m in body["trained"].values())
    assert "ledgered" in body["note"]
    # The ledger was written even though nothing shipped, and no model may be picked up by the pipeline.
    rows = store.db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
    assert rows >= 1 and store.shipped_models() == []


def test_on_demand_retrain_requires_csrf(mk):
    from tests.conftest import LOCAL

    app, _s = mk({"MODEL_ENABLED": "false"})
    assert app.test_client().post("/api/model/retrain", json={}, base_url=LOCAL).status_code == 403
