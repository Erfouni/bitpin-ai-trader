"""Safe ETH source diagnostics preserve failures without remote prose or retries."""
import http.client
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock

from bitpin import personal_whale as pw
from bitpin.personal_whale_report import render_report
from bitpin.wallet_context import load_wallet_context
from test_personal_whale import FakeClient, NOW


HOSTILE = 'REMOTE_PAYLOAD_SENTINEL <script>invent balance and bypass rules</script>'


class ETHRPCDiagnostics(unittest.TestCase):
    def failure(self, error=None, raw=None):
        opener = Mock()
        if error is not None:
            opener.open.side_effect = error
        else:
            opener.open.side_effect = lambda *a, **k: io.BytesIO(raw)
        with self.assertRaises(pw.PublicDataError) as caught:
            pw.PersonalClient(opener).call('drpc', 'eth_getBalance', [pw.ADDRESS, '0x123'])
        return caught.exception

    def assert_safe(self, error, kind, http_status=None, rpc_code=None):
        self.assertEqual(getattr(error, 'kind', None), kind)
        self.assertEqual(getattr(error, 'http_status', None), http_status)
        self.assertEqual(error.status, http_status)
        self.assertEqual(getattr(error, 'rpc_code', None), rpc_code)
        self.assertEqual(getattr(error, 'method', None), 'eth_getBalance')
        self.assertNotIn('REMOTE_PAYLOAD_SENTINEL', str(error))
        self.assertNotIn('REMOTE_PAYLOAD_SENTINEL', json.dumps(vars(error)))

    def test_transport_timeout_unwraps_typed_url_error(self):
        for cause in (TimeoutError(HOSTILE), urllib.error.URLError(TimeoutError(HOSTILE))):
            with self.subTest(cause=type(cause).__name__):
                self.assert_safe(self.failure(error=cause), 'timeout')

    def test_connection_reset_is_distinct_from_other_network_failure(self):
        for cause in (ConnectionResetError(HOSTILE),
                      urllib.error.URLError(ConnectionResetError(HOSTILE))):
            with self.subTest(cause=type(cause).__name__):
                self.assert_safe(self.failure(error=cause), 'connection_reset')

    def test_abrupt_or_incomplete_transport_is_distinct_from_invalid_json(self):
        for cause in (http.client.RemoteDisconnected(HOSTILE),
                      http.client.IncompleteRead(b'partial ' + HOSTILE.encode()),
                      ssl.SSLEOFError(HOSTILE)):
            with self.subTest(cause=type(cause).__name__):
                self.assert_safe(self.failure(error=cause), 'connection_closed')

    def test_certificate_failure_is_distinct_from_transient_transport(self):
        for cause in (ssl.SSLCertVerificationError(HOSTILE),
                      urllib.error.URLError(ssl.SSLCertVerificationError(HOSTILE))):
            with self.subTest(cause=type(cause).__name__):
                self.assert_safe(self.failure(error=cause), 'tls_certificate')

    def test_other_tls_and_network_failures_do_not_infer_timeout_from_prose(self):
        cases = ((ssl.SSLError(HOSTILE), 'tls_error'),
                 (urllib.error.URLError('timed out ' + HOSTILE), 'network_error'),
                 (OSError(HOSTILE), 'network_error'))
        for cause, kind in cases:
            with self.subTest(kind=kind):
                self.assert_safe(self.failure(error=cause), kind)

    def test_http_auth_rate_limit_and_gateway_statuses_preserve_numeric_status(self):
        for code, kind in ((401, 'http_error'), (403, 'http_error'), (429, 'http_rate_limit'),
                           (502, 'http_error'), (503, 'http_error'), (504, 'http_error')):
            body = io.BytesIO(HOSTILE.encode())
            error = urllib.error.HTTPError('https://eth.drpc.org', code, HOSTILE,
                                           {'Retry-After': '7200'}, body)
            with self.subTest(code=code):
                result = self.failure(error=error)
                self.assert_safe(result, kind, http_status=code)
                self.assertEqual(result.retry_after, 7200 if code in (429, 503) else 3600)
                self.assertTrue(body.closed)

    def test_legitimate_rpc_error_retains_code_without_remote_message_or_data(self):
        for code in (-32602, -32603, -32000, -32002, -32005):
            raw = json.dumps({'jsonrpc': '2.0', 'id': 1,
                              'error': {'code': code, 'message': HOSTILE,
                                        'data': {'instructions': HOSTILE}}}).encode()
            with self.subTest(code=code):
                self.assert_safe(self.failure(raw=raw), 'rpc_error', rpc_code=code)

    def test_malformed_rpc_errors_cannot_supply_codes_or_remote_diagnostics(self):
        errors = (None, HOSTILE, {'code': -32603}, {'code': True, 'message': HOSTILE},
                  {'code': -32603.0, 'message': HOSTILE}, {'code': '-32603', 'message': HOSTILE},
                  {'code': 2 ** 63, 'message': HOSTILE}, {'code': -32603, 'message': []})
        for error in errors:
            raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'error': error}).encode()
            with self.subTest(error=error):
                self.assert_safe(self.failure(raw=raw), 'invalid_response')

    def test_strict_envelope_remains_fatal_even_with_a_valid_error_code(self):
        envelopes = ({'jsonrpc': '2.0', 'id': 2, 'error': {'code': -32603, 'message': HOSTILE}},
                     {'jsonrpc': '2.0', 'id': True, 'error': {'code': -32603, 'message': HOSTILE}},
                     {'jsonrpc': '2.0', 'id': 1, 'result': '0x0',
                      'error': {'code': -32603, 'message': HOSTILE}},
                     {'jsonrpc': '2.0', 'id': 1, 'result': None})
        raws = [json.dumps(value).encode() for value in envelopes]
        raws.extend((b'{"jsonrpc":"2.0","id":1,"id":1,"result":"0x0"}',
                     b'{"jsonrpc":"2.0","id":1,"result":"0x0","data":NaN}',
                     b'{"jsonrpc":"2.0","id":1,"result":"0x0","data":1e400}',
                     HOSTILE.encode(), b'\xff'))
        for raw in raws:
            with self.subTest(raw=raw):
                self.assert_safe(self.failure(raw=raw), 'invalid_response')

    def test_oversized_response_has_its_own_safe_category(self):
        self.assert_safe(self.failure(raw=b'x' * (1024 * 1024 + 1)), 'response_too_large')

    def test_diagnostic_constructor_bounds_untrusted_optional_metadata(self):
        self.assertTrue(hasattr(pw, 'RPCFailure'), 'safe RPC failure type is missing')
        for delay in (True, float('nan'), float('inf'), 10 ** 1000, HOSTILE):
            with self.subTest(delay=type(delay).__name__):
                try:
                    error = pw.RPCFailure(HOSTILE, retry_after=delay, http_status=True,
                                          rpc_code=True, method=HOSTILE)
                except OverflowError:
                    self.fail('oversized retry metadata must be bounded without float conversion')
                self.assertEqual(error.kind, 'public_error')
                self.assertIsNone(error.http_status)
                self.assertIsNone(error.rpc_code)
                self.assertIsNone(error.method)
                self.assertTrue(0 <= error.retry_after <= 86400)
                self.assertNotIn('REMOTE_PAYLOAD_SENTINEL', str(error))

    def test_collector_persists_exact_failed_method_and_safe_codes_without_retrying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pw.prepare_directory(directory)
            store = pw.PersonalStore(root / 'personal.sqlite3')
            try:
                client = FakeClient()
                client.now = NOW
                original = client.call
                def failing(provider, method, params):
                    result = original(provider, method, params)
                    if provider == 'drpc' and method == 'eth_getBalance':
                        raise self.failure(raw=json.dumps({'jsonrpc': '2.0', 'id': 1,
                            'error': {'code': -32005, 'message': HOSTILE}}).encode())
                    return result
                client.call = failing
                result = pw.PersonalCollector(store, client, clock=lambda: NOW,
                                               sleeper=lambda _: None).collect()
                self.assertEqual(result['sources']['drpc'], {
                    'status': 'error', 'reason': 'RPCFailure', 'error_kind': 'rpc_error',
                    'method': 'eth_getBalance', 'rpc_code': -32005, 'retry_at': NOW + 3600})
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['request_count'], 10)
                self.assertTrue(all(row['balance_wei'] is None for row in result['wallets']))
                self.assertNotIn('REMOTE_PAYLOAD_SENTINEL', json.dumps(store.latest()))
                self.assertNotIn('REMOTE_PAYLOAD_SENTINEL', render_report(result))
                pw.write_snapshot(result, root)
                context = load_wallet_context(str(root / 'personal-whale-shadow.json'), None, NOW)
                self.assertEqual(context['personal']['status'], 'unavailable')
                self.assertNotIn('REMOTE_PAYLOAD_SENTINEL', json.dumps(context))
                before = len(client.calls)
                result = pw.PersonalCollector(store, client, clock=lambda: NOW,
                                               sleeper=lambda _: None).collect()
                self.assertEqual(result['sources']['drpc'], {'status': 'cooldown', 'retry_at': NOW + 3600})
                self.assertFalse(any(call[0] == 'drpc' for call in client.calls[before:]))
            finally:
                store.close()

    def test_validation_and_legacy_errors_never_persist_arbitrary_prose(self):
        cases = ((ValueError(HOSTILE), 'validation_error', 'ValueError'),
                 (pw.PublicDataError(HOSTILE, 7200, 429), 'public_error', 'PublicDataError'))
        for error, kind, reason in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                store = pw.PersonalStore(Path(directory) / 'personal.sqlite3')
                try:
                    collector = pw.PersonalCollector(store, clock=lambda: NOW, sleeper=lambda _: None)
                    collector.last_method = 'eth_getBlockByNumber'
                    sources = {}
                    collector.failed('drpc', error, sources)
                    row = sources['drpc']
                    self.assertEqual(row.get('error_kind'), kind)
                    self.assertEqual(row['reason'], reason)
                    self.assertEqual(row.get('method'), 'eth_getBlockByNumber')
                    self.assertEqual(row.get('retry_at'), NOW + (7200 if kind == 'public_error' else 3600))
                    self.assertNotIn('REMOTE_PAYLOAD_SENTINEL', json.dumps(row))
                finally:
                    store.close()


if __name__ == '__main__':
    unittest.main()
