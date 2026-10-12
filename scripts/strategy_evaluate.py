#!/usr/bin/env python3
"""Bounded offline evaluation of public price files and sanitized recorded Kimi."""
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bitpin.kimi_score import score_recorded_candidates
from bitpin.strategy_data import DAY, build_episodes, validate_bars
from bitpin.strategy_walkforward import evaluate_walkforward


def _read(path, manifest, kind, symbol=None):
    with open(path, "rb") as handle:
        raw = handle.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("input exceeds 4 MiB: " + path)
    digest = hashlib.sha256(raw).hexdigest()
    manifest.append({"path": os.path.abspath(path), "kind": kind, "symbol": symbol,
                     "sha256": digest, "bytes": len(raw)})
    return json.loads(raw.decode("utf-8-sig")), digest


def _mapping(values):
    result = {}
    for value in values:
        symbol, path = value.split("=", 1)
        if not symbol or symbol in result:
            raise ValueError("duplicate or empty input symbol")
        result[symbol] = path
    return result


def _panel(paths, receipt_paths, manifest, kind):
    result = {}
    for symbol, path in paths.items():
        bars, digest = _read(path, manifest, kind, symbol)
        receipt = {}
        if symbol in receipt_paths:
            receipt, _ = _read(receipt_paths[symbol], manifest, "receipt", symbol)
            if receipt.get("sha256") and receipt["sha256"] != digest:
                raise ValueError("receipt hash mismatch for " + symbol)
        receipt["sha256"] = digest
        receipt["historical_reconstruction"] = True
        result[symbol] = {"bars": bars, "receipt": receipt}
    return result


def _write(path, value):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    temporary = path + ".tmp-%d" % os.getpid()
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily", action="append", default=[], metavar="SYMBOL=FILE")
    parser.add_argument("--receipt", action="append", default=[], metavar="SYMBOL=FILE")
    parser.add_argument("--hourly", action="append", default=[], metavar="SYMBOL=FILE")
    parser.add_argument("--hourly-receipt", action="append", default=[], metavar="SYMBOL=FILE")
    parser.add_argument("--recorded", help="sanitized recorded decision JSON; never credentials")
    parser.add_argument("--currency", choices=("IRT", "USDT", "both"), default="both")
    parser.add_argument("--cutoff", type=float, help="frozen candle close cutoff; defaults earliest input receipt time")
    parser.add_argument("--output", required=True)
    parser.add_argument("--episodes-output", help="optional USDT (or IRT-only) episode file for offline Kimi preparation")
    parser.add_argument("--bootstrap-repeats", type=int, default=500)
    args = parser.parse_args(argv)
    manifest = []
    try:
        daily = _panel(_mapping(args.daily), _mapping(args.receipt), manifest, "daily")
        hourly = _panel(_mapping(args.hourly), _mapping(args.hourly_receipt), manifest, "hourly")
        receipts = [series["receipt"].get("received_at") for series in list(daily.values()) + list(hourly.values())]
        cutoff = args.cutoff if args.cutoff is not None else min([float(ts) for ts in receipts if ts is not None] or [time.time()])
        report = {"schema_version": 1, "generated_at": time.time(), "cutoff": cutoff,
                  "input_manifest": manifest, "quantitative": {}, "fresh_kimi_status": "not_run"}
        currencies = ("IRT", "USDT") if args.currency == "both" else (args.currency,)
        export = []
        for currency in currencies:
            selected = daily if currency == "USDT" else {s: v for s, v in daily.items() if s != "USDT_IRT"}
            episodes = build_episodes(selected, cutoff=cutoff, quote_symbol="USDT_IRT" if currency == "USDT" else None)
            evaluation = evaluate_walkforward(episodes, bootstrap_repeats=args.bootstrap_repeats)
            evaluation["data_coverage"] = {"episodes": len(episodes),
                                           "calendar_days": len({row["decision_time"] for row in episodes}),
                                           "symbols": sorted({row["symbol"] for row in episodes}),
                                           "first_decision": min((row["decision_time"] for row in episodes), default=None),
                                           "last_decision": max((row["decision_time"] for row in episodes), default=None),
                                           "validated_bars": {symbol: len(validate_bars(series["bars"], cutoff=cutoff))
                                                              for symbol, series in daily.items()}}
            if currency == "IRT" and "USDT_IRT" in daily and evaluation["holdout"]["episode_count"]:
                quote = {row["ts"]: row for row in validate_bars(daily["USDT_IRT"]["bars"], cutoff=cutoff)}
                start = evaluation["holdout_start"]
                end = max(row["label_end"] for row in episodes if row["decision_time"] >= start)
                consecutive = all(ts in quote for ts in range(start, end + DAY, DAY))
                gross = quote[end]["open"] / quote[start]["open"] - 1 if consecutive else None
                evaluation["holdout"]["USDT_buy_hold_benchmark"] = {
                    "currency": "IRT", "gross_return": gross,
                    "return": (1 + gross) * .996 ** 2 - 1 if gross is not None else None,
                    "cost_per_side": .004, "calendar_days": (end - start) // DAY,
                    "status": "evaluated" if consecutive else "missing_exact_quote_bars"}
            report["quantitative"][currency] = evaluation
            export = episodes
        if args.recorded:
            decisions, _ = _read(args.recorded, manifest, "recorded_kimi")
            report["recorded_kimi"] = score_recorded_candidates(decisions, hourly, cutoff=cutoff,
                                                               bootstrap_repeats=args.bootstrap_repeats)
        else:
            report["recorded_kimi"] = {"status": "not_provided"}
        code_digest = hashlib.sha256()
        for filename in ("strategy_data.py", "strategy_walkforward.py", "kimi_score.py", "evidence_statistics.py"):
            with open(os.path.join(ROOT, "bitpin", filename), "rb") as handle:
                code_digest.update(handle.read())
        with open(os.path.abspath(__file__), "rb") as handle:
            code_digest.update(handle.read())
        report["code_sha256"] = code_digest.hexdigest()
        _write(args.output, report)
        if args.episodes_output:
            _write(args.episodes_output, export)
        print(json.dumps({"output": os.path.abspath(args.output), "quantitative": {
            currency: {"status": evaluation["status"], "episodes": evaluation["data_coverage"]["episodes"],
                       "holdout_days": evaluation["holdout"]["calendar_days"]}
            for currency, evaluation in report["quantitative"].items()},
            "recorded_evaluated": report["recorded_kimi"].get("evaluated_candidates"), "fresh_kimi_status": "not_run"}))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as error:
        parser.exit(2, "evaluation input error: %s\n" % error)


if __name__ == "__main__":
    sys.exit(main())
