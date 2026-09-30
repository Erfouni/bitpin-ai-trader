# -*- coding: utf-8 -*-
"""bitpin.panel_i18n (v3.2): the panel's two languages.

Every text the panel shows is English in bitpin/panel_web.py and has a Persian translation in panel_i18n.FA;
the checks here read panel_web.py's source, so a new text without a translation (or a translation left over
after its text was removed) fails the tests, not the owner's screen."""
import ast
import os
import re
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bitpin import panel_i18n as i18n  # noqa: E402
from bitpin.panel_i18n import FA, Text, negotiate, tr, use  # noqa: E402

PLACEHOLDER_RE = re.compile(r"%(?:\([a-z_]+\))?[sd]")
TAG_RE = re.compile(r"</?[a-z]+[^>]*>")
ENTITY_RE = re.compile(r"&#?[a-z0-9]+;")


def _literal(node):
    """A string literal's value (ast.Constant from Python 3.8 on, ast.Str before), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if hasattr(ast, "Str") and isinstance(node, getattr(ast, "Str")) and isinstance(node.s, str):
        return node.s
    return None


def panel_texts():
    """{text: line} of every literal given to tr(), te() or N_() in bitpin/panel_web.py."""
    with open(os.path.join(ROOT, "bitpin", "panel_web.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("tr", "te", "N_")                 and node.args and _literal(node.args[0]) is not None:
            out.setdefault(_literal(node.args[0]), node.lineno)
    return out


class TestCatalog(unittest.TestCase):
    def test_every_text_has_a_persian_translation(self):
        texts = panel_texts()
        self.assertGreater(len(texts), 250)
        missing = sorted((line, t) for t, line in texts.items() if t not in FA)
        self.assertEqual(missing, [], "texts of bitpin/panel_web.py without a Persian translation")

    def test_no_translation_is_left_over(self):
        texts = panel_texts()
        self.assertEqual(sorted(k for k in FA if k not in texts), [], "translations of texts that are gone")

    def test_translations_keep_placeholders_and_markup(self):
        for en, fa in FA.items():
            self.assertTrue(fa.strip(), en)
            self.assertEqual(sorted(PLACEHOLDER_RE.findall(en)), sorted(PLACEHOLDER_RE.findall(fa)), en)
            self.assertEqual(sorted(TAG_RE.findall(en)), sorted(TAG_RE.findall(fa)), en)
            self.assertEqual(ENTITY_RE.findall(en), ENTITY_RE.findall(fa), en)  # &middot; &#8212;

    def test_translations_are_persian(self):
        same = [en for en, fa in FA.items() if en == fa]
        self.assertEqual(same, ["VPN"])                                        # a word Persian uses as it is
        for en, fa in FA.items():
            if en != fa:
                self.assertTrue(any(u"؀" <= ch <= u"ۿ" for ch in fa), en)


class TestLanguageChoice(unittest.TestCase):
    def test_negotiate(self):
        cases = [(None, "en"), ("", "en"), ("fa", "fa"), ("fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7", "fa"),
                 ("en-US,en;q=0.9,fa;q=0.8", "en"), ("de-DE,de;q=0.9", "en"), ("de,fa;q=0.5", "fa"),
                 ("fa;q=0.3,en;q=0.3", "fa"), ("en;q=0, fa;q=0.1", "fa"), ("FA-ir", "fa"), ("*", "en"),
                 ("fa;q=abc", "en"), ("x" * 5000, "en"), ("en-GB;q=0.5,fa-AF;q=0.6", "fa")]
        for header, want in cases:
            self.assertEqual(negotiate(header), want, header)

    def test_tr_follows_the_current_language(self):
        pair = Text(u"روز", "days")
        with use("fa"):
            self.assertEqual(tr("Dashboard"), u"داشبورد")
            self.assertEqual(tr(pair), u"روز")
            self.assertEqual(tr("not a panel text"), "not a panel text")      # unknown: as written
            self.assertTrue(i18n.is_rtl())
            with use("en"):
                self.assertEqual(tr("Dashboard"), "Dashboard")
                self.assertEqual(tr(pair), "days")
            self.assertEqual(i18n.current(), "fa")                             # the outer language is back
        self.assertEqual(i18n.current(), i18n.DEFAULT_LANG)
        with use("klingon"):
            self.assertEqual(i18n.current(), "en")

    def test_the_language_is_per_thread(self):
        seen = []
        i18n.set_lang("fa")
        try:
            t = threading.Thread(target=lambda: seen.append(i18n.current()))
            t.start()
            t.join()
            self.assertEqual(seen, ["en"])                                     # another request's thread
            self.assertEqual(i18n.current(), "fa")
        finally:
            i18n.reset_lang()
        self.assertEqual(i18n.current(), "en")


if __name__ == "__main__":
    unittest.main()
