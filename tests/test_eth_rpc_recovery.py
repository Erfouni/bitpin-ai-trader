"""Secondary public ETH gateway replacement keeps strict two-source evidence."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from bitpin import personal_whale as pw
from bitpin.personal_whale_report import render_report
from bitpin.wallet_context import load_wallet_context
from test_personal_whale import FakeClient, NOW
from test_wallet_context import personal


class ETHRPCRecovery(unittest.TestCase):
    def consume(self, doc):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / pw.MARKER).write_text(pw.MARKER_TEXT, encoding='utf-8')
            path = root / 'personal-whale-shadow.json'
            path.write_text(json.dumps(doc), encoding='utf-8')
            return load_wallet_context(str(path), None, NOW)['personal']

    def current(self):
        doc = personal()
        doc['sources']['drpc'] = doc['sources'].pop('flashbots')
        return doc

    def test_secondary_uses_documented_keyless_gateway_with_read_only_allowlist(self):
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":"0x1"}')
        client = pw.PersonalClient(opener=opener)
        self.assertIn('drpc', pw.PROVIDERS, 'replacement gateway is not approved yet')
        self.assertEqual(client.call('drpc', 'eth_getBalance', [pw.ADDRESS, '0x123']), '0x1')
        request = opener.open.call_args[0][0]
        self.assertEqual(request.full_url, 'https://eth.drpc.org')
        self.assertNotIn('Authorization', request.headers)
        self.assertEqual(json.loads(request.data)['params'], [pw.ADDRESS, '0x123'])
        for provider, method, params in (
                ('flashbots', 'eth_chainId', []), ('unknown', 'eth_chainId', []),
                ('drpc', 'eth_sendRawTransaction', ['0x123']),
                ('drpc', 'eth_getBalance', [pw.ADDRESS, 'latest'])):
            with self.subTest(provider=provider, method=method), self.assertRaises(ValueError):
                client.call(provider, method, params)

    def test_collector_labels_actual_gateways_and_keeps_request_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            store = pw.PersonalStore(Path(directory) / 'personal.sqlite3')
            try:
                client = FakeClient()
                client.now = NOW
                result = pw.PersonalCollector(store, client, clock=lambda: NOW, sleeper=lambda _: None).collect()
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(set(result['sources']), {'publicnode', 'drpc'})
                self.assertEqual({row[0] for row in client.calls}, {'publicnode', 'drpc'})
                self.assertEqual(result['request_count'], 14)
                self.assertEqual(len(result['wallets']), 3)
                self.assertFalse(result['live_enabled'])
                self.assertEqual(result['model_calls'], 0)
                self.assertEqual(result['orders_created'], 0)
            finally:
                store.close()

    def test_new_pair_enters_context_with_true_provider_identity(self):
        result = self.consume(self.current())
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['available_wallets'], 3)
        self.assertEqual(result['providers'], ['publicnode', 'drpc'])

    def test_legacy_successful_snapshot_remains_valid(self):
        result = self.consume(personal())
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['providers'], ['publicnode', 'flashbots'])

    def test_unknown_two_or_mixed_three_source_envelopes_are_excluded(self):
        for condition in ('unknown', 'three', 'no_primary'):
            doc = self.current()
            if condition == 'unknown':
                doc['sources']['unknown'] = doc['sources'].pop('drpc')
            elif condition == 'three':
                doc['sources']['flashbots'] = copy.deepcopy(doc['sources']['drpc'])
            else:
                doc['sources']['flashbots'] = doc['sources'].pop('publicnode')
            with self.subTest(condition=condition):
                result = self.consume(doc)
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['available_wallets'], 0)
                self.assertNotIn('wallets', result)

    def test_new_pair_failure_disagreement_and_staleness_hide_all_balances(self):
        for condition in ('failure', 'different_head', 'stale_anchor'):
            doc = self.current()
            if condition == 'failure':
                doc['sources']['drpc']['status'] = 'error'
            elif condition == 'different_head':
                doc['sources']['drpc']['finalized_height'] += 1000
            else:
                doc['anchor']['timestamp'] = NOW - 7201
            with self.subTest(condition=condition):
                result = self.consume(doc)
                self.assertEqual(result['status'], 'unavailable')
                self.assertNotIn('wallets', result)

    def test_report_names_gateway_that_supplied_the_receipt(self):
        rendered = render_report(self.current())
        self.assertTrue('<b>dRPC</b>' in rendered, 'new gateway label is missing')
        self.assertFalse('<b>Flashbots</b>' in rendered, 'new receipt incorrectly names retired gateway')
        self.assertTrue('<b>Flashbots</b>' in render_report(personal()), 'legacy source label changed')


if __name__ == '__main__':
    unittest.main()
