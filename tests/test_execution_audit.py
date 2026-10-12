"""Execution receipts from real runner/broker paths on a synthetic exchange only."""
import os
from decimal import Decimal

from bitpin.api import BitpinAPIError, OrderNotSent
from bitpin.broker import LiveBroker
from bitpin.risk import RiskManager
from bitpin.runner import PlannedOrder, Runner
import test_integration as ti
from test_runner import FakeLiveClient


class ExecutionAudit(ti.BrainRunnerBase):
    def runner(self, dry_run=False, cfg=None, risk=None, client=None):
        self.brain.origin = "live"
        client = client or FakeLiveClient(self.ex, {"IRT": ti.CAPITAL, "USDT": "0", "BTC": "0", "ETH": "0"})
        self.client = client
        broker = LiveBroker(client, ti.MARKETS, self.dir, min_order_irt=100000,
                            clock=self.clock, sleep=self.clock.sleep)
        rm = RiskManager(dict({"min_order_irt": 100000}, **(risk or {})), self.dir, "live", clock=self.clock,
                         persist=not dry_run)
        config = dict(ti.RUNNER_CFG)
        config.update(cfg or {})
        return Runner(None, broker, rm, config, self.dir, "live", dry_run=dry_run,
                      bars_source=self.ex.bars, clock=self.clock, sleep=self.clock.sleep,
                      brain=self.brain, context_builder=self.builder)

    def propose(self, **kw):
        d = ti.FakeDecision(targets={"USDT_IRT": 0.8, "BTC_IRT": 0.2}, **kw)
        d.mode = "scheduled"
        self.brain.next = [d]
        return d

    def receipt(self):
        rec = ti.runner_log(self.dir)[-1]
        self.assertIn("execution", rec, "A completed runner cycle needs a linked execution receipt")
        return rec, rec["execution"]

    def test_actual_fill_receipt_retains_status_amounts_and_exact_identity(self):
        self.propose()
        rep = self.runner().run_once()
        rec, receipt = self.receipt()
        self.assertEqual(receipt["decision_ref"], {"decided_at": ti.NOW, "origin": "live"})
        self.assertEqual(receipt["runtime_mode"], "live")
        self.assertEqual(rec.get("runtime_mode"), "live")  # scheduled is a brain mode, not a money mode
        self.assertEqual(rec["decision"].get("origin"), "live")
        self.assertEqual(len(receipt["orders"]), 2)
        for order in receipt["orders"]:
            self.assertEqual(order["status"], "filled")
            self.assertGreater(Decimal(order["base"]), 0)
            self.assertGreater(Decimal(order["quote"]), 0)
            self.assertGreater(Decimal(order["requested"]), 0)
            self.assertGreater(Decimal(order["avg_price"]), 0)
            self.assertTrue(order["identifier"])
        self.assertEqual({f["identifier"] for f in rec["fills"]},
                         {f["identifier"] for f in rep["fills"]})
        self.assertEqual({f["status"] for f in rec["fills"]}, {"filled"})

    def test_final_zero_fill_remains_unfilled_in_receipt_and_legacy_audit(self):
        self.propose()
        r = self.runner()

        def unfilled(symbol, side, type="market", **kw):
            return {"id": "zero-fill", "symbol": symbol, "side": side, "identifier": kw["identifier"],
                    "state": "closed", "dealed_base_amount": "0", "dealed_quote_amount": "0", "commission": "0"}

        self.client.place_order = unfilled
        r.run_once()
        rec, receipt = self.receipt()
        self.assertEqual({f["status"] for f in receipt["orders"]}, {"unfilled"})
        self.assertEqual({Decimal(f["base"]) for f in receipt["orders"]}, {Decimal(0)})
        self.assertEqual({f["status"] for f in rec["fills"]}, {"unfilled"})

    def test_unknown_submission_identifier_survives_without_claiming_a_fill(self):
        self.propose()
        r = self.runner()
        self.client.post_timeout = True
        r.run_once()
        _, receipt = self.receipt()
        self.assertEqual(len(receipt["orders"]), 1)
        order = receipt["orders"][0]
        self.assertEqual(order["status"], "unknown")
        self.assertIsNone(order["base"])
        self.assertEqual(r.broker.journal.get(order["identifier"])["status"], "unknown")

    def test_not_sent_attempt_and_restarted_retry_share_exact_decision_reference(self):
        self.propose()
        r = self.runner()
        original = self.client.place_order

        def not_sent(*args, **kw):
            raise OrderNotSent(kw["identifier"], "synthetic throttle")

        self.client.place_order = not_sent
        r.run_once()
        _, first = self.receipt()
        self.assertEqual({o["status"] for o in first["orders"]}, {"not_sent"})
        self.assertTrue(all(o["identifier"] for o in first["orders"]))
        self.assertEqual(r.state["brain_active"].get("decision_ref"),
                         {"decided_at": ti.NOW, "origin": "live"})
        self.client.place_order = original
        self.clock.t += 6
        self.brain.due = False
        self.runner(client=self.client).run_once()
        rec, retried = self.receipt()
        self.assertEqual(retried["decision_ref"], {"decided_at": ti.NOW, "origin": "live"})
        self.assertEqual(rec["decision"].get("origin"), "live")
        self.assertEqual({o["status"] for o in retried["orders"]}, {"filled"})

    def test_definite_api_rejection_captures_generated_local_journal_identifier(self):
        self.propose()
        r = self.runner()

        def rejected(*args, **kw):
            raise BitpinAPIError(400, "synthetic rejection", {"code": "invalid"})

        self.client.place_order = rejected
        r.run_once()
        _, receipt = self.receipt()
        self.assertEqual({o["status"] for o in receipt["orders"]}, {"rejected"})
        for order in receipt["orders"]:
            self.assertEqual(r.broker.journal.get(order["identifier"])["status"], "rejected")

    def test_budget_block_has_no_orders_but_records_the_actual_skip(self):
        self.propose()
        self.runner(risk={"max_orders_per_day": 1}).run_once()
        _, receipt = self.receipt()
        self.assertEqual(receipt["orders"], [])
        self.assertIn("daily order budget", receipt["skipped"][0][1])
        self.assertEqual(receipt["effective_targets"], {"USDT_IRT": 0.8, "BTC_IRT": 0.2})

    def test_receipt_explains_a_planned_leg_below_the_rebalance_threshold(self):
        self.brain.next = [ti.FakeDecision(targets={"USDT_IRT": 0.99, "BTC_IRT": 0.01})]
        self.runner().run_once()
        _, receipt = self.receipt()
        self.assertEqual([o["symbol"] for o in receipt["orders"]], ["USDT_IRT"])
        self.assertTrue(any(s == "BTC_IRT" and "rebalance threshold" in why for s, why in receipt["skipped"]))

    def test_risk_refusal_is_a_skip_with_no_submission(self):
        self.propose()
        self.runner(risk={"max_order_fraction": 0.00001}).run_once()
        _, receipt = self.receipt()
        self.assertEqual(receipt["orders"], [])
        self.assertEqual(len(receipt["skipped"]), 2)
        self.assertTrue(all("below minimum order" in why for _, why in receipt["skipped"]))

    def test_stale_decision_gets_a_no_order_receipt_with_exact_reference(self):
        self.propose(computed_against={"USDT_IRT": 0.5})
        self.runner().run_once()
        _, receipt = self.receipt()
        self.assertEqual(receipt["decision_ref"], {"decided_at": ti.NOW, "origin": "live"})
        self.assertEqual(receipt["action"], "stale")
        self.assertEqual(receipt["orders"], [])
        self.assertIn("portfolio weights moved", receipt["skipped"][0][1])

    def test_foreign_decision_retains_foreign_origin_in_live_receipt(self):
        self.propose(origin="paper")
        self.runner().run_once()
        _, receipt = self.receipt()
        self.assertEqual(receipt["decision_ref"], {"decided_at": ti.NOW, "origin": "paper"})
        self.assertEqual(receipt["runtime_mode"], "live")
        self.assertEqual(receipt["action"], "foreign")
        self.assertEqual(receipt["orders"], [])

    def test_paper_receipt_preserves_its_simulated_money_mode(self):
        self.propose()
        self.brain.origin = "paper"
        ti.BrainRunnerBase.runner(self).run_once()
        _, receipt = self.receipt()
        self.assertEqual(receipt["runtime_mode"], "paper")
        self.assertEqual(receipt["decision_ref"], {"decided_at": ti.NOW, "origin": "paper"})
        self.assertEqual({o["status"] for o in receipt["orders"]}, {"filled"})

    def test_dry_run_receipt_cannot_be_mistaken_for_live_execution(self):
        self.propose()
        rep = self.runner(dry_run=True).run_once()
        self.assertIn("execution", rep)
        self.assertEqual(rep["execution"]["runtime_mode"], "dry_run")
        self.assertEqual(rep["execution"]["orders"], [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "live_orders.json")))

    def test_hold_receipt_excludes_order_sent_by_later_maintenance(self):
        self.propose(hold=True)
        r = self.runner()

        def maintenance(rep, now, equity, syms):
            o = PlannedOrder("USDT_IRT", "buy", Decimal("1000000"), Decimal("1000000"), Decimal(0), Decimal(0),
                             reason="target")
            r._place(o, "buy", o.amount, {"USDT_IRT": self.ex.prices["USDT_IRT"]}, equity, rep)

        r._maintain = maintenance
        rep = r.run_once()
        _, receipt = self.receipt()
        self.assertEqual(len(rep["fills"]), 1)
        self.assertEqual(receipt["action"], "hold")
        self.assertEqual(receipt["orders"], [])

    def test_reporting_journal_failure_does_not_prevent_actual_order_execution(self):
        self.propose()
        r = self.runner()
        original = r.broker.journal.items

        def broken_inventory():
            raise OSError("synthetic audit inventory failure")

        r.broker.journal.items = broken_inventory
        rep = r.run_once()
        r.broker.journal.items = original
        self.assertEqual(len(rep["fills"]), 2)
        _, receipt = self.receipt()
        self.assertEqual({o["status"] for o in receipt["orders"]}, {"filled"})
