"""v3.17 CORE RULE: the code's BTC trend rule decides, Kimi is a shadow that may only veto a rise."""
import tempfile
import unittest

from bitpin import core_rule
from bitpin.brain import KimiBrain, validate_brain_config
from bitpin.llm import ConfigError, LLMError
from test_brain import ALLOWED, T0, FakeLLM, context, reply

CFG = dict(core_rule.DEFAULT_CORE, allocation="live")


def trend(up=True, days=260, start=60000.0):
    """Daily closes with a steady drift (+/-0.3% a day) and a small alternating wiggle (vol about 9%)."""
    px, out = start, []
    for i in range(days):
        px *= (1.003 if up else 0.997) * (1.005 if i % 2 else 0.995)
        out.append(px)
    return out


class PureRuleTests(unittest.TestCase):
    def test_uptrend_is_capped_and_downtrend_is_flat(self):
        w, info = core_rule.core_weight(trend(True), CFG)
        self.assertTrue(info["on"])
        self.assertAlmostEqual(w, CFG["cap"])          # low vol: the cap binds
        w, info = core_rule.core_weight(trend(False), CFG)
        self.assertFalse(info["on"])
        self.assertEqual(w, 0.0)

    def test_high_volatility_sizes_down(self):
        closes = trend(True)
        closes[-30:] = [c * (1.12 if i % 2 else 0.9) for i, c in enumerate(closes[-30:])]
        closes[-1] = max(closes) * 1.5                  # still above its average
        w, info = core_rule.core_weight(closes, CFG)
        self.assertTrue(info["on"])
        self.assertLess(w, CFG["cap"])
        self.assertAlmostEqual(w, round(CFG["target_vol"] / info["vol"], 4), places=3)

    def test_short_history_gives_no_weight(self):
        w, info = core_rule.core_weight(trend(True, days=120), CFG)
        self.assertIsNone(w)
        self.assertIn("need", info["error"])

    def test_band_holds_small_moves_but_never_skips_an_exit(self):
        self.assertEqual(core_rule.banded(0.6, 0.5, 0.15), 0.5)
        self.assertEqual(core_rule.banded(0.6, 0.4, 0.15), 0.6)
        self.assertEqual(core_rule.banded(0.0, 0.05, 0.15), 0.0)

    def test_veto_needs_a_known_multiplier_and_an_event(self):
        self.assertEqual(core_rule.parse_veto(None), (1.0, None))
        self.assertEqual(core_rule.parse_veto({"multiplier": 0, "event": "hack"}), (0.0, None))
        self.assertEqual(core_rule.parse_veto({"multiplier": 0.5, "event": "delisting"}), (0.5, None))
        for bad in ({"multiplier": 0}, {"multiplier": 0.3, "event": "hack"}, {"multiplier": True, "event": "hack"},
                    "no"):
            m, note = core_rule.parse_veto(bad)
            self.assertEqual(m, 1.0)
            self.assertIn("ignored", note)

    def test_config_is_checked(self):
        self.assertIsNone(core_rule.validate_core(None))
        for bad in ({"allocation": "real"}, {"cap": 1.5}, {"lookback_days": 5}, {"band": "x"}, {"typo": 1},
                    {"veto": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(core_rule.CoreConfigError):
                core_rule.validate_core(bad)
        with self.assertRaises(ConfigError):
            validate_brain_config({"allowed_symbols": ["USDT_IRT", "ETH_IRT"], "core": {}})

    def test_paper_equity_follows_the_held_weight_and_pays_costs(self):
        days = {d: p for d, p in enumerate([100.0, 110.0, 121.0])}
        self.assertAlmostEqual(core_rule.paper_equity([(0, 1.0)], days, 0.0), 1.21)
        self.assertAlmostEqual(core_rule.paper_equity([(0, 0.0)], days, 0.01), 1.0)
        self.assertAlmostEqual(core_rule.paper_equity([(0, 1.0)], days, 0.01), 0.99 * 1.21)


class BrainCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def brain(self, llm, allocation="live", closes=None, **core):
        cfg = {"allowed_symbols": ALLOWED, "p_rule": False, "require_plan": False, "max_validation_retries": 0,
               "core": dict(core, allocation=allocation)}
        b = KimiBrain(llm, cfg, self.tmp.name, clock=lambda: T0)
        b.core_closes = lambda sym, days, now: list(closes if closes is not None else trend(True))
        return b

    def test_live_rule_executes_and_kimi_targets_are_only_a_shadow(self):
        llm = FakeLLM([reply({"ETH_IRT": 0.5, "USDT_IRT": 0.5})])
        d = self.brain(llm).decide(context(), {"USDT_IRT": 1.0})
        self.assertTrue(d.valid, d.error)
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.6)
        self.assertEqual(d.targets["ETH_IRT"], 0.0)
        self.assertAlmostEqual(d.targets["USDT_IRT"], 0.4)
        self.assertGreater(d.shadow["targets"].get("ETH_IRT", 0.0), 0.0)   # Kimi's (validated) choice, not executed
        self.assertEqual(d.core["weight"], 0.6)
        self.assertEqual(set(d.ladder.values()), {0.0})
        self.assertEqual(len(llm.calls), 1)              # no second confirmation run for a shadow

    def test_paper_keeps_the_account_in_usdt_and_remembers_the_paper_weight(self):
        b = self.brain(FakeLLM([reply({"USDT_IRT": 1.0})]), allocation="paper")
        d = b.decide(context(), {"USDT_IRT": 1.0})
        self.assertTrue(d.valid, d.error)
        self.assertEqual(d.targets["BTC_IRT"], 0.0)
        self.assertEqual(d.targets["USDT_IRT"], 1.0)
        self.assertEqual(d.core["weight"], 0.6)
        b2 = self.brain(FakeLLM([]), allocation="paper")   # a restart reads the persisted paper weight
        self.assertEqual(b2._core_paper["weight"], 0.6)

    def test_a_failed_kimi_call_does_not_stop_the_rule(self):
        d = self.brain(FakeLLM([LLMError("network down")])).decide(context(), {"USDT_IRT": 1.0})
        self.assertTrue(d.valid, d.error)
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.6)
        self.assertFalse(d.shadow["valid"])
        self.assertIn("network", d.shadow["error"])

    def test_veto_halves_or_stops_a_rise_but_never_forces_a_sale(self):
        r = reply({"USDT_IRT": 1.0}, core_veto={"multiplier": 0.5, "event": "exchange_halt", "reason": "halt"})
        d = self.brain(FakeLLM([r])).decide(context(), {"USDT_IRT": 1.0})
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.3)
        r = reply({"USDT_IRT": 1.0}, core_veto={"multiplier": 0, "event": "hack", "reason": "exchange hacked"})
        d = self.brain(FakeLLM([r]), closes=trend(False)).decide(context(), {"USDT_IRT": 0.5, "BTC_IRT": 0.5})
        self.assertEqual(d.targets["BTC_IRT"], 0.0)       # the rule's exit stands
        r = reply({"USDT_IRT": 1.0}, core_veto={"multiplier": 0, "event": "bad", "reason": "x"})
        d = self.brain(FakeLLM([r])).decide(context(), {"USDT_IRT": 1.0})
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.6)
        self.assertTrue(any("ignored" in n for n in d.adjustments))

    def test_missing_daily_data_holds_the_current_weight(self):
        b = self.brain(FakeLLM([reply({"USDT_IRT": 1.0})]))
        def boom(*a):
            raise OSError("bitpin down")
        b.core_closes = boom
        d = b.decide(context(), {"USDT_IRT": 0.7, "BTC_IRT": 0.3})
        self.assertTrue(d.valid, d.error)
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.3)
        self.assertIn("unavailable", d.core["error"])

    def test_prompt_tells_kimi_it_is_a_shadow(self):
        llm = FakeLLM([reply({"USDT_IRT": 1.0})])
        self.brain(llm).decide(context(), {"USDT_IRT": 1.0})
        system = llm.calls[0]["messages"][0]["content"]
        self.assertIn("CORE RULE", system)
        self.assertIn("core_veto", system)


class DisplayTests(unittest.TestCase):
    RECORD_CORE = {"allocation": "paper", "symbol": "BTC_IRT", "lookback_days": 200, "close": 77800.0, "sma": 81000.0,
                   "distance_pct": -3.95, "vol": 0.42, "target": 0.0, "held": 0.0, "weight": 0.0, "veto": 1.0}

    def test_decision_view_carries_the_core_and_the_shadow(self):
        from bitpin.notify import core_view
        v = core_view(self.RECORD_CORE, {"valid": True, "targets": {"USDT_IRT": 0.9, "XAUT_IRT": 0.1}})
        self.assertEqual(v["allocation"], "paper")
        self.assertEqual(v["shadow_targets"]["XAUT_IRT"], 0.1)
        self.assertIsNone(core_view({}, {}))

    def test_panel_card_shows_the_rule_and_hides_when_off(self):
        from bitpin.notify import core_view
        from bitpin.panel_web import PanelApp
        html = PanelApp._core_card(None, {"core": core_view(self.RECORD_CORE, {"valid": False, "error_kind": "llm"})})
        self.assertIn("77800", html)
        self.assertIn("llm", html)
        self.assertEqual(PanelApp._core_card(None, {"core": None}), "")


if __name__ == "__main__":
    unittest.main()
