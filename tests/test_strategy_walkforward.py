import json
import os
import subprocess
import sys
import tempfile
import unittest

from bitpin.strategy_walkforward import evaluate_walkforward, fit_model, predict_model

DAY = 86400


def episodes(days=560, symbols=("BTC_IRT", "ETH_IRT")):
    out = []
    for day in range(days):
        for symbol in symbols:
            label = day % 3
            out.append({"episode_id": "%s-%d" % (symbol, day), "symbol": symbol,
                        "decision_time": day * DAY, "label_end": (day + 1) * DAY,
                        "label": label, "realized_return": (-.02, .001, .03)[label],
                        "return_currency": "USDT", "target": "next_24h_down_flat_up", "cost_band": .008,
                        "features": {"return_1": (label - 1) * .01, "return_7": (label - 1) * .02,
                                     "return_30": (label - 1) * .03, "volatility_14": .01,
                                     "volume_ratio": label * .1}})
    return out


class WalkForwardTests(unittest.TestCase):
    def test_model_learns_a_separable_distribution_and_ignores_nonfeature_keys(self):
        training = episodes(80, ("BTC_IRT",))
        model = fit_model(training, "combined")
        probabilities = predict_model(model, training[2])
        self.assertGreater(probabilities[2], probabilities[0])
        self.assertAlmostEqual(sum(probabilities), 1)
        poison = dict(training[2], label=0, realized_return=-999, decision_time=99999)
        self.assertEqual(probabilities, predict_model(model, poison))

    def test_calendar_splits_keep_all_assets_together_and_labels_mature(self):
        result = evaluate_walkforward(episodes(), train_days=120, calibration_days=60,
                                      development_days=90, holdout_days=180, bootstrap_repeats=20)
        self.assertEqual(result["holdout_start"], 380 * DAY)
        self.assertEqual(result["holdout"]["calendar_days"], 180)
        self.assertEqual(result["holdout"]["episode_count"], 360)
        for fold in result["folds"]:
            self.assertLess(fold["max_train_label_end"], fold["calibration_start"])
            self.assertLess(fold["max_calibration_label_end"], fold["test_start"])
            self.assertLessEqual(fold["test_end"], result["holdout_start"])
        self.assertEqual(set(result["variants"]), {"baseline", "trend", "mean_reversion", "combined"})
        self.assertEqual(set(result["holdout"]["metrics"]), set(result["variants"]))

    def test_holdout_labels_cannot_change_fitted_predictions_or_selection(self):
        rows = episodes(460, ("BTC_IRT",))
        first = evaluate_walkforward(rows, train_days=90, calibration_days=45,
                                     holdout_days=100, bootstrap_repeats=10)
        changed = [dict(x, label=0, realized_return=-.9) if x["decision_time"] >= first["holdout_start"]
                   else dict(x) for x in rows]
        second = evaluate_walkforward(changed, train_days=90, calibration_days=45,
                                      holdout_days=100, bootstrap_repeats=10)
        self.assertEqual(first["holdout"]["selected_variant"], second["holdout"]["selected_variant"])
        self.assertEqual(first["holdout"]["predictions"], second["holdout"]["predictions"])

    def test_overlapping_or_mixed_targets_are_not_pooled(self):
        rows = episodes(300, ("BTC_IRT",))
        with self.assertRaises(ValueError):
            evaluate_walkforward(rows + [dict(rows[0], episode_id="duplicate")])
        rows[1]["return_currency"] = "IRT"
        with self.assertRaises(ValueError):
            evaluate_walkforward(rows)

    def test_small_history_returns_explicit_inconclusive_status(self):
        result = evaluate_walkforward(episodes(30), bootstrap_repeats=10)
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["holdout"]["episode_count"], 0)

    def test_daily_roundtrip_comparator_charges_all_in_declared_currency(self):
        result = evaluate_walkforward(episodes(460, ("BTC_IRT",)), train_days=90,
                                      calibration_days=45, holdout_days=100, bootstrap_repeats=10)
        simulation = result["holdout"]["comparators"]["combined"]
        self.assertEqual(simulation["currency"], "USDT")
        self.assertEqual(simulation["fee_per_side"], .0035)
        self.assertLess(simulation["return"], simulation["gross_return"])
        self.assertLess(simulation["stress_return"], simulation["return"])
        self.assertGreater(simulation["long_episodes"], 0)
        self.assertLess(simulation["daily_equal_weight_long_return"],
                        simulation["daily_equal_weight_long_gross_return"])

    def test_offline_cli_writes_hashed_inputs_and_explicit_small_sample_status(self):
        project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "daily.json")
            destination = os.path.join(directory, "evaluation.json")
            bars = [{"ts": day * DAY, "open": 100, "close": 100, "low": 99, "high": 101,
                     "volume": 10} for day in range(100)]
            with open(source, "w") as handle:
                json.dump(bars, handle)
            result = subprocess.run([sys.executable, os.path.join(project, "scripts", "strategy_evaluate.py"),
                                     "--daily", "BTC_IRT=" + source, "--currency", "IRT",
                                     "--cutoff", str(100 * DAY), "--output", destination],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(destination) as handle:
                report = json.load(handle)
            self.assertEqual(report["quantitative"]["IRT"]["status"], "inconclusive")
            self.assertEqual(report["fresh_kimi_status"], "not_run")
            self.assertEqual(len(report["input_manifest"][0]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
