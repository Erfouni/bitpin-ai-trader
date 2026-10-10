import importlib
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_flow_features import liquidity, trade

ROOT = Path(__file__).resolve().parents[1]


class FlowCliTests(unittest.TestCase):
    def setUp(self):
        script = ROOT / "scripts" / "flow_shadow.py"
        if not script.exists() or importlib.util.find_spec("bitpin.flow_report") is None:
            self.fail("shadow CLI and report are not implemented")
        spec = importlib.util.spec_from_file_location("flow_cli", str(script))
        self.cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.cli)
        self.report = importlib.import_module("bitpin.flow_report")
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "shadow"
        output = contextlib.ExitStack()
        output.enter_context(contextlib.redirect_stdout(io.StringIO()))
        output.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(output.close)

    def test_live_and_credentials_flags_rejected_before_any_write(self):
        for flag in ("--live", "--keys-file", "--i-accept-the-risk", "--config"):
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/flow_shadow.py"),
                                     "--state-dir", str(self.path), flag, "anything"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(self.path.exists())

    def test_existing_foreign_directory_and_live_state_are_rejected(self):
        self.path.mkdir(); (self.path / "LIVE_CONFIRMED").write_text("do-not-read", encoding="utf-8")
        with self.assertRaises(ValueError): self.cli.prepare_directory(self.path)
        self.assertEqual(sorted(p.name for p in self.path.iterdir()), ["LIVE_CONFIRMED"])
        with self.assertRaises(ValueError): self.cli.prepare_directory(Path("/var/lib/bitpin-bot/flow-shadow"))

    def test_finite_run_uses_only_public_data_and_writes_honest_reports(self):
        class Client:
            def matches(self, symbol): return [trade(ts=999)]
            def orderbook(self, symbol): return {"bids": [["99", "2"]], "asks": [["101", "1"]]}
            def stablecoins(self): return liquidity()
        now = [1000]
        def sleep(seconds): now[0] += seconds
        result = self.cli.main(["--state-dir", str(self.path), "--symbols", "BTC_USDT",
                                "--cycles", "2", "--interval", "30"],
                               client=Client(), clock=lambda: now[0], sleeper=sleep)
        self.assertEqual(result, 0)
        snapshot = json.loads((self.path / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["mode"], "shadow")
        self.assertEqual(snapshot["orders_created"], 0)
        self.assertEqual(snapshot["markets"]["BTC_USDT"]["total_unique_trades"], 1)
        self.assertEqual(snapshot["markets"]["BTC_USDT"]["trades"]["success_count"], 2)
        self.assertIsNone(snapshot["markets"]["BTC_USDT"]["windows"]["300"]["delta_quote"])
        self.assertTrue((self.path / "report.html").exists())

    def test_failed_collection_reports_error_and_returns_nonzero(self):
        class Client:
            def matches(self, symbol): raise OSError("offline")
            def orderbook(self, symbol): raise OSError("offline")
            def stablecoins(self): raise OSError("offline")
        result = self.cli.main(["--state-dir", str(self.path), "--symbols", "BTC_USDT"],
                               client=Client(), clock=lambda: 1000)
        self.assertEqual(result, 1)
        snapshot = json.loads((self.path / "snapshot.json").read_text(encoding="utf-8"))
        self.assertIsNone(snapshot["liquidity"].get("data"))
        self.assertIsNone(snapshot["markets"]["BTC_USDT"]["windows"]["300"]["buy_ratio"])

    def test_output_escapes_source_text(self):
        from bitpin.flow_shadow import ShadowStore
        self.path.mkdir(); store = ShadowStore(self.path / "flows.sqlite3")
        try:
            store.fail("trades", "BTC_USDT", 1000, "<script>alert(1)</script>")
            html = self.report.render_report(store.snapshot(["BTC_USDT"], 1000))
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertIn("&lt;script&gt;", html)
            self.assertIn('dir="rtl"', html)
        finally: store.close()

    def test_two_collectors_cannot_own_same_state(self):
        self.cli.prepare_directory(self.path)
        with self.cli.StateLock(self.path):
            with self.assertRaises((ValueError, OSError)):
                with self.cli.StateLock(self.path): pass

    def test_unbounded_rate_and_invalid_market_rejected(self):
        for args in (["--interval", "1"], ["--interval", "nan"], ["--cycles", "0"],
                     ["--symbols", "BTC_USDT/../wallets"]):
            with self.assertRaises(SystemExit) as caught:
                self.cli.main(["--state-dir", str(self.path)] + args)
            self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.path.exists())


if __name__ == "__main__": unittest.main()
