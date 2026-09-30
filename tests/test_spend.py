"""bitpin.spend: the LLM cost meter (v3 B4), on synthetic usage logs only - no network, no keys."""
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import spend  # noqa: E402
from bitpin.spend import (DEFAULT_PRICES, LLM_USAGE_LOG, NEWS_USAGE_LOG, SPEND_FILE, cached_tokens,  # noqa: E402
                          cost_usd, format_usd, prices_for, prices_from_config, quota_message_fa, quota_streak,
                          read_records, read_summary, refresh, summary)

logging.getLogger("bitpin").addHandler(logging.NullHandler())

T0 = 1790074800.0           # 2026-09-22 11:00 UTC
DAY = 86400.0
K3 = DEFAULT_PRICES["llm"]
K26 = DEFAULT_PRICES["news"]


def rec(t, prompt=1000, completion=100, model="kimi-k3", usd=None, stage=None, error=None, cached=None, **extra):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    if cached is not None:
        usage["cached_tokens"] = cached
    if error:
        usage = {}
    r = {"time": t, "model": model, "usage": usage}
    if usd is not None:
        r["usd"] = usd
    if stage:
        r["stage"] = stage
    if error:
        r["error"] = error
    r.update(extra)
    return r


class TestPricing(unittest.TestCase):
    def test_cost_of_one_kimi_k3_decision(self):
        # the live decision measured in the year review: 31k prompt + 5.2k completion, no cache hit
        usd = cost_usd({"prompt_tokens": 31000, "completion_tokens": 5200, "total_tokens": 36200}, K3)
        self.assertAlmostEqual(usd, 31000 * 3.0 / 1e6 + 5200 * 15.0 / 1e6, places=6)     # $0.171
        self.assertEqual(usd, 0.171)

    def test_cached_input_is_billed_at_the_cached_price(self):
        u = {"prompt_tokens": 10000, "completion_tokens": 0, "total_tokens": 10000, "cached_tokens": 8000}
        self.assertAlmostEqual(cost_usd(u, K3), (2000 * 3.0 + 8000 * 0.30) / 1e6, places=9)
        # OpenAI-style and DeepSeek-style places for the same number; never more than the prompt
        self.assertEqual(cached_tokens({"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 4}}), 4)
        self.assertEqual(cached_tokens({"prompt_tokens": 10, "prompt_cache_hit_tokens": 7}), 7)
        self.assertEqual(cached_tokens({"prompt_tokens": 10, "cached_tokens": 50}), 10)
        self.assertEqual(cached_tokens({"prompt_tokens": 10, "cached_tokens": -3}), 0)
        self.assertEqual(cached_tokens({"cached_tokens": True}), 0)
        self.assertEqual(cached_tokens("nope"), 0)

    def test_odd_usages_cost_something_or_nothing_but_never_raise(self):
        self.assertEqual(cost_usd({}, K3), 0.0)
        self.assertEqual(cost_usd(None, K3), 0.0)
        self.assertEqual(cost_usd({"prompt_tokens": 100}, None), 0.0)
        # total_tokens only: billed as uncached input (the conservative reading)
        self.assertAlmostEqual(cost_usd({"total_tokens": 1000000}, K3), 3.0, places=9)
        self.assertEqual(cost_usd({"prompt_tokens": float("nan"), "completion_tokens": "x"}, K3), 0.0)
        self.assertEqual(cost_usd({"prompt_tokens": -5, "completion_tokens": -1}, K3), 0.0)

    def test_prices_from_a_config_section_with_fallbacks(self):
        p = prices_from_config({"price_in_per_m": 2.5, "price_out_per_m": 12, "price_cached_in_per_m": 0.2}, "llm")
        self.assertEqual(p, {"in": 2.5, "out": 12.0, "cached_in": 0.2})
        # a missing / broken key falls back to the stage's default, never raises
        self.assertEqual(prices_from_config({"price_in_per_m": "3"}, "llm"), K3)
        self.assertEqual(prices_from_config({"price_out_per_m": -1, "price_in_per_m": 5000}, "news"), K26)
        self.assertEqual(prices_from_config(None, "news"), K26)
        self.assertEqual(prices_from_config({}, "unknown-stage"), K3)

    def test_a_kimi_k2_model_is_priced_as_news_wherever_it_was_called(self):
        self.assertEqual(prices_for("llm", "kimi-k3"), K3)
        self.assertEqual(prices_for("llm", "kimi-k2.6"), K26)
        self.assertEqual(prices_for("news", "kimi-k2.6"), K26)
        self.assertEqual(prices_for("news", None), K26)
        self.assertEqual(prices_for("bogus", None), K3)
        custom = {"llm": {"in": 1, "out": 2, "cached_in": 0.5}, "news": {"in": 9, "out": 9, "cached_in": 9}}
        self.assertEqual(prices_for("llm", "kimi-k3", custom), custom["llm"])
        self.assertEqual(prices_for("llm", "KIMI-K2-thinking", custom), custom["news"])
        self.assertEqual(prices_for("llm", "kimi-k3", {"llm": "broken"}), K3)


class TestSummary(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="spendtest_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, records):
        with open(os.path.join(self.dir, name), "a", encoding="utf-8") as f:
            for r in records:
                f.write((json.dumps(r) if isinstance(r, dict) else r) + "\n")

    def test_today_month_total_and_per_day_buckets(self):
        now = T0 + 5 * 3600                       # 2026-09-22 16:00 UTC
        self.write(LLM_USAGE_LOG, [
            rec(T0 - 40 * DAY, usd=0.5),          # August: total only
            rec(T0 - 3 * DAY, usd=0.25),          # September, not today
            rec(T0, usd=0.171),                   # today
            rec(T0 + 3600, usd=0.1, usage_estimated=True),
        ])
        self.write(NEWS_USAGE_LOG, [rec(T0 + 60, prompt=11400, completion=700, model="kimi-k2.6", usd=0.0142)])
        s = summary(self.dir, now)
        self.assertEqual((s["day"], s["month"]), ("2026-09-22", "2026-09"))
        self.assertAlmostEqual(s["today_usd"], 0.171 + 0.1 + 0.0142, places=6)
        self.assertAlmostEqual(s["month_usd"], 0.25 + 0.171 + 0.1 + 0.0142, places=6)
        self.assertAlmostEqual(s["total_usd"], 0.5 + 0.25 + 0.171 + 0.1 + 0.0142, places=6)
        self.assertEqual((s["today_requests"], s["month_requests"], s["total_requests"]), (3, 4, 5))
        self.assertEqual(s["today_tokens"], 1100 + 1100 + 12100)
        self.assertEqual(s["records"], 5)
        self.assertEqual(s["estimated_requests"], 1)
        self.assertEqual(s["since"], T0 - 40 * DAY)
        self.assertAlmostEqual(s["by_stage"]["news"]["today_usd"], 0.0142, places=6)
        self.assertEqual(s["by_stage"]["news"]["total_requests"], 1)
        self.assertAlmostEqual(s["by_stage"]["llm"]["total_usd"], 1.021, places=6)
        self.assertEqual(sorted(s["days"]), ["2026-08-13", "2026-09-19", "2026-09-22"])
        self.assertEqual(s["days"]["2026-09-22"]["requests"], 3)
        self.assertFalse(s["quota_alert"])
        self.assertEqual((s["quota_errors_in_a_row"], s["last_quota_error_at"]), (0, None))

    def test_records_without_usd_are_priced_with_the_defaults_of_their_stage_and_model(self):
        # an older version's record (no "usd", no "stage") in the stage-2 log; a k2.6 call from stage 2
        self.write(LLM_USAGE_LOG, [rec(T0, prompt=1000000, completion=0), rec(T0, prompt=1000000, model="kimi-k2.6")])
        self.write(NEWS_USAGE_LOG, [rec(T0, prompt=0, completion=1000000, model="kimi-k2.6")])
        s = summary(self.dir, T0)
        self.assertAlmostEqual(s["by_stage"]["llm"]["today_usd"], 3.0 + (1.0 + 100 * 4.0 / 1e6), places=6)
        self.assertAlmostEqual(s["by_stage"]["news"]["today_usd"], 4.0, places=6)
        # explicit prices for the unpriced records; a priced record keeps its own number
        self.write(LLM_USAGE_LOG, [rec(T0, prompt=1000000, completion=0, usd=0.001)])
        s2 = summary(self.dir, T0, prices={"llm": {"in": 1, "out": 1, "cached_in": 1}, "news": K26})
        self.assertAlmostEqual(s2["by_stage"]["llm"]["today_usd"], 1.0 + (1.0 + 100 * 4.0 / 1e6) + 0.001, places=6)

    def test_broken_lines_and_odd_records_are_skipped_not_fatal(self):
        self.write(LLM_USAGE_LOG, ["not json", "[1, 2]", json.dumps(rec(T0, usd=0.2)), "", json.dumps({"usage": {}}),
                                   json.dumps(rec(-5, usd=9.0)), json.dumps({"time": "yesterday", "usd": 9.0})])
        s = summary(self.dir, T0)
        self.assertEqual(s["records"], 1)
        self.assertEqual(s["skipped_lines"], 5)
        self.assertAlmostEqual(s["total_usd"], 0.2, places=9)
        # no logs at all, or no state dir: an empty summary, never an exception
        empty = summary(tempfile.mkdtemp(dir=self.dir), T0)
        self.assertEqual((empty["records"], empty["total_usd"], empty["days"]), (0, 0.0, {}))
        self.assertEqual(summary(None, T0)["records"], 0)

    def test_only_the_tail_of_a_huge_log_is_read(self):
        path = os.path.join(self.dir, LLM_USAGE_LOG)
        line = json.dumps(rec(T0, usd=0.001)) + "\n"
        with open(path, "w", encoding="utf-8") as f:
            for _ in range(2000):
                f.write(line)
        records, skipped = read_records(path, "llm", max_bytes=len(line) * 10 + 5)
        self.assertLessEqual(len(records), 11)
        self.assertGreaterEqual(len(records), 9)
        self.assertEqual(skipped, 0)                 # the partial first line is dropped, not counted
        self.assertEqual(records[0]["stage"], "llm")
        self.assertEqual(read_records(os.path.join(self.dir, "missing.jsonl")), ([], 0))

    def test_quota_streak_counts_trailing_errors_of_both_stages(self):
        recs = [rec(T0, usd=0.1), rec(T0 + 10, error="llm_quota"), rec(T0 + 20, error="news_quota", stage="news")]
        self.assertEqual(quota_streak(recs), (2, T0 + 20))
        self.assertEqual(quota_streak(recs + [rec(T0 + 30, usd=0.1)]), (0, None))
        self.assertEqual(quota_streak([rec(T0, error="llm_quota")]), (1, T0))
        self.assertEqual(quota_streak([]), (0, None))
        # the summary carries it, error records cost nothing and count as records
        self.write(LLM_USAGE_LOG, [recs[0], recs[1]])
        self.write(NEWS_USAGE_LOG, [recs[2]])
        s = summary(self.dir, T0 + 60)
        self.assertEqual((s["quota_errors_in_a_row"], s["last_quota_error_at"], s["quota_alert"]), (2, T0 + 20, True))
        self.assertEqual((s["records"], s["total_requests"]), (3, 1))
        self.assertAlmostEqual(s["total_usd"], 0.1, places=9)
        one = summary(tempfile.mkdtemp(dir=self.dir), T0)
        self.assertFalse(one["quota_alert"])

    def test_refresh_writes_the_spend_file_and_read_summary_reads_it_back(self):
        self.write(LLM_USAGE_LOG, [rec(T0, usd=0.171)])
        s = refresh(self.dir, T0)
        path = os.path.join(self.dir, SPEND_FILE)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["today_usd"], s["today_usd"])
        self.assertEqual(on_disk["updated_at"], T0)
        self.assertEqual(read_summary(self.dir)["total_usd"], 0.171)
        self.assertEqual(read_summary(tempfile.mkdtemp(dir=self.dir)), {})
        self.assertEqual(read_summary(None), {})
        self.assertEqual(refresh(None, T0), {})
        # an unreadable spend file: {} (never an exception for the notifier)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(read_summary(self.dir), {})

    def test_refresh_survives_a_write_failure(self):
        self.write(LLM_USAGE_LOG, [rec(T0, usd=0.1)])
        os.makedirs(os.path.join(self.dir, SPEND_FILE))        # a directory where the file should go
        s = refresh(self.dir, T0)
        self.assertEqual(s["records"], 1)                      # computed and returned anyway


class TestMessages(unittest.TestCase):
    def test_quota_message_fa(self):
        self.assertEqual(quota_message_fa(7), "اعتبار Moonshot تمام شده؛ تا 7 ساعت دیگر derisk")
        self.assertEqual(quota_message_fa(2.5), "اعتبار Moonshot تمام شده؛ تا 2.5 ساعت دیگر derisk")
        self.assertEqual(quota_message_fa(11.6), "اعتبار Moonshot تمام شده؛ تا 12 ساعت دیگر derisk")
        self.assertEqual(quota_message_fa(-1), "اعتبار Moonshot تمام شده؛ تا 0 ساعت دیگر derisk")
        self.assertEqual(quota_message_fa(None), "اعتبار Moonshot تمام شده؛ شارژ کنید")
        self.assertEqual(quota_message_fa("soon"), "اعتبار Moonshot تمام شده؛ شارژ کنید")

    def test_format_usd(self):
        self.assertEqual(format_usd(0.171), "$0.17")
        self.assertEqual(format_usd(None), "$0.00")
        self.assertEqual(format_usd(123.4), "$123")

    def test_module_constants(self):
        self.assertEqual(spend.QUOTA_ALERT_STREAK, 2)
        self.assertEqual(spend.LLM_USAGE_LOG, "kimi_usage.jsonl")
        self.assertEqual(spend.NEWS_USAGE_LOG, "news_usage.jsonl")


if __name__ == "__main__":
    unittest.main()
