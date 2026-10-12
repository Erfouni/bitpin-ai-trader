"""One bounded HTTP429 repeat preserves exact ETH quorum and the global budget."""
import copy
from email.utils import formatdate
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from bitpin import personal_whale as pw
from test_personal_whale import FakeClient, NOW


class Clock:
    def __init__(self):
        self.now = NOW
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class Opener:
    """Replace only public HTTP; keep the real client/parser/collector/store."""
    def __init__(self, clock, faults):
        self.clock, self.faults = clock, faults
        self.backend = FakeClient()
        self.backend.now = NOW
        self.events = []

    def open(self, request, timeout):
        provider = next(key for key, url in pw.PROVIDERS.items() if url == request.full_url)
        body = json.loads(request.data)
        method, params = body['method'], body['params']
        self.events.append((provider, method, copy.deepcopy(params), self.clock.time()))
        result = self.backend.call(provider, method, params)
        fault = self.faults.get(len(self.events))
        if isinstance(fault, Exception):
            raise fault
        if isinstance(fault, tuple):
            code, retry_after = fault
            headers = {} if retry_after is None else {'Retry-After': retry_after}
            raise urllib.error.HTTPError(request.full_url, code, 'synthetic public failure',
                                           headers, io.BytesIO(b'public error body'))
        if isinstance(fault, bytes):
            return io.BytesIO(fault)
        envelope = {'jsonrpc': '2.0', 'id': 1}
        envelope.update(error=fault) if isinstance(fault, dict) else envelope.update(result=result)
        return io.BytesIO(json.dumps(envelope).encode())


class ETHRPCRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = pw.PersonalStore(Path(self.tmp.name) / 'personal.sqlite3')
        self.clock = Clock()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def seed(self):
        client = FakeClient()
        client.now = NOW - 3600
        result = pw.PersonalCollector(self.store, client, clock=lambda: NOW - 3600,
                                       sleeper=lambda _: None).collect()
        self.assertEqual(result['status'], 'ok')

    def collector(self, faults):
        opener = Opener(self.clock, faults)
        collector = pw.PersonalCollector(self.store, pw.PersonalClient(opener),
                                         clock=self.clock.time, sleeper=self.clock.sleep)
        return collector, opener

    def assert_spacing(self, opener):
        for previous, current in zip(opener.events, opener.events[1:]):
            self.assertGreaterEqual(current[3] - previous[3], 2)

    def test_late_final_header_429_recovers_all_three_exact_wallets(self):
        self.seed()
        collector, opener = self.collector({16: (429, '60')})
        result = collector.collect()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['request_count'], 17)
        self.assertEqual([row['balance_wei'] for row in result['wallets']],
                         [224209000000000000000123, 1034946612288207873404, 8013538388781412542190])
        self.assertEqual(opener.events[15][:3], opener.events[16][:3])
        self.assertEqual(opener.events[15][0:2], ('drpc', 'eth_getBlockByNumber'))
        self.assertEqual(opener.events[16][3] - opener.events[15][3], 60)
        self.assertEqual(self.clock.sleeps, [2] * 15 + [60])
        self.assertEqual(self.store.cooldown('drpc'), 0)
        self.assertFalse(result['live_enabled'])
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['orders_created'], 0)
        self.assert_spacing(opener)

    def test_repeat_allowance_is_shared_across_providers_and_methods(self):
        collector, opener = self.collector({2: (429, '45'), 15: (429, '60')})
        result = collector.collect()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['request_count'], 15)
        self.assertEqual(len(opener.events), 15)
        self.assertEqual(self.clock.sleeps.count(45), 1)
        self.assertNotIn(60, self.clock.sleeps)
        self.assertEqual(result['sources']['drpc']['http_status'], 429)
        self.assertTrue(all(row['balance_wei'] is None for row in result['wallets']))
        self.assert_spacing(opener)

    def test_long_retry_after_is_not_slept_or_repeated(self):
        self.seed()
        collector, opener = self.collector({16: (429, '7200')})
        result = collector.collect()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['request_count'], 16)
        self.assertEqual(self.clock.sleeps, [2] * 15)
        self.assertEqual(self.store.cooldown('drpc'), self.clock.time() + 7200)
        self.assertTrue(all(row['balance_wei'] is None for row in result['wallets']))

    def test_retry_uses_twentieth_request_and_cannot_create_twenty_first(self):
        collector, opener = self.collector({19: (429, '60')})
        for _ in range(19):
            try:
                result = collector.call('drpc', 'eth_chainId', [])
            except pw.RPCFailure:
                self.fail('eligible HTTP429 must recover within the twentieth request')
            self.assertEqual(result, '0x1')
        self.assertEqual(collector.requests, 20)
        self.assertEqual(len(opener.events), 20)
        with self.assertRaises(pw.RPCFailure) as caught:
            collector.call('drpc', 'eth_chainId', [])
        self.assertEqual(caught.exception.kind, 'request_budget')
        self.assertEqual(len(opener.events), 20)
        self.assertEqual(self.clock.sleeps, [2] * 18 + [60])
        self.assert_spacing(opener)

    def test_429_on_twentieth_request_has_no_room_to_repeat(self):
        collector, opener = self.collector({20: (429, '60')})
        for _ in range(19):
            collector.call('drpc', 'eth_chainId', [])
        with self.assertRaises(pw.RPCFailure) as caught:
            collector.call('drpc', 'eth_chainId', [])
        self.assertEqual(caught.exception.kind, 'http_rate_limit')
        self.assertEqual(len(opener.events), 20)
        self.assertEqual(self.clock.sleeps, [2] * 19)

    def test_exhausted_rate_limit_hides_every_balance_and_persists_cooldown(self):
        self.seed()
        collector, opener = self.collector({16: (429, '60'), 17: (429, '60')})
        result = collector.collect()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['request_count'], 17)
        self.assertTrue(all(row['balance_wei'] is None for row in result['wallets']))
        self.assertIsNone(result['anchor'])
        self.assertEqual(result['sources']['drpc']['error_kind'], 'http_rate_limit')
        self.assertEqual(result['sources']['drpc']['retry_at'], self.clock.time() + 3600)
        self.assertEqual(self.store.cooldown('drpc'), self.clock.time() + 3600)
        self.assert_spacing(opener)
        before = len(opener.events)
        followup = pw.PersonalCollector(self.store, pw.PersonalClient(opener),
                                        clock=self.clock.time, sleeper=self.clock.sleep).collect()
        self.assertEqual(followup['sources']['drpc']['status'], 'cooldown')
        self.assertFalse(any(row[0] == 'drpc' for row in opener.events[before:]))
        self.assertTrue(all(row['balance_wei'] is None for row in followup['wallets']))

    def test_second_429_retains_its_longer_cooldown(self):
        self.seed()
        collector, opener = self.collector({16: (429, '60'), 17: (429, '7200')})
        result = collector.collect()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['sources']['drpc']['retry_at'], self.clock.time() + 7200)
        self.assertEqual(result['request_count'], 17)
        self.assertNotIn(7200, self.clock.sleeps)

    def test_parser_delay_is_used_for_short_header_and_missing_header_fallback(self):
        cases = (('45', 45), (None, 60), ('invalid', 60),
                 (formatdate(NOW + 45, usegmt=True), 45), ('0', 30))
        for header, expected_delay in cases:
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                store = pw.PersonalStore(Path(directory) / 'personal.sqlite3')
                try:
                    clock = Clock()
                    opener = Opener(clock, {1: (429, header)})
                    collector = pw.PersonalCollector(store, pw.PersonalClient(opener),
                                                     clock=clock.time, sleeper=clock.sleep)
                    with patch('bitpin.flow_public.time.time', clock.time):
                        try:
                            result = collector.call('drpc', 'eth_chainId', [])
                        except pw.RPCFailure:
                            self.fail('eligible HTTP429 must recover after its parsed short delay')
                    self.assertEqual(result, '0x1')
                    self.assertEqual(collector.requests, 2)
                    self.assertEqual(clock.sleeps, [expected_delay])
                    self.assert_spacing(opener)
                finally:
                    store.close()

    def test_unsupported_failures_never_consume_rate_limit_repeat(self):
        faults = (TimeoutError('synthetic timeout'), ssl.SSLCertVerificationError('synthetic TLS'),
                  (401, '60'), (503, '60'), b'not JSON',
                  {'code': -32005, 'message': 'synthetic RPC limit'},
                  {'code': -32603, 'message': 'synthetic RPC internal error'})
        for fault in faults:
            with self.subTest(fault=type(fault).__name__):
                collector, opener = self.collector({14: fault})
                result = collector.collect()
                self.assertEqual(result['status'], 'error')
                self.assertEqual(result['request_count'], 14)
                self.assertTrue(all(row['balance_wei'] is None for row in result['wallets']))
                self.assertEqual(len(opener.events), 14)
                self.assertEqual(self.clock.sleeps[-13:], [2] * 13)
                self.assert_spacing(opener)
                self.store.db.execute('DELETE FROM observations')
                self.store.db.execute('DELETE FROM cooldown')
                self.store.db.commit()


if __name__ == '__main__':
    unittest.main()
