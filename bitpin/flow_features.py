"""Deterministic observations, never trade instructions (stdlib, Python 3.7+)."""
import re
from decimal import Decimal, InvalidOperation

SYMBOL = re.compile(r"^[A-Z0-9]{2,16}_(IRT|USDT)$")
WINDOWS = (300, 900, 3600, 14400, 86400)


def symbol_checked(symbol):
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("invalid public market symbol")
    return symbol


def number(value, zero=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid number")
    if len(str(value)) > 80:
        raise ValueError("number too long")
    try:
        value = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid decimal")
    if not value.is_finite() or value < 0 or (not zero and value == 0) or value > Decimal("1e30"):
        raise ValueError("number outside supported range")
    return value


def canonical(value):
    text = format(number(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def normalize_trades(payload, now):
    if not isinstance(payload, list) or len(payload) > 2000:
        raise ValueError("invalid matches payload")
    rows = {}
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("invalid match")
        tid = raw.get("id")
        if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", tid):
            raise ValueError("invalid trade ID")
        if raw.get("side") not in ("buy", "sell"):
            raise ValueError("unknown taker side")
        ts = float(number(raw.get("time")))
        if ts > now + 5:
            raise ValueError("future trade timestamp")
        price, base = number(raw.get("price")), number(raw.get("base_amount"))
        number(raw.get("quote_amount"), zero=True)  # validate, but this field is rounded by Bitpin
        row = {"id": tid, "side": raw["side"], "time": ts,
               "price": canonical(price), "base": canonical(base), "quote": canonical(price * base)}
        if tid in rows and rows[tid] != row:
            raise ValueError("conflicting duplicate trade")
        rows[tid] = row
    return sorted(rows.values(), key=lambda x: (x["time"], x["id"]))


def book_features(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid order book")
    def side(key, reverse):
        raw = payload.get(key)
        if not isinstance(raw, list) or not raw or len(raw) > 2000:
            raise ValueError("empty or invalid book side")
        rows = []
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError("invalid price level")
            rows.append((number(row[0]), number(row[1])))
        if len(set(p for p, _ in rows)) != len(rows):
            raise ValueError("duplicate price levels")
        return sorted(rows, reverse=reverse)
    bids, asks = side("bids", True), side("asks", False)
    bid, ask = bids[0][0], asks[0][0]
    if bid >= ask:
        raise ValueError("locked or crossed order book")
    mid = (bid + ask) / 2
    bands = {}
    for pct in ("0.1", "0.5", "1"):
        low, high = mid * (1 - Decimal(pct) / 100), mid * (1 + Decimal(pct) / 100)
        b = sum((p * q for p, q in bids if p >= low), Decimal(0))
        a = sum((p * q for p, q in asks if p <= high), Decimal(0))
        bands[pct] = {"bid_quote": float(b), "ask_quote": float(a),
                      "imbalance": float((b - a) / (b + a)) if b + a else None,
                      "complete": bids[-1][0] <= low and asks[-1][0] >= high}
    return {"mid": float(mid), "spread_pct": float((ask - bid) / mid * 100),
            "bid_levels": len(bids), "ask_levels": len(asks), "bands": bands,
            "kind": "sampled_visible_depth", "provider_time": None}


def liquidity_features(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("peggedAssets"), list):
        raise ValueError("invalid stablecoin payload")
    assets = {}
    for ident, symbol in (("1", "USDT"), ("2", "USDC")):
        found = [x for x in payload["peggedAssets"] if isinstance(x, dict) and x.get("id") == ident]
        if len(found) != 1 or found[0].get("symbol") != symbol or found[0].get("pegType") != "peggedUSD":
            raise ValueError("missing or mismatched stablecoin identity")
        row = found[0]
        current = number((row.get("circulating") or {}).get("peggedUSD"), zero=True)
        rec = {"circulating_units": float(current)}
        for period, key in (("1d", "circulatingPrevDay"), ("7d", "circulatingPrevWeek"),
                            ("30d", "circulatingPrevMonth")):
            previous = (row.get(key) or {}).get("peggedUSD")
            previous = number(previous, zero=True) if previous is not None else None
            rec["change_" + period + "_units"] = float(current - previous) if previous is not None else None
            rec["change_" + period + "_pct"] = float((current / previous - 1) * 100) if previous else None
        assets[symbol] = rec
    result = {"assets": assets, "kind": "stablecoin_supply_proxy", "unit": "USD-pegged token units",
              "provider_time": None, "provider_time_status": "not_supplied"}
    for period in ("1d", "7d", "30d"):
        values = [x["change_" + period + "_units"] for x in assets.values()]
        result["combined_change_" + period + "_units"] = sum(values) if all(v is not None for v in values) else None
    return result
