"""v3.13 P RULE (owner 2026-10-07): a coin's weight rises only on a p the code accepts - p0 (from the plan's levels)
+ 0.02, more only on B3 / B11 whose condition holds now or on a dated event named in a NEWS BRIEF headline.
Study 08, and the bot's own decisions after v3.12 (about +0.14 on B10, a row without an edge, behind every buy).
No network: fake Kimi transports."""
import json
import logging
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_brain as tb  # noqa: E402
from bitpin.brain import (B3_RISE_PCT, P_CAP, P_SHIFT_EVENT, P_SHIFT_FREE, P_SHIFT_ROW, p_rule_check,  # noqa: E402
                          p_rule_text, validate_brain_config)
from bitpin.llm import ConfigError  # noqa: E402

logging.getLogger("bitpin").addHandler(logging.NullHandler())

PLAN = tb.aplan()                      # px 80000, invalidation 76000 (l 5%), take profit 88000 (g 10%): p0 = 1/3
P0 = 1.0 / 3.0
HEAD_BTC = "Bitcoin ETF inflows hit a record as the SEC clears two new funds"
HEAD_ETH = "Ethereum developers set the date of the next network upgrade"
B10_BUY = tb.cand(row="B10", p=0.47, ev_pct=1.05)       # +0.137 over p0 on a row without an edge (the live pattern)


class TestCheck(unittest.TestCase):
    def check(self, cand, plan=PLAN, sym="BTC_IRT", ctx=None, heads=()):
        return p_rule_check(sym, cand, plan, 80000.0, ctx if ctx is not None else {}, list(heads))

    def test_rounding_above_p0_is_free_anything_more_needs_a_reason(self):
        self.assertIsNone(self.check(tb.cand(p=round(P0 + P_SHIFT_FREE, 4))))
        r = self.check(B10_BUY)
        self.assertEqual((r["reason"], r["basis"], r["p0_from"]), ("above", "none", "plan"))
        self.assertAlmostEqual(r["p0"], 0.3333, places=4)
        self.assertAlmostEqual(r["max"], round(P0 + P_SHIFT_FREE, 4), places=4)
        self.assertEqual(p_rule_text(r), "p 0.47 is above the 0.35 the code allows (p0 0.333 from the plan's levels; "
                                         "no B3 (ret_usdt[0] >= 15) or B11 (gold) condition and no dated news naming "
                                         "the coin)")

    def test_p0_comes_from_the_plans_levels_not_from_the_reply(self):
        r = self.check(tb.cand(row="B10", p0=0.45, p=0.47))           # a stated p0 cannot lift the bar
        self.assertAlmostEqual(r["p0"], 0.3333, places=4)
        r = self.check(tb.cand(row="B10", p0=0.45, p=0.47), plan={})    # without usable levels: the reply's
        self.assertIsNone(r)

    def test_b3_needs_a_15_pct_day_and_a_horizon_it_covers(self):
        rise = {"ret_usdt": [B3_RISE_PCT + 1, 20.0, 25.0]}
        self.assertIsNone(self.check(tb.cand(row="B3", p=0.47), ctx=rise))
        self.assertEqual(self.check(tb.cand(row="B3", p=0.47), ctx={"ret_usdt": [10.0, 20.0, 25.0]})["basis"], "none")
        self.assertIsNotNone(self.check(tb.cand(row="B3", p=0.47), ctx=rise, plan=tb.aplan(horizon=720)))  # 168 h row
        self.assertIsNone(self.check(tb.cand(row="B3", p=0.47), ctx={"r_usdt": [16.0, 1.0, 2.0]}))     # a compact row
        r = self.check(tb.cand(row="B3", p=round(P0 + P_SHIFT_ROW + 0.03, 3)), ctx=rise)              # still capped
        self.assertEqual((r["basis"], r["max"]), ("B3", round(P0 + P_SHIFT_ROW, 4)))

    def test_b11_is_for_gold_only(self):
        self.assertIsNone(self.check(tb.cand(row="B11", p=0.47), sym="PAXG_IRT", plan=None))
        self.assertIsNotNone(self.check(tb.cand(row="B11", p=0.47)))
        self.assertIsNone(self.check(tb.cand(row="B7+B11", p=0.47), sym="XAUT_IRT", plan=None))

    def test_a_dated_event_needs_a_brief_headline_naming_the_coin(self):
        ev = tb.cand(row="B10", p=0.52, evidence="dd48=-3.1 ETF approval 2027-03-14")
        self.assertIsNone(self.check(ev, heads=[HEAD_ETH, HEAD_BTC]))
        self.assertIsNotNone(self.check(ev))                                           # no brief
        self.assertIsNotNone(self.check(ev, heads=[HEAD_ETH]))                         # not about bitcoin
        self.assertIsNotNone(self.check(tb.cand(row="B10", p=0.52), heads=[HEAD_BTC]))  # no date in the evidence
        self.assertEqual(self.check(tb.cand(row="B10", p=0.55, evidence="ETF 2027-03-14"), heads=[HEAD_BTC])["basis"],
                         "event")                                                      # above p0 + 0.20

    def test_the_hard_cap_holds_whatever_the_reason(self):
        plan = tb.aplan(inv=72000.0, tp=84000.0)                                       # l 10, g 5: p0 = 2/3
        ev = tb.cand(row="B10", p=0.82, evidence="ETF 2027-03-14")
        r = self.check(ev, plan=plan, heads=[HEAD_BTC])
        self.assertEqual((r["reason"], r["max"]), ("above", P_CAP))
        self.assertLess(P_SHIFT_EVENT + 2.0 / 3.0, 0.9)

    def test_no_candidate_or_no_numbers_is_missing(self):
        self.assertEqual(self.check(None)["reason"], "missing")
        self.assertEqual(self.check(tb.cand(p=None))["reason"], "missing")
        self.assertEqual(p_rule_text(self.check(None)), "no analysis candidate with numbers for p and p0")


class TestValidate(unittest.TestCase):
    def test_an_unjustified_p_keeps_the_coin_at_its_weight(self):
        out = tb.av(tb.areply(candidate=B10_BUY), policy="off", p_rule=True)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=9)
        self.assertAlmostEqual(out["targets"]["USDT_IRT"], 1.0, places=9)
        self.assertEqual(out["p_rule"]["BTC_IRT"]["reason"], "above")
        self.assertTrue(any("BTC_IRT: increase blocked (P RULE: p 0.47 is above the 0.35 the code allows" in a
                            for a in out["adjustments"]), out["adjustments"])
        self.assertEqual(out["analysis"]["p_rule"]["BTC_IRT"]["basis"], "none")
        # an add to a held coin: kept at the weight it has (never a sale)
        out = tb.av(tb.areply({"USDT_IRT": 0.2, "BTC_IRT": 0.8}, candidate=dict(B10_BUY, verdict="add")),
                    current={"USDT_IRT": 0.35, "BTC_IRT": 0.65}, policy="off", p_rule=True)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.65, places=6)

    def test_off_a_justified_p_and_a_sale_are_untouched(self):
        out = tb.av(tb.areply(candidate=B10_BUY), policy="off", p_rule=False)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.3, places=6)
        self.assertEqual(out["p_rule"], {})
        out = tb.av(tb.areply(candidate=tb.cand(p=0.35, ev_pct=-0.75)), policy="off", p_rule=True)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.3, places=6)               # within rounding
        out = tb.av(tb.areply({"USDT_IRT": 0.9, "BTC_IRT": 0.1}, analysis=None), current={"USDT_IRT": 0.7, "BTC_IRT": 0.3},
                    policy="off", p_rule=True)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.1, places=6)               # a sale needs no p

    def test_no_analysis_buys_nothing_whatever_the_policy(self):
        out = tb.av(tb.areply(analysis=None), policy="off", p_rule=True)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=9)
        self.assertEqual(out["p_rule"]["BTC_IRT"]["reason"], "missing")
        out = tb.av(tb.areply(analysis=None), policy="block", p_rule=True)            # the policy blocked it first
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=9)
        self.assertEqual(out["p_rule"], {})
        out = tb.av(tb.areply(analysis=None), policy=None, p_rule=True)                # library use: nothing to check
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.3, places=6)


class TestBrain(tb.ClockBase):
    def run_one(self, reply, **cfg):
        b = self.cbrain([reply], drawdown_breaker=0.5, **cfg)
        b.should_decide(tb.T0, None, tb.actx())
        return b, self.decide_now(b, tb.actx(), positions={})

    def test_the_brain_applies_it_says_so_and_keeps_the_record(self):
        b, d = self.run_one(tb.areply(candidate=B10_BUY), p_rule=True)
        self.assertTrue(d.valid, d.error)
        self.assertAlmostEqual(d.targets.get("BTC_IRT", 0.0), 0.0, places=9)
        self.assertEqual(d.p_rule["BTC_IRT"]["reason"], "above")
        sp = self.llm.calls[0]["messages"][0]["content"]
        self.assertIn("P RULE (code): above p0 (from the plan's levels) + 0.02 a coin is increased only on B3 with "
                      "ret_usdt[0] >= 15 or B11 on a gold token (up to +0.15), or a dated event in evidence plus a NEWS "
                      "BRIEF headline naming the coin (+0.20).", sp)
        self.assertIn("B7, B10 and the TA reading add nothing", sp)
        with open(os.path.join(self.dir, "kimi_decisions.jsonl"), encoding="utf-8") as f:
            rec = json.loads(f.readlines()[-1])
        self.assertEqual(rec["decision"]["p_rule"]["BTC_IRT"]["reason"], "above")
        # the next context tells the model why the buy did not happen (the bot's words)
        self.assertTrue(any("P RULE" in a for a in b.recent_decisions()[-1]["adjustments"]))

    def test_off_restores_the_old_behaviour(self):
        b, d = self.run_one(tb.areply(candidate=B10_BUY), p_rule=False)
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.3, places=6)
        self.assertEqual(d.p_rule, {})
        self.assertNotIn("P RULE (code)", self.llm.calls[0]["messages"][0]["content"])

    def test_the_setting(self):
        base = {"allowed_symbols": tb.ALLOWED}
        self.assertIs(validate_brain_config(dict(base))["p_rule"], True)
        with self.assertRaises(ConfigError):
            validate_brain_config(dict(base, p_rule="yes"))


class TestShown(unittest.TestCase):
    REC = {"BTC_IRT": {"p": 0.47, "p0": 0.3333, "max": 0.3533, "basis": "none", "reason": "above", "p0_from": "plan"}}

    def test_the_notifier_the_dashboard_and_the_settings_show_it(self):
        from bitpin import notify as nt
        from bitpin import panel_settings as ps
        from bitpin import panel_web as pw
        from bitpin.panel_i18n import use
        v = nt._p_rule_view(dict(self.REC, junk={"p": 1}, ETH_IRT={"reason": "missing"}))
        self.assertEqual(v, {"BTC_IRT": {"p": 0.47, "p0": 0.3333, "max": 0.3533, "reason": "above"},
                             "ETH_IRT": {"p": None, "p0": None, "max": None, "reason": "missing"}})
        stub = types.SimpleNamespace(coin=lambda sym, with_code=True: sym.split("_")[0])
        lines = nt.Renderer._p_rule_lines(stub, {"p_rule": v})
        self.assertIn(u"قانون احتمال", lines[0])
        self.assertTrue(any(u"حداکثر مجاز" in ln and "BTC" in ln for ln in lines), lines)
        self.assertEqual(nt.Renderer._p_rule_lines(stub, {"p_rule": {}}), [])
        with use("en"):
            h = pw.p_rule_html(v)
            self.assertIn("Buy held back", h)
            self.assertIn("at most", h)
            self.assertIn("no analysis with p", h)
            card = pw.PanelApp._decision_card(None, {"valid": True, "hold": True, "targets": {"USDT_IRT": 1.0},
                                                     "p_rule": v})
            self.assertIn("Probability rule", card)
        with use("fa"):
            self.assertIn(u"خرید انجام نشد", pw.p_rule_html(v))
        self.assertIn("kimi:brain.p_rule", [f.key for f in ps.FIELDS])


if __name__ == "__main__":
    unittest.main()
