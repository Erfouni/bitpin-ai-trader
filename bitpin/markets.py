"""Market metadata cache, Decimal money helpers, order-book arithmetic and routing (stdlib only).

Bitpin's /api/v1/mkt/markets/ returns precisions as integers = number of decimal places
(verified: BTC_IRT base 8 / quote 0 / price 0, USDT_IRT base 2, PEPE_IRT base 0; BTC_USDT
price 2 / base 8 / quote 2, ETH_USDT 2/5/2, XRP_USDT 5/4/2, SOL_USDT 3/4/2 on 2026-09-23). Amounts
sent to the exchange are always floored (ROUND_DOWN) to these precisions so we never ask for more
than we have. Limit prices are rounded in the SAFE direction for the side (buy: down, sell: up).

Two quote currencies are supported: IRT (toman) and USDT. Notional amounts (min-order checks, order
sizes) are always in the market's own quote currency; see RiskManager.min_order_for().

Routing (best_route): USDT <-> COIN goes either direct through COIN_USDT (1 leg per direction, a
round trip is 2 legs) or through toman (sell USDT_IRT + buy COIN_IRT: 2 legs per direction, 4 per
round trip). Both are simulated on the live books - taker fees + half spreads + depth impact for the
size - and the route that delivers more of the target asset wins. IRT <-> COIN and IRT <-> USDT are
single-leg as before.
"""
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation, localcontext

from .api import atomic_write_json, read_json

log = logging.getLogger("bitpin.markets")

SYMBOL_RE = re.compile(r"^([A-Z0-9]+)_([A-Z0-9]+)$")
MARKETS_CACHE_FILE = "markets_cache.json"
ZERO = Decimal(0)

IRT = "IRT"
USDT = "USDT"
SUPPORTED_QUOTES = (IRT, USDT)
# Bitpin publishes no minimum order size. 100,000 toman (the live min_order_irt) was about 0.44 USDT
# at USDT_IRT ~228,600 (2026-09-23); rounded up to 0.5 USDT. A config value (risk.min_order_usdt).
DEFAULT_MIN_ORDER_USDT = Decimal("0.5")
DEFAULT_TAKER_FEE = Decimal("0.0035")   # Bitpin taker, COIN_IRT and COIN_USDT alike (verified live)
DEFAULT_MAKER_FEE = Decimal("0.003")    # Bitpin maker


def D(x):
    """Convert an API number (str / int / Decimal; floats via repr) to a finite Decimal."""
    if x is None or isinstance(x, bool):
        raise ValueError("not a number: %r" % (x,))
    if isinstance(x, Decimal):
        d = x
    elif isinstance(x, int):
        d = Decimal(x)
    elif isinstance(x, float):
        d = Decimal(repr(x))
    else:
        try:
            d = Decimal(str(x).strip())
        except InvalidOperation:
            raise ValueError("not a number: %r" % (x,))
    if not d.is_finite():
        raise ValueError("not a finite number: %r" % (x,))
    return d


def parse_symbol(symbol):
    """'btc_irt' -> ('BTC', 'IRT')."""
    m = SYMBOL_RE.match(str(symbol).strip().upper())
    if not m:
        raise ValueError("bad market symbol %r (expected BASE_QUOTE, e.g. BTC_IRT)" % (symbol,))
    return m.group(1), m.group(2)


def _step(precision):
    """int / int-like string = decimal places (may be negative); a string with '.' = step size."""
    if isinstance(precision, bool):
        raise ValueError("bad precision %r" % (precision,))
    if isinstance(precision, int):
        return Decimal(1).scaleb(-precision)
    s = str(precision).strip()
    if "." in s or "e" in s.lower():
        step = D(s)
        if step <= 0:
            raise ValueError("bad precision step %r" % (precision,))
        return step
    return Decimal(1).scaleb(-int(s))


def _round_to(value, precision, rounding):
    value = D(value)
    step = _step(precision)
    with localcontext() as ctx:
        ctx.prec = 60
        n = (value / step).to_integral_value(rounding=rounding)
        out = n * step
        return out.quantize(Decimal(1) if step >= 1 else step)


def floor_to_precision(value, precision):
    """Truncate toward zero to `precision` (decimal places, or a step like '0.05')."""
    return _round_to(value, precision, ROUND_DOWN)


def ceil_to_precision(value, precision):
    return _round_to(value, precision, ROUND_UP)


def fmt_amount(x, places=None):
    """Human formatting with thousands separators (ASCII only)."""
    x = D(x)
    if places is None:
        places = 0 if abs(x) >= 1000 else 8
    q = x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN) if places >= 0 else x
    s = "{:,}".format(q)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


@dataclass(frozen=True)
class Market:
    symbol: str
    base: str
    quote: str
    tradable: bool = True
    suspended: bool = False
    price_precision: int = 8
    base_amount_precision: int = 8
    quote_amount_precision: int = 8

    @classmethod
    def from_api(cls, d):
        sym = str(d["symbol"]).upper()
        base, quote = parse_symbol(sym)
        return cls(sym, str(d.get("base") or base).upper(), str(d.get("quote") or quote).upper(),
                   bool(d.get("tradable", True)), bool(d.get("suspended", False)),
                   _int(d.get("price_precision"), 8), _int(d.get("base_amount_precision"), 8),
                   _int(d.get("quote_amount_precision"), 8))

    @property
    def is_trading(self):
        return self.tradable and not self.suspended

    def floor_base(self, x):
        return floor_to_precision(x, self.base_amount_precision)

    def floor_quote(self, x):
        return floor_to_precision(x, self.quote_amount_precision)

    def floor_price(self, x):
        return floor_to_precision(x, self.price_precision)

    def ceil_price(self, x):
        return ceil_to_precision(x, self.price_precision)

    def limit_price(self, x, side):
        """A limit price on this market's price grid, rounded in the safe direction: a buy never
        pays more (down), a sell never sells for less (up)."""
        if side == "buy":
            return self.floor_price(x)
        if side == "sell":
            return self.ceil_price(x)
        raise ValueError("side must be buy or sell")

    @property
    def base_step(self):
        return _step(self.base_amount_precision)

    @property
    def quote_step(self):
        return _step(self.quote_amount_precision)


def quote_of(symbol):
    """'BTC_USDT' -> 'USDT'."""
    return parse_symbol(symbol)[1]


def _int(x, default):
    if x is None:
        return default
    return int(x)


class MarketCache:
    """Market metadata from /api/v1/mkt/markets/, cached in memory and in state_dir for `ttl` s.
    If a refresh fails, a stale cache younger than `max_stale` is used (with a warning)."""

    def __init__(self, client=None, state_dir=None, ttl=6 * 3600, max_stale=7 * 86400, clock=time.time, markets=None):
        self.client, self.ttl, self.max_stale, self.clock = client, ttl, max_stale, clock
        self.path = os.path.join(state_dir, MARKETS_CACHE_FILE) if state_dir else None
        self._markets = {}
        self._fetched_at = 0.0
        if markets is not None:  # static (tests / offline)
            self._markets = {m.symbol: m for m in markets}
            self._fetched_at = float("inf")
        elif self.path:
            data = read_json(self.path) or {}
            try:
                self._markets = {m["symbol"]: Market(**m) for m in data.get("markets", [])}
                self._fetched_at = float(data.get("fetched_at") or 0)
            except (TypeError, KeyError):
                self._markets, self._fetched_at = {}, 0.0

    def refresh(self):
        rows = self.client.markets()
        ms = {}
        for r in rows:
            try:
                m = Market.from_api(r)
            except (KeyError, ValueError, TypeError):
                continue
            ms[m.symbol] = m
        if not ms:
            raise RuntimeError("markets endpoint returned no usable markets")
        self._markets, self._fetched_at = ms, self.clock()
        if self.path:
            atomic_write_json(self.path, {"fetched_at": self._fetched_at, "markets": [asdict(m) for m in ms.values()]})

    def _ensure(self):
        age = self.clock() - self._fetched_at
        if self._markets and age < self.ttl:
            return
        if self.client is None:
            if self._markets:
                return
            raise RuntimeError("no market metadata available")
        try:
            self.refresh()
        except Exception as e:  # noqa: BLE001
            if self._markets and age < self.max_stale:
                log.warning("market metadata refresh failed (%s); using cache %.1f h old", e, age / 3600)
                return
            raise

    def get(self, symbol):
        self._ensure()
        s = str(symbol).strip().upper()
        if s not in self._markets:
            raise KeyError("unknown market %s" % s)
        return self._markets[s]

    def all(self):
        self._ensure()
        return dict(self._markets)


# --------------------------------------------------------------------------- order book

def parse_book(raw):
    """{"asks": [[price, amount], ...], "bids": [...]} (strings) -> Decimal tuples, asks ascending,
    bids descending, non-positive levels dropped."""
    def side(rows, reverse):
        out = []
        for r in rows or []:
            try:
                p, a = D(r[0]), D(r[1])
            except (ValueError, IndexError, TypeError):
                continue
            if p > 0 and a > 0:
                out.append((p, a))
        out.sort(key=lambda x: x[0], reverse=reverse)
        return out
    raw = raw or {}
    return {"asks": side(raw.get("asks"), False), "bids": side(raw.get("bids"), True)}


def best_bid(book):
    return book["bids"][0][0] if book.get("bids") else None


def best_ask(book):
    return book["asks"][0][0] if book.get("asks") else None


def mid_price(book):
    b, a = best_bid(book), best_ask(book)
    if b is None or a is None:
        return None
    return (a + b) / 2


def walk_book(book, side, quote_amount=None, base_amount=None, base_precision=None):
    """Simulate a market order against the book.
    buy: spend up to `quote_amount` on asks (lowest first); sell: sell `base_amount` into bids.
    Returns {"base", "quote", "avg_price", "complete", "levels"} (gross, before fees)."""
    base = quote = ZERO
    levels = 0
    if side == "buy":
        if quote_amount is None:
            raise ValueError("buy walk needs quote_amount")
        remaining = D(quote_amount)
        for price, amount in book["asks"]:
            if remaining <= 0:
                break
            cost = price * amount
            if cost <= remaining:
                base += amount
                quote += cost
                remaining -= cost
                levels += 1
            else:
                take = remaining / price
                if base_precision is not None:
                    take = floor_to_precision(take, base_precision)
                if take > 0:
                    base += take
                    quote += take * price
                    levels += 1
                remaining = ZERO
                break
        complete = remaining <= 0
    elif side == "sell":
        if base_amount is None:
            raise ValueError("sell walk needs base_amount")
        remaining = D(base_amount)
        for price, amount in book["bids"]:
            if remaining <= 0:
                break
            take = min(amount, remaining)
            base += take
            quote += take * price
            remaining -= take
            levels += 1
        complete = remaining <= 0
    else:
        raise ValueError("side must be buy or sell")
    avg = (quote / base) if base > 0 else None
    return {"base": base, "quote": quote, "avg_price": avg, "complete": complete, "levels": levels}


def slippage_vs_mid(book, side, avg_price):
    """Adverse deviation of avg fill price from the mid (fraction, >= spread/2)."""
    mid = mid_price(book)
    if mid is None or avg_price is None:
        return None
    return (avg_price / mid - 1) if side == "buy" else (1 - avg_price / mid)


def max_fill_within(book, side, limit_avg_price):
    """Largest (base, quote) fill whose AVERAGE price stays within `limit_avg_price`
    (<= for buys, >= for sells), using only visible levels."""
    L = D(limit_avg_price)
    base = quote = ZERO
    levels = book["asks"] if side == "buy" else book["bids"]
    for price, amount in levels:
        ok_full = (price <= L) if side == "buy" else (price >= L)
        if ok_full:
            base += amount
            quote += price * amount
            continue
        # the largest x taken at this worse price that keeps the average at L
        if side == "buy":
            x = (L * base - quote) / (price - L)
        else:
            x = (quote - L * base) / (L - price)
        if x >= amount:  # the whole level still fits under the limit: keep walking
            base += amount
            quote += price * amount
            continue
        if x > 0:
            base += x
            quote += x * price
        break
    return base, quote


# --------------------------------------------------------------------------- limit orders

def limit_crosses(book, side, price):
    """True if a limit order at `price` would trade IMMEDIATELY against `book` (a buy at or above
    the best ask, a sell at or below the best bid), i.e. it would be a taker, not a resting maker
    order. Bitpin has no post-only flag, so this check against the live book is how the caller keeps
    a 'post-only' intent. An empty opposite side never crosses."""
    price = D(price)
    if side == "buy":
        a = best_ask(book)
        return a is not None and price >= a
    if side == "sell":
        b = best_bid(book)
        return b is not None and price <= b
    raise ValueError("side must be buy or sell")


def walk_limit(book, side, base_amount, price):
    """The part of a limit order at `price` for `base_amount` that would execute immediately against
    the visible book (levels at or better than the limit). Gross, before fees.
    Returns {"base", "quote", "avg_price", "levels"}."""
    price, remaining = D(price), D(base_amount)
    base = quote = ZERO
    levels = 0
    rows = book["asks"] if side == "buy" else book["bids"]
    for p, a in rows:
        if remaining <= 0 or (side == "buy" and p > price) or (side == "sell" and p < price):
            break
        take = min(a, remaining)
        base += take
        quote += take * p
        remaining -= take
        levels += 1
    return {"base": base, "quote": quote, "avg_price": (quote / base) if base > 0 else None, "levels": levels}


# --------------------------------------------------------------------------- routing

class RouteError(ValueError):
    """No route can exist between two assets (one side must be IRT or USDT)."""


def route_options(from_asset, to_asset, markets, irt=IRT, usdt=USDT):
    """Candidate routes as [(name, [(symbol, side), ...]), ...], direct first.
    IRT <-> X: one leg on X_IRT ('single'). USDT <-> COIN: 'direct' on COIN_USDT (when that market
    exists) and 'via_irt' (USDT_IRT + COIN_IRT)."""
    f, t = str(from_asset).strip().upper(), str(to_asset).strip().upper()
    if f == t:
        raise RouteError("from and to are the same asset (%s)" % f)

    def exists(sym):
        try:
            markets.get(sym)
            return True
        except KeyError:
            return False

    if f == irt:
        return [("single", [("%s_%s" % (t, irt), "buy")])]
    if t == irt:
        return [("single", [("%s_%s" % (f, irt), "sell")])]
    if f == usdt:
        out = [("direct", [("%s_%s" % (t, usdt), "buy")])] if exists("%s_%s" % (t, usdt)) else []
        return out + [("via_irt", [("%s_%s" % (usdt, irt), "sell"), ("%s_%s" % (t, irt), "buy")])]
    if t == usdt:
        out = [("direct", [("%s_%s" % (f, usdt), "sell")])] if exists("%s_%s" % (f, usdt)) else []
        return out + [("via_irt", [("%s_%s" % (f, irt), "sell"), ("%s_%s" % (usdt, irt), "buy")])]
    raise RouteError("no route from %s to %s: one side must be %s or %s" % (f, t, irt, usdt))


def simulate_leg(market, side, amount_in, book, taker_fee=DEFAULT_TAKER_FEE):
    """One taker leg on the visible book. `amount_in` = quote to spend (buy) or base to sell (sell),
    floored to the market's precision. The fee is charged in the received asset (as on Bitpin).
    exec_cost = 1 - received / (amount_in converted at the book mid) = fee + half spread + depth
    impact (None without a two-sided book)."""
    fee = D(taker_fee)
    mid = mid_price(book)
    if side == "buy":
        amt = market.floor_quote(amount_in)
        w = walk_book(book, "buy", quote_amount=amt, base_precision=market.base_amount_precision)
        gross = w["base"]
        fair = (amt / mid) if mid else None
        notional = amt
        asset_in, asset_out = market.quote, market.base
    elif side == "sell":
        amt = market.floor_base(amount_in)
        w = walk_book(book, "sell", base_amount=amt)
        gross = w["quote"]
        fair = (amt * mid) if mid else None
        notional = (amt * mid) if mid else w["quote"]
        asset_in, asset_out = market.base, market.quote
    else:
        raise ValueError("side must be buy or sell")
    net = gross * (1 - fee)
    return {"symbol": market.symbol, "side": side, "asset_in": asset_in, "asset_out": asset_out, "amount_in": amt,
            "est_gross": gross, "est_out": net, "avg_price": w["avg_price"], "mid": mid, "fair_out": fair,
            "complete": bool(w["complete"] and amt > 0 and gross > 0), "notional": notional, "quote": market.quote,
            "exec_cost": (1 - net / fair) if fair else None}


def best_route(from_asset, to_asset, amount, get_book, markets, taker_fee=DEFAULT_TAKER_FEE, min_notional=None):
    """Cheapest way to convert `amount` of from_asset into to_asset with taker (market) orders.

    get_book(symbol) -> parsed book (parse_book format); markets: MarketCache-like (.get(symbol)).
    min_notional: optional {quote_asset: minimum order notional in that quote}, e.g.
    {"IRT": 100000, "USDT": 0.5}; a leg below it makes that route infeasible.

    Every candidate route (route_options) is simulated leg by leg on the live books (each leg's
    input is the previous leg's estimated net output). The feasible route (all legs tradable,
    visible depth sufficient, every leg above the minimum) that delivers the MOST of to_asset wins
    (ties: fewer legs). 'est_out' is net of fees. 'exec_cost' is the route's all-in cost against its
    own book mids (fees + half spreads + depth impact); 'cost_frac' is the shortfall against ONE
    common reference (the first route's chain of mids, i.e. the direct market when it exists), so it
    also contains the price gap between the routes and orders them like est_out.

    Returns {"ok", "from", "to", "amount", "route", "legs", "est_out", "exec_cost", "cost_frac",
    "complete", "reason", "alternatives"}. ok is False (with the best infeasible route) when no
    route is feasible. Raises RouteError when no route can exist at all."""
    amount = D(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    f, t = str(from_asset).strip().upper(), str(to_asset).strip().upper()
    options = route_options(f, t, markets)
    mins = {str(k).upper(): D(v) for k, v in (min_notional or {}).items() if v is not None}
    books = {}

    def book_for(sym):
        if sym not in books:
            books[sym] = get_book(sym)
        return books[sym]

    routes = []
    for name, legs in options:
        r = {"route": name, "legs": [], "n_legs": len(legs), "est_out": ZERO, "fair_out": None, "complete": False,
             "ok": False, "reason": None, "exec_cost": None, "cost_frac": None}
        amt = fair = amount
        try:
            for sym, side in legs:
                m = markets.get(sym)
                if not m.is_trading:
                    raise RouteError("%s is not tradable (suspended)" % sym)
                leg = simulate_leg(m, side, amt, book_for(sym), taker_fee)
                r["legs"].append(leg)
                amt = leg["est_out"]
                mid = leg["mid"]
                fair = None if (fair is None or not mid) else (fair / mid if side == "buy" else fair * mid)
        except Exception as e:  # noqa: BLE001 - an unavailable route is reported, never fatal
            r["reason"] = "%s: %s" % (type(e).__name__, e)
            routes.append(r)
            continue
        r["est_out"], r["fair_out"] = amt, fair
        r["complete"] = all(leg["complete"] for leg in r["legs"])
        r["exec_cost"] = (1 - amt / fair) if fair else None
        below = [leg for leg in r["legs"] if leg["quote"] in mins and leg["notional"] < mins[leg["quote"]]]
        if not r["complete"]:
            r["reason"] = "visible order book too thin (or empty) for this size on %s" % ", ".join(
                leg["symbol"] for leg in r["legs"] if not leg["complete"])
        elif below:
            r["reason"] = "below the minimum order on %s" % ", ".join(
                "%s (%s < %s %s)" % (leg["symbol"], fmt_amount(leg["notional"]), fmt_amount(mins[leg["quote"]]),
                                     leg["quote"]) for leg in below)
        else:
            r["ok"] = True
        routes.append(r)
    simulated = [r for r in routes if len(r["legs"]) == r["n_legs"]]
    ref = next((r["fair_out"] for r in simulated if r["fair_out"]), None)
    for r in simulated:
        if ref:
            r["cost_frac"] = 1 - r["est_out"] / ref
    ok = [r for r in routes if r["ok"]]
    pool = ok or simulated
    best = max(pool, key=lambda r: (r["est_out"], -r["n_legs"])) if pool else routes[0]
    reason = None if best["ok"] else (best["reason"] or "no feasible route")
    keys = ("route", "legs", "est_out", "exec_cost", "cost_frac", "complete", "ok", "reason")
    return {"ok": bool(best["ok"]), "from": f, "to": t, "amount": amount, "route": best["route"], "legs": best["legs"],
            "est_out": best["est_out"], "exec_cost": best["exec_cost"], "cost_frac": best["cost_frac"],
            "complete": best["complete"], "reason": reason,
            "alternatives": [{k: r[k] for k in keys} for r in routes if r is not best]}
