"""Pre-registered test of a slow BTC/USDT trend rule against holding USDT (research only, no orders).

The rule (owner's choice 2026-10-30: a slow BTC trend core, BTC capped at 60% of the account):
  * signal: the daily close of BTC in USDT above its N-day simple moving average -> BTC on, else USDT;
  * size while on: "fixed" = the cap (0.60), or "vol" = min(cap, TARGET_VOL / realised 30-day vol);
  * a no-trade band: the weight changes only when the target differs from the held weight by BAND or
    more, or the target is 0 (an exit is never skipped);
  * the signal of day t is traded at the close of day t+1 (no look-ahead, and one daily decision).

Variants fixed BEFORE the run: N in (50, 100, 200) x size in (fixed, vol) = 6 variants.
Primary variant fixed before the run: N=100, size=vol.
Costs per side: base 0.5% (BTC_USDT taker 0.35% + spread/slippage), stress 1.2% (the toman route).

PASS (fixed before the run): under the STRESS cost
  1. the primary variant's total return in USDT is above 0 (holding USDT = 0) and its max drawdown
     is at most 35%, and
  2. at least 4 of the 6 variants have a total return above 0.

Usage: py scripts/research_btc_trend.py   (downloads public daily bars from Bitpin)
"""
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import data  # noqa: E402

CAP = 0.60
TARGET_VOL = 0.40
VOL_DAYS = 30
BAND = 0.15
LOOKBACKS = (50, 100, 200)
SIZES = ("fixed", "vol")
PRIMARY = (100, "vol")
COSTS = {"base": 0.005, "stress": 0.012}
MAX_DD_PASS = 0.35


def closes():
    now = int(time.time())
    bars = data.closed_only(data.fetch_bars("BTC_USDT", "1D", now - 86400 * 4000, now), "1D", now)
    return [b.ts for b in bars], [b.close for b in bars]


def targets(px, n, size):
    """Target BTC weight decided at each day's close (None until the lookback is filled)."""
    out = []
    for i in range(len(px)):
        if i < max(n, VOL_DAYS + 1) - 1:
            out.append(None)
            continue
        sma = sum(px[i - n + 1:i + 1]) / n
        if px[i] <= sma:
            out.append(0.0)
            continue
        if size == "fixed":
            out.append(CAP)
            continue
        rets = [math.log(px[j] / px[j - 1]) for j in range(i - VOL_DAYS + 1, i + 1)]
        vol = statistics.pstdev(rets) * math.sqrt(365)
        out.append(min(CAP, TARGET_VOL / vol) if vol > 0 else CAP)
    return out


def simulate(ts, px, tgt, cost):
    """Equity in USDT; the target of day t-1 is traded at the close of day t."""
    start = next(i for i, t in enumerate(tgt) if t is not None) + 1
    eq, w, trades, days_on = 1.0, 0.0, 0, 0
    curve, dates = [1.0], [ts[start - 1]]
    for i in range(start, len(px)):
        # the held weight drifts with the price over the day
        r = px[i] / px[i - 1] - 1
        eq_new = eq * (1 + w * r)
        w = w * (1 + r) / (1 + w * r) if eq_new > 0 else 0.0
        eq = eq_new
        want = tgt[i - 1]
        if want is not None and (abs(want - w) >= BAND or (want == 0 and w > 0)):
            eq -= eq * abs(want - w) * cost
            w, trades = want, trades + 1
        days_on += w > 0
        curve.append(eq)
        dates.append(ts[i])
    return dates, curve, trades, days_on / max(1, len(curve) - 1)


def stats(dates, curve):
    years = (dates[-1] - dates[0]) / (365 * 86400)
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    sd = statistics.pstdev(rets)
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    return {"total": curve[-1] - 1, "cagr": curve[-1] ** (1 / years) - 1 if years > 0 else 0.0,
            "sharpe": statistics.mean(rets) / sd * math.sqrt(365) if sd > 0 else 0.0, "mdd": mdd}


def by_year(dates, curve):
    out, first = {}, {}
    for d, v in zip(dates, curve):
        y = datetime.fromtimestamp(d, timezone.utc).year
        first.setdefault(y, v)
        out[y] = v / first[y] - 1
    return out


def main():
    ts, px = closes()
    print("BTC_USDT daily closes: %d, %s .. %s" % (len(px), datetime.fromtimestamp(ts[0], timezone.utc).date(),
                                                   datetime.fromtimestamp(ts[-1], timezone.utc).date()))
    start = max(LOOKBACKS) + 1    # every variant scored over the same window
    bh = [p / px[start - 1] for p in px[start - 1:]]
    s = stats(ts[start - 1:], bh)
    print("buy and hold BTC: total %+.1f%%  CAGR %+.1f%%  Sharpe %.2f  maxDD %.1f%%"
          % (100 * s["total"], 100 * s["cagr"], s["sharpe"], 100 * s["mdd"]))
    print("hold USDT: total +0.0% (the benchmark)\n")
    results = {}
    for cost_name, cost in COSTS.items():
        print("== cost %.1f%% per side (%s)" % (100 * cost, cost_name))
        for n in LOOKBACKS:
            for size in SIZES:
                tgt = targets(px, n, size)
                tgt = [None] * start + tgt[start:]    # same scoring window for all variants
                dates, curve, trades, on = simulate(ts, px, tgt, cost)
                s = stats(dates, curve)
                results[(cost_name, n, size)] = (s, dates, curve)
                yrs = by_year(dates, curve)
                print("SMA%-3d %-5s total %+6.1f%%  CAGR %+5.1f%%  Sharpe %5.2f  maxDD %4.1f%%  trades %3d  in BTC %3.0f%%  "
                      "years %s" % (n, size, 100 * s["total"], 100 * s["cagr"], s["sharpe"], 100 * s["mdd"], trades,
                                    100 * on, " ".join("%d:%+.0f%%" % (y, 100 * r) for y, r in sorted(yrs.items()))))
                tail = [v for d, v in zip(dates, curve) if d >= dates[-1] - 150 * 86400]
                print("          last 150 days %+.1f%%" % (100 * (tail[-1] / tail[0] - 1)))
        print()
    prim = results[("stress",) + PRIMARY][0]
    winners = sum(results[("stress", n, size)][0]["total"] > 0 for n in LOOKBACKS for size in SIZES)
    ok1 = prim["total"] > 0 and prim["mdd"] <= MAX_DD_PASS
    ok2 = winners >= 4
    print("PASS check (stress cost): primary SMA%d %s total %+.1f%% maxDD %.1f%% -> %s; variants above 0: %d of 6 -> %s"
          % (PRIMARY[0], PRIMARY[1], 100 * prim["total"], 100 * prim["mdd"], ok1, winners, ok2))
    print("RESULT:", "PASS" if ok1 and ok2 else "FAIL")


if __name__ == "__main__":
    main()
