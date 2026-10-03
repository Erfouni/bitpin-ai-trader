"""v3.6 "performance": the account's profit and loss from the bot's own records - in toman and in USDT (without
the rial's fall) - for any time range and per asset, the trade history and the series of the panel's charts.

Read-only and without keys: collect() reads the bot's state files (kimi_runner.jsonl: one record per hourly cycle
with the equity and the fills; kimi_equity.json: the start value; live_orders.json: the resting orders) and PUBLIC
Bitpin candles for the prices; report() computes from plain data (the tests feed it synthetic records).

How a range is measured (t_from, t_to]:
* totals come from the equity the bot recorded at its hourly cycles (the value at t_from is the last record at or
  before it; before the first record, the start value of the account);
* in USDT = every toman value divided by the USDT_IRT rate of the same hour: the rial's fall is taken out;
* per asset (coins, USDT and the toman cash): P&L = value at t_to - value at t_from - what was paid into the asset
  + what came out of it, each trade at its own price (fees count against the asset traded). The holdings are
  rebuilt from the fills, starting from the start value in toman; the rows add up to the change of that rebuilt
  account (a mismatch with the recorded equity, e.g. after a manual trade, is reported as a warning);
* a range that ends now also gets an estimate of the account value at this minute (the last recorded value moved
  by the latest prices) and the open positions with their resting orders and price charts.

The outlook of an open position (v3.6.1), drawn from now to the end of its plan (its max hold time):
* Kimi's own analysis of the coin from its newest valid decision (kimi_decisions.jsonl, analysis.candidates: the
  setup, the base-rate row, the evidence, the bear case, p = P(take profit before an hourly close below the
  invalidation within the horizon), p0 = the same for a driftless walk, gain / loss / cost / ev in percent);
* two scenarios, the take profit (p) and the invalidation (1 - p), and their probability-weighted price;
* the range a driftless random walk with the coin's recent hourly volatility stays in with 68% / 95% probability
  (price x exp(+-z sigma sqrt(hours))): the "normal" move, the null hypothesis of Kimi's own p0.

Prices: hourly candles for the last FINE_DAYS days, 4-hour candles before that (a year of history stays one small
request per market, even with the panel's page reloading every minute).

v3.9, the indicators on a position's chart: for the markets of the open positions and resting orders (and USDT_IRT)
the hourly candles reach back TA_FETCH_HOURS (the bot's own context window), and bitpin.technical.chart_data()
computes the EMA 20 / 50 / 200, Bollinger, Donchian, RSI, MACD and traded value series exactly like the bot's
market context; each position gets them for its chart span ("ta").
"""
import bisect
import json
import math
import os
import time

from . import technical

RUNNER_LOG = "kimi_runner.jsonl"
EQUITY_FILE = "kimi_equity.json"
ORDERS_FILE = "live_orders.json"
DECISIONS_FILE = "kimi_decisions.jsonl"
MAX_DECISIONS_BYTES = 8 * 1024 * 1024  # the tail read for Kimi's newest analysis of each coin
MAX_RUNNER_BYTES = 128 * 1024 * 1024
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024
HOUR = 3600
COARSE_SECONDS = 4 * HOUR         # the candles older than FINE_DAYS
FINE_DAYS = 10
MAX_RANGE_DAYS = 400
MAX_HISTORY = 5000                # the newest trades of a range listed (the account is rebuilt from all of them)
CHART_POINTS = 360                # a series is thinned to at most this many points
POSITION_CHART_HOURS = 7 * 24     # a position's price chart covers at least this much ...
POSITION_CHART_MAX_DAYS = 30      # ... and at most this much (from 12 hours before its plan was set)
CASH = "IRT"
UNIT = "USDT"
RESTING = ("resting", "submitting", "unknown")
MISMATCH_WARN = 0.02              # rebuilt vs recorded equity
ROUNDING_FRACTION = 0.001         # v3.9.1: a fill may leave its side this far below zero (0.1% of the fill)
DUST_USDT = 1.0                   # v3.10: less than this, without a plan of the bot: a leftover, not a position
FORECAST_DEFAULT_HOURS = 72       # the outlook of a position without a plan in force (no max hold ahead)
CONE_STEPS = 24                   # points of the volatility range
SIGMA_MIN_RETURNS = 24            # hourly returns needed for the volatility range
ANALYSIS_TEXT_MAX = 400
TA_FETCH_HOURS = technical.CHART_LOOKBACK_HOURS + 8     # v3.9: the hourly candles behind a position's indicators


def _f(x):
    """A finite float, or None."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _read_json(path, limit=MAX_SMALL_FILE_BYTES):
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_runner_log(path, limit=MAX_RUNNER_BYTES):
    """The records of kimi_runner.jsonl (only the fields used here), oldest first; a bad line is skipped."""
    out = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
                f.readline()                           # the first line may be cut: skip it
            for raw in f:
                try:
                    r = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(r, dict):
                    out.append({k: r.get(k) for k in ("time", "equity_irt", "fills", "positions")})
    except OSError:
        return []
    return out


# --------------------------------------------------------------------------- the records
def parse_fills(records):
    """[{t, symbol, asset, quote_asset, side, base, quote, fee, fee_asset, reason, route, order_id}] of every fill
    the bot recorded (a fill made between two cycles carries the time of the cycle that booked it), oldest first."""
    out = []
    for r in records:
        t = _f(r.get("time"))
        fills = r.get("fills")
        if t is None or not isinstance(fills, list):
            continue
        for f in fills:
            if not isinstance(f, dict):
                continue
            sym = str(f.get("symbol") or "").upper()
            if sym.count("_") != 1:
                continue
            asset, market_quote = sym.split("_")
            side = f.get("side")
            base, quote = _f(f.get("base")), _f(f.get("quote"))
            if side not in ("buy", "sell") or not base or not quote or base <= 0 or quote <= 0:
                continue
            fee = _f(f.get("fee")) or 0.0
            out.append({"t": t, "symbol": sym, "asset": asset,
                        "quote_asset": str(f.get("quote_asset") or market_quote).upper(), "side": side,
                        "base": base, "quote": quote, "fee": max(0.0, fee),
                        "fee_asset": str(f.get("fee_asset") or "").upper(), "reason": str(f.get("reason") or "")[:40],
                        "route": str(f.get("route") or "")[:20], "order_id": str(f.get("order_id") or "")[:40]})
    out.sort(key=lambda x: x["t"])
    return out


def equity_points(records):
    """[(time, equity in toman)] of the hourly cycles, oldest first."""
    pts = []
    for r in records:
        t, e = _f(r.get("time")), _f(r.get("equity_irt"))
        if t is not None and e is not None and e > 0:
            pts.append((t, e))
    pts.sort()
    return pts


def resting_orders(orders_doc):
    """[{asset, symbol, side, price, amount, tag, quote_asset}] of the bot's orders still on the book."""
    out = []
    orders = orders_doc.get("orders") if isinstance(orders_doc, dict) else None
    if not isinstance(orders, dict):
        return out
    for o in orders.values():
        if not isinstance(o, dict) or o.get("status") not in RESTING or o.get("kind") != "limit":
            continue
        sym = str(o.get("symbol") or "").upper()
        price, amount = _f(o.get("price")), _f(o.get("base_amount"))
        if sym.count("_") != 1 or not price or o.get("side") not in ("buy", "sell"):
            continue
        asset, quote = sym.split("_")
        out.append({"asset": asset, "symbol": sym, "side": o["side"], "price": price, "amount": amount,
                    "tag": str(o.get("tag") or "")[:20], "quote_asset": quote})
    return out


def open_plans(records):
    """{symbol: plan} of the positions in the newest record (entry / stop / target in USDT, the plan's time)."""
    last = records[-1] if records else {}
    pos = last.get("positions") if isinstance(last, dict) else None
    return {str(k).upper(): v for k, v in pos.items() if isinstance(v, dict)} if isinstance(pos, dict) else {}


def _plan_time(plan):
    inner = plan.get("plan") if isinstance(plan.get("plan"), dict) else {}
    return _f(inner.get("set_at"))


def _text(v, n=ANALYSIS_TEXT_MAX):
    return " ".join(v.split())[:n] if isinstance(v, str) else ""


def read_analyses(path, limit=MAX_DECISIONS_BYTES):
    """{asset: Kimi's newest analysis of it} from the candidates of the newest VALID decisions in
    kimi_decisions.jsonl: symbol, time, setup, row, verdict, p0, p, gain_pct, loss_pct, cost_pct, ev_pct, pass,
    evidence, bear. The two texts are taken from Kimi's reply as written when it can be read (the decision keeps
    a copy cleaned for the prompt: "=", "<" and "[ ]" removed)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
                f.readline()                           # the first line may be cut: skip it
            lines = f.read().splitlines()
    except OSError:
        return {}
    out = {}
    for raw in reversed(lines):
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        dec = rec.get("decision") if isinstance(rec, dict) else None
        if not isinstance(dec, dict) or dec.get("valid") is not True or dec.get("fallback"):
            continue
        ana = dec.get("analysis")
        cands = ana.get("candidates") if isinstance(ana, dict) else None
        if not isinstance(cands, dict):
            continue
        try:
            reply = json.loads(rec.get("response") or "")
            reply = reply["analysis"]["candidates"]
        except (TypeError, ValueError, KeyError):
            reply = {}
        reply = reply if isinstance(reply, dict) else {}
        t = _f(dec.get("decided_at")) or _f(rec.get("time"))
        for sym, c in cands.items():
            sym = str(sym).upper()
            asset = sym.split("_")[0]
            if not isinstance(c, dict) or sym.count("_") != 1 or asset in out:
                continue
            rc = reply.get(sym) if isinstance(reply.get(sym), dict) else {}
            item = {"symbol": sym, "time": t, "pass": c.get("pass") is True}
            for k in ("setup", "row", "verdict"):
                item[k] = str(c.get(k) or "")[:40]
            for k in ("p0", "p", "gain_pct", "loss_pct", "cost_pct", "ev_pct"):
                item[k] = _f(c.get(k))
            for k in ("evidence", "bear"):
                item[k] = _text(rc.get(k)) or _text(c.get(k))
            if isinstance(c.get("ta"), dict):           # v3.8: the technical reading and the code's check
                from .technical import parse_ta
                item["ta"] = parse_ta(c.get("ta"))[0]
                item["ta_check"] = [x for x in (c.get("ta_check") or []) if isinstance(x, dict)][:12]
            out[asset] = item
    return out


def hourly_sigma(line):
    """The standard deviation of the hourly log returns of [[time, price], ...] (a longer step, e.g. a 4-hour
    candle, is scaled by sqrt(hours)); None with fewer than SIGMA_MIN_RETURNS returns."""
    rets = []
    for (t0, p0), (t1, p1) in zip(line, line[1:]):
        dt = (t1 - t0) / float(HOUR)
        if p0 and p1 and p0 > 0 and p1 > 0 and 0.5 <= dt <= 8:
            rets.append(math.log(p1 / p0) / math.sqrt(dt))
    if len(rets) < SIGMA_MIN_RETURNS:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))


def outlook(price, now, end, sigma_h, target=None, invalidation=None, p=None, origin=None):
    """The drawing data of a position's outlook: the 68% / 95% range of a driftless walk from now to end
    ([[time, low1, high1, low2, high2], ...]); the two scenarios, drawn from `origin` ([time, price] where they
    were set: Kimi's decision, else the plan) to end; the probability-weighted price at end (Kimi's p)."""
    out = {"from": now, "to": end, "price": price, "sigma_h": sigma_h, "cone": [], "target": target,
           "invalidation": invalidation, "p": p, "expected": None, "origin": origin}
    if price and price > 0 and sigma_h and end > now:
        for i in range(CONE_STEPS + 1):
            t = now + (end - now) * i / float(CONE_STEPS)
            s = sigma_h * math.sqrt((t - now) / float(HOUR))
            out["cone"].append([t, price * math.exp(-s), price * math.exp(s), price * math.exp(-2 * s),
                                price * math.exp(2 * s)])
    if p is not None and 0 <= p <= 1 and target and invalidation and target > invalidation:
        out["expected"] = p * target + (1 - p) * invalidation
    return out


def _origin(analysis, plan, line):
    """[time, USDT price] the scenarios start from: Kimi's decision (its price: take profit / (1 + gain)), else the
    plan (its px_usdt when it was set), else None; the chart's own price at that time when neither gives one."""
    inner = plan.get("plan") if isinstance(plan.get("plan"), dict) else {}
    t = px = None
    if analysis and analysis.get("time"):
        t = analysis["time"]
        tp = _f(plan.get("target_px_usdt")) or _f(inner.get("take_profit_usdt"))
        inv = _f(inner.get("invalidation_usdt"))
        g, l = analysis.get("gain_pct"), analysis.get("loss_pct")
        if tp and g is not None and g > -100:
            px = tp / (1.0 + g / 100.0)
        elif inv and l is not None and l < 100:
            px = inv / (1.0 - l / 100.0)
    elif _plan_time(plan) is not None:
        t, px = _plan_time(plan), _f(inner.get("px_usdt"))
    if t is None:
        return None
    if not px:
        before = [q for q in line if q[0] <= t]
        px = (before[-1] if before else (line[0] if line else [None, None]))[1]
    return [t, px] if px else None


def outlook_end(plan, now):
    """Where a position's outlook ends: its max hold time when that is ahead (at most 30 days), else 72 h."""
    hold = _f(plan.get("max_hold_until")) if isinstance(plan, dict) else None
    if hold is not None and now + HOUR < hold <= now + POSITION_CHART_MAX_DAYS * 86400:
        return hold
    return now + FORECAST_DEFAULT_HOURS * HOUR


# --------------------------------------------------------------------------- prices
class Prices(object):
    """Closes in toman: closes[asset] = (close times, closes) of ASSET_IRT; USDT_IRT is the rial's rate.
    irt(asset, t) is the last close at or before t (before the first bar: the first close); latest(asset) the
    newest price (the running bar included)."""

    def __init__(self, closes, latest=None):
        self.closes = closes
        self._latest = latest or {}

    def irt(self, asset, t):
        if asset == CASH:
            return 1.0
        ts, cs = self.closes.get(asset) or ((), ())
        if not cs:
            return None
        k = bisect.bisect_right(ts, t) - 1
        return cs[max(0, k)]

    def latest(self, asset):
        if asset == CASH:
            return 1.0
        v = self._latest.get(asset)
        if v is not None:
            return v
        cs = (self.closes.get(asset) or ((), ()))[1]
        return cs[-1] if cs else None

    def at(self, asset, t, now):
        """The price at t; at or after the last closed hour, the newest price."""
        ts = (self.closes.get(asset) or ((), ()))[0]
        if asset != CASH and ts and t >= ts[-1] and now is not None and t >= now - HOUR:
            return self.latest(asset)
        return self.irt(asset, t)

    def usdt(self, asset, t, now=None):
        u, p = self.at(UNIT, t, now), self.at(asset, t, now)
        if asset == UNIT:
            return 1.0
        return None if not u or p is None else p / u


def prices_from_bars(bars_by_asset, now, coarse=None):
    """Prices from {asset: [Bar]} of hourly candles and, optionally, {asset: [Bar]} of 4-hour candles for the
    older part (ts = the bar's OPEN time, as bitpin.data returns them). Only closed bars count; where both
    cover the same time, the hourly ones win."""
    coarse = coarse or {}
    closes, latest = {}, {}
    for asset in set(bars_by_asset) | set(coarse):
        fine = bars_by_asset.get(asset) or []
        ts, cs = [], []
        for bars, step in ((coarse.get(asset) or [], COARSE_SECONDS), (fine, HOUR)):
            done = [(b.ts + step, float(b.close)) for b in bars if b.ts + step <= now]
            if not done:
                continue
            while ts and ts[-1] >= done[0][0]:
                ts.pop()
                cs.pop()
            ts.extend(t for t, _c in done)
            cs.extend(c for _t, c in done)
        closes[asset] = (ts, cs)
        newest = fine or coarse.get(asset)
        if newest:
            latest[asset] = float(newest[-1].close)
    return Prices(closes, latest)


# --------------------------------------------------------------------------- the account rebuilt from the fills
def _legs(f):
    """(asset qty change, quote qty change) of one fill; a fee lowers what the side receives."""
    fee_base = f["fee"] if f["fee_asset"] == f["asset"] else 0.0
    fee_quote = f["fee"] if f["fee_asset"] == f["quote_asset"] else 0.0
    if f["side"] == "buy":
        return f["base"] - fee_base, -(f["quote"] + fee_quote)
    return -(f["base"] + fee_base), f["quote"] - fee_quote


def holdings_at(fills, t, start_cash):
    """{asset: qty} after every fill at or before t, starting from start_cash toman.
    v3.9.1: the exchange rounds a fee taken in the coin down to the coin's precision while the record keeps all its
    digits, so selling everything left the rebuilt amount a hair below zero (-0.00000001 BTC on the panel). A fill
    that leaves its side below zero by at most ROUNDING_FRACTION of the fill's own size leaves zero: nothing can be
    held below zero. A bigger gap (a trade the bot did not record) stays and is warned about."""
    q = {CASH: start_cash}
    for f in fills:
        if f["t"] > t:
            break
        d_base, d_quote = _legs(f)
        for asset, d, size in ((f["asset"], d_base, f["base"]), (f["quote_asset"], d_quote, f["quote"])):
            v = q.get(asset, 0.0) + d
            if v < 0.0 and d < 0.0 and -v <= ROUNDING_FRACTION * abs(size):
                v = 0.0
            q[asset] = v
    return q


def _value(q, price_fn):
    total = 0.0
    for a, n in q.items():
        p = price_fn(a)
        if p is not None and abs(n) > 1e-12:
            total += n * p
    return total


def _thin(points, n=CHART_POINTS):
    if len(points) <= n:
        return points
    step = (len(points) - 1) / float(n - 1)
    return [points[int(round(i * step))] for i in range(n)]


# --------------------------------------------------------------------------- the report
def report(records, start_equity, prices, t_from, t_to, now, orders=None, analyses=None, ta=None):
    """Everything the panel shows for (t_from, t_to]; see the module docstring. Never raises on odd data.
    analyses: read_analyses() - Kimi's newest analysis of each coin, for the outlook of the open positions;
    ta: {asset: technical.chart_data()} - v3.9, the indicators of the open positions' charts."""
    fills = parse_fills(records)
    pts = equity_points(records)
    warnings = []
    t_to = min(t_to, now)
    first_t = pts[0][0] if pts else None
    live = t_to >= now - HOUR                   # the range ends now
    times = [p[0] for p in pts]

    def equity_at(t):
        k = bisect.bisect_right(times, t) - 1
        if k < 0:
            return start_equity, None
        return pts[k][1], pts[k][0]

    v_from, at_from = equity_at(t_from)
    v_to, at_to = equity_at(t_to)
    # each recorded value is turned into USDT at the rate of ITS hour (the start value: at t_from)
    u_from = prices.at(UNIT, at_from if at_from is not None else t_from, now)
    u_to = prices.at(UNIT, at_to if at_to is not None else t_to, now)
    totals = {"from": t_from, "to": t_to, "value_from_irt": v_from, "value_to_irt": v_to, "value_from_time": at_from,
              "value_to_time": at_to, "usdt_irt_from": u_from, "usdt_irt_to": u_to}
    if v_from and v_to:
        totals["pnl_irt"] = v_to - v_from
        totals["pnl_irt_pct"] = (v_to / v_from - 1.0) * 100.0
    if v_from and v_to and u_from and u_to:
        totals["value_from_usdt"], totals["value_to_usdt"] = v_from / u_from, v_to / u_to
        totals["pnl_usdt"] = v_to / u_to - v_from / u_from
        totals["pnl_usdt_pct"] = ((v_to / u_to) / (v_from / u_from) - 1.0) * 100.0
        totals["rial_fall_pct"] = (u_to / u_from - 1.0) * 100.0          # = holding USDT, in toman
        totals["vs_usdt_points"] = totals["pnl_irt_pct"] - totals["rial_fall_pct"]
    in_range = [f for f in fills if t_from < f["t"] <= t_to]
    fees_irt = 0.0
    for f in in_range:
        p = None
        if f["fee_asset"] == f["asset"]:
            p = f["quote"] / f["base"] * (prices.irt(f["quote_asset"], f["t"]) or 0.0)
        elif f["fee_asset"]:
            p = prices.irt(f["fee_asset"], f["t"])
        if p:
            fees_irt += f["fee"] * p
    totals.update(trades=len(in_range), buys=sum(1 for f in in_range if f["side"] == "buy"),
                  sells=sum(1 for f in in_range if f["side"] == "sell"), fees_irt=fees_irt,
                  fees_usdt=(fees_irt / u_to) if u_to else None)

    # the account value at this minute: the last recorded value moved by the latest prices
    if live and at_to is not None and v_to:
        r_last = _value(holdings_at(fills, at_to, start_equity), lambda a: prices.irt(a, at_to))
        r_now = _value(holdings_at(fills, now, start_equity), prices.latest)
        u_now = prices.latest(UNIT)
        if r_last > 0 and r_now > 0:
            totals["value_now_irt"] = v_to * r_now / r_last
            totals["value_now_time"] = now
            if u_now:
                totals["value_now_usdt"] = totals["value_now_irt"] / u_now

    # per asset: value change minus net money put in, each trade at its own price
    q_from = holdings_at(fills, t_from, start_equity)
    q_to = holdings_at(fills, t_to, start_equity)
    assets = sorted(set(q_from) | set(q_to) | {f["asset"] for f in in_range}, key=lambda a: (a == CASH, a == UNIT, a))
    rows = []
    flows_irt, flows_usdt, count = {}, {}, {}
    for f in in_range:
        u_t = prices.irt(UNIT, f["t"]) or u_from
        q_irt = prices.irt(f["quote_asset"], f["t"])
        if q_irt is None or not u_t:
            continue
        d_base, d_quote = _legs(f)
        paid = -d_quote                                  # quote units that left (a buy) or came in (a sell: < 0)
        val_irt = paid * q_irt
        # the asset traded: money in (a buy) / out (a sell) at the trade's own value
        flows_irt[f["asset"]] = flows_irt.get(f["asset"], 0.0) + val_irt
        flows_usdt[f["asset"]] = flows_usdt.get(f["asset"], 0.0) + val_irt / u_t
        # the money side moves the same value the other way (its own P&L is only its revaluation)
        flows_irt[f["quote_asset"]] = flows_irt.get(f["quote_asset"], 0.0) - val_irt
        flows_usdt[f["quote_asset"]] = flows_usdt.get(f["quote_asset"], 0.0) - val_irt / u_t
        count[f["asset"]] = count.get(f["asset"], 0) + 1
    rebuilt_from = rebuilt_to = 0.0
    for a in assets:
        n0, n1 = q_from.get(a, 0.0), q_to.get(a, 0.0)
        p0, p1 = prices.at(a, t_from, now), prices.at(a, t_to, now)
        pu0, pu1 = prices.usdt(a, t_from, now), prices.usdt(a, t_to, now)
        if p0 is None or p1 is None:
            if abs(n0) > 1e-12 or abs(n1) > 1e-12 or count.get(a):
                warnings.append("no price for %s" % a)
            continue
        v0, v1 = n0 * p0, n1 * p1
        rebuilt_from += v0
        rebuilt_to += v1
        row = {"asset": a, "qty_from": n0, "qty_to": n1, "value_from_irt": v0, "value_to_irt": v1,
               "price_to_irt": p1, "price_to_usdt": pu1, "trades": count.get(a, 0),
               "pnl_irt": v1 - v0 - flows_irt.get(a, 0.0)}
        if pu0 is not None and pu1 is not None:
            row["value_to_usdt"] = n1 * pu1
            row["pnl_usdt"] = n1 * pu1 - n0 * pu0 - flows_usdt.get(a, 0.0)
        invested = max(abs(v0), abs(flows_irt.get(a, 0.0)), 1e-9)
        row["pnl_irt_pct"] = row["pnl_irt"] / invested * 100.0 if a != CASH else None
        if abs(n0) < 1e-12 and abs(n1) < 1e-12 and not count.get(a):
            continue
        rows.append(row)
    if pts and rebuilt_to and v_to and abs(rebuilt_to / v_to - 1.0) > MISMATCH_WARN and at_to is not None:
        warnings.append("the account rebuilt from the fills (%.0f toman) differs from the recorded value (%.0f) by "
                        "%.1f%%: a trade or transfer the bot did not make?" % (rebuilt_to, v_to,
                                                                                (rebuilt_to / v_to - 1) * 100))

    # the portfolio chart: the value at t_from, the recorded values in the range and the estimate for now;
    # [time, toman, USDT, toman if everything had been held in USDT since t_from]
    series = [(t, e) for t, e in pts if t_from < t <= t_to]
    series.insert(0, (t_from, v_from))
    chart = []
    for t, e in _thin(series):
        u = u_from if t == t_from else prices.at(UNIT, t, now)
        chart.append([t, e, (e / u) if u else None, (v_from * u / u_from) if (u and u_from) else None])
    if totals.get("value_now_irt") and chart and now > chart[-1][0]:
        u = prices.latest(UNIT)
        e = totals["value_now_irt"]
        chart.append([now, e, (e / u) if u else None, (v_from * u / u_from) if (u and u_from) else None])

    # the open positions and the resting orders, each with its price chart in USDT (a range that ends now)
    positions = []
    if live:
        positions = _positions(rows, open_plans(records), orders or [], prices, t_to, now, analyses or {}, ta or {})

    history = []
    for f in reversed(in_range[-MAX_HISTORY:]):
        q_irt = prices.irt(f["quote_asset"], f["t"])
        u_t = prices.irt(UNIT, f["t"])
        value_irt = f["quote"] * q_irt if q_irt else None
        history.append(dict(f, price=f["quote"] / f["base"],
                            value_irt=value_irt, value_usdt=(value_irt / u_t) if (value_irt and u_t) else None))
    return {"generated": now, "from": t_from, "to": t_to, "live": live, "start_equity_irt": start_equity,
            "first_record": first_t, "totals": totals, "assets": rows, "equity": chart, "positions": positions,
            "history": history, "history_total": len(in_range), "warnings": warnings,
            "rebuilt_value_to_irt": rebuilt_to}


def _chart_span(plan, now):
    """How far back a position's price chart reaches: at least 7 days, at least as far as its outlook runs ahead
    (so the past is not squeezed), and from 12 hours before its plan was set; at most 30 days."""
    span = max(POSITION_CHART_HOURS * HOUR, outlook_end(plan, now) - now)
    set_at = _plan_time(plan) if isinstance(plan, dict) else None
    if set_at is not None:
        span = max(span, now - (set_at - 12 * HOUR))
    return min(span, POSITION_CHART_MAX_DAYS * 86400)


def _positions(rows, plans, orders, prices, t_to, now, analyses=None, ta=None):
    held = [r["asset"] for r in rows if r["asset"] not in (CASH, UNIT) and r["qty_to"] > 1e-12]
    chart_assets = list(held)
    for o in orders:
        if o["asset"] not in chart_assets and o["asset"] not in (CASH, UNIT):
            chart_assets.append(o["asset"])
    u_now = prices.latest(UNIT)
    out = []
    for a in chart_assets:
        plan = {}
        for key in (a + "_IRT", a + "_USDT"):
            if key in plans:
                plan = plans[key]
                break
        inner = plan.get("plan") if isinstance(plan.get("plan"), dict) else {}
        set_at = _plan_time(plan)
        c_from = t_to - _chart_span(plan, now)
        ts, cs = prices.closes.get(a) or ((), ())
        line = []
        for t, c in zip(ts, cs):
            if c_from <= t <= t_to:
                u = prices.irt(UNIT, t)
                if u:
                    line.append([t, c / u])
        now_usdt = prices.usdt(a, now, now)
        if now_usdt is not None and (not line or now > line[-1][0]):
            line.append([now, now_usdt])
        row = next((r for r in rows if r["asset"] == a), {})
        o_list = []
        for o in orders:
            if o["asset"] != a:
                continue
            p_usdt = o["price"] if o["quote_asset"] == UNIT else (o["price"] / u_now if u_now else None)
            o_list.append({"side": o["side"], "price_usdt": p_usdt, "price": o["price"], "quote_asset": o["quote_asset"],
                           "amount": o["amount"], "tag": o["tag"], "symbol": o["symbol"]})
        o_list.sort(key=lambda o: -(o["price_usdt"] or 0.0))
        # v3.10: a position = the bot's own plan for the coin, or a holding worth at least DUST_USDT; the rest of the
        # charts are coins the account does not hold (a resting order), a leftover below DUST_USDT is "dust"
        qty, value_usdt = row.get("qty_to", 0.0), row.get("value_to_usdt")
        held = qty > 1e-12 and (bool(plan) or (_f(value_usdt) or 0.0) >= DUST_USDT)
        entry = _f(plan.get("entry_px_usdt"))
        target = _f(plan.get("target_px_usdt")) or _f(inner.get("take_profit_usdt"))
        invalidation = _f(inner.get("invalidation_usdt"))
        analysis = (analyses or {}).get(a)
        out.append({"asset": a, "held": held, "dust": (not held) and qty > 1e-12,
                    "qty": row.get("qty_to", 0.0), "value_irt": row.get("value_to_irt"),
                    "value_usdt": row.get("value_to_usdt"), "price_usdt": now_usdt, "entry_usdt": entry,
                    "stop_usdt": _f(plan.get("stop_px_usdt")), "target_usdt": target,
                    "invalidation_usdt": invalidation, "horizon_hours": _f(inner.get("horizon_hours")),
                    "max_hold_until": _f(plan.get("max_hold_until")), "set_at": set_at,
                    "setup": str(inner.get("setup") or "")[:40], "note": str(inner.get("note") or "")[:600],
                    "change_pct": ((now_usdt / entry - 1.0) * 100.0) if (entry and now_usdt) else None,
                    "orders": o_list, "prices": _thin(line), "analysis": analysis,
                    "outlook": outlook(now_usdt, now, outlook_end(plan, now), hourly_sigma(line), target,
                                       invalidation, analysis.get("p") if analysis else None,
                                       _origin(analysis, plan, line)),
                    "ta": technical.trim_chart((ta or {}).get(a), c_from)})
    return out


# --------------------------------------------------------------------------- collecting (the helper's worker)
def collect(state_dir, t_from, t_to, now=None, fetch=None):
    """report() from the files in state_dir and public candles (fetch(symbol, res, start, end) -> [Bar];
    default bitpin.data.fetch_bars). Run as the bot's user, never as root. A range that starts before the
    account's first record starts one hour before it. (The records' "mode" field is the decision mode - "live",
    "scheduled", "slot" ... -, not live / paper: the paper bot has its own state directory.)"""
    now = time.time() if now is None else now
    if fetch is None:
        from bitpin import data as dm
        fetch = dm.fetch_bars
    records = read_runner_log(os.path.join(state_dir, RUNNER_LOG))
    eq_doc = _read_json(os.path.join(state_dir, EQUITY_FILE)) or {}
    start_equity = _f(eq_doc.get("equity_start_irt")) if isinstance(eq_doc, dict) else None
    pts = equity_points(records)
    if not start_equity:
        start_equity = pts[0][1] if pts else 0.0
    if pts and t_from < pts[0][0] - HOUR < t_to:
        t_from = pts[0][0] - HOUR
    orders = resting_orders(_read_json(os.path.join(state_dir, ORDERS_FILE)) or {})
    fills = parse_fills(records)
    assets = {UNIT} | {f["asset"] for f in fills} | {f["quote_asset"] for f in fills} | {o["asset"] for o in orders}
    assets.discard(CASH)
    chart = set()                                   # v3.9: the markets of the position charts (a range that ends now)
    if min(t_to, now) >= now - HOUR:
        held = holdings_at(fills, now, start_equity)
        chart = {a for a, q in held.items() if q > 1e-12} | {o["asset"] for o in orders}
        chart -= {CASH, UNIT}
    chart_from = min(t_to, now) - POSITION_CHART_HOURS * HOUR
    for plan in open_plans(records).values():
        chart_from = min(chart_from, now - _chart_span(plan, now))
    start = int(min(t_from, chart_from) - 3 * HOUR)
    fine_from = max(start, int(now) - FINE_DAYS * 86400)
    fine, coarse, errors = {}, {}, []
    for a in sorted(assets):
        a_from = min(fine_from, int(now) - TA_FETCH_HOURS * HOUR) if chart and (a in chart or a == UNIT) else fine_from
        try:
            fine[a] = fetch(a + "_IRT", "60", a_from, int(now))
            if start < a_from:
                coarse[a] = fetch(a + "_IRT", "240", start, a_from + COARSE_SECONDS)
        except Exception as e:  # noqa: BLE001 - one market less, not a broken page
            errors.append("%s_IRT candles: %s" % (a, str(e)[:120]))
            fine.setdefault(a, [])
    ta = {}
    usdt_closed = [b for b in fine.get(UNIT) or [] if b.ts + HOUR <= now]
    t_ta = int(now) - POSITION_CHART_MAX_DAYS * 86400 - technical.CHART_STEP
    for a in sorted(chart):
        try:
            d = technical.chart_data(a + "_IRT", [b for b in fine.get(a) or [] if b.ts + HOUR <= now], usdt_closed,
                                     t_ta)
        except Exception as e:  # noqa: BLE001 - a chart without its indicators, not a broken page
            errors.append("%s indicators: %s" % (a, str(e)[:120]))
            d = None
        if d:
            ta[a] = d
    out = report(records, start_equity, prices_from_bars(fine, now, coarse), t_from, t_to, now, orders,
                 read_analyses(os.path.join(state_dir, DECISIONS_FILE)), ta)
    out["warnings"] = errors + out["warnings"]
    return out
