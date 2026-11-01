"""v3.19 METHODS REPORT: the core rule, Kimi's shadow and holding USDT side by side, in USDT.

Owner (2026-11-01): "every day a report in the Telegram bot comparing the methods". During the paper trial of
brain.core the decision log carries, per decision, the rule's weight (Decision.core) and Kimi's own allocation
(Decision.shadow). This module replays both on Bitpin's public daily closes:

  * every decision re-weights at the first daily close after it (Bitpin's day ends at Tehran midnight; the
    last decision before a close wins); a shadow that is invalid keeps the previous shadow weights;
  * weights drift with the prices between decisions;
  * a change pays COST_USDT_ROUTE per unit of weight for coins traded through COIN_USDT, COST_TOMAN_ROUTE
    for every other market (an estimate: spread and slippage are not measured here);
  * prices are in USDT: COIN_USDT closes for the routed coins, else SYMBOL_IRT / USDT_IRT.

Pure functions plus one injectable price fetcher; the notifier builds the Telegram text.
"""
from datetime import datetime, timedelta, timezone

SAFE = "USDT_IRT"
USDT_ROUTED = ("BTC", "ETH", "XRP", "SOL")
COST_USDT_ROUTE = 0.005
COST_TOMAN_ROUTE = 0.012
DAY = 86400


def trial_points(records):
    """(core_points, shadow_points, core_symbol): [(decided_at, {symbol: weight})] in time order from the decision
    records that carry the core rule (an invalid shadow adds no point: the previous shadow weights stay)."""
    core, shadow, sym = [], [], None
    for rec in records:
        d = rec.get("decision") if isinstance(rec, dict) else None
        c = d.get("core") if isinstance(d, dict) else None
        if not isinstance(c, dict) or not isinstance(c.get("weight"), (int, float)):
            continue
        t = d.get("decided_at")
        if not isinstance(t, (int, float)):
            continue
        sym = str(c.get("symbol") or "BTC_IRT").upper()
        core.append((t, {sym: float(c["weight"])} if c["weight"] > 0 else {}))
        s = d.get("shadow") if isinstance(d.get("shadow"), dict) else {}
        if s.get("valid") and isinstance(s.get("targets"), dict):
            shadow.append((t, {str(k).upper(): float(w) for k, w in s["targets"].items()
                               if str(k).upper() != SAFE and isinstance(w, (int, float)) and w > 0}))
    return sorted(core, key=lambda p: p[0]), sorted(shadow, key=lambda p: p[0]), sym


def cost_of(symbol):
    return COST_USDT_ROUTE if symbol.split("_")[0] in USDT_ROUTED else COST_TOMAN_ROUTE


def replay(points, prices, days):
    """[(close_time, equity)] of following the weights in `points` over the daily closes `days` (close times):
    every point is applied at the first close at or after its time (the last one before a close wins), equity
    1.0 from the first close a point applies to. prices = {symbol: {close_time: USDT close}}; a missing close
    keeps the previous one (no return that day)."""
    if not points:
        return []
    pts = sorted(points, key=lambda p: p[0])
    want, i = {}, 0
    for day in days:
        last = None
        while i < len(pts) and pts[i][0] <= day:
            last = pts[i][1]
            i += 1
        if last is not None:
            want[day] = last
    if not want:
        return []
    start = min(want)
    filled = {}
    for s, series in prices.items():          # forward-fill each symbol over the calendar
        last, filled[s] = None, {}
        for day in days:
            last = series.get(day, last)
            if last:
                filled[s][day] = last
    eq, w, prev, out = 1.0, {}, None, []
    for day in days:
        if day < start:
            continue
        if w and prev is not None:
            rets = {s: (filled.get(s, {}).get(day) or 0) / filled[s][prev] - 1 if filled.get(s, {}).get(prev) else 0.0
                    for s in w}
            rets = {s: (r if r > -1 else 0.0) for s, r in rets.items()}
            growth = 1 + sum(w[s] * rets[s] for s in w)
            eq *= growth
            w = {s: w[s] * (1 + rets[s]) / growth for s in w} if growth > 0 else {}
        if day in want:
            new = {s: v for s, v in want[day].items() if v > 0 and filled.get(s, {}).get(day)}
            for s in set(w) | set(new):
                eq -= eq * abs(new.get(s, 0.0) - w.get(s, 0.0)) * cost_of(s)
            w = new
        prev = day
        out.append((day, eq))
    return out


def usdt_closes(symbol, fetch, start, end):
    """{day: close in USDT} of an IRT market symbol; fetch(symbol, start, end) -> [(day_ts, close)]."""
    coin = symbol.split("_")[0]
    if coin in USDT_ROUTED:
        return dict(fetch("%s_USDT" % coin, start, end))
    irt, usdt = dict(fetch(symbol, start, end)), dict(fetch(SAFE, start, end))
    return {d: p / usdt[d] for d, p in irt.items() if usdt.get(d)}


def compare(records, fetch, now):
    """The comparison as numbers, or None before the trial has any decision:
    {"start", "end", "core": (total, last_day), "shadow": (...), "btc": (...), "core_now", "shadow_now"}."""
    core_pts, shadow_pts, sym = trial_points(records)
    if not core_pts:
        return None
    start, end = core_pts[0][0], now
    syms = {sym} | {s for _, w in shadow_pts for s in w}
    prices = {}
    for s in sorted(syms):
        try:
            prices[s] = usdt_closes(s, fetch, start - DAY, end + DAY)
        except Exception:  # noqa: BLE001 - one market without prices is left out (held as USDT), not the report
            prices[s] = {}
    if not prices.get(sym):
        raise ValueError("no daily closes for %s" % sym)
    days = sorted(d for d in prices.get(sym, {}) if start <= d <= end)    # closes after the first decision
    if not days:
        return {"start": start, "end": start, "core": None, "shadow": None, "btc": None,
                "core_now": core_pts[-1][1], "shadow_now": shadow_pts[-1][1] if shadow_pts else None}

    def summary(series):
        if not series:
            return None
        last = series[-1][1]
        prev = series[-2][1] if len(series) > 1 else 1.0
        return (last - 1.0, last / prev - 1.0)
    btc = prices.get(sym, {})
    btc_series = [(d, btc[d] / btc[days[0]]) for d in days] if days and btc.get(days[0]) else []
    return {"start": start, "end": days[-1] if days else end,
            "core": summary(replay(core_pts, prices, days)),
            "shadow": summary(replay(shadow_pts, prices, days)) if shadow_pts else None,
            "btc": summary(btc_series),
            "core_now": core_pts[-1][1], "shadow_now": shadow_pts[-1][1] if shadow_pts else None}


def fetch_daily(symbol, start, end):
    """Bitpin's public CLOSED daily bars as [(close_time, close)] (Bitpin's day ends at Tehran midnight)."""
    from . import data
    bars = data.closed_only(data.fetch_bars(symbol, "1D", int(start) - DAY, int(end) + DAY), "1D")
    return [(b.ts + DAY, b.close) for b in bars]


TEHRAN = timezone(timedelta(hours=3, minutes=30))


def day_label(t):
    """The Tehran date of the day that ENDS at close time t."""
    return datetime.fromtimestamp(t - 1, TEHRAN).strftime("%Y-%m-%d")


REPORT_FILE = "methods_report.json"     # in the bot's state dir: written by the brain, read by the notifier


def tail_records(path, max_bytes=16 * 1024 * 1024):
    """The JSON records of the last max_bytes of a .jsonl file (a cut first line and bad lines are skipped)."""
    import json
    import os
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def report(records, fetch, now):
    """The JSON document the bot writes after its daily decision (None before the trial has a decision)."""
    c = compare(records, fetch, now)
    if c is None:
        return None
    return {"schema": 1, "computed_at": now, "start": c["start"], "end": c["end"],
            "core": list(c["core"]) if c["core"] else None, "shadow": list(c["shadow"]) if c["shadow"] else None,
            "btc": list(c["btc"]) if c["btc"] else None, "core_now": c["core_now"], "shadow_now": c["shadow_now"]}


def load(doc):
    """The report as written by report(), checked (None when missing or malformed: the notifier then skips)."""
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        return None
    def pair(x):
        if isinstance(x, list) and len(x) == 2 and all(isinstance(v, (int, float)) for v in x):
            return tuple(float(v) for v in x)
        return None
    def weights(x):
        if not isinstance(x, dict):
            return None
        return {str(k)[:20]: float(v) for k, v in list(x.items())[:12] if isinstance(v, (int, float)) and 0 <= v <= 1}
    if not all(isinstance(doc.get(k), (int, float)) for k in ("start", "end", "computed_at")):
        return None
    return {"start": doc["start"], "end": doc["end"], "computed_at": doc["computed_at"], "core": pair(doc.get("core")),
            "shadow": pair(doc.get("shadow")), "btc": pair(doc.get("btc")), "core_now": weights(doc.get("core_now")) or {},
            "shadow_now": weights(doc.get("shadow_now"))}
