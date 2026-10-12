"""Public RPC diagnostics are useful without exposing provider-controlled text."""
import unittest

from bitpin.personal_whale_report import render_report


class RPCDiagnosticsReport(unittest.TestCase):
    def report(self, source):
        return render_report({'status': 'error', 'received_at': 1700000000,
            'request_count': 12, 'wallets': [], 'sources': {
                'publicnode': {'status': 'ok', 'finalized_height': 50000},
                'drpc': source}})

    def test_rpc_error_identifies_method_and_code_but_hides_raw_reason(self):
        rendered = self.report({'status': 'error', 'error_kind': 'rpc_error',
            'method': 'eth_getBalance', 'rpc_code': -32603,
            'reason': 'PRIVATE_PROVIDER_TEXT<script>bad()</script>'})
        self.assertTrue('RPC error' in rendered, 'RPC failure kind missing')
        self.assertTrue('eth_getBalance' in rendered, 'failed balance method missing')
        self.assertTrue('RPC -32603' in rendered, 'RPC code missing')
        self.assertNotIn('PRIVATE_PROVIDER_TEXT', rendered)
        self.assertNotIn('<script>', rendered)

    def test_http_error_shows_validated_status(self):
        rendered = self.report({'status': 'error', 'error_kind': 'http_rate_limit',
            'method': 'eth_getBlockByNumber', 'http_status': 429})
        self.assertTrue('Rate limited' in rendered, 'rate-limit label missing')
        self.assertTrue('HTTP 429' in rendered, 'HTTP status missing')
        self.assertTrue('eth_getBlockByNumber' in rendered, 'failed block method missing')

    def test_cooldown_exposes_next_allowed_attempt(self):
        rendered = self.report({'status': 'cooldown', 'retry_at': 1700003600})
        self.assertTrue('Retry after 14 Nov 2023' in rendered, 'next allowed attempt missing')
        self.assertNotIn('native balances available<br>Requests: 0', rendered)

    def test_initial_failure_exposes_next_allowed_attempt(self):
        rendered = self.report({'status': 'error', 'error_kind': 'http_rate_limit',
            'http_status': 429, 'method': 'eth_getBalance', 'retry_at': 1700003600})
        self.assertTrue('Retry after 14 Nov 2023' in rendered, 'initial failure lacks retry deadline')

    def test_untrusted_diagnostic_values_are_not_rendered_or_executed(self):
        for data in (
            {'error_kind': '<script>bad()</script>', 'method': 'PRIVATE_PROVIDER_TEXT',
             'rpc_code': 'PRIVATE_PROVIDER_TEXT', 'http_status': 'PRIVATE_PROVIDER_TEXT'},
            {'error_kind': [], 'method': {}, 'rpc_code': True, 'http_status': True},
            {'error_kind': 'rpc_error', 'method': 'eth_sendRawTransaction',
             'rpc_code': 2**40, 'http_status': 999, 'retry_at': float('nan')},
        ):
            rendered = self.report(dict(data, status='error'))
            self.assertNotIn('<script>', rendered)
            self.assertNotIn('PRIVATE_PROVIDER_TEXT', rendered)
            self.assertNotIn('eth_sendRawTransaction', rendered)
            self.assertNotIn('HTTP 999', rendered)
            self.assertNotIn('RPC 1099511627776', rendered)

    def test_unrepresentable_cooldown_does_not_break_report(self):
        for value in (10**400, float('nan'), float('inf'), True):
            rendered = self.report({'status': 'cooldown', 'retry_at': value})
            self.assertNotIn('Retry after', rendered)


if __name__ == '__main__':
    unittest.main()
