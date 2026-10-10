"""Persistent experimental market observations, entirely separate from the trader."""
import json
import os
import sqlite3
import time
from decimal import Decimal

from .flow_features import WINDOWS, book_features, liquidity_features, normalize_trades, symbol_checked
from .flow_public import PublicDataError, PublicFlowClient

MAX_GAP = 90
MACRO_INTERVAL = 3600
RETENTION = 8 * 86400


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class ShadowStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
              symbol TEXT NOT NULL, id TEXT NOT NULL, event REAL NOT NULL, received REAL NOT NULL,
              side TEXT NOT NULL, price TEXT NOT NULL, base TEXT NOT NULL, quote TEXT NOT NULL,
              PRIMARY KEY(symbol,id));
            CREATE INDEX IF NOT EXISTS trades_event ON trades(symbol,event);
            CREATE TABLE IF NOT EXISTS sources (
              source TEXT NOT NULL, symbol TEXT NOT NULL, record TEXT NOT NULL,
              PRIMARY KEY(source,symbol));
            CREATE TABLE IF NOT EXISTS observations (
              source TEXT NOT NULL, symbol TEXT NOT NULL, received REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS observations_time ON observations(received);
        """)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def state(self, source, symbol):
        row = self.db.execute("SELECT record FROM sources WHERE source=? AND symbol=?", (source, symbol)).fetchone()
        return json.loads(row[0]) if row else {}

    def _state(self, source, symbol, value):
        self.db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?)", (source, symbol, encode(value)))

    def _append_observation(self, source, symbol, now, data):
        self.db.execute("INSERT INTO observations VALUES(?,?,?,?)", (source, symbol, now, encode(data)))

    def _success(self, source, symbol, now, payload, **extra):
        old = self.state(source, symbol)
        rec = dict(old, received_at=now, attempted_at=now, status="ok", error=None, retry_at=0,
                   success_count=old.get("success_count", 0) + 1, data=payload)
        rec.update(extra)
        self._state(source, symbol, rec)
        return rec

    def fail(self, source, symbol, now, error, retry_after=60):
        old = self.state(source, symbol)
        rec = dict(old, attempted_at=now, status="error", error=str(error)[:180],
                   failure_count=old.get("failure_count", 0) + 1, retry_at=now + max(30, retry_after))
        if source == "trades":
            rec["continuous_since"] = None
        with self.db:
            self._append_observation(source, symbol, now, {
                "event": "source_error", "error": rec["error"], "retry_at": rec["retry_at"],
                "previous_received_at": old.get("received_at"),
                "previous_continuous_since": old.get("continuous_since")})
            self._state(source, symbol, rec)

    def ingest_trades(self, symbol, payload, now):
        symbol_checked(symbol)
        rows = normalize_trades(payload, now)
        old = self.state("trades", symbol)
        if not rows:
            self.fail("trades", symbol, now, "empty recent-trades buffer")
            return
        # Check the whole batch before writing: an inconsistent ID cannot partially commit.
        for row in rows:
            saved = self.db.execute("SELECT event,side,price,base,quote FROM trades WHERE symbol=? AND id=?",
                                    (symbol, row["id"])).fetchone()
            if saved and tuple(saved) != (row["time"], row["side"], row["price"], row["base"], row["quote"]):
                raise ValueError("existing trade ID changed")
        ids = set(row["id"] for row in rows)
        elapsed = now - old.get("received_at", now)
        continuous = old.get("continuous_since")
        reasons = []
        if not old:
            reasons.append("cold_start")
        else:
            if old.get("status") != "ok":
                reasons.append("source_recovery")
            if elapsed > MAX_GAP:
                reasons.append("poll_gap")
            if elapsed < 0:
                reasons.append("clock_rollback")
            if old.get("anchor") is not None and old["anchor"] not in ids:
                reasons.append("buffer_no_overlap")
        gap = bool(old) and bool(reasons)
        if continuous is None or gap:
            continuous = now
        with self.db:
            if reasons:
                self._append_observation("trades", symbol, now, {
                    "event": "coverage_reset", "reasons": reasons, "continuous_since": continuous,
                    "previous_received_at": old.get("received_at"),
                    "previous_continuous_since": old.get("continuous_since"),
                    "previous_anchor": old.get("anchor"), "anchor": rows[-1]["id"],
                    "buffer_size": len(rows)})
            for row in rows:
                self.db.execute("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?)",
                                (symbol, row["id"], row["time"], now, row["side"],
                                 row["price"], row["base"], row["quote"]))
            self._success("trades", symbol, now, {"buffer_size": len(rows), "latest_event": rows[-1]["time"]},
                          continuous_since=continuous, anchor=rows[-1]["id"],
                          gap_count=old.get("gap_count", 0) + int(gap),
                          notional_basis="price_times_base",
                          coverage_kind="REST buffer overlap; not an event-sequenced feed")

    def _observation(self, source, symbol, data, now):
        with self.db:
            self._append_observation(source, symbol, now, data)
            self._success(source, symbol, now, data)

    def ingest_book(self, symbol, payload, now):
        self._observation("book", symbol_checked(symbol), book_features(payload), now)

    def ingest_liquidity(self, payload, now):
        self._observation("liquidity", "", liquidity_features(payload), now)

    def _view(self, source, symbol, now, max_age):
        rec = dict(self.state(source, symbol))
        if not rec:
            return {"status": "missing", "data": None, "receipt_age_seconds": None}
        age = now - rec["received_at"] if "received_at" in rec else None
        rec["receipt_age_seconds"] = age
        if rec.get("status") == "ok" and (age is None or age < 0 or age > max_age):
            rec["status"] = "stale"
        return rec

    def snapshot(self, symbols, now):
        markets = {}
        for symbol in symbols:
            symbol_checked(symbol)
            trades = self._view("trades", symbol, now, MAX_GAP)
            book = self._view("book", symbol, now, MAX_GAP)
            end = trades.get("received_at", now)
            since = trades.get("continuous_since")
            windows = {}
            for seconds in WINDOWS:
                quality = trades["status"]
                if "received_at" not in trades:
                    quality = "missing"
                elif quality == "ok":
                    quality = "ready" if since is not None and end - since >= seconds else "warming_up"
                rec = {"quality": quality, "as_of": end, "window_seconds": seconds,
                       "coverage_seconds": min(seconds, max(0, end - since)) if since is not None else 0,
                       "buy_quote": None, "sell_quote": None, "delta_quote": None,
                       "buy_ratio": None, "pressure_ratio": None, "trade_count": None}
                if quality == "ready":
                    rows = self.db.execute("SELECT side,quote FROM trades WHERE symbol=? AND event>? AND event<=?"
                                           " AND received<=?", (symbol, end - seconds, end, now)).fetchall()
                    buy = sum((Decimal(r["quote"]) for r in rows if r["side"] == "buy"), Decimal(0))
                    sell = sum((Decimal(r["quote"]) for r in rows if r["side"] == "sell"), Decimal(0))
                    total = buy + sell
                    rec.update(buy_quote=float(buy), sell_quote=float(sell), delta_quote=float(buy - sell),
                               buy_ratio=float(buy / total) if total else None,
                               pressure_ratio=float((buy - sell) / total) if total else None,
                               trade_count=len(rows))
                windows[str(seconds)] = rec
            count = self.db.execute("SELECT count(*) FROM trades WHERE symbol=?", (symbol,)).fetchone()[0]
            markets[symbol] = {"quote_unit": symbol.rsplit("_", 1)[1], "trades": trades, "book": book,
                               "windows": windows, "total_unique_trades": count}
        return {"schema_version": 1, "mode": "shadow", "live_enabled": False, "generated_at": now,
                "orders_created": 0, "model_calls": 0,
                "markets": markets, "liquidity": self._view("liquidity", "", now, 2 * MACRO_INTERVAL),
                "limitations": ["REST sampling can miss trades; coverage requires repeated buffer overlap.",
                                "Depth is observed rows, not the full order book.",
                                "Stablecoin supply is a liquidity proxy, not actual exchange cash inflow.",
                                "No profitability or allocation conclusion is made by this collector."]}

    def prune(self, now):
        with self.db:
            self.db.execute("DELETE FROM trades WHERE event<?", (now - RETENTION,))
            self.db.execute("DELETE FROM observations WHERE received<?", (now - RETENTION,))


class ShadowCollector:
    def __init__(self, store, client=None, clock=time.time):
        self.store = store
        self.client = client or PublicFlowClient()
        self.clock = clock

    def _fetch(self, source, symbol, get, ingest):
        now = self.clock()
        state = self.store.state(source, symbol)
        if now < state.get("retry_at", 0):
            return
        venue = "defillama" if source == "liquidity" else "bitpin"
        if now < self.store.state("transport", venue).get("retry_at", 0):
            return
        try:
            payload = get()
            ingest(payload, self.clock())
        except (ValueError, TypeError, KeyError, AttributeError, PublicDataError, OSError) as exc:
            delay = getattr(exc, "retry_after", 0) or 60
            self.store.fail(source, symbol, self.clock(), exc, delay)
            if getattr(exc, "status", None) == 429:
                self.store.fail("transport", venue, self.clock(), "HTTP 429 backoff", delay)

    def collect(self, symbols):
        for symbol in symbols:
            symbol_checked(symbol)
            self._fetch("trades", symbol, lambda: self.client.matches(symbol),
                        lambda data, now: self.store.ingest_trades(symbol, data, now))
            self._fetch("book", symbol, lambda: self.client.orderbook(symbol),
                        lambda data, now: self.store.ingest_book(symbol, data, now))
        last = self.store.state("liquidity", "")
        if self.clock() - last.get("received_at", -MACRO_INTERVAL) >= MACRO_INTERVAL:
            self._fetch("liquidity", "", self.client.stablecoins, self.store.ingest_liquidity)
        self.store.prune(self.clock())
        return self.store.snapshot(symbols, self.clock())
