import json
from pathlib import Path
import tempfile
import unittest
from bitpin.strategy_shadow import StrategyStore, archive_snapshots, prepare_directory


class StrategyArchiveTests(unittest.TestCase):
    def test_first_observed_not_backdated_and_identical_receipt_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StrategyStore(Path(tmp)/'archive.sqlite3')
            snapshot = {'generated_at':10,'received_at':9,'status':'ok','anchor':{'timestamp':8},'balance_wei':123,'wallets':[]}
            a = archive_snapshots(store, {'personal_whale':snapshot},100)
            b = archive_snapshots(store, {'personal_whale':snapshot},110)
            row = b['sources']['personal_whale']
            self.assertEqual(row['first_observed_at'],100)
            self.assertEqual(store.receipt_count(),1)
            self.assertEqual(a['packets']['ETH_IRT']['weights']['whale'],0)
            self.assertFalse(b['live_enabled'])
            self.assertEqual(b['model_calls'],0)
            store.close()

    def test_new_failure_supersedes_old_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StrategyStore(Path(tmp)/'archive.sqlite3')
            good = {'status':'ok','generated_at':100,'received_at':100,'anchor':{'timestamp':100},'balance_wei':123}
            archive_snapshots(store,{'personal_whale':good},100)
            result = archive_snapshots(store,{'personal_whale':{'status':'error','generated_at':110,'received_at':110}},110)
            self.assertEqual(result['packets']['ETH_IRT']['coverage']['whale'],'error')
            store.close()

    def test_no_short_term_flow_is_promoted_to_daily_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StrategyStore(Path(tmp)/'archive.sqlite3')
            snapshot = {'generated_at':100,'markets':{'BTC_USDT':{'quote_unit':'USDT','trades':{'status':'ok','received_at':100},'windows':{'300':{'quality':'ready','as_of':100,'pressure_ratio':.9},'86400':{'quality':'warming_up','as_of':100}}}}}
            result = archive_snapshots(store,{'flow':snapshot},100)
            self.assertEqual(result['packets']['BTC_IRT']['weights']['flow'],0)
            self.assertEqual(result['packets']['BTC_IRT']['coverage']['flow'],'warming_up')
            store.close()

    def test_state_marker_prevents_reusing_other_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'own'
            self.assertEqual(prepare_directory(path),path.resolve())
            self.assertEqual(prepare_directory(path),path.resolve())
            other=Path(tmp)/'other'; other.mkdir(); (other/'unrelated').write_text('x')
            with self.assertRaises(ValueError): prepare_directory(other)

    def test_archive_event_and_receipt_cannot_be_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StrategyStore(Path(tmp)/'archive.sqlite3')
            archive_snapshots(store,{'flow':{'generated_at':100}},100)
            with self.assertRaises(ValueError): archive_snapshots(store,{'flow':{'generated_at':99}},99)
            store.close()


if __name__=='__main__': unittest.main()
