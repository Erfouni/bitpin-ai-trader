"""v3.13: the fixes from the external architecture review of 2026-10-07 - a failed call no longer uses up a broken
plan's or a max hold's wake-up, the second run counts in a decision's LLM reserve, the runner's pump re-check is
fail-closed, a damaged order time no longer stops trading, a code exit's sale on its toman market gets a wider price
sanity, the candle cache is written atomically and gaps no longer skew the period or the sigma, the two-step login
step survives a panel restart, and the news feeds' health is recorded, alerted and shown. No network."""
import ast
import glob
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import test_brain as tb  # noqa: E402
from test_hold_discipline import btc_position  # noqa: E402
from bitpin import data as data_mod  # noqa: E402
from bitpin import news_feeds as nf  # noqa: E402
from bitpin.analysis import hourly_log_returns  # noqa: E402
from bitpin.brain import Decision  # noqa: E402
from bitpin.data import Bar, resample  # noqa: E402
from bitpin.markets import D, Market, parse_book  # noqa: E402
from bitpin.risk import RiskManager  # noqa: E402
from bitpin.runner import EXIT_PRICE_DEVIATION, PlannedOrder, Runner  # noqa: E402

logging.getLogger("bitpin").addHandler(logging.NullHandler())

H = 3600
DAY = 86400
NOW = tb.T0
U1, U2 = "https://example.com/rss", "https://news.example.org/feed"


class TestWakeUpEvents(tb.ClockBase):
    def test_a_failed_call_leaves_a_broken_plan_or_a_max_hold_pending(self):
        b = self.cbrain([])
        evs = [{"kind": "invalidation", "symbol": "BTC_IRT", "key": "BTC_IRT@inv1"},
               {"kind": "max_hold", "symbol": "ETH_IRT", "key": "ETH_IRT@5"}]
        b._consume_events(Decision(valid=False, events=evs))
        self.assertEqual(b._max_hold_seen, [])
        b._consume_events(Decision(valid=True, events=evs))
        self.assertEqual(b._max_hold_seen, ["BTC_IRT@inv1", "ETH_IRT@5"])

    def test_the_broken_plan_wakes_kimi_again_after_a_failed_call(self):
        b = self.cbrain([])
        pos = btc_position(broken_at=NOW - H, broken_close_usdt=75000.0)
        ctx = tb.context(weights={"USDT_IRT": 0.35, "BTC_IRT": 0.65})
        kinds = lambda: [e["kind"] for e in b._held_events(NOW, Decision(valid=True), ctx, pos)]  # noqa: E731
        self.assertIn("invalidation", kinds())
        b._consume_events(Decision(valid=False, events=[e for e in b._held_events(NOW, Decision(valid=True), ctx, pos)]))
        self.assertIn("invalidation", kinds())                         # still pending
        b._consume_events(Decision(valid=True, events=[e for e in b._held_events(NOW, Decision(valid=True), ctx, pos)]))
        self.assertNotIn("invalidation", kinds())                      # answered once


class TestPacingReserve(tb.ClockBase):
    def test_the_second_run_counts_in_a_buying_decisions_worst_case(self):
        b = self.cbrain([], confirm_buys=True)
        b.llm.budget = tb.FakeBudget(5)
        self.assertIn("a decision may take 3", b.pacing_problem(NOW, "scheduled", mode="scheduled"))
        b.llm.budget = tb.FakeBudget(6)
        self.assertIsNone(b.pacing_problem(NOW, "scheduled", mode="scheduled"))
        b = self.cbrain([], confirm_buys=False)
        b.llm.budget = tb.FakeBudget(5)
        self.assertIsNone(b.pacing_problem(NOW, "scheduled", mode="scheduled"))


class TestPumpFailClosed(unittest.TestCase):
    def stub(self, guarded):
        st = types.SimpleNamespace(brain=None, events=[])
        st._pump_guarded = guarded
        st._event = lambda kind, **k: st.events.append((kind, k))
        return st

    def test_a_failed_recheck_buys_nothing_and_sells_freely(self):
        def boom(now, failed=None):
            raise RuntimeError("candles broken")
        st, info = self.stub(boom), {}
        t = Runner._pump_clamp(st, {"USDT_IRT": D("0.6"), "BTC_IRT": D("0.3"), "ETH_IRT": D("0.1")},
                               {"USDT_IRT": D("0.5"), "ETH_IRT": D("0.5")}, NOW, info)
        self.assertEqual((t["BTC_IRT"], t["ETH_IRT"], t["USDT_IRT"]), (D("0"), D("0.1"), D("0.9")))
        self.assertEqual(info["pump_guard"], ["BTC_IRT"])
        self.assertIn("pump-guard re-check (the re-check of its candles failed): kept at 0.0000 instead of 0.3000",
                      info["adjustments"][0])
        self.assertEqual(st.events[0][0], "pump_guard")

    def test_one_coins_failed_check_holds_back_only_that_coin(self):
        def one(now, failed=None):
            failed.add("BTC_IRT")
            return {}
        info = {}
        t = Runner._pump_clamp(self.stub(one), {"USDT_IRT": D("0.4"), "BTC_IRT": D("0.3"), "ETH_IRT": D("0.3")},
                               {"USDT_IRT": D("1.0")}, NOW, info)
        self.assertEqual((t["BTC_IRT"], t["ETH_IRT"], t["USDT_IRT"]), (D("0"), D("0.3"), D("0.7")))


class TestRiskState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_review_risk_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rm(self):
        return RiskManager({"min_order_irt": 1000}, self.dir, "paper", clock=lambda: NOW)

    def test_a_damaged_order_time_does_not_stop_trading(self):
        with open(os.path.join(self.dir, "risk_state_paper.json"), "w") as f:
            json.dump({"orders": [NOW - 100, "garbage", None, [1], float("nan"), NOW - 200], "limit_orders": "x"}, f)
        rm = self.rm()
        with self.assertLogs("bitpin", "WARNING"):
            self.assertEqual(rm.orders_last_24h(), 2)
        self.assertEqual(rm.orders_last_24h("limit"), 0)
        self.assertTrue(rm.can_place_order())
        rm.record_order()
        self.assertEqual(rm.orders_last_24h(), 3)

    def test_a_code_exit_may_sell_further_from_the_close_but_not_into_a_broken_quote(self):
        m = Market("AAA_IRT", "AAA", "IRT", True, False, 0, 4, 0)
        book = parse_book({"asks": [["100", "50"], ["101", "50"]], "bids": [["99", "50"], ["98", "50"]]})
        rm = self.rm()
        v = rm.vet_order("sell", m, D(20), book, D(111), D(10 ** 9))                       # mid 99.5: -10%
        self.assertFalse(v.ok)
        self.assertIn("(limit 5.0%)", v.reason)
        v = rm.vet_order("sell", m, D(20), book, D(111), D(10 ** 9), price_deviation=EXIT_PRICE_DEVIATION)
        self.assertTrue(v.ok, v.reason)
        v = rm.vet_order("sell", m, D(20), book, D(150), D(10 ** 9), price_deviation=0.25)  # -34%: a broken quote
        self.assertIn("(limit 25.0%)", v.reason)
        v = rm.vet_order("sell", m, D(20), book, D(111), D(10 ** 9), price_deviation=0.01)  # never narrower
        self.assertIn("(limit 5.0%)", v.reason)


class TestExitSaleSanity(unittest.TestCase):
    def test_only_a_code_exits_toman_sale_gets_the_wider_limit(self):
        seen = []
        st = types.SimpleNamespace(broker=types.SimpleNamespace(prepare=lambda: None), _transient_skip=False)

        def vet(side, sym, amount, lc, eq, flatten, price_deviation=None):
            seen.append(price_deviation)
            return types.SimpleNamespace(ok=False, reason="test", notes=[]), None
        st._vet = vet
        rep = {"skipped": [], "errors": [], "fills": []}
        for sym, side, route, reason in (("BTC_IRT", "sell", "via_irt", "stop"), ("BTC_IRT", "sell", "via_irt", "target"),
                                         ("BTC_USDT", "sell", "direct", "stop"), ("BTC_IRT", "sell", None, "allocation"),
                                         ("USDT_IRT", "buy", "via_irt", "stop")):
            o = PlannedOrder(sym, side, D(1), D(1), D(0), D(0), True, route=route, reason=reason, serves="BTC_IRT")
            self.assertTrue(Runner._place(st, o, side, o.amount, {}, D(1), rep))
        self.assertEqual(seen, [EXIT_PRICE_DEVIATION, EXIT_PRICE_DEVIATION, None, None, None])


class TestCandles(unittest.TestCase):
    START = (NOW // (4 * H)) * 4 * H

    def bars(self, hours):
        return [Bar(int(self.START + h * H), 1.0 + h, 1.0 + h, 1.0 + h, 1.0 + h, 1.0) for h in hours]

    def test_a_missing_first_bar_no_longer_breaks_the_period(self):
        full = self.bars(range(24))
        holed = [b for b in full if b.ts != full[1].ts]
        self.assertEqual([b.ts for b in resample(holed, 4)], [b.ts for b in resample(full, 4)][1:])
        self.assertEqual(resample(full[:1], 4), [])

    def test_a_gap_counts_like_one_hourly_return(self):
        s = [(0, 100.0), (H, 101.0), (4 * H, 105.0)]
        r = hourly_log_returns(s, 10)
        self.assertAlmostEqual(r[0], math.log(1.01))
        self.assertAlmostEqual(r[1], math.log(105.0 / 101.0) / math.sqrt(3.0))
        healthy = [(i * H, 100.0 + i) for i in range(30)]
        self.assertEqual(hourly_log_returns(healthy, 24),
                         [math.log(b / a) for a, b in zip([c for _, c in healthy][-25:], [c for _, c in healthy][-24:])])

    def test_the_cache_is_written_atomically_and_bad_rows_are_skipped(self):
        d = tempfile.mkdtemp(prefix="bitpin_review_csv_")
        try:
            with mock.patch.object(data_mod, "DATA_DIR", d):
                good = self.bars(range(3))
                bad = [Bar(good[-1].ts + H, float("nan"), 1.0, 1.0, 1.0, 1.0), Bar(good[-1].ts + 2 * H, 0.0, 0.0, 0.0, 0.0, 1.0)]
                data_mod.save_csv(good + bad, "AAA_IRT")
                self.assertEqual(os.listdir(d), ["AAA_IRT_60.csv"])                     # no temporary file left
                self.assertEqual(data_mod.load_csv("AAA_IRT"), good)
                with open(os.path.join(d, "AAA_IRT_60.csv"), "a") as f:
                    f.write("123,abc,1,1,1,1\n")
                with self.assertLogs("bitpin", "WARNING"):
                    self.assertEqual(data_mod.load_csv("AAA_IRT"), good)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_broken_bar_from_the_api_is_dropped(self):
        rows = [{"ts": 2 * H, "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"},
                {"ts": 3 * H, "open": "NaN", "high": "1", "low": "1", "close": "1", "volume": "1"}]
        with mock.patch.object(data_mod, "http_get_json", side_effect=[rows, []]), \
                mock.patch.object(data_mod.time, "sleep"):
            with self.assertLogs("bitpin", "WARNING"):
                out = data_mod.fetch_bars("AAA_IRT", "60", H, 4 * H)
        self.assertEqual([b.ts for b in out], [2 * H])


class TestTotpStep(unittest.TestCase):
    SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    def test_the_last_step_survives_a_restart_and_is_tied_to_the_secret(self):
        from bitpin import panel_web as pw
        d = tempfile.mkdtemp(prefix="bitpin_review_totp_")
        try:
            cfg = {"totp_secret": self.SECRET, "audit_log": os.path.join(d, "audit.jsonl")}
            app = pw.PanelApp(cfg, helper=None)
            self.assertIsNone(app._totp_last)
            app._remember_step(self.SECRET, 1234)
            self.assertEqual(pw.PanelApp(cfg, helper=None)._totp_last, 1234)          # a restart remembers it
            pw.PanelApp(cfg, helper=None)._remember_step(self.SECRET, 1200)           # never backwards
            self.assertEqual(pw.PanelApp(cfg, helper=None)._totp_last, 1234)
            self.assertIsNone(pw.PanelApp(dict(cfg, totp_secret="KRSXG5CTMVRXEZLUKRSXG5CTMVRXEZLU"),
                                          helper=None)._totp_last)                   # another secret starts over
            if os.name == "posix":
                self.assertEqual(os.stat(app._totp_path).st_mode & 0o777, 0o600)
            with open(app._totp_path, encoding="utf-8") as f:
                self.assertNotIn(self.SECRET, f.read())
            pw.save_totp_last(app._totp_path, None, None)
            self.assertFalse(os.path.exists(app._totp_path))
        finally:
            shutil.rmtree(d, ignore_errors=True)


def feed_result(url, ok=True, entries=(), error=None, site="example.com"):
    return nf.FeedResult(nf.Feed(site, url, "direct"), ok, 200 if ok else None, error, list(entries), 0.1, "direct")


class TestFeedHealth(unittest.TestCase):
    T = 1790467200 + H                                               # 2026-09-27 01:00 UTC

    def test_bad_days_count_utc_days_without_a_usable_read(self):
        t = self.T
        h = nf.feed_health(None, [feed_result(U1, ok=False, error="HTTP 503")], t)
        self.assertEqual(h[U1]["bad_days"], 1)
        h = nf.feed_health(h, [feed_result(U1, ok=False, error="HTTP 503")], t + H)       # the same day
        self.assertEqual(h[U1]["bad_days"], 1)
        h = nf.feed_health(h, [feed_result(U1, ok=False, error="timeout")], t + DAY)
        self.assertEqual((h[U1]["bad_days"], h[U1]["error"]), (2, "timeout"))
        h = nf.feed_health(h, [feed_result(U1, entries=[("t", "l", t + DAY, "")])], t + DAY + 60)
        self.assertEqual((h[U1]["bad_days"], h[U1]["error"]), (0, None))
        h = nf.feed_health(h, [feed_result(U1, ok=False, error="x")], t + DAY + 120)      # the day stays good
        self.assertEqual(h[U1]["bad_days"], 0)
        h = nf.feed_health(h, [feed_result(U1, entries=[("t", "l", t - 30 * DAY, "")])], t + 2 * DAY)    # stale
        self.assertEqual(h[U1]["bad_days"], 1)
        self.assertEqual(h[U1]["error"], "no entry of the last 7 days")
        self.assertEqual(list(nf.feed_health(h, [feed_result(U2)], t)), [U2])               # no longer read: dropped

    def test_the_research_writes_the_file(self):
        from bitpin import news
        d = tempfile.mkdtemp(prefix="bitpin_review_feeds_")
        try:
            news.NewsResearcher._note_feed_health(types.SimpleNamespace(state_dir=d),
                                                  [feed_result(U1, ok=False, error="HTTP 404")], self.T)
            with open(os.path.join(d, nf.FEED_HEALTH_FILE), encoding="utf-8") as f:
                doc = json.load(f)
            self.assertEqual((doc["alert_days"], doc["feeds"][U1]["bad_days"]), (nf.FEED_ALERT_DAYS, 1))
            news.NewsResearcher._note_feed_health(types.SimpleNamespace(state_dir=None), [], self.T)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_owner_is_told_once_then_when_it_is_back_and_the_panel_lists_it(self):
        from bitpin import notify as nt
        from bitpin import panel_web as pw
        from bitpin.panel_i18n import use
        bad = {"alert_days": 3, "feeds": {U1: {"site": "example.com", "bad_days": 3, "error": "HTTP 404"},
                                          U2: {"site": "example.org", "bad_days": 0}, "javascript:x": {"bad_days": 9}}}
        v = nt.feed_health_view(bad)
        self.assertEqual(([x["url"] for x in v["bad"]], v["feeds"], v["alert_days"]), ([U1], 3, 3))
        self.assertIsNone(nt.feed_health_view({"feeds": {}}))
        st = types.SimpleNamespace(flags={}, sent=[], doc=bad)
        st._read_json = lambda key: st.doc if key == "feed_health" else None
        st._flag = lambda name: st.flags.setdefault(name, {})
        st.enqueue = lambda key, kind, text: st.sent.append((key, kind, text))
        nt.Notifier._alert_feeds(st, self.T)
        self.assertEqual([k for _, k, _ in st.sent], ["alert"])
        self.assertIn("example[.]com", st.sent[0][2])                         # defanged like every notifier text
        nt.Notifier._alert_feeds(st, self.T + H)
        self.assertEqual(len(st.sent), 1)                                      # told once
        st.doc = {"alert_days": 3, "feeds": {U1: {"site": "example.com", "bad_days": 0}}}
        nt.Notifier._alert_feeds(st, self.T + 2 * H)
        self.assertEqual(st.sent[-1][1], "recovery")
        st.doc = bad
        nt.Notifier._alert_feeds(st, self.T + DAY)
        self.assertEqual(len(st.sent), 3)
        st.doc = {"alert_days": 3, "feeds": {U2: {"site": "example.org", "bad_days": 0}}}   # the owner took it out
        nt.Notifier._alert_feeds(st, self.T + DAY + H)
        self.assertEqual((len(st.sent), st.flags["feeds"]["alerted"]), (3, {}))
        with use("en"):
            card = pw.PanelApp._news_card(None, {"fetched_at": self.T, "mode": "feeds", "items": [], "feed_health": v})
        text = re.sub(r"<[^>]+>", "", card)
        self.assertIn("Sources with problems: 1 (1 of them for 3 days or more)", text)
        self.assertIn("example[.]com 3 day(s) without a usable read HTTP 404", text)


class TestNoStrayTestFunctions(unittest.TestCase):
    def test_no_module_level_function_is_named_like_a_test(self):
        """pytest collects a module-level test_* / tests_* function as a test (tests_naming took a parameter: a
        collection error); the suite runs with unittest, but it must stay collectable either way."""
        bad = []
        for p in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
            with open(p, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            bad += ["%s:%s" % (os.path.basename(p), n.name) for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
