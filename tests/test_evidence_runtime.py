"""Actual prompt/validation/confirmation flow with synthetic public evidence, no API."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from bitpin.brain import KimiBrain, ValidationError, validate_response
from bitpin.live_evidence import HEADER, refresh_messages
from bitpin.evidence_contract import attach_contract
from bitpin.analysis import MarketContextBuilder, dumps
from bitpin.notify import decision_view
from test_brain import ALLOWED, BAL, T0, context, reply


def evidence_context():
    ctx = context()
    ctx['wallet_observations'] = {
        'status': 'ok', 'collection_mode': 'shadow',
        'personal': {'status': 'ok', 'generated_at': T0, 'received_at': T0,
                     'receipt_age_s': 0.0,
                     'anchor': {'number': 100, 'hash': '0x' + 'a' * 64, 'timestamp': int(T0 - 100)},
                     'available_wallets': 1, 'wallets': [
                         {'status': 'ok', 'address': '0x' + 'b' * 40, 'asset': 'ETH',
                          'balance_wei': 1000000000000000001}]},
        'custody': {'status': 'disabled', 'available_wallets': 0}}
    return ctx


def submitted_context(messages):
    text = messages[1]['content'].split(HEADER + '\n', 1)[1]
    return json.JSONDecoder().raw_decode(text)[0]


def neutral_review(contract):
    rows = {}
    for name, source in contract['sources'].items():
        citations = []
        if source['status'] == 'available':
            fact = source['facts'][0]
            field = next(k for k, v in fact['fields'].items() if type(v) in (int, float))
            citations = [{'id': fact['id'], 'field': field, 'value': fact['fields'][field]}]
        rows[name] = {'assessment': 'neutral' if citations else 'unavailable',
                      'citations': citations, 'affected_symbols': [], 'effect': 'none',
                      'reason': 'Observed native units do not establish a directional trading edge.'}
    return {'schema_version': 1, 'request_id': contract['request_id'], 'sources': rows}


class EvidenceReply:
    """The external boundary: echoes the exact contract it actually received."""
    model = 'offline-evidence-model'

    def __init__(self, replies=None, mutate=None, after=None):
        self.replies = list(replies or [reply({'USDT_IRT': 1})])
        self.mutate, self.after = mutate, after
        self.calls = []

    def redact(self, value):
        return value

    def chat(self, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        ctx = submitted_context(messages)
        contract = ctx.get('decision_evidence')
        if not contract:
            raise AssertionError('The actual submitted request has no evidence contract')
        obj = copy.deepcopy(self.replies.pop(0))
        obj.setdefault('analysis', {})['evidence_review'] = neutral_review(contract)
        if self.mutate:
            self.mutate(obj, contract)
        if self.after:
            self.after(len(self.calls))
        return {'content': json.dumps(obj), 'model': self.model, 'finish_reason': 'stop',
                'usage': {'prompt_tokens': 50, 'completion_tokens': 50, 'total_tokens': 100}}


class EvidenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def brain(self, llm, tick=None, **overrides):
        cfg = {'allowed_symbols': ALLOWED, 'confirm_buys': False, 'p_rule': False,
               'require_plan': False, 'max_validation_retries': 0}
        cfg.update(overrides)
        return KimiBrain(llm, cfg, str(self.root), clock=lambda: (tick or [T0])[0])

    def test_enabled_evidence_requires_a_review_even_when_all_targets_are_safe(self):
        # A permissive legacy parser must not silently accept an unreviewed USDT-only reply.
        with self.assertRaisesRegex(ValidationError, 'evidence_review'):
            validate_response(reply({'USDT_IRT': 1}), {'USDT_IRT': 1}, ALLOWED, 'USDT_IRT', BAL,
                              context=evidence_context(), now=T0)

    def test_real_prompt_contains_numeric_catalog_and_required_response_contract(self):
        brain = self.brain(EvidenceReply())
        messages = brain.build_messages(evidence_context(), {'USDT_IRT': 1}, now=T0)
        ctx = submitted_context(messages)
        self.assertIn('decision_evidence', ctx)
        source = ctx['decision_evidence']['sources']['personal_wallets']
        self.assertEqual(source['facts'][0]['fields']['balance_wei'], 1000000000000000001)
        self.assertIn('"evidence_review"', messages[0]['content'])
        self.assertNotIn('decision_evidence', evidence_context())

    def test_valid_review_survives_actual_decision_log_and_notify_projection(self):
        llm = EvidenceReply()
        brain = self.brain(llm)
        decision = brain.decide(evidence_context(), {'USDT_IRT': 1})
        self.assertTrue(decision.valid, decision.error)
        self.assertEqual(len(llm.calls), 1)
        review = decision.analysis['evidence_review']
        self.assertEqual(review['status'], 'validated')
        self.assertEqual(review['sources']['personal_wallets']['effect'], 'none')
        records = [json.loads(line) for line in Path(brain.log_path).read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[-1]['decision']['analysis']['evidence_review'], review)
        self.assertEqual(decision_view(records[-1])['evidence_review'], review)
        catalog = decision.analysis['evidence_catalog']
        self.assertEqual(catalog, submitted_context(llm.calls[0])['decision_evidence'])
        self.assertNotIn('evidence_catalog', decision_view(records[-1]))

    def test_wrong_request_id_is_not_a_valid_allocation_or_a_silent_fallback(self):
        llm = EvidenceReply(mutate=lambda obj, contract: obj['analysis']['evidence_review'].update(
            request_id='0' * 64))
        decision = self.brain(llm).decide(evidence_context(), {'USDT_IRT': 1})
        self.assertFalse(decision.valid)
        self.assertEqual(decision.error_kind, 'validation')
        self.assertEqual(len(llm.calls), 1)
        self.assertFalse(decision.fallback)

    def test_unknown_or_false_citation_is_rejected_without_adding_a_probability_bonus(self):
        def mutate(obj, contract):
            row = obj['analysis']['evidence_review']['sources']['personal_wallets']
            row['citations'][0]['value'] = 1000000000000000000
        decision = self.brain(EvidenceReply(mutate=mutate)).decide(evidence_context(), {'USDT_IRT': 1})
        self.assertFalse(decision.valid)
        self.assertIn('evidence', decision.error)
        self.assertEqual(decision.error_kind, 'validation')

    def test_second_vote_validates_its_own_freshness_and_preserves_both_reviews(self):
        ctx = evidence_context()
        ctx['public_flow'] = {'status': 'ok', 'generated_at': T0,
                              'markets': {'BTC_IRT': {'status': 'ready', 'as_of': T0, 'quote_unit': 'IRT',
                                         'receipt_age_seconds': 0.0,
                                         'daily': {'buy_quote': 123, 'sell_quote': 110}}},
                              'liquidity': {'status': 'unavailable'}}
        tick = [T0]
        llm = EvidenceReply([reply({'BTC_IRT': .10, 'USDT_IRT': .90}),
                             reply({'BTC_IRT': .05, 'USDT_IRT': .95})],
                            after=lambda calls: tick.__setitem__(0, T0 + 95))
        decision = self.brain(llm, tick, confirm_buys=True).decide(ctx, {'USDT_IRT': 1})
        self.assertTrue(decision.valid, decision.error)
        self.assertEqual(len(llm.calls), 2)
        self.assertAlmostEqual(decision.targets['BTC_IRT'], .05)
        first = submitted_context(llm.calls[0])['decision_evidence']
        second = submitted_context(llm.calls[1])['decision_evidence']
        self.assertEqual(second['sources']['bitpin_flow']['status'], 'unavailable')
        self.assertNotEqual(first['request_id'], second['request_id'])
        self.assertEqual(decision.analysis['evidence_review']['request_id'], first['request_id'])
        self.assertEqual(decision.confirmation['evidence_review']['request_id'], second['request_id'])
        self.assertEqual(decision.confirmation['evidence_catalog'], second)

    def test_context_without_opted_in_sources_remains_compatible(self):
        from test_brain import FakeLLM
        llm = FakeLLM([reply({'USDT_IRT': 1})])
        decision = self.brain(llm).decide(context(), {'USDT_IRT': 1})
        self.assertTrue(decision.valid, decision.error)

    def test_missing_review_is_strict_under_every_legacy_analysis_policy(self):
        for policy in (None, 'off', 'block', 'error'):
            with self.subTest(policy=policy), self.assertRaisesRegex(ValidationError, 'evidence_review'):
                validate_response(reply({'USDT_IRT': 1}), {'USDT_IRT': 1}, ALLOWED, 'USDT_IRT', BAL,
                                  context=evidence_context(), now=T0, analysis_policy=policy)

    def test_total_context_budget_counts_catalog_and_preserves_portfolio(self):
        # Exercise the real budget algorithm with a minimal synthetic core.
        ctx = evidence_context()
        ctx['symbols'] = {'ETH_IRT': {'px': 17}}
        ctx['portfolio'] = {'equity_irt': 17, 'weights': {'USDT_IRT': 1}}
        builder = object.__new__(MarketContextBuilder)
        for limit in (500, 4000):
            builder.cfg = {'max_context_chars': limit}
            result = builder._shrink(copy.deepcopy(ctx))
            self.assertLessEqual(len(dumps(result)), limit)
            self.assertEqual(result['portfolio'], ctx['portfolio'])
            self.assertEqual(result['symbols'], ctx['symbols'])
            self.assertEqual(result, attach_contract(result))
        builder.cfg = {'max_context_chars': 500}
        result = builder._shrink(copy.deepcopy(ctx))
        self.assertNotIn('wallet_observations', result)
        self.assertIn('decision_evidence', result['truncated'])
        self.assertNotIn('decision_evidence', result)

    def test_refresh_replaces_catalog_atomically_and_never_changes_core(self):
        ctx = evidence_context()
        brain = self.brain(EvidenceReply())
        messages = brain.build_messages(ctx, {'USDT_IRT': 1}, now=T0)
        output, refreshed = refresh_messages(messages, ctx, T0 + 100)
        self.assertLessEqual(len(dumps(refreshed)), len(dumps(attach_contract(ctx))))
        self.assertEqual(refreshed['portfolio'], ctx['portfolio'])
        self.assertEqual(refreshed['symbols'], ctx['symbols'])
        self.assertEqual(submitted_context(output), refreshed)
        for row in refreshed.get('decision_evidence', {}).get('sources', {}).values():
            if row['status'] != 'available':
                self.assertEqual(row['facts'], [])

    def test_budget_compacts_duplicate_wallet_policy_before_omitting_native_facts(self):
        from bitpin.wallet_context import INTERPRETATION
        ctx = evidence_context()
        ctx['wallet_observations']['interpretation'] = INTERPRETATION
        original = attach_contract(ctx)
        builder = object.__new__(MarketContextBuilder)
        builder.cfg = {'max_context_chars': len(dumps(original)) - 500}
        result = builder._shrink(ctx)
        self.assertIn('wallet_observations', result)
        self.assertEqual(result['decision_evidence'], original['decision_evidence'])
        self.assertIn('wallet_interpretation', result['truncated'])
        self.assertLessEqual(len(dumps(result)), builder.cfg['max_context_chars'])

    def test_context_budget_reduces_catalog_before_discarding_source_groups(self):
        from test_evidence_contract import fixture
        ctx = fixture()
        ctx['symbols'].update({'COIN%02d_IRT' % i: {} for i in range(35)})
        # Several eligible daily metrics create a useful catalog larger than its
        # smaller wire allowance; values are synthetic and sources stay unchanged.
        ctx['defi_daily']['sources'].update({
            name + ':global': {'status': 'ok', 'value': 100, 'as_of': 86400,
                              'change_pct': [1.0, 2.0, 3.0]}
            for name in ('fees', 'revenue', 'open_interest', 'stablecoin_supply')})
        original = attach_contract(ctx)
        builder = object.__new__(MarketContextBuilder)
        builder.cfg = {'max_context_chars': len(dumps(ctx)) + 6200}
        result = builder._shrink(copy.deepcopy(ctx))
        self.assertTrue(all(key in result for key in ('wallet_observations', 'defi_daily', 'public_flow')))
        self.assertIn('evidence_catalog_max_chars', result)
        self.assertLessEqual(len(dumps(result)), builder.cfg['max_context_chars'])
        self.assertEqual(result['portfolio'], original['portfolio'])
        self.assertEqual(result, attach_contract(result))

    def test_age_label_growth_reduces_catalog_before_dropping_fresh_wallet_source(self):
        ctx = evidence_context()
        wallets = ctx['wallet_observations']['personal']['wallets']
        wallets[:] = [dict(wallets[0], address='0x' + ('%040x' % (i + 1))) for i in range(10)]
        ctx['wallet_observations']['personal']['available_wallets'] = 10
        messages = self.brain(EvidenceReply()).build_messages(ctx, {'USDT_IRT': 1}, now=T0)
        _, refreshed = refresh_messages(messages, ctx, T0 + 100)
        self.assertIn('wallet_observations', refreshed)
        self.assertEqual(refreshed['decision_evidence']['sources']['personal_wallets']['status'], 'available')
        self.assertEqual(len(refreshed['wallet_observations']['personal']['wallets']), 10)
        self.assertLessEqual(len(dumps(refreshed)), len(dumps(attach_contract(ctx))))


if __name__ == '__main__':
    unittest.main()
