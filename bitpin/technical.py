# -*- coding: utf-8 -*-
"""v3.8: the technical reading - one fixed method for the chart of a coin, applied by the model AND by the code.

The market context already gives the model the exact indicators of every coin (bitpin/analysis.py): ema_dev_pct,
rsi4h, macd4h_pct, bb4h, don20_4h, sup / res, vol_ratio ... Until v3.7 the model described them in its own words in
the analysis "evidence" text. Now every candidate carries a "ta" object with fixed fields and fixed values, filled
by the rules below (the system prompt renders them from this module: method_text / schema_text), and the code
applies the SAME rules to the same numbers (reading) and reports every field the model read differently (check_ta).
The panel's technical page shows the numbers, the model's reading and the check.

The reading describes the chart; it is no setup and no base-rate row of its own (B7, B8: mechanical signals did not
beat the costs) and it never blocks a decision: a wrong field is a note for the owner, not a rejection.

Fields (the price is px_usdt, for USDT_IRT its toman price px, like the levels in the context):
    trend      ema_dev_pct[0] (EMA20) and [1] (EMA50) both > 0: up, both < 0: down, else mixed
    long       ema_dev_pct[2] (EMA200) > 0: above, < 0: below
    momentum   macd4h_pct[2] (the MACD histogram, % of the price) > MACD_FLAT: rising, < -MACD_FLAT: falling, else flat
    rsi        rsi4h >= 70 overbought, >= 55 strong, > 45 neutral, > 30 weak, else oversold
    bands      bb4h[0] (the place in the Bollinger bands) > 1 above_upper, >= 0.8 upper, > 0.2 middle, >= 0 lower,
               else below_lower
    channel    the price >= don20_4h[1] breakout, <= don20_4h[0] breakdown, above their middle upper_half, else
               lower_half
    volume     vol_ratio >= 1.5 high, <= 0.7 low, else normal
    support    sup[0] (without one: don20_4h[0] when below the price), resistance res[0] (without one: don20_4h[1])
    read       the model's own overall reading: bullish / bearish / neutral (not recomputed)
"""
import json
import math
import os

TA_FIELDS = ("trend", "long", "momentum", "rsi", "bands", "channel", "volume")   # recomputed by the code
TA_LEVELS = ("support", "resistance")
TA_VALUES = {
    "trend": ("up", "down", "mixed"),
    "long": ("above", "below"),
    "momentum": ("rising", "falling", "flat"),
    "rsi": ("overbought", "strong", "neutral", "weak", "oversold"),
    "bands": ("above_upper", "upper", "middle", "lower", "below_lower"),
    "channel": ("breakout", "upper_half", "lower_half", "breakdown"),
    "volume": ("high", "normal", "low"),
    "read": ("bullish", "bearish", "neutral"),
}
TA_KEYS = TA_FIELDS + TA_LEVELS + ("read",)
MACD_FLAT = 0.02                    # % of the price: a smaller MACD histogram is flat
RSI_OVERBOUGHT, RSI_STRONG, RSI_NEUTRAL, RSI_WEAK = 70.0, 55.0, 45.0, 30.0
BB_UPPER, BB_LOWER = 0.8, 0.2
VOL_HIGH, VOL_LOW = 1.5, 0.7
LEVEL_TOLERANCE = 0.005             # a cited support / resistance within 0.5% of the rule's level is the same level
DECISIONS_FILE = "kimi_decisions.jsonl"
TAIL_BYTES = 8 * 1024 * 1024        # the last decision record is looked for in the last 8 MB of the log
# the indicator values the panel shows per coin (exactly as the model got them)
VALUE_KEYS = ("px_usdt", "px", "ema_dev_pct", "rsi4h", "rsi1d", "macd4h_pct", "bb4h", "don20_4h", "pos30", "d30h",
              "vol_ratio", "atr4h_pct", "sig_d", "ret_usdt", "ret_irt", "sup", "sup_n", "res", "res_n", "cls",
              "blocked", "pump_guard")


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def _at(v, i):
    return _num(v[i]) if isinstance(v, (list, tuple)) and len(v) > i else None


def price_of(sym_ctx, symbol=""):
    """The price the levels are on: px_usdt (USDT_IRT: its toman price px)."""
    if not isinstance(sym_ctx, dict):
        return None
    if str(symbol).upper().startswith("USDT_"):
        return _num(sym_ctx.get("px"))
    p = _num(sym_ctx.get("px_usdt"))
    return p if p is not None else None


def reading(sym_ctx, symbol=""):
    """The rule fields of one coin's context ({} for a coin without the full-detail fields): every TA_FIELDS value
    the rules give (a field whose input is missing is left out), the nearest support / resistance with its
    distance in % of the price and how many swing points hold it, and "tone": bullish when the trend is up and the
    momentum rising, bearish when down and falling, else neutral (a description, not a signal)."""
    if not isinstance(sym_ctx, dict):
        return {}
    out = {}
    d20, d50, d200 = (_at(sym_ctx.get("ema_dev_pct"), i) for i in range(3))
    if d20 is not None and d50 is not None:
        out["trend"] = "up" if d20 > 0 and d50 > 0 else ("down" if d20 < 0 and d50 < 0 else "mixed")
    if d200 is not None and d200 != 0:
        out["long"] = "above" if d200 > 0 else "below"
    hist = _at(sym_ctx.get("macd4h_pct"), 2)
    if hist is not None:
        out["momentum"] = "rising" if hist > MACD_FLAT else ("falling" if hist < -MACD_FLAT else "flat")
    rsi = _num(sym_ctx.get("rsi4h"))
    if rsi is not None:
        out["rsi"] = ("overbought" if rsi >= RSI_OVERBOUGHT else "strong" if rsi >= RSI_STRONG else
                      "neutral" if rsi > RSI_NEUTRAL else "weak" if rsi > RSI_WEAK else "oversold")
    place = _at(sym_ctx.get("bb4h"), 0)
    if place is not None:
        out["bands"] = ("above_upper" if place > 1 else "upper" if place >= BB_UPPER else "middle" if place > BB_LOWER
                        else "lower" if place >= 0 else "below_lower")
    px = price_of(sym_ctx, symbol)
    lo, hi = _at(sym_ctx.get("don20_4h"), 0), _at(sym_ctx.get("don20_4h"), 1)
    if px is not None and lo is not None and hi is not None and hi >= lo:
        out["channel"] = ("breakout" if px >= hi else "breakdown" if px <= lo else
                          "upper_half" if px >= (lo + hi) / 2.0 else "lower_half")
    vr = _num(sym_ctx.get("vol_ratio"))
    if vr is not None:
        out["volume"] = "high" if vr >= VOL_HIGH else ("low" if vr <= VOL_LOW else "normal")
    sup, res = _at(sym_ctx.get("sup"), 0), _at(sym_ctx.get("res"), 0)
    sup_n, res_n = _at(sym_ctx.get("sup_n"), 0), _at(sym_ctx.get("res_n"), 0)
    if sup is None and lo is not None and px is not None and lo < px:
        sup, sup_n = lo, None
    if res is None and hi is not None and px is not None and hi > px:
        res, res_n = hi, None
    for key, level, n in (("support", sup, sup_n), ("resistance", res, res_n)):
        if level is not None and level > 0:
            out[key] = level
            if px:
                out[key + "_dist_pct"] = round((level / px - 1.0) * 100.0, 2)
            if n is not None:
                out[key + "_n"] = int(n)
    if "trend" in out and "momentum" in out:
        out["tone"] = ("bullish" if out["trend"] == "up" and out["momentum"] == "rising" else
                       "bearish" if out["trend"] == "down" and out["momentum"] == "falling" else "neutral")
    return out


def parse_ta(raw):
    """(ta, dropped) of the model's "ta" object: the known fields with an allowed value (lower case), support /
    resistance as positive finite numbers; dropped = the names of the fields given with a value outside the method.
    None for anything that is not an object (a compact-row coin's "ta": null)."""
    if not isinstance(raw, dict):
        return None, []
    ta, dropped = {}, []
    for k in TA_FIELDS + ("read",):
        v = raw.get(k)
        if v is None:
            continue
        v = v.strip().lower() if isinstance(v, str) else v
        if v in TA_VALUES[k]:
            ta[k] = v
        else:
            dropped.append(k)
    for k in TA_LEVELS:
        v = raw.get(k)
        if v is None:
            continue
        n = _num(v)
        if n is not None and n > 0:
            ta[k] = n
        else:
            dropped.append(k)
    return ta, dropped


def check_ta(ta, code):
    """[{field, model, code}] of the fields where the model's reading differs from the rules applied to the same
    numbers: every TA_FIELDS value the code could compute, and support / resistance more than LEVEL_TOLERANCE away
    from the rule's level. Fields the model left out are not differences."""
    out = []
    if not isinstance(ta, dict) or not isinstance(code, dict):
        return out
    for k in TA_FIELDS:
        if k in ta and k in code and ta[k] != code[k]:
            out.append({"field": k, "model": ta[k], "code": code[k]})
    for k in TA_LEVELS:
        if k in ta and k in code and abs(ta[k] / code[k] - 1.0) > LEVEL_TOLERANCE:
            out.append({"field": k, "model": ta[k], "code": code[k]})
    return out


def method_text():
    """The TECHNICAL READING paragraph of the system prompt (rendered from the constants above)."""
    return ("TECHNICAL READING (a fixed method: it describes each candidate's chart; it is no setup and no row of its "
            "own, B7, B8). Fill \"ta\" from the coin's context, on px_usdt (USDT_IRT: px) like its levels: trend: "
            "ema_dev_pct[0] and [1] both > 0 = up, both < 0 = down, else mixed; long: ema_dev_pct[2] > 0 = above (EMA200), "
            "< 0 = below; momentum: macd4h_pct[2] (the histogram) > %g = rising, < -%g = falling, else flat; rsi: rsi4h "
            ">= %g overbought, >= %g strong, > %g neutral, > %g weak, else oversold; bands: bb4h[0] > 1 above_upper, "
            ">= %g upper, > %g middle, >= 0 lower, else below_lower; channel: the price >= don20_4h[1] breakout, <= "
            "don20_4h[0] breakdown, above their middle upper_half, else lower_half; volume: vol_ratio >= %g high, <= %g "
            "low, else normal; support / resistance: sup[0] / res[0] (without one: don20_4h[0] / [1]), copied exactly; "
            "read: bullish / bearish / neutral, your overall reading, consistent with the setup and the bear case. A "
            "coin without these fields (a compact row) gets \"ta\": null. The code recomputes every rule field and "
            "shows the owner each one you read differently."
            % (MACD_FLAT, MACD_FLAT, RSI_OVERBOUGHT, RSI_STRONG, RSI_NEUTRAL, RSI_WEAK, BB_UPPER, BB_LOWER, VOL_HIGH,
               VOL_LOW))


def schema_text():
    """The "ta" object of the output schema line."""
    parts = ['"%s": "<%s>"' % (k, "|".join(TA_VALUES[k])) for k in TA_FIELDS]
    parts += ['"support": <price>', '"resistance": <price>', '"read": "<%s>"' % "|".join(TA_VALUES["read"])]
    return "{" + ", ".join(parts) + "}"


# --------------------------------------------------------------------------- the panel's technical page
def _last_record(path, tail_bytes=TAIL_BYTES):
    """The last decision record of kimi_decisions.jsonl that has a market context (dict), or None. Only the last
    tail_bytes of the file are read; lines that are not JSON objects are skipped."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            start = max(0, size - int(tail_bytes))
            f.seek(start)
            data = f.read()
    except OSError:
        return None
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]                     # the first piece may be the end of a longer line
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("context"), dict) \
                and isinstance(rec["context"].get("symbols"), dict):
            return rec
    return None


def _values(sym_ctx):
    out = {}
    for k in VALUE_KEYS:
        v = sym_ctx.get(k)
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            n = _num(v)
            if n is not None:
                out[k] = n
        elif isinstance(v, (list, tuple)):
            nums = [_num(x) for x in v[:4]]
            if all(x is not None for x in nums):
                out[k] = nums
        elif isinstance(v, str):
            out[k] = v[:80]
    return out


def collect(state_dir):
    """The technical page's data from the last decision record (read as the bot's own user; never raises for a
    missing or odd file): {time, model, mode, valid, held, candidates: [{symbol, verdict, setup, row, p, p0, ev_pct,
    pass, ta, ta_code, ta_check}], coins: [{symbol, values, reading}]}. coins = every coin with the full-detail
    fields (ema_dev_pct; compact rows have no chart fields), the held ones and the candidates first."""
    rec = _last_record(os.path.join(state_dir, DECISIONS_FILE))
    if rec is None:
        return {"time": None, "candidates": [], "coins": []}
    syms = rec["context"]["symbols"]
    dec = rec.get("decision") if isinstance(rec.get("decision"), dict) else {}
    an = dec.get("analysis") if isinstance(dec.get("analysis"), dict) else {}
    cands_raw = an.get("candidates") if isinstance(an.get("candidates"), dict) else {}
    cur = rec.get("current_weights") if isinstance(rec.get("current_weights"), dict) else {}
    held = sorted(s for s, w in cur.items() if isinstance(s, str) and _num(w) and _num(w) > 1e-6
                  and not s.upper().startswith("USDT_"))
    candidates = []
    for s, c in sorted(cands_raw.items()):
        if not isinstance(c, dict):
            continue
        sc = syms.get(s) if isinstance(syms.get(s), dict) else {}
        code = c.get("ta_code") if isinstance(c.get("ta_code"), dict) else reading(sc, s)
        ta, _dropped = parse_ta(c.get("ta"))
        checks = c.get("ta_check") if isinstance(c.get("ta_check"), list) else check_ta(ta, code)
        candidates.append({"symbol": str(s)[:24], "verdict": c.get("verdict"), "setup": c.get("setup"),
                           "row": c.get("row"), "p": _num(c.get("p")), "p0": _num(c.get("p0")),
                           "ev_pct": _num(c.get("ev_pct")), "pass": c.get("pass") is True, "ta": ta,
                           "ta_code": code, "ta_check": [x for x in checks if isinstance(x, dict)][:12]})
    first = held + [c["symbol"] for c in candidates if c["symbol"] not in held]
    coins = []
    for s in first + sorted(k for k in syms if k not in first):
        sc = syms.get(s)
        if not isinstance(sc, dict) or not isinstance(sc.get("ema_dev_pct"), list):
            continue                          # a compact row: not the full technical fields
        coins.append({"symbol": str(s)[:24], "held": s in held, "candidate": s in cands_raw,
                      "values": _values(sc), "reading": reading(sc, s)})
    return {"time": _num(rec.get("time")), "model": str(rec.get("model") or dec.get("model") or "")[:60],
            "mode": str(dec.get("mode") or rec.get("trigger") or "")[:40], "valid": dec.get("valid"),
            "held": held, "candidates": candidates, "coins": coins[:80]}


# --------------------------------------------------------------------------- v3.9: the indicators as chart series
CHART_LOOKBACK_HOURS = 1700     # the bot's context window (context.analysis_lookback_bars): the last values match it
CHART_STEP = 4 * 3600           # the 4h bars every indicator is computed on (aligned to UTC, like data.resample)
VOL_DAYS = 30                   # the average traded value (analysis.symbol_features: vol_ratio)
CHART_KEYS = ("t", "close", "ema20", "ema50", "ema200", "bb", "don", "rsi", "macd", "vol")


def _r(v, nd=6):
    """A float with nd significant digits (small JSON), or None."""
    v = _num(v)
    return None if v is None else float("%.*g" % (nd, v))


def chart_data(symbol, bars, usdt_bars, t_from, lookback=CHART_LOOKBACK_HOURS):
    """v3.9: the indicators of one coin as series for the panel's position chart, computed like the bot's market
    context (analysis.symbol_features): the coin's last `lookback` closed hourly IRT bars divided by the USDT_IRT close
    of the same hour, 4h bars aligned to UTC, EMA 20 / 50 / 200, Bollinger (20, 2), Donchian 20 (the hourly closes'
    extremes), RSI 14 and MACD (12, 26, 9, in % of the bar's close) of the 4h closes, and the traded value per 4h in
    million toman. Each point is at the CLOSE of its 4h bar; only bars closing at or after t_from are kept.
    "now" = the values of symbol_features() for the same bars (what the bot gives the model now: ema_dev_pct, rsi4h,
    macd4h_pct, bb4h, don20_4h, sup / res, vol_ratio ...) and "reading" = reading() of them; "vol_avg" = the 30-day
    average traded value per 4h (million toman). None without enough bars."""
    from . import analysis, data as data_mod, indicators as ind
    hourly = [b for b in bars or [] if _num(b.close) and b.close > 0][-int(lookback):]
    while len(hourly) > 2 and hourly[1].ts - hourly[0].ts != 3600:      # like the context: resample needs 1h steps
        hourly = hourly[1:]
    usdt = [b for b in usdt_bars or [] if _num(b.close) and b.close > 0]
    if len(hourly) < 4 * 20 or not usdt:
        return None
    usdt_ts, usdt_close = [b.ts for b in usdt], [b.close for b in usdt]
    try:
        feat = analysis.symbol_features(symbol, hourly, usdt_ts, usdt_close)
    except (ValueError, ZeroDivisionError):
        return None
    basis = []
    for b in hourly:
        u = analysis._asof(usdt_ts, usdt_close, b.ts)
        if u:
            r = b.close / u
            basis.append(data_mod.Bar(b.ts, r, r, r, r, 0.0))
    b4 = data_mod.resample(basis, 4) if len(basis) > 1 else []
    if len(b4) < 20:
        return None
    c4 = [b.close for b in b4]
    e20, e50, e200 = ind.ema(c4, 20), ind.ema(c4, 50), ind.ema(c4, 200)
    lo, mid, up = ind.bollinger(c4, 20, 2.0)
    rsi = ind.rsi(c4, 14)
    line, sig, hist = ind.macd(c4)
    value = {}
    for b in hourly:                                  # traded value of the 4h buckets (complete ones, like resample)
        value.setdefault(b.ts - b.ts % CHART_STEP, []).append(b.volume * b.close)
    out = dict((k, []) for k in CHART_KEYS)
    for i, b in enumerate(b4):
        t = b.ts + CHART_STEP
        if t < t_from:
            continue
        c = c4[i]
        out["t"].append(t)
        out["close"].append(_r(c))
        out["ema20"].append(_r(e20[i]))
        out["ema50"].append(_r(e50[i]))
        out["ema200"].append(_r(e200[i]))
        out["bb"].append([_r(lo[i]), _r(mid[i]), _r(up[i])] if None not in (lo[i], mid[i], up[i]) else None)
        out["don"].append([_r(min(x.low for x in b4[i - 19:i + 1])), _r(max(x.high for x in b4[i - 19:i + 1]))]
                          if i >= 19 else None)
        out["rsi"].append(_r(rsi[i], 4))
        out["macd"].append([_r(v / c * 100.0, 4) for v in (line[i], sig[i], hist[i])]
                           if None not in (line[i], sig[i], hist[i]) and c else None)
        v = value.get(b.ts) or []
        out["vol"].append(_r(sum(v) / 1e6, 4) if len(v) == 4 else None)
    if not out["t"]:
        return None
    t_end = hourly[-1].ts
    span = [b for b in hourly if b.ts > t_end - VOL_DAYS * 86400]
    days = max(1.0, (t_end - span[0].ts + 3600) / 86400.0)
    out["vol_avg"] = _r(sum(b.volume * b.close for b in span) / days / 6.0 / 1e6, 4)
    out["now"] = _values(feat)
    out["reading"] = reading(feat, symbol)
    return out


def trim_chart(data, t_from):
    """chart_data() from the last 4h bar closing before t_from on (the lines start at the chart's left edge)."""
    if not isinstance(data, dict) or not isinstance(data.get("t"), list):
        return None
    ts = data["t"]
    k = 0
    while k + 1 < len(ts) and ts[k + 1] <= t_from:
        k += 1
    out = dict(data)
    for key in CHART_KEYS:
        if isinstance(data.get(key), list):
            out[key] = data[key][k:]
    return out if out["t"] else None
