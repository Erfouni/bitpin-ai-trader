"""Isolated daily DefiLlama observations. Never imports a model, broker or account client."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit

from .flow_public import PublicDataError, PublicFlowClient

DAY = 86400
INTERVAL = 3600
RECEIPT_MAX_AGE = 9000
CHAINS = ("Ethereum", "Solana", "BSC", "Arbitrum", "Sui", "Tron")
METRICS = ("tvl", "dex_volume", "stablecoin_supply", "fees", "revenue", "open_interest")
FLOWS = ("dex_volume", "fees", "revenue")
QUERY = "?excludeTotalDataChart=false&excludeTotalDataChartBreakdown=true"
MARKER = ".defi-shadow-state"
MARKER_TEXT = "defillama-daily-shadow-v1\n"


def source_specs(chains=CHAINS):
    chains = list(dict.fromkeys(chains))
    if any(c not in CHAINS for c in chains):
        raise ValueError("unsupported chain")
    specs = []
    for scope in ["global"] + chains:
        for metric in METRICS if scope == "global" else METRICS[:3]:
            suffix = "" if scope == "global" else "/" + scope
            if metric == "tvl":
                url = "https://api.llama.fi/v2/historicalChainTvl" + suffix
            elif metric == "stablecoin_supply":
                url = "https://stablecoins.llama.fi/stablecoincharts/" + ("all" if scope == "global" else scope)
            elif metric == "dex_volume":
                url = "https://api.llama.fi/overview/dexs" + suffix + QUERY + "&dataType=dailyVolume"
            elif metric == "open_interest":
                url = "https://api.llama.fi/overview/open-interest" + QUERY
            else:
                url = "https://api.llama.fi/overview/fees" + QUERY + "&dataType=" + (
                    "dailyFees" if metric == "fees" else "dailyRevenue")
            specs.append({"key": metric + ":" + scope, "metric": metric, "scope": scope, "url": url})
    return specs


class DefiClient(PublicFlowClient):
    """Fixed free HTTPS GET allowlist, no environment proxies, redirects or API keys."""
    _urls = frozenset(s["url"] for s in source_specs())

    def _allowed_url(self, url):
        return url in self._urls

    def get_json(self, url):
        try:
            payload = super().get_json(url)
        except RecursionError:
            raise PublicDataError("public JSON nesting exceeds parser limit", 60)
        stack = [(payload, 0)]
        while stack:
            value, depth = stack.pop()
            if depth > 64:
                raise PublicDataError("public JSON nesting exceeds limit", 60)
            children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
            stack.extend((child, depth + 1) for child in children if isinstance(child, (list, dict)))
        return payload


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    if not math.isfinite(value) or abs(value) > 1e20:
        raise ValueError("invalid magnitude")
    return value


def _timestamp(value):
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    value = _number(value)
    if value != int(value) or value < 0:
        raise ValueError("invalid date")
    return int(value)


def _pct(current, previous):
    if previous is None or previous <= 0:
        return None
    result = (current - previous) / previous * 100
    return result if math.isfinite(result) else None


def daily_features(payload, metric, now):
    """Closed UTC buckets only. Stock levels are never added like traded volumes.

    Provider history can be revised and aggregate coverage can change. A closed date is
    not a guarantee that every constituent protocol has reported or that values are final.
    """
    if metric not in METRICS:
        raise ValueError("unknown metric")
    today = int(_number(now) // DAY) * DAY
    rows = payload if metric in ("tvl", "stablecoin_supply") else payload["totalDataChart"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20000:
        raise ValueError("missing or oversized daily history")
    points = {}
    open_days = 0
    for row in rows:
        if metric == "tvl":
            stamp, value = row["date"], row["tvl"]
        elif metric == "stablecoin_supply":
            stamp = row["date"]
            # Only USD-pegged circulating units; never mix EUR/JPY units or market prices.
            value = row["totalCirculating"]["peggedUSD"]
        else:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError("invalid chart row")
            stamp, value = row
        stamp = _timestamp(stamp)
        if stamp < today - 92 * DAY:
            continue
        if stamp % DAY or stamp > today:
            raise ValueError("unaligned or future UTC bucket")
        value = _number(value)
        if value < 0 and metric not in ("fees", "revenue"):
            raise ValueError("negative stock or volume")
        if stamp in points:
            raise ValueError("duplicate daily bucket")
        points[stamp] = value
    if today in points:
        del points[today]
        open_days = 1
    if not points:
        raise ValueError("no closed UTC days")
    end = max(points)
    value = points[end]
    features = {"quality": "ok" if now - end <= 3 * DAY else "stale", "as_of": end,
                "date_utc": datetime.fromtimestamp(end, timezone.utc).strftime("%Y-%m-%d"),
                "kind": "daily_flow" if metric in FLOWS else "stock_snapshot",
                "unit": "USD-pegged token units" if metric == "stablecoin_supply" else "USD",
                "value": value, "is_net_inflow": False, "excluded_open_day_points": open_days,
                "provider_publication_time": None, "provider_finality": "unknown",
                "series": [[t, points[t]] for t in sorted(points)],
                "coverage_7d": sum(end - i * DAY in points for i in range(7)),
                "coverage_30d": sum(end - i * DAY in points for i in range(30))}
    for days in (1, 7, 30):
        features["change_%dd_pct" % days] = _pct(value, points.get(end - days * DAY))
    if metric in FLOWS:
        def window_sum(count, offset=0):
            values = [points.get(end - i * DAY) for i in range(offset, offset + count)]
            return sum(values) if all(v is not None for v in values) else None
        week, previous, month = window_sum(7), window_sum(7, 7), window_sum(30)
        features.update(sum_7d=week, mean_7d=week / 7 if week is not None else None,
                        previous_mean_7d=previous / 7 if previous is not None else None,
                        week_over_week_pct=_pct(week, previous) if week is not None else None,
                        sum_30d=month, mean_30d=month / 30 if month is not None else None)
    return features


def prepare_directory(path):
    original = Path(path).absolute()
    for parent in (original,) + tuple(original.parents):
        if parent.is_symlink():
            raise ValueError("state symlinks are not allowed")
    path = original.resolve()
    normalized = path.as_posix().lower() + "/"
    if any(v in normalized for v in ("/etc/bitpin-bot/", "/opt/bitpin-bot/", "/var/lib/bitpin-bot/",
                                     "/var/lib/bitpin-bot-paper/", "/var/lib/bitpin-flow-public/")):
        raise ValueError("choose a separate DefiLlama shadow directory")
    for parent in (path,) + tuple(path.parents):
        if any((parent / name).exists() for name in ("LIVE_CONFIRMED", "bitpin_token.json", ".flow-shadow-state")):
            raise ValueError("account or existing flow directory is not allowed")
    marker = path / MARKER
    if path.exists() and not marker.is_file() and any(path.iterdir()):
        raise ValueError("choose an empty directory")
    path.mkdir(parents=True, exist_ok=True)
    if marker.is_symlink():
        raise ValueError("invalid marker")
    if not marker.exists():
        marker.write_text(MARKER_TEXT, encoding="utf-8")
    if marker.read_text(encoding="utf-8") != MARKER_TEXT:
        raise ValueError("invalid marker")
    for name in ("daily.sqlite3", "daily.sqlite3-wal", "daily.sqlite3-shm", ".collector.lock",
                 "defi-daily.json", "report.html"):
        if (path / name).is_symlink():
            raise ValueError("state symlinks are not allowed")
    return path


class DailyLock:
    def __init__(self, directory):
        self.path = Path(directory) / ".collector.lock"
        self.file = None

    def __enter__(self):
        if self.path.is_symlink():
            raise ValueError("lock symlinks are not allowed")
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
            raise ValueError("another daily collector is using this directory")
        return self

    def __exit__(self, *args):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


class DailyStore:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, source TEXT, "
                        "received REAL, record TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS source_time ON observations(source,id)")
        self.db.execute("CREATE TABLE IF NOT EXISTS cooldown (host TEXT PRIMARY KEY, until_time REAL)")
        self.db.commit()

    def close(self):
        if self.db is not None:
            self.db.close(); self.db = None

    def latest(self, key):
        row = self.db.execute("SELECT record FROM observations WHERE source=? ORDER BY id DESC LIMIT 1",
                              (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, record):
        with self.db:
            self.db.execute("INSERT INTO observations(source,received,record) VALUES(?,?,?)",
                            (key, record["received_at"], json.dumps(record, allow_nan=False)))
            self.db.execute("DELETE FROM observations WHERE received < ?", (record["received_at"] - 90 * DAY,))

    def cooldown(self, host, until=None):
        if until is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO cooldown VALUES(?,?)", (host, until))
        row = self.db.execute("SELECT until_time FROM cooldown WHERE host=?", (host,)).fetchone()
        return row[0] if row else 0

    def observation_count(self):
        return self.db.execute("SELECT count(*) FROM observations").fetchone()[0]

    def snapshot(self, specs, now):
        sources = {}
        for spec in specs:
            row = self.latest(spec["key"]) or {"status": "missing", "received_at": None}
            row.update(spec)
            received = row["received_at"]
            row["receipt_age_seconds"] = round(now - received, 1) if received is not None else None
            if row["status"] == "ok":
                if not 0 <= now - received <= RECEIPT_MAX_AGE or now - row["features"]["as_of"] > 3 * DAY:
                    row["status"] = "stale"
            sources[spec["key"]] = row
        return {"schema_version": 1, "mode": "shadow", "live_enabled": False, "model_calls": 0,
                "orders_created": 0, "generated_at": now, "sources": sources,
                "interpretation": "Research observations only; not consumed by the trading context. "
                "TVL includes asset price effects. DEX volume is not buy/sell direction. Open interest is "
                "a stock, not traded volume or directional positioning. Fees are not token-holder earnings. "
                "USD-pegged circulating supply is not net inflows. Provider histories may be revised; "
                "closed dates do not guarantee complete reporting. No calibrated trading edge is claimed."}


class DailyCollector:
    def __init__(self, store, client=None, clock=time.time, sleeper=time.sleep):
        self.store, self.client = store, client or DefiClient(timeout=15)
        self.clock, self.sleeper = clock, sleeper

    def collect(self, specs):
        for spec in specs:
            now = self.clock()
            previous = self.store.latest(spec["key"])
            if previous and 0 <= now - previous["received_at"] < INTERVAL:
                continue
            host = urlsplit(spec["url"]).netloc
            if self.store.cooldown(host) > now:
                continue
            try:
                payload = self.client.get_json(spec["url"])
                received = self.clock()
                features = daily_features(payload, spec["metric"], received)
                record = {"received_at": received, "status": features["quality"], "features": features}
            except (PublicDataError, ValueError, TypeError, KeyError, OverflowError) as exc:
                received = self.clock()
                record = {"received_at": received, "status": "error", "error_type": type(exc).__name__}
                if isinstance(exc, PublicDataError):
                    record["http_status"] = exc.status
                    if exc.status in (429, 503):
                        self.store.cooldown(host, received + max(INTERVAL, exc.retry_after))
            self.store.save(spec["key"], record)
            self.sleeper(1)
        return self.store.snapshot(specs, self.clock())


def atomic_text(directory, name, text):
    target = Path(directory) / name
    if target.is_symlink():
        raise ValueError("report symlinks are not allowed")
    fd, temp = tempfile.mkstemp(prefix=".defi-", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, str(target))
    finally:
        if os.path.exists(temp): os.unlink(temp)


def write_snapshot(snapshot, directory):
    atomic_text(directory, "defi-daily.json", json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2))
