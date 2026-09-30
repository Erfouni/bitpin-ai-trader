"""Release v3 "one year" - the version constant, CHANGELOG.md, README.md, research/README.md and the
scripts/check_balance.py docstring (spec sections G1 and G3, plus the docs of the release). These are
documents, so the tests pin the FACTS a reader acts on (the 2027 dates, the update order, the retired
model ids, the offline studies), not the prose. No network."""
import ast
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import bitpin  # noqa: E402


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------- G1 version

class TestVersion(unittest.TestCase):
    def test_the_package_carries_the_release_version(self):
        self.assertEqual(bitpin.__version__, "3.6.1")                       # v3.6: performance
        # a plain string constant, readable without importing (deploy scripts may grep it)
        self.assertIn('__version__ = "3.6.1"', read("bitpin", "__init__.py"))

    def test_the_version_is_a_dotted_triple(self):
        self.assertRegex(bitpin.__version__, r"^\d+\.\d+\.\d+$")

    def test_the_init_module_stays_import_light(self):
        """bitpin/__init__.py is imported by every module and by the 3.7 interpreter on the server: it
        must stay a docstring plus the constant (no imports, no code that could fail at import time)."""
        tree = ast.parse(read("bitpin", "__init__.py"))
        kinds = [type(n).__name__ for n in tree.body]
        self.assertEqual(kinds, ["Expr", "Assign"], kinds)

    def test_the_changelog_and_readme_name_the_same_version(self):
        self.assertIn("## 3.0.0", read("CHANGELOG.md"))
        self.assertIn("## 3.1.0", read("CHANGELOG.md"))
        self.assertIn("## 3.1.1", read("CHANGELOG.md"))
        self.assertIn("## 3.2.0", read("CHANGELOG.md"))
        self.assertIn("## 3.3.0", read("CHANGELOG.md"))
        self.assertIn("## 3.4.0", read("CHANGELOG.md"))
        self.assertIn("## 3.4.1", read("CHANGELOG.md"))
        self.assertIn("## 3.5.0", read("CHANGELOG.md"))
        self.assertIn("## 3.5.1", read("CHANGELOG.md"))
        self.assertIn("## 3.5.2", read("CHANGELOG.md"))
        self.assertIn("## 3.5.3", read("CHANGELOG.md"))
        self.assertIn("## 3.6.0", read("CHANGELOG.md"))
        self.assertIn("## 3.6.1", read("CHANGELOG.md"))
        for name in ("README.md", "README_FA.md"):                       # v3.1: English + Persian README
            self.assertIn("`3.6.1`", read(name), name)


# --------------------------------------------------------------------------- CHANGELOG.md

class TestChangelog(unittest.TestCase):
    def setUp(self):
        self.text = read("CHANGELOG.md")
        self.v3 = self.text[self.text.index("## 3.0.0"):self.text.index("## 2.x")]

    def test_persian_summary_then_english_detail_per_spec_section(self):
        fa = self.v3.index("### خلاصهٔ فارسی")
        en = self.v3.index("### English detail")
        self.assertLess(fa, en)
        for sec in ("#### A. Horizon", "#### B. LLM client", "#### C. Context", "#### D. Prompt",
                    "#### E. Ladder", "#### F. Notifier", "#### G. Tests"):
            self.assertIn(sec, self.v3[en:], sec)
        # the sections appear in spec order
        pos = [self.v3.index(s) for s in ("#### A.", "#### B.", "#### C.", "#### D.", "#### E.", "#### F.",
                                          "#### G.")]
        self.assertEqual(pos, sorted(pos))

    def test_the_2027_dates_match_the_spec(self):
        for s in ("2027-09-21T20:30:00Z", "2027-09-16T13:00:00+03:30", "2027-09-20T13:00:00+03:30"):
            self.assertIn(s, self.v3, s)
        # Persian summary: 25 / 29 Shahrivar 1406 (Mehr 1 1406 = 2027-09-23)
        self.assertIn("۲۵ شهریور ۱۴۰۶", self.v3)
        self.assertIn("۲۹ شهریور ۱۴۰۶", self.v3)
        # the retired one-month dates are not presented as current
        self.assertNotIn("2026-10-17T13:00:00+03:30", self.v3)

    def test_the_headline_numbers_of_the_spec(self):
        for s in ("720", "`STOP_PCT_DEFAULT` is `None`", "5..40", "0.50", "hwm_window_days 90",
                  "levels_pct [-20]", "size_frac 0.25", "min_order_usdt 1.05", "held_move_pct 12",
                  "news.max_calls_per_day 2", "reasoning_effort", "llm_spend.json", "reasoning.txt",
                  "60 days", "after_hold_only", "analysis_policy", "prompt_template.txt",
                  "test_prompt_template.py", "13:30-20:00", "us market closed"):
            self.assertIn(s, self.v3, s)

    def test_the_update_path_is_in_the_right_order(self):
        g3 = self.v3[self.v3.index("#### G. Tests"):]
        steps = ["scratch/bitpin-bot-release-v3.tgz", "scp ", "sudo systemctl stop bitpin-bot",
                 "sudo bash deploy/update.sh", "apply_profile.py", "sudo bitpin-bot check",
                 "sudo bitpin-bot confirm-live", "sudo systemctl start bitpin-bot", "--rollback"]
        pos = [g3.index(s) for s in steps]
        self.assertEqual(pos, sorted(pos), list(zip(steps, pos)))

    def test_the_retired_model_ids_are_recorded_and_no_longer_used_by_the_tests(self):
        self.assertIn("moonshot-v1-*", self.v3)
        # no retired id is passed to set-model any more (the fixture comment may still NAME the family)
        self.assertNotRegex(read("tests", "test_release_review.py"), r"moonshot-v1-\d")


# --------------------------------------------------------------------------- README.md

class TestReadme(unittest.TestCase):
    def setUp(self):
        self.text = read("README_FA.md")          # v3.1: the Persian README (README.md is the English one)

    def test_one_year_competition_with_the_2027_dates(self):
        self.assertIn("یک‌ساله", self.text)
        for s in ("2026-09-21", "2027-09-21", "2027-09-16", "2027-09-20"):
            self.assertIn(s, self.text, s)
        self.assertNotIn("یک‌ماههٔ", self.text)
        self.assertNotIn("2026-10-17", self.text)
        self.assertNotIn("2026-10-21", self.text)

    def test_the_guards_describe_v3_not_v2(self):
        for s in ("۷۲۰ ساعت", "افت ۵۰٪", "۹۰ روز", "-20%", "±۱۲٪", "۱۳:۳۰ تا ۲۰:۰۰ UTC"):
            self.assertIn(s, self.text, s)
        for old in ("حد ضرر -12%", "-25%", "افت ۳۰٪", "±۸٪", "حداکثر 168 ساعت"):
            self.assertNotIn(old, self.text, old)

    def test_the_update_path_and_the_changelog_are_linked(self):
        for s in ("CHANGELOG.md", "bitpin-bot-release-v3.tgz", "deploy/update.sh", "apply_profile.py",
                  "confirm-live", "update.sh --rollback", "docs/DEPLOY_FA.md"):
            self.assertIn(s, self.text, s)
        blk = self.text[self.text.index("### مسیر به‌روزرسانی"):]
        pos = [blk.index(s) for s in ("systemctl stop bitpin-bot", "deploy/update.sh", "apply_profile.py",
                                      "confirm-live", "systemctl start bitpin-bot")]
        self.assertEqual(pos, sorted(pos))

    def test_the_staged_copy_exclusions_match_f4(self):
        blk = self.text[self.text.index("## نصب و به‌روزرسانی"):self.text.index("## خلاصهٔ پژوهش")]
        for s in ("`research/`", "`docs/dev_notes/`", "`docs/reviews/`", "scripts/kimi_check.py"):
            self.assertIn(s, blk, s)
        self.assertNotIn("هم به `/opt/bitpin-bot` کپی می‌شوند", blk)    # the v2 sentence

    def test_the_test_command_is_the_one_update_sh_runs(self):
        self.assertIn("PYTHONDONTWRITEBYTECODE=1 TZ=UTC python3 -m unittest discover -s tests", self.text)
        self.assertIn("test_prompt_template.py", self.text)


# --------------------------------------------------------------------------- research/README.md

@unittest.skipUnless(os.path.isdir(os.path.join(ROOT, "research")),
                     "research/ is not copied to the server (deploy/lib.sh stage_code): update.sh runs the tests there")
class TestResearchReadme(unittest.TestCase):
    def test_the_offline_v3_studies_are_listed(self):
        text = read("research", "README.md")
        blk = text[text.index("## مطالعه‌های نسخهٔ ۳"):]
        for s in ("`arch_review/`", "`year_review/`", "`prompt_review/`", "`rwa_scan.json`", "ROADMAP.md",
                  "Y1", "Y2", "Y3", "Y4", "Y5", "assemble_and_check.py", "STRATEGY_KNOWLEDGE.md",
                  "CHANGELOG.md"):
            self.assertIn(s, blk, s)
        # they are NOT in the repo (no dangling relative links like the 01..04 rows have)
        self.assertNotIn("](year_review", blk)
        self.assertNotIn("](prompt_review", blk)
        self.assertNotIn("](arch_review", blk)
        self.assertIn("در این ریپو نیستند", blk)
        # the original four studies stay
        for d in ("01_rule_strategies", "02_decision_frequency", "03_pump_study", "04_universe"):
            self.assertTrue(os.path.isdir(os.path.join(ROOT, "research", d)), d)
            self.assertIn(d, text)


# --------------------------------------------------------------------------- scripts/check_balance.py

class TestCheckBalanceDocstring(unittest.TestCase):
    def setUp(self):
        src = read("scripts", "check_balance.py")
        self.doc = ast.get_docstring(ast.parse(src))
        self.src = src

    def test_it_is_a_valid_docstring_without_invalid_escapes(self):
        """The usage line shows a path; a backslash there ("\\c") is an invalid escape that Python
        3.12+ warns about at compile time. compile() with warnings as errors proves the file is clean."""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(self.src, "check_balance.py", "exec")
        self.assertNotIn("\\", self.doc)

    def test_the_facts_a_reader_needs(self):
        for s in ("Read-only", "NO orders", "scripts/check_balance.py", "either order", "never the key",
                  "DIRECTLY", "1-2 of the 200 daily", "diagnostic", "bitpin-bot status"):
            self.assertIn(s, self.doc, s)
        # no stale claim that it must be run from the scripts folder
        self.assertNotIn("from the folder that contains this file", self.doc)

    def test_main_prints_the_docstring_on_bad_usage(self):
        self.assertIn("sys.exit(__doc__)", self.src)


if __name__ == "__main__":
    unittest.main()
