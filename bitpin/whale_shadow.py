"""Isolated BTC wallet research. No decision engine, account client or broker."""
import json
from pathlib import Path
import sqlite3
import time

from .defi_daily import DailyLock, atomic_text
from .whale_public import (ADDRESSES, PROVIDERS, PROVENANCE, WALLETS, PublicDataError, WhaleClient,
                           address_stats, block_hash, classify_tx, integer, normalize_tx)

DAY = 86400
MAX_AGE = 9000
MARKER = ".whale-shadow-state"
MARKER_TEXT = "bitcoin-wallet-shadow-v1\n"


def prepare_directory(value):
    original = Path(value).absolute()
    for parent in (original,) + tuple(original.parents):
        if parent.is_symlink():
            raise ValueError("state symlinks are not allowed")
    directory = original.resolve()
    normalized = directory.as_posix().lower() + "/"
    blocked = ("/etc/bitpin-bot/", "/opt/bitpin-bot/", "/var/lib/bitpin-bot/", "/var/lib/bitpin-bot-paper/",
               "/var/lib/bitpin-flow-public/", "/var/lib/bitpin-defi-shadow/")
    if any(part in normalized for part in blocked):
        raise ValueError("choose an isolated whale directory")
    for parent in (directory,) + tuple(directory.parents):
        if any((parent / name).exists() for name in ("LIVE_CONFIRMED", "bitpin_token.json",
                                                    ".flow-shadow-state", ".defi-shadow-state")):
            raise ValueError("account or other collector state is not allowed")
    marker = directory / MARKER
    if directory.exists() and not marker.is_file() and any(directory.iterdir()):
        raise ValueError("choose an empty directory")
    directory.mkdir(parents=True, exist_ok=True)
    if marker.is_symlink():
        raise ValueError("invalid marker")
    if not marker.exists():
        marker.write_text(MARKER_TEXT, encoding="utf-8")
    if marker.read_text(encoding="utf-8") != MARKER_TEXT:
        raise ValueError("invalid marker")
    for name in ("whale.sqlite3", "whale.sqlite3-wal", "whale.sqlite3-shm", ".collector.lock",
                 "whale-shadow.json", "report.html"):
        if (directory / name).is_symlink():
            raise ValueError("state symlinks are not allowed")
    return directory


class WhaleStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, received REAL, record TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS observation_time ON observations(received)")
        self.db.execute("CREATE TABLE IF NOT EXISTS cooldown(provider TEXT PRIMARY KEY, until_time REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value INTEGER)")
        self.db.commit()

    def close(self):
        if self.db is not None:
            self.db.close(); self.db = None

    def latest(self, successful=False):
        rows = self.db.execute("SELECT record FROM observations ORDER BY id DESC")
        for row in rows:
            record = json.loads(row[0])
            if not successful or record["status"] in ("ok", "reorg"):
                return record
        return None

    def epoch(self, advance=False):
        row = self.db.execute("SELECT value FROM metadata WHERE key='epoch'").fetchone()
        value = row[0] if row else 0
        if advance:
            value += 1
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES('epoch',?)", (value,))
        return value

    def cooldown(self, provider, until=None):
        if until is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO cooldown VALUES(?,?)", (provider, until))
        row = self.db.execute("SELECT until_time FROM cooldown WHERE provider=?", (provider,)).fetchone()
        return row[0] if row else 0

    def save(self, record):
        with self.db:
            self.db.execute("INSERT INTO observations(received,record) VALUES(?,?)",
                            (record["received_at"], json.dumps(record, allow_nan=False)))
            self.db.execute("DELETE FROM observations WHERE received < ?", (record["received_at"] - 8 * DAY,))

    def reference(self, received, interval, epoch):
        target = received - interval
        rows = self.db.execute("SELECT record FROM observations WHERE received BETWEEN ? AND ? "
                               "ORDER BY abs(received-?) ASC, id DESC", (target - 3600, target + 3600, target))
        for row in rows:
            record = json.loads(row[0])
            if record["status"] == "ok" and record["epoch"] == epoch:
                return record
        return None

    def snapshot(self, now):
        record = self.latest() or {"status": "missing", "received_at": None, "sources": {}, "epoch": self.epoch()}
        received = record["received_at"]
        status = record["status"]
        if received is not None and not 0 <= now - received <= MAX_AGE:
            status = "stale"
        usable = status == "ok"
        references = {name: self.reference(received, interval, record["epoch"]) if usable else None
                      for name, interval in (("24h", DAY), ("7d", 7 * DAY))}
        wallets = []
        for wallet in WALLETS:
            row = dict(wallet)
            data = record.get("data", {}).get(wallet["address"], {}) if usable else {}
            row.update(balance_sats=data.get("balance_sats"), history_capped=data.get("history_capped"),
                       pending_tx_counts=data.get("pending_tx_counts"))
            for name, ref in references.items():
                previous = ref["data"][wallet["address"]]["balance_sats"] if ref else None
                row["change_" + name + "_sats"] = data["balance_sats"] - previous if previous is not None else None
                row["reference_" + name + "_elapsed_seconds"] = received - ref["received_at"] if ref else None
            wallets.append(row)
        return {"schema_version": 1, "mode": "shadow", "live_enabled": False, "orders_created": 0,
                "model_calls": 0, "generated_at": now, "received_at": received,
                "receipt_age_seconds": now - received if received is not None else None,
                "status": status, "sources": record["sources"], "provenance": dict(PROVENANCE),
                "request_count": record.get("request_count"), "chain_epoch": record["epoch"],
                "wallets": wallets, "transactions": record.get("transactions", []) if usable else [],
                "anchor": record.get("anchor") if usable else None,
                "excluded_recent_transactions": record.get("excluded_recent_transactions") if usable else None,
                "complete_daily_flow": False, "minimum_transaction_confirmations": 6,
                "large_movement_threshold_sats": 50000000000,
                "interpretation": "Historical self-published BTC custody-address sample. Current ownership unverified. "
                "Balances include transactions with at least one confirmation; six-confirmation sample filter is not "
                "absolute finality. Unlisted addresses may share the same owner. Balance changes include fees. "
                "Bounded transaction history is not complete daily flow or buying/selling pressure. "
                "No calibrated trading edge; no effect on live decisions."}


class WhaleCollector:
    def __init__(self, store, client=None, clock=time.time, sleeper=time.sleep):
        self.store, self.client = store, client or WhaleClient()
        self.clock, self.sleeper = clock, sleeper
        self.requests = 0
        self.last_provider = None

    def get(self, provider, path):
        if self.requests >= 64:
            raise PublicDataError("run request budget exhausted", 3600)
        if self.requests:
            self.sleeper(2)
        self.requests += 1
        self.last_provider = provider
        try:
            return self.client.get(provider, path)
        except PublicDataError as exc:
            delay = max(3600, min(86400, exc.retry_after))
            self.store.cooldown(provider, self.clock() + delay)
            raise

    def height(self, provider):
        value = self.get(provider, "/blocks/tip/height")
        if not isinstance(value, str) or not re_digits(value):
            raise ValueError("invalid chain height")
        height = integer(int(value), 10000000)
        if height < 6:
            raise ValueError("invalid chain height")
        return height

    def read_provider(self, provider):
        tip = self.height(provider)
        tip_hash = block_hash(self.get(provider, "/block-height/%d" % tip))
        start_anchor = block_hash(self.get(provider, "/block-height/%d" % (tip - 5)))
        header = self.get(provider, "/block/" + tip_hash)
        stamp = integer(header["timestamp"], 2 ** 40)
        if integer(header["height"], 10000000) != tip or not -7200 <= self.clock() - stamp <= 10800:
            raise ValueError("provider tip is stale or invalid")
        data, txs = {}, {}
        for address in sorted(ADDRESSES):
            root = "/address/" + address
            before = address_stats(self.get(provider, root), address)
            seen, last, capped = set(), None, False
            address_txs = {}
            previous_height = tip
            for page in range(2):
                path = root + "/txs/chain" + ("/" + last if last else "")
                rows = self.get(provider, path)
                if not isinstance(rows, list) or len(rows) > 25:
                    raise ValueError("invalid history page")
                for raw in rows:
                    tx = normalize_tx(raw)
                    if tx["txid"] in seen or tx["height"] > previous_height or tx["block_time"] > self.clock() + 7200:
                        raise ValueError("moving or invalid transaction history")
                    if not any(row["address"] == address for row in tx["inputs"] + tx["outputs"]):
                        raise ValueError("unrelated address history")
                    if tx["txid"] in txs and txs[tx["txid"]] != tx:
                        raise ValueError("conflicting shared transaction")
                    seen.add(tx["txid"]); address_txs[tx["txid"]] = tx; txs[tx["txid"]] = tx
                    previous_height = tx["height"]
                if len(rows) < 25:
                    break
                last = rows[-1]["txid"]
                capped = page == 1
            after = address_stats(self.get(provider, root), address)
            if before["chain_stats"] != after["chain_stats"]:
                raise ValueError("address changed during pagination")
            count = before["chain_stats"]["tx_count"]
            if count < len(seen) or (not capped and count != len(seen)):
                raise ValueError("incomplete confirmed history page")
            data[address] = {"balance_sats": before["balance_sats"], "chain_stats": before["chain_stats"],
                             "pending_tx_count": after["pending_tx_count"], "history_capped": capped,
                             "txids": sorted(address_txs)}
        return {"tip": tip, "tip_hash": tip_hash, "start_anchor": start_anchor, "data": data, "txs": txs}

    def collect(self, publish=None):
        self.requests = 0
        views, sources = {}, {}
        now = self.clock()
        # Persist and publish unknown before any request. Killed/aborted runs cannot revive a prior success.
        self.store.save({"status": "collecting", "received_at": now, "sources": {}, "epoch": self.store.epoch()})
        if publish is not None:
            publish(self.store.snapshot(now))
        for provider in PROVIDERS:
            until = self.store.cooldown(provider)
            if until > now:
                sources[provider] = {"status": "cooldown", "retry_at": until}
                continue
            try:
                views[provider] = self.read_provider(provider)
                sources[provider] = {"status": "ok", "tip_height": views[provider]["tip"]}
            except (PublicDataError, ValueError, KeyError, TypeError, AttributeError) as exc:
                delay = max(3600, min(86400, getattr(exc, "retry_after", 3600)))
                self.store.cooldown(provider, self.clock() + delay)
                sources[provider] = {"status": "error", "error_type": type(exc).__name__,
                                     "http_status": getattr(exc, "status", None), "retry_at": self.clock() + delay}
        record = {"status": "error", "received_at": self.clock(), "sources": sources, "epoch": self.store.epoch()}
        if len(views) == 2:
            try:
                primary, secondary = views["blockstream"], views["mempool"]
                anchor_height = min(primary["tip"], secondary["tip"]) - 5
                anchors = [block_hash(self.get(provider, "/block-height/%d" % anchor_height)) for provider in PROVIDERS]
                if abs(primary["tip"] - secondary["tip"]) > 2 or anchors[0] != anchors[1]:
                    raise Disagreement()
                for address in ADDRESSES:
                    left, right = primary["data"][address], secondary["data"][address]
                    if any(left[key] != right[key] for key in ("chain_stats", "txids", "history_capped")):
                        raise Disagreement()
                if primary["txs"] != secondary["txs"]:
                    raise Disagreement()
                previous = self.store.latest(successful=True)
                reorg = False
                if previous:
                    old = previous["anchor"]
                    old_hashes = [block_hash(self.get(provider, "/block-height/%d" % old["height"])) for provider in PROVIDERS]
                    if old_hashes[0] != old_hashes[1]:
                        raise Disagreement()
                    reorg = old_hashes[0] != old["hash"]
                # Re-read anchor and tip after collection: never stitch across a fork or lagging tip.
                for provider in PROVIDERS:
                    if not views[provider]["tip"] <= self.height(provider) <= views[provider]["tip"] + 2:
                        raise Disagreement()
                    if block_hash(self.get(provider, "/block-height/%d" % anchor_height)) != anchors[0]:
                        raise Disagreement()
                    if block_hash(self.get(provider, "/block-height/%d" % (views[provider]["tip"] - 5))) != views[provider]["start_anchor"]:
                        raise Disagreement()
                record.update(status="reorg" if reorg else "ok", anchor={"height": anchor_height, "hash": anchors[0]},
                              epoch=self.store.epoch(advance=reorg), data={}, transactions=[], excluded_recent_transactions=0)
                for address in ADDRESSES:
                    row = dict(primary["data"][address]); row.pop("pending_tx_count")
                    row["pending_tx_counts"] = {p: views[p]["data"][address]["pending_tx_count"] for p in PROVIDERS}
                    record["data"][address] = row
                for tx in sorted(primary["txs"].values(), key=lambda row: (row["height"], row["txid"]), reverse=True):
                    confirmations = min(primary["tip"], secondary["tip"]) - tx["height"] + 1
                    if confirmations < 6:
                        record["excluded_recent_transactions"] += 1
                        continue
                    event = {"txid": tx["txid"], "block_hash": tx["block_hash"], "block_time": tx["block_time"],
                             "height": tx["height"], "confirmations": confirmations}
                    event.update(classify_tx(tx))
                    record["transactions"].append(event)
            except Disagreement:
                record["status"] = "disagreement"
            except (PublicDataError, ValueError, KeyError, TypeError) as exc:
                record["status"] = "error"
                record["error_type"] = type(exc).__name__
                if self.last_provider:
                    until = max(self.store.cooldown(self.last_provider), self.clock() + 3600)
                    self.store.cooldown(self.last_provider, until)
                    sources[self.last_provider] = {"status": "error", "error_type": type(exc).__name__,
                                                   "http_status": getattr(exc, "status", None), "retry_at": until}
        record["received_at"] = self.clock()
        record["request_count"] = self.requests
        self.store.save(record)
        return self.store.snapshot(self.clock())


class Disagreement(Exception):
    pass


def re_digits(value):
    return value.isascii() and value.isdigit() and len(value) <= 8


def write_snapshot(snapshot, directory):
    atomic_text(directory, "whale-shadow.json", json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2))
