import unittest

from bitpin.kimi_score import score_recorded_candidates

HOUR = 3600


def rows(hours=30, price=100):
    return [{"ts": i * HOUR, "open": price, "high": price + 1,
             "low": price - 1, "close": price, "volume": 10} for i in range(hours)]


def decision(at=HOUR, horizon=24, probability=.7):
    return {"time": at, "valid": True, "confidence": .99,
            "candidates": {"BTC_IRT": {"p": probability, "p0": .2}},
            "plans": {"BTC_IRT": {"px_usdt": 100, "take_profit_usdt": 110,
                                     "invalidation_usdt": 95, "horizon_hours": horizon}}}


def panel():
    return {"BTC_IRT": rows(), "USDT_IRT": [dict(x, open=1, close=1, high=1, low=1) for x in rows()]}


class KimiScoreTests(unittest.TestCase):
    def test_target_win_uses_candidate_p_and_geometric_p0_not_confidence(self):
        bars = panel()
        bars["BTC_IRT"][2]["high"] = 111
        result = score_recorded_candidates([decision()], bars, cutoff=30 * HOUR)
        row = result["outcomes"][0]
        self.assertEqual(row["outcome"], 1)
        self.assertEqual(row["probability"], .7)
        self.assertAlmostEqual(row["geometric_p0"], 1 / 3)
        self.assertAlmostEqual(result["scores"]["brier"], .09)
        self.assertEqual(result["evaluated_candidates"], 1)

    def test_close_below_invalidation_loses_but_intrabar_low_does_not(self):
        bars = panel()
        bars["BTC_IRT"][2]["low"] = 90
        bars["BTC_IRT"][3].update(close=94, low=93)
        result = score_recorded_candidates([decision()], bars, cutoff=30 * HOUR)
        self.assertEqual(result["outcomes"][0]["event_time"], 4 * HOUR)
        self.assertEqual(result["outcomes"][0]["outcome"], 0)

    def test_completed_horizon_without_either_event_is_loss(self):
        result = score_recorded_candidates([decision()], panel(), cutoff=30 * HOUR)
        self.assertEqual(result["outcomes"][0]["reason"], "neither_by_horizon")
        self.assertEqual(result["outcomes"][0]["outcome"], 0)

    def test_gap_future_horizon_and_missing_plan_are_censored(self):
        bars = panel()
        del bars["USDT_IRT"][2]
        gap = score_recorded_candidates([decision()], bars, cutoff=30 * HOUR)
        self.assertEqual(gap["censored_reasons"], {"missing_bar": 1})
        unfinished = score_recorded_candidates([decision(horizon=48)], panel(), cutoff=30 * HOUR)
        self.assertEqual(unfinished["censored_reasons"], {"unfinished_horizon": 1})
        missing = decision()
        missing["plans"] = {}
        self.assertEqual(score_recorded_candidates([missing], panel())["censored_reasons"], {"missing_plan": 1})
        bars = panel()
        bars["BTC_IRT"][2].update(high=111, close=94, low=93)
        result=score_recorded_candidates([decision()], bars, cutoff=30 * HOUR)
        self.assertEqual(result['outcomes'][0]['outcome'],1)
        self.assertEqual(result['outcomes'][0]['reason'],'target_before_invalidation')

    def test_predecision_high_and_partial_initial_hour_cannot_be_counted_as_win(self):
        bars = panel()
        bars["BTC_IRT"][0]["high"] = 120
        result = score_recorded_candidates([decision()], bars, cutoff=30 * HOUR)
        self.assertEqual(result["outcomes"][0]["outcome"], 0)
        bars["BTC_IRT"][1]["high"] = 120
        partial = score_recorded_candidates([decision(at=HOUR + 1)], bars, cutoff=30 * HOUR)
        self.assertEqual(partial["censored_reasons"], {"partial_initial_bar": 1})

    def test_partial_initial_bar_is_usable_when_its_bounds_exclude_target(self):
        result = score_recorded_candidates([decision(at=HOUR + 1)], panel(), cutoff=30 * HOUR)
        self.assertEqual(result["outcomes"][0]["reason"], "neither_by_horizon")

    def test_quote_extrema_ambiguity_and_invalid_probability_are_unevaluable(self):
        bars = panel()
        bars["BTC_IRT"][2]["high"] = 109
        bars["USDT_IRT"][2]["low"] = .9
        result = score_recorded_candidates([decision()], bars, cutoff=30 * HOUR)
        self.assertEqual(result["censored_reasons"], {"quote_extrema_ambiguous": 1})
        result = score_recorded_candidates([decision(probability=float("nan"))], panel(), cutoff=30 * HOUR)
        self.assertEqual(result["censored_reasons"], {"invalid_probability": 1})

    def test_repeated_overlapping_symbol_forecasts_not_independent(self):
        result = score_recorded_candidates([decision(), decision(at=2 * HOUR)], panel(), cutoff=30 * HOUR)
        self.assertEqual(result["evaluated_candidates"], 1)
        self.assertEqual(result["censored_reasons"], {"overlapping_forecast": 1})

    def test_unexported_confidence_is_reported_as_unavailable(self):
        recorded = decision()
        del recorded["confidence"]
        result = score_recorded_candidates([recorded], {}, cutoff=30 * HOUR)
        self.assertEqual(result["overall_confidence"]["availability"], "not_exported")


if __name__ == "__main__":
    unittest.main()
