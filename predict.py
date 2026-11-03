"""Calibrated P(close > entry) per horizon. sklearn is optional; no model may auto-BUY.

Discipline (non-negotiable, each enforced in code):
  * purged walk-forward folds — a training label may never look into the test block;
  * calibration fitted on a slice disjoint from the training slice;
  * a ship gate on out-of-sample Brier vs the deterministic base rate, on identical folds;
  * every trial written to the experiment ledger, shipped or not;
  * feature-drift check on the final fit's training window.
A model that fails the gate stays a dashboard experiment and only its base rate is published.
"""
import json
import pickle

from grounding import _get
from outcomes import HORIZONS

FEATURES = ("technicals.rvol", "range_52w.position_pct", "technicals.price_vs_sma_pct",
            "technicals.atr14_pct", "analyst.upside_pct", "news.positive", "news.negative",
            "regime.ret_20d")
MIN_TRAIN = 200
N_FOLDS = 5
MAX_DRIFT_Z = 3.0  # a feature whose test mean is this many training std-devs away is flagged as drift


def purged_splits(n, horizon, n_splits=N_FOLDS, min_train=MIN_TRAIN):
    """Walk-forward folds: train on everything before the gap, test on the block after it.

    The gap is `horizon + 1` observations: a row's label looks `horizon` sessions ahead, so its last label
    lands on the entry bar of the following label. A gap of only `horizon` would leak that boundary label.
    """
    block = (n - min_train) // n_splits
    if block < 1:
        return
    for i in range(n_splits):
        test_end = n - (n_splits - 1 - i) * block
        train_end = test_end - block - (horizon + 1)
        if train_end < min_train:
            continue
        yield list(range(train_end)), list(range(train_end + horizon + 1, test_end))


def gap_ok(train_idx, test_idx, horizon):
    """Independent check (also a test oracle): no training label may look into the test block."""
    return not (train_idx and test_idx) or max(train_idx) + horizon + 1 < min(test_idx)


def _num(v):
    try:
        return 0.0 if v is None or v != v else float(v)
    except (TypeError, ValueError):
        return 0.0


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def brier(ps, ys):
    return _mean([(p - y) ** 2 for p, y in zip(ps, ys)]) if ys else None


def hit_of(ps, ys, thr=0.5):
    return _mean([int((p >= thr) == bool(y)) for p, y in zip(ps, ys)]) if ys else None


def sklearn_ok():
    try:
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


def _xy(rows, horizon):
    xs, ys = [], []
    for r in rows:
        if r.get("horizon") != horizon or r.get("hit") is None:
            continue
        try:
            b = json.loads(r["bundle"]) if isinstance(r.get("bundle"), str) else (r.get("bundle") or {})
        except (TypeError, ValueError):
            continue
        xs.append([_num(_get(b, p)) for p in FEATURES])
        ys.append(int(r["hit"]))
    return xs, ys


FAMILIES = ("logreg", "gbdt")


def _estimator(kind):
    from sklearn.linear_model import LogisticRegression
    if kind == "gbdt":
        from sklearn.ensemble import HistGradientBoostingClassifier
        return HistGradientBoostingClassifier(max_depth=3, max_iter=80, learning_rate=0.08, random_state=0)
    return LogisticRegression(max_iter=500)


def _fit(xs, ys, kind="logreg"):
    """Classifier + probability calibration. Returns None only when sklearn is unavailable or the sample is
    single-class; a calibration that cannot fit falls back to the uncalibrated classifier, never to a constant.

    Calibration is fitted on a slice disjoint from the classifier's training data:
      * enough rows — classifier on the head, isotonic calibration on the tail (never seen by the classifier);
      * few rows — 3-fold cross-validated sigmoid calibration, which needs no extra held-out block.
    """
    if not sklearn_ok() or len(set(ys)) < 2 or len(xs) < 50:
        return None
    from sklearn.calibration import CalibratedClassifierCV
    cut = max(20, len(xs) // 5)
    inner_x, cal_x = xs[:-cut], xs[-cut:]
    inner_y, cal_y = ys[:-cut], ys[-cut:]
    try:
        if len(inner_x) >= 50 and len(set(inner_y)) > 1 and len(set(cal_y)) > 1 and len(cal_y) >= 30:
            base = _estimator(kind).fit(inner_x, inner_y)
            try:
                return _calibrate_prefit(base, cal_x, cal_y)
            except Exception:
                return base
        return CalibratedClassifierCV(_estimator(kind), cv=3, method="sigmoid").fit(xs, ys)
    except Exception:
        return None


def _calibrate_prefit(base, cal_x, cal_y):
    """sklearn >= 1.6 froze a fitted estimator with FrozenEstimator; older versions accepted cv='prefit'."""
    from sklearn.calibration import CalibratedClassifierCV
    try:
        from sklearn.frozen import FrozenEstimator
        return CalibratedClassifierCV(FrozenEstimator(base), method="isotonic").fit(cal_x, cal_y)
    except ImportError:
        return CalibratedClassifierCV(base, cv="prefit", method="isotonic").fit(cal_x, cal_y)


def _proba(clf, x):
    try:
        return float(clf.predict_proba([x])[0][1])
    except Exception:
        return None


def _predict_fold(xs, ys, train, test, kind="logreg"):
    """Out-of-sample probabilities for one fold; the training base rate when no model could be fitted.
    A fold is all-or-nothing on the model: mixing a model here with constants there would compare a
    model's Brier against a blend."""
    base = _mean([ys[i] for i in train])
    clf = _fit([xs[i] for i in train], [ys[i] for i in train], kind)
    if clf is None:
        return [base] * len(test)
    return [p if (p := _proba(clf, xs[i])) is not None else base for i in test]


def drift(xs_train, xs_test, tol=MAX_DRIFT_Z):
    """Per-feature standardised mean shift between the training window and the most recent window.
    A drifted feature invalidates a shipped model even if its historical Brier looked good."""
    out = {}
    for j, name in enumerate(FEATURES):
        tr = [r[j] for r in xs_train]
        te = [r[j] for r in xs_test]
        if len(tr) < 10 or len(te) < 10:
            continue
        mu, sd = _mean(tr), (sum((x - _mean(tr)) ** 2 for x in tr) / len(tr)) ** 0.5
        if sd <= 1e-12:
            continue
        out[name] = {"z": round((_mean(te) - mu) / sd, 3), "drifted": abs((_mean(te) - mu) / sd) > tol}
    return out


def _eval_family(xs, ys, horizon, kind):
    oos_p, oos_y, notes = [], [], []
    n = len(ys)
    for tr, te in list(purged_splits(n, horizon) or []):
        if not gap_ok(tr, te, horizon):
            notes.append("leakage_guard_rejected_fold")
            continue
        oos_p += _predict_fold(xs, ys, tr, te, kind)
        oos_y += [ys[i] for i in te]
    return oos_p, oos_y, notes


def train_horizon(rows, horizon, store=None):
    """Train, gate and (if it passes) package one horizon. Logistic and gradient-boosting are both tried
    when sklearn is present; each trial is ledgered and only the better OOS Brier may ship."""
    xs, ys = _xy(rows, horizon)
    n, notes = len(ys), []
    if n < MIN_TRAIN:
        notes.append(f"n={n} < {MIN_TRAIN}")
    base_p = _mean(ys)
    families = FAMILIES if sklearn_ok() else (None,)
    best = None
    for kind in families:
        oos_p, oos_y, fold_notes = _eval_family(xs, ys, horizon, kind or "logreg") if kind else ([], [], [])
        if kind is None:
            oos_y = ys[:0]
        notes += fold_notes
        model_brier = brier(oos_p, oos_y) if kind else brier([base_p] * 0, [])
        base_brier = brier([base_p] * len(oos_y), oos_y)
        thr_hit = hit_of(oos_p, oos_y)
        backed = bool(kind) and sklearn_ok() and len(set(oos_y)) > 1 and oos_p
        shipped = bool(oos_y and backed and model_brier is not None and base_brier is not None
                       and model_brier < base_brier and (thr_hit or 0) >= (base_p or 0) and n >= MIN_TRAIN)
        cand = {"kind": kind or "base_rate", "oos_p": oos_p, "oos_y": oos_y,
                "model_brier": model_brier, "base_brier": base_brier, "thr_hit": thr_hit,
                "backed": backed, "shipped": shipped}
        if store is not None:
            store.log_experiment(horizon, f"sklearn_{kind}_calibrated" if kind else "base_rate", n,
                                 model_brier, thr_hit, base_brier, base_p, False if kind else shipped,
                                 "; ".join(fold_notes) or "ok",
                                 {"base_rate": round(base_p, 4), "oos_n": len(oos_y), "family": kind})
        if best is None or (cand["model_brier"] is not None and
                            (best["model_brier"] is None or cand["model_brier"] < best["model_brier"])):
            best = cand
    if best is None:
        best = {"kind": "base_rate", "model_brier": None, "base_brier": None, "thr_hit": None,
                "backed": False, "shipped": False, "oos_y": []}
    shipped = best["shipped"]
    if not best["backed"] and n:
        notes.append("not_shipped: no sklearn model behind these folds")
    final = _fit(xs, ys, best["kind"]) if shipped and best["kind"] in FAMILIES else None
    dft = drift(xs[: n - n // 5], xs[-n // 5:]) if n >= MIN_TRAIN else {}
    if any(d["drifted"] for d in dft.values()):
        shipped, final = False, None
        notes.append("not_shipped: feature drift " + ",".join(k for k, d in dft.items() if d["drifted"]))
    metrics = {"base_rate": round(base_p, 4), "oos_n": len(best.get("oos_y") or []),
               "threshold_hit": best["thr_hit"], "features": list(FEATURES), "drift": dft,
               "model": _dump(final) if shipped else None, "family": best["kind"]}
    name = f"sklearn_{best['kind']}_calibrated" if shipped else "base_rate"
    if store is not None:
        store.log_experiment(horizon, name, n, best["model_brier"], best["thr_hit"], best["base_brier"],
                             base_p, shipped, "; ".join(notes) or "ok",
                             {k: v for k, v in metrics.items() if k != "model"})
    return {"horizon": horizon, "n": n, "brier": best["model_brier"], "baseline_brier": best["base_brier"],
            "hit_rate": best["thr_hit"], "baseline_hit": base_p, "shipped": shipped, "notes": notes,
            "base_rate": round(base_p, 4), "drift": dft, "model": metrics["model"], "family": best["kind"]}


def _dump(clf):
    """Pickled model kept in the ledger row so a shipped horizon can publish a real probability later."""
    if clf is None:
        return None
    try:
        import base64
        return base64.b64encode(pickle.dumps(clf)).decode("ascii")
    except Exception:
        return None


def _load(blob):
    if not blob:
        return None
    try:
        import base64
        return pickle.loads(base64.b64decode(blob))
    except Exception:
        return None


def train_all(store, horizons=HORIZONS):
    rows = store.outcomes_joined()
    return {h: train_horizon(rows, h, store) for h in horizons}


def attach(bundle, trained, now_iso):
    """Prediction facts enter the bundle; they never flip a verdict.

    A shipped model publishes its own probability with the ledger's honest interval. An unshipped one
    publishes only the base rate, labelled calibrated=False, so an experiment can never read as a forecast.
    """
    if not trained:
        return bundle

    def fact(m):
        base = m.get("base_rate")
        clf, p = _load(m.get("model")), None
        if m.get("shipped") and clf is not None:
            p = _proba(clf, [_num(_get(bundle, f)) for f in FEATURES])
        calibrated = p is not None
        p = base if p is None else round(p, 4)
        if p is None:
            return None
        half = 0.10 if not calibrated else max(0.05, round(abs(p - base), 4) + 0.05)
        return {"p_up": p, "interval": [round(max(0.0, p - half), 4), round(min(1.0, p + half), 4)],
                "calibrated": calibrated, "trained_through": now_iso, "n": m.get("n"),
                "brier": m.get("brier"), "baseline_brier": m.get("baseline_brier")}

    facts = {str(h): f for h, m in trained.items() if m and (f := fact(m)) is not None}
    if facts:
        bundle["prediction"] = facts
    return bundle
