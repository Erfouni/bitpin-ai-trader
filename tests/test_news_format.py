"""v3.5.3: a news reply in prose (not the JSON object) is asked for once more with the model's own text given back
(v3.6.3: a report with links first by a clean JSON-mode request with the text as notes; v3.6.4: while search
rounds are left, first one chance to search, the tool still offered);
the log shows the searches and the start of an unreadable reply; a timeout while the connection is being opened
(proxy CONNECT, TLS handshake) counts as a request that got no answer at all."""
import json
import os
import socket
import sys
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bitpin import news as news_mod  # noqa: E402
from bitpin.llm import TransportError, make_llm_transport  # noqa: E402
from bitpin.news import FORMAT_NUDGE, FORMAT_NUDGE_PLAIN, SEARCH_NOW_NUDGE, make_news_transport  # noqa: E402
import test_news as tn  # noqa: E402  (the fixtures: fake transport, clock, reply builders)

T0 = tn.T0
PROSE = ("I searched for the latest news. The results mention that the Fed held rates and that Bitcoin ETF inflows "
         "rose this week; I could not find Bitpin announcements. Let me know if you need more details.")
LINKED = ("I searched for the latest news. The Fed held rates (https://www.reuters.com/markets/us/fed-holds-rates/) and "
          "Bitcoin ETF inflows rose this week (https://www.coindesk.com/markets/etf-inflows-rise/).")
ANNOUNCE = ("I need to search more specifically for very recent events using the allowed sources. Let me run focused "
            "searches.")


class TestProseReply(tn.NewsTestBase):
    def test_a_prose_report_with_links_is_put_into_the_json_object_by_a_clean_request(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(LINKED)),
                                 (200, tn.completion(tn.GOOD))])
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual((len(tr.requests), len(b.items)), (3, 2))
        last = tr.requests[2]["body"]
        self.assertNotIn("tools", last)                           # no more searching: answer from what was found
        self.assertEqual(last["response_format"], {"type": "json_object"})
        self.assertEqual([m["role"] for m in last["messages"]], ["system", "user"])   # no tool history (v3.6.3)
        self.assertEqual(last["messages"][0], tr.requests[0]["body"]["messages"][0])  # the research rules
        self.assertTrue(last["messages"][1]["content"].startswith("Below are the notes of your web research"))
        self.assertIn(LINKED, last["messages"][1]["content"])     # the reply, as the notes

    def test_a_reply_that_only_announces_more_searching_may_search(self):
        # v3.6.4: kimi-check 2026-09-30 14:35 UTC - such a reply put into the JSON object was an empty brief
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(ANNOUNCE)),
                                 tn.tool_round(tn.call(2, tn.ARGS2)), (200, tn.completion(tn.GOOD))])
        with self.assertLogs("bitpin.news", level="WARNING") as cm:
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual((len(tr.requests), len(b.items)), (4, 2))
        third = tr.requests[2]["body"]
        self.assertIn("tools", third)                             # it may search now
        self.assertEqual(third["messages"][-2], {"role": "assistant", "content": ANNOUNCE})
        self.assertEqual(third["messages"][-1], {"role": "user", "content": SEARCH_NOW_NUDGE})
        notes = [q for q in tr.requests if len(q["body"]["messages"]) == 2 and q is not tr.requests[0]]
        self.assertEqual(notes, [])                               # no clean request for a reply without links
        self.assertIn("asking to search now", "\n".join(cm.output))

    def test_when_the_clean_request_is_refused_the_conversation_goes_on(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(LINKED)),
                                 (200, tn.completion("", finish="unexpected_state")), (200, tn.completion(tn.GOOD))])
        with self.assertLogs("bitpin.news", level="WARNING") as cm:
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual((len(tr.requests), len(b.items)), (4, 2))
        last = tr.requests[3]["body"]
        self.assertNotIn("response_format", last)                 # JSON mode is off after the refusal
        self.assertEqual(last["messages"][-2], {"role": "assistant", "content": LINKED})
        self.assertEqual(last["messages"][-1], {"role": "user", "content": SEARCH_NOW_NUDGE})
        self.assertFalse(r._json_mode)
        self.assertIn("empty reply (finish_reason unexpected_state) to the clean JSON request", "\n".join(cm.output))

    def test_the_moonshot_path_refusal_announcement_search_answer(self):
        # Moonshot (kimi-check on the server, 2026-09-30): JSON mode next to its builtin $web_search is answered
        # with an empty reply and finish_reason "unexpected_state", not with an HTTP 400; the next reply only
        # announced more searching
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)),
                                 (200, tn.completion("", finish="unexpected_state")),
                                 (200, tn.completion(ANNOUNCE)), tn.tool_round(tn.call(2, tn.ARGS2)),
                                 (200, tn.completion(tn.GOOD))])
        with self.assertLogs("bitpin.news", level="WARNING") as cm:
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        bodies = [q["body"] for q in tr.requests]
        self.assertEqual((len(bodies), len(b.items)), (5, 2))
        self.assertEqual(bodies[1]["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", bodies[2])           # the same request again, the tool offered
        self.assertIn("tools", bodies[2])
        for q in bodies[3:]:
            self.assertIn("tools", q)
            self.assertNotIn("response_format", q)
        self.assertEqual(bodies[3]["messages"][-1], {"role": "user", "content": SEARCH_NOW_NUDGE})
        self.assertEqual((r._json_mode, r._json_with_tools), (True, False))
        self.assertIn("empty reply (finish_reason unexpected_state) to JSON mode with the search tool",
                      "\n".join(cm.output))

    def test_prose_after_the_search_nudge_is_asked_for_the_json_without_the_tool(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(PROSE)),
                                 (200, tn.completion(PROSE)), (200, tn.completion(tn.GOOD))])
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(tr.requests), 4)
        last = tr.requests[3]["body"]
        self.assertNotIn("tools", last)                           # no more searching: answer from what was found
        self.assertEqual(last["messages"][-2], {"role": "assistant", "content": PROSE})
        self.assertEqual(last["messages"][-1], {"role": "user", "content": FORMAT_NUDGE})

    def test_a_third_prose_reply_fails_and_the_log_shows_what_happened(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(PROSE)),
                                 (200, tn.completion(PROSE)), (200, tn.completion(PROSE))])
        with self.assertLogs("bitpin.news", level="WARNING") as cm:
            b = r.research(now=T0)
        self.assertFalse(b.ok)
        self.assertEqual(len(tr.requests), 4)
        text = "\n".join(cm.output)
        self.assertIn("searches: bitcoin news", text)             # the query of the search round
        self.assertIn("I searched for the latest news", text)     # the start of the unreadable reply
        self.assertIn(news_mod.FAILED_REPLY_FILE, text)

    def test_the_prose_retries_are_bounded(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(PROSE)),
                                 (200, tn.completion(PROSE)), (200, tn.completion(PROSE)),
                                 (200, tn.completion(tn.GOOD))])
        self.assertFalse(r.research(now=T0).ok)
        self.assertEqual(len(tr.requests), 4)


class TestPluginProseReply(tn.OpenRouterNewsBase):
    def test_the_plugin_request_is_repeated_with_the_format_rule(self):
        r, tr = self.or_researcher([(200, tn.or_reply(PROSE)), (200, tn.or_reply(tn.GOOD))], stream=False)
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(tr.requests), 2)
        self.assertTrue(tr.requests[1]["body"]["messages"][-1]["content"].endswith(FORMAT_NUDGE_PLAIN))


class _HandshakeTimeout(object):
    """An opener whose open() fails the way urllib reports a TLS handshake that timed out through the proxy."""

    def open(self, req, timeout=None):
        raise urllib.error.URLError(socket.timeout("_ssl.c:983: The handshake operation timed out"))


class _HttpError(object):
    def open(self, req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)


class TestConnectionTimeouts(unittest.TestCase):
    URL = "https://api.moonshot.ai/v1/chat/completions"
    BODY = json.dumps({"model": "kimi-k3", "stream": True}).encode("utf-8")

    def test_a_handshake_timeout_is_a_request_without_any_answer(self):
        with self.assertRaises((socket.timeout, TimeoutError)) as cm:
            make_news_transport(None, opener=_HandshakeTimeout())("POST", self.URL, {}, self.BODY, 5.0)
        self.assertTrue(cm.exception.no_answer)
        self.assertIn("no connection", str(cm.exception))
        with self.assertRaises(TransportError) as cm2:
            make_llm_transport(None, opener=_HandshakeTimeout())("POST", self.URL, {}, self.BODY, 5.0)
        self.assertTrue(cm2.exception.timeout and cm2.exception.no_answer)
        self.assertFalse(cm2.exception.after_headers)

    def test_an_http_error_is_still_returned_as_a_status(self):
        status, _ = make_news_transport(None, opener=_HttpError())("POST", self.URL, {}, self.BODY, 5.0)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
