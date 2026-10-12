# -*- coding: utf-8 -*-
"""Final targets, confirmed votes and exchange fills stay distinct in owner-facing reports."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bitpin import notify as nt, panel_web as pw  # noqa: E402
from bitpin.panel_i18n import use  # noqa: E402
from scripts import panel_helper as ph  # noqa: E402

T0 = 1700000000.0
FALSE_REPORT = u"خرید بیت‌کوین انجام شد."


def decision_record(report=FALSE_REPORT):
    return {"time": T0, "context_digest": "display-test", "current_weights": {"USDT_IRT": 1.0},
            "decision": {"decided_at": T0, "valid": True, "hold": True, "origin": "live",
                         "targets": {"USDT_IRT": 1.0}, "cash_irt": 0.0, "report_fa": report,
                         "proposed": {"BTC_IRT": 0.3, "USDT_IRT": 0.7},
                         "p_rule": {"BTC_IRT": {"reason": "ev", "ev": -1.0}},
                         "adjustments": ["P RULE blocked BTC_IRT"]},
            "report_fa": FALSE_REPORT, "response_json": {"report_fa": FALSE_REPORT},
            "response": json.dumps({"report_fa": FALSE_REPORT})}


def execution_record(state="filled", origin="live", runtime_mode="live"):
    order = {"identifier": "display-order", "order_id": "exchange-order", "side": "buy", "symbol": "BTC_USDT",
             "status": state, "base": "0.02" if state == "filled" else "0", "quote": "12", "avg_price": "600",
             "requested": "0.02", "partial": False}
    return {"time": T0 + 60, "mode": runtime_mode, "action": "kimi", "status": "ok",
            "execution": {"schema_version": 1, "decision_ref": {"decided_at": T0, "origin": origin},
                          "runtime_mode": runtime_mode, "action": "kimi", "effective_targets": {"BTC_IRT": 0.2},
                          "orders": [order], "skipped": [], "errors": []}}


class TestExecutionReportDisplay(unittest.TestCase):
    def test_final_report_never_resurrects_raw_purchase_claim(self):
        for report in (FALSE_REPORT, "", None):
            v = nt.decision_view(decision_record(report))
            self.assertTrue(v["report_fa"])
            self.assertNotIn(FALSE_REPORT, v["report_fa"])
            self.assertIn("USDT_IRT", v["report_fa"])
            self.assertEqual(v["execution"]["state"], "unverified")
            self.assertNotIn(FALSE_REPORT, nt.Renderer({}).decision(v, persian=FALSE_REPORT, full=True))

    def test_cached_translation_cannot_replace_the_final_report(self):
        class OldTranslation:
            def cached(self, key):
                return FALSE_REPORT

            def translate(self, *args, **kwargs):
                raise AssertionError("decision truth must not use a translation call")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = nt.load_config(None, overrides={"state_dir": tmp, "notify_state_dir": os.path.join(tmp, "notify")})
            n = nt.Notifier(cfg, in_memory=True, translator=OldTranslation())
            text = n.decision_message(nt.decision_view(decision_record("")), full=True)
            self.assertNotIn(FALSE_REPORT, text)
            self.assertIn("USDT_IRT", text)

    def test_readers_and_panel_helper_show_only_linked_positive_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = decision_record()
            rec["decision"].update(hold=False, targets={"BTC_IRT": 0.2, "USDT_IRT": 0.8}, p_rule={})
            for filename, rows in (("kimi_decisions.jsonl", [rec]),
                                   ("kimi_runner.jsonl", [execution_record(origin="paper:test"), execution_record()])):
                with open(os.path.join(tmp, filename), "w", encoding="utf-8") as f:
                    f.write("\n".join(json.dumps(row) for row in rows) + "\n")
            cfg = nt.load_config(None, overrides={"state_dir": tmp, "notify_state_dir": os.path.join(tmp, "notify")})
            n = nt.Notifier(cfg, in_memory=True)
            for v in (n.last_decision_view(), n.last_kimi_view()):
                self.assertEqual(v.get("execution", {}).get("state"), "filled")
                self.assertEqual(len(v["execution"]["fills"]), 1)
                self.assertEqual(float(v["execution"]["fills"][0]["base"]), 0.02)
            state = ph.collect_bot_state(tmp, os.path.join(tmp, "notify"), os.path.join(tmp, "missing.json"), T0 + 120)
            self.assertEqual(state["last_decision"]["execution"]["state"], "filled")
            self.assertNotIn(FALSE_REPORT, state["last_decision"]["report_fa"])

    def test_second_vote_approval_never_says_bought(self):
        conf = {"kept": {"BTC_IRT": 0.2}, "cut": {"BTC_IRT": 0.1}, "error": None}
        with use("en"):
            html = pw.confirmation_html(conf)
            self.assertNotIn("bought", html)
            self.assertIn("approved", html.lower())
        with use("fa"):
            self.assertNotIn(u"خریده شد", pw.confirmation_html(conf))

    def test_exact_receipt_origin_is_preserved_beyond_display_label_length(self):
        origin = "live:" + "a" * 60
        rec = decision_record()
        rec["decision"]["origin"] = origin
        v = nt.decision_view(rec)
        self.assertEqual(v["origin"], origin)
        from bitpin.decision_report import execution_view
        self.assertEqual(execution_view(v, [execution_record(origin=origin)])["state"], "filled")

    def test_panel_separates_target_weights_from_execution_amounts(self):
        decision = {"valid": True, "hold": False, "targets": {"BTC_IRT": 0.2}, "report_fa": u"ترکیب هدف نهایی",
                    "execution": {"schema_version": 1, "state": "partial", "mode": "paper", "orders": 2,
                                  "pending": 1, "fills": [{"side": "buy", "symbol": "BTC_USDT", "base": 0.02,
                                                               "quote": 12.0, "avg_price": 600.0, "partial": True}],
                                  "skipped": [], "errors": [], "effective_targets": {"BTC_IRT": 0.15}}}
        with use("en"):
            html = pw.PanelApp._decision_card(None, decision)
            self.assertNotIn("Trade (new weights)", html)
            self.assertIn("Target weights", html)
            self.assertIn("Execution", html)
            self.assertIn("Paper", html)
            self.assertIn("0.02", html)
            self.assertIn("BTC_USDT", html)
            self.assertIn("Partial", html)

    def test_numeric_execution_counts_reach_panel_and_notification(self):
        ex = {"schema_version": 1, "state": "pending", "mode": "live", "orders": 2, "pending": 1,
              "fills": [], "skipped": 3, "errors": 4, "effective_targets": {}}
        with use("en"):
            html = pw.execution_html(ex)
            self.assertIn('<dt>Recorded orders</dt><dd><bdi dir="ltr">2</bdi>', html)
            self.assertIn('<dt>Pending orders</dt><dd><bdi dir="ltr">1</bdi>', html)
            self.assertIn('<dt>Execution skips</dt><dd><bdi dir="ltr">3</bdi>', html)
        text = "\n".join(nt.Renderer({})._execution_lines({"execution": ex}))
        self.assertIn(u"سفارش با نتیجهٔ نامشخص یا باز: ۱", text)


if __name__ == "__main__":
    unittest.main()
