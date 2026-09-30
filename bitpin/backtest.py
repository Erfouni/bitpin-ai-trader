"""Portfolio backtest engine for long-only spot strategies on Bitpin.

Model
-----
* A strategy returns, for every bar i and symbol s, a TARGET WEIGHT w[s][i] in [0, 1] (fraction
  of total equity held in s) decided with information up to and including the CLOSE of bar i.
  The rest of equity is held as cash in the quote currency (IRT for *_IRT markets).
* The engine executes toward w[.][i] at the OPEN of bar i+1 (never at the close that produced
  the signal), paying taker fee + slippage on the traded notional. Sells run before buys.
* Trades smaller than `rebalance_threshold` (absolute weight change) are skipped, except full
  exits (target 0) which always execute.
* The live runner uses the same contract: it acts on the last CLOSED bar and trades at market,
  so backtest and live logic match.
"""
import math
import random

from .data import RES_SECONDS, load_csv, resample

TAKER_FEE = 0.0035
MAKER_FEE = 0.003


class Panel:
    """Time-aligned OHLCV for several symbols. Missing bars are forward-filled (volume 0);
    before a symbol's first bar all its fields are None."""

    def __init__(self, ts, cols, res):
        self.ts = ts
        self.cols = cols            # symbol -> {"open": [...], "high": [...], ...}
        self.res = res
        self.symbols = sorted(cols)

    def __len__(self):
        return len(self.ts)

    def __getitem__(self, sym):
        return self.cols[sym]

    def slice(self, a, b):
        return Panel(self.ts[a:b], {s: {k: v[a:b] for k, v in c.items()} for s, c in self.cols.items()}, self.res)

    def index_at_or_after(self, t):
        for i, x in enumerate(self.ts):
            if x >= t:
                return i
        return len(self.ts)

    @classmethod
    def from_bars(cls, bars_by_symbol, res):
        all_ts = sorted({b.ts for bars in bars_by_symbol.values() for b in bars})
        cols = {}
        for sym, bars in bars_by_symbol.items():
            m = {b.ts: b for b in bars}
            o, h, l, c, v = [], [], [], [], []
            last = None
            for t in all_ts:
                b = m.get(t)
                if b is not None:
                    o.append(b.open); h.append(b.high); l.append(b.low); c.append(b.close); v.append(b.volume)
                    last = b.close
                elif last is not None:
                    o.append(last); h.append(last); l.append(last); c.append(last); v.append(0.0)
                else:
                    o.append(None); h.append(None); l.append(None); c.append(None); v.append(None)
            cols[sym] = {"open": o, "high": h, "low": l, "close": c, "volume": v}
        return cls(all_ts, cols, res)

    @classmethod
    def load(cls, symbols, res="60", base_res="60"):
        """Load cached CSVs (downloaded at base_res) and resample to `res` if needed."""
        factor = RES_SECONDS[res] // RES_SECONDS[base_res]
        data = {}
        for s in symbols:
            bars = load_csv(s, base_res)
            data[s] = resample(bars, factor) if factor > 1 else bars
        return cls.from_bars(data, res)


class Strategy:
    """Base class. Subclasses set `name`, `res`, `default_params`, optionally `param_grid`,
    and implement `weights(panel) -> {symbol: [w_0 .. w_{T-1}]}`."""

    name = "base"
    res = "60"
    default_params = {}
    param_grid = {}
    description = ""

    def __init__(self, **params):
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError("unknown params for %s: %s" % (self.name, sorted(unknown)))
        self.params = dict(self.default_params)
        self.params.update(params)

    def weights(self, panel):
        raise NotImplementedError

    def __repr__(self):
        return "%s(%s)" % (self.name, ", ".join("%s=%r" % kv for kv in sorted(self.params.items())))


def simulate(panel, weights, start=0, fee=TAKER_FEE, slippage=0.0005, rebalance_threshold=0.02,
             init_equity=1.0):
    """Run the execution model from bar index `start` (flat, all cash) to the end of `panel`.
    Returns a dict with the equity curve (at each bar close) and trade log."""
    syms = [s for s in panel.symbols if s in weights]
    T = len(panel)
    for s in syms:
        if len(weights[s]) != T:
            raise ValueError("weights[%s] has length %d, panel has %d" % (s, len(weights[s]), T))
    cash = init_equity
    units = {s: 0.0 for s in syms}
    equity = []
    trades = []
    fees_paid = 0.0
    exposure = []
    for i in range(start, T):
        # 1) execute the target decided at the close of bar i-1 at the open of bar i
        if i > start:
            opens = {s: panel[s]["open"][i] for s in syms}
            eq_open = cash + sum(units[s] * opens[s] for s in syms if opens[s] is not None)
            targets = {}
            for s in syms:
                w = weights[s][i - 1]
                if w is None or opens[s] is None or panel[s]["close"][i - 1] is None or (isinstance(w, float) and math.isnan(w)):
                    w = 0.0
                targets[s] = min(max(float(w), 0.0), 1.0)
            tot = sum(targets.values())
            if tot > 1.0 + 1e-9:
                targets = {s: w / tot for s, w in targets.items()}
            cur = {s: (units[s] * opens[s] / eq_open if opens[s] is not None and eq_open > 0 else 0.0) for s in syms}
            order = sorted(syms, key=lambda s: targets[s] - cur[s])  # sells (negative deltas) first
            for s in order:
                if opens[s] is None:
                    continue
                d = targets[s] - cur[s]
                full_exit = targets[s] == 0.0 and units[s] > 0
                if abs(d) < rebalance_threshold and not full_exit:
                    continue
                if d < 0:
                    q = units[s] if full_exit else min(units[s], -d * eq_open / opens[s])
                    px = opens[s] * (1 - slippage)
                    proceeds = q * px
                    f = proceeds * fee
                    cash += proceeds - f
                    fees_paid += f
                    units[s] -= q
                    trades.append((panel.ts[i], s, "sell", q, px, f))
                elif d > 0:
                    notional = min(d * eq_open, cash)
                    if notional <= 0:
                        continue
                    px = opens[s] * (1 + slippage)
                    f = notional * fee
                    q = (notional - f) / px
                    cash -= notional
                    fees_paid += f
                    units[s] += q
                    trades.append((panel.ts[i], s, "buy", q, px, f))
        # 2) mark to market at the close of bar i
        closes = {s: panel[s]["close"][i] for s in syms}
        pos_val = sum(units[s] * closes[s] for s in syms if closes[s] is not None)
        eq = cash + pos_val
        equity.append(eq)
        exposure.append(pos_val / eq if eq > 0 else 0.0)
    return {"ts": panel.ts[start:], "equity": equity, "trades": trades, "fees": fees_paid,
            "exposure": exposure, "init": init_equity}


def max_drawdown(eq):
    peak, mdd = -1e300, 0.0
    for x in eq:
        peak = max(peak, x)
        if peak > 0:
            mdd = max(mdd, 1 - x / peak)
    return mdd


def metrics(result, res="60", window_days=30):
    eq = result["equity"]
    if len(eq) < 2:
        return {}
    step = RES_SECONDS[res]
    bars_per_year = 365 * 86400 / step
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets)) if len(rets) > 1 else 0.0
    downside = [min(r, 0.0) for r in rets]
    dsd = math.sqrt(sum(r * r for r in downside) / len(downside))
    days = (result["ts"][-1] - result["ts"][0]) / 86400 or 1
    total = eq[-1] / result["init"] - 1
    w = max(1, int(window_days * 86400 / step))
    stride = max(1, int(86400 / step))
    windows = [eq[j + w] / eq[j] - 1 for j in range(0, len(eq) - w, stride)]
    windows.sort()

    def q(p):
        if not windows:
            return None
        return windows[min(len(windows) - 1, max(0, int(p * (len(windows) - 1))))]

    return {
        "total_return": total,
        "days": days,
        "monthly_geo": (1 + total) ** (30.0 / days) - 1 if total > -1 else -1.0,
        "max_drawdown": max_drawdown(eq),
        "sharpe": (mu / sd * math.sqrt(bars_per_year)) if sd > 0 else 0.0,
        "sortino": (mu / dsd * math.sqrt(bars_per_year)) if dsd > 0 else 0.0,
        "trades": len(result["trades"]),
        "fees_frac": result["fees"] / result["init"],
        "avg_exposure": sum(result["exposure"]) / len(result["exposure"]),
        "win30_median": q(0.5),
        "win30_p10": q(0.1),
        "win30_p90": q(0.9),
        "win30_positive_frac": (sum(1 for x in windows if x > 0) / len(windows)) if windows else None,
        "win30_count": len(windows),
    }


def evaluate(strategy, panel, start_ts=None, end_ts=None, **sim_kwargs):
    """Compute weights on panel[:end] (causal) and simulate from start_ts to end_ts."""
    end_i = panel.index_at_or_after(end_ts) if end_ts else len(panel)
    p = panel.slice(0, end_i) if end_i < len(panel) else panel
    w = strategy.weights(p)
    start_i = p.index_at_or_after(start_ts) if start_ts else 0
    res = simulate(p, w, start=start_i, **sim_kwargs)
    m = metrics(res, p.res)
    m["strategy"] = repr(strategy)
    return m, res


def buy_and_hold(panel, symbols, start_ts=None, end_ts=None, fee=TAKER_FEE, slippage=0.0005, **_ignored):
    """Equal-weight buy & hold benchmark with the same timing as `simulate`: flat at the close of
    the start bar, buys at the next open, then never rebalances. Symbols not yet listed at entry
    keep their share in cash."""
    end_i = panel.index_at_or_after(end_ts) if end_ts else len(panel)
    p = panel.slice(0, end_i) if end_i < len(panel) else panel
    start = p.index_at_or_after(start_ts) if start_ts else 0
    n = len(symbols)
    cash, units, equity, fees, trades = 1.0, {s: 0.0 for s in symbols}, [], 0.0, []
    for i in range(start, len(p)):
        if i == start + 1:
            for s in symbols:
                o = p[s]["open"][i]
                if o is None:
                    continue
                notional = 1.0 / n
                f = notional * fee
                units[s] = (notional - f) / (o * (1 + slippage))
                cash -= notional
                fees += f
                trades.append((p.ts[i], s, "buy", units[s], o * (1 + slippage), f))
        pos = sum(units[s] * p[s]["close"][i] for s in symbols if p[s]["close"][i] is not None)
        equity.append(cash + pos)
    res = {"ts": p.ts[start:], "equity": equity, "trades": trades, "fees": fees,
           "exposure": [1.0] * len(equity), "init": 1.0}
    m = metrics(res, p.res)
    m["strategy"] = "buy_and_hold(%s)" % ",".join(symbols)
    return m, res


def check_no_lookahead(strategy, panel, samples=12, seed=7, min_index=None, tol=1e-9):
    """Verify weights[i] is identical whether computed on the full panel or on panel[:i+1].
    Returns a list of violations (empty == OK)."""
    full = strategy.weights(panel)
    rng = random.Random(seed)
    lo = min_index or max(10, len(panel) // 10)
    idxs = sorted(rng.sample(range(lo, len(panel) - 1), min(samples, max(0, len(panel) - 1 - lo))))
    bad = []
    for c in idxs:
        part = strategy.weights(panel.slice(0, c + 1))
        for s in full:
            a, b = full[s][c], part[s][c]
            a = 0.0 if a is None else a
            b = 0.0 if b is None else b
            if abs(a - b) > tol:
                bad.append({"index": c, "ts": panel.ts[c], "symbol": s, "full": a, "truncated": b})
    return bad
