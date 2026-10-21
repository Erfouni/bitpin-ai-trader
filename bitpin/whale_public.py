"""Public Bitcoin observations. GET only; amounts are integer satoshis."""
import http.client
import json
import math
import re
import socket
import urllib.error
import urllib.request

from .flow_public import NoRedirect, PublicDataError, retry_seconds

PROVIDERS = {"blockstream": "https://blockstream.info/api", "mempool": "https://mempool.space/api"}
WALLETS = (
    {"address": "1Kr6QSydW9bFQG1mXiPNNu6WpJGmUa9i1g", "label": "Published BTC hot address"},
    {"address": "3JZq4atUahhuA9rLhXLMhhTo133J9rF97j", "label": "Published BTC cold address 1"},
    {"address": "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97", "label": "Published BTC cold address 2"},
)
ADDRESSES = frozenset(w["address"] for w in WALLETS)
PROVENANCE = {"publisher": "Bitfinex", "kind": "historical_self_published_custody_sample",
              "url": "https://github.com/bitfinexcom/pub/blob/83d06bdb5f5a2ce05d5ba2db5a426dd44c9f5128/wallets.txt",
              "revision": "83d06bdb5f5a2ce05d5ba2db5a426dd44c9f5128", "published_revision_date": "2022-12-12",
              "current_ownership_verified": False, "complete_exchange_reserves": False,
              "independent_investor_whales": False}
MAX_MONEY = 21000000 * 100000000
LARGE_SATS = 500 * 100000000
HEX = re.compile(r"[0-9a-f]{64}\Z")


def integer(value, maximum=2 ** 63 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid nonnegative integer")
    return value


def block_hash(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("invalid hash")
    return value


def address_stats(payload, address):
    if not isinstance(payload, dict) or payload.get("address") != address:
        raise ValueError("address identity mismatch")
    chain, pending = payload["chain_stats"], payload["mempool_stats"]
    names = ("tx_count", "funded_txo_count", "spent_txo_count", "funded_txo_sum", "spent_txo_sum")
    result = {name: integer(chain[name]) for name in names}
    balance = result["funded_txo_sum"] - result["spent_txo_sum"]
    integer(balance, MAX_MONEY)
    for name in names:
        integer(pending[name])
    return {"chain_stats": result, "balance_sats": balance, "pending_tx_count": pending["tx_count"]}


def _output(row):
    address = row.get("scriptpubkey_address")
    if address is not None and (not isinstance(address, str) or not 1 <= len(address) <= 128):
        raise ValueError("invalid script address")
    return {"address": address, "value": integer(row["value"], MAX_MONEY)}


def normalize_tx(payload):
    if not isinstance(payload, dict):
        raise ValueError("invalid transaction")
    status = payload["status"]
    if status.get("confirmed") is not True:
        raise ValueError("unconfirmed history row")
    result = {"txid": block_hash(payload["txid"]), "fee": integer(payload["fee"], MAX_MONEY),
              "height": integer(status["block_height"], 10000000),
              "block_hash": block_hash(status["block_hash"]),
              "block_time": integer(status["block_time"], 2 ** 40), "inputs": [], "outputs": []}
    vin, vout = payload["vin"], payload["vout"]
    if not isinstance(vin, list) or not isinstance(vout, list) or not 1 <= len(vin) <= 10000 or not 1 <= len(vout) <= 10000:
        raise ValueError("invalid transaction size")
    coinbase = False
    for row in vin:
        if type(row.get("is_coinbase")) is not bool:
            raise ValueError("missing coinbase flag")
        if row["is_coinbase"]:
            if len(vin) != 1:
                raise ValueError("invalid coinbase inputs")
            coinbase = True
        else:
            result["inputs"].append(_output(row["prevout"]))
    result["outputs"] = [_output(row) for row in vout]
    result["coinbase"] = coinbase
    incoming = sum(row["value"] for row in result["inputs"])
    outgoing = sum(row["value"] for row in result["outputs"])
    integer(outgoing, MAX_MONEY)
    if not coinbase and (incoming > MAX_MONEY or incoming - outgoing != result["fee"]):
        raise ValueError("transaction value/fee mismatch")
    return result


def classify_tx(tx, addresses=ADDRESSES):
    """Address-set movements only; never infer ultimate owner or trading motive."""
    inputs, outputs = tx["inputs"], tx["outputs"]
    known_in = sum(row["value"] for row in inputs if row["address"] in addresses)
    known_out = sum(row["value"] for row in outputs if row["address"] in addresses)
    any_in = any(row["address"] in addresses for row in inputs)
    all_in = bool(inputs) and all(row["address"] in addresses for row in inputs)
    all_out = all(row["address"] in addresses for row in outputs)
    if tx["coinbase"] or any(row["address"] is None for row in inputs + outputs) or (any_in and not all_in):
        kind, amount = "ambiguous", None
    elif all_in and all_out:
        kind, amount = "known_set_internal", known_out
    elif all_in:
        kind, amount = "to_unlisted", sum(row["value"] for row in outputs if row["address"] not in addresses)
    elif not any_in and any(row["address"] in addresses for row in outputs):
        kind, amount = "from_unlisted", known_out
    else:
        kind, amount = "ambiguous", None
    return {"kind": kind, "amount_sats": amount, "fee_sats": tx["fee"],
            "watched_input_sats": known_in, "watched_output_sats": known_out,
            "large_movement": amount is not None and amount >= LARGE_SATS,
            "not_a_buy_sell_signal": True}


def ipv4_connection(address, timeout=12, source_address=None):
    """Resolve only A records in this client; never change the process/global resolver."""
    last_error = None
    for family, socktype, protocol, _, peer in socket.getaddrinfo(address[0], address[1], socket.AF_INET, socket.SOCK_STREAM):
        sock = None
        try:
            sock = socket.socket(family, socktype, protocol)
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(peer)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    raise last_error or OSError("no IPv4 address available")


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # CPython 3.7–3.14 exposes this per-connection socket factory. TLS still uses the original hostname.
        self._create_connection = ipv4_connection


class IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(IPv4HTTPSConnection, request, context=self._context)


class WhaleClient:
    # Blockstream answers in about 6 s and sometimes just over 12 s; a 12 s limit failed whole runs.
    def __init__(self, opener=None, timeout=30, max_bytes=4 * 1024 * 1024):
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), IPv4HTTPSHandler())
        self.timeout, self.max_bytes = timeout, max_bytes

    def get(self, provider, path):
        if provider not in PROVIDERS or not isinstance(path, str):
            raise ValueError("unapproved provider")
        text = path == "/blocks/tip/height" or bool(re.fullmatch(r"/block-height/[0-9]{1,8}", path))
        allowed = text or bool(re.fullmatch(r"/block/[0-9a-f]{64}", path))
        for address in ADDRESSES:
            root = "/address/" + address
            allowed = allowed or path == root or path == root + "/txs/chain" or bool(
                re.fullmatch(re.escape(root) + r"/txs/chain/[0-9a-f]{64}", path))
        if not allowed:
            raise ValueError("unapproved public GET path")
        request = urllib.request.Request(PROVIDERS[provider] + path,
                                         headers={"User-Agent": "BitpinWhaleShadow/1.0", "Accept": "application/json"},
                                         method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise PublicDataError("public response exceeds size limit", 3600)
        except urllib.error.HTTPError as exc:
            status = exc.code
            delay = retry_seconds(exc.headers.get("Retry-After")) if status in (429, 503) else 3600
            exc.close()
            raise PublicDataError("public HTTP %d" % status, delay, status)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise PublicDataError("public network error: " + type(exc).__name__, 3600)
        try:
            decoded = raw.decode("utf-8")
            if text:
                return decoded.strip()
            def invalid(value):
                raise ValueError("nonfinite JSON")
            result = json.loads(decoded, parse_constant=invalid)
            stack = [(result, 0)]
            while stack:
                value, depth = stack.pop()
                if depth > 40:
                    raise ValueError("excessive JSON nesting")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("nonfinite JSON")
                children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
                for row in children:
                    if isinstance(row, float) and not math.isfinite(row):
                        raise ValueError("nonfinite JSON")
                    if isinstance(row, (dict, list)):
                        stack.append((row, depth + 1))
            return result
        except (ValueError, UnicodeError, RecursionError):
            raise PublicDataError("invalid public response", 3600)
