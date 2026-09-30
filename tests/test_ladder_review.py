"""Regression tests of the ladder-round review findings on the runner side (scratch/review_ladder_money_1,
review_ladder_schedule_1): a Kimi decision planned on holdings read before a 30-min call, code exits
whose direct COIN_USDT sell is refused, the target sell of a fill placed before the W1 review call,
fractional ladder levels in config.json, one exit alert per position, and the code exits shown to Kimi.

Same fixtures as tests/test_ladder.py (fake exchange, PaperBroker, fake clock, KimiBrain contract
double); no network, no credentials.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_ladder as tl  # noqa: E402
from bitpin.api import BitpinAPIError  # noqa: E402
from bitpin.broker import PaperBroker  # noqa: E402
from bitpin.markets import ZERO, D  # noqa: E402
from bitpin.runner import load_config  # noqa: E402


class RejectingBroker(PaperBroker):
    """The exchange answers HTTP 400 to market SELLs on the listed markets (e.g. below a real,
    unpublished minimum, or a suspended market the 6 h market cache still shows as trading)."""
    reject = ("BTC_USDT",)

    def market_sell(self, symbol, base_amount, ref_price=None, book=None):
        if symbol in self.reject:
            raise BitpinAPIError(400, "min_amount", {"detail": "below minimum"})
        return PaperBroker.market_sell(self, symbol, base_amount, ref_price, book)


class LadderReviewBase(tl.Base):
    CFG = {"ladder": {"levels_pct": [-20]}}

    def stop_setup(self):
        """The BTC -20% bid fills, Kimi gives the position a 12% stop (v3: no default stop), then BTC
        closes below it."""
        self.runner(self.CFG).run_once()
        self.ex.set(BTC=80000 * 0.79)
        self.hour()
        self.runner(self.CFG).run_once()
        self.set_stop(cfg=self.CFG)
        self.held0 = self.bal()["BTC"]
        self.assertGreater(self.held0, 0)
        self.ex.set(BTC=64000 * 0.87)

    def rejecting_runner(self, reject=("BTC_USDT",), cfg=None):
        r = self.runner(cfg if cfg is not None else self.CFG)
        broker = RejectingBroker(tl.MARKETS, self.dir, book_source=self.ex.book, clock=self.clock)
        broker.reject = tuple(reject)
        broker.track({"BTC", "USDT", "IRT"})
        r.broker = broker
        return r


class StaleUnitsTest(LadderReviewBase):
    def test_a_ladder_fill_during_the_kimi_call_makes_the_decision_stale(self):
        """Review finding (money, medium): the plan used holdings read BEFORE the (up to ~30 min) news +
        Kimi call; a ladder fill during the call was invisible and the drift guard compared the
        decision with the same stale weights."""
        self.runner().run_once()                        # 8 bids lock the USDT
        self.hour()
        self.brain.due = True
        self.brain.next = [tl.decision({"USDT_IRT": 0.5, "ETH_IRT": 0.5})]
        orig = self.brain.decide
        holder = {}

        def decide(*a, **kw):
            # while Kimi thinks, BTC crashes 21% and the exchange fills the -20% bid
            self.ex.set(BTC=80000 * 0.79)
            holder["r"].broker.sync_limits()
            return orig(*a, **kw)
        self.brain.decide = decide
        r = self.runner()
        holder["r"] = r
        rep = r.run_once()
        self.assertEqual((rep["brain"]["action"], rep["brain"]["decision_status"]), ("stale", "stale"),
                         rep["brain"])
        self.assertIn("during the Kimi call", rep["brain"]["why"])
        self.assertEqual(rep["fills"], [])                                # no ETH bought on stale weights
        self.assertEqual(self.bal().get("ETH", ZERO), ZERO)
        self.assertGreater(self.bal()["BTC"], 0)                          # the ladder fill is booked ...
        self.assertIn("BTC_IRT", self.runner()._ladder_pos)               # ... and guarded by its exits

    def test_without_a_fill_during_the_call_the_decision_executes(self):
        self.runner().run_once()
        self.hour()
        self.brain.due = True
        self.brain.next = [tl.decision({"USDT_IRT": 0.7, "BTC_IRT": 0.3})]
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "kimi")
        self.assertIn(("BTC_USDT", "buy"), [(f["symbol"], f["side"]) for f in rep["fills"]])


class DirectRouteFallbackTest(LadderReviewBase):
    def test_a_rejected_direct_stop_sells_through_the_toman_route_in_the_same_hour(self):
        """Review finding (money, medium): an HTTP 4xx on the direct BTC_USDT sell left the position
        below its stop, hour after hour."""
        self.stop_setup()
        self.hour()
        r = self.rejecting_runner()
        rep = r.run_once()
        self.assertEqual([(x["symbol"], x["reason"]) for x in rep["exits"]], [("BTC_IRT", "stop")])
        self.assertEqual([(f["symbol"], f["side"], f["reason"]) for f in rep["fills"]],
                         [("BTC_IRT", "sell", "stop"), ("USDT_IRT", "buy", "stop")])
        self.assertLess(self.bal().get("BTC", ZERO), D("0.0000001"))
        self.assertTrue(r._direct_blocked("BTC", "sell"))                 # remembered (REJECT_BACKOFF)
        self.assertTrue(self.runner(self.CFG)._direct_blocked("BTC", "sell"))   # ... across restarts
        self.assertFalse(r._direct_blocked("BTC", "buy"))
        recent = self.runner(self.CFG)._recent_exits_view(self.clock.t)
        self.assertEqual([(x["symbol"], x["reason"]) for x in recent], [("BTC_IRT", "stop")])

    def test_a_dislocated_usdt_book_does_not_block_the_stop(self):
        """Review finding (money, medium): the vet refused the direct leg (price sanity 7% > 5%) and
        nothing was sold, although the toman route worked."""
        self.stop_setup()
        self.hour()
        p = 64000 * 0.87 * 1.07
        self.ex.book_override["BTC_USDT"] = {"asks": [[repr(p * 1.001), "100"]], "bids": [[repr(p * 0.999), "100"]]}
        rep = self.runner(self.CFG).run_once()
        self.assertEqual([(f["symbol"], f["side"]) for f in rep["fills"]], [("BTC_IRT", "sell"), ("USDT_IRT", "buy")])
        self.assertLess(self.bal().get("BTC", ZERO), D("0.0000001"))

    def test_an_allocation_leg_the_vet_would_refuse_keeps_the_toman_route(self):
        p = 80000 * 1.07
        self.ex.book_override["BTC_USDT"] = {"asks": [[repr(p * 1.001), "100"]], "bids": [[repr(p * 0.999), "100"]]}
        self.brain.due = True
        self.brain.next = [tl.decision({"USDT_IRT": 0.7, "BTC_IRT": 0.3})]
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "kimi")
        self.assertEqual([(f["symbol"], f["side"]) for f in rep["fills"]], [("USDT_IRT", "sell"), ("BTC_IRT", "buy")])

    def test_a_direct_route_the_exchange_rejected_is_not_chosen_again(self):
        r = self.runner()
        r._direct_rejected("BTC_USDT", "buy", "HTTP 400 test")
        self.brain.due = True
        self.brain.next = [tl.decision({"USDT_IRT": 0.7, "BTC_IRT": 0.3})]
        rep = self.runner().run_once()
        self.assertEqual([(f["symbol"], f["side"]) for f in rep["fills"]], [("USDT_IRT", "sell"), ("BTC_IRT", "buy")])
        # after the backoff the direct route is used again
        self.clock.t += 7 * 24 * 3600
        self.assertFalse(self.runner()._direct_blocked("BTC", "buy"))


class ExitAlertTest(LadderReviewBase):
    def test_a_stop_that_cannot_sell_alerts_once_and_is_retried_every_hour(self):
        """Review finding (schedule, low): the exit event was keyed per bar, so a sale that kept failing
        sent the owner a new "selling it now" alert every hour."""
        self.stop_setup()
        for _ in range(3):
            self.hour()
            rep = self.rejecting_runner(reject=("BTC_USDT", "BTC_IRT")).run_once()
            self.assertEqual([(x["symbol"], x.get("not_sold")) for x in rep["exits"]], [("BTC_IRT", True)])
        self.assertEqual(self.bal()["BTC"], self.held0)
        ids = [e["id"] for e in tl.read_events(self.dir) if e["kind"] == "exit"]
        self.assertEqual((len(ids), len(set(ids))), (3, 1))               # the notifier sends it once
        self.assertEqual(self.runner(self.CFG)._recent_exits_view(self.clock.t), [])   # nothing was sold


class EarlyTargetTest(LadderReviewBase):
    def test_the_target_sell_rests_before_the_review_call_the_fill_triggers(self):
        """Review finding (money, low): the spec says "code attaches exits at once", but the target sell
        was placed only after the W1 review call (news + decision, up to ~30 min)."""
        self.runner().run_once()
        self.ex.set(BTC=80000 * 0.79)
        self.hour()
        self.brain.due = lambda now, ladder, positions: bool(ladder and any(
            b["status"] == "filled" for c in ladder["coins"].values() for b in c["bids"]))
        self.brain.mode_next = self.brain.kind_next = "review"
        self.brain.next = [tl.decision({"USDT_IRT": 0.876, "BTC_IRT": 0.124}, hold=True)]
        orig = self.brain.decide
        seen, holder = {}, {}

        def decide(*a, **kw):
            seen["targets"] = [(v["symbol"], float(v["price"])) for v in self.targets(broker=holder["r"].broker)]
            return orig(*a, **kw)
        self.brain.decide = decide
        r = self.runner()
        holder["r"] = r
        r.run_once()
        self.assertEqual(seen["targets"], [("BTC_USDT", 72000.0)])
        self.assertEqual(len(self.targets()), 1)                          # not doubled by the maintenance


class RecentExitsWiringTest(LadderReviewBase):
    def test_a_code_stop_is_shown_to_the_next_decision(self):
        self.stop_setup()
        self.hour()
        self.runner(self.CFG).run_once()                                  # the stop sells BTC
        self.hour()
        self.brain.due = True
        self.brain.next = [tl.decision({"USDT_IRT": 1.0}, hold=True)]
        self.runner(self.CFG).run_once()
        ex = self.builder.builds[-1]["recent_exits"]
        self.assertEqual([(x["symbol"], x["reason"]) for x in ex], [("BTC_IRT", "stop")])
        self.assertAlmostEqual(ex[0]["pnl_pct"], -13.0, delta=0.5)
        self.assertAlmostEqual(ex[0]["entry_px_usdt"], 64000.0)

    def test_a_target_fill_is_a_recent_exit_too(self):
        self.runner().run_once()
        self.ex.set(BTC=80000 * 0.79)
        self.hour()
        self.runner().run_once()
        self.ex.set(BTC=80000 * 0.93)                                     # through the 72,000 target
        self.hour()
        self.runner().run_once()
        recent = self.runner()._recent_exits_view(self.clock.t)
        self.assertEqual([(x["symbol"], x["reason"]) for x in recent], [("BTC_IRT", "target")])
        self.assertAlmostEqual(recent[0]["pnl_pct"], 12.5, delta=0.1)


class FractionalLevelsConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_cfg_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_fractional_ladder_levels_load_from_a_file(self):
        """Review finding (money, low): read_json parses 17.5 as Decimal and the deep copy through
        json.dumps crashed load_config (TypeError, a traceback restart loop)."""
        p = os.path.join(self.dir, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ladder": {"levels_pct": [-17.5, -22.5], "size_frac": 0.1},
                       "exits": {"reprice_pct": 0.75}, "routing": {"coins": ["BTC", "ETH"]}}, f)
        cfg = load_config(p)
        self.assertEqual(cfg["ladder"]["levels_pct"], [-17.5, -22.5])
        self.assertTrue(all(isinstance(x, float) for x in cfg["ladder"]["levels_pct"]))
        self.assertAlmostEqual(float(cfg["ladder"]["size_frac"]), 0.1)
        json.dumps(cfg, default=str)


if __name__ == "__main__":
    unittest.main()
