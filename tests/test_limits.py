"""Execution primitives: USDT-quoted markets, resting limit orders (paper + live, fake transports
only), cancel races, restart reconciliation, routing and the risk guards for limit / USDT orders."""
import csv
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin.api import BitpinAPIError, BitpinClient, OrderNotSent, OrderStatusUnknown, read_json  # noqa: E402
from bitpin.broker import (LIMIT_CANCELLED, LIMIT_FILLED, LIMIT_OPEN, LIMIT_PARTIAL, LIMIT_REJECTED,  # noqa: E402
                           LIMIT_UNKNOWN, BrokerError, InsufficientFunds, LimitWouldCross, LiveBroker, NotBotOrder,
                           OrderJournal, OrderTooSmall, PaperBroker, bars_range_source)
from bitpin.data import Bar  # noqa: E402
from bitpin.markets import (D, Market, MarketCache, RouteError, best_route, ceil_to_precision,  # noqa: E402
                            floor_to_precision, limit_crosses, parse_book, route_options, walk_limit)
from bitpin.risk import RiskManager  # noqa: E402

logging.getLogger("bitpin").addHandler(logging.NullHandler())

USDT_IRT = Market("USDT_IRT", "USDT", "IRT", True, False, 0, 2, 0)
BTC_IRT = Market("BTC_IRT", "BTC", "IRT", True, False, 0, 8, 0)
ETH_IRT = Market("ETH_IRT", "ETH", "IRT", True, False, 0, 5, 0)
XRP_IRT = Market("XRP_IRT", "XRP", "IRT", True, False, 0, 2, 0)          # no XRP_USDT here
BTC_USDT = Market("BTC_USDT", "BTC", "USDT", True, False, 2, 8, 2)
ETH_USDT = Market("ETH_USDT", "ETH", "USDT", True, True, 2, 5, 2)         # suspended
MARKETS = MarketCache(markets=[USDT_IRT, BTC_IRT, ETH_IRT, XRP_IRT, BTC_USDT, ETH_USDT])

T0 = 1790000000.0


def books():
    return {
        "USDT_IRT": {"asks": [["228600", "1000"]], "bids": [["228500", "1000"]]},
        "BTC_USDT": {"asks": [["86000", "0.5"], ["86100", "1"]], "bids": [["85900", "0.5"], ["85800", "1"]]},
        "BTC_IRT": {"asks": [["19700000000", "0.1"]], "bids": [["19600000000", "0.1"]]},
        "ETH_USDT": {"asks": [["3000", "5"]], "bids": [["2990", "5"]]},
        "ETH_IRT": {"asks": [["690000000", "5"]], "bids": [["680000000", "5"]]},
        "XRP_IRT": {"asks": [["360000", "5000"]], "bids": [["359000", "5000"]]},
    }


class Clock:
    def __init__(self, t=T0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


# --------------------------------------------------------------------------- markets: limit helpers, routing

class LimitHelpersTest(unittest.TestCase):
    def test_limit_price_rounds_in_the_safe_direction(self):
        self.assertEqual(BTC_USDT.limit_price("68800.129", "buy"), Decimal("68800.12"))
        self.assertEqual(BTC_USDT.limit_price("97000.001", "sell"), Decimal("97000.01"))
        self.assertEqual(BTC_IRT.limit_price("19700000000.9", "buy"), Decimal("19700000000"))
        with self.assertRaises(ValueError):
            BTC_USDT.limit_price("1", "hold")
        self.assertEqual(BTC_USDT.base_step, Decimal("1e-8"))
        self.assertEqual(BTC_USDT.quote_step, Decimal("0.01"))

    def test_crossing_check_against_best_bid_ask(self):
        b = parse_book(books()["BTC_USDT"])
        self.assertTrue(limit_crosses(b, "buy", "86000"))       # at the best ask: would take
        self.assertTrue(limit_crosses(b, "buy", "90000"))
        self.assertFalse(limit_crosses(b, "buy", "85999.99"))
        self.assertTrue(limit_crosses(b, "sell", "85900"))      # at the best bid
        self.assertFalse(limit_crosses(b, "sell", "85900.01"))
        self.assertFalse(limit_crosses(parse_book({"asks": [], "bids": [["1", "1"]]}), "buy", "5"))   # nothing to hit
        with self.assertRaises(ValueError):
            limit_crosses(b, "hold", "1")

    def test_walk_limit_takes_only_levels_within_the_price(self):
        b = parse_book(books()["BTC_USDT"])
        w = walk_limit(b, "buy", "2", "86050")
        self.assertEqual((w["base"], w["quote"], w["levels"]), (Decimal("0.5"), Decimal("43000.0"), 1))
        w = walk_limit(b, "sell", "0.2", "85000")
        self.assertEqual(w["base"], Decimal("0.2"))
        self.assertEqual(walk_limit(b, "buy", "1", "85000")["base"], 0)


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.b = books()
        self.calls = []

    def get_book(self, s):
        self.calls.append(s)
        if isinstance(self.b[s], Exception):
            raise self.b[s]
        return parse_book(self.b[s])

    def route(self, f, t, amount, **kw):
        return best_route(f, t, amount, self.get_book, MARKETS, **kw)

    def test_options(self):
        self.assertEqual(route_options("USDT", "BTC", MARKETS),
                         [("direct", [("BTC_USDT", "buy")]), ("via_irt", [("USDT_IRT", "sell"), ("BTC_IRT", "buy")])])
        self.assertEqual(route_options("btc", "usdt", MARKETS)[1][1], [("BTC_IRT", "sell"), ("USDT_IRT", "buy")])
        self.assertEqual(route_options("IRT", "BTC", MARKETS), [("single", [("BTC_IRT", "buy")])])
        self.assertEqual(route_options("USDT", "IRT", MARKETS), [("single", [("USDT_IRT", "sell")])])
        self.assertEqual(route_options("USDT", "XRP", MARKETS), [("via_irt", [("USDT_IRT", "sell"), ("XRP_IRT", "buy")])])
        for f, t in (("BTC", "ETH"), ("BTC", "BTC")):
            with self.assertRaises(RouteError):
                route_options(f, t, MARKETS)

    def test_direct_usdt_market_is_cheaper_and_numbers_are_exact(self):
        r = self.route("USDT", "BTC", "50")
        self.assertTrue(r["ok"])
        self.assertEqual(r["route"], "direct")
        gross = floor_to_precision(Decimal(50) / 86000, 8)
        self.assertEqual(r["est_out"], gross * (1 - Decimal("0.0035")))
        via = [a for a in r["alternatives"] if a["route"] == "via_irt"][0]
        irt = Decimal(50) * 228500 * (1 - Decimal("0.0035"))
        self.assertEqual(via["est_out"], floor_to_precision(floor_to_precision(irt, 0) / Decimal("19700000000"), 8)
                         * (1 - Decimal("0.0035")))
        self.assertLess(via["est_out"], r["est_out"])
        self.assertTrue(via["ok"])
        # all-in cost of the direct leg = fee + half spread (+ depth, none here)
        fair = Decimal(50) / Decimal("85950")
        self.assertEqual(r["exec_cost"], 1 - r["est_out"] / fair)
        self.assertTrue(Decimal("0.0035") < r["exec_cost"] < Decimal("0.0045"))
        self.assertEqual(r["cost_frac"], r["exec_cost"])             # the reference is the direct market
        self.assertGreater(via["cost_frac"], r["cost_frac"])
        self.assertEqual(self.calls.count("USDT_IRT"), 1)             # each book read once

    def test_wide_direct_spread_routes_via_toman(self):
        self.b["BTC_USDT"] = {"asks": [["90000", "1"]], "bids": [["85000", "1"]]}
        r = self.route("USDT", "BTC", "50")
        self.assertEqual((r["ok"], r["route"], len(r["legs"])), (True, "via_irt", 2))
        self.assertEqual(r["legs"][1]["amount_in"], floor_to_precision(r["legs"][0]["est_out"], 0))

    def test_thin_direct_book_falls_back_to_toman(self):
        self.b["BTC_USDT"] = {"asks": [["86000", "0.0001"]], "bids": [["85900", "1"]]}
        r = self.route("USDT", "BTC", "50")
        self.assertEqual(r["route"], "via_irt")
        direct = r["alternatives"][0]
        self.assertFalse(direct["ok"])
        self.assertIn("too thin", direct["reason"])

    def test_below_minimum_is_infeasible(self):
        r = self.route("USDT", "BTC", "0.4", min_notional={"USDT": "0.5", "IRT": 100000})
        self.assertFalse(r["ok"])
        self.assertIn("below the minimum", r["reason"])
        r = self.route("USDT", "BTC", "0.6", min_notional={"USDT": "0.5", "IRT": 100000})
        self.assertTrue(r["ok"])

    def test_sell_coin_for_usdt(self):
        r = self.route("BTC", "USDT", "0.001")
        self.assertEqual(r["route"], "direct")
        self.assertEqual(r["est_out"], Decimal("0.001") * 85900 * (1 - Decimal("0.0035")))

    def test_suspended_or_missing_direct_market_and_failing_book(self):
        r = self.route("USDT", "ETH", "50")                           # ETH_USDT suspended
        self.assertEqual(r["route"], "via_irt")
        self.assertIn("not tradable", r["alternatives"][0]["reason"])
        self.assertEqual(self.route("USDT", "XRP", "50")["route"], "via_irt")
        self.b["BTC_USDT"] = RuntimeError("book down")
        r = self.route("USDT", "BTC", "50")
        self.assertEqual(r["route"], "via_irt")
        self.assertIn("book down", r["alternatives"][0]["reason"])

    def test_single_leg_routes_unchanged(self):
        r = self.route("IRT", "BTC", "10000000")
        self.assertEqual((r["route"], [l["symbol"] for l in r["legs"]]), ("single", ["BTC_IRT"]))
        self.assertEqual(r["alternatives"], [])
        with self.assertRaises(ValueError):
            self.route("IRT", "BTC", "0")


# --------------------------------------------------------------------------- risk

class RiskLimitTest(unittest.TestCase):
    EQ = Decimal("3850000")
    RATE = Decimal("228550")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_risk_lim_")
        self.clock = Clock()
        self.book = parse_book(books()["BTC_USDT"])      # mid 85950

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rm(self, **cfg):
        return RiskManager(dict({"min_order_irt": 100000}, **cfg), self.dir, "paper", clock=self.clock)

    def vl(self, r, side="buy", price="68800", base="0.0001", ref="86000", post_only=True, rate=RATE, book=None):
        return r.vet_limit(side, BTC_USDT, price, base, book or self.book, ref, self.EQ, quote_irt_rate=rate,
                           post_only=post_only)

    def test_config(self):
        r = self.rm(min_order_usdt=2)
        self.assertEqual(r.min_order_for("USDT"), Decimal("2"))
        self.assertEqual(r.min_order_for("irt"), Decimal("100000"))
        self.assertEqual(self.rm().min_order_for("USDT"), Decimal("0.5"))
        self.assertEqual(self.rm().min_notional(), {"IRT": Decimal("100000"), "USDT": Decimal("0.5")})
        with self.assertRaises(ValueError):
            self.rm().min_order_for("EUR")
        for bad in ({"max_limit_distance": 1}, {"max_limit_distance": 0}, {"min_order_usdt": -1}):
            with self.assertRaises(ValueError):
                self.rm(**bad)
        with self.assertRaises(ValueError):
            self.rm(min_order_usdtt=1)

    def test_resting_bid_ok_and_rounded(self):
        v = self.vl(self.rm(), price="68800.129", base="0.000123456789")
        self.assertTrue(v.ok, v.reason)
        self.assertEqual((v.price, v.base_amount), (Decimal("68800.12"), Decimal("0.00012345")))
        self.assertEqual(v.notional, v.price * v.base_amount)
        self.assertEqual(v.notional_irt, v.notional * self.RATE)
        self.assertEqual((v.quote, v.crosses), ("USDT", False))

    def test_post_only_crossing_is_refused(self):
        v = self.vl(self.rm(), price="86000")
        self.assertFalse(v.ok)
        self.assertTrue(v.crosses)
        self.assertIn("would cross", v.reason)
        v = self.vl(self.rm(), side="sell", price="85900", base="0.0001")
        self.assertFalse(v.ok)
        self.assertTrue(v.crosses)

    def test_taker_limit_gets_the_slippage_guard(self):
        v = self.vl(self.rm(), price="86050", post_only=False)            # 0.12% above mid
        self.assertTrue(v.ok, v.reason)
        self.assertTrue(v.crosses)
        self.assertTrue(any("taker" in n for n in v.notes))
        v = self.vl(self.rm(), price="87000", post_only=False)            # 1.2% above mid > 1%
        self.assertFalse(v.ok)
        self.assertIn("slippage guard", v.reason)

    def test_price_sanity_for_limits(self):
        v = self.vl(self.rm(), price="68800", ref="80000")                 # the book is 7% off the last close
        self.assertIn("price sanity", v.reason)
        v = self.vl(self.rm(), price="91000", post_only=False)   # buy far above the close
        self.assertIn("ABOVE the last close", v.reason)
        v = self.vl(self.rm(), side="sell", price="80000", post_only=False)
        self.assertIn("BELOW the last close", v.reason)
        v = self.vl(self.rm(), price="40000")                              # 53% below the mid: wrong unit?
        self.assertIn("max_limit_distance", v.reason)
        v = self.vl(self.rm(), side="sell", price="150000")
        self.assertIn("max_limit_distance", v.reason)
        self.assertTrue(self.vl(self.rm(), price="68800", ref=None).ok)   # no close yet: book checks only

    def test_size_cap_minimum_and_rate(self):
        v = self.vl(self.rm(max_order_fraction=0.1), base="0.001")
        self.assertTrue(v.ok, v.reason)
        cap_usdt = Decimal("0.1") * self.EQ / self.RATE
        self.assertEqual(v.base_amount, floor_to_precision(cap_usdt / Decimal("68800"), 8))
        self.assertTrue(any("max_order_fraction" in n for n in v.notes))
        v = self.vl(self.rm(), base="0.000005")                           # 0.344 USDT < 0.5
        self.assertFalse(v.ok)
        self.assertIn("min_order_usdt", v.reason)
        v = self.vl(self.rm(), rate=None)
        self.assertIn("quote_irt_rate", v.reason)
        irt = self.rm().vet_limit("buy", BTC_IRT, "15000000000", "0.0001", parse_book(books()["BTC_IRT"]), None,
                                  self.EQ)
        self.assertTrue(irt.ok, irt.reason)                               # IRT markets need no rate
        self.assertEqual(irt.notional_irt, irt.notional)

    def test_halt_kill_switch_and_budgets(self):
        r = self.rm(max_limit_orders_per_day=2, max_orders_per_day=5)
        r.record_order("limit")
        r.record_order("limit")
        self.assertIn("max limit orders", self.vl(r).reason)
        self.assertEqual(r.remaining_orders(), 5)                           # market budget untouched
        self.assertEqual(r.remaining_orders("limit"), 0)
        v = r.vet_order("buy", BTC_USDT, "10", self.book, "86000", self.EQ, quote_irt_rate=self.RATE)
        self.assertTrue(v.ok, v.reason)                                    # a stop-loss still has budget
        for _ in range(20):
            r.record_cancel()
        self.assertTrue(r.can_place_order("cancel"))
        self.assertEqual(r.cancels_last_24h(), 20)
        r2 = RiskManager({"min_order_irt": 100000, "max_limit_orders_per_day": 2}, self.dir, "paper", clock=self.clock)
        self.assertEqual((r2.orders_last_24h("limit"), r2.cancels_last_24h()), (2, 20))   # persisted
        self.clock.t += 86401
        self.assertTrue(self.vl(r2).ok)                                    # rolling window
        with self.assertRaises(ValueError):
            r2.record_order("oco")
        with self.assertRaises(ValueError):
            r2.remaining_orders("cancel")
        r2.halt("test")
        self.assertIn("halted", self.vl(r2).reason)
        r2.reset()
        self.assertEqual(r2.orders_last_24h("limit"), 0)
        open(r2.kill_switch_path, "w").close()
        self.assertIn("kill switch", self.vl(r2).reason)

    def test_vet_order_usdt_market(self):
        r = self.rm()
        v = r.vet_order("buy", BTC_USDT, "10", self.book, "86000", self.EQ, quote_irt_rate=self.RATE)
        self.assertTrue(v.ok, v.reason)
        self.assertEqual((v.quote, v.notional, v.notional_irt), ("USDT", Decimal("10.00"), Decimal("10.00") * self.RATE))
        v = r.vet_order("buy", BTC_USDT, "0.4", self.book, "86000", self.EQ, quote_irt_rate=self.RATE)
        self.assertEqual(v.reason, "below minimum order: 0.4 USDT < min_order_usdt 0.5")
        v = r.vet_order("buy", BTC_USDT, "10", self.book, "86000", self.EQ)
        self.assertIn("quote_irt_rate", v.reason)
        v = self.rm(max_order_fraction=0.1).vet_order("buy", BTC_USDT, "10", self.book, "86000", self.EQ,
                                                      quote_irt_rate=self.RATE)
        self.assertEqual(v.amount, floor_to_precision(Decimal("0.1") * self.EQ / self.RATE, 2))
        v = r.vet_order("sell", BTC_USDT, "0.0005", self.book, None, self.EQ, flatten=True)   # flatten: no rate needed
        self.assertTrue(v.ok, v.reason)
        self.assertIsNone(v.notional_irt)
        v = r.vet_order("buy", USDT_IRT, "200000", parse_book(books()["USDT_IRT"]), "228550", self.EQ)
        self.assertEqual((v.ok, v.quote, v.notional_irt), (True, "IRT", Decimal("200000")))


# --------------------------------------------------------------------------- paper limits

class Ranges:
    """Fake candle range source: bars {symbol: [(open_ts, low, high, volume)]}, same rules as
    bars_range_source (bars that opened at/after start, with trades)."""
    res_seconds = 60

    def __init__(self):
        self.bars = {}
        self.calls = []

    def __call__(self, symbol, start, until):
        self.calls.append((symbol, start, until))
        lo = hi = None
        for ts, low, high, vol in self.bars.get(symbol, []):
            if ts < start or ts > until or vol <= 0:
                continue
            lo = D(low) if lo is None else min(lo, D(low))
            hi = D(high) if hi is None else max(hi, D(high))
        return (lo, hi) if lo is not None else None


class PaperLimitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_paper_lim_")
        self.clock = Clock()
        self.b = books()
        self.b["BTC_USDT"] = {"asks": [["86000", "1"]], "bids": [["85900", "1"]]}
        self.ranges = Ranges()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def broker(self, usdt="100", btc=None, **kw):
        p = PaperBroker(MARKETS, self.dir, capital_irt="10000000", book_source=lambda s: self.b[s], clock=self.clock,
                        range_source=self.ranges, **kw)
        if usdt is not None and "USDT" not in p._bal:
            p._bal["USDT"] = D(usdt)
            if btc:
                p._bal["BTC"] = D(btc)
            p._save()
        return p

    def trades(self):
        with open(os.path.join(self.dir, PaperBroker.TRADES_FILE), newline="") as fh:
            return list(csv.DictReader(fh))

    def test_bid_locks_funds_idempotently(self):
        p = self.broker()
        v = p.place_limit("BTC_USDT", "buy", "68800.129", "0.001", identifier="bid-1", tag="ladder", meta={"rung": -20})
        self.assertEqual((v["state"], v["price"], v["base_amount"], v["tag"]), (LIMIT_OPEN, Decimal("68800.12"),
                                                                            Decimal("0.001"), "ladder"))
        lock = ceil_to_precision(Decimal("68800.12") * Decimal("0.001"), 2)
        self.assertEqual(p.available()["USDT"], Decimal("100") - lock)
        self.assertEqual(p.balances()["USDT"], Decimal("100"))          # frozen still counts as equity
        self.assertEqual(p.frozen(), {"USDT": lock})
        self.assertEqual(p.locked_by_limits(), {"USDT": lock})
        v2 = p.place_limit("BTC_USDT", "buy", "68800", "0.001", identifier="bid-1")
        self.assertEqual((v2["order_id"], v["sent"], v2["sent"]), (v["order_id"], True, False))   # same order, locked once
        self.assertEqual(p.available()["USDT"], Decimal("100") - lock)
        with self.assertRaises(BrokerError):
            p.place_limit("BTC_USDT", "sell", "99000", "0.001", identifier="bid-1")
        with self.assertRaises(InsufficientFunds):                        # frozen funds are not available
            p.place_limit("BTC_USDT", "buy", "68000", "0.001", identifier="bid-2")
        with self.assertRaises(InsufficientFunds):
            p.market_buy("BTC_USDT", "40")
        self.assertEqual(len(p.list_open()), 1)
        self.assertEqual(p.list_open("ETH_USDT"), [])

    def test_post_only_cross_is_refused_before_anything_is_locked(self):
        p = self.broker()
        with self.assertRaises(LimitWouldCross) as cm:
            p.place_limit("BTC_USDT", "buy", "86000", "0.001")
        self.assertEqual(cm.exception.best, Decimal("86000"))
        self.assertEqual(p.available()["USDT"], Decimal("100"))
        self.assertEqual(p.limit_orders(), [])

    def test_partial_book_fill_then_candle_fill_with_events(self):
        p = self.broker()
        p.place_limit("BTC_USDT", "buy", "68800", "0.001", identifier="bid-1")
        # the book now shows a seller at/below our bid: that quantity is filled (as maker, at our price)
        self.b["BTC_USDT"] = {"asks": [["68750", "0.0004"], ["70000", "1"]], "bids": [["68000", "1"]]}
        rep = p.sync_limits()
        self.assertEqual([v["state"] for v in rep["changed"]], [LIMIT_PARTIAL])
        v = p.limit_orders()[0]
        self.assertEqual((v["filled_base"], v["filled_quote"]), (Decimal("0.0004"), Decimal("27.52")))
        fee1 = ceil_to_precision(Decimal("0.0004") * Decimal("0.003"), 8)
        self.assertEqual(v["fee"], fee1)
        self.assertEqual(p.balances()["BTC"], Decimal("0.0004") - fee1)
        self.assertEqual(p.frozen()["USDT"], Decimal("68.80") - Decimal("27.52"))
        ev = p.limit_fill_events()
        self.assertEqual([(e["identifier"], e["base"], e["quote"]) for e in ev], [("bid-1", Decimal("0.0004"),
                                                                                 Decimal("27.52"))])
        self.assertEqual(len(p.limit_fill_events()), 1)                   # non-destructive until acked
        p.ack_limit_fills(ev)
        self.assertEqual(p.limit_fill_events(), [])
        # a candle that opened BEFORE the order existed never fills it; one after it does
        self.b["BTC_USDT"] = {"asks": [["70000", "1"]], "bids": [["69000", "1"]]}
        self.ranges.bars["BTC_USDT"] = [(T0 - 20, "60000", "70000", 5)]
        self.clock.t += 120
        self.assertEqual(p.sync_limits()["changed"], [])
        self.assertEqual(self.ranges.calls[-1][1], T0 - T0 % 60 + 60)     # first bar that opened after placement
        self.ranges.bars["BTC_USDT"].append((T0 - T0 % 60 + 60, "68900", "69500", 0))   # no trades: ignored
        self.ranges.bars["BTC_USDT"].append((T0 - T0 % 60 + 120, "68800", "69500", 3))
        rep = p.sync_limits()
        v = rep["changed"][0]
        self.assertEqual((v["state"], v["filled_base"], v["filled_quote"]), (LIMIT_FILLED, Decimal("0.001"),
                                                                             Decimal("68.80")))
        self.assertEqual(p.frozen(), {})
        self.assertEqual(p.balances()["USDT"], Decimal("31.20"))
        ev = p.limit_fill_events()
        self.assertEqual([(e["base"], e["cum_base"], e["final"]) for e in ev], [(Decimal("0.0006"), Decimal("0.001"),
                                                                                True)])
        recs = [r for r in p.fill_records() if r["identifier"] == "bid-1"]
        self.assertEqual((recs[0]["base"], recs[0]["quote"], recs[0]["kind"], recs[0]["quote_asset"]),
                         (Decimal("0.001"), Decimal("68.80"), "limit", "USDT"))
        rows = self.trades()
        self.assertEqual([D(r["base"]) for r in rows], [Decimal("0.0004"), Decimal("0.0006")])  # deltas, each logged once
        p.sync_limits()
        self.assertEqual(len(self.trades()), 2)
        # restart: acks, balances and the order survive
        p2 = self.broker()
        self.assertEqual(len(p2.limit_fill_events()), 1)
        self.assertEqual(p2.limit_orders()[0]["state"], LIMIT_FILLED)
        self.assertEqual(p2.balances()["USDT"], Decimal("31.20"))

    def test_fill_through_requires_a_trade_below_the_bid(self):
        p = self.broker(fill_through="0.005")
        p.place_limit("BTC_USDT", "buy", "68800", "0.001", identifier="b")
        start = T0 - T0 % 60 + 60
        self.ranges.bars["BTC_USDT"] = [(start, "68700", "69000", 1)]      # only 0.15% through
        self.clock.t += 300
        self.assertEqual(p.sync_limits()["changed"], [])
        last = self.ranges.calls[-1]
        self.ranges.bars["BTC_USDT"].append((start + 240, "68400", "69000", 1))  # the bar still forming at that check
        self.clock.t += 60
        self.assertEqual(p.sync_limits()["changed"][0]["state"], LIMIT_FILLED)  # 0.58% through
        self.assertEqual(self.ranges.calls[-1][1], (int(last[2]) // 60) * 60)   # re-reads that forming bar only

    def test_sell_limit_fills_on_the_high_fee_in_quote(self):
        p = self.broker(btc="0.001")
        v = p.place_limit("BTC_USDT", "sell", "97000.001", "0.0003", identifier="tp")
        self.assertEqual(v["price"], Decimal("97000.01"))
        self.assertEqual(p.available()["BTC"], Decimal("0.0007"))
        self.ranges.bars["BTC_USDT"] = [(T0 - T0 % 60 + 60, "95000", "97100", 2)]
        self.clock.t += 120
        v = p.sync_limits()["changed"][0]
        gross = floor_to_precision(Decimal("0.0003") * Decimal("97000.01"), 2)
        fee = ceil_to_precision(gross * Decimal("0.003"), 2)
        self.assertEqual((v["state"], v["filled_quote"], v["fee"], v["fee_asset"]), (LIMIT_FILLED, gross, fee, "USDT"))
        self.assertEqual(p.balances()["USDT"], Decimal("100") + gross - fee)
        self.assertEqual(p.balances()["BTC"], Decimal("0.0007"))

    def test_cancel_releases_the_rest_and_honours_an_earlier_fill(self):
        p = self.broker()
        p.place_limit("BTC_USDT", "buy", "68800", "0.001", identifier="b1")
        # the market traded through the bid before our cancel: the cancel race keeps the fill
        self.ranges.bars["BTC_USDT"] = [(T0 - T0 % 60 + 60, "68000", "69000", 4)]
        self.clock.t += 90
        v = p.cancel("b1")
        self.assertEqual((v["state"], v["filled_base"]), (LIMIT_FILLED, Decimal("0.001")))
        p.place_limit("BTC_USDT", "buy", "60000", "0.0005", identifier="b2")
        self.b["BTC_USDT"] = {"asks": [["59990", "0.0002"], ["70000", "1"]], "bids": [["59000", "1"]]}
        v = p.cancel({"identifier": "b2"})
        self.assertEqual((v["state"], v["filled_base"], v["cancel_requested"]), (LIMIT_CANCELLED, Decimal("0.0002"),
                                                                                True))
        self.assertEqual(p.frozen(), {})
        self.assertEqual(p.cancel("b2")["state"], LIMIT_CANCELLED)          # already final: no-op
        with self.assertRaises(NotBotOrder):
            p.cancel("someone-elses")
        self.assertEqual(p.cancel_all(), [])

    def test_crossing_without_post_only_takes_then_rests(self):
        p = self.broker()
        self.b["BTC_USDT"] = {"asks": [["68750", "0.0004"], ["70000", "1"]], "bids": [["68000", "1"]]}
        v = p.place_limit("BTC_USDT", "buy", "68800", "0.001", identifier="x", post_only_intent=False)
        self.assertEqual((v["state"], v["filled_base"], v["filled_quote"]), (LIMIT_PARTIAL, Decimal("0.0004"),
                                                                             Decimal("27.50")))
        self.assertEqual(v["fee"], ceil_to_precision(Decimal("0.0004") * Decimal("0.0035"), 8))   # taker fee
        self.b["BTC_USDT"] = {"asks": [["70000", "1"]], "bids": [["68000", "1"]]}
        self.ranges.bars["BTC_USDT"] = [(T0 - T0 % 60 + 60, "68500", "69000", 1)]
        self.clock.t += 120
        v = p.sync_limits()["changed"][0]
        self.assertEqual((v["state"], v["filled_quote"]), (LIMIT_FILLED, Decimal("27.50") + Decimal("41.28")))
        self.assertEqual(p.frozen(), {})
        self.assertEqual(p.balances()["USDT"], Decimal("100") - Decimal("68.78"))   # the 0.02 left over is released

    def test_restart_keeps_resting_orders_and_locks(self):
        p = self.broker()
        p.place_limit("BTC_USDT", "buy", "68800", "0.001", identifier="b1")
        p2 = self.broker()
        self.assertEqual([v["identifier"] for v in p2.list_open()], ["b1"])
        self.assertEqual(p2.available()["USDT"], Decimal("31.20"))
        self.assertEqual(p2.balances()["USDT"], Decimal("100"))
        st = read_json(p2.path)
        self.assertEqual(st["frozen"], {"USDT": "68.80"})

    def test_market_orders_on_a_usdt_market(self):
        p = self.broker()
        f = p.market_buy("BTC_USDT", "43")
        self.assertEqual((f["base"], f["quote"], f["fee_asset"]), (Decimal("0.0005"), Decimal("43.00"), "BTC"))
        self.assertEqual(p.balances()["USDT"], Decimal("57.00"))

    def test_bars_range_source(self):
        calls = []

        def fetch(symbol, res, start, end):
            calls.append((symbol, res, start, end))
            return [Bar(start - 60, 1, 1, 50, 1, 9), Bar(start, 1, 99, 70, 1, 0), Bar(start + 60, 1, 90, 80, 1, 2),
                    Bar(start + 120, 1, 95, 75, 1, 1)]
        src = bars_range_source("1", fetch=fetch)
        self.assertEqual(src.res_seconds, 60)
        self.assertEqual(src("BTC_USDT", 1000020, 1000500), (Decimal("75"), Decimal("95")))
        self.assertEqual(calls, [("BTC_USDT", "1", 1000020, 1000500)])
        self.assertIsNone(bars_range_source("60", fetch=lambda *a: [])("BTC_USDT", 0, 1))


# --------------------------------------------------------------------------- live limits (fake exchange)

class FakeExchange:
    """In-memory Bitpin for LiveBroker: wallet with frozen funds, limit and market orders, fills driven
    by the test, async cancels. Order rows come back with string numbers like the real API."""

    def __init__(self, clock):
        self.clock = clock
        self.bal = {"IRT": D("5000000"), "USDT": D("100")}
        self.frz = {}
        self.orders = {}
        self.lock = {}
        self.next_id = 1000
        self.posts, self.cancels = [], []
        self.lists = 0
        self.books = books()
        self.place_error = None
        self.store_on_error = False
        self.cancel_mode = "async"
        self.cancel_reads = 0
        self.hidden = set()
        self.drop_empty_rows = False

    def _row(self, o):
        return {k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in o.items()}

    def wallets(self, assets=None, service="main", limit=200):
        rows = []
        for a in sorted(set(self.bal) | set(self.frz)):
            b, f = self.bal.get(a, D(0)), self.frz.get(a, D(0))
            if self.drop_empty_rows and b == 0 and f == 0 and a != "IRT":
                continue
            rows.append({"asset": a, "balance": format(b, "f"), "frozen": format(f, "f"), "service": "main"})
        return rows

    def orderbook(self, symbol):
        return self.books[symbol]

    def place_order(self, symbol, side, type="market", base_amount=None, quote_amount=None, price=None,
                    identifier=None):
        self.posts.append({"symbol": symbol, "side": side, "type": type, "base_amount": base_amount,
                           "quote_amount": quote_amount, "price": price, "identifier": identifier})
        if self.place_error is not None and not self.store_on_error:
            raise self.place_error
        m = MARKETS.get(symbol)
        self.next_id += 1
        oid = self.next_id
        o = {"id": oid, "symbol": symbol, "side": side, "type": type, "identifier": identifier, "state": "active",
             "dealed_base_amount": D(0), "dealed_quote_amount": D(0), "commission": D(0)}
        if type == "limit":
            o.update(price=D(price), base_amount=D(base_amount))
            asset, amt = (m.quote, D(price) * D(base_amount)) if side == "buy" else (m.base, D(base_amount))
            self.bal[asset] -= amt
            self.frz[asset] = self.frz.get(asset, D(0)) + amt
            self.lock[oid] = (asset, amt)
            self.orders[oid] = o
        else:
            ask = D(self.books[symbol]["asks"][0][0])
            base = floor_to_precision(D(quote_amount) / ask, m.base_amount_precision)
            fee = floor_to_precision(base * D("0.0035"), 8)
            o.update(state="closed", dealed_base_amount=base, dealed_quote_amount=D(quote_amount), commission=fee)
            self.bal[m.quote] -= D(quote_amount)
            self.bal[m.base] = self.bal.get(m.base, D(0)) + base - fee
            self.orders[oid] = o
        if self.place_error is not None:
            raise self.place_error
        return self._row(o)

    def fill(self, oid, base):
        """The market trades through resting order `oid` for `base`."""
        o = self.orders[oid]
        m = MARKETS.get(o["symbol"])
        base = min(D(base), o["base_amount"] - o["dealed_base_amount"])
        quote = base * o["price"]
        asset, locked = self.lock[oid]
        if o["side"] == "buy":
            fee = floor_to_precision(base * D("0.003"), 8)
            self.frz[m.quote] -= quote
            self.bal[m.base] = self.bal.get(m.base, D(0)) + base - fee
            self.lock[oid] = (asset, locked - quote)
        else:
            fee = floor_to_precision(quote * D("0.003"), 2)
            self.frz[m.base] -= base
            self.bal[m.quote] = self.bal.get(m.quote, D(0)) + quote - fee
            self.lock[oid] = (asset, locked - base)
        o["dealed_base_amount"] += base
        o["dealed_quote_amount"] += quote
        o["commission"] += fee
        if o["dealed_base_amount"] >= o["base_amount"]:
            self._close(oid, "closed")

    def _close(self, oid, state):
        o = self.orders[oid]
        asset, left = self.lock.pop(oid, (None, D(0)))
        if asset and left > 0:
            self.frz[asset] -= left
            self.bal[asset] += left
        if asset and self.frz.get(asset) == 0:
            del self.frz[asset]
        o["state"] = state

    def _visible(self, oid):
        if oid in self.hidden:
            return None
        o = self.orders.get(int(oid))
        if o is not None and "_cancel" in o and o["state"] == "active":
            if o["_cancel"] > 0:
                o["_cancel"] -= 1                        # the engine has not processed the cancel yet
            else:
                self._close(o["id"], "canceled")
        return o

    def get_order(self, oid):
        o = self._visible(int(oid))
        if o is None:
            raise BitpinAPIError(404, "not_found", {"detail": "Not found."})
        return self._row(o)

    def cancel_order(self, oid):
        self.cancels.append(int(oid))
        o = self.orders.get(int(oid))
        if o is None or int(oid) in self.hidden:
            return False
        if o["state"] != "active":
            raise BitpinAPIError(406, "not_allowed", {"detail": "not allowed"})
        if self.cancel_mode in ("fills_first", "406"):
            self.fill(o["id"], o["base_amount"])        # it filled before the cancel reached the engine
            if self.cancel_mode == "406":
                raise BitpinAPIError(406, "not_allowed", {"detail": "not allowed"})
            return True
        o["_cancel"] = self.cancel_reads                 # closed asynchronously (after this many reads)
        return True

    def find_order_by_identifier(self, identifier):
        for oid, o in self.orders.items():
            if o["identifier"] == identifier and self._visible(oid) is not None:
                return self._row(o)
        return None

    def open_orders(self, symbol=None):
        self.lists += 1
        return [self._row(o) for oid, o in self.orders.items()
                if self._visible(oid) is not None and o["state"] == "active"
                and (symbol is None or o["symbol"] == symbol)]

    def add_foreign(self, symbol="BTC_USDT"):
        self.next_id += 1
        self.orders[self.next_id] = {"id": self.next_id, "symbol": symbol, "side": "buy", "type": "limit",
                                     "identifier": "user-manual", "state": "active", "price": D("50000"),
                                     "base_amount": D("0.0001"), "dealed_base_amount": D(0),
                                     "dealed_quote_amount": D(0), "commission": D(0)}
        return self.next_id


class LiveLimitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_live_lim_")
        self.clock = Clock()
        self.ex = FakeExchange(self.clock)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def broker(self, **kw):
        kw.setdefault("min_order_irt", 100000)
        return LiveBroker(self.ex, MARKETS, self.dir, sleep=self.clock.sleep, clock=self.clock, **kw)

    def bid(self, b, ident="b1", price="68800", base="0.001", **kw):
        return b.place_limit("BTC_USDT", "buy", price, base, identifier=ident, **kw)

    def oid(self, b, ident):
        return int(b.journal.get(ident)["order_id"])

    def trades(self):
        with open(os.path.join(self.dir, LiveBroker.TRADES_FILE), newline="") as fh:
            return list(csv.DictReader(fh))

    # ---- placement
    def test_place_sends_a_rounded_limit_and_journals_it_resting(self):
        b = self.broker(assets=["USDT", "BTC"])
        v = self.bid(b, price="68800.129", base="0.000123456789", tag="ladder", meta={"rung": -20})
        p = self.ex.posts[0]
        self.assertEqual((p["type"], p["price"], p["base_amount"], p["quote_amount"], p["identifier"]),
                         ("limit", Decimal("68800.12"), Decimal("0.00012345"), None, "b1"))
        self.assertEqual((v["state"], v["tag"], v["meta"], v["order_id"]), (LIMIT_OPEN, "ladder", {"rung": -20}, "1001"))
        e = b.journal.get("b1")
        self.assertEqual((e["kind"], e["status"]), ("limit", "resting"))
        lock = Decimal("68800.12") * Decimal("0.00012345")
        self.assertEqual(b.available()["USDT"], Decimal("100") - lock)     # re-read: the exchange froze it
        self.assertEqual(b.balances()["USDT"], Decimal("100"))
        self.assertEqual(b.locked_by_limits(), {"USDT": lock})

    def test_idempotent_by_identifier(self):
        b = self.broker()
        v1 = self.bid(b)
        v2 = self.bid(b)
        self.assertEqual((len(self.ex.posts), v1["sent"], v2["sent"]), (1, True, False))
        self.assertEqual(v1["order_id"], v2["order_id"])
        with self.assertRaises(BrokerError):
            b.place_limit("BTC_USDT", "sell", "99000", "0.001", identifier="b1")
        b2 = self.broker()                                                   # also after a restart
        self.bid(b2)
        self.assertEqual(len(self.ex.posts), 1)

    def test_not_sent_may_be_resent_under_the_same_identifier(self):
        b = self.broker()
        self.ex.place_error = OrderNotSent("b1", "throttled")
        with self.assertRaises(OrderNotSent):
            self.bid(b)
        self.assertEqual(b.journal.get("b1")["status"], "not_sent")
        self.assertEqual(b.limit_orders()[0]["state"], LIMIT_REJECTED)
        self.ex.place_error = None
        v = self.bid(b)
        self.assertEqual((len(self.ex.posts), v["state"]), (2, LIMIT_OPEN))

    def test_rejection_is_final(self):
        b = self.broker()
        self.ex.place_error = BitpinAPIError(400, "invalid", {"code": "invalid"})
        with self.assertRaises(BitpinAPIError):
            self.bid(b)
        self.assertEqual(b.limit_orders()[0]["state"], LIMIT_REJECTED)
        self.ex.place_error = None
        self.assertEqual(self.bid(b)["state"], LIMIT_REJECTED)               # never re-sent: use a new identifier
        self.assertEqual(len(self.ex.posts), 1)

    def test_local_refusals_send_nothing(self):
        b = self.broker()
        with self.assertRaises(LimitWouldCross):
            self.bid(b, price="86000")
        with self.assertRaises(OrderTooSmall):
            self.bid(b, base="0.000005")                                     # 0.34 USDT < 0.5
        self.bid(b, "big", base="0.001")                                      # locks 68.80 of 100 USDT
        with self.assertRaises(InsufficientFunds):
            self.bid(b, "big2", base="0.001")                                 # frozen funds respected
        with self.assertRaises(BrokerError):                                 # suspended market
            b.place_limit("ETH_USDT", "buy", "2000", "0.001")
        self.assertEqual([p["identifier"] for p in self.ex.posts], ["big"])
        self.assertIsNone(b.journal.get("big2"))
        taker = self.bid(b, "x", price="86000", base="0.0001", post_only_intent=False)   # a taker is allowed
        self.assertEqual(taker["state"], LIMIT_OPEN)

    def test_journal_write_failure_sends_nothing(self):
        import bitpin.broker as broker_mod
        b = self.broker()
        real = broker_mod.atomic_write_json

        def broken(path, obj, private=False):
            raise PermissionError(13, "locked")
        broker_mod.atomic_write_json = broken
        self.addCleanup(setattr, broker_mod, "atomic_write_json", real)
        with self.assertRaises(BrokerError):
            self.bid(b)
        self.assertEqual(self.ex.posts, [])
        self.assertIsNone(b.journal.get("b1"))

    # ---- fills
    def test_partial_fills_are_booked_once_and_logged_as_deltas(self):
        b = self.broker()
        self.bid(b)
        oid = self.oid(b, "b1")
        self.ex.fill(oid, "0.0004")
        rep = b.sync_limits()
        self.assertEqual([v["state"] for v in rep["changed"]], [LIMIT_PARTIAL])
        self.assertEqual(b.locked_by_limits(), {"USDT": Decimal("0.0006") * Decimal("68800")})
        rec = [r for r in b.fill_records() if r["identifier"] == "b1"][0]
        self.assertEqual((rec["base"], rec["quote"], rec["fee"], rec["fee_asset"], rec["kind"], rec["quote_asset"]),
                         (Decimal("0.0004"), Decimal("27.52"), Decimal("0.0000012"), "BTC", "limit", "USDT"))
        ev = b.limit_fill_events()
        self.assertEqual([(e["base"], e["quote"], e["state"]) for e in ev], [(Decimal("0.0004"), Decimal("27.52"),
                                                                             LIMIT_PARTIAL)])
        b.ack_limit_fills(ev)
        self.assertEqual(b.sync_limits()["changed"], [])                     # nothing new: nothing booked
        self.assertEqual(b.limit_fill_events(), [])
        self.ex.fill(oid, "0.0006")
        v = b.sync_limits()["changed"][0]
        self.assertEqual((v["state"], v["filled_base"], v["remaining_base"]), (LIMIT_FILLED, Decimal("0.001"), 0))
        b2 = self.broker()                                                   # restart: the ack survived
        ev = b2.limit_fill_events()
        self.assertEqual([(e["base"], e["cum_base"], e["final"]) for e in ev], [(Decimal("0.0006"), Decimal("0.001"),
                                                                                True)])
        self.assertEqual([D(r["base"]) for r in self.trades()], [Decimal("0.0004"), Decimal("0.0006")])
        self.assertEqual(b2.balances()["BTC"], Decimal("0.001") - Decimal("0.000003"))
        self.assertNotIn("USDT", b2.frozen() if hasattr(b2, "frozen") else {})

    def test_resting_orders_never_block_or_get_cancelled_by_resolve_pending(self):
        b = self.broker(poll_timeout=5)
        self.bid(b)
        self.clock.t += 5000
        self.assertEqual(b.resolve_pending(), [])
        self.assertEqual(b.unresolved_count(), 0)
        self.assertEqual(self.ex.cancels, [])
        self.assertEqual(b.limit_orders()[0]["state"], LIMIT_OPEN)

    # ---- restart reconciliation, foreign orders
    def test_restart_reconciles_from_the_exchange_and_never_touches_foreign_orders(self):
        b = self.broker()
        self.bid(b, "b1")
        self.bid(b, "b2", price="64500", base="0.0003")
        foreign = self.ex.add_foreign()
        self.ex.fill(self.oid(b, "b1"), "0.0005")                     # while the bot was down
        self.ex._close(self.oid(b, "b2"), "canceled")                 # the user cancelled it in the app
        b2 = self.broker()
        rep = b2.sync_limits()
        self.assertEqual(rep["foreign_open"], 1)
        st = {v["identifier"]: (v["state"], v["filled_base"]) for v in b2.limit_orders()}
        self.assertEqual(st, {"b1": (LIMIT_PARTIAL, Decimal("0.0005")), "b2": (LIMIT_CANCELLED, Decimal("0"))})
        self.assertEqual([v["identifier"] for v in rep["open"]], ["b1"])
        self.assertEqual([v["identifier"] for v in b2.list_open()], ["b1"])
        with self.assertRaises(NotBotOrder):
            b2.cancel("user-manual")
        out = b2.cancel_all()
        self.assertEqual([v["identifier"] for v in out], ["b1"])
        self.assertNotIn(foreign, self.ex.cancels)
        self.assertEqual(self.ex.orders[foreign]["state"], "active")
        self.assertEqual(out[0]["state"], LIMIT_CANCELLED)
        self.assertEqual(out[0]["filled_base"], Decimal("0.0005"))

    def test_crash_between_journal_and_response_is_found_by_identifier(self):
        b = self.broker()
        self.ex.place_error = OrderStatusUnknown("b1", "timeout")
        self.ex.store_on_error = True                                  # the exchange got it; we did not hear back
        with self.assertRaises(OrderStatusUnknown):
            self.bid(b)
        self.assertEqual(b.limit_orders()[0]["state"], LIMIT_UNKNOWN)
        self.assertEqual(self.bid(b)["state"], LIMIT_UNKNOWN)          # idempotent: not re-sent
        self.assertEqual(len(self.ex.posts), 1)
        self.assertEqual(b.unresolved_count(), 0)                      # does not block market orders
        self.assertEqual(len(b.journal.unresolved()), 1)               # but is listed for resolve-order
        b2 = self.broker()
        rep = b2.sync_limits()
        self.assertEqual(rep["changed"][0]["state"], LIMIT_OPEN)
        self.assertEqual(b2.journal.get("b1")["order_id"], "1001")

    def test_unknown_submission_that_never_appears(self):
        for expire in (True, False):
            d = tempfile.mkdtemp(prefix="bitpin_live_lim_u_")
            self.addCleanup(shutil.rmtree, d, True)
            ex = FakeExchange(self.clock)
            ex.place_error = OrderStatusUnknown("u", "timeout")
            b = LiveBroker(ex, MARKETS, d, sleep=self.clock.sleep, clock=self.clock, min_order_irt=100000)
            b.expire_unknown_orders = expire
            with self.assertRaises(OrderStatusUnknown):
                b.place_limit("BTC_USDT", "buy", "68800", "0.001", identifier="u")
            rep = b.sync_limits()
            self.assertEqual([v["identifier"] for v in rep["unresolved"]], ["u"])
            self.clock.t += 901
            rep = b.sync_limits()
            if expire:
                self.assertEqual(rep["unresolved"], [])
                self.assertEqual(b.limit_orders()[0]["state"], LIMIT_REJECTED)
            else:
                self.assertTrue(rep["unresolved"][0]["stuck"])
                self.assertEqual(b.limit_orders()[0]["state"], LIMIT_UNKNOWN)

    def test_cancel_requested_while_unknown_is_sent_once_the_order_appears(self):
        b = self.broker()
        self.ex.place_error = OrderStatusUnknown("b1", "timeout")
        self.ex.store_on_error = True
        with self.assertRaises(OrderStatusUnknown):
            self.bid(b)
        self.ex.hidden.add(1001)                                     # the exchange does not show it yet
        v = b.cancel("b1")
        self.assertEqual((v["state"], v["cancel_requested"]), (LIMIT_UNKNOWN, True))
        self.assertEqual(self.ex.cancels, [])
        self.ex.hidden.clear()
        b2 = self.broker()                                           # even after a restart
        b2.sync_limits()
        self.assertEqual(self.ex.cancels, [1001])
        self.assertEqual(b2.sync_limits()["changed"][0]["state"], LIMIT_CANCELLED)
        self.assertEqual(b2.available()["USDT"], Decimal("100"))      # the lock was released

    # ---- cancel races
    def test_cancel_async_close(self):
        b = self.broker()
        self.bid(b)
        self.ex.fill(self.oid(b, "b1"), "0.0003")
        v = b.cancel("b1")
        self.assertEqual((v["state"], v["filled_base"]), (LIMIT_CANCELLED, Decimal("0.0003")))
        self.assertEqual(self.ex.cancels, [1001])
        self.assertEqual([e["base"] for e in b.limit_fill_events()], [Decimal("0.0003")])
        self.assertEqual(b.cancel("b1")["state"], LIMIT_CANCELLED)   # final: no second DELETE
        self.assertEqual(self.ex.cancels, [1001])

    def test_order_fills_while_we_cancel(self):
        for mode in ("fills_first", "406"):
            d = tempfile.mkdtemp(prefix="bitpin_live_lim_race_")
            self.addCleanup(shutil.rmtree, d, True)
            ex = FakeExchange(self.clock)
            b = LiveBroker(ex, MARKETS, d, sleep=self.clock.sleep, clock=self.clock, min_order_irt=100000)
            b.place_limit("BTC_USDT", "buy", "68800", "0.001", identifier="r")
            ex.cancel_mode = mode
            v = b.cancel("r")
            self.assertEqual((v["state"], v["filled_base"]), (LIMIT_FILLED, Decimal("0.001")), mode)
            ev = b.limit_fill_events()
            self.assertEqual([(e["base"], e["final"]) for e in ev], [(Decimal("0.001"), True)])
            if mode == "406":
                self.assertIn("406", b.journal.get("r")["error"])

    def test_cancel_without_waiting_is_completed_by_sync(self):
        b = self.broker()
        self.bid(b)
        self.ex.cancel_reads = 1
        v = b.cancel("b1", wait=False)               # one read right after the DELETE: not closed yet
        self.assertEqual(v["state"], LIMIT_OPEN)
        self.assertEqual(self.ex.cancels, [1001])
        self.assertTrue(v["cancel_requested"])
        self.assertEqual(b.sync_limits()["changed"][0]["state"], LIMIT_CANCELLED)

    # ---- odd exchange states
    def test_vanished_resting_order_is_written_off_only_after_it_stays_missing(self):
        b = self.broker()
        self.bid(b)
        self.clock.t += 3 * 86400                                      # an old order
        self.ex.hidden.add(1001)
        rep = b.sync_limits()
        self.assertEqual([v["identifier"] for v in rep["unresolved"]], ["b1"])
        self.assertEqual(b.journal.get("b1")["missing_since"], self.clock.t)
        self.ex.hidden.clear()                                          # a glitch: it is back
        b.sync_limits()
        self.assertIsNone(b.journal.get("b1")["missing_since"])
        self.ex.hidden.add(1001)
        b.sync_limits()
        self.clock.t += 600
        self.assertEqual(b.limit_orders()[0]["state"], LIMIT_OPEN)
        b.sync_limits()
        self.assertEqual(b.limit_orders()[0]["state"], LIMIT_OPEN)
        self.clock.t += 301
        b.sync_limits()
        self.assertEqual(b.limit_orders()[0]["state"], LIMIT_CANCELLED)
        self.assertEqual(b.journal.get("b1")["note"], "vanished")

    def test_exchange_open_but_journal_final_is_retracked(self):
        b = self.broker()
        self.bid(b, "b1")
        self.bid(b, "b2", price="64500", base="0.0003")
        b.journal.update("b1", status="closed", limit_state=LIMIT_CANCELLED)
        b.sync_limits()
        self.assertEqual(b.limit_orders()[0]["state"], LIMIT_OPEN)

    def test_a_row_for_another_market_is_never_applied(self):
        b = self.broker()
        self.bid(b)
        self.ex.orders[1001]["symbol"] = "ETH_USDT"
        before = dict(b.journal.get("b1"))
        rep = b.sync_limits()
        self.assertTrue(rep["errors"])
        self.assertEqual(b.journal.get("b1")["status"], before["status"])
        self.assertEqual(b.journal.get("b1")["reported_base"], before["reported_base"])

    def test_fills_never_go_backwards(self):
        b = self.broker()
        self.bid(b)
        self.ex.fill(1001, "0.0004")
        b.sync_limits()
        self.ex.orders[1001]["dealed_base_amount"] = D("0.0001")         # a stale / odd row
        b.sync_limits()
        self.assertEqual(b.limit_orders()[0]["filled_base"], Decimal("0.0004"))

    def test_read_only_sync_writes_and_cancels_nothing(self):
        b = self.broker()
        self.bid(b)
        self.ex.fill(1001, "0.0004")
        with open(b.journal.path, "rb") as fh:
            before = fh.read()
        rep = b.sync_limits(read_only=True)
        self.assertEqual(rep["changed"], [])
        with open(b.journal.path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(self.ex.cancels, [])

    def test_wallet_row_vanishing_after_a_limit_fill_is_not_a_data_error(self):
        self.ex.bal["USDT"] = D("68.80")
        self.ex.drop_empty_rows = True
        b = self.broker(assets=["USDT", "BTC"])
        b.refresh()
        self.bid(b)
        b.refresh()                                  # USDT all frozen: the row is still there
        self.ex.fill(1001, "0.001")                  # filled while nobody looked: the USDT row disappears
        b.refresh()                                  # before sync_limits: tolerated (a bot limit order is live)
        self.assertNotIn("USDT", b.balances())
        b.sync_limits()
        b.refresh()
        self.assertEqual(b.balances()["BTC"], Decimal("0.000997"))

    def test_journal_pruning_keeps_resting_and_unacked_limits(self):
        j = OrderJournal(os.path.join(self.dir, "j.json"), self.clock, keep=2)
        j.add("m1", kind="market", status="closed")
        j.add("r1", kind="limit", status="resting")
        j.add("f1", kind="limit", status="closed", reported_base="0.1", acked_base="0")
        j.add("m2", kind="market", status="closed")
        j.add("m3", kind="market", status="closed")
        self.assertEqual(sorted(k for k, _ in j.items()), ["f1", "r1"])

    def test_manual_resolution_of_an_unknown_limit(self):
        b = self.broker()
        self.ex.place_error = OrderStatusUnknown("b1", "timeout")
        self.ex.store_on_error = True
        with self.assertRaises(OrderStatusUnknown):
            self.bid(b)
        self.ex.fill(1001, "0.0002")
        f = b.resolve_manually("b1", order_id="1001")
        self.assertEqual(f["base"], Decimal("0.0002"))
        self.assertEqual(b.limit_orders()[0]["state"], LIMIT_PARTIAL)       # tracked again, still resting
        self.assertEqual(b.journal.get("b1")["resolved_by"], "user")
        # a resting order that vanished (capped sleeve: never written off by itself) can be closed by hand
        self.assertIsNone(b.resolve_manually("b1", not_executed=True))
        v = b.limit_orders()[0]
        self.assertEqual((v["state"], v["filled_base"]), (LIMIT_CANCELLED, Decimal("0.0002")))
        with self.assertRaises(BrokerError):
            b.resolve_manually("b1", not_executed=True)

    # ---- USDT market orders and routing through the live broker
    def test_market_orders_on_usdt_markets(self):
        b = self.broker(assets=["USDT", "BTC"])
        f = b.market_buy("BTC_USDT", "43.009")
        self.assertEqual(self.ex.posts[0]["quote_amount"], Decimal("43.00"))
        self.assertEqual((f["base"], f["fee_asset"]), (Decimal("0.0005"), "BTC"))
        self.assertEqual(b.balances()["USDT"], Decimal("57.00"))
        with self.assertRaises(OrderTooSmall):
            b.market_buy("BTC_USDT", "0.4")
        with self.assertRaises(InsufficientFunds):
            b.market_buy("BTC_USDT", "58")
        b2 = self.broker(min_order_usdt="50")
        with self.assertRaises(OrderTooSmall):
            b2.market_buy("BTC_USDT", "43")

    def test_best_route_uses_the_live_books_and_minimums(self):
        b = self.broker()
        r = b.best_route("USDT", "BTC", "50")
        self.assertEqual((r["ok"], r["route"]), (True, "direct"))
        r = b.best_route("USDT", "BTC", "0.4")
        self.assertFalse(r["ok"])
        r = b.best_route("USDT", "BTC", "0.4", min_notional=None)
        self.assertTrue(r["ok"])


class LiveLimitEndToEndTest(unittest.TestCase):
    """Through the real BitpinClient with a fake HTTP transport: the limit POST body, the open-orders
    read and the DELETE."""

    def test_place_list_cancel(self):
        d = tempfile.mkdtemp(prefix="bitpin_lim_e2e_")
        self.addCleanup(shutil.rmtree, d, True)
        clock = Clock()
        calls = []
        orders = {}

        def row(o):
            return dict(o)

        def transport(method, url, headers, body, timeout):
            u = urllib.parse.urlparse(url)
            q = dict(urllib.parse.parse_qsl(u.query))
            calls.append((method, u.path, q, json.loads(body.decode()) if body else None))
            if u.path == "/api/v1/usr/authenticate/":
                return 200, json.dumps({"access": "a.b.c", "refresh": "d.e.f"}).encode()
            if u.path == "/api/v1/wlt/wallets/":
                return 200, json.dumps([{"asset": "IRT", "balance": "0", "frozen": "0", "service": "main"},
                                        {"asset": "USDT", "balance": "100", "frozen": "0", "service": "main"}]).encode()
            if u.path == "/api/v1/mth/orderbook/BTC_USDT/":
                return 200, json.dumps(books()["BTC_USDT"]).encode()
            if method == "POST" and u.path == "/api/v1/odr/orders/":
                b = json.loads(body.decode())
                orders[77] = {"id": 77, "symbol": b["symbol"], "side": b["side"], "type": b["type"],
                              "price": b["price"], "base_amount": b["base_amount"], "identifier": b["identifier"],
                              "state": "active", "dealed_base_amount": "0", "dealed_quote_amount": "0",
                              "commission": "0"}
                return 201, json.dumps(row(orders[77])).encode()
            if method == "GET" and u.path == "/api/v1/odr/orders/":
                rows = [row(o) for o in orders.values() if q.get("state") in (None, o["state"])
                        and q.get("identifier") in (None, o["identifier"])]
                rows.append({"id": 5, "symbol": "BTC_USDT", "side": "sell", "state": "active", "identifier": None,
                             "dealed_base_amount": "0", "dealed_quote_amount": "0", "commission": "0"})
                return 200, json.dumps(rows).encode()
            if method == "DELETE" and u.path == "/api/v1/odr/orders/77/":
                orders[77]["state"] = "canceled"
                return 204, b""
            if method == "GET" and u.path == "/api/v1/odr/orders/77/":
                return 200, json.dumps(row(orders[77])).encode()
            raise AssertionError((method, url))

        client = BitpinClient(api_key="k" * 20, secret_key="s" * 20, state_dir=d, transport=transport, clock=clock,
                              sleep=clock.sleep)
        b = LiveBroker(client, MARKETS, d, sleep=clock.sleep, clock=clock, min_order_irt=100000)
        v = b.place_limit("BTC_USDT", "buy", "68800.129", "0.001", identifier="lad-1")
        post = [c for c in calls if c[0] == "POST" and c[1] == "/api/v1/odr/orders/"][0][3]
        self.assertEqual(post, {"symbol": "BTC_USDT", "type": "limit", "side": "buy", "identifier": "lad-1",
                                "base_amount": "0.00100000", "price": "68800.12"})
        self.assertEqual(v["state"], LIMIT_OPEN)
        opened = b.list_open()
        self.assertEqual([o["identifier"] for o in opened], ["lad-1"])       # the foreign order 5 is left out
        lst = [c for c in calls if c[0] == "GET" and c[1] == "/api/v1/odr/orders/"][-1][2]
        self.assertEqual((lst["state"], lst["limit"]), ("active", "100"))
        v = b.cancel("lad-1")
        self.assertEqual(v["state"], LIMIT_CANCELLED)
        self.assertEqual([c[1] for c in calls if c[0] == "DELETE"], ["/api/v1/odr/orders/77/"])
        self.assertEqual(sum(1 for c in calls if c[0] == "POST" and c[1] == "/api/v1/odr/orders/"), 1)


if __name__ == "__main__":
    unittest.main()
