"""Print the public-flow context fragment locally. Never call a model or account API."""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bitpin.flow_context import load_public_flow, snapshot_path_valid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--symbols", nargs="+", default=["BTC_IRT", "ETH_IRT", "SOL_IRT", "XRP_IRT", "USDT_IRT"])
    args = parser.parse_args(argv)
    if not snapshot_path_valid(args.snapshot):
        parser.error("snapshot must be an absolute path ending in snapshot.json")
    result = load_public_flow(args.snapshot, time.time(), args.symbols)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
