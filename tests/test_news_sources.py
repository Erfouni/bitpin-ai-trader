"""v3.5 "trusted news": the trusted source list (news.sources), a reply cut off by max_tokens (the complete items
salvaged, one retry, the unreadable reply saved), the researcher prompt, the example files, the panel field and
the longer time for Kimi's decision."""
import io
import json
import os
import stat
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from bitpin import news as news_mod  # noqa: E402
from bitpin.news import (DEFAULT_NEWS_SOURCES, LENGTH_NUDGE, NewsConfigError, build_news_messages,  # noqa: E402
                         check_sources, fresh_search_nudge, has_recent_news, normalize_source, salvage_reply,
                         sanitize_reply, source_allowed, validate_news_config)
import test_news as tn  # noqa: E402  (the fixtures: fake transport, clock, reply builders)

T0 = tn.T0                   # 2026-09-22 11:00 UTC
TRUSTED = list(DEFAULT_NEWS_SOURCES)


def item(headline, url, when="2026-09-21", why="It matters for crypto"):
    return {"headline": headline, "why_it_matters": why, "source_url": url, "time_hint": when}


MIXED = json.dumps({"items": [
    item("Fed holds rates", "https://www.reuters.com/markets/fed-holds/"),
    item("Bitcoin ETF inflows rise", "https://www.coindesk.com/markets/etf/"),
    item("Rumour of a big listing", "https://www.cryptoblogspam.example/rumour"),
], "summary": "Risk-on; a big listing is rumoured."})
OTHER_ONLY = json.dumps({"items": [item("A blog post", "https://www.cryptoblogspam.example/x")], "summary": "s"})
CUT = ("Let me look at the results first. " * 20 + '{"items": [' +
       json.dumps(item("Fed holds rates", "https://www.reuters.com/markets/fed-holds/")) + ", " +
       json.dumps(item("Bitcoin ETF inflows rise", "https://www.coindesk.com/markets/etf/")) +
       ', {"headline": "Iran rial slides as talks st')
RAMBLING = "I searched and found many things about the market. " * 300


class TestSourceList(unittest.TestCase):
    def test_entries_are_normalized_to_bare_domains(self):
        for raw, want in (("reuters.com", "reuters.com"), ("https://www.Reuters.com/markets/x", "reuters.com"),
                          ("WWW.BBC.CO.UK", "bbc.co.uk"), ("en.irna.ir", "en.irna.ir"), ("coindesk.com/", "coindesk.com"),
                          ("  theblock.co  ", "theblock.co")):
            self.assertEqual(normalize_source(raw), want, raw)
        for bad in ("", "reuters", "a.b", "not a domain", "http://", "1.2.3.4", None, 5, "exa mple.com"):
            self.assertIsNone(normalize_source(bad), bad)

    def test_the_default_is_the_trusted_list_and_null_keeps_it(self):
        self.assertEqual(validate_news_config({})["sources"], TRUSTED)
        self.assertEqual(validate_news_config({"sources": None})["sources"], TRUSTED)
        self.assertEqual(validate_news_config({"sources": []})["sources"], [])
        self.assertEqual(validate_news_config({"sources": ["https://www.reuters.com", "reuters.com", "isna.ir"]})
                         ["sources"], ["reuters.com", "isna.ir"])
        for bad in ("reuters.com", ["reuters"], [5], ["s%d.example.com" % i for i in range(101)]):
            with self.assertRaises(NewsConfigError):
                validate_news_config({"sources": bad})

    def test_the_default_list_covers_the_four_groups(self):
        for d in ("reuters.com", "apnews.com", "bloomberg.com", "bbc.com", "coindesk.com", "theblock.co",
                  "donya-e-eqtesad.com", "isna.ir", "irna.ir", "iranintl.com", "radiofarda.com", "federalreserve.gov",
                  "sec.gov", "bitpin.ir"):
            self.assertIn(d, TRUSTED)
        self.assertEqual(check_sources(TRUSTED), TRUSTED)          # already normalized, no duplicates

    def test_a_site_counts_with_its_subdomains_only(self):
        self.assertTrue(source_allowed("https://www.reuters.com/x", ["reuters.com"]))
        self.assertTrue(source_allowed("https://en.irna.ir/news/1", ["irna.ir"]))
        self.assertFalse(source_allowed("https://notreuters.com/x", ["reuters.com"]))
        self.assertFalse(source_allowed("https://reuters.com.evil.example/x", ["reuters.com"]))
        self.assertFalse(source_allowed("", ["reuters.com"]))
        self.assertFalse(source_allowed("javascript:alert(1)", ["reuters.com"]))
        self.assertTrue(source_allowed("", []))                    # no list: any site


class TestFilter(unittest.TestCase):
    def test_items_from_other_sites_are_left_out_with_the_summary(self):
        counts = {}
        items, summary, text, dropped = sanitize_reply(json.loads(MIXED), 10, 2500, now=T0, counts=counts,
                                                       sources=TRUSTED)
        self.assertEqual([i["source_url"] for i in items], ["https://www.reuters.com", "https://www.coindesk.com"])
        self.assertEqual((dropped, counts["off_source"]), (1, 1))
        self.assertEqual(summary, "")                              # it rested on the untrusted rumour
        self.assertNotIn("rumour", text.lower())
        self.assertIn("1 item from sites outside the trusted source list was left out", text)

    def test_without_a_list_every_site_is_kept(self):
        items, summary, _, dropped = sanitize_reply(json.loads(MIXED), 10, 2500, now=T0, sources=[])
        self.assertEqual((len(items), dropped), (3, 0))
        self.assertIn("rumoured", summary)

    def test_an_item_without_a_source_is_left_out(self):
        obj = {"items": [{"headline": "Unsourced claim", "why_it_matters": "x", "time_hint": "2026-09-21"}]}
        items, summary, _, _ = sanitize_reply(obj, 10, 2500, now=T0, sources=TRUSTED)
        self.assertEqual((items, summary), ([], news_mod.OFF_SOURCE_SUMMARY))

    def test_an_old_item_counts_as_old_first(self):
        obj = {"items": [item("Old story", "https://www.cryptoblogspam.example/a", "July 2025"),
                         item("Other site", "https://www.cryptoblogspam.example/b")], "summary": "s"}
        counts = {}
        items, summary, _, _ = sanitize_reply(obj, 10, 2500, now=T0, counts=counts, sources=TRUSTED)
        self.assertEqual((items, counts["old"], counts["off_source"]), ([], 1, 1))
        self.assertEqual(summary, news_mod.NOTHING_USABLE_SUMMARY)

    def test_recent_news_needs_a_trusted_source(self):
        self.assertTrue(has_recent_news(MIXED, T0, TRUSTED))
        self.assertFalse(has_recent_news(OTHER_ONLY, T0, TRUSTED))
        self.assertTrue(has_recent_news(OTHER_ONLY, T0, []))


class TestPrompt(unittest.TestCase):
    def test_the_researcher_is_told_to_search_and_cite_only_the_list(self):
        system = build_news_messages(T0, sources=TRUSTED)[0]["content"]
        self.assertIn("SOURCES: cite ONLY articles from these sites (their subdomains count): reuters.com, apnews.com",
                      system)
        self.assertIn("when a filtered search finds nothing, search without the filter", system)   # v3.5.3
        self.assertIn("site:coindesk.com", system)
        self.assertIn("Base the summary ONLY on the items you list.", system)
        plugin = build_news_messages(T0, sources=TRUSTED, provider="openrouter")[0]["content"]
        self.assertIn("SOURCES: use ONLY results from these sites", plugin)
        self.assertNotIn("site:", plugin)
        free = build_news_messages(T0, sources=())[0]["content"]
        self.assertNotIn("SOURCES:", free)
        self.assertIn("Base the summary ONLY on the items you list.", free)

    def test_the_second_search_names_the_sites(self):
        text = fresh_search_nudge(T0, TRUSTED)
        self.assertIn('"September 2026" in every query', text)
        self.assertIn("site:coindesk.com", text)
        self.assertIn("from the listed sites", text)
        self.assertNotIn("listed sites", fresh_search_nudge(T0))


class TestResearchWithTrustedSources(tn.NewsTestBase):
    def test_the_default_keeps_only_trusted_items(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(MIXED))], sources=None)
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(b.items), 2)
        self.assertEqual(len(tr.requests), 2)                       # a trusted recent item: no second search
        self.assertIn("SOURCES: cite ONLY articles from these sites", tr.requests[0]["body"]["messages"][0]["content"])
        self.assertIn("left out, and the researcher's summary with it", b.text)

    def test_only_untrusted_items_get_one_more_search_naming_the_sites(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(OTHER_ONLY)),
                                 tn.tool_round(tn.call(2, tn.ARGS2)), (200, tn.completion(tn.GOOD))], sources=None)
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(tr.requests), 4)
        self.assertIn("NOT USABLE", tr.requests[2]["body"]["messages"][-1]["content"])
        self.assertEqual(len(b.items), 2)


class TestCutReply(tn.NewsTestBase):
    def test_salvage_keeps_the_complete_items(self):
        obj = salvage_reply(CUT)
        self.assertEqual([i["headline"] for i in obj["items"]], ["Fed holds rates", "Bitcoin ETF inflows rise"])
        self.assertEqual(obj["summary"], "")
        self.assertIsNone(salvage_reply("no json at all"))
        self.assertIsNone(salvage_reply('{"items": [{"headline": "cut'))
        self.assertIsNone(salvage_reply(None))

    def test_a_cut_reply_with_complete_items_is_used_without_a_retry(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(CUT, "length"))],
                                sources=None)
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual((len(tr.requests), len(b.items)), (2, 2))

    def test_a_cut_reply_without_any_item_is_asked_for_once_more(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(RAMBLING, "length")),
                                 (200, tn.completion(tn.GOOD))], sources=None)
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(tr.requests), 3)
        last = tr.requests[2]["body"]
        self.assertNotIn("tools", last)                               # the search tool is withdrawn
        self.assertEqual(last["messages"][-1], {"role": "user", "content": LENGTH_NUDGE % 10})
        self.assertNotIn(RAMBLING[:40], json.dumps(last["messages"]))  # the cut-off text is not sent back
        self.assertEqual(len(b.items), 2)

    def test_a_second_cut_reply_fails_and_is_saved_for_the_diagnosis(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(RAMBLING, "length")),
                                 (200, tn.completion(RAMBLING, "length"))], sources=None)
        b = r.research(now=T0)
        self.assertFalse(b.ok)
        self.assertEqual(len(tr.requests), 3)
        path = os.path.join(self.dir, news_mod.FAILED_REPLY_FILE)
        with io.open(path, encoding="utf-8") as f:
            saved = f.read()
        self.assertIn("I searched and found many things", saved)
        self.assertIn("%d characters" % len(RAMBLING), saved)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class TestPluginCutReply(tn.OpenRouterNewsBase):
    def test_a_cut_reply_is_asked_for_once_more_keeping_the_search_line(self):
        cut = tn.or_reply(RAMBLING)
        cut["choices"][0]["finish_reason"] = "length"
        r, tr = self.or_researcher([(200, cut), (200, tn.or_reply(tn.GOOD))], stream=False, sources=None)
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(tr.requests), 2)
        first, second = tr.requests[0]["body"]["messages"][-1], tr.requests[1]["body"]["messages"][-1]
        self.assertEqual(second["content"].splitlines()[0], first["content"].splitlines()[0])
        self.assertTrue(second["content"].endswith(LENGTH_NUDGE % 10))


class TestExamplesPanelAndTime(unittest.TestCase):
    def test_both_examples_ship_the_default_list_and_the_longer_reply(self):
        for name in ("kimi.example.json", "news.example.json"):
            with io.open(os.path.join(ROOT, name), encoding="utf-8") as f:
                doc = json.load(f)
            sec = doc.get("news", doc)
            self.assertEqual(sec["sources"], TRUSTED, name)
            self.assertIn("DEFAULT_NEWS_SOURCES", sec["_sources"], name)
            self.assertEqual(sec["max_tokens"], 16000, name)

    def test_the_panel_edits_the_list_one_site_per_line(self):
        from bitpin import panel_settings as ps
        f = ps.BY_KEY["kimi:news.sources"]
        self.assertEqual((f.kind, f.group), ("domains", "news"))
        self.assertEqual(ps.display(f, ["reuters.com", "isna.ir"]), "reuters.com\nisna.ir")
        self.assertEqual(ps.parse_value(f, "https://www.Reuters.com/x\nisna.ir, reuters.com"), ["reuters.com", "isna.ir"])
        for bad in ("", "not a site"):
            with self.assertRaises(ps.FieldError):
                ps.parse_value(f, bad)
        self.assertEqual(ps.check_value(f, ["reuters.com"]), ["reuters.com"])
        for bad in (["Reuters.com"], ["www.reuters.com"], "reuters.com", [5], []):
            with self.assertRaises(ValueError):
                ps.check_value(f, bad)
        # a missing key shows the built-in list, and saving it unchanged writes nothing (no new confirmation)
        self.assertEqual(ps.form_values({}, {"news": {}})["kimi:news.sources"].splitlines(), TRUSTED)
        _, _, applied, errors = ps.apply_changes({}, {"news": {}}, [{"file": "kimi", "path": ["news", "sources"],
                                                                     "value": TRUSTED}])
        self.assertEqual((applied, errors), ([], []))

    def test_kimi_gets_more_time_and_the_profile_carries_it(self):
        from bitpin import llm
        self.assertEqual(llm.validate_llm_config({"timeout": 900})["timeout"], 900)
        with self.assertRaises(Exception):
            llm.validate_llm_config({"timeout": 1801})
        with io.open(os.path.join(ROOT, "kimi.example.json"), encoding="utf-8") as f:
            k = json.load(f)
        self.assertEqual((k["llm"]["timeout"], k["llm"]["deadline_seconds"], k["brain"]["decision_deadline_seconds"]),
                         (900, 1200, 1500))
        sys.path.insert(0, os.path.join(ROOT, "deploy"))
        import apply_profile
        self.assertIn("max_tokens", apply_profile.PROFILE_KIMI["news"])
        self.assertNotIn("sources", apply_profile.PROFILE_KIMI["news"])      # the owner's list is never overwritten


if __name__ == "__main__":
    unittest.main()
