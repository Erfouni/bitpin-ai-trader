"""Crash ladder, code exits, COIN_USDT routing, watchdog wiring, endgame and the event log of the
runner's brain mode (bitpin/runner.py; spec: scratch/freq/final_recommendation.json).

No network and no real credentials: a fake exchange (hourly candles, order books that follow the
price, COIN_USDT markets), the PaperBroker (resting limit orders fill when the book touches them),
a fake clock, a contract double of KimiBrain for the runner mechanics, and the REAL KimiBrain with a
fake Moonshot transport for the schedule / wake-up / endgame integration.
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin.brain import Decision, build_kimi  # noqa: E402
from bitpin.broker import PaperBroker  # noqa: E402
from bitpin.data import Bar  # noqa: E402
from bitpin.markets import ZERO, D, Market, MarketCache  # noqa: E402
from bitpin.risk import KILL_SWITCH_FILE, RiskManager  # noqa: E402
from bitpin.runner import (EVENTS_LOG, LADDER_TAG, TARGET_TAG, Runner, RunnerError, format_report,  # noqa: E402
                           ladder_plan, load_config, lvl_key, needs_replace)

logging.getLogger("bitpin").addHandler(logging.NullHandler())

TEHRAN = timezone(timedelta(hours=3, minutes=30))
LAST_CLOSED = 1790065800          # a real Bitpin 1h bar open (hh:30 UTC) = 2026-09-22 12:00 Tehran
NOW = LAST_CLOSED + 3600 + 90     # 13:01:30 Tehran, 90 s after that bar closed
KEY = "sk-LADDERTESTKEY0123456789"
COINS = ["BTC", "ETH", "XRP", "SOL"]
IRT_SYMS = ["USDT_IRT"] + ["%s_IRT" % c for c in COINS]
USDT_SYMS = ["%s_USDT" % c for c in COINS]
U = 100000.0                                  # toman per USDT
PX_USDT = {"BTC": 80000.0, "ETH": 3000.0, "XRP": 2.0, "SOL": 150.0}
CAPITAL = "1000000000"                        # 1e9 toman = 10,000 USDT
MARKETS = MarketCache(markets=(
    [Market("USDT_IRT", "USDT", "IRT", True, False, 0, 2, 0)]
    + [Market("%s_IRT" % c, c, "IRT", True, False, 0, 8, 0) for c in COINS]
    + [Market("%s_USDT" % c, c, "USDT", True, False, 6, 8, 6) for c in COINS]))
RUNNER_CFG = {"history_csv_seed": False, "history_start_ts": None, "wake_delay_seconds": 0, "data_retry_seconds": 1,
              "error_backoff_seconds": 5}


def tehran(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TEHRAN).timestamp()


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Exchange(object):
    """Hourly candles of the IRT markets (plus the forming bar), order books of every market that
    follow the price (COIN_USDT = COIN_IRT / USDT_IRT), tickers for the context builder."""

    def __init__(self, n=200):
        self.usdt = U
        self.coin = dict(PX_USDT)          # USDT price of each coin
        self.series = {s: [] for s in IRT_SYMS}
        self.book_override = {}
        self.depth = {}                     # symbol -> base amount per level
        self.book_calls = []
        start = LAST_CLOSED - (n - 1) * 3600
        for i in range(n):
            self._bar(start + i * 3600)

    def irt(self, sym):
        if sym == "USDT_IRT":
            return self.usdt
        return self.coin[sym.split("_")[0]] * self.usdt

    def _bar(self, ts, low=None):
        for s in IRT_SYMS:
            p = self.irt(s)
            lo = p * 0.999 if low is None or s not in low else low[s]
            self.series[s].append(Bar(ts, p, p * 1.001, lo, p, 10.0))

    def next_bar(self, low=None):
        self._bar(self.series["USDT_IRT"][-1].ts + 3600, low)

    def set(self, coin=None, usdt=None, **coins):
        if usdt is not None:
            self.usdt = usdt
        self.coin.update(coins)

    def bars(self, symbol, res, start, end):
        if symbol not in self.series:
            return []
        out = [b for b in self.series[symbol] if start <= b.ts <= end]
        forming = self.series[symbol][-1].ts + 3600
        if forming <= end:
            p = self.irt(symbol)
            out.append(Bar(forming, p, p, p, p, 1.0))
        return out

    def price(self, symbol):
        base, quote = symbol.split("_")
        if quote == "IRT":
            return self.irt(symbol)
        return self.coin[base]

    def book(self, symbol):
        self.book_calls.append(symbol)
        if symbol in self.book_override:
            return self.book_override[symbol]
        p = self.price(symbol)
        size = self.depth.get(symbol, {"USDT_IRT": "1000000", "BTC_IRT": "100", "ETH_IRT": "1000", "XRP_IRT": "1e6",
                                       "SOL_IRT": "10000", "BTC_USDT": "100", "ETH_USDT": "1000",
                                       "XRP_USDT": "1e6", "SOL_USDT": "10000"}[symbol])
        return {"asks": [[repr(p * (1 + 0.0002 * k)), size] for k in range(1, 6)],
                "bids": [[repr(p * (1 - 0.0002 * k)), size] for k in range(1, 6)]}

    # the public client of the context builder
    def tickers(self):
        return [{"symbol": s, "price": repr(self.irt(s))} for s in IRT_SYMS]

    def orderbook(self, symbol):
        return self.book(symbol)

    def matches(self, symbol):
        return []


def write_paper_account(state_dir, balances):
    from bitpin.api import atomic_write_json
    atomic_write_json(os.path.join(state_dir, PaperBroker.STATE_FILE), {
        "version": 1, "mode": "paper", "created_utc": "2026-09-22T00:00:00Z", "updated_utc": "2026-09-22T00:00:00Z",
        "initial_capital_irt": D(CAPITAL), "seq": 0, "orders": {},
        "balances": {k: D(v) for k, v in balances.items()}})


def read_events(state_dir):
    p = os.path.join(state_dir, EVENTS_LOG)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


# --------------------------------------------------------------------------- contract double of KimiBrain

class FakeDecision(Decision):
    pass


class LadderBrain(object):
    """The KimiBrain surface the runner uses, WITH the ladder contract (ladder_in_force, endgame_flags,
    news_request, pop_notifications, last_mode / last_events / last_trigger_kind)."""

    def __init__(self, clock, due=False):
        self.allowed = list(IRT_SYMS)
        self.limits = {"max_irt_cash": 1.0}
        self.cfg = {"risk_profile": "full", "decision_interval_hours": 24, "held_move_pct": 8.0,
                    "fallback": {"derisk_after_hours": 48}}
        self.ladder_coins = list(COINS)
        self.clock = clock
        self.due = due
        self.next = []
        self.last_decision = None
        self.last_trigger = self.last_trigger_kind = self.last_mode = None
        self.last_events = []
        self.pacing_block = None
        self.origin = ""
        self.scales = None
        self.no_new_entries_at = tehran(2026, 10, 17, 13)
        self.final_at = tehran(2026, 10, 21, 13)
        self.should_args = []
        self.decide_args = []
        self.fallback_args = []
        self.notes = []
        self.news_req = {"force": False, "focus": None}
        self.mode_next = "scheduled"
        self.kind_next = "scheduled"
        self.events_next = []
        self.clock_schedule = False

    def should_decide(self, now, last, quick, slack_seconds=0.0, ladder=None, positions=None):
        self.should_args.append({"now": now, "ladder": json.loads(json.dumps(ladder)) if ladder else ladder,
                                 "positions": json.loads(json.dumps(positions)) if positions else positions})
        due = self.due if not callable(self.due) else self.due(now, ladder, positions)
        self.last_trigger = "test trigger" if due else None
        self.last_trigger_kind = self.kind_next if due else None
        self.last_mode = self.mode_next if due else None
        self.last_events = list(self.events_next) if due else []
        return bool(due)

    def recent_decisions(self):
        return []

    def news_request(self):
        return dict(self.news_req)

    def pop_notifications(self):
        out, self.notes = self.notes, []
        return out

    def endgame_flags(self, now=None):
        now = self.clock() if now is None else now
        return {"active": True, "no_new_entries": now >= self.no_new_entries_at, "final": now >= self.final_at,
                "no_new_entries_at": self.no_new_entries_at, "final_at": self.final_at,
                "max_hold_cap_at": self.final_at, "end_at": self.final_at + 36 * 3600}

    def ladder_in_force(self, now=None):
        if self.endgame_flags(now)["no_new_entries"]:
            return {c: 0.0 for c in self.ladder_coins}
        return {c: (self.scales or {}).get(c, 1.0) for c in self.ladder_coins}

    def fallback_cash_limit(self, now=None):
        return 1.0

    def decide(self, ctx, cur, trigger=None, abort=None, min_trade_weight=None, order_budget=None, now=None,
               news=None, mode=None, ladder=None, positions=None):
        self.decide_args.append({"cur": dict(cur), "mode": mode, "ladder": ladder, "positions": positions,
                                 "news": news, "ctx": ctx})
        d = self.next.pop(0)
        d.decided_at = self.clock()
        d.expires_at = d.decided_at + 3600
        if not d.computed_against:
            d.computed_against = dict(cur)
        d.computed_cash = max(0.0, 1.0 - sum(cur.values()))
        d.mode = mode or d.mode
        self.last_decision = d
        return d

    def fallback_decision(self, current_weights, now, balances_irt_value=None, min_trade_weight=None,
                          positions=None):
        self.fallback_args.append({"cur": dict(current_weights), "positions": positions})
        return None


class FakeBuilder(object):
    def __init__(self):
        self.builds = []

    def quick_context(self, snap):
        return {"quick": True}

    def build(self, snap, recent_decisions=None, abort=None, **kw):
        self.builds.append(kw)
        return {"portfolio": snap}


def decision(targets, **kw):
    d = FakeDecision(valid=True, targets=dict(targets), confidence=0.8, origin="")
    for k, v in kw.items():
        setattr(d, k, v)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_ladder_")
        self.ex = Exchange()
        self.clock = Clock()
        self.brain = LadderBrain(self.clock)
        self.builder = FakeBuilder()
        self.balances = {"USDT": "10000"}          # 1e9 toman, all USDT (like the live account)
        write_paper_account(self.dir, self.balances)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def runner(self, cfg=None, risk=None, dry_run=False, brain=None):
        self.broker = PaperBroker(MARKETS, self.dir, book_source=self.ex.book, clock=self.clock,
                                  persist=not dry_run)
        self.rm = RiskManager(dict({"min_order_irt": 100000}, **(risk or {})), self.dir, "paper", clock=self.clock,
                              persist=not dry_run)
        c = dict(RUNNER_CFG)
        c.update(cfg or {})
        return Runner(None, self.broker, self.rm, c, self.dir, "paper", dry_run=dry_run, bars_source=self.ex.bars,
                      clock=self.clock, sleep=self.clock.sleep, brain=brain or self.brain,
                      context_builder=self.builder)

    def hour(self, n=1, low=None):
        for _ in range(n):
            self.ex.next_bar(low)
            self.clock.t += 3600

    def bids(self, symbol=None, broker=None):
        b = broker or PaperBroker(MARKETS, self.dir, book_source=self.ex.book, clock=self.clock)
        return b.limit_orders(symbol=symbol, tag=LADDER_TAG, active_only=True)

    def targets(self, broker=None):
        b = broker or PaperBroker(MARKETS, self.dir, book_source=self.ex.book, clock=self.clock)
        return b.limit_orders(tag=TARGET_TAG, active_only=True)

    def bal(self):
        return PaperBroker(MARKETS, self.dir, book_source=self.ex.book, clock=self.clock).balances()

    def avail(self):
        return PaperBroker(MARKETS, self.dir, book_source=self.ex.book, clock=self.clock).available()

    def set_stop(self, sym="BTC_IRT", stop_pct=12.0, cfg=None, runner=None):
        """v3 has NO default stop: a code stop exists only where Kimi set stop_pct for the position. The
        tests of the stop path give the position one the way the live bot gets it - an hour later a HOLD
        decision whose exits name the coin (the runner applies the exits of every valid decision to the
        coin's position: the crash-ladder position when that is the coin's only one)."""
        self.hour()
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 1.0}, hold=True, exits={sym: {"stop_pct": stop_pct}})]
        rep = (runner or self.runner)(cfg).run_once()
        self.brain.due = False
        self.assertEqual(rep["brain"]["action"], "hold", rep["brain"])
        return rep


# --------------------------------------------------------------------------- pure helpers and config

class HelperTest(unittest.TestCase):
    def test_ladder_plan_sizes_and_pro_rata(self):
        refs = {c: {"hi48": PX_USDT[c]} for c in COINS}
        scales = {c: 1.0 for c in COINS}
        plan, f = ladder_plan(refs, scales, {}, [-20.0, -25.0], 0.125, 1e9, U, 10000, 0.5)
        self.assertEqual((len(plan), f), (8, 1.0))
        self.assertAlmostEqual(plan[("BTC", "-20")]["price_usdt"], 64000.0)
        self.assertAlmostEqual(plan[("BTC", "-25")]["price_usdt"], 60000.0)
        self.assertTrue(all(abs(w["usdt"] - 1250.0) < 1e-6 for w in plan.values()))
        # the book holds only half the USDT the full ladder needs: every bid is halved
        plan, f = ladder_plan(refs, scales, {}, [-20.0, -25.0], 0.125, 1e9, U, 5000, 0.5)
        self.assertAlmostEqual(f, 0.5)
        self.assertTrue(all(abs(w["usdt"] - 625.0) < 1e-6 for w in plan.values()))
        # scale 0 = off, a disarmed level is skipped, a bid below the minimum is dropped
        plan, f = ladder_plan(refs, dict(scales, BTC=0.0, SOL=0.5), {("ETH", "-25"): False}, [-20.0, -25.0], 0.125,
                              1e9, U, 10000, 700)
        self.assertNotIn(("BTC", "-20"), plan)
        self.assertNotIn(("ETH", "-25"), plan)
        self.assertNotIn(("SOL", "-20"), plan)          # 625 USDT < 700 minimum
        self.assertEqual(ladder_plan(refs, scales, {}, [-20.0], 0.125, 1e9, 0, 1e9, 0.5), ({}, 0.0))

    def test_hysteresis(self):
        self.assertFalse(needs_replace(64000, 0.0195, 64300, 0.0195, 0.5, 10))     # +0.47%
        self.assertTrue(needs_replace(64000, 0.0195, 64400, 0.0195, 0.5, 10))      # +0.63%
        self.assertFalse(needs_replace(64000, 0.0195, 64000, 0.0213, 0.5, 10))     # +9.2% size
        self.assertTrue(needs_replace(64000, 0.0195, 64000, 0.0216, 0.5, 10))      # +10.8% size
        self.assertTrue(needs_replace(0, 0.0195, 64000, 0.0195, 0.5, 10))
        self.assertEqual(lvl_key(-20.0), "-20")
        self.assertEqual(lvl_key(D("-25.0")), "-25")

    def test_config_sections_are_validated(self):
        cfg = load_config(None, {"ladder": {"levels_pct": [20, -25], "coins": ["btc", "ETH_USDT"]}})
        self.assertEqual(cfg["ladder"]["levels_pct"], [-20.0, -25.0])
        self.assertEqual(cfg["ladder"]["coins"], ["BTC", "ETH"])
        self.assertEqual(cfg["exits"]["enabled"], True)
        self.assertEqual(load_config()["ladder"]["levels_pct"], [-20.0, -25.0])     # defaults never mutated
        for bad in ({"ladder": {"size": 0.1}}, {"ladder": {"levels_pct": []}}, {"ladder": {"size_frac": 0}},
                    {"ladder": {"enabled": "yes"}}, {"exits": {"reprice_pct": -1}}, {"routing": {"coins": "BTC"}},
                    {"ladder": {"levels_pct": [-20, -20]}}, {"ladder": []}, {"routing": {"coins": ["USDT"]}}):
            with self.assertRaises(RunnerError, msg=bad):
                load_config(None, bad)


# --------------------------------------------------------------------------- the ladder

class LadderPlacementTest(Base):
    def test_first_check_places_two_usdt_backed_maker_bids_per_coin(self):
        rep = self.runner().run_once()
        self.assertEqual(rep["status"], "ok")
        bids = self.bids()
        self.assertEqual(len(bids), 8)
        got = sorted((v["symbol"], float(v["price"])) for v in bids)
        want = sorted(("%s_USDT" % c, PX_USDT[c] * f) for c in COINS for f in (0.8, 0.75))
        self.assertEqual(got, want)
        for v in bids:
            notional = float(v["price"] * v["base_amount"])
            self.assertAlmostEqual(notional, 1250.0 * 0.998, delta=2.0)          # 12.5% of equity (cash buffer)
            self.assertEqual((v["side"], v["tag"]), ("buy", LADDER_TAG))
            self.assertIn(float(v["meta"]["level_pct"]), (-20.0, -25.0))
        self.assertLess(self.avail()["USDT"], D("25"))                            # the USDT is locked
        self.assertEqual(self.bal()["USDT"], D("10000"))                          # still the account's
        self.assertNotIn("frozen", rep)                                           # own orders: no warning
        self.assertEqual(self.rm.orders_last_24h("limit"), 8)
        self.assertEqual(self.rm.orders_last_24h("market"), 0)
        ev = [e for e in read_events(self.dir) if e["kind"] == "order_place"]
        self.assertEqual(len(ev), 8)
        self.assertTrue(all(e["tag"] == LADDER_TAG and e["id"] and e["mode"] == "paper" for e in ev))
        self.assertIn("resting order placed: ladder buy", format_report(rep))
        # the brain was shown the ladder state (and positions: none yet)
        lad = self.brain.should_args[-1]["ladder"]
        self.assertEqual(sorted(lad["coins"]), sorted(COINS))
        self.assertEqual(self.brain.should_args[-1]["positions"], {})
        # the next check shows them resting
        self.hour()
        self.runner().run_once()
        lad = self.brain.should_args[-1]["ladder"]
        self.assertTrue(all(b["status"] == "resting" for c in lad["coins"].values() for b in c["bids"]), lad)

    def test_restart_and_unchanged_reference_place_nothing_new(self):
        self.runner().run_once()
        ids = sorted(v["identifier"] for v in self.bids())
        self.assertEqual(self.runner().run_once()["status"], "already_processed")
        self.hour()
        self.runner().run_once()
        self.assertEqual(sorted(v["identifier"] for v in self.bids()), ids)
        self.assertEqual((self.rm.orders_last_24h("limit"), self.rm.cancels_last_24h()), (8, 0))

    def test_lost_runner_state_never_duplicates_a_bid(self):
        """The broker's journal (tag + meta), not the runner's memory, says which bids exist: a crash
        between placing a bid and saving the runner state cannot double the locked USDT."""
        self.runner().run_once()
        ids = sorted(v["identifier"] for v in self.bids())
        path = os.path.join(self.dir, "runner_state_paper.json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st.pop("ladder", None)
        st.pop("last_bar_ts", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
        self.runner().run_once()
        self.assertEqual(sorted(v["identifier"] for v in self.bids()), ids)

    def test_small_reference_moves_keep_the_bids_larger_ones_replace_them(self):
        self.runner().run_once()
        before = {(v["symbol"], float(v["meta"]["level_pct"])): v["identifier"] for v in self.bids()}
        self.ex.set(BTC=80000 * 1.003)                                     # new 48 h high, +0.3%
        self.hour()
        self.runner().run_once()
        self.assertEqual({(v["symbol"], float(v["meta"]["level_pct"])): v["identifier"] for v in self.bids()},
                         before)
        self.ex.set(BTC=80000 * 1.01)                                      # +1% vs the resting bids
        self.hour()
        self.runner().run_once()
        after = {(v["symbol"], float(v["meta"]["level_pct"])): v for v in self.bids()}
        for k, ident in before.items():
            if k[0] == "BTC_USDT":
                self.assertNotEqual(after[k]["identifier"], ident)
                self.assertAlmostEqual(float(after[k]["price"]), 80800 * (1 + k[1] / 100), delta=0.01)
            else:
                self.assertEqual(after[k]["identifier"], ident)
        self.assertEqual((self.rm.orders_last_24h("limit"), self.rm.cancels_last_24h()), (10, 2))
        kinds = [e["kind"] for e in read_events(self.dir)]
        self.assertEqual(kinds.count("order_cancel"), 2)

    def test_a_refused_replacement_keeps_the_resting_bid(self):
        """When vet_limit refuses the new bid (here the BTC_USDT book is 6% away from the cross-rate
        close: price sanity), the old bid is NOT cancelled first."""
        self.runner().run_once()
        old = [v["identifier"] for v in self.bids("BTC_USDT")]
        self.ex.set(BTC=80000 * 1.02)
        self.hour()
        p = 80000 * 1.02 * 1.06
        self.ex.book_override["BTC_USDT"] = {"asks": [[repr(p * 1.001), "10"]], "bids": [[repr(p * 0.999), "10"]]}
        rep = self.runner().run_once()
        self.assertEqual(sorted(v["identifier"] for v in self.bids("BTC_USDT")), sorted(old))
        self.assertEqual(self.rm.cancels_last_24h(), 0)
        self.assertEqual(rep["status"], "ok")

    def test_a_bid_that_would_cross_the_book_is_not_placed(self):
        self.ex.set(BTC=80000 * 0.79)            # the coin already trades below the -20% level
        self.hour()
        rep = self.runner().run_once()
        btc = sorted(float(v["price"]) for v in self.bids("BTC_USDT"))
        self.assertEqual(btc, [60000.0])         # -25% rests; -20% would be a taker: skipped
        self.assertTrue(any("BTC -20" in s and "cross" in s for s in rep["resting"]["skipped"]), rep["resting"])
        self.assertFalse(self.bal().get("BTC"))

    def test_bids_are_pro_rata_to_the_usdt_the_book_holds(self):
        write_paper_account(self.dir, {"USDT": "5000", "IRT": "500000000"})    # Kimi keeps half in toman
        self.runner().run_once()
        bids = self.bids()
        self.assertEqual(len(bids), 8)
        total = sum(float(v["price"] * v["base_amount"]) for v in bids)
        self.assertLessEqual(total, 5000.0)
        self.assertAlmostEqual(total, 5000 * 0.998, delta=5)
        self.assertEqual(self.bal()["IRT"], D("500000000"))                    # toman never backs a bid

    def test_endgame_cancels_every_bid_and_places_none(self):
        self.runner().run_once()
        self.assertEqual(len(self.bids()), 8)
        self.brain.no_new_entries_at = self.clock.t + 1800
        self.hour()
        rep = self.runner().run_once()
        self.assertEqual(self.bids(), [])
        self.assertEqual(len(rep["resting"]["cancelled"]), 8)
        self.hour()
        self.runner().run_once()
        self.assertEqual(self.bids(), [])
        ends = [e for e in read_events(self.dir) if e["kind"] == "endgame"]
        self.assertEqual([e["step"] for e in ends], ["ladder_off"])
        lad = self.brain.should_args[-1]["ladder"]
        self.assertTrue(all(b["status"] == "off" for c in lad["coins"].values() for b in c["bids"]))
        self.assertEqual(self.avail()["USDT"], D("10000"))

    def test_kimi_ladder_scales_are_applied_once_per_decision(self):
        self.runner().run_once()
        self.hour()
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 1.0}, hold=True, ladder={"BTC": 0.0, "ETH": 1.0, "XRP": 1.0,
                                                                          "SOL": 0.5})]
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "hold")
        self.assertIn("ladder BTC 0", " ".join(rep["brain"]["applied"]))
        self.assertEqual(self.bids("BTC_USDT"), [])
        sol = self.bids("SOL_USDT")
        self.assertEqual(len(sol), 2)
        self.assertTrue(all(abs(float(v["price"] * v["base_amount"]) - 625 * 0.998) < 3 for v in sol))
        # the scales stay in force; the same decision is not applied again
        self.brain.due = False
        self.hour()
        rep = self.runner().run_once()
        self.assertNotIn("applied", rep["brain"])
        self.assertEqual(self.bids("BTC_USDT"), [])
        self.assertEqual(self.brain.should_args[-1]["ladder"]["coins"]["BTC"]["scale"], 0.0)

    def test_halt_cancels_the_bids(self):
        self.runner().run_once()
        self.rm.halt("test halt")
        self.hour()
        rep = self.runner().run_once()
        self.assertEqual(rep["status"], "halted")
        self.assertEqual(self.bids(), [])

    def test_dry_run_places_and_writes_nothing(self):
        rep = self.runner(dry_run=True).run_once()
        self.assertEqual(rep["status"], "dry_run")
        self.assertEqual(len(rep["resting"]["would_place"]), 8)
        self.assertEqual(self.bids(), [])
        self.assertEqual(sorted(os.listdir(self.dir)), [PaperBroker.STATE_FILE])

    def test_switching_the_ladder_off_cancels_its_bids(self):
        self.runner().run_once()
        self.hour()
        self.runner(cfg={"ladder": {"enabled": False}}).run_once()
        self.assertEqual(self.bids(), [])

    def test_a_brain_without_the_ladder_contract_runs_without_it(self):
        brain = LadderBrain(self.clock)
        brain.ladder_in_force = None                     # an older KimiBrain / a test double
        r = self.runner(brain=brain)
        self.assertFalse(r.ladder_on or r.exits_on)
        rep = r.run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(self.bids(), [])


# --------------------------------------------------------------------------- fills, positions, code exits

class FillAndExitTest(Base):
    def crash_fill(self, cfg=None):
        """8 bids, then BTC closes 21% below its 48 h high: the -20% bid (64,000) fills at the next sync."""
        self.runner(cfg).run_once()
        self.ex.set(BTC=80000 * 0.79)
        self.hour()
        return self.runner(cfg).run_once()

    def test_a_fill_disarms_its_level_opens_a_position_with_exits_and_rests_a_target_sell(self):
        rep = self.crash_fill()
        self.assertEqual([(f["symbol"], f["reason"], float(f["level_pct"])) for f in rep["limit_fills"]],
                         [("BTC_USDT", "ladder", -20.0)])
        r = self.runner()
        self.assertNotIn("BTC_IRT", r._positions)                                 # the crash-ladder position
        pos = r._ladder_pos["BTC_IRT"]
        self.assertEqual(pos["source"], "ladder")
        self.assertAlmostEqual(pos["entry_px_usdt"], 64000.0)
        self.assertIsNone(pos["stop_px_usdt"])                                    # v3: no default stop
        self.assertIsNone(pos["stop_pct"])
        self.assertAlmostEqual(pos["target_px_usdt"], (64000 + 80000) / 2)       # half the 48 h drop
        # v3: max hold 720 h from the entry, never past the endgame's final decision (the fixture's is closer)
        self.assertAlmostEqual(pos["max_hold_until"], min(pos["entry_ts"] + 720 * 3600, self.brain.final_at), delta=1)
        self.assertLess(pos["max_hold_until"], pos["entry_ts"] + 720 * 3600)
        self.assertAlmostEqual(pos["amount"], float(self.bal()["BTC"]), places=8)
        tg = self.targets()
        self.assertEqual([(v["symbol"], v["side"], float(v["price"])) for v in tg], [("BTC_USDT", "sell", 72000.0)])
        self.assertEqual(tg[0]["base_amount"], self.bal()["BTC"])
        # the -20% level is not re-placed while BTC stays below -7.5%; -25% keeps resting
        self.assertEqual([float(v["price"]) for v in self.bids("BTC_USDT")], [60000.0])
        # the brain sees the fill (W1) and the guarded position
        self.hour()
        self.runner().run_once()
        seen = self.brain.should_args[-1]
        btc = seen["ladder"]["coins"]["BTC"]
        self.assertEqual(btc["bids"][0]["status"], "filled")
        self.assertAlmostEqual(btc["bids"][0]["fill_px_usdt"], 64000.0)
        self.assertIsNotNone(btc["bids"][0]["filled_at"])
        self.assertAlmostEqual(btc["dd48_pct"], -21.0, places=4)
        self.assertEqual(seen["positions"]["BTC_IRT"]["source"], "ladder")
        self.assertEqual([float(v["price"]) for v in self.bids("BTC_USDT")], [60000.0])
        kinds = [(e["kind"], e.get("reason")) for e in read_events(self.dir)]
        self.assertIn(("fill", "ladder"), kinds)
        self.assertIn(("position_open", None), kinds)

    def test_the_target_fills_closes_the_position_and_the_level_rearms(self):
        self.crash_fill()
        self.ex.set(BTC=80000 * 0.93)                     # back to -7%: above the target, above the re-arm line
        self.hour()
        rep = self.runner().run_once()
        self.assertIn(("BTC_USDT", "target"), [(f["symbol"], f["reason"]) for f in rep["limit_fills"]])
        self.assertLess(self.bal().get("BTC", ZERO), D("0.0000001"))
        self.assertNotIn("BTC_IRT", self.runner()._ladder_pos)
        self.assertNotIn("BTC_IRT", self.runner()._positions)
        self.assertEqual(self.targets(), [])
        self.assertEqual(sorted(float(v["price"]) for v in self.bids("BTC_USDT")), [60000.0, 64000.0])
        kinds = [e["kind"] for e in read_events(self.dir)]
        for k in ("position_close", "ladder_rearm"):
            self.assertIn(k, kinds)
        self.assertGreater(self.bal()["USDT"], D("10000"))      # bought at 64,000, sold at 72,000

    def test_a_close_below_the_entry_sells_nothing_without_a_stop(self):
        """v3 (A2): a crash-ladder fill has no stop until Kimi sets one - a -13% close is a wake-up for the
        model, never a sale by the code."""
        cfg = {"ladder": {"levels_pct": [-20]}}
        self.crash_fill(cfg)
        self.ex.set(BTC=64000 * 0.87)
        self.hour()
        rep = self.runner(cfg).run_once()
        self.assertEqual(rep.get("exits", []), [])
        self.assertEqual(rep["fills"], [])
        self.assertGreater(self.bal()["BTC"], 0)
        self.assertIn("BTC_IRT", self.runner(cfg)._ladder_pos)
        self.assertEqual(self.rm.orders_last_24h("market"), 0)

    def test_stop_on_the_hourly_close_sells_directly_into_usdt(self):
        cfg = {"ladder": {"levels_pct": [-20]}}
        self.crash_fill(cfg)
        self.set_stop(cfg=cfg)                            # v3: Kimi's exits give the position its 12% stop
        self.assertAlmostEqual(self.runner(cfg)._ladder_pos["BTC_IRT"]["stop_px_usdt"], 64000 * 0.88)
        self.ex.set(BTC=64000 * 0.87)                     # the close is below the -12% stop
        self.hour()
        rep = self.runner(cfg).run_once()
        self.assertEqual([(x["symbol"], x["reason"]) for x in rep["exits"]], [("BTC_IRT", "stop")])
        fills = [(f["symbol"], f["side"], f["route"], f["reason"]) for f in rep["fills"]]
        self.assertEqual(fills, [("BTC_USDT", "sell", "direct", "stop")])
        self.assertLess(self.bal().get("BTC", ZERO), D("0.0000001"))
        self.assertNotIn("BTC_IRT", self.runner(cfg)._positions)
        self.assertNotIn("BTC_IRT", self.runner(cfg)._ladder_pos)
        self.assertEqual(self.targets(), [])                  # the target sell was cancelled first
        self.assertEqual(self.rm.orders_last_24h("market"), 1)
        ev = [e for e in read_events(self.dir) if e["kind"] in ("exit", "fill") and e.get("reason") == "stop"]
        self.assertEqual([e["kind"] for e in ev], ["exit", "fill"])
        self.assertEqual(ev[1]["quote_asset"], "USDT")
        self.assertIn("CODE EXIT stop BTC_IRT", format_report(rep))
        # the bar was written ahead of the stop order: a restart does not sell again
        self.assertEqual(self.runner(cfg).run_once()["status"], "already_processed")

    def test_stop_takes_the_toman_route_when_the_usdt_book_is_too_thin(self):
        cfg = {"ladder": {"levels_pct": [-20]}}
        self.crash_fill(cfg)
        self.set_stop(cfg=cfg)
        self.ex.set(BTC=64000 * 0.87)
        self.hour()
        self.ex.depth["BTC_USDT"] = "0.001"               # the direct book cannot take the position
        rep = self.runner(cfg).run_once()
        fills = [(f["symbol"], f["side"], f["reason"]) for f in rep["fills"]]
        self.assertEqual(fills, [("BTC_IRT", "sell", "stop"), ("USDT_IRT", "buy", "stop")])
        self.assertLess(self.bal().get("BTC", ZERO), D("0.0000001"))
        self.assertLess(self.bal().get("IRT", ZERO), D("500000"))      # the proceeds went into USDT (cash buffer)

    def test_a_cancel_that_races_a_fill_books_the_fill_and_replaces_nothing(self):
        """The BTC -20% bid is due for a re-price (the 48 h high rose 1%), but it fills while it is
        being cancelled: the fill is handled, the level is disarmed, no new bid is placed for it."""
        self.runner().run_once()
        self.ex.set(BTC=80000 * 1.01)
        self.hour()
        r = self.runner()
        orig = r.broker.cancel
        wick = {"asks": [["63900", "1"]], "bids": [["63800", "1"]]}

        def cancel(order, wait=True):
            if self.bids("BTC_USDT", broker=r.broker) and "BTC_USDT" not in self.ex.book_override:
                self.ex.book_override["BTC_USDT"] = wick           # a wick touches the old bid right now
            try:
                return orig(order, wait)
            finally:
                self.ex.book_override.pop("BTC_USDT", None)
        r.broker.cancel = cancel
        rep = r.run_once()
        self.assertIn(("BTC_USDT", "ladder"), [(f["symbol"], f["reason"]) for f in rep["limit_fills"]])
        lv = r._ladder_st["coins"]["BTC"]["levels"]["-20"]
        self.assertFalse(lv["armed"])
        self.assertEqual([float(v["meta"]["level_pct"]) for v in self.bids("BTC_USDT")], [-25.0])
        self.assertGreater(self.bal()["BTC"], 0)
        self.assertIn("BTC_IRT", r._ladder_pos)

    def test_an_unacknowledged_fill_is_handled_again_without_side_effects(self):
        self.runner().run_once()
        self.ex.set(BTC=80000 * 0.79)
        self.hour()
        r = self.runner()
        r.broker.ack_limit_fills = lambda evs: (_ for _ in ()).throw(OSError("disk full"))
        r.run_once()
        first = dict(r._ladder_st["coins"]["BTC"]["levels"]["-20"])
        pos = dict(r._ladder_pos["BTC_IRT"])
        self.hour()
        r2 = self.runner()
        rep = r2.run_once()
        self.assertEqual([f["symbol"] for f in rep["limit_fills"]], ["BTC_USDT"])      # re-delivered ...
        self.assertEqual(r2._ladder_st["coins"]["BTC"]["levels"]["-20"]["filled_at"], first["filled_at"])
        self.assertAlmostEqual(r2._ladder_pos["BTC_IRT"]["amount"], pos["amount"])    # ... booked once
        ids = [e["id"] for e in read_events(self.dir) if e["kind"] == "fill"]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 1)                                             # same event id
        self.hour()
        self.assertFalse(self.runner().run_once().get("limit_fills"))                  # acked now

    def test_kimi_exits_apply_to_the_position_its_buy_opens(self):
        self.brain.due = True
        until = self.clock.t + 48 * 3600
        self.brain.next = [decision({"USDT_IRT": 0.8, "BTC_IRT": 0.2}, exits={"BTC_IRT": {
            "stop_pct": 10.0, "target_price": 90000.0, "target_rule": "none", "max_hold_hours": 48.0,
            "max_hold_until": until, "wake_up_pct": 6.0, "wake_levels": [70000.0], "source": "kimi"}})]
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "kimi")
        self.assertEqual([(f["symbol"], f["route"], f["reason"]) for f in rep["fills"]],
                         [("BTC_USDT", "direct", "allocation")])
        pos = self.runner()._positions["BTC_IRT"]
        self.assertEqual(pos["source"], "kimi")
        self.assertAlmostEqual(pos["stop_px_usdt"], pos["entry_px_usdt"] * 0.9)
        self.assertEqual(pos["target_px_usdt"], 90000.0)
        self.assertAlmostEqual(pos["max_hold_until"], until, delta=120)
        self.assertEqual((pos["wake_up_pct"], pos["wake_levels"]), (6.0, [70000.0]))
        self.assertEqual([float(v["price"]) for v in self.targets()], [90000.0])

    def test_the_allocation_cancels_the_target_sell_before_selling_the_coin(self):
        self.crash_fill()
        self.assertEqual(len(self.targets()), 1)
        self.hour()
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 1.0, "BTC_IRT": 0.0})]
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "kimi")
        self.assertIn(("BTC_USDT", "sell"), [(f["symbol"], f["side"]) for f in rep["fills"]])
        self.assertLess(self.bal().get("BTC", ZERO), D("0.0000001"))
        self.assertEqual(self.targets(), [])
        self.assertNotIn("BTC_IRT", self.runner()._positions)
        self.assertNotIn("BTC_IRT", self.runner()._ladder_pos)

    def test_the_fallback_is_given_the_guarded_positions(self):
        self.crash_fill()
        self.hour()
        self.brain.due = True
        self.brain.next = [FakeDecision(valid=False, error="down", error_kind="llm_error")]
        self.runner().run_once()
        self.assertIn("BTC_IRT", self.brain.fallback_args[-1]["positions"])
        guarded = self.brain.fallback_args[-1]["positions"]["BTC_IRT"]
        self.assertIsNone(guarded["stop_px_usdt"])                                # v3: no default stop ...
        self.assertAlmostEqual(guarded["target_px_usdt"], 72000.0)                # ... the target rule stays
        self.assertAlmostEqual(guarded["entry_px_usdt"], 64000.0)

    def test_positions_start_from_the_holdings_without_replaying_old_fills(self):
        write_paper_account(self.dir, {"USDT": "9000", "ETH": "0.3333"})       # held before the ladder existed
        self.runner().run_once()
        pos = self.runner()._positions
        self.assertEqual(sorted(pos), ["ETH_IRT"])
        self.assertEqual(pos["ETH_IRT"]["source"], "held")
        self.assertAlmostEqual(pos["ETH_IRT"]["entry_px_usdt"], 3000.0)
        self.assertIsNone(pos["ETH_IRT"]["stop_px_usdt"])                         # v3: a held coin has no default stop
        self.assertIsNone(pos["ETH_IRT"]["target_px_usdt"])


# --------------------------------------------------------------------------- the cost meter (spec C2)

class CostMeterTest(Base):
    """The runner meters its own fills (fees, signed slippage against the planned price, 7-day turnover)
    and hands the totals plus the breaker's threshold to the context builder's snapshot (v3, spec C2:
    portfolio.costs_since_start / turnover_7d_pct / halt_at_drawdown_pct)."""

    def test_a_market_fill_is_metered_and_the_snapshot_carries_the_totals(self):
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 0.7, "BTC_IRT": 0.3})]
        rep = self.runner().run_once()
        self.assertEqual([(f["symbol"], f["side"], f["route"]) for f in rep["fills"]], [("BTC_USDT", "buy", "direct")])
        r = self.runner()
        costs = r.state["costs"]
        fill = rep["fills"][0]
        # the fee of the direct leg in toman: a buy pays its fee in the coin it receives (fee x fill price x
        # the last USDT_IRT close), a USDT fee is converted at the USDT_IRT close; the traded value in toman
        fee_usdt = float(fill["fee"]) * (float(fill["avg_price"]) if fill["fee_asset"] == "BTC" else 1.0)
        self.assertAlmostEqual(costs["fees_irt"], fee_usdt * U, delta=max(1.0, fee_usdt * U * 1e-6))
        self.assertGreater(costs["fees_irt"], 0)
        self.assertEqual(costs["fills"], 1)
        self.assertEqual(len(costs["turnover"]), 1)
        self.assertAlmostEqual(costs["turnover"][0][1], float(fill["quote"]) * U, delta=1)
        # slippage: the taker buy filled through the book above the planned close (small, positive)
        self.assertGreaterEqual(costs["slippage_irt"], 0)
        self.assertLess(costs["slippage_irt"], costs["turnover"][0][1] * 0.01)
        tot = r._cost_totals(self.clock.t)
        self.assertEqual(sorted(tot), ["fees_irt", "slippage_irt", "turnover_7d_irt"])
        self.assertAlmostEqual(tot["turnover_7d_irt"], costs["turnover"][0][1])
        units, cash = r._holdings(IRT_SYMS)
        snap = r._brain_snapshot(units, cash, self.clock.t)
        self.assertEqual(snap["halt_drawdown_pct"], 30.0)                  # built-in max_drawdown 0.30 (the example ships 0.50)
        self.assertAlmostEqual(snap["fees_irt"], costs["fees_irt"])
        self.assertAlmostEqual(snap["turnover_7d_irt"], tot["turnover_7d_irt"])
        self.assertIn("slippage_irt", snap)
        # the turnover window is 7 days; the cumulative costs stay
        later = r._cost_totals(self.clock.t + 8 * 86400)
        self.assertEqual(later["turnover_7d_irt"], 0.0)
        self.assertAlmostEqual(later["fees_irt"], costs["fees_irt"])
        # the brain saw the same snapshot in its context (FakeBuilder passes it through as "portfolio")
        self.hour()
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 1.0}, hold=True)]
        self.runner().run_once()
        seen = self.brain.decide_args[-1]["ctx"]["portfolio"]
        self.assertEqual(seen["halt_drawdown_pct"], 30.0)
        self.assertAlmostEqual(seen["fees_irt"], costs["fees_irt"])

    def test_a_resting_fill_adds_turnover_and_fee_but_no_slippage(self):
        self.runner().run_once()                                        # 8 bids
        self.ex.set(BTC=80000 * 0.79)
        self.hour()
        rep = self.runner().run_once()                                  # the -20% bid fills (maker)
        self.assertEqual([f["reason"] for f in rep["limit_fills"]], ["ladder"])
        costs = self.runner().state["costs"]
        self.assertEqual(costs["fills"], 1)
        self.assertEqual(costs["slippage_irt"], 0.0)                     # a resting order fills at its own price
        self.assertGreater(costs["fees_irt"], 0)
        self.assertAlmostEqual(costs["turnover"][0][1], 1250 * 0.998 * U, delta=U)   # about 1,250 USDT in toman

    def test_no_fill_means_no_totals_and_a_broken_record_never_breaks_the_snapshot(self):
        r = self.runner()
        r.run_once()
        self.assertNotIn("costs", r.state)
        self.assertEqual(r._cost_totals(self.clock.t), {})
        units, cash = r._holdings(IRT_SYMS)
        snap = r._brain_snapshot(units, cash, self.clock.t)
        self.assertNotIn("fees_irt", snap)
        self.assertEqual(snap["halt_drawdown_pct"], 30.0)
        r.state["costs"] = {"fees_irt": "junk", "turnover": [["x", "y"]]}
        r._note_cost({"symbol": "BTC_USDT", "side": "buy", "base": "0.01", "quote": "800", "fee": "1", "fee_asset": "USDT",
                      "avg_price": "80000"}, self.clock.t, 79000.0)       # never raises
        self.assertEqual(r._brain_snapshot(units, cash, self.clock.t)["halt_drawdown_pct"], 30.0)

    def test_a_successful_cycle_records_its_status(self):
        self.runner().run_once()
        st = json.load(open(os.path.join(self.dir, "runner_state_paper.json"), encoding="utf-8"))
        self.assertEqual(st["last_cycle"]["status"], "ok")                 # the notifier's heartbeat key


# --------------------------------------------------------------------------- routing and the allocation

class RoutingTest(Base):
    def test_a_usdt_to_coin_allocation_goes_through_coin_usdt_and_resizes_the_ladder(self):
        self.runner().run_once()                                       # 8 bids lock the USDT
        self.hour()
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 0.7, "BTC_IRT": 0.3})]
        rep = self.runner().run_once()
        self.assertEqual(rep["brain"]["action"], "kimi")
        self.assertEqual([(f["symbol"], f["side"], f["quote_asset"], f["route"]) for f in rep["fills"]],
                         [("BTC_USDT", "buy", "USDT", "direct")])     # 1 leg instead of USDT_IRT + BTC_IRT
        self.assertTrue(rep["routing"])
        btc_w = float(self.bal()["BTC"]) * 80000 / 10000
        self.assertAlmostEqual(btc_w, 0.3, delta=0.01)
        # the bids were cancelled for the allocation and re-placed pro rata to the USDT left
        bids = self.bids()
        self.assertEqual(len(bids), 8)
        total = sum(float(v["price"] * v["base_amount"]) for v in bids)
        self.assertAlmostEqual(total, float(self.bal()["USDT"]) * 0.998, delta=10)
        self.assertLess(total, 7100)
        self.assertIn("BTC_IRT", self.runner()._positions)

    def test_a_thin_usdt_book_keeps_the_toman_route(self):
        self.ex.depth["BTC_USDT"] = "0.001"
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 0.7, "BTC_IRT": 0.3})]
        rep = self.runner().run_once()
        self.assertEqual([(f["symbol"], f["side"]) for f in rep["fills"]],
                         [("USDT_IRT", "sell"), ("BTC_IRT", "buy")])     # sells first
        self.assertFalse(rep.get("routing"))

    def test_a_coin_to_usdt_sale_goes_direct(self):
        write_paper_account(self.dir, {"USDT": "7000", "BTC": "0.0375"})
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 1.0, "BTC_IRT": 0.0})]
        rep = self.runner().run_once()
        self.assertEqual([(f["symbol"], f["side"], f["route"]) for f in rep["fills"]],
                         [("BTC_USDT", "sell", "direct")])
        self.assertEqual(self.bal().get("BTC", ZERO), ZERO)
        self.assertNotIn("IRT", {a for a, v in self.bal().items() if v > 0})

    def test_routing_can_be_switched_off(self):
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 0.7, "BTC_IRT": 0.3})]
        rep = self.runner(cfg={"routing": {"enabled": False}}).run_once()
        self.assertEqual([(f["symbol"], f["side"]) for f in rep["fills"]],
                         [("USDT_IRT", "sell"), ("BTC_IRT", "buy")])

    def test_a_capped_sleeve_books_usdt_quoted_fills_into_its_usdt(self):
        write_paper_account(self.dir, {"IRT": "3000000000"})          # the user's toman; the bot gets 1e9
        cfg = {"max_equity_irt": 1000000000}
        self.brain.due = True
        self.brain.next = [decision({"USDT_IRT": 1.0})]
        self.runner(cfg).run_once()
        r = self.runner(cfg)
        sl = r._sleeve_get()
        usdt0 = sl["units"]["USDT_IRT"]
        self.assertGreater(usdt0, D("9900"))
        # the ladder never uses more USDT than the sleeve owns (the account's toman is not USDT)
        self.assertLessEqual(sum(v["price"] * v["base_amount"] for v in self.bids()), usdt0)
        self.hour()
        self.brain.next = [decision({"USDT_IRT": 0.7, "BTC_IRT": 0.3})]
        rep = self.runner(cfg).run_once()
        self.assertIn(("BTC_USDT", "buy"), [(f["symbol"], f["side"]) for f in rep["fills"]])
        sl = self.runner(cfg)._sleeve_get()
        direct = [f for f in rep["fills"] if f["symbol"] == "BTC_USDT"]
        self.assertEqual(len(direct), 1)
        # the sleeve's USDT paid the direct leg; its toman only the (small) rest of the BTC target
        btc = sum((D(f["base"]) - D(f["fee"]) for f in rep["fills"] if f["symbol"].startswith("BTC_")), ZERO)
        self.assertEqual(sl["units"]["BTC_IRT"], btc)
        self.assertEqual(sl["units"]["USDT_IRT"], usdt0 - D(direct[0]["quote"]))
        irt_spent = sum((D(f["quote"]) for f in rep["fills"] if f["symbol"] == "BTC_IRT"), ZERO)
        self.assertLess(irt_spent, D("5000000"))
        self.assertEqual(self.bal()["IRT"], D("3000000000") - (D("1000000000") - sl["cash"]))
        self.assertEqual(self.bal()["BTC"], btc)


# --------------------------------------------------------------------------- watchdog wiring and events

class WatchdogWiringTest(Base):
    def test_a_veto_wake_up_forces_a_focused_news_brief_and_passes_the_mode(self):
        class News(object):
            calls = []

            def research(self, now=None, context_hint=None, force=False, abort=None, focus=None):
                self.calls.append({"force": force, "focus": focus})
                return None
        news = News()
        self.brain.due = True
        self.brain.kind_next, self.brain.mode_next = "veto", "veto"
        self.brain.events_next = [{"kind": "veto", "coin": "BTC", "text": "BTC closed 16% below its 48 h high"}]
        self.brain.news_req = {"force": True, "focus": "BTC closed 16% below its 48 h high"}
        self.brain.next = [decision({"USDT_IRT": 1.0}, hold=True, ladder={"BTC": 0.0, "ETH": 1.0, "XRP": 1.0,
                                                                          "SOL": 1.0}, mode="veto")]
        r = self.runner()
        r.news = news
        rep = r.run_once()
        self.assertEqual(news.calls, [{"force": True, "focus": "BTC closed 16% below its 48 h high"}])
        self.assertEqual(self.brain.decide_args[-1]["mode"], "veto")
        self.assertEqual(self.builder.builds[-1]["mode"], "veto")
        self.assertIn("endgame", self.builder.builds[-1])
        self.assertEqual((rep["brain"]["trigger_kind"], rep["brain"]["mode"]), ("veto", "veto"))
        self.assertEqual(self.bids("BTC_USDT"), [])                     # the veto cancelled BTC's ladder
        ev = {e["kind"]: e for e in read_events(self.dir)}
        self.assertEqual(ev["watchdog"]["trigger_kind"], "veto")
        self.assertEqual(ev["decision"]["mode"], "veto")
        self.assertEqual(ev["decision"]["ladder"]["BTC"], 0.0)

    def test_notify_only_wake_ups_are_logged_as_events(self):
        self.brain.notes = [{"kind": "usdt", "text": "USDT_IRT moved +4.2% since the last decision", "t": NOW},
                            {"kind": "drawdown", "text": "the drawdown worsened (coins 0%)", "t": NOW}]
        rep = self.runner().run_once()
        self.assertEqual([n["kind"] for n in rep["brain"]["notifications"]], ["usdt", "drawdown"])
        ev = [e for e in read_events(self.dir) if e["kind"] == "notify"]
        self.assertEqual([e["notify_kind"] for e in ev], ["usdt", "drawdown"])
        self.assertEqual(self.brain.decide_args, [])                     # no LLM call

    def test_final_decision_and_report_are_logged(self):
        self.brain.due = True
        self.brain.kind_next, self.brain.mode_next = "final", "final"
        self.brain.next = [decision({"USDT_IRT": 1.0}, hold=True, mode="final", report_fa="گزارش")]
        self.runner().run_once()
        ev = read_events(self.dir)
        d = [e for e in ev if e["kind"] == "decision"][0]
        self.assertEqual((d["mode"], d["report_fa"]), ("final", "گزارش"))
        self.assertIn("final_decision", [e.get("step") for e in ev if e["kind"] == "endgame"])
        with open(os.path.join(self.dir, EVENTS_LOG), encoding="utf-8") as f:
            self.assertIn("گزارش", f.read())          # UTF-8, not escaped

    def test_every_event_has_the_notifier_fields(self):
        self.runner().run_once()
        for e in read_events(self.dir):
            for k in ("t", "kind", "mode", "id"):
                self.assertIn(k, e)

    def test_kill_switch_cancels_the_ladder_bids_but_keeps_the_target_sells(self):
        """Review finding (money, high): STOP (also the Telegram /stop relay) left up to 100% of the
        USDT in crash bids that could fill while no code exit guarded the positions."""
        self.runner().run_once()
        self.ex.set(BTC=80000 * 0.79)                  # the BTC -20% bid fills: a position + its target sell
        self.hour()
        self.runner().run_once()
        self.assertEqual((len(self.bids()), len(self.targets())), (7, 1))
        open(os.path.join(self.dir, KILL_SWITCH_FILE), "w").close()
        self.hour()
        rep = self.runner().run_once()
        self.assertEqual(rep["status"], "kill_switch")
        self.assertEqual(self.bids(), [])
        self.assertEqual([(v["symbol"], v["side"]) for v in self.targets()], [("BTC_USDT", "sell")])
        self.assertEqual(self.rm.orders_last_24h("market"), 0)          # nothing bought or sold
        # a dry run changes nothing on the exchange
        self.hour()
        self.assertEqual(self.runner(dry_run=True).run_once()["status"], "kill_switch")
        self.assertEqual(len(self.targets()), 1)

    def test_the_loop_cancels_the_bids_when_stop_appears_while_it_waits(self):
        r = self.runner()
        r.run_once()
        self.assertEqual(len(self.bids()), 8)
        open(os.path.join(self.dir, KILL_SWITCH_FILE), "w").close()
        self.assertEqual(r.loop(max_cycles=3), "kill_switch")
        self.assertEqual(self.bids(), [])


# --------------------------------------------------------------------------- the REAL KimiBrain end to end

def kimi_reply(targets, **extra):
    r = {"targets": targets, "cash_irt": 0.0, "confidence": 0.7, "reasoning": "ladder test",
         "news_summary": "none", "key_risks": "stop -12%", "next_review_hours": 24}
    r.update(extra)
    return r


class KimiTransport(object):
    """Moonshot double: canned chat replies (plain JSON, the client falls back from streaming)."""

    def __init__(self):
        self.replies = []
        self.default = kimi_reply({"USDT_IRT": 1.0})
        self.bodies = []

    def __call__(self, method, url, headers, body, timeout):
        assert headers.get("Authorization") == "Bearer " + KEY
        if url.endswith("/models"):
            return 200, json.dumps({"data": [{"id": "kimi-test"}]}).encode("utf-8")
        self.bodies.append(json.loads(body.decode("utf-8")))
        r = self.replies.pop(0) if self.replies else self.default
        return 200, json.dumps({"model": "kimi-test", "usage": {"total_tokens": 1000},
                                "choices": [{"finish_reason": "stop",
                                             "message": {"role": "assistant", "content": json.dumps(r)}}]}
                               ).encode("utf-8")

    def user_messages(self):
        return [b["messages"][-1]["content"] for b in self.bodies]


class RecordingNews(object):
    def __init__(self):
        self.calls = []

    def research(self, now=None, context_hint=None, force=False, abort=None, focus=None, focus_key=None):
        self.calls.append({"force": force, "focus": focus, "focus_key": focus_key})
        return None


def kimi_config(**brain):
    b = {"allowed_symbols": IRT_SYMS, "web_search": False, "risk_profile": "full",
         "decision_times_local": ["13:00"], "decision_interval_hours": 24, "max_gap_hours": 26,
         "min_decision_spacing_minutes": 55, "max_decisions_per_day": 4, "max_early_decisions_per_day": 3,
         "honor_next_review_hours": False, "fallback": {"derisk_after_hours": 48}}
    b.update(brain)
    return {"llm": {"model": "kimi-test", "max_retries": 0, "backoff_seconds": 0, "backoff_max_seconds": 0},
            "brain": b,
            "context": {"universe": IRT_SYMS, "macro_symbols": ["USDT_IRT"], "rule_signals": False,
                        "competition_start_utc": "2026-09-21T20:30:00Z",
                        "competition_end_utc": "2026-10-22T20:30:00Z"}}


class RealBrainTest(Base):
    def setUp(self):
        super(RealBrainTest, self).setUp()
        self.kimi = KimiTransport()
        self.news = None

    def real(self, cfg=None, **brain):
        llm, kb, builder = build_kimi(kimi_config(**brain), self.ex, self.dir, runner_cfg=RUNNER_CFG,
                                      transport=self.kimi, env={"KIMI_API_KEY": KEY}, bars_source=self.ex.bars,
                                      clock=self.clock)
        self.kb = kb
        self.broker = PaperBroker(MARKETS, self.dir, book_source=self.ex.book, clock=self.clock)
        self.rm = RiskManager({"min_order_irt": 100000}, self.dir, "paper", clock=self.clock)
        c = dict(RUNNER_CFG)
        c.update(cfg or {})
        return Runner(None, self.broker, self.rm, c, self.dir, "paper", bars_source=self.ex.bars, clock=self.clock,
                      sleep=self.clock.sleep, brain=kb, context_builder=builder, news=self.news)

    def test_the_daily_decision_is_anchored_to_13_tehran_across_restarts_and_early_calls(self):
        decided = []
        for h in range(49):                         # 13:01 on day 1 ... 13:01 on day 3, a new process every hour
            if h == 5:                              # the 18:00 close of day 1: BTC 16% below its 48 h high (W2)
                self.ex.set(BTC=80000 * 0.84)
                self.kimi.replies = [kimi_reply({"USDT_IRT": 1.0}, ladder={"BTC": 0.0})]
            if h:
                self.hour()
            rep = self.real().run_once()
            self.assertEqual(rep["status"], "ok", rep)
            if rep["brain"].get("decided"):
                local = datetime.fromtimestamp(self.clock.t, TEHRAN)
                decided.append((local.strftime("%d %H:%M"), rep["brain"]["trigger_kind"], rep["brain"]["mode"]))
        self.assertEqual(decided, [("22 13:01", "first", "scheduled"), ("22 18:01", "veto", "veto"),
                                   ("23 13:01", "scheduled", "scheduled"), ("24 13:01", "scheduled", "scheduled")])
        self.assertEqual(len(self.kimi.bodies), 4)
        # the veto's ladder scale 0 cancelled BTC's bids and keeps them off
        self.assertEqual(self.bids("BTC_USDT"), [])
        self.assertEqual(len(self.bids("ETH_USDT")), 2)
        self.assertIn("veto", self.kimi.user_messages()[1].lower())

    def test_tehran_slots_have_no_daylight_saving(self):
        self.real()
        for y, mo, d in ((2026, 9, 22), (2027, 1, 15), (2027, 3, 25), (2027, 7, 15)):
            t = datetime(y, mo, d, 12, 0, tzinfo=timezone.utc).timestamp()
            slot = self.kb.latest_slot(t)
            self.assertEqual(datetime.fromtimestamp(slot, timezone.utc).strftime("%H:%M"), "09:30", (y, mo, d))
            self.assertEqual(self.kb.next_slot(slot) - slot, 86400)

    def test_a_ladder_fill_wakes_kimi_in_review_mode_with_a_forced_brief(self):
        self.news = RecordingNews()
        self.real().run_once()                                     # the first decision + 8 bids
        self.ex.set(BTC=80000 * 0.79)                              # the -20% bid fills
        self.hour()
        btc_w = 0.125 * 0.998 * 0.79                               # the new position's weight, about
        self.kimi.replies = [kimi_reply({"USDT_IRT": 1.0 - btc_w, "BTC_IRT": btc_w},
                                        report_fa="نگه دار")]
        rep = self.real().run_once()
        self.assertEqual((rep["brain"]["trigger_kind"], rep["brain"]["mode"]), ("review", "review"), rep["brain"])
        self.assertEqual(self.news.calls[-1]["force"], True)
        self.assertIn("BTC", self.news.calls[-1]["focus"])
        self.assertEqual(self.news.calls[-1]["focus_key"], "fill:BTC,veto:BTC")   # the -21% close is a veto too
        self.assertIn("BTC_IRT", self.runner()._ladder_pos)
        self.assertEqual(rep["brain"]["decision"]["mode"], "review")
        self.assertGreater(self.bal()["BTC"], 0)                   # kept (a review never adds)
        ev = [e for e in read_events(self.dir) if e["kind"] in ("watchdog", "decision")]
        self.assertEqual([e["kind"] for e in ev][-2:], ["watchdog", "decision"])
        self.assertEqual(ev[-1]["report_fa"], "نگه دار")
        # the review's exits (defaults restated by the brain) are applied to the ladder position: v3 restates
        # NO stop (a stop exists only where Kimi names stop_pct) and keeps the half-drop target
        pos = self.runner()._ladder_pos["BTC_IRT"]
        self.assertIsNone(pos["stop_px_usdt"])
        self.assertAlmostEqual(pos["target_px_usdt"], 72000.0, delta=1)
        # an hour later the fill is not reviewed again
        self.hour()
        rep = self.real().run_once()
        self.assertFalse(rep["brain"].get("decided"))

    def test_the_final_slot_is_a_final_decision_and_the_ladder_is_off(self):
        final = tehran(2026, 10, 21, 13)
        self.ex = Exchange()
        shift = int(final - 3600 + 1800 - LAST_CLOSED) // 3600 * 3600
        for s in self.ex.series:
            self.ex.series[s] = [Bar(b.ts + shift, b.open, b.high, b.low, b.close, b.volume) for b in self.ex.series[s]]
        self.clock.t = NOW + shift
        self.assertEqual(datetime.fromtimestamp(self.clock.t, TEHRAN).strftime("%m-%d %H:%M"), "10-21 13:01")
        # v3: the built-in endgame is the one-year one (2027); this fixture keeps its own 2026 dates
        rep = self.real(endgame={"no_new_entries_at": "2026-10-17T13:00:00+03:30", "final_at": "2026-10-21T13:00:00+03:30",
                                 "max_hold_cap_at": "2026-10-21T13:00:00+03:30"}).run_once()
        self.assertEqual(rep["brain"]["mode"], "final", rep["brain"])
        self.assertEqual(self.bids(), [])
        steps = [e["step"] for e in read_events(self.dir) if e["kind"] == "endgame"]
        self.assertEqual(sorted(steps), ["final_decision", "ladder_off"])


# --------------------------------------------------------------------------- LIVE broker (fake Bitpin client)

class LiveClient(object):
    """In-memory Bitpin for LiveBroker: a wallet with frozen funds, resting limit orders (filled by the
    test), immediate market orders at the best price, cancels processed after `cancel_reads` reads,
    and placements that are stored but answered ambiguously (`unknown_next`)."""

    def __init__(self, ex, usdt="10000"):
        self.ex = ex
        self.bal = {"IRT": D(0), "USDT": D(usdt)}
        self.frz = {}
        self.orders, self.lock = {}, {}
        self.next_id = 5000
        self.cancel_reads = 0
        self.unknown_next = 0
        self.posts = []
        self.reject = set()

    @staticmethod
    def _row(o):
        return {k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in o.items() if k[0] != "_"}

    def wallets(self, assets=None, service="main", limit=200):
        return [{"asset": a, "balance": format(self.bal.get(a, ZERO), "f"), "frozen": format(self.frz.get(a, ZERO), "f"),
                 "service": "main"} for a in sorted(set(self.bal) | set(self.frz))]

    def orderbook(self, symbol):
        return self.ex.book(symbol)

    def place_order(self, symbol, side, type="market", base_amount=None, quote_amount=None, price=None,
                    identifier=None):
        from bitpin.markets import floor_to_precision
        self.posts.append((symbol, side, type, identifier))
        m = MARKETS.get(symbol)
        self.next_id += 1
        oid = self.next_id
        o = {"id": oid, "symbol": symbol, "side": side, "type": type, "identifier": identifier, "state": "active",
             "dealed_base_amount": ZERO, "dealed_quote_amount": ZERO, "commission": ZERO}
        book = self.ex.book(symbol)
        if type == "limit":
            o.update(price=D(price), base_amount=D(base_amount))
            asset, amt = (m.quote, D(price) * D(base_amount)) if side == "buy" else (m.base, D(base_amount))
            self.bal[asset] = self.bal.get(asset, ZERO) - amt
            self.frz[asset] = self.frz.get(asset, ZERO) + amt
            self.lock[oid] = (asset, amt)
        elif side == "buy":
            ask = D(book["asks"][0][0])
            base = floor_to_precision(D(quote_amount) / ask, m.base_amount_precision)
            fee = floor_to_precision(base * D("0.0035"), m.base_amount_precision)
            o.update(state="closed", dealed_base_amount=base, dealed_quote_amount=D(quote_amount), commission=fee)
            self.bal[m.quote] -= D(quote_amount)
            self.bal[m.base] = self.bal.get(m.base, ZERO) + base - fee
        else:
            bid = D(book["bids"][0][0])
            quote = floor_to_precision(D(base_amount) * bid, m.quote_amount_precision)
            fee = floor_to_precision(quote * D("0.0035"), m.quote_amount_precision)
            o.update(state="closed", dealed_base_amount=D(base_amount), dealed_quote_amount=quote, commission=fee)
            self.bal[m.base] -= D(base_amount)
            self.bal[m.quote] = self.bal.get(m.quote, ZERO) + quote - fee
        if type == "limit" and symbol in self.reject:
            from bitpin.api import BitpinAPIError
            if type == "limit":
                asset, amt = self.lock.pop(oid)
                self.bal[asset] += amt
                self.frz[asset] -= amt
            raise BitpinAPIError(400, "invalid", {"detail": "amount is less than the minimum"})
        self.orders[oid] = o
        if self.unknown_next:
            self.unknown_next -= 1
            from bitpin.api import OrderStatusUnknown
            raise OrderStatusUnknown(identifier, "timeout after the POST (test)")
        return self._row(o)

    def fill(self, oid):
        o = self.orders[oid]
        m = MARKETS.get(o["symbol"])
        base = o["base_amount"] - o["dealed_base_amount"]
        quote = base * o["price"]
        fee = base * D("0.003")
        self.frz[m.quote] -= quote
        self.bal[m.base] = self.bal.get(m.base, ZERO) + base - fee
        o.update(dealed_base_amount=o["base_amount"], dealed_quote_amount=o["dealed_quote_amount"] + quote,
                 commission=o["commission"] + fee)
        self.lock.pop(oid, None)
        o["state"] = "closed"

    def _visible(self, oid):
        o = self.orders.get(int(oid))
        if o is not None and "_cancel" in o and o["state"] == "active":
            if o["_cancel"] > 0:
                o["_cancel"] -= 1
            else:
                asset, left = self.lock.pop(o["id"], (None, ZERO))
                if asset:
                    self.frz[asset] -= left
                    self.bal[asset] += left
                o["state"] = "canceled"
        return o

    def get_order(self, oid):
        o = self._visible(oid)
        if o is None:
            from bitpin.api import BitpinAPIError
            raise BitpinAPIError(404, "not_found", {"detail": "Not found."})
        return self._row(o)

    def cancel_order(self, oid):
        o = self.orders.get(int(oid))
        if o is None:
            return False
        o["_cancel"] = self.cancel_reads
        return True

    def find_order_by_identifier(self, identifier):
        for oid, o in self.orders.items():
            if o["identifier"] == identifier:
                return self._row(self._visible(oid))
        return None

    def open_orders(self, symbol=None):
        return [self._row(o) for oid, o in list(self.orders.items())
                if self._visible(oid) is not None and o["state"] == "active"]

    def active(self, symbol=None):
        return [o for o in self.orders.values() if o["type"] == "limit" and o["state"] == "active"
                and (symbol is None or o["symbol"] == symbol)]


class LiveLadderTest(Base):
    def setUp(self):
        super(LiveLadderTest, self).setUp()
        self.client = LiveClient(self.ex)

    def live(self, cfg=None):
        from bitpin.broker import LiveBroker
        self.broker = LiveBroker(self.client, MARKETS, self.dir, min_order_irt=100000, sleep=self.clock.sleep,
                                 clock=self.clock, poll_interval=0.01, cancel_poll_attempts=2)
        self.rm = RiskManager({"min_order_irt": 100000}, self.dir, "live", clock=self.clock)
        c = dict(RUNNER_CFG)
        c.update(cfg or {})
        return Runner(None, self.broker, self.rm, c, self.dir, "live", bars_source=self.ex.bars, clock=self.clock,
                      sleep=self.clock.sleep, brain=self.brain, context_builder=self.builder)

    def test_live_bids_survive_restarts_and_an_ambiguous_placement_is_never_doubled(self):
        self.client.unknown_next = 1                        # the first bid: stored, but the answer is lost
        rep = self.live().run_once()
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(len(self.client.active()), 8)      # it exists on the exchange
        self.assertEqual(self.rm.orders_last_24h("limit"), 8)
        self.assertTrue(any("outcome unknown" in e for e in rep["errors"]))
        self.hour()
        self.live().run_once()                              # sync finds it by identifier: no second bid
        self.assertEqual(len(self.client.active()), 8)
        self.assertEqual(len([p for p in self.client.posts if p[2] == "limit"]), 8)
        self.hour()
        self.live().run_once()
        self.assertEqual(len([p for p in self.client.posts if p[2] == "limit"]), 8)

    def test_an_asynchronous_cancel_never_leaves_two_bids_on_one_level(self):
        self.live().run_once()
        self.client.cancel_reads = 50                        # the engine is slow to process cancels
        self.ex.set(BTC=80000 * 1.01)
        self.hour()
        self.live().run_once()
        btc = self.client.active("BTC_USDT")
        self.assertEqual(len(btc), 2)                        # the old bids, cancel pending: nothing new yet
        self.client.cancel_reads = 0
        for o in btc:
            o["_cancel"] = 0
        self.hour()
        self.live().run_once()
        btc = sorted(float(o["price"]) for o in self.client.active("BTC_USDT"))
        self.assertEqual(btc, [60600.0, 64640.0])            # the re-priced pair, one per level

    def test_a_live_fill_opens_a_position_and_a_target_sell(self):
        self.live().run_once()
        o = [o for o in self.client.active("BTC_USDT") if o["price"] == D("64000.000000")][0]
        self.ex.set(BTC=80000 * 0.79)
        self.client.fill(o["id"])
        self.hour()
        rep = self.live().run_once()
        self.assertEqual([f["reason"] for f in rep["limit_fills"]], ["ladder"])
        r = self.live()
        self.assertEqual(r._ladder_pos["BTC_IRT"]["source"], "ladder")
        sells = [x for x in self.client.active("BTC_USDT") if x["side"] == "sell"]
        self.assertEqual([float(x["price"]) for x in sells], [72000.0])
        self.set_stop(runner=self.live)                   # v3: the stop comes from Kimi's exits
        # the stop, later, sells what the target order locked: it is cancelled first
        self.ex.set(BTC=64000 * 0.85)
        self.client.cancel_reads = 0
        self.hour()
        rep = self.live().run_once()
        self.assertEqual([(f["symbol"], f["side"], f["reason"]) for f in rep["fills"]],
                         [("BTC_USDT", "sell", "stop")])
        self.assertEqual([x for x in self.client.active("BTC_USDT") if x["side"] == "sell"], [])
        self.assertLess(self.client.bal.get("BTC", ZERO), D("0.000001"))

    def test_a_rejected_market_is_not_retried_every_hour(self):
        self.client.reject = {"BTC_USDT"}              # e.g. below the exchange's own minimum there
        rep = self.live().run_once()
        posts = lambda: [p for p in self.client.posts if p[0] == "BTC_USDT" and p[2] == "limit"]
        self.assertEqual(len(posts()), 1)                 # the second BTC bid waits too
        self.assertEqual(len(self.client.active()), 6)
        self.assertTrue(any("BTC_USDT" in e for e in rep["errors"]))
        for _ in range(5):
            self.hour()
            self.live().run_once()
        self.assertEqual(len(posts()), 1)
        self.client.reject = set()
        self.hour(2)
        self.live().run_once()
        self.assertEqual(len(self.client.active("BTC_USDT")), 2)
