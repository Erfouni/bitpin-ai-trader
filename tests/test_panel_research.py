# -*- coding: utf-8 -*-
"""Research view uses public receipts only; failures cannot look like live evidence."""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from bitpin.panel_auth import hash_password
from bitpin.panel_web import PanelApp, SESSION_COOKIE, CSP
from bitpin.panel_i18n import use

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location('research_helper_test', os.path.join(ROOT, 'scripts', 'panel_helper.py'))
ph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ph)
try:
    from bitpin import panel_research as research
except ImportError:
    research = None

NOW = 1791779000


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(research, 'public research collection/rendering is missing')
        self.tmp = tempfile.mkdtemp(prefix='research-panel-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = {}
        for key, name in [('flow', 'snapshot.json'), ('defi', 'defi-daily.json'),
                          ('whale', 'whale-shadow.json'), ('personal_whale', 'personal-whale-shadow.json'),
                          ('strategy', 'strategy-shadow.json')]:
            self.paths[key] = os.path.join(self.tmp, name)
        os.mkdir(os.path.join(self.tmp, 'docs'))
        self.results = os.path.join(self.tmp, 'docs', 'evidence-strategy-results.json')
        patcher = mock.patch.object(research, 'PUBLIC_PATHS', self.paths)
        patcher.start()
        self.addCleanup(patcher.stop)
        delivery_patcher = mock.patch.object(research, 'LIVE_STATUS_PATH',
            os.path.join(self.tmp, 'live-evidence-status.json'))
        delivery_patcher.start()
        self.addCleanup(delivery_patcher.stop)

    def write(self, key, doc):
        path = self.results if key == 'evaluation' else self.paths[key]
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)

    def collect(self):
        return research.collect(self.tmp, NOW)

    def test_personal_provider_projection_and_render_preserve_current_and_legacy_identity(self):
        for secondary in ('drpc', 'flashbots'):
            self.write('personal_whale', {'generated_at': NOW, 'received_at': NOW, 'status': 'ok',
                'sources': {'publicnode': {'status': 'ok'}, secondary: {'status': 'ok'}},
                'wallets': [{'address': research.WALLETS[0]['address'], 'status': 'ok', 'balance_wei': 123}]})
            with self.subTest(secondary=secondary):
                data = self.collect()
                self.assertEqual(data['wallets'][0]['provider_statuses'], {'publicnode': 'ok', secondary: 'ok'})
                with use('en'):
                    rendered = research.render(data)
                self.assertTrue(secondary in rendered)
                retired = 'flashbots' if secondary == 'drpc' else 'drpc'
                self.assertFalse(retired in rendered, 'wallet card shows a gateway that did not supply this receipt')

    def test_personal_provider_projection_rejects_mixed_or_unknown_gateway_sets(self):
        for secondary in ('unknown', 'three'):
            sources = {'publicnode': {'status': 'ok'}, 'drpc': {'status': 'ok'}}
            if secondary == 'unknown':
                sources['unknown'] = sources.pop('drpc')
            else:
                sources['flashbots'] = {'status': 'ok'}
            self.write('personal_whale', {'generated_at': NOW, 'received_at': NOW, 'status': 'ok', 'sources': sources})
            with self.subTest(secondary=secondary):
                row = self.collect()['wallets'][0]
                self.assertEqual(row['provider_statuses'], {})
                self.assertEqual(row['status'], 'invalid')
                self.assertIsNone(row['balance_wei'])

    def test_missing_delivery_receipt_is_unknown_without_changing_public_sources(self):
        data = self.collect()
        self.assertIn('live_delivery', data)
        self.assertEqual(data['live_delivery']['status'], 'missing')
        self.assertEqual(set(data['sources']), {'flow', 'defi', 'whale', 'personal_whale', 'strategy'})

    def test_delivery_collection_exposes_sanitized_transport_without_private_fields(self):
        with open(research.LIVE_STATUS_PATH, 'w', encoding='utf-8') as stream:
            json.dump({'schema_version': 1, 'generated_at': NOW, 'pipeline_stage': 'response_received',
                'quantitative_policy': 'research_only', 'account': 'must-not-leave-helper', 'sources': {
                    'public_flow': {'presence': 'included', 'status': 'partial', 'usable_count': 1},
                    'defi_daily': {'presence': 'omitted', 'status': 'absent', 'usable_count': 0},
                    'wallet_observations': {'presence': 'absent', 'status': 'absent', 'usable_count': 0}}}, stream)
        data = self.collect()
        self.assertEqual(data['live_delivery']['status'], 'ok')
        self.assertEqual(data['live_delivery']['pipeline_stage'], 'response_received')
        self.assertEqual(data['live_delivery']['sources']['public_flow']['usable_count'], 1)
        self.assertNotIn('must-not-leave-helper', json.dumps(data))

    def delivery_page(self, status='ok', stage='response_received', lang='en'):
        data = {'live_delivery': {'status': status, 'generated_at': NOW,
            'pipeline_stage': stage, 'quantitative_policy': 'research_only', 'sources': {
                'public_flow': {'presence': 'included', 'status': 'partial', 'usable_count': 1},
                'defi_daily': {'presence': 'omitted', 'status': 'absent', 'usable_count': 0},
                'wallet_observations': {'presence': 'absent', 'status': 'absent', 'usable_count': 0}}}}
        with use(lang):
            return research.render(data)

    def test_delivery_reports_actual_inclusion_separately_from_research_weights(self):
        page = self.delivery_page()
        self.assertIn('Last Kimi context delivery', page)
        self.assertIn('Response received', page)
        self.assertIn('Included in context', page)
        self.assertIn('Omitted from context', page)
        self.assertIn('Absent from context', page)
        self.assertIn('Usable observations', page)
        self.assertIn('2026-10-12', page)
        self.assertIn('does not prove the model acted on the facts or earned a profit', page)
        self.assertIn('These weights do not set Kimi', page)
        self.assertNotIn('New live influence off', page)
        self.assertNotIn('New live influence remains off', page)

    def test_missing_invalid_stale_and_uncalled_delivery_never_look_delivered(self):
        for status, stage in [('missing', None), ('invalid', None), ('error', None), ('stale', 'response_received'),
                              ('ok', 'not_called')]:
            page = self.delivery_page(status, stage)
            self.assertIn('Current delivery unknown' if status != 'ok' else 'Not submitted', page)
            self.assertIn('id="research-live-delivery"', page)
            delivery = page.split('id="research-live-delivery"', 1)[1].split('</section>', 1)[0]
            self.assertNotIn('research-status good', delivery, (status, stage))

    def test_delivery_omitted_and_zero_usable_groups_are_not_green(self):
        data = {'live_delivery': {'status': 'ok', 'generated_at': NOW, 'pipeline_stage': 'attempted',
            'sources': {'public_flow': {'presence': 'included', 'status': 'unavailable', 'usable_count': 0},
                        'defi_daily': {'presence': 'omitted', 'status': 'absent', 'usable_count': 0}}}}
        with use('en'):
            page = research.render(data)
        self.assertIn('id="research-live-delivery"', page)
        delivery = page.split('id="research-live-delivery"', 1)[1].split('</section>', 1)[0]
        self.assertIn('Request attempted', delivery)
        self.assertNotIn('research-status good', delivery)
        self.assertIn('No usable observations', delivery)

    def test_delivery_is_bilingual_and_has_no_owner_activation_control(self):
        page = self.delivery_page(lang='fa')
        for label in ('آخرین ارسال شواهد به کیمی', 'پاسخ دریافت شد', 'در متن تحلیل موجود',
                      'از متن تحلیل حذف شده', 'در متن تحلیل وجود ندارد'):
            self.assertIn(label, page)
        delivery = page.split('id="research-live-delivery"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn('<form', delivery)
        self.assertNotIn('<button', delivery)

    def test_missing_stale_future_and_malformed_are_explicit(self):
        self.assertEqual(self.collect()['sources']['flow']['status'], 'missing')
        self.write('flow', {'generated_at': NOW - 901, 'markets': {}})
        self.assertEqual(self.collect()['sources']['flow']['status'], 'stale')
        self.write('flow', {'generated_at': NOW + 1, 'markets': {}})
        self.assertEqual(self.collect()['sources']['flow']['status'], 'stale')
        with open(self.paths['flow'], 'w') as f:
            f.write('{"generated_at":NaN}')
        self.assertEqual(self.collect()['sources']['flow']['status'], 'invalid')
        with open(self.paths['flow'], 'w') as f:
            f.write('{"generated_at": %s, "generated_at": %s, "markets": {}}' % (NOW, NOW))
        self.assertEqual(self.collect()['sources']['flow']['status'], 'invalid')

    def test_huge_numeric_and_non_object_rows_remain_unavailable(self):
        self.write('flow', {'generated_at': 10**400, 'markets': {'BTC_USDT': []}})
        self.assertEqual(self.collect()['sources']['flow']['status'], 'stale')
        self.write('defi', {'generated_at': NOW, 'sources': {'tvl:global': []}})
        self.assertIsNone(self.collect()['defi'][0]['value'])
        self.write('strategy', {'generated_at': NOW, 'status': ['ok'], 'packets': {}})
        self.assertEqual(self.collect()['sources']['strategy']['status'], 'invalid')

    def test_out_of_cap_weight_is_unknown_instead_of_a_false_zero(self):
        self.write('strategy', {'generated_at': NOW, 'status': 'ok', 'packets': {
            'BTC_IRT': {'weights': {'flow': 0}}, 'ETH_IRT': {'weights': {'flow': .21}}}})
        self.assertIsNone(self.collect()['weights']['flow']['maximum'])

    def test_inconsistent_or_negative_daily_amounts_are_not_pressure_evidence(self):
        self.write('flow', {'generated_at': NOW, 'markets': {'BTC_USDT': {
            'quote_unit': 'USDT', 'trades': {'status': 'ok', 'received_at': NOW},
            'windows': {'86400': {'quality': 'ready', 'as_of': NOW, 'coverage_seconds': 86400,
                'buy_quote': -1, 'sell_quote': 10, 'pressure_ratio': .9, 'trade_count': 4}}}}})
        row = self.collect()['flow'][0]
        self.assertEqual(row['status'], 'invalid')
        self.assertIsNone(row['pressure_ratio'])

    def test_fixed_public_reads_exclude_injected_private_fields(self):
        self.write('flow', {'generated_at': NOW, 'markets': {}, 'secret': 'should-not-leave-helper'})
        self.write('strategy', {'generated_at': NOW, 'status': 'ok', 'packets': {
            'BTC_IRT': {'weights': {'flow': 0.1}, 'coverage': {'flow': 'eligible'}}}, 'receipt_count': 20})
        summary = self.collect()
        self.assertNotIn('should-not-leave-helper', json.dumps(summary))
        self.assertEqual(summary['weights']['flow']['maximum'], 0.1)
        self.assertEqual(summary['receipt_count'], 20)
        self.assertEqual(summary['sources']['strategy']['status'], 'ok')

    def test_closed_helper_command_rejects_request_paths_before_reading(self):
        helper = ph.Helper(ph.Paths(app_dir=self.tmp), clock=lambda: NOW,
                           run=lambda *a, **kw: self.fail('research must not run a process'))
        for args in ({'path': '/etc/bitpin-bot/bitpin-bot.env'}, {'currency': 'IRT'}, {'from': 0}):
            self.assertFalse(helper.handle({'cmd': 'research', 'args': args})['ok'])
        response = helper.handle({'cmd': 'research', 'args': {}})
        self.assertTrue(response['ok'], response)
        self.assertEqual(set(response['data']['sources']),
                         {'flow', 'defi', 'whale', 'personal_whale', 'strategy'})

    def test_oversized_and_symlink_files_are_not_consumed(self):
        with open(self.paths['flow'], 'wb') as f:
            f.write(b' ' * (research.MAX_BYTES + 1))
        self.assertEqual(self.collect()['sources']['flow']['status'], 'invalid')
        os.remove(self.paths['flow'])
        target = os.path.join(self.tmp, 'private.json')
        with open(target, 'w') as f:
            f.write('{"generated_at": %s, "markets": {}}' % NOW)
        try:
            os.symlink(target, self.paths['flow'])
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation unavailable')
        self.assertEqual(self.collect()['sources']['flow']['status'], 'invalid')

    def test_personal_wallets_use_catalog_exact_wei_and_unknown_history(self):
        address = '0xf6b6f07862a02c85628b3a9688beae07fea9c863'
        self.write('personal_whale', {'generated_at': NOW, 'received_at': NOW, 'status': 'ok', 'wallets': [
            {'address': address, 'person': '<script>fake</script>', 'status': 'ok',
             'balance_wei': 1000000000000000001, 'change_24h_wei': None}],
             'sources': {'publicnode': {'status': 'ok'}, 'flashbots': {'status': 'ok'}}})
        doc = self.collect()
        with use('en'):
            page = research.render(doc, 'IRT', 'holdout')
        self.assertIn('1.000000000000000001', page)
        self.assertIn('Patricio Worthalter', page)
        self.assertIn('Unknown history', page)
        self.assertNotIn('<script>', page)
        self.write('personal_whale', {'generated_at': NOW, 'received_at': NOW - 9001, 'status': 'ok',
                                     'wallets': [{'address': address, 'balance_wei': 10**18, 'status': 'ok'}]})
        self.assertIsNone(self.collect()['wallets'][1]['balance_wei'])

    def test_fresh_source_does_not_turn_missing_or_failed_wallet_into_fresh_evidence(self):
        for status in ('missing', 'error'):
            self.write('personal_whale', {'generated_at': NOW, 'received_at': NOW, 'status': 'ok', 'wallets': [
                {'address': '0xf6b6f07862a02c85628b3a9688beae07fea9c863', 'status': status,
                 'balance_wei': None}]})
            wallet = self.collect()['wallets'][1]
            self.assertEqual(wallet['status'], status)
            self.assertIsNone(wallet['balance_wei'])

    def test_daily_pressure_requires_ready_full_coverage_and_fresh_trade_receipt(self):
        def row(quality='ready', coverage=86400, received=NOW):
            return {'quote_unit': 'USDT', 'trades': {'status': 'ok', 'received_at': received},
                    'windows': {'86400': {'quality': quality, 'as_of': NOW, 'coverage_seconds': coverage,
                    'buy_quote': 800, 'sell_quote': 200, 'pressure_ratio': .6, 'trade_count': 10}}}
        self.write('flow', {'generated_at': NOW, 'markets': {'BTC_USDT': row(),
            'ETH_USDT': row('warming_up', 100), 'SOL_USDT': row(received=NOW - 901)}})
        rows = {r['market']: r for r in self.collect()['flow']}
        self.assertEqual(rows['BTC_USDT']['pressure_ratio'], .6)
        self.assertEqual(rows['ETH_USDT']['status'], 'warming_up')
        self.assertIsNone(rows['ETH_USDT']['pressure_ratio'])
        self.assertEqual(rows['SOL_USDT']['status'], 'stale')
        self.assertIsNone(rows['SOL_USDT']['buy_quote'])

    def test_every_collected_defi_indicator_is_visible_with_quality_and_units(self):
        self.write('defi', {'generated_at': NOW, 'sources': {
            'tvl:global': {'status': 'ok', 'received_at': NOW, 'metric': 'tvl', 'scope': 'global',
                'features': {'quality': 'ok', 'as_of': NOW - 86400, 'unit': 'USD', 'value': 9000000}},
            'fees:Ethereum': {'status': 'error', 'received_at': NOW, 'metric': 'fees', 'scope': 'Ethereum',
                'features': {'unit': 'USD', 'value': 1000000}}}})
        rows = self.collect()['defi']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['value'], 9000000)
        self.assertIsNone(rows[0]['value'])
        with use('fa'):
            page = research.render(self.collect(), 'IRT', 'holdout')
        self.assertIn('کارمزدها', page)
        self.assertIn('ارزش قفل‌شده', page)
        self.assertIn('USD', page)

    def test_stablecoin_unit_keeps_its_full_non_dollar_semantics(self):
        self.write('defi', {'generated_at': NOW, 'sources': {'stablecoin_supply:global': {
            'status': 'ok', 'received_at': NOW, 'metric': 'stablecoin_supply', 'scope': 'global',
            'features': {'quality': 'ok', 'as_of': NOW - 86400,
                         'unit': 'USD-pegged token units', 'value': 9000000}}}})
        self.assertEqual(self.collect()['defi'][0]['unit'], 'USD-pegged token units')

    def test_btc_custody_and_personal_eth_are_separate_and_exact(self):
        self.write('whale', {'generated_at': NOW, 'received_at': NOW, 'status': 'ok', 'wallets': [
            {'address': '1Kr6QSydW9bFQG1mXiPNNu6WpJGmUa9i1g', 'balance_sats': 100000001,
             'change_24h_sats': None}]})
        with use('en'):
            page = research.render(self.collect(), 'IRT', 'holdout')
        self.assertIn('1.00000001', page)
        self.assertIn('BTC custody addresses', page)
        self.assertIn('Publicly attributed ETH addresses', page)
        self.assertIn('Large-wallet observations', page)

    def test_results_selection_escaping_and_insufficient_kimi_are_visible(self):
        self.write('evaluation', {'generated_at': NOW, 'fresh_kimi_status': 'not_run',
            'quantitative': {'IRT': {'holdout': {'selected_variant': 'trend', 'episode_count': 540,
                'calendar_days': 180, 'metrics': {'baseline': {'accuracy': .4, 'brier': .67},
                'trend': {'accuracy': .41, 'brier': .66}},
                'comparators': {'trend': {'return': -.5}}}, 'development': {'metrics': {
                'baseline': {'accuracy': .6, 'brier': .5}}, 'episode_count': 100, 'calendar_days': 40}},
                'USDT': {'holdout': {'selected_variant': '<img src=x onerror=alert(1)>', 'metrics': {}}}},
            'recorded_kimi': {'recorded_decisions': 51, 'evaluated_candidates': 0},
            'pilot': {'first_pair_worst_case_usd': .384426, 'worst_case_usd': 6.53295}})
        doc = self.collect()
        with use('en'):
            page = research.render(doc, 'IRT', 'holdout')
            changed = research.render(doc, 'IRT', 'development')
            hostile = research.render(doc, 'USDT', 'holdout')
        self.assertIn('41.00%', page)
        self.assertIn('60.00%', changed)
        self.assertIn('Simulation, not live account loss', page)
        self.assertIn('Accuracy cannot be calculated', page)
        self.assertNotIn('<img src=x', hostile)
        self.assertNotIn('<script', page)
        self.assertNotIn(' style=', page)
        self.assertNotIn('<iframe', page)

    def test_actual_aggregate_comparators_keep_costs_and_distinct_endpoint_benchmark(self):
        # Shape and hand-checked figures from the frozen aggregate, not a rendering helper.
        self.write('evaluation', {'generated_at': NOW, 'quantitative': {'IRT': {'holdout': {
            'selected_variant': 'trend', 'metrics': {}, 'comparators': {'trend': {
                'cash_return': 0.0, 'currency': 'IRT', 'fee_per_side': .0035, 'slippage_per_side': .0005,
                'method': 'daily_independent_roundtrip_long_or_cash', 'stress_multiplier': 2,
                'return': -.5146439688930344, 'stress_return': -.8860011228842122,
                'gross_return': 1.0544642955166603, 'daily_equal_weight_long_return': -.5146439688930352,
                'daily_equal_weight_long_stress_return': -.8860011228842122,
                'daily_equal_weight_long_gross_return': 1.0544642955166603}},
            'USDT_buy_hold_benchmark': {'status': 'evaluated', 'currency': 'IRT', 'calendar_days': 180,
                'cost_per_side': .004, 'gross_return': .6876182760650384, 'return': .6741443317489353}}}}})
        doc = self.collect()
        self.assertIn('USDT_buy_hold_benchmark', doc['evaluation']['quantitative']['IRT']['holdout'])
        benchmark = doc['evaluation']['quantitative']['IRT']['holdout']['USDT_buy_hold_benchmark']
        self.assertEqual(benchmark['currency'], 'IRT')
        self.assertEqual(benchmark['cost_per_side'], .004)
        with use('en'):
            page = research.render(doc, 'IRT', 'holdout')
        for literal in ('0.350%', '0.050%', '67.41%', '-88.60%', 'Cash reference',
                        'Daily equal-weight reference', 'USDT buy and hold', 'Endpoint costs only'):
            self.assertIn(literal, page)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='research-route-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        class PublicHelper:
            def call(inner, cmd, actor=None, **args):
                if cmd != 'research' or args:
                    raise AssertionError('research may only use its no-argument public read')
                return {'sources': {}, 'wallets': [], 'weights': {}, 'evaluation': {}}
        self.app = PanelApp({'username': 'fixture', 'password_hash': hash_password('Test-1234', iterations=1000),
                             'audit_log': os.path.join(self.tmp, 'audit.jsonl')}, PublicHelper(), clock=lambda: NOW)

    def get(self, query='', signed_in=False, lang='en'):
        headers = [('Host', 'localhost'), ('Accept-Language', lang)]
        if signed_in:
            sid = self.app.sessions.create('fixture', '127.0.0.1', NOW)
            headers.append(('Cookie', '%s=%s' % (SESSION_COOKIE, sid)))
        return self.app.handle('GET', '/research', query, headers, b'', '127.0.0.1')

    def test_unauthenticated_request_redirects(self):
        status, headers, body = self.get()
        self.assertEqual(status, 303)
        self.assertIn(('Location', '/login'), headers)

    def test_packaged_persian_font_is_same_origin_binary(self):
        status, headers, body = self.app.handle('GET', '/static/fonts/Vazirmatn-Regular.woff2', '',
                                               [('Host', 'localhost')], b'', '127.0.0.1')
        self.assertEqual(status, 200)
        self.assertIn(('Content-Type', 'font/woff2'), headers)
        self.assertEqual(body[:4], b'wOF2')

    def test_authenticated_bilingual_route_and_server_side_selectors(self):
        status, headers, body = self.get('currency=USDT&period=development', True, 'fa')
        self.assertEqual(status, 200, body.decode('utf-8')[:300])
        page = body.decode('utf-8')
        self.assertIn('پژوهش و داده‌ها', page)
        self.assertIn('dir="rtl"', page)
        self.assertIn('value="USDT" selected', page)
        self.assertIn('value="development" selected', page)
        self.assertIn(('Content-Security-Policy', CSP), headers)
        self.assertNotIn('<script', page)

    def test_research_rejects_unknown_or_invalid_selectors(self):
        for query in ('path=/etc/bitpin-bot/bitpin-bot.env', 'currency=BTC', 'period=future'):
            self.assertEqual(self.get(query, True)[0], 400, query)

    def test_authenticated_post_cannot_trigger_research_actions(self):
        sid = self.app.sessions.create('fixture', '127.0.0.1', NOW)
        csrf = self.app.sessions.get(sid, NOW)['csrf']
        headers = [('Host', 'localhost'), ('Cookie', '%s=%s' % (SESSION_COOKIE, sid)),
                   ('Content-Type', 'application/x-www-form-urlencoded')]
        result = self.app.handle('POST', '/research', '', headers, ('csrf=' + csrf).encode(), '127.0.0.1')
        self.assertEqual(result[0], 404)


if __name__ == '__main__':
    unittest.main()
