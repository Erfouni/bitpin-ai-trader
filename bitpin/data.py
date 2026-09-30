"""Historical candle download and local CSV cache.

Candles come from the (undocumented) TradingView endpoint used by bitpin.ir charts:
    GET https://api.bitpin.org/v1/mkt/tv/get_bars/?symbol=BTC_IRT&res=60&from=<s>&to=<s>
It returns at most 10000 bars per request; `res` is minutes (1,5,15,30,60,240) or 1D / 1W.
`ts` is the bar OPEN time in epoch seconds. The newest bar is still forming and is dropped
by `closed_only`.
"""
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

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
        for r in rows:
            b = Bar(int(float(r["ts"])), float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"]), float(r["volume"]))
            out[b.ts] = b
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


def save_csv(bars, symbol, res="60"):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(csv_path(symbol, res), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.ts, repr(b.open), repr(b.high), repr(b.low), repr(b.close), repr(b.volume)])


def load_csv(symbol, res="60"):
    with open(csv_path(symbol, res), newline="") as f:
        r = csv.DictReader(f)
        return [Bar(int(x["ts"]), float(x["open"]), float(x["high"]), float(x["low"]),
                    float(x["close"]), float(x["volume"])) for x in r]


def available_symbols(res="60"):
    if not os.path.isdir(DATA_DIR):
        return []
    suffix = "_%s.csv" % res
    return sorted(f[: -len(suffix)] for f in os.listdir(DATA_DIR) if f.endswith(suffix))


def resample(bars, factor):
    """Aggregate consecutive bars into bars `factor` times longer (e.g. 1h -> 4h with factor=4).
    Buckets are aligned to multiples of the new period in epoch time; partial buckets are kept
    only if complete (all `factor` source bars present)."""
    if factor == 1 or not bars:
        return list(bars)
    step = bars[1].ts - bars[0].ts if len(bars) > 1 else 0
    period = step * factor
    buckets = {}
    for b in bars:
        buckets.setdefault(b.ts - (b.ts % period), []).append(b)
    out = []
    for k in sorted(buckets):
        g = buckets[k]
        if len(g) != factor:
            continue
        out.append(Bar(k, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                       sum(x.volume for x in g)))
    return out
