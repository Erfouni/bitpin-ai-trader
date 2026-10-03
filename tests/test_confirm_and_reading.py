"""v3.12: a decision that buys is asked a second time (B1) and the code gives the technical reading (the model only
adds its overall "read"). No network: fake Kimi transports."""
import logging
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_brain as tb  # noqa: E402
from bitpin.llm import LLMError  # noqa: E402

logging.getLogger("bitpin").addHandler(logging.NullHandler())

BUY = {"USDT_IRT": 0.7, "BTC_IRT": 0.3}


class TestConfirmBuys(tb.ClockBase):
    def run_one(self, replies, cur=None, **cfg):
        b = self.cbrain(replies, confirm_buys=True, **cfg)
        b.should_decide(tb.T0, None, self.ctx())
        return b, b.decide(self.ctx(), cur or {"USDT_IRT": 1.0}, trigger=b.last_trigger)

    def test_both_replies_buy_the_same(self):
        b, d = self.run_one([tb.reply(BUY), tb.reply(BUY)])
        self.assertTrue(d.valid, d.error)
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.3, places=6)
        self.assertEqual(len(self.llm.calls), 2)
        self.assertEqual(d.confirmation["kept"], {"BTC_IRT": 0.3})
        self.assertEqual(d.confirmation["cut"], {})
        self.assertTrue(any("buys confirmed by a second run: BTC_IRT +30%" in a for a in d.adjustments), d.adjustments)
        # the second call is the same system and user message, without the validator's retry note
        self.assertEqual(self.llm.calls[1]["messages"], self.llm.calls[0]["messages"][:2])

    def test_only_what_both_buy_is_bought(self):
        b, d = self.run_one([tb.reply(BUY), tb.reply({"USDT_IRT": 0.8, "BTC_IRT": 0.2})])
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.2, places=6)
        self.assertAlmostEqual(d.targets["USDT_IRT"], 0.8, places=6)
        self.assertEqual(d.confirmation["cut"], {"BTC_IRT": 0.1})
        self.assertTrue(any("buy cut by the second run" in a and "BTC_IRT -10%" in a for a in d.adjustments))

    def test_no_buy_without_agreement_or_with_a_failed_second_run(self):
        for second in (tb.reply({"USDT_IRT": 1.0}), "not json at all", LLMError("proxy down")):
            b, d = self.run_one([tb.reply(BUY), second])
            self.assertTrue(d.valid, d.error)
            self.assertAlmostEqual(d.targets.get("BTC_IRT", 0.0), 0.0, places=6)
            self.assertAlmostEqual(d.targets["USDT_IRT"], 1.0, places=6)
            self.assertTrue(d.hold)
            self.assertEqual(d.confirmation["kept"], {})
        self.assertIn("the second call failed", d.confirmation["error"])

    def test_a_sale_needs_no_second_run_and_the_switch_turns_it_off(self):
        b, d = self.run_one([tb.reply({"USDT_IRT": 1.0})], cur={"USDT_IRT": 0.7, "BTC_IRT": 0.3})
        self.assertAlmostEqual(d.targets.get("BTC_IRT", 0.0), 0.0, places=6)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(d.confirmation, {})
        b = self.cbrain([tb.reply(BUY)], confirm_buys=False)
        b.should_decide(tb.T0, None, self.ctx())
        d = b.decide(self.ctx(), {"USDT_IRT": 1.0}, trigger=b.last_trigger)
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.3, places=6)
        self.assertEqual(len(self.llm.calls), 1)
        self.assertNotIn("CONFIRMATION (code)", self.llm.calls[0]["messages"][0]["content"])

    def test_the_prompt_says_so_and_a_late_second_run_buys_nothing(self):
        b, d = self.run_one([tb.reply(BUY), tb.reply(BUY)])
        self.assertIn("CONFIRMATION (code): a scheduled or held_move decision that buys is asked a second time",
                      self.llm.calls[0]["messages"][0]["content"])
        b = self.cbrain([tb.reply(BUY), tb.reply(BUY)], confirm_buys=True, decision_deadline_seconds=60)
        b.should_decide(tb.T0, None, self.ctx())
        real = b.monotonic
        ticks = iter([0.0, 0.0, 0.0, 59.0, 59.0, 59.0, 59.0, 59.0, 59.0])
        b.monotonic = lambda: next(ticks, 59.0) + real()
        d = b.decide(self.ctx(), {"USDT_IRT": 1.0}, trigger=b.last_trigger)
        self.assertAlmostEqual(d.targets.get("BTC_IRT", 0.0), 0.0, places=6)
        self.assertIn("no time left for the second run", d.confirmation["error"])


class TestCodeReading(unittest.TestCase):
    def ctx(self):
        import test_technical as tt
        c = tb.actx()                                    # keeps its own ret_usdt: the candidate cites it
        c["symbols"]["BTC_IRT"].update({k: v for k, v in tt.BTC.items() if k != "ret_usdt"})
        return c

    def test_the_candidate_takes_the_codes_reading_and_the_models_read(self):
        out = tb.av(tb.areply(candidate=tb.cand(ta=None, read="Bullish")), ctx=self.ctx())
        c = out["analysis"]["candidates"]["BTC_IRT"]
        self.assertEqual(c["ta_source"], "code")
        self.assertEqual(c["ta_check"], [])
        self.assertEqual(c["ta"]["read"], "bullish")
        for k in ("trend", "long", "momentum", "rsi", "bands", "channel", "volume", "support", "resistance"):
            self.assertEqual(c["ta"].get(k), c["ta_code"].get(k), k)

    def test_an_old_style_reply_is_still_checked(self):
        out = tb.av(tb.areply(candidate=tb.cand(ta={"trend": "down", "read": "bearish"})), ctx=self.ctx())
        c = out["analysis"]["candidates"]["BTC_IRT"]
        self.assertEqual(c["ta_source"], "model")
        self.assertEqual(c["ta"]["trend"], "down")
        self.assertEqual(c["ta_check"], [{"field": "trend", "model": "down", "code": "up"}])


class TestPanelSecondRun(unittest.TestCase):
    def test_the_dashboard_and_the_record_show_the_second_run(self):
        from bitpin import notify as nt
        from bitpin import panel_web as pw
        from bitpin.panel_i18n import use
        conf = {"buys": {"BTC_IRT": 0.3}, "kept": {"BTC_IRT": 0.2}, "cut": {"BTC_IRT": 0.1}, "second": {},
                "second_confidence": 0.5, "error": None}
        self.assertEqual(nt._confirmation(conf), {"kept": {"BTC_IRT": 0.2}, "cut": {"BTC_IRT": 0.1}, "error": None})
        self.assertIsNone(nt._confirmation({}))
        with use("en"):
            t = pw.confirmation_html(nt._confirmation(conf))
            self.assertIn("Buy cut", t)
            self.assertIn("bought", t)
            self.assertIn("Confirmed", pw.confirmation_html({"kept": {"BTC_IRT": 0.3}, "cut": {}, "error": None}))
            card = pw.PanelApp._decision_card(None, {"valid": True, "hold": False, "targets": {"BTC_IRT": 0.2},
                                                     "confirmation": nt._confirmation(conf)})
            self.assertIn("Second run", card)
        with use("fa"):
            self.assertIn(u"خرید کم شد", pw.confirmation_html(nt._confirmation(conf)))


if __name__ == "__main__":
    unittest.main()
