"""Historical candle download and local CSV cache.

Candles come from the (undocumented) TradingView endpoint used by bitpin.ir charts:
    GET https://api.bitpin.org/v1/mkt/tv/get_bars/?symbol=BTC_IRT&res=60&from=<s>&to=<s>
It returns at most 10000 bars per request; `res` is minutes (1,5,15,30,60,240) or 1D / 1W.
`ts` is the bar OPEN time in epoch seconds. The newest bar is still forming and is dropped
by `closed_only`.
"""
import csv
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass

log = logging.getLogger(__name__)

BARS_URL = "https://api.bitpin.org/v1/mkt/tv/get_bars/"
MAX_BARS = 10000
RES_SECONDS = {"1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600, "240": 14400, "1D": 86400, "1W": 604800}
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Bitpin is always reached DIRECTLY: ProxyHandler({}) makes urllib ignore http_proxy / https_proxy /
# ALL_PROXY from the environment (see bitpin/api.py direct_opener; only the Kimi LLM client may use a
# proxy, via KIMI_HTTPS_PROXY).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class Bar:
    ts: int          # bar open time, epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: float    # base-asset volume


def http_get_json(url, params=None, timeout=20, retries=4):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    delay = 1.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bitpin-bot/1.0", "Accept": "application/json"})
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def fetch_bars(symbol, res="60", start=None, end=None):
    """Download bars for [start, end] (epoch seconds), paging backwards past the 10000-bar cap."""
    step = RES_SECONDS[res]
    end = int(end or time.time())
    start = int(start or end - step * (MAX_BARS - 1))
    out = {}
    hi = end
    while hi > start:
        lo = max(start, hi - step * (MAX_BARS - 1))
        rows = http_get_json(BARS_URL, {"symbol": symbol, "res": res, "from": lo, "to": hi})
        if isinstance(rows, dict):  # error payload such as {"detail": ...}
            raise RuntimeError("get_bars %s %s: %s" % (symbol, res, rows))
        if not rows:
            break
        bad = 0
        for r in rows:
            b = Bar(int(float(r["ts"])), float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"]), float(r["volume"]))
            if bar_ok(b):
                out[b.ts] = b
            else:
                bad += 1
        if bad:
            log.warning("get_bars %s %s: %d bar(s) with a non-finite or non-positive price dropped", symbol, res, bad)
        oldest = min(int(float(r["ts"])) for r in rows)
        if oldest >= hi:
            break
        hi = oldest - 1
        time.sleep(0.25)
    return [out[k] for k in sorted(out)]


def closed_only(bars, res="60", now=None):
    """Drop bars that have not closed yet."""
    now = now or time.time()
    step = RES_SECONDS[res]
    return [b for b in bars if b.ts + step <= now]


def csv_path(symbol, res="60"):
    return os.path.join(DATA_DIR, "%s_%s.csv" % (symbol, res))


def bar_ok(b):
    """v3.13: a usable bar - finite numbers, positive prices, a volume that is not negative (a NaN / inf or a zero
    price from a broken answer or a damaged cache would poison every indicator after it)."""
    vals = (b.open, b.high, b.low, b.close, b.volume)
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vals):
        return False
    return min(b.open, b.high, b.low, b.close) > 0 and b.volume >= 0


def save_csv(bars, symbol, res="60"):
    """Write the cache atomically (v3.13: a temporary file, fsync, rename - a crash mid-write left a cut CSV
    before); unusable bars (bar_ok) are not written."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = csv_path(symbol, res)
    tmp = "%s.tmp%d" % (path, os.getpid())
    try:
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "open", "high", "low", "close", "volume"])
            for b in bars:
                if bar_ok(b):
                    w.writerow([b.ts, repr(b.open), repr(b.high), repr(b.low), repr(b.close), repr(b.volume)])
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load_csv(symbol, res="60"):
    """The cached bars; a row that does not parse or is not usable (bar_ok) is skipped with a warning."""
    out, bad = [], 0
    with open(csv_path(symbol, res), newline="") as f:
        for x in csv.DictReader(f):
            try:
                b = Bar(int(x["ts"]), float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                        float(x["volume"]))
            except (KeyError, TypeError, ValueError):
                bad += 1
                continue
            if bar_ok(b):
                out.append(b)
            else:
                bad += 1
    if bad:
        log.warning("%s: %d unusable row(s) skipped", csv_path(symbol, res), bad)
    return out


def available_symbols(res="60"):
    if not os.path.isdir(DATA_DIR):
        return []
    suffix = "_%s.csv" % res
    return sorted(f[: -len(suffix)] for f in os.listdir(DATA_DIR) if f.endswith(suffix))


def resample(bars, factor):
    """Aggregate consecutive bars into bars `factor` times longer (e.g. 1h -> 4h with factor=4).
    Buckets are aligned to multiples of the new period in epoch time; partial buckets are kept
    only if complete (every expected source timestamp present exactly once: a duplicate cannot
    stand in for a missing bar). The source step is the most common gap between consecutive bars
    (v3.13: before, the gap of the first two bars - a missing bar right at the start gave the
    whole series a wrong period); fewer than two bars give no bucket."""
    if factor == 1 or not bars:
        return list(bars)
    gaps = Counter(b.ts - a.ts for a, b in zip(bars, bars[1:]) if b.ts > a.ts)
    if not gaps:
        return []
    step = max(gaps.items(), key=lambda kv: (kv[1], -kv[0]))[0]     # the most common; a tie: the shorter
    period = step * factor
    buckets = {}
    for b in bars:
        buckets.setdefault(b.ts - (b.ts % period), []).append(b)
    out = []
    for k in sorted(buckets):
        g = sorted(buckets[k], key=lambda b: b.ts)
        # Duplicates and missing bars both have to fail here: the cached CSV can keep
        # duplicate timestamps, and a duplicate must not fill a missing bar's slot. The
        # expected grid starts at the bucket's first bar, so a series offset from the
        # period multiples (e.g. bars at :30) keeps working.
        if [b.ts for b in g] != [g[0].ts + i * step for i in range(factor)]:
            continue
        out.append(Bar(k, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                       sum(x.volume for x in g)))
    return out
