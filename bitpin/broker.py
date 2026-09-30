"""Brokers: PaperBroker (simulated fills against the LIVE public order book) and LiveBroker
(real orders through BitpinClient).

Common interface
----------------
  balances()                 -> {asset: Decimal total incl. frozen}   ("IRT" = toman cash)
  available()                -> {asset: Decimal available}   (frozen funds, e.g. under resting
                                limit orders, are NOT available)
  order_book(symbol)         -> {"asks": [(price, amount)...], "bids": [...]} (Decimal)
  portfolio_value(prices)    -> Decimal IRT value of priced assets
  market_buy(symbol, quote_amount)  -> fill   (quote = IRT on COIN_IRT, USDT on COIN_USDT)
  market_sell(symbol, base_amount)  -> fill
  refresh()                  re-sync balances from the source of truth
  resolve_pending(read_only=False) -> list of unresolved MARKET orders (live only; the runner must
                             not trade while non-empty). read_only=True never cancels and never
                             writes. Resting limit orders are never in this list and never cancelled
                             by it: they are handled by sync_limits().
  drain_late_fills()         -> fill deltas of earlier market orders that completed after being
                             reported (informational: the runner books fills from fill_records())
  fill_records()             -> one record per order the broker remembers (market AND limit), with its
                             latest known CUMULATIVE fill {identifier, symbol, side, base, quote, fee,
                             fee_asset, kind, quote_asset} (base None = no fill data yet). This is the
                             durable source the runner's sleeve ledger is reconciled against, so a fill
                             - also every partial fill of a resting limit order - is booked exactly once
                             even if the process dies between the fill and the booking.
  unresolved_count()         -> number of MARKET orders whose outcome is not known yet
  prepare()                  called before an order's pre-trade checks (live: renews a due token)
  expire_unknown_orders      True: an unknown order that never appears is eventually treated as
                             not placed. The runner sets False when it trades a capped sleeve.
  best_route(from_asset, to_asset, amount, min_notional=None)
                             -> cheapest taker route (markets.best_route): USDT <-> COIN direct on
                             COIN_USDT or via IRT; IRT <-> X single leg.

Resting limit orders (both brokers)
-----------------------------------
  place_limit(symbol, side, price, base_amount, identifier=None, post_only_intent=True, book=None,
              tag=None, meta=None) -> limit view (below)
      The price is put on the market's grid in the safe direction (buy down, sell up), the amount
      floored. Bitpin has no post-only flag: with post_only_intent the order is checked against the
      live best bid/ask first (a fresh book unless `book` is given) and LimitWouldCross is raised -
      nothing sent - when it would trade at once. Funds are checked against AVAILABLE balances (the
      buy locks price*amount of the quote, the sell locks the amount of the base).
      Idempotent by identifier: an identifier already in the journal is never sent again; the
      existing order's view is returned (only an order that was definitely NOT accepted -
      'not_sent' - may be re-sent under the same identifier). Same identifier = same order: to change
      price or size, cancel it and place a new one with a new identifier. The returned view has an
      extra key `sent`: True when THIS call sent the order (count it with
      RiskManager.record_order("limit")), False for an idempotent repeat. Raises like the market path:
      OrderStatusUnknown (it may exist: the journal has it as 'unknown', sync_limits finds it; count it
      as sent), OrderNotSent / BitpinAPIError 4xx (not placed), BrokerError subclasses (refused
      locally, nothing sent: LimitWouldCross, InsufficientFunds, OrderTooSmall, MarketNotTradable).
  cancel(order)              order = identifier or a view. Only orders this bot placed (in its
                             journal / paper state); anything else raises NotBotOrder. Returns the
                             view after the cancel: 'cancelled' (possibly with a partial fill),
                             'filled' when it filled while we cancelled, 'partially_filled'/'open'
                             when the exchange has not closed it yet (the cancel request is kept
                             and re-sent by sync_limits), 'unknown' when it could not be found.
  cancel_all(symbol=None, tag=None) -> [views]    every active bot limit order (never foreign ones)
  list_open(symbol=None)     -> [views] of the bot's limit orders that are OPEN ON THE EXCHANGE
                             (live: one GET of the open orders, filtered to the bot's identifiers;
                             the journal is updated from those rows)
  sync_limits(read_only=False) -> {"open": [views], "unresolved": [views], "changed": [views],
                             "errors": [str], "foreign_open": int|None}
                             Call every cycle (also right after a restart): the exchange is the
                             source of truth for the bot's orders; every journaled limit order that
                             is not final is refreshed (fills, state), unknown submissions are found
                             by identifier, pending cancel requests are re-sent. Orders the bot did
                             not place are only counted (foreign_open), never touched.
  limit_orders(symbol=None, tag=None, active_only=False) -> [views]  (from the journal / paper
                             state, including 'unknown' ones)
  limit_fill_events()        -> fill deltas of limit orders not acknowledged yet (non-destructive)
  ack_limit_fills(events)    -> persist that these deltas were handled; the next limit_fill_events()
                             no longer returns them (at-least-once delivery: handle, then ack)
  locked_by_limits()         -> {asset: amount} locked by the bot's own active limit orders (to tell
                             them apart from funds frozen by orders the bot did not place)

  A limit view is {identifier, order_id, symbol, side, type "limit", price, base_amount,
  filled_base, filled_quote, fee, fee_asset, avg_price, remaining_base, state, final, tag, meta,
  created_at, updated_at, cancel_requested, error, exchange_state}; state is one of
  open | partially_filled | filled | cancelled | unknown | rejected (final: filled, cancelled,
  rejected). filled_* are cumulative and gross; the fee is in the received asset.

A fill is a dict: {symbol, side, base, quote, avg_price, fee, fee_asset, order_id, identifier,
requested, partial, status}. `base`/`quote` are the GROSS traded amounts (cumulative for that
order); the commission (`fee`) is charged in the RECEIVED asset (buy: base asset, sell: quote
asset), as on Bitpin.

PaperBroker limit-fill model (documented simplification)
--------------------------------------------------------
  * A resting buy at P fills (as MAKER, at P, maker fee 0.30% in the received asset) when
    (a) the live order book at a sync shows asks at or below P: the visible quantity there is
        filled (a PARTIAL fill when it is smaller than the remainder; realistic, because a seller
        who crossed our bid would have been matched with it), or
    (b) the market TRADED at or below P * (1 - fill_through) since the order was placed: the
        whole remainder fills. Trades are read from candles (default 1-minute bars of the public
        TradingView endpoint; only bars that OPENED at or after the placement time and have
        volume > 0 count, so trades before the order existed never fill it; bars without trades
        are ignored).
    Sells mirror this (bids at or above P; a traded high at or above P * (1 + fill_through)).
  * No queue position: touching the price is enough (fill_through 0, the default). Set
    fill_through e.g. 0.005 to require a trade-through of 0.5%.
  * A limit placed WITHOUT post_only that crosses the book executes the crossing part at once as a
    TAKER (walk of the levels within the limit, taker fee); the rest rests.
  * cancel() first applies any fill that happened before it (the paper version of the cancel
    race), then releases the locked remainder.
  * Market orders are unchanged (walk of the live book, taker fee).

Audit logging (the trade CSV) never aborts trading: if the CSV cannot be written (e.g. it is open in
Excel, which blocks other writers on Windows) the row goes to a fallback CSV, else to the log, and
the fill is still returned to the runner. Limit fills are logged as deltas (note "limit fill").
"""
import csv
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from .api import (OPEN_ORDER_STATES, AuthBudgetExceeded, AuthError, BitpinAPIError, BitpinError, OrderNotSent,
                  OrderStatusUnknown, atomic_write_json, read_json)
from .markets import (DEFAULT_MAKER_FEE, DEFAULT_MIN_ORDER_USDT, DEFAULT_TAKER_FEE, IRT, USDT, ZERO, D, best_ask,
                      best_bid, best_route, ceil_to_precision, floor_to_precision, fmt_amount, limit_crosses,
                      parse_book, parse_symbol, walk_book, walk_limit)

log = logging.getLogger("bitpin.broker")

TRADE_LOG_FIELDS = ["time_utc", "mode", "symbol", "side", "requested", "base", "quote", "avg_price", "fee",
                    "fee_asset", "order_id", "partial", "status", "note"]

# normalised states of a limit order (see the module docstring)
LIMIT_OPEN = "open"
LIMIT_PARTIAL = "partially_filled"
LIMIT_FILLED = "filled"
LIMIT_CANCELLED = "cancelled"
LIMIT_UNKNOWN = "unknown"
LIMIT_REJECTED = "rejected"
LIMIT_STATES = (LIMIT_OPEN, LIMIT_PARTIAL, LIMIT_FILLED, LIMIT_CANCELLED, LIMIT_UNKNOWN, LIMIT_REJECTED)
LIMIT_FINAL_STATES = (LIMIT_FILLED, LIMIT_CANCELLED, LIMIT_REJECTED)
LIMIT_ACTIVE_STATES = (LIMIT_OPEN, LIMIT_PARTIAL, LIMIT_UNKNOWN)
# journal status of a limit order that is on the book (not "open": that status means "market order
# of unresolved outcome" to resolve_pending, the runner and the Telegram notifier)
RESTING = "resting"


class BrokerError(Exception):
    pass


class InsufficientFunds(BrokerError):
    pass


class OrderTooSmall(BrokerError):
    pass


class MarketNotTradable(BrokerError):
    pass


class WalletDataError(BrokerError):
    """The wallet response looks incomplete (toman row missing, or a tracked asset that had a
    balance vanished without a bot order). Never plan or trade on it; retry later."""


class LimitWouldCross(BrokerError):
    """A limit order with a post-only intent would trade immediately (a buy at or above the best
    ask, a sell at or below the best bid). Nothing was sent; the caller skips it."""

    def __init__(self, symbol, side, price, best):
        self.symbol, self.side, self.price, self.best = symbol, side, price, best
        super().__init__("%s %s limit %s would cross the book (best %s %s): not placed (post-only intent)" % (
            symbol, side, price, "ask" if side == "buy" else "bid", best))


class NotBotOrder(BrokerError):
    """The order is not a limit order this bot placed: it is never touched."""


def utc_iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def append_trade_log(path, mode, fill, note="", clock=time.time):
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    row = {k: fill.get(k, "") for k in TRADE_LOG_FIELDS}
    row.update({"time_utc": utc_iso(clock()), "mode": mode, "note": note or fill.get("note", "")})
    row = {k: (format(v, "f") if isinstance(v, Decimal) else v) for k, v in row.items()}
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def fallback_log_path(path):
    root, ext = os.path.splitext(path)
    return "%s_fallback%s" % (root, ext or ".csv")


def safe_trade_log(path, mode, fill, note="", clock=time.time):
    """append_trade_log that never raises: an executed order must never be 'lost' because the
    audit CSV is locked (Excel) or the disk is full. Returns the path written, or None."""
    for p in (path, fallback_log_path(path)):
        try:
            append_trade_log(p, mode, fill, note, clock)
            if p != path:
                log.error("trade log %s is not writable; fill recorded in %s instead", path, p)
            return p
        except Exception as e:  # noqa: BLE001 - audit logging must never abort trading
            log.error("cannot write trade log %s: %s", p, e)
    log.error("FILL (not in any trade CSV): %s", {k: (format(v, "f") if isinstance(v, Decimal) else v)
                                                   for k, v in fill.items()})
    return None


def make_fill(symbol, side, base, quote, fee, fee_asset, order_id, requested, partial, status="filled", note="",
              identifier=None):
    base, quote = D(base), D(quote)
    return {"symbol": symbol, "side": side, "base": base, "quote": quote,
            "avg_price": (quote / base).quantize(Decimal("1e-10")) if base > 0 else None, "fee": D(fee),
            "fee_asset": fee_asset,
            "order_id": order_id, "identifier": identifier, "requested": D(requested), "partial": bool(partial),
            "status": status, "note": note}


def order_is_final(order):
    st = order.get("state")
    return st is not None and st not in OPEN_ORDER_STATES


def fee_asset_for(symbol, side):
    base, quote = parse_symbol(symbol)
    return base if side == "buy" else quote


def fill_from_order(symbol, side, o, requested, identifier=None):
    """Fill dict from an exchange order row (cumulative dealt amounts). `symbol`/`side` are the
    journal's (what the bot sent), never taken from the row."""
    base = D(o.get("dealed_base_amount") or 0)
    quote = D(o.get("dealed_quote_amount") or 0)
    fee = D(o.get("commission") or 0)
    done = quote if side == "buy" else base
    partial = done < D(requested) * D("0.99")
    final = order_is_final(o)
    status = "open" if not final else ("partial" if partial else "filled")
    if base <= 0:
        status = "unfilled" if final else "open"
    return make_fill(symbol, side, base, quote, fee, fee_asset_for(symbol, side), o.get("id"), requested, partial,
                     status, "state=%s" % o.get("state"), identifier=identifier)


def fill_record(identifier, symbol, side, base=None, quote=None, fee=None, fee_asset=None, **extra):
    """Normalised fill_records() row; base None = the order is known but has no fill data yet."""
    rec = {"identifier": identifier, "symbol": symbol, "side": side, "base": None, "quote": None, "fee": None,
           "fee_asset": fee_asset}
    if base is not None:
        rec.update(base=D(base), quote=D(quote or 0), fee=D(fee or 0),
                   fee_asset=fee_asset or fee_asset_for(symbol, side))
    try:
        rec["quote_asset"] = parse_symbol(symbol)[1]
    except (ValueError, TypeError):
        rec["quote_asset"] = None
    rec.update(extra)
    return rec


def new_identifier():
    """A fresh client order identifier (uuid4, the format the bot has always sent to Bitpin)."""
    return str(uuid.uuid4())


def _d(x):
    """Decimal of a stored number; None / '' -> 0."""
    if x is None or x == "":
        return ZERO
    return D(x)


def _ident_of(order):
    if isinstance(order, dict):
        order = order.get("identifier")
    if not order:
        raise NotBotOrder("no identifier given")
    return str(order)


def limit_state_of(e):
    """Normalised state of a limit-order entry (journal or paper state)."""
    st = e.get("status")
    if st in ("submitting", "unknown"):
        return LIMIT_UNKNOWN
    if st in ("rejected", "not_sent", "not_found"):
        return LIMIT_REJECTED
    ls = e.get("limit_state")
    if ls in LIMIT_STATES:
        return ls
    fb = _d(e.get("reported_base"))
    if st == RESTING:
        return LIMIT_PARTIAL if fb > 0 else LIMIT_OPEN
    if st == "closed":
        return LIMIT_FILLED if fb >= _d(e.get("base_amount")) > 0 else LIMIT_CANCELLED
    return LIMIT_UNKNOWN


def limit_view(ident, e):
    """The normalised view of one limit-order entry (see the module docstring)."""
    sym, side = e.get("symbol"), e.get("side")
    amount = _d(e.get("base_amount"))
    fb, fq, fee = _d(e.get("reported_base")), _d(e.get("reported_quote")), _d(e.get("reported_fee"))
    state = limit_state_of(e)
    final = state in LIMIT_FINAL_STATES
    try:
        fa = e.get("reported_fee_asset") or fee_asset_for(sym, side)
    except (ValueError, TypeError):
        fa = None
    return {"identifier": ident, "order_id": e.get("order_id"), "symbol": sym, "side": side, "type": "limit",
            "price": _d(e.get("price")), "base_amount": amount, "filled_base": fb, "filled_quote": fq, "fee": fee,
            "fee_asset": fa, "avg_price": (fq / fb) if fb > 0 else None,
            "remaining_base": ZERO if final else max(ZERO, amount - fb), "state": state, "final": final,
            "tag": e.get("tag"), "meta": e.get("meta"), "created_at": e.get("created_at"),
            "updated_at": e.get("updated_at"), "cancel_requested": bool(e.get("cancel_requested")),
            "error": e.get("error"), "exchange_state": e.get("state")}


def _limit_events(items):
    """Unacknowledged fill deltas of limit-order entries [(identifier, entry), ...]."""
    out = []
    for ident, e in items:
        if e.get("kind") != "limit":
            continue
        cb, cq, cf = _d(e.get("reported_base")), _d(e.get("reported_quote")), _d(e.get("reported_fee"))
        ab, aq, af = _d(e.get("acked_base")), _d(e.get("acked_quote")), _d(e.get("acked_fee"))
        if cb <= ab and cq <= aq and cf <= af:
            continue
        v = limit_view(ident, e)
        db, dq, df = cb - ab, cq - aq, cf - af
        out.append({"identifier": ident, "order_id": v["order_id"], "symbol": v["symbol"], "side": v["side"],
                    "tag": v["tag"], "meta": v["meta"], "price": v["price"], "base": db, "quote": dq, "fee": df,
                    "fee_asset": v["fee_asset"], "avg_price": (dq / db) if db > 0 else None, "cum_base": cb,
                    "cum_quote": cq, "cum_fee": cf, "state": v["state"], "final": v["final"],
                    "updated_at": e.get("updated_at")})
    out.sort(key=lambda x: float(x.get("updated_at") or 0))
    return out


def _acked_fields(e, ev):
    """Acknowledged cumulative amounts after handling event `ev` (never moves backwards)."""
    return {"acked_base": max(_d(e.get("acked_base")), D(ev["cum_base"])),
            "acked_quote": max(_d(e.get("acked_quote")), D(ev["cum_quote"])),
            "acked_fee": max(_d(e.get("acked_fee")), D(ev["cum_fee"]))}


def _unacked(e):
    return e.get("kind") == "limit" and _d(e.get("reported_base")) > _d(e.get("acked_base"))


def bars_range_source(res="1", fetch=None):
    """range_source for PaperBroker from the public candle endpoint (bitpin.data.fetch_bars):
    (symbol, start_ts, until_ts) -> (low, high) over the bars that OPENED at or after start_ts and
    had trades (volume > 0), or None when there were none."""
    def src(symbol, start_ts, until_ts):
        from . import data as data_mod   # imported late: data has no bitpin imports, no cycle
        f = fetch or data_mod.fetch_bars
        bars = f(symbol, res, int(start_ts), int(until_ts))
        lo = hi = None
        for b in bars:
            if b.ts < start_ts or not b.volume or b.volume <= 0:
                continue
            bl, bh = D(b.low), D(b.high)
            lo = bl if lo is None else min(lo, bl)
            hi = bh if hi is None else max(hi, bh)
        return (lo, hi) if lo is not None else None
    src.res_seconds = {"1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600, "240": 14400}.get(str(res), 60)
    return src


class Broker:
    mode = "base"
    quote_asset = "IRT"
    expire_unknown_orders = True
    taker_fee_rate = DEFAULT_TAKER_FEE

    def fill_records(self):
        return []

    def unresolved_count(self):
        return 0

    def prepare(self):
        pass

    def balances(self):
        raise NotImplementedError

    def available(self):
        raise NotImplementedError

    def order_book(self, symbol):
        raise NotImplementedError

    def market_buy(self, symbol, quote_amount, ref_price=None):
        raise NotImplementedError

    def market_sell(self, symbol, base_amount, ref_price=None):
        raise NotImplementedError

    def refresh(self):
        pass

    def track(self, assets):
        pass

    def resolve_pending(self, read_only=False):
        return []

    def drain_late_fills(self):
        return []

    # ---- limit orders (implemented by PaperBroker and LiveBroker)
    def place_limit(self, symbol, side, price, base_amount, identifier=None, post_only_intent=True, book=None,
                    tag=None, meta=None):
        raise NotImplementedError

    def cancel(self, order, wait=True):
        raise NotImplementedError

    def cancel_all(self, symbol=None, tag=None, wait=True):
        """Cancel every active limit order of the bot (optionally one market / one tag). Orders the
        bot did not place are never touched. Returns the views after the cancels."""
        out = []
        for v in self.limit_orders(symbol=symbol, tag=tag, active_only=True):
            out.append(self.cancel(v["identifier"], wait=wait))
        return out

    def list_open(self, symbol=None):
        return []

    def sync_limits(self, read_only=False):
        return {"open": [], "unresolved": [], "changed": [], "errors": [], "foreign_open": None}

    def limit_orders(self, symbol=None, tag=None, active_only=False):
        return []

    def limit_fill_events(self):
        return []

    def ack_limit_fills(self, events):
        pass

    def locked_by_limits(self):
        """{asset: amount} locked by the bot's OWN active limit orders (a buy locks remaining * price
        of the quote, a sell the remaining base; an order of unknown outcome is counted, as it may
        exist). Lets the caller tell its own resting orders apart from funds frozen by orders it did
        not place."""
        out = {}
        for v in self.limit_orders(active_only=True):
            base, quote = parse_symbol(v["symbol"])
            asset, amt = (quote, v["remaining_base"] * v["price"]) if v["side"] == "buy" else (base, v["remaining_base"])
            if amt > 0:
                out[asset] = out.get(asset, ZERO) + amt
        return out

    @staticmethod
    def _select(items, symbol=None, tag=None, active_only=False):
        out = []
        for ident, e in items:
            if e.get("kind") != "limit":
                continue
            if symbol is not None and e.get("symbol") != str(symbol).upper():
                continue
            if tag is not None and e.get("tag") != tag:
                continue
            v = limit_view(ident, e)
            if active_only and v["final"]:
                continue
            out.append(v)
        out.sort(key=lambda v: float(v.get("created_at") or 0))
        return out

    # ---- routing
    def default_min_notional(self):
        return None

    def best_route(self, from_asset, to_asset, amount, min_notional="default", taker_fee=None):
        """markets.best_route on this broker's books and markets (taker fee: the broker's)."""
        mins = self.default_min_notional() if min_notional == "default" else min_notional
        return best_route(from_asset, to_asset, amount, self.order_book, self.markets,
                          self.taker_fee_rate if taker_fee is None else taker_fee, mins)

    def portfolio_value(self, prices):
        """IRT value of all balances; `prices` maps asset ("BTC") or symbol ("BTC_IRT") -> IRT price.
        Unpriced assets are ignored."""
        total = ZERO
        for asset, amt in self.balances().items():
            if not amt:
                continue
            if asset == self.quote_asset:
                total += amt
                continue
            p = prices.get(asset)
            if p is None:
                p = prices.get("%s_%s" % (asset, self.quote_asset))
            if p is not None:
                total += amt * D(p)
        return total


# --------------------------------------------------------------------------- paper

class PaperBroker(Broker):
    """Simulated account. Market orders fill by walking the live public order book (asks for buys,
    bids for sells), taker fee in the received asset, partial fills when the book is too thin.
    Resting limit orders follow the model in the module docstring (maker fee, funds locked while
    they rest). State is persisted atomically to state_dir/paper_state.json; every fill is appended
    to state_dir/paper_trades.csv. With persist=False (dry runs) nothing is ever written: a missing
    account is created in memory only."""

    mode = "paper"
    STATE_FILE = "paper_state.json"
    TRADES_FILE = "paper_trades.csv"
    KEEP_ORDERS = 1000

    def __init__(self, markets, state_dir, capital_irt=None, fee_rate="0.0035", book_source=None, client=None,
                 clock=time.time, persist=True, maker_fee_rate=DEFAULT_MAKER_FEE, range_source=None,
                 candle_res="1", fill_through="0"):
        self.markets = markets
        self.fee_rate = D(fee_rate)
        self.taker_fee_rate = self.fee_rate
        self.maker_fee_rate = D(maker_fee_rate)
        self.fill_through = D(fill_through)
        if not (ZERO <= self.fill_through < 1):
            raise ValueError("fill_through must be in [0, 1)")
        self.clock = clock
        self.persist = persist
        if book_source is None:
            if client is None:
                raise ValueError("PaperBroker needs a book_source or a client for the public order book")
            book_source = client.orderbook
        self._book_source = book_source
        if range_source is None and client is not None:
            range_source = bars_range_source(candle_res)
        self._range_source = range_source
        self._range_step = int(getattr(range_source, "res_seconds", 60)) if range_source is not None else 60
        if persist:
            os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, self.STATE_FILE)
        self.trades_path = os.path.join(state_dir, self.TRADES_FILE)
        st = read_json(self.path)
        self._orders = {}
        self._limits = {}
        self._frozen = {}
        if st:
            self._bal = {a: D(v) for a, v in st.get("balances", {}).items()}
            self._frozen = {a: D(v) for a, v in (st.get("frozen") or {}).items() if D(v) != 0}
            self._seq = int(st.get("seq", 0))
            self._orders = dict(st.get("orders") or {})
            self._limits = dict(st.get("limits") or {})
            self._initial = D(st.get("initial_capital_irt", 0))
            self._created = st.get("created_utc")
            if capital_irt is not None and D(capital_irt) != self._initial:
                log.warning("existing paper account in %s kept (initial capital %s IRT); --capital-irt %s ignored. "
                            "Use --reset-paper to start over.", self.path, fmt_amount(self._initial),
                            fmt_amount(capital_irt))
        else:
            if capital_irt is None:
                raise BrokerError("no paper account in %s yet: pass --capital-irt" % state_dir)
            cap = D(capital_irt)
            if cap <= 0:
                raise BrokerError("capital must be positive")
            self._bal = {self.quote_asset: cap}
            self._seq = 0
            self._initial = cap
            self._created = utc_iso(clock())
            self._save()
            if persist:
                log.info("new paper account with %s IRT in %s", fmt_amount(cap), self.path)
            else:
                log.info("dry run: in-memory paper account with %s IRT (nothing saved)", fmt_amount(cap))

    def _save(self):
        if not self.persist:
            return
        self._prune_limits()
        atomic_write_json(self.path, {
            "version": 1, "mode": "paper", "created_utc": self._created, "updated_utc": utc_iso(self.clock()),
            "initial_capital_irt": self._initial, "seq": self._seq,
            "balances": {a: v for a, v in sorted(self._bal.items())},
            "frozen": {a: v for a, v in sorted(self._frozen.items()) if v != 0},
            "orders": self._orders,
            "limits": self._limits,
        })

    def _remember(self, fill):
        """Keep the fill in paper_state.json (saved atomically WITH the balances), so the runner can
        reconcile its sleeve ledger even if it is interrupted before booking the fill."""
        self._orders[fill["identifier"]] = {"seq": self._seq, "symbol": fill["symbol"], "side": fill["side"],
                                            "base": fill["base"], "quote": fill["quote"], "fee": fill["fee"],
                                            "fee_asset": fill["fee_asset"]}
        if len(self._orders) > self.KEEP_ORDERS:
            for k, _ in sorted(self._orders.items(), key=lambda kv: int(kv[1].get("seq", 0)))[
                    : len(self._orders) - self.KEEP_ORDERS]:
                del self._orders[k]

    def _prune_limits(self):
        if len(self._limits) <= self.KEEP_ORDERS:
            return
        done = sorted((float(e.get("created_at") or 0), k) for k, e in self._limits.items()
                      if limit_state_of(e) in LIMIT_FINAL_STATES and not _unacked(e))
        for _, k in done[: len(self._limits) - self.KEEP_ORDERS]:
            del self._limits[k]

    def fill_records(self):
        out = [fill_record(k, e.get("symbol"), e.get("side"), e.get("base"), e.get("quote"), e.get("fee"),
                           e.get("fee_asset"), kind="market") for k, e in self._orders.items()]
        for k, e in self._limits.items():
            out.append(fill_record(k, e.get("symbol"), e.get("side"), _d(e.get("reported_base")),
                                   _d(e.get("reported_quote")), _d(e.get("reported_fee")),
                                   e.get("reported_fee_asset"), kind="limit", status=e.get("status")))
        return out

    def _log_fill(self, fill, note=""):
        if self.persist:
            safe_trade_log(self.trades_path, self.mode, fill, note=note, clock=self.clock)

    def balances(self):
        out = dict(self._bal)
        for a, v in self._frozen.items():
            if v:
                out[a] = out.get(a, ZERO) + v
        return out

    def available(self):
        return dict(self._bal)

    def frozen(self):
        return {a: v for a, v in self._frozen.items() if v}

    def locked_by_limits(self):
        return self.frozen()      # exact: only the bot's own paper limit orders lock paper funds

    def order_book(self, symbol):
        return parse_book(self._book_source(symbol))

    def _market(self, symbol):
        m = self.markets.get(symbol)
        if not m.is_trading:
            raise MarketNotTradable("%s is not tradable (tradable=%s suspended=%s)" % (symbol, m.tradable, m.suspended))
        return m

    def _next_id(self):
        self._seq += 1
        return "paper-%d" % self._seq

    def market_buy(self, symbol, quote_amount, ref_price=None, book=None):
        m = self._market(symbol)
        q = m.floor_quote(D(quote_amount))
        if q <= 0:
            raise OrderTooSmall("buy amount rounds to zero")
        if q > self._bal.get(m.quote, ZERO):
            raise InsufficientFunds("paper: need %s %s, have %s" % (q, m.quote, self._bal.get(m.quote, ZERO)))
        book = book or self.order_book(symbol)
        w = walk_book(book, "buy", quote_amount=q, base_precision=m.base_amount_precision)
        gross = w["base"]
        if gross <= 0:
            fill = make_fill(symbol, "buy", 0, 0, 0, m.base, None, q, True, "unfilled", "no ask liquidity")
            self._log_fill(fill)
            return fill
        spent = min(ceil_to_precision(w["quote"], m.quote_amount_precision), q)
        fee = min(ceil_to_precision(gross * self.fee_rate, m.base_amount_precision), gross)
        self._bal[m.quote] = self._bal.get(m.quote, ZERO) - spent
        self._bal[m.base] = self._bal.get(m.base, ZERO) + (gross - fee)
        oid = self._next_id()
        partial = not w["complete"]
        fill = make_fill(symbol, "buy", gross, spent, fee, m.base, oid, q, partial,
                         "partial" if partial else "filled", "book exhausted" if partial else "", identifier=oid)
        self._remember(fill)
        self._save()
        self._log_fill(fill)
        return fill

    def market_sell(self, symbol, base_amount, ref_price=None, book=None):
        m = self._market(symbol)
        b = m.floor_base(D(base_amount))
        if b <= 0:
            raise OrderTooSmall("sell amount rounds to zero")
        if b > self._bal.get(m.base, ZERO):
            raise InsufficientFunds("paper: need %s %s, have %s" % (b, m.base, self._bal.get(m.base, ZERO)))
        book = book or self.order_book(symbol)
        w = walk_book(book, "sell", base_amount=b)
        sold = w["base"]
        if sold <= 0:
            fill = make_fill(symbol, "sell", 0, 0, 0, m.quote, None, b, True, "unfilled", "no bid liquidity")
            self._log_fill(fill)
            return fill
        gross_q = floor_to_precision(w["quote"], m.quote_amount_precision)
        fee = min(ceil_to_precision(gross_q * self.fee_rate, m.quote_amount_precision), gross_q)
        self._bal[m.base] = self._bal.get(m.base, ZERO) - sold
        self._bal[m.quote] = self._bal.get(m.quote, ZERO) + (gross_q - fee)
        oid = self._next_id()
        partial = not w["complete"]
        fill = make_fill(symbol, "sell", sold, gross_q, fee, m.quote, oid, b, partial,
                         "partial" if partial else "filled", "book exhausted" if partial else "", identifier=oid)
        self._remember(fill)
        self._save()
        self._log_fill(fill)
        return fill

    # ---- resting limit orders (paper simulation, see the module docstring)
    def place_limit(self, symbol, side, price, base_amount, identifier=None, post_only_intent=True, book=None,
                    tag=None, meta=None):
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        m = self._market(symbol)
        ident = str(identifier) if identifier else new_identifier()
        existing = self._limits.get(ident)
        if existing is not None:
            if existing.get("symbol") != m.symbol or existing.get("side") != side:
                raise BrokerError("identifier %s already belongs to a %s %s order" % (
                    ident, existing.get("side"), existing.get("symbol")))
            return dict(limit_view(ident, existing), sent=False)   # idempotent: never placed twice
        if ident in self._orders:
            raise BrokerError("identifier %s already belongs to a market order" % ident)
        px = m.limit_price(D(price), side)
        b = m.floor_base(D(base_amount))
        if px <= 0 or b <= 0:
            raise OrderTooSmall("limit price or amount rounds to zero (%s %s @ %s)" % (base_amount, m.base, price))
        bk = book if book is not None else self.order_book(symbol)
        crosses = limit_crosses(bk, side, px)
        if crosses and post_only_intent:
            raise LimitWouldCross(m.symbol, side, px, best_ask(bk) if side == "buy" else best_bid(bk))
        asset, need = (m.quote, ceil_to_precision(px * b, m.quote_amount_precision)) if side == "buy" else (m.base, b)
        have = self._bal.get(asset, ZERO)
        if need > have:
            raise InsufficientFunds("paper: limit %s needs %s %s available, have %s" % (side, need, asset, have))
        self._bal[asset] = have - need
        self._frozen[asset] = self._frozen.get(asset, ZERO) + need
        now = self.clock()
        e = {"kind": "limit", "symbol": m.symbol, "side": side, "price": px, "base_amount": b, "status": RESTING,
             "limit_state": LIMIT_OPEN, "order_id": self._next_id(), "reported_base": ZERO, "reported_quote": ZERO,
             "reported_fee": ZERO, "reported_fee_asset": fee_asset_for(m.symbol, side), "reserved": need,
             "reserve_asset": asset, "created_at": now, "updated_at": now, "tag": tag, "meta": meta,
             "post_only": bool(post_only_intent), "range_checked_at": None}
        self._limits[ident] = e
        log.info("paper limit %s %s %s @ %s placed (identifier %s)", side, b, m.symbol, px, ident)
        if crosses:   # no post-only intent: the crossing part executes at once as a taker
            w = walk_limit(bk, side, b, px)
            if w["base"] > 0:
                self._limit_fill(ident, e, m, w["base"], w["quote"], self.fee_rate, "limit fill (taker, crossed)")
        self._save()
        return dict(limit_view(ident, e), sent=True)

    def _limit_fill(self, ident, e, m, base, quote, rate, note):
        """Apply a (partial) fill of `base` for `quote` (gross) to a paper limit order."""
        remaining = _d(e["base_amount"]) - _d(e["reported_base"])
        base = min(D(base), remaining)
        if base <= 0:
            return False
        reserved = _d(e["reserved"])
        side = e["side"]
        if side == "buy":
            cost = min(ceil_to_precision(quote, m.quote_amount_precision), reserved)
            fee = min(ceil_to_precision(base * rate, m.base_amount_precision), base)
            self._frozen[m.quote] = self._frozen.get(m.quote, ZERO) - cost
            reserved -= cost
            self._bal[m.base] = self._bal.get(m.base, ZERO) + (base - fee)
            dq = cost
        else:
            dq = floor_to_precision(quote, m.quote_amount_precision)
            fee = min(ceil_to_precision(dq * rate, m.quote_amount_precision), dq)
            self._frozen[m.base] = self._frozen.get(m.base, ZERO) - base
            reserved -= base
            self._bal[m.quote] = self._bal.get(m.quote, ZERO) + (dq - fee)
        e["reported_base"] = _d(e["reported_base"]) + base
        e["reported_quote"] = _d(e["reported_quote"]) + dq
        e["reported_fee"] = _d(e["reported_fee"]) + fee
        e["reserved"] = reserved
        e["updated_at"] = self.clock()
        done = e["reported_base"] >= _d(e["base_amount"])
        if done:
            self._release(e)
            e.update(status="closed", limit_state=LIMIT_FILLED)
        else:
            e["limit_state"] = LIMIT_PARTIAL
        fill = make_fill(e["symbol"], side, base, dq, fee, fee_asset_for(e["symbol"], side), e["order_id"],
                         e["base_amount"], not done, "filled" if done else "partial", note, identifier=ident)
        self._log_fill(fill, note)
        log.info("paper limit fill %s %s %s @ %s (identifier %s, %s)", side, base, e["symbol"], e["price"], ident,
                 "filled" if done else "partial")
        return True

    def _release(self, e):
        """Return the locked remainder of a paper limit order to the available balance."""
        asset, left = e.get("reserve_asset"), _d(e.get("reserved"))
        if asset and left > 0:
            self._frozen[asset] = self._frozen.get(asset, ZERO) - left
            self._bal[asset] = self._bal.get(asset, ZERO) + left
        e["reserved"] = ZERO
        if asset and self._frozen.get(asset) is not None and self._frozen[asset] <= 0:
            if self._frozen[asset] < 0:
                log.warning("paper frozen %s went negative (%s); reset to 0", asset, self._frozen[asset])
            del self._frozen[asset]

    def _sync_one(self, ident, e, books, ranges, now):
        """Apply the fills the model gives since the order was placed. Returns True if it changed.
        books / ranges: per-sync caches (one book and one candle read per market and window)."""
        if limit_state_of(e) in LIMIT_FINAL_STATES:
            return False
        m = self.markets.get(e["symbol"])
        px, side = _d(e["price"]), e["side"]
        changed = False
        # (a) the live book shows liquidity at or through our price: it would have been matched with us
        if e["symbol"] not in books:
            try:
                books[e["symbol"]] = self.order_book(e["symbol"])
            except Exception as ex:  # noqa: BLE001 - the candle check still runs
                log.warning("paper limit sync: order book %s failed: %s", e["symbol"], ex)
                books[e["symbol"]] = None
        bk = books[e["symbol"]]
        remaining = _d(e["base_amount"]) - _d(e["reported_base"])
        if bk is not None and remaining > 0:
            w = walk_limit(bk, side, remaining, px)
            if w["base"] > 0:
                changed |= self._limit_fill(ident, e, m, w["base"], w["base"] * px, self.maker_fee_rate,
                                            "limit fill (book)")
        # (b) the market traded at/through our price since the order was placed: the rest fills
        remaining = _d(e["base_amount"]) - _d(e["reported_base"])
        if remaining > 0 and self._range_source is not None:
            step = self._range_step
            placed = float(e.get("created_at") or now)
            start = -(-int(placed) // step) * step            # first bar that opened after placement
            checked = e.get("range_checked_at")
            if checked:
                start = max(start, int(float(checked)) // step * step)   # re-read the bar that was still forming
            key = (e["symbol"], start)
            try:
                if key not in ranges:
                    ranges[key] = self._range_source(e["symbol"], start, now)
                rng = ranges[key]
            except Exception as ex:  # noqa: BLE001 - checked again next sync
                log.warning("paper limit sync: candles %s failed: %s", e["symbol"], ex)
                rng = None
            else:
                e["range_checked_at"] = now
            if rng:
                lo, hi = rng
                hit = (lo is not None and D(lo) <= px * (1 - self.fill_through)) if side == "buy" else \
                    (hi is not None and D(hi) >= px * (1 + self.fill_through))
                if hit:
                    changed |= self._limit_fill(ident, e, m, remaining, remaining * px, self.maker_fee_rate,
                                                "limit fill (traded %s %s)" % ("low" if side == "buy" else "high",
                                                                               lo if side == "buy" else hi))
        return changed

    def sync_limits(self, read_only=False):
        rep = {"open": [], "unresolved": [], "changed": [], "errors": [], "foreign_open": 0}
        active = [(k, e) for k, e in self._limits.items() if limit_state_of(e) not in LIMIT_FINAL_STATES]
        if active and not read_only:
            now = self.clock()
            books, ranges = {}, {}
            for ident, e in active:
                try:
                    if self._sync_one(ident, e, books, ranges, now):
                        rep["changed"].append(limit_view(ident, e))
                except Exception as ex:  # noqa: BLE001 - one order never stops the others
                    rep["errors"].append("%s: %s: %s" % (ident, type(ex).__name__, ex))
            self._save()
        rep["open"] = [v for v in self.limit_orders(active_only=True) if v["state"] != LIMIT_UNKNOWN]
        return rep

    def list_open(self, symbol=None):
        return [v for v in self.limit_orders(symbol=symbol, active_only=True) if v["state"] != LIMIT_UNKNOWN]

    def limit_orders(self, symbol=None, tag=None, active_only=False):
        return self._select(self._limits.items(), symbol, tag, active_only)

    def cancel(self, order, wait=True):
        ident = _ident_of(order)
        e = self._limits.get(ident)
        if e is None:
            raise NotBotOrder("%s is not a limit order of this paper account: not touched" % ident)
        if limit_state_of(e) in LIMIT_FINAL_STATES:
            return limit_view(ident, e)
        try:
            self._sync_one(ident, e, {}, {}, self.clock())    # a fill that happened before the cancel counts
        except Exception as ex:  # noqa: BLE001
            log.warning("paper cancel %s: fill check failed (%s); cancelling anyway", ident, ex)
        e["cancel_requested"] = True
        if limit_state_of(e) not in LIMIT_FINAL_STATES:
            self._release(e)
            e.update(status="closed", limit_state=LIMIT_CANCELLED, updated_at=self.clock())
            log.info("paper limit %s cancelled (filled %s of %s)", ident, e["reported_base"], e["base_amount"])
        self._save()
        return limit_view(ident, e)

    def limit_fill_events(self):
        return _limit_events(self._limits.items())

    def ack_limit_fills(self, events):
        changed = False
        for ev in events or []:
            e = self._limits.get(ev.get("identifier"))
            if e is None:
                continue
            e.update(_acked_fields(e, ev))
            changed = True
        if changed:
            self._save()


# --------------------------------------------------------------------------- live

class OrderJournal:
    """Write-ahead journal of live orders (state_dir/live_orders.json). An entry is written BEFORE
    the order is sent, so after a crash / timeout the order is resolved by its identifier instead
    of being sent again. Limit orders carry kind="limit"; while on the book their status is
    "resting" (not "open", which means a market order of unresolved outcome)."""

    UNRESOLVED = ("submitting", "unknown", "open")
    KEEP = UNRESOLVED + (RESTING,)

    def __init__(self, path, clock=time.time, keep=1000):
        self.path, self.clock, self.keep = path, clock, keep
        self._data = read_json(path, None) or {"orders": {}}
        self.dirty = False   # the in-memory journal has changes the file does not (a write failed)

    def _save(self):
        orders = self._data["orders"]
        if len(orders) > self.keep:
            done = sorted((e.get("created_at", 0), k) for k, e in orders.items()
                          if e.get("status") not in self.KEEP and not _unacked(e))
            for _, k in done[: len(orders) - self.keep]:
                del orders[k]
        try:
            atomic_write_json(self.path, self._data)
        except Exception:
            self.dirty = True
            raise
        self.dirty = False

    def flush(self):
        """Rewrite the file if an earlier write failed. Returns True when the file is current."""
        if not self.dirty:
            return True
        try:
            self._save()
            log.warning("order journal %s re-written after an earlier write failure", self.path)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("order journal %s still cannot be written: %s", self.path, e)
            return False

    def items(self):
        return list(self._data["orders"].items())

    def add(self, identifier, **fields):
        now = self.clock()
        fields.update(created_at=now, updated_at=now)
        self._data["orders"][identifier] = fields
        self._save()

    def update(self, identifier, **fields):
        e = self._data["orders"].setdefault(identifier, {"created_at": self.clock()})
        e.update(fields)
        e["updated_at"] = self.clock()
        self._save()

    def get(self, identifier):
        return self._data["orders"].get(identifier)

    def unresolved(self):
        """Orders of unknown outcome (market AND limit submissions; resting limits are not here)."""
        return [(k, e) for k, e in self._data["orders"].items() if e.get("status") in self.UNRESOLVED]

    def limits(self):
        return [(k, e) for k, e in self._data["orders"].items() if e.get("kind") == "limit"]


class LiveBroker(Broker):
    """Real orders via BitpinClient. Every order gets a uuid4 `identifier` (or the caller's, for
    limit orders), is journaled before submission, and balances are reconciled from the wallet
    afterwards. A MARKET order is polled until closed (cancel after `poll_timeout`). A LIMIT order
    rests; sync_limits() keeps its journal entry in step with the exchange. Amounts are floored to
    market precision; orders below the per-quote minimum (min_order_irt / min_order_usdt) are
    refused locally.

    Wallet sanity: a wallet read without the toman row, or in which a tracked asset that had a
    balance in the previous read is missing (without a bot order in that asset since, and without
    a bot limit order that may have filled), raises WalletDataError instead of being taken as a
    zero balance."""

    mode = "live"
    JOURNAL_FILE = "live_orders.json"
    TRADES_FILE = "live_trades.csv"
    SUPPORTED_QUOTES = (IRT, USDT)

    def __init__(self, client, markets, state_dir, irt_asset_code="IRT", irt_unit_divisor=1, min_order_irt=1000000,
                 poll_interval=1.0, poll_timeout=30.0, unknown_order_max_age=900, sleep=time.sleep, clock=time.time,
                 assets=None, max_lookup_failures=10, min_order_usdt=DEFAULT_MIN_ORDER_USDT,
                 taker_fee_rate=DEFAULT_TAKER_FEE, cancel_poll_attempts=5):
        self.client, self.markets = client, markets
        self.irt_code = str(irt_asset_code).upper()
        self.divisor = D(irt_unit_divisor)
        if self.divisor <= 0:
            raise ValueError("irt_unit_divisor must be positive")
        self.min_order_irt = D(min_order_irt)
        self.min_order_usdt = D(min_order_usdt)
        self.taker_fee_rate = D(taker_fee_rate)
        self.poll_interval, self.poll_timeout = float(poll_interval), float(poll_timeout)
        self.unknown_order_max_age = float(unknown_order_max_age)
        self.max_lookup_failures = int(max_lookup_failures)
        self.cancel_poll_attempts = int(cancel_poll_attempts)
        self.sleep, self.clock = sleep, clock
        os.makedirs(state_dir, exist_ok=True)
        self.journal = OrderJournal(os.path.join(state_dir, self.JOURNAL_FILE), clock)
        self.trades_path = os.path.join(state_dir, self.TRADES_FILE)
        self._assets = {a.upper() for a in (assets or [])}
        self._totals = None
        self._avail = None
        self._last_nonzero = {}     # asset -> total seen in the previous good read
        self._irt_was_zero = False   # the toman row was present with 0 in the previous good read
        self._expect_change = set()  # assets touched by a bot order since the previous good read
        self._late_fills = []
        self.last_wallet_rows = []

    def track(self, assets):
        self._assets |= {a.upper() for a in assets}

    def _ensure_tracked(self, m):
        """Make sure the wallet read includes both assets of market m (a newly tracked asset forces
        a fresh read)."""
        if not self._assets:
            return            # the wallet read is not filtered: every asset is included
        new = {m.base, m.quote} - self._assets - {self.quote_asset}
        if new:
            self.track(new)
            self._totals = self._avail = None

    def _min_for(self, quote):
        if quote == self.quote_asset:
            return self.min_order_irt
        if quote == USDT:
            return self.min_order_usdt
        raise BrokerError("no minimum order known for %s-quoted markets" % quote)

    def default_min_notional(self):
        return {IRT: self.min_order_irt, USDT: self.min_order_usdt}

    def _limit_assets(self):
        """Assets locked by (or just filled from) the bot's limit orders: their wallet rows may change
        or vanish between two reads without a bot action in that cycle."""
        out = set()
        for _, e in self.journal.limits():
            if e.get("status") in OrderJournal.KEEP:
                try:
                    out |= set(parse_symbol(e.get("symbol")))
                except (ValueError, TypeError):
                    continue
        return out

    # ---- balances
    def refresh(self):
        query = None
        if self._assets:
            query = sorted({a for a in self._assets if a != self.quote_asset} | {self.irt_code})
        rows = self.client.wallets(assets=query)
        totals, avail = {}, {}
        irt_seen = False
        for r in rows:
            if not isinstance(r, dict):
                continue
            if r.get("service") not in (None, "", "main"):
                continue
            a = str(r.get("asset") or "").upper()
            if not a:
                continue
            bal = D(r.get("balance") or 0)
            fr = D(r.get("frozen") or 0)
            if a == self.irt_code:
                key = self.quote_asset
                irt_seen = True
                bal, fr = bal / self.divisor, fr / self.divisor
            elif a == self.quote_asset:
                key = "IRT_UNMAPPED"  # an 'IRT' wallet exists but irt_asset_code says otherwise
            else:
                key = a
            totals[key] = totals.get(key, ZERO) + bal + fr
            avail[key] = avail.get(key, ZERO) + bal
        if not irt_seen:
            if not self._irt_was_zero:
                self._totals = self._avail = None
                raise WalletDataError("wallet response has no %s (toman) row (%d rows): not trading on an incomplete "
                                      "balance read" % (self.irt_code, len(rows)))
            totals[self.quote_asset] = avail[self.quote_asset] = ZERO  # it was 0 last time: absent = 0
        expected = self._expect_change | self._limit_assets()
        vanished = sorted(a for a in self._last_nonzero
                          if a not in totals and a not in expected and a != self.quote_asset)
        if vanished:
            self._totals = self._avail = None
            raise WalletDataError("wallet rows for %s disappeared (previous read: %s) without a bot order: not trading "
                                  "on an incomplete balance read. If you moved these coins yourself, restart the bot."
                                  % (", ".join(vanished), ", ".join("%s %s" % (a, self._last_nonzero[a])
                                                                    for a in vanished)))
        self._totals, self._avail = totals, avail
        tracked = self._assets | {self.quote_asset}
        self._last_nonzero = {a: v for a, v in totals.items() if v > 0 and (not self._assets or a in tracked)}
        self._irt_was_zero = totals.get(self.quote_asset, ZERO) == 0
        self._expect_change = set()
        self.last_wallet_rows = rows

    def balances(self):
        if self._totals is None:
            self.refresh()
        return dict(self._totals)

    def available(self):
        if self._avail is None:
            self.refresh()
        return dict(self._avail)

    def order_book(self, symbol):
        return parse_book(self.client.orderbook(symbol))

    # ---- orders
    def _market(self, symbol):
        m = self.markets.get(symbol)
        if not m.is_trading:
            raise MarketNotTradable("%s is not tradable (tradable=%s suspended=%s)" % (symbol, m.tradable, m.suspended))
        if m.quote not in self.SUPPORTED_QUOTES:
            raise BrokerError("only %s-quoted markets are supported (got %s)" % (" / ".join(self.SUPPORTED_QUOTES),
                                                                                symbol))
        return m

    def market_buy(self, symbol, quote_amount, ref_price=None, book=None):
        m = self._market(symbol)
        q = m.floor_quote(D(quote_amount))
        mn = self._min_for(m.quote)
        if q < mn:
            raise OrderTooSmall("buy %s %s is below min_order_%s %s" % (fmt_amount(q), m.quote, m.quote.lower(),
                                                                        fmt_amount(mn)))
        self._ensure_tracked(m)
        have = self.available().get(m.quote, ZERO)
        if q > have:
            raise InsufficientFunds("need %s %s, available %s" % (fmt_amount(q), m.quote, fmt_amount(have)))
        return self._submit(m, "buy", q, quote_amount=q)

    def market_sell(self, symbol, base_amount, ref_price=None, book=None):
        m = self._market(symbol)
        b = m.floor_base(D(base_amount))
        if b <= 0:
            raise OrderTooSmall("sell amount rounds to zero")
        mn = self._min_for(m.quote)
        px = D(ref_price) if ref_price is not None else best_bid(book or self.order_book(symbol))
        if px is None or b * px < mn:
            raise OrderTooSmall("sell %s %s (~%s %s) is below min_order_%s %s" % (
                b, m.base, fmt_amount(b * px) if px else "?", m.quote, m.quote.lower(), fmt_amount(mn)))
        self._ensure_tracked(m)
        have = self.available().get(m.base, ZERO)
        if b > have:
            raise InsufficientFunds("need %s %s, available %s" % (b, m.base, have))
        return self._submit(m, "sell", b, base_amount=b)

    def _journal_update(self, ident, **fields):
        """Journal update AFTER something was sent: a write failure must not turn an executed order
        into an exception (the entry then stays unresolved and is re-checked by identifier)."""
        try:
            self.journal.update(ident, **fields)
        except Exception as e:  # noqa: BLE001
            log.error("cannot update order journal for %s (%s); it will be re-checked by identifier", ident, e)

    def _touched(self, m):
        self._expect_change |= {m.base}
        if m.quote != self.quote_asset:
            self._expect_change |= {m.quote}

    def _submit(self, m, side, requested, base_amount=None, quote_amount=None):
        ident = new_identifier()
        try:
            self.journal.add(ident, kind="market", symbol=m.symbol, side=side, base_amount=base_amount,
                             quote_amount=quote_amount, status="submitting")
        except Exception as e:  # noqa: BLE001 - write-ahead failed: the order must not be sent
            self.journal._data["orders"].pop(ident, None)
            raise BrokerError("cannot write the order journal %s (%s): order NOT sent" % (self.journal.path, e))
        self._touched(m)
        try:
            order = self.client.place_order(m.symbol, side, "market", base_amount=base_amount,
                                            quote_amount=quote_amount, identifier=ident)
        except OrderStatusUnknown:
            self._journal_update(ident, status="unknown")
            self._totals = self._avail = None
            raise
        except (AuthBudgetExceeded, ValueError, OrderNotSent) as e:  # definitely not sent / not accepted
            self._journal_update(ident, status="not_sent", error=str(e)[:300])
            raise
        except BitpinAPIError as e:
            definite = isinstance(e, AuthError) or (e.status is not None and 400 <= e.status < 500)
            self._journal_update(ident, status="rejected" if definite else "unknown", error=str(e)[:300])
            raise
        except Exception:
            self._journal_update(ident, status="unknown")
            self._totals = self._avail = None
            raise
        # ---- the order exists on the exchange from here on: nothing below may raise
        oid = order.get("id")
        self._journal_update(ident, status="open", order_id=str(oid))
        final = self._wait_final(order)
        fill = fill_from_order(m.symbol, side, final, requested, identifier=ident)
        st = final.get("state")
        self._journal_update(ident, status="open" if st in OPEN_ORDER_STATES or st is None else "closed",
                             state=st, dealed_base=final.get("dealed_base_amount"),
                             dealed_quote=final.get("dealed_quote_amount"), commission=final.get("commission"),
                             **self._reported(fill))
        try:
            self.refresh()
        except Exception as e:  # noqa: BLE001 - the fill already happened
            log.warning("wallet refresh after order failed: %s", e)
            self._totals = self._avail = None
        safe_trade_log(self.trades_path, self.mode, fill, clock=self.clock)
        return fill

    @staticmethod
    def _reported(fill):
        """Journal fields with the latest known CUMULATIVE fill of an order (see fill_records)."""
        return {"reported_base": fill["base"], "reported_quote": fill["quote"], "reported_fee": fill["fee"],
                "reported_fee_asset": fill["fee_asset"]}

    def _is_final(self, order):
        return order_is_final(order)

    def _wait_final(self, order):
        if self._is_final(order):
            return order
        oid = order.get("id")
        last = order
        deadline = self.clock() + self.poll_timeout
        while self.clock() < deadline:
            self.sleep(self.poll_interval)
            try:
                last = self.client.get_order(oid)
            except BitpinError as e:
                log.warning("polling order %s failed: %s", oid, e)
                continue
            if self._is_final(last):
                return last
        log.warning("order %s still %s after %.0fs; requesting cancel of the remainder", oid, last.get("state"),
                    self.poll_timeout)
        try:
            self.client.cancel_order(oid)
        except BitpinError as e:
            log.warning("cancel of order %s failed: %s", oid, e)
        for _ in range(5):
            self.sleep(self.poll_interval)
            try:
                last = self.client.get_order(oid)
            except BitpinError:
                continue
            if self._is_final(last):
                return last
        return last

    def _fill_from_order(self, m, side, o, requested):
        return fill_from_order(m.symbol, side, o, requested)

    def drain_late_fills(self):
        out, self._late_fills = self._late_fills, []
        return out

    def prepare(self):
        ensure = getattr(self.client, "ensure_token", None)
        if ensure is not None:
            ensure()

    def _market_unresolved(self):
        return [(k, e) for k, e in self.journal.unresolved() if e.get("kind") != "limit"]

    def unresolved_count(self):
        return len(self._market_unresolved())

    def fill_records(self):
        out = []
        for ident, e in self.journal.items():
            if not e.get("symbol") or e.get("side") not in ("buy", "sell"):
                continue
            base = e.get("reported_base")
            out.append(fill_record(ident, e["symbol"], e["side"], base,
                                   e.get("reported_quote") if base is not None else None,
                                   e.get("reported_fee") if base is not None else None,
                                   e.get("reported_fee_asset"), status=e.get("status"),
                                   kind=e.get("kind") or "market"))
        return out

    def _lookup(self, ident, oid):
        """The exchange's row for a journaled order, or None if it cannot be found. By order id when
        known; an HTTP 404 for that id falls back to the identifier lookup (cancel_order treats 404
        as 'gone' too, and the id path or account may differ from what the journal assumes)."""
        if oid:
            try:
                return self.client.get_order(oid)
            except BitpinAPIError as ex:
                if ex.status != 404:
                    raise
                log.warning("order id %s (identifier %s) returned HTTP 404; looking it up by identifier", oid, ident)
        return self.client.find_order_by_identifier(ident)

    def resolve_pending(self, read_only=False):
        """Resolve journaled MARKET orders whose outcome is unknown. Returns the ones still
        unresolved; the runner must not place new orders while this list is non-empty. Limit orders
        are skipped entirely (sync_limits handles them; a resting order is never cancelled here).

        * An entry with a known order_id is looked up by id (and its remainder cancelled once it is
          older than poll_timeout); a 404 for the id falls back to the identifier lookup.
        * An entry that cannot be found ('submitting'/'unknown', or a 404 id) is looked up by
          identifier. After unknown_order_max_age it is treated as never placed - unless
          expire_unknown_orders is False (a capped sleeve cannot tell from the wallet whether it
          executed): then it stays unresolved with stuck=True until the user records the outcome
          with `run_bot.py resolve-order`.
        * Failed lookups (any exception) are counted; entries with >= max_lookup_failures
          consecutive failures are returned with stuck=True so the runner can escalate.
        * An entry is only closed when its fill could be built from the exchange's row.
        * read_only=True (dry runs): lookups only - no cancel, no journal or trade-log writes."""
        if not read_only:
            self.journal.flush()
        still = []
        changed = False
        now = self.clock()
        for ident, e in self._market_unresolved():
            age = now - float(e.get("created_at") or now)
            oid = e.get("order_id")

            def failed(ex, what="cannot resolve"):
                n = int(e.get("lookup_failures") or 0) + 1
                log.warning("%s order %s (order id %s) yet (%d consecutive failures): %s: %s",
                            what, ident, oid or "-", n, type(ex).__name__, ex)
                if not read_only:
                    self._journal_update(ident, lookup_failures=n, last_lookup_error=str(ex)[:200])
                still.append(dict(e, identifier=ident, lookup_failures=n, last_lookup_error=str(ex)[:200],
                                  stuck=n >= self.max_lookup_failures))

            try:
                o = self._lookup(ident, oid)
            except Exception as ex:  # noqa: BLE001 - never crash the cycle; count and escalate
                failed(ex)
                continue
            if o is None:
                if age <= self.unknown_order_max_age:
                    still.append(dict(e, identifier=ident))
                elif self.expire_unknown_orders:
                    log.warning("order %s (%s %s) never appeared on the exchange after %.0f min; treating it as "
                                "not placed", ident, e.get("side"), e.get("symbol"), age / 60)
                    if not read_only:
                        self._journal_update(ident, status="not_found", lookup_failures=0)
                        changed = True
                else:
                    log.error("order %s (%s %s) cannot be found on the exchange after %.0f min. It may have executed; "
                              "the bot will not trade until you check it in the Bitpin app and record the outcome "
                              "with: run_bot.py resolve-order --identifier %s (--order-id ID | --not-executed)",
                              ident, e.get("side"), e.get("symbol"), age / 60, ident)
                    still.append(dict(e, identifier=ident, stuck=True, not_found=True,
                                      last_lookup_error="not found by identifier after %.0f min" % (age / 60)))
                continue
            if not self._is_final(o):
                if not read_only:
                    if age > self.poll_timeout:
                        try:
                            self.client.cancel_order(o.get("id"))
                        except BitpinError as ex:
                            log.warning("cancel of stale order %s failed: %s", o.get("id"), ex)
                    self._journal_update(ident, status="open", order_id=str(o.get("id")), state=o.get("state"),
                                         lookup_failures=0)
                still.append(dict(e, identifier=ident, order_id=str(o.get("id")), state=o.get("state")))
                continue
            try:
                req = e.get("quote_amount") if e.get("side") == "buy" else e.get("base_amount")
                fill = fill_from_order(e["symbol"], e.get("side"), o, D(req or 0), identifier=ident)
            except Exception as ex:  # noqa: BLE001 - never mark 'closed' without a fill
                failed(ex, "cannot build the fill of")
                continue
            if read_only:
                log.info("order %s is final on the exchange (state=%s); a real run would record it", ident,
                         o.get("state"))
                continue
            changed = True
            late = {k: fill[k] - D(e.get("reported_" + k) or 0) for k in ("base", "quote", "fee")}
            if any(v != 0 for v in late.values()):
                self._late_fills.append(dict(fill, base=late["base"], quote=late["quote"], fee=late["fee"], late=True))
            self._journal_update(ident, status="closed", order_id=str(o.get("id")), state=o.get("state"),
                                 dealed_base=o.get("dealed_base_amount"), dealed_quote=o.get("dealed_quote_amount"),
                                 commission=o.get("commission"), lookup_failures=0, **self._reported(fill))
            safe_trade_log(self.trades_path, self.mode, fill, note="resolved later", clock=self.clock)
            self._expect_change |= {parse_symbol(e["symbol"])[0]}
            log.info("resolved order %s: state=%s dealed_base=%s dealed_quote=%s", ident, o.get("state"),
                     o.get("dealed_base_amount"), o.get("dealed_quote_amount"))
        if changed:
            self._totals = self._avail = None
        return still

    def resolve_manually(self, identifier, order_id=None, not_executed=False):
        """Record the outcome of a journaled order that the bot cannot resolve by itself
        (`run_bot.py resolve-order`). With order_id: the order is read from the exchange by that id
        (from the Bitpin app) and its final fill recorded (a limit order may also still be resting:
        it is then tracked again); with not_executed=True: recorded as never executed beyond what
        the journal already holds. Returns the fill (or None)."""
        e = self.journal.get(identifier)
        if e is None:
            raise BrokerError("no journaled order with identifier %s" % identifier)
        is_limit = e.get("kind") == "limit"
        if e.get("status") not in (OrderJournal.KEEP if is_limit else OrderJournal.UNRESOLVED):
            raise BrokerError("order %s is already resolved (status %s)" % (identifier, e.get("status")))
        if (order_id is not None) + bool(not_executed) != 1:
            raise BrokerError("give exactly one of order_id / not_executed")
        if not_executed and is_limit and e.get("status") == RESTING:
            # a limit order that was on the book and is gone: closed with the fills already recorded
            self.journal.update(identifier, status="closed", limit_state=LIMIT_CANCELLED, resolved_by="user",
                                lookup_failures=0, resolved_at=self.clock())
            log.warning("limit order %s recorded by the user as gone (cancelled after the recorded fill %s)",
                        identifier, e.get("reported_base") or 0)
            return None
        if not_executed:
            self.journal.update(identifier, status="not_found", resolved_by="user", lookup_failures=0,
                                resolved_at=self.clock())
            log.warning("order %s recorded by the user as NOT executed (beyond the journaled fill %s %s)", identifier,
                        e.get("reported_base") or 0, e.get("reported_quote") or 0)
            return None
        o = self.client.get_order(order_id)
        if not isinstance(o, dict):
            raise BrokerError("unexpected order response for id %s" % order_id)
        if o.get("identifier") not in (None, "") and str(o.get("identifier")) != identifier:
            raise BrokerError("order %s has identifier %s, not %s" % (order_id, o.get("identifier"), identifier))
        sym = str(o.get("symbol") or "").upper()
        if sym and sym != e.get("symbol"):
            raise BrokerError("order %s is %s, but the journaled order is %s" % (order_id, sym, e.get("symbol")))
        if o.get("side") and o.get("side") != e.get("side"):
            raise BrokerError("order %s is a %s, but the journaled order is a %s" % (order_id, o.get("side"),
                                                                                    e.get("side")))
        if e.get("kind") == "limit":
            o = dict(o, id=o.get("id") or order_id)
            self._apply_limit_row(identifier, o)
            self.journal.update(identifier, resolved_by="user", resolved_at=self.clock())
            v = limit_view(identifier, self.journal.get(identifier))
            return make_fill(v["symbol"], v["side"], v["filled_base"], v["filled_quote"], v["fee"], v["fee_asset"],
                             v["order_id"], v["base_amount"], v["state"] != LIMIT_FILLED, v["state"], "resolved by user",
                             identifier=identifier)
        if not self._is_final(o):
            raise BrokerError("order %s is still %s on the exchange: wait until it is final (or cancel it in the app)"
                              % (order_id, o.get("state")))
        req = e.get("quote_amount") if e.get("side") == "buy" else e.get("base_amount")
        fill = fill_from_order(e["symbol"], e["side"], o, D(req or 0), identifier=identifier)
        self.journal.update(identifier, status="closed", order_id=str(o.get("id") or order_id), state=o.get("state"),
                            dealed_base=o.get("dealed_base_amount"), dealed_quote=o.get("dealed_quote_amount"),
                            commission=o.get("commission"), lookup_failures=0, resolved_by="user",
                            resolved_at=self.clock(), **self._reported(fill))
        safe_trade_log(self.trades_path, self.mode, fill, note="resolved by user", clock=self.clock)
        return fill

    # ---- resting limit orders
    def limit_orders(self, symbol=None, tag=None, active_only=False):
        return self._select(self.journal.limits(), symbol, tag, active_only)

    def limit_fill_events(self):
        return _limit_events(self.journal.limits())

    def ack_limit_fills(self, events):
        for ev in events or []:
            e = self.journal.get(ev.get("identifier"))
            if e is None or e.get("kind") != "limit":
                continue
            self.journal.update(ev["identifier"], **_acked_fields(e, ev))

    def place_limit(self, symbol, side, price, base_amount, identifier=None, post_only_intent=True, book=None,
                    tag=None, meta=None):
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        m = self._market(symbol)
        ident = str(identifier) if identifier else new_identifier()
        existing = self.journal.get(ident)
        if existing is not None:
            if existing.get("kind") != "limit":
                raise BrokerError("identifier %s already belongs to a %s order" % (
                    ident, existing.get("kind") or "market"))
            if existing.get("symbol") != m.symbol or existing.get("side") != side:
                raise BrokerError("identifier %s already belongs to a %s %s order" % (
                    ident, existing.get("side"), existing.get("symbol")))
            if existing.get("status") != "not_sent":
                return dict(limit_view(ident, existing), sent=False)   # idempotent: never sent twice
        px = m.limit_price(D(price), side)
        b = m.floor_base(D(base_amount))
        if px <= 0 or b <= 0:
            raise OrderTooSmall("limit price or amount rounds to zero (%s %s @ %s)" % (base_amount, m.base, price))
        mn = self._min_for(m.quote)
        if px * b < mn:
            raise OrderTooSmall("limit %s %s %s @ %s = %s %s is below min_order_%s %s" % (
                side, b, m.base, px, fmt_amount(px * b), m.quote, m.quote.lower(), fmt_amount(mn)))
        if post_only_intent:
            bk = book if book is not None else self.order_book(m.symbol)
            if limit_crosses(bk, side, px):
                raise LimitWouldCross(m.symbol, side, px, best_ask(bk) if side == "buy" else best_bid(bk))
        self._ensure_tracked(m)
        asset, need = (m.quote, ceil_to_precision(px * b, m.quote_amount_precision)) if side == "buy" else (m.base, b)
        have = self.available().get(asset, ZERO)
        if need > have:
            raise InsufficientFunds("limit %s %s needs %s %s available, have %s" % (side, m.symbol, need, asset, have))
        try:
            self.journal.add(ident, kind="limit", symbol=m.symbol, side=side, price=px, base_amount=b,
                             status="submitting", tag=tag, meta=meta, post_only=bool(post_only_intent))
        except Exception as e:  # noqa: BLE001 - write-ahead failed: the order must not be sent
            if existing is None:
                self.journal._data["orders"].pop(ident, None)
            else:
                self.journal._data["orders"][ident] = existing
            raise BrokerError("cannot write the order journal %s (%s): order NOT sent" % (self.journal.path, e))
        self._touched(m)
        try:
            order = self.client.place_order(m.symbol, side, "limit", base_amount=b, price=px, identifier=ident)
        except OrderStatusUnknown:
            self._journal_update(ident, status="unknown")
            self._totals = self._avail = None
            raise
        except (AuthBudgetExceeded, ValueError, OrderNotSent) as e:  # definitely not sent / not accepted
            self._journal_update(ident, status="not_sent", error=str(e)[:300])
            raise
        except BitpinAPIError as e:
            definite = isinstance(e, AuthError) or (e.status is not None and 400 <= e.status < 500)
            self._journal_update(ident, status="rejected" if definite else "unknown", error=str(e)[:300])
            raise
        except Exception:
            self._journal_update(ident, status="unknown")
            self._totals = self._avail = None
            raise
        # ---- the order exists on the exchange from here on: nothing below may raise
        self._totals = self._avail = None
        try:
            self._apply_limit_row(ident, order)
        except Exception as e:  # noqa: BLE001 - it is resolved by identifier on the next sync_limits
            log.error("limit order %s accepted but its response was not understood (%s); it is re-read on the next "
                      "sync", ident, e)
            self._journal_update(ident, status="unknown", order_id=str(order.get("id") or "") or None)
        log.info("limit %s %s %s @ %s placed (identifier %s, order id %s)", side, b, m.symbol, px, ident,
                 order.get("id"))
        return dict(limit_view(ident, self.journal.get(ident)), sent=True)

    def _apply_limit_row(self, ident, row):
        """Bring the journal entry of limit order `ident` in step with the exchange's row. Fills are
        cumulative and never go backwards; a fill increase is logged to the trade CSV (as a delta)
        and the wallet is re-read on the next balances(). Returns True if anything changed.
        Raises BrokerError when the row belongs to another market/side (never applied)."""
        e = self.journal.get(ident)
        if e is None or e.get("kind") != "limit":
            raise NotBotOrder("%s is not a limit order in the journal" % ident)
        sym = str(row.get("symbol") or "").upper()
        if sym and sym != e.get("symbol"):
            raise BrokerError("exchange row for %s is %s, but the journaled order is %s" % (ident, sym, e.get("symbol")))
        if row.get("side") and row.get("side") != e.get("side"):
            raise BrokerError("exchange row for %s is a %s, but the journaled order is a %s" % (
                ident, row.get("side"), e.get("side")))
        rid = row.get("identifier")
        if rid not in (None, "") and str(rid) != ident:
            raise BrokerError("exchange row has identifier %s, not %s" % (rid, ident))
        base = D(row.get("dealed_base_amount") or 0)
        quote = D(row.get("dealed_quote_amount") or 0)
        fee = D(row.get("commission") or 0)
        pb, pq, pf = _d(e.get("reported_base")), _d(e.get("reported_quote")), _d(e.get("reported_fee"))
        if base < pb or quote < pq or fee < pf:
            log.warning("exchange row of limit order %s shows less filled (%s %s %s) than already recorded (%s %s %s); "
                        "keeping the recorded amounts", ident, base, quote, fee, pb, pq, pf)
            base, quote, fee = max(base, pb), max(quote, pq), max(fee, pf)
        requested = _d(e.get("base_amount"))
        final = order_is_final(row)
        if final:
            status = "closed"
            ls = LIMIT_FILLED if requested > 0 and base >= requested else LIMIT_CANCELLED
        else:
            status = RESTING
            ls = LIMIT_PARTIAL if base > 0 else LIMIT_OPEN
        oid = row.get("id")
        fa = fee_asset_for(e["symbol"], e["side"])
        old_state = limit_state_of(e)
        changed = ls != old_state or base != pb or quote != pq or fee != pf or e.get("status") != status
        fields = {"status": status, "limit_state": ls, "state": row.get("state"), "reported_base": base,
                  "reported_quote": quote, "reported_fee": fee, "reported_fee_asset": fa, "lookup_failures": 0,
                  "missing_since": None}
        if oid not in (None, ""):
            fields["order_id"] = str(oid)
        self._journal_update(ident, **fields)
        if base > pb:
            db, dq, dfee = base - pb, quote - pq, fee - pf
            m_base, m_quote = parse_symbol(e["symbol"])
            self._expect_change |= {m_base} | ({m_quote} if m_quote != self.quote_asset else set())
            self._totals = self._avail = None
            fill = make_fill(e["symbol"], e["side"], db, dq, dfee, fa, fields.get("order_id") or e.get("order_id"),
                             requested, ls != LIMIT_FILLED, "filled" if ls == LIMIT_FILLED else "partial",
                             "limit fill", identifier=ident)
            safe_trade_log(self.trades_path, self.mode, fill, note="limit fill", clock=self.clock)
            log.info("LIMIT FILL %s %s: +%s (cum %s of %s) at %s, state %s [%s]", e["side"].upper(), e["symbol"], db,
                     base, requested, e.get("price"), ls, ident)
        elif final and old_state not in LIMIT_FINAL_STATES:
            self._totals = self._avail = None        # the lock was released
        if changed and old_state != ls:
            log.info("limit order %s (%s %s @ %s): %s -> %s", ident, e.get("side"), e.get("symbol"), e.get("price"),
                     old_state, ls)
        return changed

    def _limit_failed(self, ident, e, ex, read_only, rep, what="cannot refresh"):
        n = int(e.get("lookup_failures") or 0) + 1
        msg = "%s limit order %s (%d consecutive failures): %s: %s" % (what, ident, n, type(ex).__name__, ex)
        log.warning(msg)
        rep["errors"].append(msg[:300])
        if not read_only:
            self._journal_update(ident, lookup_failures=n, last_lookup_error=str(ex)[:200])

    def sync_limits(self, read_only=False):
        """Refresh the bot's limit orders from the exchange (see the module docstring). read_only
        (dry runs): lookups only, no journal writes, no cancels."""
        rep = {"open": [], "unresolved": [], "changed": [], "errors": [], "foreign_open": None}
        if not read_only:
            self.journal.flush()
        mine = dict(self.journal.limits())
        if not mine:
            return rep
        active = [(k, e) for k, e in mine.items() if e.get("status") in OrderJournal.KEEP]
        rows_by_ident = None
        if active:
            try:
                rows = self.client.open_orders()
                rows_by_ident, foreign = {}, 0
                for r in rows:
                    rid = str(r.get("identifier") or "")
                    if rid in mine:
                        rows_by_ident[rid] = r
                    else:
                        foreign += 1           # not the bot's: counted, never touched
                rep["foreign_open"] = foreign
            except (AuthError, AuthBudgetExceeded):
                raise
            except Exception as ex:  # noqa: BLE001 - fall back to one lookup per order
                rep["errors"].append("open orders list failed: %s: %s" % (type(ex).__name__, ex))
                log.warning("open orders list failed (%s); looking the bot's limit orders up one by one", ex)
        # the exchange lists as open an order the journal already has as final: the exchange is right
        for ident, row in (rows_by_ident or {}).items():
            e = mine[ident]
            if e.get("status") not in OrderJournal.KEEP and not read_only:
                log.warning("limit order %s is %s in the journal but OPEN on the exchange: re-tracked", ident,
                            limit_state_of(e))
                try:
                    if self._apply_limit_row(ident, row):
                        rep["changed"].append(limit_view(ident, self.journal.get(ident)))
                except Exception as ex:  # noqa: BLE001
                    self._limit_failed(ident, e, ex, read_only, rep)
        now = self.clock()
        for ident, e in active:
            row = rows_by_ident.get(ident) if rows_by_ident is not None else None
            if row is None:
                try:
                    row = self._lookup(ident, e.get("order_id"))
                except (AuthError, AuthBudgetExceeded):
                    raise
                except Exception as ex:  # noqa: BLE001 - counted; retried next sync
                    self._limit_failed(ident, e, ex, read_only, rep)
                    rep["unresolved"].append(dict(limit_view(ident, e), lookup_failures=int(e.get("lookup_failures")
                                                                                            or 0) + 1))
                    continue
            if row is None:
                self._limit_not_found(ident, e, now, read_only, rep)
                continue
            if read_only:
                continue
            try:
                if self._apply_limit_row(ident, row):
                    rep["changed"].append(limit_view(ident, self.journal.get(ident)))
            except Exception as ex:  # noqa: BLE001
                self._limit_failed(ident, e, ex, read_only, rep, "cannot apply the exchange row of")
                continue
            e2 = self.journal.get(ident)
            if e2.get("cancel_requested") and e2.get("status") == RESTING:
                # a cancel requested earlier (before a restart, or while the outcome was unknown)
                try:
                    self.client.cancel_order(e2.get("order_id"))
                    log.warning("limit order %s: cancel re-sent (requested at %s)", ident, e2.get("cancel_requested_at"))
                except (AuthError, AuthBudgetExceeded):
                    raise
                except Exception as ex:  # noqa: BLE001
                    rep["errors"].append("re-sending the cancel of %s failed: %s" % (ident, ex))
        for ident, e in self.journal.limits():
            v = limit_view(ident, e)
            if v["state"] in (LIMIT_OPEN, LIMIT_PARTIAL):
                rep["open"].append(v)
            elif v["state"] == LIMIT_UNKNOWN and not any(u["identifier"] == ident for u in rep["unresolved"]):
                rep["unresolved"].append(v)
        return rep

    def _limit_not_found(self, ident, e, now, read_only, rep):
        """A journaled limit order that neither the open list nor a lookup can find. A submission of
        unknown outcome ages from its creation (like a market order); an order that WAS on the book
        ages from the first sync that could not find it (missing_since), so one bad lookup never
        writes off a live order."""
        v = limit_view(ident, e)
        if e.get("status") == RESTING:
            since = e.get("missing_since")
            if since is None:
                if not read_only:
                    self._journal_update(ident, missing_since=now)
                since = now
            age = now - float(since)
        else:
            age = now - float(e.get("created_at") or now)
        if age <= self.unknown_order_max_age:
            rep["unresolved"].append(v)
            return
        if not self.expire_unknown_orders:
            log.error("limit order %s (%s %s) cannot be found on the exchange for %.0f min; record its outcome with "
                      "run_bot.py resolve-order --identifier %s", ident, e.get("side"), e.get("symbol"), age / 60, ident)
            rep["unresolved"].append(dict(v, stuck=True, not_found=True))
            return
        if read_only:
            rep["unresolved"].append(v)
            return
        if e.get("status") == RESTING:
            # it was on the book before: Bitpin drops unmatched cancelled orders from the list after 7 days
            log.warning("limit order %s (%s %s) is no longer on the exchange: recorded as cancelled with its known "
                        "fill %s", ident, e.get("side"), e.get("symbol"), e.get("reported_base"))
            self._journal_update(ident, status="closed", limit_state=LIMIT_CANCELLED, note="vanished",
                                 lookup_failures=0)
            self._totals = self._avail = None
        else:
            log.warning("limit order %s (%s %s) never appeared on the exchange after %.0f min; treating it as not "
                        "placed", ident, e.get("side"), e.get("symbol"), age / 60)
            self._journal_update(ident, status="not_found", lookup_failures=0)
        rep["changed"].append(limit_view(ident, self.journal.get(ident)))

    def list_open(self, symbol=None):
        """The bot's limit orders that are open on the exchange right now (one GET; the journal is
        updated from the rows). Orders the bot did not place are left out and never touched."""
        mine = dict(self.journal.limits())
        out = []
        for r in self.client.open_orders(symbol=symbol.upper() if symbol else None):
            ident = str(r.get("identifier") or "")
            if ident not in mine:
                continue
            try:
                self._apply_limit_row(ident, r)
            except Exception as ex:  # noqa: BLE001 - shown as the journal has it
                log.warning("open order %s: %s", ident, ex)
            v = limit_view(ident, self.journal.get(ident))
            if v["state"] in (LIMIT_OPEN, LIMIT_PARTIAL):
                out.append(v)
        return out

    def cancel(self, order, wait=True):
        """Cancel one of the bot's limit orders (see the module docstring). The cancel request is
        journaled first, so a restart re-sends it until the exchange has closed the order; a fill
        that races the cancel is recorded (state 'filled', or 'cancelled' with the partial fill).
        wait=True polls the order up to cancel_poll_attempts times (poll_interval apart) until the
        exchange reports it final; wait=False reads it once (Bitpin closes cancels asynchronously:
        the order may still show as open; sync_limits records the outcome later)."""
        ident = _ident_of(order)
        e = self.journal.get(ident)
        if e is None or e.get("kind") != "limit":
            raise NotBotOrder("%s is not a limit order this bot placed: not touched" % ident)
        if limit_state_of(e) in LIMIT_FINAL_STATES:
            return limit_view(ident, e)
        self._journal_update(ident, cancel_requested=True, cancel_requested_at=self.clock())
        oid = e.get("order_id")
        if not oid:
            try:
                row = self.client.find_order_by_identifier(ident)
            except (AuthError, AuthBudgetExceeded):
                raise
            except Exception as ex:  # noqa: BLE001 - sync_limits cancels it once it is found
                log.warning("cancel %s: lookup by identifier failed (%s); the cancel is re-sent when it is found",
                            ident, ex)
                return limit_view(ident, self.journal.get(ident))
            if row is None:
                log.warning("cancel %s: the order is not on the exchange (yet); the cancel is re-sent if it appears",
                            ident)
                return limit_view(ident, self.journal.get(ident))
            self._apply_limit_row(ident, row)
            oid = self.journal.get(ident).get("order_id")
            if limit_state_of(self.journal.get(ident)) in LIMIT_FINAL_STATES or not oid:
                return limit_view(ident, self.journal.get(ident))
        try:
            gone = self.client.cancel_order(oid)
            if gone is False:
                log.info("cancel %s: order id %s no longer exists (HTTP 404); reading its final state", ident, oid)
        except (AuthError, AuthBudgetExceeded):
            raise
        except BitpinError as ex:
            # e.g. HTTP 406 'not allowed' when it has just filled; the lookup below tells
            log.warning("cancel %s (order id %s) failed: %s; reading its state", ident, oid, ex)
            self._journal_update(ident, error=("cancel: %s" % ex)[:300])
        self._totals = self._avail = None
        for i in range(max(1, self.cancel_poll_attempts) if wait else 1):
            if i:
                self.sleep(self.poll_interval)
            try:
                row = self._lookup(ident, oid)
            except (AuthError, AuthBudgetExceeded):
                raise
            except Exception as ex:  # noqa: BLE001
                log.warning("cancel %s: state lookup failed: %s", ident, ex)
                continue
            if row is None:
                continue
            try:
                self._apply_limit_row(ident, row)
            except Exception as ex:  # noqa: BLE001
                log.warning("cancel %s: %s", ident, ex)
                continue
            if order_is_final(row):
                break
        return limit_view(ident, self.journal.get(ident))
