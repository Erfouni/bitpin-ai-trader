"""Profit-reserve cash flows preserve genuine drawdown; synthetic local state only."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin.risk import RiskManager  # noqa: E402


class CapitalReserveRiskTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="capital_risk_")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.now = 100 * 86400
        self.basis = "sleeve:1000"

    def manager(self, persist=True):
        return RiskManager({"max_drawdown": "0.3"}, state_dir=self.directory, mode="paper",
                           clock=lambda: self.now, persist=persist)

    def adjust(self, manager, reserved, basis=None):
        return manager.adjust_profit_reserve(reserved, self.basis if basis is None else basis)

    def state(self):
        with open(os.path.join(self.directory, "risk_state_paper.json"), encoding="utf-8") as source:
            return json.load(source)

    # A missing cash-flow translation would falsely halt this unchanged portfolio.
    def test_profit_withdrawal_does_not_create_a_fake_drawdown(self):
        manager = self.manager()
        manager.update_equity(1500, self.basis)
        self.assertEqual(self.adjust(manager, 500), Decimal(-500))
        result = manager.update_equity(1000, self.basis)
        self.assertEqual(result["hwm"], Decimal(1000))
        self.assertEqual(result["drawdown"], Decimal(0))
        self.assertFalse(result["breached"])
        self.assertFalse(manager.is_halted())

    def test_withdrawal_keeps_the_remaining_real_loss_visible(self):
        manager = self.manager()
        manager.update_equity(1200, self.basis)
        self.adjust(manager, 200)
        result = manager.update_equity(700, self.basis)
        self.assertEqual(result["hwm"], Decimal(1000))
        self.assertEqual(result["drawdown"], Decimal("0.3"))
        self.assertTrue(result["breached"])

    def test_all_daily_marks_move_with_the_withdrawal_without_changing_their_dates(self):
        manager = self.manager()
        manager.update_equity(1200, self.basis)
        self.now += 86400
        manager.update_equity(1100, self.basis)
        self.adjust(manager, 200)
        state = self.state()
        self.assertEqual(state["hwm_log"], [[100, "1000"], [101, "900"]])
        self.assertEqual(Decimal(state["hwm"]), Decimal(1000))
        self.assertEqual(Decimal(state["last_equity"]), Decimal(900))

    # Atomic absolute receipts must survive a crash between adjustment and measurement.
    def test_restart_before_next_measurement_does_not_apply_the_withdrawal_twice(self):
        manager = self.manager()
        manager.update_equity(1200, self.basis)
        self.adjust(manager, 200)
        restarted = self.manager()
        self.assertEqual(self.adjust(restarted, 200), Decimal(0))
        result = restarted.update_equity(1000, self.basis)
        self.assertEqual(result["hwm"], Decimal(1000))
        self.assertEqual(result["drawdown"], Decimal(0))
        self.assertEqual(Decimal(self.state()["capital_profit_reserve"]), Decimal(200))

    def test_later_reserve_growth_adjusts_only_the_new_increment(self):
        manager = self.manager()
        manager.update_equity(1400, self.basis)
        self.adjust(manager, 200)
        self.assertEqual(self.adjust(manager, 300), Decimal(-100))
        result = manager.update_equity(1100, self.basis)
        self.assertEqual(result["hwm"], Decimal(1100))
        self.assertEqual(result["drawdown"], Decimal(0))

    def test_reinvestment_release_adds_back_only_the_previously_reserved_amount(self):
        manager = self.manager()
        manager.update_equity(1200, self.basis)
        self.adjust(manager, 200)
        manager.update_equity(1000, self.basis)
        self.assertEqual(self.adjust(manager, 0), Decimal(200))
        restarted = self.manager()
        self.assertEqual(self.adjust(restarted, 0), Decimal(0))
        self.assertEqual(restarted.update_equity(1200, self.basis)["drawdown"], Decimal(0))
        self.assertEqual(restarted.high_water_mark(self.basis), Decimal(1200))

    def test_basis_change_records_current_reserve_without_translating_old_history(self):
        manager = self.manager()
        manager.update_equity(1200, self.basis)
        self.adjust(manager, 200)
        new_basis = "sleeve:600"
        self.assertEqual(self.adjust(manager, 300, new_basis), Decimal(0))
        self.assertEqual(Decimal(self.state()["hwm"]), Decimal(1000))
        result = manager.update_equity(600, new_basis)
        self.assertEqual(result["hwm"], Decimal(600))
        self.assertEqual(result["drawdown"], Decimal(0))
        self.assertEqual(self.adjust(manager, 300, new_basis), Decimal(0))
        self.assertEqual(manager.high_water_mark(new_basis), Decimal(600))

    def test_initial_receipt_does_not_subtract_from_the_first_active_equity_measurement(self):
        manager = self.manager()
        self.assertEqual(self.adjust(manager, 200), Decimal(0))
        self.assertEqual(manager.update_equity(1000, self.basis)["hwm"], Decimal(1000))
        self.assertEqual(self.adjust(manager, 200), Decimal(0))
        self.assertEqual(manager.high_water_mark(self.basis), Decimal(1000))

    def test_legacy_state_without_a_receipt_adjusts_its_existing_mark_once(self):
        path = os.path.join(self.directory, "risk_state_paper.json")
        with open(path, "w", encoding="utf-8") as target:
            json.dump({"hwm": "1200", "last_equity": "1100", "basis": self.basis}, target)
        manager = self.manager()
        self.adjust(manager, 200)
        result = manager.update_equity(900, self.basis)
        self.assertEqual(result["hwm"], Decimal(1000))
        self.assertEqual(result["drawdown"], Decimal("0.1"))

    def test_old_marks_are_clamped_at_zero_when_a_withdrawal_exceeds_them(self):
        manager = self.manager()
        manager.update_equity(100, self.basis)
        self.now += 86400
        manager.update_equity(1500, self.basis)
        self.adjust(manager, 500)
        self.assertEqual(self.state()["hwm_log"], [[100, "0"], [101, "1000"]])
        self.assertEqual(manager.high_water_mark(self.basis), Decimal(1000))

    def test_dry_run_adjusts_memory_but_does_not_write_a_reserve_receipt(self):
        manager = self.manager(persist=False)
        manager.update_equity(1200, self.basis)
        self.adjust(manager, 200)
        self.assertEqual(manager.high_water_mark(self.basis), Decimal(1000))
        self.assertFalse(os.path.exists(os.path.join(self.directory, "risk_state_paper.json")))

    def test_invalid_reserve_is_rejected_without_changing_persisted_risk_state(self):
        manager = self.manager()
        manager.update_equity(1200, self.basis)
        before = self.state()
        for amount in (True, False, None, -1, "NaN", "Infinity", "-Infinity", "bad"):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    self.adjust(manager, amount)
                self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
