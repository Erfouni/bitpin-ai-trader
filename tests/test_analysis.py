"""Market-context builder tests with a fake public client and synthetic candles (no network)."""
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import analysis as an  # noqa: E402
from bitpin.analysis import (MarketContextBuilder, book_stats, build_market_context, clean, current_weights,  # noqa: E402
                             dumps, parse_utc, usdt_prices)
from bitpin.backtest import Strategy  # noqa: E402
from bitpin.data import Bar  # noqa: E402
from bitpin.llm import ConfigError  # noqa: E402
from bitpin.strategies.hold_usdt import HoldUSDT  # noqa: E402

logging.getLogger("bitpin").addHandler(logging.NullHandler())

LAST_CLOSED = 1790065800          # a real Bitpin 1h bar open (hh:30 UTC): 2026-09-22 08:30 UTC
NOW = LAST_CLOSED + 3600 + 90     # 90 s after that bar closed; the bar opened at LAST_CLOSED+3600 is forming
FORMING_PRICE = 1e12
G_USDT = 0.0002                   # USDT_IRT log-growth per hour (toman devaluation)
H_BTC = -0.0001                   # BTC's USD price log-growth per hour
SYMS = ["USDT_IRT", "BTC_IRT", "ETH_IRT", "DOGE_IRT", "PAXG_IRT"]
SECRET = "sk-SHOULDNEVERAPPEAR123456"


def usdt_px(ts):
    return 200000.0 * math.exp(G_USDT * (ts - LAST_CLOSED) / 3600.0)


def usd_px(sym, ts):
    k = (ts - LAST_CLOSED) / 3600.0
    return {"BTC_IRT": 80000.0 * math.exp(H_BTC * k), "ETH_IRT": 3000.0 * (1 + 0.05 * math.sin(k / 20.0)),
            "DOGE_IRT": 0.2, "PAXG_IRT": 3500.0 * math.exp(0.00005 * k)}[sym]


def close_at(sym, ts):
    u = usdt_px(ts)
    return u if sym == "USDT_IRT" else u * usd_px(sym, ts)


class Bars:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def __call__(self, symbol, res, start, end):
        self.calls.append((symbol, res, start, end))
        if symbol in self.fail:
            raise RuntimeError("get_bars %s: boom" % symbol)
        first = start - (start % 3600) + 1800
        if first < start:
            first += 3600
        out = []
        t = first
        while t <= end:
            c = FORMING_PRICE if t > LAST_CLOSED else close_at(symbol, t)
            vol = 2.0 if LAST_CLOSED - 24 * 3600 < t <= LAST_CLOSED else 1.0
            out.append(Bar(t, c, c * 1.002, c * 0.998, c, vol))
            t += 3600
        return out


class FakeClient:
    """Public-data client double. Holds fake credentials that must never reach the context."""
    api_key = SECRET
    secret_key = SECRET

    def __init__(self, book_fail=()):
        self.book_fail = set(book_fail)
        self.calls = []

    def tickers(self):
        self.calls.append("tickers")
        return [{"symbol": s, "price": str(close_at(s, LAST_CLOSED) * 1.001), "daily_change_price": "1.0"} for s in SYMS] + \
            [{"symbol": "XYZ_USDT", "price": "1"}]

    def orderbook(self, symbol):
        self.calls.append(("orderbook", symbol))
        if symbol in self.book_fail:
            raise RuntimeError("orderbook down")
        p = close_at(symbol, LAST_CLOSED)
        return {"bids": [[str(p * 0.999), "10"], [str(p * 0.985), "10"]],
                "asks": [[str(p * 1.001), "10"], [str(p * 1.03), "10"]]}

    def matches(self, symbol):
        # v3: never called (the flow block is gone); recorded so a test can prove it
        self.calls.append(("matches", symbol))
        return []


class FixedBTC(Strategy):
    name = "fixed_btc"
    res = "240"
    symbols = ["BTC_IRT", "USDT_IRT"]
    description = "Test strategy. Always 40% BTC and 50% USDT."

    def weights(self, panel):
        T = len(panel)
        return {"BTC_IRT": [0.4] * T, "USDT_IRT": [0.5] * T}


class Broken(Strategy):
    name = "broken"
    res = "60"
    symbols = ["ETH_IRT"]

    def weights(self, panel):
        raise ZeroDivisionError("bad strategy")


STRATS = {"hold_usdt": HoldUSDT, "fixed_btc": FixedBTC, "broken": Broken}
# rule_signals is OFF by default in v3 (C5): the tests of the rule block switch it on explicitly
CFG = {"universe": ["USDT_IRT", "BTC_IRT", "ETH_IRT", "DOGE_IRT"], "macro_symbols": ["USDT_IRT", "PAXG_IRT"],
       "lookback_bars": 1000, "analysis_lookback_bars": 1000, "rule_signals": True,
       "competition_start_utc": "2026-09-21T20:30:00Z", "competition_end_utc": "2026-10-22T20:30:00Z"}
RET_HOURS = (("24h", 24), ("7d", 168), ("30d", 720))


def no_nones(obj):
    if isinstance(obj, dict):
        return all(v is not None and no_nones(v) for v in obj.values())
    if isinstance(obj, list):
        return all(no_nones(v) for v in obj)
    if isinstance(obj, float):
        return math.isfinite(obj)
    return True


class TestContext(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxtest_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def builder(self, bars=None, client=None, **cfg):
        c = dict(CFG)
        c.update(cfg)
        return MarketContextBuilder(client or FakeClient(), c, state_dir=self.dir, bars_source=bars or Bars(),
                                    clock=lambda: NOW, strategies=STRATS)

    def test_structure_and_numbers(self):
        b = self.builder()
        snap = {"balances": {"IRT": 100000000, "USDT": "1000", "BTC": 0.001, "ZZZ": 5}, "equity_start_irt": 500000000}
        ctx = b.build(snap)
        json.dumps(ctx)  # serialisable
        self.assertTrue(no_nones(ctx))
        self.assertNotIn(SECRET, dumps(ctx))
        for k in ("clock", "legend", "portfolio", "macro", "symbols", "rule_signals"):
            self.assertIn(k, ctx)
        btc = ctx["symbols"]["BTC_IRT"]
        # price from the ticker, not from the forming candle
        self.assertAlmostEqual(btc["px"], close_at("BTC_IRT", LAST_CLOSED) * 1.001, delta=btc["px"] * 1e-5)
        # v3: USDT-terms returns over [24h, 7d, 30d] only (no 1h / 4h entries, no IRT-terms returns for coins)
        self.assertEqual(len(btc["ret_usdt"]), 3)
        for (lbl, h), r_usdt in zip(RET_HOURS, btc["ret_usdt"]):
            self.assertAlmostEqual(r_usdt, (math.exp(H_BTC * h) - 1) * 100, delta=0.011, msg=lbl)
        self.assertNotIn("ret_irt", btc)
        for gone in ("rsi1h", "don_pos", "flow"):
            self.assertNotIn(gone, btc)
        usdt = ctx["symbols"]["USDT_IRT"]
        self.assertEqual(len(usdt["ret_irt"]), 3)
        self.assertAlmostEqual(usdt["ret_irt"][0], (math.exp(G_USDT * 24) - 1) * 100, delta=0.011)
        self.assertNotIn("ret_usdt", usdt)
        self.assertAlmostEqual(btc["px_usdt"], btc["px"] / close_at("USDT_IRT", LAST_CLOSED), delta=1)
        # technicals: a steady USD downtrend -> RSI low on 4h and daily, below its EMAs, at the bottom of its range
        self.assertLess(btc["rsi4h"], 30)
        self.assertLess(btc["rsi1d"], 30)
        self.assertEqual(len(btc["ema_dev_pct"]), 3)
        self.assertTrue(all(x < 0 for x in btc["ema_dev_pct"]))
        self.assertGreater(ctx["symbols"]["USDT_IRT"]["rsi4h"], 70)
        self.assertAlmostEqual(btc["atr4h_pct"], 0.4, delta=0.1)
        self.assertGreater(btc["vol_ratio"], 1.8)
        self.assertLess(btc["vol_ratio"], 2.1)
        # the 30-day risk fields (USDT terms): a monotone series has zero sigma and sits at its 30-day low
        self.assertEqual(btc["sig_d"], 0.0)
        self.assertAlmostEqual(btc["d30h"], (math.exp(H_BTC * 719) * 1.001 - 1) * 100, delta=0.05)
        self.assertLess(btc["pos30"], 0.05)
        self.assertGreater(ctx["symbols"]["ETH_IRT"]["sig_d"], 0)
        self.assertNotIn("beta_btc", btc)              # BTC's returns are constant here: no variance, no beta
        self.assertNotIn("cls", btc)                   # crypto is the default class: not shown
        # order book: 1% depth only (depth2_m is gone), no public-trades flow
        self.assertAlmostEqual(btc["book"]["spread_pct"], 0.2, places=2)
        self.assertEqual(set(btc["book"]), {"bid", "ask", "spread_pct", "depth1_m"})
        self.assertFalse([c for c in b.client.calls if isinstance(c, tuple) and c[0] == "matches"])
        # macro
        self.assertIn("usdt_irt", ctx["macro"])
        self.assertIn("gold_paxg", ctx["macro"])
        self.assertEqual(len(ctx["macro"]["gold_paxg"]["ret_usd_24h_7d_30d_pct"]), 3)
        self.assertIn("since_competition_start_pct", ctx["macro"]["usdt_irt"])
        self.assertNotIn("PAXG_IRT", ctx["symbols"])   # macro-only symbol
        # clock
        self.assertEqual(ctx["clock"]["now_utc"], "2026-09-22 09:31")
        self.assertEqual(ctx["clock"]["now_tehran"], "2026-09-22 13:01 Tue")
        self.assertAlmostEqual(ctx["clock"]["days_left"], (parse_utc(CFG["competition_end_utc"]) - NOW) / 86400.0, places=2)
        # the legend describes what is shown
        for s in ("sig_d", "d30h", "pos30", "beta_btc", "rsi1d", "depth1_m", "cls:"):
            self.assertIn(s, ctx["legend"])
        for s in ("rsi1h", "don_pos", "depth2_m", "flow:", "us_session"):
            self.assertNotIn(s, ctx["legend"])

    def test_portfolio_weights_use_the_runners_own_price_basis(self):
        """The stage-2 prompt carries two "current portfolio weights": the runner's own line and
        portfolio.weights from this context. They must be computed on the SAME prices (the newest
        closed 1h candle, which plan_orders sizes every order with), or a change the model sizes
        against one basis can come out below min_order_irt when the runner recomputes it. The
        symbols section keeps its live ticker px - only the valuation changes."""
        from bitpin.runner import plan_orders
        from bitpin.markets import D, ZERO
        bal = {"USDT": 1000, "BTC": 0.001}
        ctx = self.builder().build({"balances": dict(bal, IRT=100000000)})
        closes = {s: D(repr(close_at(s, LAST_CLOSED))) for s in ("USDT_IRT", "BTC_IRT")}
        units = {s: D(repr(bal[s.split("_")[0]])) for s in closes}
        plan = plan_orders({s: ZERO for s in closes}, units, closes, D(100000000), "0.02", "0.0035")
        for s in closes:
            self.assertAlmostEqual(ctx["portfolio"]["weights"][s], float(plan.current[s]), places=6)
        # the ticker is still what "px" reports (it is the live price, not a valuation)
        self.assertAlmostEqual(ctx["symbols"]["BTC_IRT"]["px"], close_at("BTC_IRT", LAST_CLOSED) * 1.001, delta=1)

    def test_portfolio_section(self):
        b = self.builder()
        snap = {"balances": {"IRT": 100000000, "USDT": 1000, "BTC": 0.001, "ZZZ": 5}, "equity_start_irt": 300000000}
        ctx = b.build(snap)
        pf = ctx["portfolio"]
        # the portfolio is valued at the newest CLOSED candle (the basis the runner sizes orders on),
        # not at the ticker (which this fake prices 0.1% higher)
        pu = close_at("USDT_IRT", LAST_CLOSED)
        pb = close_at("BTC_IRT", LAST_CLOSED)
        eq = 100000000 + 1000 * pu + 0.001 * pb
        self.assertAlmostEqual(pf["equity_irt"], eq, delta=1)
        self.assertAlmostEqual(pf["weights"]["USDT_IRT"], 1000 * pu / eq, places=5)
        self.assertAlmostEqual(pf["weights"]["BTC_IRT"], 0.001 * pb / eq, places=5)
        self.assertAlmostEqual(pf["irt_cash_weight"] + sum(pf["weights"].values()), 1.0, places=3)
        self.assertEqual(pf["unpriced_assets"], ["ZZZ"])
        self.assertAlmostEqual(pf["pnl_pct"], (eq / 300000000 - 1) * 100, places=2)
        self.assertEqual(pf["drawdown_pct"], 0)
        self.assertEqual(current_weights(ctx), pf["weights"])
        # v3: the coin share of the book (USDT_IRT is neither crypto nor RWA)
        self.assertAlmostEqual(pf["coin_share"], pf["weights"]["BTC_IRT"], places=3)
        self.assertEqual(pf["rwa_share"], 0)
        self.assertIn("portfolio: equity_irt", ctx["legend"])
        # a later, smaller balance: high-water mark persisted -> drawdown
        ctx2 = self.builder().build({"balances": {"IRT": 50000000, "USDT": 1000}})
        pf2 = ctx2["portfolio"]
        self.assertEqual(pf2["hwm_irt"], pf["hwm_irt"])
        self.assertGreater(pf2["drawdown_pct"], 5)
        self.assertEqual(pf2["equity_start_irt"], 300000000)  # persisted from the first snapshot
        # without any start equity configured, the first equity seen is used and persisted
        shutil.rmtree(self.dir)
        ctx3 = self.builder().build({"balances": {"IRT": 50000000}})
        self.assertEqual(ctx3["portfolio"]["equity_start_irt"], 50000000)
        self.assertEqual(ctx3["portfolio"]["drawdown_pct"], 0)

    def test_failing_symbol_and_endpoint_do_not_break(self):
        b = self.builder(bars=Bars(fail={"DOGE_IRT"}), client=FakeClient(book_fail={"ETH_IRT"}))
        ctx = b.build(None)
        self.assertIn("unavailable", ctx["symbols"]["DOGE_IRT"])
        self.assertIn("DOGE_IRT", ctx["data_errors"])
        self.assertIn("unavailable", ctx["symbols"]["ETH_IRT"]["book"])
        self.assertIn("ret_usdt", ctx["symbols"]["ETH_IRT"])
        self.assertIn("px", ctx["symbols"]["BTC_IRT"])
        self.assertNotIn("portfolio", ctx)

    def test_no_ticker_uses_closed_candle(self):
        class NoTickers(FakeClient):
            def tickers(self):
                raise RuntimeError("tickers down")
        ctx = self.builder(client=NoTickers()).build(None)
        self.assertAlmostEqual(ctx["symbols"]["BTC_IRT"]["px"], close_at("BTC_IRT", LAST_CLOSED),
                               delta=close_at("BTC_IRT", LAST_CLOSED) * 1e-5)
        self.assertIn("tickers", ctx["data_errors"])

    def test_rule_signals(self):
        ctx = self.builder().build(None)
        rs = ctx["rule_signals"]
        self.assertIn("REJECTED", rs["note"])
        self.assertEqual(rs["strategies"]["hold_usdt"]["targets"], {"USDT_IRT": 1.0})
        self.assertTrue(rs["strategies"]["hold_usdt"]["holdout_verified"])
        fx = rs["strategies"]["fixed_btc"]
        self.assertEqual(fx["targets"], {"BTC_IRT": 0.4, "USDT_IRT": 0.5})
        self.assertEqual(fx["irt_cash"], 0.1)
        self.assertEqual(fx["res"], "4h")
        self.assertFalse(fx["holdout_verified"])
        self.assertIn("broken", rs["errors"])
        self.assertIn("ZeroDivisionError", rs["errors"]["broken"])

    def test_rule_signals_and_matches_are_off_by_default(self):
        """C5: the rejected strategies block is gone unless context.rule_signals is switched on; the
        public-trades flow block is gone for good (context.matches is accepted, does nothing)."""
        cfg = {k: v for k, v in CFG.items() if k != "rule_signals"}
        b = MarketContextBuilder(FakeClient(), cfg, state_dir=self.dir, bars_source=Bars(), clock=lambda: NOW,
                                 strategies=STRATS)
        self.assertFalse(b.cfg["rule_signals"])
        self.assertFalse(b.cfg["matches"])
        self.assertNotIn("rule_signals", b.build(None))
        b2 = MarketContextBuilder(FakeClient(), dict(cfg, matches=True), state_dir=self.dir, bars_source=Bars(),
                                  clock=lambda: NOW, strategies=STRATS)
        ctx = b2.build(None)
        self.assertNotIn("flow", ctx["symbols"]["BTC_IRT"])
        self.assertFalse([c for c in b2.client.calls if isinstance(c, tuple) and c[0] == "matches"])

    def test_rule_signals_time_budget(self):
        ticks = {"t": 0.0}

        def mono():
            ticks["t"] += 1.0
            return ticks["t"]
        b = MarketContextBuilder(FakeClient(), dict(CFG, rule_time_budget_seconds=0), state_dir=self.dir,
                                 bars_source=Bars(), clock=lambda: NOW, strategies=STRATS, monotonic=mono)
        rs = b.build(None)["rule_signals"]
        self.assertEqual(sorted(rs["skipped_time_budget"]), ["broken", "fixed_btc", "hold_usdt"])

    def test_incremental_candle_cache(self):
        bars = Bars()
        b = self.builder(bars=bars, rule_signals=False)
        b.build(None)
        first = {c[0]: c[2] for c in bars.calls}
        # USDT_IRT and BTC_IRT are fetched with the 84-day trend history, the others with analysis_lookback_bars
        from bitpin.analysis import TREND_BARS
        self.assertLessEqual(first["BTC_IRT"], NOW - TREND_BARS * 3600)
        self.assertLessEqual(first["USDT_IRT"], NOW - TREND_BARS * 3600)
        self.assertGreater(first["ETH_IRT"], NOW - 1100 * 3600)
        bars.calls.clear()
        b.build(None, now=NOW + 3600)
        for sym, _, start, _ in bars.calls:
            self.assertEqual(start, LAST_CLOSED - 3 * 3600)   # only the newest bars are fetched
            self.assertLess(first[sym], start)

    def test_recent_decisions_and_shrink(self):
        pu = close_at("USDT_IRT", LAST_CLOSED)   # portfolio basis: the closed candle, not the ticker
        recent = [{"t": NOW - 7200, "targets": {"USDT_IRT": 0.8, "BTC_IRT": 0.2, "ETH_IRT": 0.0}, "confidence": 0.6,
                   "hold": False, "reasoning": "x" * 1000, "equity_irt": 1000000000, "usdt_irt": pu / 1.01}]
        b = self.builder()
        ctx = b.build({"balances": {"USDT": 5000}}, recent_decisions=recent)
        rd = ctx["recent_decisions"][0]
        self.assertEqual(rd["age_h"], 2.0)
        self.assertEqual(rd["targets"], {"USDT_IRT": 0.8, "BTC_IRT": 0.2})
        self.assertNotIn("reasoning", rd)          # the model's free text is never fed back (injection persistence)
        self.assertNotIn("x" * 50, dumps(ctx))
        self.assertAlmostEqual(rd["usdt_irt_chg_pct"], 1.0, places=2)
        self.assertAlmostEqual(rd["equity_chg_pct"], (5000 * pu / 1e9 - 1) * 100, places=1)
        full = len(dumps(ctx))
        small = self.builder(max_context_chars=int(full * 0.8)).build({"balances": {"USDT": 5000}}, recent)
        self.assertIn("truncated", small)
        self.assertTrue(set(small["truncated"]) <= {"rule_desc", "betas", "book", "adjusted"}, small["truncated"])
        self.assertLess(len(dumps(small)), full)
        tiny = self.builder(max_context_chars=int(full * 0.5)).build({"balances": {"USDT": 5000}}, recent)
        self.assertIn("book", tiny["truncated"])
        self.assertNotIn("book", tiny["symbols"]["BTC_IRT"])
        self.assertNotIn("pos30", tiny["symbols"]["BTC_IRT"])

    def test_helpers(self):
        bs = book_stats({"bids": [["990000", "100"], ["985000", "100"], ["970000", "100"], ["bad", "1"]],
                         "asks": [["1010000", "100"], ["1020000", "100"], ["1035000", "100"]]})
        self.assertEqual(bs["spread_pct"], 2.0)
        self.assertEqual(bs["depth1_m"], [99.0, 101.0])
        self.assertNotIn("depth2_m", bs)
        self.assertEqual(book_stats({"bids": [], "asks": []}), {"empty": True})
        self.assertEqual(clean({"a": None, "b": float("nan"), "c": [], "d": {"e": None}, "f": 1}), {"f": 1})
        self.assertEqual(parse_utc("2026-10-22T20:30:00Z"), 1792701000.0)
        ctx = {"symbols": {"USDT_IRT": {"px": 200000}, "BTC_IRT": {"px": 1, "px_usdt": 80000},
                           "ETH_IRT": {"unavailable": "x"}}}
        self.assertEqual(usdt_prices(ctx), {"USDT_IRT": 200000.0, "BTC_IRT": 80000.0})

    def test_quick_context_for_scheduling(self):
        client = FakeClient()
        bars = Bars()
        b = self.builder(bars=bars, client=client)
        q = b.quick_context({"balances": {"USDT": 1000, "BTC": 0.001}})
        self.assertEqual(bars.calls, [])                      # no candles
        self.assertEqual(client.calls, ["tickers"])           # a single public request
        self.assertTrue(q["quick"])
        pu = close_at("USDT_IRT", LAST_CLOSED) * 1.001   # quick_context fetches no candles: the ticker price
        self.assertAlmostEqual(q["symbols"]["USDT_IRT"]["px"], pu, delta=1)
        self.assertAlmostEqual(q["symbols"]["BTC_IRT"]["px_usdt"], usd_px("BTC_IRT", LAST_CLOSED), delta=0.1)
        self.assertEqual(usdt_prices(q)["BTC_IRT"], q["symbols"]["BTC_IRT"]["px_usdt"])
        self.assertIn("drawdown_pct", q["portfolio"])
        self.assertIn("BTC_IRT", q["portfolio"]["weights"])
        self.assertNotIn("sigma7_equity_pct", q["portfolio"])   # no candles: no risk block

    def test_one_shot_function(self):
        ctx = build_market_context(FakeClient(), dict(CFG, rule_signals=False), None, None, NOW, Bars())
        self.assertNotIn("rule_signals", ctx)
        self.assertIn("BTC_IRT", ctx["symbols"])

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            MarketContextBuilder(FakeClient(), {"univers": []}, self.dir)
        # every setting, the competition dates included, is checked at construction (startup, exit 78),
        # not at the first quick_context()/build() hours later
        for bad in ({"competition_end_utc": "22 Oct 2026"}, {"competition_end_utc": "2026-10-23 00:00 Tehrn"},
                    {"competition_end_utc": "2026-13-01T00:00:00Z"}, {"competition_end_utc": 1792701000},
                    {"competition_start_utc": "2026-10-23T00:00:00Z"},           # after the end
                    {"irt_unit_divisor": "10"}, {"irt_unit_divisor": 0}, {"orderbook": "yes"}, {"matches": "no"},
                    {"universe": ["BTC"]}, {"lookback_bars": 10}, {"irt_asset": ""}, {"rule_params": []}):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                MarketContextBuilder(FakeClient(), dict(CFG, **bad), self.dir)
        with self.assertRaises(TypeError):        # no silent default: the equity baseline must be persisted
            MarketContextBuilder(FakeClient(), CFG)
        blocker = os.path.join(self.dir, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        with self.assertRaises(ConfigError):
            MarketContextBuilder(FakeClient(), CFG, os.path.join(blocker, "state"))
        # the one-year competition dates of v3 are accepted like any other ISO date
        b = MarketContextBuilder(FakeClient(), dict(CFG, competition_end_utc="2027-09-21T20:30:00Z"), self.dir)
        self.assertEqual(b._end, parse_utc("2027-09-22 00:00 Tehran"))

    def test_date_formats_are_the_same_on_every_python(self):
        end = 1792701000.0                        # 2026-10-22 20:30 UTC = 2026-10-23 00:00 Tehran
        for s in ("2026-10-22T20:30:00Z", "2026-10-22T20:30:00", "2026-10-22 20:30", "2026-10-22T20:30Z",
                  "2026-10-22T20:30:00.000Z", "2026-10-22T20:30:00.0Z", "2026-10-22T20:30:00+00:00",
                  "2026-10-23T00:00:00+03:30", "2026-10-23 00:00 Tehran", "2026-10-23T00:00:00+0330",
                  "2026-10-22T20:30:00 UTC", "2026-10-22t20:30:00z", "2026-10-22 24:00 +03:30"):
            self.assertEqual(parse_utc(s), end, s)
        self.assertEqual(parse_utc("2026-10-22T20:30:00.5Z"), end + 0.5)
        self.assertEqual(parse_utc("2026-10-22"), end - 20.5 * 3600)
        for bad in ("22 Oct 2026", "2026-10-22T25:00Z", "2026-10-22T24:30Z", "2026-02-30", "2026-10-22T20:30+15:00", ""):
            with self.assertRaises(ValueError, msg=bad):
                parse_utc(bad)
        b = MarketContextBuilder(FakeClient(), dict(CFG, competition_end_utc="2026-10-23 00:00 Tehran"), self.dir,
                                 bars_source=Bars(), clock=lambda: NOW, strategies=STRATS)
        self.assertEqual(b.quick_context(None)["clock"]["competition_end_utc"], "2026-10-22 20:30")

    def test_quick_context_and_build_never_raise(self):
        b = self.builder()

        def boom(*a, **kw):
            raise KeyError("unexpected")
        b._quick_context = boom
        b._build = boom
        q = b.quick_context({"balances": {}})
        self.assertTrue(q["quick"])
        self.assertIn("KeyError", q["data_errors"]["quick_context"])
        self.assertIn("now_utc", q["clock"])
        full = b.build({"balances": {}})
        self.assertIn("KeyError", full["data_errors"]["build"])
        self.assertNotIn("symbols", full)          # KimiBrain refuses to decide on it (no LLM call)

    def test_build_is_bounded_by_a_time_budget(self):
        # finding 6: one build() could block the single-threaded runner for about an hour
        ticks = {"t": 0.0}

        def mono():
            ticks["t"] += 20.0                   # every check sees 20 s more (slow, throttled API)
            return ticks["t"]
        bars = Bars()
        client = FakeClient()
        b = MarketContextBuilder(client, dict(CFG, build_time_budget_seconds=100), state_dir=self.dir,
                                 bars_source=bars, clock=lambda: NOW, strategies=STRATS, monotonic=mono)
        ctx = b.build({"balances": {"USDT": 10}})
        self.assertIn("time budget", ctx["data_errors"]["halted"])
        self.assertLess(len(bars.calls) + len(client.calls), 8)
        skipped = [s for s, f in ctx.get("symbols", {}).items() if "unavailable" in f]
        self.assertTrue(skipped)
        self.assertTrue(all("skipped" in ctx["symbols"][s]["unavailable"] for s in skipped))

    def test_abort_stops_the_build(self):
        bars, client = Bars(), FakeClient()
        ctx = self.builder(bars=bars, client=client).build(None, abort=lambda: "STOP file present")
        self.assertEqual(bars.calls, [])
        self.assertEqual(client.calls, [])
        self.assertIn("STOP file present", ctx["data_errors"]["halted"])
        self.assertTrue(all("unavailable" in f for f in ctx["symbols"].values()))

    def test_no_usdt_candles_means_no_other_fetches(self):
        bars, client = Bars(fail={"USDT_IRT"}), FakeClient()
        ctx = self.builder(bars=bars, client=client).build(None)
        self.assertEqual([c[0] for c in bars.calls], ["USDT_IRT"])
        self.assertEqual(client.calls, ["tickers"])
        self.assertIn("USDT_IRT candles unavailable", ctx["symbols"]["BTC_IRT"]["unavailable"])
        self.assertIn("unavailable", ctx["macro"]["btc_trend84"])

    def test_no_tickers_means_no_book_fetches_and_no_increases(self):
        class NoTickers(FakeClient):
            def tickers(self):
                raise RuntimeError("tickers down")
        client = NoTickers()
        ctx = self.builder(client=client).build(None)
        self.assertFalse([c for c in client.calls if isinstance(c, tuple)])      # no orderbook / matches calls
        btc = ctx["symbols"]["BTC_IRT"]
        self.assertIn("px", btc)
        self.assertIn("skipped", btc["book"]["unavailable"])
        self.assertEqual(btc["blocked"], "order book not available")

    def test_market_status_and_book_health_block_increases(self):
        # finding 5: a suspended market or a broken book must not be increased (USDT would be sold for nothing)
        class Client(FakeClient):
            def markets(self):
                self.calls.append("markets")
                return [{"symbol": "BTC_IRT", "tradable": True, "suspended": True},
                        {"symbol": "DOGE_IRT", "tradable": False, "suspended": False},
                        {"symbol": "ETH_IRT", "tradable": True, "suspended": False}]

            def orderbook(self, symbol):
                self.calls.append(("orderbook", symbol))
                p = close_at(symbol, LAST_CLOSED)
                if symbol == "ETH_IRT":                          # 5% spread
                    return {"bids": [[str(p * 0.975), "10"]], "asks": [[str(p * 1.025), "10"]]}
                if symbol == "USDT_IRT":                         # one-sided
                    return {"bids": [[str(p), "10"]], "asks": []}
                return FakeClient.orderbook(self, symbol)
        ctx = self.builder(client=Client()).build(None)
        s = ctx["symbols"]
        self.assertIn("suspended", s["BTC_IRT"]["blocked"])
        self.assertIn("not tradable", s["DOGE_IRT"]["blocked"])
        self.assertIn("max_spread_pct", s["ETH_IRT"]["blocked"])
        self.assertIn("one-sided", s["USDT_IRT"]["blocked"])
        self.assertIn("blocked", ctx["legend"])
        ok = self.builder(client=Client(), max_spread_pct=6.0).build(None)["symbols"]
        self.assertNotIn("blocked", ok["ETH_IRT"])
        # without an order book the book is not a reason; the market status still is
        nob = self.builder(client=Client(), orderbook=False).build(None)["symbols"]
        self.assertNotIn("blocked", nob["ETH_IRT"])
        self.assertIn("suspended", nob["BTC_IRT"]["blocked"])
        # a failing markets() blocks nothing by itself
        class Down(Client):
            def markets(self):
                raise RuntimeError("down")
        ctx = self.builder(client=Down(), max_spread_pct=6.0).build(None)
        self.assertIn("markets", ctx["data_errors"])
        self.assertNotIn("blocked", ctx["symbols"]["BTC_IRT"])
        # the brain treats blocked symbols as not tradable
        from bitpin.brain import KimiBrain
        brain = KimiBrain(None, {"allowed_symbols": ["USDT_IRT", "BTC_IRT", "ETH_IRT", "DOGE_IRT"]}, self.dir)
        self.assertEqual(brain.tradable_symbols({"symbols": s}), set())
        self.assertEqual(brain.tradable_symbols({"symbols": ok}), {"ETH_IRT"})

    def test_drawdown_uses_the_breakers_high_water_mark(self):
        pu = close_at("USDT_IRT", LAST_CLOSED)   # portfolio basis: the closed candle, not the ticker
        b = self.builder(rule_signals=False)
        b.build({"balances": {"USDT": 2000}})                          # persisted HWM: 2000 USDT
        pf = b.build({"balances": {"USDT": 1000}, "high_water_mark_irt": 1250 * pu})["portfolio"]
        self.assertAlmostEqual(pf["drawdown_pct"], 20.0, places=2)     # the runner's HWM, not the persisted one
        pf = b.build({"balances": {"USDT": 1000}})["portfolio"]
        self.assertAlmostEqual(pf["drawdown_pct"], 50.0, places=2)

    def test_the_distance_to_the_halt_and_the_costs_come_from_the_snapshot(self):
        """C2: the runner adds halt_drawdown_pct (the breaker's threshold), the cumulative fees and
        slippage since the competition start and the 7-day turnover to the snapshot; the context
        shows the drawdown left before the halt, the costs in M IRT and % of equity and the
        turnover in % of equity. Junk values are dropped, never a crash."""
        pu = close_at("USDT_IRT", LAST_CLOSED)
        b = self.builder(rule_signals=False)
        b.build({"balances": {"USDT": 2000}})
        eq = 1000 * pu
        ctx = b.build({"balances": {"USDT": 1000}, "high_water_mark_irt": 1250 * pu, "halt_drawdown_pct": 50,
                       "fees_irt": 0.01 * eq, "slippage_irt": 0.005 * eq, "turnover_7d_irt": 0.4 * eq})
        pf = ctx["portfolio"]
        self.assertEqual((pf["halt_at_drawdown_pct"], pf["to_halt_pct"]), (50.0, 30.0))
        self.assertAlmostEqual(pf["costs_since_start"]["pct_of_equity"], 1.5, places=2)
        self.assertAlmostEqual(pf["costs_since_start"]["fees_m"], 0.01 * eq / 1e6, places=4)
        self.assertAlmostEqual(pf["turnover_7d_pct"], 40.0, places=2)
        for s in ("to_halt_pct", "costs_since_start", "turnover_7d_pct"):
            self.assertIn(s, ctx["legend"])
        pf = b.build({"balances": {"USDT": 1000}, "halt_drawdown_pct": "junk", "fees_irt": -5, "turnover_7d_irt": None,
                      "slippage_irt": "x"})["portfolio"]
        for k in ("halt_at_drawdown_pct", "to_halt_pct", "costs_since_start", "turnover_7d_pct"):
            self.assertNotIn(k, pf)
        self.assertIn("weights", pf)

    def test_fallback_decisions_are_marked(self):
        recent = [{"t": NOW - 3600, "targets": {"USDT_IRT": 0.95}, "confidence": 0.0, "fallback": "cash_sweep"}]
        rd = self.builder(rule_signals=False).build(None, recent_decisions=recent)["recent_decisions"][0]
        self.assertEqual(rd["fallback"], "cash_sweep")
        self.assertNotIn("confidence", rd)

    def test_toman_under_another_wallet_code(self):
        # the runner's broker.balances() puts toman under "IRT"; raw wallet codes need irt_asset/divisor
        pu = close_at("USDT_IRT", LAST_CLOSED)   # portfolio basis: the closed candle, not the ticker
        snap = {"balances": {"RIAL": 5e9, "USDT": 5000}}
        pf = self.builder(rule_signals=False).build(snap)["portfolio"]
        self.assertEqual(pf["unpriced_toman_like"], ["RIAL"])       # hidden toman: the brain refuses to decide
        self.assertAlmostEqual(pf["equity_irt"], 5000 * pu, delta=1)
        shutil.rmtree(self.dir)
        pf = self.builder(rule_signals=False, irt_asset="rial", irt_unit_divisor=10).build(snap)["portfolio"]
        self.assertNotIn("unpriced_toman_like", pf)
        self.assertEqual(pf["irt_cash"], 5e8)
        self.assertAlmostEqual(pf["equity_irt"], 5e8 + 5000 * pu, delta=1)
        self.assertAlmostEqual(pf["irt_cash_weight"] + pf["weights"]["USDT_IRT"], 1.0, places=3)

    def test_the_84_day_trend_state_of_btc(self):
        """C4: macro.btc_trend84 = BTC/USDT vs its close 84 and 63 days ago; on = above the 84-day one.
        USDT_IRT and BTC_IRT are fetched with that much history whatever analysis_lookback_bars says;
        with less history the field says so (never a crash, never a cap)."""
        ctx = self.builder(rule_signals=False).build(None)
        tr = ctx["macro"]["btc_trend84"]
        self.assertFalse(tr["on"])                       # a steady USD downtrend: below both references
        self.assertAlmostEqual(tr["vs84d_pct"], (math.exp(H_BTC * 84 * 24) - 1) * 100, delta=0.05)
        self.assertAlmostEqual(tr["vs63d_pct"], (math.exp(H_BTC * 63 * 24) - 1) * 100, delta=0.05)
        self.assertIn("btc_trend84", ctx["legend"])

        class Young(Bars):
            def __call__(self, symbol, res, start, end):
                if symbol == "BTC_IRT":
                    start = max(start, LAST_CLOSED - 40 * 24 * 3600)
                return Bars.__call__(self, symbol, res, start, end)
        ctx = self.builder(bars=Young(), rule_signals=False).build(None)
        self.assertIn("needs 84 days", ctx["macro"]["btc_trend84"]["unavailable"])
        self.assertNotIn("btc_trend84", ctx["legend"])
        self.assertIn("ret_usdt", ctx["symbols"]["BTC_IRT"])   # the rest of BTC is still there


# --------------------------------------------------------------------------- asset classes, the US session, risk

from bitpin.analysis import (ASSET_CLASSES, ASSET_CLASS_NAMES, PLAN_HORIZON_HOURS, PLAN_HORIZON_STORED,  # noqa: E402
                             RWA_MAX_SPREAD_PCT, asset_class, beta_corr, clean_plan, compact_row, grid_log_returns,
                             portfolio_risk, range_position, session_bound, sigma_daily_pct, trend_state,
                             us_session_next_open, us_session_open)

H = 3600


class SynthMarket:
    """Synthetic closed candles up to LAST_CLOSED for any symbol list: usd(sym, k) gives the USD price k
    hours after LAST_CLOSED (k <= 0); the toman rate drifts G_USDT per hour. Order books with a spread
    per symbol; a young symbol has only `young[sym]` hours of history."""

    def __init__(self, syms, usd, spreads=None, young=None):
        self.syms, self.usd, self.spreads, self.young = list(syms), usd, spreads or {}, young or {}

    def close(self, sym, ts):
        u = usdt_px(ts)
        return u if sym == "USDT_IRT" else u * self.usd(sym, (ts - LAST_CLOSED) / 3600.0)

    def bars(self, symbol, res, start, end):
        first = start - (start % 3600) + 1800
        first += 3600 if first < start else 0
        if symbol in self.young:
            first = max(first, LAST_CLOSED - self.young[symbol] * 3600)
        return [Bar(t, self.close(symbol, t), self.close(symbol, t) * 1.003, self.close(symbol, t) * 0.997,
                    self.close(symbol, t), 3.0) for t in range(int(first), int(min(end, LAST_CLOSED)) + 1, 3600)]

    def tickers(self):
        return [{"symbol": s, "price": str(self.close(s, LAST_CLOSED))} for s in self.syms]

    def orderbook(self, s):
        p = self.close(s, LAST_CLOSED)
        w = self.spreads.get(s, 0.001)
        return {"bids": [[str(p * (1 - w * i)), "5"] for i in range(1, 21)],
                "asks": [[str(p * (1 + w * i)), "5"] for i in range(1, 21)]}

    def builder(self, now=NOW, **cfg):
        c = {"universe": self.syms, "macro_symbols": ["USDT_IRT"], "max_context_chars": 0}
        c.update(cfg)
        return MarketContextBuilder(self, c, state_dir=None, bars_source=self.bars, clock=lambda: now)


def wavy_usd(sym, k):
    """BTC-driven prices: ETH has twice BTC's log moves, PAXG an independent wave, DOGE is flat."""
    btc = 0.03 * math.sin(k / 5.0) + 0.01 * math.sin(k / 1.7)
    return {"BTC_IRT": 80000.0 * math.exp(btc), "ETH_IRT": 3000.0 * math.exp(2 * btc),
            "PAXG_IRT": 3500.0 * math.exp(0.01 * math.cos(k / 3.3)), "DOGE_IRT": 0.2,
            "GOOGLX_IRT": 250.0 * math.exp(0.5 * btc + 0.02 * math.sin(k / 2.9))}.get(
                sym, 1.0 * math.exp(0.02 * math.sin(k / 4.1)))


class TestAssetClassesAndSession(unittest.TestCase):
    def test_asset_class_is_a_static_map_with_crypto_as_the_default(self):
        self.assertEqual(asset_class("BTC_IRT"), "crypto")
        for sym, cls in (("PAXG_IRT", "gold"), ("xaut", "gold"), ("GLDON_IRT", "gold"), ("SLVON_IRT", "silver"),
                         ("USOON_IRT", "oil"), ("UNGON_IRT", "gas"), ("COPXON_IRT", "copper"),
                         ("NVDAX_IRT", "us_stock"), ("GOOGLX_USDT", "us_stock"), ("MSFTON_IRT", "us_stock"),
                         ("ORCLB_IRT", "us_stock"), ("SPYON_IRT", "us_etf"), ("QQQON_IRT", "us_etf"),
                         ("SMHB_IRT", "us_etf"), ("DRAMB_IRT", "us_etf"), ("IEFAON_IRT", "us_etf"),
                         ("TLTON_IRT", "bond"), ("AGGON_IRT", "bond"),
                         ("COINX_IRT", "crypto_beta"), ("CRCLX_IRT", "crypto_beta"), ("MSTRON_IRT", "crypto_beta"),
                         ("HOODX_IRT", "crypto_beta")):
            self.assertEqual(asset_class(sym), cls, sym)
        # no suffix rules: coins ending in X / ON / B stay crypto
        for sym in ("TRX_IRT", "AVAX_IRT", "TON_IRT", "ARB_IRT", "BNB_IRT", "SHIB_IRT", "AGLD_IRT", "", None):
            self.assertEqual(asset_class(sym), "crypto", sym)
        self.assertTrue(set(ASSET_CLASSES.values()) <= set(ASSET_CLASS_NAMES))
        self.assertGreaterEqual(len(ASSET_CLASSES), 75)
        # the session-bound classes: everything but crypto, gold and silver - plus the Ondo FUND tokens of
        # gold and silver (GLDON, SLVON): their reference trades only in the US session (v3 review)
        for sym in ("BTC_IRT", "PAXG_IRT", "XAUT_IRT"):
            self.assertFalse(session_bound(sym), sym)
        for sym in ("USOON_IRT", "UNGON_IRT", "COPXON_IRT", "NVDAX_IRT", "SPYON_IRT", "TLTON_IRT", "COINX_IRT",
                    "SLVON_IRT", "GLDON_IRT", "slvon"):
            self.assertTrue(session_bound(sym), sym)
        self.assertEqual((asset_class("SLVON_IRT"), asset_class("GLDON_IRT")), ("silver", "gold"))   # class unchanged

    def test_us_session_follows_us_daylight_saving(self):
        """09:30-16:00 New York: 13:30-20:00 UTC in US daylight time, 14:30-21:00 UTC in standard time
        (2026-11-01 to 2027-03-14); the switch Sundays themselves are weekend days."""
        for s, want in (("2026-12-01T13:45:00Z", False), ("2026-12-01T14:29:59Z", False), ("2026-12-01T14:30:00Z", True),
                        ("2026-12-01T20:30:00Z", True), ("2026-12-01T21:00:00Z", False),
                        ("2026-10-30T13:45:00Z", True), ("2026-11-02T13:45:00Z", False), ("2026-11-02T14:45:00Z", True),
                        ("2027-03-12T13:45:00Z", False), ("2027-03-15T13:45:00Z", True), ("2027-03-15T20:30:00Z", False)):
            self.assertEqual(us_session_open(parse_utc(s)), want, s)
        self.assertEqual(us_session_next_open(parse_utc("2026-12-01T09:31:00Z")), parse_utc("2026-12-01T14:30:00Z"))
        self.assertEqual(us_session_next_open(parse_utc("2026-10-30T20:30:00Z")), parse_utc("2026-11-02T14:30:00Z"))
        self.assertEqual(us_session_next_open(parse_utc("2027-03-12T21:30:00Z")), parse_utc("2027-03-15T13:30:00Z"))
        # the daily 19:00 Tehran slot (15:30 UTC) is inside the session in both seasons, on every weekday
        for day in ("2026-09-28", "2026-12-07", "2027-03-16", "2027-07-02"):
            self.assertTrue(us_session_open(parse_utc(day + "T15:31:00Z")), day)

    def test_us_session_hours(self):
        """Mon-Fri 13:30-20:00 UTC in US daylight time, no holiday calendar; the next open is Monday 13:30
        over a weekend."""
        for s, want in (("2026-09-22T13:30:00Z", True), ("2026-09-22T13:29:59Z", False), ("2026-09-22T19:59:00Z", True),
                        ("2026-09-22T20:00:00Z", False), ("2026-09-22T09:31:00Z", False), ("2026-09-25T15:00:00Z", True),
                        ("2026-09-26T15:00:00Z", False), ("2026-09-27T15:00:00Z", False), ("2026-09-28T13:30:00Z", True)):
            self.assertEqual(us_session_open(parse_utc(s)), want, s)
        self.assertEqual(us_session_next_open(parse_utc("2026-09-22T15:00:00Z")), parse_utc("2026-09-22T15:00:00Z"))
        self.assertEqual(us_session_next_open(parse_utc("2026-09-22T09:31:00Z")), parse_utc("2026-09-22T13:30:00Z"))
        self.assertEqual(us_session_next_open(parse_utc("2026-09-22T20:00:00Z")), parse_utc("2026-09-23T13:30:00Z"))
        self.assertEqual(us_session_next_open(parse_utc("2026-09-25T20:30:00Z")), parse_utc("2026-09-28T13:30:00Z"))
        self.assertEqual(us_session_next_open(parse_utc("2026-09-26T02:00:00Z")), parse_utc("2026-09-28T13:30:00Z"))

    def test_session_bound_tokens_carry_the_session_and_are_blocked_outside_it(self):
        """C3: a stock / ETF / oil token shows us_session, us_open_in_h and quote_noise_pct and is
        blocked (not increased by the brain, no buy by the runner; selling allowed) while the US
        market is closed or its spread exceeds 1%, whatever max_spread_pct says. Gold is never."""
        syms = ["USDT_IRT", "BTC_IRT", "PAXG_IRT", "GOOGLX_IRT", "USOON_IRT", "COINX_IRT"]
        m = SynthMarket(syms, wavy_usd, spreads={"USOON_IRT": 0.006})       # 1.2% spread: above the 1% RWA cap
        ctx = m.builder(max_spread_pct=6.0).build(None)          # NOW = Tuesday 09:31 UTC: closed
        g = ctx["symbols"]["GOOGLX_IRT"]
        self.assertEqual((g["cls"], g["us_session"], g["us_open_in_h"]), ("us_stock", "closed", 4.0))
        self.assertGreater(g["quote_noise_pct"], 0)
        self.assertEqual(g["blocked"], "us market closed (Mon-Fri 09:30-16:00 New York)")
        self.assertEqual(ctx["symbols"]["COINX_IRT"]["cls"], "crypto_beta")
        self.assertIn("blocked", ctx["symbols"]["COINX_IRT"])
        self.assertEqual(ctx["symbols"]["PAXG_IRT"]["cls"], "gold")
        for k in ("us_session", "blocked", "quote_noise_pct"):
            self.assertNotIn(k, ctx["symbols"]["PAXG_IRT"])
            self.assertNotIn(k, ctx["symbols"]["BTC_IRT"])
        self.assertIn("us_session: open / closed", ctx["legend"])
        # during the session: open, no block for a tight book; the 1% RWA spread cap still blocks oil
        open_now = LAST_CLOSED + 5 * H + 90                       # 13:31 UTC Tuesday
        ctx = m.builder(now=open_now, max_spread_pct=6.0).build(None)
        g = ctx["symbols"]["GOOGLX_IRT"]
        self.assertEqual(g["us_session"], "open")
        self.assertNotIn("us_open_in_h", g)
        self.assertNotIn("blocked", g)
        oil = ctx["symbols"]["USOON_IRT"]
        self.assertGreater(oil["book"]["spread_pct"], RWA_MAX_SPREAD_PCT)
        self.assertIn("us market token", oil["blocked"])
        self.assertNotIn("blocked", ctx["symbols"]["BTC_IRT"])
        # the brain reads the same "blocked" key: a closed token cannot be increased, gold can
        from bitpin.brain import KimiBrain
        d = tempfile.mkdtemp(prefix="ctxcls_")
        self.addCleanup(shutil.rmtree, d, True)
        brain = KimiBrain(None, {"allowed_symbols": syms}, d)
        self.assertEqual(brain.tradable_symbols(m.builder(max_spread_pct=6.0).build(None)),
                         {"USDT_IRT", "BTC_IRT", "PAXG_IRT"})
        # a universe without session-bound tokens carries no such legend
        self.assertNotIn("us_session", SynthMarket(syms[:3], wavy_usd).builder().build(None)["legend"])


class TestRiskFields(unittest.TestCase):
    def test_sigma_range_beta_and_correlation(self):
        m = SynthMarket(["USDT_IRT", "BTC_IRT", "ETH_IRT", "PAXG_IRT", "DOGE_IRT"], wavy_usd)
        ctx = m.builder().build(None)
        btc, eth, paxg, doge = (ctx["symbols"][s] for s in ("BTC_IRT", "ETH_IRT", "PAXG_IRT", "DOGE_IRT"))
        # ETH's log moves are exactly twice BTC's: beta 2, correlation 1; BTC vs itself 1 / 1
        self.assertEqual((btc["beta_btc"], btc["corr_btc"]), (1.0, 1.0))
        self.assertEqual((eth["beta_btc"], eth["corr_btc"]), (2.0, 1.0))
        self.assertAlmostEqual(eth["sig_d"], 2 * btc["sig_d"], delta=0.02)
        self.assertLess(abs(paxg["corr_btc"]), 0.5)           # an independent wave
        self.assertNotIn("beta_btc", doge)                    # a flat price has no variance
        self.assertEqual(doge["sig_d"], 0.0)
        # sig_d = std of the last 168 hourly log returns x sqrt(24), in %
        ser = [(t, wavy_usd("BTC_IRT", (t - LAST_CLOSED) / 3600.0)) for t in range(LAST_CLOSED - 400 * H, LAST_CLOSED + 1, H)]
        rets = [math.log(b[1] / a[1]) for a, b in zip(ser[-169:], ser[-168:])]
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))
        self.assertAlmostEqual(btc["sig_d"], sd * math.sqrt(24) * 100, places=2)
        self.assertEqual(sigma_daily_pct(ser[-20:]), None)   # fewer than 24 returns
        # d30h / pos30: the live px vs the highest close of the last 720 closes and its place in that range
        closes = [c for _, c in ser[-720:]]
        px = wavy_usd("BTC_IRT", 0)
        self.assertAlmostEqual(btc["d30h"], (px / max(closes) - 1) * 100, places=1)
        self.assertAlmostEqual(btc["pos30"], (px - min(closes)) / (max(closes) - min(closes)), places=1)
        self.assertEqual(range_position(ser[-10:], px), (None, None))
        self.assertEqual(range_position(ser, None), (None, None))
        # beta needs 30 common 4h points
        gb = grid_log_returns(ser, LAST_CLOSED)
        self.assertEqual(len(gb), 100)
        self.assertEqual(beta_corr(dict(list(gb.items())[:20]), gb), (None, None))
        self.assertEqual(beta_corr(gb, {k: 0.0 for k in gb}), (None, None))

    def test_the_portfolio_risk_block(self):
        """C2: coin_share / rwa_share, the weighted beta, the average pairwise correlation, the 7-day
        equity sigma and the effective number of bets of the HELD coins; USDT_IRT carries no risk in
        USDT terms; loss_if_all_stops_pct counts only the positions that have a stop."""
        m = SynthMarket(["USDT_IRT", "BTC_IRT", "ETH_IRT", "PAXG_IRT", "DOGE_IRT"], wavy_usd)
        b = m.builder()
        bal = {"USDT": 1000, "BTC": 1000 / wavy_usd("BTC_IRT", 0), "ETH": 1000 / wavy_usd("ETH_IRT", 0),
               "PAXG": 2000 / wavy_usd("PAXG_IRT", 0)}
        eth_px = wavy_usd("ETH_IRT", 0)
        pos = {"ETH_IRT": {"entry_ts": NOW - 5 * H, "entry_px_usdt": eth_px, "stop_pct": 10.0, "source": "kimi"},
               "BTC_IRT": {"entry_ts": NOW - 5 * H, "entry_px_usdt": 1.0, "source": "kimi"}}      # no stop
        ctx = b.build({"balances": bal, "halt_drawdown_pct": 50}, positions=pos)
        pf = ctx["portfolio"]
        w = pf["weights"]
        self.assertAlmostEqual(pf["coin_share"], w["BTC_IRT"] + w["ETH_IRT"], places=3)
        self.assertAlmostEqual(pf["rwa_share"], w["PAXG_IRT"], places=3)
        syms = ctx["symbols"]
        self.assertAlmostEqual(pf["beta_btc"], w["BTC_IRT"] * 1.0 + w["ETH_IRT"] * 2.0 + w["PAXG_IRT"] * syms["PAXG_IRT"]["beta_btc"],
                               places=1)
        self.assertGreater(pf["sigma7_equity_pct"], 0)
        self.assertGreater(pf["effective_bets"], 1.0)             # gold is a second bet next to BTC/ETH
        self.assertLess(pf["effective_bets"], 3.0)
        self.assertLess(pf["corr_avg_30d"], 0.8)                  # BTC-ETH 1.0, PAXG independent
        self.assertGreater(pf["corr_avg_30d"], 0.0)
        self.assertEqual(pf["to_halt_pct"], 50.0)
        # only ETH has a stop: 10% below its entry (= the current price here)
        self.assertAlmostEqual(pf["loss_if_all_stops_pct"], w["ETH_IRT"] * 10.0, delta=0.15)
        self.assertNotIn("stop", ctx["positions"]["BTC_IRT"])
        # a BTC/ETH-only book is one bet
        one = b.build({"balances": {"BTC": bal["BTC"], "ETH": bal["ETH"]}})["portfolio"]
        self.assertAlmostEqual(one["effective_bets"], 1.0, places=2)
        self.assertAlmostEqual(one["corr_avg_30d"], 1.0, places=2)
        # USDT only: shares 0, no risk numbers, nothing crashes
        cash = b.build({"balances": {"USDT": 1000}})["portfolio"]
        self.assertEqual((cash["coin_share"], cash["rwa_share"]), (0, 0))
        for k in ("beta_btc", "sigma7_equity_pct", "effective_bets", "corr_avg_30d"):
            self.assertNotIn(k, cash)
        self.assertEqual(portfolio_risk({}, {}), {})
        self.assertEqual(portfolio_risk({"X_IRT": 0.5}, {"X_IRT": {0: 0.0, 1: 0.0}}), {})

    def test_trend_state_helper(self):
        ser = [(LAST_CLOSED - k * H, 100.0 * math.exp(0.0001 * (2200 - k))) for k in range(2200, -1, -1)]
        tr = trend_state(ser)
        self.assertTrue(tr["on"])
        self.assertAlmostEqual(tr["vs84d_pct"], (math.exp(0.0001 * 84 * 24) - 1) * 100, places=2)
        self.assertAlmostEqual(tr["vs63d_pct"], (math.exp(0.0001 * 63 * 24) - 1) * 100, places=2)
        self.assertIn("unavailable", trend_state(ser[-1000:]))
        self.assertIn("unavailable", trend_state([]))

    def test_plan_horizons_are_one_year_wide(self):
        """A1: a plan runs 24..720 h (30 days) in v3; a stored plan may be shorter (the endgame cap)."""
        self.assertEqual(PLAN_HORIZON_HOURS, (24, 720))
        self.assertEqual(PLAN_HORIZON_STORED, (1, 720))
        p = clean_plan({"setup": "breakout", "horizon_hours": 900, "invalidation_usdt": 2.3})
        self.assertEqual(p["horizon_hours"], 720)
        self.assertEqual(clean_plan({"setup": "other", "horizon_hours": 3, "invalidation_usdt": 2.3})["horizon_hours"], 3)


# --------------------------------------------------------------------------- bot state sections

from bitpin.analysis import LIQUID_UNIVERSE, average_entry, dd48_usdt  # noqa: E402


class TestBotState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ctxbot_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def builder(self, **cfg):
        c = dict(CFG)
        c.update(cfg)
        return MarketContextBuilder(FakeClient(), c, state_dir=self.dir, bars_source=Bars(), clock=lambda: NOW,
                                    strategies=STRATS)

    def test_no_bot_state_means_no_new_sections(self):
        ctx = self.builder().build({"balances": {"IRT": 1000000}})
        for k in ("features", "ladder", "positions", "focus", "mode"):
            self.assertNotIn(k, ctx)
        self.assertNotIn("endgame", ctx["clock"])
        self.assertNotIn("ladder:", ctx["legend"])

    def test_ladder_positions_endgame_and_focus(self):
        ladder = {"enabled": True, "coins": {
            "BTC": {"scale": 1.0, "dd48_pct": -16.2, "high48_usdt": 95000.0, "close_usdt": 79610.0,
                    "bids": [{"level_pct": -20, "price_usdt": 76000.0, "status": "resting"},
                             {"level_pct": -25, "price_usdt": 71250.0, "status": "resting"}]},
            "ETH": {"scale": 0.5, "armed": False}}}
        pos = {"ETH_IRT": {"entry_ts": NOW - 5 * H, "entry_px_usdt": 2900.0, "stop_pct": 12.0,
                           "target_px_usdt": 3200.0, "max_hold_until": NOW + 163 * H, "source": "ladder",
                           "wake_levels": [3100, 2700]}}
        eg = {"active": True, "no_new_entries": False, "final": False, "no_new_entries_at": NOW + 600 * H,
              "final_at": NOW + 696 * H, "end_at": NOW + 731 * H}
        ctx = self.builder().build({"balances": {"IRT": 1000000}}, mode="veto", ladder=ladder, positions=pos,
                                   endgame=eg, events=[{"kind": "veto", "coin": "BTC", "text": "t"},
                                                       {"kind": "held_move", "symbol": "ETH_IRT"}])
        json.dumps(ctx)
        self.assertEqual(ctx["features"], {"ladder": True, "code_exits": True})
        self.assertEqual(ctx["mode"], "veto")
        self.assertIn("ladder: per coin scale", ctx["legend"])
        self.assertIn("blocked", ctx["legend"])
        btc = ctx["ladder"]["BTC"]
        self.assertEqual((btc["scale"], btc["dd48"], btc["hi48"], btc["px"]), (1.0, -16.2, 95000, 79610))   # the runner's
        self.assertEqual(btc["bids"], [[-20.0, 76000, "resting"], [-25.0, 71250, "resting"]])
        eth = ctx["ladder"]["ETH"]                     # computed from the candles when the runner sent none
        self.assertLessEqual(eth["dd48"], 0.0)
        self.assertEqual([b[0] for b in eth["bids"]], [-20.0, -25.0])
        self.assertEqual({b[2] for b in eth["bids"]}, {"planned"})
        self.assertAlmostEqual(eth["bids"][0][1], eth["hi48"] * 0.8, delta=eth["hi48"] * 1e-4)
        self.assertFalse(eth["armed"])
        p = ctx["positions"]["ETH_IRT"]
        px = ctx["symbols"]["ETH_IRT"]["px_usdt"]
        self.assertEqual((p["src"], p["age_h"], p["entry"], p["stop"], p["target"], p["hold_left_h"]),
                         ("ladder", 5.0, 2900.0, 2552.0, 3200.0, 163.0))
        self.assertAlmostEqual(p["pnl_pct"], (px / 2900.0 - 1) * 100, places=1)
        self.assertEqual(p["wake"], [3100, 2700])
        self.assertEqual(ctx["clock"]["endgame"], {"no_new_entries": False, "final": False, "no_new_entries_in_h": 600.0,
                                                   "final_in_h": 696.0, "end_in_h": 731.0})
        self.assertEqual(sorted(ctx["focus"]), ["BTC_IRT", "ETH_IRT"])
        self.assertEqual((len(ctx["focus"]["BTC_IRT"]["c4h"]), len(ctx["focus"]["BTC_IRT"]["c1d"])), (12, 7))
        self.assertAlmostEqual(ctx["focus"]["BTC_IRT"]["c4h"][-1],
                               close_at("BTC_IRT", LAST_CLOSED) / close_at("USDT_IRT", LAST_CLOSED), delta=1)
        # nothing is held (toman only): the stop of the position costs nothing
        self.assertEqual(ctx["portfolio"]["loss_if_all_stops_pct"], 0)

    def test_recent_code_exits_are_shown_with_their_age(self):
        """Review finding (decision, medium): Kimi was never told that a code exit sold a coin; the
        runner passes Runner._recent_exits_view(now) and the brain blocks buying it back (ago_h)."""
        exits = [{"symbol": "sol_irt", "reason": "stop", "t": NOW - 11 * H, "px_usdt": 132.0,
                  "entry_px_usdt": 150.0, "pnl_pct": -12.0},
                 {"symbol": "BTC_IRT", "reason": "target", "t": NOW - 30 * H, "px_usdt": 72000.0,
                  "entry_px_usdt": 64000.0, "pnl_pct": 12.5},
                 {"symbol": "ETH_IRT", "reason": "stop", "t": "junk"}, "junk", {"reason": "stop", "t": NOW}]
        ctx = self.builder().build({"balances": {"IRT": 1000000}}, ladder={"coins": {}}, positions={},
                                   recent_exits=exits)
        json.dumps(ctx)
        self.assertEqual(ctx["recent_exits"], [
            {"symbol": "BTC_IRT", "reason": "target", "ago_h": 30.0, "px": 72000, "entry": 64000, "pnl_pct": 12.5},
            {"symbol": "SOL_IRT", "reason": "stop", "ago_h": 11.0, "px": 132.0, "entry": 150.0, "pnl_pct": -12.0}])
        self.assertIn("recent_exits: coins the CODE sold on purpose", ctx["legend"])
        self.assertNotIn("recent_exits", self.builder().build(None, recent_exits=[]))
        self.assertNotIn("recent_exits", self.builder().build(None))
        many = [{"symbol": "BTC_IRT", "reason": "stop", "t": NOW - i * H} for i in range(20)]
        self.assertEqual(len(self.builder().build(None, recent_exits=many)["recent_exits"]), 8)

    def test_atr_and_traded_value_are_in_usdt(self):
        """v3.10: a coin flat in USDT while the toman falls 0.1% an hour: its ATR is its own 0.2% range (not the
        toman's drift), its traded value in USDT is flat (vol_ratio 1.0, not inflated by the falling toman)."""
        from bitpin.data import Bar
        t0 = 1790006400 - 900 * 3600
        usdt, coin = [], []
        for i in range(900):
            t = t0 + i * 3600
            rate = 100000.0 * 1.001 ** i
            usdt.append(Bar(t, rate, rate, rate, rate, 50.0))
            px = 50000.0 * rate                                          # 50,000 USDT in toman
            coin.append(Bar(t, px, px * 1.001, px * 0.999, px, 2.0))
        f = an.symbol_features("XYZ_IRT", coin, [b.ts for b in usdt], [b.close for b in usdt])
        self.assertAlmostEqual(f["atr4h_pct"], 0.2, delta=0.02)
        self.assertEqual(f["vol_ratio"], 1.0)
        self.assertAlmostEqual(f["vol24h_k"], 24 * 2.0 * 50000.0 / 1000.0, delta=1.0)  # thousand USDT
        self.assertNotIn("vol24h_m", f)
        self.assertIn("vol24h_k: 24h traded value, thousand USDT", an.LEGEND)
        self.assertIn("atr4h_pct: ATR(14) of the 4h candles in USDT", an.LEGEND)

    def test_features_without_positions_and_a_broken_state_never_break_the_context(self):
        ctx = self.builder().build(None, ladder={"coins": {"BTC": "junk"}}, positions={})
        self.assertEqual(ctx["features"], {"ladder": True, "code_exits": True})
        self.assertNotIn("positions", ctx)
        ctx = self.builder().build(None, ladder={"coins": {"BTC": {"bids": [5, {"level_pct": "x"}]}}},
                                   positions={"BTC_IRT": {"entry_px_usdt": "bad", "entry_ts": None}}, events=[7])
        self.assertIn("symbols", ctx)
        self.assertNotIn("bot_state", ctx.get("data_errors", {}))
        ctx = self.builder().build(None, ladder={"coins": {1: {}, "btc": {"scale": 1}}},
                                   positions={7: {}, "eth_irt": {"entry_px_usdt": 1.0}})
        self.assertEqual(sorted(k for k in ctx["ladder"] if k != "on"), ["BTC"])      # an empty entry is dropped
        self.assertIn("ETH_IRT", ctx["positions"])
        self.assertNotIn("bot_state", ctx.get("data_errors", {}))

    def test_dd48_and_average_entry(self):
        bars = Bars()("BTC_IRT", "60", LAST_CLOSED - 100 * H, LAST_CLOSED)
        u = Bars()("USDT_IRT", "60", LAST_CLOSED - 100 * H, LAST_CLOSED)
        dd, hi, last = dd48_usdt(bars, [b.ts for b in u], [b.close for b in u], "BTC_IRT")
        self.assertAlmostEqual(dd, (math.exp(H_BTC * 47) - 1) * 100, places=2)
        self.assertAlmostEqual(hi, 80000.0 * math.exp(-H_BTC * 47), delta=0.5)
        self.assertIsNone(dd48_usdt(bars[:1], [], [], "BTC_IRT"))
        fills = [{"side": "buy", "base": 1.0, "px_usdt": 100.0, "t": 10}, {"side": "buy", "base": 1.0, "px_usdt": 80.0, "t": 20},
                 {"side": "sell", "base": 0.5, "px_usdt": 120.0, "t": 30}]
        e = average_entry(fills)
        self.assertEqual((round(e["amount"], 9), round(e["avg_px_usdt"], 9), e["entry_ts"]), (1.5, 90.0, 10))
        closed = fills + [{"side": "sell", "base": 5, "px_usdt": 1, "t": 40}]
        self.assertIsNone(average_entry(closed))
        again = closed + [{"side": "buy", "base": 2.0, "quote_usdt": 150.0, "t": 50}]
        self.assertEqual(average_entry(again), {"amount": 2.0, "avg_px_usdt": 75.0, "entry_ts": 50})
        self.assertIsNone(average_entry([{"side": "buy", "base": "x"}, "junk", {"side": "buy", "base": 1}]))

    def test_recent_decisions_name_their_mode(self):
        recent = [{"t": NOW - 3 * H, "targets": {"USDT_IRT": 1.0}, "confidence": 0.3, "mode": "veto"},
                  {"t": NOW - 2 * H, "targets": {"USDT_IRT": 1.0}, "confidence": 0.3, "mode": "evil<script>"},
                  {"t": NOW - H, "targets": {"USDT_IRT": 1.0}, "confidence": 0.3}]
        rd = self.builder().build(None, recent)["recent_decisions"]
        self.assertEqual([r.get("mode") for r in rd], ["veto", None, None])

    def test_rejected_rule_strategies_carry_no_description(self):
        rs = self.builder().build(None)["rule_signals"]["strategies"]
        self.assertIn("desc", rs["hold_usdt"])
        self.assertNotIn("desc", rs["fixed_btc"])

    def test_compact_row_keeps_the_keys_the_brain_relies_on(self):
        feat = {"px": 1, "px_usdt": 2, "ret_usdt": [1.0, 2.0, 3.0], "rsi4h": 50.0, "rsi1d": 40.0, "sig_d": 3.1,
                "d30h": -4.5, "pos30": 0.3, "beta_btc": 1.2, "corr_btc": 0.8, "cls": "us_stock", "us_session": "closed",
                "us_open_in_h": 4.0, "quote_noise_pct": 1.1, "book": {"spread_pct": 0.5, "depth1_m": [1, 2]},
                "blocked": "us market closed", "stale_h": 5.0, "pump_guard": "until x", "ema_dev_pct": [1, 2, 3]}
        self.assertEqual(compact_row(feat, -7.25), {"px": 1, "px_usdt": 2, "r_usdt": [1.0, 2.0, 3.0], "rsi4h": 50.0,
                                                    "dd48": -7.25, "sig_d": 3.1, "d30h": -4.5, "beta": 1.2, "sp": 0.5,
                                                    "d1_m": [1, 2], "cls": "us_stock", "us_session": "closed",
                                                    "blocked": "us market closed", "stale_h": 5.0, "pump_guard": "until x"})
        self.assertEqual(compact_row({"px": 1}), {"px": 1})
        self.assertEqual(compact_row({"unavailable": "x"}), {"unavailable": "x"})

    def test_the_context_stays_compact_for_17_symbols(self):
        syms = list(LIQUID_UNIVERSE)
        base = {s: 1.0 + i * 0.37 for i, s in enumerate(syms)}

        def close(sym, ts):
            k = (ts - LAST_CLOSED) / 3600.0
            u = 230000.0 * math.exp(0.0002 * k)
            return u if sym == "USDT_IRT" else u * 1000 * base[sym] * (1 + 0.08 * math.sin(k / (7 + base[sym])))

        def bars(symbol, res, start, end):
            first = start - (start % 3600) + 1800
            first += 3600 if first < start else 0
            return [Bar(t, close(symbol, t), close(symbol, t) * 1.003, close(symbol, t) * 0.997, close(symbol, t), 3.0)
                    for t in range(int(first), int(min(end, LAST_CLOSED)) + 1, 3600)]

        class Client17(object):
            def tickers(self):
                return [{"symbol": s, "price": str(close(s, LAST_CLOSED))} for s in syms]

            def orderbook(self, s):
                p = close(s, LAST_CLOSED)
                return {"bids": [[str(p * (1 - 0.001 * i)), "5"] for i in range(1, 21)],
                        "asks": [[str(p * (1 + 0.001 * i)), "5"] for i in range(1, 21)]}

        b = MarketContextBuilder(Client17(), {"max_context_chars": 0, "lookback_bars": 1000}, state_dir=None,
                                 bars_source=bars, clock=lambda: NOW)
        recent = [{"t": NOW - 86400 * i, "targets": {"USDT_IRT": 0.7, "BTC_IRT": 0.3}, "confidence": 0.5,
                   "proposed": {"USDT_IRT": 0.6, "BTC_IRT": 0.4}, "adjustments": ["x" * 100], "equity_irt": 3.8e6,
                   "usdt_irt": 229000} for i in range(6, 0, -1)]
        ladder = {"enabled": True, "coins": {c: {"scale": 1.0, "bids": [
            {"level_pct": -20, "price_usdt": 1000.0, "status": "resting"},
            {"level_pct": -25, "price_usdt": 900.0, "status": "resting"}]} for c in ("BTC", "ETH", "XRP", "SOL")}}
        pos = {s: {"entry_ts": NOW - 5 * H, "entry_px_usdt": 1000.0, "stop_pct": 12.0, "target_px_usdt": 1100.0,
                   "max_hold_until": NOW + 100 * H, "source": "ladder"} for s in ("ETH_IRT", "SOL_IRT")}
        eg = {"active": True, "no_new_entries": False, "final": False, "no_new_entries_at": NOW + 600 * H,
              "final_at": NOW + 696 * H, "end_at": NOW + 731 * H}
        ctx = b.build({"balances": {"IRT": 1000, "BTC": 0.0001, "ETH": 0.001}}, recent, mode="veto", ladder=ladder,
                      positions=pos, endgame=eg, events=[{"kind": "veto", "coin": "BTC"}, {"kind": "fill", "coin": "ETH"}])
        self.assertEqual(len(ctx["symbols"]), 17)
        self.assertNotIn("rule_signals", ctx)          # off by default in v3
        # v3.4: + MACD / Bollinger / Donchian / support-resistance of the full-detail coins (was 14000)
        self.assertLess(len(dumps(ctx)), 16000, len(dumps(ctx)))

    def test_the_wide_38_symbol_universe_is_compact_and_short_histories_degrade(self):
        """The owner's wide universe (USDT_IRT + 37 coins): full detail only for USDT_IRT, the ladder coins
        and the held / guarded / targeted / focused coins; one compact row for every other coin; a coin
        with a few days of history simply lacks what it cannot compute (never a crash)."""
        wide = ("USDT_IRT BTC_IRT ETH_IRT XRP_IRT SOL_IRT BNB_IRT DOGE_IRT PAXG_IRT XAUT_IRT SLVON_IRT DASH_IRT "
                "ZEC_IRT SHIB_IRT PEPE_IRT ADA_IRT TRX_IRT SUI_IRT LINK_IRT NEAR_IRT ARB_IRT XLM_IRT HYPE_IRT "
                "AVAX_IRT GRAM_IRT UNI_IRT DOT_IRT LTC_IRT INJ_IRT PUMP_IRT ASTER_IRT OP_IRT WLD_IRT FIL_IRT BCH_IRT "
                "CAKE_IRT SEI_IRT HBAR_IRT CRV_IRT").split()
        base = {s: 1.0 + i * 0.37 for i, s in enumerate(wide)}
        young = {"GRAM_IRT": 5 * 24}                     # listed 5 days ago

        def close(sym, ts):
            k = (ts - LAST_CLOSED) / 3600.0
            u = 230000.0 * math.exp(0.0002 * k)
            return u if sym == "USDT_IRT" else u * 1000 * base[sym] * (1 + 0.08 * math.sin(k / (7 + base[sym])))

        def bars(symbol, res, start, end):
            first = start - (start % 3600) + 1800
            first += 3600 if first < start else 0
            if symbol in young:
                first = max(first, LAST_CLOSED - young[symbol] * 3600)
            return [Bar(t, close(symbol, t), close(symbol, t) * 1.003, close(symbol, t) * 0.997, close(symbol, t), 3.0)
                    for t in range(int(first), int(min(end, LAST_CLOSED)) + 1, 3600)]

        class ClientWide(object):
            def tickers(self):
                return [{"symbol": s, "price": str(close(s, LAST_CLOSED))} for s in wide]

            def orderbook(self, s):
                p = close(s, LAST_CLOSED)
                return {"bids": [[str(p * (1 - 0.001 * i)), "5"] for i in range(1, 21)],
                        "asks": [[str(p * (1 + 0.001 * i)), "5"] for i in range(1, 21)]}

        cfg = {"universe": wide, "max_context_chars": 0, "rule_signals": False}
        b = MarketContextBuilder(ClientWide(), cfg, state_dir=None, bars_source=bars, clock=lambda: NOW)
        self.assertEqual(b.cfg["compact_symbols_above"], 20)
        recent = [{"t": NOW - 86400, "targets": {"USDT_IRT": 0.8, "LINK_IRT": 0.2}, "confidence": 0.5}]
        pos = {"HYPE_IRT": {"entry_ts": NOW - 5 * H, "entry_px_usdt": 1000.0, "stop_pct": 12.0,
                            "target_px_usdt": 1100.0, "max_hold_until": NOW + 100 * H, "source": "kimi"}}
        ctx = b.build({"balances": {"IRT": 1000, "DOGE": 1.0}}, recent, positions=pos,
                      events=[{"kind": "held_move", "symbol": "AVAX_IRT"}])
        syms = ctx["symbols"]
        self.assertEqual(len(syms), 38)
        full = {s for s, f in syms.items() if "ret_usdt" in f or s == "USDT_IRT"}
        self.assertEqual(full, {"USDT_IRT", "BTC_IRT", "ETH_IRT", "XRP_IRT", "SOL_IRT", "DOGE_IRT", "LINK_IRT",
                                "HYPE_IRT", "AVAX_IRT"})
        row = syms["CRV_IRT"]
        self.assertEqual(set(row), {"px", "px_usdt", "r_usdt", "rsi4h", "dd48", "sig_d", "d30h", "beta", "sp", "d1_m"})
        self.assertEqual(len(row["r_usdt"]), 3)
        self.assertEqual(syms["XAUT_IRT"]["cls"], "gold")
        self.assertEqual(syms["SLVON_IRT"]["cls"], "silver")
        self.assertIn("Compact rows", ctx["legend"])
        self.assertIn("us_session: open / closed", ctx["legend"])      # SLVON (an Ondo fund token) is session-bound
        self.assertTrue(set(syms["GRAM_IRT"]) <= {"px", "px_usdt", "r_usdt", "rsi4h", "dd48", "sig_d", "d30h", "beta",
                                                   "sp", "d1_m"})
        self.assertIn("px", syms["GRAM_IRT"])
        self.assertLess(len(dumps(ctx)), 16000, len(dumps(ctx)))    # v3.4: + technical levels (was 14000)
        # a small universe (or compact_symbols_above 0) keeps full detail everywhere
        b0 = MarketContextBuilder(ClientWide(), dict(cfg, compact_symbols_above=0), state_dir=None, bars_source=bars,
                                  clock=lambda: NOW)
        ctx0 = b0.build({"balances": {"IRT": 1000}}, [])
        self.assertTrue(all("ret_usdt" in f for s, f in ctx0["symbols"].items() if s != "USDT_IRT" and
                            "unavailable" not in f))
        self.assertNotIn("Compact rows", ctx0["legend"])

    def test_the_53_symbol_universe_stays_under_22k_chars(self):
        """C1: the owner's one-year universe (38 coins + 15 tokenized gold / oil / gas / copper / stock /
        ETF / bond markets) with every bot section stays under 22k characters, compact rows carry the
        class and the US session, and the closed US market blocks the stock tokens."""
        coins = ("USDT_IRT BTC_IRT ETH_IRT XRP_IRT SOL_IRT BNB_IRT DOGE_IRT PAXG_IRT XAUT_IRT SLVON_IRT DASH_IRT "
                 "ZEC_IRT SHIB_IRT PEPE_IRT ADA_IRT TRX_IRT SUI_IRT LINK_IRT NEAR_IRT ARB_IRT XLM_IRT HYPE_IRT "
                 "AVAX_IRT GRAM_IRT UNI_IRT DOT_IRT LTC_IRT INJ_IRT PUMP_IRT ASTER_IRT OP_IRT WLD_IRT FIL_IRT BCH_IRT "
                 "CAKE_IRT SEI_IRT HBAR_IRT CRV_IRT").split()
        rwa = ("USOON_IRT UNGON_IRT COPXON_IRT COINX_IRT CRCLX_IRT HOODX_IRT MSTRON_IRT GOOGLX_IRT NVDAX_IRT AAPLX_IRT "
               "SPYON_IRT QQQON_IRT TLTON_IRT GLDON_IRT AMZNX_IRT").split()
        syms = coins + rwa
        self.assertEqual(len(syms), 53)
        base = {s: 1.0 + i * 0.37 for i, s in enumerate(syms)}

        def usd(sym, k):
            return 1000 * base[sym] * (1 + 0.08 * math.sin(k / (7 + base[sym])) + 0.01 * math.sin(k / 2.3))
        m = SynthMarket(syms, usd, spreads={"AMZNX_IRT": 0.02}, young={"UNGON_IRT": 12 * 24, "GRAM_IRT": 5 * 24})
        b = m.builder(rule_signals=False)
        recent = [{"t": NOW - 86400 * i, "targets": {"USDT_IRT": 0.5, "BTC_IRT": 0.2, "XAUT_IRT": 0.3}, "confidence": 0.5,
                   "proposed": {"USDT_IRT": 0.4, "BTC_IRT": 0.3, "XAUT_IRT": 0.3}, "adjustments": ["x" * 100],
                   "equity_irt": 3.8e6, "usdt_irt": 229000} for i in range(6, 0, -1)]
        ladder = {"enabled": True, "coins": {c: {"scale": 1.0, "bids": [
            {"level_pct": -20, "price_usdt": 1000.0, "status": "resting"}]} for c in ("BTC", "ETH", "XRP", "SOL")}}
        pos = {s: {"entry_ts": NOW - 50 * H, "entry_px_usdt": 1000.0, "stop_pct": 12.0, "max_hold_until": NOW + 600 * H,
                   "source": "kimi", "plan": {"setup": "dip_in_uptrend", "horizon_hours": 720, "invalidation_usdt": 900.0,
                                              "take_profit_usdt": 1200.0, "note": "dip in an uptrend", "px_usdt": 1000.0,
                                              "set_at": NOW - 50 * H}} for s in ("ETH_IRT", "SOL_IRT", "XAUT_IRT", "GOOGLX_IRT")}
        eg = {"active": True, "no_new_entries": False, "final": False, "no_new_entries_at": NOW + 8000 * H,
              "final_at": NOW + 8100 * H, "end_at": NOW + 8130 * H}
        exits = [{"symbol": "ADA_IRT", "reason": "stop", "t": NOW - 30 * H, "px_usdt": 0.5, "entry_px_usdt": 0.6,
                  "pnl_pct": -16.0}]
        bal = {"IRT": 1000, "BTC": 0.0002 / base["BTC_IRT"], "ETH": 0.001 / base["ETH_IRT"], "XAUT": 0.001 / base["XAUT_IRT"],
               "GOOGLX": 0.0005 / base["GOOGLX_IRT"], "USDT": 500}
        ctx = b.build(dict(balances=bal, halt_drawdown_pct=50, fees_irt=20000, slippage_irt=5000, turnover_7d_irt=400000),
                      recent, mode="veto", ladder=ladder, positions=pos, endgame=eg, recent_exits=exits,
                      events=[{"kind": "veto", "coin": "BTC"}, {"kind": "held_move", "symbol": "SOL_IRT"}])
        json.dumps(ctx)
        self.assertEqual(len(ctx["symbols"]), 53)
        n = len(dumps(ctx))
        self.assertLess(n, 24000, n)                # v3.4: + the technical levels (was 22000)
        syms_out = ctx["symbols"]
        full = {s for s, f in syms_out.items() if "ret_usdt" in f or s == "USDT_IRT"}
        self.assertEqual(full, {"USDT_IRT", "BTC_IRT", "ETH_IRT", "XRP_IRT", "SOL_IRT", "XAUT_IRT", "GOOGLX_IRT"})
        row = syms_out["NVDAX_IRT"]                    # a compact session-bound row
        self.assertEqual((row["cls"], row["us_session"]), ("us_stock", "closed"))
        self.assertIn("us market closed", row["blocked"])
        self.assertTrue(set(row) <= {"px", "px_usdt", "r_usdt", "rsi4h", "dd48", "sig_d", "d30h", "beta", "sp", "d1_m",
                                     "cls", "us_session", "blocked"})
        self.assertEqual(syms_out["GLDON_IRT"]["cls"], "gold")
        self.assertIn("us market closed", syms_out["GLDON_IRT"]["blocked"])   # an Ondo fund token: session-bound
        self.assertNotIn("blocked", syms_out["XAUT_IRT"])                      # 24/7 gold
        self.assertEqual(syms_out["MSTRON_IRT"]["cls"], "crypto_beta")
        self.assertNotIn("cls", syms_out["CRV_IRT"])
        self.assertIn("px", syms_out["UNGON_IRT"])         # 12 days of history: a row, fewer fields
        g = syms_out["GOOGLX_IRT"]                          # a held session-bound coin in full detail
        self.assertEqual((g["cls"], g["us_session"], g["us_open_in_h"]), ("us_stock", "closed", 4.0))
        self.assertIn("quote_noise_pct", g)
        pf = ctx["portfolio"]
        self.assertGreater(pf["rwa_share"], 0)
        self.assertGreater(pf["coin_share"], 0)
        self.assertEqual(pf["to_halt_pct"], 50.0)
        self.assertIn("costs_since_start", pf)
        for k in ("sigma7_equity_pct", "effective_bets", "loss_if_all_stops_pct"):
            self.assertIn(k, pf)
        self.assertIn("btc_trend84", ctx["macro"])
        for s in ("us_session: open / closed", "portfolio: equity_irt", "btc_trend84", "Compact rows", "your_plan",
                  "recent_exits"):
            self.assertIn(s, ctx["legend"])
        self.assertIn("recent_exits", ctx)


# --------------------------------------------------------------------------- v3.4 technical data

def zigzag(points, seg=10, t0=LAST_CLOSED - 3000 * 3600):
    """4h bars (open = high = low = close, like the bot's USDT-terms bars made of hourly closes) walking in
    straight lines through `points`, `seg` bars per leg."""
    closes = []
    for a, b in zip(points, points[1:]):
        closes += [a + (b - a) * i / float(seg) for i in range(seg)]
    closes.append(points[-1])
    return [Bar(t0 + i * 4 * 3600, c, c, c, c, 0.0) for i, c in enumerate(closes)]


class TestTechnicalLevels(unittest.TestCase):
    """v3.4: MACD, Bollinger bands, the 20-bar Donchian channel and support / resistance levels (the owner asked
    for more technical data for Kimi; the prompt already told it to set invalidations at the Donchian low)."""

    def test_macd_follows_its_definition(self):
        from bitpin import indicators as ind
        line, sig, hist = ind.macd(list(range(60)))
        self.assertIsNone(line[24])
        self.assertAlmostEqual(line[25], 7.0)                   # EMA12 - EMA26 of a ramp: (25 - 11) / 2
        self.assertIsNone(sig[32])
        self.assertAlmostEqual(sig[-1], 7.0)
        self.assertAlmostEqual(hist[-1], 0.0)
        line, sig, hist = ind.macd([5.0] * 60)
        self.assertEqual((line[-1], sig[-1], hist[-1]), (0.0, 0.0, 0.0))
        self.assertEqual(an.macd_pct([float(i) for i in range(60)]), [round(700 / 59.0, 2), round(700 / 59.0, 2), 0.0])
        self.assertIsNone(an.macd_pct([1.0] * 34))

    def test_bollinger_place_and_width(self):
        self.assertEqual(an.bollinger_pos_width([5.0] * 30), [0.5, 0.0])
        pos, width = an.bollinger_pos_width([100.0 + (i % 2) for i in range(30)] + [104.0])
        self.assertGreater(pos, 1.0)                            # a close above the upper band
        self.assertGreater(width, 0)
        self.assertIsNone(an.bollinger_pos_width([1.0] * 19))

    def test_support_and_resistance_of_a_zigzag(self):
        bars = zigzag([100, 110, 95, 110, 90, 105, 95, 100])
        sup, res = an.support_resistance(bars, 100.0)
        self.assertEqual(sup, [(95.0, 2), (90.0, 2)])            # nearest first, with the swing points they hold
        self.assertEqual(res, [(105.0, 1), (110.0, 3)])          # 110: two swing highs + the highest high
        sup, res = an.support_resistance(bars, 100.0, levels=1)
        self.assertEqual((sup, res), ([(95.0, 2)], [(105.0, 1)]))

    def test_a_broken_resistance_turns_into_support(self):
        bars = zigzag([100, 104, 98, 112, 110])
        sup, res = an.support_resistance(bars, 110.0)
        self.assertIn(104.0, [m for m, _ in sup])
        self.assertEqual([m for m, _ in res], [112.0])

    def test_no_levels_from_too_little_or_odd_data(self):
        self.assertEqual(an.support_resistance([], 100.0), ([], []))
        self.assertEqual(an.support_resistance(zigzag([100, 110]), 100.0), ([], []))
        self.assertEqual(an.support_resistance(zigzag([100, 110, 95, 110]), 0), ([], []))
        self.assertEqual(an.support_resistance(zigzag([100, 110, 95, 110]), "x"), ([], []))
        bad = zigzag([100, 110, 95, 110, 90, 105])
        bad[5] = Bar(bad[5].ts, float("nan"), float("nan"), float("nan"), float("nan"), 0.0)
        an.support_resistance(bad, 100.0)                       # never raises

    def test_symbol_features_carry_the_new_fields_in_usdt_terms(self):
        usdt = [Bar(LAST_CLOSED - (999 - i) * 3600, 200000.0, 200000.0, 200000.0, 200000.0, 1.0) for i in range(1000)]
        path = zigzag([100, 110, 95, 110, 90, 105, 95, 100], seg=24)      # 169 4h closes -> hourly bars
        hourly = []
        for i in range(1000):
            c = path[min(len(path) - 1, i // 6)].close * 200000.0
            hourly.append(Bar(usdt[i].ts, c, c, c, c, 1.0))
        f = an.symbol_features("ETH_IRT", hourly, [b.ts for b in usdt], [b.close for b in usdt])
        px = f["px_usdt"]
        self.assertEqual(len(f["macd4h_pct"]), 3)
        self.assertEqual(len(f["bb4h"]), 2)
        lo, hi = f["don20_4h"]
        self.assertLessEqual(lo, hi)
        self.assertTrue(f["sup"] and all(s < px for s in f["sup"]), f)
        self.assertTrue(f["res"] and all(r > px for r in f["res"]), f)
        self.assertEqual(f["sup"], sorted(f["sup"], reverse=True))
        self.assertEqual(len(f["sup_n"]), len(f["sup"]))
        self.assertEqual(len(f["res_n"]), len(f["res"]))
        row = an.compact_row(f)
        for k in ("macd4h_pct", "bb4h", "don20_4h", "sup", "res", "sup_n", "res_n"):
            self.assertNotIn(k, row)                            # compact rows keep their size
        self.assertIn("don20_4h", an.LEGEND)
        self.assertIn("sup / res", an.LEGEND)


if __name__ == "__main__":
    unittest.main()
