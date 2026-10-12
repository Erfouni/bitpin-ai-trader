"""Archive fixed public snapshots without network, account or model access."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from bitpin.strategy_shadow import StrategyStore, prepare_directory, read_public_snapshots, archive_snapshots, DailyLock, atomic_text
from bitpin.strategy_report import write_report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',default='state/strategy-shadow')
    args=parser.parse_args(argv)
    try:
        directory=prepare_directory(args.state_dir)
        with DailyLock(directory):
            store=StrategyStore(directory/'archive.sqlite3')
            try:
                snapshot=archive_snapshots(store,read_public_snapshots(),time.time())
                atomic_text(directory,'strategy-shadow.json',json.dumps(snapshot,indent=2,allow_nan=False)+'\n')
                write_report(snapshot,directory)
                print('strategy-shadow status=ok receipts=%d orders=0 model_calls=0 live_enabled=false'%snapshot['receipt_count'])
            finally: store.close()
        return 0
    except (OSError,ValueError,sqlite3.Error):
        print('strategy-shadow failed; public archive unavailable',file=sys.stderr)
        return 1


if __name__=='__main__': sys.exit(main())
