"""v3.13 P RULE (owner 2026-10-07; tightened in v3.13.1): a coin's weight rises only on a reason the code can check -
B11 on a gold token, or a NEWS BRIEF headline naming the coin quoted in the evidence - with p0 from the plan's own
levels, p within that reason's bound and ev_pct >= 0 at it. Study 08, and the bot's own decisions after v3.12 (about
+0.14 on B10, a row without an edge, behind every buy). The four ways the 3.13.0 rule was broken (the external
reviewer's three and a fourth) are regression tests here. No network: fake Kimi transports."""
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
from bitpin.brain import (P_CAP, P_ROUND, P_SHIFT_EVENT, P_SHIFT_ROW, p_rule_check, p_rule_text,  # noqa: E402
                          quoted_headline, validate_brain_config)
from bitpin.llm import ConfigError  # noqa: E402

logging.getLogger("bitpin").addHandler(logging.NullHandler())

PLAN = tb.aplan()                      # px 80000, invalidation 76000 (l 5%), take profit 88000 (g 10%): p0 = 1/3
P0 = 1.0 / 3.0
HEAD_BTC = "Bitcoin ETF inflows hit a record as the SEC clears two new funds"
HEAD_ETH = "Ethereum developers set the date of the next network upgrade"
QUOTE_BTC = "dd48=-3.1 ETF inflows hit a record as the SEC clears"          # 6+ words of HEAD_BTC in a row
B10_BUY = tb.cand(row="B10", p=0.47, ev_pct=1.05)       # +0.137 over p0 on a row without an edge (the live pattern)


class TestCheck(unittest.TestCase):
    def check(self, cand, plan=PLAN, sym="BTC_IRT", ctx=None, heads=(), px=80000.0):
        return p_rule_check(sym, cand, plan, px, ctx if ctx is not None else {}, list(heads))

    def test_without_a_reason_the_code_accepts_nothing_is_bought(self):
        r = self.check(B10_BUY)
        self.assertEqual((r["reason"], r["basis"]), ("no_basis", "none"))
        self.assertAlmostEqual(r["p0"], 0.3333, places=4)
        self.assertEqual(p_rule_text(r), "no reason the code accepts (B11 on a gold token, or a NEWS BRIEF headline "
                                         "naming the coin quoted in evidence); p0 0.333 from the plan's levels")
        for p in (round(P0 + 0.02, 4), round(P0, 4), 0.2):              # the 3.13.0 free rounding, p0, below p0
            self.assertEqual(self.check(tb.cand(row="B10", p=p))["reason"], "no_basis", p)
        rise = {"ret_usdt": [16.0, 20.0, 25.0]}                         # B3 is no buy signal (the knowledge)
        self.assertEqual(self.check(tb.cand(row="B3", p=0.47), ctx=rise)["reason"], "no_basis")

    def test_p0_comes_only_from_the_plans_own_levels(self):
        for plan in ({}, dict(PLAN, take_profit_usdt=None), dict(PLAN, take_profit_usdt=79000.0),
                     dict(PLAN, invalidation_usdt=81000.0)):
            r = self.check(tb.cand(row="B11", p=0.47, p0=0.45), plan=plan, sym="PAXG_IRT")
            self.assertEqual(r["reason"], "unverifiable", plan)
        self.assertIn("cannot compute p0", p_rule_text(r))
        self.assertIsNone(self.check(tb.cand(row="B11", p=0.48), sym="PAXG_IRT"))

    def test_b11_for_gold_up_to_its_bound_and_only_with_a_positive_ev(self):
        # a gold token pays the other coins' cost floor (2.2%, COST_FLOOR_OTHER), not the majors' 1%
        gold = lambda p: self.check(tb.cand(row="B11", p=p), sym="PAXG_IRT")  # noqa: E731
        self.assertIsNone(gold(round(P0 + P_SHIFT_ROW, 4)))             # ev 0.05
        self.assertIsNone(gold(0.48))                                   # ev 0.48x10 - 0.52x5 - 2.2 = 0
        r = gold(0.46)                                                  # ev -0.3
        self.assertEqual((r["reason"], r["basis"], r["ev"]), ("ev", "B11", -0.3))
        self.assertIn("ev_pct -0.30 at p 0.46", p_rule_text(r))
        r = self.check(tb.cand(row="B11", p=0.46, cost_pct=3.0), sym="PAXG_IRT")        # a higher stated cost counts
        self.assertEqual(r["ev"], -1.1)
        r = gold(round(P0 + P_SHIFT_ROW + P_ROUND + 0.01, 3))
        self.assertEqual((r["reason"], r["max"]), ("above", round(P0 + P_SHIFT_ROW, 4)))
        self.assertIsNone(gold(round(P0 + P_SHIFT_ROW + P_ROUND, 3)))   # rounding: the ev uses the bound itself
        self.assertEqual(self.check(tb.cand(row="B11", p=0.47))["reason"], "no_basis")              # not gold
        self.assertEqual(self.check(tb.cand(row="B11", p=0.47), sym="PAXG_IRT",
                                    plan=tb.aplan(horizon=800))["reason"], "no_basis")          # beyond B11's 720 h

    def test_a_quoted_headline_naming_the_coin_is_the_only_news_reason(self):
        ev = tb.cand(row="B10", p=0.52, evidence=QUOTE_BTC)
        self.assertIsNone(self.check(ev, heads=[HEAD_ETH, HEAD_BTC]))
        self.assertEqual(self.check(ev, heads=[HEAD_ETH])["reason"], "no_basis")                # not about bitcoin
        self.assertEqual(self.check(ev)["reason"], "no_basis")                                  # no brief
        r = self.check(tb.cand(row="B10", p=0.55, evidence=QUOTE_BTC), heads=[HEAD_BTC])
        self.assertEqual((r["reason"], r["basis"], r["max"]), ("above", "event", round(P0 + P_SHIFT_EVENT, 4)))
        self.assertEqual(quoted_headline("bitcoin etf flows record", [HEAD_BTC], "BTC"), None)  # a paraphrase
        self.assertEqual(quoted_headline("why: Bitcoin hits record high today", ["Bitcoin hits record high"], "BTC"),
                         "Bitcoin hits record high")                                           # short: whole headline
        self.assertIsNone(quoted_headline("Bitcoin hits record", ["Bitcoin hits record high"], "BTC"))

    def test_the_hard_cap_holds_whatever_the_reason(self):
        plan = tb.aplan(inv=72000.0, tp=84000.0)                        # l 10, g 5: p0 = 2/3
        r = self.check(tb.cand(row="B10", p=0.82, evidence=QUOTE_BTC), plan=plan, heads=[HEAD_BTC])
        self.assertEqual((r["reason"], r["max"]), ("above", P_CAP))

    def test_no_candidate_or_no_p_is_missing(self):
        self.assertEqual(self.check(None)["reason"], "missing")
        self.assertEqual(self.check(tb.cand(p=None))["reason"], "missing")
        self.assertEqual(p_rule_text(self.check(None)), "no analysis candidate with a number for p")


class TestTheFourBreaks(unittest.TestCase):
    """The ways the 3.13.0 rule let an unjustified buy through, end to end through validate_response with the
    production policies (analysis "off", p_rule on; the plan check of a NEW position where it applies)."""

    def run_one(self, cand, plans, heads=(), current=None, positions=None, **kw):
        reply = tb.areply(candidate=cand, plans=plans, **({"targets": kw.pop("targets")} if "targets" in kw else {}))
        return tb.av(reply, policy="off", p_rule=True, news_headlines=list(heads), current=current,
                     positions=positions, **kw)

    def test_1_a_lopsided_plan_cannot_lift_p0(self):
        plan = tb.aplan(inv=70400.0, tp=80080.0)                        # target +0.1%, invalidation -12%: p0 0.992
        cand = tb.cand(row="B10", p=0.79, p0=0.992, gain_pct=0.1, loss_pct=12.0, ev_pct=-1.0)
        out = self.run_one(cand, {"BTC_IRT": plan}, plan_policy="block")
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=9)
        self.assertEqual(out["p_rule"]["BTC_IRT"]["reason"], "no_basis")
        out = self.run_one(dict(cand, evidence=QUOTE_BTC), {"BTC_IRT": plan}, heads=[HEAD_BTC])     # even with news
        self.assertEqual((out["p_rule"]["BTC_IRT"]["reason"], out["targets"]["BTC_IRT"]), ("ev", 0.0))

    def test_2_the_replys_own_p0_is_never_used(self):
        for plan in (tb.aplan(inv=76000.0, tp=79000.0), dict(tb.aplan(), take_profit_usdt=None)):
            out = self.run_one(tb.cand(row="B10", p=0.79, p0=0.78), {"BTC_IRT": plan})
            self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=9)
            self.assertEqual(out["p_rule"]["BTC_IRT"]["reason"], "unverifiable")
        # an add to a winner past its first target, without a new plan: the held plan's target is below the price
        held = {"BTC_IRT": {"source": "kimi", "entry_px_usdt": 70000.0, "amount": 0.01,
                            "plan": dict(tb.aplan(inv=66000.0, tp=78000.0, horizon=720), set_at=tb.T0 - 3600)}}
        out = self.run_one(tb.cand(row="B10", p=0.79, p0=0.78, verdict="add", evidence=QUOTE_BTC), {},
                           heads=[HEAD_BTC], current={"USDT_IRT": 0.7, "BTC_IRT": 0.3}, positions=held,
                           targets={"USDT_IRT": 0.5, "BTC_IRT": 0.5})
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.3, places=6)
        self.assertEqual(out["p_rule"]["BTC_IRT"]["reason"], "unverifiable")

    def test_3_a_date_next_to_any_headline_is_no_news_reason(self):
        cand = tb.cand(row="B10", p=0.47, evidence="Fed decision 2026-10-28")
        out = self.run_one(cand, {"BTC_IRT": tb.aplan()}, heads=[HEAD_BTC])
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=9)
        self.assertEqual(out["p_rule"]["BTC_IRT"]["reason"], "no_basis")

    def test_4_the_free_rounding_cannot_buy_a_wide_bracket(self):
        plan = tb.aplan(inv=70400.0, tp=110400.0, horizon=720)          # +38% / -12%: ev 0 at p0 + 0.02
        out = self.run_one(tb.cand(row="B10", p=0.26, p0=0.24, gain_pct=38.0, loss_pct=12.0, ev_pct=0.0),
                           {"BTC_IRT": plan})
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=9)
        self.assertEqual(out["p_rule"]["BTC_IRT"]["reason"], "no_basis")

    def test_the_sound_paths_stay_open(self):
        out = self.run_one(tb.cand(row="B10", p=0.47, evidence=QUOTE_BTC), {"BTC_IRT": tb.aplan()}, heads=[HEAD_BTC])
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.3, places=6)                    # quoted news, ev > 0
        self.assertEqual(out["p_rule"], {})
        out = self.run_one(None, {}, current={"USDT_IRT": 0.7, "BTC_IRT": 0.3}, targets={"USDT_IRT": 0.9, "BTC_IRT": 0.1})
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.1, places=6)                    # a sale needs no p


class TestValidate(unittest.TestCase):
    def test_an_unjustified_p_keeps_the_coin_at_its_weight(self):
        out = tb.av(tb.areply(candidate=B10_BUY), policy="off", p_rule=True)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=9)
        self.assertAlmostEqual(out["targets"]["USDT_IRT"], 1.0, places=9)
        self.assertTrue(any("BTC_IRT: increase blocked (P RULE: no reason the code accepts" in a
                            for a in out["adjustments"]), out["adjustments"])
        self.assertEqual(out["analysis"]["p_rule"]["BTC_IRT"]["reason"], "no_basis")
        out = tb.av(tb.areply({"USDT_IRT": 0.2, "BTC_IRT": 0.8}, candidate=dict(B10_BUY, verdict="add")),
                    current={"USDT_IRT": 0.35, "BTC_IRT": 0.65}, policy="off", p_rule=True)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.65, places=6)                   # kept, never sold

    def test_off_restores_the_old_behaviour(self):
        out = tb.av(tb.areply(candidate=B10_BUY), policy="off", p_rule=False)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.3, places=6)
        self.assertEqual(out["p_rule"], {})

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
        self.assertEqual(d.p_rule["BTC_IRT"]["reason"], "no_basis")
        sp = self.llm.calls[0]["messages"][0]["content"]
        self.assertIn("Add at most 0.15 only on B11 for a gold token, at most 0.20 only with a coin-specific event "
                      "whose NEWS BRIEF headline names the coin, quoted word for word in evidence; every other row "
                      "(B3, B7, B10 ...) and the TA reading add nothing", sp)
        self.assertIn("P RULE (code): p0 comes from the plan's own levels (take_profit_usdt above px_usdt above "
                      "invalidation_usdt); without such a reason, or with ev_pct < 0 at that p, the coin is not "
                      "increased.", sp)
        with open(os.path.join(self.dir, "kimi_decisions.jsonl"), encoding="utf-8") as f:
            rec = json.loads(f.readlines()[-1])
        self.assertEqual(rec["decision"]["p_rule"]["BTC_IRT"]["reason"], "no_basis")
        self.assertTrue(any("P RULE" in a for a in b.recent_decisions()[-1]["adjustments"]))

    def test_off_restores_the_old_behaviour_and_wording(self):
        b, d = self.run_one(tb.areply(candidate=B10_BUY), p_rule=False)
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.3, places=6)
        self.assertEqual(d.p_rule, {})
        sp = self.llm.calls[0]["messages"][0]["content"]
        self.assertNotIn("P RULE (code)", sp)
        self.assertIn("Add at most 0.15 on a row with a positive result whose CONDITION holds now", sp)

    def test_the_setting(self):
        base = {"allowed_symbols": tb.ALLOWED}
        self.assertIs(validate_brain_config(dict(base))["p_rule"], True)
        with self.assertRaises(ConfigError):
            validate_brain_config(dict(base, p_rule="yes"))


class TestShown(unittest.TestCase):
    REC = {"BTC_IRT": {"p": 0.47, "p0": 0.3333, "max": 0.3333, "basis": "none", "reason": "no_basis", "ev": None},
           "ETH_IRT": {"p": 0.6, "p0": 0.3333, "max": 0.4833, "basis": "B11", "reason": "above", "ev": None},
           "SOL_IRT": {"p": 0.38, "p0": 0.3333, "max": 0.4833, "basis": "B11", "reason": "ev", "ev": -0.3},
           "XRP_IRT": {"p": 0.5, "p0": None, "max": None, "basis": "none", "reason": "unverifiable", "ev": None}}

    def test_the_notifier_the_dashboard_and_the_settings_show_it(self):
        from bitpin import notify as nt
        from bitpin import panel_settings as ps
        from bitpin import panel_web as pw
        from bitpin.panel_i18n import use
        v = nt._p_rule_view(dict(self.REC, junk={"p": 1}, ADA_IRT={"reason": "above"}))
        self.assertEqual({s: r["reason"] for s, r in v.items()},
                         {"BTC_IRT": "no_basis", "ETH_IRT": "above", "SOL_IRT": "ev", "XRP_IRT": "unverifiable",
                          "ADA_IRT": "missing"})                       # "above" without its numbers: missing
        stub = types.SimpleNamespace(coin=lambda sym, with_code=True: sym.split("_")[0])
        lines = nt.Renderer._p_rule_lines(stub, {"p_rule": v})
        self.assertIn(u"قانون احتمال", lines[0])
        text = "\n".join(lines)
        for want in (u"دلیل مجازی نداشت", u"حداکثر مجاز", u"ارزش انتظاری با همان p منفی بود",
                     u"برنامه هدف و سطح ابطالِ قابل‌استفاده نداشت", u"تحلیلی با p نداشت"):
            self.assertIn(want, text)
        self.assertEqual(nt.Renderer._p_rule_lines(stub, {"p_rule": {}}), [])
        with use("en"):
            h = pw.p_rule_html(v)
            for want in ("Buy held back", "no accepted reason", "at most", "negative ev", "no usable plan levels",
                         "no analysis with p"):
                self.assertIn(want, h)
            card = pw.PanelApp._decision_card(None, {"valid": True, "hold": True, "targets": {"USDT_IRT": 1.0},
                                                     "p_rule": v})
            self.assertIn("Probability rule", card)
        with use("fa"):
            h = pw.p_rule_html(v)
            self.assertIn(u"خرید انجام نشد", h)
            self.assertIn(u"دلیل مجازی نداشت", h)
        self.assertIn("kimi:brain.p_rule", [f.key for f in ps.FIELDS])


if __name__ == "__main__":
    unittest.main()
