"""Prespecified, training-only technical models and chronological evaluation."""
import math
from collections import Counter

from .strategy_data import DAY, FEATURES

VARIANTS = ("baseline", "trend", "mean_reversion", "combined")
MODEL_FEATURES = {"baseline": (), "trend": ("return_7", "return_30", "volatility_14"),
                  "mean_reversion": ("return_1", "return_7", "volume_ratio"), "combined": FEATURES}


def _softmax(values):
    peak = max(values)
    exp = [math.exp(x - peak) for x in values]
    total = sum(exp)
    return [x / total for x in exp]


def fit_model(rows, variant, shrinkage=20.0):
    """Regularized diagonal Gaussian three-class model; transforms fit on train.

    Class moments shrink toward training-wide moments. Averaging the feature
    log likelihood reduces overconfidence from correlated technical transforms.
    No parameter search uses a development or holdout outcome.
    """
    names = MODEL_FEATURES[variant]
    counts = Counter(row["label"] for row in rows)
    prior = [(counts.get(k, 0) + 1) / (len(rows) + 3) for k in range(3)]
    means, scales, moments = {}, {}, {}
    for name in names:
        values = [float(row["features"][name]) for row in rows]
        center = sum(values) / len(values) if values else 0
        variance = sum((v - center) ** 2 for v in values) / len(values) if values else 1
        scale = math.sqrt(variance) if variance > 1e-12 else 1
        means[name], scales[name] = center, scale
        z = [(v - center) / scale for v in values]
        moments[name] = []
        for label in range(3):
            selected = [v for v, row in zip(z, rows) if row["label"] == label]
            n = len(selected)
            mean = sum(selected) / (n + shrinkage)
            second = (sum(v * v for v in selected) + shrinkage) / (n + shrinkage)
            moments[name].append((mean, max(.2, second - mean * mean)))
    return {"variant": variant, "prior": prior, "features": list(names), "means": means,
            "scales": scales, "moments": moments, "shrinkage": shrinkage,
            "sample_count": len(rows), "method": "shrunk_diagonal_gaussian_mean_log_likelihood"}


def predict_model(model, episode):
    """Read only the declared feature input; labels and returns are ignored."""
    scores = [math.log(p) for p in model["prior"]]
    names = model["features"]
    for name in names:
        z = (episode["features"][name] - model["means"][name]) / model["scales"][name]
        for label in range(3):
            mean, variance = model["moments"][name][label]
            scores[label] += -.5 * (math.log(variance) + (z - mean) ** 2 / variance) / len(names)
    return _softmax(scores)


def _brier(label, probabilities):
    return sum((p - int(k == label)) ** 2 for k, p in enumerate(probabilities))


def _prepare(episodes):
    rows = sorted(episodes, key=lambda x: (x["decision_time"], x["symbol"]))
    targets, previous = set(), {}
    identities = set()
    for row in rows:
        targets.add((row["target"], row["return_currency"], row["cost_band"]))
        if row["episode_id"] in identities or row["decision_time"] < previous.get(row["symbol"], -1):
            raise ValueError("duplicate or overlapping episodes")
        identities.add(row["episode_id"])
        previous[row["symbol"]] = row["label_end"]
        if row["label"] not in (0, 1, 2) or row["label_end"] <= row["decision_time"]:
            raise ValueError("invalid labelled episode")
        if not math.isfinite(row["realized_return"]) or row["realized_return"] < -1:
            raise ValueError("invalid realized return")
        if not all(math.isfinite(row["features"][name]) for name in FEATURES):
            raise ValueError("invalid features")
    if len(targets) > 1:
        raise ValueError("mixed targets, currencies or cost bands require separate evaluation")
    return rows


def _fit_period(rows, calibration_start, test_start):
    from .evidence_statistics import fit_influences
    train = [row for row in rows if row["label_end"] < calibration_start]
    calibration = [row for row in rows if calibration_start <= row["decision_time"] < test_start
                   and row["label_end"] < test_start]
    models = {variant: fit_model(train, variant) for variant in VARIANTS}
    weights, losses = {"baseline": 0}, {}
    for variant in VARIANTS:
        records = [{"time": row["decision_time"], "label_end": row["label_end"], "label": row["label"],
                    "base_probs": models["baseline"]["prior"],
                    "forecasts": {"technical": predict_model(models[variant], row)}} for row in calibration]
        if variant != "baseline":
            fitted = fit_influences(records, ["technical"], {"technical": .50}, cutoff=test_start)
            weights[variant] = fitted["weights"].get("technical", 0)
        losses[variant] = sum(_brier(row["label"], _forecast(models, weights, variant, row))
                              for row in calibration) / len(calibration) if calibration else None
    selected = min(VARIANTS, key=lambda variant: losses[variant] if losses[variant] is not None else float("inf"))
    return {"models": models, "weights": weights, "calibration_brier": losses, "selected_variant": selected,
            "train_count": len(train), "calibration_count": len(calibration),
            "max_train_label_end": max((row["label_end"] for row in train), default=None),
            "max_calibration_label_end": max((row["label_end"] for row in calibration), default=None)}


def _forecast(models, weights, variant, row):
    candidate = predict_model(models[variant], row)
    baseline = models["baseline"]["prior"]
    weight = weights[variant]
    return [(1 - weight) * b + weight * c for b, c in zip(baseline, candidate)]


def _compound(daily):
    value = 1.0
    for result in daily:
        value *= max(0, 1 + result)
    return value - 1


def _summarize(records, repeats, seed, fee, slippage):
    from .evidence_statistics import score_predictions, paired_block_bootstrap, reality_check
    metrics, comparators, daily_scores = {}, {}, {}
    times = sorted({row["episode"]["decision_time"] for row in records})
    currency = records[0]["episode"]["return_currency"] if records else None
    for variant in VARIANTS:
        scored = [{"label": row["episode"]["label"], "probabilities": row["probabilities"][variant]}
                  for row in records]
        metrics[variant] = score_predictions(scored)
        metrics[variant]["calendar_days"] = len(times)
        benchmark_correct = sum(row["episode"]["label"] == row["training_majority"] for row in records)
        metrics[variant]["training_majority_accuracy"] = benchmark_correct / len(records) if records else None
        by_day, net, gross, stress, always_long, longs = {}, [], [], [], [], 0
        for row in records:
            by_day.setdefault(row["episode"]["decision_time"], []).append(row)
        daily_scores[variant] = []
        for ts in times:
            group = by_day[ts]
            daily_scores[variant].append(sum(_brier(row["episode"]["label"], row["probabilities"][variant])
                                             for row in group) / len(group))
            returns, net_returns, stress_returns = [], [], []
            for row in group:
                probs = row["probabilities"][variant]
                long = probs[2] > max(probs[0], probs[1])
                realized = row["episode"]["realized_return"]
                longs += int(long)
                returns.append(realized if long else 0)
                net_returns.append((1 + realized) * (1 - fee - slippage) ** 2 - 1 if long else 0)
                stress_returns.append((1 + realized) * (1 - 2 * (fee + slippage)) ** 2 - 1 if long else 0)
            gross.append(sum(returns) / len(group))
            net.append(sum(net_returns) / len(group))
            stress.append(sum(stress_returns) / len(group))
            always_long.append(sum(row["episode"]["realized_return"] for row in group) / len(group))
        comparators[variant] = {"currency": currency, "return": _compound(net), "gross_return": _compound(gross),
                               "stress_return": _compound(stress), "cash_return": 0.0,
                               "daily_equal_weight_long_gross_return": _compound(always_long),
                               "daily_equal_weight_long_return": _compound([(1 + r) * (1 - fee - slippage) ** 2 - 1
                                                                            for r in always_long]),
                               "daily_equal_weight_long_stress_return": _compound([(1 + r) * (1 - 2 * (fee + slippage)) ** 2 - 1
                                                                                   for r in always_long]),
                               "fee_per_side": fee, "slippage_per_side": slippage, "stress_multiplier": 2,
                               "long_episodes": longs, "method": "daily_independent_roundtrip_long_or_cash"}
    differences = [[base - value for base, value in zip(daily_scores["baseline"], daily_scores[variant])]
                   for variant in VARIANTS[1:]]
    intervals = {variant: {str(block): paired_block_bootstrap(
                              [{"time": ts, "difference": value} for ts, value in zip(times, values)],
                              block_size=block, repeats=repeats, seed=seed)
                          for block in (3, 7, 14)} for variant, values in zip(VARIANTS[1:], differences)}
    family = reality_check([{"time": ts, "differences": list(values)}
                            for ts, values in zip(times, zip(*differences))],
                           block_size=7, repeats=repeats, seed=seed)
    regimes = {}
    for name, predicate in (("past_up", lambda r: r > .008), ("past_down", lambda r: r < -.008),
                            ("past_flat", lambda r: -.008 <= r <= .008)):
        subset = [row for row in records if predicate(row["episode"]["features"]["return_30"])]
        regimes[name] = {variant: score_predictions([{"label": row["episode"]["label"],
                                                      "probabilities": row["probabilities"][variant]} for row in subset])
                         for variant in VARIANTS}
    return {"episode_count": len(records), "calendar_days": len(times), "metrics": metrics,
            "comparators": comparators, "paired_brier_improvement": intervals, "reality_check": family,
            "regimes": regimes, "missing_calendar_days": ((times[-1] - times[0]) // DAY + 1 - len(times)) if times else 0,
            "predictions": [{"episode_id": row["episode"]["episode_id"],
                             "decision_time": row["episode"]["decision_time"],
                             "probabilities": row["probabilities"]} for row in records]}


def evaluate_walkforward(episodes, train_days=180, calibration_days=90, development_days=90,
                         holdout_days=180, bootstrap_repeats=500, seed=20261011,
                         fee=.0035, slippage=.0005):
    """Fit on mature past labels, evaluate calendar folds and a frozen holdout.

    All symbols split at identical timestamps. Candidate selection and technical
    influence use only the calibration window immediately preceding each test.
    This research comparator is independent of bot fills, ladders and accounts.
    """
    if min(train_days, calibration_days, development_days, holdout_days, bootstrap_repeats) < 1:
        raise ValueError("periods and bootstrap repeats must be positive")
    if min(fee, slippage) < 0 or 2 * (fee + slippage) >= 1:
        raise ValueError("invalid declared costs")
    rows = _prepare(episodes)
    empty = {"episode_count": 0, "calendar_days": 0, "metrics": {}, "predictions": []}
    report = {"status": "inconclusive", "variants": list(VARIANTS), "folds": [], "development": dict(empty),
              "holdout": dict(empty), "holdout_start": None, "promotion": "research_only",
              "method": "training-only shrunk Gaussian; past-calibration capped technical mixture",
              "class_order": ["down", "flat", "up"], "trial_family": list(VARIANTS),
              "parameters": {"train_days": train_days, "calibration_days": calibration_days,
                             "development_days": development_days, "holdout_days": holdout_days,
                             "shrinkage": 20, "technical_cap": .5, "seed": seed},
              "limitations": ["reconstructed prices; original collector history unavailable",
                              "calendar-day bootstrap samples are not extra market observations",
                              "simple daily long/cash comparator is not full live-bot replay"]}
    if not rows:
        return report
    first = min(row["decision_time"] for row in rows)
    end = max(row["decision_time"] for row in rows) + DAY
    holdout_start = end - holdout_days * DAY
    report["holdout_start"] = holdout_start
    first_test = first + (train_days + calibration_days) * DAY
    if first_test > holdout_start:
        report["limitations"].append("insufficient history for prescribed train/calibration/holdout")
        return report
    development = []
    test_start = first_test
    while test_start < holdout_start:
        test_end = min(test_start + development_days * DAY, holdout_start)
        fitted = _fit_period(rows, test_start - calibration_days * DAY, test_start)
        test = [row for row in rows if test_start <= row["decision_time"] < test_end]
        records = _predict_records(fitted, test)
        fold = {k: v for k, v in fitted.items() if k != "models"}
        fold.update(calibration_start=test_start - calibration_days * DAY, test_start=test_start, test_end=test_end)
        fold.update(_summarize(records, bootstrap_repeats, seed, fee, slippage))
        report["folds"].append(fold)
        development.extend(records)
        test_start = test_end
    final = _fit_period(rows, holdout_start - calibration_days * DAY, holdout_start)
    holdout = _predict_records(final, [row for row in rows if row["decision_time"] >= holdout_start])
    report["development"] = _summarize(development, bootstrap_repeats, seed, fee, slippage)
    report["holdout"] = _summarize(holdout, bootstrap_repeats, seed, fee, slippage)
    report["holdout"].update({k: v for k, v in final.items() if k != "models"})
    report["holdout"].update(calibration_start=holdout_start - calibration_days * DAY, test_start=holdout_start)
    return report


def _predict_records(fitted, rows):
    majority = max(range(3), key=lambda label: fitted["models"]["baseline"]["prior"][label])
    return [{"episode": row, "training_majority": majority,
             "probabilities": {variant: _forecast(fitted["models"], fitted["weights"], variant, row)
                               for variant in VARIANTS}} for row in rows]
