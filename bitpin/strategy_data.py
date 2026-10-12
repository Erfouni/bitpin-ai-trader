"""Closed, consecutive public price episodes for a reconstructed-history experiment.

No network, account, news or present-day collector data is read here. A receipt
dates the reconstruction; it does not assert original historical availability.
"""
import hashlib
import json
import math
import time
from collections import Counter

DAY = 86400
CLASSES = ("down", "flat", "up")
FEATURES = ("return_1", "return_7", "return_30", "volatility_14", "volume_ratio")


def _hash(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def validate_bars(rows, step=DAY, cutoff=None):
    """Drop malformed, duplicate, illiquid and not-yet-closed OHLCV rows.

    A repeated timestamp is excluded even when both copies agree. All callers
    check exact adjacency after validation, so a rejected row creates a gap.
    """
    cutoff = time.time() if cutoff is None else float(cutoff)
    if not math.isfinite(cutoff) or not isinstance(step, int) or isinstance(step, bool) or step <= 0:
        raise ValueError("finite cutoff and positive integer candle step required")
    parsed, counts = [], Counter()
    for row in rows:
        try:
            get = row.get if isinstance(row, dict) else lambda k: getattr(row, k)
            if isinstance(get("ts"), bool):
                continue
            ts = float(get("ts"))
            if not math.isfinite(ts) or ts != int(ts) or ts < 0:
                continue
            counts[int(ts)] += 1
            raw_values = [get(k) for k in ("open", "high", "low", "close", "volume")]
            if any(isinstance(value, bool) for value in raw_values):
                continue
            values = [float(value) for value in raw_values]
            if not all(math.isfinite(x) for x in values):
                continue
            op, high, low, close, volume = values
            if min(op, high, low, close) <= 0 or volume <= 0:
                continue
            if high < max(op, close, low) or low > min(op, close, high):
                continue
            if ts + step > cutoff:
                continue
            parsed.append(dict(zip(("ts", "open", "high", "low", "close", "volume"),
                                   [int(ts)] + values)))
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            continue
    return sorted((row for row in parsed if counts[row["ts"]] == 1), key=lambda x: x["ts"])


def _features(history):
    closes = [row["close"] for row in history]
    daily = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 14, len(closes))]
    mean = sum(daily) / len(daily)
    volumes = [row["volume"] for row in history[-30:]]
    return {"return_1": closes[-1] / closes[-2] - 1,
            "return_7": closes[-1] / closes[-8] - 1,
            "return_30": closes[-1] / closes[-31] - 1,
            "volatility_14": math.sqrt(sum((x - mean) ** 2 for x in daily) / len(daily)),
            "volume_ratio": volumes[-1] / (sum(volumes) / len(volumes)) - 1}


def build_episodes(panel, horizon=DAY, warmup=60, cutoff=None, cost_band=.008,
                   quote_symbol=None):
    """Return labelled daily episodes with outcomes separate from blinded input.

    ``panel`` maps symbols to OHLCV sequences, or ``{bars, receipt}`` wrappers.
    ``ts`` is the candle open. The decision consumes ``warmup`` completed bars;
    entry is the following open, outcome is the open exactly ``horizon`` later.
    In USDT mode both historical prices and outcomes require synchronized quote
    bars. Only open/close ratios are presented: simultaneous OHLC extremes cannot
    be recovered from two independent candle series.
    """
    if horizon <= 0 or horizon % DAY or warmup < 31:
        raise ValueError("horizon must be positive whole days; warmup must be at least 31")
    if not math.isfinite(cost_band) or cost_band < 0:
        raise ValueError("cost_band must be finite and nonnegative")
    cutoff = time.time() if cutoff is None else float(cutoff)
    clean, receipts = {}, {}
    if quote_symbol is not None:
        if quote_symbol != 'USDT_IRT' or any(not isinstance(s,str) or not s.endswith('_IRT') for s in panel):
            raise ValueError('USDT conversion requires IRT-quoted assets and USDT_IRT')
    for symbol, series in panel.items():
        wrapper = series if isinstance(series, dict) else {"bars": series}
        clean[symbol] = {row["ts"]: row for row in validate_bars(wrapper.get("bars", []), cutoff=cutoff)}
        receipts[symbol] = dict(wrapper.get("receipt") or {})
    if quote_symbol is not None and quote_symbol not in clean:
        raise ValueError("quote series is required")
    episodes = []
    for symbol in sorted(clean):
        if symbol == quote_symbol:
            continue
        source = clean[symbol]
        quote = clean.get(quote_symbol)
        bars = {}
        for ts, row in source.items():
            if quote is not None and ts not in quote:
                continue
            bars[ts] = {"ts": ts, "open": row["open"] / quote[ts]["open"] if quote else row["open"],
                        "close": row["close"] / quote[ts]["close"] if quote else row["close"],
                        "volume": row["volume"]}
        source_hash = _hash([source[k] for k in sorted(source)])
        quote_hash = _hash([quote[k] for k in sorted(quote)]) if quote else None
        for decision in sorted(bars):
            label_end = decision + horizon
            history_times = list(range(decision - warmup * DAY, decision, DAY))
            outcome_times = list(range(decision, label_end + DAY, DAY))
            if any(ts not in bars for ts in history_times + outcome_times):
                continue
            history = [bars[ts] for ts in history_times]
            anchor = history[-1]["close"]
            volume_mean = sum(row["volume"] for row in history) / warmup
            blinded = [{"step": i - warmup + 1, "open": row["open"] / anchor,
                        "close": row["close"] / anchor, "volume": row["volume"] / volume_mean}
                       for i, row in enumerate(history)]
            features = _features(history)
            realized = bars[label_end]["open"] / bars[decision]["open"] - 1
            label = 0 if realized < -cost_band else 2 if realized > cost_band else 1
            input_hash = _hash({"history": blinded, "features": features})
            currency = "USDT" if quote_symbol else symbol.rsplit("_", 1)[-1]
            episodes.append({"episode_id": _hash([symbol, decision, horizon, currency, input_hash]),
                             "decision_time": decision, "label_end": label_end, "symbol": symbol,
                             "features": features, "history": blinded, "input_hash": input_hash,
                             "label": label, "realized_return": realized, "return_currency": currency,
                             "target": "next_%dh_down_flat_up" % (horizon // 3600), "cost_band": cost_band,
                             "provenance": {"historical_reconstruction": True, "source_hash": source_hash,
                                            "quote_hash": quote_hash, "quote_symbol": quote_symbol,
                                            "source_receipts": [receipts[symbol]] + ([receipts[quote_symbol]] if quote else []),
                                            "history_end": decision, "input_last_bar_open": decision - DAY,
                                            "no_forward_fill": True}})
    return sorted(episodes, key=lambda x: (x["decision_time"], x["symbol"]))
