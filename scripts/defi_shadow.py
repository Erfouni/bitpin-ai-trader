"""Collect daily PUBLIC DefiLlama observations separately from the live-flow input."""
import argparse
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bitpin.defi_daily import CHAINS, DailyCollector, DailyLock, DailyStore, prepare_directory, source_specs, write_snapshot
from bitpin.defi_report import write_report


def main(argv=None, client=None, clock=time.time, sleeper=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="state/defi-daily-shadow")
    parser.add_argument("--chains", nargs="*", choices=CHAINS, default=list(CHAINS))
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        directory = prepare_directory(args.state_dir)
        specs = source_specs(args.chains)
        with DailyLock(directory):
            store = DailyStore(directory / "daily.sqlite3")
            try:
                snapshot = store.snapshot(specs, clock()) if args.report_only else \
                    DailyCollector(store, client=client, clock=clock, sleeper=sleeper).collect(specs)
                write_snapshot(snapshot, directory)
                write_report(snapshot, directory)
                problems = sum(s["status"] != "ok" for s in snapshot["sources"].values())
                print("defi-shadow sources=%d source_problems=%d orders=0 model_calls=0 live_enabled=false" %
                      (len(specs), problems), flush=True)
                return 1 if problems else 0
            finally:
                store.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("defi-shadow error: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
