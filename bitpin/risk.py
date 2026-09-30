"""Risk controls applied by the runner before every order.

* Kill switch: a file named STOP in the state dir stops the loop before any (further) order.
  `py scripts/run_bot.py stop` creates it, `resume` removes it.
* Max drawdown circuit breaker measured from the high-water mark of MANAGED equity (IRT): the bot's
  own sleeve when max_equity_irt is set, else IRT + the strategy's coins. The HWM restarts when
  the basis changes (e.g. a new max_equity_irt). The runner confirms a breach with a second wallet
  read and with the holdings valued at the order-book mid (see Runner._drawdown), so a single
  candle print neither halts nor raises the high-water mark. Default 30% (the competition config
  sets 50%). Action "halt" (default) stops trading and never sells; "flatten" halts and then sells
  the managed positions to IRT, retrying every cycle until they are below the minimum order size
  (flatten sells skip the price-sanity, max-order-fraction and order-count limits but keep the
  slippage guard). A halt persists until `run_bot.py risk-reset` (which refuses to run while a bot
  holds the state dir).
  ROLLING high-water mark (hwm_window_days, default 90; 0 = all-time): the mark is the highest
  equity seen in the last N days, not since the start. WHY: the equity is measured in toman and the
  competition runs a year; a toman that devalues 30-50% a year makes every coin/USDT position gain
  in toman terms, so an all-time mark set during a spike would sit far above any later equity for
  months and a later ordinary 50% crypto drawdown would halt the bot for good (year review Y1/Y2:
  "a year of toman appreciation must not trap the bot"). The state keeps one sample per UTC day
  (the day's highest equity, "hwm_log"); samples older than the window fall out, so after a long
  quiet decline the mark follows the equity down and the breaker measures the LAST 90 days only.
  A state file of an older version (all-time "hwm", no log) is taken as a sample of the day it was
  written (hwm_at) or, without that, of the first cycle after the upgrade, so it expires too.
* Max orders per rolling 24 h: a rebalance that has buys is skipped as a whole when the remaining
  budget cannot cover all its orders (never sells-then-no-buys); only orders actually sent count.
  Resting LIMIT placements have their own rolling budget (max_limit_orders_per_day), so re-placing
  resting bids can never use up the budget that market orders (e.g. a code-enforced stop) need,
  and a runaway re-placement loop is still bounded. Cancels are counted (cancels_last_24h, for
  reports) but never blocked: a cancel only removes exposure, and every cancel-and-replace cycle
  needs a placement, which is budgeted.
* Minimum order notional PER QUOTE currency, in that currency: min_order_irt (IRT markets, default
  1,000,000 until Bitpin's real minimum is verified) and min_order_usdt (USDT markets, default 0.5
  USDT = about 100,000 toman at USDT_IRT ~228,600 on 2026-09-23).
* Max single-order size as a fraction of equity (larger orders are shrunk; the rest waits for the
  next bar). Equity is always in IRT; a USDT-quoted order is converted with `quote_irt_rate` (IRT
  per USDT, e.g. the USDT_IRT mid), which the caller must pass for every non-IRT market.
* Price sanity: skip if the order-book mid deviates more than max_price_deviation (5%) from the
  last candle close. For a limit order also: a buy may not be priced above, a sell not below, the
  last close by more than max_price_deviation, and no limit order may sit further than
  max_limit_distance (50%) from the mid (unit / fat-finger guard).
* Slippage guard, for TAKERS only: a market order is walked through the visible order book; if the
  estimated average price is worse than the mid by more than max_slippage (1%), or the visible book
  is too thin, the order is shrunk to the size that fits, or skipped if that is below the minimum.
  A limit order that would cross the book (a taker) is refused when post_only is set (the default:
  Bitpin has no post-only flag, so this is how the intent is kept) and otherwise when its limit
  price is worse than the mid by more than max_slippage. A resting limit order has no slippage.
State (HWM, halt flag, market / limit / cancel timestamps) lives in state_dir/risk_state_<mode>.json.
"""
import logging
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal

from .api import atomic_write_json, read_json
from .markets import (DEFAULT_MIN_ORDER_USDT, IRT, USDT, ZERO, D, best_ask, best_bid, fmt_amount, limit_crosses,
                      max_fill_within, mid_price, slippage_vs_mid, walk_book)

log = logging.getLogger("bitpin.risk")

KILL_SWITCH_FILE = "STOP"
DEFAULT_RISK = {
    "max_drawdown": 0.30,
    "drawdown_action": "halt",
    "hwm_window_days": 90,          # rolling high-water mark window; 0 = all-time (the pre-v3 behaviour)
    "max_orders_per_day": 50,
    "min_order_irt": 1000000,
    "min_order_usdt": float(DEFAULT_MIN_ORDER_USDT),
    "max_order_fraction": 1.0,
    "max_price_deviation": 0.05,
    "max_slippage": 0.01,
    "max_limit_orders_per_day": 120,
    "max_limit_distance": 0.5,
}


@dataclass
class Vet:
    ok: bool
    amount: Decimal = ZERO          # quote for buys (IRT or USDT), base units for sells; floored to precision
    notional: Decimal = ZERO        # in the market's QUOTE currency (IRT for COIN_IRT markets)
    est_avg_price: Decimal = None
    est_slippage: Decimal = None
    notes: list = field(default_factory=list)
    reason: str = None
    quote: str = IRT                # the market's quote currency
    notional_irt: Decimal = None    # notional in IRT (None when a USDT order came without quote_irt_rate)


@dataclass
class LimitVet:
    """Result of RiskManager.vet_limit(). price / base_amount are on the market's grid (the price is
    rounded in the safe direction for the side, the amount floored); notional = price * base_amount
    in the market's quote currency. crosses=True: at vet time the order would have traded at once."""
    ok: bool
    price: Decimal = None
    base_amount: Decimal = ZERO
    notional: Decimal = ZERO
    notional_irt: Decimal = None
    quote: str = IRT
    crosses: bool = False
    notes: list = field(default_factory=list)
    reason: str = None


class RiskManager:
    def __init__(self, config=None, state_dir="state", mode="paper", clock=time.time, persist=True):
        cfg = dict(DEFAULT_RISK)
        for k, v in (config or {}).items():
            if k.startswith("_"):
                continue
            if k not in DEFAULT_RISK:
                raise ValueError("unknown risk setting %r (known: %s)" % (k, sorted(DEFAULT_RISK)))
            cfg[k] = v
        if cfg["drawdown_action"] not in ("halt", "flatten"):
            raise ValueError("drawdown_action must be 'halt' or 'flatten'")
        self.cfg = cfg
        self.max_drawdown = D(cfg["max_drawdown"])
        self.drawdown_action = cfg["drawdown_action"]
        self.max_orders_per_day = int(cfg["max_orders_per_day"])
        self.min_order_irt = D(cfg["min_order_irt"])
        self.min_order_usdt = D(cfg["min_order_usdt"])
        self.max_order_fraction = D(cfg["max_order_fraction"])
        self.max_price_deviation = D(cfg["max_price_deviation"])
        self.max_slippage = D(cfg["max_slippage"])
        self.max_limit_orders_per_day = int(cfg["max_limit_orders_per_day"])
        self.max_limit_distance = D(cfg["max_limit_distance"])
        w = cfg["hwm_window_days"]
        wf = None
        if not isinstance(w, bool) and isinstance(w, (int, float, Decimal)):
            try:
                wf = float(w)       # load_config reads 90.0 as Decimal (read_json): a number all the same
            except (ArithmeticError, TypeError, ValueError):
                wf = None
        if wf is None or wf != wf or wf < 0 or wf > 3660:
            raise ValueError("hwm_window_days must be a number of days between 0 (all-time) and 3660, not %r" % (w,))
        self.hwm_window_days = wf
        if not (0 < self.max_drawdown <= 1 and 0 < self.max_order_fraction <= 1 and self.max_slippage > 0):
            raise ValueError("risk limits out of range")
        if not (0 < self.max_limit_distance < 1 and self.min_order_irt >= 0 and self.min_order_usdt >= 0
                and self.max_limit_orders_per_day >= 0):
            raise ValueError("risk limits out of range (max_limit_distance must be in (0, 1); minimums and "
                             "max_limit_orders_per_day must not be negative)")
        self.state_dir, self.mode, self.clock, self.persist = state_dir, mode, clock, persist
        self.path = os.path.join(state_dir, "risk_state_%s.json" % mode)
        self._st = read_json(self.path) or {}
        self._st.setdefault("hwm", None)
        self._st.setdefault("hwm_log", [])         # [[utc day number, that day's highest equity], ...]
        self._st.setdefault("halted", False)
        self._st.setdefault("halt_reason", None)
        self._st.setdefault("orders", [])          # market (taker) orders sent
        self._st.setdefault("limit_orders", [])    # limit orders placed
        self._st.setdefault("cancels", [])         # cancels requested (counted, never blocked)

    def _save(self):
        if self.persist:
            atomic_write_json(self.path, self._st)

    # ---- kill switch / halt
    @property
    def kill_switch_path(self):
        return os.path.join(self.state_dir, KILL_SWITCH_FILE)

    def kill_switch_active(self):
        return os.path.exists(self.kill_switch_path)

    def is_halted(self):
        return bool(self._st.get("halted"))

    def halt_reason(self):
        return self._st.get("halt_reason")

    def halt(self, reason):
        self._st["halted"] = True
        self._st["halt_reason"] = reason
        self._st["halted_at"] = self.clock()
        self._save()
        log.error("TRADING HALTED: %s", reason)

    @property
    def flattened(self):
        return bool(self._st.get("flattened"))

    @property
    def flatten_pending(self):
        return bool(self._st.get("flatten_pending"))

    def start_flatten(self):
        """Persist the intent BEFORE any flatten order: every later cycle retries until done."""
        self._st["flatten_pending"] = True
        self._st["flatten_started_at"] = self.clock()
        self._save()

    def finish_flatten(self):
        self._st["flatten_pending"] = False
        self._st["flattened"] = True
        self._st["flattened_at"] = self.clock()
        self._save()

    def mark_flattened(self):  # backwards compatible name
        self.finish_flatten()

    def reset(self):
        self._st = {"hwm": None, "hwm_log": [], "halted": False, "halt_reason": None, "orders": [],
                    "limit_orders": [], "cancels": [], "reset_at": self.clock()}
        self._save()

    # ---- drawdown
    def _stored_basis(self):
        b = self._st.get("basis")
        if b is None and self._st.get("hwm") is not None:
            return "account"  # state written before bases existed measured the whole account
        return b

    @staticmethod
    def _day(t):
        return int(float(t) // 86400)

    def _log_entries(self):
        """The stored daily samples as [(day, equity)], bad entries dropped (never raises)."""
        out = []
        for e in self._st.get("hwm_log") or []:
            try:
                d, v = int(e[0]), D(e[1])
            except (TypeError, ValueError, IndexError, ArithmeticError):
                continue
            if v > 0:
                out.append((d, v))
        return out

    def _rolling_hwm(self, now):
        """The high-water mark of the window ending at `now`: the highest daily sample of the last
        hwm_window_days (every sample with window 0). A legacy all-time "hwm" without any sample counts
        as a sample of hwm_at (or of today, so that it expires one window after the upgrade)."""
        entries = self._log_entries()
        if not entries and self._st.get("hwm") is not None:
            try:
                legacy = D(self._st["hwm"])
            except (TypeError, ValueError, ArithmeticError):
                legacy = ZERO
            if legacy > 0:
                at = self._st.get("hwm_at")
                day = self._day(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else self._day(now)
                entries = [(day, legacy)]
                self._st["hwm_log"] = [[day, str(legacy)]]
        if not entries:
            return ZERO
        if self.hwm_window_days > 0:
            first = self._day(now) - int(self.hwm_window_days) + 1
            entries = [e for e in entries if e[0] >= first]
        return max([v for _, v in entries] + [ZERO])

    def _hwm_for(self, basis, now=None):
        stored = self._stored_basis()
        if basis is not None and stored not in (None, basis):
            return ZERO, True
        return self._rolling_hwm(self.clock() if now is None else now), False

    def high_water_mark(self, basis=None):
        """Rolling high-water mark for `basis` (0 if none yet, or if the basis changed)."""
        return self._hwm_for(basis)[0]

    def peek_drawdown(self, equity, basis=None):
        """Same numbers as update_equity() but without any side effect."""
        equity = D(equity)
        hwm, _ = self._hwm_for(basis)
        hwm = max(hwm, equity)
        dd = (1 - equity / hwm) if hwm > 0 else ZERO
        return {"equity": equity, "hwm": hwm, "drawdown": dd, "breached": dd >= self.max_drawdown}

    def _record_sample(self, equity, now):
        """Keep today's highest equity in hwm_log and drop the samples outside the window (with
        window 0 everything is kept: at most one entry per day, a few hundred a year)."""
        day = self._day(now)
        entries = self._log_entries()
        today = max([v for d, v in entries if d == day] + [equity])
        entries = [e for e in entries if e[0] != day] + [(day, today)]
        if self.hwm_window_days > 0:
            first = day - int(self.hwm_window_days) + 1
            entries = [e for e in entries if e[0] >= first]
        entries.sort()
        self._st["hwm_log"] = [[d, str(v)] for d, v in entries]

    def update_equity(self, equity, basis=None):
        """Track the (rolling) high-water mark; halt when drawdown >= max_drawdown. Returns a dict.
        `basis` names what the equity measures (e.g. "sleeve:100000000" or "account"); when it
        changes the high-water mark restarts from the current equity. The state also carries the
        numbers a read-only reader (the Telegram notifier) shows: hwm, last_equity, drawdown,
        max_drawdown and hwm_window_days."""
        equity = D(equity)
        now = self.clock()
        hwm, rebased = self._hwm_for(basis, now)
        if rebased:
            log.warning("drawdown basis changed from %s to %s: high-water mark restarts at %s IRT",
                        self._stored_basis(), basis, fmt_amount(equity))
            self._st["hwm_log"] = []
            self._st["hwm"] = None
        if basis is not None:
            self._st["basis"] = basis
        if equity > 0:
            self._record_sample(equity, now)
        if equity > hwm:
            hwm = equity
        dd = (1 - equity / hwm) if hwm > 0 else ZERO
        self._st["hwm"] = hwm
        self._st["hwm_at"] = now
        self._st["last_equity"] = equity
        self._st["drawdown"] = float(dd)
        self._st["max_drawdown"] = float(self.max_drawdown)
        self._st["hwm_window_days"] = self.hwm_window_days
        breached = dd >= self.max_drawdown
        if breached and not self.is_halted():
            self.halt("drawdown %.1f%% from high-water mark %s IRT (limit %.0f%%)" % (
                dd * 100, fmt_amount(hwm), self.max_drawdown * 100))
        else:
            self._save()
        return {"equity": equity, "hwm": hwm, "drawdown": dd, "breached": breached}

    # ---- order counts (rolling 24 h). kind "market" = taker orders (the original budget,
    # max_orders_per_day), "limit" = limit placements (max_limit_orders_per_day), "cancel" = cancel
    # requests (counted, never limited).
    _COUNTERS = {"market": "orders", "limit": "limit_orders", "cancel": "cancels"}

    def _key(self, kind):
        try:
            return self._COUNTERS[kind]
        except KeyError:
            raise ValueError("order kind must be one of %s, not %r" % (sorted(self._COUNTERS), kind))

    def _recent(self, kind="market"):
        now = self.clock()
        return [t for t in (float(x) for x in self._st.get(self._key(kind)) or []) if now - t < 86400]

    def _recent_orders(self):
        return self._recent("market")

    def _limit_for(self, kind):
        if kind == "cancel":
            return None
        return self.max_limit_orders_per_day if kind == "limit" else self.max_orders_per_day

    def orders_last_24h(self, kind="market"):
        return len(self._recent(kind))

    def can_place_order(self, kind="market"):
        lim = self._limit_for(kind)
        return lim is None or self.orders_last_24h(kind) < lim

    def remaining_orders(self, kind="market"):
        lim = self._limit_for(kind)
        if lim is None:
            raise ValueError("cancels are not limited")
        return max(0, lim - self.orders_last_24h(kind))

    def record_order(self, kind="market"):
        """Count one order that reached the exchange: kind "market" (taker) or "limit" (a limit
        placement, also one that was answered with an unknown outcome), or one cancel request
        ("cancel", same as record_cancel())."""
        key = self._key(kind)
        o = self._recent(kind)
        o.append(self.clock())
        self._st[key] = o
        self._save()

    def record_cancel(self):
        self.record_order("cancel")

    def cancels_last_24h(self):
        return self.orders_last_24h("cancel")

    # ---- per-quote minimum / conversion
    def min_order_for(self, quote):
        """Minimum order notional for a market quoted in `quote`, in that currency."""
        q = str(quote).upper()
        if q == IRT:
            return self.min_order_irt
        if q == USDT:
            return self.min_order_usdt
        raise ValueError("no minimum order configured for %s-quoted markets (supported: IRT, USDT)" % q)

    def min_notional(self):
        """{quote: minimum} for markets.best_route(min_notional=...)."""
        return {IRT: self.min_order_irt, USDT: self.min_order_usdt}

    @staticmethod
    def _rate(market, quote_irt_rate):
        """IRT per unit of the market's quote currency (1 for IRT markets), or None when unknown."""
        if market.quote == IRT:
            return Decimal(1)
        if quote_irt_rate is None:
            return None
        r = D(quote_irt_rate)
        return r if r > 0 else None

    # ---- per-order checks
    def vet_order(self, side, market, amount, book, ref_close, equity, ignore_halt=False, flatten=False,
                  quote_irt_rate=None):
        """Check / resize one TAKER (market) order. `amount` = quote to spend (buy; IRT on COIN_IRT,
        USDT on COIN_USDT) or base units to sell (sell). `equity` is in IRT; for a market quoted in
        anything but IRT, `quote_irt_rate` (IRT per quote unit, e.g. the USDT_IRT mid) is required for
        the max_order_fraction cap. `ref_close` is the last candle close of THIS market (its quote).
        `flatten=True` (opt-in flatten-on-drawdown sells only) ignores the halt, the order count,
        the price-sanity check and max_order_fraction; the slippage guard and minimum still apply.
        `ignore_halt` is kept for backwards compatibility and means the same as flatten."""
        notes = []
        flatten = flatten or ignore_halt
        quote = market.quote

        def no(reason):
            return Vet(False, ZERO, ZERO, None, None, notes, reason, quote, None)

        if self.kill_switch_active():
            return no("kill switch file %s present" % self.kill_switch_path)
        if flatten and side != "sell":
            return no("flatten mode only sells")
        if self.is_halted() and not flatten:
            return no("trading halted: %s" % self.halt_reason())
        if not flatten and not self.can_place_order("market"):
            return no("max orders per 24h reached (%d)" % self.max_orders_per_day)
        if not market.is_trading:
            return no("market %s not tradable/suspended" % market.symbol)
        try:
            min_q = self.min_order_for(quote)
        except ValueError as e:
            return no(str(e))
        rate = self._rate(market, quote_irt_rate)
        if rate is None and not flatten:
            return no("%s is quoted in %s: quote_irt_rate (IRT per %s) is required to size the order against "
                      "equity" % (market.symbol, quote, quote))
        amount = D(amount)
        if amount <= 0:
            return no("zero amount")
        mid = mid_price(book)
        if mid is None:
            return no("order book empty on one side")
        if ref_close is not None and D(ref_close) > 0 and not flatten:
            dev = abs(mid / D(ref_close) - 1)
            if dev > self.max_price_deviation:
                return no("price sanity: book mid %s deviates %.2f%% from last close %s (limit %.1f%%)" % (
                    fmt_amount(mid), dev * 100, fmt_amount(ref_close), self.max_price_deviation * 100))
        if not flatten:
            cap = self.max_order_fraction * D(equity) / rate   # in the market's quote currency
            if side == "buy" and amount > cap:
                notes.append("max_order_fraction: %s -> %s %s" % (fmt_amount(amount), fmt_amount(cap), quote))
                amount = cap
            elif side == "sell" and amount * mid > cap:
                notes.append("max_order_fraction: %s -> %s %s" % (amount, cap / mid, market.base))
                amount = cap / mid

        def walk(a):
            if side == "buy":
                return walk_book(book, "buy", quote_amount=a, base_precision=market.base_amount_precision)
            return walk_book(book, "sell", base_amount=a)

        w = walk(amount)
        slip = slippage_vs_mid(book, side, w["avg_price"])
        if not w["complete"] or slip is None or slip > self.max_slippage:
            limit = mid * (1 + self.max_slippage) if side == "buy" else mid * (1 - self.max_slippage)
            fb, fq = max_fill_within(book, side, limit)
            new = min(amount, fq if side == "buy" else fb)
            notes.append("slippage guard: %s -> %s (est. slippage %s, visible book %s)" % (
                fmt_amount(amount), fmt_amount(new), "%.2f%%" % (slip * 100) if slip is not None else "n/a",
                "sufficient" if w["complete"] else "too thin"))
            amount = new
            if amount > 0:
                w = walk(amount)
                slip = slippage_vs_mid(book, side, w["avg_price"])
        amount = market.floor_quote(amount) if side == "buy" else market.floor_base(amount)
        notional = amount if side == "buy" else amount * mid
        n_irt = notional * rate if rate is not None else None
        if notional < min_q:
            return Vet(False, ZERO, notional, w["avg_price"], slip, notes,
                       "below minimum order: %s %s < %s %s" % (
                           fmt_amount(notional), quote, "min_order_%s" % quote.lower(), fmt_amount(min_q)),
                       quote, n_irt)
        return Vet(True, amount, notional, w["avg_price"], slip, notes, None, quote, n_irt)

    def vet_limit(self, side, market, price, base_amount, book, ref_close, equity, quote_irt_rate=None,
                  post_only=True):
        """Check / resize one LIMIT order before place_limit(). `price` in the market's quote,
        `base_amount` in base units, `equity` in IRT (non-IRT markets need `quote_irt_rate`),
        `ref_close` = last candle close of this market. Guards: kill switch, halt, the limit-order
        budget (max_limit_orders_per_day), tradable market, book-mid sanity vs the last close, the
        limit price vs the last close (a buy not above / a sell not below it by more than
        max_price_deviation), distance from the mid (<= max_limit_distance), crossing (refused with
        post_only; a crossing order without post_only is a taker and gets the slippage check on its
        limit price), max_order_fraction (shrinks base_amount), and the per-quote minimum."""
        notes = []
        quote = market.quote

        def no(reason, **kw):
            return LimitVet(False, kw.get("price"), ZERO, ZERO, None, quote, kw.get("crosses", False), notes, reason)

        if side not in ("buy", "sell"):
            return no("side must be buy or sell")
        if self.kill_switch_active():
            return no("kill switch file %s present" % self.kill_switch_path)
        if self.is_halted():
            return no("trading halted: %s" % self.halt_reason())
        if not self.can_place_order("limit"):
            return no("max limit orders per 24h reached (%d)" % self.max_limit_orders_per_day)
        if not market.is_trading:
            return no("market %s not tradable/suspended" % market.symbol)
        try:
            min_q = self.min_order_for(quote)
        except ValueError as e:
            return no(str(e))
        rate = self._rate(market, quote_irt_rate)
        if rate is None:
            return no("%s is quoted in %s: quote_irt_rate (IRT per %s) is required to size the order against "
                      "equity" % (market.symbol, quote, quote))
        try:
            price, base = D(price), D(base_amount)
        except ValueError as e:
            return no(str(e))
        if price <= 0 or base <= 0:
            return no("price and amount must be positive")
        price = market.limit_price(price, side)
        if price <= 0:
            return no("price rounds to zero on the %s price grid" % market.symbol)
        mid = mid_price(book)
        if mid is None:
            return no("order book empty on one side", price=price)
        ref = D(ref_close) if ref_close is not None else None
        if ref is not None and ref > 0:
            dev = abs(mid / ref - 1)
            if dev > self.max_price_deviation:
                return no("price sanity: book mid %s deviates %.2f%% from last close %s (limit %.1f%%)" % (
                    fmt_amount(mid), dev * 100, fmt_amount(ref), self.max_price_deviation * 100), price=price)
            if side == "buy" and price > ref * (1 + self.max_price_deviation):
                return no("limit price %s is %.2f%% ABOVE the last close %s (a buy may be at most %.1f%% above)" % (
                    fmt_amount(price), (price / ref - 1) * 100, fmt_amount(ref), self.max_price_deviation * 100),
                    price=price)
            if side == "sell" and price < ref * (1 - self.max_price_deviation):
                return no("limit price %s is %.2f%% BELOW the last close %s (a sell may be at most %.1f%% below)" % (
                    fmt_amount(price), (1 - price / ref) * 100, fmt_amount(ref), self.max_price_deviation * 100),
                    price=price)
        dist = abs(price / mid - 1)
        if dist > self.max_limit_distance:
            return no("limit price %s is %.1f%% away from the book mid %s (max_limit_distance %.0f%%): wrong unit or "
                      "quote currency?" % (fmt_amount(price), dist * 100, fmt_amount(mid),
                                           self.max_limit_distance * 100), price=price)
        crosses = limit_crosses(book, side, price)
        if crosses:
            best = best_ask(book) if side == "buy" else best_bid(book)
            if post_only:
                return no("would cross the book (post-only intent): %s limit %s vs best %s %s" % (
                    side, fmt_amount(price), "ask" if side == "buy" else "bid", fmt_amount(best)),
                    price=price, crosses=True)
            worst = (price / mid - 1) if side == "buy" else (1 - price / mid)
            if worst > self.max_slippage:
                return no("slippage guard: crossing %s limit %s is %.2f%% worse than the mid %s (max_slippage %.1f%%)"
                          % (side, fmt_amount(price), worst * 100, fmt_amount(mid), self.max_slippage * 100),
                          price=price, crosses=True)
            notes.append("crosses the book: executes (partly) as a taker at up to %s" % fmt_amount(price))
        cap = self.max_order_fraction * D(equity) / rate          # quote currency
        if price * base > cap:
            new = cap / price
            notes.append("max_order_fraction: %s -> %s %s" % (base, new, market.base))
            base = new
        base = market.floor_base(base)
        notional = price * base
        if notional < min_q:
            return LimitVet(False, price, ZERO, notional, notional * rate, quote, crosses, notes,
                            "below minimum order: %s %s < %s %s" % (
                                fmt_amount(notional), quote, "min_order_%s" % quote.lower(), fmt_amount(min_q)))
        return LimitVet(True, price, base, notional, notional * rate, quote, crosses, notes, None)
