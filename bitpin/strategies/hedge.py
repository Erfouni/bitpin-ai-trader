"""Toman-hedge / macro allocation strategies ("hedge" family).

Premise
-------
The toman loses value fast, so the portfolio's floor asset is USDT_IRT, never IRT cash: every
strategy here keeps whatever it does not allocate elsewhere in USDT. The question each strategy
answers is only "how much non-dollar risk (gold, BTC, ETH) do we hold on top of the dollar hedge?".
That is why all signals are computed in USD terms (asset_IRT / USDT_IRT): the toman factor is
common to every IRT market and the dollar hedge already owns it.

Market facts that shape the design (measured on TRAIN data only, 2024-04 .. 2026-04-25):
* Round-trip cost is ~0.8% (0.35% taker + slippage per side), so allocations change slowly:
  decisions are taken once per day at a fixed hour, and an asset is only re-balanced when its
  weight is more than `band` away from target or it enters/leaves the book. Between rebalances
  the strategy reports the *drifted* weights of its model book, so the engine sees nothing to do.
* PAXG_IRT is thin and its hourly closes bounce (lag-1 autocorrelation of hourly returns ~ -0.3,
  a Roll-implied effective spread of ~0.7%). Signals therefore use a 24h EMA of the price, and
  the strategies are built to trade rarely (they were checked with 0.2-0.3% slippage).
* Toman-strengthening episodes (USDT_IRT -15..-21% in 30 days) are the worst months for every
  IRT asset. They were not predictable from USDT_IRT's own trend or drawdown (its forward 30-day
  return stayed positive on average), so no strategy here ever parks in IRT cash.
* In USD terms BTC/ETH showed usable 60-90 day momentum; gold showed none (it simply trended
  up), so gold is held as a structural hedge and only gated/sized in hedge_risk_parity.

All indicators are causal: weight[i] depends on bars 0..i only.
"""
import math

from ..backtest import Strategy
from ..data import RES_SECONDS

DAY = 86400
DECISION_HOUR_UTC = 8      # daily decision on the first bar at/after 08:00 UTC (~11:30 Tehran)
BASE = "USDT_IRT"          # floor / risk-off asset


# --------------------------------------------------------------------------- helpers

def _bars_per_day(panel):
    return max(1, DAY // RES_SECONDS[panel.res])


def _ema(xs, n):
    """EMA that tolerates a leading run of None (symbol not listed yet). Seeded with the first
    value; element i depends on xs[0..i] only."""
    out = [None] * len(xs)
    k = 2.0 / (n + 1)
    v = None
    for i, x in enumerate(xs):
        if x is None:
            continue
        v = x if v is None else x * k + v * (1 - k)
        out[i] = v
    return out


def _usd_price(panel, sym):
    """Price of `sym` in USDT (asset_IRT / USDT_IRT); None where either is missing."""
    a, u = panel[sym]["close"], panel[BASE]["close"]
    return [x / y if (x is not None and y) else None for x, y in zip(a, u)]


def _momentum(xs, lag):
    """xs[i] / xs[i-lag] - 1 (None during warm-up)."""
    out = [None] * len(xs)
    for i in range(lag, len(xs)):
        a, b = xs[i], xs[i - lag]
        if a is not None and b:
            out[i] = a / b - 1
    return out


def _rolling_vol(xs, days, bpd):
    """Annualised volatility of (overlapping) 1-day log returns over the last `days` days,
    computed with running sums (O(n)). None during warm-up."""
    n = len(xs)
    r = [None] * n
    for i in range(bpd, n):
        a, b = xs[i], xs[i - bpd]
        if a and b:
            r[i] = math.log(a / b)
    w = days * bpd
    out = [None] * n
    s = s2 = 0.0
    cnt = 0
    for i in range(n):
        if r[i] is not None:
            s += r[i]; s2 += r[i] * r[i]; cnt += 1
        j = i - w
        if j >= 0 and r[j] is not None:
            s -= r[j]; s2 -= r[j] * r[j]; cnt -= 1
        if i >= w + bpd and cnt >= w // 2:
            m = s / cnt
            var = max(s2 / cnt - m * m, 0.0)
            out[i] = math.sqrt(var * 365)
    return out


def _decision_flags(ts, every_days=1, hour=DECISION_HOUR_UTC):
    """True on the first bar of each `every_days`-day period (periods start at `hour` UTC)."""
    flags, prev = [], None
    for t in ts:
        key = (t - hour * 3600) // (every_days * DAY)
        flags.append(key != prev)
        prev = key
    return flags


def _drifting_book(panel, syms, decide, flags, band, base=BASE):
    """Run a model portfolio over the panel and return {sym: [weights]}.

    On decision bars `decide(i)` returns target weights (sum <= 1, remainder = IRT cash).
    Each non-base asset is moved to its target only if it enters/leaves the book or is more than
    `band` away from its target; otherwise it keeps drifting with prices. The base asset (USDT)
    absorbs whatever the moved assets release or need. The reported weight at every bar is the
    book's current (drifted) weight, so the engine only trades when the book is changed."""
    T = len(panel)
    closes = {s: panel[s]["close"] for s in syms}
    others = [s for s in syms if s != base]
    units = {s: 0.0 for s in syms}
    cash = 1.0
    out = {s: [0.0] * T for s in syms}
    for i in range(T):
        px = {s: closes[s][i] for s in syms}
        val = cash + sum(units[s] * px[s] for s in syms if px[s] is not None)
        cur = {s: (units[s] * px[s] / val if px[s] is not None and val > 0 else 0.0) for s in syms}
        if flags[i] and val > 0:
            tgt = decide(i)
            tgt = {s: (max(0.0, tgt.get(s, 0.0)) if px[s] is not None else 0.0) for s in syms}
            tot = sum(tgt.values())
            if tot > 1.0:
                tgt = {s: w / tot for s, w in tgt.items()}
            new = dict(cur)
            changed = False
            for s in others:
                if (tgt[s] > 0) != (cur[s] > 1e-12) or abs(tgt[s] - cur[s]) > band:
                    new[s] = tgt[s]
                    changed = True
            if base in syms and px[base] is not None:
                cash_tgt = 1.0 - sum(tgt.values())
                b = max(0.0, 1.0 - cash_tgt - sum(new[s] for s in others))
                if changed or (b > 0) != (cur[base] > 1e-12) or abs(b - cur[base]) > band:
                    new[base] = b
                    changed = True
            if changed:
                tot = sum(new.values())
                if tot > 1.0:
                    new = {s: w / tot for s, w in new.items()}
                for s in syms:
                    units[s] = new[s] * val / px[s] if px[s] else 0.0
                cash = val * (1.0 - sum(new.values()))
                cur = new
        for s in syms:
            out[s][i] = cur[s]
    return out


def _trend_score(sm, lags, hyst):
    """Multi-lookback trend score in [0, 1] for a (smoothed) price series.

    For each lag a binary state is kept with a dead zone: it switches ON when the lag-momentum
    rises above +hyst and OFF when it falls below -hyst (the first state is simply mom > 0).
    The score is the average state over the lags; None while any lag is warming up."""
    n = len(sm)
    states = []
    for lag in lags:
        mom = _momentum(sm, lag)
        st, prev = [None] * n, None
        for i in range(n):
            m = mom[i]
            if m is None:
                continue
            if prev is None:
                prev = 1.0 if m > 0 else 0.0
            elif m > hyst:
                prev = 1.0
            elif m < -hyst:
                prev = 0.0
            st[i] = prev
        states.append(st)
    out = [None] * n
    k = float(len(lags))
    for i in range(n):
        vals = [s[i] for s in states]
        if all(v is not None for v in vals):
            out[i] = sum(vals) / k
    return out


def _lags(lookback, bpd, factors=(0.5, 1.0, 2.0)):
    return [max(1, int(round(lookback * f))) * bpd for f in factors]


# --------------------------------------------------------------------------- strategies

class HedgeCoreTrend(Strategy):
    """Dollar core + gold sleeve + trend-gated BTC sleeve.

    Target: `w_paxg` in PAXG and `w_btc` in BTC, the rest in USDT. Each sleeve is scaled by its
    USD-terms trend score (share of the lookbacks L/2, L, 2L days over which the 24h-EMA price has
    risen against USDT, with a +-`hyst` dead zone). A sleeve that is not trending hands its weight
    back to USDT. `lb_paxg = 0` holds the gold sleeve permanently (no gate), which is what TRAIN
    preferred: gold's USD trend had no forecasting value there, BTC's 60-90 day trend did."""

    name = "hedge_core_trend"
    res = "60"
    symbols = ["USDT_IRT", "PAXG_IRT", "BTC_IRT"]
    description = ("USDT core with a gold sleeve and a BTC sleeve, each scaled by a multi-lookback "
                   "USD-terms trend score; daily decisions, band rebalancing.")
    # defaults = the TRAIN-selected config (centre of a plateau: every +-25% neighbour also beat
    # hold_usdt on win30 median and p10)
    default_params = {"w_paxg": 0.3, "w_btc": 0.2, "lb_paxg": 0, "lb_btc": 90, "hyst": 0.02,
                      "band": 0.1, "every_days": 1}
    param_grid = {
        "w_paxg": [0.3, 0.5, 0.7],
        "w_btc": [0.0, 0.2, 0.3],
        "lb_paxg": [0, 60, 120],
        "lb_btc": [30, 60, 90],
        "hyst": [0.02],
    }

    def weights(self, panel):
        p = self.params
        bpd = _bars_per_day(panel)
        sleeves = []
        for sym, w, lb in (("PAXG_IRT", p["w_paxg"], p["lb_paxg"]), ("BTC_IRT", p["w_btc"], p["lb_btc"])):
            if w <= 0:
                continue
            score = None
            if lb and lb > 0:
                score = _trend_score(_ema(_usd_price(panel, sym), bpd), _lags(lb, bpd), p["hyst"])
            sleeves.append((sym, w, score))

        def decide(i):
            tgt = {}
            for sym, w, score in sleeves:
                tgt[sym] = w * (1.0 if score is None else (score[i] or 0.0))
            tgt[BASE] = max(0.0, 1.0 - sum(tgt.values()))
            return tgt

        flags = _decision_flags(panel.ts, p["every_days"])
        return _drifting_book(panel, self.symbols, decide, flags, p["band"])


class HedgeRiskParity(Strategy):
    """USD-terms risk parity with a volatility target, optional trend gate and USDT floor.

    Each risky asset (PAXG, BTC, ETH) gets weight proportional to trend_score / USD-terms
    volatility (daily returns of the 24h-EMA price over `vol_days`). The trend score is the
    multi-lookback one used by hedge_core_trend; `lookback = 0` disables the gate (pure
    inverse-vol, score 1). The risky mix is then scaled so that its stand-alone USD-terms vol,
    ignoring diversification (i.e. conservatively), equals `target_vol`, capped at `max_risky`
    of equity. The rest is USDT: the portfolio is "dollars + a controlled amount of non-dollar
    risk", and it automatically shrinks a sleeve whose volatility spikes (e.g. a gold crash)."""

    name = "hedge_risk_parity"
    res = "60"
    symbols = ["USDT_IRT", "PAXG_IRT", "BTC_IRT", "ETH_IRT"]
    description = ("Inverse-vol weights across PAXG/BTC/ETH in USD terms, optionally trend-gated, "
                   "scaled to a target volatility; remainder in USDT. Daily decisions, band rebalancing.")
    # defaults = the TRAIN-selected config; lookback 45..90 is a plateau, <= 40 whipsaws badly.
    # lookback=0 (pure inverse-vol, ~12 trades/year) is the low-turnover alternative.
    default_params = {"target_vol": 0.1, "vol_days": 60, "lookback": 60, "hyst": 0.02,
                      "max_risky": 0.6, "band": 0.1, "every_days": 1}
    param_grid = {
        "target_vol": [0.1, 0.15, 0.2, 0.3],
        "vol_days": [20, 60],
        "lookback": [0, 30, 60, 90],       # 0 = no trend gate
        "max_risky": [0.6, 0.9],
        "band": [0.05, 0.1],
    }
    RISKY = ["PAXG_IRT", "BTC_IRT", "ETH_IRT"]

    def weights(self, panel):
        p = self.params
        bpd = _bars_per_day(panel)
        vol, score = {}, {}
        for s in self.RISKY:
            sm = _ema(_usd_price(panel, s), bpd)
            vol[s] = _rolling_vol(sm, int(p["vol_days"]), bpd)
            if p["lookback"] and p["lookback"] > 0:
                score[s] = _trend_score(sm, _lags(p["lookback"], bpd), p["hyst"])
            else:
                score[s] = None

        def decide(i):
            raw = {}
            for s in self.RISKY:
                v = vol[s][i]
                sc = 1.0 if score[s] is None else score[s][i]
                if v and sc:
                    raw[s] = sc / v
            tgt = {}
            if raw:
                tot = sum(raw.values())
                w = {s: x / tot for s, x in raw.items()}          # sums to 1
                mix_vol = sum(w[s] * vol[s][i] for s in w)        # no diversification credit
                scale = min(p["max_risky"], p["target_vol"] / mix_vol) if mix_vol > 0 else 0.0
                tgt = {s: w[s] * scale for s in w}
            tgt[BASE] = max(0.0, 1.0 - sum(tgt.values()))
            return tgt

        flags = _decision_flags(panel.ts, p["every_days"])
        return _drifting_book(panel, self.symbols, decide, flags, p["band"])
