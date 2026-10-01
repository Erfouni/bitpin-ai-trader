# -*- coding: utf-8 -*-
"""v3.7: the panel's side of the feed news - the dashboard's news card (scripts/panel_helper.py news_view, PanelApp.
_news_card) and the news settings of the trade form (news.mode, news.feeds)."""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from bitpin import panel_settings as ps  # noqa: E402
from bitpin.news import NewsBrief  # noqa: E402
import panel_helper as ph  # noqa: E402
import test_panel_web as tpw  # noqa: E402

T0 = tpw.T0                                   # 2026-09-22 11:00 UTC


def cache_doc(**over):
    brief = NewsBrief(ok=True, text="1. ...", fetched_at=T0 - 600, model="kimi-k2.6", mode="feeds",
                      summary="Risk-off after an exchange hack.",
                      items=[{"headline": "Exchange X hacked, hot wallet drained", "why_it_matters": "risk-off for alts",
                              "source_url": "https://www.coindesk.com",
                              "link": "https://www.coindesk.com/business/x-hacked/", "time_hint": "2026-09-22 10:00 UTC"},
                             {"headline": "Fed holds rates", "why_it_matters": "calm", "source_url": "https://www.reuters.com",
                              "time_hint": "2026-09-22"}],
                      feeds={"tried": 4, "ok": 3, "headlines": 40, "failed_count": 1, "sites": {"coindesk.com": 20},
                             "failed": [{"site": "apnews.com", "url": "https://apnews.com/x", "error": "HTTP 403"}]})
    d = {"version": 2, "brief": brief.to_dict(), "last_error": "", "last_error_at": None,
         "feeds": dict(brief.feeds), "feeds_at": T0 - 610}
    d.update(over)
    return d


class TestNewsView(unittest.TestCase):
    def test_the_view_of_the_cache(self):
        v = ph.news_view(cache_doc())
        self.assertEqual((v["fetched_at"], v["mode"], v["model"]), (T0 - 600, "feeds", "kimi-k2.6"))
        self.assertEqual(v["items"][0], {"headline": "Exchange X hacked, hot wallet drained", "why": "risk-off for alts",
                                         "time": T0 - 3600, "site": "coindesk.com",
                                         "link": "https://www.coindesk.com/business/x-hacked/"})
        self.assertEqual((v["items"][1]["time"], v["items"][1]["link"]), ("2026-09-22", ""))
        self.assertEqual((v["feeds"]["ok"], v["feeds"]["failed"][0]["site"]), (3, "apnews.com"))
        self.assertIsNone(ph.news_view({}))
        self.assertIsNone(ph.news_view(None))

    def test_a_failed_attempt_without_a_brief(self):
        v = ph.news_view({"version": 2, "last_error": "no recent headline (0 of 26 feeds read)",
                          "last_error_at": T0, "feeds": {"tried": 26, "ok": 0, "failed": [{"site": "x.com", "error": 5}]}})
        self.assertEqual((v["fetched_at"], v["items"], v["feeds"]["tried"]), (None, [], 26))
        self.assertIn("0 of 26 feeds read", v["last_error"])

    def test_hostile_cache_values_are_cleaned(self):
        d = cache_doc()
        d["brief"]["items"][0]["link"] = "javascript:alert(1)"
        d["brief"]["items"][0]["headline"] = tpw.HOSTILE
        d["feeds"]["failed"][0]["url"] = "javascript:x"
        v = ph.news_view(d)
        self.assertEqual(v["items"][0]["link"], "")
        self.assertEqual(v["feeds"]["failed"][0]["url"], "")


class TestNewsCard(tpw.PanelCase):
    def setUp(self):
        super(TestNewsCard, self).setUp()
        self.c.cookies["__Host-bplang"] = "en"                     # the English texts; the Persian ones below

    def status_with(self, news):
        st = tpw.status_data()
        st["news"] = news
        self.helper.responses["status"] = st

    def test_the_card(self):
        self.login()
        self.status_with(ph.news_view(cache_doc()))
        t = self.c.get("/").text
        self.assertIn("News brief", t)
        self.assertIn('<a href="https://www.coindesk.com/business/x-hacked/" target="_blank" '
                      'rel="noopener noreferrer nofollow">Exchange X hacked, hot wallet drained</a>', t)
        self.assertIn('coindesk.com &middot; <bdi dir="ltr">2026-09-22 13:30</bdi>', t)   # 10:00 UTC as Tehran time
        self.assertIn("risk-off for alts", t)
        self.assertIn("Risk-off after an exchange hack.", t)
        self.assertIn("From the trusted sources&#x27; feeds", t)
        self.assertIn('Feeds: <bdi dir="ltr">3</bdi> of <bdi dir="ltr">4</bdi> read, <bdi dir="ltr">40</bdi> headlines',
                      t)
        self.assertIn('<bdi dir="ltr">1</bdi> could not be read', t)
        self.assertIn("HTTP 403", t)
        self.assertNotIn("The last news attempt failed", t)
        self.c.cookies["__Host-bplang"] = "fa"
        t = self.c.get("/").text
        self.assertIn(u"خلاصه‌ی خبر", t)
        self.assertIn(u'فیدها: <bdi dir="ltr">3</bdi> از <bdi dir="ltr">4</bdi> خوانده شد، <bdi dir="ltr">40</bdi> تیتر', t)

    def test_a_failure_after_the_brief_is_shown(self):
        self.login()
        self.status_with(ph.news_view(cache_doc(last_error="no recent headline (0 of 26 feeds read)",
                                                last_error_at=T0)))
        t = self.c.get("/").text
        self.assertIn("The last news attempt failed", t)
        self.assertIn("0 of 26 feeds read", t)

    def test_no_brief_and_hostile_values(self):
        self.login()
        self.status_with(None)
        self.assertIn("No news brief yet.", self.c.get("/").text)
        v = ph.news_view(cache_doc())
        v["items"][0]["headline"] = tpw.HOSTILE
        v["items"][0]["link"] = 'javascript:alert("x")'
        v["summary"] = tpw.HOSTILE
        v["feeds"]["failed"][0]["error"] = tpw.HOSTILE
        self.status_with(v)
        t = self.c.get("/").text
        self.assertEscaped(t, "news card")
        self.assertNotIn("javascript:", t)


class TestNewsSettings(unittest.TestCase):
    def field(self, key):
        return next(f for f in ps.FIELDS if f.key == key)

    def test_the_news_mode(self):
        f = self.field("kimi:news.mode")
        self.assertEqual([v for v, _l in f.choices], ["feeds", "search"])
        self.assertEqual(ps.parse_value(f, "search"), "search")
        self.assertEqual(ps.check_value(f, "feeds"), "feeds")
        with self.assertRaises(ValueError):
            ps.check_value(f, "rss")
        self.assertEqual(ps.form_values({}, {"news": {}})["kimi:news.mode"], "feeds")       # the built-in default

    def test_the_extra_feeds(self):
        f = self.field("kimi:news.feeds")
        text = " https://www.tasnimnews.ir/rss \n\nhttps://news.example.com/feed?a=1,b=2\nhttps://www.tasnimnews.ir/rss"
        self.assertEqual(ps.parse_value(f, text), ["https://www.tasnimnews.ir/rss",
                                                   "https://news.example.com/feed?a=1,b=2"])
        self.assertIsNone(ps.parse_value(f, "  "))                         # empty = only the built-in feeds
        for bad in ("ftp://x.com/rss", "https://127.0.0.1/rss", "not a url", "https://user:pw@x.com/rss"):
            with self.assertRaises(ps.FieldError, msg=bad):
                ps.parse_value(f, bad)
        self.assertEqual(ps.check_value(f, ["https://x.com/rss"]), ["https://x.com/rss"])
        self.assertIsNone(ps.check_value(f, None))
        for bad in (["javascript:x"], ["https://x.com/rss", "https://x.com/rss"], "https://x.com/rss", [5]):
            with self.assertRaises(ValueError, msg=str(bad)):
                ps.check_value(f, bad)
        self.assertEqual(ps.display(f, ["https://a.com/rss", "https://b.com/feed"]),
                         "https://a.com/rss\nhttps://b.com/feed")
        self.assertEqual(ps.form_values({}, {"news": {}})["kimi:news.feeds"], "")

    def test_applying_writes_only_what_changed(self):
        kimi = {"news": {"model": "kimi-k2.6"}}
        changes = [{"file": "kimi", "path": ["news", "mode"], "value": "search"},
                   {"file": "kimi", "path": ["news", "feeds"], "value": ["https://www.tasnimnews.ir/rss"]}]
        _c, k2, applied, errors = ps.apply_changes({}, copy.deepcopy(kimi), changes)
        self.assertEqual(errors, [])
        self.assertEqual(k2["news"]["mode"], "search")
        self.assertEqual(k2["news"]["feeds"], ["https://www.tasnimnews.ir/rss"])
        _c, k3, applied, errors = ps.apply_changes({}, copy.deepcopy(kimi),
                                                   [{"file": "kimi", "path": ["news", "mode"], "value": "feeds"},
                                                    {"file": "kimi", "path": ["news", "feeds"], "value": None}])
        self.assertEqual((errors, applied), ([], []))                     # the defaults: nothing to write
        self.assertEqual(json.dumps(k3, sort_keys=True), json.dumps(kimi, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
