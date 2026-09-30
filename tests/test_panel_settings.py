# -*- coding: utf-8 -*-
"""v3.1: the trade settings form of the panel (bitpin/panel_settings.py): the field table matches the real
config keys, a round trip of the shipped examples changes nothing, the form parses Persian digits and lists,
and the helper side (apply_changes) accepts only the table's keys with checked values."""
import copy
import importlib.util
import json
import os
import sys
import unittest
from collections import OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bitpin import panel_settings as ps  # noqa: E402
from bitpin.panel_i18n import Text  # noqa: E402


def load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def read(name):
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg, self.kimi = ps.load_docs(read("config.example.json"), read("kimi.example.json"))
        self.defaults = ps.code_defaults()

    def form(self, cfg=None, kimi=None):
        vals = ps.form_values(cfg or self.cfg, kimi or self.kimi, self.defaults)
        # a browser does not post an unchecked checkbox
        return OrderedDict((k, v) for k, v in vals.items() if not (ps.BY_KEY[k].kind == "bool" and v == ""))

    def apply(self, form, cfg=None, kimi=None):
        changes, errors = ps.parse_form(form)
        self.assertEqual(errors, {})
        return ps.apply_changes(cfg or self.cfg, kimi or self.kimi, changes, self.defaults)


class TestFieldTable(Base):
    def test_every_field_is_a_real_key(self):
        docs = {"config": self.cfg, "kimi": self.kimi}
        for f in ps.FIELDS:
            in_example = ps._walk(docs[f.file], f.path) is not ps.MISSING
            in_code = ps._walk(self.defaults[f.file], f.path) is not ps.MISSING
            self.assertTrue(in_example or in_code, f.key)
            self.assertIn(f.group, [g[0] for g in ps.GROUPS], f.key)

    def test_keys_are_unique_and_every_text_is_in_both_languages(self):
        def persian(s):
            return any(u"؀" <= ch <= u"ۿ" for ch in s)

        def english(s):
            return bool(s.strip()) and all(ord(ch) < 128 for ch in s)

        def pair_ok(t, where):
            self.assertIsInstance(t, Text, where)
            self.assertTrue(persian(t.fa), where)
            self.assertTrue(english(t.en), where)

        self.assertEqual(len(ps.BY_KEY), len(ps.FIELDS))
        for f in ps.FIELDS:
            pair_ok(f.label, f.key)
            if f.help is not None:
                pair_ok(f.help, f.key)
            if isinstance(f.unit, Text):
                self.assertTrue(f.unit.fa and english(f.unit.en), f.key)
            else:
                self.assertTrue(f.unit == "" or english(f.unit), f.key)
            if f.kind == "choice":
                self.assertTrue(f.choices, f.key)
                for _value, label in f.choices:
                    if isinstance(label, Text):
                        pair_ok(label, f.key)
                    else:
                        self.assertTrue(english(label), f.key)
        for gid, title, intro in ps.GROUPS:
            pair_ok(title, gid)
            if intro is not None:
                pair_ok(intro, gid)

    def test_form_errors_are_in_both_languages(self):
        f = ps.BY_KEY["config:risk.max_drawdown"]
        for bad in ("abc", "0", "99"):
            with self.assertRaises(ps.FieldError) as cm:
                ps.parse_value(f, bad)
            self.assertTrue(any(u"؀" <= ch <= u"ۿ" for ch in cm.exception.text.fa), bad)
            self.assertEqual(str(cm.exception), cm.exception.text.en)
            self.assertTrue(all(ord(ch) < 128 for ch in cm.exception.text.en), bad)
        _changes, errors = ps.parse_form({"config:risk.max_drawdown": "x"})
        self.assertIsInstance(errors["config:risk.max_drawdown"], Text)

    def test_english_help_comes_from_the_examples(self):
        found = [f.key for f in ps.FIELDS if ps.help_en(f, ROOT)]
        self.assertIn("config:risk.max_drawdown", found)
        self.assertIn("kimi:brain.decision_times_local", found)
        self.assertIn("config:max_equity_irt", found)
        self.assertGreater(len(found), len(ps.FIELDS) // 2)
        self.assertIsNone(ps.help_en(ps.BY_KEY["config:fee_rate"], os.path.join(ROOT, "no-such-dir")))


class TestRoundTrip(Base):
    def test_the_shipped_examples_round_trip_without_a_change(self):
        cfg2, kimi2, applied, errors = self.apply(self.form())
        self.assertEqual((applied, errors), ([], []))
        self.assertEqual(json.dumps(cfg2), json.dumps(self.cfg))
        self.assertEqual(json.dumps(kimi2), json.dumps(self.kimi))

    def test_display_values(self):
        v = ps.form_values(self.cfg, self.kimi, self.defaults)
        self.assertEqual(v["config:risk.max_drawdown"], "50")
        self.assertEqual(v["config:fee_rate"], "0.35")
        self.assertEqual(v["config:ladder.size_frac"], "25")
        self.assertEqual(v["config:ladder.levels_pct"], "-20")
        self.assertEqual(v["config:ladder.enabled"], "on")
        self.assertEqual(v["config:max_equity_irt"], "")
        self.assertEqual(v["kimi:brain.decision_times_local"], "19:00")
        self.assertEqual(v["kimi:brain.held_move_pct"], "12")
        self.assertEqual(v["kimi:brain.slot_reasoning_effort"], "null")
        self.assertEqual(v["config:risk.min_order_irt"], "100000")
        self.assertTrue(v["kimi:brain.allowed_symbols"].startswith("USDT_IRT, BTC_IRT, "))

    def test_a_missing_section_shows_the_defaults_and_is_created_on_a_change(self):
        kimi = copy.deepcopy(self.kimi)
        del kimi["guard"]
        form = self.form(kimi=kimi)
        self.assertEqual(form["kimi:guard.pump_rise_pct"], "30")
        _c, kimi2, applied, errors = self.apply(form, kimi=kimi)
        self.assertEqual((applied, errors), ([], []))
        self.assertNotIn("guard", kimi2)
        form["kimi:guard.pump_rise_pct"] = "40"
        _c, kimi2, applied, errors = self.apply(form, kimi=kimi)
        self.assertEqual(errors, [])
        self.assertEqual(kimi2["guard"], {"pump_rise_pct": 40.0})
        self.assertEqual(applied, [{"file": "kimi", "path": "guard.pump_rise_pct", "old": 30.0, "new": 40.0}])


class TestChanges(Base):
    def test_percent_bool_list_and_nullable_changes(self):
        form = self.form()
        form["config:risk.max_drawdown"] = "۴۵"                 # Persian digits
        del form["config:ladder.enabled"]                       # unchecked
        form["config:ladder.levels_pct"] = "-20، -25"            # Persian comma
        form["config:risk.min_order_irt"] = "۱۵۰٬۰۰۰"            # Persian thousands separator
        form["config:max_equity_irt"] = ""
        form["kimi:brain.decision_times_local"] = "7:00, 19:00, 19:00"
        form["kimi:brain.allowed_symbols"] = form["kimi:brain.allowed_symbols"] + ", btc_irt"
        cfg2, kimi2, applied, errors = self.apply(form)
        self.assertEqual(errors, [])
        self.assertEqual(cfg2["risk"]["max_drawdown"], 0.45)
        self.assertIs(cfg2["ladder"]["enabled"], False)
        self.assertEqual(cfg2["ladder"]["levels_pct"], [-20, -25])
        self.assertEqual(cfg2["risk"]["min_order_irt"], 150000)
        self.assertIsNone(cfg2["max_equity_irt"])
        self.assertEqual(kimi2["brain"]["decision_times_local"], ["07:00", "19:00"])
        self.assertEqual(kimi2["brain"]["allowed_symbols"], self.kimi["brain"]["allowed_symbols"])   # deduped
        self.assertEqual(sorted(a["path"] for a in applied),
                         ["brain.decision_times_local", "ladder.enabled", "ladder.levels_pct",
                          "risk.max_drawdown", "risk.min_order_irt"])
        # the original documents are not modified
        self.assertEqual(self.cfg["risk"]["max_drawdown"], 0.5)

    def test_numeric_equality_is_not_a_change(self):
        form = self.form()
        form["kimi:brain.held_move_pct"] = "12.0"
        form["config:risk.max_drawdown"] = "50.00"
        form["kimi:brain.extra_instructions"] = "\r\n" + self.kimi["brain"]["extra_instructions"].replace("\n", "\r\n") + "  "
        _c, _k, applied, errors = self.apply(form)
        self.assertEqual((applied, errors), ([], []))

    def test_form_errors(self):
        form = self.form()
        bad = {"config:risk.max_drawdown": "abc", "config:risk.hwm_window_days": "12.5",
               "config:risk.max_orders_per_day": "0", "kimi:brain.decision_times_local": "25:00",
               "kimi:brain.allowed_symbols": "BTC-IRT", "config:ladder.levels_pct": "-20, 5",
               "kimi:brain.risk_profile": "yolo", "kimi:news.extra_topics": "x" * 1001,
               "config:risk.max_slippage": ""}
        form.update(bad)
        changes, errors = ps.parse_form(form)
        self.assertEqual(sorted(errors), sorted(bad))
        self.assertNotIn("config:risk.max_drawdown", [("%s:%s" % (c["file"], ".".join(c["path"]))) for c in changes])
        for msg in errors.values():
            self.assertTrue(msg)

    def test_the_helper_does_not_trust_the_web_side(self):
        c = lambda path, value, file="config": {"file": file, "path": path.split("."), "value": value}  # noqa: E731
        cases = [c("state_dir", "/tmp/x"),                          # not a form key
                 c("risk.max_drawdown", 2.0),                       # 200 %
                 c("risk.max_drawdown", True),
                 c("risk.hwm_window_days", 1.5),
                 c("risk.drawdown_action", "sell_everything"),
                 c("ladder.levels_pct", [-20, -20]),
                 c("ladder.levels_pct", "-20"),
                 c("brain.allowed_symbols", ["BTC_IRT; rm -rf"], "kimi"),
                 c("brain.decision_times_local", ["19:00", 7], "kimi"),
                 c("risk.max_drawdown", float("nan")),
                 {"file": "passwd", "path": ["x"], "value": 1},
                 "not a dict"]
        cfg2, kimi2, applied, errors = ps.apply_changes(self.cfg, self.kimi, cases, self.defaults)
        self.assertEqual(applied, [])
        self.assertEqual(len(errors), len(cases))
        self.assertEqual(json.dumps(cfg2), json.dumps(self.cfg))
        _c, _k, applied, errors = ps.apply_changes(self.cfg, self.kimi, "x", self.defaults)
        self.assertEqual(errors, ["changes must be a list"])
        twice = [c("risk.max_drawdown", 0.4), c("risk.max_drawdown", 0.3)]
        cfg2, _k, applied, errors = ps.apply_changes(self.cfg, self.kimi, twice, self.defaults)
        self.assertEqual(cfg2["risk"]["max_drawdown"], 0.4)
        self.assertEqual(len(errors), 1)

    def test_a_null_section_is_not_switched_on_by_the_form(self):
        kimi = copy.deepcopy(self.kimi)
        kimi["news"] = None
        form = self.form(kimi=kimi)
        _c, kimi2, applied, errors = self.apply(form, kimi=kimi)
        self.assertEqual((applied, errors), ([], []))
        self.assertIsNone(kimi2["news"])
        form["kimi:news.max_items"] = "5"
        _c, kimi2, applied, errors = self.apply(form, kimi=kimi)
        self.assertEqual(applied, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("null", errors[0])
        self.assertIsNone(kimi2["news"])

    def test_a_changed_pair_still_passes_the_bots_own_validation(self):
        ap = load_module("apply_profile_for_panel_settings", os.path.join("deploy", "apply_profile.py"))
        form = self.form()
        form["config:risk.max_drawdown"] = "40"
        form["kimi:brain.held_move_pct"] = "10"
        form["kimi:brain.decision_times_local"] = "13:00, 19:00"
        form["kimi:news.max_items"] = "8"
        cfg2, kimi2, applied, errors = self.apply(form)
        self.assertEqual(errors, [])
        self.assertEqual(len(applied), 4)
        problems, _warnings = ap.validate(ap.dumps(kimi2), ap.dumps(cfg2), ROOT)
        self.assertEqual(problems, [])
        form["kimi:brain.allowed_symbols"] = form["kimi:brain.allowed_symbols"] + ", NOSUCHCOIN_IRT"
        cfg3, kimi3, _applied, errors = self.apply(form)
        self.assertEqual(errors, [])
        problems, _warnings = ap.validate(ap.dumps(kimi3), ap.dumps(cfg3), ROOT)
        self.assertTrue(problems)


if __name__ == "__main__":
    unittest.main()
