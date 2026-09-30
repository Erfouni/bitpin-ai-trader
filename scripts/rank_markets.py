"""Rank tradable Bitpin markets by recent quote-currency volume (from daily candles).

Usage: py scripts/rank_markets.py [--quote IRT] [--days 60] [--top 25]
Writes data/market_ranking_<QUOTE>.json
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bitpin.data import DATA_DIR, fetch_bars, http_get_json  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quote", default="IRT")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    markets = http_get_json("https://api.bitpin.org/api/v1/mkt/markets/")
    cands = [m for m in markets if m["quote"] == a.quote and m["tradable"] and not m.get("suspended")]
    now = int(time.time())
    rows = []
    for i, m in enumerate(cands):
        try:
            bars = fetch_bars(m["symbol"], "1D", now - 86400 * a.days, now)
        except Exception as e:  # noqa: BLE001
            print("skip", m["symbol"], e, file=sys.stderr)
            continue
        qv = sum(b.volume * b.close for b in bars)
        active_days = sum(1 for b in bars if b.volume > 0)
        rows.append({"symbol": m["symbol"], "quote_volume": qv, "active_days": active_days,
                     "bars": len(bars), "price_precision": m["price_precision"],
                     "base_amount_precision": m["base_amount_precision"],
                     "quote_amount_precision": m["quote_amount_precision"]})
        if i % 50 == 0:
            print("%d/%d" % (i, len(cands)), file=sys.stderr)
        time.sleep(0.22)
    rows.sort(key=lambda r: -r["quote_volume"])
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "market_ranking_%s.json" % a.quote), "w") as f:
        json.dump(rows, f, indent=1)
    for r in rows[: a.top]:
        print("%-14s %20.0f  days=%d" % (r["symbol"], r["quote_volume"], r["active_days"]))


if __name__ == "__main__":
    main()
