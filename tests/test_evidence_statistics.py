import unittest
from bitpin.evidence_statistics import (normalize_probs, score_predictions,
    fit_influences, paired_block_bootstrap, reality_check)


class EvidenceStatisticsTests(unittest.TestCase):
    def test_exact_scores(self):
        result = score_predictions([{'label': 2, 'probs': [.1, .2, .7]},
                                    {'label': 0, 'probabilities': [.6, .3, .1]}])
        self.assertAlmostEqual(result['brier'], .2)
        self.assertEqual(result['accuracy'], 1)
        self.assertEqual(result['confusion_matrix'], [[1, 0, 0], [0, 0, 0], [0, 0, 1]])
        self.assertEqual(result['count'], 2)

    def test_invalid_probabilities_and_labels(self):
        for values in ([.1, .2, .6], [float('nan'), 0, 1], [True, 0, 0], [-.1, .1, 1], [1, 0]):
            with self.assertRaises(ValueError):
                normalize_probs(values)
        with self.assertRaises(ValueError):
            score_predictions([{'label': True, 'probs': [1, 0, 0]}])
        self.assertIsNone(score_predictions([])['brier'])

    def test_fit_mature_labels_caps_and_unavailable_source(self):
        rows = [{'time': i * 86400, 'label_end': (i + 1) * 86400,
                 'label': 2, 'base_probs': [1/3., 1/3., 1/3.],
                 'forecasts': {'technical': [0, 0, 1]}} for i in range(40)]
        result = fit_influences(rows, ['technical', 'whale'], {'technical': .5, 'whale': .05}, cutoff=39*86400)
        self.assertEqual(result['sample_count'], 39)
        self.assertEqual(result['max_label_end'], 39*86400)
        self.assertAlmostEqual(result['weights']['technical'], .5)
        self.assertEqual(result['weights']['whale'], 0)
        self.assertAlmostEqual(result['baseline_weight'], .5)
        bad = dict(rows[0], label_end=0)
        with self.assertRaises(ValueError):
            fit_influences([bad], ['technical'], {'technical': .5})

    def test_shrinkage_rejects_harmful_forecasts(self):
        rows = [{'time': i*86400, 'label_end': (i+1)*86400, 'label': 0,
                 'base_probs': [.8, .1, .1], 'forecasts': {'technical': [0, 0, 1]}}
                for i in range(60)]
        self.assertEqual(fit_influences(rows, ['technical'], {'technical': .5})['weights']['technical'], 0)

    def test_calendar_bootstrap_reproducible_with_gaps(self):
        rows = [{'time': i*86400, 'difference': .1} for i in list(range(20))+list(range(25,45))]
        result = paired_block_bootstrap(rows, repeats=100, seed=4)
        self.assertEqual(result, paired_block_bootstrap(rows, repeats=100, seed=4))
        self.assertAlmostEqual(result['low'], .1)
        self.assertAlmostEqual(result['high'], .1)
        self.assertEqual(result['missing_calendar_days'], 5)
        self.assertEqual(result['calendar_days'], 40)
        self.assertEqual(result['segments'], 2)
        with self.assertRaises(ValueError):
            paired_block_bootstrap(rows+[rows[0]])

    def test_family_diagnostic_and_small_sample(self):
        rows = [{'time': i*86400, 'differences': [.1, -.1]} for i in range(40)]
        result = reality_check(rows, repeats=99, seed=5)
        self.assertAlmostEqual(result['max_mean'], .1)
        self.assertAlmostEqual(result['p_value'], .01)
        self.assertEqual(result['candidates'], 2)
        self.assertEqual(paired_block_bootstrap([1, 2])['status'], 'inconclusive_small_sample')


if __name__ == '__main__':
    unittest.main()
