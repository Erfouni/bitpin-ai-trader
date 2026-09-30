"""Strategy runner: turns a backtested Strategy into paper/live orders with EXACTLY the backtest
contract (see bitpin/backtest.py):

1. Candle history is ANCHORED like the backtest's: seeded from the local data/<SYM>_60.csv cache
   (the files Panel.load reads) and extended with fresh closed 1h bars from the API; symbols
   without a CSV are downloaded from `history_start_ts`. The head is never dropped, so
   path-dependent strategies (state that starts at panel index 0) see the same history as in the
   backtest. The still-forming bar is dropped (data.closed_only) and bars are resampled with
   data.resample exactly like Panel.load does for the strategy's `res`.
2. strategy.weights(panel); the LAST row is the target (None/NaN/unlisted -> 0, clipped to
   [0, 1], normalised if the sum > 1) - identical to simulate().
3. Current weights = managed holdings valued at the latest CLOSED 1h close. Managed holdings are
   - without max_equity_irt: all IRT (cash) + all units of the strategy's coins;
   - with max_equity_irt: the bot's own SLEEVE, a ledger kept in runner state that starts with
     max_equity_irt IRT and no coins and changes only with the bot's own fills. Coins the user
     already held are never sold, gains are not skimmed off, losses are not refilled from the
     IRT reserve, and the drawdown breaker measures the sleeve. Fills are booked per order
     identifier as the delta against what the sleeve already booked for that order, and the
     sleeve is reconciled with the broker's durable order records (order journal / paper state)
     every cycle, so a fill is booked exactly once even if the process dies in between. An order
     of unknown outcome is never assumed "not executed" in capped mode (run_bot.py resolve-order).
4. Same rebalance rule as simulate(): skip |delta| < 0.02 unless it is a full exit; SELLS FIRST,
   then buys with the IRT actually available after the sells. Buys skipped by a temporary failure
   after the sells are retried within the same bar (at most MAX_BUY_RETRIES times).
5. The bar's ts is written to state BEFORE any order is sent, so a bar is never traded twice,
   also across restarts. Dry runs never send, cancel or journal an order and never write runner /
   risk / paper state. Orders of unknown outcome are looked up every cycle (every minute while
   any are open), also when the bar was already processed.
6. The drawdown breaker needs the breach on a second wallet read AND with the holdings valued at
   the order-book mid; the high-water mark rises only as far as both valuations agree. A flatten
   never re-sells a symbol that has an order of unknown outcome.

Timing: Bitpin's hourly bars open at hh:30 UTC (Tehran hh:00). A resampled bar of P seconds
(epoch-aligned bucket k) contains the 1h bars k+30min ... and closes at k + P + 30min. The loop
wakes `wake_delay_seconds` after each such close.

Brain mode (Runner(..., brain=KimiBrain, context_builder=MarketContextBuilder)): the targets come
from the Kimi LLM instead of a strategy; EVERYTHING after the targets is the strategy-mode path
(plan_orders with threshold / full exit, sells first, risk manager, order journal, sleeve and
max_equity_irt, write-ahead of the bar, same-bar buy retry, kill switch, drawdown breaker).
Every hour, shortly after the 1h bar close:
  a. the breaker and orders of unknown outcome are checked first (no Kimi call while halted/pending);
  b. brain.should_decide(quick_context, slack_seconds=BRAIN_SCHEDULE_SLACK) (a decision due before
     the next hourly check is taken now; pacing postponements are logged) -> if due: FIRST the
     stage-1 news brief (news.research(now, coin-name hint, abort=STOP check); cached ~2 h, never
     raises; skipped without a "news" section; a STOP created during it ends the hour), THEN
     builder.build() (so the prices are fetched after the news call) + brain.decide(news=brief)
     with the runner's own current weights (plan_orders' `cur`; the sleeve when
     capped), the STOP file as abort (context build and every LLM request) and min_trade_weight =
     min_order_irt * (1 + MIN_ORDER_HEADROOM) / managed equity, so no change the brain plans is
     below the risk manager's minimum order. decide() never raises; any other brain/context error
     is logged, never fatal;
  c. a VALID, not-HOLD decision is executed once (state brain_executed), never after its
     expires_at (default decided_at + 1 h) and not if the weights moved more than
     DECISION_DRIFT_LIMIT since it was computed (computed_against);
  d. otherwise, ONLY if the latest decision is invalid, or no decision was due but IRT cash is
     above the brain's cash limit (brain.fallback_cash_limit(now): min(max_irt_cash,
     fallback.sweep_irt_above), or the toman share of its last valid decision while that is younger
     than fallback.derisk_after_hours) by at least one executable step: brain.fallback_decision(current,
     now, balances_irt_value, min_trade_weight) -> "cash_sweep" (excess
     toman into USDT_IRT, coins untouched) or "derisk" (coins into USDT_IRT after a long outage) or
     None = no trade. Fallbacks skip the brain's turnover cap but not the risk manager;
  e. one line per bar in state_dir/kimi_runner.jsonl (decision -> action -> plan -> fills).

Crash ladder, code exits, routing (brain mode with a KimiBrain that has ladder_in_force(); config
sections "ladder", "exits", "routing"; the research spec is scratch/freq/final_recommendation.json).
Every hourly check, in this order:
  1. RESTING ORDERS: broker.sync_limits() (the exchange is the source of truth, also right after a
     restart), then every unacknowledged limit fill (broker.limit_fill_events) is handled and acked:
     a ladder fill disarms its level (W1 for the brain), every fill goes to bot_events.jsonl, and the
     sleeve / the positions book it from broker.fill_records() (cumulative, exactly once).
  2. POSITIONS: every coin the bot manages (ladder fill, Kimi buy, or already held) is guarded by at
     most TWO positions, each with its own average entry in USDT terms (from the bot's own fills), stop
     (v3: NONE unless Kimi set stop_pct for the position), target, max hold (720 h, capped by the
     endgame) and wake levels: the ALLOCATION
     position ("main": Kimi buys and coins already held; target only when Kimi sets one) and the
     CRASH-LADDER position ("ladder": the ladder fills of that coin; target half the 48 h drop
     regained). A ladder fill never changes the entry, stop or target of the allocation position. The
     exits of the last valid Kimi decision (Decision.exits) apply to the allocation position, or to the
     ladder position when the coin has no allocation position. Sales are booked against the position
     they served (a target sell / a code exit), anything else against the ladder position first.
  3. CODE EXITS (no LLM): an hourly close (COIN_IRT / USDT_IRT) at or below a position's stop -> market
     sell of THAT position's amount (all managed units when every position of the coin exits) into
     USDT through the cheaper route (COIN_USDT directly, or COIN_IRT + USDT_IRT); a close at or above
     its target -> the same sell (normally its resting target sell has filled already).
     KIMI'S PLANS: the entry plan of a Kimi decision (Decision.plans: setup, horizon, invalidation
     level, take-profit, a sanitised note) is kept WITH the allocation position it opens or adds to
     (position "plan", runner state, restart-safe; pending specs "plan_specs" for PLAN_SPEC_TTL), and a
     position without one - or whose plan is broken / past its horizon (a restated thesis) - takes the
     plan a later decision gives for it; a decision that opens the allocation position of a coin held
     only as a crash-ladder lot never puts that plan on the ladder lot. An hourly close below the
     invalidation level marks the plan broken (_check_plans): a W3 wake-up for the brain, never a sale.
  4. KIMI: should_decide(ladder=, positions=) (clock slots, W1-W4, pacing - all in the brain), a
     forced / focused news brief for a veto or fill review, build + decide in the brain's mode; the
     ladder scales, exits and plans of EVERY valid non-fallback decision are applied (also a HOLD, an
     expired or a stale one), once per decision. W4 below the coin weight and W5 are notifications only.
     PUMP GUARD: before a Kimi decision is executed its targets are re-checked on the runner's own hourly
     candles (_pump_clamp): a coin that rose guard.pump_rise_pct within pump_window_hours in the last
     pump_lookback_hours is not raised above its current weight (the rest to USDT_IRT; sales untouched);
     the clamp is a decision adjustment (also in the brain's recent_decisions) and a "pump_guard" bot
     event, and _unexecutable_legs compares against the clamped targets. The same guard keeps a
     guarded LADDER coin's bids off (_maintain_ladder: resting ones cancelled, none placed or re-armed,
     one log line + "pump_guard" event per pump) until it ends.
  5. EXECUTION (unchanged contract: threshold, sells first, sleeve, journal, write-ahead): the plan's
     USDT <-> coin legs go directly through COIN_USDT when markets.best_route says that is cheaper
     (routing.coins, default BTC ETH XRP SOL). Before a plan runs, the bot's own resting orders that
     lock what it spends are cancelled (the target sell of a coin it sells; the ladder bids when it
     spends more USDT than is free) - the allocation always wins, the ladder is re-sized after it.
  6. MAINTENANCE: target sells (resting maker sells on COIN_USDT, one per position, each sized to its
     position when the coin has two) and ladder bids (resting maker buys
     on COIN_USDT at levels_pct below the highest of the last 48 hourly closes in USDT terms,
     size_frac of equity each, times Kimi's per-coin scale, pro rata to the USDT the book holds, never
     more than the free USDT). An order is replaced only when its price moves more than reprice_pct
     or its size more than resize_pct, and only after the replacement passed RiskManager.vet_limit
     (a refused replacement keeps the old order). A filled level is re-armed when the coin closes
     above -rearm_pct from its 48 h high. From the endgame cut-off (brain endgame no_new_entries,
     2026-10-17 13:00 Tehran) every ladder bid is cancelled; a halt and the STOP kill switch cancel them
     too. A market / side
     whose resting order the exchange rejected (4xx) waits REJECT_BACKOFF before the next attempt; while
     it waits, a resting order of that market / side is never cancelled for a re-price or re-size (the
     replacement could not be placed).
  Idempotency: the broker's journal (identifier, tag, meta) - not runner memory - says which resting
  orders exist, so a restart never duplicates a bid; the bar is written ahead before the first market
  order of the cycle; fills are booked per identifier as cumulative deltas; limit fills are acked
  only after they were handled (at-least-once, handled idempotently).
  state_dir/bot_events.jsonl: one JSON line per event for the Telegram notifier - decision, fill
  (route; reason allocation / ladder / stop / target / cash_sweep / derisk / endgame), order_place /
  order_cancel (tag ladder / target), ladder_rearm, exit, position_open / position_close, watchdog,
  notify (W4 below the coin weight, W5), fallback, endgame (ladder_off / final_decision /
  default_to_usdt). Each has "t", "kind", "mode" and a stable "id" (the same event re-emitted after a
  restart has the same id).
  Resting orders stay on the exchange while the bot is between checks (that is their point: crash
  entries and targets at zero latency). The STOP kill switch (and a halt) CANCELS the crash-ladder
  bids when the running bot sees it (run_once and loop): a stopped bot enforces no code exits, so it
  must not keep buying. The target sells stay (they only sell a position at its target). A bot that
  is down for another reason (crash loop, exit 78, systemctl stop) leaves both on Bitpin: run
  `run_bot.py cancel-resting` for that.
"""
import dataclasses
import hashlib
import inspect
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import data as data_mod
from .api import (AuthBudgetExceeded, AuthError, BitpinAPIError, OrderNotSent, OrderStatusUnknown, atomic_write_json,
                  read_json)
from .backtest import Panel
from .broker import BrokerError, InsufficientFunds, LimitWouldCross, NotBotOrder, WalletDataError
from .markets import IRT, USDT, ZERO, D, best_ask, best_bid, fmt_amount, mid_price, parse_symbol

log = logging.getLogger("bitpin.runner")

HOUR = 3600
TEHRAN = timezone(timedelta(hours=3, minutes=30))
RESEARCH_HISTORY_START = 1712313000   # first bar of the research CSVs in data/ (2024-04-05 10:30 UTC)
STALE_ALERT_AFTER = 3                 # consecutive stale bars before a loud clock/feed alert
FETCH_AHEAD = 6 * HOUR                # candle requests reach this far past the local clock
MAX_BUY_RETRIES = 5                   # re-tries of a bar's buy leg after a transient failure
BRAIN_HISTORY_BARS = 100              # brain mode: hourly candles the runner loads at a start (valuation and its own
#                                       re-check of the pump guard: 72 h lookback + a 24 h window)
DECISION_TTL = HOUR                   # a decision without expires_at expires 1 h after decided_at
# A decision is not executed at all if the weights moved more than this since it was made. It is
# deliberately larger than MIN_ORDER_HEADROOM: no drift limit can guarantee that every leg is still
# executable (a leg planned at exactly min_order_irt*(1+MIN_ORDER_HEADROOM) survives a drift of only
# min_order_irt*MIN_ORDER_HEADROOM/equity, about 0.08% of a 3.6M toman account), so tightening it would
# drop whole decisions - and trade nothing for an hour - without buying that guarantee. Instead
# _unexecutable_legs names the legs the drift killed, in the log and in kimi_runner.jsonl.
DECISION_DRIFT_LIMIT = 0.05
BRAIN_RUNNER_LOG = "kimi_runner.jsonl"   # one line per processed bar in brain mode (decision -> execution)
BRAIN_SCHEDULE_SLACK = 15 * 60        # a Kimi decision due within this long after the hourly check is taken now
MIN_ORDER_HEADROOM = 0.03             # brain's smallest change = min_order_irt * (1 + this) / equity (price moves)
SAFE = "USDT_IRT"                     # the base asset of the brain (USDT, valued in toman)
EVENTS_LOG = "bot_events.jsonl"       # structured events for the Telegram notifier (brain mode)
LADDER_TAG = "ladder"                 # broker tag of the crash-ladder bids
TARGET_TAG = "target"                 # broker tag of the code exits' resting target sells
WATCHDOG_KINDS = ("review", "veto", "held", "drawdown")   # W1-W4 trigger kinds (KimiBrain.last_trigger_kind)
EXIT_SPEC_KEYS = ("stop_pct", "target_price", "target_rule", "max_hold_hours", "max_hold_until", "wake_up_pct",
                  "wake_levels", "source")
POSITION_VIEW_KEYS = ("entry_ts", "entry_px_usdt", "amount", "source", "stop_pct", "stop_px_usdt", "target_px_usdt",
                      "max_hold_until", "wake_up_pct", "wake_levels", "plan")
LADDER_LOT_VIEW_KEYS = ("entry_ts", "entry_px_usdt", "amount", "stop_px_usdt", "target_px_usdt", "max_hold_until")
LOT_MAIN, LOT_LADDER = "main", "ladder"   # the two positions a coin can have (allocation / crash ladder)
LADDER_REFILL_REVIEW = 0.25           # a disarmed ladder order that filled this much more of its size gets a new W1
EXIT_SPEC_TTL = 6 * HOUR              # a Kimi exit spec applies to a position its decision opened within this long
PLAN_SPEC_TTL = EXIT_SPEC_TTL         # ... and so does its entry plan (Decision.plans) to the Kimi buy it opens / adds
RECENT_EXITS_KEEP = 72 * HOUR         # code exits shown to Kimi (context recent_exits; no re-entry within 24 h)
REJECT_BACKOFF = 6 * HOUR             # a resting order the exchange rejected (4xx) is not re-sent on that market for this long

# The crash ladder (research spec: resting USDT-backed maker bids at -20% / -25% below the highest of
# the last 48 hourly closes in USDT terms, 12.5% of equity each, on BTC/ETH/XRP/SOL_USDT).
DEFAULT_LADDER = {
    "enabled": True,          # needs brain mode with a KimiBrain that has ladder_in_force()
    "coins": None,            # null = brain.ladder_coins (default BTC ETH XRP SOL)
    "levels_pct": [-20.0, -25.0],
    "size_frac": 0.125,       # of the managed equity per bid, times Kimi's scale for the coin
    "rearm_pct": 7.5,         # a filled level is re-armed when the coin closes above -this from its 48 h high
    "lookback_hours": 48,
    "reprice_pct": 0.5,       # a resting bid is replaced only when its price moves more than this ...
    "resize_pct": 10.0,       # ... or its size more than this
}
# Code exits on every coin position (stop / target / max hold / wake levels; the levels themselves come
# from the brain's exit specs: v3 has NO default stop (Kimi sets stop_pct per position), target half the
# 48 h drop for a ladder fill, max hold 720 h).
DEFAULT_EXITS = {
    "enabled": True,
    "target_orders": True,    # keep the target as a resting maker sell on COIN_USDT (else: sold at the close)
    "reprice_pct": 0.5,
    "resize_pct": 10.0,
}
# Execution through COIN_USDT when markets.best_route says it is cheaper than COIN_IRT + USDT_IRT.
DEFAULT_ROUTING = {"enabled": True, "coins": ["BTC", "ETH", "XRP", "SOL"]}   # coins null = every coin
SECTION_DEFAULTS = {"ladder": DEFAULT_LADDER, "exits": DEFAULT_EXITS, "routing": DEFAULT_ROUTING}

DEFAULT_CONFIG = {
    "strategy": "selected",
    "params": {},
    "base_url": "https://api.bitpin.org",
    "state_dir": "state",
    "irt_asset_code": "IRT",
    "irt_unit_divisor": 1,
    "fee_rate": 0.0035,
    "rebalance_threshold": 0.02,
    "cash_buffer_frac": 0.002,
    "max_equity_irt": None,
    "history_start_ts": RESEARCH_HISTORY_START,
    "history_csv_seed": True,
    "lookback_bars": 9000,
    "wake_delay_seconds": 60,
    "data_retry_seconds": 20,
    "data_max_wait_seconds": 600,
    "error_backoff_seconds": 60,
    "error_backoff_max_seconds": 900,
    "order_poll_interval_seconds": 1.0,
    "order_poll_timeout_seconds": 30,
    "unknown_order_max_age_seconds": 900,
    "risk": {},
    "ladder": DEFAULT_LADDER,
    "exits": DEFAULT_EXITS,
    "routing": DEFAULT_ROUTING,
}

FATAL_ERRORS = (AuthError, AuthBudgetExceeded)


class StaleData(Exception):
    """The newest closed candle is not available yet. kind: 'missing' (the expected bar is not
    there) or 'not_opened' (the exchange has not started the next bar yet)."""

    def __init__(self, message, kind="missing"):
        super().__init__(message)
        self.kind = kind


class RunnerError(Exception):
    """Configuration / strategy contract violation."""


# --------------------------------------------------------------------------- config

def _num_in(where, v, lo, hi, lo_open=False):
    # read_json (config files) parses JSON numbers with a fraction as Decimal: accepted like floats
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        raise RunnerError("%s must be a number, got %r" % (where, v))
    x = float(v)
    if not math.isfinite(x) or x > hi or x < lo or (lo_open and x <= lo):
        raise RunnerError("%s must be %s %g and <= %g, got %r" % (where, ">" if lo_open else ">=", lo, hi, v))
    return x


def _coin_list(where, v):
    if v is None:
        return None
    if not isinstance(v, (list, tuple)) or not all(isinstance(c, str) and c.strip() for c in v):
        raise RunnerError("%s must be null or a list of coin names like [\"BTC\", \"ETH\"]" % where)
    out = []
    for c in v:
        c = c.strip().upper()
        for suffix in ("_USDT", "_IRT"):
            if c.endswith(suffix):
                c = c[:-len(suffix)]
        if c in (USDT, IRT):
            raise RunnerError("%s: %s is not a coin" % (where, c))
        if c not in out:
            out.append(c)
    return out


def _validate_sections(cfg):
    """Type / range checks of the ladder, exits and routing sections (in place)."""
    lc, ec, rc = cfg["ladder"], cfg["exits"], cfg["routing"]
    for sec, key in (("ladder", "enabled"), ("exits", "enabled"), ("exits", "target_orders"), ("routing", "enabled")):
        if not isinstance(cfg[sec][key], bool):
            raise RunnerError("%s.%s must be true or false" % (sec, key))
    lc["coins"] = _coin_list("ladder.coins", lc["coins"])
    rc["coins"] = _coin_list("routing.coins", rc["coins"])
    lv = lc["levels_pct"]
    if not isinstance(lv, (list, tuple)) or not 1 <= len(lv) <= 6:
        raise RunnerError("ladder.levels_pct must be a list of 1 to 6 percentages below the 48 h high, e.g. [-20, -25]")
    levels = []
    for x in lv:
        x = -abs(_num_in("ladder.levels_pct", x, -90.0, 90.0))
        if not -90.0 <= x <= -1.0:
            raise RunnerError("ladder.levels_pct: each level must be 1..90%% below the 48 h high, got %r" % (x,))
        if x in levels:
            raise RunnerError("ladder.levels_pct: level %g appears twice" % x)
        levels.append(x)
    lc["levels_pct"] = levels
    lc["size_frac"] = _num_in("ladder.size_frac", lc["size_frac"], 0.0, 1.0, lo_open=True)
    lc["rearm_pct"] = _num_in("ladder.rearm_pct", lc["rearm_pct"], 0.0, 50.0, lo_open=True)
    lc["lookback_hours"] = int(_num_in("ladder.lookback_hours", lc["lookback_hours"], 2, 500))
    for sec in (lc, ec):
        name = "ladder" if sec is lc else "exits"
        sec["reprice_pct"] = _num_in("%s.reprice_pct" % name, sec["reprice_pct"], 0.0, 20.0)
        sec["resize_pct"] = _num_in("%s.resize_pct" % name, sec["resize_pct"], 0.0, 100.0)


def load_config(path=None, overrides=None):
    cfg = dict(DEFAULT_CONFIG)
    cfg["risk"] = {}
    for sec, dflt in SECTION_DEFAULTS.items():
        cfg[sec] = json.loads(json.dumps(dflt))           # a deep copy: the defaults are never mutated
    src = {}
    if path:
        src = read_json(path)
        if src is None:
            raise RunnerError("config file not found: %s" % path)
        if not isinstance(src, dict):
            raise RunnerError("config file %s must contain a JSON object {...}" % path)
    for layer in (src, overrides or {}):
        for k, v in layer.items():
            if k.startswith("_"):
                continue
            if k not in DEFAULT_CONFIG:
                raise RunnerError("unknown config key %r (known: %s)" % (k, sorted(DEFAULT_CONFIG)))
            if k == "risk":
                cfg["risk"].update({rk: rv for rk, rv in (v or {}).items() if not rk.startswith("_")})
            elif k in SECTION_DEFAULTS:
                if v is None:
                    continue
                if not isinstance(v, dict):
                    raise RunnerError("config key %r must be an object {...}" % k)
                for sk, sv in v.items():
                    if str(sk).startswith("_"):
                        continue
                    if sk not in SECTION_DEFAULTS[k]:
                        raise RunnerError("unknown %s key %r (known: %s)" % (k, sk, sorted(SECTION_DEFAULTS[k])))
                    # a deep copy with every Decimal as a float: read_json parses 17.5 as Decimal, which
                    # json.dumps cannot serialise ("levels_pct": [-17.5, -22.5] crashed the bot)
                    cfg[k][sk] = _plain(sv) if isinstance(sv, (list, tuple, dict)) else sv
            elif v is not None or k in ("max_equity_irt", "history_start_ts"):
                cfg[k] = v
    _validate_sections(cfg)
    if cfg["max_equity_irt"] is not None:
        try:
            cap = D(cfg["max_equity_irt"])
        except ValueError:
            raise RunnerError("max_equity_irt must be a number or null, got %r" % (cfg["max_equity_irt"],))
        if cap <= 0:
            raise RunnerError("max_equity_irt must be > 0 (or null for no cap), got %s. To trade nothing, do not "
                              "start the bot (or create the STOP file)." % cfg["max_equity_irt"])
    if cfg["history_start_ts"] is not None:
        try:
            int(cfg["history_start_ts"])
        except (TypeError, ValueError):
            raise RunnerError("history_start_ts must be an epoch-seconds integer or null")
    return cfg


# --------------------------------------------------------------------------- time helpers

def fmt_ts(ts):
    if ts is None:
        return "-"
    u = datetime.fromtimestamp(ts, tz=timezone.utc)
    return "%s UTC (%s Tehran)" % (u.strftime("%Y-%m-%d %H:%M"), u.astimezone(TEHRAN).strftime("%H:%M"))


def expected_last_closed_1h(now, offset):
    """Open ts of the newest 1h bar that has closed at `now` (hourly bars open at k*3600+offset)."""
    return ((int(now) - offset) // HOUR) * HOUR + offset - HOUR


def next_bar_close(now, res_seconds, offset):
    """First close time > now of a strategy bar. For 1h bars closes are at n*3600+offset; for a
    resampled bar of P seconds (epoch-aligned bucket) closes are at n*P+offset."""
    n = (int(now) - offset) // res_seconds + 1
    return n * res_seconds + offset


def bar_close_time(bar_ts, res_seconds, offset):
    if res_seconds == HOUR:
        return bar_ts + HOUR
    return bar_ts + res_seconds + offset


# --------------------------------------------------------------------------- targets + plan

def compute_targets(strategy, panel):
    """Last-row target weights with the same sanitising as backtest.simulate()."""
    w = strategy.weights(panel)
    if not isinstance(w, dict):
        raise RunnerError("%s.weights() must return a dict" % strategy.name)
    T = len(panel)
    out = {}
    for s in panel.symbols:
        if s not in w:
            continue
        seq = w[s]
        if len(seq) != T:
            raise RunnerError("weights[%s] has length %d, panel has %d" % (s, len(seq), T))
        x = seq[-1]
        close = panel[s]["close"][-1]
        try:
            xf = None if x is None else float(x)
        except (TypeError, ValueError):
            xf = None
        if xf is None or math.isnan(xf) or close is None:
            xf = 0.0
        out[s] = min(max(xf, 0.0), 1.0)
    tot = sum(out.values())
    if tot > 1.0 + 1e-9:
        out = {s: v / tot for s, v in out.items()}
    return {s: D(repr(v)) for s, v in out.items()}


@dataclass
class PlannedOrder:
    symbol: str
    side: str
    amount: Decimal          # sell: base units; buy: quote to spend (IRT on COIN_IRT, USDT on COIN_USDT)
    est_notional: Decimal    # IRT at the reference close
    target: Decimal
    current: Decimal
    full_exit: bool = False
    quote: str = IRT         # the market's quote currency (what a buy spends / a sell receives)
    route: str = None        # "direct" (COIN_USDT), "via_irt", "single" or None (the plan's own leg)
    reason: str = None       # allocation / stop / target / cash_sweep / derisk / endgame (reports, events)
    serves: str = None       # the managed symbol this leg moves (BTC_IRT for a BTC_USDT leg)

    def describe(self):
        via = " on %s (%s route)" % (self.symbol, self.route) if self.route == "direct" else ""
        if self.side == "sell":
            return "SELL %s %s (~%s IRT)%s%s" % (fmt_amount(self.amount), parse_symbol(self.symbol)[0],
                                               fmt_amount(self.est_notional), " [full exit]" if self.full_exit else "",
                                               via)
        return "BUY %s with %s %s%s" % (parse_symbol(self.symbol)[0], fmt_amount(self.amount), self.quote, via)


@dataclass
class Plan:
    equity: Decimal
    effective_equity: Decimal
    cash: Decimal
    targets: dict
    current: dict
    orders: list = field(default_factory=list)
    skipped: list = field(default_factory=list)

    @property
    def sells(self):
        return [o for o in self.orders if o.side == "sell"]

    @property
    def buys(self):
        return [o for o in self.orders if o.side == "buy"]


def plan_orders(targets, units, closes, cash, threshold="0.02", fee_rate="0.0035", equity_cap=None, dust_irt=ZERO):
    """Mirror of backtest.simulate()'s rebalance step, in Decimal.
    targets/units/closes: {symbol: Decimal}; cash: IRT. Sells are listed before buys.
    `units`/`cash` must be what the bot manages (the runner passes its sleeve when capped);
    `equity_cap` is a legacy option that only scales targets and is not used by the runner."""
    threshold, fee_rate, dust_irt = D(threshold), D(fee_rate), D(dust_irt)
    syms = sorted(targets)
    equity = D(cash) + sum((units.get(s, ZERO) * closes[s] for s in syms if closes.get(s) is not None), ZERO)
    eff = min(equity, D(equity_cap)) if equity_cap is not None else equity
    cur = {}
    for s in syms:
        c = closes.get(s)
        cur[s] = (units.get(s, ZERO) * c / eff) if (c is not None and eff > 0) else ZERO
    plan = Plan(equity, eff, D(cash), dict(targets), cur)
    order = sorted(syms, key=lambda s: targets[s] - cur[s])  # stable: same tie order as simulate()
    est_cash = D(cash)
    for s in order:
        c = closes.get(s)
        if c is None:
            continue
        t, u = targets[s], units.get(s, ZERO)
        d = t - cur[s]
        full_exit = t == 0 and u > 0
        if abs(d) < threshold and not full_exit:
            if d != 0:
                plan.skipped.append((s, "delta %+.4f below rebalance threshold %s" % (d, threshold)))
            continue
        if d < 0:
            q = u if full_exit else min(u, -d * eff / c)
            if full_exit and q * c < dust_irt:
                plan.skipped.append((s, "dust position (~%s IRT) below minimum order" % fmt_amount(q * c)))
                continue
            plan.orders.append(PlannedOrder(s, "sell", q, q * c, t, cur[s], full_exit))
            est_cash += q * c * (1 - fee_rate)
        elif d > 0:
            notional = min(d * eff, est_cash)
            if notional <= 0:
                plan.skipped.append((s, "no cash left for buy"))
                continue
            plan.orders.append(PlannedOrder(s, "buy", notional, notional, t, cur[s]))
            est_cash -= notional
    return plan


# --------------------------------------------------------------------------- crash ladder helpers (pure)

def _plain(obj):
    """A JSON-like structure with every Decimal as a float (state read back by read_json)."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _fmt_px(x):
    """A price / amount for the log (None -> "-")."""
    try:
        return "-" if x is None else "%.8g" % float(x)
    except (TypeError, ValueError):
        return str(x)


def _fmt_frac(x):
    try:
        return "-" if x is None else "%.2f%%" % (float(x) * 100)
    except (TypeError, ValueError):
        return "-"


def lvl_key(level):
    """The state key of a ladder level: -20.0 -> "-20"."""
    try:
        return "%g" % float(level)
    except (TypeError, ValueError):
        return str(level)


def needs_replace(old_price, old_base, new_price, new_base, reprice_pct, resize_pct):
    """True when a resting order at (old_price, old_base) should be replaced by (new_price,
    new_base): the price moved more than reprice_pct or the size more than resize_pct (both %).
    Hysteresis, so an hourly re-computation does not cancel and re-place an order for noise."""
    try:
        op, ob, np_, nb = float(old_price), float(old_base), float(new_price), float(new_base)
    except (TypeError, ValueError):
        return True
    if op <= 0 or ob <= 0:
        return True
    if abs(np_ / op - 1.0) * 100.0 > float(reprice_pct) + 1e-9:
        return True
    return abs(nb / ob - 1.0) * 100.0 > float(resize_pct) + 1e-9


def ladder_plan(refs, scales, armed, levels, size_frac, equity_irt, usdt_rate, usdt_budget, min_usdt):
    """The resting bids the ladder wants: {(coin, level_key): {"coin", "level_pct", "price_usdt",
    "usdt"}} and the pro-rata factor. refs: {coin: {"hi48": USDT}} (coins without one are skipped);
    scales: {coin: 0..1}; armed: {(coin, level_key): bool} (missing = armed). Each bid is size_frac *
    scale * equity_irt / usdt_rate USDT at hi48 * (1 + level/100); when their sum is more than
    usdt_budget every bid is scaled down by the same factor (the ladder never plans more USDT than the
    book holds); a bid below min_usdt after that is dropped."""
    want = {}
    rate = float(usdt_rate or 0)
    if rate <= 0 or float(equity_irt or 0) <= 0:
        return {}, 0.0
    for coin in sorted(refs):
        ref = refs.get(coin) or {}
        hi = ref.get("hi48")
        sc = float(scales.get(coin, 0.0) or 0.0)
        if not hi or hi <= 0 or sc <= 0:
            continue
        for lv in levels:
            k = (coin, lvl_key(lv))
            if armed.get(k, True) is False:
                continue
            want[k] = {"coin": coin, "level_pct": float(lv), "price_usdt": float(hi) * (1.0 + float(lv) / 100.0),
                       "usdt": float(size_frac) * min(1.0, sc) * float(equity_irt) / rate}
    total = sum(w["usdt"] for w in want.values())
    if total <= 0:
        return {}, 0.0
    factor = max(0.0, min(1.0, float(usdt_budget or 0) / total))
    out = {}
    for k, w in want.items():
        u = w["usdt"] * factor
        if u >= float(min_usdt or 0) and u > 0:
            out[k] = dict(w, usdt=u)
    return out, factor


# --------------------------------------------------------------------------- process locks

class StateLock:
    """Exclusive lock on a lock file (default state_dir/bot.lock) so two bots never trade the same
    state / account. Take it BEFORE reading any state file."""

    def __init__(self, state_dir, name="bot.lock", what=None):
        self.path = os.path.join(state_dir, name)
        self.what = what or ("state dir %s" % state_dir)
        self._f = None

    def acquire(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise RunnerError("another bot instance is already running with %s (lock %s)" % (self.what, self.path))
        self._f = f
        return self

    def is_free(self):
        """True if nobody holds the lock right now (takes and releases it)."""
        try:
            self.acquire()
        except RunnerError:
            return False
        self.release()
        return True

    def release(self):
        if self._f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._f.close()
        self._f = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def account_lock_dir():
    """User-level directory for per-account locks (shared by every state dir on this machine)."""
    d = os.environ.get("BITPIN_BOT_LOCK_DIR")
    if d:
        return d
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "bitpin-bot")


def account_lock(fingerprint):
    """One live bot per Bitpin account, whatever --state-dir / --config it was started with."""
    return StateLock(account_lock_dir(), "account-%s.lock" % fingerprint,
                     what="the same Bitpin API key (account lock)")


# --------------------------------------------------------------------------- runner

class BrainStrategy(object):
    """Stand-in for a Strategy in brain mode: the symbols KimiBrain may allocate to (USDT_IRT
    included). Its targets come from KimiBrain decisions, never from weights()."""
    name = "kimi"
    res = "60"

    def __init__(self, symbols):
        self.symbols = list(dict.fromkeys(symbols))
        self.params = {}

    def weights(self, panel):
        raise RunnerError("brain mode: the targets come from KimiBrain, not from a strategy")

    def __repr__(self):
        return "KimiBrain(%d symbols)" % len(self.symbols)


class Runner:
    def __init__(self, strategy, broker, risk, config=None, state_dir="state", mode="paper", dry_run=False,
                 bars_source=None, clock=time.time, sleep=time.sleep, seed_source="default", brain=None,
                 context_builder=None, news=None):
        """strategy mode: `strategy` is a Strategy. Brain mode: brain=KimiBrain and
        context_builder=MarketContextBuilder (ONE instance for the process, it keeps a candle cache);
        `strategy` may then be None (a BrainStrategy over brain.allowed is used). news: the stage-1
        NewsResearcher (bitpin/news.py) or None; asked right before every Kimi decision (brain mode only)."""
        if brain is not None:
            if context_builder is None:
                raise RunnerError("brain mode needs a MarketContextBuilder (context_builder=...)")
            strategy = BrainStrategy(brain.allowed)
            seed_source = None            # no strategy history: only recent closes are needed
        elif strategy is None:
            raise RunnerError("a strategy (or a brain) is required")
        self.brain, self.builder = brain, context_builder
        self.news = news if brain is not None else None      # stage 1 (NewsResearcher) or None
        self.strategy, self.broker, self.risk = strategy, broker, risk
        self.cfg = load_config(None, config)
        self.state_dir, self.mode, self.dry_run = state_dir, mode, dry_run
        self.clock, self.sleep = clock, sleep
        self.res = str(strategy.res)
        if self.res not in data_mod.RES_SECONDS or data_mod.RES_SECONDS[self.res] % HOUR:
            raise RunnerError("strategy res %r is not a multiple of 1h" % self.res)
        self.res_seconds = data_mod.RES_SECONDS[self.res]
        for s in strategy.symbols:
            if parse_symbol(s)[1] != "IRT":
                raise RunnerError("runner trades IRT-quoted markets only; %s is not" % s)
        # same panel universe as research.load_panel_for (strategy symbols + USDT_IRT)
        self.symbols = list(dict.fromkeys(list(strategy.symbols) + ["USDT_IRT"]))
        self.bars_source = bars_source or data_mod.fetch_bars
        if seed_source == "default":
            seed_source = data_mod.load_csv if self.cfg["history_csv_seed"] else None
        self.seed_source = seed_source
        self.threshold = D(self.cfg["rebalance_threshold"])
        self.fee_rate = D(self.cfg["fee_rate"])
        self.cash_buffer = D(self.cfg["cash_buffer_frac"])
        cap = self.cfg.get("max_equity_irt")
        self.equity_cap = D(cap) if cap is not None else None
        self.state_path = os.path.join(state_dir, "runner_state_%s.json" % mode)
        self.state = read_json(self.state_path) or {}
        self._sleeve = None
        self.extra_symbols = []
        if self.equity_cap is None and self.state.get("sleeve"):
            log.warning("runner state has a bot sleeve from a run with max_equity_irt, but max_equity_irt is not set "
                        "now: the bot manages the WHOLE toman balance and all strategy coins; the sleeve is dropped")
            self.state.pop("sleeve", None)
        elif self.equity_cap is not None and self.state.get("sleeve"):
            raw = self.state["sleeve"]
            # a lowered cap that the sleeve's cash cannot absorb is refused at start, not every cycle
            self._cash_after_cap_change(D(raw.get("budget")), D(raw.get("cash")), self.equity_cap)
            owned = {s for s, u in (raw.get("units") or {}).items() if D(u) > 0}
            # coins the sleeve bought for a different strategy: priced separately and exited (target 0)
            self.extra_symbols = sorted(owned - set(self.symbols))
            if self.extra_symbols:
                log.warning("the bot sleeve holds %s, which %s does not trade: they will be sold",
                            ", ".join(self.extra_symbols), strategy.name)
        self._cache = {}
        self._server_newest = {}
        # the last close this bot ever saw per symbol, across restarts: a held coin whose candle
        # endpoint answers [] must never be valued at 0 (_remember_prices / _prices)
        self._last_prices, self._price_seen = {}, {}
        for s, v in (self.state.get("last_prices") or {}).items():
            at = 0.0
            if isinstance(v, dict):
                v, at = v.get("px"), v.get("at") or 0.0
            try:
                p, at = D(str(v)), float(at)
            except Exception:  # noqa: BLE001 - a corrupt entry is simply not used
                continue
            if p > 0:
                self._last_prices[s], self._price_seen[s] = p, at
        self._anchor = None
        self._history_checked = False
        self._stale_bars = 0
        self._clock_behind = None
        self._transient_skip = False
        self._frozen_seen = ()          # last reported total-vs-available gap (log it only when it changes)
        self._exec_deadline = None      # brain mode: the executing decision's expires_at (no order after it)
        self.offset = None
        broker.track({parse_symbol(s)[0] for s in self.symbols + self.extra_symbols} | {"IRT"})
        # A capped sleeve cannot see in the wallet whether an unknown order executed (the user's own
        # coins and toman share the account), so such an order is never silently aged out.
        broker.expire_unknown_orders = not self.capped
        self._init_bot_features()

    def _init_bot_features(self):
        """Crash ladder / code exits / routing (brain mode). The ladder and the exits need a brain
        that speaks the ladder contract (KimiBrain.ladder_in_force etc.); an older brain (or a test
        double) runs without them, exactly as before."""
        b = self.brain
        self._v2 = b is not None and callable(getattr(b, "ladder_in_force", None)) \
            and callable(getattr(b, "endgame_flags", None))
        lc, ec, rc = self.cfg["ladder"], self.cfg["exits"], self.cfg["routing"]
        self.ladder_on = bool(self._v2 and lc["enabled"])
        self.exits_on = bool(self._v2 and ec["enabled"])
        self.routing_on = bool(b is not None and rc["enabled"])
        self.ladder_levels = [float(x) for x in lc["levels_pct"]]
        coins = lc["coins"] if lc["coins"] is not None else [str(c).upper() for c in
                                                            (getattr(b, "ladder_coins", None) or [])]
        self.ladder_coins = []
        for c in coins if self._v2 else []:
            if c + "_IRT" not in self.symbols:
                log.warning("ladder coin %s is not traded by the brain (%s_IRT not in allowed_symbols): no ladder bids "
                            "for it", c, c)
                continue
            self.ladder_coins.append(c)
        self.routing_coins = None if rc["coins"] is None else set(rc["coins"])
        st = self.state
        lad = st.get("ladder") if isinstance(st.get("ladder"), dict) else {}
        # read_json gives Decimals: the ladder / position state is kept in floats (it is only compared
        # and passed to the brain; order amounts are always taken from the broker and the wallet)
        self._ladder_st = _plain(lad)
        self._ladder_st.setdefault("coins", {})
        self._positions = {k: _plain(v) for k, v in (st.get("positions") or {}).items() if isinstance(v, dict)}
        self._ladder_pos = {k: _plain(v) for k, v in (st.get("ladder_positions") or {}).items()
                            if isinstance(v, dict)}
        for k in [k for k, v in self._positions.items() if v.get("source") == LADDER_TAG and k not in self._ladder_pos]:
            # an older version merged ladder fills into the coin's one position (source "ladder"): it is
            # the coin's crash-ladder position now (its exits are the ladder's)
            self._ladder_pos[k] = self._positions.pop(k)
        sa = st.get("sell_attr")
        self._sell_attr = {str(k): str(v) for k, v in sa.items()} if isinstance(sa, dict) else {}
        pb = st.get("pos_booked")
        self._pos_booked = {k: (dict(v) if isinstance(v, dict) else {"ignore": True}) for k, v in pb.items()} \
            if isinstance(pb, dict) else None     # None: not initialised yet
        self._exit_specs = {k: _plain(v) for k, v in (st.get("exit_specs") or {}).items() if isinstance(v, dict)}
        self._plan_specs = {k: _plain(v) for k, v in (st.get("plan_specs") or {}).items() if isinstance(v, dict)}
        re_ = st.get("recent_exits")
        self._recent_exits = [_plain(r) for r in re_ if isinstance(r, dict)] if isinstance(re_, list) else []
        if isinstance(st.get("costs"), dict):
            st["costs"] = _plain(st["costs"])       # the cost meter's totals (spec C2) are floats too
        self._refs = {}
        self._cycle_prices = {}
        self._cycle_lc = {}
        self._fills_handled = 0
        if self.brain is not None and (self.ladder_on or self.exits_on or self.routing_on):
            self.broker.track({USDT})

    # ---- bot feature state (ladder levels, positions) in runner_state_<mode>.json
    def _save_bot_state(self):
        if self.dry_run or self.brain is None:
            return
        try:
            self._save_state(ladder=self._ladder_st, positions=self._positions, ladder_positions=self._ladder_pos,
                             pos_booked=self._pos_booked if self._pos_booked is not None else None,
                             exit_specs=self._exit_specs, plan_specs=self._plan_specs,
                             recent_exits=self._recent_exits, sell_attr=self._sell_attr)
        except Exception as e:  # noqa: BLE001 - retried with the next save; never abort after an order
            log.error("cannot save the ladder / positions state (%s); it is saved with the next update", e)

    def _level(self, coin, level):
        c = self._ladder_st.setdefault("coins", {}).setdefault(coin, {})
        return c.setdefault("levels", {}).setdefault(lvl_key(level), {"armed": True})

    # ---- state
    def _save_state(self, **fields):
        if self.dry_run:
            return
        self.state.update(fields)
        self.state["updated_at"] = self.clock()
        atomic_write_json(self.state_path, self.state)

    @property
    def last_bar_ts(self):
        v = self.state.get("last_bar_ts")
        return int(v) if v is not None else None

    # ---- sleeve (bot-owned ledger, only with max_equity_irt)
    @property
    def capped(self):
        return self.equity_cap is not None

    def _basis(self):
        return ("sleeve:%s" % format(self.equity_cap, "f")) if self.capped else "account"

    def _fill_records(self):
        try:
            return list(self.broker.fill_records())
        except Exception as e:  # noqa: BLE001 - reconciled on the next cycle
            log.warning("cannot read the broker's order records (%s); the sleeve is reconciled later", e)
            return None

    @staticmethod
    def _booked_entry(rec):
        return {"symbol": rec.get("symbol"), "side": rec.get("side"), "base": D(rec.get("base") or 0),
                "quote": D(rec.get("quote") or 0), "fee": D(rec.get("fee") or 0), "fee_asset": rec.get("fee_asset")}

    @staticmethod
    def _cash_after_cap_change(budget, cash, cap):
        """Sleeve cash after max_equity_irt changed from `budget` to `cap` (the difference is added
        to / taken from its cash). RunnerError if a lowered cap is more than the sleeve's cash."""
        new_cash = cash + (cap - budget)
        if new_cash < 0:
            raise RunnerError(
                "max_equity_irt was lowered from %s to %s, but the bot's sleeve has only %s IRT in cash (the "
                "rest is in coins). Use a cap >= %s, or let the strategy move to cash first." % (
                    fmt_amount(budget), fmt_amount(cap), fmt_amount(cash), fmt_amount(budget - cash)))
        return new_cash

    def _sleeve_get(self):
        if self._sleeve is not None:
            return self._sleeve
        raw = self.state.get("sleeve")
        cap = self.equity_cap
        dirty = False
        if not raw:
            # every order the broker already remembers belongs to an earlier (uncapped) run: never ours
            recs = self._fill_records()
            if recs is None:   # without them, a later sync would book those orders into the sleeve
                raise BrokerError("cannot read the broker's order records: the bot sleeve is created on a later cycle")
            sl = {"budget": cap, "cash": cap, "units": {}, "created_at": self.clock(),
                  "booked": {r["identifier"]: {"ignore": True} for r in recs if r.get("identifier")}}
            log.warning("new bot sleeve: budget %s IRT, no coins. Only the sleeve is traded: coins already in the "
                        "account are never sold and toman above the budget is never used.", fmt_amount(cap))
            dirty = True
        else:
            sl = {"budget": D(raw.get("budget")), "cash": D(raw.get("cash")),
                  "units": {s: D(u) for s, u in (raw.get("units") or {}).items()},
                  "created_at": raw.get("created_at"), "booked": {}}
            if isinstance(raw.get("booked"), dict):
                for k, v in raw["booked"].items():
                    sl["booked"][k] = {"ignore": True} if (not isinstance(v, dict) or v.get("ignore")) \
                        else self._booked_entry(v)
            else:
                # sleeve written by an older version, which booked each order's journaled fill: take
                # those as booked so they are not booked twice
                recs = self._fill_records()
                if recs is None:
                    raise BrokerError("cannot read the broker's order records to migrate the bot sleeve; retried "
                                      "on a later cycle")
                sl["booked"] = {r["identifier"]: self._booked_entry(r) for r in recs
                                if r.get("identifier") and r.get("base") is not None}
                log.warning("bot sleeve from an older version: %d journaled fills recorded as already booked",
                            len(sl["booked"]))
                dirty = True
            if sl["budget"] != cap:
                new_cash = self._cash_after_cap_change(sl["budget"], sl["cash"], cap)
                log.warning("max_equity_irt changed from %s to %s IRT: sleeve cash %s -> %s IRT (drawdown high-water "
                            "mark restarts)", fmt_amount(sl["budget"]), fmt_amount(cap), fmt_amount(sl["cash"]),
                            fmt_amount(new_cash))
                sl["cash"], sl["budget"] = new_cash, cap
                dirty = True
        self._sleeve = sl
        if dirty:
            self._save_sleeve()
        return sl

    def _save_sleeve(self):
        if self._sleeve is None or self.dry_run:
            return
        sl = self._sleeve
        try:
            self._save_state(sleeve={"budget": sl["budget"], "cash": sl["cash"], "created_at": sl.get("created_at"),
                                     "units": {s: u for s, u in sorted(sl["units"].items()) if u != 0},
                                     "booked": sl.get("booked") or {}})
        except Exception as e:  # noqa: BLE001 - retried with the next save; never abort after a fill
            log.error("cannot save the bot sleeve (%s); it will be saved with the next update", e)

    def _sleeve_book(self, rec, save=True):
        """Book an order's CUMULATIVE fill into the sleeve, as the delta against what the sleeve
        itself has already booked for that order identifier (sleeve['booked']). Idempotent: the
        immediate fill, a late fill and the reconciliation against the broker's durable records
        (_sync_sleeve) can all deliver the same order without booking it twice, and a fill whose
        booking was interrupted (Ctrl+C, crash, failed write) is booked by the next sync.
        Returns True if the sleeve changed."""
        if not self.capped or not rec or rec.get("base") is None:
            return False
        sl = self._sleeve_get()
        ident = rec.get("identifier")
        prev = sl["booked"].get(ident) if ident else None
        if prev is not None and prev.get("ignore"):
            return False
        s = rec["symbol"]
        base_asset, quote_asset = parse_symbol(s)
        b, q, fee = D(rec.get("base") or 0), D(rec.get("quote") or 0), D(rec.get("fee") or 0)
        db, dq, dfee = (b - prev["base"], q - prev["quote"], fee - prev["fee"]) if prev else (b, q, fee)
        if db == 0 and dq == 0 and dfee == 0:
            return False
        fa = rec.get("fee_asset")
        fee_base = dfee if fa == base_asset else ZERO
        fee_quote = dfee if fa == quote_asset else ZERO
        if rec["side"] == "buy":
            du, dc = db - fee_base, -dq - fee_quote
        else:
            du, dc = -db - fee_base, dq - fee_quote
        # the sleeve keeps coins per managed symbol (COIN_IRT) and toman as cash: a COIN_USDT fill
        # (routing, ladder bid, target sell) moves the coin AND the sleeve's USDT (USDT_IRT units)
        ukey = s if quote_asset == IRT else "%s_%s" % (base_asset, IRT)
        u = sl["units"].get(ukey, ZERO) + du
        if u < 0:
            log.warning("sleeve %s units would go negative (%s); set to 0", ukey, u)
            u = ZERO
        sl["units"][ukey] = u
        if quote_asset == IRT:
            sl["cash"] += dc
        else:
            qkey = "%s_%s" % (quote_asset, IRT)
            qu = sl["units"].get(qkey, ZERO) + dc
            if qu < 0:
                log.warning("sleeve %s units would go negative (%s); set to 0", qkey, qu)
                qu = ZERO
            sl["units"][qkey] = qu
        if ident:
            sl["booked"][ident] = self._booked_entry(dict(rec, base=b, quote=q, fee=fee))
        if save:
            self._save_sleeve()
        return True

    def _sync_sleeve(self, rep=None):
        """Reconcile the sleeve with every order the broker remembers (order journal / paper
        state), so fills whose booking was lost are booked, exactly once."""
        if not self.capped:
            return
        recs = self._fill_records()
        if recs is None:
            return
        sl = self._sleeve_get()
        changed = False
        for r in recs:
            if not r.get("identifier"):
                continue
            prev = sl["booked"].get(r["identifier"])
            if self._sleeve_book(r, save=False):
                changed = True
                log.warning("sleeve: booked %s fill of order %s that the sleeve had %s: %s %s base %s quote %s fee %s",
                            "the remaining" if prev else "the", r["identifier"], "only partly" if prev else "not yet",
                            r["side"], r["symbol"], r["base"], r["quote"], r["fee"])
                if rep is not None:
                    rep.setdefault("sleeve_bookings", []).append(r["identifier"])
        # forget orders the broker no longer remembers (pruned journal): bounded state
        known = {r["identifier"] for r in recs if r.get("identifier")}
        stale = [k for k in sl["booked"] if k not in known]
        for k in stale:
            del sl["booked"][k]
        if changed or stale:
            self._save_sleeve()

    def _after_resolve(self, rep):
        """After resolve_pending: report late fills, then reconcile the sleeve from the broker's
        durable order records (which already contain those late fills)."""
        for f in self.broker.drain_late_fills():
            log.warning("late fill of an earlier order %s: %s base %s quote %s", f.get("identifier"), f["side"],
                        f["base"], f["quote"])
            rep.setdefault("late_fills", []).append(f)
        self._sync_sleeve(rep)

    def _holdings(self, syms, refresh=True, clamp=True):
        """(units, cash) the bot manages. Without a cap: the account's IRT and strategy coins.
        With a cap: the sleeve, clamped to what the account really holds (clamp=False: the
        ledger as booked, used while an order's outcome is unknown)."""
        if refresh:
            self.broker.refresh()
        bal = self.broker.balances()
        acct_units = {s: bal.get(parse_symbol(s)[0], ZERO) for s in syms}
        acct_cash = bal.get("IRT", ZERO)
        if not self.capped:
            return acct_units, acct_cash
        sl = self._sleeve_get()
        units = {}
        for s in syms:
            own = sl["units"].get(s, ZERO)
            u = min(own, acct_units[s]) if (clamp and own > 0) else max(own, ZERO)
            if u < own:
                log.warning("the bot sleeve owns %s %s but the account holds only %s: using %s (moved manually?)",
                            own, parse_symbol(s)[0], acct_units[s], u)
            units[s] = u
        cash = max(ZERO, min(sl["cash"], acct_cash) if clamp else sl["cash"])
        if clamp and cash < sl["cash"]:
            log.warning("the bot sleeve has %s IRT but the account only %s IRT: using %s", fmt_amount(sl["cash"]),
                        fmt_amount(acct_cash), fmt_amount(cash))
        return units, cash

    def _frozen_funds(self, syms):
        """{asset: locked amount} for the managed assets whose TOTAL balance is larger than the
        AVAILABLE one - toman or coins tied up in an order this bot did not place (a manual limit
        order left open in the Bitpin app), or in the unfilled remainder of one.

        Sizing deliberately keeps using the totals: valuing the account at what is available would
        make the bot's OWN pending order look like a loss to the drawdown breaker and could halt it.
        But _execute can only spend broker.available(), so a buy planned on locked toman ends as
        ("SYM", "no IRT available") and a full exit of a locked coin stays partial. The operator has
        to see that, so it is a WARNING and a field in the cycle report.

        Only without max_equity_irt: with a sleeve the user trades the same account on purpose, so
        the account-wide gap says nothing about the bot's own money."""
        if self.capped:
            return {}
        try:
            total, avail = self.broker.balances(), self.broker.available()
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - informational only
            log.debug("frozen-funds check unavailable: %s", e)
            return {}
        # the bot's OWN resting orders (ladder bids, target sells) lock funds on purpose: not reported
        try:
            own = self.broker.locked_by_limits() or {}
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - informational only
            log.debug("own locked funds unavailable: %s", e)
            own = {}
        assets = {parse_symbol(s)[0] for s in syms} | {"IRT"}
        out = {}
        for a in sorted(assets):
            gap = D(total.get(a, ZERO)) - D(avail.get(a, ZERO)) - D(own.get(a, ZERO))
            if gap > 0:
                out[a] = gap
        key = tuple(sorted((a, format(v, "f")) for a, v in out.items()))
        if out and key != self._frozen_seen:
            log.warning("part of the account is LOCKED in an order the bot did not place (or in an unfilled "
                        "remainder): %s. Order sizes are computed on the full balance, but the bot can only spend "
                        "what is free: a buy may be skipped with 'no IRT available' and a full exit may stay "
                        "partial. Cancel the order in the Bitpin app if it is not yours.",
                        ", ".join("%s %s" % (a, fmt_amount(v)) for a, v in sorted(out.items())))
        self._frozen_seen = key
        return out

    def management_summary(self, balances):
        """Human lines for the live banner / status: what the bot will and will not touch."""
        coins = {parse_symbol(s)[0]: s for s in self.symbols}
        held = ["%s %s" % (a, balances[a]) for a in sorted(coins) if balances.get(a, ZERO) > 0]
        lines = []
        if self.capped:
            raw = self.state.get("sleeve")
            if raw:
                u = {s: D(v) for s, v in (raw.get("units") or {}).items() if D(v) > 0}
                lines.append("bot sleeve: budget %s IRT, cash %s IRT, coins %s" % (
                    fmt_amount(D(raw.get("budget"))), fmt_amount(D(raw.get("cash"))),
                    ", ".join("%s %s" % (parse_symbol(s)[0], v) for s, v in sorted(u.items())) or "none"))
            else:
                lines.append("bot sleeve: will be created with %s IRT and no coins" % fmt_amount(self.equity_cap))
            lines.append("NOT touched by the bot: toman above the sleeve, and coins you hold yourself%s" % (
                (" (" + ", ".join(held) + ")") if held else ""))
        else:
            lines.append("managed: the WHOLE toman balance and ALL units of the strategy's coins")
            if held:
                lines.append("strategy coins in the account that the bot may SELL to reach its targets: %s"
                             % ", ".join(held))
        return lines

    # ---- data
    def _history_start(self, now):
        if self._anchor is None and self.brain is not None:
            # brain mode values holdings at the latest closes only (the context builder keeps its own history),
            # plus what the runner's re-check of the pump guard needs (lookback + window hours)
            bars = BRAIN_HISTORY_BARS
            try:
                g = (getattr(self.brain, "cfg", None) or {}).get("guard") or {}
                bars = max(bars, int(math.ceil(float(g.get("pump_lookback_hours") or 0)
                                               + float(g.get("pump_window_hours") or 0))) + 4)
            except (TypeError, ValueError, AttributeError):
                pass
            self._anchor = int(now) - bars * HOUR
        if self._anchor is None:
            hs = self.cfg.get("history_start_ts")
            self._anchor = int(hs) if hs is not None else int(now) - int(self.cfg["lookback_bars"]) * HOUR
        return self._anchor

    def _seed(self, symbol, now):
        if not self.seed_source:
            return []
        try:
            bars = list(self.seed_source(symbol) or [])
        except FileNotFoundError:
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("cannot read the local candle cache for %s (%s); downloading history instead", symbol, e)
            return []
        return [b for b in bars if b.ts + HOUR <= now]

    def _fetch(self, symbol, now):
        cached = self._cache.get(symbol)
        if cached is None:
            cached = self._seed(symbol, now)
        start = cached[-1].ts - 3 * HOUR if cached else self._history_start(now)
        # `to` reaches past the local clock: the exchange then shows its still-forming bar even if
        # this computer's clock runs behind (see _exchange_now); closed_only() filters it out.
        fresh = list(self.bars_source(symbol, "60", start, int(now) + FETCH_AHEAD) or [])
        if fresh:
            self._server_newest[symbol] = max(b.ts for b in fresh)
        return cached, fresh

    def _merge(self, symbol, cached, fresh, now):
        merged = {b.ts: b for b in cached}
        merged.update({b.ts: b for b in fresh})
        # anchored: the head is never dropped (see module docstring)
        bars = data_mod.closed_only([merged[k] for k in sorted(merged)], "60", now)
        # data.resample infers the base step from the first two bars: keep them contiguous
        while len(bars) > 2 and bars[1].ts - bars[0].ts != HOUR:
            bars = bars[1:]
        self._cache[symbol] = bars
        return bars

    def _candles(self, symbol, now):
        return self._merge(symbol, *self._fetch(symbol, now), now)

    def _exchange_now(self, fetched, now):
        """`now`, unless the exchange already serves a bar that by the LOCAL clock has not even
        opened yet: then this computer's clock runs behind, and the exchange's timeline is used
        (a time just after its newest - still forming - bar opened), so the bar that has really
        closed is not mistaken for the forming one. A lone future bar is not taken as evidence:
        the bar before it must be there too."""
        best = None
        for s in self.symbols:
            ts = {b.ts for b in fetched[s][1]}
            if not ts:
                continue
            newest = max(ts)
            if newest - HOUR in ts and (best is None or newest > best):
                best = newest
        self._clock_behind = None
        if best is None:
            return now
        local_forming = expected_last_closed_1h(now, best % HOUR) + HOUR
        if best <= local_forming:
            return now
        self._clock_behind = best - now
        log.error("the exchange already serves the %s bar, which by this computer's clock has not opened yet: the clock "
                  "is at least %.0f min BEHIND real time. Using the exchange's candle timeline; fix the clock (Windows: "
                  "Settings > Time & language > Date & time > Sync now).", fmt_ts(best), max(best - now, 0) / 60)
        return best + 1

    def load_panel(self, now):
        syms = list(dict.fromkeys(self.symbols + self.extra_symbols))
        fetched = {s: self._fetch(s, now) for s in syms}
        now = self._exchange_now(fetched, now)
        bars = {s: self._merge(s, *fetched[s], now) for s in self.symbols}
        extra = {s: self._merge(s, *fetched[s], now) for s in self.extra_symbols}
        nonempty = [b for b in bars.values() if b]
        if not nonempty:
            raise StaleData("no candles returned")
        latest = max(b[-1].ts for b in nonempty)
        self.offset = latest % HOUR
        expected = expected_last_closed_1h(now, self.offset)
        if latest < expected:
            raise StaleData("newest closed 1h candle is %s, expected %s" % (fmt_ts(latest), fmt_ts(expected)))
        # The exchange must already have started the NEXT bar: proof that `latest` is closed on the
        # server too, even if the local clock runs ahead.
        if max(self._server_newest.get(s, 0) for s in self.symbols) <= latest:
            raise StaleData("the exchange has not opened the bar after %s yet (local clock ahead?)" % fmt_ts(latest),
                            kind="not_opened")
        for s, b in bars.items():
            if not b:
                log.warning("%s: no candles (not listed?)", s)
            elif b[-1].ts < latest - 6 * HOUR:
                log.warning("%s: last candle %s is old (market halted?)", s, fmt_ts(b[-1].ts))
        factor = self.res_seconds // HOUR
        # data.resample divides by the step of the first two bars: a symbol listed within the last
        # hour (a single closed bar) cannot form a complete bucket anyway
        series = {s: (b if factor == 1 else data_mod.resample(b, factor) if len(b) >= 2 else [])
                  for s, b in bars.items()}
        panel = Panel.from_bars(series, self.res)
        if not len(panel):
            raise StaleData("empty panel after resampling")
        last_close_1h = {s: b[-1].close for s, b in list(bars.items()) + list(extra.items()) if b}
        self._remember_prices(last_close_1h)
        return panel, last_close_1h

    def _check_history(self, panel):
        if self._history_checked:
            return
        self._history_checked = True
        if self.brain is not None:
            return
        log.info("strategy history: %d %s bars from %s to %s", len(panel), self.res, fmt_ts(panel.ts[0]),
                 fmt_ts(panel.ts[-1]))
        hs = self.cfg.get("history_start_ts")
        if hs is not None and panel.ts[0] > int(hs) + 2 * 86400:
            log.warning("strategy history starts at %s, later than history_start_ts %s: strategies whose state "
                        "starts at the first bar may trade differently from the backtest (data/*.csv missing and "
                        "the download incomplete?)", fmt_ts(panel.ts[0]), fmt_ts(int(hs)))

    def _remember_prices(self, last_close_1h):
        """Keep the newest close of every symbol that HAS one, in memory and in
        runner_state_<mode>.json. A market whose candle endpoint answers [] for a while (a halt, a
        delisting, a broken chart endpoint) must never be valued at 0: plan_orders would drop it out
        of the equity, the drawdown breaker would see the loss and halt the bot for good, and the
        position could not be sold either. The remembered close survives a restart, which is exactly
        when the in-memory candle cache is empty (systemd restarts the unit after every outage)."""
        now = self.clock()
        changed = False
        for s, p in (last_close_1h or {}).items():
            if p is None:
                continue
            try:
                d = D(repr(p))
            except Exception:  # noqa: BLE001 - a bad close is simply not remembered
                continue
            if d <= 0:
                continue
            if self._last_prices.get(s) != d:
                changed = True
            self._last_prices[s] = d
            self._price_seen[s] = now          # per symbol, so the warning can say how old it really is
        if changed:
            self._save_state(last_prices={s: {"px": format(p, "f"), "at": round(self._price_seen.get(s, now), 3)}
                                          for s, p in sorted(self._last_prices.items())})

    def _prices(self, syms, panel, last_close_1h):
        """Valuation / sizing prices: the newest CLOSED 1h close (fresher than a 4h bar's close when a
        bar is processed late); the strategy's own panel is used only for its signal. A symbol with no
        candle at all falls back to the last close this bot ever saw (_remember_prices, persisted):
        valuing a held coin at 0 is never right. Such a symbol keeps its weight, so plan_orders plans
        no change for it, and it can still be SOLD (the risk manager vets that order against the live
        order book); the age of the fallback is logged every cycle."""
        out = {}
        for s in syms:
            p = last_close_1h.get(s)
            if p is None and s in panel.cols:
                p = panel[s]["close"][-1]
            if p is not None:
                out[s] = D(repr(p))
                continue
            old = self._last_prices.get(s)
            if old is not None:
                age = self.clock() - float(self._price_seen.get(s) or 0)
                log.warning("%s: no candle in this cycle; valued at the last close this bot saw (%s IRT, %.0f min "
                            "old). If this lasts, check whether the market was suspended or delisted.",
                            s, fmt_amount(old), max(age, 0) / 60.0)
            out[s] = old
        return out

    @staticmethod
    def _price_gaps(syms, prices, units):
        """Symbols the bot HOLDS whose valuation price is missing (no candle now and no remembered
        close). Their value is unknown, not 0, so this cycle must neither trade nor move the
        high-water mark nor trip the drawdown breaker."""
        return sorted(s for s in syms if units.get(s, ZERO) > 0 and prices.get(s) is None)

    # ---- drawdown: a breach must show on a second wallet read AND at the order-book mid
    def _mids(self, units):
        out = {}
        for s, u in units.items():
            if u <= 0:
                continue
            try:
                out[s] = mid_price(self.broker.order_book(s))
            except FATAL_ERRORS:
                raise
            except Exception as e:  # noqa: BLE001 - that symbol is valued at its close only
                log.warning("drawdown check: order book of %s unavailable (%s); valued at its last close only", s, e)
                out[s] = None
        return out

    def _drawdown(self, plan, targets, prices, units, cash, clamp=True, gaps=()):
        """Drawdown of managed equity. Equity is valued at the last closed 1h candle, which on a thin
        IRT book can be ONE trade's price. So a single print can neither trip the breaker nor raise
        the high-water mark: when the close-valued equity would do either, the holdings are also
        valued at the current order-book mid. The breaker trips only if BOTH valuations breach (the
        wallet is re-read first), and the high-water mark rises only to the LOWER of the two.

        `gaps`: symbols the bot holds whose price is unknown (_price_gaps). Their value is missing,
        not zero, so the equity is a LOWER BOUND only: the breaker must not halt on it and the
        high-water mark must not move. The cycle is reported as "stale_prices" by _guard and does
        not trade - the same protection the pending-order guard gives."""
        basis = self._basis()
        eq = plan.equity
        if gaps:
            log.error("no price for %s, which the bot HOLDS: the managed equity (%s IRT) is incomplete. Not trading "
                      "this cycle, not updating the high-water mark and NOT halting on this number. If the market "
                      "stays unlisted, sell it in the Bitpin app.", ", ".join(gaps), fmt_amount(eq))
            dd = self.risk.peek_drawdown(eq, basis)
            dd.update({"equity_close": eq, "equity_mid": None, "breached": False, "stale_prices": list(gaps)})
            return dd, plan
        breach = self.risk.peek_drawdown(eq, basis)["breached"] and not self.risk.is_halted()
        if breach:
            log.warning("drawdown %.1f%% at the last closes reaches the limit; re-reading the wallet and the order "
                        "books to confirm", self.risk.peek_drawdown(eq, basis)["drawdown"] * 100)
            units, cash = self._holdings(list(targets), clamp=clamp)
            plan = plan_orders(targets, units, prices, cash, self.threshold, self.fee_rate, None,
                               dust_irt=self.risk.min_order_irt)
            eq = plan.equity
        info = {"equity_close": eq, "equity_mid": None}
        risk_eq = eq
        hwm = self.risk.high_water_mark(basis)
        if breach or eq > hwm:
            mids = self._mids(units)
            # a symbol with an order-book mid is valued even when its candle price is missing, so the
            # second valuation really is independent of the candle feed
            eq_mid = cash + sum((units.get(s, ZERO) * (mids[s] if mids.get(s) is not None else prices[s])
                                 for s in targets if units.get(s, ZERO) > 0
                                 and (mids.get(s) is not None or prices.get(s) is not None)), ZERO)
            info["equity_mid"] = eq_mid
            lo, hi = min(eq, eq_mid), max(eq, eq_mid)
            if not self.risk.peek_drawdown(lo, basis)["breached"]:
                risk_eq = lo                      # agree (no breach): the HWM rises at most to the lower one
            elif self.risk.peek_drawdown(hi, basis)["breached"]:
                risk_eq = hi                      # both breach: confirmed
            else:
                risk_eq = min(hi, hwm) if hwm > 0 else hi   # they disagree: neither halt nor raise the HWM
            if breach or eq_mid < eq * Decimal("0.99"):
                log.warning("managed equity at the last closes %s IRT, at the order-book mid %s IRT: the breaker uses "
                            "%s IRT", fmt_amount(eq), fmt_amount(eq_mid), fmt_amount(risk_eq))
            if breach and not self.risk.peek_drawdown(risk_eq, basis)["breached"]:
                log.warning("drawdown NOT confirmed by the order book (a one-off candle print?): not halting")
        dd = self.risk.peek_drawdown(risk_eq, basis) if self.dry_run else self.risk.update_equity(risk_eq, basis)
        dd.update(info)
        return dd, plan

    # ---- one cycle
    def _log_pending(self, pending, quiet=False):
        stuck = [p for p in pending if p.get("stuck")]
        (log.info if quiet and not stuck else log.error)(
            "%d order(s) with unknown outcome (identifiers %s); not trading until resolved", len(pending),
            ", ".join(p.get("identifier", "?") for p in pending))
        for p in stuck:
            log.error("ORDER UNRESOLVED (%s lookup failures, last: %s) for %s %s identifier %s order id %s. Check the "
                      "order in the Bitpin app, then record its outcome: run_bot.py resolve-order --state-dir %s "
                      "--identifier %s (--order-id ID | --not-executed). The bot does not trade until it is resolved.",
                      p.get("lookup_failures") or 0, p.get("last_lookup_error", "-"), p.get("side"), p.get("symbol"),
                      p.get("identifier"), p.get("order_id") or "-", self.state_dir, p.get("identifier"))

    def _buy_retry_due(self, bar_ts):
        r = self.state.get("retry_buys") or {}
        return (not self.dry_run and r.get("bar_ts") == bar_ts
                and int(r.get("attempts") or 0) < MAX_BUY_RETRIES)

    def run_once(self):
        now = self.clock()
        rep = {"time": now, "mode": self.mode, "dry_run": self.dry_run, "strategy": repr(self.strategy),
               "status": None, "fills": [], "skipped": [], "errors": []}
        if self.risk.kill_switch_active():
            rep["status"] = "kill_switch"
            log.warning("kill switch %s present: not trading", self.risk.kill_switch_path)
            self._kill_cleanup(rep)
            return rep
        if self.risk.is_halted():
            if self.risk.flatten_pending and not self.dry_run:
                return self._flatten_cycle(rep)
            self._halt_cleanup(rep)
            rep["status"] = "halted"
            log.error("trading is halted (%s)%s. Investigate, then stop the bot and run: run_bot.py risk-reset --mode %s",
                      self.risk.halt_reason(), " - flatten pending" if self.risk.flatten_pending else "", self.mode)
            return rep
        panel, last_close_1h = self.load_panel(now)
        if self._clock_behind is not None:
            rep["clock_behind_seconds"] = self._clock_behind
        self._check_history(panel)
        bar_ts = panel.ts[-1]
        rep["bar_ts"] = bar_ts
        rep["bar_close"] = bar_close_time(bar_ts, self.res_seconds, self.offset or 0)
        # Orders of earlier cycles are resolved (and their fills booked) every cycle, also when this
        # bar was already traded: the loop then polls them every minute instead of once per bar.
        pending = self.broker.resolve_pending(read_only=self.dry_run)
        self._after_resolve(rep)
        buys_only = False
        if self.last_bar_ts is not None and bar_ts <= self.last_bar_ts:
            if pending or not self._buy_retry_due(bar_ts):
                rep["status"] = "already_processed"
                if pending:
                    rep["pending"] = pending
                    self._log_pending(pending, quiet=True)
                else:
                    log.info("bar %s already processed; waiting for the next one", fmt_ts(bar_ts))
                return rep
            buys_only = True
            log.warning("bar %s: retrying the buys that a temporary failure skipped (attempt %d of %d)", fmt_ts(bar_ts),
                        int(self.state["retry_buys"].get("attempts") or 0) + 1, MAX_BUY_RETRIES)

        if self.brain is not None:
            return self._brain_once(rep, now, panel, last_close_1h, bar_ts, pending, buys_only)

        targets = compute_targets(self.strategy, panel)
        for s in self.extra_symbols:
            targets.setdefault(s, ZERO)
        prices = self._prices(targets, panel, last_close_1h)
        # while an order's outcome is unknown the sleeve ledger (pre-order) is the best measure
        units, cash = self._holdings(list(targets), clamp=not pending)
        gaps = self._price_gaps(targets, prices, units)
        frozen = self._frozen_funds(list(targets))
        if frozen:
            rep["frozen"] = {a: format(v, "f") for a, v in frozen.items()}
        plan = plan_orders(targets, units, prices, cash, self.threshold, self.fee_rate, None,
                           dust_irt=self.risk.min_order_irt)
        # the breaker is evaluated every cycle, also while orders are pending
        dd, plan = self._drawdown(plan, targets, prices, units, cash, clamp=not pending, gaps=gaps)
        rep.update(equity=plan.equity, equity_mid=dd.get("equity_mid"), cash=plan.cash, targets=targets,
                   current=plan.current, managed="sleeve" if self.capped else "account",
                   plan=[o.describe() for o in plan.orders], plan_skipped=plan.skipped, drawdown=dd)
        self._log_plan(bar_ts, plan, prices, dd)
        done = self._guard(rep, dd, pending, bar_ts)
        if done is not None:
            return done
        return self._trade(rep, plan, targets, prices, last_close_1h, bar_ts, now, buys_only,
                           strategy=self.strategy.name, params=self.strategy.params, res=self.res)

    def _guard(self, rep, dd, pending, bar_ts):
        """Drawdown breaker, missing prices and orders of unknown outcome: the finished report if the
        cycle must end here (no trade), else None."""
        if dd.get("stale_prices"):
            rep["status"] = "stale_prices"
            rep["stale_prices"] = list(dd["stale_prices"])
            return rep
        if dd["breached"]:
            rep["status"] = "halted"
            if self.risk.drawdown_action == "flatten" and not self.dry_run and not self.risk.flattened:
                log.error("drawdown_action=flatten: selling the managed positions to IRT (retried until done)")
                self.risk.start_flatten()
                self._save_state(last_bar_ts=bar_ts)
                return self._flatten_cycle(rep)
            self._halt_cleanup(rep)
            return rep
        if pending:
            stuck = [p for p in pending if p.get("stuck")]
            rep["status"] = "pending_orders_stuck" if stuck else "pending_orders"
            rep["pending"] = pending
            self._log_pending(pending)
            return rep
        return None

    def _trade(self, rep, plan, targets, prices, last_close_1h, bar_ts, now, buys_only, **state_fields):
        """The one execution path of both modes: dry-run preview, or write-ahead of the bar, sells
        first, buys with the IRT actually available, same-bar buy retry, report."""
        if self.dry_run:
            rep["status"] = "dry_run"
            rep["vetted"] = self._preview(plan, last_close_1h)
            return rep

        if not buys_only:
            # write-ahead: this bar is consumed before the first order goes out
            self._save_state(last_bar_ts=bar_ts, retry_buys=None, **state_fields)
            orders = plan.orders
        else:
            r = dict(self.state.get("retry_buys") or {})
            self._save_state(retry_buys=dict(r, attempts=int(r.get("attempts") or 0) + 1))
            orders = plan.buys
            for o in plan.sells:
                rep["skipped"].append((o.symbol, "buy-leg retry: sells wait for the next bar"))
        budget_ok, why = self._order_budget_ok(plan, orders)
        self._transient_skip = False
        if budget_ok:
            if self.brain is not None and orders:
                self._free_resting_for(plan, rep, buys_only=buys_only)
            self._execute(plan, last_close_1h, rep, buys_only=buys_only)
        else:
            rep["skipped"].append(("*", why))
            log.warning("rebalance skipped: %s", why)
        rep["status"] = "ok"
        if self._transient_skip and plan.buys and self.broker.unresolved_count() == 0 \
                and not self.risk.kill_switch_active():
            # buys were skipped by a temporary failure (after the sells): retry them within this bar
            r = self.state.get("retry_buys") or {}
            attempts = int(r.get("attempts") or 0) if r.get("bar_ts") == bar_ts else 0
            if attempts < MAX_BUY_RETRIES:
                self._save_state(retry_buys={"bar_ts": bar_ts, "attempts": attempts})
                rep["retry_buys"] = True
                log.warning("some buys were skipped by a temporary failure; they are retried in %.0f s",
                            float(self.cfg["error_backoff_seconds"]))
        elif buys_only:
            self._save_state(retry_buys=None)
        try:
            u2, c2 = self._holdings(list(targets))
            rep["equity_after"] = c2 + sum((u2[s] * prices[s] for s in targets if prices.get(s) is not None), ZERO)
        except Exception as e:  # noqa: BLE001 - informational
            log.warning("post-trade balance refresh failed: %s", e)
        rep["unresolved_after"] = self.broker.unresolved_count()
        self._save_state(last_cycle={
            "bar_ts": bar_ts, "time": now, "status": "ok", "equity": rep["equity"], "equity_after": rep.get("equity_after"),
            "orders": len(rep["fills"]), "skipped": [list(x) for x in rep["skipped"]], "errors": rep["errors"]})
        return rep

    # ---- brain mode (KimiBrain decides the targets; execution is the same as strategy mode)
    def _abort_reason(self):
        """Polled by the LLM client before every request: the STOP kill switch ends a decision."""
        if self.risk.kill_switch_active():
            return "kill switch file %s present" % self.risk.kill_switch_path
        return None

    def _brain_snapshot(self, units, cash, now=None):
        """What the context builder values: the MANAGED holdings only (the sleeve when
        max_equity_irt is set), toman under 'IRT' (already divided by irt_unit_divisor). Since v3 also
        the informational keys of the context's portfolio block (spec C2; every one optional, the
        builder drops what is missing): halt_drawdown_pct (the breaker's threshold in %, so the model
        sees its distance to the halt), fees_irt / slippage_irt (the cumulative costs of the bot's own
        fills, _cost_totals) and turnover_7d_irt (the traded value of the last 7 days)."""
        bal = {"IRT": float(cash)}
        for s, u in units.items():
            if u > 0:
                a = parse_symbol(s)[0]
                bal[a] = bal.get(a, 0.0) + float(u)
        snap = {"balances": bal}
        if self.capped:
            # the sleeve's starting capital: the model's P&L is the bot's own, not the whole account's
            snap["equity_start_irt"] = float(self._sleeve_get()["budget"])
        try:
            hwm = self.risk.high_water_mark(self._basis())
            if hwm and hwm > 0:
                snap["high_water_mark_irt"] = float(hwm)   # the model sees the breaker's own drawdown
        except Exception:  # noqa: BLE001 - informational
            pass
        try:
            snap["halt_drawdown_pct"] = float(self.risk.max_drawdown) * 100.0    # 0.50 -> 50
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            for k, v in self._cost_totals(self.clock() if now is None else now).items():
                if v is not None:
                    snap[k] = float(v)
        except Exception as e:  # noqa: BLE001 - informational: never lose the decision over the cost meter
            log.warning("cost totals unavailable: %s", e)
        return snap

    # ---- the cost meter of the bot's own fills (spec C2: fees + slippage since the start, 7-day turnover)
    @staticmethod
    def _ref_px(symbol, last_close_1h):
        """The price a market order was planned on: the last hourly close of its market (a direct
        COIN_USDT leg: the COIN_IRT close over the USDT_IRT close). None when unknown (then the fill
        adds no slippage, only its fee and turnover)."""
        lc = last_close_1h or {}
        try:
            if lc.get(symbol):
                return D(lc[symbol])
            base, quote = parse_symbol(symbol)
            if quote == USDT and lc.get("%s_%s" % (base, IRT)) and lc.get(SAFE):
                return D(lc["%s_%s" % (base, IRT)]) / D(lc[SAFE])
        except (ArithmeticError, TypeError, ValueError):
            return None
        return None

    def _note_cost(self, fill, now, ref_px=None):
        """Book one fill of the bot (a market order or a resting maker order) into the running cost
        totals kept in the runner state (key "costs"): the fee in toman (a USDT or coin fee is converted
        at the last USDT_IRT price / the fill price), the SIGNED slippage against the planned price
        (a buy filled above the last close or a sell below it costs, the reverse earns; a resting order
        fills at its own price: 0) and the traded value for the 7-day turnover. Everything is toman
        (the bot's own unit); an unknown USDT_IRT price leaves a USDT-quoted fill out of the totals
        rather than guessing. The totals start with the first fill after this version was installed
        (the state has no fills before that), so early in the year they are "since the upgrade", which
        the context shows as costs since the start. Never raises."""
        try:
            sym = str(fill.get("symbol") or "")
            base_a, quote_a = parse_symbol(sym)
            b, q, fee = D(fill.get("base") or 0), D(fill.get("quote") or 0), D(fill.get("fee") or 0)
            avg = D(fill.get("avg_price") or 0)
            usdt = self._last_prices.get(SAFE)
            if quote_a == IRT:
                to_irt = D(1)
            elif quote_a == USDT and usdt:
                to_irt = D(usdt)
            else:
                return
            fa = fill.get("fee_asset")
            if fee <= 0:
                fee_irt = ZERO
            elif fa == IRT:
                fee_irt = fee
            elif fa == USDT and usdt:
                fee_irt = fee * D(usdt)
            elif fa == base_a and avg > 0:
                fee_irt = fee * avg * to_irt
            elif fa == quote_a:
                fee_irt = fee * to_irt
            else:
                fee_irt = ZERO
            slip = ZERO
            if ref_px is not None and avg > 0 and b > 0:
                ref = D(ref_px)
                if ref > 0:
                    slip = ((avg - ref) if str(fill.get("side")) == "buy" else (ref - avg)) * b * to_irt
            costs = self.state.get("costs") if isinstance(self.state.get("costs"), dict) else {}
            t = float(now)
            turnover = [[float(a), float(v)] for a, v in (costs.get("turnover") or []) if t - float(a) < 7 * 24 * HOUR]
            turnover.append([round(t, 3), float(q * to_irt)])
            costs = {"since": float(costs.get("since") or t), "fees_irt": float(costs.get("fees_irt") or 0.0) + float(fee_irt),
                     "slippage_irt": float(costs.get("slippage_irt") or 0.0) + float(slip), "fills": int(costs.get("fills") or 0) + 1,
                     "turnover": turnover}
            self._save_state(costs=costs)
        except Exception as e:  # noqa: BLE001 - a meter, never a reason to lose a fill
            log.warning("cost meter: fill not booked (%s)", e)

    def _cost_totals(self, now):
        """{"fees_irt", "slippage_irt", "turnover_7d_irt"} (toman) from the running totals of _note_cost,
        for the context's portfolio block; an empty dict before the first booked fill."""
        costs = self.state.get("costs")
        if not isinstance(costs, dict) or not costs:
            return {}
        t = float(now)
        turn = sum(float(v) for a, v in (costs.get("turnover") or []) if t - float(a) < 7 * 24 * HOUR)
        return {"fees_irt": float(costs.get("fees_irt") or 0.0), "slippage_irt": float(costs.get("slippage_irt") or 0.0),
                "turnover_7d_irt": turn}

    def _min_trade_weight(self, equity):
        """The risk manager's minimum order as a fraction of the managed equity, with headroom for
        the price move between the bar close (planning) and the order book (vetting): the brain
        plans no change smaller than this, so none of its orders is refused as below the minimum."""
        try:
            eq = float(equity)
            m = float(self.risk.min_order_irt)
        except (AttributeError, TypeError, ValueError):
            return None
        if eq <= 0 or m <= 0 or not math.isfinite(eq) or not math.isfinite(m):
            return None
        return m * (1.0 + MIN_ORDER_HEADROOM) / eq

    @staticmethod
    def _expires_at(d):
        fn = getattr(d, "expiry", None)          # KimiBrain Decision: expires_at, else decided_at + 1 h
        if callable(fn):
            try:
                exp = float(fn())
                if math.isfinite(exp) and exp > 0:
                    return exp
            except (TypeError, ValueError):
                pass
        exp = getattr(d, "expires_at", None)
        try:
            exp = float(exp) if exp is not None else None
        except (TypeError, ValueError):
            exp = None
        if exp is None or exp <= 0:
            exp = float(getattr(d, "decided_at", 0) or 0) + DECISION_TTL
        return exp

    @staticmethod
    def _drift(d, cur):
        """Largest weight change since the decision was computed (0 if the decision does not say).

        An EMPTY computed_against is not "unknown" for a decision that carries computed_cash: it
        means the portfolio was 100% toman then (the fresh-account state). Treating it as unknown
        turned the guard off for exactly those decisions."""
        against = getattr(d, "computed_against", None)
        if not isinstance(against, dict):
            return 0.0
        if not against and getattr(d, "computed_cash", None) is None:
            return 0.0                      # a decision from an older version: it does not say
        drift = 0.0
        for s in set(against) | set(cur):
            try:
                drift = max(drift, abs(float(cur.get(s, 0.0) or 0.0) - float(against.get(s, 0.0) or 0.0)))
            except (TypeError, ValueError):
                continue
        return drift

    def _decision_targets(self, d, cur_dec, keep_extras):
        """Decision.targets -> Decimal targets for plan_orders over the managed symbols. Extra sleeve
        coins (not tradable by the brain) are exited, except by a cash sweep (which never sells)."""
        raw = getattr(d, "targets", None) or {}
        targets = {}
        for s in self.symbols:
            if s in raw:
                try:
                    x = float(raw[s])
                except (TypeError, ValueError):
                    x = float("nan")
                if not math.isfinite(x):
                    raise RunnerError("decision target of %s is not a number: %r" % (s, raw[s]))
                targets[s] = min(max(x, 0.0), 1.0)
            else:   # the contract lists every allowed symbol; a missing one is left as it is
                targets[s] = float(cur_dec.get(s, ZERO))
        unknown = sorted(set(raw) - set(self.symbols))
        if unknown:
            log.warning("decision targets %s are not managed by the bot: ignored", ", ".join(unknown))
        for s in self.extra_symbols:
            targets[s] = float(cur_dec.get(s, ZERO)) if keep_extras else 0.0
        tot = sum(targets.values())
        if tot > 1.0 + 1e-9:
            targets = {s: v / tot for s, v in targets.items()}
        return {s: D(repr(v)) for s, v in targets.items()}

    def _pump_guarded(self, now, failed=None):
        """{symbol: guard} of the brain's coins that the anti-pump buy guard catches on the RUNNER'S OWN
        hourly candles (analysis.pump_guard with the kimi.json "guard" numbers the brain carries). The
        brain already refuses to raise a coin the context marks pump_guard; this is the independent
        re-check right before a Kimi decision is executed, and the only check of the crash-ladder bids
        (_ladder_pumped). `failed` (a set): the symbols whose check raised are added to it."""
        from .analysis import pump_guard, usdt_closes, validate_guard_config
        try:
            g = validate_guard_config((getattr(self.brain, "cfg", None) or {}).get("guard"))
        except Exception:  # noqa: BLE001 - the brain's config was validated at start; the defaults then
            g = validate_guard_config(None)
        if g.get("pump_rise_pct") is None:
            return {}
        usdt = self._cache.get(SAFE) or []
        ts, cl = [b.ts for b in usdt], [b.close for b in usdt]
        out = {}
        if not ts:
            return out
        for s in self.symbols:
            bars = self._cache.get(s)
            if s == SAFE or not bars:
                continue
            try:
                r = pump_guard(usdt_closes(bars, ts, cl, s), now, g["pump_rise_pct"], g["pump_window_hours"],
                               g["pump_lookback_hours"])
            except Exception as e:  # noqa: BLE001 - a re-check only: the brain's own guard stands
                log.warning("pump guard re-check of %s failed: %s", s, e)
                if failed is not None:
                    failed.add(s)
                continue
            if r is not None:
                out[s] = r
        return out

    def _pump_clamp(self, targets, cur_dec, now, info, decision=None):
        """A Kimi decision's target ABOVE the current weight of a pump-guarded coin (_pump_guarded) is cut
        back to the current weight, the difference to USDT_IRT (a sale is never touched). Each clamp is
        recorded like a brain adjustment - in the decision's adjustments, the brain's record of it
        (KimiBrain.note_runner_adjustment: recent_decisions shows it to the model) and info["adjustments"]
        (kimi_runner.jsonl) - and as a bot event "pump_guard" (scope "allocation") for the owner.
        Returns targets."""
        try:
            guarded = self._pump_guarded(now)
        except Exception as e:  # noqa: BLE001
            log.warning("pump guard re-check failed: %s", e)
            return targets
        clamped = []
        for s in sorted(guarded):
            t, c = targets.get(s), D(cur_dec.get(s, ZERO) or ZERO)
            if t is None or t <= c:
                continue
            targets[s] = c
            targets[SAFE] = targets.get(SAFE, ZERO) + (t - c)
            clamped.append((s, float(t), float(c)))
        if not clamped:
            return targets
        txt = "; ".join("%s rose %g%% within the guard window (until %s)" % (
            s, guarded[s]["rise_pct"], fmt_ts(guarded[s]["until"])) for s, _, _ in clamped)
        log.error("PUMP GUARD (runner re-check): the decision would buy %s - kept at the current weight, the rest "
                  "to USDT_IRT", txt)
        info["pump_guard"] = [s for s, _, _ in clamped]
        notes = ["%s: increase blocked by the runner's pump-guard re-check (+%g%% within the guard window on its own "
                 "candles, until %s): kept at %.4f instead of %.4f, the rest to %s"
                 % (s, guarded[s]["rise_pct"], fmt_ts(guarded[s]["until"]), c, t, SAFE) for s, t, c in clamped]
        info["adjustments"] = list(info.get("adjustments") or []) + notes
        at = getattr(decision, "decided_at", None)
        adj = getattr(decision, "adjustments", None)
        if isinstance(adj, list):
            adj.extend(notes)
        record = getattr(self.brain, "note_runner_adjustment", None)
        if callable(record) and at is not None:
            try:
                record(at, notes)
            except Exception as e:  # noqa: BLE001 - bookkeeping only; the clamp itself stands
                log.warning("the brain could not record the runner's pump-guard adjustment: %s", e)
        for (s, t, c), note in zip(clamped, notes):
            self._event("pump_guard", key="allocation:%s:%s" % (s, at if at is not None else round(float(now), 3)),
                        scope="allocation", symbol=s, coin=parse_symbol(s)[0], rise_pct=guarded[s].get("rise_pct"),
                        until=guarded[s].get("until"), target=round(t, 6), kept=round(c, 6), decided_at=at,
                        why=note)
        return targets

    def _research_takes(self, name):
        """Whether self.news.research() accepts the keyword `name` (an older news module does not)."""
        try:
            sig = inspect.signature(self.news.research)
        except (TypeError, ValueError, AttributeError):
            return False
        return name in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    @staticmethod
    def _session_closed(sym, now):
        """Why no buy order may go to `sym` now, or None: a session-bound token (a tokenized US stock /
        ETF / oil / gas: everything but crypto, gold and silver, analysis.session_bound) outside the US
        session (Mon-Fri 09:30-16:00 New York, analysis.us_session_open). Off the session Bitpin's quote of
        such a token is a stale print with 2-3% noise (year review Y5): the brain may not increase it
        then (the context marks it blocked "us market closed") and this is the runner's own check
        right before its orders, so a buy leg retried an hour later never lands on a dead quote.
        Selling is never held back. An older analysis module without the helpers: nothing is closed."""
        from . import analysis as an
        bound, is_open = getattr(an, "session_bound", None), getattr(an, "us_session_open", None)
        if not callable(bound) or not callable(is_open):
            return None
        try:
            if not bound(sym) or is_open(float(now)):
                return None
        except Exception as e:  # noqa: BLE001 - a broken helper must not stop the cycle; the brain's guard stands
            log.warning("US session check of %s failed: %s", sym, e)
            return None
        when = Runner._us_next_open(now)
        return "us market closed (Mon-Fri 09:30-16:00 New York%s)" % ((": next open %s" % fmt_ts(when)) if when else "")

    def _spread_blocked(self, sym):
        """Why no buy order may go to the session-bound token `sym` now because of its order book, or
        None: a spread above analysis.RWA_MAX_SPREAD_PCT (1%) or an empty side. The context marks such a
        token blocked and the brain may not increase it; this is the runner's own check right before the
        order (spec C3), next to the US-session check. Crypto, gold and silver are not checked here (the
        risk manager's slippage guard covers every market). A failed book read blocks nothing: the
        order's own vet reads the book again and refuses a bad one."""
        from . import analysis as an
        bound, cap = getattr(an, "session_bound", None), getattr(an, "RWA_MAX_SPREAD_PCT", None)
        if not callable(bound) or cap is None:
            return None
        try:
            if not bound(sym):
                return None
            book = self.broker.order_book(sym)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - the order's vet still reads the book
            log.warning("spread check of %s failed: %s", sym, e)
            return None
        bid, ask = best_bid(book), best_ask(book)
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return "empty order book side (us market token)"
        spread = float((ask - bid) / ((ask + bid) / 2) * 100)
        if spread > float(cap):
            return "spread %.2f%% > %.2f%% (us market token)" % (spread, float(cap))
        return None

    @staticmethod
    def _us_next_open(now):
        """Epoch of the next US session open after `now` (analysis.us_session_next_open), or None."""
        from . import analysis as an
        nxt = getattr(an, "us_session_next_open", None)
        try:
            when = nxt(float(now)) if callable(nxt) else None
        except Exception:  # noqa: BLE001
            return None
        return float(when) if when else None

    def _session_clamp(self, targets, cur_dec, now, info, decision=None):
        """A Kimi decision's target ABOVE the current weight of a session-bound token while the US session
        is closed (_session_closed) is cut back to the current weight, the difference to USDT_IRT (a sale is
        never touched) - the same bookkeeping as _pump_clamp: a decision adjustment, the brain's record of
        it (recent_decisions shows it to the model), info["session_closed"] and a bot event
        "session_closed" for the owner. Returns targets."""
        clamped = []
        for s in sorted(targets):
            t, c = targets.get(s), D(cur_dec.get(s, ZERO) or ZERO)
            if t is None or t <= c or s == SAFE:
                continue
            why = self._session_closed(s, now) or self._spread_blocked(s)
            if why is None:
                continue
            targets[s] = c
            targets[SAFE] = targets.get(SAFE, ZERO) + (t - c)
            clamped.append((s, float(t), float(c), why))
        if not clamped:
            return targets
        log.error("US SESSION (runner check): the decision would buy %s while the US market is closed or its book "
                  "is too wide - kept at the current weight, the rest to USDT_IRT", ", ".join(s for s, _, _, _ in clamped))
        info["session_closed"] = [s for s, _, _, _ in clamped]
        notes = ["%s: increase blocked by the runner's US-session check (%s): kept at %.4f instead of %.4f, the rest "
                 "to %s" % (s, why, c, t, SAFE) for s, t, c, why in clamped]
        info["adjustments"] = list(info.get("adjustments") or []) + notes
        at = getattr(decision, "decided_at", None)
        adj = getattr(decision, "adjustments", None)
        if isinstance(adj, list):
            adj.extend(notes)
        record = getattr(self.brain, "note_runner_adjustment", None)
        if callable(record) and at is not None:
            try:
                record(at, notes)
            except Exception as e:  # noqa: BLE001 - bookkeeping only; the clamp itself stands
                log.warning("the brain could not record the runner's US-session adjustment: %s", e)
        next_open = self._us_next_open(now)
        for (s, t, c, why), note in zip(clamped, notes):
            self._event("session_closed", key="allocation:%s:%s" % (s, at if at is not None else round(float(now), 3)),
                        scope="allocation", symbol=s, coin=parse_symbol(s)[0], target=round(t, 6), kept=round(c, 6),
                        decided_at=at, why=note, next_open=next_open,
                        reason="session" if why.startswith("us market closed") else "spread")
        return targets

    def _ladder_pumped(self, now, failed=None):
        """{coin: guard} of the crash-ladder coins the anti-pump buy guard catches (_pump_guarded on the
        runner's own candles): while a coin is guarded its ladder bids are neither placed nor re-armed,
        and resting ones are cancelled (_maintain_ladder). `failed` (a set): coins whose check failed."""
        errs = set()
        try:
            g = self._pump_guarded(now, errs)
        except Exception as e:  # noqa: BLE001
            log.warning("pump guard of the ladder coins failed: %s", e)
            g, errs = {}, {"%s_%s" % (c, IRT) for c in self.ladder_coins}
        out = {}
        for c in self.ladder_coins:
            sym = "%s_%s" % (c, IRT)
            if sym in g:
                out[c] = g[sym]
            elif sym in errs and failed is not None:
                failed.add(c)
        return out

    def _redact(self, text):
        """Secrets out of anything that reaches a file on disk. kimi_runner.jsonl carries
        brain_error (the text of ANY exception escaping the brain path) and news.error, so it gets
        the same redaction as the brain's own kimi_decisions.jsonl: a proxy URL with credentials, a
        Bearer token or an sk- key in an exception message must not land in the audit log, which
        `bitpin-bot run` prints and the /opt/bitpin-bot.bak-* backups copy."""
        for fn in (getattr(self.brain, "_redact", None), getattr(self.news, "redact", None),
                   getattr(getattr(self.brain, "llm", None), "redact", None)):
            if callable(fn):
                try:
                    return fn(text)
                except Exception:  # noqa: BLE001 - try the next one; never lose the line over this
                    continue
        try:
            from .llm import redact_text
            return redact_text(text)
        except Exception:  # noqa: BLE001
            return text

    def _unexecutable_legs(self, d, plan, targets=None, skip=()):
        """Legs of a Kimi decision that the recomputed plan no longer executes. The brain sizes every
        change to at least one minimum order AT DECISION TIME, but the portfolio may have moved by up
        to DECISION_DRIFT_LIMIT since, and plan_orders recomputes the deltas against the NEW weights:
        a leg then falls under the rebalance threshold (dropped) or under min_order_irt (refused by
        RiskManager.vet_order). Without this it shows up only inside rep["skipped"]. targets: what the
        plan was made from (the decision's targets after the runner's own pump-guard clamp); skip: the
        symbols that clamp cut back - their leg is the clamp's (recorded there), never "unexecutable"."""
        against = getattr(d, "computed_against", None)
        against = against if isinstance(against, dict) else {}
        changed = {}
        src = targets if isinstance(targets, dict) else (getattr(d, "targets", None) or {})
        for s, w in src.items():
            if s in skip:
                continue
            try:
                delta = float(w) - float(against.get(s, 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if abs(delta) >= float(self.threshold):
                changed[s] = delta
        planned = {o.symbol for o in plan.orders}
        out = []
        for s in sorted(changed):
            if s not in planned:
                out.append("%s (planned %+.4f of equity, now below the rebalance threshold %s)"
                           % (s, changed[s], self.threshold))
        try:
            floor = D(self.risk.min_order_irt)
        except Exception:  # noqa: BLE001
            floor = ZERO
        for o in plan.orders:
            if o.symbol in changed and not o.full_exit and floor > 0 and o.est_notional < floor:
                out.append("%s (%s ~%s IRT, below min_order_irt %s)"
                           % (o.symbol, o.side, fmt_amount(o.est_notional), fmt_amount(floor)))
        return out

    def _brain_log(self, rec):
        if self.dry_run:
            return
        try:
            line = self._redact(json.dumps(rec, sort_keys=True, default=str, ensure_ascii=False))
            with open(os.path.join(self.state_dir, BRAIN_RUNNER_LOG), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 - audit only; never abort trading
            log.warning("cannot append to %s: %s", BRAIN_RUNNER_LOG, e)

    @staticmethod
    def _decision_summary(d):
        out = {"decided_at": getattr(d, "decided_at", None), "valid": bool(getattr(d, "valid", False)),
               "hold": bool(getattr(d, "hold", False)), "confidence": getattr(d, "confidence", None),
               "fallback": bool(getattr(d, "fallback", False)),
               "fallback_reason": getattr(d, "fallback_reason", None),
               "error_kind": getattr(d, "error_kind", "") or None,
               "error": (getattr(d, "error", "") or "")[:300] or None,
               "targets": {s: w for s, w in (getattr(d, "targets", None) or {}).items() if w},
               "expires_at": Runner._expires_at(d)}
        for k in ("mode", "trigger_kind"):
            v = getattr(d, k, None)
            if isinstance(v, str) and v:
                out[k] = v
        lad = getattr(d, "ladder", None)
        if isinstance(lad, dict) and lad:
            out["ladder"] = dict(lad)
        ex = getattr(d, "exits", None)
        if isinstance(ex, dict) and ex:
            out["exits"] = {s: {k: e.get(k) for k in ("stop_pct", "target_price", "max_hold_until")}
                            for s, e in ex.items() if isinstance(e, dict)}
        return out

    def _brain_choose(self, now, cur, cur_dec, cash_w, equity, snap, info):
        """Ask KimiBrain (when a decision is due) and pick what to execute this bar:
        (decision, kind) with kind "kimi" / "cash_sweep" / "derisk" / "endgame", or (None, None) = no
        trade. With the ladder contract (self._v2) the brain gets the runner's ladder / positions,
        the decision its mode, the news its force / focus, and every valid Kimi decision's ladder
        scales and exits are applied here (also a HOLD, expired or stale one).
        Never raises for an LLM / context / brain problem (logged; the bot then does not trade,
        except for the brain's fallback). cur_dec: plan_orders' Decimal weights of every managed
        symbol (sleeve coins outside the brain's symbols included)."""
        brain = self.brain
        made = None
        failed = None
        if equity <= 0:
            info.update(action="none", why="managed equity is 0")
            log.warning("managed equity is 0: nothing for Kimi to allocate")
            return None, None
        # the smallest change the brain may plan: the rebalance threshold, or one minimum order
        mtw = self._min_trade_weight(equity)
        step = max(float(self.threshold), mtw or 0.0)
        if mtw is not None and mtw > float(self.threshold):
            info["min_trade_weight"] = round(mtw, 6)
        v2 = self._v2
        bot = {}
        if v2:
            # the runner's own ladder / position state (None = that feature is off)
            bot = {"ladder": self._ladder_view(now), "positions": self._positions_view()}
        try:
            quick = self.builder.quick_context(snap)
            due = brain.should_decide(now, brain.last_decision, quick, slack_seconds=BRAIN_SCHEDULE_SLACK, **bot)
            if v2:
                self._brain_notifications(now, info)
            if due:
                trigger = getattr(brain, "last_trigger", None)
                info["trigger"] = trigger
                kw = {}
                bkw = {}
                nkw = {}
                if v2:
                    mode = getattr(brain, "last_mode", None)
                    events = list(getattr(brain, "last_events", None) or [])
                    tkind = getattr(brain, "last_trigger_kind", None)
                    info.update(trigger_kind=tkind, mode=mode)
                    if events:
                        info["events"] = [str(e.get("text") or e.get("kind"))[:200] for e in events
                                          if isinstance(e, dict)]
                    if tkind in WATCHDOG_KINDS:
                        self._event("watchdog", key="%s:%s" % (tkind, round(float(now))), trigger_kind=tkind,
                                    mode=mode, trigger=trigger, events=info.get("events"))
                    try:
                        nr = brain.news_request() or {}
                        nkw = {"force": bool(nr.get("force")), "focus": nr.get("focus")}
                        if nr.get("focus_key"):
                            nkw["focus_key"] = nr.get("focus_key")
                    except Exception as e:  # noqa: BLE001 - the brief is then the normal (cached) one
                        log.warning("news request of the brain failed: %s", e)
                        nkw = {}
                    if nkw.get("force"):
                        info["news_forced"] = True
                    eg = self._endgame(now)
                    bkw = dict(mode=mode, events=events, endgame=eg, **bot)
                    if self.exits_on:
                        # the code exits of the last days: Kimi is told the code sold them on purpose, and
                        # the brain blocks buying such a coin back within 24 h (anti-churn)
                        bkw["recent_exits"] = self._recent_exits_view(now)
                    kw.update(mode=mode, **bot)
                stopped = None
                spent = self._llm_budget_spent()
                if spent:                      # stage 2 cannot run: do not buy a stage-1 brief for it
                    log.warning("Kimi decision due (%s) but the daily LLM budget is used up (%s): no news research "
                                "this hour either", trigger, spent)
                    info["llm_budget"] = spent
                if self.news is not None and not spent:   # only then: a brain without news support keeps working
                    # stage 1 BEFORE the market context: a research call can take minutes (deadline
                    # 180 s, a flaky proxy), and stage 2 must decide on prices fetched AFTER it
                    brief = self._research_news(now, cur, **nkw)
                    kw["news"] = brief
                    info["news"] = self._news_summary(brief)
                    stopped = self._abort_reason()
                if stopped:                    # STOP created during the news call: no decision this hour
                    log.warning("%s during the news research: no Kimi decision this hour", stopped)
                    info.update(action="none", why="kill switch: %s" % stopped)
                    return None, None
                log.info("Kimi decision due (%s): building the market context", trigger)
                ctx = self.builder.build(snap, recent_decisions=brain.recent_decisions(), abort=self._abort_reason,
                                         **bkw)
                # now = THIS cycle's time, not the clock after the news research and the context
                # build: decided_at (and the pacing history) must not drift with the work time, or
                # the next hourly check is just short of the interval and a decision is lost
                made = brain.decide(ctx, cur, trigger=trigger, abort=self._abort_reason, min_trade_weight=mtw,
                                    order_budget=self._order_budget_info(), now=now, **kw)
            else:
                block = getattr(brain, "pacing_block", None)
                if block:
                    info["pacing_block"] = block
                    log.info("a Kimi decision is due but postponed by the pacing limits: %s", block)
                else:
                    self._warn_if_schedule_slipped(now, brain)
                    log.info("no Kimi decision due this hour")
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - never crash the bot on the brain path
            failed = "%s: %s" % (type(e).__name__, e)
            log.exception("Kimi brain / market context failed (%s): no Kimi trade this hour", failed)
            info["brain_error"] = failed[:300]
        latest = made if made is not None else brain.last_decision
        info["decided"] = made is not None
        if latest is not None:
            info["decision"] = self._decision_summary(latest)
        if made is not None and v2:
            self._decision_event(made, info)
        if v2 and failed is None and latest is not None and getattr(latest, "valid", False) \
                and not getattr(latest, "fallback", False) and self._origin_ok(latest):
            # the ladder scales and exits of EVERY valid Kimi decision - also a HOLD, an expired or a
            # stale one - are applied (once per decision)
            self._apply_decision_extras(latest, now, info)
        # 1. a valid Kimi decision that has not been executed yet (and is neither expired nor stale)
        if failed is None and latest is not None and getattr(latest, "valid", False):
            executed = self.state.get("brain_executed")
            if getattr(latest, "hold", False):
                info["decision_status"] = "hold"
                if made is not None:
                    info.update(action="hold", why="valid decision: HOLD (every change below the rebalance threshold)")
                    log.info("Kimi: HOLD - nothing to execute")
                else:
                    info.update(action="none", why="latest Kimi decision was a HOLD")
            elif executed is not None and abs(float(executed) - float(latest.decided_at or 0)) < 1e-6:
                info.update(action="none", why="latest Kimi decision already executed", decision_status="executed")
                log.info("latest Kimi decision (%s) already executed", fmt_ts(latest.decided_at))
            elif not self._origin_ok(latest):
                # kimi_brain_state.json / kimi_decisions.jsonl are shared by every mode that uses this
                # state dir, but "executed once" (brain_executed) lives in runner_state_<mode>.json.
                # Without this, a live runner would treat a decision made by a paper run - including a
                # canned --test-kimi-reply - as "not executed yet" and trade it with REAL money.
                origin = str(getattr(latest, "origin", "") or "")
                info.update(action="foreign", decision_status="foreign",
                            why="decision was made by %s, this bot is %s: NOT executed"
                                % (origin or "an unknown run", self._origin()))
                log.error("Kimi decision of %s was made by %s, but this process is %s: NOT executed. A decision "
                          "made by another run (a paper run or a test hook on the same --state-dir) is never "
                          "traded here; a new decision follows the normal schedule.",
                          fmt_ts(latest.decided_at), origin or "an unknown run", self._origin())
            elif self.clock() >= self._expires_at(latest):
                info.update(action="expired", why="decision expired at %s" % fmt_ts(self._expires_at(latest)),
                            decision_status="expired")
                log.warning("Kimi decision of %s EXPIRED at %s: NOT executed", fmt_ts(latest.decided_at),
                            fmt_ts(self._expires_at(latest)))
            else:
                drift = self._drift(latest, cur)
                if drift > DECISION_DRIFT_LIMIT:
                    info.update(action="stale", why="portfolio weights moved %.3f since the decision" % drift,
                                decision_status="stale")
                    log.warning("Kimi decision of %s NOT executed: the portfolio weights moved by up to %.3f since it "
                                "was computed (limit %.2f); the next decision uses the new weights",
                                fmt_ts(latest.decided_at), drift, DECISION_DRIFT_LIMIT)
                else:
                    info.update(action="kimi", why="valid Kimi decision", decision_status="executing")
                    return latest, "kimi"
        elif latest is not None:
            info["decision_status"] = "invalid"
        # 2. the brain's fallback, ONLY after an invalid decision or when no decision was due but the
        #    toman cash is above the limit: cash sweep into USDT_IRT; derisk after a long LLM outage.
        #    Without a new decision the brain is asked only when the excess cash is large enough to be
        #    traded at all (>= one executable step: rebalance_threshold or one minimum order), so the
        #    rest left by a sweep does not re-trigger it.
        latest_invalid = failed is not None or (latest is not None and not getattr(latest, "valid", False))
        # the brain's cash limit: min(max_irt_cash, fallback.sweep_irt_above), or the toman share of its
        # last valid decision while that is recent (KimiBrain.fallback_cash_limit)
        fcl = getattr(brain, "fallback_cash_limit", None)
        try:
            max_cash = float(fcl(now)) if callable(fcl) else \
                float((getattr(brain, "limits", None) or {}).get("max_irt_cash", 0.05))
        except Exception as e:  # noqa: BLE001 - never crash the bot on the brain path
            log.warning("brain cash limit unavailable (%s): using 5%%", e)
            max_cash = 0.05
        excess = cash_w - max_cash
        cash_due = made is None and excess > 1e-9 and excess >= step
        if not (latest_invalid or cash_due):
            if info.get("action") is None:
                info.update(action="none", why="%s; IRT cash %.4f within the limit %.2f (+ one step %.4f)" % (
                    "decision postponed (pacing)" if info.get("pacing_block") else "no decision due", cash_w, max_cash,
                    step))
            return None, None
        info.pop("why", None)
        # the IRT values of the SAME holdings, so the brain takes the toman cash weight from the
        # wallet (coins it does not trade - e.g. sleeve coins of an earlier symbol list - are not cash)
        values = {"IRT": float(cash_w * float(equity))}
        values.update({s: float(w * equity) for s, w in cur_dec.items() if w > 0})
        try:
            # the current time (a failed Kimi call may have taken minutes): the fallback's expiry and the
            # derisk delay are measured from it
            fkw = {"positions": self._positions_view()} if v2 else {}
            fb = brain.fallback_decision(cur, self.clock(), balances_irt_value=values, min_trade_weight=mtw, **fkw)
        except Exception as e:  # noqa: BLE001
            log.exception("brain fallback failed: %s", e)
            info.update(action="none", why="fallback failed: %s" % e)
            return None, None
        if fb is None or not getattr(fb, "valid", False):
            if latest_invalid:
                info.update(action="none", why="no valid Kimi decision and no fallback due: not trading")
                log.warning("no valid Kimi decision: NOT trading this hour (IRT cash %.2f%%, limit %.0f%%)",
                            cash_w * 100, max_cash * 100)
            else:
                info.update(action="none", why="the brain returned no fallback")
            return None, None
        kind = getattr(fb, "fallback_reason", None) or "fallback"
        info["fallback"] = self._decision_summary(fb)
        if self.clock() >= self._expires_at(fb):
            info.update(action="expired", why="fallback decision expired")
            log.warning("fallback decision %s expired: NOT executed", kind)
            return None, None
        info.update(action=kind, why="brain fallback: %s" % kind)
        if kind == "derisk":
            info["why"] = ("no valid Kimi decision for a long time: managed coins into USDT_IRT (at most %.0f%% "
                           "toman)%s" % (max_cash * 100, "; coins with live code exits are left to them" if v2 else ""))
        elif kind == "endgame":
            info["why"] = ("no valid FINAL Kimi decision after the endgame final slot: every coin into USDT_IRT (the "
                           "default end state, never toman)")
        else:
            info["why"] = "IRT cash %.2f%% > cash limit %.0f%%: excess toman into USDT_IRT, coins untouched" % (
                cash_w * 100, max_cash * 100)
        return fb, kind

    def _warn_if_schedule_slipped(self, now, brain):
        """Loud line when the hourly schedule silently lost a decision: should_decide() said no and
        did not blame the pacing limits, although the last decision is already older than
        decision_interval_hours minus BRAIN_SCHEDULE_SLACK. Without it the only trace is
        "no Kimi decision due this hour", and a persistently late candle feed (or a slow stage 1)
        halves the decision rate for as long as it lasts."""
        try:
            if getattr(brain, "clock_schedule", False) is True:
                return           # clock slots (decision_times_local): a slot stays due until it ran
            last = getattr(brain, "last_decision", None)
            at = float(getattr(last, "decided_at", 0) or 0)
            hours = float((getattr(brain, "cfg", None) or {}).get("decision_interval_hours") or 0)
            if at <= 0 or hours <= 0:
                return
            gap = float(now) - at
            if gap > hours * HOUR - BRAIN_SCHEDULE_SLACK:
                log.warning("the hourly Kimi schedule SLIPPED: the last decision was %.0f min ago (interval %g h, "
                            "slack %.0f min) and none is due at this check, so the next one is a whole interval "
                            "away. The decision time drifts when a cycle starts late (a late candle) or stage 1 / "
                            "the context build take long.", gap / 60.0, hours, BRAIN_SCHEDULE_SLACK / 60.0)
        except Exception as e:  # noqa: BLE001 - diagnostics only
            log.debug("schedule check failed: %s", e)

    def _origin(self):
        """Who this runner is, for Decision.origin: the brain's own origin when it has one (run_bot.py
        sets it from the mode and the test hooks), else the runner mode."""
        o = str(getattr(self.brain, "origin", "") or "")
        return o or self.mode

    def _origin_ok(self, d):
        """True when `d` was made by THIS runner (same mode / test-hook identity), so it may be
        executed. kimi_brain_state.json is shared by every mode using the same state dir, while the
        executed-once marker is per mode, so without this check a live run could execute a decision
        that a paper run (or a canned test reply) left behind."""
        mine = str(getattr(self.brain, "origin", "") or "")
        if not mine:
            return True                     # no origin configured (library use / older state): unchanged behaviour
        return str(getattr(d, "origin", "") or "") == mine

    def _order_budget_info(self):
        """{"remaining", "max_per_24h"} of the daily order cap, for the decision prompt: a rebalance
        whose orders do not all fit is skipped as a whole (_order_budget_ok), so the model must know."""
        try:
            return {"remaining": int(self.risk.remaining_orders()), "max_per_24h": int(self.risk.max_orders_per_day)}
        except Exception as e:  # noqa: BLE001 - informational only
            log.debug("order budget unavailable: %s", e)
            return None

    def _llm_budget_spent(self):
        """Why stage 2 cannot run at all today (daily call or token budget used up), or None. Checked
        BEFORE the stage-1 news research, which would otherwise buy a brief for a decision that fails
        immediately at budget.consume()."""
        try:
            b = getattr(getattr(self.brain, "llm", None), "budget", None)
            if b is None:
                return None
            if b.remaining() <= 0:
                return "max_calls_per_day reached (%d)" % b.limit
            if b.tokens_exhausted():
                return "max_tokens_per_day reached (%d)" % b.max_tokens
        except Exception as e:  # noqa: BLE001 - never block a decision on this pre-check
            log.debug("LLM budget pre-check failed: %s", e)
        return None

    def _research_news(self, now, cur, force=False, focus=None, focus_key=None):
        """Stage 1: the (cached, ~2 h) news brief for this decision. Never raises; None with the kill
        switch on. The STOP kill switch is polled before every news request and during retry waits
        (abort=...). The hint names coins only - never amounts or weights. force / focus / focus_key: the
        brain's news_request() for a veto or a ladder-fill review (a fresh brief about that event, reused
        while it is recent when the same event is asked about again)."""
        if self._abort_reason():
            return None
        try:
            held = sorted(s.split("_")[0] for s, w in (cur or {}).items() if w and w > 0.01 and s != "USDT_IRT")
            coins = [s.split("_")[0] for s in getattr(self.brain, "allowed", []) if s != "USDT_IRT"]
            hint = "held: %s; tradable on Bitpin: %s" % (", ".join(held) or "none (toman/USDT only)", ", ".join(coins))
            kw = {}
            if force:
                kw["force"] = True
            if focus:
                kw["focus"] = str(focus)[:300]
            if force and focus_key:
                kw["focus_key"] = str(focus_key)[:120]
            # B3: after a plain HOLD the daily brief is reused until it is 48 h old (news.after_hold_only,
            # news.research_gate); the researcher needs the kind of the brain's last decision for that.
            # Only the clock slots are gated: a wake-up (forced or not: held move, drawdown, max hold, plan
            # invalidation) refreshes a brief older than the cache as in v2 (v3 money review). Older news
            # modules take no such argument: the gate is then simply off (the brief is researched as before).
            from . import news as news_mod
            from . import brain as brain_mod
            slot = getattr(self.brain, "last_trigger_kind", None) in getattr(brain_mod, "SLOT_KINDS",
                                                                             ("first", "scheduled", "final"))
            kind_fn = getattr(news_mod, "decision_kind", None)
            if callable(kind_fn) and not force and slot and self._research_takes("last_decision_kind"):
                try:
                    kw["last_decision_kind"] = kind_fn(getattr(self.brain, "last_decision", None))
                except Exception:  # noqa: BLE001 - the gate is a saving, never a blocker
                    pass
            return self.news.research(now, context_hint=hint, abort=self._abort_reason, **kw)
        except Exception as e:  # noqa: BLE001 - research() never raises; defence in depth
            log.warning("news research failed: %s", e)
            # NOT None: None means "no news research is configured" in the stage-2 prompt. A failure
            # here must tell the model that the research was UNAVAILABLE.
            try:
                from bitpin.news import NewsBrief
                # redacted here: this branch bypasses NewsResearcher.redact() / llm.redact(), and the
                # message goes into kimi_runner.jsonl and into the stage-2 prompt's UNAVAILABLE line
                return NewsBrief(ok=False, error=self._redact("internal: %s: %s"
                                                              % (type(e).__name__, str(e)[:200]))[:300])
            except Exception:  # noqa: BLE001
                return None

    @staticmethod
    def _news_summary(b):
        """The news brief's state for kimi_runner.jsonl and the report (no text)."""
        if b is None:
            return None
        return {"ok": bool(getattr(b, "ok", False)), "fetched_at": getattr(b, "fetched_at", None),
                "cached": bool(getattr(b, "cached", False)), "stale": bool(getattr(b, "stale", False)),
                "items": len(getattr(b, "items", None) or []), "searches": getattr(b, "searches", 0),
                "error": str(getattr(b, "error", "") or "")[:200]}

    def _book(self, syms, prices, units, cash):
        """(hold targets, plan) of the managed book: the current weights as targets, no orders."""
        base = plan_orders({s: ZERO for s in syms}, units, prices, cash, self.threshold, self.fee_rate, None,
                           dust_irt=self.risk.min_order_irt)
        hold = {s: base.current.get(s, ZERO) for s in syms}
        plan = plan_orders(hold, units, prices, cash, self.threshold, self.fee_rate, None,
                           dust_irt=self.risk.min_order_irt)
        return hold, plan

    def _brain_once(self, rep, now, panel, last_close_1h, bar_ts, pending, buys_only):
        syms = list(dict.fromkeys(self.symbols + self.extra_symbols))
        prices = self._prices(syms, panel, last_close_1h)
        self._cycle_prices, self._cycle_lc = prices, last_close_1h
        if self.ladder_on or self.exits_on:
            self._refs = self._compute_refs()
        # resting orders first: their fills change the balances read below (and W1 needs them)
        fills_seen = self._fills_handled
        self._resting_sync(rep, now)
        self._endgame_ladder_off(rep, now)
        # while an order's outcome is unknown the sleeve ledger (pre-order) is the best measure
        units, cash = self._holdings(syms, clamp=not pending)
        gaps = self._price_gaps(syms, prices, units)
        frozen = self._frozen_funds(syms)
        if self.exits_on and not gaps:
            self._sync_positions(now, prices, units)
        # the current portfolio as targets: plan_orders' own weights (the basis the brain must use), no orders
        hold, plan = self._book(syms, prices, units, cash)
        # the breaker is evaluated every cycle, also while orders are pending, BEFORE any Kimi call
        dd, plan = self._drawdown(plan, hold, prices, units, cash, clamp=not pending, gaps=gaps)
        info = {"action": None}
        if frozen:
            info["frozen"] = {a: format(v, "f") for a, v in frozen.items()}
        rep.update(equity=plan.equity, equity_mid=dd.get("equity_mid"), cash=plan.cash, current=plan.current,
                   managed="sleeve" if self.capped else "account", drawdown=dd, brain=info, plan=[], plan_skipped=[])
        if frozen:
            rep["frozen"] = info["frozen"]
        cur_dec = dict(plan.current)
        # the brain's current weights: plan_orders' own `cur` over the brain's symbols (zeros left out)
        cur = {s: float(w) for s, w in cur_dec.items() if s in self.symbols and w > 0}
        cash_w = float(plan.cash / plan.equity) if plan.equity > 0 else 0.0
        log.info("=== %s%s | Kimi brain | bar %s ===", self.mode.upper(), " DRY-RUN" if self.dry_run else "",
                 fmt_ts(bar_ts))
        log.info("%s %s IRT (IRT cash %.2f%%) | drawdown %.2f%% from HWM %s",
                 ("sleeve equity (budget %s)" % fmt_amount(self.equity_cap)) if self.capped else "managed equity",
                 fmt_amount(plan.equity), cash_w * 100, dd["drawdown"] * 100, fmt_amount(dd["hwm"]))
        done = self._guard(rep, dd, pending, bar_ts)
        if done is not None:
            info.update(action="none", why="no trade: %s" % done.get("status"))
            self._save_bot_state()
            self._brain_audit(done, bar_ts, cur, cash_w)
            return done
        equity = plan.equity

        # code exits (no LLM): stops and targets on the hourly close, BEFORE Kimi is asked
        if not buys_only and self.exits_on and self._has_positions():
            ok, sold = self._run_exits(rep, now, bar_ts, prices, last_close_1h, equity, units)
            if sold:
                try:
                    units, cash = self._holdings(syms)
                    hold, plan = self._book(syms, prices, units, cash)
                    cur_dec = dict(plan.current)
                    cur = {s: float(w) for s, w in cur_dec.items() if s in self.symbols and w > 0}
                    cash_w = float(plan.cash / plan.equity) if plan.equity > 0 else 0.0
                    rep.update(current=plan.current, cash=plan.cash)
                    self._sync_positions(now, prices, units)
                except FATAL_ERRORS:
                    raise
                except Exception as e:  # noqa: BLE001 - the brain is not asked on a stale book
                    rep["errors"].append("balance read after the code exits failed: %s" % e)
                    log.error("balance read after the code exits failed (%s): no Kimi decision this hour", e)
                    ok = False
            if not ok:
                rep["status"] = "ok"
                rep["unresolved_after"] = self.broker.unresolved_count()
                info.update(action="none", why="a code-exit order has an unknown outcome (or the balance read "
                                               "failed): resolved first, no Kimi decision this hour")
                self._save_bot_state()
                self._brain_audit(rep, bar_ts, cur, cash_w)
                return rep
        if not buys_only and self.exits_on and self._has_positions():
            # Kimi's own plans: an hourly close below a plan's invalidation level wakes Kimi (never a sale)
            broken = self._check_plans(now, prices)
            if broken:
                info["plans_broken"] = broken

        state_fields = {}
        decision = None
        if buys_only:
            act = self.state.get("brain_active") or {}
            if act.get("bar_ts") != bar_ts or self.clock() >= float(act.get("expires_at") or 0) \
                    or not isinstance(act.get("targets"), dict):
                log.warning("buy retry: this bar's decision is expired or unknown; the buys are not retried")
                self._save_state(retry_buys=None)
                rep["status"] = "ok"
                info.update(action="none", why="buy retry dropped (decision expired)")
                self._maintain(rep, now, equity, syms)
                self._brain_audit(rep, bar_ts, cur, cash_w)
                return rep
            targets = {s: D(v) for s, v in act["targets"].items()}
            deadline = float(act.get("expires_at") or 0) or None
            info.update(action=act.get("kind"), why="buy retry of this bar's %s decision" % act.get("kind"))
        else:
            if self._fills_handled != fills_seen:
                # a resting order filled this hour (W1): its target sell rests NOW, not after the review
                # call the same fill triggers (news + decision, up to ~30 min)
                self._early_targets(rep, now, units, equity)
            snap = self._brain_snapshot(units, cash, now)
            decision, kind = self._brain_choose(now, cur, cur_dec, cash_w, plan.equity, snap, info)
            if decision is not None and info.get("trigger") is not None:
                # A Kimi call (news research + decision) can take up to ~30 min, and ladder bids or target
                # sells may have filled on the exchange meanwhile. The drift guard and the plan use the
                # holdings read NOW, not the ones the decision was computed against.
                fresh = self._reread_book(rep, now, syms, prices)
                if fresh is None:
                    info.update(action="none", why="balance read after the Kimi call failed: nothing executed this "
                                                   "hour", decision_status="not_executed")
                    decision = None
                else:
                    units, cash, cur_dec, cur_after, plan_after = fresh
                    drift = self._drift(decision, cur_after)
                    if drift > DECISION_DRIFT_LIMIT:
                        info.update(action="stale", decision_status="stale",
                                    why="portfolio weights moved %.3f during the Kimi call" % drift)
                        log.warning("the %s decision is NOT executed: the portfolio weights moved by up to %.3f WHILE "
                                    "it was being made (limit %.2f; e.g. a ladder bid or a target sell filled). Its "
                                    "ladder scales and exits are applied; the next decision sees the new weights.",
                                    kind, drift, DECISION_DRIFT_LIMIT)
                        decision = None
                    cur = cur_after
                    cash_w = float(plan_after.cash / plan_after.equity) if plan_after.equity > 0 else 0.0
                    rep.update(current=plan_after.current, cash=plan_after.cash)
            targets = None
            if decision is not None:
                try:
                    targets = self._decision_targets(decision, cur_dec, keep_extras=(kind == "cash_sweep"))
                except RunnerError as e:
                    log.error("decision not executable: %s", e)
                    rep["errors"].append(str(e))
                    info.update(action="none", why=str(e))
                if targets is not None and kind == "kimi":
                    targets = self._pump_clamp(targets, cur_dec, now, info, decision)
                    # the session / spread at the time of EXECUTION (after the Kimi call), not the cycle start
                    targets = self._session_clamp(targets, cur_dec, self.clock(), info, decision)
            if targets is None:
                rep["status"] = "dry_run" if self.dry_run else "ok"
                if not self.dry_run:
                    # the bar is processed (idempotency): a restart does not ask again for this bar
                    self._save_state(last_bar_ts=bar_ts, retry_buys=None, last_cycle={
                        "bar_ts": bar_ts, "time": now, "status": "ok", "equity": plan.equity, "orders": len(rep["fills"]),
                        "brain_action": info.get("action"), "skipped": [], "errors": rep["errors"]})
                self._maintain(rep, now, equity, syms)
                self._brain_audit(rep, bar_ts, cur, cash_w)
                return rep
            deadline = self._expires_at(decision)
            state_fields["brain_active"] = {"bar_ts": bar_ts, "kind": kind, "decided_at": decision.decided_at,
                                            "expires_at": deadline, "targets": targets}
            if kind == "kimi":
                state_fields["brain_executed"] = decision.decided_at   # a Kimi decision is executed once
        plan = plan_orders(targets, units, prices, cash, self.threshold, self.fee_rate, None,
                           dust_irt=self.risk.min_order_irt)
        kind = info.get("action")
        if kind == "kimi" and not buys_only and decision is not None:
            lost = self._unexecutable_legs(decision, plan, targets, skip=set(info.get("pump_guard") or ()))
            if lost:
                info["unexecutable"] = lost
                log.warning("%d leg(s) of this Kimi decision are no longer executable after the portfolio moved "
                            "since it was computed: %s. The rest of the decision IS executed.", len(lost),
                            "; ".join(lost))
        # USDT <-> coin legs through COIN_USDT when that is cheaper (sells first stays the rule)
        plan = self._route_plan(plan, prices, rep, last_close_1h, plan.effective_equity)
        reason = "allocation" if kind == "kimi" else (kind or "allocation")
        for o in plan.orders:
            if not o.reason:
                o.reason = reason
        rep.update(targets=targets, plan=[o.describe() for o in plan.orders], plan_skipped=plan.skipped)
        if kind == "derisk" and plan.orders:
            log.error("DERISK: %s. Check Kimi: run_bot.py kimi-check --kimi-config ...", info.get("why"))
        elif kind == "endgame" and plan.orders:
            log.error("ENDGAME DEFAULT: %s", info.get("why"))
        elif kind == "cash_sweep" and plan.orders:
            log.warning("CASH SWEEP: %s", info.get("why"))
        elif kind in ("derisk", "cash_sweep", "endgame"):
            log.info("fallback %s: nothing to trade (changes below the rebalance threshold)", kind)
        if kind in ("derisk", "cash_sweep", "endgame") and plan.orders and not buys_only:
            self._event("fallback", key="%s:%s" % (kind, getattr(decision, "decided_at", now)), reason=kind,
                        why=info.get("why"), targets={s: float(w) for s, w in targets.items() if w},
                        plan=[o.describe() for o in plan.orders])
            if kind == "endgame":
                self._event("endgame", key="default_to_usdt:%s" % getattr(decision, "decided_at", now),
                            step="default_to_usdt", why=info.get("why"))
        self._log_plan(bar_ts, plan, prices, dd)
        # no order of this decision is sent after it expired (checked before every order)
        self._exec_deadline = deadline
        try:
            out = self._trade(rep, plan, targets, prices, last_close_1h, bar_ts, now, buys_only, **state_fields)
        finally:
            self._exec_deadline = None
        self._maintain(out, now, equity, syms)
        self._brain_audit(out, bar_ts, cur, cash_w)
        return out

    def _brain_audit(self, rep, bar_ts, cur, cash_w):
        """One line per processed bar in state_dir/kimi_runner.jsonl: what Kimi / the fallback
        decided, and what was planned and executed."""
        info = rep.get("brain") or {}
        rec = {
            "time": round(float(rep.get("time") or self.clock()), 3), "bar_ts": bar_ts, "mode": self.mode,
            "status": rep.get("status"), "equity_irt": rep.get("equity"), "irt_cash_w": round(cash_w, 6),
            "current": {s: round(w, 6) for s, w in cur.items() if w > 0.0005},
            "action": info.get("action"), "why": info.get("why"), "trigger": info.get("trigger"),
            "pacing_block": info.get("pacing_block"), "min_trade_weight": info.get("min_trade_weight"),
            "decided": info.get("decided"), "decision": info.get("decision"), "fallback": info.get("fallback"),
            "brain_error": info.get("brain_error"), "news": info.get("news"),
            "unexecutable": info.get("unexecutable"), "stale_prices": rep.get("stale_prices"),
            "frozen": info.get("frozen"),
            "targets": {s: w for s, w in (rep.get("targets") or {}).items() if w},
            "plan": rep.get("plan"),
            "fills": [{k: f.get(k) for k in ("side", "symbol", "base", "quote", "fee", "fee_asset", "order_id",
                                             "partial", "quote_asset", "route", "reason")}
                      for f in rep.get("fills") or []],
            "skipped": [list(x) for x in rep.get("skipped") or []], "errors": rep.get("errors")}
        for k in ("trigger_kind", "mode", "events", "notifications", "news_forced", "pump_guard", "plans_broken",
                  "adjustments"):
            if info.get(k):
                rec[k] = info[k]
        for k in ("exits", "limit_fills", "resting", "routing", "ladder_state"):
            if rep.get(k):
                rec[k] = rep[k]
        if self.exits_on and self._has_positions():
            keys = ("amount", "entry_px_usdt", "stop_px_usdt", "target_px_usdt", "max_hold_until", "source", "plan")
            if self._positions:
                rec["positions"] = {s: {k: p.get(k) for k in keys} for s, p in sorted(self._positions.items())}
            if self._ladder_pos:
                rec["ladder_positions"] = {s: {k: p.get(k) for k in keys} for s, p in sorted(self._ladder_pos.items())}
        self._brain_log(rec)

    # ---- crash ladder, code exits, routing, events (brain mode; module docstring)
    def _endgame(self, now):
        fn = getattr(self.brain, "endgame_flags", None)
        if not callable(fn):
            return {}
        try:
            return dict(fn(now) or {})
        except Exception as e:  # noqa: BLE001 - no endgame information: nothing is switched off by it
            log.warning("endgame flags unavailable: %s", e)
            return {}

    def _event(self, kind, key=None, **fields):
        """One line in state_dir/bot_events.jsonl (brain mode, never in a dry run). `key` makes the
        event's id stable, so the same event written again after a restart is recognisable."""
        if self.dry_run or self.brain is None:
            return
        rec = {"t": round(float(self.clock()), 3), "kind": kind, "mode": self.mode}
        rec.update({k: v for k, v in fields.items() if v is not None})
        ident = key if key is not None else json.dumps(rec, sort_keys=True, default=str)
        rec["id"] = hashlib.sha1(("%s|%s|%s" % (self.mode, kind, ident)).encode("utf-8")).hexdigest()[:16]
        try:
            line = self._redact(json.dumps(rec, sort_keys=True, default=str, ensure_ascii=False))
            with open(os.path.join(self.state_dir, EVENTS_LOG), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 - audit only; never abort trading
            log.warning("cannot append to %s: %s", EVENTS_LOG, e)

    def _fill_event(self, fill, reason=None, route=None):
        try:
            sym = fill.get("symbol")
            quote_asset = parse_symbol(sym)[1] if sym else None
            key = "%s:%s" % (fill.get("identifier") or fill.get("order_id"), fill.get("base"))
            self._event("fill", key=key, reason=reason or fill.get("reason"), route=route or fill.get("route"),
                        symbol=sym, side=fill.get("side"), base=fill.get("base"), quote=fill.get("quote"),
                        quote_asset=quote_asset, avg_price=fill.get("avg_price"), fee=fill.get("fee"),
                        fee_asset=fill.get("fee_asset"), order_id=fill.get("order_id"),
                        identifier=fill.get("identifier"), partial=fill.get("partial"))
        except Exception as e:  # noqa: BLE001
            log.warning("fill event not written: %s", e)

    def _brain_notifications(self, now, info):
        """W4 below the risk-reduce coin weight and W5 (USDT_IRT +-4%): notifications only."""
        try:
            notes = list(self.brain.pop_notifications() or [])
        except Exception as e:  # noqa: BLE001
            log.warning("brain notifications unavailable: %s", e)
            return
        for n in notes:
            if not isinstance(n, dict):
                continue
            text = str(n.get("text") or "")[:300]
            log.warning("NOTIFY (%s): %s", n.get("kind"), text)
            info.setdefault("notifications", []).append({"kind": n.get("kind"), "text": text})
            self._event("notify", key="%s:%s" % (n.get("kind"), n.get("t")), notify_kind=n.get("kind"), text=text)

    def _decision_event(self, d, info):
        try:
            ex = getattr(d, "exits", None) or {}
            self._event(
                "decision", key="decision:%s" % getattr(d, "decided_at", None),
                decided_at=getattr(d, "decided_at", None), valid=bool(getattr(d, "valid", False)),
                hold=bool(getattr(d, "hold", False)), mode=getattr(d, "mode", None) or None,
                trigger_kind=getattr(d, "trigger_kind", None) or None, trigger=info.get("trigger"),
                confidence=getattr(d, "confidence", None),
                targets={s: w for s, w in (getattr(d, "targets", None) or {}).items() if w},
                cash_irt=getattr(d, "cash_irt", None), ladder=dict(getattr(d, "ladder", None) or {}) or None,
                exits={s: {k: e.get(k) for k in ("stop_pct", "target_price", "max_hold_until")}
                       for s, e in ex.items() if isinstance(e, dict)} or None,
                report_fa=(getattr(d, "report_fa", "") or "")[:800] or None,
                events=info.get("events"), error_kind=getattr(d, "error_kind", "") or None,
                error=(getattr(d, "error", "") or "")[:300] or None)
            if getattr(d, "mode", None) == "final" and getattr(d, "valid", False):
                # once per competition: the first valid FINAL decision (a later final-mode slot or wake-up
                # is not "the final decision" again), keyed on the endgame's final_at
                fa = (getattr(d, "endgame", None) or {}).get("final_at") or self._endgame(d.decided_at).get("final_at")
                if self._ladder_st.get("final_event_for") != fa:
                    self._event("endgame", key="final_decision:%s" % fa, step="final_decision",
                                decided_at=d.decided_at, targets={s: w for s, w in (d.targets or {}).items() if w})
                    if not self.dry_run:
                        self._ladder_st["final_event_for"] = fa
                        self._save_bot_state()
        except Exception as e:  # noqa: BLE001
            log.warning("decision event not written: %s", e)

    def _apply_decision_extras(self, d, now, info):
        """Decision.ladder -> the per-coin ladder scales; Decision.exits -> the exits of the positions
        it keeps (and of the ones its execution opens). Once per decision."""
        at = float(getattr(d, "decided_at", 0) or 0)
        if self._ladder_st.get("applied_at") is not None and abs(float(self._ladder_st["applied_at"]) - at) < 1e-6:
            return
        applied = []
        lad = getattr(d, "ladder", None)
        if isinstance(lad, dict) and lad:
            sc = {}
            for c, v in lad.items():
                try:
                    x = float(v)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(x):
                    sc[str(c).upper()] = min(1.0, max(0.0, x))
            if sc:
                self._ladder_st["scales"] = sc
                applied.append("ladder %s" % ", ".join("%s %g" % kv for kv in sorted(sc.items())))
        ex = getattr(d, "exits", None)
        if isinstance(ex, dict):
            specs = {}
            for sym, spec in ex.items():
                if not isinstance(spec, dict):
                    continue
                sym = str(sym).upper()
                specs[sym] = dict({k: spec.get(k) for k in EXIT_SPEC_KEYS}, _at=at)
                # the coin's allocation position; a coin with only a crash-ladder position: that one (it
                # is the position the brain showed Kimi for the coin)
                pos = self._positions.get(sym)
                if pos is None:
                    pos = self._ladder_pos.get(sym)
                if pos is not None and self.exits_on:
                    self._set_exits(pos, spec)
                    applied.append("exits %s: stop %s target %s" % (
                        sym, _fmt_px(pos.get("stop_px_usdt")), _fmt_px(pos.get("target_px_usdt"))))
            self._exit_specs = specs
        pl = getattr(d, "plans", None)
        if isinstance(pl, dict):
            from .analysis import clean_plan
            from .brain import plan_over
            specs = {}
            tg = getattr(d, "targets", None)
            tg = tg if isinstance(tg, dict) else {}
            ag = getattr(d, "computed_against", None)
            ag = ag if isinstance(ag, dict) else {}
            for sym, p in pl.items():
                c = clean_plan(p)
                if c is None:
                    continue
                sym = str(sym).upper()
                for k in ("set_at", "broken_at", "broken_close_usdt"):
                    c.pop(k, None)
                c["set_at"] = at
                specs[sym] = c
                # a position this decision keeps that has no plan yet (e.g. a coin held before plans existed),
                # or whose plan is broken / past its horizon (the model restated its thesis without a buy),
                # takes it now; a coin it buys gets it when the buy is booked (_attach_plan). A decision that
                # RAISES a coin held only as a crash-ladder lot opens its allocation position: that plan is
                # the new position's, never the ladder lot's.
                try:
                    raises = sym in ag and float(tg.get(sym) or 0.0) > float(ag.get(sym) or 0.0) + 1e-9
                except (TypeError, ValueError):
                    raises = False
                pos = self._positions.get(sym)
                if pos is None and not raises:
                    pos = self._ladder_pos.get(sym)
                if pos is None or not self.exits_on:
                    continue
                old = pos.get("plan") if isinstance(pos.get("plan"), dict) else None
                over = plan_over(old, now) if old is not None else None
                if old is None or over:
                    pos["plan"] = dict(c)
                    applied.append("plan %s%s: %s, %d h, invalid %s" % (
                        sym, " restated (the old one %s)" % over if over else "", c["setup"], c["horizon_hours"],
                        _fmt_px(c["invalidation_usdt"])))
            # like the exit specs: the plans of the LATEST decision are the pending ones
            self._plan_specs = specs
        self._ladder_st["applied_at"] = at
        if applied:
            info["applied"] = applied
            log.info("Kimi decision of %s applied: %s", fmt_ts(at), "; ".join(applied))
        self._save_bot_state()

    def _ladder_scales(self, now):
        """{coin: scale 0..1} in force: the last applied Kimi decision's, else the brain's
        ladder_in_force (restart); every coin 0 from the endgame cut-off."""
        if self._endgame(now).get("no_new_entries"):
            return {c: 0.0 for c in self.ladder_coins}
        sc = self._ladder_st.get("scales")
        if not isinstance(sc, dict):
            try:
                sc = dict(self.brain.ladder_in_force(now) or {})
            except Exception as e:  # noqa: BLE001
                log.warning("ladder scales of the brain unavailable (%s): the ladder is off this hour", e)
                return {c: 0.0 for c in self.ladder_coins}
        out = {}
        for c in self.ladder_coins:
            try:
                out[c] = min(1.0, max(0.0, float(sc.get(c, 1.0))))
            except (TypeError, ValueError):
                out[c] = 0.0
        return out

    def _compute_refs(self):
        """{coin: {"dd48", "hi48", "close"}} in USDT terms (COIN_IRT close / USDT_IRT close of the same
        hour, as in the research and the brain's context) from the runner's own closed hourly bars."""
        from .analysis import dd48_usdt
        u = self._cache.get(SAFE) or []
        if not u:
            return {}
        uts, uc = [b.ts for b in u], [b.close for b in u]
        coins = set(self.ladder_coins) | {parse_symbol(s)[0] for s in set(self._positions) | set(self._ladder_pos)}
        out = {}
        for c in sorted(coins):
            sym = "%s_%s" % (c, IRT)
            bars = self._cache.get(sym)
            if not bars:
                continue
            try:
                r = dd48_usdt(bars, uts, uc, sym, hours=int(self.cfg["ladder"]["lookback_hours"]))
            except Exception as e:  # noqa: BLE001
                log.warning("48 h reference of %s unavailable: %s", c, e)
                continue
            if r:
                out[c] = {"dd48": float(r[0]), "hi48": float(r[1]), "close": float(r[2])}
        return out

    def _usdt_rate(self):
        p = self._cycle_prices.get(SAFE) if self._cycle_prices else None
        if p is None:
            p = self._last_prices.get(SAFE)
        return D(p) if p is not None and D(p) > 0 else None

    def _usdt_market(self, coin):
        """COIN_USDT Market when it exists and trades, else None."""
        try:
            m = self.broker.markets.get("%s_%s" % (coin, USDT))
        except Exception:  # noqa: BLE001 - not listed
            return None
        return m if getattr(m, "is_trading", False) else None

    def _ref_close(self, symbol, last_close_1h):
        """The last close of `symbol` in its own quote: the 1h close for a managed IRT market, the
        COIN_IRT / USDT_IRT cross of the same hour for COIN_USDT (the runner has no COIN_USDT candles)."""
        lc = last_close_1h or {}
        if symbol in lc and lc[symbol] is not None:
            return D(repr(lc[symbol]))
        base, quote = parse_symbol(symbol)
        if quote == USDT:
            a, u = lc.get("%s_%s" % (base, IRT)), lc.get(SAFE)
            if a is not None and u:
                return D(repr(a)) / D(repr(u))
        return None

    def _has_limits(self, tag=None):
        try:
            return bool(self.broker.limit_orders(tag=tag, active_only=True))
        except FATAL_ERRORS:
            raise
        except Exception:  # noqa: BLE001
            return False

    def _record_limit(self, kind="limit"):
        try:
            self.risk.record_order(kind)
        except Exception as e:  # noqa: BLE001 - never lose an order over the counter
            log.error("cannot record the %s in the risk state: %s", kind, e)

    # ---- resting orders: sync, fills, cancels
    def _resting_sync(self, rep, now):
        """Every cycle: bring the bot's resting orders in step with the exchange, then handle their
        new fills (ladder level disarmed, events, sleeve)."""
        if self.brain is None:
            return
        try:
            r = self.broker.sync_limits(read_only=self.dry_run) or {}
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - retried next cycle
            rep["errors"].append("resting orders: sync failed: %s" % e)
            log.warning("resting orders: sync failed (%s); retried next cycle", e)
            return
        for e in r.get("errors") or []:
            log.warning("resting orders: %s", e)
        if r.get("unresolved"):
            rep["resting_unresolved"] = [v.get("identifier") for v in r["unresolved"]]
        self._process_limit_fills(rep)
        self._sync_sleeve(rep)          # limit fills into the sleeve (cumulative, exactly once)

    def _process_limit_fills(self, rep):
        """Handle every unacknowledged limit fill, then ack them (at-least-once, idempotent)."""
        try:
            evs = list(self.broker.limit_fill_events() or [])
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("limit fill events unavailable: %s", e)
            return
        if not evs:
            return
        self._fills_handled += len(evs)
        changed = False
        for ev in evs:
            try:
                meta = ev.get("meta") if isinstance(ev.get("meta"), dict) else {}
                tag = ev.get("tag")
                sym = ev.get("symbol")
                reason = tag if tag in (LADDER_TAG, TARGET_TAG) else "limit"
                if tag == LADDER_TAG and ev.get("side") == "buy":
                    changed |= self._ladder_filled(meta, ev)
                rec = {"symbol": sym, "side": ev.get("side"), "base": ev.get("base"), "quote": ev.get("quote"),
                       "avg_price": ev.get("avg_price"), "reason": reason, "identifier": ev.get("identifier"),
                       "state": ev.get("state"), "level_pct": meta.get("level_pct")}
                rep.setdefault("limit_fills", []).append(rec)
                log.warning("RESTING ORDER FILLED (%s): %s %s %s @ %s (%s)", reason, ev.get("side"),
                            fmt_amount(D(ev.get("base") or 0)), sym, ev.get("avg_price") or ev.get("price"),
                            ev.get("state"))
                self._event("fill", key="%s:%s" % (ev.get("identifier"), ev.get("cum_base")), reason=reason,
                            route="resting", symbol=sym, side=ev.get("side"), base=ev.get("base"),
                            quote=ev.get("quote"), quote_asset=parse_symbol(sym)[1] if sym else None,
                            avg_price=ev.get("avg_price"), fee=ev.get("fee"), fee_asset=ev.get("fee_asset"),
                            order_id=ev.get("order_id"), identifier=ev.get("identifier"), state=ev.get("state"),
                            level_pct=meta.get("level_pct"), coin=meta.get("coin"))
                self._note_cost(dict(ev, symbol=sym), self.clock())
                if tag == TARGET_TAG and ev.get("side") == "sell" and sym:
                    tsym = str(meta.get("symbol") or "%s_%s" % (parse_symbol(sym)[0], IRT)).upper()
                    tpos = self._book_of(self._lot_of(ev)).get(tsym) or self._positions.get(tsym) or \
                            self._ladder_pos.get(tsym) or {}
                    self._note_exit(tsym, "target", ev.get("avg_price") or ev.get("price"), tpos,
                                    ident=ev.get("identifier"))
            except Exception as e:  # noqa: BLE001 - one event never blocks the others
                log.exception("limit fill event not handled: %s", e)
        if changed:
            self._save_bot_state()
        if not self.dry_run:
            try:
                self.broker.ack_limit_fills(evs)
            except FATAL_ERRORS:
                raise
            except Exception as e:  # noqa: BLE001 - re-delivered next cycle, handled idempotently
                log.warning("limit fills not acknowledged (%s); they are handled again next cycle", e)

    def _ladder_filled(self, meta, ev):
        """A ladder bid filled (fully or partly): its level is disarmed until the coin recovers above
        -rearm_pct; the first fill of an order sets filled_at (the brain's W1 review). A later fill of
        the SAME order is
        * a new fill when the level was re-armed meanwhile (the rest of a partly filled bid kept resting
          and a new crash reached it): the level is disarmed again (no second bid at that level in this
          crash) and filled_at is set again (W1);
        * a new W1 review when it grew the order's filled amount by at least LADDER_REFILL_REVIEW of its
          size and at least doubled it since the last review mark (the bulk of a bid whose first fill
          was a small wick is reviewed too)."""
        coin = str(meta.get("coin") or parse_symbol(ev.get("symbol"))[0]).upper()
        lv = meta.get("level_pct")
        if lv is None:
            return False
        ls = self._level(coin, lv)
        px = ev.get("avg_price") or ev.get("price")
        try:
            px = float(px) if px is not None else None
        except (TypeError, ValueError):
            px = None
        cum = D(ev.get("cum_base") or 0)
        now = round(float(self.clock()), 3)
        if ls.get("fill_ident") == ev.get("identifier"):
            prev = D(ls.get("fill_cum") or 0)
            if cum <= prev:
                return False
            ls["fill_cum"] = format(cum, "f")
            if ls.get("armed", True) is not False:
                ls.update(armed=False, filled_at=now, fill_px_usdt=px, review_cum=format(cum, "f"),
                          hi48_usdt=meta.get("hi48_usdt"))
                log.warning("LADDER FILL: the rest of %s's %s%% bid filled at %s USDT after the level had re-armed: "
                            "level disarmed again until the coin is back above -%g%%", coin, abs(float(lv)), px,
                            float(self.cfg["ladder"]["rearm_pct"]))
                return True
            mark = D(ls["review_cum"]) if ls.get("review_cum") is not None else prev
            size = self._limit_size(ev.get("identifier"))
            if size and cum - mark >= size * D(repr(LADDER_REFILL_REVIEW)) and cum >= mark * 2:
                ls.update(filled_at=now, fill_px_usdt=px, review_cum=format(cum, "f"))
                log.warning("LADDER FILL: %s's %s%% bid has now filled %s of %s (%s more since the last review): "
                            "Kimi reviews it again", coin, abs(float(lv)), fmt_amount(cum), fmt_amount(size),
                            fmt_amount(cum - mark))
            return True
        ls.update(armed=False, filled_at=now, fill_px_usdt=px, fill_ident=ev.get("identifier"),
                  fill_cum=format(cum, "f"), review_cum=format(cum, "f"), hi48_usdt=meta.get("hi48_usdt"))
        log.warning("LADDER FILL: %s bid at %s%% below the 48 h high filled at %s USDT: level disarmed until the coin "
                    "is back above -%g%%", coin, abs(float(lv)), px, float(self.cfg["ladder"]["rearm_pct"]))
        return True

    def _limit_size(self, ident):
        """The base amount of the bot's limit order `ident` (None when unknown)."""
        try:
            for v in self.broker.limit_orders(tag=LADDER_TAG):
                if v.get("identifier") == ident:
                    return D(v.get("base_amount") or 0) or None
        except FATAL_ERRORS:
            raise
        except Exception:  # noqa: BLE001 - informational (a second review is skipped)
            return None
        return None

    def _endgame_ladder_off(self, rep, now):
        """From the endgame cut-off (no new coin entries) the ladder bids are cancelled at the START of
        the check - before the exits, the news research and a Kimi call that can take half an hour, and
        also in a check that ends early (orders of unknown outcome, stale prices) - not only by the
        maintenance at its end."""
        if not self.ladder_on:
            return
        try:
            eg = self._endgame(now)
            if not eg.get("no_new_entries"):
                return
            if not self._ladder_st.get("endgame_off_at") and not self.dry_run:
                self._ladder_st["endgame_off_at"] = round(float(now), 3)
                log.warning("ENDGAME: no new coin entries from now on - every crash-ladder bid is cancelled")
                self._event("endgame", key="ladder_off", step="ladder_off",
                            no_new_entries_at=eg.get("no_new_entries_at"))
                self._save_bot_state()
            if self._has_limits(LADDER_TAG):
                self._cancel_limits(rep, tag=LADDER_TAG, why="endgame: no new coin entries")
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - the maintenance at the end of the check tries again
            rep.setdefault("errors", []).append("endgame: cancelling the ladder bids failed: %s" % e)
            log.warning("endgame: cancelling the ladder bids failed (%s); retried at the end of the check", e)

    def _cancel_views(self, rep, views, why):
        """Cancel these bot limit orders; returns the views after the cancels (a cancel can find a fill)."""
        out = []
        for v in views:
            ident = v.get("identifier")
            if self.dry_run:
                rep.setdefault("resting", {}).setdefault("would_cancel", []).append(
                    "%s %s @ %s (%s)" % (v.get("side"), v.get("symbol"), v.get("price"), why))
                continue
            try:
                after = self.broker.cancel(ident)
            except NotBotOrder:
                continue
            except FATAL_ERRORS:
                raise
            except Exception as e:  # noqa: BLE001 - re-tried by the next maintenance / sync
                rep["errors"].append("cancel %s: %s" % (ident, e))
                log.warning("cancel of resting order %s failed: %s", ident, e)
                continue
            self._record_limit("cancel")
            out.append(after or v)
            meta = v.get("meta") if isinstance(v.get("meta"), dict) else {}
            rep.setdefault("resting", {}).setdefault("cancelled", []).append(
                "%s %s %s @ %s: %s -> %s" % (v.get("tag"), v.get("side"), v.get("symbol"), v.get("price"), why,
                                             (after or {}).get("state")))
            log.info("resting %s order %s (%s %s @ %s) cancelled: %s -> %s", v.get("tag"), ident, v.get("side"),
                     v.get("symbol"), v.get("price"), why, (after or {}).get("state"))
            self._event("order_cancel", key="cancel:%s" % ident, tag=v.get("tag"), symbol=v.get("symbol"),
                        side=v.get("side"), price=v.get("price"), base_amount=v.get("base_amount"), why=why,
                        state=(after or {}).get("state"), filled_base=(after or {}).get("filled_base"),
                        identifier=ident, level_pct=meta.get("level_pct"), coin=meta.get("coin"))
        return out

    @staticmethod
    def _lot_of(view):
        """The position a target sell serves (meta "lot"; an order of an older version: the allocation's)."""
        meta = view.get("meta") if isinstance(view.get("meta"), dict) else {}
        return meta.get("lot") if meta.get("lot") in (LOT_MAIN, LOT_LADDER) else LOT_MAIN

    def _cancel_limits(self, rep, tag=None, coin=None, why="", lot=None):
        """Cancel the bot's active limit orders (one tag / one coin / one position's, or all). Foreign
        orders are never touched (the broker refuses them). Returns True if any order was cancelled."""
        if self.brain is None:
            return False
        try:
            views = list(self.broker.limit_orders(tag=tag, active_only=True))
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("resting orders unavailable (%s): nothing cancelled", e)
            return False
        if coin is not None:
            views = [v for v in views if parse_symbol(v["symbol"])[0] == coin]
        if lot is not None:
            views = [v for v in views if self._lot_of(v) == lot]
        if not views:
            return False
        done = self._cancel_views(rep, views, why)
        if done:
            self._process_limit_fills(rep)     # a cancel that raced a fill
        return bool(done)

    def _halt_cleanup(self, rep):
        """A halted bot must not buy through resting bids: cancel them (target sells may stay)."""
        if self.dry_run or self.brain is None or not self._has_limits(LADDER_TAG):
            return
        log.error("trading is halted: cancelling the crash-ladder bids")
        self._cancel_limits(rep, tag=LADDER_TAG, why="trading halted")

    def _kill_cleanup(self, rep):
        """The STOP kill switch (also the Telegram /stop relay): the bot stops, and while it is stopped
        nothing enforces the code exits, so it must not keep BUYING through its resting crash-ladder
        bids either. They are cancelled (cancels only remove exposure; the target sells stay - they
        only ever sell a position at a profit). Never raises except for the fatal auth errors; a
        failed cancel is journaled (cancel_requested) and re-sent by the next sync of any run."""
        if self.dry_run or self.brain is None:
            return
        rep.setdefault("errors", [])
        try:
            if not self._has_limits(LADDER_TAG):
                return
            log.error("kill switch: cancelling the crash-ladder bids (they would buy while nothing guards the "
                      "positions); the resting target sells stay")
            self._cancel_limits(rep, tag=LADDER_TAG, why="kill switch (STOP)")
            left = [v for v in self.broker.limit_orders(tag=LADDER_TAG, active_only=True)
                    if not v.get("final", False)]
            if left:
                log.error("kill switch: %d crash-ladder bid(s) could not be confirmed as cancelled yet (%s). Check the "
                          "Bitpin app, or run: bitpin-bot cancel-resting --tag %s", len(left),
                          ", ".join("%s @ %s" % (v.get("symbol"), v.get("price")) for v in left[:8]), LADDER_TAG)
            self._save_bot_state()
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - the bot stops anyway; the journal keeps the cancel requests
            rep["errors"].append("kill switch: cancelling the ladder bids failed: %s" % e)
            log.exception("kill switch: cancelling the ladder bids failed (%s): check the Bitpin app, or run "
                          "bitpin-bot cancel-resting --tag %s", e, LADDER_TAG)

    def _free_resting_for(self, plan, rep, buys_only=False):
        """The allocation always wins over the bot's own resting orders: cancel the target sells of
        every coin the plan sells, and the ladder bids when the plan spends more USDT than is free
        (the maintenance at the end of the cycle re-sizes the ladder to the USDT that is left)."""
        if not self._has_limits():
            return
        if not buys_only:
            for coin in sorted({parse_symbol(o.symbol)[0] for o in plan.sells} - {USDT}):
                self._cancel_limits(rep, tag=TARGET_TAG, coin=coin, why="the allocation sells %s" % coin)
        need = ZERO
        for o in plan.orders:
            if o.side == "sell" and parse_symbol(o.symbol)[0] == USDT and not buys_only:
                need += D(o.amount)
            elif o.side == "buy" and o.quote == USDT:
                need += D(o.amount)
        if need <= 0 or not self._has_limits(LADDER_TAG):
            return
        try:
            self.broker.refresh()
            free = D(self.broker.available().get(USDT, ZERO))
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("USDT balance unavailable (%s): the ladder bids are cancelled to be safe", e)
            free = ZERO
        if need > free:
            self._cancel_limits(rep, tag=LADDER_TAG, why="the allocation needs %s USDT (free %s)" % (
                fmt_amount(need), fmt_amount(free)))

    # ---- positions and code exits
    # A coin has at most two positions: the allocation's (LOT_MAIN: Kimi buys, coins already held) and
    # the crash ladder's (LOT_LADDER: its ladder fills). Each has its own entry, stop, target and max
    # hold, so a crash buy never changes the exits of the allocation it sits next to, and the stop of
    # one never sells the other.
    def _book_of(self, lot):
        return self._ladder_pos if lot == LOT_LADDER else self._positions

    def _lots(self, sym=None):
        """[(symbol, lot, position)] of every guarded position; per symbol the allocation's first."""
        out = [(s, LOT_MAIN, p) for s, p in self._positions.items() if sym is None or s == sym]
        out += [(s, LOT_LADDER, p) for s, p in self._ladder_pos.items() if sym is None or s == sym]
        return sorted(out, key=lambda x: (x[0], x[1] != LOT_MAIN))

    def _has_positions(self):
        return bool(self._positions or self._ladder_pos)

    def _positions_view(self):
        """The brain's `positions` (module docstring of bitpin/brain.py), or None when exits are off.
        Per coin the allocation position's fields - or the ladder position's when the coin has no
        allocation position (that is the one a Kimi exit spec for the coin then applies to); a coin
        with both also carries "ladder_lot": the crash-ladder position's own entry / stop / target /
        max hold, which the code keeps."""
        if not self.exits_on:
            return None
        out = {}
        for s in sorted(set(self._positions) | set(self._ladder_pos)):
            main, lad = self._positions.get(s), self._ladder_pos.get(s)
            top = main if main is not None else lad
            v = {k: top.get(k) for k in POSITION_VIEW_KEYS}
            if main is not None and lad is not None:
                v["ladder_lot"] = {k: lad.get(k) for k in LADDER_LOT_VIEW_KEYS}
            out[s] = v
        return out

    def _set_exits(self, pos, spec):
        """Concrete stop / target / max hold / wake levels of a position from an exit spec (brain's
        Decision.exits or default_exit_spec) and the position's average entry."""
        from .brain import concrete_exit_prices
        try:
            c = concrete_exit_prices(spec, pos.get("entry_px_usdt"), pos.get("high48_usdt"))
        except Exception as e:  # noqa: BLE001 - the old exits stay in force
            log.warning("exits of a position not set (%s)", e)
            return False
        pos["exit_spec"] = {k: spec.get(k) for k in EXIT_SPEC_KEYS}
        until = c.get("max_hold_until")
        pos.update(stop_pct=float(spec.get("stop_pct") or 0.0) or None, stop_px_usdt=c.get("stop_px_usdt"),
                   target_px_usdt=c.get("target_px_usdt"),
                   max_hold_until=until if until is not None else pos.get("max_hold_until"),
                   wake_up_pct=c.get("wake_up_pct"), wake_levels=list(c.get("wake_levels") or []))
        return True

    def _default_exits(self, sym, pos, now):
        """Exit spec of a NEW position: the spec of the Kimi decision that opened it (a Kimi buy within
        EXIT_SPEC_TTL of the decision), else the v3 defaults (NO stop, max hold 720 h from entry capped
        by the endgame, wake-up +-held_move_pct; a target of half the 48 h drop only for a crash-ladder
        position - an allocation position has a target only when Kimi sets one)."""
        from .brain import resolve_exits
        req = {}
        spec = self._exit_specs.get(sym)
        if pos.get("source") == "kimi" and isinstance(spec, dict) and now - float(spec.get("_at") or 0) < EXIT_SPEC_TTL:
            req = {k: spec[k] for k in ("stop_pct", "target_price", "target_rule", "max_hold_hours", "wake_up_pct",
                                        "wake_levels") if spec.get(k) is not None}
            if spec.get("target_rule") == "none" and spec.get("target_price") is None:
                req["target_rule"] = "none"
        try:
            held = float((getattr(self.brain, "cfg", None) or {}).get("held_move_pct") or 8.0)
        except (TypeError, ValueError):
            held = 8.0
        notes = []
        out = resolve_exits({sym: req} if req else {}, {sym: 1.0}, SAFE,
                            {sym: {"entry_ts": pos.get("entry_ts"), "source": pos.get("source")}},
                            {sym: pos.get("entry_px_usdt")}, now, self._endgame(now), "scheduled", held, notes)
        s = out[sym]
        s["source"] = "kimi" if req else "default"
        return s

    def _attach_plan(self, sym, pos, now):
        """The entry plan of the Kimi decision that bought this allocation position: the coin's pending
        Decision.plans spec (_apply_decision_extras) while it is younger than PLAN_SPEC_TTL and newer than
        the plan the position carries (an add with a new plan replaces it: the model restated its
        thesis). Kept in runner state with the position; the brain shows it back as your_plan."""
        spec = self._plan_specs.get(sym)
        if not isinstance(spec, dict):
            return False
        try:
            at = float(spec.get("set_at") or 0)
        except (TypeError, ValueError):
            return False
        if not -HOUR < float(now) - at < PLAN_SPEC_TTL:
            return False
        cur = pos.get("plan") if isinstance(pos.get("plan"), dict) else None
        try:
            if cur is not None and float(cur.get("set_at") or 0) >= at:
                return False
        except (TypeError, ValueError):
            pass
        pos["plan"] = dict(spec)
        log.info("POSITION %s: Kimi's plan attached (%s, %s h, invalid %s USDT)", sym, spec.get("setup"),
                 spec.get("horizon_hours"), _fmt_px(spec.get("invalidation_usdt")))
        return True

    def _pos_open(self, sym, amount, px_usdt, now, source, high48=None):
        lot = LOT_LADDER if source == LADDER_TAG else LOT_MAIN
        pos = {"entry_ts": round(float(now), 3), "entry_px_usdt": float(px_usdt), "amount": float(amount),
               "source": source, "opened_at": round(float(now), 3)}
        coin = parse_symbol(sym)[0]
        hi = high48 if high48 else (self._refs.get(coin) or {}).get("hi48")
        pos["high48_usdt"] = float(hi) if hi else None
        self._set_exits(pos, self._default_exits(sym, pos, now))
        if lot == LOT_MAIN and source == "kimi":
            self._attach_plan(sym, pos, now)
        self._book_of(lot)[sym] = pos
        log.warning("POSITION %s%s opened (%s): %s at %s USDT; stop %s, target %s, max hold until %s", sym,
                    " (crash ladder)" if lot == LOT_LADDER else "", source, _fmt_px(amount), _fmt_px(px_usdt),
                    _fmt_px(pos.get("stop_px_usdt")), _fmt_px(pos.get("target_px_usdt")),
                    fmt_ts(pos.get("max_hold_until")))
        self._event("position_open", key="%s@%s%s" % (sym, pos["entry_ts"], ":ladder" if lot == LOT_LADDER else ""),
                    symbol=sym, source=source, lot=lot, amount=pos["amount"], entry_px_usdt=pos["entry_px_usdt"],
                    stop_px_usdt=pos.get("stop_px_usdt"), target_px_usdt=pos.get("target_px_usdt"),
                    max_hold_until=pos.get("max_hold_until"))
        return pos

    def _pos_add(self, sym, amount, px_usdt, now, source, high48=None):
        """Book a buy into the position it belongs to (a ladder fill into the coin's crash-ladder
        position, anything else into its allocation position): the average entry of THAT position and
        its exits follow; the other position is never touched."""
        lot = LOT_LADDER if source == LADDER_TAG else LOT_MAIN
        pos = self._book_of(lot).get(sym)
        if pos is None or float(pos.get("amount") or 0) <= 0:
            return self._pos_open(sym, amount, px_usdt, now, source, high48)
        a0, e0 = float(pos.get("amount") or 0), float(pos.get("entry_px_usdt") or px_usdt)
        a1 = a0 + float(amount)
        pos["entry_px_usdt"] = (a0 * e0 + float(amount) * float(px_usdt)) / a1 if a1 > 0 else float(px_usdt)
        pos["amount"] = a1
        if lot == LOT_LADDER and high48 and not pos.get("high48_usdt"):
            pos["high48_usdt"] = float(high48)
        spec = pos.get("exit_spec") if isinstance(pos.get("exit_spec"), dict) else None
        self._set_exits(pos, spec or self._default_exits(sym, pos, now))
        if lot == LOT_MAIN and source == "kimi":
            self._attach_plan(sym, pos, now)
        return pos

    def _reduce_lots(self, sym, amount, first=None):
        """A sale of `amount` coins: taken from the position `first` (the one the sale served), then
        from the crash-ladder position, then from the allocation position."""
        left = float(amount)
        order = [first] if first in (LOT_MAIN, LOT_LADDER) else []
        order += [x for x in (LOT_LADDER, LOT_MAIN) if x not in order]
        for lot in order:
            pos = self._book_of(lot).get(sym)
            if pos is None or left <= 0:
                continue
            a = float(pos.get("amount") or 0)
            take = min(a, left)
            pos["amount"] = a - take
            left -= take

    def _note_exit(self, sym, why, px_usdt, pos, ident=None):
        """Remember a code exit (stop / target) for RECENT_EXITS_KEEP: Kimi is told about it in the
        context (recent_exits); after a STOP the coin may not be bought back within 24 h (anti-churn,
        brain)."""
        try:
            now = float(self.clock())
            if ident and any(r.get("ident") == ident for r in self._recent_exits):
                return
            entry = pos.get("entry_px_usdt") if isinstance(pos, dict) else None
            try:
                pnl = round((float(px_usdt) / float(entry) - 1.0) * 100.0, 2) if entry and px_usdt else None
            except (TypeError, ValueError, ZeroDivisionError):
                pnl = None
            rec = {"symbol": sym, "reason": why, "t": round(now, 3),
                   "px_usdt": float(px_usdt) if px_usdt is not None else None,
                   "entry_px_usdt": float(entry) if entry else None, "pnl_pct": pnl}
            if ident:
                rec["ident"] = str(ident)
            keep = [r for r in self._recent_exits if isinstance(r, dict)
                    and now - float(r.get("t") or 0) < RECENT_EXITS_KEEP]
            self._recent_exits = (keep + [rec])[-20:]
            self._save_bot_state()
        except Exception as e:  # noqa: BLE001 - informational for the brain; never breaks an exit
            log.warning("recent exit of %s not recorded: %s", sym, e)

    def _recent_exits_view(self, now):
        """The code exits of the last RECENT_EXITS_KEEP for the brain / context builder (oldest first)."""
        out = []
        for r in self._recent_exits:
            try:
                if isinstance(r, dict) and float(now) - float(r.get("t") or 0) < RECENT_EXITS_KEEP:
                    out.append({k: r.get(k) for k in ("symbol", "reason", "t", "px_usdt", "entry_px_usdt", "pnl_pct")})
            except (TypeError, ValueError):
                continue
        return out

    def _pos_close(self, sym, why, lot=None):
        for lt in ((lot,) if lot else (LOT_MAIN, LOT_LADDER)):
            pos = self._book_of(lt).pop(sym, None)
            if pos is None:
                continue
            log.warning("POSITION %s%s closed (%s)", sym, " (crash ladder)" if lt == LOT_LADDER else "", why)
            self._event("position_close", key="%s@%s:close%s" % (sym, pos.get("entry_ts"),
                                                                  ":ladder" if lt == LOT_LADDER else ""),
                        symbol=sym, why=why, lot=lt, entry_px_usdt=pos.get("entry_px_usdt"), source=pos.get("source"))

    def _sync_positions(self, now, prices, units):
        """Book the bot's new fills (broker.fill_records(): cumulative per identifier, exactly once)
        into the positions, then reconcile the positions with the managed holdings."""
        if not self.exits_on:
            return
        rate = self._usdt_rate()
        rate = float(rate) if rate is not None else None
        recs = self._fill_records()
        if recs is not None:
            if self._pos_booked is None:
                # the first run with positions: history is not replayed, the holdings open them below
                self._pos_booked = {r["identifier"]: {"ignore": True} for r in recs if r.get("identifier")}
            views = {}
            try:
                for v in self.broker.limit_orders():
                    views[v["identifier"]] = v
            except FATAL_ERRORS:
                raise
            except Exception:  # noqa: BLE001 - without it a ladder fill is booked as source "kimi"
                pass
            for r in recs:
                ident = r.get("identifier")
                if not ident or r.get("base") is None:
                    continue
                prev = self._pos_booked.get(ident)
                if isinstance(prev, dict) and prev.get("ignore"):
                    continue
                try:
                    base_a, quote_a = parse_symbol(r["symbol"])
                except (ValueError, TypeError, KeyError):
                    continue
                b, q, f = D(r.get("base") or 0), D(r.get("quote") or 0), D(r.get("fee") or 0)
                pb = D((prev or {}).get("b") or 0)
                pq = D((prev or {}).get("q") or 0)
                pf = D((prev or {}).get("f") or 0)
                db, dq, df = b - pb, q - pq, f - pf
                self._pos_booked[ident] = {"b": format(b, "f"), "q": format(q, "f"), "f": format(f, "f")}
                sym = "%s_%s" % (base_a, IRT)
                if db <= 0 or base_a in (USDT, IRT) or sym not in self.symbols:
                    continue
                v = views.get(ident) or {}
                meta = v.get("meta") if isinstance(v.get("meta"), dict) else {}
                if r.get("side") == "buy":
                    px_q = (dq / db) if dq > 0 else None
                    if px_q is None:
                        continue
                    px_usdt = float(px_q) if quote_a == USDT else (float(px_q) / rate if rate else None)
                    if not px_usdt:
                        continue
                    net = db - (df if r.get("fee_asset") == base_a else ZERO)
                    source = LADDER_TAG if v.get("tag") == LADDER_TAG else "kimi"
                    self._pos_add(sym, float(net), px_usdt, now, source, high48=meta.get("hi48_usdt"))
                else:
                    # the position the sale served: a code exit (_run_exits) or a target sell (its meta)
                    first = self._sell_attr.pop(str(ident), None)
                    if first is None and v.get("tag") == TARGET_TAG:
                        first = meta.get("lot") if meta.get("lot") in (LOT_MAIN, LOT_LADDER) else LOT_MAIN
                    self._reduce_lots(sym, float(db), first)
            known = {r["identifier"] for r in recs if r.get("identifier")}
            for k in [k for k in self._pos_booked if k not in known]:
                del self._pos_booked[k]
            for k in [k for k in self._sell_attr if k not in known]:
                del self._sell_attr[k]
        # reconcile with what the bot really manages
        floor = D(self.risk.min_order_irt)
        for sym in sorted(set(self._positions) | set(self._ladder_pos) | {s for s in self.symbols if s != SAFE}):
            u = D(units.get(sym, ZERO) or ZERO)
            px = prices.get(sym)
            if px is None:
                continue
            value = u * D(px)
            lots = self._lots(sym)
            if value < floor or u <= 0:
                if lots:
                    self._pos_close(sym, "sold (holding below one minimum order)")
                continue
            if not lots:
                if rate:
                    self._pos_open(sym, float(u), float(D(px)) / rate, now, "held")
                continue
            total = sum(float(p.get("amount") or 0) for _, _, p in lots)
            main = self._positions.get(sym)
            if float(u) < total * (1 - 1e-6):
                self._reduce_lots(sym, total - float(u))
            elif float(u) > total * (1 + 1e-6):
                extra = float(u) - total
                ev = extra * float(D(px))
                if rate and main is not None and ev >= float(floor) * 0.05:
                    self._pos_add(sym, extra, float(D(px)) / rate, now, main.get("source") or "held")
                elif rate and main is None and ev >= float(floor):
                    self._pos_open(sym, extra, float(D(px)) / rate, now, "held")
                else:
                    p = main if main is not None else lots[0][2]
                    p["amount"] = float(p.get("amount") or 0) + extra
            lots = self._lots(sym)
            if len(lots) == 2:
                # a position below one minimum order (a tiny partial fill, the dust of a sold one) cannot
                # be sold or given a target on its own: its coins join the coin's other position
                for _, lot, p in lots:
                    if float(p.get("amount") or 0) * float(D(px)) < float(floor):
                        other = self._book_of(LOT_MAIN if lot == LOT_LADDER else LOT_LADDER)[sym]
                        other["amount"] = float(other.get("amount") or 0) + float(p.get("amount") or 0)
                        self._pos_close(sym, "below one minimum order: its coins join the %s position" % (
                            "allocation" if lot == LOT_LADDER else "crash-ladder"), lot=lot)
                        break

    def _exits_due(self, prices):
        """[(symbol, "stop" | "target", close_usdt, level_usdt, lot)] of the positions whose hourly close
        (COIN_IRT / USDT_IRT of the same bar) reached their stop or their target."""
        out = []
        u = prices.get(SAFE)
        if u is None or D(u) <= 0:
            return out
        for sym, lot, pos in self._lots():
            p = prices.get(sym)
            if p is None:
                continue
            close = float(D(p) / D(u))
            stop, tgt = pos.get("stop_px_usdt"), pos.get("target_px_usdt")
            if stop and close <= float(stop):
                out.append((sym, "stop", close, float(stop), lot))
            elif tgt and close >= float(tgt):
                out.append((sym, "target", close, float(tgt), lot))
        return out

    def _check_plans(self, now, prices):
        """Kimi's plan of a position is BROKEN when an hourly close (COIN_IRT / USDT_IRT of the same bar, like
        the code exits) is below its invalidation level: marked once (plan broken_at / broken_close_usdt).
        That is a wake-up (the brain's W3, mode held_move), NOT an exit: the code does not sell on it, the
        model decides. Returns the symbols marked now."""
        out = []
        u = prices.get(SAFE)
        if u is None or D(u) <= 0:
            return out
        for sym, lot, pos in self._lots():
            plan = pos.get("plan")
            if not isinstance(plan, dict) or plan.get("broken_at"):
                continue
            p = prices.get(sym)
            try:
                inv = float(plan.get("invalidation_usdt"))
            except (TypeError, ValueError):
                continue
            if p is None or not inv > 0:
                continue
            close = float(D(p) / D(u))
            if close < inv:
                plan["broken_at"] = round(float(now), 3)
                plan["broken_close_usdt"] = close
                log.warning("PLAN BROKEN %s: the hourly close %s USDT is below the invalidation level %s USDT of Kimi's "
                            "%s plan - Kimi is woken (nothing is sold by this)", sym, _fmt_px(close), _fmt_px(inv),
                            plan.get("setup"))
                self._event("plan_broken", key="%s@%s" % (sym, plan.get("set_at")), symbol=sym, lot=lot,
                            close_usdt=close, level_usdt=inv, setup=plan.get("setup"))
                out.append(sym)
        if out:
            self._save_bot_state()
        return out

    def _write_ahead(self, bar_ts):
        """The bar is consumed before the first market order of the cycle (a restart never repeats it)."""
        if not self.dry_run and self.last_bar_ts != bar_ts:
            self._save_state(last_bar_ts=bar_ts, retry_buys=None)

    def _run_exits(self, rep, now, bar_ts, prices, last_close_1h, equity, units):
        """Code exits on the hourly close. Returns (ok, sold): ok False = an order's outcome is
        unknown (the cycle ends; it is resolved by identifier first). A position that exits sells ITS
        amount; when every position of the coin exits, all its managed units are sold."""
        due = self._exits_due(prices)
        sold = False
        by_sym = {}
        for d in due:
            by_sym.setdefault(d[0], []).append(d)
        for sym in sorted(by_sym):
            items = by_sym[sym]
            whole = {lot for _, lot, _ in self._lots(sym)} <= {d[4] for d in items}
            sell = []
            for _, why, close, level, lot in items:
                pos = self._book_of(lot).get(sym) or {}
                item = {"symbol": sym, "reason": why, "close_usdt": round(close, 8), "level_usdt": round(level, 8),
                        "entry_px_usdt": pos.get("entry_px_usdt"), "lot": lot}
                rep.setdefault("exits", []).append(item)
                log.warning("CODE EXIT %s: %s%s - the hourly close %s USDT is %s the %s %s USDT (entry %s)",
                            why.upper(), sym, " (crash-ladder position)" if lot == LOT_LADDER else "", _fmt_px(close),
                            "at or below" if why == "stop" else "at or above", why, _fmt_px(level),
                            _fmt_px(pos.get("entry_px_usdt")))
                # one event per position and reason (not per hour): a sale that keeps failing must not send
                # the owner a new "selling it now" alert every hour; the failure is logged loudly instead
                self._event("exit", key="%s@%s:%s%s" % (sym, pos.get("entry_ts"), why,
                                                        ":ladder" if lot == LOT_LADDER else ""),
                            symbol=sym, reason=why, lot=lot, close_usdt=close, level_usdt=level,
                            entry_px_usdt=pos.get("entry_px_usdt"), source=pos.get("source"))
                sell.append((item, why, close, lot, pos))
            if self.dry_run:
                for it in sell:
                    it[0]["dry_run"] = True
                continue
            self._write_ahead(bar_ts)
            n = len(rep["fills"])
            if whole:
                why = "stop" if any(w == "stop" for _, w, _, _, _ in sell) else "target"
                ok = self._sell_to_usdt(sym, why, rep, last_close_1h, equity, units)
            else:
                _, why, _, lot, pos = sell[0]            # the coin's other position stays
                ok = self._sell_to_usdt(sym, why, rep, last_close_1h, equity, units,
                                        amount=float(pos.get("amount") or 0), lot=lot)
            if len(rep["fills"]) > n:
                sold = True
                coin = parse_symbol(sym)[0]
                if not whole:
                    for f in rep["fills"][n:]:
                        try:
                            mine = f.get("side") == "sell" and parse_symbol(f.get("symbol"))[0] == coin
                        except (ValueError, TypeError):
                            mine = False
                        if mine and f.get("identifier"):
                            self._sell_attr[str(f["identifier"])] = sell[0][3]
                for _, w, close, _, pos in sell:
                    self._note_exit(sym, w, close, pos)
            elif ok:
                for it in sell:
                    it[0]["not_sold"] = True
                log.error("CODE EXIT of %s did NOT sell this hour (%s); it is tried again at the next hourly close",
                          sym, "; ".join(str(x) for x in (rep["errors"][-1:] or
                                                          [s for _, s in rep["skipped"][-1:]])) or "?")
            if not ok:
                self._save_bot_state()
                return False, sold
        if sold:
            self._save_bot_state()
        return True, sold

    def _best_route(self, coin, side, amount):
        """markets.best_route for coin -> USDT (side "sell", amount = coin) or USDT -> coin ("buy",
        amount = USDT), or None when routing is off / not possible."""
        if not self.routing_on or amount is None or D(amount) <= 0:
            return None
        if self.routing_coins is not None and coin not in self.routing_coins:
            return None
        if self._usdt_market(coin) is None:
            return None
        if self._direct_blocked(coin, side):
            return None                  # the exchange rejected this direct route recently: toman route
        fn = getattr(self.broker, "best_route", None)
        if not callable(fn):
            return None
        try:
            mins = self.risk.min_notional()
        except Exception:  # noqa: BLE001
            mins = None
        try:
            if side == "sell":
                return fn(coin, USDT, D(amount), min_notional=mins)
            return fn(USDT, coin, D(amount), min_notional=mins)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - the plan's own (IRT) legs are used
            log.warning("route %s %s unavailable (%s): using the toman market", side, coin, e)
            return None

    # ---- the direct COIN_USDT route: a market / side the exchange rejected waits REJECT_BACKOFF
    @staticmethod
    def _direct_key(coin, side):
        return "mkt:%s_%s:%s" % (coin, USDT, side)

    def _direct_blocked(self, coin, side):
        until = (self._ladder_st.get("backoff") or {}).get(self._direct_key(coin, side))
        try:
            return until is not None and self.clock() < float(until)
        except (TypeError, ValueError):
            return False

    def _direct_rejected(self, symbol, side, why):
        """A market order on COIN_USDT was definitely rejected by the exchange (HTTP 4xx): the toman
        route is used for this coin / side until REJECT_BACKOFF has passed (it is never re-tried every
        hour, and a code exit falls back at once)."""
        if self.dry_run:
            return
        coin = parse_symbol(symbol)[0]
        until = round(float(self.clock()) + REJECT_BACKOFF, 3)
        self._ladder_st.setdefault("backoff", {})[self._direct_key(coin, side)] = until
        log.error("the direct route %s %s was rejected by the exchange (%s): the toman route is used for it until %s",
                  side, symbol, why, fmt_ts(until))
        self._save_bot_state()

    def _direct_vet_ok(self, symbol, side, amount, last_close_1h, equity):
        """RiskManager.vet_order of a direct COIN_USDT leg at route-selection time (price sanity, minimum,
        slippage): a leg the vet would refuse is not routed directly (the toman legs are kept)."""
        if last_close_1h is None or equity is None:
            return True
        try:
            vet, _ = self._vet(side, symbol, D(amount), last_close_1h, equity)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - cannot vet it: keep the toman legs
            log.info("routing: %s %s cannot be vetted (%s): toman route", side, symbol, e)
            return False
        if not vet.ok:
            log.info("routing: %s %s would be refused (%s): toman route", side, symbol, vet.reason)
        return bool(vet.ok)

    def _sell_to_usdt(self, sym, reason, rep, last_close_1h, equity, units, amount=None, lot=None):
        """Sell the managed units of `sym` (at most `amount` coins: one position's) into USDT through the
        cheaper route (a code exit). When the direct COIN_USDT sell is refused (the vet, a local refusal
        or an HTTP 4xx rejection) the toman route is taken in the SAME check: a stop-loss must never wait
        for the next hour because one route does not work. lot: only that position's target sell is
        cancelled (the coin's other position keeps its own)."""
        coin = parse_symbol(sym)[0]
        self._cancel_limits(rep, tag=TARGET_TAG, coin=coin, why="code exit (%s)" % reason, lot=lot)
        try:
            self.broker.refresh()
            have = D(self.broker.available().get(coin, ZERO))
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - retried at the next hourly close
            rep["errors"].append("code exit %s: balance read failed: %s" % (sym, e))
            log.error("code exit %s: balance read failed (%s); retried at the next hourly close", sym, e)
            return True
        cap = min(D(units.get(sym, ZERO) or ZERO), have)
        if amount is not None:
            part = D(repr(float(amount)))
            try:
                m = self.broker.markets.get(sym)
                part = m.floor_base(part) if m is not None else part
            except Exception:  # noqa: BLE001 - the order's own rounding applies
                pass
            if lot is not None and have < part and self._cancel_limits(
                    rep, tag=TARGET_TAG, coin=coin, why="code exit (%s): its coins are locked by another target sell"
                                                        % reason):
                # the coin's other target sell (e.g. one of an older version, sized to every unit) locks this
                # position's coins: a stop never waits for the maintenance - it is re-placed at the end of the check
                try:
                    self.broker.refresh()
                    have = D(self.broker.available().get(coin, ZERO))
                except FATAL_ERRORS:
                    raise
                except Exception as e:  # noqa: BLE001 - retried at the next hourly close
                    rep["errors"].append("code exit %s: balance read failed: %s" % (sym, e))
                    return True
                cap = min(D(units.get(sym, ZERO) or ZERO), have)
            cap = min(cap, part)
        amount = cap
        if amount <= 0:
            return True
        px = D(repr(last_close_1h[sym])) if last_close_1h.get(sym) is not None else ZERO
        est = amount * px
        r = self._best_route(coin, "sell", amount)
        if r and r.get("ok") and r.get("route") == "direct":
            o = PlannedOrder("%s_%s" % (coin, USDT), "sell", amount, est, ZERO, ZERO, True, quote=USDT,
                             route="direct", reason=reason, serves=sym)
            n, ns = len(rep["fills"]), len(rep["skipped"])
            if not self._place(o, "sell", amount, last_close_1h, equity, rep):
                return False                   # outcome unknown: resolved by identifier first
            if len(rep["fills"]) > n:
                return True
            why = rep["skipped"][-1][1] if len(rep["skipped"]) > ns else (rep["errors"][-1] if rep["errors"] else "?")
            log.warning("code exit %s: the direct %s_%s sell did not execute (%s): selling through the toman route "
                        "instead", sym, coin, USDT, why)
            try:
                self.broker.refresh()
                amount = min(amount, D(self.broker.available().get(coin, ZERO)))
            except FATAL_ERRORS:
                raise
            except Exception as e:  # noqa: BLE001
                rep["errors"].append("code exit %s: balance read failed: %s" % (sym, e))
                return True
            if amount <= 0:
                return True
            est = amount * px
        o = PlannedOrder(sym, "sell", amount, est, ZERO, ZERO, True, route="via_irt", reason=reason, serves=sym)
        n = len(rep["fills"])
        if not self._place(o, "sell", amount, last_close_1h, equity, rep):
            return False
        got = ZERO
        for f in rep["fills"][n:]:
            got += D(f.get("quote") or 0) - (D(f.get("fee") or 0) if f.get("fee_asset") == IRT else ZERO)
        if got <= 0:
            return True
        # second leg: the toman proceeds into USDT (the default end state of a coin is USDT, never toman)
        try:
            avail = D(self.broker.available().get(IRT, ZERO)) * (1 - self.cash_buffer)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - the cash sweep moves it later
            log.warning("code exit %s: toman balance unavailable (%s); the proceeds stay in toman for now", sym, e)
            return True
        if self.capped:
            avail = min(avail, max(ZERO, self._sleeve_get()["cash"]) * (1 - self.cash_buffer))
        amt = min(got, avail)
        if amt < D(self.risk.min_order_irt):
            return True
        o2 = PlannedOrder(SAFE, "buy", amt, amt, ZERO, ZERO, route="via_irt", reason=reason, serves=sym)
        return self._place(o2, "buy", amt, last_close_1h, equity, rep)

    # ---- routing of an allocation plan
    def _route_plan(self, plan, prices, rep, last_close_1h=None, equity=None):
        """Rewrite the plan's USDT <-> coin legs as direct COIN_USDT orders where markets.best_route
        says that is cheaper: coin sells whose toman would buy USDT_IRT become COIN_USDT sells, coin
        buys paid by selling USDT_IRT become COIN_USDT buys with USDT. The rest of the plan (toman
        legs, thresholds, order: sells first) is unchanged. With last_close_1h / equity, a direct leg
        that RiskManager.vet_order would refuse (price sanity against the IRT cross, minimum,
        slippage) keeps the toman legs instead; a direct route the exchange rejected (4xx) recently
        is not chosen (_direct_blocked)."""
        if not self.routing_on or not plan.orders:
            return plan
        rate = prices.get(SAFE)
        if rate is None or D(rate) <= 0:
            return plan
        rate = D(rate)
        orders = list(plan.orders)
        min_irt = D(self.risk.min_order_irt)
        u_sell = next((o for o in orders if o.symbol == SAFE and o.side == "sell"), None)
        u_buy = next((o for o in orders if o.symbol == SAFE and o.side == "buy"), None)
        fee_slack = self.fee_rate * 3          # plan_orders sizes a buy after a sell net of one fee
        new, notes = [], []
        if u_buy is not None:
            room = D(u_buy.amount)                         # toman the plan spends on USDT
            for o in orders:
                if o is u_buy or o.side != "sell" or o.symbol == SAFE or room <= 0 or o.route:
                    new.append(o)
                    continue
                coin = parse_symbol(o.symbol)[0]
                part = min(D(o.est_notional), room)
                rest = D(o.est_notional) - part
                if rest < min_irt * Decimal("1.05") or rest <= D(o.est_notional) * fee_slack:
                    part, rest = D(o.est_notional), ZERO   # the rest is plan_orders' fee headroom, not a leg
                base_part = D(o.amount) if rest == 0 else D(o.amount) * part / D(o.est_notional)
                r = self._best_route(coin, "sell", base_part)
                if not (r and r.get("ok") and r.get("route") == "direct") or \
                        not self._direct_vet_ok("%s_%s" % (coin, USDT), "sell", base_part, last_close_1h, equity):
                    new.append(o)
                    continue
                new.append(PlannedOrder("%s_%s" % (coin, USDT), "sell", base_part, part, o.target, o.current,
                                        o.full_exit and rest == 0, quote=USDT, route="direct", serves=o.symbol))
                if rest > 0:
                    new.append(dataclasses.replace(o, amount=D(o.amount) - base_part, est_notional=rest,
                                                   full_exit=False))
                room -= part
                notes.append("%s sold directly into USDT on %s_USDT (~%s IRT, est. cost %s)" % (
                    coin, coin, fmt_amount(part), _fmt_frac(r.get("exec_cost"))))
            spent = D(u_buy.amount) - room
            if spent > 0:
                idx = new.index(u_buy)
                left = D(u_buy.amount) - spent
                if left < min_irt:
                    new.pop(idx)
                else:
                    new[idx] = dataclasses.replace(u_buy, amount=left, est_notional=left)
        elif u_sell is not None:
            room_irt, room_usdt = D(u_sell.est_notional), D(u_sell.amount)
            for o in orders:
                if o.side != "buy" or o.symbol == SAFE or room_irt <= 0 or o.route:
                    new.append(o)
                    continue
                coin = parse_symbol(o.symbol)[0]
                part = min(D(o.amount), room_irt)
                rest = D(o.amount) - part
                if rest <= ZERO and room_irt - part <= room_irt * fee_slack:
                    part = room_irt                    # plan_orders kept a fee headroom: the direct leg uses it
                usdt_amt = room_usdt * part / room_irt
                r = self._best_route(coin, "buy", usdt_amt)
                if not (r and r.get("ok") and r.get("route") == "direct") or \
                        not self._direct_vet_ok("%s_%s" % (coin, USDT), "buy", usdt_amt, last_close_1h, equity):
                    new.append(o)
                    continue
                new.append(PlannedOrder("%s_%s" % (coin, USDT), "buy", usdt_amt, part, o.target, o.current,
                                        quote=USDT, route="direct", serves=o.symbol))
                if rest >= min_irt:
                    new.append(dataclasses.replace(o, amount=rest, est_notional=rest))
                room_irt -= part
                room_usdt -= usdt_amt
                notes.append("%s bought directly with USDT on %s_USDT (~%s IRT, est. cost %s)" % (
                    coin, coin, fmt_amount(part), _fmt_frac(r.get("exec_cost"))))
            if room_usdt < D(u_sell.amount):
                idx = new.index(u_sell)
                if room_irt < min_irt or room_usdt <= 0:
                    new.pop(idx)
                else:
                    new[idx] = dataclasses.replace(u_sell, amount=room_usdt, est_notional=room_irt,
                                                   full_exit=u_sell.full_exit)
        if not notes:
            return plan
        for n in notes:
            log.info("routing: %s", n)
        rep["routing"] = notes
        sells = [o for o in new if o.side == "sell"]
        buys = [o for o in new if o.side == "buy"]
        return dataclasses.replace(plan, orders=sells + buys)

    def _reread_book(self, rep, now, syms, prices):
        """The managed book read again after a Kimi call: new resting-order fills are handled first
        (ladder level, events, sleeve, positions), then the holdings are read. Returns (units, cash,
        cur_dec, cur, plan) like the top of _brain_once, or None when the balance read failed."""
        self._resting_sync(rep, now)
        try:
            units, cash = self._holdings(syms)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - nothing is executed on a stale book
            rep["errors"].append("balance read after the Kimi call failed: %s" % e)
            log.error("balance read after the Kimi call failed (%s): the decision is not executed this hour", e)
            return None
        _, plan = self._book(syms, prices, units, cash)
        cur_dec = dict(plan.current)
        cur = {s: float(w) for s, w in cur_dec.items() if s in self.symbols and w > 0}
        if self.exits_on and not self._price_gaps(syms, prices, units):
            self._sync_positions(now, prices, units)
            self._save_bot_state()
        return units, cash, cur_dec, cur, plan

    def _early_targets(self, rep, now, units, equity):
        """Target sells of the positions a resting-order fill opened this hour, placed BEFORE the Kimi
        call the fill triggers (the spec: "code attaches exits at once"). The full maintenance still
        runs at the end of the cycle."""
        if self.dry_run or not self.exits_on or not self._has_positions():
            return
        if self.risk.kill_switch_active() or self.risk.is_halted():
            return
        try:
            if self.broker.unresolved_count():
                return
            self._maintain_targets(rep, now, units, equity)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - retried by the maintenance at the end of the cycle
            rep["errors"].append("early target sells: %s: %s" % (type(e).__name__, e))
            log.warning("target sells after the fill not placed yet (%s); retried at the end of the cycle", e)
        self._save_bot_state()

    # ---- maintenance of the resting orders (end of every processed hourly check)
    def _maintain(self, rep, now, equity, syms):
        if self.brain is None or not (self.ladder_on or self.exits_on or self._has_limits()):
            return
        if self.risk.kill_switch_active() or self.risk.is_halted():
            return
        try:
            if self.broker.unresolved_count():
                log.info("resting orders are not maintained while a market order's outcome is unknown")
                return
            units, _ = self._holdings(syms)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - retried at the next hourly check
            rep["errors"].append("resting orders: balance read failed: %s" % e)
            log.warning("resting orders not maintained this hour: balance read failed (%s)", e)
            return
        try:
            if self.exits_on:
                self._sync_positions(now, self._cycle_prices, units)
            self._maintain_targets(rep, now, units, equity)
            seen = self._fills_handled
            self._maintain_ladder(rep, now, equity, units)
            if self._fills_handled != seen and self.exits_on:
                # a bid filled while the ladder was maintained (a cancel raced it): its position and
                # target sell follow now, not an hour later
                units, _ = self._holdings(syms)
                self._sync_positions(now, self._cycle_prices, units)
                self._maintain_targets(rep, now, units, equity)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - never crash the cycle over the resting orders
            rep["errors"].append("resting orders: %s: %s" % (type(e).__name__, e))
            log.exception("resting-order maintenance failed: %s", e)
        self._save_bot_state()

    def _place_resting(self, rep, sym, side, price, base, tag, meta, book, equity=None):
        """Place one post-only limit order. Returns the view, or None when it was not placed."""
        if self.dry_run:
            rep.setdefault("resting", {}).setdefault("would_place", []).append(
                "%s %s %s @ %s (%s)" % (side, fmt_amount(base), sym, fmt_amount(price), tag))
            return None
        bkey = "%s:%s" % (sym, side)
        backoff = self._ladder_st.setdefault("backoff", {})
        until = backoff.get(bkey)
        if until is not None and self.clock() < float(until):
            rep.setdefault("resting", {}).setdefault("skipped", []).append(
                "%s %s %s: rejected by the exchange earlier, retried after %s" % (tag, side, sym, fmt_ts(until)))
            return None
        backoff.pop(bkey, None)
        try:
            v = self.broker.place_limit(sym, side, price, base, post_only_intent=True, book=book, tag=tag, meta=meta)
        except OrderStatusUnknown as e:
            self._record_limit("limit")
            rep["errors"].append("%s limit %s %s: outcome unknown: %s" % (tag, side, sym, e))
            log.error("%s limit %s %s: outcome unknown (%s); found by identifier at the next sync", tag, side, sym, e)
            return None
        except FATAL_ERRORS:
            raise
        except LimitWouldCross as e:
            log.info("%s limit %s %s not placed: %s", tag, side, sym, e)
            rep.setdefault("resting", {}).setdefault("skipped", []).append("%s %s %s: would cross" % (tag, side, sym))
            return None
        except BrokerError as e:              # refused locally: nothing sent
            log.warning("%s limit %s %s not placed: %s", tag, side, sym, e)
            rep.setdefault("resting", {}).setdefault("skipped", []).append("%s %s %s: %s" % (tag, side, sym, e))
            return None
        except OrderNotSent as e:
            log.warning("%s limit %s %s not sent: %s", tag, side, sym, e)
            return None
        except BitpinAPIError as e:
            if e.status is None or not 400 <= e.status < 500:
                self._record_limit("limit")
            else:
                # a definite rejection (e.g. below the exchange's own minimum on this market): not
                # re-sent every hour - this market / side waits REJECT_BACKOFF
                backoff[bkey] = round(float(self.clock()) + REJECT_BACKOFF, 3)
            rep["errors"].append("%s limit %s %s: %s" % (tag, side, sym, e))
            log.error("%s limit %s %s failed: %s", tag, side, sym, e)
            return None
        except Exception as e:  # noqa: BLE001 - state unknown: the journal has it, sync_limits resolves it
            self._record_limit("limit")
            rep["errors"].append("%s limit %s %s: %s" % (tag, side, sym, e))
            log.exception("%s limit %s %s: unexpected error", tag, side, sym)
            return None
        if v.get("sent"):
            self._record_limit("limit")
        rep.setdefault("resting", {}).setdefault("placed", []).append(
            "%s %s %s %s @ %s" % (tag, side, fmt_amount(v.get("base_amount") or base), sym,
                                  fmt_amount(v.get("price") or price)))
        log.info("resting %s order placed: %s %s %s @ %s (identifier %s)", tag, side, v.get("base_amount"), sym,
                 v.get("price"), v.get("identifier"))
        self._event("order_place", key="place:%s" % v.get("identifier"), tag=tag, symbol=sym, side=side,
                    price=v.get("price"), base_amount=v.get("base_amount"), identifier=v.get("identifier"),
                    level_pct=(meta or {}).get("level_pct"), coin=(meta or {}).get("coin"),
                    notional=(D(v.get("price") or 0) * D(v.get("base_amount") or 0)))
        return v

    def _get_book(self, sym, books):
        if sym not in books:
            try:
                books[sym] = self.broker.order_book(sym)
            except FATAL_ERRORS:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("order book %s unavailable: %s", sym, e)
                books[sym] = None
        return books[sym]

    def _vet_resting(self, side, coin, price, base, equity, books):
        """RiskManager.vet_limit of a post-only order on COIN_USDT, or None when it cannot be vetted."""
        m = self._usdt_market(coin)
        sym = "%s_%s" % (coin, USDT)
        if m is None:
            return None, None
        book = self._get_book(sym, books)
        if book is None:
            return None, None
        ref = self._ref_close(sym, self._cycle_lc)
        rate = self._usdt_rate()
        vet = self.risk.vet_limit(side, m, D(repr(float(price))), D(repr(float(base))), book, ref, equity,
                                  quote_irt_rate=rate, post_only=True)
        return vet, book

    def _backoff_until(self, symbol, side):
        """Epoch until which a resting order on `symbol` / `side` waits after an exchange rejection
        (REJECT_BACKOFF), or None when it may be placed now."""
        until = (self._ladder_st.get("backoff") or {}).get("%s:%s" % (symbol, side))
        try:
            return float(until) if until is not None and self.clock() < float(until) else None
        except (TypeError, ValueError):
            return None

    def _maintain_targets(self, rep, now, units, equity):
        """A resting maker sell at the target of every position (COIN_USDT), replaced only when the
        target or the size moved by more than the exits' tolerances. A coin with two positions (the
        allocation's and the crash ladder's) has one target sell per position, each sized to its own
        position (meta "lot"); a coin with one position sells all its managed units at the target."""
        ec = self.cfg["exits"]
        try:
            views = list(self.broker.limit_orders(tag=TARGET_TAG, active_only=True))
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("target orders unavailable: %s", e)
            return
        by_key = {}
        for v in views:
            by_key.setdefault((parse_symbol(v["symbol"])[0], self._lot_of(v)), []).append(v)
        books = {}
        wanted = set()
        for sym, lot, pos in self._lots() if self.exits_on else []:
            coin = parse_symbol(sym)[0]
            key = (coin, lot)
            vs = by_key.get(key, [])
            live = [v for v in vs if not v.get("cancel_requested")]
            tgt = pos.get("target_px_usdt")
            # a resting target on COIN_USDT for the ladder / routing coins (liquid USDT books); any other
            # coin's target is enforced at the hourly close (_exits_due)
            usable = coin in self.ladder_coins or self.routing_coins is None or coin in self.routing_coins
            m = self._usdt_market(coin) if usable else None
            if not (ec["target_orders"] and tgt and m is not None):
                continue                   # no target order for it: any old one is cancelled below
            wanted.add(key)
            u = D(units.get(sym, ZERO) or ZERO)
            split = len(self._lots(sym)) > 1
            amount = m.floor_base(min(u, D(repr(float(pos.get("amount") or 0)))) if split else u)
            if amount <= 0:
                continue
            what = "%s%s" % (sym, " (crash-ladder position)" if lot == LOT_LADDER else "")
            if len(live) == 1 and len(vs) == 1 and not needs_replace(live[0]["price"], live[0]["remaining_base"], tgt,
                                                                     amount, ec["reprice_pct"], ec["resize_pct"]):
                continue
            if len(vs) > len(live):
                continue                   # a cancel is still pending: the funds are not free yet
            until = self._backoff_until("%s_%s" % (coin, USDT), "sell")
            if until is not None:
                if live:                   # the replacement could not be placed: the resting one is kept
                    log.info("target sell of %s not re-priced: %s_%s sells wait after an exchange rejection until "
                             "%s", what, coin, USDT, fmt_ts(until))
                    continue
            if not self.risk.can_place_order("limit"):
                log.warning("target of %s not (re)placed: the daily limit-order budget is used up", what)
                continue
            vet, book = self._vet_resting("sell", coin, tgt, amount, equity, books)
            if vet is None or not vet.ok:
                why = vet.reason if vet is not None else "no order book / market"
                log.warning("target sell of %s not (re)placed (%s)%s", what, why,
                            "; the resting one is kept" if live else "")
                rep.setdefault("resting", {}).setdefault("skipped", []).append("target %s: %s" % (what, why))
                continue
            if live or split:
                if live:
                    after = self._cancel_views(rep, live, "target re-priced / re-sized")
                    self._process_limit_fills(rep)
                    if len(after) < len(live) or any(not a.get("final", False) for a in after):
                        continue           # the old order is not closed yet: its coins are still locked
                try:
                    self.broker.refresh()
                    have = D(self.broker.available().get(coin, ZERO))
                except FATAL_ERRORS:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("target of %s: balance read failed (%s)", what, e)
                    continue
                base = min(vet.base_amount, m.floor_base(have))
            else:
                base = vet.base_amount
            if base <= 0 or vet.price * base < D(self.risk.min_order_for(USDT)):
                continue
            self._place_resting(rep, "%s_%s" % (coin, USDT), "sell", vet.price, base, TARGET_TAG,
                                {"coin": coin, "symbol": sym, "target_usdt": float(tgt), "lot": lot}, book)
        for key, vs in sorted(by_key.items()):
            if key not in wanted:
                live = [v for v in vs if not v.get("cancel_requested")]
                if live:
                    self._cancel_views(rep, live, "no position / no target any more")
                    self._process_limit_fills(rep)

    def _ladder_budget(self, views, units):
        """USDT the ladder may lock: the free USDT plus what its own bids lock now (a sleeve: at most
        the sleeve's USDT), minus the cash buffer."""
        try:
            free = D(self.broker.available().get(USDT, ZERO))
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("USDT balance unavailable (%s): no new ladder bids this hour", e)
            return ZERO, ZERO
        locked = sum((D(v["remaining_base"]) * D(v["price"]) for v in views if v.get("side") == "buy"), ZERO)
        total = free + locked
        if self.capped:
            total = min(total, D(units.get(SAFE, ZERO) or ZERO))
        return max(ZERO, total * (1 - self.cash_buffer)), free

    def _maintain_ladder(self, rep, now, equity, units):
        lc = self.cfg["ladder"]
        try:
            views = list(self.broker.limit_orders(tag=LADDER_TAG, active_only=True))
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("ladder orders unavailable: %s", e)
            return
        eg = self._endgame(now)
        if eg.get("no_new_entries") and self.ladder_on and not self._ladder_st.get("endgame_off_at"):
            self._ladder_st["endgame_off_at"] = round(float(now), 3)
            log.warning("ENDGAME: no new coin entries from now on - every crash-ladder bid is cancelled")
            self._event("endgame", key="ladder_off", step="ladder_off",
                        no_new_entries_at=eg.get("no_new_entries_at"))
        scales = self._ladder_scales(now) if self.ladder_on else {}
        on = self.ladder_on and not eg.get("no_new_entries")
        # the anti-pump buy guard covers the ladder too: while a coin is guarded it gets no bid (none placed,
        # no filled level re-armed) and its resting bids are cancelled - a -20% bid under a pump peak would
        # buy the pump back, the pattern the pump study found to lose. Kimi's scale stays as it is and
        # applies again when the guard ends. A coin whose check failed is left as it is this hour.
        pump_failed = set()
        pumped = self._ladder_pumped(now, pump_failed) if on else {}
        if on:
            self._note_ladder_pumps(pumped)
        eff = {c: (0.0 if c in pumped else v) for c, v in scales.items()}
        slots = {}
        for v in views:
            meta = v.get("meta") if isinstance(v.get("meta"), dict) else {}
            coin = str(meta.get("coin") or parse_symbol(v["symbol"])[0]).upper()
            slots.setdefault((coin, lvl_key(meta.get("level_pct"))), []).append(v)
        # re-arm the filled levels of a coin that is back above -rearm_pct from its 48 h high
        rearm = float(lc["rearm_pct"])
        for coin in self.ladder_coins:
            ref = self._refs.get(coin)
            if not ref or ref["dd48"] <= -rearm or coin in pumped or coin in pump_failed:
                continue
            for lv in self.ladder_levels:
                ls = self._level(coin, lv)
                if ls.get("armed", True) is False:
                    ls.update(armed=True, rearmed_at=round(float(now), 3))
                    log.info("ladder %s %g%% re-armed (the coin is %.1f%% from its 48 h high)", coin, lv, ref["dd48"])
                    self._event("ladder_rearm", key="%s:%s:%s" % (coin, lvl_key(lv), ls.get("fill_ident")),
                                coin=coin, level_pct=lv, dd48_pct=ref["dd48"])
        desired, factor = {}, 0.0
        budget, free = ZERO, ZERO
        rate = self._usdt_rate()
        if on and rate is None:
            log.warning("ladder: no USDT_IRT price this hour: the bids are left as they are")
            return
        # a coin without a 48 h reference this hour (candles missing) or without a trading COIN_USDT
        # market keeps its resting bids untouched: a data gap must not cancel the ladder
        unknown = {c for c in self.ladder_coins if not self._refs.get(c) or self._usdt_market(c) is None} | pump_failed
        if on:
            budget, free = self._ladder_budget(views, units)
            refs = {c: self._refs.get(c) for c in self.ladder_coins if c not in unknown}
            armed = {(c, lvl_key(lv)): self._level(c, lv).get("armed", True) for c in self.ladder_coins
                     for lv in self.ladder_levels}
            desired, factor = ladder_plan(refs, eff, armed, self.ladder_levels, lc["size_frac"], float(equity),
                                          float(rate), float(budget), float(self.risk.min_order_for(USDT)))
        state = {"on": on, "factor": round(factor, 4), "budget_usdt": float(budget),
                 "desired": {"%s %s" % k: [round(w["price_usdt"], 8), round(w["usdt"], 4)] for k, w in desired.items()}}
        if pumped:
            state["pump_guard"] = {c: fmt_ts(g["until"]) for c, g in sorted(pumped.items())}
        rep["ladder_state"] = state
        books = {}
        pending_place = {}
        cancelled_any = False
        reserve = len(desired) + 2
        # phase 1: cancel what must go; vet replacements BEFORE cancelling the old bid
        for key, vs in sorted(slots.items()):
            coin, lk = key
            live = [v for v in vs if not v.get("cancel_requested")]
            if not live:
                continue
            w = desired.get(key)
            if w is None:
                ls = self._level(coin, float(lk)) if lk not in ("None", "") else {}
                if on and eff.get(coin, 0.0) > 0 and ls.get("armed", True) is False and len(live) == 1:
                    continue           # the unfilled rest of a filled bid keeps resting at its level
                if on and eff.get(coin, 0.0) > 0 and coin in unknown:
                    continue           # no reference this hour: nothing is decided about it
                why = ("the ladder is off" if not on else
                       "pump-guarded until %s (no crash-ladder buy right after a pump)" % fmt_ts(pumped[coin]["until"])
                       if coin in pumped else "scale 0" if eff.get(coin, 0.0) <= 0
                       else "not wanted any more (budget / level)")
                self._cancel_views(rep, live, why)
                cancelled_any = True
                continue
            keep, extra = live[-1], live[:-1]
            if extra:
                self._cancel_views(rep, extra, "duplicate ladder bid")
                cancelled_any = True
            base_new = w["usdt"] / w["price_usdt"]
            if not needs_replace(keep["price"], keep["remaining_base"], w["price_usdt"], base_new, lc["reprice_pct"],
                                 lc["resize_pct"]):
                continue
            if self.risk.remaining_orders("limit") <= reserve:
                log.info("ladder %s %s%%: re-pricing postponed (limit-order budget kept for new bids and targets)",
                         coin, lk)
                continue
            until = self._backoff_until("%s_%s" % (coin, USDT), "buy")
            if until is not None:
                # the exchange rejected a bid on this market recently: the replacement could not be placed
                # before that wait ends, so the resting bid is kept (a crash must still find it)
                log.info("ladder %s %s%%: re-pricing postponed: %s_%s bids wait after an exchange rejection until %s",
                         coin, lk, coin, USDT, fmt_ts(until))
                continue
            vet, book = self._vet_resting("buy", coin, w["price_usdt"], base_new, equity, books)
            if vet is None or not vet.ok:
                log.warning("ladder %s %s%%: the replacement bid is refused (%s): the resting bid is kept", coin, lk,
                            vet.reason if vet is not None else "no order book / market")
                continue
            self._cancel_views(rep, [keep], "re-priced / re-sized (%s -> %s USDT)" % (
                keep["price"], _fmt_px(w["price_usdt"])))
            cancelled_any = True
            pending_place[key] = (vet, book)
        if cancelled_any:
            self._process_limit_fills(rep)            # a bid that filled while it was cancelled
        if not desired:
            return
        # phase 2: place the missing bids, never more than the free USDT
        try:
            self.broker.refresh()
            now_views = list(self.broker.limit_orders(tag=LADDER_TAG, active_only=True))
            free = D(self.broker.available().get(USDT, ZERO))
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("ladder: balances unavailable after the cancels (%s): no new bids this hour", e)
            return
        occupied = set()
        for v in now_views:
            meta = v.get("meta") if isinstance(v.get("meta"), dict) else {}
            occupied.add((str(meta.get("coin") or parse_symbol(v["symbol"])[0]).upper(), lvl_key(meta.get("level_pct"))))
        if self.capped:
            locked = sum((D(v["remaining_base"]) * D(v["price"]) for v in now_views if v.get("side") == "buy"), ZERO)
            free = min(free, max(ZERO, D(units.get(SAFE, ZERO) or ZERO) - locked))
        free = free * (1 - self.cash_buffer)
        min_usdt = D(self.risk.min_order_for(USDT))
        for key, w in sorted(desired.items(), key=lambda kv: (-kv[1]["level_pct"], kv[0])):   # shallowest first
            coin, lk = key
            if key in occupied or self._level(coin, float(lk)).get("armed", True) is False:
                continue
            if not self.risk.can_place_order("limit"):
                log.warning("ladder: the daily limit-order budget is used up: no new bids")
                break
            got = pending_place.get(key)
            if got is None:
                got = self._vet_resting("buy", coin, w["price_usdt"], w["usdt"] / w["price_usdt"], equity, books)
            vet, book = got
            if vet is None or not vet.ok:
                why = vet.reason if vet is not None else "no order book / market"
                log.info("ladder %s %s%% not placed: %s", coin, lk, why)
                rep.setdefault("resting", {}).setdefault("skipped", []).append("ladder %s %s%%: %s" % (coin, lk, why))
                continue
            base = vet.base_amount
            if vet.price * base > free:           # never more than the free USDT: shrink to it
                m = self._usdt_market(coin)
                base = m.floor_base(free / vet.price) if m is not None and vet.price > 0 else ZERO
            if base <= 0 or vet.price * base < min_usdt:
                log.info("ladder %s %s%% not placed: not enough free USDT (%s)", coin, lk, fmt_amount(free))
                continue
            ref = self._refs.get(coin) or {}
            v = self._place_resting(rep, "%s_%s" % (coin, USDT), "buy", vet.price, base, LADDER_TAG,
                                    {"coin": coin, "level_pct": float(w["level_pct"]), "hi48_usdt": ref.get("hi48"),
                                     "scale": scales.get(coin)}, book)
            if v is not None:
                free -= D(v.get("price") or vet.price) * D(v.get("base_amount") or base)
                self._level(coin, float(lk)).update(price_usdt=float(vet.price), identifier=v.get("identifier"),
                                                    placed_at=round(float(now), 3))

    def _note_ladder_pumps(self, pumped):
        """A log line and a bot event (kind "pump_guard", scope "ladder") once per pump of a ladder coin;
        a line when its guard ended. The pumps in force are kept in the ladder state."""
        seen = self._ladder_st.get("pump_guard")
        seen = seen if isinstance(seen, dict) else {}
        cur = {}
        for c in sorted(pumped):
            g = pumped[c]
            at = round(float(g["at"]), 3)
            cur[c] = {"at": at, "until": round(float(g["until"]), 3), "rise_pct": g.get("rise_pct")}
            old = seen.get(c) if isinstance(seen.get(c), dict) else {}
            try:
                same = old.get("at") is not None and abs(float(old["at"]) - at) < 1e-3
            except (TypeError, ValueError):
                same = False
            if not same:
                log.warning("PUMP GUARD (crash ladder): %s rose %g%% within the guard window - no ladder bid for it "
                            "until %s (resting bids cancelled, filled levels not re-armed; Kimi's scale is kept)",
                            c, g.get("rise_pct"), fmt_ts(g["until"]))
                self._event("pump_guard", key="ladder:%s:%s" % (c, at), scope="ladder", coin=c,
                            symbol="%s_%s" % (c, IRT), rise_pct=g.get("rise_pct"), until=g["until"])
        for c in sorted(set(seen) - set(cur)):
            log.info("PUMP GUARD (crash ladder): %s is no longer guarded - its ladder bids follow its scale again", c)
        if cur:
            self._ladder_st["pump_guard"] = cur
        else:
            self._ladder_st.pop("pump_guard", None)

    def _ladder_view(self, now):
        """The brain's `ladder` state (module docstring of bitpin/brain.py), or None when off."""
        if not self.ladder_on:
            return None
        lc = self.cfg["ladder"]
        scales = self._ladder_scales(now)
        eg = self._endgame(now)
        pumped = self._ladder_pumped(now)
        active = {}
        try:
            for v in self.broker.limit_orders(tag=LADDER_TAG, active_only=True):
                if v.get("cancel_requested"):
                    continue
                meta = v.get("meta") if isinstance(v.get("meta"), dict) else {}
                active[(str(meta.get("coin") or parse_symbol(v["symbol"])[0]).upper(),
                        lvl_key(meta.get("level_pct")))] = v
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("ladder orders unavailable: %s", e)
        coins = {}
        for c in self.ladder_coins:
            ref = self._refs.get(c) or {}
            sc = scales.get(c, 0.0)
            bids = []
            for lv in self.ladder_levels:
                ls = self._level(c, lv)
                v = active.get((c, lvl_key(lv)))
                px = None
                if ls.get("armed", True) is False and ls.get("filled_at"):
                    status, px = "filled", ls.get("fill_px_usdt")
                elif v is not None:
                    status, px = "resting", float(v["price"])
                elif sc <= 0 or eg.get("no_new_entries") or not lc["enabled"] or c in pumped:
                    status = "off"
                else:
                    status = "cancelled"
                if px is None and ref.get("hi48"):
                    px = ref["hi48"] * (1.0 + lv / 100.0)
                bids.append({"level_pct": lv, "price_usdt": px, "status": status,
                             "filled_at": ls.get("filled_at") if status == "filled" else None,
                             "fill_px_usdt": ls.get("fill_px_usdt") if status == "filled" else None,
                             # the level's last fill even after it re-armed (the brain's fill watermark
                             # decides whether it was reviewed: W1 is not lost when the level re-arms in
                             # the hour its review was postponed)
                             "last_fill_at": ls.get("filled_at"), "last_fill_px_usdt": ls.get("fill_px_usdt"),
                             "size_frac": round(float(lc["size_frac"]) * sc, 6)})
            coins[c] = {"scale": sc, "high48_usdt": ref.get("hi48"), "close_usdt": ref.get("close"),
                        "dd48_pct": ref.get("dd48"),
                        "armed": any(self._level(c, lv).get("armed", True) is not False for lv in self.ladder_levels),
                        "bids": bids}
            if c in pumped:
                coins[c]["pump_guard_until"] = pumped[c]["until"]
        return {"enabled": bool(lc["enabled"]), "coins": coins}

    def cancel_resting_orders(self, tag=None, why="operator request"):
        """Cancel the bot's resting orders (all, or one tag: "ladder" / "target"), e.g. for a CLI
        command before a manual intervention. Returns the report dict."""
        rep = {"errors": []}
        self._cancel_limits(rep, tag=tag, why=why)
        self._save_bot_state()
        return rep

    def _order_budget_ok(self, plan, orders=None):
        """A rebalance with buys runs only if the daily order budget covers ALL its orders, so the
        cap can never be hit after the sells and leave the proceeds idle in IRT."""
        orders = plan.orders if orders is None else orders
        n = len(orders)
        left = self.risk.remaining_orders()
        if n and any(o.side == "buy" for o in orders) and left < n:
            return False, "daily order budget: %d orders planned, only %d of max_orders_per_day %d left" % (
                n, left, self.risk.max_orders_per_day)
        return True, None

    # ---- flatten (opt-in drawdown action), retried every cycle until done
    def _flatten_cycle(self, rep):
        rep["status"] = "flattening"
        syms = list(dict.fromkeys(list(self.strategy.symbols) + self.extra_symbols))
        # the bot's own resting orders first: bids must not buy while it flattens, and target sells
        # lock the coins it has to sell
        self._cancel_limits(rep, tag=None, why="flatten")
        try:
            pending = self.broker.resolve_pending()
            self._after_resolve(rep)           # books late fills into the sleeve
            units, _ = self._holdings(syms)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            rep["errors"].append("flatten: balance read failed: %s" % e)
            log.error("flatten: balance read failed (%s); retrying next cycle", e)
            return rep
        # A symbol with an order of unknown outcome is not sold again: the sleeve ledger does not
        # include that order yet, and re-sending its amount (clamped to the account's TOTAL units)
        # could sell coins the user holds outside the sleeve.
        blocked = set()
        if pending:
            rep["pending"] = pending
            blocked = {p.get("symbol") for p in pending}
            if any(not s for s in blocked):
                blocked = set(syms)
            self._log_pending(pending)
            log.error("flatten: not selling %s until those orders are resolved", ", ".join(sorted(b for b in blocked if b)))
        mids = {}
        for s in syms:
            u = units.get(s, ZERO)
            if u <= 0 or s in blocked:
                continue
            try:
                mids[s] = mid_price(self.broker.order_book(s))
            except FATAL_ERRORS:
                raise
            except Exception as e:  # noqa: BLE001
                rep["errors"].append("flatten %s: order book failed: %s" % (s, e))
                mids[s] = None
                continue
            if mids[s] is not None and u * mids[s] < self.risk.min_order_irt:
                continue  # dust
            try:
                have = self.broker.available().get(parse_symbol(s)[0], ZERO)
            except FATAL_ERRORS:
                raise
            except Exception as e:  # noqa: BLE001
                rep["errors"].append("flatten: balance read failed: %s" % e)
                continue
            o = PlannedOrder(s, "sell", u, u * (mids[s] or ZERO), ZERO, ZERO, True)
            if not self._place(o, "sell", min(u, have), {}, ZERO, rep, flatten=True):
                blocked.add(s)   # outcome unknown: resolved by identifier next cycle
                break
        remaining = []
        try:
            after, _ = self._holdings(syms)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            rep["errors"].append("flatten: balance re-read failed: %s" % e)
            after = units
        for s in syms:
            u = after.get(s, ZERO)
            if s in blocked or (u > 0 and (mids.get(s) is None or u * mids[s] >= self.risk.min_order_irt)):
                remaining.append((s, u))
        if remaining or self.broker.unresolved_count():
            rep["flatten_remaining"] = remaining
            log.error("FLATTEN INCOMPLETE: still holding %s; retrying next cycle",
                      ", ".join("%s %s" % (parse_symbol(s)[0], u) for s, u in remaining) or "(orders unresolved)")
        else:
            self.risk.finish_flatten()
            rep["status"] = "halted"
            log.error("flatten complete: managed positions are in IRT. Trading stays halted until risk-reset.")
        return rep

    # ---- execution
    def _vet(self, side, symbol, amount, last_close_1h, equity, flatten=False):
        market = self.broker.markets.get(symbol)
        book = self.broker.order_book(symbol)
        if getattr(market, "quote", IRT) == IRT:
            ref = last_close_1h.get(symbol)
            ref = D(repr(ref)) if ref is not None else None
            return self.risk.vet_order(side, market, amount, book, ref, equity, flatten=flatten), book
        # a COIN_USDT route: the reference is the COIN_IRT / USDT_IRT cross of the same hour, and the
        # order is sized against the IRT equity at the USDT_IRT close
        ref = self._ref_close(symbol, last_close_1h)
        u = last_close_1h.get(SAFE)
        rate = D(repr(u)) if u is not None else self._usdt_rate()
        return self.risk.vet_order(side, market, amount, book, ref, equity, flatten=flatten,
                                   quote_irt_rate=rate), book

    def _must_stop(self, o, rep):
        """Checked before every order: the kill switch, and (brain mode) the decision's expiry."""
        if self.risk.kill_switch_active():
            rep["skipped"].append((o.symbol, "kill switch"))
            log.warning("kill switch: stopping before %s", o.describe())
            return True
        if self._exec_deadline is not None and self.clock() >= self._exec_deadline:
            rep["skipped"].append((o.symbol, "decision expired"))
            log.warning("the decision expired at %s: stopping before %s (no order of an expired decision is sent)",
                        fmt_ts(self._exec_deadline), o.describe())
            return True
        return False

    def _execute(self, plan, last_close_1h, rep, buys_only=False):
        eq = plan.effective_equity
        for o in ([] if buys_only else plan.sells):
            if self._must_stop(o, rep):
                return
            try:
                have = self.broker.available().get(parse_symbol(o.symbol)[0], ZERO)
            except FATAL_ERRORS:
                raise
            except Exception as e:  # noqa: BLE001
                rep["errors"].append("balance read failed: %s" % e)
                log.error("balance read failed (%s); aborting this cycle", e)
                self._transient_skip = True
                return
            amount = min(o.amount, have)
            if not self._place(o, "sell", amount, last_close_1h, eq, rep):
                return
        if not plan.buys:
            return
        try:
            self.broker.refresh()
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001
            log.error("balance refresh before buys failed (%s); skipping buys", e)
            rep["errors"].append("refresh before buys: %s" % e)
            self._transient_skip = True
            return
        for o in plan.buys:
            if self._must_stop(o, rep):
                return
            # the trading-hours guard right before the order (C3): a buy leg of a session-bound token that
            # was planned during the session and retried after the close must not land on a dead quote
            closed = self._session_closed(o.symbol, self.clock()) or self._spread_blocked(o.symbol)
            if closed:
                rep["skipped"].append((o.symbol, closed))
                log.warning("skip buy %s: %s", o.symbol, closed)
                continue
            q = o.quote or IRT            # what the buy spends: toman, or USDT on a COIN_USDT route
            try:
                avail = self.broker.available().get(q, ZERO) * (1 - self.cash_buffer)
            except FATAL_ERRORS:
                raise
            except Exception as e:  # noqa: BLE001
                rep["errors"].append("balance read failed: %s" % e)
                log.error("balance read failed (%s); aborting this cycle", e)
                self._transient_skip = True
                return
            if self.capped:  # never spend toman (or USDT) outside the sleeve
                if q == IRT:
                    own = self._sleeve_get()["cash"]
                else:
                    own = self._sleeve_get()["units"].get("%s_%s" % (q, IRT), ZERO)
                avail = min(avail, max(ZERO, own) * (1 - self.cash_buffer))
            amount = min(o.amount, avail)
            if amount <= 0:
                rep["skipped"].append((o.symbol, "no %s available" % q))
                continue
            if not self._place(o, "buy", amount, last_close_1h, eq, rep):
                return

    def _record_sent(self):
        try:
            self.risk.record_order()
        except Exception as e:  # noqa: BLE001 - never lose a fill over the order counter
            log.error("cannot record the order in the risk state: %s", e)

    def _place(self, o, side, amount, last_close_1h, equity, rep, flatten=False):
        """Vet + send one order. Returns False when the rest of the cycle must be aborted.
        Only orders that reached the exchange count toward max_orders_per_day. A skip caused by a
        temporary failure sets self._transient_skip (the buy leg is then retried within the bar)."""
        try:
            self.broker.prepare()   # a due token is renewed BEFORE the book snapshot, not before the POST
            vet, book = self._vet(side, o.symbol, amount, last_close_1h, equity, flatten)
        except FATAL_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - nothing was sent: skip only this order
            rep["errors"].append("%s %s: pre-trade check failed: %s" % (side, o.symbol, e))
            log.error("%s %s: pre-trade check failed (%s); order skipped", side, o.symbol, e)
            self._transient_skip = True
            return True
        for n in vet.notes:
            log.warning("%s %s: %s", o.symbol, side, n)
        if not vet.ok:
            rep["skipped"].append((o.symbol, vet.reason))
            log.warning("skip %s %s: %s", side, o.symbol, vet.reason)
            return True
        try:
            if side == "sell":
                fill = self.broker.market_sell(o.symbol, vet.amount, ref_price=vet.est_avg_price, book=book)
            else:
                fill = self.broker.market_buy(o.symbol, vet.amount, book=book)
        except OrderStatusUnknown as e:
            self._record_sent()
            rep["errors"].append(str(e))
            log.error("ORDER OUTCOME UNKNOWN: %s - aborting this cycle; it will be resolved by identifier", e)
            return False
        except FATAL_ERRORS:
            raise
        except OrderNotSent as e:  # definitely not accepted (e.g. throttled): re-planned later
            rep["errors"].append("%s %s: %s" % (side, o.symbol, e))
            log.error("%s %s not sent: %s", side, o.symbol, e)
            self._transient_skip = True
            return True
        except BrokerError as e:  # refused locally, nothing sent
            rep["errors"].append("%s %s: %s" % (side, o.symbol, e))
            log.error("%s %s refused: %s", side, o.symbol, e)
            if isinstance(e, (WalletDataError, InsufficientFunds)):
                self._transient_skip = True
            return True
        except BitpinAPIError as e:
            self._record_sent()
            rep["errors"].append("%s %s: %s" % (side, o.symbol, e))
            log.error("%s %s failed: %s", side, o.symbol, e)
            rejected = e.status is not None and 400 <= e.status < 500
            if rejected and o.route == "direct" and self.brain is not None:
                self._direct_rejected(o.symbol, side, e)       # the next plans use the toman route
            return rejected  # definite rejection: continue
        except Exception as e:  # noqa: BLE001 - state unknown: stop sending for this bar
            self._record_sent()
            rep["errors"].append("%s %s: %s" % (side, o.symbol, e))
            log.exception("%s %s: unexpected error; aborting this cycle", side, o.symbol)
            return False
        self._sleeve_book(fill)
        self._record_sent()
        quote_asset = parse_symbol(o.symbol)[1]
        if self.brain is not None:
            fill = dict(fill)
            fill.update(quote_asset=quote_asset, route=o.route or "single", reason=o.reason or "allocation")
            self._fill_event(fill)
        rep["fills"].append(fill)
        self._note_cost(dict(fill, symbol=o.symbol, side=side), self.clock(), self._ref_px(o.symbol, last_close_1h))
        log.info("FILL %s %s: base %s quote %s %s avg %s fee %s %s%s [%s]", side.upper(), o.symbol,
                 fmt_amount(fill["base"]), fmt_amount(fill["quote"]), quote_asset,
                 fmt_amount(fill["avg_price"]) if fill["avg_price"] else "-", fill["fee"], fill["fee_asset"],
                 " PARTIAL" if fill["partial"] else "", fill.get("order_id"))
        return True

    def _preview(self, plan, last_close_1h):
        out = []
        ok, why = self._order_budget_ok(plan)
        if not ok:
            out.append("rebalance would be SKIPPED: %s" % why)
        cash = plan.cash + sum((o.est_notional * (1 - self.fee_rate) for o in plan.sells if (o.quote or IRT) == IRT),
                               ZERO)
        for o in plan.orders:
            amount = o.amount
            if o.side == "buy" and (o.quote or IRT) == IRT:  # same cash buffer as the real execution
                amount = max(ZERO, min(amount, cash * (1 - self.cash_buffer)))
                cash -= amount
            try:
                vet, _ = self._vet(o.side, o.symbol, amount, last_close_1h, plan.effective_equity)
            except Exception as e:  # noqa: BLE001
                out.append("%s %s: preview failed: %s" % (o.side, o.symbol, e))
                continue
            if vet.ok:
                msg = "%s %s amount %s (~%s IRT, est. slippage %s)" % (
                    o.side.upper(), o.symbol, fmt_amount(vet.amount), fmt_amount(vet.notional),
                    "%.3f%%" % (vet.est_slippage * 100) if vet.est_slippage is not None else "n/a")
            else:
                msg = "%s %s would be SKIPPED: %s" % (o.side.upper(), o.symbol, vet.reason)
            if vet.notes:
                msg += " [" + "; ".join(vet.notes) + "]"
            out.append(msg)
            log.info("DRY RUN: %s", msg)
        return out

    def _log_plan(self, bar_ts, plan, closes, dd):
        log.info("=== %s%s | %s | bar %s (res %s) ===", self.mode.upper(), " DRY-RUN" if self.dry_run else "",
                 self.strategy, fmt_ts(bar_ts), self.res)
        log.info("%s %s IRT (cash %s IRT) | drawdown %.2f%% from HWM %s",
                 ("sleeve equity (budget %s)" % fmt_amount(self.equity_cap)) if self.capped else "equity",
                 fmt_amount(plan.equity), fmt_amount(plan.cash), dd["drawdown"] * 100, fmt_amount(dd["hwm"]))
        for s in sorted(plan.targets):
            log.info("  %-10s price %14s  current %.4f  target %.4f", s,
                     fmt_amount(closes[s]) if closes.get(s) is not None else "-", plan.current.get(s, 0),
                     plan.targets[s])
        for s, why in plan.skipped:
            log.info("  no trade %s: %s", s, why)
        if not plan.orders:
            log.info("  plan: no orders")
        for o in plan.orders:
            log.info("  plan: %-10s %s", o.symbol, o.describe())

    # ---- loop
    def run_once_with_retry(self, max_wait=None):
        """Retry while the newest candle is not ready. In loop mode (max_wait None) a bar the
        exchange has not closed yet is waited for up to one hour (a local clock that runs ahead)."""
        loop_mode = max_wait is None
        wait = float(self.cfg["data_max_wait_seconds"] if max_wait is None else max_wait)
        retry = float(self.cfg["data_retry_seconds"])
        start = self.clock()
        deadline = start + wait
        while True:
            try:
                rep = self.run_once()
                self._stale_bars = 0
                return rep
            except StaleData as e:
                dl = max(deadline, start + HOUR) if (loop_mode and e.kind == "not_opened") else deadline
                if self.clock() + retry > dl:
                    self._stale_bars += 1
                    log.warning("candles not ready (%s); giving up on this bar", e)
                    if self._stale_bars >= STALE_ALERT_AFTER:
                        log.error("%d bars in a row skipped because candles were not ready: the bot is NOT trading. "
                                  "Check that this computer's clock is correct (it must match real time within a "
                                  "few minutes; local time now %s) and that Bitpin's candle feed is up.",
                                  self._stale_bars, fmt_ts(self.clock()))
                    return {"status": "stale", "error": str(e), "fills": [], "skipped": [], "errors": [],
                            "stale_bars": self._stale_bars}
                log.info("candles not ready (%s); retry in %.0fs", e, retry)
                self.sleep(retry)

    def _sleep_until(self, t):
        while True:
            left = t - self.clock()
            if left <= 0 or self.risk.kill_switch_active():
                return
            self.sleep(min(left, 30.0))

    def loop(self, max_cycles=None):
        base = float(self.cfg["error_backoff_seconds"])
        backoff = base
        cycles = 0
        log.info("runner loop started: %s, mode %s%s, state %s", self.strategy, self.mode,
                 " (dry run)" if self.dry_run else "", self.state_dir)
        while True:
            if self.risk.kill_switch_active():
                log.warning("kill switch %s present: loop stopped", self.risk.kill_switch_path)
                self._kill_cleanup({"errors": []})
                return "kill_switch"
            try:
                rep = self.run_once_with_retry()
                backoff = base
            except FATAL_ERRORS as e:
                log.error("fatal: %s", e)
                raise
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - never crash the loop on a transient error
                log.exception("cycle failed; retrying in %.0fs", backoff)
                self._sleep_until(self.clock() + backoff)
                backoff = min(backoff * 2, float(self.cfg["error_backoff_max_seconds"]))
                continue
            cycles += 1
            if rep.get("status") in ("halted", "kill_switch"):
                return rep["status"]
            if max_cycles is not None and cycles >= max_cycles:
                return rep.get("status")
            pending = rep.get("pending") or []
            if pending and all(p.get("stuck") for p in pending):
                # needs the user (resolve-order): keep checking, but not every minute
                self._sleep_until(self.clock() + float(self.cfg["error_backoff_max_seconds"]))
                continue
            if (rep.get("status") in ("pending_orders", "pending_orders_stuck", "flattening") or pending
                    or rep.get("unresolved_after") or rep.get("retry_buys")):
                # orders of unknown outcome are polled every minute (inside unknown_order_max_age),
                # and skipped buys are retried within the bar
                self._sleep_until(self.clock() + base)
                continue
            off = self.offset if self.offset is not None else 1800
            wake = next_bar_close(self.clock(), self.res_seconds, off) + float(self.cfg["wake_delay_seconds"])
            log.info("next check at %s", fmt_ts(wake))
            self._sleep_until(wake)


def format_report(rep):
    """Short human summary of a run_once() report."""
    lines = ["status: %s" % rep.get("status")]
    if rep.get("bar_ts") is not None:
        lines.append("bar: %s (closed %s)" % (fmt_ts(rep["bar_ts"]), fmt_ts(rep.get("bar_close"))))
    if rep.get("equity") is not None:
        lines.append("%s before: %s IRT" % ("sleeve equity" if rep.get("managed") == "sleeve" else "equity",
                                            fmt_amount(rep["equity"])))
    if rep.get("stale_prices"):
        lines.append("NO PRICE for held %s: the equity above is incomplete, so this cycle did not trade and the "
                     "high-water mark was not moved" % ", ".join(rep["stale_prices"]))
    if rep.get("frozen"):
        lines.append("LOCKED in an order the bot did not place: %s (order sizes use the full balance, but only free "
                     "funds can be spent)" % ", ".join("%s %s" % (a, v) for a, v in sorted(rep["frozen"].items())))
    br = rep.get("brain")
    if br:
        nw = br.get("news")
        if nw:
            if nw.get("ok"):
                state = "STALE (the refresh failed: %s)" % nw.get("error") if nw.get("stale") else \
                    ("cached" if nw.get("cached") else "fresh")
                lines.append("kimi news: ok, %s, %s items, researched %s" % (state, nw.get("items"),
                                                                             fmt_ts(nw.get("fetched_at"))))
            else:
                lines.append("kimi news: UNAVAILABLE (%s) - the decision ran without news" % (nw.get("error") or "?"))
        d = br.get("decision")
        if d:
            if d.get("valid"):
                txt = "valid%s conf %s targets %s" % (" HOLD" if d.get("hold") else "", d.get("confidence"),
                                                      {s: round(float(w), 4) for s, w in (d.get("targets") or {}).items()})
            else:
                txt = "INVALID (%s): %s" % (d.get("error_kind"), d.get("error"))
            lines.append("kimi decision (%s, %s): %s" % ("new" if br.get("decided") else "latest",
                                                        fmt_ts(d.get("decided_at")), txt))
        if br.get("unexecutable"):
            lines.append("no longer executable after the portfolio moved since the decision: %s"
                         % "; ".join(br["unexecutable"]))
        if br.get("brain_error"):
            lines.append("kimi error: %s" % br["brain_error"])
        if br.get("pacing_block"):
            lines.append("kimi decision due but postponed (pacing): %s" % br["pacing_block"])
        if br.get("trigger_kind"):
            lines.append("kimi wake-up: %s (mode %s)%s" % (br["trigger_kind"], br.get("mode"),
                                                          (": " + "; ".join(br["events"])) if br.get("events") else ""))
        for n in br.get("notifications") or []:
            lines.append("notification (%s): %s" % (n.get("kind"), n.get("text")))
        for a in br.get("applied") or []:
            lines.append("applied from the decision: %s" % a)
        lines.append("kimi action: %s%s" % (br.get("action"), (" - %s" % br["why"]) if br.get("why") else ""))
    for x in rep.get("exits") or []:
        lines.append("CODE EXIT %s %s: close %s USDT vs %s %s USDT%s" % (
            x.get("reason"), x.get("symbol"), _fmt_px(x.get("close_usdt")), x.get("reason"),
            _fmt_px(x.get("level_usdt")), " (dry run: not sold)" if x.get("dry_run") else ""))
    for f in rep.get("limit_fills") or []:
        lines.append("resting order filled (%s): %s %s base=%s quote=%s avg=%s" % (
            f.get("reason"), f.get("side"), f.get("symbol"), _fmt_px(f.get("base")), _fmt_px(f.get("quote")),
            _fmt_px(f.get("avg_price"))))
    for p in rep.get("routing") or []:
        lines.append("routing: %s" % p)
    for p in rep.get("plan") or []:
        lines.append("planned: %s" % p)
    for p in rep.get("vetted") or []:
        lines.append("dry-run: %s" % p)
    for f in rep.get("fills") or []:
        qa = f.get("quote_asset") or "IRT"
        lines.append("filled: %s %s base=%s quote=%s %s avg=%s fee=%s %s%s%s" % (
            f["side"], f["symbol"], fmt_amount(f["base"]), fmt_amount(f["quote"]), qa,
            fmt_amount(f["avg_price"]) if f["avg_price"] else "-", f["fee"], f["fee_asset"],
            " (partial)" if f["partial"] else "", (" [%s]" % f["reason"]) if f.get("reason") else ""))
    resting = rep.get("resting") or {}
    for k, label in (("placed", "resting order placed"), ("cancelled", "resting order cancelled"),
                     ("would_place", "dry-run: would place"), ("would_cancel", "dry-run: would cancel"),
                     ("skipped", "resting order not placed")):
        for x in resting.get(k) or []:
            lines.append("%s: %s" % (label, x))
    ls = rep.get("ladder_state")
    if ls and ls.get("on"):
        lines.append("ladder: %d bid(s) wanted, pro-rata factor %s, USDT budget %s" % (
            len(ls.get("desired") or {}), ls.get("factor"), _fmt_px(ls.get("budget_usdt"))))
    for s, why in rep.get("skipped") or []:
        lines.append("skipped: %s: %s" % (s, why))
    for e in rep.get("errors") or []:
        lines.append("error: %s" % e)
    for p in rep.get("pending") or []:
        lines.append("pending order: %s %s identifier %s%s" % (p.get("side"), p.get("symbol"), p.get("identifier"),
                                                              " (LOOKUPS FAILING)" if p.get("stuck") else ""))
    for s, u in rep.get("flatten_remaining") or []:
        lines.append("flatten remaining: %s %s" % (s, u))
    if rep.get("equity_after") is not None:
        lines.append("%s after: %s IRT" % ("sleeve equity" if rep.get("managed") == "sleeve" else "equity",
                                           fmt_amount(rep["equity_after"])))
    if rep.get("error"):
        lines.append("error: %s" % rep["error"])
    return "\n".join(lines)
