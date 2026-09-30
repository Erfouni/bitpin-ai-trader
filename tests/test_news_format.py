"""v3.5.3: a news reply in prose (not the JSON object) is asked for once more with the model's own text given back;
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
from bitpin.news import FORMAT_NUDGE, FORMAT_NUDGE_PLAIN, make_news_transport  # noqa: E402
import test_news as tn  # noqa: E402  (the fixtures: fake transport, clock, reply builders)

T0 = tn.T0
PROSE = ("I searched for the latest news. The results mention that the Fed held rates and that Bitcoin ETF inflows "
         "rose this week; I could not find Bitpin announcements. Let me know if you need more details.")


class TestProseReply(tn.NewsTestBase):
    def test_a_prose_reply_is_asked_for_once_more_with_its_own_text(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(PROSE)),
                                 (200, tn.completion(tn.GOOD))])
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual((len(tr.requests), len(b.items)), (3, 2))
        last = tr.requests[2]["body"]
        self.assertNotIn("tools", last)                           # no more searching: answer from what was found
        self.assertEqual(last["messages"][-2], {"role": "assistant", "content": PROSE})
        self.assertEqual(last["messages"][-1], {"role": "user", "content": FORMAT_NUDGE})

    def test_a_second_prose_reply_fails_and_the_log_shows_what_happened(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(PROSE)),
                                 (200, tn.completion(PROSE))])
        with self.assertLogs("bitpin.news", level="WARNING") as cm:
            b = r.research(now=T0)
        self.assertFalse(b.ok)
        self.assertEqual(len(tr.requests), 3)
        text = "\n".join(cm.output)
        self.assertIn("searches: bitcoin news", text)             # the query of the search round
        self.assertIn("I searched for the latest news", text)     # the start of the unreadable reply
        self.assertIn(news_mod.FAILED_REPLY_FILE, text)

    def test_the_prose_retry_happens_once_only(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(PROSE)),
                                 (200, tn.completion(PROSE)), (200, tn.completion(tn.GOOD))])
        self.assertFalse(r.research(now=T0).ok)
        self.assertEqual(len(tr.requests), 3)


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
