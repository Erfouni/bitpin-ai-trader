"""Offline preparation/scoring; fresh requests require the owner to add --owner-execute."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bitpin import kimi_experiment as experiment  # noqa: E402


def read_json(path):
    with open(path, "rb") as stream:
        raw = stream.read(128 * 1024 * 1024 + 1)
    if len(raw) > 128 * 1024 * 1024:
        raise experiment.ExperimentError("input JSON exceeds local size limit")
    value = json.loads(raw.decode("utf-8"))
    if isinstance(value, dict) and isinstance(value.get("episodes"), list):
        return value["episodes"]
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command")
    prepare = subcommands.add_parser("prepare", help="offline blinded paired registration; never reads credentials")
    prepare.add_argument("--episodes", required=True)
    prepare.add_argument("--state-dir", required=True)
    prepare.add_argument("--case-id", action="append", help="predeclare case IDs; selection never inspects outcomes")
    prepare.add_argument("--max-cases", type=int, default=16)
    prepare.add_argument("--stability-repeats", type=int, default=0, choices=(0, 1, 2))
    prepare.add_argument("--reasoning-effort", default="low", choices=("low", "high", "max"))
    execute = subcommands.add_parser("execute", help="owner alone authorizes fresh requests")
    execute.add_argument("--state-dir", required=True)
    execute.add_argument("--owner-execute", action="store_true")
    execute.add_argument("--credential-file", help="owner-only private one-line existing Kimi key; never pass the key as an argument")
    execute.add_argument("--use-kimi-proxy", action="store_true", help="owner explicitly permits existing KIMI_HTTPS_PROXY")
    execute.add_argument("--max-calls", type=int, help="optional bounded stop after this many registered pending calls")
    execute.add_argument("--fixture-responses", help="offline synthetic envelope list with exact request_hash and response")
    score = subcommands.add_parser("score", help="free input-bound rescoring; never reads credentials")
    score.add_argument("--state-dir", required=True)
    score.add_argument("--episodes", required=True)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error("choose prepare, execute, or score")
    try:
        if args.command == "prepare":
            manifest = experiment.prepare_experiment(read_json(args.episodes), args.state_dir,
                                                     case_ids=args.case_id, max_cases=args.max_cases,
                                                     stability_repeats=args.stability_repeats,
                                                     reasoning_effort=args.reasoning_effort)
            result = {k: manifest[k] for k in ("manifest_hash", "case_count", "fresh_status", "model",
                                              "reasoning_effort", "excluded_duplicate_inputs", "contamination")}
            result.update({"registered_calls": len(manifest["requests"]), "worst_case_usd": manifest["worst_case_micro_usd"] / 1e6,
                           "cap_usd": 10, "manifest_path": os.path.abspath(os.path.join(args.state_dir, "manifest.json"))})
        elif args.command == "score":
            result = experiment.score_experiment(args.state_dir, read_json(args.episodes))
        else:
            transport = None
            if args.fixture_responses:
                if args.owner_execute or args.credential_file or args.use_kimi_proxy:
                    raise experiment.ExperimentError("fixtures cannot read owner credentials or count as fresh Kimi")
                manifest = experiment.load_manifest(args.state_dir)
                fixtures = read_json(args.fixture_responses)
                if not isinstance(fixtures, list):
                    raise experiment.ExperimentError("fixture response envelope list required")
                by_hash = {}
                known = {r["request_hash"] for r in manifest["requests"]}
                for row in fixtures:
                    if not isinstance(row, dict) or set(row) != {"request_hash", "response"} or row["request_hash"] not in known or row["request_hash"] in by_hash:
                        raise experiment.ExperimentError("fixture request hash mismatch or duplicate")
                    by_hash[row["request_hash"]] = row["response"]
                if not known.issubset(by_hash):
                    raise experiment.ExperimentError("fixture must provide every registered request signature")
                def transport(payload):
                    signature = experiment._hash({"endpoint": experiment.ENDPOINT,
                                                  "prompt_version": experiment.PROMPT_VERSION, "payload": payload})
                    if signature not in by_hash:
                        raise experiment.ExperimentError("fixture request signature mismatch")
                    return by_hash[signature]
            result = experiment.execute_experiment(args.state_dir, args.owner_execute, args.credential_file,
                                                    transport, args.max_calls, args.use_kimi_proxy)
        print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except experiment.ExperimentError as error:
        print("Experiment stopped: " + str(error), file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        # Neither a server error body nor arbitrary local content is safe to print.
        print("Experiment stopped: invalid or unreadable local artifact.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
