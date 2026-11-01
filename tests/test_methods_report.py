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
    def test_points_keep_decision_times_and_skip_records_without_the_rule(self):
        core, shadow, sym = mr.trial_points([{"decision": {"decided_at": T}}, rec(0, 0.6),
                                             rec(1, 0.6, {"XAUT_IRT": 0.35, "USDT_IRT": 0.65}),
                                             rec(2, 0.0, valid=False)])
        self.assertEqual(sym, "BTC_IRT")
        h = 15.5 * 3600
        self.assertEqual(core, [(T + h, {"BTC_IRT": 0.6}), (T + D + h, {"BTC_IRT": 0.6}), (T + 2 * D + h, {})])
        self.assertEqual(shadow, [(T + h, {}), (T + D + h, {"XAUT_IRT": 0.35})])   # an invalid shadow adds nothing

    def test_a_decision_applies_at_the_next_close_and_the_last_one_before_it_wins(self):
        days = [T + i * D for i in range(3)]
        prices = {"BTC_IRT": {days[0]: 100.0, days[1]: 110.0, days[2]: 121.0}}
        eq = mr.replay([(days[0] - 3600, {"BTC_IRT": 1.0}), (days[0] - 60, {})], prices, days)
        self.assertEqual(eq[-1][1], 1.0)                          # the later decision (USDT) won
        eq = mr.replay([(days[0] + 3600, {"BTC_IRT": 1.0})], prices, days)
        self.assertEqual(eq[0][0], days[1])                       # bought at the close after the decision
        self.assertAlmostEqual(eq[-1][1], (1 - mr.COST_USDT_ROUTE) * 1.1)

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

    def test_compare_prices_in_usdt_from_the_first_close_after_the_trial_began(self):
        fetch = fetcher({"BTC_USDT": [100, 110, 99, 121], "XAUT_IRT": [2000, 2000, 2200, 2200],
                         "USDT_IRT": [10, 10, 10, 10]})
        records = [rec(0, 0.6, {"XAUT_IRT": 0.5, "USDT_IRT": 0.5})]
        c = mr.compare(records, fetch, now=T + 3 * D + 100)
        self.assertEqual(c["end"], T + 3 * D)
        self.assertAlmostEqual(c["btc"][0], 0.1)                  # 110 at the first close after it -> 121
        self.assertGreater(c["shadow"][0], 0.0)                   # gold +10%
        self.assertEqual(c["core_now"], {"BTC_IRT": 0.6})
        self.assertIsNone(mr.compare([], fetch, now=T))
        early = mr.compare(records, fetch, now=T + 20 * 3600)     # no close since the first decision yet
        self.assertIsNone(early["core"])
        partial = mr.compare([rec(0, 0.6, {"NEW_IRT": 0.5})], fetch, now=T + 3 * D)   # NEW_IRT has no prices
        self.assertIsNotNone(partial["shadow"])

    def test_labels_are_the_tehran_day_that_ended(self):
        close = 1793392200 + D                                    # Bitpin's daily close at Tehran midnight
        self.assertEqual(mr.day_label(close), "2026-10-31")


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
