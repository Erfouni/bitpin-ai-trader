"""Observe a small publicly attributed ETH wallet cohort, with no trading access."""
import argparse
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bitpin.personal_whale import DailyLock, PersonalCollector, PersonalStore, prepare_directory, write_snapshot
from bitpin.personal_whale_report import write_report


def main(argv=None, client=None, clock=time.time, sleeper=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default='state/personal-whale-shadow')
    parser.add_argument('--report-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        directory = prepare_directory(args.state_dir)
        with DailyLock(directory):
            store = PersonalStore(directory / 'personal.sqlite3')
            try:
                def publish(snapshot):
                    write_snapshot(snapshot, directory)
                    write_report(snapshot, directory)
                snapshot = store.snapshot(clock()) if args.report_only else \
                    PersonalCollector(store, client=client, clock=clock, sleeper=sleeper).collect(publish)
                publish(snapshot)
                print('personal-whale-shadow status=%s wallets=%d orders=0 model_calls=0 live_enabled=false' %
                      (snapshot['status'],len(snapshot.get('wallets') or [{}])), flush=True)
                return 0 if snapshot['status'] == 'ok' else 1
            finally:
                store.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print('personal-whale-shadow error: ' + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__': sys.exit(main())
