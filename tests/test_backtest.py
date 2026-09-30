import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin.backtest import Panel, Strategy, buy_and_hold, check_no_lookahead, simulate  # noqa: E402
from bitpin.data import Bar, resample  # noqa: E402


def panel_from_closes(closes, sym="AAA_IRT", opens=None):
    opens = opens or closes
    bars = [Bar(3600 * i, o, max(o, c), min(o, c), c, 1.0) for i, (o, c) in enumerate(zip(opens, closes))]
    return Panel.from_bars({sym: bars}, "60")


class SimulateTest(unittest.TestCase):
    def test_executes_next_open_with_fee_and_slippage(self):
        p = panel_from_closes([100, 110, 121], opens=[100, 105, 120])
        w = {"AAA_IRT": [1.0, 1.0, 1.0]}
        r = simulate(p, w, fee=0.01, slippage=0.0, rebalance_threshold=0.0)
        # bar0 close: flat -> equity 1. bar1 open: buy at 105 with 1% fee.
        units = 0.99 / 105
        self.assertAlmostEqual(r["equity"][0], 1.0)
        self.assertAlmostEqual(r["equity"][1], units * 110)
        self.assertAlmostEqual(r["equity"][2], units * 121)
        self.assertEqual(len(r["trades"]), 1)

    def test_full_exit_and_threshold(self):
        p = panel_from_closes([100, 100, 100, 100])
        w = {"AAA_IRT": [1.0, 0.99, 0.0, 0.0]}
        r = simulate(p, w, fee=0.0, slippage=0.0, rebalance_threshold=0.02)
        sides = [t[2] for t in r["trades"]]
        self.assertEqual(sides, ["buy", "sell"])  # 0.99 change skipped, exit executed
        self.assertAlmostEqual(r["equity"][-1], 1.0)

    def test_weights_normalised_when_over_one(self):
        bars_a = [Bar(3600 * i, 10, 10, 10, 10, 1) for i in range(3)]
        bars_b = [Bar(3600 * i, 20, 20, 20, 20, 1) for i in range(3)]
        p = Panel.from_bars({"A_IRT": bars_a, "B_IRT": bars_b}, "60")
        r = simulate(p, {"A_IRT": [1, 1, 1], "B_IRT": [1, 1, 1]}, fee=0, slippage=0, rebalance_threshold=0)
        self.assertAlmostEqual(r["exposure"][-1], 1.0)
        self.assertAlmostEqual(r["equity"][-1], 1.0)

    def test_buy_and_hold_matches_simulate_single(self):
        p = panel_from_closes([100, 101, 99, 120, 130], opens=[100, 100, 100, 118, 128])
        m1, r1 = buy_and_hold(p, ["AAA_IRT"], fee=0.0035, slippage=0.0005)
        r2 = simulate(p, {"AAA_IRT": [1.0] * 5}, fee=0.0035, slippage=0.0005, rebalance_threshold=0.02)
        for a, b in zip(r1["equity"], r2["equity"]):
            self.assertAlmostEqual(a, b)


class LookaheadTest(unittest.TestCase):
    def test_detects_future_peek(self):
        class Peek(Strategy):
            name = "peek"
            symbols = ["AAA_IRT"]

            def weights(self, p):
                c = p["AAA_IRT"]["close"]
                return {"AAA_IRT": [1.0 if i + 1 < len(c) and c[i + 1] > c[i] else 0.0 for i in range(len(c))]}

        closes = [100 + ((i * 7) % 13) for i in range(300)]
        self.assertTrue(check_no_lookahead(Peek(), panel_from_closes(closes), samples=10))


class ResampleTest(unittest.TestCase):
    def test_resample_ohlc(self):
        bars = [Bar(1800 + 3600 * i, i, i + 0.5, i - 0.5, i + 0.1, 1) for i in range(8)]
        out = resample(bars, 4)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].open, 0)
        self.assertEqual(out[0].close, 3.1)
        self.assertEqual(out[0].high, 3.5)
        self.assertEqual(out[0].low, -0.5)
        self.assertEqual(out[0].volume, 4)


if __name__ == "__main__":
    unittest.main()
