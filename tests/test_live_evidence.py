"""Delivery evidence describes actual prompts, without publishing account data."""
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bitpin import analysis as an
from bitpin.brain import KimiBrain
from bitpin.llm import ConfigError, LLMError
from test_analysis import Bars, CFG, FakeClient, NOW, STRATS
from test_brain import FakeLLM, context, reply


def wallet_packet():
    return {'status': 'ok', 'collection_mode': 'shadow',
            'personal': {'status': 'ok', 'generated_at': NOW, 'received_at': NOW,
                         'available_wallets': 1, 'wallets': [{'status': 'ok', 'balance_wei': 10**18 + 1}]},
            'custody': {'status': 'unavailable'}, 'interpretation': 'supporting observations'}


class LiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def module(self):
        try:
            module = importlib.import_module('bitpin.live_evidence')
        except ImportError:
            module = None
        self.assertIsNotNone(module, 'actual prompt delivery tracking is missing')
        return module

    def builder(self, **cfg):
        return an.MarketContextBuilder(FakeClient(), dict(CFG, rule_signals=False, **cfg), state_dir=None,
                                      bars_source=Bars(), clock=lambda: NOW, strategies=STRATS)

    def test_wallet_settings_default_off_and_reject_other_files(self):
        cfg = an.validate_context_config({})
        self.assertIn('personal_whale_snapshot', cfg, 'wallet activation setting is missing')
        self.assertIsNone(cfg['personal_whale_snapshot'])
        self.assertIsNone(cfg['custody_whale_snapshot'])
        for key in ('personal_whale_snapshot', 'custody_whale_snapshot'):
            for path in ('relative.json', '/tmp/kimi.env', True, {}):
                with self.assertRaises(ConfigError):
                    an.validate_context_config({key: path})

    def test_opt_in_enters_real_prompt_and_preserves_exact_native_units(self):
        path = str(self.root / 'personal-whale-shadow.json')
        self.assertIn('personal_whale_snapshot', an.validate_context_config({}), 'wallet setting is missing')
        with mock.patch('bitpin.wallet_context.load_wallet_context', return_value=wallet_packet()), \
                mock.patch('bitpin.wallet_context.recheck_freshness', side_effect=lambda value, now: value):
            ctx = self.builder(personal_whale_snapshot=path).build(now=NOW)
        self.assertEqual(ctx['wallet_observations']['personal']['wallets'][0]['balance_wei'], 1000000000000000001)
        llm = FakeLLM([])
        brain = KimiBrain(llm, {}, str(self.root / 'brain'), clock=lambda: NOW)
        messages = brain.build_messages(ctx, {'USDT_IRT': 1}, now=NOW)
        self.assertIn('wallet_observations', messages[1]['content'])
        self.assertEqual(llm.calls, [])

    def test_optional_wallet_packet_cannot_crowd_out_portfolio(self):
        core = {'portfolio': {'equity_irt': 12}, 'symbols': {'BTC_IRT': {'px': 10}}}
        packet = dict(wallet_packet(), padding='x' * 3000)
        ctx = self.builder(max_context_chars=500)._shrink(dict(core, wallet_observations=packet))
        self.assertNotIn('wallet_observations', ctx, 'optional wallet context was not dropped')
        self.assertEqual(ctx['portfolio'], core['portfolio'])
        self.assertEqual(ctx['symbols'], core['symbols'])
        self.assertIn('wallet_observations', ctx['truncated'])

    def test_delivery_projection_does_not_expose_account_prices_or_raw_text(self):
        module = self.module()
        ctx = dict(context(), wallet_observations=wallet_packet(),
                   public_flow={'status': 'ok', 'markets': {'BTC_IRT': {'status': 'ready'}}},
                   defi_daily={'status': 'partial', 'available_sources': 6})
        ctx['raw_secret'] = 'DO NOT PUBLISH'
        doc = module.delivery_status(ctx, NOW, 'attempted')
        self.assertEqual(doc['sources']['wallet_observations']['usable_count'], 1)
        self.assertEqual(doc['sources']['public_flow']['usable_count'], 1)
        self.assertEqual(doc['sources']['defi_daily']['usable_count'], 6)
        serialized = json.dumps(doc)
        for forbidden in ('DO NOT PUBLISH', 'equity_irt', 'balance_wei', 'BTC_IRT', 'wallets', 'px'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(doc['quantitative_policy'], 'research_only')

    def test_absent_and_budget_omitted_have_distinct_delivery_states(self):
        doc = self.module().delivery_status({'truncated': ['wallet_observations']}, NOW, 'not_called')
        self.assertEqual(doc['sources']['wallet_observations']['presence'], 'omitted')
        self.assertEqual(doc['sources']['defi_daily']['presence'], 'absent')
        self.assertEqual(doc['sources']['defi_daily']['usable_count'], 0)

    def test_status_round_trip_is_bounded_strict_and_stale_aware(self):
        module = self.module()
        module.write_delivery_status(str(self.root), {}, NOW, 'attempted')
        path = self.root / 'live-evidence-status.json'
        self.assertEqual(module.read_delivery_status(str(path), NOW)['pipeline_stage'], 'attempted')
        self.assertEqual(module.read_delivery_status(str(path), NOW + 2 * 86400)['status'], 'stale')
        for content in ('{"schema_version":1,"schema_version":1}', '{"generated_at":NaN}', ' ' * 65537):
            path.write_text(content, encoding='utf-8')
            self.assertEqual(module.read_delivery_status(str(path), NOW)['status'], 'invalid')

    def test_invalid_delivery_shape_and_future_time_cannot_claim_received(self):
        module = self.module()
        doc = module.delivery_status({}, NOW, 'response_received')
        path = self.root / 'live-evidence-status.json'
        for mutation in (lambda d: d.update(generated_at=NOW + 1),
                         lambda d: d.update(pipeline_stage='live_active'),
                         lambda d: d['sources']['defi_daily'].update(usable_count=True),
                         lambda d: d['sources']['defi_daily'].update(presence='absent', usable_count=6)):
            data = copy.deepcopy(doc)
            mutation(data)
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertNotEqual(module.read_delivery_status(str(path), NOW)['status'], 'ok')

    def test_runtime_attempt_and_response_are_separate_and_no_extra_calls(self):
        self.module()
        llm = FakeLLM([reply({'USDT_IRT': 1})])
        brain = KimiBrain(llm, {'confirm_buys': False, 'p_rule': False,
                               'allowed_symbols': ['USDT_IRT', 'BTC_IRT', 'ETH_IRT']}, str(self.root), clock=lambda: NOW)
        ctx = context()
        result = brain.decide(ctx, {'USDT_IRT': 1})
        self.assertTrue(result.valid)
        self.assertEqual(len(llm.calls), 1)
        doc = json.loads((self.root / 'live-evidence-status.json').read_text(encoding='utf-8'))
        self.assertEqual(doc['pipeline_stage'], 'response_received')
        llm.replies.append(LLMError('offline failure'))
        brain.decide(ctx, {'USDT_IRT': 1})
        doc = json.loads((self.root / 'live-evidence-status.json').read_text(encoding='utf-8'))
        self.assertEqual(doc['pipeline_stage'], 'attempted')
        self.assertEqual(len(llm.calls), 2)

    def test_every_chat_rechecks_optional_freshness_without_changing_core(self):
        module = self.module()
        ctx = dict(context(), public_flow={'status': 'ok', 'generated_at': NOW - 89,
                    'markets': {'BTC_IRT': {'status': 'ready', 'as_of': NOW - 89,
                        'receipt_age_seconds': 89.0, 'daily': {'buy_quote': 123}}},
                    'liquidity': {'status': 'unavailable'}})
        llm = FakeLLM([{}, {}])
        tick = [NOW]
        brain = KimiBrain(llm, {}, str(self.root), clock=lambda: tick[0])
        messages = brain.build_messages(ctx, {'USDT_IRT': 1}, now=NOW)
        brain._chat_with_evidence(messages, ctx)
        self.assertIn('123', llm.calls[0]['messages'][1]['content'])
        tick[0] += 2
        brain._chat_with_evidence(messages, ctx)
        self.assertNotIn('buy_quote', llm.calls[1]['messages'][1]['content'])
        self.assertIn('80000', llm.calls[1]['messages'][1]['content'])
        self.assertIn('buy_quote', ctx['public_flow']['markets']['BTC_IRT']['daily'])
        self.assertEqual(module.read_delivery_status(str(self.root / 'live-evidence-status.json'), tick[0])
                         ['sources']['public_flow']['usable_count'], 0)


if __name__ == '__main__':
    unittest.main()
