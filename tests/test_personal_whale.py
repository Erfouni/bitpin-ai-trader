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
PATRICIO = '0xf6b6f07862a02c85628b3a9688beae07fea9c863'
ARTHUR = '0x6cd66dbdfe289ab83d7311b668ada83a12447e21'


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
        self.balances = {PATRICIO: 1034946612288207873404, ARTHUR: 8013538388781412542190}
        self.disagree_address = None

    def call(self, provider, method, params):
        self.calls.append((provider, method, copy.deepcopy(params)))
        if self.bad == provider or (self.late and provider == 'flashbots' and len(self.calls) >= 9):
            raise pw.PublicDataError('unavailable', 7200, 429)
        if method == 'eth_chainId': return '0x1'
        if method == 'eth_getBlockByNumber':
            height = 1000 + (self.now - (NOW - 7 * pw.DAY)) // 12 if params[0] == 'finalized' else int(params[0], 16)
            return head(number=height, digest=self.old_hash, timestamp=NOW - 7 * pw.DAY - 900 + (height - 1000) * 12)
        if method == 'eth_getBalance':
            if params[0] == pw.ADDRESS: balance = self.balance
            elif params[0] in self.balances: balance = self.balances[params[0]]
            else: raise AssertionError('unexpected wallet')
            mismatch = self.disagree or params[0] == self.disagree_address
            return hex(balance + (1 if mismatch and provider == 'flashbots' else 0))
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
        self.assertEqual(len(reads), 6)
        self.assertEqual({row[2][1] for row in reads}, {hex(51400)})
        for address in (pw.ADDRESS, PATRICIO, ARTHUR):
            self.assertEqual({row[0] for row in reads if row[2][0] == address}, {'publicnode', 'flashbots'})

    def test_cohort_exact_balances_and_vitalik_scalar_compatibility(self):
        result = self.collect()
        rows = {row['address']: row for row in result.get('wallets', [])}
        self.assertEqual(set(rows), {pw.ADDRESS, PATRICIO, ARTHUR})
        self.assertEqual(rows[PATRICIO]['balance_wei'], 1034946612288207873404)
        self.assertEqual(rows[ARTHUR]['balance_wei'], 8013538388781412542190)
        self.assertEqual(rows[pw.ADDRESS]['balance_wei'], result['balance_wei'])
        self.assertTrue(all(row['current_ownership_verified'] is False for row in rows.values()))

    def test_one_new_wallet_dispute_invalidates_entire_observation(self):
        self.client.disagree_address = PATRICIO
        result = self.collect()
        self.assertEqual(result['status'], 'disputed')
        self.assertIsNone(result['balance_wei'])
        self.assertEqual(len(result.get('wallets', [])), 3)
        self.assertTrue(all(row['balance_wei'] is None for row in result['wallets']))
        self.assertTrue(all(row['change_24h_wei'] is None for row in result['wallets']))

    def test_three_wallet_followup_is_bounded_and_spaced(self):
        self.collect()
        self.client.calls = []
        delays = []
        result = pw.PersonalCollector(self.store, self.client, clock=lambda: NOW,
                                     sleeper=delays.append).collect()
        self.assertEqual(result['request_count'], 16)
        self.assertEqual(delays, [2] * 15)

    def test_legacy_balance_is_not_fabricated_for_new_profiles(self):
        self.store.save({'status': 'ok', 'received_at': NOW - pw.DAY, 'epoch': 0,
                         'sources': {}, 'balance_wei': 224209 * 10**18 + 100,
                         'anchor': {'number': 44200, 'hash': HASH, 'timestamp': NOW - pw.DAY - 900}})
        legacy = self.store.snapshot(NOW - pw.DAY)
        self.assertEqual(len(legacy.get('wallets', [])), 3)
        self.assertEqual(legacy['wallets'][0]['balance_wei'], 224209 * 10**18 + 100)
        self.assertIsNone(legacy['wallets'][1]['balance_wei'])
        self.assertIsNone(legacy['wallets'][2]['balance_wei'])
        result = self.collect()
        self.assertEqual(result['change_24h_wei'], 23)
        for row in result['wallets'][1:]:
            self.assertIsNone(row['change_24h_wei'])
            self.assertIsNone(row['reference_24h_balance_wei'])
            self.assertIsNone(row['reference_24h_elapsed_seconds'])
            self.assertIsNone(row['reference_24h_block_elapsed_seconds'])

    def test_each_wallet_uses_its_own_observed_reference(self):
        self.collect(now=NOW - pw.DAY)
        self.client.balance += 17
        self.client.balances[PATRICIO] += 19
        self.client.balances[ARTHUR] -= 31
        result = self.collect()
        rows = {row['address']: row for row in result.get('wallets', [])}
        self.assertIn(PATRICIO, rows)
        self.assertEqual(rows[PATRICIO]['change_24h_wei'], 19)
        self.assertEqual(rows[ARTHUR]['change_24h_wei'], -31)
        self.assertEqual(rows[pw.ADDRESS]['change_24h_wei'], 17)
        self.assertEqual(rows[ARTHUR]['reference_24h_balance_wei'], 8013538388781412542190)
        self.assertEqual(rows[ARTHUR]['reference_24h_received_at'], NOW - pw.DAY)
        self.assertEqual(rows[ARTHUR]['reference_24h_elapsed_seconds'], 86400)
        self.assertEqual(rows[ARTHUR]['reference_24h_block_elapsed_seconds'], 86400)

    def test_new_wallet_zero_reference_remains_a_real_measured_zero(self):
        self.client.balances[PATRICIO] = 0
        self.collect(now=NOW - pw.DAY)
        self.client.balances[PATRICIO] = 1
        result = self.collect()
        rows = {row['address']: row for row in result.get('wallets', [])}
        self.assertIn(PATRICIO, rows)
        self.assertEqual(rows[PATRICIO]['reference_24h_balance_wei'], 0)
        self.assertEqual(rows[PATRICIO]['change_24h_wei'], 1)

    def test_wallet_reference_skips_missing_roster_members_within_tolerance(self):
        self.client.now = NOW - pw.DAY - 1800
        self.collect(now=self.client.now)
        self.store.save({'status': 'ok', 'received_at': NOW - pw.DAY, 'epoch': 0,
                         'sources': {}, 'balance_wei': 224209 * 10**18 + 100,
                         'anchor': {'number': 44200, 'hash': HASH, 'timestamp': NOW - pw.DAY - 900}})
        self.client.balances[PATRICIO] += 5
        result = self.collect()
        row = result['wallets'][1]
        self.assertEqual(row['change_24h_wei'], 5)
        self.assertEqual(row['reference_24h_elapsed_seconds'], 88200)
        self.assertEqual(result['change_24h_wei'], 23)
        self.assertEqual(result['reference_24h_elapsed_seconds'], 86400)

    def test_malformed_added_balance_hides_every_wallet(self):
        original = self.client.call
        def malformed(provider, method, params):
            value = original(provider, method, params)
            return '0x01' if method == 'eth_getBalance' and params[0] == ARTHUR else value
        self.client.call = malformed
        result = self.collect()
        self.assertEqual(result['status'], 'error')
        self.assertTrue(all(row['balance_wei'] is None for row in result['wallets']))
        self.assertGreaterEqual(self.store.cooldown('publicnode'), NOW + 3600)

    def test_reviewed_catalog_is_immutable_and_snapshot_edits_cannot_expand_rpc_access(self):
        from bitpin.personal_wallets import WALLETS
        with self.assertRaises(TypeError): WALLETS[1]['address'] = '0x' + '0' * 40
        with self.assertRaises(TypeError): WALLETS[1]['attribution']['current_ownership_verified'] = True
        result = self.collect()
        result['wallets'][1]['address'] = '0x' + '0' * 40
        result['wallets'][1]['attribution']['current_ownership_verified'] = True
        self.assertFalse(self.store.snapshot(NOW)['wallets'][1]['attribution']['current_ownership_verified'])
        with self.assertRaises(ValueError):
            pw.PersonalClient(opener=unittest.mock.Mock()).call('publicnode', 'eth_getBalance',
                                                             [result['wallets'][1]['address'], '0x1'])

    def test_rpc_accepts_only_reviewed_added_addresses_at_explicit_height(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = lambda *a, **k: io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":"0x1"}')
        for address in (PATRICIO, ARTHUR):
            self.assertEqual(pw.PersonalClient(opener).call('publicnode', 'eth_getBalance', [address, '0x1']), '0x1')
            self.assertEqual(json.loads(opener.open.call_args[0][0].data)['params'], [address, '0x1'])

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
        result['wallets'][0]['reference_24h_block_elapsed_seconds'] -= 1800
        rendered = render_report(result)
        self.assertIn('Receipt interval: 24.00 hours', rendered)
        self.assertIn('Block interval: 23.50 hours', rendered)

    def test_report_discloses_all_profiles_and_individual_exact_values(self):
        from bitpin.personal_whale_report import render_report
        self.collect(now=NOW - pw.DAY)
        self.client.balances[PATRICIO] += 19
        self.client.balances[ARTHUR] -= 31
        rendered = render_report(self.collect())
        self.assertIn('Patricio Worthalter', rendered)
        self.assertIn('Arthur Hayes', rendered)
        self.assertIn('1,034.946612288207873423 ETH', rendered)
        self.assertIn('8,013.538388781412542159 ETH', rendered)
        self.assertIn('+0.000000000000000019', rendered)
        self.assertIn('-0.000000000000000031', rendered)
        self.assertIn('Current control and whether funds are personal remain unverified', rendered)
        self.assertIn('personal versus fund capital is ambiguous', rendered)
        self.assertIn('No self-disclosure found', rendered)
        self.assertIn('transparency-report-initial-foundation-setup', rendered)

    def test_report_legacy_scalar_has_unknown_added_balances(self):
        from bitpin.personal_whale_report import render_report
        result = self.collect()
        del result['wallets']
        rendered = render_report(result)
        self.assertIn('224,209.000000000000000123 ETH', rendered)
        self.assertIn('Patricio Worthalter', rendered)
        self.assertIn('Arthur Hayes', rendered)
        self.assertNotIn('1,034.946612288207873404 ETH', rendered)

    def test_report_failure_cannot_reveal_stale_wallet_metrics(self):
        from bitpin.personal_whale_report import render_report
        result = self.collect()
        result['status'] = 'disputed'
        rendered = render_report(result)
        self.assertNotIn('1,034.946612288207873404', rendered)
        self.assertNotIn('224,209.000000000000000123', rendered)
        self.assertIn('Patricio Worthalter', rendered)


if __name__ == '__main__': unittest.main()
