"""Collect PUBLIC data in an isolated shadow directory. This CLI cannot trade."""
import argparse
import math
import os
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bitpin.flow_features import symbol_checked
from bitpin.flow_report import write_reports
from bitpin.flow_shadow import ShadowCollector, ShadowStore

DEFAULT_SYMBOLS = ("BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "USDT_IRT")
MARKER = ".flow-shadow-state"


def prepare_directory(path):
    path = Path(path).resolve()
    normalized = path.as_posix().lower() + "/"
    forbidden = ("/var/lib/bitpin-bot/", "/var/lib/bitpin-bot-paper/", "/etc/bitpin-bot/",
                 "/opt/bitpin-bot/", "/state/live/", "/state/paper/")
    if any(value in normalized for value in forbidden):
        raise ValueError("the live/paper bot directories cannot hold shadow state")
    marker = path / MARKER
    if path.exists() and not marker.is_file() and any(path.iterdir()):
        raise ValueError("choose an empty, separate shadow directory")
    for parent in (path,) + tuple(path.parents):
        if any((parent / name).exists() for name in ("LIVE_CONFIRMED", "bitpin_token.json")):
            raise ValueError("shadow state must not be inside account state")
    path.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text("public-flow-shadow-v1\n", encoding="utf-8")
    if marker.is_symlink() or marker.read_text(encoding="utf-8") != "public-flow-shadow-v1\n":
        raise ValueError("invalid shadow directory marker")
    for name in ("flows.sqlite3", "flows.sqlite3-wal", "flows.sqlite3-shm", ".collector.lock"):
        if (path / name).is_symlink():
            raise ValueError("state symlinks are not allowed")
    return path


class StateLock:
    def __init__(self, directory):
        self.path = Path(directory) / ".collector.lock"
        self.file = None

    def __enter__(self):
        self.file = open(str(self.path), "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.file.seek(0, 2) == 0:
                    self.file.write(b"0"); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError("another collector is using this shadow directory")
        return self

    def __exit__(self, *args):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


def main(argv=None, client=None, clock=time.time, sleeper=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="state/flow-shadow")
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cycles", type=int, default=1)
    mode.add_argument("--continuous", action="store_true")
    mode.add_argument("--report-only", action="store_true")
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or not 30 <= args.interval <= 3600:
        parser.error("interval must be 30..3600 seconds")
    if not 1 <= args.cycles <= 10000:
        parser.error("cycles must be 1..10000")
    symbols = list(dict.fromkeys(args.symbols))
    try:
        if not 1 <= len(symbols) <= 10:
            raise ValueError("choose 1..10 distinct markets")
        for symbol in symbols:
            symbol_checked(symbol)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        directory = prepare_directory(args.state_dir)
        with StateLock(directory):
            store = ShadowStore(directory / "flows.sqlite3")
            try:
                if args.report_only:
                    write_reports(store.snapshot(symbols, clock()), directory)
                    return 0
                collector = ShadowCollector(store, client=client, clock=clock)
                cycle = 0
                while True:
                    started = clock()
                    snapshot = collector.collect(symbols)
                    write_reports(snapshot, directory)
                    cycle += 1
                    problems = sum(m[source]["status"] != "ok" for m in snapshot["markets"].values()
                                   for source in ("trades", "book"))
                    problems += snapshot["liquidity"]["status"] != "ok"
                    print("shadow cycle=%d markets=%d source_problems=%d orders=0 model_calls=0" %
                          (cycle, len(symbols), problems), flush=True)
                    if not args.continuous and cycle >= args.cycles:
                        return 1 if problems else 0
                    sleeper(max(0, args.interval - (clock() - started)))
            finally:
                store.close()
    except (ValueError, OSError) as exc:
        print("shadow error: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    sys.exit(main())
