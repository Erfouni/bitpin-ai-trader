"""v3.6.2: the news reply forced into its JSON object by the API's JSON mode (response_format json_object) after
the first search and on every request without the tool, with a fallback when the API refuses it; a stream the
tunnel cut after the reply had arrived is used instead of paying for it again."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bitpin import news as news_mod  # noqa: E402
from bitpin.news import JSON_MODE, SALVAGE_CUT_MIN_ITEMS, partial_stream_reply  # noqa: E402
import test_news as tn  # noqa: E402  (the fixtures: fake transport, clock, reply builders)

T0 = tn.T0
PROSE = "I searched the news. The Fed held rates this week and ETF inflows rose. Let me know if you need more."


def items_json(n):
    return json.dumps({"items": [{"headline": "Event number %d happened" % i, "why_it_matters": "Moves crypto",
                                  "source_url": "https://www.reuters.com/markets/e%d/" % i,
                                  "time_hint": "2026-09-2%d" % (i % 9)} for i in range(n)],
                       "summary": "Mixed."})


class TestJsonMode(tn.NewsTestBase):
    def test_json_mode_after_the_first_search_and_on_requests_without_the_tool(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)), (200, tn.completion(PROSE)),
                                 (200, tn.completion(PROSE)), (200, tn.completion(tn.GOOD))])
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        first, second, third, fourth = (q["body"] for q in tr.requests)
        self.assertNotIn("response_format", first)                   # the first request searches freely
        self.assertIn("tools", first)
        self.assertEqual(second["response_format"], JSON_MODE)       # after the search: the answer is the JSON
        self.assertIn("tools", second)                               # ... and it may still search
        self.assertEqual(third["response_format"], JSON_MODE)        # v3.6.4: the search nudge, the tool offered
        self.assertIn("tools", third)
        self.assertEqual(fourth["response_format"], JSON_MODE)       # the format retry, without the tool
        self.assertNotIn("tools", fourth)

    def test_a_refusal_with_the_tool_keeps_json_mode_for_the_requests_without_it(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)),
                                 (400, {"error": {"message": "response_format json_object is not supported with tools"}}),
                                 (200, tn.completion(PROSE)), (200, tn.completion(PROSE)),
                                 (200, tn.completion(tn.GOOD))])
        with self.assertLogs("bitpin.news", level="WARNING") as cm:
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        bodies = [q["body"] for q in tr.requests]
        self.assertIn("response_format", bodies[1])
        self.assertNotIn("response_format", bodies[2])               # the same request again, the tool offered
        self.assertIn("tools", bodies[2])
        self.assertNotIn("response_format", bodies[3])               # the search nudge: the tool, no JSON mode
        self.assertIn("tools", bodies[3])
        self.assertEqual(bodies[4]["response_format"], JSON_MODE)    # the format retry still gets it
        self.assertNotIn("tools", bodies[4])
        self.assertIn("refused JSON mode together with the search tool", "\n".join(cm.output))
        self.assertEqual((r._json_mode, r._json_with_tools), (True, False))

    def test_a_refusal_without_the_tool_turns_json_mode_off(self):
        r, tr = self.researcher([tn.tool_round(tn.call(1, tn.ARGS1)),
                                 (400, {"error": {"message": "unknown field response_format"}}),
                                 (200, tn.completion(tn.GOOD))], max_tool_rounds=1)
        with self.assertLogs("bitpin.news", level="WARNING"):
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(tr.requests[1]["body"]["response_format"], JSON_MODE)
        self.assertNotIn("response_format", tr.requests[2]["body"])
        self.assertFalse(r._json_mode)
        tr.script += [tn.tool_round(tn.call(2, tn.ARGS2)), (200, tn.completion(tn.GOOD))]
        r.research(now=T0 + 26 * 3600, force=True)                   # remembered: no JSON mode at all
        self.assertTrue(all("response_format" not in q["body"] for q in tr.requests[3:]))


class TestCutStreams(tn.NewsTestBase):
    def cut(self, text, keep):
        """An SSE body of `text` in chunks of 40 characters, cut after `keep` characters (no finish, no [DONE])."""
        pieces = [text[i:i + 40] for i in range(0, keep, 40)]
        return tn.sse(*[tn.sse_chunk(content=p) for p in pieces], done=False)

    def test_complete_items_of_a_cut_json_are_used_when_there_are_enough(self):
        text = items_json(5)
        keep = text.index('{"headline": "Event number %d' % SALVAGE_CUT_MIN_ITEMS) + 10   # N complete items, then a cut
        r, tr = self.researcher([(200, self.cut(text, keep))])
        with self.assertLogs("bitpin.news", level="WARNING") as cm:
            b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(tr.requests), 1)
        self.assertEqual(len(b.items), SALVAGE_CUT_MIN_ITEMS)
        self.assertIn("%d complete items" % SALVAGE_CUT_MIN_ITEMS, "\n".join(cm.output))

    def test_too_few_complete_items_are_asked_for_again(self):
        text = items_json(5)
        keep = text.index('{"headline": "Event number %d' % (SALVAGE_CUT_MIN_ITEMS - 1)) + 10
        r, tr = self.researcher([(200, self.cut(text, keep)), (200, tn.completion(tn.GOOD))])
        b = r.research(now=T0)
        self.assertTrue(b.ok, b.error)
        self.assertEqual(len(tr.requests), 2)
        self.assertEqual(len(b.items), 2)                            # GOOD, the answer of the second request

    def test_a_cut_tool_round_is_never_used(self):
        body = tn.sse(tn.sse_chunk(tool_calls=[{"index": 0, "id": "call_1", "type": "builtin_function",
                                                "function": {"name": "$web_search", "arguments": tn.ARGS1[:20]}}]),
                      done=False)
        r, tr = self.researcher([(200, body), (200, tn.completion(tn.GOOD))])
        self.assertTrue(r.research(now=T0).ok)
        self.assertEqual(len(tr.requests), 2)


class TestPartialStreamReply(unittest.TestCase):
    def test_the_readable_part_of_a_cut_stream(self):
        raw = tn.sse(tn.sse_chunk(reasoning="thinking"), tn.sse_chunk(content='{"items": ['),
                     tn.sse_chunk(content='], "summary": "x"}'), done=False) + b'data: {"choices": [{"del'
        part = partial_stream_reply(raw)
        self.assertEqual(part["content"], '{"items": [], "summary": "x"}')
        self.assertEqual((part["chunks"], part["reasoning_chars"], part["tool_calls"]), (3, 8, False))
        self.assertIsNone(partial_stream_reply(b'{"not": "a stream"}'))
        self.assertIsNone(partial_stream_reply(tn.sse({"error": {"message": "overloaded"}}, done=False)))
        tool = partial_stream_reply(tn.sse(tn.sse_chunk(tool_calls=[{"index": 0}]), done=False))
        self.assertTrue(tool["tool_calls"])
        broken = partial_stream_reply(tn.sse(tn.sse_chunk(content="a"), "not json", tn.sse_chunk(content="b"),
                                             done=False))
        self.assertEqual(broken["content"], "a")                     # nothing after an unreadable event

    def test_the_json_mode_constant(self):
        self.assertEqual(news_mod.JSON_MODE, {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
