# -*- coding: utf-8 -*-
"""Synthetic capital settings and authenticated panel requests; no exchange calls."""
import copy
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bitpin import panel_settings as ps
from test_panel_web import Client, PanelCase, T0, CONFIG_TEXT, KIMI_TEXT


class CapitalFields(unittest.TestCase):
    def test_scoped_form_does_not_clear_unrelated_checkboxes(self):
        fields = [f for f in ps.FIELDS if f.group == "capital"]
        changes, errors = ps.parse_form({"config:max_equity_irt": "۲۵۰٬۰۰۰",
                                        "config:protected_assets": "BTC = ۰٫۰۰۲۵\nIRT = 1000"}, fields=fields)
        self.assertEqual(errors, {})
        self.assertEqual(changes, [
            {"file": "config", "path": ["max_equity_irt"], "value": 250000},
            {"file": "config", "path": ["reinvest_profits"], "value": False},
            {"file": "config", "path": ["protected_assets"], "value": {"BTC": "0.0025", "IRT": "1000"}},
        ])

    def test_quantity_map_keeps_decimal_precision_and_normalizes_assets(self):
        field = ps.BY_KEY["config:protected_assets"]
        self.assertEqual(ps.parse_value(field, "btc = 0.000000000000000000123\nUSDT = ۱۲٫۵۰۰"),
                         {"BTC": "0.000000000000000000123", "USDT": "12.5"})
        self.assertEqual(ps.parse_value(field, ""), {})
        self.assertEqual(ps.display(field, {"BTC": "0.0025", "IRT": "1000"}), "BTC = 0.0025\nIRT = 1000")

    def test_quantity_locks_are_not_rounded_to_decimal_context_precision(self):
        field = ps.BY_KEY["config:protected_assets"]
        self.assertEqual(ps.parse_value(field, "BTC = 0.123456789012345678901234567890123"),
                         {"BTC": "0.123456789012345678901234567890123"})

    def test_invalid_or_duplicate_locks_are_rejected_without_partial_save(self):
        field = ps.BY_KEY["config:protected_assets"]
        for value in ("BTC = -1", "BTC = NaN", "BTC = Infinity", "BTC = 1e6", "BTC = 1\nbtc = 2",
                      "BTC_IRT = 1", "BTC = ", "<script> = 1"):
            with self.subTest(value=value):
                with self.assertRaises(ps.FieldError):
                    ps.parse_value(field, value)
        for value in ([], {"BTC": True}, {"BTC": "-1"}, {"BTC": "NaN"}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ps.check_value(field, value)

    def test_empty_default_locks_do_not_create_a_config_change(self):
        cfg, kimi, changed, errors = ps.apply_changes({}, {}, [
            {"file": "config", "path": ["protected_assets"], "value": {}}],
            {"config": {"protected_assets": {}}, "kimi": {}})
        self.assertEqual((cfg, kimi, changed, errors), ({}, {}, [], []))

    def test_numeric_json_floor_round_trips_without_a_policy_change(self):
        field = ps.BY_KEY["config:protected_assets"]
        config = {"protected_assets": {"BTC": 1e-6}}
        text = ps.display(field, config["protected_assets"])
        self.assertEqual(text, "BTC = 0.000001")
        value = ps.parse_value(field, text)
        cfg, kimi, changed, errors = ps.apply_changes(config, {}, [
            {"file": "config", "path": ["protected_assets"], "value": value}],
            {"config": {}, "kimi": {}})
        self.assertEqual((cfg, kimi, changed, errors), ({"protected_assets": {"BTC": 1e-6}}, {}, [], []))

    def test_helper_accepts_numeric_json_floor_in_scientific_notation(self):
        field = ps.BY_KEY["config:protected_assets"]
        self.assertEqual(ps.check_value(field, {"BTC": 1e-6}), {"BTC": "0.000001"})


class CapitalPage(PanelCase):
    def setUp(self):
        super(CapitalPage, self).setUp()
        self.cfg_doc, self.kimi_doc = ps.load_docs(CONFIG_TEXT, KIMI_TEXT)
        self.cfg_doc.update(max_equity_irt=250000, reinvest_profits=True, protected_assets={"BTC": "0.0025"})
        self.helper.responses["config_get"] = lambda a: {"config": json.dumps(self.cfg_doc),
                                                        "kimi": json.dumps(self.kimi_doc)}
        self.snapshot = {
            "configured": {"max_equity_irt": 250000, "reinvest_profits": True, "protected_assets": {"BTC": "0.0025"}},
            "active": {"budget_irt": 250000, "managed_equity_irt": 220000, "cash_irt": 100000,
                       "profit_reserve_irt": 5000, "spendable_cash_irt": 95000, "external_reserve_irt": 900000,
                       "units": {"BTC": "0.0001"},
                       "protected_assets": {"BTC": "0.0025"}, "pending": False, "reason": None, "updated_at": T0 - 60},
            "account": {"balances": {"BTC": "0.005123456789", "USDT": "12.5"},
                        "prices_irt": {"BTC": "1000000", "USDT": "80000"}, "updated_at": T0 - 120},
            "orders": {"active_count": 0, "unknown_count": 0}, "warnings": [],
        }
        self.helper.responses["capital_status"] = lambda a: self.snapshot
        self.helper.responses["settings_set"] = self.save_settings

    def save_settings(self, args):
        cfg, kimi, changed, errors = ps.apply_changes(self.cfg_doc, self.kimi_doc, args["changes"],
                                                     {"config": self.cfg_doc, "kimi": self.kimi_doc})
        if not args["dry_run"] and not errors:
            self.cfg_doc, self.kimi_doc = cfg, kimi
        return {"problems": errors, "warnings": [], "written": not args["dry_run"] and bool(changed) and not errors,
                "confirm_needed": True, "changed": changed, "diff": "", "backup": None}

    def form(self, **extra):
        form = {"action": "preview", "config:max_equity_irt": "250000", "config:reinvest_profits": "on",
                "asset_0": "BTC", "quantity_0": "0.0025"}
        form.update(extra)
        return form

    def english_client(self):
        client = Client(self.app, lang="en")
        self.login(client)
        return client

    def test_page_exposes_quantity_controls_and_saved_and_active_values(self):
        client = self.english_client()
        response = client.get("/capital")
        self.assertEqual(response.status, 200)
        self.assertIn('name="asset_0"', response.text)
        self.assertIn('name="quantity_0" value="0.0025"', response.text)
        self.assertIn('name="config:reinvest_profits"', response.text)
        self.assertIn('href="/capital"', client.get("/").text)
        self.assertIn("220,000", response.text)
        self.assertIn("95,000", response.text)
        self.assertIn("2026-", response.text)
        self.assertNotIn('<textarea name="config:protected_assets"', response.text)

    def test_capital_controls_show_plain_labels_and_external_cash(self):
        response = self.english_client().get("/capital")
        self.assertIn("900,000", response.text)
        self.assertNotIn('<code class="key"', response.text)

    def test_new_asset_row_can_copy_a_dated_cached_balance_without_javascript(self):
        client = self.english_client()
        response = client.get("/capital")
        self.assertIn('value="lock-current:1">', response.text)
        response = client.post("/capital", self.form(action="lock-current:1", asset_1="USDT", quantity_1=""))
        self.assertEqual(response.status, 200)
        self.assertIn('name="quantity_1" value="12.5"', response.text)
        self.assertNotIn("settings_set", self.helper.cmds())

    def test_enter_in_a_quantity_field_defaults_to_preview(self):
        response = self.english_client().get("/capital")
        form = re.search(r'<form[^>]+action="/capital"[^>]*>(.*?)</form>', response.text, re.S).group(1)
        first_submit = re.search(r'<button[^>]+name="action"[^>]+value="([^"]+)"', form).group(1)
        self.assertEqual(first_submit, "preview")

    def test_saved_numeric_floor_can_be_previewed_unchanged_from_rendered_controls(self):
        self.cfg_doc["protected_assets"] = {"BTC": 1e-6}
        before = copy.deepcopy(self.cfg_doc)
        client = self.english_client()
        rendered = client.get("/capital")
        quantity = re.search(r'name="quantity_0" value="([^"]+)"', rendered.text).group(1)
        self.assertEqual(quantity, "0.000001")
        response = client.post("/capital", self.form(quantity_0=quantity))
        self.assertEqual(response.status, 200)
        self.assertIn("No value changes.", response.text)
        self.assertEqual(self.cfg_doc, before)

    def test_missing_active_values_stay_unknown_instead_of_zero(self):
        self.snapshot["active"] = {}
        self.snapshot["account"] = {}
        self.snapshot["orders"] = {}
        response = self.english_client().get("/capital")
        self.assertEqual(response.status, 200)
        self.assertIn('data-capital="managed_equity_irt"><span class="capital-value">Unknown</span>', response.text)
        self.assertIn('data-capital="spendable_cash_irt"><span class="capital-value">Unknown</span>', response.text)
        self.assertIn("Manual quantity", response.text)

    def test_active_quantities_are_shown_separately_from_editable_saved_locks(self):
        self.snapshot["active"]["protected_assets"] = {"BTC": "0.003"}
        self.snapshot["active"]["pending"] = True
        response = self.english_client().get("/capital")
        self.assertIn("Pending activation", response.text)
        self.assertIn('data-active-lock="BTC"><bdi dir="ltr">0.003</bdi>', response.text)
        self.assertIn('name="quantity_0" value="0.0025"', response.text)

    def test_overlength_quantity_is_rejected_instead_of_truncated(self):
        self.login()
        value = "1" * 161
        response = self.c.post("/capital", self.form(action="save", quantity_0=value))
        self.assertEqual(response.status, 400)
        self.assertIn('value="%s"' % value, response.text)
        self.assertNotIn("settings_set", self.helper.cmds())

    def test_a_row_cannot_embed_another_asset_definition(self):
        self.login()
        response = self.c.post("/capital", self.form(action="save", asset_0="BTC = 1\nUSDT", quantity_0="2"))
        self.assertEqual(response.status, 400)
        self.assertNotIn("settings_set", self.helper.cmds())

    def test_malformed_row_indexes_are_rejected_without_clearing_locks(self):
        self.login()
        response = self.c.post("/capital", self.form(action="save", asset_32="USDT", quantity_32="2"))
        self.assertEqual(response.status, 400)
        self.assertNotIn("settings_set", self.helper.cmds())

    def test_invalid_snapshot_time_cannot_populate_a_lock(self):
        self.snapshot["account"]["updated_at"] = "unknown"
        client = self.english_client()
        self.assertEqual(client.post("/capital", self.form(action="lock-current:0")).status, 400)

    def test_undated_cache_cannot_populate_a_lock(self):
        self.snapshot["account"]["updated_at"] = None
        client = self.english_client()
        response = client.get("/capital")
        self.assertIn('value="lock-current:0" disabled aria-disabled="true"', response.text)
        self.assertEqual(client.post("/capital", self.form(action="lock-current:0")).status, 400)

    def test_add_and_remove_rows_keep_unsaved_policy_values(self):
        client = self.english_client()
        response = client.post("/capital", self.form(action="add-lock", **{"config:max_equity_irt": "175000"}))
        self.assertIn('name="asset_1"', response.text)
        self.assertIn('name="config:max_equity_irt" value="175000"', response.text)
        response = client.post("/capital", self.form(action="remove-lock:0", asset_1="USDT", quantity_1="2.5"))
        self.assertIn('name="asset_0" value="USDT"', response.text)
        self.assertIn('name="quantity_0" value="2.5"', response.text)
        self.assertNotIn("settings_set", self.helper.cmds())

    def test_null_cap_displays_whole_account_warning(self):
        self.cfg_doc["max_equity_irt"] = None
        response = self.english_client().get("/capital")
        self.assertIn("No base amount is set", response.text)

    def test_hostile_cache_and_helper_text_is_escaped(self):
        self.snapshot["warnings"] = ['<script>alert("x")</script>']
        self.snapshot["active"]["reason"] = '<script>alert("x")</script>'
        response = self.english_client().get("/capital")
        self.assertIn("&lt;script&gt;", response.text)
        self.assertNotIn("<script>", response.text)

    def test_dedicated_save_preserves_unrelated_settings(self):
        self.login()
        before = copy.deepcopy(self.cfg_doc)
        form = self.form(action="save", asset_0="USDT", quantity_0="12.50")
        del form["config:reinvest_profits"]
        response = self.c.post("/capital", form)
        self.assertEqual((response.status, response.header("Location")), (303, "/capital"))
        self.assertFalse(self.cfg_doc["reinvest_profits"])
        self.assertEqual(self.cfg_doc["protected_assets"], {"USDT": "12.5"})
        for key, value in before.items():
            if key not in ("max_equity_irt", "reinvest_profits", "protected_assets"):
                self.assertEqual(self.cfg_doc[key], value, key)
        self.assertIn(u"اعمال تنظیمات", self.c.get("/capital").text)

    def test_lock_current_uses_cached_units_exactly_and_does_not_save(self):
        client = self.english_client()
        before = copy.deepcopy(self.cfg_doc)
        response = client.post("/capital", self.form(action="lock-current:0"))
        self.assertEqual(response.status, 200)
        self.assertIn('name="quantity_0" value="0.005123456789"', response.text)
        self.assertEqual(self.cfg_doc, before)
        self.assertNotIn("settings_set", self.helper.cmds())

    def test_missing_cached_quantity_does_not_replace_manual_quantity(self):
        self.snapshot["account"] = {"balances": {}, "updated_at": None}
        client = self.english_client()
        response = client.post("/capital", self.form(action="lock-current:0"))
        self.assertEqual(response.status, 400)
        self.assertIn('name="quantity_0" value="0.0025"', response.text)
        self.assertNotIn("settings_set", self.helper.cmds())

    def test_invalid_lock_row_is_returned_and_nothing_is_saved(self):
        self.login()
        before = copy.deepcopy(self.cfg_doc)
        response = self.c.post("/capital", self.form(action="save", quantity_0="-1"))
        self.assertEqual(response.status, 400)
        self.assertIn('name="quantity_0" value="-1"', response.text)
        self.assertEqual(self.cfg_doc, before)
        self.assertNotIn("settings_set", self.helper.cmds())

    def test_capital_post_requires_existing_csrf_and_authentication(self):
        self.assertEqual(self.c.post("/capital", self.form(action="save")).status, 303)
        self.login()
        self.assertEqual(self.c.post("/capital", self.form(action="save"), csrf=False).status, 403)
        self.assertNotIn("settings_set", self.helper.cmds())

    def test_preview_keeps_config_unchanged_and_delegates_policy_gate_to_helper(self):
        self.login()
        before = copy.deepcopy(self.cfg_doc)
        self.helper.responses["settings_set"] = {"problems": ["stop the bot and resolve resting orders"],
                                                 "warnings": [], "written": False, "changed": []}
        response = self.c.post("/capital", self.form())
        self.assertEqual(response.status, 200)
        self.assertIn("stop the bot and resolve resting orders", response.text)
        self.assertEqual(self.cfg_doc, before)


if __name__ == "__main__":
    unittest.main()
