"""Causal technical indicators (pure Python).

Every function returns a list the same length as its input where element i depends only on
inputs[0..i]. Values that are not yet defined (warm-up) are None.
"""
import math


def sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs, n):
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2.0 / (n + 1)
    v = sum(xs[:n]) / n
    out[n - 1] = v
    for i in range(n, len(xs)):
        v = xs[i] * k + v * (1 - k)
        out[i] = v
    return out


def stdev(xs, n):
    out = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1: i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out


def rsi(xs, n=14):
    """Wilder RSI."""
    out = [None] * len(xs)
    if len(xs) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = xs[i] - xs[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(xs)):
        d = xs[i] - xs[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def true_range(highs, lows, closes):
    tr = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            pc = closes[i - 1]
            tr.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return tr


def atr(highs, lows, closes, n=14):
    """Wilder ATR."""
    tr = true_range(highs, lows, closes)
    out = [None] * len(tr)
    if len(tr) < n:
        return out
    v = sum(tr[:n]) / n
    out[n - 1] = v
    for i in range(n, len(tr)):
        v = (v * (n - 1) + tr[i]) / n
        out[i] = v
    return out


def rolling_max(xs, n):
    out = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        out[i] = max(xs[i - n + 1: i + 1])
    return out


def rolling_min(xs, n):
    out = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        out[i] = min(xs[i - n + 1: i + 1])
    return out


def pct_change(xs, n=1):
    out = [None] * len(xs)
    for i in range(n, len(xs)):
        if xs[i - n]:
            out[i] = xs[i] / xs[i - n] - 1
    return out


def bollinger(xs, n=20, k=2.0):
    mid = sma(xs, n)
    sd = stdev(xs, n)
    up = [None if m is None else m + k * s for m, s in zip(mid, sd)]
    lo = [None if m is None else m - k * s for m, s in zip(mid, sd)]
    return lo, mid, up


def macd(xs, fast=12, slow=26, signal=9):
    """(line, signal, histogram): EMA(fast) - EMA(slow) of xs, the EMA(signal) of that line and the
    difference of the two (the classic MACD 12 / 26 / 9)."""
    ef, es = ema(xs, fast), ema(xs, slow)
    line = [None if a is None or b is None else a - b for a, b in zip(ef, es)]
    sig = [None] * len(xs)
    first = next((i for i, v in enumerate(line) if v is not None), None)
    if first is not None:
        for i, v in enumerate(ema(line[first:], signal)):
            sig[first + i] = v
    hist = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def zscore(xs, n):
    mid = sma(xs, n)
    sd = stdev(xs, n)
    return [None if m is None or not s else (x - m) / s for x, m, s in zip(xs, mid, sd)]
