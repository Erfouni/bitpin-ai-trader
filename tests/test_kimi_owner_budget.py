import json
import os
import tempfile
import unittest
from unittest.mock import patch
from bitpin import kimi_experiment as experiment
from tests.test_kimi_experiment import episode,completion


class OwnerBudgetTests(unittest.TestCase):
    def test_distinct_paid_states_share_one_cumulative_owner_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            grant=os.path.join(tmp,'grant')
            states=[os.path.join(tmp,'pilot1'),os.path.join(tmp,'pilot2')]
            for n,state in enumerate(states):
                cases=[episode('case-%s-%s'%(n,i), (n*20+i)*86400) for i in range(16)]
                experiment.prepare_experiment(cases,state,stability_repeats=1)
            with patch.object(experiment,'_owner_budget_directory',return_value=grant), patch.object(experiment,'_owner_transport',return_value=lambda p:completion()):
                first=experiment.execute_experiment(states[0],owner_execute=True)
                self.assertGreater(first['reserved_usd'],5)
                with self.assertRaises(experiment.ExperimentError):
                    experiment.execute_experiment(states[1],owner_execute=True)
                self.assertEqual(experiment.load_manifest(states[1])['fresh_status'],'not_run')
            with open(os.path.join(grant,'owner-budget.json'),encoding='utf-8') as f:
                self.assertEqual(json.load(f)['reserved_micro_usd'],round(first['reserved_usd']*1e6))

    def test_unknown_charge_kept_and_same_request_cannot_retry_in_another_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            states=[os.path.join(tmp,'pilot1'),os.path.join(tmp,'pilot2')]
            for state in states: experiment.prepare_experiment([episode()],state)
            def lost(payload): raise TimeoutError('synthetic only')
            with patch.object(experiment,'_owner_budget_directory',return_value=os.path.join(tmp,'grant')), patch.object(experiment,'_owner_transport',return_value=lost):
                result=experiment.execute_experiment(states[0],owner_execute=True,max_calls=1)
                self.assertTrue(result['halted'])
                with self.assertRaises(experiment.ExperimentError): experiment.execute_experiment(states[1],owner_execute=True,max_calls=1)


if __name__=='__main__': unittest.main()
