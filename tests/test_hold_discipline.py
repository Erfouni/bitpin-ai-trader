"""v3.11 HOLD DISCIPLINE and the re-entry cooldown (study 07, owner 2026-10-03): a held coin whose plan stands
is not sold by a scheduled / held_move decision; an expired plan keeps its invalidation; a stop above the
invalidation is dropped; exit_news must quote a NEWS BRIEF headline that names the coin; after a stop or a
decision's sale no buy back within 72 h unless the coin is 3% cheaper. No network: fake Kimi transports."""
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_brain as tb  # noqa: E402
from bitpin.brain import (HOLD_MODES, MODES, names_coin, parse_exit_news, protected_positions,  # noqa: E402
                          resolve_limits, validate_brain_config, validate_response)
from bitpin.llm import ConfigError  # noqa: E402
from bitpin.runner import Runner  # noqa: E402

logging.getLogger("bitpin").addHandler(logging.NullHandler())

H = 3600
FULL = resolve_limits("full")
ALLOWED3 = ["USDT_IRT", "BTC_IRT", "ETH_IRT"]
PX = {"USDT_IRT": 200000.0, "BTC_IRT": 80000.0, "ETH_IRT": 3000.0}
NOW = tb.T0
HEAD_SOL = "Hackers drain 40 million dollars from a Solana lending protocol bridge"
HEAD_BTC = "Bitpin suspends Bitcoin withdrawals after a wallet incident"
HEAD_MARKET = "Crypto market slides as the Fed holds rates and ETF outflows grow"


def btc_position(inv=76000.0, tp=90000.0, entry=80000.0, set_at=None, horizon=720, **plan_extra):
    plan = {"setup": "trend_continuation", "horizon_hours": horizon, "invalidation_usdt": inv,
            "take_profit_usdt": tp, "note": "trend above the 4h EMAs", "set_at": NOW - 2 * H if set_at is None else set_at}
    plan.update(plan_extra)
    return {"BTC_IRT": {"source": "kimi", "entry_ts": NOW - 2 * H, "entry_px_usdt": entry, "amount": 0.01,
                        "plan": plan}}


def obj(targets, **extra):
    d = {"targets": targets, "cash_irt": 0.0, "confidence": 0.6, "reasoning": "r"}
    d.update(extra)
    return d


class TestProtectedPositions(unittest.TestCase):
    def test_a_standing_plan_protects_the_coin_in_the_full_decision_modes_only(self):
        pos = btc_position()
        for mode in MODES:
            got = protected_positions(pos, PX, mode)
            if mode in HOLD_MODES:
                self.assertIn("BTC_IRT", got, mode)
                self.assertIn("sold only after an hourly close below its invalidation 76000 USDT", got["BTC_IRT"])
                self.assertIn("at its take profit 90000 USDT", got["BTC_IRT"])
            else:
                self.assertEqual(got, {}, mode)        # review / veto / risk_reduce / final keep their sales
        self.assertEqual(protected_positions(pos, PX, "scheduled", hold_discipline=False), {})
        self.assertEqual(protected_positions(None, PX, "scheduled"), {})

    def test_a_broken_plan_a_reached_target_no_plan_or_a_ladder_lot_are_not_protected(self):
        self.assertEqual(protected_positions(btc_position(broken_at=NOW - H), PX, "scheduled"), {})
        self.assertEqual(protected_positions(btc_position(tp=79000.0), PX, "scheduled"), {})   # px 80000 >= tp
        no_plan = btc_position()
        no_plan["BTC_IRT"].pop("plan")
        self.assertEqual(protected_positions(no_plan, PX, "scheduled"), {})
        ladder = {"BTC_IRT": {"source": "ladder", "entry_px_usdt": 64000.0, "amount": 0.01}}
        self.assertEqual(protected_positions(ladder, PX, "scheduled"), {})
        no_tp = protected_positions(btc_position(tp=None), PX, "scheduled")
        self.assertIn("at its take profit (none set)", no_tp["BTC_IRT"])

    def test_an_expired_plan_still_protects(self):
        old = btc_position(set_at=NOW - 900 * H, horizon=720)                  # past its horizon
        self.assertIn("BTC_IRT", protected_positions(old, PX, "held_move"))


class TestExitNews(unittest.TestCase):
    def test_names_coin(self):
        self.assertTrue(names_coin(HEAD_SOL, "SOL"))
        self.assertTrue(names_coin(HEAD_BTC, "BTC"))
        self.assertTrue(names_coin("XRP lawsuit: the court rules against Ripple", "XRP"))
        self.assertFalse(names_coin(HEAD_MARKET, "BTC"))
        self.assertFalse(names_coin("Solana-like chains rally", "BTC"))
        self.assertFalse(names_coin("Bitcoin rallies", ""))

    def test_only_a_quoted_headline_naming_the_coin_counts(self):
        notes = []
        heads = [HEAD_SOL, HEAD_BTC, HEAD_MARKET]
        allowed = {"USDT_IRT", "BTC_IRT", "SOL_IRT"}
        got = parse_exit_news({"BTC_IRT": HEAD_BTC.upper(), "SOL_IRT": "drain 40 million dollars from a Solana lending"},
                              allowed, "USDT_IRT", heads, notes)
        self.assertEqual(sorted(got), ["BTC_IRT", "SOL_IRT"])
        self.assertEqual(notes, [])
        notes = []
        got = parse_exit_news({"BTC_IRT": HEAD_MARKET, "SOL_IRT": "Solana got hacked badly today",
                               "USDT_IRT": HEAD_BTC, "PEPE_IRT": HEAD_BTC}, allowed, "USDT_IRT", heads, notes)
        self.assertEqual(got, {})
        joined = " | ".join(notes)
        self.assertIn("exit_news BTC_IRT ignored: the headline does not name BTC", joined)
        self.assertIn("exit_news SOL_IRT ignored: the quote is not a headline of the NEWS BRIEF", joined)
        self.assertIn("exit_news 'USDT_IRT' ignored: not a coin of this account", joined)
        notes = []
        self.assertEqual(parse_exit_news({"BTC_IRT": HEAD_BTC}, allowed, "USDT_IRT", [], notes), {})
        self.assertIn("this decision has no NEWS BRIEF items", notes[0])
        notes = []
        self.assertEqual(parse_exit_news(["BTC_IRT"], allowed, "USDT_IRT", heads, notes), {})
        self.assertIn("must be an object", notes[0])
        self.assertEqual(parse_exit_news(None, allowed, "USDT_IRT", heads, []), {})


class TestValidateHold(unittest.TestCase):
    CUR = {"USDT_IRT": 0.35, "BTC_IRT": 0.65}

    def v(self, o, positions=None, **kw):
        positions = btc_position() if positions is None else positions
        kw.setdefault("protected", protected_positions(positions, PX, kw.get("mode", "scheduled")))
        return validate_response(o, dict(self.CUR), ALLOWED3, "USDT_IRT", FULL, rebalance_threshold=0.02,
                                 positions=positions, px_usdt=dict(PX), now=NOW, **kw)

    def test_a_sale_of_a_protected_coin_is_cut_back_to_its_weight(self):
        out = self.v(obj({"USDT_IRT": 1.0}))
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.65, places=6)
        self.assertAlmostEqual(out["targets"]["USDT_IRT"], 0.35, places=6)
        self.assertTrue(out["hold"])
        self.assertTrue(any("BTC_IRT kept at its current weight (no sale): its plan protects it" in a
                            for a in out["adjustments"]), out["adjustments"])
        # a trim is a sale too
        out = self.v(obj({"USDT_IRT": 0.6, "BTC_IRT": 0.4}))
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.65, places=6)

    def test_a_switch_into_another_coin_is_paid_from_usdt_then_cut(self):
        out = self.v(obj({"USDT_IRT": 0.35, "ETH_IRT": 0.65}))
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.65, places=6)
        self.assertAlmostEqual(out["targets"]["ETH_IRT"], 0.35, places=6)      # what the USDT could pay
        self.assertAlmostEqual(out["targets"]["USDT_IRT"], 0.0, places=6)
        self.assertAlmostEqual(sum(out["targets"].values()) + out["cash_irt"], 1.0, places=6)

    def test_the_modes_that_may_sell_and_a_raise_are_untouched(self):
        out = self.v(obj({"USDT_IRT": 1.0}), mode="risk_reduce")
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=6)
        out = self.v(obj({"USDT_IRT": 0.25, "BTC_IRT": 0.75}))                 # adding is allowed (the hurdle's job)
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.75, places=6)

    def test_exit_news_naming_the_coin_lifts_the_protection(self):
        out = self.v(obj({"USDT_IRT": 1.0}, exit_news={"BTC_IRT": HEAD_BTC}), news_headlines=[HEAD_MARKET, HEAD_BTC])
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.0, places=6)
        self.assertEqual(out["exit_news"], {"BTC_IRT": " ".join(HEAD_BTC.lower().split())})
        out = self.v(obj({"USDT_IRT": 1.0}, exit_news={"BTC_IRT": HEAD_MARKET}), news_headlines=[HEAD_MARKET])
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.65, places=6)       # a market-wide story does not
        self.assertTrue(any("does not name BTC" in a for a in out["adjustments"]))

    def test_a_stop_above_the_invalidation_is_dropped_a_lower_one_kept(self):
        pos = btc_position(entry=85000.0)
        out = self.v(obj({"USDT_IRT": 0.35, "BTC_IRT": 0.65}, exits={"BTC_IRT": {"stop_pct": 5}}), positions=pos)
        self.assertIsNone(out["exits"]["BTC_IRT"].get("stop_pct"))
        self.assertTrue(any("stop_pct 5 dropped: a stop above the plan's invalidation 76000 USDT" in a
                            for a in out["adjustments"]), out["adjustments"])
        out = self.v(obj({"USDT_IRT": 0.35, "BTC_IRT": 0.65}, exits={"BTC_IRT": {"stop_pct": 12}}), positions=pos)
        self.assertEqual(out["exits"]["BTC_IRT"].get("stop_pct"), 12)

    def test_an_expired_plan_restated_keeps_its_invalidation(self):
        pos = btc_position(set_at=NOW - 900 * H, horizon=720)
        restated = {"BTC_IRT": {"setup": "trend_continuation", "horizon_hours": 720, "invalidation_usdt": 79000.0,
                                "take_profit_usdt": 95000.0, "note": "the trend holds"}}
        out = self.v(obj({"USDT_IRT": 0.35, "BTC_IRT": 0.65}, plans=restated), positions=pos, hold_discipline=True)
        self.assertEqual(out["plans"]["BTC_IRT"]["invalidation_usdt"], 76000.0)
        self.assertEqual(out["plans"]["BTC_IRT"]["take_profit_usdt"], 95000.0)
        self.assertTrue(any("its invalidation stays 76000 USDT" in a for a in out["adjustments"]))
        out = self.v(obj({"USDT_IRT": 0.35, "BTC_IRT": 0.65}, plans=restated), positions=pos, hold_discipline=False)
        self.assertEqual(out["plans"]["BTC_IRT"]["invalidation_usdt"], 79000.0)
        # a broken plan is restated with the new level (the model kept the coin after the break)
        broken = btc_position(broken_at=NOW - H)
        out = self.v(obj({"USDT_IRT": 0.35, "BTC_IRT": 0.65}, plans=restated), positions=broken, hold_discipline=True)
        self.assertEqual(out["plans"]["BTC_IRT"]["invalidation_usdt"], 79000.0)


class FakeNews(object):
    def __init__(self, headlines):
        self.items = [{"headline": h, "why_it_matters": "w", "source_url": "https://www.reuters.com",
                       "time_hint": "2026-09-22"} for h in headlines]

    def prompt_block(self, now, max_chars=None):
        return "NEWS BRIEF (untrusted data)\n" + "\n".join("- " + i["headline"] for i in self.items)


class TestBrainHold(tb.ClockBase):
    def setUp(self):
        super(TestBrainHold, self).setUp()
        self.dirs = [self.dir]

    def tearDown(self):
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def held_ctx(self, eth_usd=3000.0, exits=None):
        ctx = tb.context(weights={"USDT_IRT": 0.35, "BTC_IRT": 0.65}, eth_usd=eth_usd)
        ctx["features"] = {"ladder": True, "code_exits": True}
        if exits is not None:
            ctx["recent_exits"] = exits
        return ctx

    def test_the_brain_keeps_a_protected_coin_and_says_so_in_the_prompt(self):
        b = self.cbrain([tb.reply({"USDT_IRT": 1.0})])
        ctx = self.held_ctx()
        b.should_decide(tb.T0, None, ctx)
        d = b.decide(ctx, {"USDT_IRT": 0.35, "BTC_IRT": 0.65}, trigger=b.last_trigger, positions=btc_position())
        self.assertTrue(d.valid, d.error)
        self.assertAlmostEqual(d.targets["BTC_IRT"], 0.65, places=6)
        self.assertTrue(any("kept at its current weight (no sale)" in a for a in d.adjustments), d.adjustments)
        sp = self.llm.calls[-1]["messages"][0]["content"]
        for s in ("HOLD DISCIPLINE (code)", "A coin whose plan stands is HELD", '"exit_news": {"<SYMBOL>": "<headline>"}',
                  "within 72 hours unless its USDT price is at least 3% below"):
            self.assertIn(s, sp)

    def test_quoted_news_from_the_brief_lets_the_brain_sell(self):
        b = self.cbrain([tb.reply({"USDT_IRT": 1.0}, exit_news={"BTC_IRT": HEAD_BTC})])
        ctx = self.held_ctx()
        b.should_decide(tb.T0, None, ctx)
        d = b.decide(ctx, {"USDT_IRT": 0.35, "BTC_IRT": 0.65}, trigger=b.last_trigger, positions=btc_position(),
                     news=FakeNews([HEAD_MARKET, HEAD_BTC]))
        self.assertTrue(d.valid, d.error)
        self.assertAlmostEqual(d.targets.get("BTC_IRT", 0.0), 0.0, places=6)

    def test_off_restores_the_old_rules(self):
        b = self.cbrain([tb.reply({"USDT_IRT": 1.0})], hold_discipline=False, reentry_cooldown_hours=0)
        ctx = self.held_ctx()
        b.should_decide(tb.T0, None, ctx)
        d = b.decide(ctx, {"USDT_IRT": 0.35, "BTC_IRT": 0.65}, trigger=b.last_trigger, positions=btc_position())
        self.assertAlmostEqual(d.targets.get("BTC_IRT", 0.0), 0.0, places=6)
        sp = self.llm.calls[-1]["messages"][0]["content"]
        self.assertIn("Do not exit or reverse a position before its horizon", sp)
        self.assertNotIn("HOLD DISCIPLINE", sp)
        self.assertNotIn("exit_news", sp)
        self.assertNotIn("After a stop or a decision's sale", sp)

    def buy_eth(self, exits, eth_usd=3000.0, now=None):
        self.dir = tempfile.mkdtemp(prefix="holdtest_")          # a fresh brain each time: the first decision (a slot)
        self.dirs.append(self.dir)
        b = self.cbrain([tb.reply({"USDT_IRT": 0.6, "ETH_IRT": 0.4})])
        ctx = tb.context(eth_usd=eth_usd)
        ctx["recent_exits"] = exits
        b.should_decide(tb.T0, None, ctx)
        return b.decide(ctx, {"USDT_IRT": 1.0}, trigger=b.last_trigger)

    def test_no_buy_back_within_72_h_of_a_sale_unless_3_pct_cheaper(self):
        sale = {"symbol": "ETH_IRT", "reason": "sold", "ago_h": 10.0, "px": 3000.0, "entry": 2900.0, "pnl_pct": 3.4}
        d = self.buy_eth([sale])
        self.assertAlmostEqual(d.targets.get("ETH_IRT", 0.0), 0.0)
        self.assertTrue(any("sold 10 h ago at 3000 USDT: no buy back within 72 h unless its price is at or below 2910 "
                            "USDT" in a for a in d.adjustments), d.adjustments)
        self.assertAlmostEqual(self.buy_eth([sale], eth_usd=2905.0).targets["ETH_IRT"], 0.4)     # 3% cheaper: free
        self.assertAlmostEqual(self.buy_eth([dict(sale, ago_h=73.0)]).targets["ETH_IRT"], 0.4)   # after 72 h
        # a stop sale: 24 h absolute, then the cooldown; a target sale: no cooldown at a slot
        stop = dict(sale, reason="stop", ago_h=30.0)
        self.assertAlmostEqual(self.buy_eth([stop]).targets.get("ETH_IRT", 0.0), 0.0)
        target = dict(sale, reason="target", ago_h=10.0)
        self.assertAlmostEqual(self.buy_eth([target]).targets["ETH_IRT"], 0.4)

    def test_the_settings_are_checked(self):
        base = {"allowed_symbols": tb.ALLOWED}
        cfg = validate_brain_config(dict(base))
        self.assertEqual((cfg["hold_discipline"], cfg["reentry_cooldown_hours"], cfg["reentry_waive_pct"]),
                         (True, 72, 3.0))
        for bad in ({"hold_discipline": "yes"}, {"reentry_cooldown_hours": 200}, {"reentry_cooldown_hours": -1},
                    {"reentry_waive_pct": 25}):
            with self.assertRaises(ConfigError):
                validate_brain_config(dict(base, **bad))
        self.assertEqual(validate_brain_config(dict(base, reentry_cooldown_hours=0))["reentry_cooldown_hours"], 0)


class TestRunnerSale(unittest.TestCase):
    def stub(self, now, exits=()):
        st = types.SimpleNamespace(clock=lambda: now, _recent_exits=list(exits), saved=0)
        st._save_bot_state = lambda: None
        st._note_exit = lambda *a, **k: Runner._note_exit(st, *a, **k)
        return st

    def test_a_closed_position_is_remembered_as_sold_unless_a_code_exit_was_the_sale(self):
        lots = [("ETH_IRT", "main", {"entry_px_usdt": 2900.0, "amount": 0.1})]
        st = self.stub(NOW)
        Runner._note_sale(st, "ETH_IRT", 3000.0, lots)
        self.assertEqual(len(st._recent_exits), 1)
        r = st._recent_exits[0]
        self.assertEqual((r["symbol"], r["reason"], r["px_usdt"], r["entry_px_usdt"]), ("ETH_IRT", "sold", 3000.0, 2900.0))
        self.assertAlmostEqual(r["pnl_pct"], 3.45, places=2)
        st = self.stub(NOW, [{"symbol": "ETH_IRT", "reason": "stop", "t": NOW - H, "px_usdt": 2600.0}])
        Runner._note_sale(st, "ETH_IRT", 2600.0, lots)
        self.assertEqual(len(st._recent_exits), 1)                          # the stop was the sale
        st = self.stub(NOW, [{"symbol": "ETH_IRT", "reason": "stop", "t": NOW - 5 * H, "px_usdt": 2600.0}])
        Runner._note_sale(st, "ETH_IRT", 2700.0, lots)
        self.assertEqual([x["reason"] for x in st._recent_exits], ["stop", "sold"])


class FakePrices(object):
    def __init__(self, px):
        self.px = px

    def usdt(self, asset, t0, t1):
        return self.px.get(asset)


class TestPanelHold(unittest.TestCase):
    """v3.11 in the panel: the report's hold state and pauses (bitpin/performance.py), the position cards and the
    "Sold lately" card (bitpin/panel_web.py), in English and in Persian."""

    STATE = {"recent_exits": [
        {"symbol": "ETH_IRT", "reason": "sold", "t": NOW - 10 * H, "px_usdt": 3000.0},
        {"symbol": "SOL_IRT", "reason": "target", "t": NOW - 5 * H, "px_usdt": 150.0},       # no pause
        {"symbol": "XRP_IRT", "reason": "stop", "t": NOW - 80 * H, "px_usdt": 2.0},          # older than 72 h
        {"symbol": "ETH_IRT", "reason": "stop", "t": NOW - 30 * H, "px_usdt": 2800.0}],      # the newer sale wins
        "positions": {"BTC_IRT": {"plan": {"invalidation_usdt": 76000.0}}}}

    def test_pauses_and_hold_states(self):
        from bitpin import performance as perf
        rules = dict(perf.DEFAULT_RULES)
        pz = perf.sales_pauses(self.STATE, rules, FakePrices({"ETH": 2950.0}), NOW)
        self.assertEqual([(x["asset"], x["reason"]) for x in pz], [("ETH", "sold")])
        self.assertEqual(pz[0]["until"], NOW - 10 * H + 72 * H)
        self.assertAlmostEqual(pz[0]["free_price_usdt"], 2910.0)
        self.assertFalse(pz[0]["free"])
        self.assertTrue(perf.sales_pauses(self.STATE, rules, FakePrices({"ETH": 2900.0}), NOW)[0]["free"])
        self.assertEqual(perf.sales_pauses(self.STATE, dict(rules, reentry_cooldown_hours=0), FakePrices({}), NOW), [])
        pos = [{"asset": "BTC", "held": True, "invalidation_usdt": 76000.0, "target_usdt": 90000.0, "price_usdt": 80000.0},
               {"asset": "ETH", "held": False, "invalidation_usdt": None}]
        perf.hold_rules(pos, self.STATE, rules, pz)
        self.assertEqual((pos[0]["hold"], pos[1]["hold"]), ("protected", None))
        self.assertEqual(pos[1]["pause"]["asset"], "ETH")
        pos[0]["price_usdt"] = 91000.0
        perf.hold_rules(pos, self.STATE, rules, pz)
        self.assertEqual(pos[0]["hold"], "target")
        broken = {"positions": {"BTC_IRT": {"plan": {"invalidation_usdt": 76000.0, "broken_at": NOW - H}}}}
        perf.hold_rules(pos, broken, rules, [])
        self.assertEqual(pos[0]["hold"], "broken")
        perf.hold_rules(pos, broken, dict(rules, hold_discipline=False), [])
        self.assertEqual(pos[0]["hold"], "off")

    def test_the_rules_come_from_the_newest_decision_record(self):
        import json as _json
        from bitpin import performance as perf
        d = tempfile.mkdtemp(prefix="holdrules_")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "kimi_decisions.jsonl")
        self.assertEqual(perf.read_rules(path), {})
        with open(path, "w") as f:
            f.write(_json.dumps({"rules": {"hold_discipline": True, "reentry_cooldown_hours": 72}}) + "\n")
            f.write(_json.dumps({"rules": {"hold_discipline": False, "reentry_cooldown_hours": 24,
                                           "reentry_waive_pct": 5}}) + "\n")
            f.write("{broken\n")
        self.assertEqual(perf.read_rules(path), {"hold_discipline": False, "reentry_cooldown_hours": 24.0,
                                                 "reentry_waive_pct": 5.0})

    def test_the_cards_say_when_the_bot_may_sell_and_buy_back(self):
        from bitpin import panel_web as pw
        from bitpin.panel_i18n import use
        base = {"asset": "BTC", "qty": 0.01, "value_irt": 1.0, "value_usdt": 1.0, "price_usdt": 80000.0,
                "entry_usdt": 80000.0, "stop_usdt": None, "target_usdt": 90000.0, "invalidation_usdt": 76000.0,
                "orders": [], "prices": [], "analysis": None, "outlook": {}, "max_hold_until": None, "set_at": None,
                "held": True, "hold": "protected"}
        pause = {"asset": "ETH", "reason": "sold", "t": NOW - 10 * H, "price_usdt": 3000.0, "until": NOW + 62 * H,
                 "free_price_usdt": 2910.0, "price_now_usdt": 2950.0, "free": False}
        with use("en"):
            t = pw.PanelApp._position_panel(base)
            self.assertIn("Sold only on", t)
            self.assertIn("an hourly close below", t)
            self.assertIn("76,000.00 USDT", t)
            self.assertIn("its target", t)
            t = pw.PanelApp._position_panel(dict(base, hold="broken"))
            self.assertIn("its invalidation broke: Kimi may sell it or restate the plan", t)
            t = pw.PanelApp._position_panel(dict(base, asset="ETH", held=False, hold=None, pause=pause))
            self.assertIn("Buy back", t)
            self.assertIn("unless at", t)
            self.assertIn("2,910.00 USDT", t)
            app = types.SimpleNamespace(_position_panel=pw.PanelApp._position_panel)
            sec = pw.PanelApp._positions_section(app, {"positions": [base], "pauses": [pause]})
            self.assertIn("Sold lately: a pause before buying back", sec)
            self.assertIn("decision", sec)
            self.assertNotIn("Sold lately", pw.PanelApp._positions_section(app, {"positions": [base]}))
        with use("fa"):
            t = pw.PanelApp._position_panel(base)
            self.assertIn(u"فروش فقط با", t)
            sec = pw.PanelApp._positions_section(app, {"positions": [base], "pauses": [pause]})
            self.assertIn(u"فروخته‌شده‌های اخیر: مکث پیش از خرید دوباره", sec)


if __name__ == "__main__":
    unittest.main()
