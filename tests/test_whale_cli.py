import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bitpin.whale_shadow import prepare_directory

try:
    from bitpin.whale_report import render_report, btc
except ImportError:
    render_report = btc = None

ROOT = Path(__file__).resolve().parents[1]


def load_cli():
    path = ROOT / "scripts" / "whale_shadow.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("whale_cli", str(path))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class CLI(unittest.TestCase):
    def test_report_only_writes_shadow_unknown_snapshot_without_network(self):
        cli = load_cli(); self.assertIsNotNone(cli, "missing shadow CLI")
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(cli.main(["--state-dir", directory, "--report-only"], clock=lambda: 100000), 1)
            snapshot = json.loads((Path(directory) / "whale-shadow.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["status"], "missing")
            self.assertFalse(snapshot["live_enabled"])
            self.assertTrue((Path(directory) / "report.html").is_file())

    def test_invalid_directory_fails_without_overwriting_existing_file(self):
        cli = load_cli(); self.assertIsNotNone(cli, "missing shadow CLI")
        with tempfile.TemporaryDirectory() as directory, patch("sys.stderr", new=io.StringIO()):
            path = Path(directory) / "keep.txt"; path.write_text("keep")
            self.assertEqual(cli.main(["--state-dir", directory, "--report-only"]), 2)
            self.assertEqual(path.read_text(), "keep")

    def test_other_collector_and_account_markers_rejected_before_reads(self):
        for marker in (".defi-shadow-state", ".flow-shadow-state", "LIVE_CONFIRMED", "bitpin_token.json"):
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / marker).touch()
                with self.assertRaises(ValueError): prepare_directory(Path(directory) / "nested")
                self.assertFalse((Path(directory) / "nested").exists())

    def test_outputs_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = prepare_directory(Path(directory) / "whale")
            target = Path(directory) / "keep"; target.write_text("keep")
            try: (path / "whale.sqlite3").symlink_to(target)
            except OSError: self.skipTest("platform does not permit symlinks")
            with self.assertRaises(ValueError): prepare_directory(path)
            self.assertEqual(target.read_text(), "keep")

    def test_live_directories_rejected(self):
        for value in ("/var/lib/bitpin-bot/test", "/etc/bitpin-bot/test", "/opt/bitpin-bot/test", "/var/lib/bitpin-defi-shadow/test"):
            with self.assertRaises(ValueError): prepare_directory(value)

    def test_report_escapes_external_text_and_distinguishes_unavailable_from_zero(self):
        self.assertTrue(callable(render_report), "missing shadow report")
        snap = {"status": "missing", "generated_at": 100000, "received_at": None, "sources": {},
                "wallets": [{"address": "<script>alert(1)</script>", "label": "<script>", "balance_sats": None,
                             "change_24h_sats": None, "change_7d_sats": 0}],
                "transactions": [], "excluded_recent_transactions": None}
        html = render_report(snap)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Unknown", html)
        self.assertIn("0.00000000", html)
        self.assertIn("No effect on trades", html)
        self.assertIn('name="viewport"', html)

    def test_btc_formatter_preserves_one_satoshi_and_negative_changes(self):
        self.assertTrue(callable(btc), "missing integer formatter")
        self.assertEqual(btc(1), "0.00000001")
        self.assertEqual(btc(-1), "-0.00000001")
        self.assertEqual(btc(13001007903066), "130,010.07903066")

    def test_interrupted_collection_publishes_unknown_output_first(self):
        cli = load_cli(); self.assertIsNotNone(cli)
        class Interrupted:
            def get(self, provider, path): raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KeyboardInterrupt):
                cli.main(["--state-dir", directory], client=Interrupted(), clock=lambda: 100000, sleeper=lambda _: None)
            self.assertTrue((Path(directory) / "whale-shadow.json").is_file(), "unknown snapshot must exist before network requests")
            snap = json.loads((Path(directory) / "whale-shadow.json").read_text(encoding="utf-8"))
            self.assertEqual(snap["status"], "collecting")
            self.assertEqual(snap["transactions"], [])


if __name__ == "__main__": unittest.main()
