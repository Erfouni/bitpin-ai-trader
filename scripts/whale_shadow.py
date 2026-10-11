"""Observe a bounded public Bitcoin custody-address sample; never trade."""
import argparse
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bitpin.whale_shadow import DailyLock, WhaleCollector, WhaleStore, prepare_directory, write_snapshot
from bitpin.whale_report import write_report


def main(argv=None, client=None, clock=time.time, sleeper=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="state/whale-shadow")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        directory = prepare_directory(args.state_dir)
        with DailyLock(directory):
            store = WhaleStore(directory / "whale.sqlite3")
            try:
                def publish(snapshot):
                    write_snapshot(snapshot, directory)
                    write_report(snapshot, directory)
                snapshot = store.snapshot(clock()) if args.report_only else \
                    WhaleCollector(store, client=client, clock=clock, sleeper=sleeper).collect(publish=publish)
                publish(snapshot)
                print("whale-shadow status=%s wallets=3 orders=0 model_calls=0 live_enabled=false" % snapshot["status"], flush=True)
                return 0 if snapshot["status"] == "ok" else 1
            finally:
                store.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("whale-shadow error: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
