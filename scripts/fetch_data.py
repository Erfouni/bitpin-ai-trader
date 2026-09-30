"""Download hourly candle history for a list of symbols into data/<SYMBOL>_60.csv.

Usage:
  py scripts/fetch_data.py BTC_IRT USDT_IRT ETH_IRT --days 730
  py scripts/fetch_data.py --from-ranking IRT --top 15 --days 730
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bitpin.data import DATA_DIR, closed_only, fetch_bars, save_csv  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--from-ranking", metavar="QUOTE")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--res", default="60")
    a = ap.parse_args()
    syms = list(a.symbols)
    if a.from_ranking:
        with open(os.path.join(DATA_DIR, "market_ranking_%s.json" % a.from_ranking)) as f:
            syms += [r["symbol"] for r in json.load(f)[: a.top]]
    now = int(time.time())
    for s in dict.fromkeys(syms):
        bars = closed_only(fetch_bars(s, a.res, now - a.days * 86400, now), a.res)
        save_csv(bars, s, a.res)
        span = (bars[-1].ts - bars[0].ts) / 86400 if bars else 0
        print("%-12s %6d bars  %.0f days" % (s, len(bars), span))


if __name__ == "__main__":
    main()
