# -*- coding: utf-8 -*-
"""v3.7: the news brief from the trusted sources' own feeds (bitpin/news_feeds.py, news.mode "feeds"): parsing RSS,
Atom, RDF and Google News sitemaps safely, picking the headlines, the one JSON-mode request without tools, and the
items' links and dates taken from the feed (the model only cites a headline by its number)."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bitpin import news as news_mod  # noqa: E402
from bitpin import news_feeds as nf  # noqa: E402
from bitpin.news import NewsConfigError, NewsResearcher, validate_news_config  # noqa: E402
import test_news as tn  # noqa: E402  (the fixtures: fake transport, clock, reply builders)

T0 = tn.T0                                   # 2026-09-22 11:00 UTC
DAY = "Tue, 22 Sep 2026"


def rss(*items, extra=""):
    body = "".join("<item><title>%s</title><link>%s</link><pubDate>%s</pubDate><description>%s</description></item>"
                   % it for it in items)
    return ('<?xml version="1.0" encoding="UTF-8"?>%s<rss version="2.0"><channel><title>Feed</title>%s</channel>'
            '</rss>' % (extra, body)).encode("utf-8")


RSS_REUTERS = rss(("Fed holds rates, signals patience", "https://www.reuters.com/markets/us/fed-holds/",
                   DAY + " 09:00:00 +0000", "&lt;p&gt;The Fed kept rates on hold.&lt;/p&gt;"),
                  ("Old story", "https://www.reuters.com/markets/old/", "Tue, 08 Sep 2026 10:00:00 +0000", ""),
                  ("Off-site story", "https://www.example.org/x/", DAY + " 08:00:00 +0000", ""))
RSS_COINDESK = rss(("Bitcoin ETF inflows continue", "https://www.coindesk.com/markets/etf-inflows/",
                    DAY + " 10:30:00 +0000", "Inflows for a fifth day."),
                   ("Exchange X hacked", "https://www.coindesk.com/business/x-hacked/", DAY + " 10:00:00 +0000",
                    "Hot wallet drained."))
RSS_IRNA = rss((u"بانک مرکزی نرخ ارز را اعلام کرد", "https://www.irna.ir/news/123/", DAY + " 13:00:00 +0330",
                u"بانک‌ مرکزی امروز"),)
ATOM = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>'
        b'<entry><title type="html">Atom &amp;lt;b&amp;gt;item&amp;lt;/b&amp;gt;</title>'
        b'<link rel="alternate" href="https://cointelegraph.com/news/atom-item"/>'
        b'<link rel="enclosure" href="https://cdn.example/img.jpg"/>'
        b'<updated>2026-09-22T08:15:00Z</updated><summary>An Atom summary</summary>'
        b'<source><title>Not this title</title></source></entry></feed>')
SITEMAP = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
           b'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9" '
           b'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"><url>'
           b'<loc>https://www.reuters.com/world/sitemap-story/</loc>'
           b'<image:image><image:loc>https://img/x.jpg</image:loc><image:title>Image title</image:title></image:image>'
           b'<news:news><news:publication><news:name>Reuters</news:name></news:publication>'
           b'<news:publication_date>2026-09-22T07:00:00+00:00</news:publication_date>'
           b'<news:title>Sitemap story title</news:title></news:news></url></urlset>')
RDF = (b'<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
       b'xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
       b'<item><title>RDF item</title><link>https://decrypt.co/1/rdf</link><dc:date>2026-09-22T06:00:00+03:30</dc:date>'
       b'</item></rdf:RDF>')


def reply(items, summary="Mixed."):
    return json.dumps({"items": items, "summary": summary})


class FakeFeeds(object):
    """feed_fetch(url, route, timeout) from a mapping url -> (status, body) or an exception; records the calls."""

    def __init__(self, table=None, default=None):
        self.table = dict(table or {})
        self.default = default
        self.calls = []

    def __call__(self, url, route, timeout):
        self.calls.append((url, route, timeout))
        r = self.table.get((url, route), self.table.get(url, self.default))
        if r is None:
            return 404, b""
        if isinstance(r, Exception):
            raise r
        return r


def table_url(site):
    return next(u for s, u, _r in nf.FEED_TABLE if s == site)


class TestParsing(unittest.TestCase):
    def test_rss_atom_sitemap_and_rdf(self):
        e = nf.parse_feed(RSS_REUTERS)
        self.assertEqual(len(e), 3)
        self.assertEqual(e[0][0], "Fed holds rates, signals patience")
        self.assertEqual(e[0][1], "https://www.reuters.com/markets/us/fed-holds/")
        self.assertEqual(e[0][2], T0 - 2 * 3600)
        a = nf.parse_feed(ATOM)
        self.assertEqual(a[0][1], "https://cointelegraph.com/news/atom-item")      # rel=alternate, not the enclosure
        self.assertNotIn("Not this title", a[0][0])                               # the <source>'s title is skipped
        self.assertEqual(nf.clean_feed_text(a[0][0], 200), "Atom item")           # escaped markup removed
        s = nf.parse_feed(SITEMAP)
        self.assertEqual(s[0][:3], ("Sitemap story title", "https://www.reuters.com/world/sitemap-story/",
                                    T0 - 4 * 3600))
        r = nf.parse_feed(RDF)
        self.assertEqual((r[0][0], r[0][1], r[0][2]), ("RDF item", "https://decrypt.co/1/rdf", T0 - 8.5 * 3600))

    def test_dates(self):
        self.assertEqual(nf.parse_date("Thu, 01 Oct 2026 12:23:27 +0330"), nf.parse_date("2026-10-01T08:53:27Z"))
        self.assertEqual(nf.parse_date("2026-10-01T12:23:27+03:30"), nf.parse_date("2026-10-01 08:53:27"))
        self.assertEqual(nf.parse_date("2026-10-01"), nf.parse_date("2026-10-01T00:00:00Z"))
        for bad in ("", "yesterday", "2026-13-40", None, 5, "x" * 100):
            self.assertIsNone(nf.parse_date(bad), bad)

    def test_unsafe_or_unreadable_bodies_are_refused(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
                b'<rss><channel><item><title>&lol2;</title></item></channel></rss>')
        for body, why in ((bomb, "entities"), (b'<!DOCTYPE rss [ ]><rss/>', "entities"),
                          (b"<!DOCTYPE html><html><body>hi</body></html>", "not a feed"), (b"", "empty"),
                          (b"<html><body>unclosed", "HTML page"),
                          (b"not xml at all", "not XML"), (b"<?xml version='1.0'?><svg></svg>", "not a feed"),
                          (b"<rss>" + b"x" * (nf.FEED_MAX_BYTES + 1) + b"</rss>", "larger")):
            with self.assertRaises(nf.FeedError) as cm:
                nf.parse_feed(body)
            self.assertIn(why, str(cm.exception), body[:40])
        # an external DOCTYPE without an internal subset is harmless (expat never fetches it)
        self.assertEqual(len(nf.parse_feed(b'<!DOCTYPE rss SYSTEM "http://x/rss.dtd">' + RSS_COINDESK[38:])), 2)

    def test_text_cleaning_keeps_persian_joiners_and_drops_overrides(self):
        t = nf.clean_feed_text(u"<b>بانک‌ها</b> ‮evil‬  x\x00y", 100)
        self.assertEqual(t, u"بانک‌ها evil xy")
        self.assertEqual(nf.clean_feed_text("a" * 50, 10), "a" * 9 + u"…")

    def test_links(self):
        self.assertEqual(nf.clean_link("https://www.reuters.com/a/b?c=1"), "https://www.reuters.com/a/b?c=1")
        for bad in ("javascript:alert(1)", "https://user:pw@x.com/", "https://x.com/a b", "ftp://x.com/",
                    "https://x.com/" + "a" * 500, "", None, 'https://x.com/"q"'):
            self.assertEqual(nf.clean_link(bad), "", bad)


class TestSelection(unittest.TestCase):
    def results(self, **bodies):
        out = []
        for site, body in bodies.items():
            site = site.replace("_", ".")
            out.append(nf.FeedResult(nf.Feed(site, "https://feed/" + site, "proxy"), True, 200, "",
                                     nf.parse_feed(body), 0.1, "proxy"))
        return out

    def test_recent_trusted_and_unique_headlines_newest_first(self):
        sources = ["reuters.com", "coindesk.com", "irna.ir"]
        res = self.results(reuters_com=RSS_REUTERS, coindesk_com=RSS_COINDESK + b"", irna_ir=RSS_IRNA)
        res.append(nf.FeedResult(nf.Feed("coindesk.com", "https://feed/2", "proxy"), True, 200, "",
                                 nf.parse_feed(RSS_COINDESK), 0.1, "proxy"))          # the same items again
        hs = nf.select_headlines(res, sources, T0)
        self.assertEqual([h.title for h in hs], ["Bitcoin ETF inflows continue", "Exchange X hacked",
                                                 u"بانک مرکزی نرخ ارز را اعلام کرد",
                                                 "Fed holds rates, signals patience"])
        self.assertEqual(hs[3].summary, "The Fed kept rates on hold.")
        self.assertEqual(hs[2].published, T0 - 5400)                   # 13:00 +0330 = 09:30 UTC
        self.assertNotIn("Old story", [h.title for h in hs])            # older than FEED_MAX_AGE_HOURS
        self.assertNotIn("Off-site story", [h.title for h in hs])       # its link is not on the trusted list

    def test_every_site_gets_its_turn(self):
        many = rss(*[("Story %d" % i, "https://www.irna.ir/n/%d" % i, DAY + " 10:%02d:00 +0000" % i, "")
                     for i in range(40)])
        res = self.results(irna_ir=many, reuters_com=RSS_REUTERS)
        hs = nf.select_headlines(res, [], T0, per_site=10, total=5)
        self.assertIn("Fed holds rates, signals patience", [h.title for h in hs])   # not crowded out
        self.assertEqual(len(hs), 5)
        self.assertEqual(len(nf.select_headlines(res, [], T0, per_site=10, total=100)), 12)   # 10 + Fed + Off-site

    def test_stats(self):
        res = self.results(reuters_com=RSS_REUTERS)
        res.append(nf.FeedResult(nf.Feed("apnews.com", "https://apnews.com/x", "proxy"), False, 403, "HTTP 403",
                                 [], 0.5, "proxy"))
        st = nf.feed_stats(res, nf.select_headlines(res, [], T0))
        self.assertEqual((st["tried"], st["ok"], st["headlines"], st["failed_count"]), (2, 1, 2, 1))
        self.assertEqual(st["failed"][0]["site"], "apnews.com")


class TestFeedsFor(unittest.TestCase):
    def test_the_table_follows_the_trusted_list(self):
        all_feeds = nf.feeds_for([])
        self.assertEqual(len(all_feeds), len(nf.FEED_TABLE))
        some = nf.feeds_for(["reuters.com", "irna.ir"])
        self.assertEqual([(f.site, f.route) for f in some], [("reuters.com", "proxy"), ("irna.ir", "direct")])
        extra = nf.feeds_for(["reuters.com"], ["https://www.tasnimnews.ir/rss", "https://news.example.com/feed"],
                             news_mod.normalize_source)
        self.assertEqual([(f.site, f.route) for f in extra[1:]], [("tasnimnews.ir", "direct"),
                                                                  ("news.example.com", "proxy")])

    def test_every_table_site_is_a_default_source_and_every_url_is_https(self):
        for site, url, route in nf.FEED_TABLE:
            self.assertIn(site, news_mod.DEFAULT_NEWS_SOURCES)
            self.assertTrue(url.startswith("https://"), url)
            self.assertIn(route, nf.ROUTES)
            self.assertEqual(nf.default_route(site), route, site)


class TestFetching(unittest.TestCase):
    def test_parallel_fetch_routes_and_fallback(self):
        f1 = nf.Feed("reuters.com", "https://r/feed", "proxy")
        f2 = nf.Feed("irna.ir", "https://i/feed", "direct")
        f3 = nf.Feed("coindesk.com", "https://c/feed", "proxy")
        f4 = nf.Feed("apnews.com", "https://a/feed", "proxy")
        fake = FakeFeeds({("https://r/feed", "proxy"): ConnectionResetError("tunnel"),
                          ("https://r/feed", "direct"): (200, RSS_REUTERS),
                          "https://i/feed": (200, RSS_IRNA), "https://c/feed": (200, b"<html>"),
                          "https://a/feed": (403, b"")})
        res = nf.fetch_feeds([f1, f2, f3, f4], fake, has_proxy=True)
        self.assertEqual([(r.ok, r.route) for r in res], [(True, "direct"), (True, "direct"), (False, "proxy"),
                                                          (False, "proxy")])
        self.assertIn("not a feed", res[2].error)
        self.assertEqual(res[3].error, "HTTP 403")                     # an HTTP answer: no second route
        self.assertEqual(sorted(c[:2] for c in fake.calls if c[0] == "https://a/feed"), [("https://a/feed", "proxy")])
        self.assertEqual(sorted(c[:2] for c in fake.calls if c[0] == "https://i/feed"), [("https://i/feed", "direct")])

    def test_without_a_proxy_everything_goes_direct_once(self):
        fake = FakeFeeds(default=ConnectionResetError("down"))
        res = nf.fetch_feeds([nf.Feed("reuters.com", "https://r/feed", "proxy")], fake, has_proxy=False)
        self.assertFalse(res[0].ok)
        self.assertEqual([c[1] for c in fake.calls], ["direct"])


class TestMessagesAndRefs(unittest.TestCase):
    def headlines(self):
        res = [nf.FeedResult(nf.Feed(s, "https://feed/" + s, "proxy"), True, 200, "", nf.parse_feed(b), 0.1, "proxy")
               for s, b in (("reuters.com", RSS_REUTERS), ("coindesk.com", RSS_COINDESK), ("irna.ir", RSS_IRNA))]
        return nf.select_headlines(res, ["reuters.com", "coindesk.com", "irna.ir"], T0)

    def test_the_request_lists_numbered_headlines_and_no_tool(self):
        msgs, kept = nf.build_feed_messages(T0, self.headlines(), "BTC, ETH", "oil", 5, focus="BTC fell 16%")
        system, user = msgs[0]["content"], msgs[1]["content"]
        self.assertIn('"ref"', system)
        self.assertIn("Use ONLY the listed headlines", system)
        self.assertIn("At most 5 items", system)
        self.assertNotIn("search", system.lower().replace("research", ""))
        self.assertIn("[1] 2026-09-22 10:30 | coindesk.com | Bitcoin ETF inflows continue - Inflows for a fifth day.",
                      user)
        self.assertIn(u"[3] 2026-09-22 09:30 | irna.ir | بانک مرکزی نرخ ارز را اعلام کرد", user)
        self.assertIn("URGENT FOCUS (look for this first): BTC fell 16%", user)
        self.assertIn("Coins that matter to the account (for focus only): BTC, ETH", user)
        self.assertIn("Also look for: oil", user)
        self.assertEqual(len(kept), 4)

    def test_links_and_dates_come_from_the_feed(self):
        hs = self.headlines()
        obj = {"items": [{"ref": 2, "headline": "Exchange X was hacked", "why_it_matters": "risk-off",
                          "source_url": "https://evil.example/", "time_hint": "2020-01-01"},
                         {"ref": "[2]", "headline": "duplicate"}, {"ref": 99, "headline": "unknown"},
                         {"headline": "no ref"}, {"ref": 4.0, "headline": "Fed holds", "why_it_matters": "calm"}],
               "summary": "Risk-off."}
        std, dropped = nf.items_from_refs(obj, hs)
        self.assertEqual(dropped, 3)
        self.assertEqual(std["items"][0], {"headline": "Exchange X was hacked", "why_it_matters": "risk-off",
                                           "source_url": "https://www.coindesk.com/business/x-hacked/",
                                           "link": "https://www.coindesk.com/business/x-hacked/",
                                           "time_hint": "2026-09-22 10:00 UTC"})
        self.assertEqual(std["items"][1]["source_url"], "https://www.reuters.com/markets/us/fed-holds/")
        self.assertEqual(std["summary"], "Risk-off.")


class FeedsTestBase(tn.NewsTestBase):
    FEEDS = {table_url("reuters.com"): (200, RSS_REUTERS), table_url("coindesk.com"): (200, RSS_COINDESK),
             table_url("irna.ir"): (200, RSS_IRNA)}

    def feeds_researcher(self, script, feeds=None, **cfg):
        cfg.setdefault("mode", "feeds")
        cfg.setdefault("sources", ["reuters.com", "coindesk.com", "irna.ir"])
        fake = FakeFeeds(self.FEEDS if feeds is None else feeds)
        tr = tn.FakeTransport(script, self.clock)
        r = NewsResearcher(cfg, state_dir=self.dir, transport=tr, env=tn.ENV, clock=lambda: T0, monotonic=self.clock,
                           sleep=self.clock.sleep, feed_fetch=fake)
        return r, tr, fake


class TestFeedsResearch(FeedsTestBase):
    def test_one_json_mode_request_without_tools(self):
        r, tr, fake = self.feeds_researcher([(200, tn.completion(reply([
            {"ref": 2, "headline": "Exchange X hacked, hot wallet drained", "why_it_matters": "risk-off for alts"},
            {"ref": 1, "headline": "Bitcoin ETF inflows continue", "why_it_matters": "supports BTC"}],
            "Mixed: a hack against steady ETF demand.")))])
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(tr.requests), 1)
        body = tr.requests[0]["body"]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", body)
        self.assertEqual(body["model"], "kimi-k2.6")
        self.assertIn("[1] 2026-09-22 10:30 | coindesk.com | Bitcoin ETF inflows continue", body["messages"][1]["content"])
        self.assertEqual(len(fake.calls), 3)                          # the three sources' feeds only
        self.assertEqual((b.mode, b.searches, len(b.items)), ("feeds", 0, 2))
        self.assertEqual(b.items[0]["source_url"], "https://www.coindesk.com")       # the brief keeps the host
        self.assertEqual(b.items[0]["link"], "https://www.coindesk.com/business/x-hacked/")   # the panel the link
        self.assertEqual(b.items[0]["time_hint"], "2026-09-22 10:00 UTC")
        self.assertIn("1. [2026-09-22 10:00 UTC] Exchange X hacked, hot wallet drained - risk-off for alts "
                      "(source: www.coindesk.com)", b.text)
        self.assertEqual((b.feeds["tried"], b.feeds["ok"], b.feeds["headlines"]), (3, 3, 4))
        block = b.prompt_block(T0 + 60)
        self.assertIn("picked from the latest headlines of the trusted sources' feeds", block)
        cached = json.load(open(os.path.join(self.dir, news_mod.CACHE_FILE), encoding="utf-8"))
        self.assertEqual(cached["feeds"]["headlines"], 4)
        self.assertEqual(cached["brief"]["mode"], "feeds")
        back = news_mod.NewsBrief.from_dict(cached["brief"])
        self.assertEqual((back.mode, back.items[0]["link"], back.feeds["ok"]),
                         ("feeds", "https://www.coindesk.com/business/x-hacked/", 3))
        self.assertTrue(r.research(now=T0 + 60).cached)               # the cache works as in search mode

    def test_no_headline_means_no_model_call(self):
        r, tr, fake = self.feeds_researcher([], feeds={})
        with self.assertLogs("bitpin.news", level="WARNING"):
            b = r.research(now=T0)
        self.assertFalse(b.ok)
        self.assertIn("no recent headline from the trusted sources' feeds (0 of 3 feeds read)", b.error)
        self.assertEqual(tr.requests, [])
        cached = json.load(open(os.path.join(self.dir, news_mod.CACHE_FILE), encoding="utf-8"))
        self.assertEqual(cached["feeds"]["failed_count"], 3)
        self.assertIn("HTTP 404", cached["feeds"]["failed"][0]["error"])

    def test_an_empty_reply_to_json_mode_is_asked_again_without_it(self):
        r, tr, _ = self.feeds_researcher([(200, tn.completion("", finish="unexpected_state")),
                                          (200, tn.completion(reply([{"ref": 1, "headline": "ETF inflows",
                                                                      "why_it_matters": "x"}])))])
        with self.assertLogs("bitpin.news", level="WARNING"):
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual([("response_format" in q["body"]) for q in tr.requests], [True, False])
        self.assertFalse(r._json_mode)

    def test_a_prose_reply_is_asked_for_once_more(self):
        r, tr, _ = self.feeds_researcher([(200, tn.completion("Here are the news: ETF inflows.")),
                                          (200, tn.completion(reply([{"ref": 1, "headline": "ETF inflows",
                                                                      "why_it_matters": "x"}])))])
        with self.assertLogs("bitpin.news", level="WARNING"):
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(tr.requests), 2)
        self.assertTrue(tr.requests[1]["body"]["messages"][-1]["content"].endswith(news_mod.FORMAT_NUDGE_PLAIN))

    def test_items_without_a_listed_ref_are_left_out(self):
        r, tr, _ = self.feeds_researcher([(200, tn.completion(reply([
            {"headline": "Invented event", "why_it_matters": "x", "source_url": "https://www.reuters.com/x"},
            {"ref": 4, "headline": "Fed holds rates", "why_it_matters": "calm"}])))])
        with self.assertLogs("bitpin.news", level="WARNING") as cm:
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual([i["headline"] for i in b.items], ["Fed holds rates"])
        self.assertIn("cited no listed headline", "\n".join(cm.output))

    def test_a_cut_reply_keeps_its_complete_items(self):
        text = reply([{"ref": 1, "headline": "ETF inflows", "why_it_matters": "x"},
                      {"ref": 2, "headline": "Exchange hacked", "why_it_matters": "y"}])
        cut = text[:text.index('{"ref": 2') + 12]
        r, tr, _ = self.feeds_researcher([(200, tn.completion(cut, finish="length"))])
        with self.assertLogs("bitpin.news", level="WARNING"):
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual([i["headline"] for i in b.items], ["ETF inflows"])

    def test_extra_feeds_and_the_trusted_list(self):
        extra = "https://news.example.com/rss"
        feeds = dict(self.FEEDS)
        feeds[extra] = (200, rss(("Example item", "https://news.example.com/a/", DAY + " 10:45:00 +0000", "")))
        cfg = {"mode": "feeds", "sources": ["reuters.com", "news.example.com"], "feeds": [extra]}
        fake = FakeFeeds(feeds)
        tr = tn.FakeTransport([(200, tn.completion(reply([{"ref": 1, "headline": "Example item",
                                                           "why_it_matters": "x"}])))], self.clock)
        r = NewsResearcher(cfg, state_dir=self.dir, transport=tr, env=tn.ENV, clock=lambda: T0, monotonic=self.clock,
                           sleep=self.clock.sleep, feed_fetch=fake)
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(sorted(c[0] for c in fake.calls), sorted([table_url("reuters.com"), extra]))
        self.assertEqual(b.items[0]["link"], "https://news.example.com/a/")

    def test_the_search_mode_is_still_there(self):
        r, tr, fake = self.feeds_researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(tn.GOOD))],
                                            mode="search", sources=[])
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual((b.mode, b.searches, fake.calls), ("search", 1, []))
        self.assertIn("written by a separate news-research model", b.prompt_block(T0 + 60))


class TestConfig(unittest.TestCase):
    def test_mode_and_feeds(self):
        self.assertEqual(validate_news_config({})["mode"], "feeds")
        self.assertIsNone(validate_news_config({})["feeds"])
        self.assertEqual(validate_news_config({"mode": "search"})["mode"], "search")
        self.assertEqual(validate_news_config({"feeds": ["https://www.tasnimnews.ir/rss",
                                                         "https://www.tasnimnews.ir/rss"]})["feeds"],
                         ["https://www.tasnimnews.ir/rss"])
        for bad in ({"mode": "rss"}, {"mode": None, "feeds": "https://x.com/rss"}, {"feeds": ["ftp://x.com/rss"]},
                    {"feeds": ["https://127.0.0.1/rss"]}, {"feeds": ["https://user:pw@x.com/rss"]},
                    {"feeds": ["https://x.com/a b"]}, {"feeds": ["https://x.com/" + "a" * 400]},
                    {"feeds": ["https://x.com/%d" % i for i in range(31)]}, {"feeds": [5]}):
            with self.assertRaises(NewsConfigError, msg=str(bad)[:60]):
                validate_news_config(bad)


if __name__ == "__main__":
    unittest.main()
