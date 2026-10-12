"""Offline scoring of recorded target-before-hourly-invalidation probabilities.

Candidate p, overall allocation confidence, and the new three-class event are
different quantities. Missing or ambiguous price paths never become losses.
"""
import math
import time
from collections import Counter

from .strategy_data import validate_bars

HOUR = 3600


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _score(rows, probability_key):
    if not rows:
        return {"count": 0, "brier": None, "log_loss": None, "accuracy_at_half": None, "reliability_bins": []}
    brier, log_loss, correct = 0.0, 0.0, 0
    bins = [[] for _ in range(5)]
    for row in rows:
        p, outcome = row[probability_key], row["outcome"]
        brier += (p - outcome) ** 2
        log_loss -= math.log(max(1e-15, min(1 - 1e-15, p if outcome else 1 - p)))
        correct += int((p >= .5) == bool(outcome))
        bins[min(4, int(p * 5))].append((p, outcome))
    return {"count": len(rows), "brier": brier / len(rows), "log_loss": log_loss / len(rows),
            "accuracy_at_half": correct / len(rows),
            "reliability_bins": [{"bin": i, "count": len(group),
                                  "mean_probability": sum(p for p, _ in group) / len(group) if group else None,
                                  "event_frequency": sum(y for _, y in group) / len(group) if group else None}
                                 for i, group in enumerate(bins)]}


def _outcome(at, end, target, invalidation, coin, quote, cutoff):
    containing = [ts for ts in coin if ts <= at < ts + HOUR]
    if not containing:
        return None, "missing_bar", None
    ts = max(containing)
    while ts < end:
        if ts + HOUR > cutoff:
            return None, "unfinished_horizon", None
        if ts not in coin or ts not in quote:
            return None, "missing_bar", None
        bar, unit = coin[ts], quote[ts]
        possible_target = bar["high"] / unit["low"] >= target
        certain_target = bar["high"] / unit["high"] >= target
        if ts < at and possible_target:
            return None, "partial_initial_bar", None
        if ts + HOUR > end and possible_target:
            return None, "partial_terminal_bar", None
        if possible_target and not certain_target:
            return None, "quote_extrema_ambiguous", None
        invalid = ts + HOUR <= end and bar["close"] / unit["close"] < invalidation
        if certain_target:
            # A guaranteed touch in a full hour precedes its closing invalidation:
            # the close is below the lower level and cannot itself be the target.
            return 1, "target_before_invalidation", ts + HOUR
        if invalid:
            return 0, "hourly_close_invalidation", ts + HOUR
        ts += HOUR
    if end > cutoff:
        return None, "unfinished_horizon", None
    return 0, "neither_by_horizon", end


def score_recorded_candidates(decisions, bars, cutoff=None, bootstrap_repeats=500, seed=20261011):
    """Score sanitized decision records against exact synchronized hourly bars.

    Decisions: ``{time, valid, candidates:{symbol:{p}}, plans:{symbol:{px_usdt,
    take_profit_usdt,invalidation_usdt,horizon_hours}}, symbols:{symbol:{px_usdt}}}``.
    Plans/entry must be contemporaneous, never copied from a later decision.
    ``bars`` maps COIN_IRT/USDT_IRT to OHLCV lists or ``{bars,receipt}`` wrappers.
    Initial/terminal partial hours are usable only when whole-bar bounds exclude
    target contact. Overlapping same-symbol forecast intervals are not counted
    as independent calibration observations.
    """
    cutoff = time.time() if cutoff is None else float(cutoff)
    clean = {}
    for symbol, series in bars.items():
        series = series.get("bars", []) if isinstance(series, dict) else series
        clean[symbol] = {row["ts"]: row for row in validate_bars(series, step=HOUR, cutoff=cutoff)}
    outcomes, reasons, previous_end, seen = [], Counter(), {}, set()
    decisions_with_confidence, candidate_count = 0, 0
    ordered = sorted(decisions, key=lambda row: _number(row.get("time", row.get("decision_time"))) or 0)
    for decision in ordered:
        decisions_with_confidence += int(_number(decision.get("confidence")) is not None)
        at = _number(decision.get("time", decision.get("decision_time")))
        candidates = decision.get("candidates") or {}
        plans = decision.get("plans") or {}
        for symbol, candidate in sorted(candidates.items()):
            candidate_count += 1
            row = {"symbol": symbol, "decision_time": at, "probability": None, "outcome": None,
                   "reason": None, "model": decision.get("model"), "response_sha256": decision.get("response_sha256")}
            reason = None
            p = _number(candidate.get("p")) if isinstance(candidate, dict) else None
            plan = plans.get(symbol)
            if decision.get("valid") is False or decision.get("recorded_response_present") is False:
                reason = "invalid_decision"
            elif at is None or at > cutoff:
                reason = "invalid_or_future_decision_time"
            elif p is None or not 0 <= p <= 1:
                reason = "invalid_probability"
            elif not isinstance(plan, dict):
                reason = "missing_plan"
            if not reason:
                entry = _number(plan.get("px_usdt", (decision.get("symbols", {}).get(symbol) or {}).get("px_usdt")))
                target, invalidation = _number(plan.get("take_profit_usdt")), _number(plan.get("invalidation_usdt"))
                horizon = _number(plan.get("horizon_hours"))
                if None in (entry, target, invalidation, horizon) or not target > entry > invalidation > 0:
                    reason = "invalid_plan_levels"
                elif horizon != int(horizon) or not 24 <= horizon <= 720:
                    reason = "invalid_plan_horizon"
                elif not symbol.endswith("_IRT") or symbol == "USDT_IRT":
                    reason = "unsupported_symbol_units"
                else:
                    end = at + horizon * HOUR
                    identity = (symbol, at, decision.get("response_sha256"))
                    row.update(probability=p, label_end=end, geometric_p0=(entry - invalidation) / (target - invalidation))
                    if identity in seen or at < previous_end.get(symbol, -1):
                        reason = "overlapping_forecast"
                    else:
                        seen.add(identity)
                        previous_end[symbol] = end
                        outcome, reason, event_time = _outcome(at, end, target, invalidation,
                                                             clean.get(symbol, {}), clean.get("USDT_IRT", {}), cutoff)
                        row.update(outcome=outcome, event_time=event_time)
            row["reason"] = reason
            outcomes.append(row)
            if row["outcome"] is None:
                reasons[reason] += 1
    scored = [row for row in outcomes if row["outcome"] is not None]
    report = {"status": "inconclusive", "recorded_decisions": len(decisions), "recorded_candidates": candidate_count,
              "evaluated_candidates": len(scored), "censored_candidates": candidate_count - len(scored),
              "calendar_days": len({int(row["decision_time"] // 86400) for row in scored}),
              "censored_reasons": dict(reasons), "outcomes": outcomes,
              "scores": _score(scored, "probability"), "geometric_benchmark": _score(scored, "geometric_p0"),
              "probability_semantics": "target before hourly-close invalidation within original plan horizon",
              "geometric_p0_semantics": "loss_distance/(gain_distance+loss_distance); not empirical base rate",
              "overall_confidence": {"exported_count": decisions_with_confidence,
                                     "availability": "not_exported" if not decisions_with_confidence else
                                         "exported" if decisions_with_confidence == len(decisions) else "partial_export",
                                     "score_status": "not_scored_distinct_allocation_semantics"},
              "limitations": ["hourly quote-ratio extrema and partial-hour ordering can censor cases",
                              "overlapping forecasts are excluded from independent calibration counts",
                              "new historical macro/flow/news/whale evidence is unavailable"]}
    if scored:
        from .evidence_statistics import paired_block_bootstrap
        daily = {}
        for row in scored:
            day = int(row["decision_time"] // 86400) * 86400
            daily.setdefault(day, []).append((row["geometric_p0"] - row["outcome"]) ** 2
                                              - (row["probability"] - row["outcome"]) ** 2)
        report["paired_brier_improvement"] = paired_block_bootstrap(
            [{"time": day, "difference": sum(values) / len(values)} for day, values in sorted(daily.items())],
            block_size=7, repeats=bootstrap_repeats, seed=seed)
    return report
