"""Capital policy snapshots and settings transitions; synthetic files, no account calls."""
import importlib.util
import contextlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SPEC = importlib.util.spec_from_file_location("capital_panel_helper", os.path.join(ROOT, "scripts", "panel_helper.py"))
ph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ph)
logging.getLogger("bitpin.panel_helper").setLevel(logging.CRITICAL)


class CapitalHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capital_helper_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name in ("etc", "state", "notify", "backups"):
            os.mkdir(os.path.join(self.tmp, name))
        for name in ("config", "kimi"):
            shutil.copy(os.path.join(ROOT, name + ".example.json"), self.path("etc", name + ".json"))
        self.bot_state = "inactive"
        self.service_status_rc = 0
        self.calls = []
        self.worker_output = None
        self.h = ph.Helper(ph.Paths(app_dir=ROOT, etc_dir=self.path("etc"), state_dir=self.path("state"),
                                    notify_state_dir=self.path("notify"), backup_dir=self.path("backups"),
                                    lock_file=self.path("helper.lock"), state_in_process=True),
                           run=self.run_command, clock=lambda: 1800000000)
        self.write("live_orders.json", {"orders": {}})
        self.write("runner_state_live.json", {})

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def run_command(self, argv, timeout, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["systemctl", "show"]:
            return self.service_status_rc, "ActiveState=%s\nLoadState=loaded\n" % self.bot_state
        if argv[:1] == ["runuser"]:
            return 0, self.worker_output or "{}"
        if argv[1:] == ["confirm-live", "--check"]:
            return 1, "settings changed"
        raise AssertionError("unexpected external operation %r" % argv)

    def write(self, filename, doc):
        with open(self.path("state", filename), "w", encoding="utf-8") as file:
            json.dump(doc, file)

    def config(self, **changes):
        with open(self.path("etc", "config.json"), encoding="utf-8") as file:
            doc = json.load(file)
        doc.update(changes)
        return doc

    def save_config(self, doc):
        with open(self.path("etc", "config.json"), "w", encoding="utf-8") as file:
            json.dump(doc, file)

    def call(self, command, **args):
        response = self.h.handle({"cmd": command, "args": args})
        self.assertTrue(response["ok"], response)
        return response["data"]

    def config_put(self, doc, **args):
        return self.call("config_put", file="config", text=json.dumps(doc), **args)

    def snapshot(self, policy=None):
        from bitpin.capital import policy_fingerprint
        cap, reinvest, protected = policy or (None, True, {})
        return {"capital_policy": policy_fingerprint(cap, reinvest, protected), "capital_status": {
            "budget_irt": cap, "managed_equity_irt": "80000", "cash_irt": "80000",
            "profit_reserve_irt": "0", "spendable_cash_irt": "80000", "units": {},
            "protected_assets": protected, "pending": False, "reason": None, "updated_at": 1799999999}}

    def test_status_preserves_unknown_values_instead_of_zero_balances(self):
        data = self.call("capital_status")
        self.assertEqual(data["configured"], {"max_equity_irt": None, "reinvest_profits": True,
                                              "protected_assets": {}})
        self.assertEqual(data["account"], {"balances": None, "prices_irt": None, "updated_at": None})
        self.assertIsNone(data["active"]["managed_equity_irt"])
        self.assertIsNone(data["active"]["cash_irt"])
        self.assertIn("external_reserve_irt", data["active"])
        self.assertIsNone(data["active"]["external_reserve_irt"])
        self.assertTrue(data["active"]["pending"])
        self.assertEqual(data["orders"], {"active_count": 0, "unknown_count": 0})

    def test_status_projects_cached_account_and_capital_without_network(self):
        self.save_config(self.config(max_equity_irt=80000, reinvest_profits=False,
                                     protected_assets={"BTC": "0.003"}))
        from bitpin.capital import policy_fingerprint
        self.write("runner_state_live.json", {"capital_policy": policy_fingerprint(80000, False, {"BTC": "0.003"}),
            "capital_status": {
            "budget_irt": "80000", "managed_equity_irt": "84000", "cash_irt": "60000",
            "profit_reserve_irt": "4000", "spendable_cash_irt": "56000", "units": {"USDT": "0.24"},
            "protected_assets": {"BTC": "0.003"}, "pending": False, "reason": None,
            "updated_at": 1799990000}, "account_snapshot": {"balances": {"IRT": "150000", "BTC": "0.006"},
                "prices_irt": {"BTC": "5000000"}, "updated_at": 1799990000}, "credential": "DO_NOT_PROJECT"})
        data = self.call("capital_status")
        self.assertEqual(data["active"]["profit_reserve_irt"], "4000")
        self.assertEqual(data["active"]["units"], {"USDT": "0.24"})
        self.assertFalse(data["active"]["pending"])
        self.assertEqual(data["account"]["balances"], {"IRT": "150000", "BTC": "0.006"})
        self.assertEqual(data["account"]["updated_at"], 1799990000)
        self.assertTrue(any("cached" in warning.lower() for warning in data["warnings"]))
        self.assertNotIn("DO_NOT_PROJECT", json.dumps(data))
        self.assertEqual(self.calls, [])

    def test_external_irt_reserve_is_projected_as_exact_cached_amount(self):
        state = self.snapshot()
        state["capital_status"]["external_reserve_irt"] = "5000.125"
        self.write("runner_state_live.json", state)
        data = self.call("capital_status")
        self.assertEqual(data["active"].get("external_reserve_irt"), "5000.125")

    def test_saved_active_policy_becomes_pending_after_config_changes(self):
        self.write("runner_state_live.json", self.snapshot())
        self.assertFalse(self.call("capital_status")["active"]["pending"])
        self.config_put(self.config(max_equity_irt=80000))
        data = self.call("capital_status")
        self.assertTrue(data["active"]["pending"])
        self.assertIn("not been activated", data["active"]["reason"])

    def test_old_sleeve_is_visible_without_inventing_profit_reserve_or_account(self):
        self.write("runner_state_live.json", {"sleeve": {"budget": "80000", "cash": "25000",
                                                        "units": {"USDT_IRT": "0.5"}},
                                              "last_cycle": {"equity_after": "82000"}, "updated_at": 1799999999})
        data = self.call("capital_status")
        self.assertEqual(data["active"]["budget_irt"], "80000")
        self.assertEqual(data["active"]["managed_equity_irt"], "82000")
        self.assertEqual(data["active"]["units"], {"USDT": "0.5"})
        self.assertIsNone(data["active"]["profit_reserve_irt"])
        self.assertIsNone(data["active"]["spendable_cash_irt"])
        self.assertIsNone(data["account"]["balances"])
        self.assertTrue(data["active"]["pending"])

    def test_resting_and_uncertain_orders_prevent_claiming_protection_active(self):
        self.write("runner_state_live.json", {"capital_status": {"pending": False, "updated_at": 1799999999}})
        self.write("live_orders.json", {"orders": {
            "resting": {"kind": "limit", "status": "resting", "limit_state": "open"},
            "open": {"kind": "market", "status": "open"},
            "unknown": {"kind": "market", "status": "unknown"},
            "submitting": {"kind": "limit", "status": "submitting"},
            "closed": {"kind": "limit", "status": "closed", "limit_state": "filled"}}})
        data = self.call("capital_status")
        self.assertEqual(data["orders"], {"active_count": 1, "unknown_count": 3})
        self.assertTrue(data["active"]["pending"])
        self.assertIn("orders", data["active"]["reason"])

    def test_orders_placed_under_matching_active_policy_do_not_make_it_pending(self):
        self.write("runner_state_live.json", self.snapshot())
        self.write("live_orders.json", {"orders": {
            "resting": {"kind": "limit", "status": "resting", "limit_state": "open"}}})
        data = self.call("capital_status")
        self.assertEqual(data["orders"], {"active_count": 1, "unknown_count": 0})
        self.assertFalse(data["active"]["pending"])
        transition = self.config_put(self.config(max_equity_irt=80000))
        self.assertFalse(transition["written"])
        self.assertTrue(any("orders" in problem for problem in transition["problems"]))

    def test_unknown_valuation_is_null_while_known_matching_policy_stays_active(self):
        state = self.snapshot()
        state["capital_status"]["managed_equity_irt"] = None
        self.write("runner_state_live.json", state)
        data = self.call("capital_status")
        self.assertIsNone(data["active"]["managed_equity_irt"])
        self.assertFalse(data["active"]["pending"])

    def test_json_editor_cannot_change_capital_while_bot_is_running(self):
        self.bot_state = "active"
        data = self.config_put(self.config(max_equity_irt=80000))
        self.assertFalse(data["written"])
        self.assertTrue(any("stopped" in problem for problem in data["problems"]), data)
        self.assertIsNone(self.config()["max_equity_irt"])
        self.assertEqual(os.listdir(self.path("backups")), [])

    def test_structured_settings_cannot_change_capital_while_bot_is_running(self):
        self.bot_state = "active"
        data = self.call("settings_set", changes=[{"file": "config", "path": ["max_equity_irt"],
                                                    "value": 80000}])
        self.assertFalse(data["written"])
        self.assertTrue(any("stopped" in problem for problem in data["problems"]), data)

    def test_failed_service_status_cannot_authorize_capital_change(self):
        self.service_status_rc = 1
        data = self.config_put(self.config(max_equity_irt=80000))
        self.assertFalse(data["written"])
        self.assertTrue(any("stopped" in problem for problem in data["problems"]), data["problems"])

    def test_json_editor_cannot_bypass_unresolved_order_gate(self):
        for status in ("resting", "submitting", "unknown", "open", "unrecognized"):
            with self.subTest(status=status):
                self.write("live_orders.json", {"orders": {"synthetic": {"kind": "limit", "status": status}}})
                data = self.config_put(self.config(max_equity_irt=80000))
                self.assertFalse(data["written"], status)
                self.assertTrue(any("orders" in problem for problem in data["problems"]), data)

    def test_closed_limit_with_active_or_unknown_limit_state_is_not_resolved(self):
        for limit_state in ("open", "partially_filled", "unknown", "invalid_state"):
            with self.subTest(limit_state=limit_state):
                self.write("live_orders.json", {"orders": {"synthetic": {"kind": "limit", "status": "closed",
                                                                        "limit_state": limit_state}}})
                data = self.config_put(self.config(max_equity_irt=80000))
                self.assertFalse(data["written"], data["problems"])

    def test_invalid_or_undated_snapshot_never_claims_active_protection(self):
        for key, value in (("updated_at", None), ("cash_irt", "NaN"), ("units", {"BTC": True}),
                           ("protected_assets", {"BTC": "-1"})):
            with self.subTest(key=key):
                state = self.snapshot()
                state["capital_status"][key] = value
                self.write("runner_state_live.json", state)
                data = self.call("capital_status")
                self.assertTrue(data["active"]["pending"])

    def test_reinvestment_and_protected_asset_changes_share_json_editor_gate(self):
        self.save_config(self.config(max_equity_irt=80000))
        self.bot_state = "active"
        for changes in ({"reinvest_profits": False}, {"protected_assets": {"BTC": "0.003"}}):
            with self.subTest(changes=changes):
                data = self.config_put(self.config(**changes))
                self.assertFalse(data["written"])
                self.assertTrue(any("stopped" in problem for problem in data["problems"]), data["problems"])

    def test_reinvestment_and_asset_changes_share_structured_settings_gate(self):
        self.save_config(self.config(max_equity_irt=80000))
        self.write("live_orders.json", {"orders": {"unknown": {"kind": "market", "status": "unknown"}}})
        for key, value in (("reinvest_profits", False), ("protected_assets", {"BTC": "0.003"})):
            with self.subTest(key=key):
                data = self.call("settings_set", changes=[{"file": "config", "path": [key], "value": value}])
                self.assertFalse(data["written"])
                self.assertTrue(any("orders" in problem for problem in data["problems"]), data["problems"])

    def test_stopped_bot_with_resolved_orders_can_write_pending_policy(self):
        self.write("live_orders.json", {"orders": {"done": {"kind": "market", "status": "closed"}}})
        data = self.config_put(self.config(max_equity_irt=80000))
        self.assertTrue(data["written"], data)
        self.assertTrue(data["confirm_needed"])
        self.assertTrue(data["backup"])
        status = self.call("capital_status")
        self.assertEqual(status["configured"]["max_equity_irt"], 80000)
        self.assertTrue(status["active"]["pending"])

    def test_corrupt_or_missing_order_journal_fails_closed(self):
        for content in (None, "{", '{"orders":[]}', '{"orders":{"bad":null}}',
                        '{"orders":{},"orders":{}}', '{"orders":{}}\xff'):
            with self.subTest(content=content):
                path = self.path("state", "live_orders.json")
                if content is None:
                    os.remove(path)
                else:
                    with open(path, "wb") as file:
                        file.write(content.encode("latin1"))
                data = self.config_put(self.config(max_equity_irt=80000))
                self.assertFalse(data["written"], data)
                self.assertTrue(any("orders" in problem for problem in data["problems"]), data)

    def test_corrupt_runner_state_and_oversize_journal_fail_closed(self):
        with open(self.path("state", "runner_state_live.json"), "w") as file:
            file.write("{")
        data = self.config_put(self.config(max_equity_irt=80000))
        self.assertFalse(data["written"], data)
        self.assertTrue(data["problems"])
        self.write("runner_state_live.json", {})
        with open(self.path("state", "live_orders.json"), "w") as file:
            file.write('{"orders":{},"padding":"' + "x" * (4 * 1024 * 1024) + '"}')
        data = self.config_put(self.config(max_equity_irt=80000))
        self.assertFalse(data["written"], data)
        self.assertTrue(any("orders" in problem for problem in data["problems"]), data)

    def test_nonstandard_json_numbers_are_unavailable(self):
        baseline = self.config()
        for content in ('{"orders":{},"extra":NaN}', '{"orders":{},"extra":Infinity}'):
            with self.subTest(kind=content[:40]):
                self.save_config(baseline)
                with open(self.path("state", "live_orders.json"), "w") as file:
                    file.write(content)
                data = self.config_put(self.config(max_equity_irt=80000))
                self.assertFalse(data["written"])
                self.assertTrue(data["problems"])
                status = self.call("capital_status")
                self.assertIsNone(status["orders"]["active_count"])

    def test_unrelated_setting_and_explicit_defaults_need_no_capital_gate(self):
        self.bot_state = "active"
        os.remove(self.path("state", "live_orders.json"))
        doc = self.config(reinvest_profits=True, protected_assets={})
        doc["risk"]["max_drawdown"] = 0.4
        data = self.config_put(doc)
        self.assertTrue(data["written"], data)
        self.assertEqual(data["problems"], [])
        self.assertFalse(any("whole account" in warning.lower() for warning in data["warnings"]))

    def test_removing_budget_warns_about_whole_account_authority(self):
        self.save_config(self.config(max_equity_irt=80000))
        data = self.config_put(self.config(max_equity_irt=None), dry_run=True)
        self.assertFalse(data["written"])
        self.assertTrue(any("whole account" in warning.lower() for warning in data["warnings"]), data)

    def test_worker_failure_cannot_be_treated_as_zero_orders(self):
        self.h.p.state_in_process = False
        self.worker_output = '{"orders":{"active_count":0,"unknown_count":0}}'
        data = self.config_put(self.config(max_equity_irt=80000))
        self.assertFalse(data["written"], data)
        self.assertTrue(data["problems"])

    def test_worker_reads_only_the_fixed_state_files_and_refuses_symlink(self):
        target = self.path("other.json")
        with open(target, "w") as file:
            file.write('{"orders":{}}')
        os.remove(self.path("state", "live_orders.json"))
        try:
            os.symlink(target, self.path("state", "live_orders.json"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        data = self.config_put(self.config(max_equity_irt=80000))
        self.assertFalse(data["written"])
        self.assertIsNone(self.call("capital_status")["orders"]["active_count"])

    def test_worker_entry_point_returns_cached_projection(self):
        self.write("runner_state_live.json", self.snapshot())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = ph.main(["capital-worker", "--state-dir", self.path("state")])
        self.assertEqual(result, 0)
        state = json.loads(output.getvalue())
        self.assertEqual(state["orders"], {"active_count": 0, "unknown_count": 0})
        self.assertEqual(state["active"]["cash_irt"], "80000")

    def test_privilege_dropped_worker_is_used_when_not_reading_in_process(self):
        self.write("runner_state_live.json", self.snapshot())
        self.worker_output = json.dumps(ph.collect_capital_state(self.path("state"), 1800000000))
        self.h.p.state_in_process = False
        data = self.call("capital_status")
        self.assertEqual(data["active"]["cash_irt"], "80000")
        self.assertEqual(self.calls[0][:4], ["runuser", "-u", "bitpin", "--"])
        self.assertIn("capital-worker", self.calls[0])

    def test_worker_preserves_valid_account_when_order_journal_is_invalid(self):
        state = self.snapshot()
        state["account_snapshot"] = {"balances": {"IRT": "150000"}, "prices_irt": {},
                                     "updated_at": 1799999999}
        self.write("runner_state_live.json", state)
        self.write("live_orders.json", {"orders": []})
        self.worker_output = json.dumps(ph.collect_capital_state(self.path("state"), 1800000000))
        self.h.p.state_in_process = False
        data = self.call("capital_status")
        self.assertEqual(data["account"]["balances"], {"IRT": "150000"})
        self.assertIsNone(data["orders"]["active_count"])
        self.assertTrue(data["active"]["pending"])

    def test_capital_status_rejects_caller_selected_paths(self):
        response = self.h.handle({"cmd": "capital_status", "args": {"state_dir": self.path("etc")}})
        self.assertFalse(response["ok"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
