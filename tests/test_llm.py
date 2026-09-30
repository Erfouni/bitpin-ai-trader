"""LLMClient tests with a fake transport (never calls the real Moonshot/Kimi API)."""
import http.client
import io
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from bitpin.api import TransportError  # noqa: E402
from bitpin.llm import (FINAL_ANSWER_NUDGE, THINKING_DEFAULTS, WEB_SEARCH_TOOL, ConfigError,  # noqa: E402
                        LLMAuthError, LLMBudgetExceeded, LLMClient, LLMError, extract_json_object, find_json_objects,
                        llm_transport, make_llm_transport, parse_json_object_strict, redact_text,
                        validate_llm_config)
from bitpin.news import HARD_LIMIT_GRACE_SECONDS, StrictProxyHandler  # noqa: E402

try:
    from http.server import ThreadingHTTPServer  # noqa: E402  (3.7+)
except ImportError:  # pragma: no cover
    ThreadingHTTPServer = None

logging.getLogger("bitpin").addHandler(logging.NullHandler())

KEY = "sk-TESTKEY1234567890abcdef"
ENV = {"KIMI_API_KEY": KEY}
T0 = 1790074800.0  # 2026-09-22 UTC


def completion(content=None, finish="stop", tool_calls=None, usage=None, reasoning=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return {"id": "cmpl-1", "model": "kimi-test", "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


def search_call(i, query="bitcoin news"):
    args = json.dumps({"search_result": {"search_id": "s%d" % i, "query": query}, "usage": {"total_tokens": 1000}})
    return {"index": 0, "id": "call_%d" % i, "type": "builtin_function",
            "function": {"name": "$web_search", "arguments": args}}


class Timeout(object):
    """Script item: the request hangs for its whole timeout (virtual time advances), then times out."""


TIMEOUT = Timeout()


class FakeTransport:
    """Scripted responses: each item is (status, payload), an Exception instance to raise, or
    TIMEOUT (advances the fake clock by the request timeout, then raises a timeout)."""

    def __init__(self, script, clock=None):
        self.script = list(script)
        self.requests = []
        self.clock = clock

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append({"method": method, "url": url, "headers": dict(headers),
                              "body": json.loads(body.decode("utf-8")) if body else None, "timeout": timeout})
        if not self.script:
            raise AssertionError("unexpected extra request")
        item = self.script.pop(0)
        if item is TIMEOUT:
            if self.clock is not None:
                self.clock.t += timeout
            raise TransportError("timeout: read timed out", timeout=True)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        return status, raw


class Clock:
    def __init__(self, t=T0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class LLMTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="llmtest_")
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def client(self, script, state_dir="default", **cfg):
        base = {"model": "kimi-test"}
        base.update(cfg)
        tr = FakeTransport(script, self.clock)
        c = LLMClient(base, state_dir=self.dir if state_dir == "default" else state_dir, transport=tr, env=ENV,
                      sleep=self.clock.sleep, clock=self.clock, monotonic=self.clock)
        return c, tr


class TestChat(LLMTestBase):
    def test_json_mode_request_shape(self):
        c, tr = self.client([(200, completion('{"targets": {"USDT_IRT": 1}}'))])
        out = c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(out["content"], '{"targets": {"USDT_IRT": 1}}')
        self.assertEqual(out["tool_rounds"], 0)
        self.assertEqual(out["usage"]["total_tokens"], 120)
        self.assertTrue(out["json_mode_used"])
        self.assertFalse(out["json_fallback"])
        r = tr.requests[0]
        self.assertEqual(r["method"], "POST")
        self.assertEqual(r["url"], "https://api.moonshot.ai/v1/chat/completions")
        self.assertEqual(r["headers"]["Authorization"], "Bearer " + KEY)
        self.assertEqual(r["body"]["model"], "kimi-test")
        self.assertEqual(r["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(r["body"]["temperature"], 0.3)
        self.assertNotIn("tools", r["body"])
        self.assertEqual(r["timeout"], 120.0)
        # the key is only in the header, never in the body
        self.assertNotIn(KEY, json.dumps(r["body"]))
        # json_mode=False: no response_format, and the reply is not flagged as JSON mode
        tr.script.append((200, completion("{}")))
        out = c.chat([{"role": "user", "content": "hi"}], json_mode=False)
        self.assertNotIn("response_format", tr.requests[1]["body"])
        self.assertFalse(out["json_mode_used"])

    def test_web_search_tool_loop(self):
        c1, c2 = search_call(1), search_call(2, "USD IRR free market rate")
        script = [
            (200, completion("", "tool_calls", [c1], {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55})),
            (200, completion(None, "tool_calls", [c2], {"prompt_tokens": 1500, "completion_tokens": 5, "total_tokens": 1505})),
            (200, completion('{"targets": {}}', "stop", None, {"prompt_tokens": 3000, "completion_tokens": 200, "total_tokens": 3200})),
        ]
        c, tr = self.client(script)
        out = c.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], web_search=True)
        self.assertEqual(out["tool_rounds"], 2)
        self.assertEqual(out["search_calls"], 2)
        self.assertEqual(out["content"], '{"targets": {}}')
        self.assertEqual(out["usage"]["total_tokens"], 55 + 1505 + 3200)
        self.assertEqual(out["usage"]["search"]["total_tokens"], 2000)
        self.assertFalse(out["forced_final"])
        self.assertEqual(len(tr.requests), 3)
        for r in tr.requests:
            self.assertEqual(r["body"]["tools"], [WEB_SEARCH_TOOL])
        m2 = tr.requests[1]["body"]["messages"]
        self.assertEqual(len(m2), 4)
        self.assertEqual(m2[2]["role"], "assistant")
        # the VERIFIED echo format: content "" (never null), each call only id / type / function (no index)
        self.assertEqual(m2[2]["tool_calls"], [{"id": c1["id"], "type": c1["type"], "function": c1["function"]}])
        self.assertEqual(m2[2]["content"], "")
        self.assertNotIn("reasoning_content", m2[2])
        self.assertEqual(m2[3], {"role": "tool", "tool_call_id": "call_1", "name": "$web_search",
                                 "content": c1["function"]["arguments"]})  # arguments passed back unchanged
        m3 = tr.requests[2]["body"]["messages"]
        self.assertEqual(len(m3), 6)
        self.assertEqual(m3[4]["content"], "")                     # the round whose content was None
        self.assertEqual(m3[4]["tool_calls"], [{"id": c2["id"], "type": c2["type"], "function": c2["function"]}])
        self.assertNotIn("reasoning_content", m3[4])
        self.assertEqual(m3[5]["tool_call_id"], "call_2")
        self.assertEqual(m3[5]["content"], c2["function"]["arguments"])

    def test_tool_rounds_are_bounded_then_answer_is_forced(self):
        script = [(200, completion("", "tool_calls", [search_call(i)])) for i in range(6)]
        script.append((200, completion('{"targets": {"USDT_IRT": 1}}')))
        c, tr = self.client(script, max_tool_rounds=6)
        out = c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertEqual(out["content"], '{"targets": {"USDT_IRT": 1}}')
        self.assertTrue(out["forced_final"])
        self.assertEqual(out["tool_rounds"], 6)
        self.assertEqual(len(tr.requests), 7)
        last = tr.requests[6]["body"]
        self.assertNotIn("tools", last)                       # the searches are over
        self.assertEqual(last["messages"][-1], {"role": "user", "content": FINAL_ANSWER_NUDGE})
        self.assertEqual(last["messages"][-2]["role"], "tool")   # every tool call was answered
        self.assertEqual(last["response_format"], {"type": "json_object"})
        self.assertTrue(out["json_mode_used"])
        self.assertEqual(c.budget.used(), 1)

    def test_model_still_calling_tools_after_the_limit_is_an_error(self):
        script = [(200, completion("", "tool_calls", [search_call(i)])) for i in range(7)]
        c, tr = self.client(script, max_tool_rounds=6)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertIn("6 rounds", str(cm.exception))
        self.assertEqual(len(tr.requests), 7)

    def test_final_request_without_tools_refused_is_resent_with_tools(self):
        script = [(200, completion("", "tool_calls", [search_call(0)])),
                  (400, {"error": {"message": "tool message without tools definition", "type": "invalid_request_error"}}),
                  (200, completion('{"targets": {}}'))]
        c, tr = self.client(script, max_tool_rounds=1)
        out = c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertEqual(out["content"], '{"targets": {}}')
        self.assertNotIn("tools", tr.requests[1]["body"])
        self.assertIn("tools", tr.requests[2]["body"])
        self.assertEqual(tr.requests[2]["body"]["messages"][-1]["content"], FINAL_ANSWER_NUDGE)

    def test_prompt_token_limit_stops_searching(self):
        big = {"prompt_tokens": 150000, "completion_tokens": 5, "total_tokens": 150005}
        script = [(200, completion("", "tool_calls", [search_call(0)], big)), (200, completion('{"targets": {}}'))]
        c, tr = self.client(script, max_prompt_tokens_per_call=100000)
        out = c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertTrue(out["forced_final"])
        self.assertEqual(out["tool_rounds"], 1)
        self.assertNotIn("tools", tr.requests[1]["body"])

    def test_tool_calls_finish_without_calls_is_an_error(self):
        c, tr = self.client([(200, completion("", "tool_calls", []))])
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertEqual(len(tr.requests), 1)

    def test_response_format_rejected_with_tools_falls_back(self):
        err = {"error": {"message": "response_format json_object is not supported with tools", "type": "invalid_request_error"}}
        script = [(400, err),
                  (200, completion('Here is my decision:\n```json\n{"targets": {"USDT_IRT": 1.0}, "confidence": 0.7}\n```'))]
        c, tr = self.client(script)
        out = c.chat([{"role": "user", "content": "u"}], json_mode=True, web_search=True)
        self.assertTrue(out["json_fallback"])
        self.assertFalse(out["json_mode_used"])
        self.assertIn("response_format", tr.requests[0]["body"])
        self.assertNotIn("response_format", tr.requests[1]["body"])
        self.assertEqual(extract_json_object(out["content"], "targets"), {"targets": {"USDT_IRT": 1.0}, "confidence": 0.7})
        # remembered: the next call with tools does not send response_format at all
        tr.script.append((200, completion('{"targets": {}}')))
        out2 = c.chat([{"role": "user", "content": "u"}], json_mode=True, web_search=True)
        self.assertTrue(out2["json_fallback"])
        self.assertNotIn("response_format", tr.requests[2]["body"])
        # without tools response_format is still used
        tr.script.append((200, completion('{"targets": {}}')))
        c.chat([{"role": "user", "content": "u"}], json_mode=True, web_search=False)
        self.assertIn("response_format", tr.requests[3]["body"])

    def test_other_400_with_tools_does_not_disable_json_mode(self):
        c, tr = self.client([(400, {"error": {"message": "The request was rejected because it was considered high risk",
                                              "type": "content_filter"}})], stream=False)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}], json_mode=True, web_search=True)
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("high risk", cm.exception.detail)
        self.assertEqual(len(tr.requests), 1)
        self.assertFalse(c._no_response_format_with_tools)
        tr.script.append((200, completion('{"targets": {}}')))
        c.chat([{"role": "user", "content": "u"}], json_mode=True, web_search=True)
        self.assertIn("response_format", tr.requests[1]["body"])

    def test_400_without_tools_is_an_error(self):
        c, tr = self.client([(400, {"error": {"message": "bad request"}})], stream=False)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}], json_mode=True, web_search=False)
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(len(tr.requests), 1)

    def test_reasoning_content_is_never_the_answer(self):
        """A non-streamed reply: reasoning_content is returned as res["reasoning"] (v3 B2) but never
        parsed as the answer, never in the usage log."""
        c, _ = self.client([(200, completion('{"targets": {"BTC_IRT": 1}}', reasoning='{"targets": {"PEPE_IRT": 1}}'))])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(out["content"], '{"targets": {"BTC_IRT": 1}}')
        self.assertNotIn("reasoning_content", out)
        self.assertEqual(out["reasoning"], '{"targets": {"PEPE_IRT": 1}}')
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            self.assertNotIn("PEPE", f.read())


class TestRetries(LLMTestBase):
    def test_retries_on_429_5xx_and_network(self):
        script = [(429, {"error": {"message": "rate limit reached", "type": "rate_limit_reached_error"}}),
                  (503, b"<html>busy</html>"),
                  TransportError("timeout", timeout=True),
                  (200, completion('{"ok": 1}'))]
        c, tr = self.client(script)
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(out["content"], '{"ok": 1}')
        self.assertEqual(len(tr.requests), 4)
        self.assertEqual(self.clock.sleeps, [2.0, 4.0, 8.0])

    def test_gives_up_after_max_retries(self):
        c, tr = self.client([(500, {})] * 4)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(len(tr.requests), 4)  # 1 + 3 retries

    def test_post_timeouts_are_retried_at_most_once_per_call(self):
        c, tr = self.client([TIMEOUT, TIMEOUT, (200, completion("{}"))])
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(cm.exception.kind, "llm_timeout")
        self.assertEqual(len(tr.requests), 2)     # 1 + 1 retry, although max_retries is 3
        # connection errors (the request never reached the server) keep the normal retries
        c, tr = self.client([TransportError("network error: connection refused")] * 3 + [(200, completion("{}"))])
        c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 4)

    def test_deadline_bounds_the_whole_call(self):
        # every request times out, the model keeps searching: before the fix one call could block for about an hour
        script = []
        for i in range(10):
            script += [TIMEOUT, (200, completion("", "tool_calls", [search_call(i)]))]
        c, tr = self.client(script, deadline_seconds=300, max_timeout_retries=5, max_tool_rounds=10)
        t_start = self.clock.t
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertEqual(cm.exception.kind, "llm_timeout")
        self.assertLessEqual(self.clock.t - t_start, 300)
        for r in tr.requests:
            self.assertLessEqual(r["timeout"], 120)
        # the caller's time_limit shortens it further and cuts each request's timeout (to the time left
        # minus 1 s: the default transport may overrun its timeout by its 0.5 s hard-limit grace)
        c, tr = self.client([(200, completion("{}"))])
        c.chat([{"role": "user", "content": "u"}], time_limit=45)
        self.assertEqual(tr.requests[0]["timeout"], 44)
        # no time left: no request and no budget spent
        c, tr = self.client([])
        used = c.budget.used()
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}], time_limit=5)
        self.assertEqual(cm.exception.kind, "llm_timeout")
        self.assertEqual(c.budget.used(), used)
        self.assertEqual(tr.requests, [])

    def test_abort_is_checked_before_every_request(self):
        flag = {"stop": False}

        def abort():
            return "STOP file present" if flag["stop"] else None

        class Tr(FakeTransport):
            def __call__(tr, *a):
                flag["stop"] = True           # the kill switch appears during the first request
                return FakeTransport.__call__(tr, *a)
        c, _ = self.client([])
        tr = Tr([(200, completion("", "tool_calls", [search_call(0)])), (200, completion("{}"))])
        c._transport = tr
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}], web_search=True, abort=abort)
        self.assertEqual(cm.exception.kind, "llm_aborted")
        self.assertIn("STOP", str(cm.exception))
        self.assertEqual(len(tr.requests), 1)

    def test_401_403_are_auth_errors_without_retry_and_without_key(self):
        for status in (401, 403):
            c, tr = self.client([(status, {"error": {"message": "Invalid Authentication %s" % KEY,
                                                     "type": "invalid_authentication_error"}})])
            with self.assertRaises(LLMAuthError) as cm:
                c.chat([{"role": "user", "content": "u"}])
            self.assertEqual(len(tr.requests), 1)
            self.assertNotIn(KEY, str(cm.exception))
            self.assertIn("KIMI_API_KEY", str(cm.exception))
            self.assertEqual(cm.exception.kind, "llm_auth")

    def test_quota_429_fails_fast(self):
        c, tr = self.client([(429, {"error": {"message": "Your account is suspended, please check your plan",
                                              "type": "exceeded_current_quota_error"}})])
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 1)

    def test_no_key_or_no_model(self):
        c = LLMClient({"model": "m"}, state_dir=self.dir, transport=FakeTransport([]), env={})
        with self.assertRaises(LLMAuthError):
            c.chat([{"role": "user", "content": "u"}])
        c = LLMClient({}, state_dir=self.dir, transport=FakeTransport([]), env=ENV)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertIn("model", str(cm.exception))
        self.assertNotIn(KEY, repr(c))


def _http_error(code, body):
    return urllib.error.HTTPError("https://api.moonshot.ai/v1/chat/completions", code, "x", {},
                                  io.BytesIO(json.dumps(body).encode("utf-8")))


class TestNetworkDrops(LLMTestBase):
    """The Kimi proxy drops connections now and then (RemoteDisconnected, raised unwrapped by urllib):
    every raw network exception from ANY transport is a retried network error, never a crash."""

    def test_raw_network_errors_from_the_transport_are_retried(self):
        script = [http.client.RemoteDisconnected("Remote end closed connection without response"),
                  ConnectionResetError(104, "Connection reset by peer"),
                  http.client.IncompleteRead(b""),
                  (200, completion('{"ok": 1}'))]
        c, tr = self.client(script, backoff_seconds=2.0)
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(out["content"], '{"ok": 1}')
        self.assertEqual(len(tr.requests), 4)
        self.assertEqual(self.clock.sleeps, [2.0, 4.0, 8.0])
        # URLError too; the retries are bounded by max_retries and the error names the exception
        self.clock.sleeps[:] = []
        c, tr = self.client([urllib.error.URLError("x")] * 4, backoff_seconds=2.0)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 4)
        self.assertEqual(self.clock.sleeps, [2.0, 4.0, 8.0])
        self.assertIn("URLError", str(cm.exception))
        self.assertEqual(cm.exception.kind, "llm")

    def test_the_shipped_backoff_waits_3_s_first(self):
        c, tr = self.client([http.client.RemoteDisconnected("Remote end closed connection without response"),
                             (200, completion("{}"))], backoff_seconds=3.0)
        c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(self.clock.sleeps, [3.0])

    def test_raw_socket_timeout_counts_as_a_timeout(self):
        c, tr = self.client([socket.timeout("timed out"), socket.timeout("timed out"), (200, completion("{}"))])
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(cm.exception.kind, "llm_timeout")
        self.assertEqual(len(tr.requests), 2)          # max_timeout_retries=1, although max_retries is 3

    def test_http_error_raised_by_a_transport_is_handled(self):
        c, tr = self.client([_http_error(400, {"error": {"message": "bad request"}})], stream=False)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(len(tr.requests), 1)                  # no retry
        c, tr = self.client([_http_error(503, {}), (200, completion('{"ok": 2}'))])
        self.assertEqual(c.chat([{"role": "user", "content": "u"}])["content"], '{"ok": 2}')
        self.assertEqual(len(tr.requests), 2)                  # retried

    def test_a_drop_after_a_long_wait_counts_like_a_timeout_and_is_charged(self):
        """A non-streaming kimi-k3 call with max_tokens 32000 waits minutes with no bytes flowing. A
        RemoteDisconnected that late means Moonshot most likely processed - and billed - the request,
        so it must NOT be retried max_retries times, and the tokens must not vanish from the daily
        budget (which only ever counted the usage of replies that arrived)."""
        class Slow(FakeTransport):
            def __call__(self, method, url, headers, body, timeout):
                self.requests.append({"method": method, "url": url, "headers": dict(headers),
                                      "body": json.loads(body.decode("utf-8")) if body else None,
                                      "timeout": timeout})
                self.clock.t += 120                      # two minutes of silence, then the drop
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
        tr = Slow([], self.clock)
        c = LLMClient({"model": "kimi-test", "max_tokens": 32000, "max_tokens_per_day": 4000000,
                       "backoff_seconds": 1.0}, state_dir=self.dir, transport=tr, env=ENV,
                      sleep=self.clock.sleep, clock=self.clock, monotonic=self.clock)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u" * 400}])
        self.assertEqual(len(tr.requests), 2)            # max_timeout_retries=1, not max_retries=3
        self.assertIn("RemoteDisconnected", str(cm.exception))
        charged = c.budget.tokens_used()
        self.assertGreaterEqual(charged, 2 * 32000)      # both lost replies charged (prompt + max_tokens)
        # ... while a drop that happens IMMEDIATELY (the proxy refusing the connection) is free and
        # keeps the full retry budget
        tr2 = FakeTransport([http.client.RemoteDisconnected("x")] * 4, self.clock)
        c2 = LLMClient({"model": "kimi-test", "max_tokens": 32000, "max_tokens_per_day": 4000000,
                        "backoff_seconds": 1.0}, state_dir=os.path.join(self.dir, "b"), transport=tr2, env=ENV,
                       sleep=self.clock.sleep, clock=self.clock, monotonic=self.clock)
        with self.assertRaises(LLMError):
            c2.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr2.requests), 4)
        self.assertEqual(c2.budget.tokens_used(), 0)

    def test_default_transport_maps_remote_disconnected(self):
        class Opener(object):
            handlers = []

            def open(self, req, timeout=None):
                raise http.client.RemoteDisconnected("Remote end closed connection without response")
        tr = make_llm_transport(opener=Opener())
        with self.assertRaises(TransportError) as cm:
            tr("POST", "https://api.moonshot.ai/v1/chat/completions", {}, b"{}", 5)
        self.assertFalse(cm.exception.timeout)
        self.assertIn("RemoteDisconnected", str(cm.exception))


class TestKimiK3Settings(LLMTestBase):
    def test_temperature_null_is_omitted(self):
        c, tr = self.client([(200, completion("{}"))], temperature=None)
        c.chat([{"role": "user", "content": "u"}])
        self.assertNotIn("temperature", tr.requests[0]["body"])
        self.assertEqual(tr.requests[0]["body"]["response_format"], {"type": "json_object"})

    def test_model_and_max_tokens_override_per_call(self):
        c, tr = self.client([(200, dict(completion("{}"), model=None))], model="kimi-k3")
        out = c.chat([{"role": "user", "content": "u"}], model="kimi-k2.6", max_tokens=8000)
        self.assertEqual(tr.requests[0]["body"]["model"], "kimi-k2.6")
        self.assertEqual(tr.requests[0]["body"]["max_tokens"], 8000)
        self.assertEqual(out["model"], "kimi-k2.6")            # the reply named no model: the override
        tr.script.append((200, completion("{}")))
        c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(tr.requests[1]["body"]["model"], "kimi-k3")
        self.assertEqual(tr.requests[1]["body"]["max_tokens"], 32000)
        self.assertEqual(c.budget.used(), 2)                   # one shared budget

    def test_thinking_model_defaults_apply_only_to_absent_keys(self):
        cfg = validate_llm_config({"model": "kimi-k3"})
        self.assertIsNone(cfg["temperature"])
        self.assertEqual((cfg["max_tokens"], cfg["timeout"], cfg["deadline_seconds"]), (32000, 300, 540))
        self.assertEqual(THINKING_DEFAULTS["max_tokens"], 32000)
        self.assertEqual(validate_llm_config({"model": "kimi-k3", "temperature": 0.3})["temperature"], 0.3)
        self.assertEqual(validate_llm_config({"model": "kimi-k3", "max_tokens": 4096})["max_tokens"], 4096)
        old = validate_llm_config({"model": "kimi-k2.6"})
        self.assertEqual((old["temperature"], old["max_tokens"], old["timeout"], old["deadline_seconds"]),
                         (0.3, 4096, 120, 480))
        c, tr = self.client([(200, completion("{}"))], model="kimi-k3")
        c.chat([{"role": "user", "content": "u"}])
        body = tr.requests[0]["body"]
        self.assertNotIn("temperature", body)
        self.assertEqual(body["max_tokens"], 32000)
        self.assertEqual(tr.requests[0]["timeout"], 300)


class TestBudgetAndLogging(LLMTestBase):
    def test_daily_budget_persists_and_resets(self):
        c, tr = self.client([(200, completion("{}")), (200, completion("{}"))], max_calls_per_day=2)
        c.chat([{"role": "user", "content": "u"}])
        c.chat([{"role": "user", "content": "u"}])
        with self.assertRaises(LLMBudgetExceeded):
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 2)  # no request was sent for the refused call
        # a new client instance (bot restart) sees the same budget
        c2, tr2 = self.client([(200, completion("{}"))], max_calls_per_day=2)
        with self.assertRaises(LLMBudgetExceeded):
            c2.chat([{"role": "user", "content": "u"}])
        self.assertEqual(c2.budget.remaining(), 0)
        # next UTC day: budget available again
        self.clock.t += 86400
        c2.chat([{"role": "user", "content": "u"}])
        self.assertEqual(c2.budget.used(), 1)

    def test_the_budget_of_an_older_version_does_not_count_on_the_upgrade_day(self):
        """Review finding (decision, high): kimi_budget.json still held the hourly bot's usage of the same
        UTC day (up to 60 calls / 4M tokens were allowed) against the new 24 calls / 300k tokens."""
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        with open(os.path.join(self.dir, "kimi_budget.json"), "w") as f:
            json.dump({day: {"calls": 40, "total_tokens": 2000000}, "2026-01-01": {"calls": 3}}, f)
        c, _ = self.client([(200, completion("{}"))], max_calls_per_day=24, max_tokens_per_day=300000)
        self.assertEqual((c.budget.used(), c.budget.tokens_used(), c.budget.remaining()), (0, 0, 24))
        self.assertFalse(c.budget.tokens_exhausted())
        c.chat([{"role": "user", "content": "u"}])
        with open(os.path.join(self.dir, "kimi_budget.json")) as f:
            rec = json.load(f)[day]
        self.assertEqual((rec["v"], rec["calls"], rec["legacy"]), (2, 1, {"calls": 40, "total_tokens": 2000000}))
        # this version's own records keep counting (a restart does not reset them)
        c2, _ = self.client([], max_calls_per_day=24)
        self.assertEqual(c2.budget.used(), 1)

    def test_budget_is_enforced_without_state_dir(self):
        c, tr = self.client([(200, completion("{}"))] * 10, state_dir=None, max_calls_per_day=2)
        c.chat([{"role": "user", "content": "u"}])
        c.chat([{"role": "user", "content": "u"}])
        with self.assertRaises(LLMBudgetExceeded):
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 2)
        self.assertEqual(c.budget.tokens_used(), 240)
        with self.assertRaises(TypeError):     # state_dir must be given (None explicitly for in-memory)
            LLMClient({"model": "m"}, env=ENV)

    def test_daily_token_budget(self):
        u = {"prompt_tokens": 900, "completion_tokens": 200, "total_tokens": 1100}
        c, tr = self.client([(200, completion("{}", usage=u))] * 3, max_tokens_per_day=2000)
        c.chat([{"role": "user", "content": "u"}])
        c.chat([{"role": "user", "content": "u"}])     # 2200 tokens now: the next call is refused
        with self.assertRaises(LLMBudgetExceeded) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertIn("token", str(cm.exception))
        self.assertEqual(len(tr.requests), 2)

    def test_one_call_counts_once_despite_tool_rounds(self):
        script = [(200, completion("", "tool_calls", [search_call(1)])), (200, completion("{}"))]
        c, _ = self.client(script, max_calls_per_day=5)
        c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertEqual(c.budget.used(), 1)
        day, rec = c.budget.today()
        self.assertEqual(rec["total_tokens"], 240)

    def test_usage_log_has_no_secret(self):
        c, _ = self.client([(200, completion('{"a": "%s"}' % "x"))])
        c.chat([{"role": "user", "content": "my key is " + KEY}])
        files = os.listdir(self.dir)
        self.assertIn("kimi_usage.jsonl", files)
        for fn in files:
            with open(os.path.join(self.dir, fn), encoding="utf-8") as f:
                self.assertNotIn(KEY, f.read(), fn)
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["usage"]["total_tokens"], 120)
        self.assertEqual(rec["finish_reason"], "stop")


class TestConfigAndHelpers(LLMTestBase):
    def test_config_validation(self):
        for bad in ({"api_key": "sk-abc"}, {"modle": "x"}, {"base_url": "http://api.moonshot.ai/v1"},
                    {"base_url": "http://127.0.0.1:8888/v1"}, {"base_url": "https:///v1"},
                    {"timeout": "120"}, {"max_calls_per_day": 1.5}, {"max_retries": True}, {"model": 5},
                    {"api_key_env": "BITPIN_SECRET_KEY"}, {"api_key_env": "HOME"}, {"deadline_seconds": 0},
                    {"temperature": 7}):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                LLMClient(bad, state_dir=None, env=ENV)
        c = LLMClient({"base_url": "https://api.moonshot.cn/v1/", "model": "m", "_note": "x",
                       "api_key_env": "MOONSHOT_API_KEY"}, state_dir=None, env={"MOONSHOT_API_KEY": KEY})
        self.assertEqual(c.base_url, "https://api.moonshot.cn/v1")
        self.assertTrue(c.has_key)

    def test_unwritable_state_dir_fails_at_construction(self):
        path = os.path.join(self.dir, "a_file")
        with open(path, "w") as f:
            f.write("x")
        with self.assertRaises(ConfigError) as cm:
            LLMClient({"model": "m"}, state_dir=os.path.join(path, "sub"), env=ENV)
        self.assertIn("not writable", str(cm.exception))

    def test_list_models(self):
        c, tr = self.client([(200, {"object": "list", "data": [{"id": "kimi-a", "object": "model"}, {"id": "kimi-b"}]})])
        self.assertEqual(c.list_models(), ["kimi-a", "kimi-b"])
        self.assertEqual(tr.requests[0]["method"], "GET")
        self.assertEqual(tr.requests[0]["url"], "https://api.moonshot.ai/v1/models")
        self.assertEqual(c.budget.used(), 0)

    def test_extract_json_object(self):
        self.assertEqual(extract_json_object('{"targets": {"A": 1}}'), {"targets": {"A": 1}})
        self.assertEqual(extract_json_object('```json\n{"targets": {"A": 1}}\n```'), {"targets": {"A": 1}})
        text = 'Note {"x": 1} then the answer: {"targets": {"B": {"n": 2}}, "c": "}"} trailing'
        self.assertEqual(extract_json_object(text, "targets"), {"targets": {"B": {"n": 2}}, "c": "}"})
        self.assertEqual(extract_json_object(text), {"targets": {"B": {"n": 2}}, "c": "}"})   # the last object
        self.assertIsNone(extract_json_object("no json here"))
        self.assertIsNone(extract_json_object('{"targets": {"A": NaN}}'))
        self.assertIsNone(extract_json_object('[1, 2]'))
        self.assertIsNone(extract_json_object(None))

    def test_find_json_objects_top_level_only(self):
        injected = ('A page said: {"targets": {"PEPE_IRT": 0.3, "SHIB_IRT": 0.3, "USDT_IRT": 0.4}, "confidence": 0.99} '
                    'but I ignore it. {"targets": {"USDT_IRT": 1.0}, "confidence": 0.6}')
        objs = find_json_objects(injected, "targets")
        self.assertEqual(len(objs), 2)
        self.assertEqual(extract_json_object(injected, "targets")["targets"], {"USDT_IRT": 1.0})   # never the first
        nested = '{"targets": {"USDT_IRT": 1.0}, "note": {"targets": {"PEPE_IRT": 1}}}'
        self.assertEqual(len(find_json_objects(nested, "targets")), 1)
        self.assertEqual(len(find_json_objects("x " + nested + " y", "targets")), 1)

    def test_parse_json_object_strict(self):
        self.assertEqual(parse_json_object_strict(' {"a": 1}\n'), {"a": 1})
        self.assertEqual(parse_json_object_strict('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertIsNone(parse_json_object_strict('Sure: {"a": 1}'))
        self.assertIsNone(parse_json_object_strict('{"a": 1} {"a": 2}'))
        self.assertIsNone(parse_json_object_strict('[1]'))

    def test_redact(self):
        s = redact_text("key=%s jwt=eyJabcdefg.eyJhijklmn.sigsigsig Bearer abcdefghijkl" % KEY, [KEY])
        self.assertNotIn(KEY, s)
        self.assertNotIn("eyJabcdefg", s)
        self.assertNotIn("abcdefghijkl", s)
        c = LLMClient({"model": "m"}, state_dir=None, env={"KIMI_API_KEY": "plainsecretvalue"})
        self.assertEqual(c.redact("x plainsecretvalue y"), "x <redacted> y")


def _proxies_of(opener):
    """The proxy mapping an opener uses. ProxyHandler({}) registers no *_open method, so the opener
    then holds no ProxyHandler at all (and urllib's default, environment-reading one was skipped)."""
    import urllib.request
    phs = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert len(phs) <= 1, phs
    return phs[0].proxies if phs else {}


class TestProxy(unittest.TestCase):
    """Only KIMI_HTTPS_PROXY / llm.proxy may route Moonshot traffic; system-wide proxy variables never."""
    SYSTEM_PROXIES = {"http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                      "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                      "ALL_PROXY": "socks5://127.0.0.1:9", "all_proxy": "socks5://127.0.0.1:9"}

    def test_no_proxy_configured_means_direct_even_with_system_proxies(self):
        from unittest import mock
        with mock.patch.dict(os.environ, self.SYSTEM_PROXIES):
            c = LLMClient({"model": "m"}, state_dir=None, env=ENV)
            self.assertIsNone(c.proxy)
            self.assertEqual(_proxies_of(c._transport.opener), {})
            self.assertEqual(c.proxy_display, "none (direct)")
            self.assertEqual(_proxies_of(llm_transport.opener), {})
            # os.environ as env: HTTPS_PROXY is not KIMI_HTTPS_PROXY
            os.environ.pop("KIMI_HTTPS_PROXY", None)
            c = LLMClient({"model": "m"}, state_dir=None)
            self.assertIsNone(c.proxy)

    def test_kimi_proxy_from_env_or_config(self):
        p = "http://127.0.0.1:1081"
        c = LLMClient({"model": "m"}, state_dir=None, env=dict(ENV, KIMI_HTTPS_PROXY=p))
        self.assertEqual(c.proxy, p)
        self.assertEqual(c.proxy_source, "KIMI_HTTPS_PROXY")
        self.assertEqual(_proxies_of(c._transport.opener), {"https": p, "http": p})
        c = LLMClient({"model": "m", "proxy": "https://127.0.0.1:8443"}, state_dir=None, env=ENV)
        self.assertEqual(c.proxy, "https://127.0.0.1:8443")
        self.assertEqual(c.proxy_source, "llm.proxy")
        c = LLMClient({"model": "m", "proxy": "http://127.0.0.1:2000"}, state_dir=None,
                      env=dict(ENV, KIMI_HTTPS_PROXY="127.0.0.1:1081"))          # the env var wins; bare host:port
        self.assertEqual(c.proxy, "http://127.0.0.1:1081")
        # an explicitly given transport is used as is
        tr = FakeTransport([])
        c = LLMClient({"model": "m"}, state_dir=None, env=dict(ENV, KIMI_HTTPS_PROXY=p), transport=tr)
        self.assertIs(c._transport, tr)

    def test_other_schemes_are_rejected_clearly(self):
        for bad in ("socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1080", "ftp://127.0.0.1:21", "vmess://abc",
                    "http://127.0.0.1", "http://:1081", "http://127.0.0.1:1081/path", "http://127.0.0.1:99999"):
            with self.assertRaises(ConfigError, msg=bad) as cm:
                LLMClient({"model": "m"}, state_dir=None, env=dict(ENV, KIMI_HTTPS_PROXY=bad))
            self.assertIn("KIMI_HTTPS_PROXY", str(cm.exception))
            with self.assertRaises(ConfigError, msg=bad):
                LLMClient({"model": "m", "proxy": bad}, state_dir=None, env=ENV)
        with self.assertRaises(ConfigError) as cm:
            LLMClient({"model": "m", "proxy": "socks5://127.0.0.1:1080"}, state_dir=None, env=ENV)
        self.assertIn("http:// or https://", str(cm.exception))
        with self.assertRaises(ConfigError):
            LLMClient({"model": "m", "proxy": 1081}, state_dir=None, env=ENV)
        # empty = not set
        self.assertIsNone(LLMClient({"model": "m"}, state_dir=None, env=dict(ENV, KIMI_HTTPS_PROXY=" ")).proxy)

    def test_proxy_credentials_are_never_shown(self):
        p = "http://bob:s3cretpw@127.0.0.1:1081"
        with self.assertLogs("bitpin.llm", "INFO") as logs:
            c = LLMClient({"model": "m"}, state_dir=None, env=dict(ENV, KIMI_HTTPS_PROXY=p))
        text = "\n".join(logs.output) + repr(c) + c.proxy_display
        self.assertNotIn("s3cretpw", text)
        self.assertIn("http://***@127.0.0.1:1081", text)
        self.assertEqual(c.redact("proxy %s failed" % p), "proxy <redacted> failed")
        self.assertNotIn("s3cretpw", c.redact("password s3cretpw"))
        # a transport error through a proxy with credentials does not leak them
        tr = make_llm_transport("http://bob:s3cretpw@127.0.0.1:%d" % _free_port())
        with self.assertRaises(TransportError) as cm:
            tr("GET", "http://example.invalid/v1/models", {}, None, 3)
        self.assertNotIn("s3cretpw", str(cm.exception))
        self.assertIn("***@", str(cm.exception))

    def test_requests_really_go_through_the_configured_proxy_only(self):
        from unittest import mock
        seen = []

        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = b'{"data": [{"id": "via-proxy"}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv, url = _serve(Proxy)
        try:
            with mock.patch.dict(os.environ, self.SYSTEM_PROXIES):     # dead system proxies are ignored
                tr = make_llm_transport(url)
                status, body = tr("GET", "http://api.example.invalid/v1/models", {}, None, 5)
                self.assertEqual(status, 200)
                self.assertEqual(seen, ["http://api.example.invalid/v1/models"])   # an absolute URL: a proxy request
                # direct: the system proxy (a closed port) would fail; the server is reached directly
                status, _ = llm_transport("GET", url + "/direct", {}, None, 5)
                self.assertEqual(status, 200)
                self.assertEqual(seen[-1], "/direct")
        finally:
            srv.shutdown()
            srv.server_close()


def _free_port():
    import socket as _s
    s = _s.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


class TestTokenCaps(LLMTestBase):
    def test_tokens_per_call_stop_the_searching(self):
        big = {"prompt_tokens": 140000, "completion_tokens": 10, "total_tokens": 140010}
        script = [(200, completion("", "tool_calls", [search_call(i)], big)) for i in range(2)]
        script.append((200, completion('{"targets": {}}')))
        c, tr = self.client(script, max_tokens_per_call=250000, max_prompt_tokens_per_call=None)
        out = c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertTrue(out["forced_final"])
        self.assertEqual(out["tool_rounds"], 2)
        self.assertEqual(len(tr.requests), 3)
        self.assertNotIn("tools", tr.requests[2]["body"])
        self.assertEqual(tr.requests[2]["body"]["messages"][-1]["content"], FINAL_ANSWER_NUDGE)

    def test_tokens_fraction(self):
        c, _ = self.client([(200, completion("{}"))], max_tokens_per_day=1000)
        self.assertEqual(c.budget.tokens_fraction(), 0.0)
        c.chat([{"role": "user", "content": "u"}])
        self.assertAlmostEqual(c.budget.tokens_fraction(), 0.12)
        c2, _ = self.client([], max_tokens_per_day=None)
        self.assertEqual(c2.budget.tokens_fraction(), 0.0)


def _read_request(conn):
    """Read the WHOLE request (headers + Content-Length body): a socket closed with unread data sends a
    TCP reset on Windows, which could abort the client's read of a complete response (a flaky test)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    n = 0
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"content-length":
            n = int(v.strip() or 0)
    while len(body) < n:
        chunk = conn.recv(65536)
        if not chunk:
            break
        body += chunk
    return data


class _RawServer:
    """A one-connection local TCP server that runs script(conn, stop) after reading the request."""

    def __init__(self, script):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.th = threading.Thread(target=self._run, args=(script,), daemon=True)
        self.th.start()

    def _run(self, script):
        try:
            self.sock.settimeout(10)
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            try:
                conn.settimeout(10)
                _read_request(conn)
                script(conn, self.stop)
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.th.join(10)
        self.sock.close()
        for th in threading.enumerate():          # let the transport's worker see the closed connection
            if th.name == "news-http":
                th.join(10)


def _trickle_body(conn, stop):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n{")
    while not stop.wait(0.3):                     # one byte every 0.3 s: no single read ever times out
        conn.sendall(b" ")


def _trickle_headers(conn, stop):
    for ch in b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 1000:
        if stop.wait(0.3):
            return
        conn.sendall(bytes([ch]))


class TestTransport(unittest.TestCase):
    def _elapsed(self, script, timeout=2.0):
        srv = _RawServer(script)
        self.addCleanup(srv.close)
        t0 = time.monotonic()
        with self.assertRaises(TransportError) as cm:
            make_llm_transport(None)("POST", "http://127.0.0.1:%d/v1/chat/completions" % srv.port,
                                     {"Content-Type": "application/json"}, b"{}", timeout)
        self.assertTrue(cm.exception.timeout)
        return time.monotonic() - t0

    def test_trickling_body_is_cut_at_the_timeout(self):
        el = self._elapsed(_trickle_body)
        self.assertGreaterEqual(el, 1.9)
        self.assertLess(el, 2.0 + HARD_LIMIT_GRACE_SECONDS + 0.3)

    def test_trickling_headers_are_cut_by_the_hard_limit(self):
        el = self._elapsed(_trickle_headers)
        self.assertGreaterEqual(el, 1.9)
        self.assertLess(el, 2.0 + HARD_LIMIT_GRACE_SECONDS + 0.3)

    @unittest.skipIf(ThreadingHTTPServer is None, "ThreadingHTTPServer not available")
    def test_no_proxy_env_does_not_bypass_the_kimi_proxy(self):
        seen = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        srv, url = _serve(H)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        target = "http://api.example.invalid/v1/models"
        for val in ("*", "api.example.invalid", ".invalid", "example.invalid,127.0.0.1"):
            with self.subTest(no_proxy=val):
                with mock.patch.dict(os.environ, {"NO_PROXY": val, "no_proxy": val}):
                    c = LLMClient({"model": "m"}, state_dir=None, env=dict(ENV, KIMI_HTTPS_PROXY=url))
                    st, raw = c._transport("GET", target, {}, None, 10)
                self.assertEqual(st, 200)
                self.assertEqual(seen[-1], target)            # an absolute-URL request line = via the proxy
                phs = [h for h in c._transport.opener.handlers if isinstance(h, StrictProxyHandler)]
                self.assertEqual(len(phs), 1)

    def test_trickling_response_is_cut_at_the_request_time_limit(self):
        try:
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        except ImportError:  # pragma: no cover
            self.skipTest("ThreadingHTTPServer not available")

        class Trickle(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                try:
                    for _ in range(40):              # one byte every 0.1 s: each read is fast, the body never ends
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.1)
                except (OSError, ValueError):
                    pass

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        srv.daemon_threads = True
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            t0 = time.monotonic()
            with self.assertRaises(TransportError) as cm:
                llm_transport("GET", "http://127.0.0.1:%d/x" % srv.server_address[1], {}, None, 0.8)
            self.assertTrue(cm.exception.timeout)
            self.assertLess(time.monotonic() - t0, 2.5)
        finally:
            srv.shutdown()
            srv.server_close()


# --------------------------------------------------------------------------- streaming (SSE)

def sse_chunk(content=None, reasoning=None, finish=None, tool_calls=None, choice_usage=None, usage=None, role=None,
              model="kimi-k3"):
    delta = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    ch = {"index": 0, "delta": delta, "finish_reason": finish}
    if choice_usage is not None:
        ch["usage"] = choice_usage
    out = {"id": "chatcmpl-s1", "object": "chat.completion.chunk", "created": 1, "model": model, "choices": [ch]}
    if usage is not None:
        out["usage"] = usage
    return out


def sse(*events, **kw):
    """An SSE body: each event a dict (JSON) or a raw data string; [DONE] at the end unless done=False."""
    nl = "\r\n" if kw.get("crlf") else "\n"
    parts = []
    for e in events:
        parts.append("data: " + (e if isinstance(e, str) else json.dumps(e)) + nl + nl)
    if kw.get("done", True):
        parts.append("data: [DONE]" + nl + nl)
    return "".join(parts).encode("utf-8")


MOONSHOT_USAGE = {"prompt_tokens": 9000, "completion_tokens": 1300, "total_tokens": 10300}


def decision_stream(text='{"targets": {"USDT_IRT": 1}, "confidence": 0.4}', usage=MOONSHOT_USAGE, done=True,
                    reasoning=('Let me think... {"targets": {"PEPE_IRT": 1}}', " more thinking")):
    """A kimi-k3 style stream: role, reasoning deltas, the JSON split into pieces, usage in the LAST
    chunk's choice (Moonshot's place for it), then [DONE]."""
    ev = [sse_chunk(role="assistant", content="")]
    ev += [sse_chunk(reasoning=r) for r in reasoning]
    third = max(1, len(text) // 3)
    ev += [sse_chunk(content=text[i:i + third]) for i in range(0, len(text), third)]
    ev.append(sse_chunk(finish="stop", choice_usage=usage))
    return sse(*ev, done=done)


class TestStreaming(LLMTestBase):
    def test_request_asks_for_a_stream_with_usage(self):
        c, tr = self.client([(200, decision_stream())])
        out = c.chat([{"role": "user", "content": "u"}])
        body = tr.requests[0]["body"]
        self.assertIs(body["stream"], True)
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertTrue(out["streamed"])
        self.assertTrue(c.streaming)

    def test_sse_reply_is_rebuilt_and_reasoning_only_reaches_the_reasoning_field(self):
        """v3 B2: the reasoning deltas are joined into res["reasoning"] (for the brain's reasoning file)
        and nowhere else: not in the content, not in the usage log, not in the spend file."""
        c, tr = self.client([(200, decision_stream())])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(out["content"], '{"targets": {"USDT_IRT": 1}, "confidence": 0.4}')
        self.assertEqual(out["finish_reason"], "stop")
        self.assertEqual(out["usage"]["total_tokens"], 10300)            # from the stream, not estimated
        self.assertFalse(out["usage_estimated"])
        self.assertEqual(out["model"], "kimi-k3")
        self.assertEqual(c.budget.tokens_used(), 10300)
        self.assertEqual(out["reasoning"], 'Let me think... {"targets": {"PEPE_IRT": 1}} more thinking')
        rest = dict(out)
        rest.pop("reasoning")
        self.assertNotIn("PEPE", json.dumps(rest))
        for fn in ("kimi_usage.jsonl", "llm_spend.json"):
            with open(os.path.join(self.dir, fn)) as f:
                line = f.read()
            self.assertNotIn("PEPE", line, fn)
            self.assertNotIn("thinking", line, fn)
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            self.assertIn('"stream": true', f.read())

    def test_openai_style_usage_chunk_and_crlf_and_comments(self):
        body = (b": keep-alive\r\n\r\n" +
                sse(sse_chunk(content='{"a": '), sse_chunk(content="1}", finish="stop"),
                    {"id": "x", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
                    crlf=True))
        c, _ = self.client([(200, body)])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(out["content"], '{"a": 1}')
        self.assertEqual(out["usage"]["total_tokens"], 7)

    def test_a_stream_without_usage_is_estimated_and_charged(self):
        c, tr = self.client([(200, decision_stream(usage=None))])
        msgs = [{"role": "user", "content": "x" * 3500}]
        out = c.chat(msgs)
        self.assertTrue(out["usage_estimated"])
        req_bytes = len(json.dumps(tr.requests[0]["body"], ensure_ascii=False).encode("utf-8"))
        self.assertGreaterEqual(out["usage"]["prompt_tokens"], req_bytes / 3.5 - 1)
        self.assertGreater(out["usage"]["completion_tokens"], 0)          # content + reasoning characters
        self.assertEqual(c.budget.tokens_used(), out["usage"]["total_tokens"])
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            self.assertIn('"usage_estimated": true', f.read())

    def test_a_stream_cut_midway_is_retried_and_charged_what_it_carried(self):
        cut = decision_stream(done=False)
        cut = cut[:cut.index(b'"finish_reason": "stop"') - 200]           # cut before the last chunk
        c, tr = self.client([(200, cut), (200, decision_stream())], max_tokens=32000)
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 2)
        self.assertEqual(out["content"], '{"targets": {"USDT_IRT": 1}, "confidence": 0.4}')
        self.assertEqual(c.budget.used(), 1)                              # one chat() call
        # the cut reply was already generating: the prompt and what it streamed are charged too - not
        # prompt + max_tokens (review finding: every cut stream cost ~41k tokens of the daily budget)
        self.assertGreater(c.budget.tokens_used(), 10300)
        self.assertLess(c.budget.tokens_used(), 10300 + 1000)
        self.assertEqual(len(self.clock.sleeps), 1)

    def test_a_stream_the_tunnel_cuts_is_charged_by_what_arrived(self):
        """Review finding (decision, medium): each stream the tunnel cut after the headers was charged
        prompt + max_tokens (41k tokens for kimi-k3), a failed decision twice, and a few of them used up
        llm.max_tokens_per_day - blocking the crash veto and the fill review until 03:30 Tehran."""
        partial = decision_stream(reasoning=("r" * 7000,), done=False)
        partial = partial[:partial.index(b'"finish_reason": "stop"') - 200]

        class Cut(FakeTransport):
            def __call__(self, method, url, headers, body, timeout):
                self.requests.append({"method": method, "url": url, "headers": dict(headers),
                                      "body": json.loads(body.decode("utf-8")) if body else None,
                                      "timeout": timeout})
                self.clock.t += 125
                e = http.client.RemoteDisconnected("Remote end closed connection without response")
                e.after_headers = True
                e.partial_body = partial
                raise e
        tr = Cut([], self.clock)
        c = LLMClient({"model": "kimi-k3", "max_tokens": 32000, "max_tokens_per_day": 300000,
                       "backoff_seconds": 1.0}, state_dir=self.dir, transport=tr, env=ENV,
                      sleep=self.clock.sleep, clock=self.clock, monotonic=self.clock)
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "u" * 3500}])
        self.assertEqual(len(tr.requests), 2)                   # max_timeout_retries 1: still sparing
        per_request = c.budget.tokens_used() / 2.0
        self.assertGreater(per_request, 7000 / 4.0)             # the streamed reasoning is charged ...
        self.assertLess(per_request, 8000)                      # ... not prompt + 32000 max_tokens
        # a NON-streamed request cut the same way (no partial stream): the whole reply was most likely
        # made - prompt + max_tokens, as before
        c2 = LLMClient({"model": "kimi-k3", "max_tokens": 32000, "max_tokens_per_day": 300000, "stream": False,
                        "backoff_seconds": 1.0}, state_dir=os.path.join(self.dir, "ns"), transport=Cut([], self.clock),
                       env=ENV, sleep=self.clock.sleep, clock=self.clock, monotonic=self.clock)
        with self.assertRaises(LLMError):
            c2.chat([{"role": "user", "content": "u"}])
        self.assertGreaterEqual(c2.budget.tokens_used(), 2 * 32000)

    def test_the_default_transport_keeps_the_part_of_a_body_that_arrived(self):
        class Resp(object):
            status = 200

            def __init__(self):
                self.chunks = [b'data: {"choices": [{"delta": {"reasoning_content": "abc"}}]}\n\n', b"data: {"]

            def read1(self, n):
                if self.chunks:
                    return self.chunks.pop(0)
                raise ConnectionResetError("reset by peer")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener(object):
            handlers = []

            def open(self, req, timeout=None):
                return Resp()
        tr = make_llm_transport(opener=Opener())
        with self.assertRaises(TransportError) as cm:
            tr("POST", "https://api.moonshot.ai/v1/chat/completions", {}, b"{}", 5)
        self.assertTrue(cm.exception.after_headers)
        self.assertTrue(cm.exception.partial_body.startswith(b"data: {"))
        from bitpin.news import stream_char_counts
        meta = stream_char_counts(cm.exception.partial_body)
        self.assertEqual((meta["chunks"], meta["reasoning_chars"]), (1, 3))
        self.assertEqual(stream_char_counts(b"not a stream")["chunks"], 0)
        self.assertEqual(stream_char_counts(None)["chunks"], 0)

    def test_a_second_cut_gives_up_after_max_timeout_retries(self):
        cut = decision_stream(done=False)[:150]
        c, tr = self.client([(200, cut), (200, cut)])
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertIn("stream cut", str(cm.exception))
        self.assertEqual(len(tr.requests), 2)                           # max_timeout_retries=1: billed twice at most

    def test_error_event_inside_the_stream_is_a_network_error(self):
        err = sse(sse_chunk(reasoning="hm"), {"error": {"message": "server overloaded", "type": "engine_error"}},
                  done=False)
        c, tr = self.client([(200, err), (200, decision_stream())])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 2)
        self.assertIn("USDT_IRT", out["content"])

    def test_unreadable_chunk_is_retried(self):
        bad = sse(sse_chunk(content="{"), "{not json", done=True)
        c, tr = self.client([(200, bad), (200, decision_stream())])
        c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 2)

    def test_stream_options_refused_by_name_is_dropped_for_good(self):
        c, tr = self.client([(400, {"error": {"message": "Unknown parameter: stream_options"}}),
                             (200, decision_stream()), (200, decision_stream())])
        c.chat([{"role": "user", "content": "u"}])
        c.chat([{"role": "user", "content": "u"}])
        b = [r["body"] for r in tr.requests]
        self.assertIn("stream_options", b[0])
        self.assertTrue(b[1]["stream"] and "stream_options" not in b[1])
        self.assertTrue(b[2]["stream"] and "stream_options" not in b[2])
        self.assertEqual(c.budget.used(), 2)                              # the fallback costs no extra call
        self.assertTrue(c.streaming)
        self.assertIn("stream_options", c.stream_note)

    def test_stream_refused_by_name_falls_back_once_to_normal_requests(self):
        with self.assertLogs("bitpin.news", "WARNING") as logs:
            c, tr = self.client([(400, {"error": {"message": "stream mode is not supported together with "
                                                            "response_format"}}),
                                 (200, completion('{"x": 1}')), (200, completion('{"x": 2}'))])
            self.assertEqual(c.chat([{"role": "user", "content": "u"}])["content"], '{"x": 1}')
        self.assertIn("NOT streamed", "\n".join(logs.output))
        c.chat([{"role": "user", "content": "u"}])
        self.assertNotIn("stream", tr.requests[1]["body"])
        self.assertNotIn("stream", tr.requests[2]["body"])
        self.assertFalse(c.streaming)
        self.assertIn("refused stream=true", c.stream_note)

    def test_an_unexplained_400_steps_down_and_remembers_what_worked(self):
        c, tr = self.client([(400, {"error": {"message": "Invalid request"}}),
                             (400, {"error": {"message": "Invalid request"}}),
                             (200, completion('{"x": 1}')), (200, completion('{"x": 2}'))])
        c.chat([{"role": "user", "content": "u"}])
        b = [r["body"] for r in tr.requests]
        self.assertEqual([("stream" in x, "stream_options" in x) for x in b[:3]],
                         [(True, True), (True, False), (False, False)])
        c.chat([{"role": "user", "content": "u"}])
        self.assertNotIn("stream", tr.requests[3]["body"])
        self.assertFalse(c.streaming)

    def test_a_400_that_is_not_about_streaming_changes_nothing(self):
        c, tr = self.client([(400, {"error": {"message": "context too long"}})] * 3)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(len(tr.requests), 3)                             # levels 0, 1 and 2 were all refused
        self.assertTrue(c.streaming)                                      # so streaming stays on
        self.assertEqual(c.stream_note, "")
        self.assertEqual(c.budget.used(), 1)

    def test_after_a_streamed_success_a_400_costs_no_extra_request(self):
        c, tr = self.client([(200, decision_stream()), (400, {"error": {"message": "bad request"}})])
        c.chat([{"role": "user", "content": "u"}])
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(len(tr.requests), 2)

    def test_a_server_ignoring_stream_answers_with_json(self):
        c, _ = self.client([(200, completion('{"ok": 1}'))])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual((out["content"], out["streamed"], out["usage_estimated"]), ('{"ok": 1}', False, False))

    def test_stream_false_never_streams(self):
        c, tr = self.client([(200, completion('{"ok": 1}'))], stream=False)
        c.chat([{"role": "user", "content": "u"}])
        self.assertNotIn("stream", tr.requests[0]["body"])
        self.assertFalse(c.streaming)
        with self.assertRaises(ConfigError):
            validate_llm_config({"stream": "yes"})

    def test_streamed_tool_calls_are_joined_and_echoed_unchanged(self):
        args = '{"search_result":{"search_id":"s1" ,  "query":"bitcoin"},"usage":{"total_tokens": 900}}'
        tc = [{"index": 0, "id": "call_1", "type": "builtin_function",
               "function": {"name": "$web_search", "arguments": args[:10]}}]
        round1 = sse(sse_chunk(role="assistant", tool_calls=tc),
                     sse_chunk(tool_calls=[{"index": 0, "function": {"arguments": args[10:40]}}]),
                     sse_chunk(tool_calls=[{"index": 0, "function": {"arguments": args[40:]}}]),
                     sse_chunk(finish="tool_calls", choice_usage={"total_tokens": 50}))
        c, tr = self.client([(200, round1), (200, decision_stream())])
        out = c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertEqual(out["tool_rounds"], 1)
        self.assertEqual(out["search_calls"], 1)
        echo = tr.requests[1]["body"]["messages"][-2]
        self.assertEqual(echo, {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "builtin_function", "function": {"name": "$web_search", "arguments": args}}]})
        self.assertEqual(tr.requests[1]["body"]["messages"][-1]["content"], args)
        self.assertEqual(out["usage"]["search"]["total_tokens"], 900)

    def test_default_transport_allows_large_streams(self):
        c = LLMClient({"model": "m"}, state_dir=None, env=ENV)
        self.assertGreaterEqual(c._transport.max_bytes, 64 * 1024 * 1024)


def _chunked(conn, pieces, delay=0.05, finish=True):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
    for p in pieces:
        conn.sendall(b"%x\r\n%s\r\n" % (len(p), p))
        time.sleep(delay)
    if finish:
        conn.sendall(b"0\r\n\r\n")


class TestStreamingOverASocket(unittest.TestCase):
    """The default transport reads a chunked event stream as it arrives (each event keeps the tunnel
    busy) and turns a connection closed in the middle of the stream into a network error."""

    def _post(self, script, timeout=5.0):
        srv = _RawServer(script)
        self.addCleanup(srv.close)
        return make_llm_transport(None)("POST", "http://127.0.0.1:%d/v1/chat/completions" % srv.port,
                                        {"Content-Type": "application/json"}, b'{"stream": true}', timeout)

    def test_a_chunked_stream_is_read_to_its_end(self):
        from bitpin.news import parse_chat_stream
        body = decision_stream()
        pieces = [body[i:i + 40] for i in range(0, len(body), 40)]
        status, raw = self._post(lambda conn, stop: _chunked(conn, pieces, delay=0.01))
        self.assertEqual(status, 200)
        payload, meta = parse_chat_stream(raw)
        self.assertEqual(payload["choices"][0]["message"]["content"], '{"targets": {"USDT_IRT": 1}, "confidence": 0.4}')
        self.assertTrue(meta["done"] and meta["usage_in_stream"])

    def test_a_connection_closed_mid_stream_is_a_network_error(self):
        body = decision_stream()
        with self.assertRaises(TransportError) as cm:
            self._post(lambda conn, stop: _chunked(conn, [body[:120], body[120:240]], finish=False))
        self.assertFalse(cm.exception.timeout)
        self.assertTrue(cm.exception.after_headers)          # accepted (and billed) before the cut


# --------------------------------------------------------------------------- v3 B1: reasoning_effort

class TestReasoningEffort(LLMTestBase):
    def test_config_accepts_null_and_the_three_levels(self):
        self.assertIsNone(validate_llm_config({"model": "kimi-k3"})["reasoning_effort"])
        self.assertIsNone(validate_llm_config({"model": "kimi-k3", "reasoning_effort": None})["reasoning_effort"])
        for v, want in (("low", "low"), ("high", "high"), ("max", "max"), (" High ", "high"), ("MAX", "max")):
            self.assertEqual(validate_llm_config({"model": "kimi-k3", "reasoning_effort": v})["reasoning_effort"], want)

    def test_config_refuses_other_values_and_the_thinking_key(self):
        for bad in ("medium", "", "  ", 1, True, ["high"], {"level": "high"}):
            with self.assertRaises(ConfigError) as cm:
                validate_llm_config({"model": "kimi-k3", "reasoning_effort": bad})
            self.assertIn("reasoning_effort", str(cm.exception))
            self.assertIn("low/high/max", str(cm.exception))
        # the kimi-k2.x "thinking" parameter is refused by kimi-k3: fail at startup, naming the right key
        for key in ("thinking", "Thinking"):
            with self.assertRaises(ConfigError) as cm:
                validate_llm_config({"model": "kimi-k3", key: {"type": "enabled"}})
            self.assertIn("thinking", str(cm.exception))
            self.assertIn("reasoning_effort", str(cm.exception))

    def test_not_sent_by_default_sent_top_level_when_set(self):
        c, tr = self.client([(200, completion("{}"))])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertNotIn("reasoning_effort", tr.requests[0]["body"])
        self.assertIsNone(out["reasoning_effort"])
        c, tr = self.client([(200, completion("{}"))], reasoning_effort="high")
        out = c.chat([{"role": "user", "content": "u"}])
        body = tr.requests[0]["body"]
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("thinking", body)
        self.assertEqual(out["reasoning_effort"], "high")
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            self.assertEqual(json.loads(f.readlines()[-1])["reasoning_effort"], "high")

    def test_per_call_override_wins_and_is_sent_in_every_round(self):
        script = [(200, completion("", "tool_calls", [search_call(1)])), (200, completion("{}"))]
        c, tr = self.client(script, reasoning_effort="high")
        out = c.chat([{"role": "user", "content": "u"}], web_search=True, reasoning_effort="max")
        self.assertEqual([r["body"]["reasoning_effort"] for r in tr.requests], ["max", "max"])
        self.assertEqual(out["reasoning_effort"], "max")
        # the override is normalised like the config value; None means "the configured level"
        c, tr = self.client([(200, completion("{}"))], reasoning_effort="high")
        c.chat([{"role": "user", "content": "u"}], reasoning_effort=" Low ")
        self.assertEqual(tr.requests[0]["body"]["reasoning_effort"], "low")
        c, tr = self.client([(200, completion("{}"))], reasoning_effort="high")
        c.chat([{"role": "user", "content": "u"}], reasoning_effort=None)
        self.assertEqual(tr.requests[0]["body"]["reasoning_effort"], "high")

    def test_a_bad_override_fails_before_any_request_or_budget_use(self):
        c, tr = self.client([(200, completion("{}"))])
        for bad in ("medium", 3, ""):
            with self.assertRaises(ValueError):
                c.chat([{"role": "user", "content": "u"}], reasoning_effort=bad)
        self.assertEqual(tr.requests, [])
        self.assertEqual(c.budget.used(), 0)


# --------------------------------------------------------------------------- v3 B2: the reasoning text

class TestReasoningCapture(LLMTestBase):
    def test_rounds_are_joined_redacted_and_never_echoed(self):
        script = [(200, completion("", "tool_calls", [search_call(1)], reasoning="first think " + KEY)),
                  (200, completion("{}", reasoning="second think"))]
        c, tr = self.client(script)
        out = c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertEqual(out["reasoning"], "first think <redacted>\n\nsecond think")
        # the echoed assistant turn of round 2 carries no reasoning (the verified echo format)
        echoed = [m for m in tr.requests[1]["body"]["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(echoed), 1)
        self.assertNotIn("reasoning_content", echoed[0])
        self.assertNotIn("first think", json.dumps(tr.requests[1]["body"]))

    def test_no_reasoning_gives_an_empty_string(self):
        c, _ = self.client([(200, completion("{}"))])
        self.assertEqual(c.chat([{"role": "user", "content": "u"}])["reasoning"], "")
        c, _ = self.client([(200, completion("{}", reasoning="   "))])
        self.assertEqual(c.chat([{"role": "user", "content": "u"}])["reasoning"], "")

    def test_a_runaway_reasoning_is_capped(self):
        c, _ = self.client([(200, decision_stream(reasoning=("a" * 40, "b" * 40)))])
        with mock.patch("bitpin.llm.REASONING_MAX_CHARS", 50):
            out = c.chat([{"role": "user", "content": "u"}])
        self.assertTrue(out["reasoning"].startswith("a" * 40 + "b" * 10))
        self.assertTrue(out["reasoning"].endswith("[reasoning truncated]"))
        self.assertLessEqual(len(out["reasoning"]), 50 + len("\n[reasoning truncated]"))

    def test_reasoning_never_reaches_journald_style_logging(self):
        c, _ = self.client([(200, decision_stream(reasoning=("SECRET-THOUGHT-XYZ",)))])
        with self.assertLogs("bitpin", level="DEBUG") as cm:
            logging.getLogger("bitpin.llm").debug("marker")       # assertLogs needs at least one record
            c.chat([{"role": "user", "content": "u"}])
        self.assertNotIn("SECRET-THOUGHT-XYZ", "\n".join(cm.output))


# --------------------------------------------------------------------------- v3 B4: the cost meter (stage 2 side)

class TestSpendMeter(LLMTestBase):
    def test_usage_records_are_priced_and_the_spend_file_is_refreshed(self):
        u = {"prompt_tokens": 31000, "completion_tokens": 5200, "total_tokens": 36200}
        c, _ = self.client([(200, completion("{}", usage=u))])
        c.chat([{"role": "user", "content": "u"}])
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["stage"], "llm")
        self.assertEqual(rec["usd"], 0.171)                    # 31k * $3/M + 5.2k * $15/M
        with open(os.path.join(self.dir, "llm_spend.json")) as f:
            s = json.load(f)
        self.assertEqual(s["today_usd"], 0.171)
        self.assertEqual(s["total_requests"], 1)
        self.assertEqual(s["day"], "2026-09-22")
        self.assertFalse(s["quota_alert"])

    def test_configured_prices_and_cached_tokens(self):
        u = {"prompt_tokens": 10000, "completion_tokens": 1000, "total_tokens": 11000, "cached_tokens": 8000}
        c, _ = self.client([(200, completion("{}", usage=u))], price_in_per_m=2.0, price_out_per_m=10.0,
                           price_cached_in_per_m=0.5)
        c.chat([{"role": "user", "content": "u"}])
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            rec = json.loads(f.readline())
        self.assertAlmostEqual(rec["usd"], (2000 * 2.0 + 8000 * 0.5 + 1000 * 10.0) / 1e6, places=9)
        for bad in ({"price_in_per_m": -1}, {"price_out_per_m": "15"}, {"price_cached_in_per_m": 5000}):
            with self.assertRaises(ConfigError):
                validate_llm_config(dict({"model": "m"}, **bad))
        cfg = validate_llm_config({"model": "m"})
        self.assertEqual((cfg["price_in_per_m"], cfg["price_out_per_m"], cfg["price_cached_in_per_m"]), (3.0, 15.0, 0.3))

    def test_an_exhausted_quota_is_logged_and_two_in_a_row_raise_the_alert(self):
        quota = {"error": {"message": "Your account balance is insufficient (quota exhausted)", "type": "quota"}}
        c, tr = self.client([(429, quota), (429, quota), (200, completion("{}"))])
        for _ in range(2):
            with self.assertRaises(LLMError) as cm:
                c.chat([{"role": "user", "content": "u"}])
            self.assertEqual(cm.exception.kind, "llm_quota")
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            recs = [json.loads(line) for line in f]
        self.assertEqual([r.get("error") for r in recs], ["llm_quota", "llm_quota"])
        self.assertEqual(recs[0]["status"], 429)
        self.assertEqual(recs[0]["usage"], {})
        with open(os.path.join(self.dir, "llm_spend.json")) as f:
            s = json.load(f)
        self.assertEqual((s["quota_errors_in_a_row"], s["quota_alert"], s["total_usd"]), (2, True, 0.0))
        # the next successful request ends the streak
        c.chat([{"role": "user", "content": "u"}])
        with open(os.path.join(self.dir, "llm_spend.json")) as f:
            s = json.load(f)
        self.assertEqual((s["quota_errors_in_a_row"], s["quota_alert"]), (0, False))
        self.assertEqual(len(tr.requests), 3)

    def test_a_lost_reply_is_metered_as_estimated(self):
        c, _ = self.client([TIMEOUT, TIMEOUT], max_retries=1, max_timeout_retries=1, max_tokens=1000)
        with self.assertRaises(LLMError):
            c.chat([{"role": "user", "content": "x" * 350}])
        with open(os.path.join(self.dir, "kimi_usage.jsonl")) as f:
            recs = [json.loads(line) for line in f]
        self.assertEqual(len(recs), 2)
        for r in recs:
            self.assertTrue(r["lost"] and r["usage_estimated"])
            self.assertGreater(r["usd"], 0)
            self.assertEqual(r["usage"]["completion_tokens"], 1000)
        with open(os.path.join(self.dir, "llm_spend.json")) as f:
            s = json.load(f)
        self.assertEqual(s["estimated_requests"], 2)
        self.assertAlmostEqual(s["today_usd"], sum(r["usd"] for r in recs), places=6)

    def test_in_memory_client_writes_nothing(self):
        c, _ = self.client([(200, completion("{}"))], state_dir=None)
        c.chat([{"role": "user", "content": "u"}])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "llm_spend.json")))


# --------------------------------------------------------------------------- OpenRouter / generic providers

from bitpin.llm import (OPENROUTER_BASE_URL, detect_provider, is_thinking_model, model_base_name,  # noqa: E402
                        parse_model_list)
from bitpin.news import parse_chat_stream, stream_char_counts  # noqa: E402

OR_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
OR_ENV = {"OPENROUTER_API_KEY": OR_KEY}
RELAY = "https://relay.example.com/v1"


def or_chunk(**delta):
    """An OpenRouter stream chunk whose delta is exactly `delta` (reasoning / reasoning_details / content ...)."""
    return {"id": "gen-1", "object": "chat.completion.chunk", "model": "openai/gpt-5",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def or_stream(*events, **kw):
    """An OpenRouter event stream: its ': OPENROUTER PROCESSING' keep-alive comments first, then the events."""
    head = b": OPENROUTER PROCESSING\n\n: OPENROUTER PROCESSING\n\n"
    return head + sse(*events, **kw)


OR_USAGE = {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200, "cost": 0.0042, "is_byok": False,
            "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 150}}


class OpenRouterBase(LLMTestBase):
    def orclient(self, script, env=None, **cfg):
        base = {"model": "openai/gpt-5", "base_url": OPENROUTER_BASE_URL, "api_key_env": "OPENROUTER_API_KEY"}
        base.update(cfg)
        tr = FakeTransport(script, self.clock)
        c = LLMClient(base, state_dir=self.dir, transport=tr, env=OR_ENV if env is None else env,
                      sleep=self.clock.sleep, clock=self.clock, monotonic=self.clock)
        return c, tr

    def usage_log(self):
        with open(os.path.join(self.dir, "kimi_usage.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]


class TestProviderDetection(unittest.TestCase):
    def test_detect_provider_by_host_or_explicit_value(self):
        for url, prov, want in (
                (OPENROUTER_BASE_URL, "auto", "openrouter"), ("https://OpenRouter.ai/api/v1", None, "openrouter"),
                ("https://eu.openrouter.ai/api/v1", "auto", "openrouter"),
                ("https://api.moonshot.ai/v1", "auto", "moonshot"),
                ("https://api.moonshot.cn/v1", " AUTO ", "moonshot"),
                (RELAY, "auto", "openai"), ("https://openrouter.ai.evil.example/v1", "auto", "openai"),
                ("https://notopenrouter.ai/v1", "auto", "openai"), ("", "auto", "openai"), (None, "auto", "openai"),
                (RELAY, "openrouter", "openrouter"), (RELAY, "Moonshot", "moonshot"), (RELAY, "openai", "openai")):
            self.assertEqual(detect_provider(url, prov), want, (url, prov))
        self.assertEqual(detect_provider(OPENROUTER_BASE_URL), "openrouter")          # provider defaults to "auto"
        for bad in ("anthropic", "", 5, True, ["openrouter"]):
            with self.assertRaises(ValueError):
                detect_provider(OPENROUTER_BASE_URL, bad)

    def test_the_provider_key_is_validated_against_the_host(self):
        self.assertEqual(validate_llm_config({"model": "m"})["provider"], "auto")
        self.assertEqual(validate_llm_config({"model": "m", "provider": None})["provider"], "auto")
        ok = validate_llm_config({"model": "m", "provider": " OpenRouter ", "base_url": OPENROUTER_BASE_URL,
                                  "api_key_env": "OPENROUTER_API_KEY"})
        self.assertEqual(ok["provider"], "openrouter")
        # an explicit dialect for a relay host auto cannot recognise
        relay = validate_llm_config({"model": "m", "provider": "openrouter", "base_url": RELAY,
                                     "api_key_env": "OPENROUTER_API_KEY"})
        self.assertEqual(relay["provider"], "openrouter")
        for bad, want in (({"provider": "moonshot", "base_url": OPENROUTER_BASE_URL,
                            "api_key_env": "OPENROUTER_API_KEY"}, "contradicts"),
                          ({"provider": "openrouter", "api_key_env": "OPENROUTER_API_KEY"}, "contradicts"),
                          ({"provider": "openai", "base_url": OPENROUTER_BASE_URL,
                            "api_key_env": "OPENROUTER_API_KEY"}, "contradicts"),
                          ({"provider": "anthropic"}, "llm.provider"), ({"provider": 5}, "llm.provider")):
            with self.assertRaises(ConfigError, msg=repr(bad)) as cm:
                validate_llm_config(dict({"model": "m"}, **bad))
            self.assertIn(want, str(cm.exception))

    def test_the_key_variable_is_bound_to_the_platform(self):
        def check(base_url, name, provider="auto"):
            return validate_llm_config({"model": "m", "base_url": base_url, "api_key_env": name, "provider": provider})
        for name in ("OPENROUTER_API_KEY", "OPENROUTER_KEY_2"):
            self.assertEqual(check(OPENROUTER_BASE_URL, name)["api_key_env"], name)
        for name in ("KIMI_API_KEY", "OPENAI_API_KEY", "OPENAI_KEY"):
            self.assertEqual(check(RELAY, name)["api_key_env"], name)
        self.assertEqual(check(RELAY, "OPENROUTER_API_KEY", "openrouter")["provider"], "openrouter")
        for base_url, name, want in (
                (OPENROUTER_BASE_URL, "KIMI_API_KEY", "OPENROUTER_"),      # the Kimi key never goes to OpenRouter
                (OPENROUTER_BASE_URL, "MOONSHOT_API_KEY", "OPENROUTER_"),
                (OPENROUTER_BASE_URL, "BITPIN_SECRET_KEY", "OPENROUTER_"),
                (OPENROUTER_BASE_URL, "openrouter_api_key", "OPENROUTER_"),
                ("https://api.moonshot.ai/v1", "OPENROUTER_API_KEY", "KIMI_* or MOONSHOT_*"),   # nor the reverse
                ("https://api.moonshot.ai/v1", "OPENAI_API_KEY", "KIMI_* or MOONSHOT_*"),
                (RELAY, "OPENROUTER_API_KEY", "OPENAI_*"), (RELAY, "HOME", "OPENAI_*")):
            with self.assertRaises(ConfigError, msg=(base_url, name)) as cm:
                check(base_url, name)
            self.assertIn("api_key_env", str(cm.exception))
            self.assertIn(want, str(cm.exception))

    def test_an_openrouter_key_is_never_sent_to_another_platform(self):
        for cfg in ({"model": "kimi-k3"}, {"model": "m", "base_url": RELAY}):
            tr = FakeTransport([(200, completion("{}"))])
            c = LLMClient(cfg, state_dir=None, transport=tr, env={"KIMI_API_KEY": OR_KEY})
            self.assertFalse(c.has_key)
            with self.assertRaises(LLMAuthError) as cm:
                c.chat([{"role": "user", "content": "u"}])
            self.assertIn("OpenRouter key", str(cm.exception))
            self.assertNotIn(OR_KEY, str(cm.exception) + repr(c))
            self.assertEqual(tr.requests, [])
            self.assertEqual(c.redact("key " + OR_KEY), "key <redacted>")
        c = LLMClient({"model": "openai/gpt-5", "base_url": OPENROUTER_BASE_URL, "api_key_env": "OPENROUTER_API_KEY"},
                      state_dir=None, transport=FakeTransport([]), env=OR_ENV)
        self.assertTrue(c.has_key)
        self.assertEqual(c.provider, "openrouter")


class TestOpenRouterRequests(OpenRouterBase):
    def test_request_shape_attribution_and_usage(self):
        c, tr = self.orclient([(200, completion('{"a": 1}'))], stream=False)
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(out["content"], '{"a": 1}')
        r = tr.requests[0]
        self.assertEqual(r["url"], OPENROUTER_BASE_URL + "/chat/completions")
        self.assertEqual(r["headers"]["Authorization"], "Bearer " + OR_KEY)
        self.assertEqual(r["headers"]["X-Title"], "bitpin-bot")
        self.assertFalse([h for h in r["headers"] if h.lower() in ("http-referer", "referer")])
        body = r["body"]
        self.assertEqual(body["usage"], {"include": True})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual((body["temperature"], body["max_tokens"]), (0.3, 4096))      # not a thinking model
        for k in ("reasoning_effort", "reasoning", "tools", "plugins"):
            self.assertNotIn(k, body)
        self.assertNotIn(OR_KEY, json.dumps(body))

    def test_moonshot_and_generic_requests_carry_no_openrouter_fields(self):
        c, tr = self.client([(200, completion("{}"))], reasoning_effort="high")
        c.chat([{"role": "user", "content": "u"}])
        tr2 = FakeTransport([(200, completion("{}"))], self.clock)
        g = LLMClient({"model": "m", "base_url": RELAY, "reasoning_effort": "low"}, state_dir=None, transport=tr2,
                      env=ENV)
        self.assertEqual(g.provider, "openai")
        g.chat([{"role": "user", "content": "u"}])
        for r, effort in ((tr.requests[0], "high"), (tr2.requests[0], "low")):
            self.assertNotIn("X-Title", r["headers"])
            for k in ("usage", "plugins", "reasoning"):
                self.assertNotIn(k, r["body"])
            self.assertEqual(r["body"]["reasoning_effort"], effort)

    def test_reasoning_effort_goes_as_reasoning_object_on_openrouter(self):
        for configured, per_call, sent in (("max", None, "high"), ("high", None, "high"), ("low", None, "low"),
                                           ("medium", None, "medium"), ("low", "max", "high"),
                                           (None, "Medium", "medium")):
            c, tr = self.orclient([(200, completion("{}"))], reasoning_effort=configured)
            out = c.chat([{"role": "user", "content": "u"}], reasoning_effort=per_call)
            body = tr.requests[0]["body"]
            self.assertEqual(body["reasoning"], {"effort": sent}, (configured, per_call))
            self.assertNotIn("reasoning_effort", body)
            self.assertEqual(out["reasoning_effort"], (per_call or configured).lower())
        # "medium" is OpenRouter's: Moonshot still refuses it at startup and per call
        with self.assertRaises(ConfigError):
            validate_llm_config({"model": "kimi-k3", "reasoning_effort": "medium"})
        c, tr = self.client([(200, completion("{}"))])
        with self.assertRaises(ValueError):
            c.chat([{"role": "user", "content": "u"}], reasoning_effort="medium")
        with self.assertRaises(ConfigError) as cm:
            validate_llm_config({"model": "m", "base_url": OPENROUTER_BASE_URL, "api_key_env": "OPENROUTER_API_KEY",
                                 "reasoning_effort": "extreme"})
        self.assertIn("low/medium/high/max", str(cm.exception))

    def test_thinking_rules_follow_the_model_base_name(self):
        for m, want in (("moonshotai/kimi-k3", "kimi-k3"), ("MoonshotAI/Kimi-K3:free", "kimi-k3:free"),
                        ("kimi-k3", "kimi-k3"), ("openai/gpt-5", "gpt-5"), ("", ""), (None, ""), (5, "")):
            self.assertEqual(model_base_name(m), want, m)
        self.assertTrue(is_thinking_model("moonshotai/kimi-k3"))
        self.assertTrue(is_thinking_model("MoonshotAI/Kimi-K3:free"))
        for m in ("openai/gpt-5", "anthropic/claude-sonnet-4.5", "deepseek/deepseek-chat", "vendor/not-kimi-k3"):
            self.assertFalse(is_thinking_model(m), m)
        cfg = validate_llm_config({"model": "moonshotai/kimi-k3", "base_url": OPENROUTER_BASE_URL,
                                   "api_key_env": "OPENROUTER_API_KEY"})
        self.assertEqual({k: cfg[k] for k in THINKING_DEFAULTS}, THINKING_DEFAULTS)
        c, tr = self.orclient([(200, completion("{}"))], model="moonshotai/kimi-k3")
        c.chat([{"role": "user", "content": "u"}])
        self.assertNotIn("temperature", tr.requests[0]["body"])                    # the kimi-k3 rule, by model
        self.assertEqual(tr.requests[0]["body"]["max_tokens"], 32000)
        c, tr = self.orclient([(200, completion("{}"))], temperature=0.7, max_tokens=12000)
        c.chat([{"role": "user", "content": "u"}])                                 # other models: as configured
        self.assertEqual((tr.requests[0]["body"]["temperature"], tr.requests[0]["body"]["max_tokens"]), (0.7, 12000))

    def test_web_search_is_the_web_plugin_in_one_request(self):
        c, tr = self.orclient([(200, completion('{"a": 1}'))], stream=False)
        out = c.chat([{"role": "user", "content": "u"}], web_search=True)
        self.assertEqual(len(tr.requests), 1)
        body = tr.requests[0]["body"]
        self.assertEqual(body["plugins"], [{"id": "web"}])
        self.assertNotIn("tools", body)
        self.assertEqual((out["tool_rounds"], out["search_calls"], out["json_mode_used"]), (0, 1, True))
        self.assertTrue(self.usage_log()[0]["web_search"])


class TestOpenRouterStreaming(OpenRouterBase):
    def test_comments_are_skipped_and_both_reasoning_shapes_are_kept(self):
        body = or_stream(
            or_chunk(role="assistant", content="", reasoning="Step one. ",
                     reasoning_details=[{"type": "reasoning.text", "text": "Step one. ", "format": "unknown",
                                         "index": 0}]),
            or_chunk(reasoning_details=[{"type": "reasoning.text", "text": "Step two."}]),
            or_chunk(reasoning=None, reasoning_details=[{"type": "reasoning.encrypted", "data": "opaque"}]),
            or_chunk(content='{"targets": '), or_chunk(content='{"USDT_IRT": 1}}'),
            {"id": "gen-1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"id": "gen-1", "choices": [], "usage": OR_USAGE})
        c, tr = self.orclient([(200, body)])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertIs(tr.requests[0]["body"]["stream"], True)
        self.assertEqual(out["content"], '{"targets": {"USDT_IRT": 1}}')
        self.assertEqual(out["reasoning"], "Step one. Step two.")         # once, although sent in both fields
        self.assertEqual((out["usage"]["total_tokens"], out["usage_estimated"], out["streamed"]), (1200, False, True))
        self.assertEqual(c.budget.tokens_used(), 1200)
        rec = self.usage_log()[0]
        self.assertEqual((rec["usd"], rec["provider"]), (0.0042, "openrouter"))     # OpenRouter's billed cost
        self.assertNotIn("Step", json.dumps(rec))

    def test_reasoning_of_a_reply_that_was_not_streamed(self):
        for msg, want in (({"reasoning": "thought"}, "thought"),
                          ({"reasoning_details": [{"type": "reasoning.summary", "summary": "sum"}]}, "sum"),
                          ({"reasoning": "", "reasoning_details": [{"type": "reasoning.text", "text": "t"}]}, "t"),
                          ({"reasoning_content": "moonshot", "reasoning": "other"}, "moonshot"),
                          ({"reasoning_details": "garbage"}, "")):
            reply = completion("{}")
            reply["choices"][0]["message"].update(msg)
            c, _ = self.orclient([(200, reply)], stream=False)
            self.assertEqual(c.chat([{"role": "user", "content": "u"}])["reasoning"], want, msg)

    def test_an_error_chunk_inside_the_stream_is_a_cut_stream(self):
        err = {"id": "gen-1", "object": "chat.completion.chunk", "provider": "OpenAI",
               "error": {"code": "server_error", "message": "Provider disconnected unexpectedly"},
               "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}]}
        body = or_stream(or_chunk(content='{"a": '), err, done=False)
        c, tr = self.orclient([(200, body), (200, completion('{"a": 1}'))])
        out = c.chat([{"role": "user", "content": "u"}])
        self.assertEqual((out["content"], len(tr.requests)), ('{"a": 1}', 2))
        self.assertTrue(self.usage_log()[0].get("lost"))                   # the cut stream was charged
        c, tr = self.orclient([(200, body)], max_retries=0)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertIn("Provider disconnected unexpectedly", str(cm.exception))

    def test_parse_chat_stream_with_openrouter_shapes(self):
        ann = {"type": "url_citation", "url_citation": {"url": "https://www.reuters.com/x", "title": "T"}}
        raw = or_stream(or_chunk(reasoning="ab", reasoning_details=[{"type": "reasoning.text", "text": "ab"}]),
                        or_chunk(content="hi", annotations=[ann]),
                        {"id": "g", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": OR_USAGE})
        payload, meta = parse_chat_stream(raw)
        msg = payload["choices"][0]["message"]
        self.assertEqual((msg["content"], msg["reasoning_content"], msg["annotations"]), ("hi", "ab", [ann]))
        self.assertEqual((meta["reasoning_chars"], meta["content_chars"], meta["chunks"]), (2, 2, 3))
        self.assertEqual(payload["usage"]["cost"], 0.0042)
        self.assertEqual(stream_char_counts(raw)["reasoning_chars"], 2)


class TestOpenRouterErrors(OpenRouterBase):
    CREDITS = {"error": {"code": 402, "message": "Insufficient credits. Add more using https://openrouter.ai/settings"}}

    def test_402_is_an_exhausted_quota_like_moonshots_429(self):
        c, tr = self.orclient([(402, self.CREDITS), (402, self.CREDITS), (200, completion("{}"))])
        for _ in range(2):
            with self.assertRaises(LLMError) as cm:
                c.chat([{"role": "user", "content": "u"}])
            e = cm.exception
            self.assertEqual((e.kind, e.status), ("llm_quota", 402))
            self.assertIn("HTTP 402", str(e))
            self.assertIn("Insufficient credits", str(e))
        self.assertEqual(len(tr.requests), 2)                                  # never retried
        recs = self.usage_log()
        self.assertEqual([(r.get("error"), r["status"], r["usage"], r["usd"]) for r in recs],
                         [("llm_quota", 402, {}, 0.0)] * 2)
        with open(os.path.join(self.dir, "llm_spend.json")) as f:
            s = json.load(f)
        self.assertEqual((s["quota_errors_in_a_row"], s["quota_alert"]), (2, True))
        c.chat([{"role": "user", "content": "u"}])                             # the next success ends the streak
        with open(os.path.join(self.dir, "llm_spend.json")) as f:
            self.assertEqual(json.load(f)["quota_errors_in_a_row"], 0)

    def test_a_402_from_moonshot_keeps_the_old_handling(self):
        c, tr = self.client([(402, {"error": {"message": "pay", "type": "x"}})])
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual((cm.exception.kind, cm.exception.status), ("llm", 402))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "kimi_usage.jsonl")))

    def test_openrouter_error_bodies_are_parsed(self):
        raw = json.dumps({"error": {"message": "Invalid schema for response_format 'x'", "type": "invalid_request"}})
        body = {"error": {"code": 400, "message": "Provider returned error",
                          "metadata": {"raw": raw, "provider_name": "OpenAI"}}}
        c, _ = self.orclient([(400, body)], stream=False)
        with self.assertRaises(LLMError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("Provider returned error [400] (OpenAI: Invalid schema for response_format 'x')",
                      cm.exception.detail)
        for meta, want in (({"raw": "upstream said no " + OR_KEY}, "(provider: upstream said no <redacted>)"),
                           ({"raw": {"error": {"message": "as an object"}}, "provider_name": "X"}, "(X: as an object)"),
                           ({"raw": ""}, "Oops [400]"), ("junk", "Oops [400]")):
            c, _ = self.orclient([(400, {"error": {"code": 400, "message": "Oops", "metadata": meta}})], stream=False)
            with self.assertRaises(LLMError) as cm:
                c.chat([{"role": "user", "content": "u"}])
            self.assertIn(want, cm.exception.detail)
            self.assertNotIn(OR_KEY, str(cm.exception) + cm.exception.detail)
        c, _ = self.orclient([(401, {"error": {"code": 401, "message": "No auth credentials found"}})])
        with self.assertRaises(LLMAuthError) as cm:
            c.chat([{"role": "user", "content": "u"}])
        self.assertIn("No auth credentials found [401]", str(cm.exception))
        self.assertIn("OpenRouter key", str(cm.exception))
        self.assertIn("OPENROUTER_API_KEY", str(cm.exception))
        self.assertNotIn("api.moonshot.cn", str(cm.exception))


class TestModelListing(OpenRouterBase):
    OPENROUTER_MODELS = {"data": [
        {"id": "openai/gpt-5", "name": "OpenAI: GPT-5", "context_length": 400000,
         "pricing": {"prompt": "0.00000125", "completion": "0.00001", "input_cache_read": "0.000000125",
                     "request": "0", "image": "0"},
         "supported_parameters": ["max_tokens", "reasoning", "include_reasoning", "response_format",
                                  "structured_outputs", "temperature"]},
        {"id": "moonshotai/kimi-k3", "name": "MoonshotAI: Kimi K3", "context_length": 262144,
         "pricing": {"prompt": "0.000003", "completion": "0.000015"}, "supported_parameters": ["temperature", "tools"]},
        {"id": "openrouter/auto", "name": "Auto Router", "context_length": 2000000,
         "pricing": {"prompt": "-1", "completion": "-1"}},
        {"id": "meta-llama/llama-4:free", "pricing": {"prompt": "0", "completion": "0"},
         "top_provider": {"context_length": 131072}, "supported_parameters": []},
    ]}

    @staticmethod
    def row(i, name=None, ctx=None, p_in=None, p_out=None, p_cached=None, json_=None, reasoning=None):
        return {"id": i, "name": name, "context_length": ctx, "price_in_per_m": p_in, "price_out_per_m": p_out,
                "price_cached_in_per_m": p_cached, "supports_json": json_, "supports_reasoning": reasoning}

    def test_openrouter_models_are_described_per_million_tokens(self):
        c, tr = self.orclient([(200, self.OPENROUTER_MODELS)])
        self.assertEqual(c.list_models_detailed(), [
            self.row("openai/gpt-5", "OpenAI: GPT-5", 400000, 1.25, 10.0, 0.125, True, True),
            self.row("moonshotai/kimi-k3", "MoonshotAI: Kimi K3", 262144, 3.0, 15.0, None, False, False),
            self.row("openrouter/auto", "Auto Router", 2000000),                 # "-1" = a variable price: unknown
            self.row("meta-llama/llama-4:free", None, 131072, 0.0, 0.0, None, False, False)])
        r = tr.requests[0]
        self.assertEqual((r["method"], r["url"], r["headers"]["X-Title"]), ("GET", OPENROUTER_BASE_URL + "/models",
                                                                            "bitpin-bot"))
        self.assertEqual(c.budget.used(), 0)
        c, _ = self.orclient([(200, self.OPENROUTER_MODELS)])
        self.assertEqual(c.list_models(), ["openai/gpt-5", "moonshotai/kimi-k3", "openrouter/auto",
                                           "meta-llama/llama-4:free"])

    def test_moonshot_lists_ids_only(self):
        models = [{"id": "kimi-k3", "object": "model", "owned_by": "moonshot"}, {"id": "kimi-k2.6", "object": "model"}]
        c, _ = self.client([(200, {"object": "list", "data": models})])
        self.assertEqual(c.list_models_detailed(), [self.row("kimi-k3"), self.row("kimi-k2.6")])

    def test_malformed_entries_never_break_the_listing(self):
        payload = {"data": [
            None, "x", 5, [], {"id": None}, {"id": 5}, {"id": ""}, {"id": "  "}, {"id": "a b"}, {"id": "x\ny"},
            {"id": "z" * 201}, {"no_id": True},
            {"id": "ok/1", "name": 5, "context_length": "big", "pricing": "cheap", "supported_parameters": "reasoning"},
            {"id": "ok/1", "name": "the same id again"},
            {"id": " ok/2 ", "pricing": {"prompt": "abc", "completion": "NaN", "input_cache_read": True},
             "context_length": -5},
            {"id": "ok/3", "pricing": {"prompt": "1e309", "completion": 0.000002, "input_cache_read": "Infinity"},
             "context_length": True, "top_provider": "x"},
            {"id": "ok/4", "name": "Weird​Name\n\tX", "context_length": 8192.0,
             "supported_parameters": [None, 5, {"x": 1}, "response_format"]},
            {"id": "ok/5", "pricing": {"prompt": ["0.1"], "completion": {"x": 1}, "input_cache_read": "5"},
             "context_length": "4096", "top_provider": {"context_length": 99}},
        ]}
        self.assertEqual(parse_model_list(payload), [
            self.row("ok/1"), self.row("ok/2"), self.row("ok/3", p_out=2.0),
            self.row("ok/4", "Weird Name X", 8192, json_=True, reasoning=False),
            self.row("ok/5", ctx=4096)])                              # "5" USD per token is absurd: unknown
        for bad in (None, [], "x", {}, {"data": None}, {"data": "x"}, {"data": {"id": "a"}}, {"data": 5}):
            self.assertEqual(parse_model_list(bad), [], bad)
        c, _ = self.orclient([(200, {"data": 5})])
        self.assertEqual(c.list_models_detailed(), [])
        c, _ = self.orclient([(200, {"data": 5})])
        self.assertEqual(c.list_models(), [])


class TestOpenRouterSpend(OpenRouterBase):
    def test_the_billed_cost_is_the_metered_usd(self):
        base = {"prompt_tokens": 10000, "completion_tokens": 1000, "total_tokens": 11000}
        for usage, want in ((dict(base, cost=0.0123), 0.0123),
                            (dict(base, cost=0.001, is_byok=True, cost_details={"upstream_inference_cost": 0.02}),
                             0.021),
                            (dict(base, cost=0.001, is_byok=True), 0.045),      # BYOK without the upstream cost
                            (dict(base, cost=-1), 0.045), (dict(base, cost=True), 0.045),
                            (dict(base, cost="0.1"), 0.045),
                            (dict(base), 0.045)):                              # no cost: the configured prices
            self.fresh_dir()
            c, _ = self.orclient([(200, completion("{}", usage=usage))])
            c.chat([{"role": "user", "content": "u"}])
            self.assertAlmostEqual(self.usage_log()[0]["usd"], want, places=9, msg=usage)
        # Moonshot never trusts a "cost" in its usage: its records are priced with the configured prices
        self.fresh_dir()
        c, _ = self.client([(200, completion("{}", usage=dict(base, cost=9.0)))])
        c.chat([{"role": "user", "content": "u"}])
        rec = self.usage_log()[0]
        self.assertAlmostEqual(rec["usd"], 0.045, places=9)
        self.assertNotIn("provider", rec)

    def fresh_dir(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir = tempfile.mkdtemp(prefix="llmtest_")


if __name__ == "__main__":
    unittest.main()
