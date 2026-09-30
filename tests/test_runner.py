"""Runner + RiskManager + CLI tests on synthetic candles and a paper broker (no network)."""
import contextlib
import importlib.util
import io
import logging
import math
import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import data as data_mod  # noqa: E402
from bitpin.api import AuthError, OrderNotSent, OrderStatusUnknown, atomic_write_json, read_json  # noqa: E402
from bitpin.backtest import Panel, Strategy  # noqa: E402
from bitpin.broker import LiveBroker, OrderTooSmall, PaperBroker, fallback_log_path  # noqa: E402
from bitpin.data import Bar, resample  # noqa: E402
from bitpin.markets import D, Market, MarketCache, floor_to_precision, parse_book  # noqa: E402
from bitpin.risk import KILL_SWITCH_FILE, RiskManager  # noqa: E402
from bitpin.runner import (RESEARCH_HISTORY_START, Runner, RunnerError, StaleData, StateLock,  # noqa: E402
                           account_lock, compute_targets, expected_last_closed_1h, load_config, next_bar_close,
                           plan_orders)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logging.getLogger("bitpin").addHandler(logging.NullHandler())

LAST_CLOSED = 1790065800          # a real Bitpin 1h bar open (hh:30 UTC)
NOW = LAST_CLOSED + 3600 + 90     # 90 s after that bar closed
FORMING_PRICE = 1e9               # the still-forming bar has an absurd price: it must never be used

MARKETS = MarketCache(markets=[
    Market("AAA_IRT", "AAA", "IRT", True, False, 0, 4, 0),
    Market("BBB_IRT", "BBB", "IRT", True, False, 0, 4, 0),
    Market("USDT_IRT", "USDT", "IRT", True, False, 0, 2, 0),
])


class Clock:
    def __init__(self, t=NOW):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FixedWeights(Strategy):
    name = "fixed_test"
    res = "60"
    symbols = ["AAA_IRT", "BBB_IRT"]
    default_params = {"a": 0.5, "b": 0.5}

    def weights(self, panel):
        self.seen = panel
        T = len(panel)
        return {"AAA_IRT": [self.params["a"]] * T, "BBB_IRT": [self.params["b"]] * T}


class FixedWeights4h(FixedWeights):
    name = "fixed_test_4h"
    res = "240"


class Market4Test(object):
    """Synthetic hourly candles + an order book that follows the latest price."""

    def __init__(self, n=400):
        self.prices = {"AAA_IRT": 100.0, "BBB_IRT": 200.0, "USDT_IRT": 230000.0}
        self.series = {s: [] for s in self.prices}
        start = LAST_CLOSED - (n - 1) * 3600
        for i in range(n):
            self.add_bar(start + i * 3600)
        self.fail_next = 0

    def add_bar(self, ts, prices=None):
        prices = prices or {}
        for s in self.series:
            p = prices.get(s, self.prices[s])
            self.prices[s] = p
            self.series[s].append(Bar(ts, p, p * 1.001, p * 0.999, p, 10.0))

    def source(self, symbol, res, start, end):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("network down")
        bars = [b for b in self.series[symbol] if start <= b.ts <= end]
        forming = self.series[symbol][-1].ts + 3600
        if forming <= end:
            bars.append(Bar(forming, FORMING_PRICE, FORMING_PRICE, FORMING_PRICE, FORMING_PRICE, 1.0))
        return bars

    def book(self, symbol):
        p = self.prices[symbol]
        return {"asks": [[str(round(p * (1 + 0.0005 * k), 6)), "1000000"] for k in range(1, 6)],
                "bids": [[str(round(p * (1 - 0.0005 * k), 6)), "1000000"] for k in range(1, 6)]}


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_runner_test_")
        self.mkt = Market4Test()
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def runner(self, strategy, dry_run=False, risk=None, broker_cls=PaperBroker, capital="100000000", cfg=None,
               seed_source=None):
        broker = broker_cls(MARKETS, self.dir, capital_irt=capital, book_source=lambda s: self.mkt.book(s),
                            clock=self.clock, persist=not dry_run)
        rm = RiskManager(dict({"min_order_irt": 100000}, **(risk or {})), self.dir, "paper", clock=self.clock,
                         persist=not dry_run)
        c = {"lookback_bars": 300, "data_retry_seconds": 1, "wake_delay_seconds": 0, "error_backoff_seconds": 5,
             "history_csv_seed": False, "history_start_ts": None}  # synthetic data only, never data/*.csv
        c.update(cfg or {})
        return Runner(strategy, broker, rm, c, self.dir, "paper", dry_run=dry_run, bars_source=self.mkt.source,
                      clock=self.clock, sleep=self.clock.sleep, seed_source=seed_source)

    def rm(self):
        return RiskManager({}, self.dir, "paper", clock=self.clock)

    def paper_balances(self):
        return PaperBroker(MARKETS, self.dir, book_source=self.mkt.book).balances()

    def next_bar(self, prices=None):
        self.mkt.add_bar(self.mkt.series["AAA_IRT"][-1].ts + 3600, prices)
        self.clock.t += 3600


class RunOnceTest(RunnerBase):
    def test_first_run_trades_then_same_bar_is_never_traded_again(self):
        st = FixedWeights()
        r = self.runner(st)
        rep = r.run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["bar_ts"], LAST_CLOSED)
        # the strategy only saw closed bars, and the panel includes USDT_IRT like research.load_panel_for
        self.assertEqual(st.seen.ts[-1], LAST_CLOSED)
        self.assertLess(max(st.seen["AAA_IRT"]["close"]), 1000)
        self.assertIn("USDT_IRT", st.seen.symbols)
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "AAA_IRT"), ("buy", "BBB_IRT")])
        bal = r.broker.balances()
        eq = bal["IRT"] + bal["AAA"] * 100 + bal["BBB"] * 200
        self.assertAlmostEqual(float(bal["AAA"] * 100 / eq), 0.5, delta=0.01)
        self.assertAlmostEqual(float(bal["BBB"] * 200 / eq), 0.5, delta=0.01)
        # same bar again: nothing
        rep2 = r.run_once()
        self.assertEqual(rep2["status"], "already_processed")
        self.assertEqual(rep2["fills"], [])
        # restart (new objects, same state dir): still nothing
        rep3 = self.runner(FixedWeights()).run_once()
        self.assertEqual(rep3["status"], "already_processed")
        with open(os.path.join(self.dir, PaperBroker.TRADES_FILE)) as f:
            self.assertEqual(len(f.readlines()), 3)  # header + 2 fills
        # next bar: processed, but no trade needed (within threshold)
        self.next_bar()
        rep4 = self.runner(FixedWeights()).run_once()
        self.assertEqual(rep4["status"], "ok")
        self.assertEqual(rep4["bar_ts"], LAST_CLOSED + 3600)
        self.assertEqual(rep4["fills"], [])

    def test_sells_before_buys_and_buy_uses_available_irt(self):
        r = self.runner(FixedWeights(a=1.0, b=0.0))
        r.run_once()
        self.assertGreater(r.broker.balances()["AAA"], 0)
        self.next_bar()
        r2 = self.runner(FixedWeights(a=0.0, b=1.0))
        rep = r2.run_once()
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("sell", "AAA_IRT"), ("buy", "BBB_IRT")])
        bal = r2.broker.balances()
        self.assertEqual(bal["AAA"], 0)
        self.assertGreater(bal["IRT"], 0)
        self.assertLess(bal["IRT"], D("100000000") * D("0.003"))  # ~ the cash buffer is all that is left

    def test_threshold_skip_and_full_exit_below_threshold(self):
        self.assertEqual(self.runner(FixedWeights(a=0.01, b=0.0)).run_once()["fills"], [])  # 0.01 < 0.02
        self.next_bar()
        rep = self.runner(FixedWeights(a=0.03, b=0.0)).run_once()
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "AAA_IRT")])
        self.next_bar({"AAA_IRT": 50.0})  # position halves to ~1.5%: 0.03 - 0.015 < threshold -> no top-up
        self.assertEqual(self.runner(FixedWeights(a=0.03, b=0.0)).run_once()["fills"], [])
        self.next_bar()
        rep = self.runner(FixedWeights(a=0.0, b=0.0)).run_once()  # |delta| 0.015 < 0.02 but a full exit
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("sell", "AAA_IRT")])
        self.assertEqual(rep["fills"][0]["base"], rep["fills"][0]["requested"])

    def test_kill_switch_blocks_before_any_order(self):
        open(os.path.join(self.dir, KILL_SWITCH_FILE), "w").close()
        r = self.runner(FixedWeights())
        rep = r.run_once()
        self.assertEqual(rep["status"], "kill_switch")
        self.assertEqual(r.broker.balances(), {"IRT": D("100000000")})
        self.assertIsNone(r.last_bar_ts)
        self.assertEqual(r.loop(), "kill_switch")

    def test_kill_switch_mid_cycle_stops_remaining_orders(self):
        test = self

        class StopAfterSell(PaperBroker):
            def market_sell(self, *a, **k):
                f = PaperBroker.market_sell(self, *a, **k)
                open(os.path.join(test.dir, KILL_SWITCH_FILE), "w").close()
                return f

        self.runner(FixedWeights(a=1.0, b=0.0), broker_cls=StopAfterSell).run_once()
        self.next_bar()
        rep = self.runner(FixedWeights(a=0.0, b=1.0), broker_cls=StopAfterSell).run_once()
        self.assertEqual([f["side"] for f in rep["fills"]], ["sell"])
        self.assertIn(("BBB_IRT", "kill switch"), rep["skipped"])

    def test_dry_run_writes_nothing(self):
        r = self.runner(FixedWeights(), dry_run=True)
        rep = r.run_once()
        self.assertEqual(rep["status"], "dry_run")
        self.assertEqual(len(rep["vetted"]), 2)
        self.assertFalse(os.path.exists(r.state_path))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "risk_state_paper.json")))
        self.assertEqual(r.broker.balances(), {"IRT": D("100000000")})
        # a real run afterwards still trades this bar
        self.assertEqual(self.runner(FixedWeights()).run_once()["status"], "ok")

    def test_stale_candles_are_not_traded(self):
        self.clock.t += 2 * 3600  # the newest closed candle should be 2 bars later than what the source has
        r = self.runner(FixedWeights())
        with self.assertRaises(StaleData):
            r.run_once()
        rep = r.run_once_with_retry(max_wait=3)
        self.assertEqual(rep["status"], "stale")
        self.assertIsNone(r.last_bar_ts)
        self.assertEqual(r.broker.balances(), {"IRT": D("100000000")})

    def test_bar_not_used_until_exchange_opened_the_next_one(self):
        # local clock says the bar closed, but the exchange has not started the next bar yet
        real = self.mkt.source
        self.mkt.source = lambda *a: [b for b in real(*a) if b.close != FORMING_PRICE]
        r = self.runner(FixedWeights())
        with self.assertRaises(StaleData):
            r.run_once()
        self.assertIsNone(r.last_bar_ts)

    def test_failed_book_read_skips_only_that_order(self):
        real = self.mkt.book

        def flaky(symbol):
            if symbol == "AAA_IRT":
                raise RuntimeError("orderbook timeout")
            return real(symbol)

        self.mkt.book = flaky
        rep = self.runner(FixedWeights()).run_once()
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "BBB_IRT")])
        self.assertTrue(any("pre-trade" in e for e in rep["errors"]))

    def test_pending_order_blocks_trading(self):
        class Pending(PaperBroker):
            def resolve_pending(self, read_only=False):
                return [{"identifier": "abc", "symbol": "AAA_IRT"}]

        r = self.runner(FixedWeights(), broker_cls=Pending)
        rep = r.run_once()
        self.assertEqual(rep["status"], "pending_orders")
        self.assertEqual(rep["fills"], [])
        self.assertIsNone(r.last_bar_ts)

    def test_unknown_order_outcome_aborts_cycle_but_bar_is_consumed(self):
        calls = []

        class Unknown(PaperBroker):
            def market_buy(self, symbol, *a, **k):
                calls.append(symbol)
                raise OrderStatusUnknown("id-1", "timeout")

        r = self.runner(FixedWeights(), broker_cls=Unknown)
        rep = r.run_once()
        self.assertEqual(calls, ["AAA_IRT"])      # second buy never attempted
        self.assertTrue(rep["errors"])
        self.assertEqual(r.last_bar_ts, LAST_CLOSED)  # write-ahead: this bar will not be retried

    def test_drawdown_halts_and_persists(self):
        r = self.runner(FixedWeights(a=1.0, b=0.0), risk={"max_drawdown": 0.1})
        r.run_once()
        self.next_bar({"AAA_IRT": 80.0})
        r2 = self.runner(FixedWeights(a=1.0, b=0.0), risk={"max_drawdown": 0.1})
        rep = r2.run_once()
        self.assertEqual(rep["status"], "halted")
        self.assertEqual(rep["fills"], [])
        self.assertTrue(RiskManager({}, self.dir, "paper").is_halted())
        self.next_bar({"AAA_IRT": 80.0})
        self.assertEqual(self.runner(FixedWeights(a=0.0, b=0.0)).run_once()["status"], "halted")
        RiskManager({}, self.dir, "paper").reset()
        self.assertEqual(self.runner(FixedWeights(a=0.0, b=0.0)).run_once()["status"], "ok")

    def test_loop_survives_transient_errors(self):
        self.mkt.fail_next = 1
        r = self.runner(FixedWeights())
        r.loop(max_cycles=1)
        self.assertEqual(r.last_bar_ts, LAST_CLOSED)
        self.assertIn(5.0, self.clock.sleeps)  # error backoff

    def test_resample_240_uses_only_complete_buckets(self):
        st = FixedWeights4h()
        r = self.runner(st)
        rep = r.run_once()
        # LAST_CLOSED opens a new epoch-aligned 4h bucket, which is therefore incomplete
        bucket = LAST_CLOSED - LAST_CLOSED % 14400
        self.assertEqual(LAST_CLOSED, bucket + 1800)
        self.assertEqual(rep["bar_ts"], bucket - 14400)
        # identical to what Panel.load would build from the same closed hourly bars
        closed = {s: [b for b in self.mkt.series[s] if b.ts <= LAST_CLOSED][-300:] for s in r.symbols}
        ref = Panel.from_bars({s: resample(b, 4) for s, b in closed.items()}, "240")
        self.assertEqual(st.seen.ts, ref.ts)
        self.assertEqual(st.seen["AAA_IRT"]["close"], ref["AAA_IRT"]["close"])
        # an hour later the bucket is still incomplete: same bar, no second trade
        self.next_bar()
        self.assertEqual(self.runner(FixedWeights4h()).run_once()["status"], "already_processed")


class PlanTest(unittest.TestCase):
    def test_threshold_full_exit_and_sells_first(self):
        closes = {"A_IRT": D(100), "B_IRT": D(100), "C_IRT": D(100)}
        units = {"A_IRT": D(50), "B_IRT": D(1), "C_IRT": D(0)}      # equity 10000: A 0.50, B 0.01, C 0
        plan = plan_orders({"A_IRT": D("0.51"), "B_IRT": D(0), "C_IRT": D("0.3")}, units, closes, D(4900))
        self.assertEqual([(o.symbol, o.side) for o in plan.orders], [("B_IRT", "sell"), ("C_IRT", "buy")])
        self.assertTrue(plan.orders[0].full_exit)
        self.assertEqual(plan.orders[0].amount, D(1))
        self.assertEqual(plan.orders[1].amount, D("3000.0"))
        self.assertEqual([s for s, _ in plan.skipped], ["A_IRT"])

    def test_buys_capped_by_cash_after_sells(self):
        closes = {"A_IRT": D(100), "B_IRT": D(100)}
        plan = plan_orders({"A_IRT": D(0), "B_IRT": D(1)}, {"A_IRT": D(100)}, closes, D(0), fee_rate="0.01")
        self.assertEqual([o.side for o in plan.orders], ["sell", "buy"])
        self.assertEqual(plan.orders[1].amount, D("9900.00"))

    def test_dust_full_exit_skipped(self):
        plan = plan_orders({"A_IRT": D(0)}, {"A_IRT": D("0.001")}, {"A_IRT": D(100)}, D(1000), dust_irt=D(1000))
        self.assertEqual(plan.orders, [])
        self.assertIn("dust", plan.skipped[0][1])

    def test_equity_cap(self):
        plan = plan_orders({"A_IRT": D(1)}, {}, {"A_IRT": D(100)}, D(1000000), equity_cap=D(250000))
        self.assertEqual(plan.orders[0].amount, D(250000))
        self.assertEqual(plan.effective_equity, D(250000))

    def test_compute_targets_sanitising(self):
        bars = {s: [Bar(3600 * i, 10, 10, 10, 10, 1) for i in range(3)] for s in ("A_IRT", "B_IRT", "C_IRT", "D_IRT")}
        bars["E_IRT"] = []  # not listed yet: close is None
        p = Panel.from_bars(bars, "60")

        class S(Strategy):
            name = "s"
            symbols = list(bars)

            def weights(self, panel):
                return {"A_IRT": [0, 0, 0.8], "B_IRT": [0, 0, 0.7], "C_IRT": [0, 0, None],
                        "D_IRT": [0, 0, math.nan], "E_IRT": [1, 1, 1]}

        t = compute_targets(S(), p)
        self.assertAlmostEqual(float(t["A_IRT"]), 0.8 / 1.5)
        self.assertAlmostEqual(float(t["B_IRT"]), 0.7 / 1.5)
        self.assertEqual((t["C_IRT"], t["D_IRT"], t["E_IRT"]), (0, 0, 0))

        class Bad(S):
            def weights(self, panel):
                return {"A_IRT": [1]}

        with self.assertRaises(RunnerError):
            compute_targets(Bad(), p)


class TimeTest(unittest.TestCase):
    def test_bar_schedule_with_half_hour_offset(self):
        self.assertEqual(expected_last_closed_1h(NOW, 1800), LAST_CLOSED)
        self.assertEqual(expected_last_closed_1h(LAST_CLOSED + 3600, 1800), LAST_CLOSED)  # exactly at close
        self.assertEqual(expected_last_closed_1h(LAST_CLOSED + 3599, 1800), LAST_CLOSED - 3600)
        self.assertEqual(next_bar_close(NOW, 3600, 1800), LAST_CLOSED + 7200)
        bucket = LAST_CLOSED - LAST_CLOSED % 14400
        self.assertEqual(next_bar_close(NOW, 14400, 1800), bucket + 14400 + 1800)  # its last 1h bar closes then

    def test_state_lock_is_exclusive(self):
        d = tempfile.mkdtemp(prefix="bitpin_lock_")
        self.addCleanup(shutil.rmtree, d, True)
        with StateLock(d):
            with self.assertRaises(RunnerError):
                StateLock(d).acquire()
        StateLock(d).acquire().release()


class RiskTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_risk_test_")
        self.clock = Clock()
        self.m = Market("AAA_IRT", "AAA", "IRT", True, False, 0, 4, 0)
        self.book = parse_book({"asks": [["100", "50"], ["101", "50"], ["130", "1000"]],
                                "bids": [["99", "50"], ["98", "50"], ["70", "1000"]]})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rm(self, **cfg):
        return RiskManager(dict({"min_order_irt": 1000}, **cfg), self.dir, "paper", clock=self.clock)

    def test_ok_order(self):
        v = self.rm().vet_order("buy", self.m, D(3000), self.book, D(100), D(100000))
        self.assertTrue(v.ok)
        self.assertEqual(v.amount, D(3000))
        self.assertLess(v.est_slippage, D("0.01"))

    def test_min_notional(self):
        v = self.rm().vet_order("buy", self.m, D(999), self.book, D(100), D(100000))
        self.assertFalse(v.ok)
        self.assertIn("minimum", v.reason)

    def test_price_sanity(self):
        v = self.rm().vet_order("buy", self.m, D(3000), self.book, D(106), D(100000))
        self.assertFalse(v.ok)
        self.assertIn("price sanity", v.reason)

    def test_slippage_guard_shrinks(self):
        # 20000 IRT would eat into the 130 level; the guard keeps the average within 1% of mid 99.5
        v = self.rm().vet_order("buy", self.m, D(20000), self.book, D(100), D(10 ** 9))
        self.assertTrue(v.ok)
        self.assertLess(v.amount, D(20000))
        self.assertLessEqual(v.est_slippage, D("0.01"))
        v = self.rm().vet_order("sell", self.m, D(500), self.book, D(100), D(10 ** 9))
        self.assertTrue(v.ok)
        self.assertLess(v.amount, D(500))
        self.assertLessEqual(v.est_slippage, D("0.01"))

    def test_slippage_guard_skips_when_nothing_fits(self):
        book = parse_book({"asks": [["110", "50"]], "bids": [["90", "50"]]})  # spread alone is 10%
        v = self.rm().vet_order("buy", self.m, D(3000), book, D(100), D(10 ** 9))
        self.assertFalse(v.ok)

    def test_max_order_fraction(self):
        v = self.rm(max_order_fraction=0.5).vet_order("buy", self.m, D(4000), self.book, D(100), D(6000))
        self.assertTrue(v.ok)
        self.assertEqual(v.amount, D(3000))

    def test_orders_per_day(self):
        r = self.rm(max_orders_per_day=2)
        r.record_order()
        r.record_order()
        self.assertFalse(r.vet_order("buy", self.m, D(3000), self.book, D(100), D(100000)).ok)
        self.clock.t += 86401
        self.assertTrue(r.vet_order("buy", self.m, D(3000), self.book, D(100), D(100000)).ok)

    def test_halt_and_kill_switch(self):
        r = self.rm(max_drawdown=0.2)
        r.update_equity(D(1000))
        self.assertFalse(r.update_equity(D(850))["breached"])
        self.assertTrue(r.update_equity(D(790))["breached"])
        self.assertTrue(RiskManager({}, self.dir, "paper").is_halted())  # persisted
        self.assertFalse(r.vet_order("buy", self.m, D(3000), self.book, D(100), D(100000)).ok)
        r.reset()
        self.assertTrue(r.vet_order("buy", self.m, D(3000), self.book, D(100), D(100000)).ok)
        open(os.path.join(self.dir, KILL_SWITCH_FILE), "w").close()
        v = r.vet_order("buy", self.m, D(3000), self.book, D(100), D(100000))
        self.assertFalse(v.ok)
        self.assertIn("kill switch", v.reason)

    def test_unknown_setting_rejected(self):
        with self.assertRaises(ValueError):
            RiskManager({"max_drawdwn": 0.1}, self.dir, "paper")

    def test_flatten_mode_skips_price_sanity_count_and_fraction_but_not_slippage(self):
        r = self.rm(max_orders_per_day=1, max_order_fraction=0.1)
        r.record_order()
        r.halt("test")
        v = r.vet_order("sell", self.m, D(40), self.book, D(130), D(1000), flatten=True)
        self.assertTrue(v.ok)                          # halted, over the count, 30% off the close, > fraction
        self.assertLessEqual(v.est_slippage, D("0.01"))
        self.assertFalse(r.vet_order("buy", self.m, D(3000), self.book, D(100), D(10 ** 9), flatten=True).ok)
        self.assertFalse(r.vet_order("sell", self.m, D(40), self.book, D(130), D(1000)).ok)

    def test_drawdown_basis_change_restarts_hwm(self):
        r = self.rm(max_drawdown=0.3)
        r.update_equity(D(1000), "account")
        self.assertTrue(r.peek_drawdown(D(100), "account")["breached"])
        self.assertFalse(r.update_equity(D(100), "sleeve:100")["breached"])
        self.assertEqual(r.update_equity(D(100), "sleeve:100")["hwm"], D(100))


class SleeveTest(RunnerBase):
    """max_equity_irt: the bot trades only its own ledger (review findings money-1 / safety-1)."""
    CAP = {"max_equity_irt": 100000000}

    def test_cap_never_touches_coins_the_user_already_held(self):
        r = self.runner(FixedWeights(a=1.0, b=0.0), capital="200000000", cfg=self.CAP)
        r.broker._bal.update({"AAA": D(9200000), "BBB": D(100000)})   # the user's own 920M AAA + 20M BBB
        r.broker._save()
        rep = r.run_once()
        self.assertEqual(rep["managed"], "sleeve")
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "AAA_IRT")])
        self.assertGreater(rep["fills"][0]["quote"], D(99000000))
        self.assertLessEqual(rep["fills"][0]["quote"], D(100000000))
        bal = self.paper_balances()
        self.assertEqual(bal["BBB"], D(100000))           # target 0, but not bot-owned: never sold
        self.assertGreater(bal["AAA"], D(9200000))        # none of the user's AAA sold
        self.assertGreater(bal["IRT"], D(99000000))       # toman above the budget untouched
        # AAA +2.5%: like the backtest's buy & hold, the gain is kept (no selling back to the cap)
        self.next_bar({"AAA_IRT": 102.5})
        rep = self.runner(FixedWeights(a=1.0, b=0.0), capital="200000000", cfg=self.CAP).run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["fills"], [])
        self.assertGreater(rep["equity"], D(100000000))

    def test_cap_losses_are_not_refilled_and_breaker_measures_the_sleeve(self):
        self.runner(FixedWeights(a=1.0, b=0.0), capital="1000000000", cfg=self.CAP).run_once()
        self.next_bar({"AAA_IRT": 50.0})                  # the sleeve loses ~50%, the account ~5%
        rep = self.runner(FixedWeights(a=1.0, b=0.0), capital="1000000000", cfg=self.CAP,
                          risk={"max_drawdown": 0.9}).run_once()
        self.assertEqual(rep["fills"], [])                # no top-up from the 900M reserve
        self.assertGreater(rep["drawdown"]["drawdown"], D("0.49"))
        self.next_bar({"AAA_IRT": 50.0})
        rep = self.runner(FixedWeights(a=1.0, b=0.0), capital="1000000000", cfg=self.CAP,
                          risk={"max_drawdown": 0.3}).run_once()
        self.assertEqual(rep["status"], "halted")
        self.assertTrue(self.rm().is_halted())

    def test_cap_change_moves_sleeve_cash_and_restarts_the_hwm(self):
        self.runner(FixedWeights(a=0.5, b=0.0), cfg={"max_equity_irt": 50000000}).run_once()
        self.next_bar()
        r = self.runner(FixedWeights(a=0.5, b=0.0), cfg={"max_equity_irt": 80000000})
        rep = r.run_once()
        self.assertEqual(D(r.state["sleeve"]["budget"]), D(80000000))
        self.assertEqual(rep["drawdown"]["hwm"], rep["equity"])
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "AAA_IRT")])
        self.next_bar()
        with self.assertRaises(RunnerError):             # the sleeve has < 70M cash to give back:
            self.runner(FixedWeights(a=0.5, b=0.0), cfg={"max_equity_irt": 10000000})   # refused at start

    def test_sleeve_coins_of_a_previous_strategy_are_sold(self):
        self.runner(FixedWeights(a=0.0, b=1.0), cfg=self.CAP).run_once()
        self.assertGreater(self.paper_balances()["BBB"], 0)
        self.next_bar()

        class OnlyA(Strategy):
            name = "only_a_test"
            res = "60"
            symbols = ["AAA_IRT"]

            def weights(self, panel):
                return {"AAA_IRT": [1.0] * len(panel)}

        rep = self.runner(OnlyA(), cfg=self.CAP).run_once()
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("sell", "BBB_IRT"), ("buy", "AAA_IRT")])

    def test_zero_or_negative_cap_is_rejected(self):
        for bad in (0, -5, "abc"):
            with self.assertRaises(RunnerError):
                load_config(None, {"max_equity_irt": bad})
        self.assertIsNone(load_config(None, {})["max_equity_irt"])


class SleeveQuoteTest(RunnerBase):
    """A COIN_USDT fill (routing, a ladder bid, a target sell) moves the sleeve's coin AND its USDT
    (units under USDT_IRT), never its toman cash; a COIN_IRT fill is booked as before."""

    def test_usdt_quoted_fills_move_the_sleeves_usdt_not_its_toman(self):
        r = self.runner(FixedWeights(a=1.0, b=0.0), cfg={"max_equity_irt": 100000000})
        sl = r._sleeve_get()
        sl["units"]["USDT_IRT"] = D("1000")
        r._sleeve_book({"identifier": "x1", "symbol": "BTC_USDT", "side": "buy", "base": D("0.01"),
                        "quote": D("800"), "fee": D("0.00003"), "fee_asset": "BTC"})
        self.assertEqual((sl["units"]["BTC_IRT"], sl["units"]["USDT_IRT"], sl["cash"]),
                         (D("0.00997"), D("200"), D("100000000")))
        # the same cumulative record again books nothing; a larger one books only the delta
        self.assertFalse(r._sleeve_book({"identifier": "x1", "symbol": "BTC_USDT", "side": "buy", "base": D("0.01"),
                                         "quote": D("800"), "fee": D("0.00003"), "fee_asset": "BTC"}))
        r._sleeve_book({"identifier": "x2", "symbol": "BTC_USDT", "side": "sell", "base": D("0.005"),
                        "quote": D("450"), "fee": D("1.35"), "fee_asset": "USDT"})
        self.assertEqual((sl["units"]["BTC_IRT"], sl["units"]["USDT_IRT"], sl["cash"]),
                         (D("0.00497"), D("648.65"), D("100000000")))
        r._sleeve_book({"identifier": "x3", "symbol": "USDT_IRT", "side": "sell", "base": D("100"),
                        "quote": D("10000000"), "fee": D("35000"), "fee_asset": "IRT"})
        self.assertEqual((sl["units"]["USDT_IRT"], sl["cash"]), (D("548.65"), D("109965000")))


class FakeLiveClient:
    """Bitpin private API stand-in for LiveBroker: market orders fill at the synthetic price."""

    def __init__(self, mkt, wallet):
        self.mkt = mkt
        self.wallet = {a: D(v) for a, v in wallet.items()}
        self.orders, self.n, self.placed = {}, 0, []
        self.hook_after_order = None        # called at the first wallet read after an order
        self.post_timeout = False           # the order executes, but the POST outcome is unknown
        self.findable = True                # identifier lookups find orders

    def wallets(self, assets=None, service="main", limit=200):
        if self.hook_after_order is not None and self.placed:
            h, self.hook_after_order = self.hook_after_order, None
            h()
        return [{"asset": a, "balance": format(v, "f"), "frozen": "0", "service": "main"}
                for a, v in self.wallet.items()]

    def orderbook(self, symbol):
        return self.mkt.book(symbol)

    def place_order(self, symbol, side, type="market", base_amount=None, quote_amount=None, price=None,
                    identifier=None):
        self.n += 1
        base_a = symbol.split("_")[0]
        px = D(repr(self.mkt.prices[symbol]))
        if side == "buy":
            q = D(quote_amount)
            b = floor_to_precision(q / px, 4)
            fee = floor_to_precision(b * D("0.0035"), 4)
            self.wallet["IRT"] -= q
            self.wallet[base_a] = self.wallet.get(base_a, D(0)) + b - fee
        else:
            b = D(base_amount)
            q = floor_to_precision(b * px, 0)
            fee = floor_to_precision(q * D("0.0035"), 0)
            self.wallet[base_a] -= b
            self.wallet["IRT"] += q - fee
        o = {"id": self.n, "symbol": symbol, "side": side, "identifier": identifier, "state": "closed",
             "dealed_base_amount": format(b, "f"), "dealed_quote_amount": format(q, "f"), "commission": format(fee, "f")}
        self.orders[self.n] = o
        self.placed.append((side, symbol, b if side == "sell" else q))
        if self.post_timeout:
            self.post_timeout = False
            raise OrderStatusUnknown(identifier, "POST timed out")
        return dict(o)

    def get_order(self, oid):
        return dict(self.orders[int(oid)])

    def find_order_by_identifier(self, ident):
        for o in self.orders.values():
            if self.findable and o["identifier"] == ident:
                return dict(o)
        return None

    def cancel_order(self, oid):
        return True


class LiveSleeveTest(RunnerBase):
    """Capped live runs through LiveBroker + a fake client (review round 2, money + safety)."""
    CAP = {"max_equity_irt": 100000000}

    def live_runner(self, client, strategy=None, cfg=None, risk=None):
        broker = LiveBroker(client, MARKETS, self.dir, min_order_irt=100000, sleep=self.clock.sleep, clock=self.clock)
        rm = RiskManager(dict({"min_order_irt": 100000}, **(risk or {})), self.dir, "live", clock=self.clock)
        c = {"lookback_bars": 300, "history_csv_seed": False, "history_start_ts": None, "error_backoff_seconds": 5}
        c.update(cfg if cfg is not None else self.CAP)
        return Runner(strategy or FixedWeights(a=1.0, b=0.0), broker, rm, c, self.dir, "live",
                      bars_source=self.mkt.source, clock=self.clock, sleep=self.clock.sleep, seed_source=None)

    def test_fill_is_booked_after_an_interrupt_between_the_order_and_the_booking(self):
        client = FakeLiveClient(self.mkt, {"IRT": "500000000"})

        def sigint():
            raise KeyboardInterrupt("systemctl stop")
        client.hook_after_order = sigint            # lands in LiveBroker's post-order wallet refresh
        with self.assertRaises(KeyboardInterrupt):
            self.live_runner(client).run_once()
        self.assertEqual(read_json(os.path.join(self.dir, "runner_state_live.json"))["sleeve"]["units"], {})
        self.next_bar()
        rep = self.live_runner(client).run_once()   # restart
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(len(client.placed), 1)      # NOT bought a second time from the user's reserve
        sl = read_json(os.path.join(self.dir, "runner_state_live.json"))["sleeve"]
        self.assertEqual(D(sl["units"]["AAA_IRT"]), client.wallet["AAA"])
        self.assertEqual(D(sl["cash"]), D(100000000) - client.placed[0][2])

    def test_failed_journal_write_does_not_book_a_fill_twice(self):
        import bitpin.broker as broker_mod
        real = broker_mod.atomic_write_json
        fails = {"n": 1}

        def flaky(path, obj, private=False):
            if fails["n"] and path.endswith("live_orders.json") and \
                    any(e.get("status") == "closed" for e in obj.get("orders", {}).values()):
                fails["n"] -= 1
                raise PermissionError(13, "locked by a sync client")
            return real(path, obj, private)
        broker_mod.atomic_write_json = flaky
        self.addCleanup(setattr, broker_mod, "atomic_write_json", real)
        client = FakeLiveClient(self.mkt, {"IRT": "500000000"})
        self.live_runner(client).run_once()
        self.next_bar()
        self.live_runner(client).run_once()         # restart before any other journal write
        sl = read_json(os.path.join(self.dir, "runner_state_live.json"))["sleeve"]
        self.assertEqual(D(sl["units"]["AAA_IRT"]), client.wallet["AAA"])
        self.assertGreater(D(sl["cash"]), 0)

    def test_sleeve_ignores_orders_of_an_earlier_uncapped_run(self):
        client = FakeLiveClient(self.mkt, {"IRT": "500000000"})
        self.live_runner(client, cfg={}).run_once()             # uncapped: buys ~all toman
        self.assertEqual(len(client.placed), 1)
        client.wallet["IRT"] += D(100000000)
        self.next_bar()
        r = self.live_runner(client, strategy=FixedWeights(a=0.0, b=0.0))   # capped now, target 0
        rep = r.run_once()
        self.assertEqual(rep["fills"], [])                   # the earlier AAA is the user's, not the sleeve's
        self.assertEqual(r._sleeve_get()["units"], {})
        self.assertEqual(r._sleeve_get()["cash"], D(100000000))

    def test_new_sleeve_is_not_created_without_the_order_records(self):
        client = FakeLiveClient(self.mkt, {"IRT": "500000000"})
        self.live_runner(client, cfg={}).run_once()             # uncapped: buys AAA (the user's from now on)
        client.wallet["IRT"] += D(100000000)
        self.next_bar()
        r = self.live_runner(client, strategy=FixedWeights(a=0.0, b=0.0))
        real = r.broker.fill_records
        r.broker.fill_records = mock.Mock(side_effect=OSError("journal unreadable"))
        with self.assertRaises(Exception):
            r.run_once()
        self.assertIsNone((read_json(os.path.join(self.dir, "runner_state_live.json")) or {}).get("sleeve"))
        r.broker.fill_records = real
        rep = r.run_once()
        self.assertEqual(rep["fills"], [])                       # the earlier AAA is never booked as the sleeve's
        self.assertEqual(r._sleeve_get()["units"], {})

    def test_sleeve_of_an_older_version_is_not_booked_twice(self):
        client = FakeLiveClient(self.mkt, {"IRT": "500000000"})
        self.live_runner(client).run_once()
        path = os.path.join(self.dir, "runner_state_live.json")
        st = read_json(path)
        before = dict(st["sleeve"])
        st["sleeve"].pop("booked")                              # as written by the previous version
        atomic_write_json(path, st)
        self.next_bar()
        self.live_runner(client).run_once()
        sl = read_json(path)["sleeve"]
        self.assertEqual((sl["units"], sl["cash"]), (before["units"], before["cash"]))
        self.assertEqual(len(client.placed), 1)

    def test_capped_runner_never_ages_out_an_unknown_order(self):
        client = FakeLiveClient(self.mkt, {"IRT": "200000000"})
        client.post_timeout, client.findable = True, False   # executed, but never findable by identifier
        r = self.live_runner(client, cfg={"max_equity_irt": 50000000})
        self.assertFalse(r.broker.expire_unknown_orders)
        self.assertTrue(self.live_runner(client, cfg={}).broker.expire_unknown_orders)
        rep = r.run_once()
        self.assertTrue(rep["errors"])
        self.assertEqual(rep["unresolved_after"], 1)            # the loop polls every minute
        for _ in range(3):
            self.next_bar()
            rep = self.live_runner(client, cfg={"max_equity_irt": 50000000}).run_once()
            self.assertEqual(rep["status"], "pending_orders_stuck")
        self.assertEqual(len(client.placed), 1)                  # never bought a second time

    def test_unknown_order_is_polled_before_the_bar_is_reported_as_processed(self):
        client = FakeLiveClient(self.mkt, {"IRT": "200000000"})
        client.post_timeout, client.findable = True, False
        r = self.live_runner(client, cfg={"max_equity_irt": 50000000})
        r.run_once()
        self.clock.t += 60
        rep = r.run_once()                                       # same bar, one minute later
        self.assertEqual(rep["status"], "already_processed")
        self.assertEqual(len(rep["pending"]), 1)
        client.findable = True                                   # the exchange now shows it
        self.clock.t += 60
        rep = r.run_once()
        self.assertEqual(rep["status"], "already_processed")
        self.assertNotIn("pending", rep)
        self.assertEqual(r._sleeve_get()["units"]["AAA_IRT"], client.wallet["AAA"])   # booked

    def test_flatten_never_resends_a_sell_whose_outcome_is_unknown(self):
        # the user holds 300,000 AAA of their own; the sleeve buys, AAA falls, flatten starts, the
        # flatten sell executes but its outcome is unknown: it must not be sent again from the
        # sleeve ledger (clamped to the account's total, i.e. the user's own coins)
        client = FakeLiveClient(self.mkt, {"IRT": "200000000", "AAA": "300000"})
        risk = {"max_drawdown": 0.1, "drawdown_action": "flatten"}
        cap = {"max_equity_irt": 50000000}
        self.live_runner(client, cfg=cap, risk=risk).run_once()
        self.next_bar({"AAA_IRT": 80.0})
        client.post_timeout, client.findable = True, False
        rep = self.live_runner(client, cfg=cap, risk=risk).run_once()
        self.assertEqual(rep["status"], "flattening")
        for _ in range(3):
            self.clock.t += 60
            rep = self.live_runner(client, cfg=cap, risk=risk).run_once()
            self.assertEqual(rep["status"], "flattening")
            self.assertEqual(rep["fills"], [])
        self.assertEqual(client.wallet["AAA"], D(300000))        # the user's coins are untouched
        self.assertEqual([p[0] for p in client.placed], ["buy", "sell"])
        client.findable = True                                   # resolved: the flatten completes
        self.clock.t += 60
        rep = self.live_runner(client, cfg=cap, risk=risk).run_once()
        self.assertEqual(rep["status"], "halted")
        self.assertTrue(RiskManager({}, self.dir, "live").flattened)
        self.assertEqual(client.wallet["AAA"], D(300000))


class ParityStrategy(Strategy):
    """Path-dependent like hedge._drifting_book: the target depends on the bar's index, i.e. on
    where the panel starts."""
    name = "parity_test"
    res = "60"
    symbols = ["AAA_IRT", "BBB_IRT"]

    def weights(self, panel):
        self.seen = panel
        T = len(panel)
        return {"AAA_IRT": [0.3 if i % 2 else 0.6 for i in range(T)], "BBB_IRT": [0.0] * T}


class HistoryTest(RunnerBase):
    def test_history_is_anchored_at_the_csv_head_and_never_slides(self):
        cut = LAST_CLOSED - 100 * 3600
        seed = {s: [b for b in self.mkt.series[s] if b.ts <= cut] for s in self.mkt.series}
        st = ParityStrategy()
        r = self.runner(st, seed_source=lambda s: list(seed[s]), cfg={"lookback_bars": 50})
        rep = r.run_once()
        full = Panel.from_bars({s: [b for b in self.mkt.series[s] if b.ts <= LAST_CLOSED] for s in r.symbols}, "60")
        self.assertEqual(st.seen.ts, full.ts)            # the backtest's panel, not the last 50 bars
        self.assertEqual(rep["targets"]["AAA_IRT"], D(repr(ParityStrategy().weights(full)["AAA_IRT"][-1])))
        first, n = st.seen.ts[0], len(st.seen)
        for k in range(3):
            self.next_bar()
            r.run_once()
            self.assertEqual(st.seen.ts[0], first)       # the head never moves
            self.assertEqual(len(st.seen), n + k + 1)

    def test_default_config_seeds_from_the_research_csvs(self):
        cfg = load_config()
        self.assertTrue(cfg["history_csv_seed"])
        self.assertEqual(cfg["history_start_ts"], RESEARCH_HISTORY_START)
        broker = PaperBroker(MARKETS, self.dir, capital_irt="1000", book_source=self.mkt.book, persist=False)
        r = Runner(FixedWeights(), broker, self.rm(), {}, self.dir, "paper", dry_run=True, bars_source=self.mkt.source)
        self.assertIs(r.seed_source, data_mod.load_csv)


class SafetyTest(RunnerBase):
    def test_locked_trade_log_does_not_abort_the_rebalance(self):
        self.runner(FixedWeights(a=1.0, b=0.0)).run_once()
        path = os.path.join(self.dir, PaperBroker.TRADES_FILE)
        os.remove(path)
        os.mkdir(path)                                   # unwritable, like a CSV that Excel holds open
        self.next_bar()
        rep = self.runner(FixedWeights(a=0.0, b=1.0)).run_once()
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("sell", "AAA_IRT"), ("buy", "BBB_IRT")])
        self.assertTrue(os.path.exists(fallback_log_path(path)))

    def _crash_book(self, empty=False):
        real = self.mkt.book

        def book(symbol):
            if symbol != "AAA_IRT":
                return real(symbol)
            if empty:
                return {"asks": [], "bids": []}
            return {"asks": [["75.0", "10000000"]], "bids": [["74.9", "10000000"]]}   # mid 6.3% below close 80
        self.mkt.book = book
        return real

    def test_flatten_ignores_price_sanity_and_completes(self):
        risk = {"max_drawdown": 0.1, "drawdown_action": "flatten"}
        self.runner(FixedWeights(a=1.0, b=0.0), risk=risk).run_once()
        self.next_bar({"AAA_IRT": 80.0})
        self._crash_book()
        rep = self.runner(FixedWeights(a=1.0, b=0.0), risk=risk).run_once()
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("sell", "AAA_IRT")])
        self.assertEqual(rep["status"], "halted")
        self.assertTrue(self.rm().flattened)
        self.assertFalse(self.rm().flatten_pending)
        self.assertEqual(self.paper_balances().get("AAA", 0), 0)

    def test_flatten_is_retried_until_the_positions_are_gone(self):
        risk = {"max_drawdown": 0.1, "drawdown_action": "flatten"}
        self.runner(FixedWeights(a=1.0, b=0.0), risk=risk).run_once()
        self.next_bar({"AAA_IRT": 80.0})
        real = self._crash_book(empty=True)
        rep = self.runner(FixedWeights(a=1.0, b=0.0), risk=risk).run_once()
        self.assertEqual(rep["status"], "flattening")
        self.assertTrue(self.rm().flatten_pending)
        self.assertGreater(self.paper_balances()["AAA"], 0)
        self.mkt.book = real
        self.clock.t += 60
        rep = self.runner(FixedWeights(a=1.0, b=0.0), risk=risk).run_once()   # also survives a restart
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("sell", "AAA_IRT")])
        self.assertEqual(rep["status"], "halted")
        self.assertTrue(self.rm().flattened)
        self.assertEqual(self.paper_balances().get("AAA", 0), 0)

    def test_a_single_bad_balance_read_does_not_trip_the_breaker(self):
        class Glitch(PaperBroker):
            glitches = 0

            def balances(self):
                b = PaperBroker.balances(self)
                if Glitch.glitches > 0:
                    Glitch.glitches -= 1
                    b.pop("IRT", None)                  # e.g. a wallet response without the toman row
                return b

        self.runner(FixedWeights(a=0.5, b=0.0), broker_cls=Glitch, risk={"max_drawdown": 0.3}).run_once()
        self.next_bar()
        Glitch.glitches = 1
        rep = self.runner(FixedWeights(a=0.5, b=0.0), broker_cls=Glitch, risk={"max_drawdown": 0.3}).run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertFalse(self.rm().is_halted())

    def test_breaker_runs_while_orders_are_pending(self):
        class Pending(PaperBroker):
            def resolve_pending(self, read_only=False):
                return [{"identifier": "abc", "symbol": "AAA_IRT"}]

        self.runner(FixedWeights(a=1.0, b=0.0), risk={"max_drawdown": 0.1}).run_once()
        self.next_bar({"AAA_IRT": 80.0})
        rep = self.runner(FixedWeights(a=1.0, b=0.0), broker_cls=Pending, risk={"max_drawdown": 0.1}).run_once()
        self.assertEqual(rep["status"], "halted")
        self.assertTrue(self.rm().is_halted())

    def test_dry_run_resolves_pending_read_only(self):
        seen = []

        class Rec(PaperBroker):
            def resolve_pending(self, read_only=False):
                seen.append(read_only)
                return []

        self.runner(FixedWeights(), broker_cls=Rec, dry_run=True).run_once()
        self.assertEqual(seen, [True])
        self.assertFalse(os.path.exists(os.path.join(self.dir, PaperBroker.STATE_FILE)))

    def test_rebalance_skipped_when_the_daily_budget_cannot_cover_it(self):
        risk = {"max_orders_per_day": 3}
        self.runner(FixedWeights(a=1.0, b=0.0), risk=risk).run_once()   # 1 order
        self.rm().record_order()                                          # 2 used, 1 left
        self.next_bar()
        rep = self.runner(FixedWeights(a=0.0, b=1.0), risk=risk).run_once()   # needs a sell AND a buy
        self.assertEqual(rep["fills"], [])
        self.assertIn("daily order budget", rep["skipped"][0][1])
        self.assertGreater(self.paper_balances()["AAA"], 0)                # not sold into idle IRT

    def test_locally_refused_orders_do_not_count(self):
        class Refuse(PaperBroker):
            def market_buy(self, *a, **k):
                raise OrderTooSmall("below the minimum")

        self.runner(FixedWeights(), broker_cls=Refuse).run_once()
        self.assertEqual(self.rm().orders_last_24h(), 0)

    def test_local_clock_ahead_of_the_exchange_waits_in_loop_mode(self):
        real = self.mkt.source
        opened_at = LAST_CLOSED + 3600 + 900          # the exchange opens the next bar 15 min "late"
        self.mkt.source = lambda *a: [b for b in real(*a) if b.close != FORMING_PRICE or self.clock.t >= opened_at]
        r = self.runner(FixedWeights(), cfg={"data_retry_seconds": 30})
        self.assertEqual(r.run_once_with_retry(max_wait=120)["status"], "stale")   # --once keeps its short wait
        self.assertEqual(r.run_once_with_retry()["status"], "ok")                  # the loop waits for the bar
        self.assertGreaterEqual(self.clock.t, opened_at)


class Round2Test(RunnerBase):
    """Review round 2: breaker confirmation, clock behind, single-bar resample, buy-leg retry."""

    def _book_at(self, prices):
        real = self.mkt.book

        def book(symbol):
            if symbol in prices:
                p = prices[symbol]
                return {"asks": [[str(p * 1.0005), "1000000"]], "bids": [[str(p * 0.9995), "1000000"]]}
            return real(symbol)
        self.mkt.book = book
        return real

    def test_one_bar_spike_close_does_not_trip_the_breaker(self):
        risk = {"max_drawdown": 0.3, "drawdown_action": "flatten"}
        self.runner(FixedWeights(a=1.0, b=0.0), risk=risk).run_once()
        self.next_bar({"AAA_IRT": 60.0})                 # one trade printed -40%; the book is still at 100
        self._book_at({"AAA_IRT": 100.0})
        rep = self.runner(FixedWeights(a=1.0, b=0.0), risk=risk).run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertFalse(self.rm().is_halted())
        self.assertGreater(self.paper_balances()["AAA"], 0)   # nothing flattened
        self.assertIsNotNone(rep["drawdown"]["equity_mid"])
        # a real crash (the book agrees) still halts
        self.next_bar({"AAA_IRT": 60.0})
        self._book_at({"AAA_IRT": 60.0})
        rep = self.runner(FixedWeights(a=1.0, b=0.0), risk={"max_drawdown": 0.3}).run_once()
        self.assertEqual(rep["status"], "halted")

    def test_one_bar_spike_up_does_not_raise_the_high_water_mark(self):
        self.runner(FixedWeights(a=1.0, b=0.0)).run_once()
        hwm0 = self.rm().high_water_mark("account")
        self.next_bar({"AAA_IRT": 150.0})                # +50% print, the book stays at 100
        self._book_at({"AAA_IRT": 100.0})
        self.runner(FixedWeights(a=1.0, b=0.0)).run_once()
        self.assertLess(self.rm().high_water_mark("account"), hwm0 * D("1.01"))

    def test_local_clock_behind_still_trades_the_newest_closed_bar(self):
        self.clock.t = NOW - 3600                        # this computer runs an hour behind
        r = self.runner(FixedWeights())
        rep = r.run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["bar_ts"], LAST_CLOSED)     # not the bar before it
        self.assertGreater(rep["clock_behind_seconds"], 0)
        self.assertLess(max(r.strategy.seen["AAA_IRT"]["close"]), 1000)   # the forming bar is never used

    def test_a_lone_future_bar_does_not_move_the_timeline(self):
        real = self.mkt.source

        def src(symbol, res, start, end):
            bars = real(symbol, res, start, end)
            return bars + [Bar(LAST_CLOSED + 4 * 3600, 5.0, 5.0, 5.0, 5.0, 1.0)]   # garbage, 3 h ahead
        self.mkt.source = src
        rep = self.runner(FixedWeights()).run_once()
        self.assertEqual((rep["status"], rep["bar_ts"]), ("ok", LAST_CLOSED))
        self.assertNotIn("clock_behind_seconds", rep)

    def test_4h_strategy_with_a_symbol_that_has_one_closed_bar(self):
        class New4h(FixedWeights4h):
            name = "new_4h_test"
            symbols = ["AAA_IRT", "BBB_IRT"]

        real = self.mkt.source

        def src(symbol, res, start, end):
            bars = real(symbol, res, start, end)
            return bars[-2:] if symbol == "BBB_IRT" else bars   # BBB listed an hour ago: 1 closed bar
        self.mkt.source = src
        rep = self.runner(New4h()).run_once()                    # no ZeroDivisionError in data.resample
        self.assertEqual(rep["status"], "ok")

    def test_buys_skipped_by_a_temporary_failure_are_retried_within_the_bar(self):
        class FlakyRefresh(PaperBroker):
            calls, fail_at = 0, None

            def refresh(self):
                FlakyRefresh.calls += 1
                if FlakyRefresh.calls == FlakyRefresh.fail_at:
                    raise RuntimeError("wallet read timed out")

        self.runner(FixedWeights(a=1.0, b=0.0), broker_cls=FlakyRefresh).run_once()
        self.next_bar()
        FlakyRefresh.calls, FlakyRefresh.fail_at = 0, 2          # 1: planning read, 2: between sells and buys
        r = self.runner(FixedWeights(a=0.0, b=1.0), broker_cls=FlakyRefresh)
        rep = r.run_once()
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("sell", "AAA_IRT")])
        self.assertTrue(rep["retry_buys"])
        self.clock.t += 60
        rep = r.run_once()                                        # same bar: only the buys
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "BBB_IRT")])
        self.assertNotIn("retry_buys", rep)
        self.clock.t += 60
        self.assertEqual(r.run_once()["status"], "already_processed")
        self.assertGreater(self.paper_balances()["BBB"], 0)

    def test_buy_retry_gives_up_after_max_retries(self):
        from bitpin.runner import MAX_BUY_RETRIES
        calls = []

        class Throttled(PaperBroker):
            def market_buy(self, symbol, *a, **k):
                calls.append(symbol)
                raise OrderNotSent("x", "throttled")

        r = self.runner(FixedWeights(), broker_cls=Throttled)
        rep = r.run_once()
        self.assertTrue(rep["retry_buys"])                         # throttled = temporary
        for _ in range(10):
            self.clock.t += 60
            rep = r.run_once()
        self.assertEqual(rep["status"], "already_processed")       # gave up
        self.assertEqual(len(calls), 2 * (1 + MAX_BUY_RETRIES))

    def test_paper_fill_is_booked_after_an_interrupt_before_the_booking(self):
        class Interrupted(PaperBroker):
            armed = True

            def market_buy(self, *a, **k):
                fill = PaperBroker.market_buy(self, *a, **k)   # saved with the paper balances
                if Interrupted.armed:
                    Interrupted.armed = False
                    raise KeyboardInterrupt
                return fill

        cap = {"max_equity_irt": 50000000}
        with self.assertRaises(KeyboardInterrupt):
            self.runner(FixedWeights(a=1.0, b=0.0), capital="200000000", broker_cls=Interrupted, cfg=cap).run_once()
        self.next_bar()
        r = self.runner(FixedWeights(a=1.0, b=0.0), capital="200000000", broker_cls=Interrupted, cfg=cap)
        rep = r.run_once()
        self.assertEqual(rep["fills"], [])                         # the sleeve knows it already holds AAA
        self.assertEqual(r._sleeve_get()["units"]["AAA_IRT"], self.paper_balances()["AAA"])

    def test_loop_polls_soon_after_an_order_of_unknown_outcome(self):
        class Unknown(PaperBroker):
            n = 0

            def market_buy(self, *a, **k):
                raise OrderStatusUnknown("id-1", "timeout")

            def unresolved_count(self):
                Unknown.n += 1
                return 1 if Unknown.n == 1 else 0

        r = self.runner(FixedWeights(), broker_cls=Unknown)
        r.loop(max_cycles=2)
        self.assertEqual(self.clock.sleeps[0], 5.0)               # error_backoff_seconds, not the next bar


def load_run_bot():
    spec = importlib.util.spec_from_file_location("run_bot_under_test", os.path.join(ROOT, "scripts", "run_bot.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_cli_test_")
        self.lockdir = tempfile.mkdtemp(prefix="bitpin_cli_locks_")
        self.rb = load_run_bot()
        self.handlers = list(logging.getLogger().handlers)
        self.level = logging.getLogger().level

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            if h not in self.handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(self.level)
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.lockdir, ignore_errors=True)

    def test_paper_takes_the_lock_before_touching_state(self):
        with StateLock(self.dir):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.rb.main(["paper", "--capital-irt", "1000000", "--once", "--state-dir", self.dir])
        self.assertFalse(os.path.exists(os.path.join(self.dir, PaperBroker.STATE_FILE)))

    def test_live_takes_account_and_state_locks_before_reading_state(self):
        atomic_write_json(os.path.join(self.dir, self.rb.CONFIRM_FILE),
                          {"confirmed": True, "irt_asset_code": "IRT", "irt_unit_divisor": "1"})
        env = {"BITPIN_API_KEY": "DUMMYAPIKEY123", "BITPIN_SECRET_KEY": "DUMMYSECRET456",
               "BITPIN_BOT_LOCK_DIR": self.lockdir}
        called = []
        self.rb.make_live = lambda *a, **k: called.append(a)
        other_dir = tempfile.mkdtemp(prefix="bitpin_cli_other_")
        self.addCleanup(shutil.rmtree, other_dir, True)
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", FakeTTY()):
            fp = self.rb.account_fingerprint("DUMMYAPIKEY123", "DUMMYSECRET456")
            with account_lock(fp):                       # another bot on the same account, other state dir
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.rb.main(["live", "--i-accept-the-risk", "--state-dir", self.dir])
            with StateLock(self.dir):                    # another bot on the same state dir
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.rb.main(["live", "--i-accept-the-risk", "--state-dir", self.dir])
        self.assertEqual(called, [])                     # the journal / risk / runner state was never read

    def test_risk_reset_is_refused_while_a_bot_runs(self):
        RiskManager({}, self.dir, "live").halt("test")
        with StateLock(self.dir):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.rb.main(["risk-reset", "--mode", "live", "--state-dir", self.dir])
        self.assertTrue(RiskManager({}, self.dir, "live").is_halted())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.rb.main(["risk-reset", "--mode", "live", "--state-dir", self.dir]), 0)
        self.assertFalse(RiskManager({}, self.dir, "live").is_halted())

    def test_stop_warns_when_no_bot_uses_the_state_dir(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.rb.main(["stop", "--state-dir", self.dir]), 1)
        self.assertIn("WARNING", out.getvalue())
        with StateLock(self.dir), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.rb.main(["stop", "--state-dir", self.dir]), 0)
        self.assertTrue(os.path.exists(os.path.join(self.dir, KILL_SWITCH_FILE)))

    def test_keys_file_labels_and_line_order(self):
        p = os.path.join(self.dir, "dummy_keys.txt")   # dummy values, not real credentials

        def load(text):
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            return self.rb.load_credentials(p)
        self.assertEqual(load("secret_key: DUMMYSECRET1\napi_key: DUMMYAPIKEY1\n"),
                         ("DUMMYAPIKEY1", "DUMMYSECRET1", False))
        self.assertEqual(load("DUMMYAPIKEY1\nDUMMYSECRET1\n"), ("DUMMYAPIKEY1", "DUMMYSECRET1", True))
        self.assertEqual(load("DUMMYAPIKEY1\nabcKEYxyz=\n"), ("DUMMYAPIKEY1", "abcKEYxyz=", True))

    def test_config_and_credential_errors_exit_78(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.rb.main(["paper", "--strategy", "no_such_strategy", "--once", "--state-dir", self.dir])
        self.assertEqual(cm.exception.code, 78)
        env = {"BITPIN_API_KEY": "", "BITPIN_SECRET_KEY": ""}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            self.rb.main(["live", "--dry-run", "--state-dir", self.dir])
        self.assertEqual(cm.exception.code, 78)

    def test_state_dir_is_bound_to_one_account(self):
        fp_a = self.rb.account_fingerprint("KEYA111111", "SECRETA111")
        fp_b = self.rb.account_fingerprint("KEYB222222", "SECRETB222")
        self.rb.check_account_binding(self.dir, fp_a, write=False)
        self.assertFalse(os.path.exists(os.path.join(self.dir, self.rb.ACCOUNT_FILE)))   # a dry check never writes
        self.rb.check_account_binding(self.dir, fp_a, write=True)
        self.rb.check_account_binding(self.dir, fp_a, write=True)                           # same account: fine
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.rb.check_account_binding(self.dir, fp_b, write=False)
        self.assertEqual(cm.exception.code, 78)
        with open(os.path.join(self.dir, self.rb.ACCOUNT_FILE)) as f:
            text = f.read()
        self.assertNotIn("KEYA111111", text)
        self.assertNotIn("SECRETA111", text)
        # the live command refuses before the journal / sleeve / risk state is read
        atomic_write_json(os.path.join(self.dir, self.rb.CONFIRM_FILE),
                          {"confirmed": True, "irt_asset_code": "IRT", "irt_unit_divisor": "1"})
        called = []
        self.rb.make_live = lambda *a, **k: called.append(a)
        env = {"BITPIN_API_KEY": "KEYB222222", "BITPIN_SECRET_KEY": "SECRETB222", "BITPIN_BOT_LOCK_DIR": self.lockdir}
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", FakeTTY()), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.rb.main(["live", "--i-accept-the-risk", "--state-dir", self.dir])
        self.assertEqual((cm.exception.code, called), (78, []))

    def test_resolve_order_lists_and_records_not_executed(self):
        from bitpin.broker import OrderJournal
        j = OrderJournal(os.path.join(self.dir, LiveBroker.JOURNAL_FILE))
        j.add("ident-1", symbol="AAA_IRT", side="buy", quote_amount="5000000", status="unknown")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.rb.main(["resolve-order", "--state-dir", self.dir]), 0)
        self.assertIn("ident-1", out.getvalue())
        with StateLock(self.dir), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.rb.main(["resolve-order", "--state-dir", self.dir, "--identifier", "ident-1", "--not-executed",
                          "--yes"])                        # refused while a bot runs on the state dir
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.rb.main(["resolve-order", "--state-dir", self.dir, "--identifier", "ident-1",
                                           "--not-executed", "--yes"]), 0)
        e = OrderJournal(os.path.join(self.dir, LiveBroker.JOURNAL_FILE)).get("ident-1")
        self.assertEqual((e["status"], e["resolved_by"]), ("not_found", "user"))

    def test_unlabelled_keys_are_swapped_once_on_a_credential_rejection(self):
        made = []

        class C:
            def __init__(self, base, key, secret, state_dir=None):
                self.key = key
                made.append(key)

            def wallets(self, assets=None):
                if self.key != "RIGHTKEY":
                    raise AuthError(406, "api_credential_wrong", None, "rejected")
                return []

        cfg = {"irt_asset_code": "IRT", "base_url": "https://example.invalid"}
        with contextlib.redirect_stdout(io.StringIO()):
            c = self.rb.connect_client(cfg, self.dir, "RIGHTSECRET", "RIGHTKEY", True, client_factory=C)
        self.assertEqual((c.key, made), ("RIGHTKEY", ["RIGHTSECRET", "RIGHTKEY"]))
        made.clear()
        self.rb.connect_client(cfg, self.dir, "RIGHTSECRET", "RIGHTKEY", False, client_factory=C)
        self.assertEqual(made, ["RIGHTSECRET"])            # labelled / env keys: no probe, no swap
        self.assertEqual(self.rb.account_fingerprint("A1", "B2"), self.rb.account_fingerprint("B2", "A1"))


if __name__ == "__main__":
    unittest.main()
