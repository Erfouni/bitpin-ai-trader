"""Capital limits on real runner/paper-broker paths, with synthetic funds only."""
from decimal import Decimal as D
from unittest import mock

from test_runner import RunnerBase, FixedWeights
from test_ladder import Base as LadderBase, decision
from bitpin.risk import KILL_SWITCH_FILE
import os
from bitpin.runner import RunnerError, PlannedOrder, load_config


class CapitalRunnerTest(RunnerBase):
    def test_fixed_requires_positive_cap(self):
        with self.assertRaises(RunnerError):
            load_config(overrides={"reinvest_profits": False})

    def test_whole_asset_floor_excludes_holdings(self):
        r = self.runner(FixedWeights(), cfg={"protected_assets": {"AAA": "200", "IRT": "90000000"}})
        r.broker._bal["AAA"] = D(200)
        units, cash = r._holdings(["AAA_IRT"])
        self.assertEqual(units["AAA_IRT"], 0)
        self.assertEqual(cash, D(10000000))

    def test_stale_sell_cannot_sell_protected_quantity(self):
        r = self.runner(FixedWeights(), cfg={"protected_assets": {"AAA": "150"}})
        r.broker._bal["AAA"] = D(200)
        o = PlannedOrder("AAA_IRT", "sell", D(200), D(20000), D(0), D(1))
        rep = {"fills": [], "errors": [], "skipped": []}
        with mock.patch.object(r, "_vet", wraps=r._vet) as vet:
            r._place(o, "sell", D(200), {"AAA_IRT": 100}, D(100000000), rep)
        self.assertLessEqual(vet.call_args[0][2], D(50))
        self.assertGreaterEqual(r.broker.balances()["AAA"], D(150))

    def test_locked_quote_is_not_buying_power(self):
        r = self.runner(FixedWeights(), cfg={"protected_assets": {"USDT": "75"}})
        r.broker._bal["USDT"] = D(100)
        self.assertEqual(r._capital_available("USDT"), D(25))

    def test_reserve_is_partition_and_loss_does_not_refill(self):
        cfg = {"max_equity_irt": "5000000", "reinvest_profits": False}
        r = self.runner(FixedWeights(a=0,b=0), cfg=cfg)
        sl = r._sleeve_get()
        sl["cash"] = D(7000000)
        units, cash = r._holdings(["AAA_IRT"])
        self.assertEqual(cash, D(5000000))
        self.assertEqual(sl["profit_reserve_irt"], D(2000000))
        self.assertEqual(sl["cash"], D(7000000))
        sl["cash"] = D(6000000)
        r._save_sleeve()
        r2 = self.runner(FixedWeights(a=0,b=0), cfg=cfg)
        self.assertEqual(r2._holdings(["AAA_IRT"])[1], D(4000000))
        self.assertEqual(r2._capital_available("IRT"), D(4000000))

    def test_reinvestment_releases_prior_reserved_profit(self):
        r = self.runner(FixedWeights(), cfg={"max_equity_irt": "5000000"})
        sl = r._sleeve_get()
        sl["cash"], sl["profit_reserve_irt"] = D(6000000), D(1000000)
        self.assertEqual(r._holdings(["AAA_IRT"])[1], D(6000000))
        self.assertEqual(sl["profit_reserve_irt"], 0)

    def test_policy_change_blocks_prior_unknown_order(self):
        r = self.runner(FixedWeights(), cfg={"protected_assets": {"USDT": "100"}})
        with mock.patch.object(r.broker, "unresolved_count", return_value=1):
            rep = r.run_once()
        self.assertEqual(rep["status"], "capital_policy_pending")
        self.assertEqual(rep["fills"], [])
        self.assertTrue(r.state["capital_status"]["pending"])

    def test_resting_order_cannot_use_locked_quote(self):
        r = self.runner(FixedWeights(), cfg={"protected_assets": {"IRT": "100000000"}})
        rep = {"errors": []}
        result = r._place_resting(rep, "AAA_IRT", "buy", D(90), D(2000), "ladder", {}, None,
                                  equity=D(100000000))
        self.assertIsNone(result)
        self.assertEqual(r.broker.limit_orders(active_only=True), [])

    def test_fixed_plan_trims_appreciation_and_reinvest_plan_keeps_it(self):
        units, prices = {"AAA_IRT": D(100000)}, {"AAA_IRT": D(100)}
        args = ({"AAA_IRT": D(1)}, units, prices, D(0), D(".02"), D(".0035"), None)
        fixed = self.runner(FixedWeights(), cfg={"max_equity_irt": "5000000", "reinvest_profits": False})
        self.assertEqual(fixed._capital_plan(*args).sells[0].amount, D(50000))
        compound = self.runner(FixedWeights(), cfg={"max_equity_irt": "5000000"})
        self.assertEqual(compound._capital_plan(*args).orders, [])

    def test_current_weights_for_kimi_stay_normalized_in_fixed_mode(self):
        r = self.runner(FixedWeights(), cfg={"max_equity_irt": "5000000", "reinvest_profits": False})
        hold, plan = r._book(["AAA_IRT"], {"AAA_IRT": D(100)}, {"AAA_IRT": D(100000)}, D(0))
        self.assertEqual(hold, {"AAA_IRT": D(1)})
        self.assertEqual(plan.orders, [])

    def test_policy_change_cancels_the_previous_resting_order_then_activates(self):
        # v3.16.1: before, one resting order of the old policy kept the gate pending for good (no sync ran
        # ahead of the gate), and with it every code stop, hold exit and Kimi decision
        r = self.runner(FixedWeights())
        r.broker.place_limit("AAA_IRT", "buy", D(90), D(2000))
        protected = self.runner(FixedWeights(), cfg={"protected_assets": {"IRT": "100000000"}})
        rep = protected.run_once()
        self.assertNotEqual(rep["status"], "capital_policy_pending")
        self.assertEqual(protected.broker.limit_orders(active_only=True), [])
        self.assertEqual(protected.state["capital_policy"], protected._capital_fingerprint)
        self.assertFalse((protected.state.get("capital_status") or {}).get("pending"))

    def test_a_failed_cancel_stays_pending_and_is_retried_next_cycle(self):
        r = self.runner(FixedWeights())
        r.broker.place_limit("AAA_IRT", "buy", D(90), D(2000))
        protected = self.runner(FixedWeights(), cfg={"protected_assets": {"IRT": "100000000"}})
        with mock.patch.object(protected.broker, "cancel", side_effect=RuntimeError("network down")):
            self.assertEqual(protected.run_once()["status"], "capital_policy_pending")
        self.assertNotEqual(protected.run_once()["status"], "capital_policy_pending")
        self.assertEqual(protected.broker.limit_orders(active_only=True), [])

    def test_status_free_cash_excludes_own_limit_commitment(self):
        r = self.runner(FixedWeights(), cfg={"max_equity_irt": "5000000"})
        r._sleeve_get()
        r.broker.place_limit("AAA_IRT", "buy", D(90), D(2000))
        self.assertEqual(r._holdings(["AAA_IRT"])[1], D(5000000))
        self.assertEqual(r.state["capital_status"]["spendable_cash_irt"], D(4820000))
        self.assertEqual(r._capital_available("IRT"), D(4820000))

    def test_limit_quote_rounding_cannot_cross_protected_floor(self):
        floor = D("99899999.75")
        r = self.runner(FixedWeights(), cfg={"protected_assets": {"IRT": str(floor)}})
        rep = {"errors": []}
        r._place_resting(rep, "AAA_IRT", "buy", D(99), D("1010.1035"), "ladder", {}, None,
                         equity=D(100000000))
        self.assertGreaterEqual(r.broker.available()["IRT"], floor)
        self.assertEqual(r.broker.limit_orders(active_only=True), [])

    def test_profit_sweep_never_reserves_cash_committed_to_resting_buy(self):
        r = self.runner(FixedWeights(), cfg={"max_equity_irt": "1000", "reinvest_profits": False})
        sl = r._sleeve_get()
        sl["cash"], sl["units"] = D(400), {"AAA_IRT": D(6)}
        r.broker._bal["IRT"], r.broker._bal["AAA"] = D(400), D(6)
        r.broker.place_limit("BBB_IRT", "buy", D(100), D(4))
        r._last_prices["AAA_IRT"] = D(134)
        r._holdings(["AAA_IRT"])
        self.assertEqual(sl["profit_reserve_irt"], 0)
        self.assertEqual(r._capital_available("IRT"), 0)

    def test_lower_cap_cannot_withdraw_reserved_profit(self):
        r = self.runner(FixedWeights(), cfg={"max_equity_irt": "5000000", "reinvest_profits": False})
        sl = r._sleeve_get()
        sl["cash"], sl["profit_reserve_irt"] = D(2000000), D(1500000)
        r._save_sleeve()
        with self.assertRaises(RunnerError):
            self.runner(FixedWeights(), cfg={"max_equity_irt": "4000000", "reinvest_profits": False})

    def test_unbooked_final_fill_is_reconciled_before_cap_withdrawal(self):
        r = self.runner(FixedWeights(), cfg={"max_equity_irt": "5000000"})
        r._sleeve_get()  # persisted before the simulated crash window
        r.broker.market_buy("AAA_IRT", D(4500000))
        smaller = self.runner(FixedWeights(), cfg={"max_equity_irt": "1000000"})
        with self.assertRaises(RunnerError):
            smaller.run_once()
        self.assertNotEqual(smaller.state.get("capital_policy"), smaller._capital_fingerprint)
        self.assertIsNone(smaller._sleeve)
        for _ in range(2):
            with self.assertRaises(RunnerError):
                smaller.run_once()
            self.assertIsNone(smaller._sleeve)
        self.assertEqual(len(smaller.broker._orders), 1)  # no trades after the refused transition

    def test_real_wallet_shortage_cannot_spend_reserved_profit(self):
        r = self.runner(FixedWeights(), cfg={"max_equity_irt": "1000000", "reinvest_profits": False})
        sl = r._sleeve_get()
        sl["cash"], sl["profit_reserve_irt"] = D(1000000), D(200000)
        r.broker._bal["IRT"] = D(250000)
        self.assertEqual(r._holdings(["AAA_IRT"])[1], D(50000))
        self.assertEqual(r._capital_available("IRT"), D(50000))

    def test_reinvestment_toggle_preserves_loss_history_with_cashflow_adjustment(self):
        r = self.runner(FixedWeights(), cfg={"max_equity_irt": "1000000", "reinvest_profits": False})
        sl = r._sleeve_get()
        sl["cash"], sl["profit_reserve_irt"] = D(1000000), D(200000)
        r._save_sleeve()
        r._holdings(["AAA_IRT"])
        r.risk.update_equity(D(1000000), r._basis())
        self.assertEqual(r.risk.update_equity(D(800000), r._basis())["drawdown"], D(".2"))
        compound = self.runner(FixedWeights(), cfg={"max_equity_irt": "1000000"})
        cash = compound._holdings(["AAA_IRT"])[1]
        dd = compound.risk.update_equity(cash, compound._basis())
        self.assertEqual(dd["hwm"], D(1200000))
        self.assertAlmostEqual(float(dd["drawdown"]), 1.0 / 6.0)


class CapitalBrainTest(LadderBase):
    def test_pending_policy_still_honors_stop_bid_cancellation(self):
        original = self.runner()
        original.run_once()
        self.assertGreater(len(self.bids()), 0)
        prior = original.state["capital_policy"]
        protected = self.runner(cfg={"protected_assets": {"USDT": "10000"}})
        with open(os.path.join(self.dir, KILL_SWITCH_FILE), "w"):
            pass
        report = protected.run_once()
        self.assertEqual(report["status"], "kill_switch")
        self.assertEqual(self.bids(), [])
        self.assertEqual(protected.state["capital_policy"], prior)

    def test_a_policy_change_over_resting_ladder_bids_keeps_the_bot_running(self):
        original = self.runner()
        original.run_once()
        self.assertGreater(len(self.bids()), 0)
        protected = self.runner(cfg={"protected_assets": {"USDT": "1"}})
        report = protected.run_once()
        self.assertNotEqual(report["status"], "capital_policy_pending")
        self.assertEqual(protected.state["capital_policy"], protected._capital_fingerprint)
        self.assertTrue(any("capital policy change" in c for c in (report.get("resting") or {}).get("cancelled", [])),
                        report.get("resting"))

    def test_kimi_decision_receives_explicit_capital_policy(self):
        r = self.runner(cfg={"max_equity_irt": "1000000", "reinvest_profits": False,
                             "protected_assets": {"USDT": "100"}})
        r.broker._bal["IRT"] = D(1000000)
        r.brain.due = True
        r.brain.next = [decision({"USDT_IRT": 1.0})]
        with mock.patch.object(r.brain, "decide", wraps=r.brain.decide) as decide:
            r.run_once()
        self.assertTrue(decide.called)
        policy = decide.call_args[0][0]["capital_policy"]
        self.assertFalse(policy["reinvest_profits"])
        self.assertEqual(policy["base_budget_irt"], "1000000")
        self.assertEqual(policy["protected_assets"], {"USDT": "100"})
