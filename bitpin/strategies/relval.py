"""Relative-value ("relval") strategies: long-only switching between correlated IRT markets.

Why the ratio to USDT_IRT matters
---------------------------------
The toman devalues ~4%/month, so the natural "neutral" asset of an IRT portfolio is USDT_IRT,
not IRT cash. For any market X_IRT the ratio X_IRT / USDT_IRT is (almost exactly) X's price in
USD, i.e. the only part of X's return that is *relative* to the benchmark we must beat. All
strategies here therefore hold USDT_IRT whenever nothing is clearly more attractive, and only
switch into another asset when the relevant price ratio says so.

Cost discipline
---------------
Switching USDT -> X is a sell plus a buy: ~0.8% (2 x (0.35% taker + 0.05% slippage)), and a
round trip back is ~1.6%. Signals below are therefore slow (multi-week ratio trends on 4h bars),
use entry/exit hysteresis and a minimum holding time, and the emitted target weights *drift with
prices* between decisions (see `_drifting_targets`) so the engine does not pay for rebalancing a
position back to a fixed fraction every time prices move 2%.

All indicators are causal: value i uses bars 0..i only. Weights are targets decided at the close
of bar i and executed by the engine at the open of bar i+1.

TRAIN findings (2024-05 .. 2026-04-25, ~370 configs evaluated across the family)
-------------------------------------------------------------------------------
* Ratio returns mean-revert at 1-4h (bid/ask + local premium noise, strongest for PAXG/USDT) but
  the reversion (~1%) is smaller than a ~1.6% round trip; at 1-4 weeks crypto ratios trend mildly.
* Full-size rotations make large total returns but their 30-day p10 is -9%..-15% (e.g. holding
  XRP after its Dec-2024 spike). Sizing the rotated asset to a ~0.75%/day vol target (average
  exposure ~28%, mostly PAXG) keeps the median uplift and brings p10 back to hold_usdt's level.
* The worst 30-day windows of every IRT strategy here are toman rallies (USDT_IRT -13..-22%);
  USD-relative switching cannot avoid them, so win30_p10 is at best a tie with hold_usdt.
* Pair z-score switching (relval_pair) trades too often and, even vol-sized, does not beat
  hold_usdt's median: kept for reference, not recommended.
"""
import math

from ..backtest import Strategy
from ..data import RES_SECONDS

BASE = "USDT_IRT"


# ---------------------------------------------------------------------------------------------
# helpers (O(T), None-aware: a series is None until its first valid value, then fully defined
# because Panel forward-fills after listing)
# ---------------------------------------------------------------------------------------------
def _log_ratio(a, b):
    """log(a/b) per bar; None where either price is missing."""
    return [math.log(x / y) if (x is not None and y is not None and x > 0 and y > 0) else None
            for x, y in zip(a, b)]


def _first_valid(xs):
    for i, x in enumerate(xs):
        if x is not None:
            return i
    return len(xs)


def _rolling_mean_std(xs, n):
    """Rolling mean and population std over the last n values (O(T) running sums).
    Values are centred on the first valid value to keep the running sums numerically stable."""
    T = len(xs)
    mean, sd = [None] * T, [None] * T
    f = _first_valid(xs)
    if f >= T or n < 2:
        return mean, sd
    c = xs[f]
    s = s2 = 0.0
    for i in range(f, T):
        x = xs[i] - c
        s += x
        s2 += x * x
        if i - n >= f:
            y = xs[i - n] - c
            s -= y
            s2 -= y * y
        if i - f >= n - 1:
            m = s / n
            mean[i] = m + c
            sd[i] = math.sqrt(max(s2 / n - m * m, 0.0))
    return mean, sd


def _zscore(xs, n):
    """(x - SMA_n(x)) / STD_n(x); None during warm-up or when the window is flat."""
    mean, sd = _rolling_mean_std(xs, n)
    return [None if m is None or not s or s < 1e-12 else (x - m) / s for x, m, s in zip(xs, mean, sd)]


def _diff_std(xs, n):
    """Rolling std (over n bars) of 1-bar changes of xs: per-bar volatility of a log series."""
    T = len(xs)
    d = [None] * T
    for i in range(1, T):
        if xs[i] is not None and xs[i - 1] is not None:
            d[i] = xs[i] - xs[i - 1]
    return _rolling_mean_std(d, n)[1]


def _mom_tstat(xs, n, sd):
    """Volatility-normalised momentum: (x_i - x_{i-n}) / (sd_i * sqrt(n)), sd = per-bar vol.
    ~N(0,1) for a driftless random walk, so scores are comparable across assets with very
    different volatility (PAXG vs SOL)."""
    out = [None] * len(xs)
    for i in range(n, len(xs)):
        if xs[i] is None or xs[i - n] is None or not sd[i]:
            continue
        out[i] = (xs[i] - xs[i - n]) / (sd[i] * math.sqrt(n))
    return out


def _bars_per_day(panel):
    return 86400 // RES_SECONDS[panel.res]


def _drifting_targets(panel, desired, symbols, band):
    """Turn per-bar DESIRED weights (risky symbols only; the rest goes to USDT_IRT) into target
    weights that follow buy-and-hold drift between decisions.

    A position keeps the weight it drifts to as prices move; it is reset to the desired weight
    only when it is opened, closed, or has drifted more than `band` away from it. This mirrors
    what the engine holds, so the engine trades only at real decisions (not on every 2% of drift).
    Uses closes up to bar i only -> causal."""
    T = len(panel)
    closes = {s: panel[s]["close"] for s in symbols}
    out = {s: [0.0] * T for s in symbols}
    cur = {s: 0.0 for s in symbols}          # current (drifted) weights, incl. BASE
    for i in range(T):
        if i > 0:                            # 1) let yesterday's weights drift with bar-i returns
            vals = {}
            for s, w in cur.items():
                a, b = closes[s][i - 1], closes[s][i]
                vals[s] = w * (b / a) if (w and a and b) else w
            tot = sum(vals.values())
            cur = {s: (v / tot if tot > 0 else 0.0) for s, v in vals.items()}
        risky = 0.0                          # 2) apply decisions / band resets
        for s in symbols:
            if s == BASE:
                continue
            d = desired[s][i] if closes[s][i] is not None else 0.0
            if d <= 0.0:
                cur[s] = 0.0
            elif cur[s] <= 0.0 or abs(cur[s] - d) > band:
                cur[s] = d
            risky += cur[s]
        if risky > 1.0:                      # can only happen after drift; scale back to 100%
            for s in symbols:
                if s != BASE:
                    cur[s] /= risky
            risky = 1.0
        cur[BASE] = (1.0 - risky) if closes[BASE][i] is not None else 0.0
        for s in symbols:
            out[s][i] = cur[s]
    return out


# ---------------------------------------------------------------------------------------------
# 1) Rotation: hold the asset(s) whose price ratio to USDT_IRT is trending up the strongest
# ---------------------------------------------------------------------------------------------
class RelvalRotation(Strategy):
    """Relative-strength rotation against USDT_IRT ("dual momentum" on USD-price ratios).

    Score of asset X = volatility-normalised momentum of log(X_IRT / USDT_IRT) over lookback_days
    (signal "mom"), or the z-score of that log ratio vs its lookback mean (signal "z").
    USDT_IRT itself scores 0 by construction, so an asset is only held when it beats the dollar.
      * the top_k assets with score >= enter are held; a held asset is dropped when score < exit;
        a challenger replaces the weakest holding only if it scores > held + margin and the holding
        is older than min_hold_days (hysteresis against ~0.8% switch costs);
      * position size = sleeve / top_k, scaled down to vol_target (daily vol of the USD ratio)
        when vol_target > 0, so a 4%/day coin gets a smaller slice than 1%/day gold;
      * everything not allocated sits in USDT_IRT (never IRT cash).
    """

    name = "relval_rotation"
    res = "240"
    UNIVERSES = {
        # fixed a priori from liquidity (brief: BTC/ETH/XRP/SOL are the most liquid after USDT)
        # plus PAXG, the classic gold-vs-dollar store of value for toman savers
        "core": ["PAXG_IRT", "BTC_IRT", "ETH_IRT", "XRP_IRT", "SOL_IRT"],
        "crypto": ["BTC_IRT", "ETH_IRT", "XRP_IRT", "SOL_IRT"],
        "gold_btc": ["PAXG_IRT", "BTC_IRT"],
        "gold": ["PAXG_IRT"],
    }
    symbols = ["USDT_IRT", "PAXG_IRT", "BTC_IRT", "ETH_IRT", "XRP_IRT", "SOL_IRT"]
    description = ("Hold the asset whose ratio to USDT_IRT (its USD price) trends up most strongly, sized to a "
                   "daily-vol target; everything else in USDT_IRT. Slow, hysteresis-gated switching.")
    default_params = {"universe": "core", "signal": "mom", "lookback_days": 60, "enter": 1.0,
                      "exit": -0.5, "margin": 0.5, "top_k": 1, "min_hold_days": 3,
                      "vol_target": 0.0075, "vol_days": 30, "sleeve": 1.0, "band": 0.1}
    # 54 combos around the TRAIN plateau (lookback 45-90 d, wide enter/exit hysteresis, small risk)
    param_grid = {
        "lookback_days": [45, 60, 90],
        "enter": [0.5, 1.0],
        "exit": [-1.0, -0.5, 0.0],
        "vol_target": [0.0075, 0.01, 0.0],
    }

    def weights(self, panel):
        p = self.params
        T = len(panel)
        bpd = _bars_per_day(panel)
        n = max(2, int(round(p["lookback_days"] * bpd)))
        vol_n = max(2, int(round(p["vol_days"] * bpd)))
        k = int(p["top_k"])
        min_hold = int(round(p["min_hold_days"] * bpd))
        base = panel[BASE]["close"]
        uni = self.UNIVERSES[p["universe"]]
        score, vol = {}, {}
        for s in uni:
            lr = _log_ratio(panel[s]["close"], base)
            sd = _diff_std(lr, vol_n)
            if p["signal"] == "mom":
                score[s] = _mom_tstat(lr, n, sd)
            elif p["signal"] == "z":
                score[s] = _zscore(lr, n)
            else:
                raise ValueError("unknown signal %r" % p["signal"])
            vol[s] = [None if x is None else x * math.sqrt(bpd) for x in sd]   # daily vol

        desired = {s: [0.0] * T for s in self.symbols}
        held = {}  # symbol -> bar index when entered
        for i in range(T):
            cur = {s: score[s][i] for s in uni}
            # 1) exits: broken relative trend (score < exit) or no data
            for s in list(held):
                if cur[s] is None or cur[s] < p["exit"]:
                    del held[s]
            ranked = sorted((s for s in uni if s not in held and cur[s] is not None and cur[s] >= p["enter"]),
                            key=lambda s: -cur[s])
            # 2) fill free slots with the strongest eligible assets
            while len(held) < k and ranked:
                held[ranked.pop(0)] = i
            # 3) swap the weakest holding for a clearly stronger challenger (after min hold)
            if ranked and len(held) == k:
                weakest = min(held, key=lambda s: cur[s])
                if cur[ranked[0]] > cur[weakest] + p["margin"] and i - held[weakest] >= min_hold:
                    del held[weakest]
                    held[ranked.pop(0)] = i
            for s in held:
                size = p["sleeve"] / k
                if p["vol_target"] > 0 and vol[s][i]:
                    size *= min(1.0, p["vol_target"] / vol[s][i])
                desired[s][i] = size
        return _drifting_targets(panel, desired, self.symbols, p["band"])


# ---------------------------------------------------------------------------------------------
# 2) Pair switch: hold the cheap (mr) or strong (trend) leg of one ratio, USDT_IRT when neutral
# ---------------------------------------------------------------------------------------------
class RelvalPair(Strategy):
    """Classic ratio z-score switch between two correlated IRT markets A and B.

    z = z-score of log(A/B) over lookback_days.
      mode "mr"    : z <= -entry -> hold A (A cheap vs B);  z >= entry -> hold B.
      mode "trend" : z >=  entry -> hold A (A outperforming); z <= -entry -> hold B.
    The position is closed (back to USDT_IRT) once |z| comes back inside exit (mr: reversion
    done / trend: relative trend faded), never before min_hold_days.
    usd_filter (t-stat threshold, None = off): the chosen leg is only held while its own
    lookback_days momentum vs USDT_IRT is >= usd_filter, so we never sit in a leg that is merely
    "less bad" while both fall against the dollar. The filter is only checked at entry/switch
    time (not every bar) to avoid churn.
    vol_target > 0 sizes the held leg to that daily vol of its USD ratio (rest in USDT_IRT), with
    the same drifting-target / rebalance-band logic as relval_rotation.
    """

    name = "relval_pair"
    res = "240"
    PAIRS = {
        "btc_eth": ("BTC_IRT", "ETH_IRT"),
        "btc_paxg": ("BTC_IRT", "PAXG_IRT"),
        "btc_xrp": ("BTC_IRT", "XRP_IRT"),
        "eth_sol": ("ETH_IRT", "SOL_IRT"),
    }
    symbols = ["USDT_IRT", "BTC_IRT", "ETH_IRT", "PAXG_IRT", "XRP_IRT", "SOL_IRT"]
    description = ("Z-score of the price ratio of two correlated markets; hold the cheap (mr) or the "
                   "strong (trend) leg, USDT_IRT when neutral or when the leg is falling against USDT.")
    default_params = {"pair": "btc_paxg", "mode": "trend", "lookback_days": 60, "entry": 1.5,
                      "exit": 0.25, "usd_filter": None, "min_hold_days": 5, "vol_days": 30,
                      "vol_target": 0.01, "band": 0.1}
    # 72 combos; on TRAIN no configuration is robustly better than hold_usdt (see module docstring)
    param_grid = {
        "pair": ["btc_eth", "btc_paxg", "btc_xrp"],
        "mode": ["mr", "trend"],
        "lookback_days": [10, 30, 60],
        "entry": [1.0, 1.5],
        "vol_target": [0.0, 0.01],
    }

    def weights(self, panel):
        p = self.params
        T = len(panel)
        bpd = _bars_per_day(panel)
        n = max(2, int(round(p["lookback_days"] * bpd)))
        a, b = self.PAIRS[p["pair"]]
        z = _zscore(_log_ratio(panel[a]["close"], panel[b]["close"]), n)
        base = panel[BASE]["close"]
        usd, vol = {}, {}
        for s in (a, b):
            lr = _log_ratio(panel[s]["close"], base)
            sd = _diff_std(lr, max(2, int(round(p["vol_days"] * bpd))))
            usd[s] = _mom_tstat(lr, n, sd)
            vol[s] = [None if x is None else x * math.sqrt(bpd) for x in sd]   # daily vol

        def ok(s, i):
            return p["usd_filter"] is None or (usd[s][i] is not None and usd[s][i] >= p["usd_filter"])

        min_hold = int(round(p["min_hold_days"] * bpd))
        if p["mode"] not in ("mr", "trend"):
            raise ValueError("unknown mode %r" % p["mode"])
        sign = 1.0 if p["mode"] == "trend" else -1.0
        desired = {s: [0.0] * T for s in self.symbols}
        state, since = None, -10 ** 9   # None = USDT, else symbol of the held leg
        for i in range(T):
            zi = z[i]
            if zi is not None and i - since >= min_hold:
                sz = sign * zi          # sz >= entry  -> A is the leg to hold
                want = a if sz >= p["entry"] else (b if sz <= -p["entry"] else None)
                if want is not None and want != state and ok(want, i):
                    state, since = want, i
                elif state is not None and want is None and abs(zi) < p["exit"]:
                    state, since = None, i
            if state is not None:
                size = 1.0
                if p["vol_target"] > 0 and vol[state][i]:
                    size = min(1.0, p["vol_target"] / vol[state][i])
                desired[state][i] = size
        return _drifting_targets(panel, desired, self.symbols, p["band"])
