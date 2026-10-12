"""Execution truth and deterministic reporting; fake orders only, no network."""
import copy
import unittest

from bitpin.decision_report import decision_report_fa, execution_view


DECISION = {"decided_at": 1700000000.0, "origin": "test-decision-origin", "valid": True,
            "targets": {"BTC_IRT": 0.3, "USDT_IRT": 0.7}, "cash_irt": 0, "hold": False}


def row(status="filled", base=0.02, **kw):
    r = {"identifier": "test-order", "order_id": 7, "symbol": "BTC_IRT", "side": "buy", "base": base,
         "quote": 200, "requested": 200, "avg_price": 10000, "status": status, "partial": False}
    r.update(kw)
    return r


def receipt(orders=(), mode="live", **kw):
    e = {"schema_version": 1, "decision_ref": {k: DECISION[k] for k in ("decided_at", "origin")},
         "runtime_mode": mode, "action": "kimi", "effective_targets": DECISION["targets"],
         "orders": list(orders), "skipped": [], "errors": []}
    e.update(kw)
    return {"mode": "scheduled", "execution": e}


class TestExecutionTruth(unittest.TestCase):
    def view(self, records, **kw):
        return execution_view(DECISION, records, **kw)

    def test_final_targets_do_not_prove_execution_and_raw_prose_is_ignored(self):
        d = dict(DECISION, report_fa="بیت‌کوین خریده شد", reasoning="bought", hold=True,
                 p_rule={"BTC_IRT": {"reason": "ev"}}, targets={"USDT_IRT": 1})
        report = decision_report_fa(d)
        self.assertNotIn("خریده شد", report)
        self.assertIn("USDT_IRT 100%", report)
        self.assertIn("رد شد", report)
        self.assertIn("تأیید نشده", report)
        self.assertEqual(self.view([])["state"], "unverified")

    def test_empty_audited_cycle_is_no_fill_even_with_ok_status(self):
        rec = dict(receipt(), status="ok", fills=[row()])
        v = self.view([rec])
        self.assertEqual(v["state"], "no_fill")
        self.assertEqual(v["fills"], [])

    def test_zero_final_unknown_and_not_sent_do_not_confirm_buy(self):
        for status, base, state in (("filled", 0, "no_fill"), ("unfilled", 0, "no_fill"),
                                    ("unknown", .02, "pending"), ("submitting", .02, "pending"),
                                    ("not_sent", .02, "no_fill"), ("rejected", .02, "no_fill")):
            v = self.view([receipt([row(status, base)])])
            self.assertEqual((v["state"], v["fills"]), (state, []))
            self.assertNotIn("مقدار اجرای واقعی ثبت‌شده", decision_report_fa(DECISION, v))

    def test_unidentified_ambiguous_attempt_is_pending_without_fill_proof(self):
        for status in ("unknown", "submitting", "open"):
            v = self.view([receipt([row(status, None, identifier=None, order_id=None)])])
            self.assertEqual(v["state"], "pending")
            self.assertEqual((v["orders"], v["pending"], v["fills"]), (1, 1, []))

    def test_known_positive_fills_describe_amount_not_complete_rebalance(self):
        v = self.view([receipt([row()])])
        self.assertEqual(v["state"], "filled")
        self.assertEqual(v["fills"][0]["base"], .02)
        report = decision_report_fa(DECISION, v)
        self.assertIn("خرید BTC_IRT 0.02", report)
        self.assertIn("رسیدن کامل به وزن‌های هدف تأیید نشده", report)

    def test_positive_open_fill_is_partial_and_remainder_pending(self):
        v = self.view([receipt([row("open", .01, quote=100)])])
        self.assertEqual((v["state"], v["pending"]), ("pending", 1))
        self.assertTrue(v["fills"][0]["partial"])
        self.assertIn("هنوز مشخص نیست", decision_report_fa(DECISION, v))

    def test_closed_partial_or_cancelled_fill_preserves_actual_portion(self):
        for status in ("partial", "closed", "cancelled"):
            v = self.view([receipt([row(status, .01, quote=100)])])
            self.assertEqual(v["state"], "partial")
            self.assertEqual(v["fills"][0]["base"], .01)

    def test_exact_timestamp_origin_and_runtime_mode_are_required(self):
        for ref in ({"decided_at": DECISION["decided_at"] + 1, "origin": DECISION["origin"]},
                    {"decided_at": DECISION["decided_at"], "origin": "foreign"},
                    {"decided_at": True, "origin": DECISION["origin"]}):
            self.assertEqual(self.view([receipt([row()], decision_ref=ref)])["state"], "unverified")
        for mode in ("paper", "dry_run"):
            rec = receipt([row()], mode=mode)
            self.assertEqual(self.view([rec])["state"], "unverified")
            v = self.view([rec], mode=mode)
            self.assertEqual(v["state"], "preview")
            self.assertIn("اجرای آزمایشی", decision_report_fa(DECISION, v))

    def test_legacy_or_missing_origin_cannot_supply_fill_proof(self):
        self.assertEqual(self.view([{"decision": DECISION, "fills": [row()], "status": "ok"}])["state"], "unverified")
        self.assertEqual(execution_view(dict(DECISION, origin=""), [receipt([row()])])["state"], "unverified")

    def test_retry_cumulative_receipts_count_identifier_once(self):
        records = [receipt([row("open", .01, quote=100)]), receipt([row("closed", .02)]),
                   receipt([row("closed", .02)])]
        v = self.view(records)
        self.assertEqual((v["state"], v["orders"], len(v["fills"])), ("filled", 1, 1))
        self.assertEqual(v["fills"][0]["base"], .02)

    def test_unknown_later_resolved_from_exact_local_journal_without_api(self):
        rec = receipt([row("unknown", None)])
        journal = {"orders": {"test-order": {"kind": "market", "symbol": "BTC_IRT", "side": "buy",
                                              "status": "closed", "reported_base": ".02", "reported_quote": "200",
                                              "quote_amount": "200", "order_id": 7}}}
        before = copy.deepcopy((rec, journal))
        v = self.view([rec], journal=journal)
        self.assertEqual((v["state"], v["fills"][0]["base"]), ("filled", .02))
        self.assertEqual((rec, journal), before)
        for field, value in (("kind", "limit"), ("side", "sell"), ("symbol", "ETH_IRT")):
            altered = copy.deepcopy(journal)
            altered["orders"]["test-order"][field] = value
            self.assertEqual(self.view([rec], journal=altered)["state"], "pending")

    def test_unrelated_journal_order_and_fill_cannot_be_attributed(self):
        journal = {"orders": {"another-order": dict(row(), reported_base=.02, reported_quote=200)}}
        self.assertEqual(self.view([receipt()], journal=journal)["state"], "no_fill")

    def test_identifier_collision_with_foreign_symbol_is_ignored(self):
        v = self.view([receipt([row(), row(symbol="ETH_IRT", base=999)])])
        self.assertEqual(v["fills"][0]["base"], .02)
        self.assertEqual(len(v["fills"]), 1)

    def test_malformed_numbers_identifiers_and_schema_fail_closed(self):
        for base in (True, "nan", "inf", -1, {}, "1e1000"):
            self.assertEqual(self.view([receipt([row(base=base)])])["fills"], [])
        for changed in ({"identifier": "<script>", "order_id": None}, {"symbol": "<b>BTC</b>"},
                        {"side": "purchase"}, {"status": "invented"}):
            self.assertEqual(self.view([receipt([row(**changed)])])["fills"], [])
        self.assertEqual(self.view([receipt([row()], schema_version=True)])["state"], "unverified")
        self.assertEqual(self.view(None)["state"], "unverified")

    def test_structured_second_vote_has_no_purchase_claim(self):
        report = decision_report_fa(dict(DECISION, confirmation={"kept": {"BTC_IRT": .1}, "cut": {"BTC_IRT": .2}}))
        self.assertIn("در بررسی دوم تأیید نشد", report)
        self.assertIn("فقط افزایش وزن پیشنهادی", report)
        self.assertNotIn("خریده شد", report)

    def test_report_is_bounded_and_preserves_unverified_execution_warning(self):
        d = dict(DECISION, targets={"COIN%02d_IRT" % i: .03 for i in range(24)})
        report = decision_report_fa(d, limit=200)
        self.assertLessEqual(len(report), 200)
        self.assertIn("تأیید نشده", report)

    def test_report_uses_execution_time_clamped_targets_and_keeps_pending_warning(self):
        v = self.view([receipt([row("unknown", None)], effective_targets={"USDT_IRT": 1})])
        report = decision_report_fa(DECISION, v)
        self.assertIn("USDT_IRT 100%", report)
        self.assertNotIn("BTC_IRT 30%", report)
        d = dict(DECISION, targets={"COIN%02d_IRT" % i: .03 for i in range(24)})
        v["effective_targets"] = d["targets"]
        bounded = decision_report_fa(d, v, limit=200)
        self.assertIn("نامعلوم", bounded)
        self.assertIn("تأیید نشده", bounded)

    def test_malformed_execution_state_falls_back_without_crashing(self):
        for value in ([], {}, True, 42, None):
            text = decision_report_fa(DECISION, {"state": value})
            self.assertIn("تأیید نشده", text)

    def test_zero_weights_in_full_universe_do_not_hide_a_later_positive_target(self):
        targets = {"COIN%02d_IRT" % i: 0 for i in range(52)}
        targets["BTC_IRT"] = 1
        d = dict(DECISION, targets=targets)
        report = decision_report_fa(d)
        self.assertIn("BTC_IRT 100%", report)
        ex = self.view([receipt(effective_targets=targets)])
        self.assertEqual(ex["effective_targets"], {"BTC_IRT": 1})

    def test_cash_is_not_invented_by_truncating_execution_targets(self):
        targets = {"COIN%02d_IRT" % i: .04 for i in range(25)}
        ex = self.view([receipt(effective_targets=targets)])
        report = decision_report_fa(dict(DECISION, targets=targets), ex, limit=3500)
        self.assertIn("COIN24_IRT 4%", report)
        self.assertNotIn("تومان", report)


if __name__ == "__main__":
    unittest.main()
