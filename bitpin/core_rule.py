"""v3.17 CORE RULE: a slow, pre-registered BTC/USDT trend rule decides the allocation; Kimi is a shadow.

Owner's choice (2026-10-30, after the research "how similar bots use LLMs"): the code decides, the model
does not. The rule is the variant scripts/research_btc_trend.py tested on Bitpin's daily BTC_USDT closes
since 2021 (SMA 200, volatility-scaled, BTC capped at 60%):

  * the last CLOSED daily close of BTC in USDT above its `lookback_days` simple moving average -> BTC on;
  * the size while on is min(cap, target_vol / realised `vol_days`-day volatility), else 0 (all USDT);
  * a no-trade band: the held weight changes only when the target differs by `band` or more, or the
    target is 0 (an exit is never skipped and never waits for the model);
  * Kimi's optional "core_veto" may only LOWER a rise of the core weight (multiplier 1, 0.5 or 0) and
    must name an event category; it never buys, never upsizes and never blocks a sale.

`allocation` "paper" logs what the rule would hold and keeps the account in USDT; "live" executes it.
Every function here is pure (no I/O): the brain injects the closes.
"""
import math
import statistics

DEFAULT_CORE = {
    "allocation": "paper",       # "paper" = log only, the account stays in USDT; "live" = execute the rule
    "symbol": "BTC_IRT",         # the market the rule allocates to (bought through BTC_USDT by routing)
    "price_symbol": "BTC_USDT",  # the daily closes the signal is computed on (BTC in USDT)
    "lookback_days": 200,
    "cap": 0.60,
    "target_vol": 0.40,
    "vol_days": 30,
    "band": 0.15,
    "veto": True,                # accept Kimi's core_veto (lower only)
}
ALLOCATIONS = ("paper", "live")
VETO_MULTIPLIERS = (0.0, 0.5, 1.0)
VETO_EVENTS = ("hack", "exchange_halt", "delisting", "stablecoin_depeg", "regulatory", "other")


class CoreConfigError(ValueError):
    pass


def validate_core(cfg):
    """None (the rule is off) or the merged, checked core config."""
    if cfg is None:
        return None
    if not isinstance(cfg, dict):
        raise CoreConfigError("brain.core must be null or an object")
    out = dict(DEFAULT_CORE)
    for k, v in cfg.items():
        if k.startswith("_"):
            continue
        if k not in DEFAULT_CORE:
            raise CoreConfigError("unknown brain.core key %r (known: %s)" % (k, sorted(DEFAULT_CORE)))
        out[k] = v
    if out["allocation"] not in ALLOCATIONS:
        raise CoreConfigError("brain.core.allocation must be one of %s" % ", ".join(ALLOCATIONS))
    for k in ("symbol", "price_symbol"):
        if not isinstance(out[k], str) or not out[k].strip():
            raise CoreConfigError("brain.core.%s must be a market symbol" % k)
        out[k] = out[k].strip().upper()
    for k, lo, hi, integer in (("lookback_days", 20, 400, True), ("vol_days", 5, 120, True), ("cap", 0.0, 1.0, False),
                               ("target_vol", 0.05, 2.0, False), ("band", 0.0, 0.5, False)):
        v = out[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi \
                or (integer and int(v) != v):
            raise CoreConfigError("brain.core.%s must be a number in %s..%s" % (k, lo, hi))
        out[k] = int(v) if integer else float(v)
    if not isinstance(out["veto"], bool):
        raise CoreConfigError("brain.core.veto must be true or false")
    return out


def core_weight(closes, cfg):
    """(weight, info) of the rule on daily closes (oldest first, closed days only); weight None when there is
    not enough history (the caller then holds)."""
    n, vd = cfg["lookback_days"], cfg["vol_days"]
    px = [float(c) for c in closes if c is not None and float(c) > 0]
    if len(px) < max(n, vd + 1):
        return None, {"error": "need %d daily closes, have %d" % (max(n, vd + 1), len(px))}
    sma = sum(px[-n:]) / n
    rets = [math.log(px[i] / px[i - 1]) for i in range(len(px) - vd, len(px))]
    vol = statistics.pstdev(rets) * math.sqrt(365)
    on = px[-1] > sma
    w = (min(cfg["cap"], cfg["target_vol"] / vol) if vol > 0 else cfg["cap"]) if on else 0.0
    return round(w, 4), {"close": px[-1], "sma": round(sma, 2), "vol": round(vol, 4), "on": on,
                         "distance_pct": round((px[-1] / sma - 1) * 100, 2)}


def banded(target, held, band):
    """The weight to hold: the target when it moved by `band` or more or is an exit, else the held weight."""
    if target <= 0:
        return 0.0
    return target if abs(target - held) >= band - 1e-12 else held


def parse_veto(raw):
    """(multiplier, note) of the reply's optional core_veto; a missing or malformed veto is 1 (no veto)."""
    if raw is None:
        return 1.0, None
    if not isinstance(raw, dict):
        return 1.0, "core_veto ignored: not an object"
    m = raw.get("multiplier")
    ev = raw.get("event")
    if isinstance(m, bool) or not isinstance(m, (int, float)) or float(m) not in VETO_MULTIPLIERS:
        return 1.0, "core_veto ignored: multiplier must be one of 1, 0.5, 0"
    if float(m) < 1.0 and ev not in VETO_EVENTS:
        return 1.0, "core_veto ignored: a veto needs an event category (%s)" % ", ".join(VETO_EVENTS)
    return float(m), None


def paper_equity(points, closes_by_day, cost):
    """USDT equity (start 1.0) of holding the logged core weights: points = [(day_ts, weight)] in time order,
    closes_by_day = {day_ts: close}. Each weight is held from its day's close to the next point; a change
    pays `cost` per unit of weight traded."""
    eq, w, prev_px = 1.0, 0.0, None
    days = sorted(closes_by_day)
    want = dict(points)
    for d in days:
        px = closes_by_day[d]
        if prev_px is not None and w:
            r = px / prev_px - 1
            eq *= 1 + w * r
            w = w * (1 + r) / (1 + w * r)      # the held weight drifts with the price
        if d in want:
            new = want[d]
            eq -= eq * abs(new - w) * cost
            w = new
        prev_px = px
    return eq
