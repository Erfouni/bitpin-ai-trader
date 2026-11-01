"""v3.19 METHODS REPORT: the core rule (paper), Kimi's shadow and holding USDT compared in USDT."""
import unittest

from bitpin import methods_report as mr

D = mr.DAY
T = 1793000000 // D * D           # a UTC midnight


def rec(day, weight, shadow=None, valid=True, hour=15.5):
    return {"decision": {"decided_at": T + day * D + hour * 3600,
                         "core": {"symbol": "BTC_IRT", "weight": weight},
                         "shadow": {"valid": valid, "targets": shadow or {"USDT_IRT": 1.0}}}}


def fetcher(series):
    """series = {market: [close day0, close day1, ...]}"""
    def fetch(symbol, start, end):
        return [(T + i * D, p) for i, p in enumerate(series[symbol]) if start <= T + i * D <= end]
    return fetch


class ReplayTests(unittest.TestCase):
    def test_points_take_the_last_decision_of_a_day_and_skip_records_without_the_rule(self):
        core, shadow, sym = mr.trial_points([{"decision": {"decided_at": T}}, rec(0, 0.6), rec(0, 0.0, hour=20),
                                             rec(1, 0.6, {"XAUT_IRT": 0.35, "USDT_IRT": 0.65}),
                                             rec(2, 0.6, valid=False)])
        self.assertEqual(sym, "BTC_IRT")
        self.assertEqual(core, [(T, {}), (T + D, {"BTC_IRT": 0.6}), (T + 2 * D, {"BTC_IRT": 0.6})])
        self.assertEqual(shadow, [(T, {}), (T + D, {"XAUT_IRT": 0.35})])   # an invalid shadow keeps the last

    def test_replay_follows_weights_pays_costs_and_drifts(self):
        days = [T + i * D for i in range(3)]
        prices = {"BTC_IRT": {days[0]: 100.0, days[1]: 110.0, days[2]: 121.0}}
        eq = mr.replay([(days[0], {"BTC_IRT": 1.0})], prices, days)
        self.assertAlmostEqual(eq[-1][1], (1 - mr.COST_USDT_ROUTE) * 1.21)
        eq = mr.replay([(days[0], {"BTC_IRT": 0.5})], prices, days)
        self.assertAlmostEqual(eq[1][1], (1 - 0.5 * mr.COST_USDT_ROUTE) * 1.05)
        eq = mr.replay([(days[0], {})], prices, days)
        self.assertEqual(eq[-1][1], 1.0)

    def test_toman_route_markets_cost_more(self):
        self.assertEqual(mr.cost_of("BTC_IRT"), mr.COST_USDT_ROUTE)
        self.assertEqual(mr.cost_of("XAUT_IRT"), mr.COST_TOMAN_ROUTE)

    def test_compare_prices_in_usdt_and_stops_at_the_last_closed_day(self):
        fetch = fetcher({"BTC_USDT": [100, 110, 99, 120], "XAUT_IRT": [2000, 2000, 2200, 0],
                         "USDT_IRT": [10, 10, 10, 10]})
        records = [rec(0, 0.6, {"XAUT_IRT": 0.5, "USDT_IRT": 0.5})]
        c = mr.compare(records, fetch, now=T + 3 * D + 100)       # day 3 is still open
        self.assertEqual(c["end"], T + 2 * D)
        self.assertAlmostEqual(c["btc"][0], -0.01)
        self.assertGreater(c["shadow"][0], 0.0)                   # gold +10% on the last day
        self.assertEqual(c["core_now"], {"BTC_IRT": 0.6})
        self.assertIsNone(mr.compare([], fetch, now=T))
        partial = mr.compare([rec(0, 0.6, {"NEW_IRT": 0.5})], fetch, now=T + 3 * D)   # NEW_IRT has no prices
        self.assertIsNotNone(partial["shadow"])


class ReportFileTests(unittest.TestCase):
    def test_report_round_trips_and_rejects_garbage(self):
        fetch = fetcher({"BTC_USDT": [100, 110, 121], "USDT_IRT": [10, 10, 10]})
        doc = mr.report([rec(0, 0.6)], fetch, T + 3 * D)
        c = mr.load(doc)
        self.assertEqual(c["core_now"], {"BTC_IRT": 0.6})
        self.assertIsNone(mr.load({"schema": 2}))
        self.assertIsNone(mr.load("x"))

    def test_tail_records_skips_a_cut_line(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "log.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps({"a": "x" * 50}) + "\n" + json.dumps({"b": 1}) + "\n")
            self.assertEqual(mr.tail_records(p, max_bytes=20), [{"b": 1}])
            self.assertEqual(len(mr.tail_records(p)), 2)
            self.assertEqual(mr.tail_records(os.path.join(d, "missing")), [])


class TelegramTextTests(unittest.TestCase):
    def test_methods_text_names_every_method_from_the_bot_report(self):
        from bitpin import notify
        fetch = fetcher({"BTC_USDT": [100, 110, 121], "USDT_IRT": [10, 10, 10], "XAUT_IRT": [40, 41, 42]})
        doc = mr.report([rec(0, 0.6, {"XAUT_IRT": 0.35, "USDT_IRT": 0.65})], fetch, T + 3 * D)

        class Fake:
            def _read_json(self, name):
                return doc if name == "methods" else None
        text = notify.Notifier.methods_text(Fake(), T + 3 * D)
        for word in ("قانون اصلی", "کیمی", "USDT", "BTC", "XAUT"):
            self.assertIn(word, text)
        Fake._read_json = lambda self, name: None
        self.assertIsNone(notify.Notifier.methods_text(Fake(), T))


if __name__ == "__main__":
    unittest.main()
