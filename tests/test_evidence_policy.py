import unittest
from bitpin.evidence_policy import assemble_packet, mixture, CAPS


class EvidencePolicyTests(unittest.TestCase):
    def observation(self, **changes):
        row = {'group': 'technical', 'source': 'public_bars', 'symbol': 'BTC_IRT',
               'fact_id': 'daily_prices', 'unit': 'IRT', 'event_time': 100,
               'received_at': 110, 'first_observed_at': 115, 'max_age': 100,
               'status': 'ok', 'metrics': {'return_1': .02}, 'sha256': 'a'*64}
        row.update(changes)
        return row

    def packet(self, observations, **changes):
        args = {'cutoff': 150, 'symbol': 'BTC_IRT', 'observations': observations,
                'baseline': [.3, .4, .3], 'forecasts': {'technical': {'target': 'next_24h_down_flat_up',
                'symbol':'BTC_IRT','return_currency':'IRT','decision_time':150,'label_end':150+86400,'received_at':150,
                'cost_band': .008, 'probabilities': [.1, .2, .7]}},
                'influence': {'weights': {'technical': .5}, 'max_label_end': 140,
                              'target':'next_24h_down_flat_up','cost_band':.008,'return_currency':'IRT',
                              'sample_count': 40, 'calibration_calendar_days': 40}}
        args.update(changes)
        return assemble_packet(**args)

    def test_eligible_evidence_and_hash(self):
        packet = self.packet([self.observation()])
        self.assertEqual(packet['coverage']['technical'], 'eligible')
        self.assertAlmostEqual(packet['probabilities'][2], .5)
        self.assertEqual(len(packet['packet_sha256']), 64)
        self.assertFalse(packet['live_enabled'])
        self.assertEqual(packet, self.packet([self.observation()]))

    def test_future_receipt_and_first_archive_observation_excluded(self):
        for changes in ({'received_at': 151}, {'first_observed_at': 151}, {'event_time': 151}):
            result = self.packet([self.observation(**changes)])
            self.assertEqual(result['weights']['technical'], 0)
            self.assertEqual(result['probabilities'], [.3, .4, .3])

    def test_latest_failure_never_resurrects_old_success(self):
        result = self.packet([self.observation(), self.observation(received_at=130, first_observed_at=130, status='disputed')])
        self.assertEqual(result['coverage']['technical'], 'disputed')
        self.assertEqual(result['weights']['technical'], 0)

    def test_stale_invalid_units_and_missing_hash(self):
        for changes in ({'max_age': 10}, {'unit': ''}, {'sha256': ''}, {'metrics': {'x': float('nan')}}):
            self.assertEqual(self.packet([self.observation(**changes)])['weights']['technical'], 0)

    def test_old_receipt_despite_long_event_age_and_invalid_event_time(self):
        self.assertEqual(self.packet([self.observation(max_age=200,receipt_max_age=10)])['weights']['technical'],0)
        result=self.packet([self.observation(event_time=float('nan'))])
        self.assertEqual(result['weights']['technical'],0)
        self.assertEqual(len(result['packet_sha256']),64)

    def test_duplicate_fact_is_one_observation(self):
        result = self.packet([self.observation(), self.observation()])
        self.assertEqual(len(result['evidence']), 1)
        self.assertEqual(result['weights']['technical'], .5)

    def test_mixed_targets_future_training_and_untrained_zero(self):
        forecast = {'technical': {'target': 'target_before_stop', 'cost_band': .008, 'probabilities': [.1, .2, .7]}}
        self.assertEqual(self.packet([self.observation()], forecasts=forecast)['weights']['technical'], 0)
        self.assertEqual(self.packet([self.observation()], influence={'weights': {'technical': .5}, 'max_label_end': 151, 'sample_count': 40, 'calibration_calendar_days':40})['weights']['technical'], 0)
        self.assertEqual(self.packet([self.observation()], influence={})['weights']['technical'], 0)

    def test_forecast_explicitly_for_other_asset_currency_or_future_time_is_excluded(self):
        for changes in ({'symbol':'ETH_IRT'},{'return_currency':'USDT'},{'decision_time':151},
                        {'received_at':151},{'label_end':151}, {'symbol':None}):
            forecast={'target':'next_24h_down_flat_up','cost_band':.008,'probabilities':[.1,.2,.7],
                      'symbol':'BTC_IRT','return_currency':'IRT','decision_time':150,'label_end':150+86400,'received_at':150}
            forecast.update(changes)
            self.assertEqual(self.packet([self.observation()],forecasts={'technical':forecast})['weights']['technical'],0)

    def test_missing_group_reverts_to_baseline_without_amplification(self):
        result = mixture([.3,.4,.3], {'technical': [.1,.2,.7]}, {'technical': .3, 'whale': .05}, CAPS)
        self.assertEqual(result['weights']['whale'], 0)
        self.assertAlmostEqual(result['baseline_weight'], .7)
        self.assertAlmostEqual(result['probabilities'][2], .42)
        with self.assertRaises(ValueError):
            mixture([1,0,0], {'whale':[0,0,1]}, {'whale':.06}, CAPS)


if __name__ == '__main__':
    unittest.main()
