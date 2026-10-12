"""Synthetic offline contracts for optional supporting public evidence."""
import copy
import importlib
import json
import unittest


SYMBOLS = ['USDT_IRT', 'BTC_IRT', 'ETH_IRT', 'SOL_IRT', 'LINK_IRT', 'PAXG_IRT', 'AAPLX_IRT']


def fixture():
    return {'portfolio': {'equity_irt': 17, 'weights': {'USDT_IRT': 1}},
            'symbols': {symbol: {} for symbol in SYMBOLS},
            'public_flow': {'status': 'ok', 'markets': {
                'BTC_USDT': {'status': 'ready', 'quote_unit': 'USDT', 'as_of': 123456,
                             'daily': {'window_seconds': 86400, 'buy_quote': 300,
                                       'sell_quote': 100, 'delta_quote': 200,
                                       'buy_ratio': .75, 'trade_count': 4}}},
                'liquidity': {'status': 'unavailable'}},
            'defi_daily': {'status': 'partial', 'provider': 'DefiLlama', 'sources': {
                'tvl:global': {'status': 'ok', 'as_of': 86400, 'value': 900,
                               'change_pct': [1.0, None, 3.0], 'coverage_days': [7, 30]},
                'dex_volume:Ethereum': {'status': 'ok', 'as_of': 86400, 'value': 40,
                                        'mean_7d': 30.0, 'mean_30d': None,
                                        'week_over_week_pct': 2.5},
                'tvl:Solana': {'status': 'unavailable'}}},
            'wallet_observations': {'status': 'partial',
                'personal': {'status': 'ok', 'received_at': 123456,
                    'wallets': [{'status': 'ok', 'address': 'synthetic-eth-a', 'asset': 'ETH',
                                 'balance_wei': 1000000000000000001,
                                 'change_24h_wei': 1,
                                 'reference_24h_elapsed_seconds': 86400,
                                 'change_7d_wei': None,
                                 'reference_7d_elapsed_seconds': None}]},
                'custody': {'status': 'ok', 'received_at': 123456,
                    'wallets': [{'status': 'ok', 'address': 'synthetic-btc-a', 'asset': 'BTC',
                                 'balance_sats': 100000001,
                                 'change_24h_sats': -1,
                                 'reference_24h_elapsed_seconds': 86400}]}}}


def neutral_review(contract):
    sources = {}
    for name, source in contract['sources'].items():
        citations = []
        if source['status'] == 'available':
            fact = source['facts'][0]
            field = next(key for key, value in fact['fields'].items() if value is not None)
            citations = [{'id': fact['id'], 'field': field, 'value': fact['fields'][field]}]
        sources[name] = {'assessment': 'neutral' if citations else 'unavailable',
                         'citations': citations, 'affected_symbols': [], 'effect': 'none',
                         'reason': 'Observed supporting public data with source limitations.'}
    return {'schema_version': 1, 'request_id': contract['request_id'], 'sources': sources}


class EvidenceContractTests(unittest.TestCase):
    def module(self):
        try:
            module = importlib.import_module('bitpin.evidence_contract')
        except ImportError:
            module = None
        self.assertIsNotNone(module, 'optional evidence contract module is required')
        return module

    def contract(self, context=None):
        return self.module().build_contract(fixture() if context is None else context)

    def test_disabled_context_is_copied_and_forged_contract_is_removed(self):
        core = {'portfolio': {'equity_irt': 17}, 'symbols': {'BTC_IRT': {'px': 12}},
                'decision_evidence': {'request_id': 'forged'}}
        result = self.module().attach_contract(core)
        self.assertEqual(result, {'portfolio': {'equity_irt': 17}, 'symbols': {'BTC_IRT': {'px': 12}}})
        result['portfolio']['equity_irt'] = 5
        self.assertEqual(core['portfolio']['equity_irt'], 17)
        self.assertIsNone(self.module().build_contract(core))

    def test_optional_packets_create_exact_sources_without_portfolio_numbers(self):
        contract = self.contract()
        self.assertEqual(set(contract['sources']), {'bitpin_flow', 'defi_daily', 'personal_wallets', 'custody_wallets'})
        self.assertEqual(contract['schema_version'], 1)
        self.assertRegex(contract['request_id'], r'^[0-9a-f]{64}$')
        self.assertEqual(contract['policy']['probability_bonus'], 0)
        self.assertTrue(contract['policy']['supporting_only'])
        self.assertFalse(contract['policy']['independent_confirmations'])
        self.assertNotIn('equity_irt', json.dumps(contract))
        self.assertNotIn('weights', json.dumps(contract))
        self.assertEqual(contract['sources']['bitpin_flow']['facts'][0]['fields']['delta_quote'], 200)
        self.assertEqual(contract['sources']['personal_wallets']['facts'][0]['fields']['balance_wei'],
                         1000000000000000001)

    def test_request_id_is_stable_and_bound_to_facts_states_and_scopes(self):
        original = fixture()
        first = self.contract(original)
        reordered = copy.deepcopy(original)
        reordered['portfolio']['equity_irt'] = 99
        reordered['defi_daily']['sources'] = dict(reversed(list(reordered['defi_daily']['sources'].items())))
        self.assertEqual(self.contract(reordered)['request_id'], first['request_id'])
        reordered['public_flow']['markets']['BTC_USDT']['daily']['delta_quote'] = 201
        self.assertNotEqual(self.contract(reordered)['request_id'], first['request_id'])
        reordered = copy.deepcopy(original)
        reordered['wallet_observations']['custody']['status'] = 'unavailable'
        self.assertNotEqual(self.contract(reordered)['request_id'], first['request_id'])

    def test_absent_unavailable_and_budget_omission_are_distinct(self):
        contract = self.contract({'public_flow': {'status': 'unavailable'},
                                  'truncated': ['wallet_observations', 'defi_daily']})
        self.assertEqual(contract['sources']['bitpin_flow']['status'], 'unavailable')
        self.assertEqual(contract['sources']['defi_daily']['status'], 'omitted')
        self.assertEqual(contract['sources']['personal_wallets']['status'], 'omitted')
        self.assertEqual(contract['sources']['custody_wallets']['status'], 'omitted')
        for source in contract['sources'].values():
            self.assertEqual(source['facts'], [])
            self.assertTrue(source['limitations'])
        only_flow = self.contract({'public_flow': {'status': 'unavailable'}})
        self.assertEqual(only_flow['sources']['defi_daily']['status'], 'absent')

    def test_market_chain_and_global_scope_exclude_unrelated_assets(self):
        context = fixture()
        context['symbols']['LINK_IRT']['related_chains'] = ['Ethereum']
        contract = self.contract(context)
        flow = contract['sources']['bitpin_flow']['facts'][0]
        self.assertEqual(flow['allowed_symbols'], ['BTC_IRT'])
        facts = contract['sources']['defi_daily']['facts']
        global_fact = next(f for f in facts if f['scope']['network'] == 'global')
        chain_fact = next(f for f in facts if f['scope']['network'] == 'Ethereum')
        self.assertEqual(chain_fact['allowed_symbols'], ['ETH_IRT', 'LINK_IRT'])
        self.assertIn('USDT_IRT', global_fact['allowed_symbols'])
        self.assertNotIn('PAXG_IRT', global_fact['allowed_symbols'])
        self.assertNotIn('AAPLX_IRT', global_fact['allowed_symbols'])
        for source in ('personal_wallets', 'custody_wallets'):
            self.assertEqual(len(contract['sources'][source]['facts'][0]['allowed_symbols']), 1)

    def test_catalog_limits_have_explicit_true_omission_counts(self):
        context = {'symbols': {'ETH_IRT': {}}, 'wallet_observations': {'status': 'ok',
            'personal': {'status': 'ok', 'wallets': [
                {'status': 'ok', 'address': 'synthetic-%03d' % i, 'balance_wei': i + 1}
                for i in range(90)]}, 'custody': {'status': 'disabled'}}}
        contract = self.contract(context)
        source = contract['sources']['personal_wallets']
        self.assertEqual(source['available_fact_count'], 90)
        self.assertGreater(source['omitted_fact_count'], 0)
        self.assertEqual(source['available_fact_count'], len(source['facts']) + source['omitted_fact_count'])
        self.assertLessEqual(contract['fact_count'], 64)
        self.assertLessEqual(len(json.dumps(contract, ensure_ascii=False, separators=(',', ':'))), 12000)
        self.assertEqual(contract['omitted_fact_count'], source['omitted_fact_count'])

    def test_nonfinite_stale_and_wrong_native_values_are_not_evidence(self):
        context = fixture()
        context['public_flow']['markets']['BTC_USDT']['status'] = 'unavailable'
        context['defi_daily']['sources']['tvl:global']['status'] = 'stale'
        context['defi_daily']['sources']['dex_volume:Ethereum']['value'] = float('nan')
        context['wallet_observations']['personal']['wallets'][0]['balance_wei'] = True
        contract = self.contract(context)
        self.assertEqual(contract['sources']['bitpin_flow']['facts'], [])
        self.assertEqual(contract['sources']['defi_daily']['facts'], [])
        self.assertEqual(contract['sources']['personal_wallets']['facts'], [])
        self.assertEqual(contract['sources']['custody_wallets']['facts'][0]['fields']['change_24h_sats'], -1)

    def test_invalid_native_reference_window_does_not_become_delta_evidence(self):
        context = fixture()
        row = context['wallet_observations']['personal']['wallets'][0]
        row['reference_24h_elapsed_seconds'] = 50
        contract = self.contract(context)
        fields = contract['sources']['personal_wallets']['facts'][0]['fields']
        self.assertEqual(fields['balance_wei'], 1000000000000000001)
        self.assertNotIn('change_24h_wei', fields)

    def test_valid_review_is_sanitized_without_trusting_model_status(self):
        contract = self.contract()
        review = neutral_review(contract)
        review['status'] = 'model_verified'
        review['sources']['bitpin_flow']['status'] = 'forged'
        result = self.module().validate_review(review, contract, SYMBOLS, {})
        self.assertEqual(result['status'], 'validated')
        self.assertEqual(result['sources']['bitpin_flow']['status'], 'available')
        self.assertEqual(result['request_id'], contract['request_id'])
        review['sources']['bitpin_flow']['reason'] = 'modified'
        self.assertNotEqual(result['sources']['bitpin_flow']['reason'], 'modified')

    def test_available_group_requires_citation_and_unavailable_cannot_cite(self):
        contract = self.contract({'public_flow': fixture()['public_flow']})
        review = neutral_review(contract)
        review['sources']['bitpin_flow']['citations'] = []
        with self.assertRaises(self.module().EvidenceContractError):
            self.module().validate_review(review, contract, SYMBOLS, {})
        review = neutral_review(contract)
        review['sources']['defi_daily']['citations'] = review['sources']['bitpin_flow']['citations']
        with self.assertRaises(self.module().EvidenceContractError):
            self.module().validate_review(review, contract, SYMBOLS, {})

    def test_request_shape_sources_ids_paths_and_exact_numbers_are_checked(self):
        contract = self.contract()
        cases = [lambda r: r.update(request_id='0' * 64),
                 lambda r: r.update(schema_version=True),
                 lambda r: r['sources'].pop('defi_daily'),
                 lambda r: r['sources'].update(extra={}),
                 lambda r: r['sources']['bitpin_flow']['citations'][0].update(id='invented'),
                 lambda r: r['sources']['bitpin_flow']['citations'][0].update(field='daily.delta_quote'),
                 lambda r: r['sources']['bitpin_flow']['citations'][0].update(value=True),
                 lambda r: r['sources']['bitpin_flow']['citations'][0].update(value=86400.0),
                 lambda r: r['sources']['bitpin_flow']['citations'][0].update(value=float('nan')),
                 lambda r: r['sources']['bitpin_flow'].update(reason='short'),
                 lambda r: r['sources']['bitpin_flow'].update(reason='x' * 241)]
        for mutate in cases:
            review = neutral_review(contract)
            mutate(review)
            with self.subTest(mutation=cases.index(mutate)):
                with self.assertRaises(self.module().EvidenceContractError):
                    self.module().validate_review(review, contract, SYMBOLS, {})

    def test_dotted_list_index_citation_uses_exact_scalar_and_units(self):
        contract = self.contract()
        review = neutral_review(contract)
        fact = next(f for f in contract['sources']['defi_daily']['facts'] if f['scope']['network'] == 'global')
        review['sources']['defi_daily']['citations'] = [
            {'id': fact['id'], 'field': 'change_pct.1', 'value': None},
            {'id': fact['id'], 'field': 'value', 'value': 900}]
        self.assertEqual(fact['units']['change_pct.1'], 'percent')
        self.module().validate_review(review, contract, SYMBOLS, {})
        for field in ('change_pct', 'change_pct.01', 'change_pct.-1', 'change_pct.1.extra'):
            review['sources']['defi_daily']['citations'][0]['field'] = field
            with self.assertRaises(self.module().EvidenceContractError):
                self.module().validate_review(review, contract, SYMBOLS, {})
        review['sources']['defi_daily']['citations'][0].update(field='change_pct.1', unit='USD')
        with self.assertRaises(self.module().EvidenceContractError):
            self.module().validate_review(review, contract, SYMBOLS, {})

    def test_nonzero_effect_requires_scoped_affected_asset_and_candidate_reference(self):
        contract = self.contract()
        review = neutral_review(contract)
        row = review['sources']['bitpin_flow']
        row.update(assessment='supports', effect='supports_existing', affected_symbols=['BTC_IRT'])
        reference = row['citations'][0]['id']
        candidates = {'BTC_IRT': {'evidence_refs': [reference]}}
        result = self.module().validate_review(review, contract, SYMBOLS, candidates)
        self.assertEqual(result['sources']['bitpin_flow']['effect'], 'supports_existing')
        for affected, candidate in (([], candidates), (['ETH_IRT'], {'ETH_IRT': {'evidence_refs': [reference]}}),
                                    (['BTC_IRT'], {}), (['BTC_IRT'], {'BTC_IRT': {'evidence_refs': []}})):
            row['affected_symbols'] = affected
            with self.subTest(affected=affected, candidate=candidate):
                with self.assertRaises(self.module().EvidenceContractError):
                    self.module().validate_review(review, contract, SYMBOLS, candidate)

    def test_candidate_refs_cannot_be_unknown_uncited_duplicate_or_wrong_asset(self):
        contract = self.contract()
        review = neutral_review(contract)
        reference = review['sources']['bitpin_flow']['citations'][0]['id']
        uncited = next(f['id'] for f in contract['sources']['defi_daily']['facts']
                       if f['id'] != review['sources']['defi_daily']['citations'][0]['id'])
        for candidates in ({'BTC_IRT': {'evidence_refs': ['invented']}},
                           {'BTC_IRT': {'evidence_refs': [uncited]}},
                           {'ETH_IRT': {'evidence_refs': [reference]}},
                           {'BTC_IRT': {'evidence_refs': [reference, reference]}},
                           {'BTC_IRT': {'evidence_refs': reference}}):
            with self.subTest(candidates=candidates):
                with self.assertRaises(self.module().EvidenceContractError):
                    self.module().validate_review(review, contract, SYMBOLS, candidates)

    def test_neutral_global_citations_allow_gold_empty_refs_but_reject_unrelated_refs(self):
        context = fixture()
        context['symbols']['XAUT_IRT'] = {'cls': 'gold'}
        context['public_flow']['liquidity'] = {'status': 'ok', 'kind': 'stablecoin_supply_proxy',
            'received_at': 123456, 'assets': {'USDT': {'circulating_units': 1300,
                                                     'change_7d_units': 7}}}
        context['defi_daily']['sources']['stablecoin_supply:global'] = {
            'status': 'ok', 'as_of': 86400, 'value': 2700, 'change_pct': [1.0, 2.0, 3.0]}
        contract = self.contract(context)
        review = neutral_review(contract)
        flow = next(f for f in contract['sources']['bitpin_flow']['facts']
                    if f['scope']['kind'] == 'global_liquidity_proxy')
        defi = next(f for f in contract['sources']['defi_daily']['facts']
                    if f['scope']['metric'] == 'stablecoin_supply')
        review['sources']['bitpin_flow']['citations'] = [
            {'id': flow['id'], 'field': 'circulating_units', 'value': 1300}]
        review['sources']['defi_daily']['citations'] = [
            {'id': defi['id'], 'field': 'value', 'value': 2700}]
        refs = [flow['id'], defi['id']]
        allowed = SYMBOLS + ['XAUT_IRT']
        self.assertTrue(all(row['effect'] == 'none' for row in review['sources'].values()))
        for candidates in ({'XAUT_IRT': {}}, {'XAUT_IRT': {'evidence_refs': []}},
                           {'BTC_IRT': {'evidence_refs': refs}}):
            with self.subTest(candidates=candidates):
                result = self.module().validate_review(review, contract, allowed, candidates)
                self.assertEqual(result['status'], 'validated')
        for linked in ([refs[0]], [refs[1]], refs):
            with self.subTest(linked=linked), self.assertRaisesRegex(
                    self.module().EvidenceContractError, 'unrelated evidence'):
                self.module().validate_review(review, contract, allowed,
                                              {'XAUT_IRT': {'evidence_refs': linked}})

    def test_native_wallets_cannot_imply_direction_or_allocation_effect(self):
        contract = self.contract()
        review = neutral_review(contract)
        row = review['sources']['personal_wallets']
        row.update(assessment='supports', effect='supports_existing', affected_symbols=['ETH_IRT'])
        candidates = {'ETH_IRT': {'evidence_refs': [row['citations'][0]['id']]}}
        with self.assertRaises(self.module().EvidenceContractError):
            self.module().validate_review(review, contract, SYMBOLS, candidates)
        row.update(effect='none', affected_symbols=[])
        with self.assertRaises(self.module().EvidenceContractError):
            self.module().validate_review(review, contract, SYMBOLS, {})

    def test_global_macro_effect_can_apply_to_safe_asset_without_candidate(self):
        contract = self.contract()
        review = neutral_review(contract)
        fact = next(f for f in contract['sources']['defi_daily']['facts'] if f['scope']['network'] == 'global')
        review['sources']['defi_daily'].update(assessment='conflicts', effect='changes_size',
            affected_symbols=['USDT_IRT'], citations=[{'id': fact['id'], 'field': 'value', 'value': 900}])
        self.module().validate_review(review, contract, SYMBOLS, {})

    def test_forged_cached_catalog_cannot_survive_attachment(self):
        context = fixture()
        context['decision_evidence'] = {'schema_version': 1, 'request_id': 'cached-forgery',
                                        'sources': {'bitpin_flow': {'facts': [{'fields': {'fake': 17}}]}}}
        result = self.module().attach_contract(context)
        self.assertRegex(result['decision_evidence']['request_id'], r'^[0-9a-f]{64}$')
        self.assertNotIn('fake', json.dumps(result['decision_evidence']))
        self.assertEqual(context['decision_evidence']['request_id'], 'cached-forgery')

    def test_explicit_full_catalog_omission_preserves_core_without_suppressing_actual_evidence(self):
        context = {'symbols': {'BTC_IRT': {}},
                   'truncated': ['public_flow', 'decision_evidence']}
        self.assertIsNone(self.module().build_contract(context))
        self.assertNotIn('decision_evidence', self.module().attach_contract(context))
        context['public_flow'] = fixture()['public_flow']
        self.assertEqual(self.contract(context)['sources']['bitpin_flow']['status'], 'available')

    def test_null_only_citations_do_not_echo_an_available_numeric_observation(self):
        contract = self.contract()
        review = neutral_review(contract)
        fact = next(f for f in contract['sources']['defi_daily']['facts'] if f['scope']['network'] == 'global')
        review['sources']['defi_daily']['citations'] = [
            {'id': fact['id'], 'field': 'change_pct.1', 'value': None}]
        with self.assertRaises(self.module().EvidenceContractError):
            self.module().validate_review(review, contract, SYMBOLS, {})

    def test_duplicate_citation_paths_and_non_json_value_types_are_rejected(self):
        contract = self.contract()
        review = neutral_review(contract)
        row = review['sources']['bitpin_flow']
        row['citations'].append(copy.deepcopy(row['citations'][0]))
        with self.assertRaises(self.module().EvidenceContractError):
            self.module().validate_review(review, contract, SYMBOLS, {})
        for value in ('86400', [86400], {'value': 86400}, float('inf'), 10 ** 400):
            review = neutral_review(contract)
            review['sources']['bitpin_flow']['citations'][0]['value'] = value
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(self.module().EvidenceContractError):
                    self.module().validate_review(review, contract, SYMBOLS, {})

    def test_wallet_identity_and_request_digest_ignore_address_list_order(self):
        context = fixture()
        section = context['wallet_observations']['personal']
        section['wallets'].append({'status': 'ok', 'address': 'synthetic-eth-b',
                                   'asset': 'ETH', 'balance_wei': 1000000000000000002})
        first = self.contract(context)
        section['wallets'].reverse()
        second = self.contract(context)
        self.assertEqual(first['request_id'], second['request_id'])
        self.assertEqual(first['sources']['personal_wallets']['facts'], second['sources']['personal_wallets']['facts'])

    def test_explicit_liquidity_keeps_token_units_and_global_asset_scope(self):
        context = fixture()
        context['public_flow']['liquidity'] = {'status': 'ok', 'kind': 'stablecoin_supply_proxy',
            'received_at': 123456, 'assets': {'USDT': {'circulating_units': 1000,
                'change_1d_units': 10, 'change_7d_units': None, 'change_30d_units': -20},
                'USDC': {'circulating_units': 200, 'change_1d_units': None}}}
        facts = self.contract(context)['sources']['bitpin_flow']['facts']
        self.assertEqual(len(facts), 3)
        liquidity = next(f for f in facts if f['scope'].get('asset') == 'USDT')
        self.assertEqual(liquidity['fields']['change_30d_units'], -20)
        self.assertEqual(liquidity['units']['circulating_units'], 'USDT token units')
        self.assertIn('USDT_IRT', liquidity['allowed_symbols'])
        self.assertNotIn('PAXG_IRT', liquidity['allowed_symbols'])

    def test_changed_omitted_fact_invalidates_previous_request_id(self):
        context = {'symbols': {'ETH_IRT': {}}, 'wallet_observations': {'status': 'ok',
            'personal': {'status': 'ok', 'wallets': [
                {'status': 'ok', 'address': 'synthetic-%03d' % i, 'balance_wei': i + 1}
                for i in range(90)]}, 'custody': {'status': 'disabled'}}}
        first = self.contract(context)
        visible = {f['fields']['balance_wei'] for f in first['sources']['personal_wallets']['facts']}
        omitted = next(row for row in context['wallet_observations']['personal']['wallets']
                       if row['balance_wei'] not in visible)
        omitted['balance_wei'] += 1000
        second = self.contract(context)
        self.assertNotEqual(first['request_id'], second['request_id'])
        self.assertEqual(first['sources']['personal_wallets']['facts'], second['sources']['personal_wallets']['facts'])

    def test_arbitrum_defaults_to_native_eth_and_requires_explicit_arb_relation(self):
        context = {'symbols': {'ETH_IRT': {}, 'ARB_IRT': {}, 'PAXG_IRT': {}},
                   'defi_daily': {'status': 'partial', 'sources': {
                       'tvl:Arbitrum': {'status': 'ok', 'as_of': 86400, 'value': 900}}}}
        fact = self.contract(context)['sources']['defi_daily']['facts'][0]
        self.assertEqual(fact['allowed_symbols'], ['ETH_IRT'])
        context['symbols']['ARB_IRT']['related_chains'] = ['Arbitrum']
        context['symbols']['PAXG_IRT']['related_chains'] = ['Arbitrum']
        fact = self.contract(context)['sources']['defi_daily']['facts'][0]
        self.assertEqual(fact['allowed_symbols'], ['ARB_IRT', 'ETH_IRT'])

    def test_adaptive_caps_bound_catalog_and_preserve_native_types_and_true_counts(self):
        context = {'symbols': {'ETH_IRT': {}}, 'wallet_observations': {'status': 'ok',
            'personal': {'status': 'ok', 'wallets': [
                {'status': 'ok', 'address': 'synthetic-%03d' % i,
                 'balance_wei': 1000000000000000001 + i} for i in range(90)]},
            'custody': {'status': 'disabled'}}}
        for cap in (4000, 6000):
            context['evidence_catalog_max_chars'] = cap
            with self.subTest(cap=cap):
                result = self.module().attach_contract(context)
                contract = result['decision_evidence']
                self.assertEqual(contract.get('catalog_max_chars'), cap)
                self.assertLessEqual(len(json.dumps(contract, ensure_ascii=False, separators=(',', ':'))), cap)
                source = contract['sources']['personal_wallets']
                self.assertEqual(source['available_fact_count'], 90)
                self.assertEqual(source['available_fact_count'], len(source['facts']) + source['omitted_fact_count'])
                self.assertGreater(source['omitted_fact_count'], 0)
                self.assertTrue(source['facts'])
                self.assertTrue(all(type(f['fields']['balance_wei']) is int for f in source['facts']))
                self.assertTrue(all(1000000000000000001 <= f['fields']['balance_wei'] <= 1000000000000000090
                                    for f in source['facts']))
                self.assertEqual(result['wallet_observations'], context['wallet_observations'])

    def test_effective_catalog_cap_is_bound_to_request_id_even_when_all_facts_fit(self):
        context = {'public_flow': {'status': 'unavailable'}, 'evidence_catalog_max_chars': 4000}
        first = self.contract(context)
        context['evidence_catalog_max_chars'] = 6000
        second = self.contract(context)
        self.assertEqual(first['sources'], second['sources'])
        self.assertNotEqual(first['request_id'], second['request_id'])

    def test_invalid_catalog_caps_use_default_and_do_not_change_request_identity(self):
        context = fixture()
        default = self.contract(context)
        for invalid in (True, False, 3999, 12001, 6000.0, '6000', None, [], {}):
            context['evidence_catalog_max_chars'] = invalid
            with self.subTest(cap=invalid):
                contract = self.contract(context)
                self.assertEqual(contract.get('catalog_max_chars'), 12000)
                self.assertEqual(contract['request_id'], default['request_id'])

    def test_explicit_omission_marker_cannot_suppress_actual_evidence_under_adaptive_cap(self):
        context = fixture()
        context.update(evidence_catalog_max_chars=4000, truncated=['decision_evidence'])
        contract = self.contract(context)
        self.assertEqual(contract.get('catalog_max_chars'), 4000)
        self.assertGreater(contract['fact_count'], 0)
        self.assertEqual(contract['sources']['personal_wallets']['available_fact_count'], 1)
        self.assertEqual(contract['sources']['custody_wallets']['available_fact_count'], 1)
        self.assertLessEqual(len(json.dumps(contract, ensure_ascii=False, separators=(',', ':'))), 4000)


if __name__ == '__main__':
    unittest.main()
