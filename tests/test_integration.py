"""Kimi brain <-> runner <-> CLI <-> deploy integration (no network, no real credentials).

* Runner brain mode with a contract double of KimiBrain (runner logic only), and end to end with the
  REAL KimiBrain + LLMClient (fake HTTP transport) + MarketContextBuilder (fake Bitpin data) in paper
  mode: valid decision -> orders; invalid reply -> no trade; LLM down -> cash sweep only; >12 h without
  a valid decision -> derisk; expired decision -> not executed.
* Network isolation: Bitpin traffic (BitpinClient, data.py) ignores http_proxy / https_proxy / ALL_PROXY;
  Kimi traffic uses KIMI_HTTPS_PROXY only (local HTTP servers on 127.0.0.1).
* CLI: live --non-interactive gate, confirm-live, kimi-check diagnostics, the paper-only test hook.
* Deploy kit: the exact systemd ExecStart lines and the KIMI_HTTPS_PROXY env template.
"""
import contextlib
import http.server
import importlib.util
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin.api import TransportError, atomic_write_json, read_json  # noqa: E402
from bitpin.brain import KimiBrain, build_kimi  # noqa: E402
from bitpin.broker import PaperBroker  # noqa: E402
from bitpin.data import Bar  # noqa: E402
from bitpin.markets import ZERO, D, Market, MarketCache  # noqa: E402
from bitpin.news import NewsBrief, NewsResearcher  # noqa: E402
from bitpin.risk import KILL_SWITCH_FILE, RiskManager  # noqa: E402
from bitpin.runner import (BRAIN_RUNNER_LOG, BRAIN_SCHEDULE_SLACK, MIN_ORDER_HEADROOM, Runner,  # noqa: E402
                           RunnerError, format_report)

logging.getLogger("bitpin").addHandler(logging.NullHandler())

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "sk-INTEGRATIONTESTKEY0123456789"
LAST_CLOSED = 1790065800          # a real Bitpin 1h bar open (hh:30 UTC)
NOW = LAST_CLOSED + 3600 + 90     # 90 s after that bar closed
SYMS = ["USDT_IRT", "BTC_IRT", "ETH_IRT"]
PX = {"USDT_IRT": 100000.0, "BTC_IRT": 100000.0 * 80000, "ETH_IRT": 100000.0 * 3000}
CAPITAL = "1000000000"            # 1e9 toman
MARKETS = MarketCache(markets=[
    Market("USDT_IRT", "USDT", "IRT", True, False, 0, 2, 0),
    Market("BTC_IRT", "BTC", "IRT", True, False, 0, 8, 0),
    Market("ETH_IRT", "ETH", "IRT", True, False, 0, 6, 0),
])
HAS_FALLBACK = hasattr(KimiBrain, "fallback_decision")
NEED_FALLBACK = "KimiBrain.fallback_decision (bitpin/brain.py) is not implemented yet"


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Exchange(object):
    """Hourly candles (plus the still-forming bar), order books and tickers that follow the price."""

    def __init__(self, n=900):
        self.prices = dict(PX)
        self.series = {s: [] for s in SYMS}
        start = LAST_CLOSED - (n - 1) * 3600
        for i in range(n):
            self.add_bar(start + i * 3600)

    def add_bar(self, ts, prices=None):
        for s in self.series:
            p = (prices or {}).get(s, self.prices[s])
            self.prices[s] = p
            self.series[s].append(Bar(ts, p, p * 1.001, p * 0.999, p, 10.0))

    def bars(self, symbol, res, start, end):
        out = [b for b in self.series[symbol] if start <= b.ts <= end]
        forming = self.series[symbol][-1].ts + 3600
        if forming <= end:
            p = self.prices[symbol]
            out.append(Bar(forming, p, p, p, p, 1.0))
        return out

    def book(self, symbol):
        p = self.prices[symbol]
        size = {"USDT_IRT": "100000000", "BTC_IRT": "1000", "ETH_IRT": "100000"}[symbol]
        return {"asks": [[repr(p * (1 + 0.0005 * k)), size] for k in range(1, 6)],
                "bids": [[repr(p * (1 - 0.0005 * k)), size] for k in range(1, 6)]}

    # the public client the context builder uses
    def tickers(self):
        return [{"symbol": s, "price": repr(p)} for s, p in self.prices.items()]

    def orderbook(self, symbol):
        return self.book(symbol)

    def matches(self, symbol):
        return []


def reply(targets, conf=0.8, cash=0.0, review=2, plans="auto"):
    """A canned decision. plans "auto": an entry plan for every coin it holds (a NEW position needs one
    with the runner's code exits: brain.require_plan), invalidation well below the start price."""
    d = {"targets": targets, "cash_irt": cash, "confidence": conf, "reasoning": "integration test",
         "news_summary": "none", "key_risks": "none", "next_review_hours": review}
    if plans == "auto":
        plans = {s: {"setup": "trend_continuation", "horizon_hours": 72,
                     "invalidation_usdt": 0.6 * PX[s] / PX["USDT_IRT"], "note": "integration test thesis"}
                 for s, w in targets.items() if s != "USDT_IRT" and w > 0 and s in PX}
    if plans:
        d["plans"] = plans
    return d


class FakeKimiTransport(object):
    """HTTP transport double for LLMClient: canned chat replies, an outage switch, a clock hook."""

    def __init__(self, clock=None):
        self.replies = []
        self.default = None
        self.down = False
        self.calls = []
        self.bodies = []
        self.clock = clock
        self.advance = 0

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        self.bodies.append(json.loads(body.decode("utf-8")) if body else None)
        assert headers.get("Authorization") == "Bearer " + KEY
        if self.down:
            raise TransportError("network error: [test] Moonshot unreachable")
        if url.endswith("/models"):
            return 200, json.dumps({"data": [{"id": "kimi-test"}]}).encode("utf-8")
        r = self.replies.pop(0) if self.replies else self.default
        if self.advance and self.clock is not None:
            self.clock.t += self.advance            # the call took this long
        content = r if isinstance(r, str) else json.dumps(r)
        return 200, json.dumps({"model": "kimi-test", "usage": {"total_tokens": 1000},
                                "choices": [{"finish_reason": "stop",
                                             "message": {"role": "assistant", "content": content}}]}).encode("utf-8")

    @property
    def chats(self):
        return [c for c in self.calls if c[1].endswith("/chat/completions")]


def write_paper_account(state_dir, balances):
    atomic_write_json(os.path.join(state_dir, PaperBroker.STATE_FILE), {
        "version": 1, "mode": "paper", "created_utc": "2026-09-22T00:00:00Z", "updated_utc": "2026-09-22T00:00:00Z",
        "initial_capital_irt": D(CAPITAL), "seq": 0, "orders": {},
        "balances": {k: D(v) for k, v in balances.items()}})


def weights(broker, prices):
    bal = broker.balances()
    eq = bal.get("IRT", ZERO) + sum(bal.get(s.split("_")[0], ZERO) * D(repr(prices[s])) for s in SYMS)
    w = {s: float(bal.get(s.split("_")[0], ZERO) * D(repr(prices[s])) / eq) for s in SYMS}
    w["IRT"] = float(bal.get("IRT", ZERO) / eq)
    return w


def read_dir(path):
    out = {}
    for name in sorted(os.listdir(path)):
        with open(os.path.join(path, name), "rb") as f:
            out[name] = f.read()
    return out


def runner_log(state_dir):
    p = os.path.join(state_dir, BRAIN_RUNNER_LOG)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


RUNNER_CFG = {"history_csv_seed": False, "history_start_ts": None, "wake_delay_seconds": 0, "data_retry_seconds": 1,
              "error_backoff_seconds": 5}


# --------------------------------------------------------------------------- contract double of KimiBrain

class FakeDecision(object):
    def __init__(self, valid=True, targets=None, decided_at=NOW, hold=False, expires_at=None, computed_against=None,
                 fallback=False, fallback_reason=None, error="", error_kind="", origin=None, computed_cash=0.0):
        self.valid, self.targets, self.decided_at, self.hold = valid, dict(targets or {}), decided_at, hold
        self.expires_at = expires_at if expires_at is not None else decided_at + 3600
        self.computed_against = computed_against
        self.computed_cash = computed_cash
        self.origin = origin              # None = "this runner's own" (filled in by FakeBrain.decide)
        self.fallback, self.fallback_reason = fallback, fallback_reason
        self.error, self.error_kind, self.confidence = error, error_kind, 0.8


class FakeBrain(object):
    """Implements the KimiBrain surface the runner uses, with the documented fallback contract."""

    def __init__(self, clock, due=True):
        self.allowed = list(SYMS)
        self.limits = {"max_irt_cash": 0.05}
        self.cfg = {"risk_profile": "balanced", "decision_interval_hours": 2, "fallback": {"derisk_after_hours": 12}}
        self.clock = clock
        self.due = due
        self.next = []
        self.last_decision = None
        self.last_trigger = None
        self.decide_calls = []
        self.fallback_calls = []
        self.fallback_values = None
        self.should_calls = 0
        self.should_slack = []
        self.last_valid_at = None
        self.raise_in_decide = None
        self.pacing_block = None
        self.fallback_mtw = []
        self.origin = ""                  # like a KimiBrain built without one: the origin guard is off
        self.order_budgets = []

    def should_decide(self, now, last, quick, slack_seconds=0.0):
        self.should_calls += 1
        self.should_slack.append(slack_seconds)
        self.last_trigger = "test" if self.due else None
        return self.due

    def recent_decisions(self):
        return []

    def decide(self, ctx, cur, trigger=None, abort=None, min_trade_weight=None, order_budget=None, now=None):
        self.decide_calls.append({"ctx": ctx, "cur": dict(cur), "abort": abort, "trigger": trigger,
                                  "min_trade_weight": min_trade_weight, "order_budget": order_budget, "now": now})
        self.order_budgets.append(order_budget)
        if self.raise_in_decide:
            raise self.raise_in_decide
        d = self.next.pop(0)
        d.decided_at = self.clock()
        if d.expires_at is None or d.expires_at < d.decided_at - 1e6:
            d.expires_at = d.decided_at + 3600
        if d.computed_against is None:
            d.computed_against = dict(cur)
        if d.origin is None:
            d.origin = self.origin
        self.last_decision = d
        if d.valid:
            self.last_valid_at = d.decided_at
        return d

    def fallback_decision(self, current_weights, now, balances_irt_value=None, min_trade_weight=None):
        self.fallback_calls.append(dict(current_weights))
        self.fallback_values = balances_irt_value
        self.fallback_mtw.append(min_trade_weight)
        cash = 1.0 - sum(current_weights.values())
        if balances_irt_value:                      # the contract: the cash weight from the IRT values
            cash = balances_irt_value.get("IRT", 0.0) / sum(balances_irt_value.values())
        lim = self.limits["max_irt_cash"]
        if self.last_valid_at is not None and now - self.last_valid_at > 12 * 3600:
            t = {s: 0.0 for s in self.allowed}
            t["USDT_IRT"] = 1.0 - lim
            return FakeDecision(True, t, now, fallback=True, fallback_reason="derisk")
        if cash > lim:
            t = {s: current_weights.get(s, 0.0) for s in self.allowed}
            t["USDT_IRT"] = t.get("USDT_IRT", 0.0) + cash - lim
            return FakeDecision(True, t, now, fallback=True, fallback_reason="cash_sweep")
        return None


class FakeBuilder(object):
    def __init__(self):
        self.snaps = []
        self.build_aborts = []

    def quick_context(self, snap):
        self.snaps.append(snap)
        return {"quick": True}

    def build(self, snap, recent_decisions=None, abort=None):
        self.build_aborts.append(abort)
        return {"portfolio": snap}


class BrainRunnerBase(unittest.TestCase):
    """Fixture for the runner's brain mode: fake brain + builder, paper broker on the fake exchange.
    Carries no test of its own, so a subclass adds cases instead of re-running every one of them."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_int_runner_")
        self.ex = Exchange()
        self.clock = Clock()
        self.brain = FakeBrain(self.clock)
        self.builder = FakeBuilder()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def runner(self, dry_run=False, cfg=None, risk=None):
        broker = PaperBroker(MARKETS, self.dir, capital_irt=CAPITAL, book_source=self.ex.book, clock=self.clock,
                             persist=not dry_run)
        rm = RiskManager(dict({"min_order_irt": 100000}, **(risk or {})), self.dir, "paper", clock=self.clock,
                         persist=not dry_run)
        c = dict(RUNNER_CFG)
        c.update(cfg or {})
        return Runner(None, broker, rm, c, self.dir, "paper", dry_run=dry_run, bars_source=self.ex.bars,
                      clock=self.clock, sleep=self.clock.sleep, brain=self.brain, context_builder=self.builder)

    def next_bar(self, hours=1):
        for _ in range(hours):
            self.ex.add_bar(self.ex.series["USDT_IRT"][-1].ts + 3600)
            self.clock.t += 3600


class RunnerBrainTest(BrainRunnerBase):
    """Runner brain-mode logic against the documented KimiBrain contract (no LLM involved)."""

    def test_valid_decision_executes_once_through_the_normal_execution_path(self):
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.8, "BTC_IRT": 0.2, "ETH_IRT": 0.0})]
        r = self.runner()
        rep = r.run_once()
        self.assertEqual((rep["status"], rep["brain"]["action"]), ("ok", "kimi"))
        self.assertEqual(sorted((f["side"], f["symbol"]) for f in rep["fills"]),
                         [("buy", "BTC_IRT"), ("buy", "USDT_IRT")])
        w = weights(r.broker, self.ex.prices)
        self.assertAlmostEqual(w["USDT_IRT"], 0.8, delta=0.01)
        self.assertAlmostEqual(w["BTC_IRT"], 0.2, delta=0.01)
        self.assertEqual(self.brain.decide_calls[0]["cur"], {})          # all toman: no coin weights
        # same bar: nothing; next bar: the same (not expired) decision is NOT executed again
        self.assertEqual(r.run_once()["status"], "already_processed")
        self.brain.due = False
        self.next_bar()
        self.clock.t -= 3000                                            # still inside the decision's hour
        rep = self.runner().run_once()
        self.assertEqual((rep["fills"], rep["brain"]["action"]), ([], "none"))
        self.assertEqual(self.brain.fallback_calls, [])                  # valid latest + cash within limit
        lines = runner_log(self.dir)
        self.assertEqual([x["action"] for x in lines], ["kimi", "none"])
        self.assertEqual(len(lines[0]["fills"]), 2)

    def test_decision_of_a_crashed_cycle_is_executed_after_restart_but_never_after_expiry(self):
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.9, "BTC_IRT": 0.1, "ETH_IRT": 0.0})]
        r = self.runner()
        with mock.patch.object(Runner, "_trade", side_effect=KeyboardInterrupt):   # dies before the write-ahead
            with self.assertRaises(KeyboardInterrupt):
                r.run_once()
        self.assertIsNone(r.state.get("last_bar_ts"))
        self.brain.due = False                                          # restart: decision is not due again
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "kimi")
        self.assertTrue(rep["fills"])
        # the same situation after the decision expired: not executed
        shutil.rmtree(self.dir)
        os.makedirs(self.dir)
        self.brain = FakeBrain(self.clock)
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.9, "BTC_IRT": 0.1, "ETH_IRT": 0.0})]
        r = self.runner()
        with mock.patch.object(Runner, "_trade", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                r.run_once()
        self.brain.due = False
        self.brain.last_decision.expires_at = self.clock.t - 1
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["decision_status"], "expired")
        # the expired decision (10% BTC) is not executed; only the contract's cash sweep runs
        # (no decision due and all the money is toman)
        self.assertEqual(rep["brain"]["action"], "cash_sweep")
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "USDT_IRT")])

    def test_no_order_is_sent_after_the_decision_expired(self):
        """The expiry is checked before EVERY order, not only when the decision is picked."""
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.5, "BTC_IRT": 0.3, "ETH_IRT": 0.2})]
        r = self.runner()
        real = r.broker.market_buy

        def slow_buy(*a, **k):
            fill = real(*a, **k)
            self.clock.t += 3601                                        # this order outlived the decision
            return fill
        r.broker.market_buy = slow_buy
        rep = r.run_once()
        self.assertEqual(rep["brain"]["action"], "kimi")
        self.assertEqual(len(rep["fills"]), 1)
        self.assertEqual([why for _, why in rep["skipped"]], ["decision expired"])
        self.assertFalse(rep.get("retry_buys"))                          # an expired decision is not retried
        self.assertIsNone(r._exec_deadline)

    def test_decision_computed_against_other_weights_is_not_executed(self):
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.9, "BTC_IRT": 0.1, "ETH_IRT": 0.0},
                                        computed_against={"USDT_IRT": 0.5})]
        rep = self.runner().run_once()
        self.assertEqual((rep["brain"]["action"], rep["fills"]), ("stale", []))

    def test_a_decision_computed_from_an_all_toman_portfolio_still_has_a_drift_guard(self):
        """Regression: computed_against is {} exactly when the portfolio was 100% toman (the fresh
        account). _drift() read that as "the decision does not say" and returned 0, so the 5% guard
        never fired for those decisions - the very case where the whole equity can move."""
        d = FakeDecision(targets={"BTC_IRT": 1.0}, computed_against={}, computed_cash=1.0)
        self.assertGreater(Runner._drift(d, {"USDT_IRT": 0.5}), 0.05)
        self.assertEqual(Runner._drift(d, {}), 0.0)                       # still 100% toman: no drift
        # a decision from an older version (no computed_cash) keeps the previous, permissive behaviour
        old = FakeDecision(targets={"BTC_IRT": 1.0}, computed_against={}, computed_cash=None)
        self.assertEqual(Runner._drift(old, {"USDT_IRT": 0.5}), 0.0)
        # end to end: the account now holds half USDT, so the decision is NOT executed
        write_paper_account(self.dir, {"IRT": "500000000", "USDT": "5000"})
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.0, "BTC_IRT": 1.0, "ETH_IRT": 0.0},
                                        computed_against={}, computed_cash=1.0)]
        rep = self.runner().run_once()
        self.assertEqual((rep["brain"]["action"], rep["fills"]), ("stale", []))
        self.assertIn("moved", rep["brain"]["why"])

    def test_a_decision_made_by_another_run_is_never_executed(self):
        """Regression: kimi_brain_state.json is shared by every mode using the same state dir, while
        the executed-once marker lives in runner_state_<mode>.json. A live runner therefore treated a
        decision left behind by a paper run - including a canned --test-kimi-reply - as "not executed
        yet" and traded it with real money. Only the run that made a decision may execute it."""
        self.brain.origin = "live"
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 1.0, "BTC_IRT": 0.0, "ETH_IRT": 0.0},
                                        origin="paper:test-hook")]
        rep = self.runner().run_once()
        self.assertEqual((rep["brain"]["action"], rep["fills"]), ("foreign", []))
        self.assertIn("paper:test-hook", rep["brain"]["why"])
        self.assertEqual(rep["brain"]["decision_status"], "foreign")
        # the real shape of the bug: the foreign decision is only READ from the shared state file
        # (no decision is due this hour). It is still never executed; only the safety fallback runs.
        self.next_bar()
        self.brain.due = False
        self.brain.last_decision = FakeDecision(targets={"USDT_IRT": 0.0, "BTC_IRT": 1.0, "ETH_IRT": 0.0},
                                                origin="paper:test-hook", decided_at=self.clock())
        rep = self.runner().run_once()
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "USDT_IRT")])
        self.assertEqual(rep["brain"]["decision_status"], "foreign")   # BTC was NOT bought
        self.brain.due = True
        self.brain.last_decision = None
        # the same decision made by THIS run is executed as before
        self.next_bar()
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 1.0, "BTC_IRT": 0.0, "ETH_IRT": 0.0}, origin="live",
                                        decided_at=self.clock())]
        self.assertEqual(self.runner().run_once()["brain"]["action"], "kimi")
        # a brain without an origin (library use, older state) keeps the previous behaviour
        self.next_bar()
        self.brain.origin = ""
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.9, "BTC_IRT": 0.1, "ETH_IRT": 0.0}, origin="paper",
                                        decided_at=self.clock())]
        self.assertEqual(self.runner().run_once()["brain"]["action"], "kimi")

    def test_the_model_is_told_the_remaining_order_budget(self):
        """_order_budget_ok skips a whole rebalance with buys when the budget cannot cover it, and the
        decision is still marked executed. The model has to know how many orders are left."""
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 1.0, "BTC_IRT": 0.0, "ETH_IRT": 0.0})]
        self.runner(risk={"max_orders_per_day": 7}).run_once()
        self.assertEqual(self.brain.order_budgets[-1], {"remaining": 7, "max_per_24h": 7})

    def test_no_news_research_when_the_llm_budget_is_used_up(self):
        """Stage 1 costs 20-50k tokens. It must not be bought for a decision that fails immediately
        at budget.consume() because the daily LLM call / token budget is gone."""
        class Budget(object):
            limit, max_tokens = 3, 100

            def remaining(self):
                return 0

            def tokens_exhausted(self):
                return False

        class Researcher(object):
            def __init__(self):
                self.calls = 0

            def research(self, *a, **k):
                self.calls += 1
                raise AssertionError("stage 1 must not run when stage 2 cannot")

        self.brain.llm = type("L", (object,), {"budget": Budget()})()
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 1.0, "BTC_IRT": 0.0, "ETH_IRT": 0.0})]
        news = Researcher()
        broker = PaperBroker(MARKETS, self.dir, capital_irt=CAPITAL, book_source=self.ex.book, clock=self.clock)
        rm = RiskManager({"min_order_irt": 100000}, self.dir, "paper", clock=self.clock)
        r = Runner(None, broker, rm, RUNNER_CFG, self.dir, "paper", bars_source=self.ex.bars, clock=self.clock,
                   sleep=self.clock.sleep, brain=self.brain, context_builder=self.builder, news=news)
        rep = r.run_once()
        self.assertEqual(news.calls, 0)
        self.assertIn("max_calls_per_day", rep["brain"]["llm_budget"])

    def test_brain_errors_never_crash_and_only_the_fallback_can_trade(self):
        self.brain.raise_in_decide = RuntimeError("context exploded")
        rep = self.runner().run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertIn("context exploded", rep["brain"]["brain_error"])
        self.assertEqual(rep["brain"]["action"], "cash_sweep")          # all toman: excess swept into USDT
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "USDT_IRT")])

    def test_fallback_only_after_an_invalid_decision_or_when_no_decision_was_due(self):
        # a valid HOLD decision this bar: no fallback even though all the money is toman
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.0}, hold=True)]
        rep = self.runner().run_once()
        self.assertEqual((rep["brain"]["action"], rep["fills"], self.brain.fallback_calls), ("hold", [], []))
        # next bar, no decision due, cash above the limit -> cash sweep
        self.brain.due = False
        self.next_bar()
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "cash_sweep")
        self.assertEqual(len(self.brain.fallback_calls), 1)
        # no decision due and the cash left by the sweep (~5%) is too small to trade: fallback not asked
        self.next_bar()
        rep = self.runner().run_once()
        self.assertEqual((rep["brain"]["action"], rep["fills"], len(self.brain.fallback_calls)), ("none", [], 1))
        # an invalid decision: the fallback is always asked (derisk check); nothing tradable -> no order
        self.brain.due = True
        self.brain.next = [FakeDecision(valid=False, error="validation: prose", error_kind="validation")]
        self.next_bar()
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["decision_status"], "invalid")
        self.assertEqual(rep["fills"], [])
        self.assertEqual(len(self.brain.fallback_calls), 2)

    def test_derisk_sells_coins_into_usdt(self):
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.5, "BTC_IRT": 0.3, "ETH_IRT": 0.2})]
        r = self.runner()
        r.run_once()
        self.brain.due = True
        for _ in range(13):
            self.next_bar()
            self.brain.next = [FakeDecision(valid=False, error="llm: down", error_kind="llm")]
            rep = self.runner().run_once()
            if rep["brain"]["action"] == "derisk":
                break
        self.assertEqual(rep["brain"]["action"], "derisk")
        sides = [(f["side"], f["symbol"]) for f in rep["fills"]]
        self.assertEqual(sides[-1], ("buy", "USDT_IRT"))
        self.assertEqual(sorted(sides[:2]), [("sell", "BTC_IRT"), ("sell", "ETH_IRT")])   # sells first
        bal = r.broker.__class__(MARKETS, self.dir, book_source=self.ex.book).balances()
        self.assertEqual((bal.get("BTC"), bal.get("ETH")), (ZERO, ZERO))

    def test_halt_or_pending_orders_skip_the_brain(self):
        rm = RiskManager({}, self.dir, "paper", clock=self.clock)
        rm.halt("test halt")
        rep = self.runner().run_once()
        self.assertEqual(rep["status"], "halted")
        self.assertEqual((self.brain.should_calls, self.brain.decide_calls), (0, []))

    def test_kill_switch_is_the_llm_abort_and_stops_orders(self):
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.9, "BTC_IRT": 0.1, "ETH_IRT": 0.0})]
        r = self.runner()
        orig = self.brain.decide

        def decide_then_stop(ctx, cur, trigger=None, abort=None, min_trade_weight=None, order_budget=None, now=None):
            self.assertIsNone(abort())
            open(os.path.join(self.dir, KILL_SWITCH_FILE), "w").close()
            self.assertIn("kill switch", abort())
            return orig(ctx, cur, trigger, abort, min_trade_weight, order_budget)
        self.brain.decide = decide_then_stop
        rep = r.run_once()
        self.assertEqual(rep["fills"], [])
        self.assertIn(("BTC_IRT", "kill switch"), rep["skipped"])
        # the same STOP check is the context build's abort (no more market requests once it exists)
        self.assertIn("kill switch", self.builder.build_aborts[0]())

    def test_dry_run_asks_the_brain_but_writes_no_state(self):
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.9, "BTC_IRT": 0.1, "ETH_IRT": 0.0})]
        before = sorted(os.listdir(self.dir))
        rep = self.runner(dry_run=True).run_once()
        self.assertEqual((rep["status"], rep["brain"]["action"]), ("dry_run", "kimi"))
        self.assertTrue(rep["vetted"])
        self.assertEqual(sorted(os.listdir(self.dir)), before)

    def test_capped_bot_passes_its_sleeve_to_the_brain(self):
        write_paper_account(self.dir, {"IRT": "900000000", "BTC": "0.0125"})   # user's own BTC: 100M IRT
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 1.0, "BTC_IRT": 0.0, "ETH_IRT": 0.0})]
        rep = self.runner(cfg={"max_equity_irt": 200000000}).run_once()
        self.assertEqual(self.builder.snaps[0]["balances"], {"IRT": 200000000.0})
        self.assertEqual(self.brain.decide_calls[0]["cur"], {})
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "USDT_IRT")])
        bal = PaperBroker(MARKETS, self.dir, book_source=self.ex.book).balances()
        self.assertEqual(bal["BTC"], D("0.0125"))                       # the user's coins are never sold
        self.assertGreater(bal["IRT"], D("700000000"))                  # toman above the sleeve untouched

    def test_cash_sweep_never_counts_sleeve_coins_outside_the_brain_as_cash(self):
        """A capped sleeve still holding a coin the brain no longer trades (ETH here): the fallback
        gets the IRT values of the managed holdings, so that coin is neither swept nor sold."""
        write_paper_account(self.dir, {"IRT": "700000000", "ETH": "1"})        # ETH = 300M IRT
        atomic_write_json(os.path.join(self.dir, "runner_state_paper.json"), {"sleeve": {
            "budget": "800000000", "cash": "500000000", "units": {"ETH_IRT": "1"}, "booked": {}}})
        self.brain.allowed = ["USDT_IRT", "BTC_IRT"]
        self.brain.due = False
        r = self.runner(cfg={"max_equity_irt": 800000000})
        self.assertEqual(r.extra_symbols, ["ETH_IRT"])
        rep = r.run_once()
        self.assertEqual(rep["brain"]["action"], "cash_sweep")
        vals = self.brain.fallback_values
        self.assertAlmostEqual(vals["IRT"], 5e8, delta=1)
        self.assertAlmostEqual(vals["ETH_IRT"], 3e8, delta=1)
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "USDT_IRT")])
        self.assertAlmostEqual(float(rep["targets"]["USDT_IRT"]), 0.575, places=6)   # 62.5% cash -> 5%
        self.assertAlmostEqual(float(rep["targets"]["ETH_IRT"]), 0.375, places=6)    # kept, not sold
        bal = PaperBroker(MARKETS, self.dir, book_source=self.ex.book).balances()
        self.assertEqual(bal["ETH"], D("1"))

    def test_buy_retry_reuses_this_bars_decision(self):
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.7, "BTC_IRT": 0.3, "ETH_IRT": 0.0})]
        r = self.runner()
        real = self.ex.book
        calls = {"n": 0}

        def flaky(symbol):
            if symbol == "BTC_IRT" and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("book timeout")
            return real(symbol)
        r.broker._book_source = flaky
        rep = r.run_once()
        self.assertTrue(rep.get("retry_buys"))
        self.clock.t += 60
        rep2 = r.run_once()
        self.assertEqual([(f["side"], f["symbol"]) for f in rep2["fills"]], [("buy", "BTC_IRT")])
        self.assertEqual(len(self.brain.decide_calls), 1)                # no second Kimi call for the retry

    def test_the_brain_gets_the_schedule_slack_and_one_minimum_order_as_a_weight(self):
        write_paper_account(self.dir, {"IRT": "3600000"})                      # a small account
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.96, "BTC_IRT": 0.0, "ETH_IRT": 0.0})]
        rep = self.runner().run_once()
        mtw = 100000 * (1 + MIN_ORDER_HEADROOM) / 3600000                       # min_order_irt 100,000
        self.assertAlmostEqual(self.brain.decide_calls[0]["min_trade_weight"], mtw, places=9)
        self.assertEqual(self.brain.should_slack, [BRAIN_SCHEDULE_SLACK])
        self.assertAlmostEqual(rep["brain"]["min_trade_weight"], mtw, places=6)
        self.assertEqual(runner_log(self.dir)[0]["min_trade_weight"], rep["brain"]["min_trade_weight"])
        # a large account: one minimum order is far below the threshold (passed, not reported)
        shutil.rmtree(self.dir)
        os.makedirs(self.dir)
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.96, "BTC_IRT": 0.0, "ETH_IRT": 0.0})]
        rep = self.runner().run_once()
        self.assertAlmostEqual(self.brain.decide_calls[1]["min_trade_weight"], 100000 * 1.03 / 1e9, places=12)
        self.assertNotIn("min_trade_weight", rep["brain"])

    def test_no_cash_sweep_is_asked_for_below_one_minimum_order(self):
        # 3.6M toman with 7.3% toman: 2.3% above max_irt_cash - more than the 2% rebalance threshold but
        # less than one minimum order (100,000 = 2.86% with headroom): the sweep could not be executed
        write_paper_account(self.dir, {"IRT": "262800", "USDT": "33.372"})
        self.brain.due = False
        rep = self.runner().run_once()
        self.assertEqual((rep["brain"]["action"], rep["fills"], self.brain.fallback_calls), ("none", [], []))
        # 8% toman: one minimum order fits -> the brain's cash sweep is asked, with the same step
        shutil.rmtree(self.dir)
        os.makedirs(self.dir)
        write_paper_account(self.dir, {"IRT": "288000", "USDT": "33.12"})
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "cash_sweep")
        self.assertAlmostEqual(self.brain.fallback_mtw[0], 100000 * (1 + MIN_ORDER_HEADROOM) / 3600000, places=9)
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "USDT_IRT")])

    def test_a_pacing_postponement_is_reported(self):
        write_paper_account(self.dir, {"USDT": "10000"})                        # all USDT: nothing to sweep
        self.brain.due = False
        self.brain.pacing_block = "max_decisions_per_day=20 reached in the last 24 h"
        rep = self.runner().run_once()
        self.assertEqual((rep["brain"]["action"], rep["brain"]["pacing_block"]), ("none", self.brain.pacing_block))
        self.assertIn("decision postponed (pacing)", rep["brain"]["why"])
        self.assertIn("postponed (pacing): max_decisions_per_day=20", format_report(rep))
        self.assertEqual(runner_log(self.dir)[0]["pacing_block"], self.brain.pacing_block)

    def test_cash_sweep_threshold_comes_from_the_brain(self):
        """KimiBrain.fallback_cash_limit (e.g. the 40% toman of its last valid decision) replaces
        max_irt_cash as the runner's sweep threshold: 30% toman is then no reason to ask the fallback."""
        write_paper_account(self.dir, {"IRT": "300000000", "USDT": "7000"})          # 30% toman, 70% USDT
        self.brain.due = False
        self.brain.fallback_cash_limit = lambda now=None: 0.4
        rep = self.runner().run_once()
        self.assertEqual((rep["brain"]["action"], rep["fills"], self.brain.fallback_calls), ("none", [], []))
        self.assertIn("within the limit 0.40", rep["brain"]["why"])
        self.brain.fallback_cash_limit = lambda now=None: 0.05                        # the plain 5% limit
        self.next_bar()
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "cash_sweep")
        self.assertEqual(len(self.brain.fallback_calls), 1)

    def test_brain_mode_needs_a_context_builder(self):
        broker = PaperBroker(MARKETS, self.dir, capital_irt=CAPITAL, book_source=self.ex.book, clock=self.clock)
        with self.assertRaises(RunnerError):
            Runner(None, broker, RiskManager({}, self.dir, "paper"), RUNNER_CFG, self.dir, "paper",
                   brain=self.brain)


# --------------------------------------------------------------------------- stage 1 (news) in the runner

class FakeNews(object):
    """NewsResearcher double: records every research() call (and the shared call order)."""

    def __init__(self, order, brief=None, on_research=None):
        self.order = order
        self.calls = []
        self.brief = brief if brief is not None else NewsBrief(ok=True, text="Summary: calm markets",
                                                               fetched_at=NOW - 600, items=[{"headline": "h"}],
                                                               searches=1)
        self.on_research = on_research

    def research(self, now=None, context_hint=None, force=False, abort=None):
        self.order.append("research")
        self.calls.append({"now": now, "hint": context_hint, "abort": abort, "force": force})
        if self.on_research:
            self.on_research()
        return self.brief


class NewsFakeBrain(FakeBrain):
    """FakeBrain whose decide() takes the stage-1 brief (like KimiBrain.decide)."""

    def __init__(self, clock, order):
        FakeBrain.__init__(self, clock)
        self.order = order

    def decide(self, ctx, cur, trigger=None, abort=None, min_trade_weight=None, news=None, order_budget=None, now=None):
        self.order.append("decide")
        d = FakeBrain.decide(self, ctx, cur, trigger, abort, min_trade_weight, order_budget, now)
        self.decide_calls[-1]["news"] = news
        return d


class OrderBuilder(FakeBuilder):
    def __init__(self, order):
        FakeBuilder.__init__(self)
        self.order = order

    def build(self, snap, recent_decisions=None, abort=None):
        self.order.append("build")
        return FakeBuilder.build(self, snap, recent_decisions, abort)


class RunnerNewsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_int_news_")
        self.ex = Exchange()
        self.clock = Clock()
        self.order = []
        self.brain = NewsFakeBrain(self.clock, self.order)
        self.builder = OrderBuilder(self.order)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def runner(self, news):
        broker = PaperBroker(MARKETS, self.dir, capital_irt=CAPITAL, book_source=self.ex.book, clock=self.clock)
        rm = RiskManager({"min_order_irt": 100000}, self.dir, "paper", clock=self.clock)
        return Runner(None, broker, rm, RUNNER_CFG, self.dir, "paper", bars_source=self.ex.bars, clock=self.clock,
                      sleep=self.clock.sleep, brain=self.brain, context_builder=self.builder, news=news)

    def test_research_comes_before_the_context_build_and_the_decision(self):
        news = FakeNews(self.order)
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.8, "BTC_IRT": 0.2, "ETH_IRT": 0.0})]
        r = self.runner(news)
        rep = r.run_once()
        self.assertEqual(self.order, ["research", "build", "decide"])
        call = news.calls[0]
        self.assertEqual(call["now"], NOW)
        self.assertEqual(call["abort"], r._abort_reason)             # the STOP check, polled per request
        self.assertFalse(call["force"])                              # the ~2 h cache applies
        self.assertEqual(call["hint"], "held: none (toman/USDT only); tradable on Bitpin: BTC, ETH")
        self.assertIs(self.brain.decide_calls[0]["news"], news.brief)
        self.assertEqual(rep["brain"]["action"], "kimi")
        self.assertEqual(rep["brain"]["news"]["ok"], True)
        self.assertIn("kimi news: ok, fresh, 1 items", format_report(rep))
        self.assertEqual(runner_log(self.dir)[0]["news"]["items"], 1)
        # a decision that is not due: no research either
        self.brain.due = False
        self.next_bar()
        self.runner(news).run_once()
        self.assertEqual(len(news.calls), 1)
        # the hint names coins only (held ones first), never amounts
        self.brain.due = True
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.8, "BTC_IRT": 0.2, "ETH_IRT": 0.0}, hold=True)]
        self.next_bar()
        self.runner(news).run_once()
        self.assertEqual(news.calls[1]["hint"], "held: BTC; tradable on Bitpin: BTC, ETH")

    def next_bar(self):
        self.ex.add_bar(self.ex.series["USDT_IRT"][-1].ts + 3600)
        self.clock.t += 3600

    def test_stop_created_during_the_research_ends_the_hour(self):
        stop = os.path.join(self.dir, KILL_SWITCH_FILE)
        news = FakeNews(self.order, on_research=lambda: open(stop, "w").close())
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 0.8, "BTC_IRT": 0.2, "ETH_IRT": 0.0})]
        rep = self.runner(news).run_once()
        self.assertEqual(self.order, ["research"])                   # no context build, no Kimi decision
        self.assertEqual((rep["fills"], self.brain.fallback_calls), ([], []))
        self.assertEqual(rep["brain"]["action"], "none")
        self.assertIn("kill switch", rep["brain"]["why"])

    def test_kill_switch_skips_news_research(self):
        open(os.path.join(self.dir, KILL_SWITCH_FILE), "w").close()
        news = FakeNews(self.order)
        rep = self.runner(news).run_once()
        self.assertEqual((rep["status"], news.calls, self.order), ("kill_switch", [], []))
        # and _research_news itself returns None with the switch on
        self.assertIsNone(self.runner(news)._research_news(NOW, {}))
        self.assertEqual(news.calls, [])

    def test_a_crashing_researcher_never_blocks_the_decision(self):
        class Broken(object):
            def research(self, *a, **k):
                raise RuntimeError("boom")
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 1.0, "BTC_IRT": 0.0, "ETH_IRT": 0.0})]
        rep = self.runner(Broken()).run_once()
        self.assertEqual(self.order, ["build", "decide"])
        # NOT None: None means "news research is not configured" in the stage-2 prompt, so an internal
        # failure is passed on as an UNAVAILABLE brief instead
        brief = self.brain.decide_calls[0]["news"]
        self.assertIsNotNone(brief)
        self.assertFalse(brief.ok)
        self.assertIn("internal: RuntimeError: boom", brief.error)
        # the PROMPT gets a bot-authored category only: the raw text of an exception (or of an HTTP
        # body copied into it) never reaches the message that decides the allocation
        self.assertIn("NEWS BRIEF: UNAVAILABLE (an internal error in the bot)", brief.prompt_block(NOW))
        self.assertNotIn("RuntimeError", brief.prompt_block(NOW))
        self.assertEqual(rep["brain"]["news"]["ok"], False)
        self.assertEqual(rep["brain"]["action"], "kimi")

    def test_runner_without_news_still_calls_decide_without_the_argument(self):
        brain = FakeBrain(self.clock)                                   # decide() has no news parameter
        brain.next = [FakeDecision(targets={"USDT_IRT": 1.0, "BTC_IRT": 0.0, "ETH_IRT": 0.0})]
        broker = PaperBroker(MARKETS, self.dir, capital_irt=CAPITAL, book_source=self.ex.book, clock=self.clock)
        rm = RiskManager({"min_order_irt": 100000}, self.dir, "paper", clock=self.clock)
        r = Runner(None, broker, rm, RUNNER_CFG, self.dir, "paper", bars_source=self.ex.bars, clock=self.clock,
                   sleep=self.clock.sleep, brain=brain, context_builder=FakeBuilder())
        self.assertIsNone(r.news)
        rep = r.run_once()
        self.assertEqual(rep["brain"]["action"], "kimi")
        self.assertNotIn("news", brain.decide_calls[0])
        self.assertIsNone(runner_log(self.dir)[0]["news"])
        # a strategy runner never keeps a researcher
        self.assertIsNone(Runner(None, broker, rm, RUNNER_CFG, self.dir, "paper", bars_source=self.ex.bars,
                                 clock=self.clock, brain=brain, context_builder=FakeBuilder(), news=None).news)


# --------------------------------------------------------------------------- real KimiBrain end to end

def kimi_config(**brain):
    b = {"allowed_symbols": SYMS, "web_search": False, "risk_profile": "balanced"}
    b.update(brain)
    return {"llm": {"model": "kimi-test", "max_retries": 0, "backoff_seconds": 0, "backoff_max_seconds": 0},
            "brain": b,
            "context": {"universe": SYMS, "macro_symbols": ["USDT_IRT"], "rule_signals": False,
                        "competition_start_utc": "2026-09-21T20:30:00Z",
                        "competition_end_utc": "2026-10-22T20:30:00Z"}}


class EndToEndPaperTest(unittest.TestCase):
    """The hourly cycle in paper mode with the REAL KimiBrain, LLMClient (fake HTTP transport) and
    MarketContextBuilder (fake public Bitpin data)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_int_e2e_")
        self.ex = Exchange()
        self.clock = Clock()
        self.kimi = FakeKimiTransport(self.clock)
        self.build_delay = 0

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def builder_bars(self, symbol, res, start, end):
        self.clock.t += self.build_delay               # how long each candle request of the context build takes
        return self.ex.bars(symbol, res, start, end)

    def runner(self, news=None, runner_cfg=None, **brain_cfg):
        rcfg = dict(RUNNER_CFG, **(runner_cfg or {}))
        llm, brain, builder = build_kimi(kimi_config(**brain_cfg), self.ex, self.dir, runner_cfg=rcfg,
                                         transport=self.kimi, env={"KIMI_API_KEY": KEY}, bars_source=self.builder_bars,
                                         clock=self.clock)
        broker = PaperBroker(MARKETS, self.dir, capital_irt=CAPITAL, book_source=self.ex.book, clock=self.clock)
        rm = RiskManager({"min_order_irt": 100000}, self.dir, "paper", clock=self.clock)
        self.brain = brain
        return Runner(None, broker, rm, rcfg, self.dir, "paper", bars_source=self.ex.bars, clock=self.clock,
                      sleep=self.clock.sleep, brain=brain, context_builder=builder, news=news)

    def next_bar(self, hours=1):
        for _ in range(hours):
            self.ex.add_bar(self.ex.series["USDT_IRT"][-1].ts + 3600)
            self.clock.t += 3600

    def invest(self):
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        r = self.runner()
        rep = r.run_once()
        self.assertEqual((rep["status"], rep["brain"]["action"]), ("ok", "kimi"), rep.get("brain"))
        return r, rep

    def test_hourly_cycle_valid_decision_places_orders(self):
        r, rep = self.invest()
        self.assertEqual(sorted(f["symbol"] for f in rep["fills"] if f["side"] == "buy"), sorted(SYMS))
        w = weights(r.broker, self.ex.prices)
        self.assertAlmostEqual(w["USDT_IRT"], 0.75, delta=0.03)
        self.assertLessEqual(w["BTC_IRT"] + w["ETH_IRT"], 0.26)
        self.assertLess(w["IRT"], 0.02)
        self.assertEqual(len(self.kimi.chats), 1)
        with open(os.path.join(self.dir, "kimi_decisions.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 1)
        self.assertEqual([x["action"] for x in runner_log(self.dir)], ["kimi"])
        with open(os.path.join(self.dir, "kimi_decisions.jsonl"), encoding="utf-8") as f:
            self.assertNotIn(KEY, f.read())
        # the same bar again (restart): no second Kimi call, no order
        self.assertEqual(self.runner().run_once()["status"], "already_processed")
        # next hour: the decision is not due yet (2 h interval) and the cash is invested: nothing
        self.next_bar()
        rep = self.runner().run_once()
        self.assertEqual((rep["fills"], rep["brain"]["action"], len(self.kimi.chats)), ([], "none", 1))

    def test_invalid_reply_means_no_trade(self):
        r, _ = self.invest()
        before = r.broker.balances()
        self.next_bar(2)                                     # decision due again
        self.kimi.replies = ["I would buy BTC here.", "Still no JSON, sorry."]
        rep = self.runner().run_once()
        self.assertEqual(len(self.kimi.chats), 3)            # the validation retry was used
        self.assertFalse(rep["brain"]["decision"]["valid"])
        self.assertEqual((rep["brain"]["action"], rep["fills"]), ("none", []))
        self.assertEqual(PaperBroker(MARKETS, self.dir, book_source=self.ex.book).balances(), before)

    @unittest.skipUnless(HAS_FALLBACK, NEED_FALLBACK)
    def test_llm_down_only_sweeps_the_toman_into_usdt(self):
        write_paper_account(self.dir, {"IRT": "800000000", "BTC": "0.025"})    # 20% BTC, 80% toman
        self.kimi.down = True
        rep = self.runner().run_once()
        self.assertFalse(rep["brain"]["decision"]["valid"])
        self.assertEqual(rep["brain"]["action"], "cash_sweep")
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "USDT_IRT")])
        bal = PaperBroker(MARKETS, self.dir, book_source=self.ex.book).balances()
        self.assertEqual(bal["BTC"], D("0.025"))                                # never sells coins
        w = weights(PaperBroker(MARKETS, self.dir, book_source=self.ex.book), self.ex.prices)
        self.assertLessEqual(w["IRT"], 0.06)
        self.assertAlmostEqual(w["USDT_IRT"], 0.75, delta=0.02)

    @unittest.skipUnless(HAS_FALLBACK, NEED_FALLBACK)
    def test_more_than_12h_without_a_valid_decision_derisks_into_usdt(self):
        """Kimi goes down after a valid decision: the bot keeps the coins and retries hourly; once
        Kimi has failed for more than fallback.derisk_after_hours (12) it sells them into USDT_IRT.
        (Code exits off: with them the derisk leaves the guarded coins to their exits, see
        test_derisk_defers_to_live_code_exits.)"""
        no_exits = {"exits": {"enabled": False}}
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        rep = self.runner(runner_cfg=no_exits).run_once()
        self.assertEqual((rep["status"], rep["brain"]["action"]), ("ok", "kimi"), rep.get("brain"))
        self.kimi.down = True
        actions = []
        first_failure = None
        for h in range(1, 21):
            self.next_bar()
            rep = self.runner(runner_cfg=no_exits).run_once()
            actions.append(rep["brain"]["action"])
            d = rep["brain"].get("decision") or {}
            if first_failure is None and rep["brain"].get("decided") and not d.get("valid"):
                first_failure = h
            if rep["brain"]["action"] == "derisk":
                break
            self.assertFalse([f for f in rep["fills"] if f["side"] == "sell"], "no coin sold before the derisk")
        self.assertEqual(actions[-1], "derisk", actions)
        self.assertIsNotNone(first_failure)
        self.assertGreater(h - first_failure, 12)          # > derisk_after_hours after Kimi began failing
        self.assertLessEqual(h - first_failure, 14)
        sides = [(f["side"], f["symbol"]) for f in rep["fills"]]
        self.assertEqual(sorted(sides[:2]), [("sell", "BTC_IRT"), ("sell", "ETH_IRT")])
        w = weights(PaperBroker(MARKETS, self.dir, book_source=self.ex.book), self.ex.prices)
        self.assertLess(w["BTC_IRT"] + w["ETH_IRT"], 0.01)
        self.assertGreater(w["USDT_IRT"], 0.93)

    @unittest.skipUnless(HAS_FALLBACK, NEED_FALLBACK)
    def test_derisk_defers_to_live_code_exits(self):
        """The research: "derisk never overrides live code exits". Kimi-bought coins are positions with
        a max hold (and a stop / target where Kimi set one; v3 has no default stop); a long Kimi outage
        leaves them to those exits."""
        # v3: a position is protected by live exits only where Kimi set a stop (there is no default stop), so
        # the decision names one for both coins; an unstopped position is sold by the derisk like any other
        stopped = reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})
        stopped["exits"] = {"BTC_IRT": {"stop_pct": 12}, "ETH_IRT": {"stop_pct": 12}}
        self.kimi.replies = [stopped]
        r = self.runner()
        rep = r.run_once()
        self.assertEqual((rep["status"], rep["brain"]["action"]), ("ok", "kimi"), rep.get("brain"))
        self.assertEqual(sorted(r._positions), ["BTC_IRT", "ETH_IRT"])
        self.assertTrue(all(p["stop_px_usdt"] > 0 and p["max_hold_until"] > 0 for p in r._positions.values()))
        self.kimi.down = True
        for _ in range(20):
            self.next_bar()
            rep = self.runner().run_once()
            self.assertFalse([f for f in rep["fills"] if f["side"] == "sell"], rep["brain"])
        bal = PaperBroker(MARKETS, self.dir, book_source=self.ex.book).balances()
        self.assertGreater(bal["BTC"], 0)
        self.assertGreater(bal["ETH"], 0)

    def test_the_two_hour_schedule_does_not_slip_although_the_context_build_takes_time(self):
        """decide() stamps decided_at after the context build, the hourly check comes before it: the
        check 2 h later is a little less than 2 h after decided_at and must still decide."""
        self.build_delay = 45                              # 3 candle requests: decided_at = check + 135 s
        self.invest()
        self.next_bar()
        self.clock.t = NOW + 3600                          # the loop wakes at the bar close, not relative to the last cycle
        rep = self.runner().run_once()
        self.assertEqual((rep["brain"]["decided"], len(self.kimi.chats)), (False, 1))   # 1 h: not due
        self.next_bar()
        self.clock.t = NOW + 7200
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        rep = self.runner().run_once()
        self.assertTrue(rep["brain"]["decided"], rep["brain"])                          # 2 h: due (not 3 h)
        self.assertEqual(len(self.kimi.chats), 2)
        self.assertIn("scheduled", rep["brain"]["trigger"])

    def test_small_account_sweep_is_at_least_one_minimum_order(self):
        """3.6M toman: one minimum order (100,000) is 2.8% of equity, more than the 2% threshold. Kimi
        is down and 7.5% is toman: the sweep of the 2.5% excess alone would be refused by the risk
        manager every hour; the brain moves one full minimum order instead."""
        write_paper_account(self.dir, {"IRT": "270000", "USDT": "33.3"})
        self.kimi.down = True
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "cash_sweep")
        self.assertEqual([(f["side"], f["symbol"]) for f in rep["fills"]], [("buy", "USDT_IRT")], rep["skipped"])
        self.assertGreaterEqual(D(rep["fills"][0]["quote"]), D("100000"))
        self.assertFalse([s for s in rep["skipped"] if "minimum" in str(s[1])], rep["skipped"])
        w = weights(PaperBroker(MARKETS, self.dir, book_source=self.ex.book), self.ex.prices)
        self.assertLessEqual(w["IRT"], 0.05)

    def test_small_account_kimi_change_below_one_minimum_order_is_a_hold(self):
        write_paper_account(self.dir, {"USDT": "36"})                           # 3.6M toman, all USDT
        self.kimi.replies = [reply({"USDT_IRT": 0.977, "BTC_IRT": 0.023})]     # 82,800 toman of BTC
        rep = self.runner().run_once()
        self.assertTrue(rep["brain"]["decision"]["valid"])
        self.assertEqual((rep["brain"]["action"], rep["fills"], rep["skipped"]), ("hold", [], []))

    def news_researcher(self, reply_text=None, status=200):
        """A REAL NewsResearcher (bitpin/news.py) with a fake Moonshot transport: one $web_search round,
        then `reply_text` (or an HTTP error status)."""
        self.news_bodies = []

        def transport(method, url, headers, body, timeout):
            assert headers.get("Authorization") == "Bearer " + KEY
            msgs = json.loads(body.decode("utf-8"))["messages"]
            self.news_bodies.append(msgs)
            if status != 200:
                return status, b'{"error": {"message": "Invalid request: tokenization failed"}}'
            if not any(m.get("role") == "tool" for m in msgs):
                call = {"id": "c1", "type": "builtin_function",
                        "function": {"name": "$web_search", "arguments": '{"search_result": {"id": "s"}}'}}
                return 200, json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [call]}}],
                    "usage": {"prompt_tokens": 1500, "completion_tokens": 50, "total_tokens": 1550}}).encode("utf-8")
            return 200, json.dumps({"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": reply_text}}],
                "usage": {"prompt_tokens": 7100, "completion_tokens": 400, "total_tokens": 7500}}).encode("utf-8")
        return NewsResearcher({"model": "kimi-k2.6", "max_retries": 0}, self.dir, transport=transport,
                              env={"KIMI_API_KEY": KEY}, clock=self.clock, sleep=self.clock.sleep)

    def test_news_is_researched_right_before_each_decision(self):
        news_reply = json.dumps({"items": [{"headline": "US Fed holds rates, signals patience",
                                            "why_it_matters": "risk-on tone for crypto",
                                            "source_url": "https://www.reuters.com/markets/fed-holds",
                                            "time_hint": "2026-09-21"}],
                                 "summary": "risk-on after the Fed"})
        news = self.news_researcher(news_reply)
        order = []
        orig = news.research

        def research(*a, **k):
            order.append(("research", len(self.kimi.chats)))
            return orig(*a, **k)
        news.research = research
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        rep = self.runner(news=news, decision_interval_hours=1).run_once()
        self.assertEqual((rep["status"], rep["brain"]["action"]), ("ok", "kimi"), rep.get("brain"))
        self.assertEqual(order, [("research", 0)])                      # before the (only) decision call
        self.assertEqual(len(self.news_bodies), 2)                      # 1 search round + the answer
        user = self.kimi.bodies[-1]["messages"][1]["content"]
        self.assertIn("<<<NEWS_BRIEF", user)
        self.assertIn("US Fed holds rates", user)
        self.assertLess(user.index("NEWS_BRIEF>>>"), user.index("MARKET CONTEXT (JSON"))
        hint = self.news_bodies[0][1]["content"]
        self.assertIn("tradable on Bitpin: BTC, ETH", hint)
        self.assertNotRegex(hint.split("Coins that matter")[1], r"\d")      # coin names only, no amounts
        self.assertTrue(runner_log(self.dir)[0]["news"]["ok"])
        with open(os.path.join(self.dir, "kimi_decisions.jsonl"), encoding="utf-8") as f:
            self.assertTrue(json.loads(f.readline())["news"]["ok"])
        # the hourly decision an hour later reuses the cached brief (younger than cache_minutes 110);
        # the one after that researches again. v3 (B3): the gate researches only after a decision that was
        # NOT a plain HOLD, so this one trades (the after-HOLD reuse has its own test below)
        self.next_bar()
        self.kimi.replies = [reply({"USDT_IRT": 0.65, "BTC_IRT": 0.25, "ETH_IRT": 0.10})]
        rep = self.runner(news=news, decision_interval_hours=1).run_once()
        self.assertTrue(rep["brain"]["decided"], rep["brain"])
        self.assertEqual([x[0] for x in order], ["research", "research"])
        self.assertEqual(len(self.news_bodies), 2)                      # served from news_cache.json
        self.assertTrue(rep["brain"]["news"]["cached"])
        self.assertIn("US Fed holds rates", self.kimi.bodies[-1]["messages"][1]["content"])
        self.next_bar()
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        rep = self.runner(news=news, decision_interval_hours=1).run_once()
        self.assertEqual(len(order), 3)
        self.assertEqual(len(self.news_bodies), 4)                      # a new research call (cache expired)
        self.assertFalse(rep["brain"]["news"]["cached"])

    def test_after_a_plain_hold_the_brief_is_reused_instead_of_researched(self):
        """v3 (B3): the runner tells the researcher the kind of the last decision; after a plain HOLD the
        daily brief is reused (reused="after_hold_only") even once the cache expired, until it is 48 h old."""
        news_reply = json.dumps({"items": [{"headline": "US Fed holds rates, signals patience",
                                            "why_it_matters": "risk-on tone for crypto",
                                            "source_url": "https://www.reuters.com/markets/fed-holds",
                                            "time_hint": "2026-09-21"}],
                                 "summary": "risk-on after the Fed"})
        news = self.news_researcher(news_reply)
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        rep = self.runner(news=news).run_once()
        self.assertEqual((rep["status"], rep["brain"]["action"]), ("ok", "kimi"), rep.get("brain"))
        self.assertEqual(len(self.news_bodies), 2)
        # the same targets an hour later: a HOLD (nothing to execute)
        self.next_bar()
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        rep = self.runner(news=news, decision_interval_hours=1).run_once()
        self.assertTrue(rep["brain"]["decided"] and rep["brain"]["decision"]["hold"], rep["brain"])
        # two hours on the cache (110 min) has expired, but the last decision was a HOLD: reused, no call
        self.next_bar()
        self.next_bar()
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        rep = self.runner(news=news, decision_interval_hours=1).run_once()
        self.assertTrue(rep["brain"]["decided"], rep["brain"])
        self.assertEqual(len(self.news_bodies), 2)
        self.assertTrue(rep["brain"]["news"]["cached"])
        self.assertIn("US Fed holds rates", self.kimi.bodies[-1]["messages"][1]["content"])

    def test_news_failure_never_blocks_the_decision(self):
        news = self.news_researcher(status=400)
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        rep = self.runner(news=news).run_once()
        self.assertEqual((rep["status"], rep["brain"]["action"]), ("ok", "kimi"), rep.get("brain"))
        self.assertFalse(rep["brain"]["news"]["ok"])
        user = self.kimi.bodies[-1]["messages"][1]["content"]
        self.assertIn("NEWS BRIEF: UNAVAILABLE (HTTP 400", user)
        self.assertIn("kimi news: UNAVAILABLE", format_report(rep))

    def test_full_profile_all_in_one_coin_is_executed(self):
        self.kimi.replies = [reply({"BTC_IRT": 1.0})]
        rep = self.runner(risk_profile="full").run_once()
        self.assertEqual((rep["status"], rep["brain"]["action"]), ("ok", "kimi"), rep.get("brain"))
        w = weights(PaperBroker(MARKETS, self.dir, book_source=self.ex.book), self.ex.prices)
        self.assertGreater(w["BTC_IRT"], 0.97)

    def test_expired_decision_is_not_executed(self):
        self.kimi.replies = [reply({"USDT_IRT": 0.75, "BTC_IRT": 0.15, "ETH_IRT": 0.10})]
        self.kimi.advance = 3700                              # the Kimi call took longer than the decision's life
        rep = self.runner().run_once()
        self.assertTrue(rep["brain"]["decision"]["valid"])
        self.assertEqual((rep["brain"]["action"], rep["fills"]), ("expired", []))
        self.assertEqual(PaperBroker(MARKETS, self.dir, book_source=self.ex.book).balances(), {"IRT": D(CAPITAL)})


# --------------------------------------------------------------------------- network isolation

class _Recorder(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append(self.path)
        body = json.dumps(self.server.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):                     # an HTTPS request through this "proxy": record, refuse
        self.server.hits.append("CONNECT " + self.path)
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def serve(payload):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    srv.hits = []
    srv.payload = payload
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


PROXY_CHILD = r"""
import json, os, sys, urllib.request
sys.path.insert(0, sys.argv[1])
base = sys.argv[2]
from bitpin import data as data_mod
from bitpin.api import BitpinClient
from bitpin.llm import LLMClient, LLMError
out = {}
# control: plain urllib reads the proxy variables of this environment
urllib.request.urlopen(base + "/control", timeout=10).read()
out["tickers"] = BitpinClient(base).tickers()
data_mod.BARS_URL = base + "/v1/mkt/tv/get_bars/"
out["bars"] = [b.ts for b in data_mod.fetch_bars("USDT_IRT", "60", 1000, 1000 + 3600 * 3)]
import importlib.util
spec = importlib.util.spec_from_file_location("check_balance", sys.argv[1] + "/scripts/check_balance.py")
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)
cb.BASE = base
out["check_balance"] = cb.call("GET", "/check_balance")[0]
llm = LLMClient({"model": "kimi-test", "max_retries": 0}, state_dir=None)
out["llm_proxy"] = llm.proxy_display
try:
    llm.list_models()
    out["llm"] = "ok?"
except LLMError as e:
    out["llm"] = "refused by the fake proxy: %s" % e
print(json.dumps(out))
"""


class ProxyIsolationTest(unittest.TestCase):
    def setUp(self):
        self.target = serve([{"symbol": "USDT_IRT", "price": "100000", "ts": 1000, "open": 1, "high": 1, "low": 1,
                              "close": 1, "volume": 1}])
        self.env_proxy = serve({"via": "env proxy"})
        self.kimi_proxy = serve({"via": "kimi proxy"})
        self.base = "http://127.0.0.1:%d" % self.target.server_port

    def tearDown(self):
        for s in (self.target, self.env_proxy, self.kimi_proxy):
            s.shutdown()
            s.server_close()

    def test_bitpin_goes_direct_and_only_kimi_uses_kimi_https_proxy(self):
        """A process started with system-wide proxy variables (as a server may have them): BitpinClient
        and data.fetch_bars go DIRECT, Kimi goes ONLY through KIMI_HTTPS_PROXY, and plain urllib
        (the control request) would have used the system proxy."""
        purl = "http://127.0.0.1:%d" % self.env_proxy.server_port
        env = dict(os.environ)
        for k in ("no_proxy", "NO_PROXY"):
            env.pop(k, None)
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
            env[k] = purl
        env.update(KIMI_HTTPS_PROXY="http://127.0.0.1:%d" % self.kimi_proxy.server_port, KIMI_API_KEY=KEY,
                   PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run([sys.executable, "-c", PROXY_CHILD, ROOT, self.base], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        res = json.loads(out.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertEqual(res["tickers"][0]["symbol"], "USDT_IRT")
        self.assertEqual(res["bars"], [1000])
        self.assertIn("/api/v1/mkt/tickers/", self.target.hits)
        self.assertTrue(any(h.startswith("/v1/mkt/tv/get_bars/") for h in self.target.hits))
        self.assertEqual(res["check_balance"], 200)                    # scripts/check_balance.py: direct too
        self.assertIn("/check_balance", self.target.hits)
        self.assertEqual(len(self.env_proxy.hits), 1)                  # only the control request
        self.assertIn("/control", self.env_proxy.hits[0])
        self.assertEqual(self.kimi_proxy.hits, ["CONNECT api.moonshot.ai:443"])
        self.assertIn("refused by the fake proxy", res["llm"])
        self.assertNotIn(KEY, out.stdout.decode("utf-8") + out.stderr.decode("utf-8"))


# --------------------------------------------------------------------------- CLI

def load_run_bot():
    spec = importlib.util.spec_from_file_location("run_bot_integration", os.path.join(ROOT, "scripts", "run_bot.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_int_cli_")
        self.lockdir = tempfile.mkdtemp(prefix="bitpin_int_locks_")
        self.rb = load_run_bot()
        self.handlers = list(logging.getLogger().handlers)
        self.level = logging.getLogger().level
        self.kimi_path = os.path.join(self.dir, "kimi.json")
        with open(self.kimi_path, "w", encoding="utf-8") as f:
            json.dump(kimi_config(), f)
        self.cfg_path = os.path.join(self.dir, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"max_equity_irt": 500000000}, f)
        self.sd = os.path.join(self.dir, "state")
        os.makedirs(self.sd)

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            if h not in self.handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(self.level)
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.lockdir, ignore_errors=True)

    def confirm_irt(self):
        atomic_write_json(os.path.join(self.sd, self.rb.CONFIRM_FILE),
                          {"confirmed": True, "irt_asset_code": "IRT", "irt_unit_divisor": "1"})

    def live_args(self, *extra):
        return ["live", "--brain", "kimi", "--config", self.cfg_path, "--kimi-config", self.kimi_path,
                "--state-dir", self.sd] + list(extra)

    def run_main(self, argv, stdin=None, inputs=None):
        out, err = io.StringIO(), io.StringIO()
        code = None
        env = {"BITPIN_API_KEY": "DUMMYAPIKEY123", "BITPIN_SECRET_KEY": "DUMMYSECRET456",
               "BITPIN_BOT_LOCK_DIR": self.lockdir, "KIMI_API_KEY": KEY}
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdin", stdin or io.StringIO()), \
                mock.patch("builtins.input", side_effect=list(inputs or [])), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.rb.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue()

    def test_non_interactive_live_gate(self):
        called = []

        def make_live(*a, **k):          # stands in for the Bitpin connection: stop right there
            called.append(a)
            raise SystemExit(0)
        self.rb.make_live = make_live
        # without --i-accept-the-risk
        code, text = self.run_main(self.live_args("--non-interactive"))
        self.assertEqual(code, 78)
        self.confirm_irt()
        # no LIVE_CONFIRMED yet
        code, text = self.run_main(self.live_args("--i-accept-the-risk", "--non-interactive"))
        self.assertEqual(code, 78)
        self.assertIn("confirm-live", text)
        # confirm-live (interactive) writes it
        code, text = self.run_main(["confirm-live", "--config", self.cfg_path, "--kimi-config", self.kimi_path,
                                    "--state-dir", self.sd], stdin=FakeTTY(), inputs=["I ACCEPT THE RISK"])
        self.assertEqual(code, 0, text)
        rec = read_json(os.path.join(self.sd, self.rb.LIVE_CONFIRMED_FILE))
        self.assertEqual(rec["brain"], "kimi")
        self.assertNotIn(KEY, json.dumps(rec, default=str))
        # matching settings: accepted (goes on to the credentials / exchange connection)
        code, text = self.run_main(self.live_args("--i-accept-the-risk", "--non-interactive"))
        self.assertEqual((code, len(called)), (0, 1), text)
        # a changed setting in kimi.json -> refused again
        cfg = kimi_config(risk_profile="aggressive")
        with open(self.kimi_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        code, text = self.run_main(self.live_args("--i-accept-the-risk", "--non-interactive"))
        self.assertEqual((code, len(called)), (78, 1))
        self.assertIn("differ", text)
        # a strategy run is not covered by a Kimi confirmation
        code, text = self.run_main(["live", "--config", self.cfg_path, "--state-dir", self.sd, "--i-accept-the-risk",
                                    "--non-interactive"])
        self.assertEqual((code, len(called)), (78, 1))
        # interactive live without a terminal is refused; with one it still asks for the typed phrase
        code, _ = self.run_main(self.live_args("--i-accept-the-risk"))
        self.assertEqual(code, 78)

    def test_confirm_live_prints_the_configured_fallback_limits_not_hard_coded_ones(self):
        """Regression: the summary hard-coded "(5%)" and "default 12 h" instead of reading
        brain.fallback of the kimi.json being confirmed, so a user who changed sweep_irt_above was
        shown a number the bot does not use."""
        self.rb.make_live = lambda *a, **k: (_ for _ in ()).throw(SystemExit(0))
        self.confirm_irt()
        cfg = kimi_config(risk_profile="full")
        cfg["brain"]["fallback"] = {"sweep_irt_above": 0.10, "derisk_after_hours": 8}
        with open(self.kimi_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        with open(self.cfg_path, encoding="utf-8") as f:
            rcfg = json.load(f)
        rcfg["risk"] = dict(rcfg.get("risk") or {}, max_orders_per_day=180)
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(rcfg, f)
        code, text = self.run_main(["confirm-live", "--config", self.cfg_path, "--kimi-config", self.kimi_path,
                                    "--state-dir", self.sd], stdin=FakeTTY(), inputs=["I ACCEPT THE RISK"])
        self.assertEqual(code, 0, text)
        self.assertIn("cash limit 10%", text)
        self.assertIn("derisk delay (8 h)", text)
        self.assertNotIn("(5%)", text)
        self.assertNotIn("default 12 h", text)
        self.assertIn("toman share of the last valid decision", text)   # risk_profile "full"
        # the order budget is part of what is confirmed, so it is shown
        self.assertIn("order budget   : at most 180 orders", text)

    def test_live_confirmation_covers_the_users_settings_not_the_code_defaults(self):
        """Comments / formatting and a code update that changes a built-in default keep the
        confirmation (update.sh restarts the service); any value in config.json does not."""
        called = []

        def make_live(*a, **k):
            called.append(a)
            raise SystemExit(0)
        self.rb.make_live = make_live
        self.confirm_irt()
        code, text = self.run_main(["confirm-live", "--config", self.cfg_path, "--kimi-config", self.kimi_path,
                                    "--state-dir", self.sd], stdin=FakeTTY(), inputs=["I ACCEPT THE RISK"])
        self.assertEqual(code, 0, text)
        # comments, key order and formatting: still confirmed
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write('{\n  "_note": "my comment",\n\n  "max_equity_irt": 500000000\n}\n')
        with open(self.kimi_path, "w", encoding="utf-8") as f:
            cfg = kimi_config()
            cfg["_comment"] = "added later"
            cfg["llm"]["_model"] = "explained"
            json.dump(cfg, f, indent=4, sort_keys=True)
        code, text = self.run_main(self.live_args("--i-accept-the-risk", "--non-interactive"))
        self.assertEqual((code, len(called)), (0, 1), text)
        # a new version of the code with another built-in default: still confirmed
        runner_mod = sys.modules["bitpin.runner"]
        with mock.patch.dict(runner_mod.DEFAULT_CONFIG, {"wake_delay_seconds": 90, "fee_rate": 0.004}):
            code, text = self.run_main(self.live_args("--i-accept-the-risk", "--non-interactive"))
        self.assertEqual((code, len(called)), (0, 2), text)
        # a changed value in config.json: refused (exit 78, not restarted by systemd)
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"max_equity_irt": 600000000}, f)
        code, text = self.run_main(self.live_args("--i-accept-the-risk", "--non-interactive"))
        self.assertEqual((code, len(called)), (78, 2))
        self.assertIn("config.json or kimi.json changed", text)

    def test_confirm_live_check_is_read_only_and_needs_no_terminal(self):
        """update.sh runs 'confirm-live --check' with the NEW code before it stops the live bot."""
        argv = ["confirm-live", "--check", "--config", self.cfg_path, "--kimi-config", self.kimi_path,
                "--state-dir", self.sd]
        code, text = self.run_main(argv)
        self.assertEqual(code, 1)
        self.assertIn("toman (IRT) balance is not confirmed", text)
        self.confirm_irt()
        code, text = self.run_main(argv)
        self.assertEqual(code, 1)
        self.assertIn("no LIVE_CONFIRMED", text)
        code, _ = self.run_main(argv[:1] + argv[2:], stdin=FakeTTY(), inputs=["I ACCEPT THE RISK"])
        self.assertEqual(code, 0)
        before = sorted(os.listdir(self.sd))
        code, text = self.run_main(argv)
        self.assertEqual(code, 0, text)
        self.assertIn("CONFIRMED: these settings", text)
        code, text = self.run_main(argv[:4] + argv[6:])                   # a strategy run is not covered
        self.assertEqual(code, 1)
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"max_equity_irt": 1000}, f)
        code, text = self.run_main(argv)
        self.assertEqual(code, 1)
        self.assertIn("settings differ", text)
        self.assertEqual(sorted(os.listdir(self.sd)), before)            # nothing written

    def test_confirm_live_requires_the_irt_confirmation_and_the_phrase(self):
        argv = ["confirm-live", "--config", self.cfg_path, "--kimi-config", self.kimi_path, "--state-dir", self.sd]
        code, text = self.run_main(argv, stdin=FakeTTY(), inputs=["I ACCEPT THE RISK"])
        self.assertEqual(code, 78)
        self.assertIn("status", text)
        self.confirm_irt()
        code, _ = self.run_main(argv, stdin=io.StringIO(), inputs=["I ACCEPT THE RISK"])   # not a terminal
        self.assertEqual(code, 78)
        code, _ = self.run_main(argv, stdin=FakeTTY(), inputs=["yes"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.sd, self.rb.LIVE_CONFIRMED_FILE)))

    def test_confirm_live_show_and_typed_phrase_need_no_terminal(self):
        """v3.1 panel: --show prints the summary only; --typed-phrase confirms with the phrase the owner typed
        in the panel (no terminal), and a wrong phrase writes nothing."""
        argv = ["confirm-live", "--config", self.cfg_path, "--kimi-config", self.kimi_path, "--state-dir", self.sd]
        conf = os.path.join(self.sd, self.rb.LIVE_CONFIRMED_FILE)
        code, text = self.run_main(argv + ["--show"], stdin=io.StringIO())
        self.assertEqual(code, 78)                                        # the toman balance first
        self.confirm_irt()
        code, text = self.run_main(argv + ["--show"], stdin=io.StringIO())
        self.assertEqual(code, 0, text)
        self.assertIn("settings digest:", text)
        self.assertIn("nothing was asked or written", text)
        self.assertFalse(os.path.exists(conf))
        code, text = self.run_main(argv + ["--typed-phrase", "i accept the risk"], stdin=io.StringIO())
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(conf))
        code, text = self.run_main(argv + ["--typed-phrase", "  I ACCEPT THE RISK "], stdin=io.StringIO())
        self.assertEqual(code, 0, text)
        rec = read_json(conf)
        self.assertEqual(rec["confirmed_how"], "typed-phrase")
        code, text = self.run_main(argv + ["--check"])
        self.assertEqual(code, 0, text)
        code, _ = self.run_main(argv + ["--show", "--check"])              # one mode at a time
        self.assertEqual(code, 2)

    def kimi_check(self, transport, env_extra=None):
        """kimi-check with LLMClient's own transport replaced (no network)."""
        env = dict(env_extra or {})
        with mock.patch.dict(os.environ, env), \
                mock.patch("bitpin.llm.make_llm_transport", lambda proxy=None, opener=None: transport):
            return self.run_main(["kimi-check", "--kimi-config", self.kimi_path])

    # ---- final review 3

    def test_an_empty_capital_irt_is_not_an_argparse_error(self):
        """systemd expands `--capital-irt ${PAPER_CAPITAL_IRT}` with an unset or emptied variable to
        an empty ARGUMENT instead of dropping it, and bitpin-bot.env is read after the unit's own
        Environment= default. argparse exit 2 is neither 0 nor 78, so the paper unit restarted for
        ever while `bitpin-bot health` still printed RESULT: OK."""
        self.assertIsNone(self.rb.optional_decimal(""))
        self.assertIsNone(self.rb.optional_decimal("  "))
        self.assertEqual(self.rb.optional_decimal("5000000"), D("5000000"))
        seen = {}

        def fake_runner(*a, **kw):
            raise SystemExit(0)
        with mock.patch.object(self.rb, "PaperBroker", lambda markets, sd, **kw: seen.update(kw) or fake_runner()):
            code, text = self.run_main(["paper", "--strategy", "hold_usdt", "--state-dir", self.sd,
                                        "--capital-irt", "", "--once", "--dry-run"])
        self.assertNotEqual(code, 2, text)                 # NOT "invalid D value: ''"
        self.assertIsNone(seen.get("capital_irt"))
        self.assertNotIn("invalid", text)

    def test_an_auth_failure_exits_78_instead_of_a_traceback(self):
        """AuthError / AuthBudgetExceeded are runner FATAL_ERRORS, deliberately re-raised out of
        Runner.loop(). Neither subclasses RunnerError or BrokerError, so main() let them escape as an
        uncaught traceback with status 1 - which RestartPreventExitStatus=0 78 does not cover, so
        systemd restarted the unit for ever."""
        from bitpin.api import AuthBudgetExceeded, AuthError, BitpinError
        self.assertFalse(issubclass(AuthError, (self.rb.RunnerError, self.rb.BrokerError)))
        self.assertFalse(issubclass(AuthBudgetExceeded, (self.rb.RunnerError, self.rb.BrokerError)))
        self.assertTrue(issubclass(AuthError, BitpinError))
        for exc in (AuthError("key rejected"), AuthBudgetExceeded("auth budget used up")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(self.rb, "cmd_status", side_effect=exc):
                    code, text = self.run_main(["status", "--config", self.cfg_path, "--state-dir", self.sd])
                self.assertEqual(code, self.rb.EXIT_CONFIG, text)
                self.assertIn("authentication problem", text)
                self.assertIn("a restart cannot fix this", text)

    def test_the_command_recommended_after_kimi_check_actually_works(self):
        """`--brain kimi` without `--kimi-config` exits 78, and the bitpin-bot wrapper's run branch
        only adds --state-dir: the old suggestion printed an error right after RESULT: OK."""
        src = self.rb_source()
        rec = src[src.index("Recommended before live trading"):]
        rec = rec[:rec.index("return 0")]
        self.assertIn("systemctl start bitpin-bot-paper", rec)
        self.assertIn("--kimi-config /etc/bitpin-bot/kimi.json", rec)
        # the flag combination the old line printed still fails, which is why it was replaced
        code, text = self.run_main(["paper", "--brain", "kimi", "--state-dir", self.sd, "--once"])
        self.assertEqual(code, 78)
        self.assertIn("needs --kimi-config", text)

    def rb_source(self):
        with open(os.path.join(ROOT, "scripts", "run_bot.py"), encoding="utf-8") as f:
            return f.read()

    def test_kimi_check_diagnostics(self):
        def status(code, body=b"{}"):
            return lambda m, u, h, b, t: (code, body)

        def down(m, u, h, b, t):
            raise TransportError("network error: [Errno 111] Connection refused")
        ok = FakeKimiTransport()
        code, text = self.kimi_check(ok)
        self.assertEqual(code, 0, text)
        self.assertIn("RESULT: OK", text)
        self.assertIn("* kimi-test", text)
        self.assertIn("KIMI_API_KEY", text)
        self.assertNotIn(KEY, text)
        code, text = self.kimi_check(status(401))
        self.assertEqual(code, 1)
        self.assertIn("REJECTED", text)
        for st in (403, 451):
            code, text = self.kimi_check(status(st))
            self.assertEqual(code, 1)
            self.assertIn("KIMI_HTTPS_PROXY", text)
            self.assertIn(str(st), text)
        code, text = self.kimi_check(down)
        self.assertEqual(code, 1)
        self.assertIn("CONNECTION ERROR", text)
        code, text = self.kimi_check(down, {"KIMI_HTTPS_PROXY": "http://u:secretpw@127.0.0.1:1081"})
        self.assertIn("through the Kimi proxy http://***@127.0.0.1:1081", text)
        self.assertNotIn("secretpw", text)
        code, text = self.kimi_check(ok, {"KIMI_HTTPS_PROXY": "socks5://127.0.0.1:1080"})
        self.assertEqual(code, 78)
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": ""}), \
                mock.patch("bitpin.llm.make_llm_transport", lambda proxy=None, opener=None: ok):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(self.rb.main(["kimi-check", "--kimi-config", self.kimi_path]), 1)
            self.assertIn("NOT SET", out.getvalue())
        self.assertEqual(ok.calls, [("GET", "https://api.moonshot.ai/v1/models")])   # no chat call, no Bitpin

    def test_test_hook_exists_only_for_paper(self):
        """The canned-reply hooks can never be enabled for live trading: the options do not exist for
        live / confirm-live / kimi-check (argparse exit 2), and make_brain refuses them in live mode
        (exit 78) as a second line of defence."""
        reply_path = os.path.join(self.dir, "reply.json")
        with open(reply_path, "w", encoding="utf-8") as f:
            json.dump(reply({"USDT_IRT": 1.0}), f)
        for extra in (["--test-kimi-reply", "unreachable"], ["--test-kimi-news", "unreachable"],
                      ["--test-kimi-reply", reply_path, "--test-kimi-news", reply_path]):
            for argv in (self.live_args("--dry-run", *extra), self.live_args("--i-accept-the-risk", "--non-interactive",
                                                                            *extra),
                         ["confirm-live", "--kimi-config", self.kimi_path] + extra,
                         ["kimi-check", "--kimi-config", self.kimi_path] + extra):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                    self.rb.main(argv)
                self.assertEqual(cm.exception.code, 2, argv)                 # argparse: no such option
        cfg = {"base_url": "https://api.bitpin.org"}
        for kw in ({"test_reply": "unreachable"}, {"test_news": "unreachable"},
                   {"test_reply": reply_path, "test_news": reply_path}):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                self.rb.make_brain(cfg, kimi_config(), self.sd, "live", **kw)
            self.assertEqual(cm.exception.code, 78, kw)
        # the paper hooks never mix canned and real Kimi calls: news alone is refused
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.rb.make_brain(cfg, kimi_config(), self.sd, "paper", test_news=reply_path)
        self.assertEqual(cm.exception.code, 78)
        # paper: the canned transport answers; the real key leaves the process environment and any
        # other Moonshot client would hit a dead local proxy
        kc = dict(kimi_config(), news={"model": "kimi-k2.6"})
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": KEY}):
            llm, brain, builder, news = self.rb.make_brain(cfg, kc, self.sd, "paper", test_reply=reply_path)
            self.assertNotEqual(os.environ["KIMI_API_KEY"], KEY)
            self.assertEqual(os.environ["KIMI_HTTPS_PROXY"], self.rb.TEST_HOOK_DEAD_PROXY)
            self.assertEqual(llm.list_models(), ["kimi-test"])
            self.assertIsNone(news)                                          # no news hook: no news research
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": KEY}):
            llm, _, _, news = self.rb.make_brain(cfg, kc, self.sd, "paper", test_reply="unreachable")
            with self.assertRaises(Exception):
                llm.list_models()
        # the news hook: a real NewsResearcher over the canned transport (1 simulated search round)
        news_path = os.path.join(self.dir, "news.json")
        with open(news_path, "w", encoding="utf-8") as f:
            json.dump({"items": [{"headline": "Calm day", "why_it_matters": "none", "source_url": "https://www.reuters.com/markets/calm-day",
                                  "time_hint": "today"}], "summary": "calm"}, f)
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": KEY}):
            _, _, _, news = self.rb.make_brain(cfg, kc, self.sd, "paper", test_reply=reply_path, test_news=news_path)
            b = news.research(force=True)
            self.assertTrue(b.ok and not b.cached, b.error)
            self.assertEqual((b.searches, len(b.items)), (1, 1))
            _, _, _, news = self.rb.make_brain(cfg, kc, os.path.join(self.dir, "s2"), "paper", test_reply=reply_path,
                                               test_news="unreachable")
            self.assertEqual(news.cfg["max_retries"], 3)
            news.cfg["max_retries"] = 0                                      # no real backoff sleeps in the test
            b = news.research(force=True)
            self.assertFalse(b.ok)
            self.assertIn("RemoteDisconnected", b.error)
        # live: make_brain returns the stage-1 researcher built from the "news" section (4 values)
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": KEY, "KIMI_HTTPS_PROXY": "http://127.0.0.1:1081"}):
            out = self.rb.make_brain(cfg, kc, self.sd, "live")
        self.assertEqual(len(out), 4)
        self.assertEqual((out[3].model, out[3].proxy, out[3].has_key), ("kimi-k2.6", "http://127.0.0.1:1081", True))
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": KEY}):
            self.assertIsNone(self.rb.make_brain(cfg, kimi_config(), self.sd, "live")[3])     # no news section

    def news_check(self, kimi, argv_extra=(), news_status=200, env_extra=None, decide_reply=None,
                   decide_finish="stop"):
        """kimi-check --news with fake transports for BOTH stages (no network). decide_reply: the
        stage-2 decision reply (default: a valid one)."""
        path = os.path.join(self.dir, "kimi_news.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kimi, f)
        calls = []

        def news_transport(method, url, headers, body, timeout):
            msgs = json.loads(body.decode("utf-8"))["messages"]
            calls.append(msgs)
            if news_status != 200:
                return news_status, b'{"error": {"message": "Invalid request: tokenization failed"}}'
            if not any(m.get("role") == "tool" for m in msgs):
                call = {"id": "c1", "type": "builtin_function",
                        "function": {"name": "$web_search", "arguments": '{"search_result": {"id": "s"}}'}}
                return 200, json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "", "tool_calls": [call]}}],
                    "usage": {"total_tokens": 1500}}).encode("utf-8")
            content = json.dumps({"items": [{"headline": "ETF inflows continue", "why_it_matters": "supportive",
                                             "source_url": "https://www.coindesk.com/x", "time_hint": "today"}],
                                  "summary": "mildly risk-on"})
            return 200, json.dumps({"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
                                                                                    "content": content}}],
                                    "usage": {"total_tokens": 7500}}).encode("utf-8")

        decision = decide_reply if decide_reply is not None else json.dumps(
            {"targets": {"USDT_IRT": 1.0}, "cash_irt": 0.0, "confidence": 0.5, "reasoning": "r",
             "news_summary": "n", "key_risks": "k", "next_review_hours": 2})

        class Models(FakeKimiTransport):
            def __call__(self, method, url, headers, body, timeout):
                self.calls.append((method, url))
                if url.endswith("/chat/completions"):       # the stage-2 decision check
                    self.chat_bodies.append(json.loads(body.decode("utf-8")))
                    return 200, json.dumps({"model": "kimi-k3", "choices": [
                        {"index": 0, "finish_reason": decide_finish,
                         "message": {"role": "assistant", "content": decision}}],
                        "usage": {"prompt_tokens": 14000, "completion_tokens": 900, "total_tokens": 14900}}
                    ).encode("utf-8")
                return 200, json.dumps({"data": [{"id": "kimi-k3"}, {"id": "kimi-k2.6"}]}).encode("utf-8")
        ok = Models()
        ok.chat_bodies = []
        with mock.patch.dict(os.environ, dict(env_extra or {})), \
                mock.patch("bitpin.llm.make_llm_transport", lambda proxy=None, opener=None: ok), \
                mock.patch("bitpin.news.make_news_transport", lambda proxy=None, opener=None: news_transport):
            code, text = self.run_main(["kimi-check", "--kimi-config", path, "--news"] + list(argv_extra))
        return code, text, calls, ok

    def test_kimi_check_news_reports_honestly(self):
        k = kimi_config()
        k["llm"] = dict(k["llm"], model="kimi-k3")
        k["brain"]["risk_profile"] = "full"
        k["news"] = {"model": "kimi-k2.6"}
        # a bot state dir whose news budget is used up and which holds a cached brief: research(force=True)
        # there would return the OLD brief with ok=True, stale=True and no request (a false OK)
        bot_sd = os.path.join(self.dir, "botstate")
        os.makedirs(bot_sd)
        now = __import__("time").time()
        day = __import__("time").strftime("%Y-%m-%d", __import__("time").gmtime(now))
        atomic_write_json(os.path.join(bot_sd, "news_budget.json"), {day: {"v": 2, "calls": 99, "total_tokens": 10}})
        atomic_write_json(os.path.join(bot_sd, "news_cache.json"), {"version": 2, "brief": NewsBrief(
            ok=True, text="Summary: OLD brief", fetched_at=now - 3600).to_dict(), "last_error": "", "last_error_at": None})
        before = read_dir(bot_sd)
        code, text, calls, ok = self.news_check(k, ["--state-dir", bot_sd])
        self.assertEqual(code, 0, text)
        self.assertEqual(len(calls), 2)                                 # a REAL (fake-transport) research call
        self.assertIn("NEWS: OK - 1 items, 1 searches", text)
        self.assertIn("ETF inflows continue", text)
        self.assertNotIn("OLD brief", text)
        self.assertIn("RESULT: OK - both stages were exercised for real", text)
        self.assertIn("decision model kimi-k3 answered a decision request", text)
        self.assertIn("news model kimi-k2.6 researched a fresh brief", text)
        self.assertIn("paper rehearsal", text)
        self.assertNotIn(KEY, text)
        self.assertEqual(read_dir(bot_sd), before)
        # stage 2 is checked with a REAL chat call, with the shipped settings, not only GET /models:
        # being listed says nothing about the timeout, the thinking length or the JSON format
        self.assertEqual(ok.calls, [("GET", "https://api.moonshot.ai/v1/models"),
                                    ("POST", "https://api.moonshot.ai/v1/chat/completions")])
        body = ok.chat_bodies[0]
        self.assertEqual(body["model"], "kimi-k3")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("temperature", body)                   # kimi-k3 must not get one
        self.assertNotIn("tools", body)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertIn("stage 2 OK", text)
        self.assertIn("the validator accepted the reply", text)
        # a stage-2 reply that the validator rejects fails the check (it used to pass on GET /models)
        code, text, _, _ = self.news_check(k, decide_reply="I cannot help with that")
        self.assertEqual(code, 1, text)
        self.assertIn("STAGE 2: FAILED", text)
        self.assertIn("RESULT: FAILED (decision)", text)
        self.assertNotIn("RESULT: OK", text)
        # an EMPTY reply cut off at max_tokens (kimi-k3 thinking past its budget) fails it too
        code, text, _, _ = self.news_check(k, decide_reply="", decide_finish="length")
        self.assertEqual(code, 1, text)
        self.assertIn("cut off at max_tokens", text)
        self.assertIn("raise llm.max_tokens", text)
        # the research fails (HTTP 400): no OK anywhere, exit 1
        code, text, calls, _ = self.news_check(k, news_status=400)
        self.assertEqual(code, 1, text)
        self.assertIn("NEWS: FAILED", text)
        self.assertIn("RESULT: FAILED (news)", text)
        self.assertNotIn("RESULT: OK", text)
        self.assertNotIn("NEWS: OK", text)
        # the news model is not offered to the key
        k2 = dict(k, news={"model": "kimi-k2.9"})
        code, text, calls, _ = self.news_check(k2)
        self.assertEqual((code, calls), (1, []), text)
        self.assertIn("not offered to this key", text)
        # no news section / news disabled: said so, exit 1, no request
        k3 = dict(k)
        del k3["news"]
        code, text, calls, _ = self.news_check(k3)
        self.assertEqual((code, calls), (1, []), text)
        self.assertIn("NEWS: not configured", text)
        self.assertNotIn("RESULT: OK", text)
        code, text, calls, _ = self.news_check(dict(k, news={"enabled": False}))
        self.assertEqual((code, calls), (1, []), text)
        self.assertIn("NEWS: disabled", text)

    def test_status_shows_what_the_kimi_brain_manages(self):
        bal = {"IRT": D("1000000000"), "BTC": D("0.01"), "DOGE": D("100")}

        class Budget(object):
            limit = 150

            def used(self):
                return 1

        class Client(object):
            auth_budget = Budget()

            def wallets(self, assets=None):
                return [{"asset": a, "balance": str(v), "frozen": "0", "service": "main"} for a, v in bal.items()]

            def tickers(self):
                return [{"symbol": "BTC_IRT", "price": "8000000000"}, {"symbol": "DOGE_IRT", "price": "20000"},
                        {"symbol": "USDT_IRT", "price": "100000"}]

            def open_orders(self):
                return []

        class Journal(object):
            def unresolved(self):
                return []

        class Broker(object):
            journal = Journal()

            def refresh(self):
                pass

            def balances(self):
                return dict(bal)

            available = balances

            def track(self, assets):
                pass
        self.rb.make_live = lambda *a, **k: (Client(), None, Broker())
        code, text = self.run_main(["status", "--config", self.cfg_path, "--kimi-config", self.kimi_path,
                                    "--state-dir", self.sd, "--no-confirm"])
        self.assertEqual(code, 0, text)
        self.assertIn("portfolio managed by the Kimi brain", text)
        for s in SYMS:
            self.assertIn(s, text)
        self.assertIn("not managed by the bot (never traded): DOGE", text)
        self.assertIn("bot sleeve: will be created with 500,000,000 IRT", text)
        # without --kimi-config: the configured strategy's universe (selected = hold_usdt: USDT_IRT only)
        code, text = self.run_main(["status", "--config", self.cfg_path, "--state-dir", self.sd, "--no-confirm"])
        self.assertEqual(code, 0, text)
        self.assertIn("not managed by the bot (never traded): BTC, DOGE", text)

    def test_brain_and_strategy_are_exclusive_and_the_kimi_config_is_required(self):
        code, _ = self.run_main(["paper", "--brain", "kimi", "--strategy", "hold_usdt", "--kimi-config", self.kimi_path,
                                 "--state-dir", self.sd, "--capital-irt", "1000000", "--once"])
        self.assertEqual(code, 78)
        code, _ = self.run_main(["paper", "--brain", "kimi", "--state-dir", self.sd, "--capital-irt", "1000000",
                                 "--once"])
        self.assertEqual(code, 78)


# --------------------------------------------------------------------------- final review 3 fixes

class MissingPriceTest(unittest.TestCase):
    """A held coin whose 1h candle endpoint returns an EMPTY list must never be valued at 0: that
    made plan_orders drop it out of the equity, the drawdown breaker halt the bot for the rest of the
    competition (exit 0, so systemd does not restart it), and the position unsellable until someone
    runs risk-reset."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_int_gap_")
        self.ex = Exchange()
        self.clock = Clock()
        self.brain = FakeBrain(self.clock, due=False)
        self.brain.limits = {"max_irt_cash": 1.0}          # risk_profile "full": no cash cap
        self.brain.fallback_cash_limit = lambda now=None: 0.05
        self.builder = FakeBuilder()
        # 95% BTC, 5% toman - allowed by risk_profile "full", which has no per-coin cap
        eq = D(CAPITAL)
        write_paper_account(self.dir, {"IRT": eq * D("0.05"), "BTC": eq * D("0.95") / D(repr(PX["BTC_IRT"]))})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def runner(self, missing=(), risk=None):
        def bars(symbol, res, start, end):
            return [] if symbol in missing else self.ex.bars(symbol, res, start, end)
        broker = PaperBroker(MARKETS, self.dir, fee_rate="0.0035", book_source=self.ex.book, clock=self.clock)
        rm = RiskManager(dict({"max_drawdown": 0.30, "min_order_irt": 100000}, **(risk or {})), self.dir, "paper",
                         clock=self.clock)
        r = Runner(None, broker, rm, RUNNER_CFG, self.dir, "paper", bars_source=bars, clock=self.clock,
                   sleep=self.clock.sleep, brain=self.brain, context_builder=self.builder)
        r.state.pop("last_bar_ts", None)
        return r, rm

    def next_bar(self):
        self.ex.add_bar(self.ex.series["USDT_IRT"][-1].ts + 3600)
        self.clock.t += 3600

    def test_a_missing_candle_feed_never_halts_the_bot_on_a_zero_valuation(self):
        r, rm = self.runner()
        ok = r.run_once()                                   # everything priced: the normal cycle
        self.assertEqual(ok["status"], "ok")
        self.assertAlmostEqual(float(ok["equity"]), float(D(CAPITAL)), delta=1)
        # the last close of every symbol is now in runner_state_paper.json, so a FRESH process (systemd
        # restarts the unit after every outage, and brain mode never seeds from data/*.csv) recovers it
        state = read_json(os.path.join(self.dir, "runner_state_paper.json"))
        self.assertIn("BTC_IRT", state["last_prices"])
        r2, rm2 = self.runner(missing={"BTC_IRT"})
        rep = r2.run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertAlmostEqual(float(rep["equity"]), float(D(CAPITAL)), delta=1)
        self.assertFalse(rm2.is_halted())

    def test_a_price_that_was_never_seen_stops_the_cycle_instead_of_halting(self):
        r, rm = self.runner(missing={"BTC_IRT"})            # no candle now and nothing remembered
        rep = r.run_once()
        self.assertEqual(rep["status"], "stale_prices")
        self.assertEqual(rep["stale_prices"], ["BTC_IRT"])
        self.assertFalse(rm.is_halted())
        self.assertEqual(rep["fills"], [])
        # the high-water mark must not move either: the equity is a lower bound, not a number
        self.assertEqual(float(rm.high_water_mark("account")), 0.0)
        self.assertEqual(runner_log(self.dir)[0]["stale_prices"], ["BTC_IRT"])
        self.assertIn("NO PRICE for held BTC_IRT", format_report(rep))
        # and it recovers by itself as soon as the feed is back - no risk-reset needed
        self.next_bar()
        r2, rm2 = self.runner()
        self.assertEqual(r2.run_once()["status"], "ok")
        self.assertFalse(rm2.is_halted())


class BrainLogRedactionTest(BrainRunnerBase):
    """kimi_runner.jsonl carries brain_error (the text of any exception from the brain path) and
    news.error, so it needs the same redaction as the brain's own kimi_decisions.jsonl."""

    def test_the_runner_audit_log_is_redacted(self):
        secret = "http://user:hunter2@127.0.0.1:1081"
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 1.0, "BTC_IRT": 0.0, "ETH_IRT": 0.0})]
        self.brain.raise_in_decide = RuntimeError("proxy %s refused: Bearer sk-abcdefghijklmnop" % secret)
        rep = self.runner().run_once()
        self.assertIn("brain_error", rep["brain"])
        with open(os.path.join(self.dir, BRAIN_RUNNER_LOG), encoding="utf-8") as f:
            raw = f.read()
        for leak in ("hunter2", "sk-abcdefghijklmnop"):
            self.assertNotIn(leak, raw)
        self.assertIn("RuntimeError", raw)          # the diagnosis itself is kept


class DecisionTimeTest(BrainRunnerBase):
    """The decision is stamped with the CYCLE time, not the clock after the news research and the
    context build: otherwise decided_at drifts with the work time, the next hourly check falls just
    short of decision_interval_hours and every second decision is silently dropped."""

    def test_decide_is_given_the_cycle_time(self):
        self.brain.next = [FakeDecision(targets={"USDT_IRT": 1.0, "BTC_IRT": 0.0, "ETH_IRT": 0.0})]
        r = self.runner()
        rep = r.run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(self.brain.decide_calls[0]["now"], NOW)

    def test_a_slipped_schedule_is_logged(self):
        self.brain.due = False
        self.brain.last_decision = FakeDecision(targets={}, decided_at=NOW - 3600 + 60)
        self.brain.cfg = dict(self.brain.cfg, decision_interval_hours=1)
        with self.assertLogs("bitpin.runner", level="WARNING") as cm:
            self.runner().run_once()
        self.assertTrue([ln for ln in cm.output if "schedule SLIPPED" in ln], cm.output)


class DriftLegsTest(BrainRunnerBase):
    """DECISION_DRIFT_LIMIT (0.05) is larger than MIN_ORDER_HEADROOM (0.03), so an accepted decision
    can still contain legs that are no longer executable. They must be named once, loudly, not
    buried in rep["skipped"]."""

    def test_legs_killed_by_the_drift_are_reported(self):
        self.assertGreater(0.05, MIN_ORDER_HEADROOM)     # the gap this test exists for
        # the decision was computed against BTC 0.00, but the bot already holds BTC 0.03
        eq = D(CAPITAL)
        write_paper_account(self.dir, {"IRT": eq * D("0.97"), "BTC": eq * D("0.03") / D(repr(PX["BTC_IRT"]))})
        d = FakeDecision(targets={"USDT_IRT": 0.0, "BTC_IRT": 0.045, "ETH_IRT": 0.0})
        d.computed_against = {}                       # "it was 100% toman when I decided"
        self.brain.next = [d]
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "kimi")
        lost = rep["brain"].get("unexecutable")
        self.assertTrue(lost, rep["brain"])
        self.assertIn("BTC_IRT", lost[0])
        self.assertEqual(runner_log(self.dir)[0]["unexecutable"], lost)
        self.assertIn("no longer executable after the portfolio moved", format_report(rep))


class FrozenFundsTest(BrainRunnerBase):
    """Sizing keeps using the TOTAL balances (using the available ones would make the bot's own
    pending order look like a loss to the drawdown breaker), but execution can only spend the
    available ones, so a total-vs-available gap has to be visible to the operator."""

    def test_locked_funds_are_reported(self):
        self.brain.due = False
        r = self.runner()
        r.broker.refresh()
        total = dict(r.broker.balances())
        avail = dict(total)
        avail["IRT"] = total["IRT"] / 2                       # half the toman sits in a manual order
        r.broker.available = lambda: dict(avail)
        rep = r.run_once()
        self.assertIn("frozen", rep)
        self.assertEqual(sorted(rep["frozen"]), ["IRT"])
        self.assertEqual(runner_log(self.dir)[0]["frozen"], rep["frozen"])
        self.assertIn("LOCKED in an order the bot did not place: IRT", format_report(rep))
        # the equity is still the whole account: a pending order must not trip the breaker
        self.assertAlmostEqual(float(rep["equity"]), float(D(CAPITAL)), delta=1)


# --------------------------------------------------------------------------- deploy kit

class DeployKitTest(unittest.TestCase):
    def read(self, *p):
        with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
            return f.read()

    def exec_start(self, unit):
        lines = [ln for ln in self.read("deploy", unit).splitlines() if ln.startswith("ExecStart=")]
        self.assertEqual(len(lines), 1)
        return lines[0][len("ExecStart="):]

    # ---- final review 3

    @staticmethod
    def _bash():
        """A bash that can read this checkout. On Windows, WSL's /bin/bash cannot see a C:\\ path, so
        Git Bash is tried first and the test is skipped when neither works."""
        probe = "test -f '%s/deploy/lib.sh'" % ROOT.replace("\\", "/")
        pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        for cand in (os.path.join(pf, "Git", "bin", "bash.exe"), os.path.join(pf, "Git", "usr", "bin", "bash.exe"),
                     shutil.which("bash"), "/bin/bash"):
            if not cand or not os.path.exists(cand):
                continue
            try:
                ok = subprocess.call([cand, "-c", probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError:
                continue
            if ok == 0:
                return cand
        return None

    def run_lib(self, script, systemctl):
        """Run `script` against the REAL deploy/lib.sh, with `systemctl` (a shell function body)
        standing in for the real one. A function beats any binary on PATH, so this works the same on
        Windows and on the server."""
        sh = self._bash()
        if sh is None:
            self.skipTest("no bash that can read this checkout")
        body = "systemctl() {\n%s\n}\n. '%s/deploy/lib.sh'\n%s\n" % (systemctl, ROOT.replace("\\", "/"), script)
        proc = subprocess.Popen([sh, "-c", body], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.communicate()[0]
        return proc.returncode, out.decode("utf-8", "replace")

    def test_running_units_sees_a_unit_in_its_restart_backoff(self):
        """`systemctl is-active --quiet` is non-zero for "activating (auto-restart)", which is where a
        failing unit spends almost all of its up-to-15-minute backoff - the state health reports as
        RESTART LOOP. Both installers then believed nothing was running: install.sh bypassed its "use
        update.sh" guard and update.sh skipped the live-confirmation check, swapped the tree under the
        unit and exited 0 while systemd started the new code minutes later with no rollback."""
        fake = ('if [ "$1" = "is-active" ]; then return 3; fi\n'          # like a unit in auto-restart
                'if [ "$1" = "show" ]; then echo "activating"; return 0; fi\n'
                'return 0')
        code, out = self.run_lib('echo "[$(running_units)]"', fake)
        self.assertEqual(code, 0, out)
        self.assertIn("bitpin-bot.service", out)
        self.assertIn("bitpin-bot-paper.service", out)

    def test_running_units_is_empty_only_when_the_units_are_really_inactive(self):
        fake = 'if [ "$1" = "show" ]; then echo "inactive"; return 0; fi\nreturn 3'
        code, out = self.run_lib('echo "[$(running_units)]"', fake)
        self.assertEqual(code, 0, out)
        self.assertIn("[]", out)

    def test_running_units_matches_the_helpers_own_live_check(self):
        """deploy/bitpin-bot refuses to share the live bot's token for exactly these states; the
        installers must use the same list, or the kit contradicts itself."""
        states = ["active", "activating", "reloading", "deactivating"]
        self.assertIn("|".join(states) + ")", self.read("deploy", "bitpin-bot"))
        for st in states:
            with self.subTest(state=st):
                fake = 'if [ "$1" = "show" ]; then echo "%s"; return 0; fi\nreturn 3' % st
                _, out = self.run_lib('echo "[$(running_units)]"', fake)
                self.assertIn("bitpin-bot.service", out)

    def test_update_refuses_to_leave_code_the_units_cannot_start(self):
        """Step 6 tested `[ -z "$running" ]` first, so a new version whose run_bot.py lost a flag the
        units pass was left installed with only a WARN line and update.sh exited 0. argparse then
        exits 2, which RestartPreventExitStatus=0 78 does not cover."""
        upd = self.read("deploy", "update.sh")
        flags = upd.index('check_cli_flags || flags_ok=0')
        check = upd.index('if [ "$flags_ok" = 0 ]; then')
        nothing_ran = upd.index('if [ -z "$running" ]; then\n        info "services were not running')
        self.assertLess(flags, check)
        self.assertLess(check, nothing_ran)

    def test_health_exits_nonzero_when_the_order_budget_is_nearly_used_up(self):
        """DEPLOY_FA tells the operator that the last line must be RESULT: OK, so a warning that
        does not change the exit status can be scrolled past. Under risk_profile "full" 180 orders /
        24 h is reachable, and after that whole rebalances are skipped."""
        helper = self.read("deploy", "bitpin-bot")
        self.assertIn("sys.exit(1 if low else 0)", helper)
        self.assertNotIn('"$ETC_DIR/config.json" <<\'PY\' || true', helper)
        # the embedded checker itself: 176 of 180 used -> exit 1, 100 of 180 -> exit 0
        end = helper.index("sys.exit(1 if low else 0)")
        code = helper[helper.rindex("import json, sys, time", 0, end):end] + "sys.exit(1 if low else 0)"
        d = tempfile.mkdtemp(prefix="bitpin_budget_")
        self.addCleanup(shutil.rmtree, d, True)
        script = os.path.join(d, "check.py")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"risk": {"max_orders_per_day": 180}}, f)
        now = time.time()
        for used, want in ((176, 1), (100, 0)):
            state = os.path.join(d, "risk_%d.json" % used)
            with open(state, "w", encoding="utf-8") as f:
                json.dump({"orders": [now - 60] * used}, f)
            p = subprocess.run([sys.executable, script, state, cfg], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            self.assertEqual(p.returncode, want, p.stdout)

    def test_purge_only_deletes_a_user_this_installer_created(self):
        """ensure_user deliberately ADOPTS a pre-existing nologin account named bitpin (a shared
        server may run other services), so an unconditional userdel would orphan their files."""
        lib, un = self.read("deploy", "lib.sh"), self.read("deploy", "uninstall.sh")
        self.assertIn(".user-created-by-installer", lib)
        self.assertIn(".user-created-by-installer", un)
        # the marker is written ONLY in the branch that ran useradd
        made = lib.index("useradd --system")
        adopted = lib.index('ok "system user $APP_USER exists"')
        marker = lib.index(".user-created-by-installer")
        self.assertLess(made, marker)
        self.assertLess(marker, adopted)
        self.assertIn("was NOT created by this installer", un)

    def test_paper_unit_survives_an_emptied_capital_variable(self):
        """systemd expands an empty variable in ExecStart to an empty ARGUMENT instead of dropping
        it, and bitpin-bot.env is read after the unit's own Environment= default. The example file
        ships the line commented out, so a user is likely to uncomment and clear it."""
        self.assertIn("--capital-irt ${PAPER_CAPITAL_IRT}", self.exec_start("bitpin-bot-paper.service"))
        rb = load_run_bot()
        self.assertIsNone(rb.optional_decimal(""))
        self.assertIsNone(rb.optional_decimal("   "))
        self.assertEqual(rb.optional_decimal("5000000"), D("5000000"))

    def test_live_unit_runs_the_kimi_brain_non_interactively(self):
        self.assertEqual(self.exec_start("bitpin-bot.service"),
                         "/usr/bin/python3 /opt/bitpin-bot/scripts/run_bot.py live --brain kimi --config "
                         "/etc/bitpin-bot/config.json --kimi-config /etc/bitpin-bot/kimi.json --state-dir "
                         "/var/lib/bitpin-bot --i-accept-the-risk --non-interactive")

    def test_paper_unit_and_both_parse_with_the_cli(self):
        paper = self.exec_start("bitpin-bot-paper.service")
        self.assertIn("run_bot.py paper --brain kimi --config /etc/bitpin-bot/config.json --kimi-config "
                      "/etc/bitpin-bot/kimi.json --state-dir /var/lib/bitpin-bot-paper --capital-irt", paper)
        rb = load_run_bot()
        for unit in ("bitpin-bot.service", "bitpin-bot-paper.service"):
            argv = self.exec_start(unit).replace("${PAPER_CAPITAL_IRT}", "100000000").split()[2:]
            seen = {}
            with mock.patch.object(rb, "cmd_live", lambda a: seen.setdefault("args", a) and 0), \
                    mock.patch.object(rb, "cmd_paper", lambda a: seen.setdefault("args", a) and 0):
                rb.main(argv)
            self.assertEqual(seen["args"].brain, "kimi")

    def test_health_does_not_report_a_restart_loop_as_ok(self):
        """Regression: `health` printed RESULT: OK (exit 0) for a unit stuck in systemd's auto-restart
        loop, which is the state a daily check actually observes (RestartSec grows to 15 min), and for
        an enabled unit that had never written a decision."""
        h = self.read("deploy", "bitpin-bot")
        self.assertIn('sub="$(systemctl show -p SubState --value "$u"', h)
        self.assertIn('if [ "$sub" = "auto-restart" ]; then', h)
        self.assertIn("RESTART LOOP", h)
        # the "no valid decision for 6 h" alarm covers activating units too, not only active ones
        self.assertIn('if { [ "$state" = "active" ] || [ "$state" = "activating" ]; }', h)
        # an enabled unit that has been up and has never logged a decision is not silently OK
        self.assertIn("unit_uptime_minutes", h)
        self.assertIn("has never logged a Kimi decision", h)
        # the daily order budget is surfaced (nothing else on that screen would show it)
        self.assertIn("orders in the last 24 h", h)
        self.assertIn("max_orders_per_day", h)

    def test_run_escape_hatch_always_names_a_state_dir(self):
        """Regression: `bitpin-bot run ...` without --state-dir fell back to config.json's relative
        "state", which resolves against the root-owned /opt/bitpin-bot."""
        h = self.read("deploy", "bitpin-bot")
        self.assertIn('--state-dir|--state-dir=*) has_sd=1', h)
        self.assertIn('as_bot "$PYTHON" "$RUN_BOT" "$@" --state-dir "$run_sd"', h)
        self.assertIn('run_sd="$PAPER_STATE_DIR"', h)      # "run paper ..." never touches the live state

    def test_update_prunes_failed_copies_and_the_kit_never_ships_the_key_tool(self):
        """A failed update leaves $APP_DIR.failed-<stamp>; nothing used to delete those. And
        scripts/kimi_check.py would prompt for the Kimi key and write it in cleartext next to itself,
        a second secret location the deploy kit says does not exist."""
        upd = self.read("deploy", "update.sh")
        self.assertIn('"$APP_DIR".failed-*', upd)
        self.assertIn("removed old failed-update copy", upd)
        lib = self.read("deploy", "lib.sh")
        self.assertIn("--exclude='./scripts/kimi_check.py'", lib)
        # update.sh runs this suite inside the STAGED copy, which leaves the tool out on purpose: there
        # only its absence can be checked (reading it failed the test step and blocked every update).
        if not os.path.exists(os.path.join(ROOT, "scripts", "kimi_check.py")):
            return
        tool = self.read("scripts", "kimi_check.py")
        for gone in ("kimi.key", "getpass", "O_CREAT", "--reset-key"):
            self.assertNotIn(gone, tool)
        self.assertIn("never asks for - or stores - the key", tool)

    def test_update_checks_the_live_confirmation_with_the_services_files(self):
        upd = self.read("deploy", "update.sh")
        live = self.exec_start("bitpin-bot.service")
        self.assertIn("confirm-live --check", upd)
        for opt in ('--config "$ETC_DIR/config.json"', '--kimi-config "$ETC_DIR/kimi.json"',
                    '--state-dir "$STATE_DIR"'):
            self.assertIn(opt, upd)
        for path in ("/etc/bitpin-bot/config.json", "/etc/bitpin-bot/kimi.json", "--state-dir /var/lib/bitpin-bot "):
            self.assertIn(path, live + " ")
        helper = self.read("deploy", "bitpin-bot")
        self.assertIn('confirm-live --config "$ETC_DIR/config.json"', helper)

    def test_persian_guide_matches_the_kit_and_the_server_rules(self):
        doc = self.read("docs", "DEPLOY_FA.md")
        for ip in ("SERVER_IP", "Public IP"):                          # the whitelist uses the server's own IP
            self.assertIn(ip, doc)
        self.assertIn(self.exec_start("bitpin-bot.service"), doc)        # the exact service command
        for must in ("KIMI_HTTPS_PROXY=http://127.0.0.1:", "kimi-check", "confirm-live", "risk_profile",
                     "systemctl enable --now bitpin-bot", "bitpin-bot stop", "update.sh", "derisk_after_hours",
                     "Ubuntu 24.04", "journalctl -u bitpin-bot",
                     # the two-stage design and the account settings
                     "KIMI_HTTPS_PROXY=http://127.0.0.1:1081", "kimi-check --news", "NEWS: OK", "kimi-k3", "kimi-k2.6",
                     "equity_start_irt", "sweep_irt_above", '"full"', "bitpin-bot-paper", "RemoteDisconnected", "STALE",
                     # the final review: the real restart behaviour, the raw JSON log, the order budget
                     "۱۵ دقیقه", "RESTART LOOP", "max_orders_per_day", "rebalance skipped: daily order budget",
                     "json.dumps(json.loads(l)"):
            self.assertIn(must, doc)
        # the install order: upload -> install.sh -> env file -> status -> kimi-check --news ->
        # optional paper run -> confirm-live -> systemctl enable --now
        steps = ["## گام ۱.", "sudo bash deploy/install.sh", "sudo nano /etc/bitpin-bot/bitpin-bot.env",
                 "sudo bitpin-bot status", "sudo bitpin-bot kimi-check --news", "sudo systemctl start bitpin-bot-paper",
                 "sudo bitpin-bot confirm-live", "sudo systemctl enable --now bitpin-bot"]
        pos = [doc.index(s) for s in steps]
        self.assertEqual(pos, sorted(pos), list(zip(steps, pos)))
        # every command the guide gives exists in the kit
        helper = self.read("deploy", "bitpin-bot")
        for cmd in ("check)", "status)", "kimi-check)", "confirm-live)", "health)", "stop|resume)", "logs)",
                    "decisions)", "run)"):
            self.assertIn(cmd, helper)
        low = doc.lower()
        # the only proxy instruction is KIMI_HTTPS_PROXY; nothing about VPNs / tunnels, and nothing that
        # changes the shared server (firewall, SSH, time settings)
        for word in ("vmess", "xray", "v2ray", "tunnel", "tinyproxy", "vpn", "ssh -n", "no_proxy=",
                     "ufw enable", "ufw allow", "sshd_config", "set-ntp", "تونل",
                     "فیلترشکن"):
            self.assertNotIn(word, low)
        self.assertNotIn("HTTPS_PROXY=http", doc.replace("KIMI_HTTPS_PROXY=http", ""))
        for unit_ref in ("bitpin-proxy-tunnel",):
            self.assertNotIn(unit_ref, doc)
            self.assertFalse(os.path.exists(os.path.join(ROOT, "deploy", unit_ref + ".service")))

    def test_env_template_has_only_the_kimi_proxy_setting(self):
        """This server reaches Moonshot only through the local HTTP proxy on 127.0.0.1:1081: the shipped
        env file (a fresh install copies it) sets exactly that one proxy line, and nothing else routes
        traffic (no NO_PROXY, no system-wide proxy variables, no VPN/tunnel instructions)."""
        env = self.read("deploy", "bitpin-bot.env.example")
        active = [ln for ln in env.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertEqual([ln for ln in active if "PROXY" in ln.upper()], ["KIMI_HTTPS_PROXY=http://127.0.0.1:1081"])
        self.assertEqual(sorted(ln.split("=")[0] for ln in active),
                         ["BITPIN_API_KEY", "BITPIN_SECRET_KEY", "KIMI_API_KEY", "KIMI_HTTPS_PROXY"])
        low = env.lower().replace("kimi_https_proxy=", "")
        for word in ("https_proxy=http", "no_proxy=", "vmess", "xray", "v2ray", "tunnel", "vpn"):
            self.assertNotIn(word, low)
        # the bot reads it like systemd does: KIMI_HTTPS_PROXY is a valid proxy URL for LLMClient
        from bitpin.llm import parse_proxy_url
        self.assertEqual(parse_proxy_url(active[-1].split("=", 1)[1]), "http://127.0.0.1:1081")


if __name__ == "__main__":
    unittest.main()
