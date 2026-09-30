"""v3.5.2: a streamed request that got NO answer at all (the tunnel swallowed it: on the server the first large
request after a quiet spell often hangs while the next one passes) is retried up to SILENT_EXTRA_RETRIES more
times on top of max_timeout_retries; a reply lost AFTER its headers (most likely billed) stays limited."""
import json
import os
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bitpin import llm as llm_mod  # noqa: E402
from bitpin import news as news_mod  # noqa: E402
from bitpin.llm import LLMError, TransportError, make_llm_transport  # noqa: E402
import test_llm as tl  # noqa: E402  (the fixtures: fake transport, clock, reply builder)
import test_news as tn  # noqa: E402  (the raw socket server that never answers)


def silent():
    e = TransportError("timeout (via proxy http://127.0.0.1:1081): TimeoutError: no answer 120 s after the request "
                       "was sent (the connection went silent)", timeout=True)
    e.no_answer = True
    return e


def cut_after_headers():
    e = TransportError("timeout: read timed out", timeout=True)
    e.after_headers = True
    return e


class TestSilentRetries(tl.LLMTestBase):
    def test_a_silent_request_gets_three_more_tries(self):
        c, tr = self.client([silent(), silent(), silent(), (200, tl.completion("{}"))])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(out["content"], "{}")
        self.assertEqual(len(tr.requests), 4)
        self.assertEqual(llm_mod.SILENT_EXTRA_RETRIES, 3)

    def test_four_silent_requests_give_up(self):
        c, tr = self.client([silent(), silent(), silent(), silent(), (200, tl.completion("{}"))])
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(cm.exception.kind, "llm_timeout")
        self.assertEqual(len(tr.requests), 4)

    def test_a_reply_cut_after_its_headers_stays_limited(self):
        c, tr = self.client([cut_after_headers(), cut_after_headers(), (200, tl.completion("{}"))])
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 2)                   # max_timeout_retries 1, as before

    def test_silent_tries_do_not_use_up_the_timeout_retry(self):
        c, tr = self.client([silent(), cut_after_headers(), (200, tl.completion("{}"))])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual((out["content"], len(tr.requests)), ("{}", 3))

    def test_a_plain_request_keeps_the_old_limit(self):
        c, tr = self.client([silent(), silent(), (200, tl.completion("{}"))], stream=False)
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 2)

    def test_every_silent_try_is_charged_to_the_budget(self):
        c, tr = self.client([silent(), (200, tl.completion("{}"))], max_tokens=1000)
        c.chat([{"role": "user", "content": "u"}])
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            recs = [json.loads(line) for line in f]
        lost = [r for r in recs if r.get("lost")]
        self.assertEqual(len(lost), 1)                           # the silent try counts (prompt + max_tokens)
        self.assertEqual(lost[0]["usage"]["completion_tokens"], 1000)


class TestTheTransportMarksASilentRequest(unittest.TestCase):
    def test_no_answer_is_marked_through_both_transports(self):
        srv = tn._RawServer(lambda conn, stop: stop.wait(8))     # reads the request, never answers
        self.addCleanup(srv.close)
        with mock.patch.object(news_mod, "STREAM_HEADERS_SECONDS", 0.5):
            t0 = time.monotonic()
            with self.assertRaises(TransportError) as cm:
                make_llm_transport(None)("POST", "http://127.0.0.1:%d/v1/chat/completions" % srv.port,
                                         {"Content-Type": "application/json"}, b'{"model": "kimi-k3", "stream": true}',
                                         6.0)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertTrue(cm.exception.timeout)
        self.assertTrue(cm.exception.no_answer)
        self.assertFalse(cm.exception.after_headers)


if __name__ == "__main__":
    unittest.main()
