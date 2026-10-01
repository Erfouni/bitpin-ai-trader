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

    def test_usdt_irt_is_read_on_its_toman_price_and_odd_rows_give_nothing(self):
        usdt = {"px": 257000.0, "ema_dev_pct": [1.0, 2.0, 9.0], "don20_4h": [240000.0, 256000.0], "rsi4h": 75.3,
                "sup": [251000.0], "res": []}
        r = reading(usdt, "USDT_IRT")
        self.assertEqual((r["channel"], r["rsi"], r["support"]), ("breakout", "overbought", 251000.0))
        self.assertNotIn("resistance", r)
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
        m = method_text()
        for frag in ("> 0.02 = rising", ">= 70 overbought", ">= 0.8 upper", "vol_ratio >= 1.5 high, <= 0.7 low",
                     "no setup and no row of its own, B7, B8", "\"ta\": null"):
            self.assertIn(frag, m)
        obj = json.loads(schema_text().replace("<price>", "1"))
        self.assertEqual(list(obj), list(ta_mod.TA_KEYS[:7]) + ["support", "resistance", "read"])
        self.assertEqual(obj["rsi"], "<overbought|strong|neutral|weak|oversold>")


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
        self.assertEqual([c["symbol"] for c in d["coins"]], ["BTC_IRT", "ETH_IRT", "USDT_IRT"])   # held, candidate
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


if __name__ == "__main__":
    unittest.main()
