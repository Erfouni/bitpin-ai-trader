"""Economic parsing and isolated shadow observations; no account/network fixtures."""
import copy
import io
import json
from pathlib import Path
import tempfile
import socket
import ssl
import unittest
from unittest.mock import patch
import urllib.error

try:
    from bitpin import whale_public as public
    from bitpin import whale_shadow as shadow
except ImportError:
    public = shadow = None

NOW = 1791734400
A = "1Kr6QSydW9bFQG1mXiPNNu6WpJGmUa9i1g"
B = "3JZq4atUahhuA9rLhXLMhhTo133J9rF97j"
C = "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97"
OTHER = "bc1qunlisted"


def transaction(inputs=None, outputs=None, ident=1, height=990, coinbase=False):
    inputs = inputs if inputs is not None else [(A, 100000000)]
    outputs = outputs if outputs is not None else [(OTHER, 99999000)]
    return {"txid": "%064x" % ident, "fee": 0 if coinbase else sum(v for _, v in inputs) - sum(v for _, v in outputs),
            "vin": [{"is_coinbase": True, "prevout": None}] if coinbase else
                   [{"is_coinbase": False, "prevout": {"scriptpubkey_address": a, "value": v}} for a, v in inputs],
            "vout": [{"scriptpubkey_address": a, "value": v} for a, v in outputs],
            "status": {"confirmed": True, "block_height": height, "block_hash": "%064x" % (height + 1000),
                       "block_time": NOW - 7200}}


def stats(address, balance=100000000):
    return {"address": address, "chain_stats": {"tx_count": 1, "funded_txo_count": 1,
            "spent_txo_count": 0, "funded_txo_sum": balance, "spent_txo_sum": 0},
            "mempool_stats": {"tx_count": 0, "funded_txo_count": 0, "spent_txo_count": 0,
                              "funded_txo_sum": 0, "spent_txo_sum": 0}}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.bad_provider = None
        self.different = False
        self.reorg = False
        self.moving = False
        self.full_page = False
        self.now = NOW

    def get(self, provider, path):
        self.calls.append((provider, path))
        if provider == self.bad_provider:
            raise public.PublicDataError("HTTP 429", 7200, 429)
        if path == "/blocks/tip/height":
            return "1000"
        if path.startswith("/block-height/"):
            height = int(path.rsplit("/", 1)[1])
            return "%064x" % (height + 1000 + (10000 if self.reorg and height == 995 else 0))
        if path.startswith("/block/"):
            return {"timestamp": self.now - 300, "height": 1000}
        if path.startswith("/address/"):
            address = path.split("/")[2]
            if "/txs/chain" in path:
                if self.full_page:
                    offset = 25 if path.count("/") == 5 else 0
                    return [transaction(inputs=[(A, 100000000), (B, 100000000), (C, 100000000)],
                                        outputs=[(OTHER, 299999000)], ident=i + 1 + offset) for i in range(25)]
                return [transaction(inputs=[(A, 100000000), (B, 100000000), (C, 100000000)],
                                    outputs=[(OTHER, 299999000)])]
            result = stats(address)
            if self.full_page:
                result["chain_stats"]["tx_count"] = 50
            if self.different and provider == "mempool":
                result["chain_stats"]["funded_txo_sum"] += 1
            if self.moving and sum(p == provider and x == path for p, x in self.calls) > 1:
                result["chain_stats"]["tx_count"] += 1
            return result
        raise AssertionError(path)


class Helpers:
    def require(self, module, name):
        function = getattr(module, name, None)
        self.assertTrue(callable(function), "missing whale feature: " + name)
        return function

    def normalized(self, tx):
        return self.require(public, "normalize_tx")(tx)

    def classified(self, tx):
        return self.require(public, "classify_tx")(self.normalized(tx), {A, B, C})


class Features(Helpers, unittest.TestCase):
    def test_confirmed_balance_excludes_mempool(self):
        value = stats(A)
        value["mempool_stats"]["funded_txo_sum"] = 777
        self.assertEqual(self.require(public, "address_stats")(value, A)["balance_sats"], 100000000)

    def test_boolean_and_float_amounts_rejected(self):
        parse = self.require(public, "address_stats")
        for value in (True, 1.0, -1, "100", 2 ** 63):
            record = stats(A); record["chain_stats"]["funded_txo_sum"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse(record, A)

    def test_cumulative_turnover_can_exceed_bitcoin_supply(self):
        record = stats(A)
        record["chain_stats"].update(funded_txo_sum=3000000000000000, spent_txo_sum=2999999900000000)
        self.assertEqual(self.require(public, "address_stats")(record, A)["balance_sats"], 100000000)

    def test_bad_identity_and_negative_balance_rejected(self):
        parse = self.require(public, "address_stats")
        for record in (stats(B), stats(A)):
            if record["address"] == A:
                record["chain_stats"]["spent_txo_sum"] = 100000001
            with self.assertRaises(ValueError):
                parse(record, A)

    def test_known_set_internal_move_excludes_fee_from_external_flow(self):
        result = self.classified(transaction(outputs=[(B, 99999000)]))
        self.assertEqual(result["kind"], "known_set_internal")
        self.assertEqual(result["amount_sats"], 99999000)
        self.assertEqual(result["fee_sats"], 1000)

    def test_outgoing_uses_unlisted_outputs_not_change(self):
        result = self.classified(transaction(outputs=[(B, 60000000), (OTHER, 39999000)]))
        self.assertEqual((result["kind"], result["amount_sats"]), ("to_unlisted", 39999000))

    def test_incoming_uses_only_watched_outputs(self):
        result = self.classified(transaction(inputs=[(OTHER, 100000000)], outputs=[(A, 30000000), (OTHER, 69999000)]))
        self.assertEqual((result["kind"], result["amount_sats"]), ("from_unlisted", 30000000))

    def test_mixed_inputs_unknown_scripts_coinbase_are_ambiguous(self):
        rows = [transaction(inputs=[(A, 50000000), (OTHER, 50000000)]),
                transaction(outputs=[(None, 99999000)]), transaction(coinbase=True)]
        for tx in rows:
            with self.subTest(tx=tx):
                result = self.classified(tx)
                self.assertEqual(result["kind"], "ambiguous")
                self.assertIsNone(result["amount_sats"])

    def test_normalization_ignores_provider_display_metadata(self):
        first = transaction(); second = copy.deepcopy(first); second["sigops"] = 4
        self.assertEqual(self.normalized(first), self.normalized(second))

    def test_fee_conservation_and_noninteger_output_are_rejected(self):
        rows = [transaction(), transaction(), transaction()]
        rows[0]["fee"] = 2
        rows[1]["vout"][0]["value"] = 1.0
        rows[2]["status"]["confirmed"] = False
        for tx in rows:
            with self.assertRaises(ValueError):
                self.normalized(tx)

    def test_large_threshold_does_not_use_unrelated_transaction_value(self):
        result = self.classified(transaction(inputs=[(OTHER, 60000000000)], outputs=[(A, 1), (OTHER, 59999999999)]))
        self.assertFalse(result["large_movement"])


class Transport(Helpers, unittest.TestCase):
    def test_ipv4_connection_restricts_resolution_and_retains_hostname_tls(self):
        connection = self.require(public, "IPv4HTTPSConnection")("mempool.space", timeout=12)
        self.assertEqual(connection.host, "mempool.space")
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        class Socket:
            def settimeout(self, value): self.timeout = value
            def connect(self, address): self.peer = address
            def close(self): self.closed = True
        sock = Socket()
        with patch.object(public.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]) as resolver, \
             patch.object(public.socket, "socket", return_value=sock):
            actual = connection._create_connection(("mempool.space",443),12,None)
        self.assertIs(actual,sock)
        self.assertEqual(resolver.call_args[0][2],socket.AF_INET)
        self.assertEqual(sock.timeout,12)

    def test_failed_ipv4_socket_is_closed(self):
        connect = self.require(public, "ipv4_connection")
        class Socket:
            closed = False
            def settimeout(self, value): pass
            def connect(self, address): raise OSError("unreachable")
            def close(self): self.closed = True
        sock = Socket()
        with patch.object(public.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1",443))]), \
             patch.object(public.socket, "socket", return_value=sock), self.assertRaises(OSError):
            connect(("mempool.space",443),12,None)
        self.assertTrue(sock.closed)

    def test_allowlist_refuses_broadcast_credentials_and_arbitrary_address(self):
        client = self.require(public, "WhaleClient")()
        for provider, path in [("blockstream", "/tx"), ("blockstream", "/address/secret"),
                               ("blockstream", "/blocks/tip/height?key=abc"),
                               ("https://evil.example", "/blocks/tip/height")]:
            with self.assertRaises(ValueError):
                client.get(provider, path)

    def test_public_transport_has_no_post_or_auth(self):
        seen = []
        class Response(io.BytesIO):
            pass
        class Opener:
            def open(self, request, timeout):
                seen.append(request)
                return Response(b"1000")
        client = self.require(public, "WhaleClient")(opener=Opener())
        self.assertEqual(client.get("blockstream", "/blocks/tip/height"), "1000")
        self.assertEqual(seen[0].get_method(), "GET")
        self.assertNotIn("authorization", dict((k.lower(), v) for k, v in seen[0].header_items()))

    def test_size_and_nonfinite_json_rejected(self):
        class Opener:
            def __init__(self, raw): self.raw = raw
            def open(self, request, timeout): return io.BytesIO(self.raw)
        constructor = self.require(public, "WhaleClient")
        for raw in (b"{" + b"x" * 33, b'{"a":NaN}', b'{"a":[1e400]}'):
            with self.assertRaises(public.PublicDataError):
                constructor(opener=Opener(raw), max_bytes=32).get("blockstream", "/address/" + A)


class Persistence(Helpers, unittest.TestCase):
    def make_store(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        store = self.require(shadow, "WhaleStore")(Path(directory.name) / "whale.sqlite3")
        self.addCleanup(store.close)
        return store

    def collect(self, store, client, stamp=NOW, sleeper=lambda _: None):
        client.now = stamp
        return self.require(shadow, "WhaleCollector")(store, client=client, clock=lambda: stamp, sleeper=sleeper).collect()

    def test_agreement_deduplicates_shared_tx_and_has_no_live_effect(self):
        snap = self.collect(self.make_store(), FakeClient())
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(len(snap["transactions"]), 1)
        self.assertFalse(snap["live_enabled"])
        self.assertEqual((snap["orders_created"], snap["model_calls"]), (0, 0))
        self.assertIsNone(snap["wallets"][0]["change_24h_sats"])

    def test_disagreement_hides_current_metrics(self):
        store = self.make_store(); self.collect(store, FakeClient())
        client = FakeClient(); client.different = True
        snap = self.collect(store, client, NOW + 3600)
        self.assertEqual(snap["status"], "disagreement")
        self.assertEqual(snap["transactions"], [])
        self.assertIsNone(snap["wallets"][0]["balance_sats"])

    def test_moving_history_is_not_success(self):
        client = FakeClient(); client.moving = True
        snap = self.collect(self.make_store(), client)
        self.assertEqual(snap["status"], "error")
        self.assertEqual(snap["transactions"], [])

    def test_two_pages_are_bounded_and_marked_as_sample(self):
        client = FakeClient(); client.full_page = True
        snap = self.collect(self.make_store(), client)
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(len(snap["transactions"]), 50)
        self.assertFalse(snap["complete_daily_flow"])
        self.assertLessEqual(len(client.calls), 64)
        self.assertTrue(all(row["history_capped"] for row in snap["wallets"]))

    def test_failure_cooldown_is_persisted_and_other_provider_still_checked(self):
        store = self.make_store(); client = FakeClient(); client.bad_provider = "blockstream"
        snap = self.collect(store, client)
        self.assertEqual(snap["status"], "error")
        next_client = FakeClient(); later = self.collect(store, next_client, NOW + 3600)
        self.assertEqual(later["sources"]["blockstream"]["status"], "cooldown")
        self.assertFalse(any(provider == "blockstream" for provider, _ in next_client.calls))
        self.assertTrue(any(provider == "mempool" for provider, _ in next_client.calls))

    def test_24h_delta_requires_nearby_observation_and_uses_real_elapsed_time(self):
        store = self.make_store(); self.collect(store, FakeClient(), NOW)
        snap = self.collect(store, FakeClient(), NOW + 86400 + 120)
        self.assertEqual(snap["wallets"][0]["change_24h_sats"], 0)
        self.assertEqual(snap["wallets"][0]["reference_24h_elapsed_seconds"], 86520)
        self.assertIsNone(snap["wallets"][0]["change_7d_sats"])

    def test_missing_reference_is_not_interpolated_or_zero(self):
        store = self.make_store(); self.collect(store, FakeClient(), NOW)
        snap = self.collect(store, FakeClient(), NOW + 86400 + 3601)
        self.assertEqual(snap["status"], "ok")
        self.assertIsNone(snap["wallets"][0]["change_24h_sats"])

    def test_seven_day_delta(self):
        store = self.make_store(); self.collect(store, FakeClient(), NOW)
        snap = self.collect(store, FakeClient(), NOW + 7 * 86400)
        self.assertEqual(snap["wallets"][0]["change_7d_sats"], 0)

    def test_report_only_stale_or_future_observation_has_no_metrics(self):
        store = self.make_store(); self.collect(store, FakeClient(), NOW)
        for stamp in (NOW + 9001, NOW - 1):
            snap = store.snapshot(stamp)
            self.assertEqual(snap["status"], "stale")
            self.assertEqual(snap["transactions"], [])
            self.assertIsNone(snap["wallets"][0]["balance_sats"])

    def test_reorg_resets_reference_epoch(self):
        store = self.make_store(); self.collect(store, FakeClient(), NOW)
        client = FakeClient(); client.reorg = True
        snap = self.collect(store, client, NOW + 86400)
        self.assertEqual(snap["status"], "reorg")
        later = self.collect(store, client, NOW + 86400 + 3600)
        self.assertEqual(later["status"], "ok")
        self.assertIsNone(later["wallets"][0]["change_24h_sats"])

    def test_request_spacing(self):
        sleeps = []
        self.collect(self.make_store(), FakeClient(), sleeper=sleeps.append)
        self.assertTrue(sleeps)
        self.assertTrue(all(delay >= 2 for delay in sleeps))

    def test_less_than_six_confirmations_excluded_from_eligible_section(self):
        class Recent(FakeClient):
            def get(self, provider, path):
                value = super().get(provider, path)
                if "/txs/chain" in path:
                    value[0]["status"]["block_height"] = 999
                return value
        snap = self.collect(self.make_store(), Recent())
        self.assertEqual(snap["transactions"], [])
        self.assertEqual(snap["excluded_recent_transactions"], 1)

    def test_unrelated_address_history_rejected(self):
        class Unrelated(FakeClient):
            def get(self, provider, path):
                value = super().get(provider, path)
                return [transaction()] if "/txs/chain" in path else value
        snap = self.collect(self.make_store(), Unrelated())
        self.assertEqual(snap["status"], "error")

    def test_truncated_exhausted_history_rejected(self):
        class Truncated(FakeClient):
            def get(self, provider, path):
                value = super().get(provider, path)
                if isinstance(value, dict) and "chain_stats" in value:
                    value["chain_stats"]["tx_count"] = 9
                return value
        snap = self.collect(self.make_store(), Truncated())
        self.assertEqual(snap["status"], "error")

    def test_late_transport_failure_persists_provider_cooldown(self):
        class Late(FakeClient):
            def get(self, provider, path):
                value = super().get(provider, path)
                if provider == "blockstream" and path == "/block-height/995":
                    raise public.PublicDataError("HTTP 429", 7200, 429)
                return value
        store = self.make_store()
        snap = self.collect(store, Late())
        self.assertEqual(snap["status"], "error")
        self.assertGreaterEqual(store.cooldown("blockstream"), NOW + 7200)

    def test_balance_reference_uses_current_run_not_failed_observation(self):
        store = self.make_store(); self.collect(store, FakeClient(), NOW)
        bad = FakeClient(); bad.different = True
        self.collect(store, bad, NOW + 3600)
        snap = self.collect(store, FakeClient(), NOW + 86400)
        self.assertEqual(snap["wallets"][0]["change_24h_sats"], 0)

    def test_late_malformed_hash_marks_source_error_and_backs_off(self):
        class Malformed(FakeClient):
            def get(self, provider, path):
                value = super().get(provider, path)
                count = sum(p == provider and x == path for p, x in self.calls)
                if provider == "mempool" and path == "/block-height/995" and count > 1:
                    return "invalid hash"
                return value
        store = self.make_store(); snap = self.collect(store, Malformed())
        self.assertEqual(snap["status"], "error")
        self.assertEqual(snap["sources"]["mempool"]["status"], "error")
        self.assertGreaterEqual(store.cooldown("mempool"), NOW + 3600)

    def test_interrupted_run_invalidates_previous_success_before_requests(self):
        class Interrupted(FakeClient):
            def get(self, provider, path): raise KeyboardInterrupt()
        store = self.make_store(); self.collect(store, FakeClient())
        with self.assertRaises(KeyboardInterrupt): self.collect(store, Interrupted(), NOW + 3600)
        snap = store.snapshot(NOW + 3600)
        self.assertEqual(snap["status"], "collecting")
        self.assertEqual(snap["transactions"], [])
        self.assertIsNone(snap["wallets"][0]["balance_sats"])


if __name__ == "__main__":
    unittest.main()
