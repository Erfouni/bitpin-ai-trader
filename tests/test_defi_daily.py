"""Daily public facts: hand-computed windows, unavailable data, durable isolation."""
import copy
import importlib
import importlib.util
import json
import io
import os
from pathlib import Path
import tempfile
import unittest

DAY = 86400
TODAY = 20000 * DAY
NOW = TODAY + 12 * 3600


def chart(metric="dex_volume", days=61):
    points = [[TODAY - (days - i) * DAY, 100.0] for i in range(days)]
    if metric == "tvl":
        return [{"date": t, "tvl": v} for t, v in points]
    if metric == "stablecoin_supply":
        return [{"date": str(t), "totalCirculating": {"peggedUSD": v, "peggedEUR": 900},
                 "totalCirculatingUSD": {"peggedUSD": 77, "peggedEUR": 1000}} for t, v in points]
    return {"totalDataChart": points, "total24h": 999999, "total7d": 99999999,
            "protocols": [{"slug": "example", "chains": ["Ethereum"]}]}


class DailyFeaturesTests(unittest.TestCase):
    def setUp(self):
        if importlib.util.find_spec("bitpin.defi_daily") is None:
            self.fail("daily DefiLlama shadow features are not implemented")
        self.m = importlib.import_module("bitpin.defi_daily")

    def test_volume_excludes_today_and_compares_disjoint_complete_weeks(self):
        raw = chart()
        for row in raw["totalDataChart"][-7:]: row[1] = 200
        raw["totalDataChart"].append([TODAY, 99999999])
        f = self.m.daily_features(raw, "dex_volume", NOW)
        self.assertEqual(f["as_of"], TODAY - DAY)
        self.assertEqual(f["value"], 200)
        self.assertEqual(f["mean_7d"], 200)
        self.assertEqual(f["sum_7d"], 1400)
        self.assertEqual(f["previous_mean_7d"], 100)
        self.assertEqual(f["week_over_week_pct"], 100)
        self.assertAlmostEqual(f["mean_30d"], 3700 / 30)
        self.assertEqual(f["excluded_open_day_points"], 1)

    def test_stock_changes_are_levels_not_summed_open_interest(self):
        raw = chart("open_interest")
        raw["totalDataChart"][-1][1] = 120
        f = self.m.daily_features(raw, "open_interest", NOW)
        self.assertEqual(f["change_1d_pct"], 20)
        self.assertEqual(f["change_7d_pct"], 20)
        self.assertNotIn("sum_7d", f)
        self.assertNotIn("mean_7d", f)

    def test_tvl_changes_keep_price_effect_warning(self):
        raw = chart("tvl"); raw[-1]["tvl"] = 80
        f = self.m.daily_features(raw, "tvl", NOW)
        self.assertEqual(f["change_30d_pct"], -20)
        self.assertEqual(f["unit"], "USD")
        self.assertFalse(f["is_net_inflow"])

    def test_stablecoin_supply_never_sums_foreign_currency_units_or_prices(self):
        f = self.m.daily_features(chart("stablecoin_supply"), "stablecoin_supply", NOW)
        self.assertEqual(f["value"], 100)
        self.assertEqual(f["unit"], "USD-pegged token units")

    def test_missing_days_remain_unknown_instead_of_zero_or_forward_fill(self):
        raw = chart(); del raw["totalDataChart"][-4]
        f = self.m.daily_features(raw, "dex_volume", NOW)
        self.assertIsNone(f["mean_7d"])
        self.assertIsNone(f["week_over_week_pct"])
        self.assertEqual(f["coverage_7d"], 6)
        self.assertEqual(f["value"], 100)

    def test_negative_fees_are_valid_but_negative_volume_is_not(self):
        raw = chart(); raw["totalDataChart"][-1][1] = -20
        self.assertEqual(self.m.daily_features(raw, "fees", NOW)["value"], -20)
        with self.assertRaises(ValueError): self.m.daily_features(raw, "dex_volume", NOW)

    def test_zero_baselines_do_not_create_infinite_percentages(self):
        raw = chart("tvl")
        for row in raw: row["tvl"] = 0
        raw[-1]["tvl"] = 100
        f = self.m.daily_features(raw, "tvl", NOW)
        self.assertIsNone(f["change_1d_pct"])

    def test_tiny_positive_baseline_never_emits_infinity_or_aborts_storage(self):
        raw = chart("tvl"); raw[-2]["tvl"] = 1e-320
        f = self.m.daily_features(raw, "tvl", NOW)
        self.assertIsNone(f["change_1d_pct"])
        json.dumps(f, allow_nan=False)

    def test_duplicate_invalid_future_or_nonfinite_points_are_rejected(self):
        for point in ([TODAY - DAY, 5], [TODAY + DAY, 4], [TODAY - 1, 4],
                      [TODAY - 2 * DAY, float("nan")], [True, 4], [TODAY - DAY, True]):
            raw = chart(); raw["totalDataChart"].append(point)
            with self.subTest(point=point), self.assertRaises(ValueError):
                self.m.daily_features(raw, "dex_volume", NOW)

    def test_empty_history_and_old_history_are_not_healthy(self):
        with self.assertRaises(ValueError): self.m.daily_features({"totalDataChart": []}, "fees", NOW)
        f = self.m.daily_features(chart(), "fees", NOW + 5 * DAY)
        self.assertEqual(f["quality"], "stale")


class DailyCollectorTests(unittest.TestCase):
    def setUp(self):
        if importlib.util.find_spec("bitpin.defi_daily") is None:
            self.fail("isolated daily DefiLlama collector is not implemented")
        self.m = importlib.import_module("bitpin.defi_daily")
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = self.m.prepare_directory(Path(self.tmp.name) / "defi")
        self.store = self.m.DailyStore(self.directory / "daily.sqlite3")
        self.addCleanup(lambda: self.store.close())
        self.specs = self.m.source_specs([])

    def collect(self, client, now=NOW):
        return self.m.DailyCollector(self.store, client, clock=lambda: now, sleeper=lambda x: None).collect(self.specs)

    def client(self, errors=None):
        module = self.m
        class Client:
            def get_json(self, url):
                spec = next(s for s in module.source_specs() if s["url"] == url)
                if errors and spec["metric"] in errors: raise errors[spec["metric"]]
                return chart(spec["metric"])
        return Client()

    def test_persists_observations_and_reports_no_trading_connection(self):
        snap = self.collect(self.client())
        self.assertEqual(snap["mode"], "shadow")
        self.assertFalse(snap["live_enabled"])
        self.assertEqual(snap["model_calls"], 0)
        self.assertEqual(len(snap["sources"]), 6)
        self.assertTrue(all(s["status"] == "ok" for s in snap["sources"].values()))
        self.assertEqual(self.store.observation_count(), 6)

    def test_restart_cache_avoids_refetch_and_expired_receipts_are_stale(self):
        self.collect(self.client())
        self.store.close(); self.store = self.m.DailyStore(self.directory / "daily.sqlite3")
        class NoFetch:
            def get_json(self, url): raise AssertionError("hourly cache was ignored")
        snap = self.collect(NoFetch(), NOW + 1800)
        self.assertTrue(all(s["status"] == "ok" for s in snap["sources"].values()))
        snap = self.store.snapshot(self.specs, NOW + 10000)
        self.assertTrue(all(s["status"] == "stale" for s in snap["sources"].values()))

    def test_one_bad_source_does_not_stop_other_sources_or_become_zero(self):
        snap = self.collect(self.client({"tvl": ValueError("bad schema")}))
        self.assertEqual(snap["sources"]["tvl:global"]["status"], "error")
        self.assertNotIn("features", snap["sources"]["tvl:global"])
        self.assertEqual(snap["sources"]["dex_volume:global"]["status"], "ok")

    def test_rate_limit_is_durable_and_shared_by_host(self):
        from bitpin.flow_public import PublicDataError
        self.collect(self.client({"tvl": PublicDataError("limited", 7200, 429)}))
        class RateLimitedHost:
            def get_json(self, url):
                if "api.llama.fi" in url: raise AssertionError("host cooldown bypassed")
                return chart("stablecoin_supply")
        self.store.close(); self.store = self.m.DailyStore(self.directory / "daily.sqlite3")
        snap = self.collect(RateLimitedHost(), NOW + 3700)
        self.assertNotEqual(snap["sources"]["dex_volume:global"]["status"], "ok")
        self.assertEqual(snap["sources"]["stablecoin_supply:global"]["status"], "ok")

    def test_account_or_existing_flow_directory_is_rejected(self):
        for marker in ("LIVE_CONFIRMED", ".flow-shadow-state", "bitpin_token.json"):
            directory = Path(self.tmp.name) / marker.replace(".", "_")
            directory.mkdir(); (directory / marker).write_text("sentinel")
            with self.assertRaises(ValueError): self.m.prepare_directory(directory)

    def test_reports_cannot_be_consumed_by_live_flow_reader(self):
        from bitpin.flow_context import load_public_flow
        snap = self.collect(self.client())
        self.m.write_snapshot(snap, self.directory)
        self.assertFalse((self.directory / "snapshot.json").exists())
        self.assertEqual(load_public_flow(str(self.directory / "defi-daily.json"), NOW, ["BTC_IRT"])["status"],
                         "unavailable")

    def test_transport_rejects_paid_private_and_arbitrary_hosts_before_network(self):
        client = self.m.DefiClient()
        for url in ("https://pro-api.llama.fi/key/api/inflows/aave/1000", "https://example.com/",
                    "http://api.llama.fi/v2/historicalChainTvl", "https://api.llama.fi/v2/historicalChainTvl?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError): client.get_json(url)

    def test_deeply_nested_json_is_a_source_error(self):
        from bitpin.flow_public import PublicDataError
        class Opener:
            def open(self, *args, **kwargs):
                return io.BytesIO(b"[" * 2000 + b"0" + b"]" * 2000)
        client = self.m.DefiClient(opener=Opener())
        with self.assertRaises(PublicDataError): client.get_json(self.specs[0]["url"])

    def test_sparkline_preserves_calendar_gaps_and_excludes_older_points(self):
        render = importlib.import_module("bitpin.defi_report")
        adjacent = render.sparkline([[TODAY - 2 * DAY, 10], [TODAY - DAY, 20]])
        separated = render.sparkline([[TODAY - 20 * DAY, 10], [TODAY - DAY, 20]])
        self.assertNotEqual(adjacent, separated)
        self.assertNotIn("polyline", separated)
        self.assertEqual(render.sparkline([[TODAY - 60 * DAY, 1], [TODAY - 2 * DAY, 10], [TODAY - DAY, 20]]),
                         adjacent)

    def test_report_only_never_fetches_and_preserves_observations(self):
        if importlib.util.find_spec("scripts.defi_shadow") is None:
            self.fail("daily shadow CLI is not implemented")
        cli = importlib.import_module("scripts.defi_shadow")
        self.collect(self.client())
        class NoNetwork:
            def get_json(self, url): raise AssertionError("report-only attempted network")
        count = self.store.observation_count()
        self.assertEqual(cli.main(["--state-dir", str(self.directory), "--chains", "--report-only"],
                                  client=NoNetwork(), clock=lambda: NOW), 0)
        self.assertEqual(self.store.observation_count(), count)
        self.assertTrue((self.directory / "report.html").is_file())
        self.assertTrue((self.directory / "defi-daily.json").is_file())
        self.assertFalse((self.directory / "snapshot.json").exists())

    def test_parallel_collector_is_refused_without_changing_snapshot(self):
        if importlib.util.find_spec("scripts.defi_shadow") is None:
            self.fail("daily shadow CLI is not implemented")
        cli = importlib.import_module("scripts.defi_shadow")
        with self.m.DailyLock(self.directory):
            self.assertEqual(cli.main(["--state-dir", str(self.directory), "--chains"], client=self.client()), 2)
        self.assertEqual(self.store.observation_count(), 0)

    def test_report_escapes_untrusted_text_and_labels_missing_values(self):
        if importlib.util.find_spec("bitpin.defi_report") is None:
            self.fail("daily shadow report is not implemented")
        render = importlib.import_module("bitpin.defi_report").render_report
        snap = self.collect(self.client())
        snap["sources"]["tvl:global"]["scope"] = "<script>alert(1)</script>"
        snap["sources"]["tvl:global"]["status"] = "error"
        html = render(snap)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('dir="rtl"', html)
        self.assertIn("—", html)
