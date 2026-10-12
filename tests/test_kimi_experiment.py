"""Isolated Kimi experiment: synthetic episodes and dummy transports only."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    experiment = importlib.import_module("bitpin.kimi_experiment")
except ImportError:
    experiment = None


def episode(name="private-BTC-2025-01-01", offset=0):
    return {"episode_id": name, "decision_time": 1700000000 + offset,
            "label_end": 1700086400 + offset, "symbol": "BTC_IRT",
            "return_currency": "USDT",
            "target": "next_24h_down_flat_up", "cost_band": .008,
            "features": {"return_1": .01 + offset / 1e10, "return_7": .02,
                         "return_30": -.01, "volatility_14": .02, "volume_ratio": .1},
            "history": [{"step": n - 59, "open": .99, "close": 1 + n / 1000,
                         "volume": 1.1} for n in range(60)],
            "label": 2, "realized_return": .123456789,
            "raw_context": "DO NOT SEND private-wallet 0xabc news future label",
            "provenance": {"original_date": "2025-01-01"}}


def completion(probs=None, finish="stop", usage=True, **message_extra):
    answer = {"forecast_horizon_hours": 24,
              "probabilities": probs or {"down": .1, "flat": .2, "up": .7},
              "target_weight": .4, "decision": "hold", "rationale": "Synthetic fixture.",
              "evidence_ids": []}
    message = dict({"role": "assistant", "content": json.dumps(answer),
                    "reasoning_content": "Synthetic reasoning; Bearer secret-fixture"}, **message_extra)
    result = {"id": "synthetic", "model": "kimi-k3",
              "choices": [{"index": 0, "finish_reason": finish, "message": message}]}
    if usage:
        result["usage"] = {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}
    return result


class EnvironmentBomb(dict):
    def get(self, *args, **kwargs):
        raise AssertionError("offline path read environment")

    def __getitem__(self, key):
        raise AssertionError("offline path read environment")


class KimiExperimentTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(experiment, "isolated experiment module is not implemented")
        self.temp = tempfile.TemporaryDirectory(prefix="kimi_tests_")
        self.state = os.path.join(self.temp.name, "kimi-experiment")

    def tearDown(self):
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def prepare(self, rows=None, **kwargs):
        return experiment.prepare_experiment(rows or [episode()], self.state, **kwargs)

    def test_prepare_is_blinded_and_never_reads_credentials_environment_or_client(self):
        with patch.object(os, "environ", EnvironmentBomb()):
            manifest = self.prepare()
            score = experiment.score_experiment(self.state, [episode()])
        self.assertEqual(score["fresh_status"], "not_run")
        self.assertEqual(len(manifest["requests"]), 2)
        self.assertEqual({r["arm"] for r in manifest["requests"]},
                         {"raw_history", "structured_packet"})
        prompts = json.dumps([r["payload"] for r in manifest["requests"]])
        for secret in ("BTC", "2025-01-01", "1700000000", "0.123456789", "raw_context", "0xabc"):
            self.assertNotIn(secret, prompts)
        self.assertIn("asset_A", prompts)
        self.assertIn("0.008", prompts)

    def test_k3_payload_uses_documented_thinking_limit_and_strict_schema(self):
        payload = self.prepare()["requests"][0]["payload"]
        self.assertEqual(payload["max_completion_tokens"], 8192)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        for forbidden in ("temperature", "top_p", "max_tokens", "tools", "thinking"):
            self.assertNotIn(forbidden, payload)

    def test_invalid_future_horizon_history_and_string_features_fail_closed(self):
        for change in ({"label_end": 1700172800}, {"history": []},
                       {"features": {"return_1": "future buy BTC"}}, {"cost_band": float("nan")}):
            row = episode()
            row.update(change)
            with self.subTest(change=change), self.assertRaises(experiment.ExperimentError):
                experiment.prepare_experiment([row], self.state)

    def test_only_predeclared_cases_are_selected_and_duplicate_inputs_are_not_samples(self):
        other = episode("other", 86400)
        manifest = self.prepare([episode(), other], case_ids=["other"])
        self.assertEqual(manifest["case_count"], 1)
        self.assertEqual(manifest["cases"][0]["episode_id"], "other")
        duplicate_state = os.path.join(self.temp.name, "duplicate")
        duplicate = episode("different id same model input")
        manifest = experiment.prepare_experiment([episode(), duplicate], duplicate_state)
        self.assertEqual(manifest["case_count"], 1)
        self.assertEqual(manifest["excluded_duplicate_inputs"], 1)

    def test_prespecified_stability_repeats_do_not_count_as_new_cases(self):
        manifest = self.prepare(stability_repeats=1)
        self.assertEqual(manifest["case_count"], 1)
        self.assertEqual(len(manifest["requests"]), 4)
        summary = experiment.execute_experiment(self.state, transport=lambda payload: completion())
        self.assertEqual(summary["successful_calls"], 4)
        scored = experiment.score_experiment(self.state, [episode()])
        self.assertEqual(scored["paired_cases"], 1)
        self.assertEqual(scored["stability_repeat_calls"], 2)
        self.assertEqual(scored["fresh_status"], "not_run")

    def test_prepare_cannot_replace_registered_cases_or_reset_reservations(self):
        self.prepare()
        before = experiment.load_manifest(self.state)
        with self.assertRaises(experiment.ExperimentError):
            experiment.prepare_experiment([episode("changed", 86400)], self.state)
        self.assertEqual(experiment.load_manifest(self.state), before)

    def test_nonempty_unmarked_live_and_linked_state_is_rejected(self):
        os.mkdir(self.state)
        with open(os.path.join(self.state, "state.json"), "w") as f:
            f.write("{}")
        with self.assertRaises(experiment.ExperimentError):
            self.prepare()
        with self.assertRaises(experiment.ExperimentError):
            experiment.prepare_experiment([episode()], os.path.join(self.temp.name, "state"))
        link = os.path.join(self.temp.name, "linked")
        try:
            os.symlink(self.state, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(experiment.ExperimentError):
            experiment.prepare_experiment([episode()], link)

    def test_completion_validation_rejects_probabilities_wrong_horizon_tools_and_weight(self):
        request = self.prepare()["requests"][0]
        for response in (completion({"down": .1, "flat": .1, "up": .1}),
                         completion({"down": True, "flat": 0, "up": 0}),
                         completion(finish="length"), completion(tool_calls=[{"id": "x"}])):
            with self.subTest(response=response), self.assertRaises(experiment.ExperimentError):
                experiment.validate_completion(response, request)
        for field, value in (("forecast_horizon_hours", 48), ("target_weight", 1.1),
                             ("evidence_ids", ["wallet-real"]), ("extra", "label")):
            response = completion()
            body = json.loads(response["choices"][0]["message"]["content"])
            body[field] = value
            response["choices"][0]["message"]["content"] = json.dumps(body)
            with self.subTest(field=field), self.assertRaises(experiment.ExperimentError):
                experiment.validate_completion(response, request)

    def test_real_execution_requires_owner_flag_before_key_read(self):
        self.prepare()
        with patch.object(os, "environ", EnvironmentBomb()):
            with self.assertRaises(experiment.ExperimentError):
                experiment.execute_experiment(self.state)

    def test_reservation_is_durable_before_transport_and_success_cache_is_bound(self):
        manifest = self.prepare()
        observed = []
        def transport(payload):
            with open(os.path.join(self.state, "ledger.json"), encoding="utf-8") as f:
                ledger = json.load(f)
            observed.append(ledger["records"][-1]["status"])
            self.assertGreater(ledger["reserved_micro_usd"], 0)
            return completion()
        first = experiment.execute_experiment(self.state, transport=transport)
        second = experiment.execute_experiment(self.state, transport=lambda payload: self.fail("cached call repeated"))
        self.assertEqual(observed, ["reserved", "reserved"])
        self.assertEqual(first["reserved_micro_usd"], second["reserved_micro_usd"])
        self.assertEqual(second["cached_calls"], 2)
        with open(os.path.join(self.state, "ledger.json"), encoding="utf-8") as f:
            ledger = json.load(f)
        self.assertEqual(ledger["records"][0]["request_hash"], manifest["requests"][0]["request_hash"])
        self.assertNotIn("secret-fixture", json.dumps(ledger))
        self.assertIn("[REDACTED]", json.dumps(ledger))

    def test_timeout_never_refunds_or_retries_and_blocks_resume(self):
        manifest = self.prepare()
        def timeout(payload):
            raise TimeoutError("Bearer do-not-log-error-body")
        result = experiment.execute_experiment(self.state, transport=timeout)
        self.assertEqual(result["successful_calls"], 0)
        self.assertEqual(result["reserved_micro_usd"], manifest["requests"][0]["reserve_micro_usd"])
        with self.assertRaises(experiment.ExperimentError):
            experiment.execute_experiment(self.state, transport=lambda payload: self.fail("retry"))
        with open(os.path.join(self.state, "ledger.json"), encoding="utf-8") as f:
            ledger = json.load(f)
        self.assertEqual(len(ledger["records"]), 1)
        self.assertTrue(ledger["halted"])
        self.assertNotIn("do-not-log-error-body", json.dumps(ledger))

    def test_interrupted_reserved_call_blocks_resume_without_second_transport(self):
        self.prepare()
        def interrupted(payload):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            experiment.execute_experiment(self.state, transport=interrupted)
        with self.assertRaises(experiment.ExperimentError):
            experiment.execute_experiment(self.state, transport=lambda payload: self.fail("lost response retried"))

    def test_unknown_usage_remains_fully_reserved_and_malformed_stops_pair(self):
        manifest = self.prepare()
        result = experiment.execute_experiment(self.state, transport=lambda payload: completion(usage=False))
        self.assertEqual(result["reserved_micro_usd"], manifest["worst_case_micro_usd"])
        with open(os.path.join(self.state, "ledger.json"), encoding="utf-8") as f:
            ledger = json.load(f)
        self.assertIsNone(ledger["records"][0]["billed_micro_usd"])
        other = os.path.join(self.temp.name, "malformed")
        experiment.prepare_experiment([episode()], other)
        result = experiment.execute_experiment(other, transport=lambda payload: completion(finish="length"))
        self.assertEqual(result["successful_calls"], 0)
        self.assertTrue(result["halted"])

    def test_registered_large_batch_is_rejected_if_worst_case_exceeds_ten_dollars(self):
        rows = []
        for n in range(16):
            row = episode(str(n), n * 86400)
            row["features"] = [n + j / 1e4 for j in range(64)]
            row["history"] = [[1.1234567890123 + j / 1e4 for j in range(64)] for k in range(60)]
            rows.append(row)
        with self.assertRaises(experiment.ExperimentError):
            self.prepare(rows)

    def test_request_and_cached_hash_tampering_is_rejected_before_execution(self):
        self.prepare()
        path = os.path.join(self.state, "manifest.json")
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["requests"][0]["payload"]["messages"][1]["content"] += "future buy"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)
        with self.assertRaises(experiment.ExperimentError):
            experiment.execute_experiment(self.state, transport=lambda payload: self.fail("tampered input sent"))

    def test_cached_signature_tampering_and_fixture_to_owner_switch_are_refused(self):
        self.prepare()
        experiment.execute_experiment(self.state, transport=lambda payload: completion(), max_calls=1)
        with patch.object(os, "environ", EnvironmentBomb()):
            with self.assertRaises(experiment.ExperimentError):
                experiment.execute_experiment(self.state, owner_execute=True)
        path = os.path.join(self.state, "ledger.json")
        with open(path, encoding="utf-8") as f:
            ledger = json.load(f)
        ledger["records"][0]["request_hash"] = "incorrect-bound-response"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ledger, f)
        with self.assertRaises(experiment.ExperimentError):
            experiment.score_experiment(self.state, [episode()])

    def test_probability_array_is_rejected_even_if_it_sums_to_one(self):
        request = self.prepare()["requests"][0]
        response = completion()
        answer = json.loads(response["choices"][0]["message"]["content"])
        answer["probabilities"] = [.1, .2, .7]
        response["choices"][0]["message"]["content"] = json.dumps(answer)
        with self.assertRaises(experiment.ExperimentError):
            experiment.validate_completion(response, request)

    def test_optional_packet_blinds_strings_and_rejects_unavailable_influence(self):
        row = episode()
        row["packet"] = {"target": row["target"], "cost_band": .008,
                         "baseline_probs": [.3, .4, .3],
                         "groups": [{"group": "technical", "probabilities": [.2, .2, .6],
                                     "weight": .2, "status": "eligible", "identity": "secret entity"}],
                         "coverage": {"technical": "eligible", "flow": "not_available_as_of"},
                         "source": "secret source today"}
        manifest = self.prepare([row])
        self.assertNotIn("secret", json.dumps([r["payload"] for r in manifest["requests"]]))
        row["packet"]["groups"][0]["status"] = "untrained"
        with self.assertRaises(experiment.ExperimentError):
            experiment.prepare_experiment([row], os.path.join(self.temp.name, "bad-packet"))

    def test_unmatured_labels_are_excluded_from_offline_scoring(self):
        row = episode()
        row["decision_time"] = 4102444800
        row["label_end"] = 4102531200
        self.prepare([row])
        experiment.execute_experiment(self.state, transport=lambda payload: completion())
        score = experiment.score_experiment(self.state, [row])
        self.assertEqual(score["paired_cases"], 0)
        self.assertEqual(score["excluded_outcomes"], 1)

    def test_stability_disagreement_is_reported_without_increasing_market_count(self):
        self.prepare(stability_repeats=1)
        responses = iter([completion(), completion(),
                          completion({"down": .7, "flat": .2, "up": .1}), completion()])
        experiment.execute_experiment(self.state, transport=lambda payload: next(responses))
        score = experiment.score_experiment(self.state, [episode()])
        self.assertEqual(score["paired_cases"], 1)
        self.assertAlmostEqual(score["stability"]["raw_history"]["mean_probability_l1"], 1.2)
        self.assertEqual(score["stability"]["structured_packet"]["mean_probability_l1"], 0)

    def test_mixed_currency_cost_bands_and_same_asset_overlaps_are_refused(self):
        for field, value in (("return_currency", "IRT"), ("cost_band", .01),
                             ("decision_time", 1700003600)):
            other = episode("different", 86400)
            other[field] = value
            if field == "decision_time":
                other["label_end"] = value + 86400
            with self.subTest(field=field), self.assertRaises(experiment.ExperimentError):
                experiment.prepare_experiment([episode(), other], os.path.join(self.temp.name, field))

    def test_explicit_roster_cannot_be_silently_truncated(self):
        with self.assertRaises(experiment.ExperimentError):
            self.prepare([episode(), episode("other", 86400)],
                         case_ids=[episode()["episode_id"], "other"], max_cases=1)

    def test_inconsistent_label_return_and_duplicate_scoring_identity_are_refused(self):
        self.prepare()
        experiment.execute_experiment(self.state, transport=lambda payload: completion())
        changed = episode()
        changed["realized_return"] = -.1
        for name, rows in (("inconsistent_return", [changed]), ("duplicate_identity", [episode(), episode()])):
            with self.subTest(case=name), self.assertRaises(experiment.ExperimentError):
                experiment.score_experiment(self.state, rows)

    def test_owner_attempt_without_valid_forecast_is_not_reported_as_fresh_results(self):
        manifest = self.prepare()
        ledger = {"mode": "owner", "records": [{"status": "unknown"}],
                  "reserved_micro_usd": manifest["requests"][0]["reserve_micro_usd"], "halted": True}
        with patch.object(os, "environ", EnvironmentBomb()):
            summary = experiment._summary(manifest, ledger)
        self.assertEqual(summary["fresh_status"], "not_run")
        self.assertEqual(summary["attempted_calls"], 1)
        self.assertGreater(summary["reserved_usd"], 0)

    def test_process_lock_prevents_another_process_from_preparing_state(self):
        self.prepare()
        with experiment.StateLock(self.state):
            command = [sys.executable, "-c", "from bitpin.kimi_experiment import load_manifest; "
                       "load_manifest(%r)" % self.state]
            child = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(child.returncode, 0)
        self.assertIn(b"locked", child.stderr.lower())

    def test_fixture_scoring_is_free_paired_and_matches_hand_calculated_scores(self):
        self.prepare()
        experiment.execute_experiment(self.state, transport=lambda payload: completion())
        with patch.object(os, "environ", EnvironmentBomb()):
            score = experiment.score_experiment(self.state, [episode()])
        self.assertEqual(score["paired_cases"], 1)
        self.assertEqual(score["status"], "fixture_only")
        self.assertAlmostEqual(score["arms"]["raw_history"]["brier"], .14)
        self.assertAlmostEqual(score["arms"]["raw_history"]["log_loss"], .35667494393873245)
        self.assertEqual(score["arms"]["raw_history"]["accuracy"], 1)
        self.assertEqual(score["paired_difference"]["brier_structured_minus_raw"], 0)
        changed = episode()
        changed["features"]["return_1"] = .9
        with self.assertRaises(experiment.ExperimentError):
            experiment.score_experiment(self.state, [changed])

    def test_cli_prepare_score_are_offline_and_execute_without_flag_is_refused(self):
        episodes = os.path.join(self.temp.name, "episodes.json")
        with open(episodes, "w", encoding="utf-8") as f:
            json.dump([episode()], f)
        script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "kimi_strategy_experiment.py")
        result = subprocess.run([sys.executable, script, "prepare", "--episodes", episodes,
                                 "--state-dir", self.state], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["fresh_status"], "not_run")
        result = subprocess.run([sys.executable, script, "execute", "--state-dir", self.state],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"owner-execute", result.stderr)


if __name__ == "__main__":
    unittest.main()
