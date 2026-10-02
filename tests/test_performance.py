"""bitpin.performance (v3.6): the P&L report of the panel - totals in toman and in USDT, the rows per asset that
add up, fees, the trade history, the chart series, the open positions with their orders, and collect() reading
the state files with hourly and 4-hour candles. A synthetic world with known prices: USDT gets 0.1% dearer every
hour (the rial falls), BTC gains 0.05% per hour in USDT. No network."""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bitpin import performance as perf  # noqa: E402
from bitpin.data import Bar  # noqa: E402

H = 3600
T0 = 1789999200                          # an hour boundary
START = 10000000.0                       # toman


def usdt_at(t):
    return 100000.0 * (1 + 0.001 * (t - T0) / H)


def btc_usdt_at(t):
    return 50000.0 * (1 + 0.0005 * (t - T0) / H)


def btc_at(t):
    return btc_usdt_at(t) * usdt_at(t)


PRICE = {"USDT": usdt_at, "BTC": btc_at}


def hour(t, step=H):
    """The last close at or before t (the candles close on the hour)."""
    return t - t % step


def make_bars(fn, start, end, step=H, now=None):
    """Candles of fn from start to end; the running one (not closed at `now`) closes at fn(now)."""
    out, t = [], start - start % step
    while t <= end:
        close_t = t + step if now is None else min(t + step, now)
        out.append(Bar(int(t), fn(t), fn(t), fn(t), fn(close_t), 1.0))
        t += step
    return out


def cycle(k):
    return T0 + k * H + 1800             # the bot's hourly cycle, half past


def fill(k, symbol, side, base, quote, fee, fee_asset):
    return {"symbol": symbol, "side": side, "base": str(base), "quote": str(quote), "fee": str(fee),
            "fee_asset": fee_asset, "quote_asset": symbol.split("_")[1], "reason": "allocation", "route": "direct",
            "order_id": "o%d" % k}


def world_fills():
    """k -> the fills booked at cycle k."""
    c1, c2, c5, c10 = cycle(1), cycle(2), cycle(5), cycle(10)
    sell_quote = 0.0001 * btc_at(c10)
    return {1: [fill(1, "USDT_IRT", "buy", 20, 20 * usdt_at(c1), 0.02, "USDT")],
            2: [fill(2, "BTC_USDT", "buy", 0.0002, 0.0002 * btc_usdt_at(c2), 0.0000002, "BTC")],
            5: [fill(5, "BTC_IRT", "buy", 0.0001, 0.0001 * btc_at(c5), 0.0000001, "BTC")],
            10: [fill(10, "BTC_IRT", "sell", 0.0001, sell_quote, sell_quote * 0.0035, "IRT")]}


def value_at(fills, t):
    """The account value at cycle time t, priced like the report prices it (the last hourly close)."""
    q = perf.holdings_at(fills, t, START)
    return sum(n * (1.0 if a == "IRT" else PRICE[a](hour(t))) for a, n in q.items())


def world_records(n=30, skew=1.0):
    """n hourly records; the recorded equity is the rebuilt value (times skew: a trade the bot did not make)."""
    by_k = world_fills()
    records, booked = [], []
    for k in range(n):
        booked_now = by_k.get(k, [])
        rec = {"time": cycle(k), "fills": booked_now, "positions": {}}
        records.append(rec)
        booked = perf.parse_fills(records)
        rec["equity_irt"] = value_at(booked, cycle(k)) * skew
    records[-1]["positions"] = {"BTC_USDT": {
        "amount": "0.0001998", "entry_px_usdt": str(btc_usdt_at(cycle(2))), "stop_px_usdt": "45000",
        "target_px_usdt": "60000", "max_hold_until": str(cycle(2) + 7 * 86400),
        "plan": {"setup": "trend_continuation", "note": "A test plan <b>", "set_at": str(cycle(2)),
                 "invalidation_usdt": "47000", "take_profit_usdt": "60000", "px_usdt": str(btc_usdt_at(cycle(2))),
                 "horizon_hours": 168}}}
    return records


def world_analyses(at=None):
    """read_analyses() of a decision at cycle 20 that re-tested the BTC plan."""
    return {"BTC": {"symbol": "BTC_IRT", "time": at or cycle(20), "pass": True, "setup": "trend_continuation",
                    "row": "B10", "verdict": "hold", "p0": 0.3, "p": 0.35, "gain_pct": 20.0, "loss_pct": 6.0,
                    "cost_pct": 0.5, "ev_pct": 2.6,
                    "evidence": "ema_dev_pct=[0.5,0.9,5.0] all>0, ret_usdt[2]=4.2, macro.btc_trend84 on",
                    "bear": "B7 trend_continuation net -1.6% 7d in HOLDOUT; beta_btc=1.3"}}


def world_prices(now, start=T0 - 48 * H):
    bars = {a: make_bars(fn, start, now, now=now) for a, fn in PRICE.items()}
    return perf.prices_from_bars(bars, now)


ORDERS_DOC = {"orders": {
    "a": {"status": "resting", "kind": "limit", "symbol": "BTC_USDT", "side": "sell", "price": "60000",
          "base_amount": "0.0001998", "tag": "target"},
    "b": {"status": "closed", "kind": "limit", "symbol": "BTC_USDT", "side": "buy", "price": "40000",
          "base_amount": "0.0002", "tag": "ladder"},
    "c": {"status": "resting", "kind": "limit", "symbol": "BTC_IRT", "side": "buy", "price": "4500000000",
          "base_amount": "0.0001", "tag": "ladder"},
    "d": {"status": "resting", "kind": "market", "symbol": "BTC_IRT", "side": "buy", "price": "1"},
    "e": {"status": "resting", "kind": "limit", "symbol": "ETH_USDT", "side": "buy", "price": "2000",
          "base_amount": "0.01", "tag": "ladder"}}}


class TestTotals(unittest.TestCase):
    def setUp(self):
        self.records = world_records()
        self.now = cycle(29) + 600
        self.prices = world_prices(self.now)

    def report(self, t_from, t_to, orders=None, records=None):
        return perf.report(records or self.records, START, self.prices, t_from, t_to, self.now, orders)

    def test_toman_and_usdt_totals_use_the_rate_of_each_value(self):
        t_to = cycle(19) + 900
        r = self.report(T0, t_to)
        t = r["totals"]
        v_to = self.records[19]["equity_irt"]
        self.assertEqual((t["value_from_irt"], t["value_to_irt"], t["value_to_time"]), (START, v_to, cycle(19)))
        self.assertAlmostEqual(t["pnl_irt"], v_to - START, places=4)
        self.assertAlmostEqual(t["pnl_irt_pct"], (v_to / START - 1) * 100, places=9)
        u0, u1 = usdt_at(T0), usdt_at(hour(cycle(19)))
        self.assertAlmostEqual(t["pnl_usdt_pct"], ((v_to / u1) / (START / u0) - 1) * 100, places=9)
        self.assertAlmostEqual(t["rial_fall_pct"], (u1 / u0 - 1) * 100, places=9)
        self.assertAlmostEqual(t["vs_usdt_points"], t["pnl_irt_pct"] - t["rial_fall_pct"], places=9)
        self.assertFalse(r["live"])
        self.assertNotIn("value_now_irt", t)
        self.assertEqual(r["positions"], [])                     # a range in the past: no open positions

    def test_trades_and_fees_of_the_range(self):
        t = self.report(T0, cycle(19))["totals"]
        self.assertEqual((t["trades"], t["buys"], t["sells"]), (4, 3, 1))
        # a fee in the asset bought is worth the trade's own price; one in toman is itself
        fees = (0.02 * usdt_at(cycle(1))                                           # USDT, bought with toman
                + 0.0000002 * btc_usdt_at(cycle(2)) * usdt_at(hour(cycle(2)))       # BTC, bought with USDT
                + 0.0000001 * btc_at(cycle(5))                                     # BTC, bought with toman
                + float(world_fills()[10][0]["fee"]))                              # toman
        self.assertAlmostEqual(t["fees_irt"], fees, places=4)
        t2 = self.report(cycle(3), cycle(19))["totals"]                            # (from, to]: two trades
        self.assertEqual((t2["trades"], t2["buys"], t2["sells"]), (2, 1, 1))

    def test_the_rows_add_up_to_the_rebuilt_account(self):
        for t_from, t_to in ((T0, cycle(19) + 900), (cycle(3) + 60, cycle(25)), (cycle(1), cycle(12))):
            r = self.report(t_from, t_to)
            q0 = perf.holdings_at(perf.parse_fills(self.records), t_from, START)
            v0 = sum(n * (1.0 if a == "IRT" else PRICE[a](hour(t_from))) for a, n in q0.items())
            u0, u1 = usdt_at(hour(t_from)), usdt_at(hour(t_to))
            self.assertAlmostEqual(sum(a["pnl_irt"] for a in r["assets"]), r["rebuilt_value_to_irt"] - v0, places=3)
            self.assertAlmostEqual(sum(a["pnl_usdt"] for a in r["assets"]), r["rebuilt_value_to_irt"] / u1 - v0 / u0,
                                   places=6)
            self.assertEqual(r["warnings"], [])

    def test_usdt_gains_only_from_the_rial_and_cash_loses_in_usdt(self):
        rows = dict((a["asset"], a) for a in self.report(T0, cycle(28))["assets"])
        self.assertEqual(sorted(rows), ["BTC", "IRT", "USDT"])
        self.assertGreater(rows["USDT"]["pnl_irt"], 0)                 # the rial fell
        self.assertLess(abs(rows["USDT"]["pnl_usdt"]), 0.1)           # in USDT: only fees and rounding
        self.assertAlmostEqual(rows["IRT"]["pnl_irt"], 0.0, places=4)  # toman is the unit of the toman column
        self.assertLess(rows["IRT"]["pnl_usdt"], 0)                   # ... and lost value against USDT
        self.assertIsNone(rows["IRT"]["pnl_irt_pct"])
        self.assertGreater(rows["BTC"]["pnl_usdt"], 0)                # BTC rose in USDT
        self.assertEqual((rows["BTC"]["trades"], rows["USDT"]["trades"]), (3, 1))
        self.assertAlmostEqual(rows["BTC"]["qty_to"], 0.0002 - 0.0000002 + 0.0001 - 0.0000001 - 0.0001, places=12)

    def test_a_sold_out_asset_keeps_its_row_in_the_range_it_was_traded(self):
        recs = world_records()
        recs[12]["fills"] = [fill(12, "BTC_IRT", "sell", 0.0001997, 0.0001997 * btc_at(cycle(12)), 0, "IRT")]
        r = self.report(cycle(11), cycle(20), records=recs)
        btc = [a for a in r["assets"] if a["asset"] == "BTC"][0]
        self.assertAlmostEqual(btc["qty_to"], 0.0, places=12)
        self.assertEqual(btc["trades"], 1)
        later = self.report(cycle(13), cycle(20), records=recs)
        self.assertNotIn("BTC", [a["asset"] for a in later["assets"]])   # nothing held, nothing traded

    def test_a_trade_the_bot_did_not_make_is_a_warning(self):
        r = self.report(T0, cycle(20), records=world_records(skew=1.05))
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("differs from the recorded value", r["warnings"][0])

    def test_history_is_newest_first_with_values(self):
        h = self.report(T0, cycle(29))["history"]
        self.assertEqual([x["order_id"] for x in h], ["o10", "o5", "o2", "o1"])
        buy_btc_usdt = h[2]
        self.assertAlmostEqual(buy_btc_usdt["price"], btc_usdt_at(cycle(2)), places=6)
        self.assertAlmostEqual(buy_btc_usdt["value_irt"], buy_btc_usdt["quote"] * usdt_at(hour(cycle(2))), places=4)
        self.assertAlmostEqual(buy_btc_usdt["value_usdt"], buy_btc_usdt["quote"], places=9)
        self.assertEqual(self.report(T0, cycle(29))["history_total"], 4)


class TestChartAndPositions(unittest.TestCase):
    def setUp(self):
        self.records = world_records()
        self.now = cycle(29) + 600
        self.prices = world_prices(self.now)

    def test_the_chart_starts_at_the_range_start_and_ends_with_the_estimate_for_now(self):
        r = perf.report(self.records, START, self.prices, T0, self.now, self.now, perf.resting_orders(ORDERS_DOC))
        eq = r["equity"]
        self.assertEqual(eq[0][:2], [T0, START])
        self.assertAlmostEqual(eq[0][2], START / usdt_at(T0), places=9)
        self.assertAlmostEqual(eq[0][3], START, places=6)                      # holding USDT: the same at the start
        self.assertEqual([p[0] for p in eq[1:-1]], [cycle(k) for k in range(30)])
        self.assertEqual(eq[-1][0], self.now)                                  # the estimate
        t = r["totals"]
        self.assertTrue(r["live"])
        self.assertEqual(eq[-1][1], t["value_now_irt"])
        self.assertAlmostEqual(eq[-1][3], START * usdt_at(self.now) / usdt_at(T0), places=3)
        # the last record moved by the latest prices (the recorded value was priced at the hour's close)
        q = perf.holdings_at(perf.parse_fills(self.records), self.now, START)
        moved = sum(n * (1.0 if a == "IRT" else PRICE[a](self.now)) for a, n in q.items())
        at_record = sum(n * (1.0 if a == "IRT" else PRICE[a](hour(cycle(29)))) for a, n in q.items())
        self.assertAlmostEqual(t["value_now_irt"], self.records[-1]["equity_irt"] * moved / at_record, places=3)

    def test_a_long_series_is_thinned(self):
        pts = [(T0 + i, float(i)) for i in range(1000)]
        thin = perf._thin(pts)
        self.assertEqual(len(thin), perf.CHART_POINTS)
        self.assertEqual((thin[0], thin[-1]), (pts[0], pts[-1]))

    def test_positions_with_their_plan_orders_and_prices(self):
        r = perf.report(self.records, START, self.prices, T0, self.now, self.now, perf.resting_orders(ORDERS_DOC))
        pos = dict((p["asset"], p) for p in r["positions"])
        self.assertEqual(sorted(pos), ["BTC", "ETH"])                         # held, or only an order
        btc = pos["BTC"]
        self.assertAlmostEqual(btc["entry_usdt"], btc_usdt_at(cycle(2)), places=6)
        self.assertEqual((btc["stop_usdt"], btc["target_usdt"], btc["setup"]), (45000.0, 60000.0, "trend_continuation"))
        self.assertEqual(btc["set_at"], float(cycle(2)))
        self.assertAlmostEqual(btc["price_usdt"], btc_usdt_at(self.now), places=6)
        self.assertAlmostEqual(btc["change_pct"], (btc_usdt_at(self.now) / btc_usdt_at(cycle(2)) - 1) * 100, places=6)
        # the resting limit orders only (not the closed one, not the market one), the highest first; a toman
        # price is shown in USDT at the latest rate
        self.assertEqual([(o["side"], o["tag"]) for o in btc["orders"]], [("sell", "target"), ("buy", "ladder")])
        self.assertAlmostEqual(btc["orders"][0]["price_usdt"], 60000.0)
        self.assertAlmostEqual(btc["orders"][1]["price_usdt"], 4500000000.0 / usdt_at(self.now), places=4)
        # a week of hourly prices in USDT, then the price now
        line = btc["prices"]
        self.assertEqual(line[-1][0], self.now)
        self.assertGreaterEqual(line[0][0], self.now - perf.POSITION_CHART_HOURS * H - H)
        self.assertAlmostEqual(line[-2][1], btc_usdt_at(line[-2][0]), places=6)
        self.assertEqual(pos["ETH"]["prices"], [])                            # no ETH candles in this world
        self.assertEqual(pos["ETH"]["orders"][0]["price_usdt"], 2000.0)


def decision_line(t, cands, valid=True, fallback=False, reply=None):
    """One kimi_decisions.jsonl record: the parsed decision (cleaned texts) and Kimi's reply as written."""
    return json.dumps({"time": t, "decision": {"valid": valid, "fallback": fallback, "decided_at": t,
                                               "analysis": {"candidates": cands}},
                       "response": json.dumps({"analysis": {"candidates": reply}}) if reply is not None else "<junk"})


class TestOutlook(unittest.TestCase):
    """v3.6.1: Kimi's newest analysis of each coin and the outlook drawn from it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_outlook_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, perf.DECISIONS_FILE)

    def test_the_newest_valid_analysis_of_each_coin(self):
        btc_old = {"setup": "breakout", "row": "B7", "p": 0.25, "evidence": "old"}
        eth = {"setup": "trend_continuation", "row": "b10", "p": "0.4", "p0": 0.3, "gain_pct": 15, "loss_pct": 5,
               "cost_pct": 0.5, "ev_pct": 2.5, "pass": True, "verdict": "hold", "evidence": "ema dev pct 1 all 0",
               "bear": "x" * 900}
        btc = {"setup": "trend_continuation", "row": "B10", "p": 0.3, "evidence": "ema dev pct 0.5 all 0",
               "bear": "close 70000 breaks it"}
        lines = [decision_line(T0, {"BTC_IRT": btc_old, "ETH_IRT": eth}),
                 decision_line(T0 + H, {"BTC_IRT": btc}, reply={"BTC_IRT": dict(btc, evidence="ema_dev_pct=[0.5] all>0",
                                                                                bear="close <70000  breaks it")}),
                 decision_line(T0 + 2 * H, {"BTC_IRT": dict(btc, p=0.9)}, valid=False),     # an invalid decision
                 decision_line(T0 + 3 * H, {"BTC_IRT": dict(btc, p=0.8)}, fallback=True),  # the bot's fallback
                 "{broken", json.dumps(["not", "a", "record"])]
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        a = perf.read_analyses(self.path)
        self.assertEqual(sorted(a), ["BTC", "ETH"])
        self.assertEqual((a["BTC"]["p"], a["BTC"]["time"], a["BTC"]["symbol"]), (0.3, T0 + H, "BTC_IRT"))
        self.assertEqual(a["BTC"]["evidence"], "ema_dev_pct=[0.5] all>0")        # Kimi's own words
        self.assertEqual(a["BTC"]["bear"], "close <70000 breaks it")             # one line
        self.assertEqual((a["ETH"]["p"], a["ETH"]["row"], a["ETH"]["pass"]), (0.4, "b10", True))
        self.assertEqual(a["ETH"]["evidence"], "ema dev pct 1 all 0")           # no readable reply: the cleaned text
        self.assertEqual(len(a["ETH"]["bear"]), perf.ANALYSIS_TEXT_MAX)
        self.assertEqual(perf.read_analyses(os.path.join(self.dir, "missing.jsonl")), {})

    def test_the_tail_of_a_big_file_is_enough(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("x" * 5000 + "\n" + decision_line(T0, {"SOL_IRT": {"p": 0.5}}) + "\n")
        self.assertEqual(perf.read_analyses(self.path, limit=2000)["SOL"]["p"], 0.5)

    def test_hourly_volatility(self):
        up, down = 1.01, 1 / 1.01
        line, px = [], 100.0
        for i in range(49):
            line.append([T0 + i * H, px])
            px *= up if i % 2 == 0 else down
        sig = perf.hourly_sigma(line)
        self.assertAlmostEqual(sig, math.log(1.01) * math.sqrt(48 / 47.0), places=9)
        four_hourly = [[T0 + i * 4 * H, p] for i, (_t, p) in enumerate(line)]
        self.assertAlmostEqual(perf.hourly_sigma(four_hourly), sig / 2, places=9)   # sqrt(4) per step
        self.assertIsNone(perf.hourly_sigma(line[:10]))
        self.assertIsNone(perf.hourly_sigma([[T0 + i * 20 * H, 1.0 + i] for i in range(40)]))  # gaps are skipped

    def test_the_range_the_scenarios_and_their_weighted_price(self):
        o = perf.outlook(100.0, T0, T0 + 16 * H, 0.01, 120.0, 90.0, 0.4, [T0 - 5 * H, 98.0])
        self.assertEqual(len(o["cone"]), perf.CONE_STEPS + 1)
        self.assertEqual(o["cone"][0], [T0, 100.0, 100.0, 100.0, 100.0])
        t, lo1, hi1, lo2, hi2 = o["cone"][-1]
        self.assertEqual(t, T0 + 16 * H)
        for got, want in ((lo1, 100 * math.exp(-0.04)), (hi1, 100 * math.exp(0.04)), (lo2, 100 * math.exp(-0.08)),
                          (hi2, 100 * math.exp(0.08))):
            self.assertAlmostEqual(got, want, places=9)
        self.assertAlmostEqual(o["expected"], 0.4 * 120 + 0.6 * 90, places=9)
        self.assertEqual(o["origin"], [T0 - 5 * H, 98.0])
        self.assertIsNone(perf.outlook(100.0, T0, T0 + H, 0.01, 120.0, 90.0, None)["expected"])
        self.assertIsNone(perf.outlook(100.0, T0, T0 + H, 0.01, 90.0, 120.0, 0.5)["expected"])     # levels swapped
        self.assertEqual(perf.outlook(100.0, T0, T0 + H, None)["cone"], [])
        self.assertEqual(perf.outlook(None, T0, T0 + H, 0.01)["cone"], [])

    def test_the_outlook_ends_with_the_plan(self):
        now = T0
        self.assertEqual(perf.outlook_end({"max_hold_until": now + 50 * H}, now), now + 50 * H)
        for hold in (None, now - H, now + 0.5 * H, now + 40 * 86400, "x"):
            self.assertEqual(perf.outlook_end({"max_hold_until": hold}, now), now + perf.FORECAST_DEFAULT_HOURS * H)

    def test_positions_carry_kimis_analysis_and_the_outlook(self):
        now = cycle(29) + 600
        records, prices = world_records(), world_prices(now)
        r = perf.report(records, START, prices, T0, now, now, [], world_analyses())
        btc = [p for p in r["positions"] if p["asset"] == "BTC"][0]
        self.assertEqual((btc["invalidation_usdt"], btc["target_usdt"], btc["horizon_hours"]), (47000.0, 60000.0, 168.0))
        self.assertEqual(btc["analysis"]["row"], "B10")
        o = btc["outlook"]
        self.assertEqual(o["to"], float(cycle(2) + 7 * 86400))                   # the plan's max hold
        self.assertEqual(o["origin"][0], cycle(20))                              # Kimi's decision ...
        self.assertAlmostEqual(o["origin"][1], 60000 / 1.2, places=6)          # ... at its price: take profit / (1 + g)
        self.assertAlmostEqual(o["expected"], 0.35 * 60000 + 0.65 * 47000, places=6)
        self.assertEqual((o["target"], o["invalidation"], o["p"]), (60000.0, 47000.0, 0.35))
        self.assertTrue(o["cone"])
        self.assertEqual(o["cone"][0][0], now)
        plain = perf.report(records, START, prices, T0, now, now, [])["positions"][0]["outlook"]
        self.assertEqual(plain["origin"], [float(cycle(2)), btc_usdt_at(cycle(2))])   # the plan: when it was set
        self.assertIsNone(plain["expected"])


class TestPricesAndParsing(unittest.TestCase):
    def test_hourly_candles_replace_the_4_hour_ones_where_both_exist(self):
        now = T0 + 30 * H + 600
        coarse = make_bars(usdt_at, T0 - 40 * H, T0 + 12 * H, step=4 * H)
        fine = make_bars(usdt_at, T0 + 4 * H, now, now=now)
        p = perf.prices_from_bars({"USDT": fine}, now, {"USDT": coarse})
        ts, cs = p.closes["USDT"]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(len(ts), len(set(ts)))
        # T0 is 2 hours past a 4-hour boundary: the 4-hour closes run to T0+2h, the hourly ones from T0+5h
        self.assertEqual(ts[ts.index(T0 + 5 * H) - 1], T0 + 2 * H)
        self.assertEqual([t for t in ts if T0 + 2 * H < t < T0 + 5 * H], [])
        self.assertEqual(ts[-1], T0 + 30 * H)                                 # only closed candles
        self.assertEqual(p.latest("USDT"), usdt_at(now))                      # the running candle
        self.assertEqual(p.irt("USDT", T0 - 9 * H), usdt_at(T0 - 10 * H))    # a 4-hour close
        self.assertEqual(p.at("USDT", now, now), usdt_at(now))
        self.assertEqual(p.irt("IRT", 0), 1.0)
        self.assertIsNone(p.irt("DOGE", now))
        self.assertIsNone(p.usdt("DOGE", now, now))

    def test_bad_fills_and_records_are_skipped(self):
        records = [{"time": T0, "fills": [
            {"symbol": "BTC_IRT", "side": "buy", "base": "0", "quote": "5"},
            {"symbol": "BTCIRT", "side": "buy", "base": "1", "quote": "5"},
            {"symbol": "BTC_IRT", "side": "hold", "base": "1", "quote": "5"},
            {"symbol": "BTC_IRT", "side": "buy", "base": "nan", "quote": "5"},
            {"symbol": "btc_irt", "side": "buy", "base": "1", "quote": "5", "fee": "-3", "reason": "x" * 99},
            "not a fill"]},
            {"time": None, "fills": [{"symbol": "BTC_IRT", "side": "buy", "base": "1", "quote": "5"}]},
            {"time": T0 + 1, "fills": "nope", "equity_irt": "abc"}]
        f = perf.parse_fills(records)
        self.assertEqual(len(f), 1)
        self.assertEqual((f[0]["symbol"], f[0]["fee"], len(f[0]["reason"])), ("BTC_IRT", 0.0, 40))
        self.assertEqual(perf.equity_points(records), [])
        self.assertEqual(perf.resting_orders({"orders": "x"}), [])
        self.assertEqual(perf.open_plans([{"positions": ["x"]}]), {})

    def test_an_empty_account_is_a_report_not_an_error(self):
        p = world_prices(T0 + 5 * H)
        r = perf.report([], START, p, T0, T0 + 5 * H, T0 + 5 * H)
        self.assertEqual(r["totals"]["value_to_irt"], START)
        self.assertEqual(r["totals"]["pnl_irt"], 0.0)
        self.assertEqual((r["history"], r["positions"]), ([], []))
        self.assertEqual([a["asset"] for a in r["assets"]], ["IRT"])


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_perf_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        with open(os.path.join(self.dir, perf.RUNNER_LOG), "w", encoding="utf-8") as f:
            for rec in world_records():
                f.write(json.dumps(dict(rec, mode="scheduled" if rec["time"] == cycle(3) else "live",
                                        decision={"big": "x" * 100})) + "\n")
            f.write("{broken line\n")
            f.write("[1, 2]\n")
        with open(os.path.join(self.dir, perf.EQUITY_FILE), "w", encoding="utf-8") as f:
            json.dump({"equity_start_irt": START, "hwm_irt": START * 1.2, "updated": T0}, f)
        with open(os.path.join(self.dir, perf.ORDERS_FILE), "w", encoding="utf-8") as f:
            json.dump(ORDERS_DOC, f)
        with open(os.path.join(self.dir, perf.DECISIONS_FILE), "w", encoding="utf-8") as f:
            f.write(decision_line(cycle(20), {"BTC_IRT": world_analyses()["BTC"]}) + "\n")
        self.calls = []

    def fetch(self, symbol, res, start, end):
        self.calls.append((symbol, res, start, end))
        asset = symbol.split("_")[0]
        if asset not in PRICE:
            raise RuntimeError("get_bars %s: no such market" % symbol)
        step = {"60": H, "240": 4 * H}[res]
        return make_bars(PRICE[asset], start, end, step=step, now=self.now)

    def test_all_starts_at_the_first_record_and_uses_hourly_candles_for_a_short_history(self):
        self.now = cycle(29) + 600
        r = perf.collect(self.dir, int(self.now - 400 * 86400), int(self.now), self.now, fetch=self.fetch)
        self.assertEqual(r["from"], cycle(0) - H)
        self.assertEqual(sorted(set(c[:2] for c in self.calls)),
                         [("BTC_IRT", "60"), ("ETH_IRT", "60"), ("USDT_IRT", "60")])
        self.assertIn("ETH_IRT candles: get_bars ETH_IRT: no such market", r["warnings"])
        self.assertEqual(r["start_equity_irt"], START)
        self.assertEqual(r["totals"]["trades"], 4)                              # the "scheduled" record counts too
        self.assertAlmostEqual(r["totals"]["value_to_irt"], world_records()[-1]["equity_irt"], places=4)
        self.assertEqual(len(r["positions"]), 2)
        btc = [p for p in r["positions"] if p["asset"] == "BTC"][0]
        self.assertEqual((btc["analysis"]["p"], btc["analysis"]["row"]), (0.35, "B10"))   # from kimi_decisions.jsonl
        self.assertAlmostEqual(btc["outlook"]["expected"], 0.35 * 60000 + 0.65 * 47000, places=6)
        json.dumps(r)                                                           # what the worker prints

    def test_an_older_range_takes_4_hour_candles_before_the_last_ten_days(self):
        self.now = T0 + 15 * 86400
        r = perf.collect(self.dir, int(T0 - 86400), int(self.now) - 2 * 86400, self.now, fetch=self.fetch)
        self.assertEqual(r["from"], cycle(0) - H)                               # the account starts later
        usdt = [c for c in self.calls if c[0] == "USDT_IRT"]
        fine_from = int(self.now) - perf.FINE_DAYS * 86400
        self.assertEqual([c[1] for c in usdt], ["60", "240"])
        self.assertEqual(usdt[0][2:], (fine_from, int(self.now)))
        self.assertEqual(usdt[1][3], fine_from + 4 * H)
        self.assertLessEqual(usdt[1][2], cycle(0) - 4 * H)
        rows = r["assets"]
        self.assertAlmostEqual(sum(a["pnl_irt"] for a in rows), r["rebuilt_value_to_irt"] - START, places=3)

    def test_the_position_charts_get_the_bots_indicators(self):
        """v3.9: a range that ends now - the markets of the positions and resting orders (and USDT_IRT) take hourly
        candles over the bot's context window; each position gets technical.chart_data() for its chart span."""
        from bitpin import technical
        self.now = T0 + 15 * 86400
        r = perf.collect(self.dir, int(T0 - 86400), int(self.now), self.now, fetch=self.fetch)
        hourly = dict((c[0], c[2]) for c in self.calls if c[1] == "60")
        long_from = int(self.now) - perf.TA_FETCH_HOURS * H
        self.assertEqual(hourly, {"USDT_IRT": long_from, "BTC_IRT": long_from, "ETH_IRT": long_from})
        btc = [p for p in r["positions"] if p["asset"] == "BTC"][0]
        ta = btc["ta"]
        self.assertTrue(set(technical.CHART_KEYS) <= set(ta))
        self.assertIsInstance(ta["now"], dict)
        self.assertIsInstance(ta["reading"], dict)
        chart_from = btc["prices"][0][0]
        self.assertLessEqual(ta["t"][0], chart_from)                  # trimmed to the chart: one bar before it
        self.assertGreater(ta["t"][1], chart_from)
        self.assertLessEqual(ta["t"][-1], self.now)
        eth = [p for p in r["positions"] if p["asset"] == "ETH"][0]   # no market in this world: no indicators
        self.assertIsNone(eth["ta"])
        json.dumps(r)
        # a range that ended earlier has no position charts: no long candles, no indicators
        self.calls = []
        r = perf.collect(self.dir, int(T0 - 86400), int(self.now) - 2 * 86400, self.now, fetch=self.fetch)
        self.assertEqual(r["positions"], [])
        self.assertTrue(all(c[2] >= int(self.now) - perf.FINE_DAYS * 86400 for c in self.calls if c[1] == "60"))

    def test_missing_files_give_an_empty_report(self):
        self.now = T0 + 5 * H
        empty = tempfile.mkdtemp(prefix="bitpin_perf_empty_")
        self.addCleanup(shutil.rmtree, empty, True)
        r = perf.collect(empty, int(T0), int(self.now), self.now, fetch=self.fetch)
        self.assertEqual((r["totals"]["trades"], r["history"], r["positions"]), (0, [], []))
        self.assertEqual(r["start_equity_irt"], 0.0)


if __name__ == "__main__":
    unittest.main()
