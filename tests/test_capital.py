"""Pure capital policy contracts using synthetic balances, with no network."""
import os
import sys
import unittest
from decimal import Decimal, localcontext

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import capital  # noqa: E402


class CapitalPolicyTest(unittest.TestCase):
    # These contracts catch accepting unsafe settings or ambiguous asset floors.
    def test_protected_assets_canonicalize_names_and_preserve_decimal_units(self):
        self.assertEqual(capital.normalize_protected_assets({" usdt ": "12.500", "btc": "0.125"}),
                         {"USDT": Decimal("12.5"), "BTC": Decimal("0.125")})

    def test_absent_protected_assets_means_no_quantity_floors(self):
        self.assertEqual(capital.normalize_protected_assets(None), {})
        self.assertEqual(capital.normalize_protected_assets({}), {})

    def test_protected_assets_reject_duplicate_canonical_names(self):
        with self.assertRaises(ValueError):
            capital.normalize_protected_assets({"USDT": 1, " usdt ": 2})

    def test_protected_assets_reject_non_asset_names(self):
        for name in ("", " ", "BTC_IRT", "BTC/IRT", None, 1, True, "بیت"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                capital.normalize_protected_assets({name: 1})

    def test_protected_assets_reject_non_mapping_inputs(self):
        for value in ([], "USDT", 1, False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                capital.normalize_protected_assets(value)

    def test_protected_assets_reject_boolean_nonfinite_and_negative_quantities(self):
        for value in (True, False, None, "bad", "NaN", "Infinity", "-Infinity", "-0.01",
                      Decimal("sNaN"), float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                capital.normalize_protected_assets({"USDT": value})

    def test_zero_quantity_floor_is_valid(self):
        self.assertEqual(capital.normalize_protected_assets({"USDT": 0}), {"USDT": Decimal(0)})

    def test_sweeping_requires_a_positive_sleeve_cap(self):
        for cap in (None, 0, "0.0"):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                capital.validate_policy(cap, False, {})

    def test_reinvestment_allows_no_cap(self):
        self.assertEqual(capital.validate_policy(None, True, {"usdt": "2.5"}),
                         {"USDT": Decimal("2.5")})
        self.assertEqual(capital.validate_policy(0, True, {}), {})

    def test_policy_rejects_invalid_caps(self):
        for cap in (True, False, "-1", "NaN", "Infinity", "bad"):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                capital.validate_policy(cap, True, {})

    def test_reinvestment_flag_requires_a_real_boolean(self):
        for flag in (None, 0, 1, "false", "true", Decimal(1)):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                capital.validate_policy(1000, flag, {})

    def test_validation_returns_fresh_normalized_protected_assets(self):
        original = {"USDT": Decimal("2.5")}
        result = capital.validate_policy(1000, False, original)
        result["USDT"] = Decimal(99)
        self.assertEqual(original, {"USDT": Decimal("2.5")})

    # Fingerprints catch reuse of an active policy after its monetary boundary changes.
    def test_fingerprint_ignores_map_order_name_case_and_decimal_scale(self):
        first = capital.policy_fingerprint("1000.00", False, {" btc ": "0.10", "usdt": "20.0"})
        equivalent = capital.policy_fingerprint(Decimal(1000), False, {"USDT": 20, "BTC": ".1"})
        self.assertEqual(first, equivalent)
        self.assertEqual(len(first), 64)

    def test_fingerprint_changes_for_cap_reinvestment_or_quantity_floor(self):
        original = capital.policy_fingerprint(1000, False, {"USDT": 20})
        for cap, reinvest, protected in ((1001, False, {"USDT": 20}),
                                         (1000, True, {"USDT": 20}),
                                         (1000, False, {"USDT": 21}),
                                         (1000, False, {"BTC": 20})):
            with self.subTest(cap=cap, reinvest=reinvest, protected=protected):
                self.assertNotEqual(original, capital.policy_fingerprint(cap, reinvest, protected))

    def test_fingerprint_rejects_invalid_policy_before_hashing(self):
        with self.assertRaises(ValueError):
            capital.policy_fingerprint(None, False, {})

    # Spendability catches sales or quote purchases that invade floors or foreign funds.
    def test_free_balance_floor_is_conservative_when_funds_are_frozen(self):
        self.assertEqual(capital.spendable_asset("BTC", "10", "6", {"BTC": "4"}), Decimal(2))

    def test_quote_spending_preserves_the_usdt_quantity_floor(self):
        self.assertEqual(capital.spendable_asset(" usdt ", "100", "100", {"USDT": "80"}), Decimal(20))

    def test_floor_at_or_above_free_balance_leaves_nothing_to_spend(self):
        for floor in (6, 8, 12):
            with self.subTest(floor=floor):
                self.assertEqual(capital.spendable_asset("BTC", 10, 6, {"BTC": floor}), Decimal(0))

    def test_available_balance_cannot_overstate_the_account_total(self):
        self.assertEqual(capital.spendable_asset("USDT", 10, 30, {"USDT": 4}), Decimal(6))

    def test_capped_sleeve_cannot_spend_unrelated_account_cash(self):
        self.assertEqual(capital.spendable_asset("IRT", 9000, 9000, {}, owned=1000), Decimal(1000))

    def test_own_order_commitments_reduce_sleeve_spending(self):
        self.assertEqual(capital.spendable_asset("USDT", 100, 90, {"USDT": 10}, owned=40, committed=15),
                         Decimal(25))

    def test_wallet_floor_also_bounds_a_larger_sleeve_claim(self):
        self.assertEqual(capital.spendable_asset("BTC", 10, 6, {"BTC": 4}, owned=9, committed=1), Decimal(2))

    def test_profit_reserve_reduces_only_irt_spending(self):
        self.assertEqual(capital.spendable_asset("IRT", 9000, 9000, {}, owned=1000,
                                                committed=100, reserved_irt=200), Decimal(700))
        self.assertEqual(capital.spendable_asset("USDT", 50, 50, {}, owned=30,
                                                committed=5, reserved_irt=200), Decimal(25))

    def test_reserve_is_protected_even_without_an_explicit_ownership_limit(self):
        self.assertEqual(capital.spendable_asset("IRT", 1000, 900, {}, reserved_irt=200), Decimal(700))

    def test_external_cash_withdrawal_cannot_expose_reserve_despite_larger_sleeve_ledger(self):
        self.assertEqual(capital.spendable_asset("IRT", 250000, 250000, {},
                                                owned=1000000, reserved_irt=200000), Decimal(50000))

    def test_free_wallet_cash_must_retain_the_reserve_when_other_cash_is_frozen(self):
        self.assertEqual(capital.spendable_asset("IRT", 1000000, 250000, {},
                                                owned=1000000, reserved_irt=200000), Decimal(50000))

    def test_irt_quantity_floor_and_profit_reserve_both_remain_in_the_actual_wallet(self):
        self.assertEqual(capital.spendable_asset("IRT", 250000, 250000, {"IRT": 30000},
                                                owned=1000000, reserved_irt=200000), Decimal(20000))

    def test_physical_cash_below_reserve_is_unspendable_even_if_available_overstates_total(self):
        for available in (190000, 250000):
            with self.subTest(available=available):
                self.assertEqual(capital.spendable_asset("IRT", 190000, available, {},
                                                        owned=1000000, reserved_irt=200000), Decimal(0))

    def test_excess_own_commitments_or_reserve_cannot_create_negative_spending(self):
        self.assertEqual(capital.spendable_asset("IRT", 1000, 1000, {}, owned=100,
                                                committed=80, reserved_irt=50), Decimal(0))

    def test_asset_keyed_quantities_select_only_the_requested_asset(self):
        self.assertEqual(capital.spendable_asset("usdt", {"USDT": 100, "IRT": 1000},
                                                {"usdt": 90}, {"USDT": 10}, owned={"USDT": 40},
                                                committed={"USDT": 15}), Decimal(25))

    def test_missing_owned_asset_cannot_borrow_a_different_asset(self):
        self.assertEqual(capital.spendable_asset("USDT", {"USDT": 100}, {"USDT": 100}, {},
                                                owned={"IRT": 1000}), Decimal(0))

    def test_invalid_spendability_inputs_are_rejected(self):
        for key, value in (("total", True), ("total", -1), ("available", "NaN"),
                           ("owned", "Infinity"), ("committed", -1), ("reserved_irt", False)):
            args = dict(asset="IRT", total=100, available=100, protected={})
            args[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                capital.spendable_asset(**args)

    def test_spending_subtraction_is_exact_under_a_low_decimal_precision(self):
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(capital.spendable_asset("IRT", "1000000000000000000000000000000.01",
                                                    "1000000000000000000000000000000.01",
                                                    {"IRT": "1000000000000000000000000000000"}),
                             Decimal("0.01"))
            self.assertEqual(context.prec, 6)

    # Sweeps catch reserve recycling, sweeping unrealized gains without cash, and double sweeps.
    def test_realized_excess_moves_from_active_cash_to_profit_reserve(self):
        self.assertEqual(capital.sweep_profit(500, 0, 1200, 1000, False),
                         (Decimal(200), Decimal(-200)))

    def test_sweep_never_requires_a_sale_when_cash_is_below_equity_excess(self):
        self.assertEqual(capital.sweep_profit(30, 0, 1400, 1000, False),
                         (Decimal(30), Decimal(-30)))

    def test_prior_profit_reserve_is_not_counted_as_active_cash_again(self):
        self.assertEqual(capital.sweep_profit(250, 200, 1300, 1000, False),
                         (Decimal(250), Decimal(-50)))

    def test_loss_does_not_top_up_active_equity_from_saved_profit(self):
        self.assertEqual(capital.sweep_profit(250, 200, 800, 1000, False),
                         (Decimal(200), Decimal(0)))

    def test_repeated_sweep_of_the_same_active_equity_is_idempotent(self):
        self.assertEqual(capital.sweep_profit(500, 200, 1000, 1000, False),
                         (Decimal(200), Decimal(0)))

    def test_no_active_cash_or_no_excess_cannot_sweep(self):
        for cash, reserved, equity in ((200, 200, 1200), (500, 0, 1000), (500, 0, 900)):
            with self.subTest(cash=cash, reserved=reserved, equity=equity):
                self.assertEqual(capital.sweep_profit(cash, reserved, equity, 1000, False),
                                 (Decimal(reserved), Decimal(0)))

    def test_reinvestment_releases_existing_reserve_into_active_cash(self):
        self.assertEqual(capital.sweep_profit(250, 200, 800, 1000, True),
                         (Decimal(0), Decimal(200)))

    def test_reinvestment_with_no_cap_keeps_all_cash_active(self):
        self.assertEqual(capital.sweep_profit(500, 0, 500, None, True),
                         (Decimal(0), Decimal(0)))

    def test_reserve_cannot_exceed_the_cash_it_partitions(self):
        with self.assertRaises(ValueError):
            capital.sweep_profit(100, 200, 1000, 1000, False)

    def test_sweep_rejects_invalid_money_or_reinvestment_flag(self):
        for key, value in (("cash", True), ("reserved", -1), ("equity", "NaN"),
                           ("capital", "Infinity"), ("capital", 0), ("reinvest", 1)):
            args = dict(cash=500, reserved=0, equity=1200, capital=1000, reinvest=False)
            args[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                capital.sweep_profit(**args)

    def test_sweep_preserves_small_profit_exactly_under_low_decimal_precision(self):
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(capital.sweep_profit("1.0001", "1", "1000000000000000000000000000000.0001",
                                                 "1000000000000000000000000000000", False),
                             (Decimal("1.0001"), Decimal("-0.0001")))
            self.assertEqual(context.prec, 6)


if __name__ == "__main__":
    unittest.main()
