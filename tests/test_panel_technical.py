# -*- coding: utf-8 -*-
"""v3.8: the panel's technical page (/technical) - the helper command "technical" (bitpin.technical.collect of the
last decision record, as the user bitpin in production), the page with Kimi's reading and the code's check, the
indicators of every coin, the method in words, Persian, escaping; the analysis box's technical row."""
import contextlib
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from bitpin import panel_web as pw  # noqa: E402
from bitpin.technical import DECISIONS_FILE, collect  # noqa: E402
import panel_helper as ph  # noqa: E402
import test_panel_helper as tph  # noqa: E402
import test_panel_web as tpw  # noqa: E402
from test_technical import BTC  # noqa: E402

ETH = dict(BTC, px_usdt=3000.0, ema_dev_pct=[-0.4, -0.2, 2.0], rsi4h=41.0, macd4h_pct=[-0.1, 0.0, -0.1],
           bb4h=[0.15, 2.5], don20_4h=[2950.0, 3300.0], sup=[2950.0], sup_n=[2], res=[3100.0], res_n=[3],
           vol_ratio=0.6)


def record(ta=None, check=None):
    eth = {"verdict": "reject", "setup": "other", "row": "B10", "p": 0.4, "p0": 0.4, "ev_pct": -1.0, "pass": False}
    if ta is not None:
        eth.update(ta=ta, ta_check=check or [])
    return {"time": tpw.T0 - 3600, "model": "kimi-k3", "current_weights": {"BTC_IRT": 0.3, "USDT_IRT": 0.7},
            "context": {"symbols": {"BTC_IRT": BTC, "ETH_IRT": ETH, "USDT_IRT": {"px": 257000.0, "rsi4h": 75.3,
                                                                                "ema_dev_pct": [0.5, 1.0, 8.0],
                                                                                "ret_irt": [0.2, 1.0, 22.0]}}},
            "decision": {"mode": "slot", "valid": True, "analysis": {"candidates": {"ETH_IRT": eth}}}}


class TestHelperCommand(tph.Base):
    def write(self, rec):
        with open(os.path.join(self.paths.state_dir, DECISIONS_FILE), "w", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def test_in_process_and_as_the_bot_user(self):
        self.write(record(ta={"trend": "down", "rsi": "weak", "read": "bearish"}))
        data = self.ok("technical")
        self.assertEqual([c["symbol"] for c in data["coins"]], ["BTC_IRT", "ETH_IRT", "USDT_IRT"])
        self.assertEqual(data["candidates"][0]["ta"]["read"], "bearish")
        self.paths.state_in_process = False
        self.run_fake.worker_out = json.dumps({"time": 5, "candidates": [], "coins": []})
        self.assertEqual(self.ok("technical"), {"time": 5, "candidates": [], "coins": []})
        argv = [c[0] for c in self.run_fake.calls if c[0][0] == "runuser"][-1]
        self.assertEqual(argv[:4], ["runuser", "-u", "bitpin", "--"])
        self.assertIn("ta-worker", argv)
        self.assertEqual(argv[argv.index("--state-dir") + 1], self.paths.state_dir)
        self.run_fake.worker_out = "Traceback: boom"
        self.assertIn("the technical data could not be read", self.err("technical"))

    def test_the_worker_prints_one_json_line(self):
        self.write(record())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ph.main(["ta-worker", "--state-dir", self.paths.state_dir])
        self.assertEqual(rc, 0)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["held"], ["BTC_IRT"])


class TestPage(tpw.PanelCase):
    def setUp(self):
        super(TestPage, self).setUp()
        self.c.cookies["__Host-bplang"] = "en"
        self.login()

    def show(self, data):
        self.helper.responses["technical"] = lambda a: json.loads(json.dumps(data))
        return self.c.get("/technical").text

    def data(self, **kw):
        import tempfile
        import shutil
        d = tempfile.mkdtemp(prefix="ta_page_")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, DECISIONS_FILE), "w", encoding="utf-8") as f:
            f.write(json.dumps(record(**kw)) + "\n")
        return collect(d)

    def test_kimis_reading_the_check_and_every_coin(self):
        t = self.show(self.data(ta={"trend": "down", "long": "above", "momentum": "falling", "rsi": "neutral",
                                    "bands": "lower", "channel": "breakdown", "volume": "low", "support": 2950,
                                    "resistance": 3100, "read": "bearish"},
                                check=[{"field": "rsi", "model": "neutral", "code": "weak"}]))
        for s in ("Technical analysis", "Kimi&#x27;s technical reading", "<b>Bearish</b>", "Reject",
                  '<span class="ta-bad">Neutral</span><small class="rule">rules: Weak</small>',
                  '<span class="ta-ok">Breakdown</span>', "1 differ", "Indicators of every coin", "BTC_IRT", "Held",
                  "Analysed", "61.0 / 58.0", "+0.8% / +1.6% / +6.0%", "+0.07%", u"0.86 · 3.1%",
                  "76,000.00 - 81,000.00", u"1.70×", "78,400.00", u"-2.0% ×3", "+3.0% / +8.0%",
                  "+1.0% / +22.0%", "How the reading works", "70 or more overbought", "kimi-k3", "slot"):
            self.assertIn(s, t, s)
        self.assertIn('href="/technical"', t)                                 # the navigation entry
        # the page in Persian
        self.c.cookies["__Host-bplang"] = "fa"
        t = self.c.get("/technical").text
        for s in (u"تحلیل تکنیکال", u"خوانش تکنیکال کیمی", u"نزولی (منفی)", u"طبق قاعده: ضعیف", u"اشباع خرید",
                  u"اندیکاتورهای همهٔ کوین‌ها", u"در سبد"):
            self.assertIn(s, t, s)

    def test_an_old_decision_and_no_decision(self):
        t = self.show(self.data())
        self.assertIn("No reading", t)
        self.assertIn("older than the technical reading (version 3.8)", t)
        t = self.show({"time": None, "candidates": [], "coins": []})
        self.assertIn("No decision with a market context is recorded yet.", t)
        self.helper.fail["technical"] = "the technical data could not be read (exit 1): boom"
        r = self.c.get("/technical")
        self.assertEqual(r.status, 502)

    def test_hostile_values_are_escaped(self):
        d = self.data(ta={"trend": "down", "read": "bearish"})
        d["candidates"][0]["symbol"] = tpw.HOSTILE
        d["candidates"][0]["verdict"] = tpw.HOSTILE
        d["candidates"][0]["ta"]["trend"] = tpw.HOSTILE
        d["coins"][0]["symbol"] = tpw.HOSTILE
        d["coins"][0]["values"]["cls"] = tpw.HOSTILE
        d["model"] = tpw.HOSTILE
        t = self.show(d)
        self.assertEscaped(t, "technical page")


class TestAnalysisBox(unittest.TestCase):
    def test_the_technical_row(self):
        html = pw.analysis_html({"analysis": {"setup": "other", "row": "B10", "verdict": "cut",
                                              "ta": {"trend": "down", "rsi": "weak", "read": "bearish"},
                                              "ta_check": [{"field": "rsi", "model": "weak", "code": "neutral"}]}})
        self.assertIn("Technical reading", html)
        self.assertIn("<b>Bearish</b>", html)
        self.assertIn('<span class="ta-ok">Trend: Down</span>', html)
        self.assertIn('<span class="ta-bad">RSI: Weak</span>', html)
        self.assertIn("1 read differently from the rules", html)
        self.assertNotIn("Technical reading", pw.analysis_html({"analysis": {"setup": "other"}}))


if __name__ == "__main__":
    unittest.main()
