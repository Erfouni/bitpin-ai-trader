import importlib
import importlib.util
import http.client
import io
import json
import os
import tempfile
import unittest

from test_flow_features import liquidity, trade


class FlowShadowTests(unittest.TestCase):
    def setUp(self):
        if importlib.util.find_spec("bitpin.flow_shadow") is None:
            self.fail("persistent shadow collection is not implemented")
        self.m = importlib.import_module("bitpin.flow_shadow")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "flows.sqlite3")
        self.store = self.m.ShadowStore(self.db)
        self.addCleanup(lambda: self.store.close())

    def seed_window(self):
        self.store.ingest_trades("BTC_USDT", [trade(ts=999)], 1000)
        for ts in range(1030, 1331, 30):
            self.store.ingest_trades("BTC_USDT", [trade(ts=999), trade("b", "buy", "300", 1020),
                                                 trade("c", "sell", "100", 1021)], ts)

    def view(self, now=1330):
        return self.store.snapshot(["BTC_USDT"], now)["markets"]["BTC_USDT"]

    def test_window_warms_then_reports_literal_taker_delta(self):
        self.store.ingest_trades("BTC_USDT", [trade(ts=999)], 1000)
        first = self.view(1000)["windows"]["300"]
        self.assertEqual(first["quality"], "warming_up")
        self.assertIsNone(first["delta_quote"])
        for ts in range(1030, 1301, 30):
            self.store.ingest_trades("BTC_USDT", [trade(ts=999), trade("b", "buy", "300", 1020),
                                                 trade("c", "sell", "100", 1021)], ts)
        window = self.view(1300)["windows"]["300"]
        self.assertEqual(window["quality"], "ready")
        self.assertEqual(window["delta_quote"], 200)
        self.assertEqual(window["buy_ratio"], 0.75)
        self.assertEqual(window["trade_count"], 2)
        self.assertIsNone(self.view(1300)["windows"]["900"]["delta_quote"])

    def test_replay_and_restart_never_double_count(self):
        self.seed_window()
        self.store.close(); self.store = self.m.ShadowStore(self.db)
        self.store.ingest_trades("BTC_USDT", [trade("c", "sell", "100", 1021),
                                            trade("b", "buy", "300", 1020), trade(ts=999)], 1331)
        self.assertEqual(self.view(1331)["total_unique_trades"], 3)
        self.assertEqual(self.view(1331)["windows"]["300"]["trade_count"], 0)
        self.assertEqual(self.view(1331)["windows"]["300"]["quality"], "ready")

    def test_no_overlap_or_long_pause_resets_coverage(self):
        self.seed_window()
        self.store.ingest_trades("BTC_USDT", [trade("new", ts=1331)], 1332)
        self.assertEqual(self.view(1332)["windows"]["300"]["quality"], "warming_up")
        self.assertEqual(self.view(1332)["trades"]["gap_count"], 1)
        self.store.ingest_trades("BTC_USDT", [trade("new", ts=1331)], 2000)
        self.assertEqual(self.view(2000)["trades"]["gap_count"], 2)

    def test_failure_and_stale_data_never_emit_zero_pressure(self):
        self.seed_window()
        self.assertEqual(self.view(1500)["windows"]["300"]["quality"], "stale")
        self.assertIsNone(self.view(1500)["windows"]["300"]["delta_quote"])
        self.store.fail("trades", "BTC_USDT", 1331, "network failure")
        self.assertEqual(self.view(1331)["windows"]["300"]["quality"], "error")
        self.store.ingest_trades("BTC_USDT", [trade("c", "sell", "100", 1021)], 1340)
        self.assertEqual(self.view(1340)["windows"]["300"]["quality"], "warming_up")

    def test_conflict_rejects_entire_batch_without_partial_insert(self):
        self.store.ingest_trades("BTC_USDT", [trade(ts=999)], 1000)
        with self.assertRaises(ValueError):
            self.store.ingest_trades("BTC_USDT", [trade("new", ts=1000), trade(side="sell", ts=999)], 1001)
        self.assertEqual(self.view(1001)["total_unique_trades"], 1)

    def test_source_failure_isolated_and_macro_cached(self):
        class Client:
            macro_calls = 0
            def matches(self, symbol):
                if symbol == "ETH_USDT": raise OSError("unavailable")
                return [trade(ts=999)]
            def orderbook(self, symbol):
                return {"bids": [["99", "2"]], "asks": [["101", "2"]]}
            def stablecoins(self):
                self.macro_calls += 1
                return liquidity()
        client = Client(); clock = [1000]
        collector = self.m.ShadowCollector(self.store, client, clock=lambda: clock[0])
        snap = collector.collect(["BTC_USDT", "ETH_USDT"])
        self.assertEqual(snap["mode"], "shadow")
        self.assertFalse(snap["live_enabled"])
        self.assertEqual(snap["markets"]["ETH_USDT"]["trades"]["status"], "error")
        self.assertEqual(snap["markets"]["BTC_USDT"]["trades"]["status"], "ok")
        self.assertEqual(snap["liquidity"]["data"]["combined_change_1d_units"], 0)
        clock[0] = 1030; collector.collect(["BTC_USDT"])
        self.assertEqual(client.macro_calls, 1)

    def test_empty_first_buffer_and_clock_rollback_are_not_ready(self):
        self.store.ingest_trades("BTC_USDT", [], 1000)
        self.assertEqual(self.view(1000)["windows"]["300"]["quality"], "missing")
        self.seed_window()
        self.assertIsNone(self.view(1000)["windows"]["300"]["delta_quote"])

    def test_persistent_retry_after_suppresses_requests(self):
        from bitpin.flow_public import PublicDataError
        class Client:
            calls = 0
            def matches(self, symbol):
                self.calls += 1
                raise PublicDataError("limited", 120)
            def orderbook(self, symbol):
                return {"bids": [["99", "2"]], "asks": [["101", "2"]]}
            def stablecoins(self): return liquidity()
        client = Client(); now = [1000]
        self.m.ShadowCollector(self.store, client, lambda: now[0]).collect(["BTC_USDT"])
        self.store.close(); self.store = self.m.ShadowStore(self.db)
        now[0] = 1030
        self.m.ShadowCollector(self.store, client, lambda: now[0]).collect(["BTC_USDT"])
        self.assertEqual(client.calls, 1)
        now[0] = 1121
        self.m.ShadowCollector(self.store, client, lambda: now[0]).collect(["BTC_USDT"])
        self.assertEqual(client.calls, 2)

    def test_receipt_age_does_not_claim_provider_freshness(self):
        self.store.ingest_liquidity(liquidity(), 1000)
        rec = self.store.snapshot([], 9000)["liquidity"]
        self.assertEqual(rec["status"], "stale")
        self.assertIsNone(rec["data"]["provider_time"])

    def test_truncated_http_stream_records_error_and_other_sources_continue(self):
        from bitpin.flow_public import PublicFlowClient
        class Socket:
            def makefile(self, mode):
                return io.BytesIO(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\n[]")
        class Opener:
            def open(self, req, timeout):
                if "/matches/" in req.full_url:
                    response = http.client.HTTPResponse(Socket())
                    response.begin()
                    return response
                data = liquidity() if "llama.fi" in req.full_url else {
                    "bids": [["99", "2"]], "asks": [["101", "2"]]}
                return io.BytesIO(json.dumps(data).encode("utf-8"))
        client = PublicFlowClient(opener=Opener())
        snap = self.m.ShadowCollector(self.store, client, lambda: 1000).collect(["BTC_USDT"])
        self.assertEqual(snap["markets"]["BTC_USDT"]["trades"]["status"], "error")
        self.assertIn("IncompleteRead", snap["markets"]["BTC_USDT"]["trades"]["error"])
        self.assertEqual(snap["markets"]["BTC_USDT"]["book"]["status"], "ok")
        self.assertEqual(snap["liquidity"]["status"], "ok")
        self.assertIsNone(snap["markets"]["BTC_USDT"]["windows"]["300"]["delta_quote"])

    def test_failure_recovery_and_coverage_history_survive_reopen_and_expire(self):
        self.store.ingest_trades("BTC_USDT", [trade(ts=999)], 1000)
        self.store.fail("trades", "BTC_USDT", 1030, "network failure")
        self.store.ingest_trades("BTC_USDT", [trade(ts=999)], 1060)
        self.store.ingest_trades("BTC_USDT", [trade("new", ts=1061)], 1090)
        self.store.ingest_trades("BTC_USDT", [trade("new", ts=1061)], 1200)
        self.store.close(); self.store = self.m.ShadowStore(self.db)
        events = [json.loads(row[0]) for row in self.store.db.execute(
            "SELECT payload FROM observations WHERE source='trades' ORDER BY received")]
        self.assertEqual([event["event"] for event in events],
                         ["coverage_reset", "source_error", "coverage_reset", "coverage_reset", "coverage_reset"])
        self.assertEqual(events[1]["error"], "network failure")
        self.assertEqual(events[1]["previous_received_at"], 1000)
        self.assertEqual(events[2]["reasons"], ["source_recovery"])
        self.assertEqual(events[3]["reasons"], ["buffer_no_overlap"])
        self.assertEqual(events[4]["reasons"], ["poll_gap"])
        self.assertEqual(self.store.state("trades", "BTC_USDT")["status"], "ok")
        self.assertIsNone(self.store.state("trades", "BTC_USDT")["error"])
        self.store.prune(1201 + self.m.RETENTION)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0], 0)


if __name__ == "__main__": unittest.main()
