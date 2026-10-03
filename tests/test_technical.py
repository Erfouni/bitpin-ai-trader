# -*- coding: utf-8 -*-
"""v3.8: the technical reading (bitpin/technical.py) - one fixed method applied by the model ("ta" of every analysis
candidate) and by the code (reading / check_ta); the prompt renders the method from the same constants; the
decision keeps the model's reading, the code's and the differences, and never fails over them."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bitpin import technical as ta_mod  # noqa: E402
from bitpin.technical import check_ta, collect, method_text, parse_ta, reading, schema_text  # noqa: E402
import test_brain as tb  # noqa: E402  (the analysis fixtures: areply, cand, av, actx)

# a fictional BTC row with every technical field (USDT prices)
BTC = {"px": 21000000000, "px_usdt": 80000.0, "ema_dev_pct": [0.8, 1.6, 6.0], "rsi4h": 61.0, "rsi1d": 58.0,
       "macd4h_pct": [0.12, 0.05, 0.07], "bb4h": [0.86, 3.1], "don20_4h": [76000.0, 81000.0], "pos30": 0.7,
       "vol_ratio": 1.7, "atr4h_pct": 1.5, "sup": [78400.0, 76000.0], "sup_n": [3, 2], "res": [82000.0],
       "res_n": [2], "ret_usdt": [0.5, 3.0, 8.0], "sig_d": 2.0, "d30h": -4.0}


class TestRules(unittest.TestCase):
    def test_a_full_row(self):
        r = reading(BTC, "BTC_IRT")
        self.assertEqual({k: r[k] for k in ta_mod.TA_FIELDS},
                         {"trend": "up", "long": "above", "momentum": "rising", "rsi": "strong", "bands": "upper",
                          "channel": "upper_half", "volume": "high"})
        self.assertEqual((r["support"], r["support_dist_pct"], r["support_n"]), (78400.0, -2.0, 3))
        self.assertEqual((r["resistance"], r["resistance_dist_pct"], r["resistance_n"]), (82000.0, 2.5, 2))
        self.assertEqual(r["tone"], "bullish")

    def test_every_boundary(self):
        def one(**f):
            row = dict(BTC)
            row.update(f)
            return reading(row, "BTC_IRT")
        self.assertEqual(one(ema_dev_pct=[-0.1, -0.2, -3.0])["trend"], "down")
        self.assertEqual(one(ema_dev_pct=[0.1, -0.2, 3.0])["trend"], "mixed")
        self.assertEqual(one(ema_dev_pct=[0.0, 0.2, 3.0])["trend"], "mixed")
        self.assertEqual(one(ema_dev_pct=[0.1, 0.2, -3.0])["long"], "below")
        self.assertNotIn("long", one(ema_dev_pct=[0.1, 0.2, 0.0]))
        for hist, want in ((0.021, "rising"), (0.02, "flat"), (-0.02, "flat"), (-0.021, "falling")):
            self.assertEqual(one(macd4h_pct=[0, 0, hist])["momentum"], want, hist)
        for rsi, want in ((70, "overbought"), (69.9, "strong"), (55, "strong"), (54.9, "neutral"), (45.1, "neutral"),
                          (45, "weak"), (30.1, "weak"), (30, "oversold"), (10, "oversold")):
            self.assertEqual(one(rsi4h=rsi)["rsi"], want, rsi)
        for place, want in ((1.01, "above_upper"), (1.0, "upper"), (0.8, "upper"), (0.79, "middle"), (0.21, "middle"),
                            (0.2, "lower"), (0.0, "lower"), (-0.01, "below_lower")):
            self.assertEqual(one(bb4h=[place, 2.0])["bands"], want, place)
        for px, want in ((81000.0, "breakout"), (90000.0, "breakout"), (76000.0, "breakdown"), (78500.0, "upper_half"),
                         (78499.0, "lower_half")):
            self.assertEqual(one(px_usdt=px)["channel"], want, px)
        for vr, want in ((1.5, "high"), (1.49, "normal"), (0.71, "normal"), (0.7, "low")):
            self.assertEqual(one(vol_ratio=vr)["volume"], want, vr)
        r = one(sup=[], sup_n=[], res=None)                  # no swing levels: the channel's edges
        self.assertEqual((r["support"], r["resistance"]), (76000.0, 81000.0))
        self.assertNotIn("support_n", r)
        self.assertEqual(one(ema_dev_pct=[-1, -1, -1], macd4h_pct=[0, 0, -0.5])["tone"], "bearish")
        self.assertEqual(one(macd4h_pct=[0, 0, -0.5])["tone"], "neutral")

    def test_usdt_irt_gets_no_reading_and_odd_rows_give_nothing(self):
        """v3.10: the technical reading is in USDT only - USDT_IRT is the toman's price, not a coin's chart."""
        usdt = {"px": 257000.0, "ema_dev_pct": [1.0, 2.0, 9.0], "don20_4h": [240000.0, 256000.0], "rsi4h": 75.3,
                "sup": [251000.0], "res": []}
        self.assertEqual(reading(usdt, "USDT_IRT"), {})
        self.assertIn("USDT_IRT (the toman's price) get \"ta\": null", ta_mod.method_text_v38())
        self.assertNotIn("USDT_IRT: px", method_text())
        self.assertEqual(ta_mod.context_text(reading(usdt, "USDT_IRT")), "")         # v3.12: no "ta" in the context
        self.assertEqual(reading(None), {})
        self.assertEqual(reading({"px_usdt": "x", "rsi4h": True, "ema_dev_pct": "1,2"}), {})


class TestModelReading(unittest.TestCase):
    def test_parse_and_check(self):
        ta, dropped = parse_ta({"trend": " Up ", "long": "above", "momentum": "rising", "rsi": "neutral",
                                "bands": "upper", "channel": "sideways", "volume": "high", "support": 78400,
                                "resistance": "82000", "read": "bullish", "extra": 1})
        self.assertEqual(dropped, ["channel", "resistance"])
        self.assertEqual(ta, {"trend": "up", "long": "above", "momentum": "rising", "rsi": "neutral", "bands": "upper",
                              "volume": "high", "support": 78400.0, "read": "bullish"})
        self.assertEqual(check_ta(ta, reading(BTC, "BTC_IRT")), [{"field": "rsi", "model": "neutral", "code": "strong"}])
        self.assertEqual(check_ta(dict(ta, support=78700.0), reading(BTC)), [
            {"field": "rsi", "model": "neutral", "code": "strong"}])               # 0.38% away: the same level
        self.assertEqual(check_ta(dict(ta, support=77000.0), reading(BTC))[-1]["field"], "support")
        self.assertEqual(parse_ta(None), (None, []))
        self.assertEqual(parse_ta("bullish"), (None, []))
        self.assertEqual(check_ta(None, reading(BTC)), [])

    def test_the_prompt_text_and_schema_come_from_the_constants(self):
        m = method_text()                                       # v3.12: the code's reading, the model adds "read"
        for frag in ("beyond +-0.02% of the price = rising / falling", "rsi4h 70 / 55 / 45 / 30", "from 0.8 upper",
                     "vol_ratio 1.5 / 0.7 = high / low", "no setup and no row of its own (B7, B8, study 08)",
                     "Take it as given, never recompute it", "goes into its \"read\""):
            self.assertIn(frag, m)
        self.assertEqual(json.loads("{" + schema_text() + "}"), {"read": "<bullish|bearish|neutral>"})
        m38 = ta_mod.method_text_v38()                          # the v3.8..v3.11 text (the model filled "ta")
        for frag in ("> 0.02 = rising", ">= 70 overbought", ">= 0.8 upper", "vol_ratio >= 1.5 high, <= 0.7 low",
                     "no setup and no row of its own, B7, B8", "\"ta\": null"):
            self.assertIn(frag, m38)
        obj = json.loads(ta_mod.schema_text_v38().replace("<price>", "1"))
        self.assertEqual(list(obj), list(ta_mod.TA_KEYS[:7]) + ["support", "resistance", "read"])
        self.assertEqual(obj["rsi"], "<overbought|strong|neutral|weak|oversold>")

    def test_the_code_reading_for_the_context_and_the_candidate(self):
        """v3.12: the context's compact "ta" and the candidate's "ta" built from the code's reading + the model's read."""
        r = reading(BTC, "BTC_IRT")
        txt = ta_mod.context_text(r)
        self.assertEqual(txt.split("/"), [r[k] for k in ta_mod.TA_FIELDS])
        self.assertEqual(ta_mod.context_text({"trend": "up"}), "up/?/?/?/?/?/?")
        self.assertEqual(ta_mod.context_text({}), "")
        c = ta_mod.code_ta(r, " Bullish ")
        self.assertEqual(c["read"], "bullish")
        self.assertEqual(c["support"], r["support"])
        self.assertNotIn("tone", c)
        self.assertNotIn("read", ta_mod.code_ta(r, "to the moon"))
        self.assertIsNone(ta_mod.code_ta({}, "bullish"))


class TestInTheDecision(unittest.TestCase):
    def ctx(self):
        c = tb.actx()                                    # keeps its own ret_usdt: the candidate cites it
        c["symbols"]["BTC_IRT"].update({k: v for k, v in BTC.items() if k != "ret_usdt"})
        return c

    def test_the_decision_keeps_both_readings_and_the_differences(self):
        model_ta = {"trend": "up", "long": "above", "momentum": "rising", "rsi": "neutral", "bands": "upper",
                    "channel": "upper_half", "volume": "high", "support": 78400, "resistance": 82000, "read": "bullish"}
        with self.assertLogs("bitpin.brain", level="INFO") as cm:
            out = tb.av(tb.areply(candidate=tb.cand(ta=model_ta)), ctx=self.ctx())
        self.assertAlmostEqual(out["targets"]["BTC_IRT"], 0.3, places=6)        # a wrong field never blocks
        c = out["analysis"]["candidates"]["BTC_IRT"]
        self.assertEqual(c["ta"]["read"], "bullish")
        self.assertEqual(c["ta_code"]["rsi"], "strong")
        self.assertEqual(c["ta_check"], [{"field": "rsi", "model": "neutral", "code": "strong"}])
        self.assertIn("technical reading of BTC_IRT: rsi read neutral, the rules give strong", "\n".join(cm.output))
        self.assertFalse(any("technical" in n for n in out["adjustments"]))     # not fed back as an adjustment

    def test_a_missing_or_null_reading_is_fine(self):
        for ta in (None, "x", {"trend": "sideways"}):
            out = tb.av(tb.areply(candidate=tb.cand(ta=ta)), ctx=self.ctx(), policy="error")
            c = out["analysis"]["candidates"]["BTC_IRT"]
            self.assertEqual(c["ta_check"], [])
            self.assertEqual(c["ta_code"]["trend"], "up")
        self.assertEqual(c["ta_dropped"], ["trend"])


class TestPanelData(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="technical_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, *recs, junk=b""):
        with open(os.path.join(self.dir, ta_mod.DECISIONS_FILE), "wb") as f:
            for r in recs:
                f.write(json.dumps(r).encode("utf-8") + b"\n")
            f.write(junk)

    def test_the_last_record_with_a_context(self):
        eth = dict(BTC, px_usdt=3000.0, rsi4h=40.0, sup=[2900.0], res=[3100.0], don20_4h=[2800.0, 3200.0])
        rec = {"time": 1790074800.0, "model": "kimi-k3", "current_weights": {"BTC_IRT": 0.3, "USDT_IRT": 0.7},
               "context": {"symbols": {"BTC_IRT": BTC, "ETH_IRT": eth, "DOGE_IRT": {"px": 5, "r_usdt": [1, 2, 3]},
                                       "USDT_IRT": {"px": 257000.0, "rsi4h": 75.0, "ema_dev_pct": [1, 2, 9]},
                                       "LINK_IRT": {"px": 9, "px_usdt": 20.0, "rsi4h": 50.0}}},
               "decision": {"mode": "slot", "valid": True, "analysis": {"candidates": {"ETH_IRT": {
                   "verdict": "reject", "setup": "other", "row": "B10", "p": 0.4, "p0": 0.4, "ev_pct": -1.0,
                   "pass": False, "ta": {"trend": "down", "rsi": "weak"}}}}}}
        self.write({"time": 1, "context": {"symbols": {}}}, rec, junk=b'{"time": 3, "context": ')   # a cut last line
        d = collect(self.dir)
        self.assertEqual((d["time"], d["model"], d["mode"], d["held"]), (1790074800.0, "kimi-k3", "slot", ["BTC_IRT"]))
        self.assertEqual([c["symbol"] for c in d["coins"]], ["BTC_IRT", "ETH_IRT"])   # held, candidate; no USDT_IRT
        self.assertEqual(d["usdt"], {"px": 257000.0, "ret_irt": []})                   # v3.10: the toman apart
        self.assertEqual(d["usdt_weight"], 0.7)
        self.assertTrue(d["coins"][0]["held"])
        self.assertEqual(d["coins"][0]["values"]["rsi4h"], 61.0)
        self.assertEqual(d["coins"][0]["reading"]["trend"], "up")
        c = d["candidates"][0]
        self.assertEqual((c["symbol"], c["verdict"], c["ta"]), ("ETH_IRT", "reject", {"trend": "down", "rsi": "weak"}))
        self.assertEqual(c["ta_check"], [{"field": "trend", "model": "down", "code": "up"}])   # recomputed (old record)

    def test_no_file_or_nothing_readable(self):
        self.assertEqual(collect(self.dir), {"time": None, "candidates": [], "coins": []})
        self.write(junk=b"not json\n[1, 2]\n")
        self.assertEqual(collect(self.dir)["coins"], [])



# --------------------------------------------------------------------------- v3.9: the chart series
T_END = 1790006400                      # an hour boundary (and a 4h one)


def synth(n, t_end=T_END, seed=7):
    """n hourly bars of a coin in toman (a random walk in USDT times a falling rial) and of USDT_IRT."""
    import random
    from bitpin.data import Bar
    rnd = random.Random(seed)
    coin, usdt = [], []
    p_u, p_c = 100000.0, 50000.0
    for i in range(n):
        t = t_end - (n - i) * 3600
        p_u *= 1 + 0.0002 + rnd.gauss(0, 0.001)
        p_c *= 1 + rnd.gauss(0.0001, 0.006)
        usdt.append(Bar(t, p_u, p_u, p_u, p_u, 10.0))
        irt = p_c * p_u
        coin.append(Bar(t, irt, irt * 1.002, irt * 0.998, irt, 1.0 + rnd.random()))
    return coin, usdt


class TestChartData(unittest.TestCase):
    def setUp(self):
        self.coin, self.usdt = synth(1900)
        self.t_from = T_END - 30 * 86400
        self.d = ta_mod.chart_data("XYZ_IRT", self.coin, self.usdt, self.t_from)

    def test_the_last_values_are_the_bots_own(self):
        """The series end at exactly what the bot's market context gives the model now (symbol_features of the
        same last 1700 hourly bars), and "now" / "reading" are that context and the rules' reading of it."""
        from bitpin import analysis
        d = self.d
        feat = analysis.symbol_features("XYZ_IRT", self.coin[-1700:], [b.ts for b in self.usdt],
                                        [b.close for b in self.usdt])
        c = d["close"][-1]
        for k, n in enumerate((20, 50, 200)):
            self.assertAlmostEqual((c / d["ema%d" % n][-1] - 1) * 100, feat["ema_dev_pct"][k], delta=0.051)
        self.assertAlmostEqual(d["rsi"][-1], feat["rsi4h"], delta=0.051)
        for j in range(3):
            self.assertAlmostEqual(d["macd"][-1][j], feat["macd4h_pct"][j], delta=0.0051)
        lo, mid, up = d["bb"][-1]
        self.assertAlmostEqual((c - lo) / (up - lo), feat["bb4h"][0], delta=0.0051)
        self.assertAlmostEqual((up - lo) / mid * 100, feat["bb4h"][1], delta=0.051)
        for j in range(2):
            self.assertAlmostEqual(d["don"][-1][j] / feat["don20_4h"][j], 1.0, delta=1e-4)
        self.assertEqual(d["now"]["ema_dev_pct"], feat["ema_dev_pct"])
        self.assertEqual(d["now"]["vol_ratio"], feat["vol_ratio"])
        self.assertEqual(d["reading"], reading(feat, "XYZ_IRT"))

    def test_the_shape_of_the_series(self):
        d = self.d
        n = len(d["t"])
        self.assertGreater(n, 170)
        for k in ta_mod.CHART_KEYS:
            self.assertEqual(len(d[k]), n, k)
        self.assertGreaterEqual(d["t"][0], self.t_from)
        self.assertLessEqual(d["t"][-1], T_END)
        self.assertTrue(all(t % ta_mod.CHART_STEP == 0 for t in d["t"]))                  # the close of a 4h bar
        self.assertTrue(all(b - a == ta_mod.CHART_STEP for a, b in zip(d["t"], d["t"][1:])))
        bucket = [(b, u) for b, u in zip(self.coin, self.usdt) if d["t"][-1] - ta_mod.CHART_STEP <= b.ts < d["t"][-1]]
        self.assertEqual(len(bucket), 4)
        usd = sum(b.volume * b.close / u.close for b, u in bucket) / 1e3              # v3.10: thousand USDT
        self.assertAlmostEqual(d["vol"][-1], usd, delta=d["vol"][-1] * 1e-3)
        self.assertGreater(d["vol_avg"], 0)
        json.dumps(d)                                               # what the worker prints

    def test_only_the_bots_window_counts(self):
        self.assertEqual(ta_mod.chart_data("XYZ_IRT", self.coin[-1700:], self.usdt, self.t_from), self.d)

    def test_too_little_data(self):
        self.assertIsNone(ta_mod.chart_data("XYZ_IRT", self.coin[-60:], self.usdt, self.t_from))
        self.assertIsNone(ta_mod.chart_data("XYZ_IRT", self.coin, [], self.t_from))
        self.assertIsNone(ta_mod.chart_data("XYZ_IRT", self.coin, self.usdt, T_END + 86400))     # nothing that late

    def test_trim(self):
        t = self.d["t"]
        cut = ta_mod.trim_chart(self.d, t[10] + 3600)
        self.assertEqual(cut["t"][0], t[10])                          # the bar before the cut: no gap at the edge
        for k in ta_mod.CHART_KEYS:
            self.assertEqual(cut[k], self.d[k][10:], k)
        self.assertEqual(cut["now"], self.d["now"])
        self.assertEqual(ta_mod.trim_chart(self.d, 0)["t"], t)
        self.assertIsNone(ta_mod.trim_chart(None, 0))


if __name__ == "__main__":
    unittest.main()
