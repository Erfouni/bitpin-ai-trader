"""Read-only, opt-in daily public observations. No model, account or order calls."""
import json
import math
import os
from pathlib import Path
import stat

from .flow_shadow import MACRO_INTERVAL, MAX_GAP

MAX_BYTES = 512 * 1024
DAY = 86400
INTERPRETATION = (
    "public_flow is unvalidated supporting data, not an independent buy basis or a calibrated probability edge. "
    "Only fully covered 24h taker flow is shown; short windows are excluded. Do not increase p above the "
    "existing P RULE allowance, create a new setup/base-rate row, or trigger additional decisions from it. "
    "Kimi remains the allocator under all existing gates, costs and position rules. Cite the exact market, "
    "window and value when discussing supporting or conflicting evidence. BTC_USDT is a different market "
    "from BTC_IRT: never rename or convert its flow into IRT; never sum different quote currencies. "
    "Delta is executed taker buy value minus sell value, not exchange deposits/withdrawals or smart-money "
    "identity. Stablecoin supply changes are a global liquidity proxy, not proof of crypto buying. "
    "Provider publication time is unknown; receipt time does not prove source freshness. "
    "REST overlap cannot guarantee complete exchange event coverage. Missing, stale or warming data means "
    "no evidence, not zero pressure. A zero-trade window has unknown buy ratio. Existing IRT order books "
    "remain execution/liquidity context, not a separate short-term entry signal."
)


def snapshot_path_valid(value):
    return (isinstance(value, str) and "\x00" not in value and os.path.isabs(value)
            and Path(value).name == "snapshot.json")


def _read_regular(path, limit):
    if os.path.islink(str(path)):
        raise ValueError("symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(str(path), flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("not a bounded regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("oversized file")
    return data


def _number(value, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("non-numeric value")
    if not math.isfinite(value) or abs(value) > 1e30 or (nonnegative and value < 0):
        raise ValueError("invalid numeric value")
    return value


def _age(received, now, max_age):
    age = now - _number(received, True)
    if not 0 <= age <= max_age:
        raise ValueError("stale or future receipt")
    return age


def _daily(market, symbol, now, generated):
    source = market["trades"]
    if source["status"] != "ok":
        return {"status": "unavailable"}
    received = _number(source["received_at"], True)
    age = _age(received, now, MAX_GAP)
    if received > generated or market["quote_unit"] != symbol.rsplit("_", 1)[1]:
        raise ValueError("market or time mismatch")
    if source.get("notional_basis") != "price_times_base":
        raise ValueError("unknown notional basis")
    day = market["windows"][str(DAY)]
    if day["quality"] != "ready":
        return {"status": "warming_up" if day["quality"] == "warming_up" else "unavailable"}
    if (day["window_seconds"] != DAY or day["as_of"] != received
            or _number(day["coverage_seconds"], True) != DAY
            or received - _number(source["continuous_since"], True) < DAY):
        raise ValueError("incomplete coverage")
    buy, sell = _number(day["buy_quote"], True), _number(day["sell_quote"], True)
    count = day["trade_count"]
    if type(count) is not int or not 0 <= count <= 10 ** 9 or ((buy + sell == 0) != (count == 0)):
        raise ValueError("invalid trade count")
    delta, ratio = buy - sell, buy / (buy + sell) if buy + sell else None
    if not math.isclose(_number(day["delta_quote"]), delta, rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError("delta mismatch")
    if ratio is None:
        if day.get("buy_ratio") is not None:
            raise ValueError("zero-volume ratio")
    elif not math.isclose(_number(day["buy_ratio"]), ratio, rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError("ratio mismatch")
    return {"status": "ready", "quote_unit": market["quote_unit"], "as_of": received,
            "receipt_age_seconds": round(age, 1), "daily": {
                "window_seconds": DAY, "buy_quote": buy, "sell_quote": sell, "delta_quote": delta,
                "buy_ratio": ratio, "trade_count": count}}


def _liquidity(record, now, generated):
    if record["status"] != "ok":
        raise ValueError("source unavailable")
    received = _number(record["received_at"], True)
    age = _age(received, now, 2 * MACRO_INTERVAL)
    data = record["data"]
    if received > generated or data["kind"] != "stablecoin_supply_proxy":
        raise ValueError("unknown macro data")
    assets = {}
    for symbol in ("USDT", "USDC"):
        raw = data["assets"][symbol]
        current = _number(raw["circulating_units"], True)
        if not current:
            raise ValueError("missing supply")
        row = {"circulating_units": current}
        for period in ("1d", "7d", "30d"):
            key = "change_" + period + "_units"
            value = raw.get(key)
            if value is not None:
                value = _number(value)
                if current - value < 0:
                    raise ValueError("negative previous supply")
            row[key] = value
        assets[symbol] = row
    return {"status": "ok", "kind": "stablecoin_supply_proxy", "assets": assets,
            "received_at": received, "receipt_age_seconds": round(age, 1), "provider_time": None}


def _unique(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _invalid_constant(value):
    raise ValueError("nonfinite JSON")


def load_public_flow(path, now, symbols):
    """Return only validated numeric facts, or a fixed unavailable status; never fetch or trade."""
    unavailable = {"status": "unavailable", "interpretation": INTERPRETATION}
    try:
        if not snapshot_path_valid(path):
            return unavailable
        target = Path(path)
        if _read_regular(target.parent / ".flow-shadow-state", 64) not in (
                b"public-flow-shadow-v1\n", b"public-flow-shadow-v1\r\n"):
            return unavailable
        raw = json.loads(_read_regular(target, MAX_BYTES).decode("utf-8"),
                         parse_constant=_invalid_constant, object_pairs_hook=_unique)
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or raw["mode"] != "shadow":
            return unavailable
        generated = _number(raw["generated_at"], True)
        _age(generated, now, MAX_GAP)
        allowed = set(symbols)
        allowed.update(s.rsplit("_", 1)[0] + "_USDT" for s in symbols if s.endswith("_IRT") and s != "USDT_IRT")
        markets = {}
        for symbol in sorted(allowed.intersection(raw["markets"]))[:10]:
            try:
                markets[symbol] = _daily(raw["markets"][symbol], symbol, now, generated)
            except (ValueError, TypeError, KeyError, OverflowError):
                markets[symbol] = {"status": "unavailable"}
        try:
            liquidity = _liquidity(raw["liquidity"], now, generated)
        except (ValueError, TypeError, KeyError, OverflowError):
            liquidity = {"status": "unavailable"}
        return {"status": "ok", "collection_mode": "shadow", "generated_at": generated,
                "interpretation": INTERPRETATION, "markets": markets, "liquidity": liquidity}
    except (ValueError, TypeError, KeyError, AttributeError, OSError, OverflowError, RecursionError):
        return unavailable
