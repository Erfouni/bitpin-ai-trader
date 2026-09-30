"""Cross-sectional momentum rotation for Bitpin IRT markets.

Why it is built this way
------------------------
* Every *_IRT price contains the toman's devaluation (the USDT_IRT move), and the benchmark /
  risk-off asset is USDT_IRT. So each coin is judged on its price RELATIVE to USDT
  (coin_IRT / USDT_IRT, i.e. roughly its dollar price): a coin is only worth holding when it is
  beating simply holding USDT. Capital that is not in a coin sits in USDT_IRT, never in IRT cash.
* Score = risk-adjusted momentum: the log return vs USDT over the lookback divided by the coin's
  own volatility scaled to that lookback (a t-stat-like number, so a calm steady riser such as
  gold can outrank a noisy meme coin). With `blend` the score is the mean over L/2, L and 2L,
  which is less sensitive to the exact lookback.
* Absolute filter: a coin can only enter with score > entry_score (0 = it beat USDT). Unused slots
  stay in USDT_IRT, so in a weak market the strategy is simply "hold USDT".
* Position size per slot = 1/top_k, optionally scaled down by min(1, vol_target / annualised vol vs
  USDT). This keeps volatile alts small and lets low-volatility trends (PAXG, BTC) take full slots.
* Slow schedule: each sub-portfolio ("tranche") rebalances once per `rebalance_h` hours on an
  epoch-aligned boundary (timestamp based, so it is causal and does not depend on where the data
  starts). `tranches` phase-shifted sub-portfolios share the capital, which removes most of the
  luck of the rebalance day (e.g. 7 weekly tranches -> 1/7 of the book is reviewed each day).
* Between rebalances the reported target weights DRIFT with prices (we report what the book
  actually holds). Otherwise the engine would pay 0.7%+ round-trip fees to rebalance back to fixed
  weights every time prices move 2%.

Everything is computed from data up to the close of the current bar; signals are only needed on
rebalance bars and volatility uses prefix sums, so one weights() call is O(T * symbols).
"""
import math

from ..backtest import Strategy
from ..data import RES_SECONDS

USDT = "USDT_IRT"
# The liquid IRT universe (>= 850 days of hourly history), excluding USDT itself.
LIQUID = ["BTC_IRT", "ETH_IRT", "XRP_IRT", "SOL_IRT", "DOGE_IRT", "PAXG_IRT", "DASH_IRT", "SHIB_IRT",
          "PEPE_IRT", "ADA_IRT", "TRX_IRT", "BNB_IRT", "SUI_IRT", "LINK_IRT", "NEAR_IRT", "ARB_IRT"]
# Large-cap coins + gold (a priori choice by global market cap, not by past Bitpin returns).
MAJORS = ["BTC_IRT", "ETH_IRT", "XRP_IRT", "SOL_IRT", "DOGE_IRT", "PAXG_IRT", "TRX_IRT", "BNB_IRT", "ADA_IRT"]
# The two largest crypto assets + tokenised gold.
CORE = ["BTC_IRT", "ETH_IRT", "PAXG_IRT"]
UNIVERSES = {"all": LIQUID, "major": MAJORS, "core": CORE}


def _rel_log(panel, syms):
    """log(coin_IRT / USDT_IRT) per bar: the coin's price in dollar terms (None when unknown)."""
    u = panel[USDT]["close"]
    out = {}
    for s in syms:
        c = panel[s]["close"]
        out[s] = [math.log(x / y) if (x and y) else None for x, y in zip(c, u)]
    return out


def _prefix_sq(lr, lag):
    """Prefix sums (and counts) of squared overlapping `lag`-bar log returns, so the variance over
    any window is O(1). Daily (not hourly) returns are used to avoid tick/bid-ask noise, which is
    large on low-priced markets such as PEPE_IRT or SHIB_IRT."""
    n = len(lr)
    ps = [0.0] * (n + 1)
    pc = [0] * (n + 1)
    for i in range(n):
        a = lr[i]
        b = lr[i - lag] if i >= lag else None
        if a is not None and b is not None:
            d = a - b
            ps[i + 1] = ps[i] + d * d
            pc[i + 1] = pc[i] + 1
        else:
            ps[i + 1] = ps[i]
            pc[i + 1] = pc[i]
    return ps, pc


def _schedule(ts, step, period_h, phase_h=0.0):
    """True on the first bar whose CLOSE (ts + step) passes an epoch-aligned boundary of
    `period_h` hours shifted by `phase_h`."""
    P = int(period_h * 3600)
    off = int(phase_h * 3600)
    flags, prev = [], None
    for t in ts:
        k = (t + step - off) // P
        flags.append(prev is not None and k != prev)
        prev = k
    return flags


class RotationMomentum(Strategy):
    name = "rotation_momentum"
    res = "60"
    symbols = LIQUID + [USDT]
    description = ("Risk-adjusted momentum rotation over the liquid IRT coins, measured against USDT: hold the "
                   "top_k coins that beat USDT, vol-targeted slot sizes, rest in USDT_IRT; weekly review "
                   "in 7 daily-staggered tranches.")
    default_params = {
        "lookback_h": 1440,     # momentum lookback in hours (60 days)
        "blend": 1,             # 1: score = mean over lookback/2, lookback, 2*lookback
        "vol_h": 1440,          # window for the volatility estimate (hours)
        "vol_target": 0.25,     # slot scaled by min(1, vol_target / annualised vol vs USDT); 0 = off
        "entry_score": 0.0,     # a new coin needs score above this (0 = must have beaten USDT)
        "exit_score": 0.0,      # a held coin is kept while its score stays above this ...
        "hold_buffer": 0,       # ... and its rank is < top_k + hold_buffer
        "top_k": 2,             # slots per tranche
        "rebalance_h": 168,     # review period of each tranche (hours)
        "tranches": 7,          # phase-shifted sub-portfolios sharing the capital
        "phase_h": 0,           # shift of the whole schedule (hours); for timing-luck checks
        "band": 0.05,           # keep a held coin's drifted weight if within this of its target
        "universe": "all",      # "all" (16 liquid coins), "major" (9) or "core" (BTC/ETH/PAXG)
    }
    param_grid = {
        "lookback_h": [720, 1440],
        "vol_target": [0.0, 0.15, 0.25, 0.4],
        "top_k": [1, 2, 3],
        "entry_score": [0.0, 0.5, 1.0],
        "universe": ["all", "major"],
    }

    # ------------------------------------------------------------------ signals
    def _prepare(self, panel):
        p = self.params
        self._step = RES_SECONDS.get(panel.res) or (panel.ts[1] - panel.ts[0])
        self._day = max(1, 86400 // self._step)
        self._uni = [s for s in UNIVERSES[p["universe"]] if s in panel.cols]
        self._lr = _rel_log(panel, self._uni)
        self._sq = {s: _prefix_sq(self._lr[s], self._day) for s in self._uni}
        self._cache = {}

    def _dvol(self, s, i):
        """Daily volatility of the coin vs USDT over the last vol_h hours (None if unknown)."""
        V = int(self.params["vol_h"] * 3600 // self._step)
        if i < V:
            return None
        ps, pc = self._sq[s]
        cnt = pc[i + 1] - pc[i + 1 - V]
        if cnt < V // 2:
            return None
        v = math.sqrt((ps[i + 1] - ps[i + 1 - V]) / cnt)
        return v if v > 0 else None

    def _scores(self, i):
        """({sym: score}, {sym: daily vol}) at bar i, using only data up to bar i."""
        if i in self._cache:
            return self._cache[i]
        p = self.params
        L = int(p["lookback_h"] * 3600 // self._step)
        Ls = [L // 2, L, 2 * L] if p["blend"] else [L]
        scores, vols = {}, {}
        if i >= max(Ls):
            for s in self._uni:
                lr = self._lr[s]
                dvol = self._dvol(s, i)
                if lr[i] is None or dvol is None or any(lr[i - l] is None for l in Ls):
                    continue
                # return vs USDT over each lookback, in units of its own volatility over that lookback
                sc = [(lr[i] - lr[i - l]) / (dvol * math.sqrt(l / self._day)) for l in Ls]
                scores[s] = sum(sc) / len(sc)
                vols[s] = dvol
        self._cache[i] = (scores, vols)
        return self._cache[i]

    def _decide(self, i, cur):
        """New target fractions of ONE tranche, given its current (drifted) fractions `cur`."""
        p = self.params
        K = int(p["top_k"])
        scores, vols = self._scores(i)
        if not scores:                       # still warming up: keep what we have (USDT)
            return cur or {USDT: 1.0}
        ranked = sorted(scores, key=lambda s: -scores[s])
        rank = {s: r for r, s in enumerate(ranked)}
        # 1) keep held coins that still qualify (hysteresis: exit rules are looser than entry)
        held = [s for s in cur if s != USDT and cur[s] > 0]
        chosen = sorted((s for s in held if s in rank and rank[s] < K + p["hold_buffer"]
                         and scores[s] > p["exit_score"]), key=lambda s: rank[s])[:K]
        # 2) fill free slots with the best-ranked coins that pass the absolute filter
        for s in ranked:
            if len(chosen) >= K:
                break
            if s not in chosen and scores[s] > p["entry_score"]:
                chosen.append(s)
        if not chosen:
            return {USDT: 1.0}
        want = {}
        for s in chosen:
            w = 1.0 / K
            if p["vol_target"] > 0:
                w *= min(1.0, p["vol_target"] / (vols[s] * math.sqrt(365)))
            want[s] = w
        new = {}
        for s in chosen:
            w0 = cur.get(s, 0.0)
            new[s] = w0 if (w0 > 0 and abs(w0 - want[s]) < p["band"]) else want[s]
        tot = sum(new.values())
        if tot > 1.0:
            new = {s: w / tot for s, w in new.items()}
            tot = 1.0
        new[USDT] = 1.0 - tot                # everything not in a coin is parked in USDT
        return new

    # ------------------------------------------------------------------ portfolio path
    def weights(self, panel):
        p = self.params
        T = len(panel)
        self._prepare(panel)
        allsyms = self._uni + [USDT]
        closes = {s: panel[s]["close"] for s in allsyms}
        N = max(1, int(p["tranches"]))
        per = p["rebalance_h"]
        scheds = [_schedule(panel.ts, self._step, per, p["phase_h"] + j * per / N) for j in range(N)]
        books = [dict() for _ in range(N)]   # tranche -> {sym: value}; each tranche starts at 1/N
        out = {s: [0.0] * T for s in allsyms}
        for i in range(T):
            for j in range(N):
                bk = books[j]
                if not bk:
                    if closes[USDT][i] is None:
                        continue
                    books[j] = bk = {USDT: 1.0 / N}      # hold USDT until the first decision
                elif i > 0:
                    for s in bk:                        # drift with prices from close i-1 to close i
                        a, b = closes[s][i - 1], closes[s][i]
                        if a and b:
                            bk[s] *= b / a
                if scheds[j][i]:
                    tot = sum(bk.values())
                    new = self._decide(i, {s: v / tot for s, v in bk.items()})
                    books[j] = {s: w * tot for s, w in new.items() if w > 0}
            vals = {}
            for bk in books:
                for s, v in bk.items():
                    vals[s] = vals.get(s, 0.0) + v
            tot = sum(vals.values())
            if tot <= 0:
                continue
            for s, v in vals.items():
                if closes[s][i] is not None:
                    out[s][i] = v / tot
        return out


class RotationCore(RotationMomentum):
    """Same machinery on a 3-asset universe (BTC, ETH, PAXG vs USDT), holding the single best
    trend: a 'dual momentum' switch between crypto, gold and dollars. Only the most liquid
    markets are traded, so live slippage should be closest to the backtest."""
    name = "rotation_core"
    symbols = CORE + [USDT]
    description = ("Dual momentum over BTC/ETH/PAXG vs USDT: hold the best risk-adjusted trend that beats "
                   "USDT (vol-targeted), else USDT_IRT; weekly review in 7 daily-staggered tranches.")
    default_params = dict(RotationMomentum.default_params, universe="core", top_k=1, vol_target=0.3)
    param_grid = {
        "lookback_h": [720, 1080, 1440, 2160],
        "vol_target": [0.0, 0.2, 0.3, 0.4],
        "entry_score": [0.0, 0.5, 1.0],
        "top_k": [1, 2],
    }
