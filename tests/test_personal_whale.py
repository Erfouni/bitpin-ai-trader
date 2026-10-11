import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bitpin import personal_whale as pw

NOW = 1800000000
HASH = '0x' + 'a' * 64


def head(number=1000, digest=HASH, timestamp=NOW - 900):
    return {'number': hex(number), 'hash': digest, 'timestamp': hex(timestamp)}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.balance = 224209 * 10 ** 18 + 123
        self.bad = None
        self.disagree = False
        self.old_hash = HASH
        self.late = False
        self.now = NOW

    def call(self, provider, method, params):
        self.calls.append((provider, method, copy.deepcopy(params)))
        if self.bad == provider or (self.late and provider == 'flashbots' and len(self.calls) >= 9):
            raise pw.PublicDataError('unavailable', 7200, 429)
        if method == 'eth_chainId': return '0x1'
        if method == 'eth_getBlockByNumber':
            height = 1000 + (self.now - (NOW - 7 * pw.DAY)) // 12 if params[0] == 'finalized' else int(params[0], 16)
            return head(number=height, digest=self.old_hash, timestamp=NOW - 7 * pw.DAY - 900 + (height - 1000) * 12)
        if method == 'eth_getBalance':
            return hex(self.balance + (1 if self.disagree and provider == 'flashbots' else 0))
        raise AssertionError('unexpected call')


class PersonalWhale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = pw.prepare_directory(self.tmp.name)
        self.store = pw.PersonalStore(self.directory / 'personal.sqlite3')
        self.client = FakeClient()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def collect(self, now=NOW, publish=None):
        self.client.now = now
        return pw.PersonalCollector(self.store, self.client, clock=lambda: now, sleeper=lambda _: None).collect(publish)

    def test_exact_wei_and_unknown_daily_history(self):
        result = self.collect()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['balance_wei'], self.client.balance)
        self.assertIsNone(result['change_24h_wei'])
        self.assertFalse(result['live_enabled'])
        self.assertFalse(result['current_ownership_verified'])
        self.assertFalse(result['complete_portfolio'])
        self.assertEqual(result['orders_created'], 0)

    def test_both_balances_use_common_fixed_block(self):
        self.collect()
        reads = [row for row in self.client.calls if row[1] == 'eth_getBalance']
        self.assertEqual(len(reads), 2)
        self.assertEqual(reads[0][2], [pw.ADDRESS, hex(51400)])
        self.assertEqual(reads[1][2], reads[0][2])

    def test_disagreement_hides_metrics(self):
        self.client.disagree = True
        result = self.collect()
        self.assertEqual(result['status'], 'disputed')
        self.assertIsNone(result['balance_wei'])

    def test_provider_error_reports_both_and_persists_cooldown(self):
        self.client.bad = 'publicnode'
        result = self.collect()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['sources']['publicnode']['status'], 'error')
        self.assertEqual(result['sources']['flashbots']['status'], 'ok')
        self.assertEqual(self.store.cooldown('publicnode'), NOW + 7200)
        before = len(self.client.calls)
        result = self.collect()
        self.assertEqual(result['sources']['publicnode']['status'], 'cooldown')
        self.assertTrue(all(row[0] != 'publicnode' for row in self.client.calls[before:]))

    def test_late_error_hides_previous_success(self):
        self.collect()
        self.client.calls = []
        self.client.late = True
        result = self.collect()
        self.assertEqual(result['status'], 'error')
        self.assertIsNone(result['balance_wei'])
        self.assertEqual(result['sources']['flashbots']['status'], 'error')
        self.assertGreater(self.store.cooldown('flashbots'), NOW)

    def test_interrupted_collection_publishes_unknown_before_requests(self):
        self.collect()
        self.client.calls = []
        observed = []
        def interrupted(provider, method, params):
            self.assertEqual(self.store.snapshot(NOW)['status'], 'collecting')
            self.assertEqual(observed[0]['status'], 'collecting')
            raise KeyboardInterrupt()
        self.client.call = interrupted
        with self.assertRaises(KeyboardInterrupt): self.collect(publish=observed.append)
        self.assertIsNone(self.store.snapshot(NOW)['balance_wei'])

    def test_daily_and_weekly_references_exact_and_same_epoch(self):
        self.collect(now=NOW - 7 * pw.DAY)
        self.collect(now=NOW - pw.DAY)
        self.client.balance += 17
        result = self.collect()
        self.assertEqual(result['change_24h_wei'], 17)
        self.assertEqual(result['change_7d_wei'], 17)
        self.assertEqual(result['reference_24h_elapsed_seconds'], pw.DAY)

    def test_stale_and_future_receipts_hide_metrics(self):
        self.collect()
        for now in (NOW + 9001, NOW - 1):
            result = self.store.snapshot(now)
            self.assertEqual(result['status'], 'stale')
            self.assertIsNone(result['balance_wei'])
            self.assertIsNone(result['anchor'])

    def test_changed_previous_anchor_starts_new_epoch(self):
        self.collect()
        self.client.old_hash = '0x' + 'b' * 64
        result = self.collect()
        self.assertEqual(result['status'], 'reorg')
        self.assertEqual(result['chain_epoch'], 1)
        self.assertIsNone(result['balance_wei'])

    def test_rollback_with_unavailable_old_height_starts_new_epoch(self):
        self.collect()
        original = self.client.call
        def rolled_back(provider, method, params):
            result = original(provider, method, params)
            if method == 'eth_getBlockByNumber':
                if params[0] == 'finalized' or params[0] == hex(51300):
                    return head(number=51300, timestamp=NOW - 2100)
                if params[0] == hex(51400): return None
            return result
        self.client.call = rolled_back
        self.client.calls = []
        result = self.collect()
        self.assertEqual(result['status'], 'reorg')
        self.assertEqual(result['chain_epoch'], 1)
        self.assertEqual(self.store.epoch(), 1)
        self.assertIsNone(result['balance_wei'])
        self.assertFalse(any(row[1:] == ('eth_getBlockByNumber', [hex(51400), False])
                             for row in self.client.calls))
        result = self.collect()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['chain_epoch'], 1)
        self.assertIsNone(result['change_24h_wei'])

    def test_reference_outside_hour_tolerance_is_unknown(self):
        self.collect(now=NOW - pw.DAY - 3601)
        self.assertIsNone(self.collect()['change_24h_wei'])

    def test_quantity_and_block_reject_bad_data(self):
        self.assertEqual(pw.quantity(hex(10 ** 25 + 1)), 10 ** 25 + 1)
        for value in (True, 1, '0x', '0x01', '-0x1', '0X1', '0x' + 'f' * 65):
            with self.subTest(value=value), self.assertRaises(ValueError): pw.quantity(value)
        for data in (None, {}, head(timestamp=NOW - 7201), head(timestamp=NOW + 121), head(digest='bad')):
            with self.subTest(data=data), self.assertRaises(ValueError): pw.block(data, NOW)

    def test_rpc_allowlist_prevents_transactions_and_other_addresses(self):
        opener = unittest.mock.Mock()
        client = pw.PersonalClient(opener=opener)
        for method, params in [('eth_sendRawTransaction', ['0x123']), ('eth_call', []),
                               ('eth_getBalance', ['0x' + '0' * 40, '0x1']),
                               ('eth_getBalance', [pw.ADDRESS, 'latest']),
                               ('eth_getBlockByNumber', ['finalized', True])]:
            with self.subTest(method=method,params=params), self.assertRaises(ValueError):
                client.call('publicnode', method, params)
        opener.open.assert_not_called()

    def test_rpc_response_validation(self):
        for data in ({'jsonrpc':'2.0','id':2,'result':'0x1'},
                     {'jsonrpc':'2.0','id':True,'result':'0x1'},
                     {'jsonrpc':'2.0','id':1,'error':{'code':-1}},
                     {'jsonrpc':'2.0','id':1,'result':None},
                     {'jsonrpc':'1.0','id':1,'result':'0x1'}):
            with self.subTest(data=data), self.assertRaises(pw.PublicDataError):
                pw.rpc_result(json.dumps(data).encode())
        for raw in (b'{"jsonrpc":"2.0","id":1,"result":"0x1","x":1e400}', b'[[[[NaN]]]]', b'not JSON'):
            with self.subTest(raw=raw), self.assertRaises(pw.PublicDataError): pw.rpc_result(raw)

    def test_other_collectors_account_markers_and_nonempty_directories_rejected(self):
        for marker in ('.whale-shadow-state','.flow-shadow-state','.defi-shadow-state','LIVE_CONFIRMED','bitpin_token.json','keep'):
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / marker).write_text('do not read')
                with self.assertRaises(ValueError): pw.prepare_directory(directory)

    def test_report_only_and_exact_eth_format(self):
        from bitpin.personal_whale_report import eth, render_report
        self.assertEqual(eth(1), '0.000000000000000001')
        self.assertEqual(eth(-10**18 - 1), '-1.000000000000000001')
        self.assertEqual(eth(None), 'Unknown')
        result = self.collect()
        result['sources']['publicnode']['status'] = '<script>alert(1)</script>'
        rendered = render_report(result)
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertIn('Current ownership unverified', rendered)

    def test_report_exposes_receipt_and_block_intervals(self):
        from bitpin.personal_whale_report import render_report
        self.collect(now=NOW - pw.DAY)
        result = self.collect()
        result['reference_24h_block_elapsed_seconds'] -= 1800
        rendered = render_report(result)
        self.assertIn('Receipt interval: 24.00 hours', rendered)
        self.assertIn('Block interval: 23.50 hours', rendered)


if __name__ == '__main__': unittest.main()
