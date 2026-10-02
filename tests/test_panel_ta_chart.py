# -*- coding: utf-8 -*-
"""v3.9: the indicators on the panel's position charts - line_chart's columns and fixed axes, the overlays and the
RSI / MACD / traded-value panes of a position (bitpin/technical.py chart_data of synthetic candles), and the
performance page's switch that keeps its choice through the live reload."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bitpin import panel_web as pw  # noqa: E402
from bitpin import technical  # noqa: E402
from bitpin.panel_i18n import use  # noqa: E402
import test_panel_web as tw  # noqa: E402  (its PanelCase: the app with the fake helper)
from test_technical import T_END, synth  # noqa: E402

H = 3600


def position(with_ta=True):
    """A position card's data like bitpin/performance.py makes it, with the indicators of synthetic candles."""
    coin, usdt = synth(1900)
    c_from = T_END - 7 * 86400
    line = [[b.ts + H, b.close / u.close] for b, u in zip(coin, usdt) if b.ts + H >= c_from]
    p = {"asset": "XYZ", "qty": 0.5, "value_irt": 1.0, "value_usdt": 1.0, "price_usdt": line[-1][1],
         "entry_usdt": None, "stop_usdt": None, "target_usdt": None, "invalidation_usdt": None, "orders": [],
         "prices": line, "analysis": None, "outlook": {}, "max_hold_until": None, "set_at": None}
    if with_ta:
        p["ta"] = technical.trim_chart(technical.chart_data("XYZ_IRT", coin, usdt, T_END - 30 * 86400), c_from)
    return p


class TestLineChart(unittest.TestCase):
    def test_columns_from_zero(self):
        t0 = T_END
        html = pw.line_chart([], [("ta-vavg", 2.0)], bars=[("ta-vol", [(t0 + i * 4 * H, 1.0 + i) for i in range(6)])])
        self.assertEqual(html.count('<rect class="b ta-vol"'), 5)      # a column covers the 4h before its time:
        #                                                                  the first one lies before the plot
        self.assertIn('class="h ta-vavg"', html)
        for x, w in re.findall(r'<rect class="b ta-vol" x="([-\d.]+)" y="[-\d.]+" width="([\d.]+)"', html):
            self.assertGreaterEqual(float(x), 0.0)                       # never past the plot's left edge
            self.assertLessEqual(float(x) + float(w), 1000.0 + 1e-6)
        neg = pw.line_chart([], bars=[("ta-hn", [(t0, -0.2), (t0 + 4 * H, -0.1)]), ("ta-hp", [(t0 + 8 * H, 0.3)])],
                            pct=True)
        self.assertIn('class="g z"', neg)                                # the zero line of a signed pane
        self.assertEqual(neg.count('class="b ta-hn"'), 1)              # the first column lies before the plot
        self.assertEqual(neg.count('class="b ta-hp"'), 1)

    def test_fixed_value_axis_and_time_range(self):
        t0 = T_END
        pts = [(t0 + i * H, 40.0 + i) for i in range(10)]
        html = pw.line_chart([("ta-rsi", pts)], y_range=(0, 100), x_range=(t0 + 2 * H, t0 + 8 * H))
        self.assertIn("<span>100</span>", html)
        self.assertIn("<span>0</span>", html)
        drawn = re.search(r'<polyline class="l ta-rsi" points="([^"]+)"', html).group(1).split()
        self.assertEqual(len(drawn), 7)                                  # only the points inside the time range
        self.assertEqual(drawn[0].split(",")[0], "0.0")
        self.assertEqual(drawn[-1].split(",")[0], "1000.0")


class TestPositionCard(unittest.TestCase):
    def test_the_indicators_on_the_chart_and_the_panes(self):
        p = position()
        with use("en"):
            html = pw.PanelApp._position_panel(p)
        for s in ('<polyline class="l ta-e20"', '<polyline class="l ta-e50"', '<polyline class="l ta-e200"',
                  '<polyline class="l ta-bbm"', '<polygon class="a ta-bb"', '<polyline class="l ta-don"',
                  'class="h ta-sup"', 'class="h ta-res"', '<polyline class="l ta-rsi"', 'class="h ta-lim"',
                  '<polyline class="l ta-macd"', '<polyline class="l ta-sig"', 'class="b ta-vol"',
                  "Indicators: 4-hour bars in USDT, computed like the bot&#x27;s market context", "EMA 20:",
                  "Bollinger 20, 2", "Donchian 20:", "Support ", "Resistance ", "The rules read it now:", "RSI 14:",
                  "MACD 12, 26, 9 in percent of the price: histogram", "Traded value per 4 hours (million toman)",
                  "the last 24 hours against the 30-day average"):
            self.assertIn(s, html, s)
        self.assertEqual(html.count('<figure class="chart xs'), 3)       # RSI, MACD, traded value
        self.assertLess(html.index('class="l ta-e20"'), html.index('class="l pl"'))   # the price line on top
        ema = p["ta"]["ema20"][-1]
        self.assertIn(pw.price_text(ema), html)

    def test_off_and_without_data_the_card_is_the_old_one(self):
        p = position()
        with use("en"):
            off = pw.PanelApp._position_panel(p, show_ta=False)
            old = pw.PanelApp._position_panel(position(with_ta=False))
        self.assertEqual(off, old)
        self.assertNotIn("ta-", off)


class TestPerformancePage(tw.PanelCase):
    def setUp(self):
        tw.PanelCase.setUp(self)
        base = self.helper.responses["performance"]

        def report(a):
            rep = base(a)
            for p in rep["positions"]:
                p["ta"] = position()["ta"]
            return rep
        self.helper.responses["performance"] = report
        self.c = tw.Client(self.app, lang="en")
        self.login()

    def test_the_switch_keeps_its_choice_through_the_live_reload(self):
        t = self.c.get("/performance").text
        self.assertIn('class="l ta-e20"', t)
        self.assertIn('href="/performance?range=all&amp;ta=0"', t)
        self.assertIn("Hide the indicators", t)
        t = self.c.get("/performance", "range=all&ta=0").text
        self.assertNotIn('class="l ta-e20"', t)
        self.assertIn("Show the indicators", t)
        self.assertIn('href="/performance?range=all"', t)
        self.assertIn('<meta http-equiv="refresh" content="60; url=/performance?range=all&amp;ta=0&amp;auto=1">', t)
        self.assertIn("range=7d&amp;ta=0", t)                             # the range buttons keep it too
        t = self.c.get("/performance", "range=all&ta=0&live=0").text
        self.assertIn('href="/performance?range=all&amp;live=0"', t)     # showing them again keeps live off

    def test_no_switch_without_indicators(self):
        self.helper.responses["performance"] = tw.perf_report
        t = self.c.get("/performance").text
        self.assertNotIn("Hide the indicators", t)


if __name__ == "__main__":
    unittest.main()
