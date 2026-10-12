import unittest

from bitpin.strategy_data import build_episodes, validate_bars


DAY = 86400


def candles(count=70, scale=1.0):
    return [{"ts": i * DAY, "open": (100 + i) * scale,
             "close": (100.5 + i) * scale, "high": (101 + i) * scale,
             "low": (99 + i) * scale, "volume": 10 + i}
            for i in range(count)]


class StrategyDataTests(unittest.TestCase):
    def test_quote_conversion_rejects_incompatible_original_units(self):
        for panel,quote in (({'BTC_USDT':candles(),'USDT_IRT':candles(scale=.1)},'USDT_IRT'),
                            ({'BTC_IRT':candles(),'ETH_IRT':candles(scale=.1)},'ETH_IRT')):
            with self.assertRaises(ValueError):
                build_episodes(panel,quote_symbol=quote,cutoff=70*DAY)

    def test_inputs_end_before_entry_and_outcome_does_not_change_input(self):
        rows = candles()
        episodes = build_episodes({"BTC_IRT": rows}, cutoff=70 * DAY)
        first = episodes[0]
        self.assertEqual(first["decision_time"], 60 * DAY)
        self.assertEqual(first["label_end"], 61 * DAY)
        self.assertEqual(first["return_currency"], "IRT")
        self.assertAlmostEqual(first["realized_return"], 161 / 160 - 1)
        self.assertEqual(first["label"], 1)
        self.assertEqual(len(first["history"]), 60)
        self.assertEqual(first["history"][-1]["close"], 1.0)
        self.assertNotIn("symbol", first["history"][0])
        self.assertNotIn("ts", first["history"][0])
        changed = [dict(x) for x in rows]
        changed[61].update(open=200, high=201, close=200, low=199)
        future = build_episodes({"BTC_IRT": changed}, cutoff=70 * DAY)[0]
        self.assertEqual(first["history"], future["history"])
        self.assertEqual(first["features"], future["features"])
        self.assertEqual(first["input_hash"], future["input_hash"])
        self.assertEqual(future["label"], 2)

    def test_forming_candle_and_future_label_are_excluded(self):
        episodes = build_episodes({"BTC_IRT": candles()}, cutoff=69 * DAY + 1)
        self.assertEqual(episodes[-1]["label_end"], 68 * DAY)
        self.assertTrue(all(x["label_end"] < 69 * DAY for x in episodes))

    def test_gap_in_history_or_outcome_never_forward_fills(self):
        rows = candles(64)
        del rows[61]
        self.assertEqual(build_episodes({"BTC_IRT": rows}, cutoff=64 * DAY), [])

    def test_synchronized_quote_removes_exchange_rate_return(self):
        rows = candles()
        quote = candles(scale=.1)
        episodes = build_episodes({"BTC_IRT": rows, "USDT_IRT": quote},
                                  cutoff=70 * DAY, quote_symbol="USDT_IRT")
        self.assertEqual({x["symbol"] for x in episodes}, {"BTC_IRT"})
        self.assertEqual(episodes[0]["return_currency"], "USDT")
        self.assertAlmostEqual(episodes[0]["realized_return"], 0)
        self.assertAlmostEqual(episodes[0]["features"]["return_30"], 0)
        del quote[61]
        self.assertEqual(build_episodes({"BTC_IRT": rows[:64], "USDT_IRT": quote},
                                       cutoff=64 * DAY, quote_symbol="USDT_IRT"), [])

    def test_invalid_ohlc_nonfinite_illiquid_and_duplicate_are_rejected(self):
        rows = candles(6)
        rows[0]["high"] = 1
        rows[1]["volume"] = 0
        rows[2]["close"] = float("nan")
        rows.append(dict(rows[3]))
        clean = validate_bars(rows, step=DAY, cutoff=6 * DAY)
        self.assertEqual([b["ts"] for b in clean], [4 * DAY, 5 * DAY])

    def test_provenance_identifies_reconstructed_history(self):
        panel = {"BTC_IRT": {"bars": candles(), "receipt": {"source_url": "https://example.org/public",
                  "received_at": 70 * DAY, "sha256": "a" * 64}}}
        episode = build_episodes(panel, cutoff=70 * DAY)[0]
        self.assertTrue(episode["provenance"]["historical_reconstruction"])
        self.assertEqual(episode["provenance"]["source_receipts"][0]["sha256"], "a" * 64)

    def test_a_corrupt_duplicate_cannot_leave_an_apparently_clean_timestamp(self):
        rows = candles(3)
        rows.append(dict(rows[1], high=1))
        rows[2]["volume"] = True
        self.assertEqual([row["ts"] for row in validate_bars(rows, cutoff=3 * DAY)], [0])

    def test_nonfinite_cutoff_cannot_bypass_forming_candle_gate(self):
        for cutoff in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                build_episodes({"BTC_IRT": candles()}, cutoff=cutoff)


if __name__ == "__main__":
    unittest.main()
