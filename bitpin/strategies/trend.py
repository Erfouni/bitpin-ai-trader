"""Time-series trend following on 4h bars, with USDT_IRT (not IRT cash) as the risk-off asset.

Design choices, all driven by how this market behaves:

* The toman loses ~4%/month, so every unit of equity that is not in a trending coin sits in
  USDT_IRT.  Holding IRT cash is never the default.
* The real decision is "this coin or USDT", so trends are measured on the coin's USDT price
  (coin_IRT / USDT_IRT).  IRT prices rise with the dollar even when a coin goes nowhere, and
  trading those fake breakouts costs fees for nothing.
* Swapping USDT -> coin -> USDT is four taker fills (~1.6% round trip with slippage), so signals
  are slow (weeks), a position is only opened on a FRESH signal (the breakout / cross bar, or
  within `late` bars of it) and is never resized while held.
* Positions are sized by inverse volatility (target_vol / annualised vol of the USDT price) when
  they are opened, and total coin exposure at entry is capped by `max_gross`.  Coins move
  together, so per-coin vol scaling alone does not stop several correlated positions from breaking
  down in the same week; on TRAIN the cap (~0.2) is what kept the 30-day left tail at or above
  plain USDT.
* Held positions are reported at their drifted weight (see `_assemble`), so the engine does not
  trim winners or top up losers every bar: a position is bought once and sold once.

Every indicator is causal and O(n); signals use closes only.  Weights at bar i depend on the
history up to i (a position keeps the size it was opened with), so a live runner should load at
least as much history as the longest lookback plus a few weeks (e.g. 3000 4h bars); positions are
path dependent only for as long as a trend lasts.
"""
import math
from collections import deque

from ..backtest import Strategy
from ..data import RES_SECONDS

RISK_OFF = "USDT_IRT"
MIN_W = 0.03   # smallest position worth opening (the engine skips rebalances < 0.02)

# Liquid IRT markets with >= 850 days of history, most liquid first.  The order is also the
# priority when several coins signal on the same bar and the gross cap has no room for all.
UNIVERSES = {
    "liquid": ["XRP_IRT", "BTC_IRT", "SOL_IRT", "ETH_IRT", "DOGE_IRT", "PAXG_IRT", "DASH_IRT", "SHIB_IRT",
               "PEPE_IRT", "ADA_IRT", "TRX_IRT", "BNB_IRT", "SUI_IRT", "LINK_IRT", "NEAR_IRT", "ARB_IRT"],
    "major": ["XRP_IRT", "BTC_IRT", "SOL_IRT", "ETH_IRT", "DOGE_IRT", "PAXG_IRT"],
}


# ------------------------------------------------------------------ causal helpers, all O(n)

def _first_valid(xs):
    for i, x in enumerate(xs):
        if x is not None:
            return i
    return len(xs)


def _prev_extreme(xs, n, use_max):
    """out[i] = max (use_max) or min of xs[i-n .. i-1], i.e. the n bars BEFORE bar i, so that
    "close > previous n-bar high" is a breakout.  None until n valid bars exist."""
    out = [None] * len(xs)
    dq = deque()  # candidate indices; their values are monotonic
    s = _first_valid(xs)
    for i in range(s, len(xs)):
        if i - s >= n:
            while dq[0] < i - n:
                dq.popleft()
            out[i] = xs[dq[0]]
        x = xs[i]
        while dq and ((xs[dq[-1]] <= x) if use_max else (xs[dq[-1]] >= x)):
            dq.pop()
        dq.append(i)
    return out


def _ema(xs, n):
    out = [None] * len(xs)
    s = _first_valid(xs)
    if len(xs) - s < n:
        return out
    k = 2.0 / (n + 1)
    v = sum(xs[s:s + n]) / n
    out[s + n - 1] = v
    for i in range(s + n, len(xs)):
        v = xs[i] * k + v * (1 - k)
        out[i] = v
    return out


def _close_atr(xs, n):
    """Wilder average of |close - previous close|: an ATR computed on closes only."""
    out = [None] * len(xs)
    s = _first_valid(xs)
    v, acc = None, []
    for i in range(s + 1, len(xs)):
        tr = abs(xs[i] - xs[i - 1])
        if v is None:
            acc.append(tr)
            if len(acc) == n:
                v = sum(acc) / n
                out[i] = v
        else:
            v = (v * (n - 1) + tr) / n
            out[i] = v
    return out


def _inv_vol_size(xs, n, target_vol, w_max, bars_per_year):
    """min(w_max, target_vol / annualised stdev of the last n log returns); None during warm-up."""
    out = [None] * len(xs)
    q = deque()
    s1 = s2 = 0.0
    ann = math.sqrt(bars_per_year)
    for i in range(_first_valid(xs) + 1, len(xs)):
        if xs[i] is None or xs[i - 1] is None:
            continue
        r = math.log(xs[i] / xs[i - 1])
        q.append(r)
        s1 += r
        s2 += r * r
        if len(q) > n:
            old = q.popleft()
            s1 -= old
            s2 -= old * old
        if len(q) == n:
            m = s1 / n
            sd = math.sqrt(max(s2 / n - m * m, 0.0)) * ann
            out[i] = w_max if sd <= 0 else min(w_max, target_vol / sd)
    return out


def _usdt_price(panel, sym):
    """The coin's price in USDT, coin_IRT / USDT_IRT (None before listing)."""
    c, u = panel[sym]["close"], panel[RISK_OFF]["close"]
    return [None if (a is None or b is None or b <= 0) else a / b for a, b in zip(c, u)]


def _assemble(panel, coins, signal, max_gross):
    """Build drift-aware portfolio targets from per-coin signals.

    signal[s][i] is None (no position wanted) or (size, may_open): the coin's trend is on, `size` is
    the weight to open with, and `may_open` says whether a NEW position may be opened on this bar
    (fresh signal).  A virtual copy of the portfolio is carried forward: held coins are reported at
    their drifted weight (no trimming / topping up, so the engine sees no rebalance), a new position
    gets min(size, room left under max_gross) and is skipped if that is below MIN_W, and all
    remaining equity is USDT_IRT.  Bar i only uses closes up to i."""
    T = len(panel)
    px = {s: panel[s]["close"] for s in coins}
    ux = panel[RISK_OFF]["close"]
    out = {s: [0.0] * T for s in coins}
    out[RISK_OFF] = [0.0] * T
    val = {s: 0.0 for s in coins}   # virtual value of each position (initial equity = 1)
    uval, cash = 0.0, 1.0           # virtual USDT value and IRT cash (cash only before USDT exists)
    for i in range(T):
        if i > 0:                   # 1) let holdings drift with close-to-close returns
            for s in coins:
                if val[s] > 0 and px[s][i - 1] and px[s][i]:
                    val[s] *= px[s][i] / px[s][i - 1]
            if uval > 0 and ux[i - 1] and ux[i]:
                uval *= ux[i] / ux[i - 1]
        eq = cash + uval + sum(val.values())
        w = {s: val[s] / eq for s in coins}
        for s in coins:             # 2) exits first, so that their room can be reused
            if w[s] > 0 and (signal[s][i] is None or px[s][i] is None):
                w[s] = 0.0
        room = max_gross - sum(w.values())
        for s in coins:             # 3) fresh entries, in universe (liquidity) order
            sig = signal[s][i]
            if w[s] == 0.0 and sig is not None and sig[1] and px[s][i] is not None:
                size = min(sig[0], room)
                if size >= MIN_W:
                    w[s] = size
                    room -= size
        gross = sum(w.values())
        for s in coins:
            out[s][i] = w[s]
            val[s] = w[s] * eq
        if ux[i] is not None:       # 4) everything else in USDT
            out[RISK_OFF][i] = max(1.0 - gross, 0.0)
            uval, cash = out[RISK_OFF][i] * eq, 0.0
        else:
            uval, cash = 0.0, eq * (1.0 - gross)
    return out


class _TrendCommon:
    """Shared plumbing.  Deliberately NOT a Strategy subclass, so strategy discovery ignores it.
    Subclasses implement _signal(px, size) -> per-bar None or (size, may_open)."""

    res = "240"
    symbols = UNIVERSES["liquid"] + [RISK_OFF]

    def weights(self, panel):
        p = self.params
        coins = [s for s in UNIVERSES[p["universe"]] if s in panel.cols]
        bars_per_year = 365 * 86400 / RES_SECONDS[panel.res]
        signal = {}
        for s in coins:
            px = _usdt_price(panel, s)
            size = _inv_vol_size(px, p["vol_n"], p["target_vol"], p["w_max"], bars_per_year)
            signal[s] = self._signal(px, size)
        w = _assemble(panel, coins, signal, p["max_gross"])
        for s in self.symbols:
            w.setdefault(s, [0.0] * len(panel))
        return w

    def _signal(self, px, size):
        raise NotImplementedError


class TrendDonchian(_TrendCommon, Strategy):
    name = "trend_donchian"
    description = ("Donchian breakout of each coin's USDT price on 4h closes: open (inverse-vol sized, "
                   "gross-capped) on a close above the previous entry_n-bar high, close on a close below the "
                   "previous exit_n-bar low (exit_n = entry_n * exit_frac) or an optional close-ATR trailing "
                   "stop.  Idle equity in USDT_IRT.")
    # entry 240 bars = 40 days, exit 120 bars = 20 days; ~5% per crypto position, <= 20% in coins
    default_params = {"universe": "liquid", "entry_n": 240, "exit_frac": 0.5, "atr_k": 0.0, "atr_n": 42,
                      "vol_n": 180, "target_vol": 0.05, "w_max": 1.0, "max_gross": 0.2, "late": 0}
    param_grid = {"entry_n": [150, 180, 240, 300], "exit_frac": [0.33, 0.5, 0.75],
                  "target_vol": [0.05, 0.1, 0.15], "max_gross": [0.2, 0.3, 0.4, 0.5]}

    def _signal(self, px, size):
        p = self.params
        hi = _prev_extreme(px, p["entry_n"], True)
        lo = _prev_extreme(px, max(2, int(round(p["entry_n"] * p["exit_frac"]))), False)
        atr = _close_atr(px, p["atr_n"]) if p["atr_k"] > 0 else None
        sig = [None] * len(px)
        on, peak, w0, age = False, 0.0, 0.0, 0
        for i, x in enumerate(px):
            if x is None:
                on = False
                continue
            breakout = hi[i] is not None and x > hi[i]
            if not on:
                if breakout and size[i] is not None:
                    on, peak, w0, age = True, x, size[i], 0
            else:
                peak = max(peak, x)
                age += 1
                if (lo[i] is not None and x < lo[i]) or \
                        (atr is not None and atr[i] is not None and x < peak - p["atr_k"] * atr[i]):
                    on = False
            if on:
                # a new position may be opened within `late` bars of the breakout or on a new high
                sig[i] = (w0, p["late"] is None or age <= p["late"] or breakout)
        return sig


class TrendEMA(_TrendCommon, Strategy):
    name = "trend_ema"
    description = ("EMA regime of each coin's USDT price on 4h closes: on when the fast EMA is `band` above "
                   "the slow EMA, off when it is `band` below (hysteresis); inverse-vol sized, gross-capped "
                   "entries only within `late` bars of the cross.  Idle equity in USDT_IRT.")
    # fast 60 bars = 10 days, slow 360 bars = 60 days
    default_params = {"universe": "liquid", "fast": 60, "slow": 360, "band": 0.01, "vol_n": 180,
                      "target_vol": 0.05, "w_max": 1.0, "max_gross": 0.2, "late": 6}
    param_grid = {"fast": [30, 60, 90], "slow": [240, 360, 540], "band": [0.01, 0.03],
                  "target_vol": [0.05, 0.1], "max_gross": [0.2, 0.3, 0.4]}

    def _signal(self, px, size):
        p = self.params
        ef, es = _ema(px, p["fast"]), _ema(px, p["slow"])
        sig = [None] * len(px)
        on, w0, age = False, 0.0, 0
        for i in range(len(px)):
            if ef[i] is None or es[i] is None:
                on = False
                continue
            if not on:
                if ef[i] > es[i] * (1 + p["band"]) and size[i] is not None:
                    on, w0, age = True, size[i], 0
            else:
                age += 1
                if ef[i] < es[i] * (1 - p["band"]):
                    on = False
            if on:
                sig[i] = (w0, p["late"] is None or age <= p["late"])
        return sig
