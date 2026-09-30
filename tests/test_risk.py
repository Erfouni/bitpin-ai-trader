"""Risk manager tests of release v3 (spec E2): the drawdown breaker measures against a ROLLING
high-water mark (risk.hwm_window_days, default 90; 0 = the old all-time mark), so a year of toman
appreciation cannot leave the bot halted forever below a mark set months ago; the state file carries
what the read-only Telegram notifier shows; older state files are read. No network."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin.risk import DEFAULT_RISK, RiskManager  # noqa: E402

DAY = 86400.0
T0 = 1790121600.0          # 2026-09-23 00:00 UTC


class Clock(object):
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t


class RollingHwmTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="risk_v3_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = Clock()

    def rm(self, **cfg):
        return RiskManager(cfg, state_dir=self.dir, mode="live", clock=self.clock)

    def state(self):
        with open(os.path.join(self.dir, "risk_state_live.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_the_default_window_is_90_days_and_the_config_key_is_validated(self):
        self.assertEqual(DEFAULT_RISK["hwm_window_days"], 90)
        self.assertEqual(self.rm().hwm_window_days, 90.0)
        self.assertEqual(self.rm(hwm_window_days=0).hwm_window_days, 0.0)
        for bad in (-1, "90", True, None, 4000):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.rm(hwm_window_days=bad)
        # config.json is read with Decimal numbers: 90.0 written by hand must load (v3 money review)
        from decimal import Decimal
        self.assertEqual(self.rm(hwm_window_days=Decimal("90.0")).hwm_window_days, 90.0)
        self.assertEqual(self.rm(hwm_window_days=90.0).hwm_window_days, 90.0)
        with self.assertRaises(ValueError):
            self.rm(hwm_window_days=Decimal("NaN"))

    def test_a_mark_older_than_the_window_falls_out(self):
        """The mark set during a spike 100 days ago no longer counts: the breaker measures the LAST
        90 days, so an ordinary later drawdown from a lower level is judged against that level."""
        r = self.rm(max_drawdown=0.5)
        r.update_equity(Decimal(1000), "account")               # the spike
        self.assertEqual(r.high_water_mark("account"), Decimal(1000))
        self.clock.t += 30 * DAY
        self.assertFalse(r.update_equity(Decimal(600), "account")["breached"])   # -40%: within 50%
        self.clock.t += 61 * DAY                                  # the spike is now 91 days old
        d = r.update_equity(Decimal(600), "account")
        self.assertEqual(d["hwm"], Decimal(600))                  # the mark followed the equity down
        self.assertEqual(d["drawdown"], 0)
        self.assertFalse(d["breached"])
        # a fresh 50% drop from the new mark still halts
        d = r.update_equity(Decimal(300), "account")
        self.assertTrue(d["breached"])
        self.assertTrue(r.is_halted())
        self.assertIn("50%", r.halt_reason())

    def test_window_zero_keeps_the_all_time_mark(self):
        r = self.rm(hwm_window_days=0, max_drawdown=0.3)
        r.update_equity(Decimal(1000), "account")
        self.clock.t += 400 * DAY
        d = r.update_equity(Decimal(800), "account")
        self.assertEqual(d["hwm"], Decimal(1000))
        self.assertAlmostEqual(float(d["drawdown"]), 0.2)
        self.assertFalse(d["breached"])
        self.assertTrue(r.update_equity(Decimal(700), "account")["breached"])

    def test_one_sample_per_utc_day_keeps_the_days_high_and_the_log_bounded(self):
        r = self.rm()
        for eq in (100, 130, 120):                                # three reads on the same day
            r.update_equity(Decimal(eq), "account")
            self.clock.t += 3600
        log = self.state()["hwm_log"]
        self.assertEqual(len(log), 1)
        self.assertEqual(Decimal(log[0][1]), Decimal(130))
        for i in range(200):                                      # a slow decline over 200 days
            self.clock.t += DAY
            r.update_equity(Decimal(130 - i // 4), "account")
        self.assertLessEqual(len(self.state()["hwm_log"]), 90)
        self.assertEqual(r.high_water_mark("account"), Decimal(130 - (200 - 90) // 4))

    def test_peek_drawdown_has_no_side_effect_and_agrees_with_update(self):
        r = self.rm(max_drawdown=0.5)
        r.update_equity(Decimal(1000), "account")
        self.clock.t += 95 * DAY
        p = r.peek_drawdown(Decimal(400), "account")
        self.assertEqual(p["hwm"], Decimal(400))                  # the old mark is outside the window
        self.assertFalse(p["breached"])
        self.assertEqual(self.state()["hwm"], "1000")             # nothing written by peek
        self.assertEqual(r.update_equity(Decimal(400), "account")["hwm"], Decimal(400))

    def test_a_basis_change_restarts_the_log(self):
        r = self.rm()
        r.update_equity(Decimal(1000), "account")
        d = r.update_equity(Decimal(100), "sleeve:100")
        self.assertEqual(d["hwm"], Decimal(100))
        self.assertEqual(len(self.state()["hwm_log"]), 1)

    def test_an_older_state_file_is_read_and_its_mark_expires_after_one_window(self):
        """Before v3 the state held only the all-time "hwm". It is honoured on the first cycles after
        the upgrade and treated as a sample of the upgrade day, so it drops out 90 days later."""
        with open(os.path.join(self.dir, "risk_state_live.json"), "w", encoding="utf-8") as f:
            json.dump({"hwm": "5000000", "halted": False, "halt_reason": None, "orders": [], "basis": "account",
                       "last_equity": "4000000"}, f)
        r = self.rm(max_drawdown=0.5)
        self.assertEqual(r.high_water_mark("account"), Decimal(5000000))
        d = r.update_equity(Decimal(4000000), "account")
        self.assertEqual(d["hwm"], Decimal(5000000))
        self.assertAlmostEqual(float(d["drawdown"]), 0.2)
        self.clock.t += 91 * DAY
        self.assertEqual(r.update_equity(Decimal(4000000), "account")["hwm"], Decimal(4000000))
        # a corrupt log entry never breaks the reading
        st = self.state()
        st["hwm_log"].append(["x", "y"])
        with open(os.path.join(self.dir, "risk_state_live.json"), "w", encoding="utf-8") as f:
            json.dump(st, f)
        self.assertEqual(self.rm().high_water_mark("account"), Decimal(4000000))

    def test_the_state_carries_what_the_notifier_shows(self):
        r = self.rm(max_drawdown=0.5)
        r.update_equity(Decimal(1000), "account")
        r.update_equity(Decimal(900), "account")
        st = self.state()
        self.assertEqual(st["max_drawdown"], 0.5)
        self.assertEqual(st["hwm_window_days"], 90.0)
        self.assertAlmostEqual(st["drawdown"], 0.1)
        self.assertEqual(st["hwm"], "1000")
        self.assertEqual(st["last_equity"], "900")
        self.assertEqual(st["hwm_at"], self.clock.t)
        r.reset()
        self.assertEqual(self.state()["hwm_log"], [])


if __name__ == "__main__":
    unittest.main()
