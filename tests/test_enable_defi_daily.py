"""Activation preparation never changes allocation, model or budget settings."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class EnableDailyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'deploy' / 'enable_defi_daily.py'
        spec = importlib.util.spec_from_file_location('enable_defi_daily', str(path))
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_only_context_snapshot_changes_and_comments_survive(self):
        raw = {'llm': {'model': 'kimi-k3'}, 'brain': {'confirm_buys': True},
               'context': {'max_context_chars': 36000, '_note': 'keep'}, 'news': {'enabled': False}}
        original = copy.deepcopy(raw)
        result = self.module.with_daily_context(raw, str(Path(tempfile.gettempdir()) / 'defi-daily.json'))
        self.assertEqual(raw, original)
        result['context'].pop('defi_daily_snapshot')
        self.assertEqual(result, original)

    def test_invalid_path_and_sections_are_rejected(self):
        for raw, path in [({}, 'relative/defi-daily.json'), ({'context': []}, '/tmp/defi-daily.json'),
                          ([], '/tmp/defi-daily.json'), ({}, '/tmp/kimi.env')]:
            with self.assertRaises(ValueError):
                self.module.with_daily_context(raw, path)

    def test_atomic_write_preserves_existing_file_mode(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'kimi.json'
            path.write_text('{}', encoding='utf-8')
            path.chmod(0o600)
            self.module.atomic_settings(path, {'llm': {'model': 'kimi-k3'},
                'context': {'defi_daily_snapshot': str(Path(root) / 'defi-daily.json')}})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['context']['defi_daily_snapshot'],
                             str(Path(root) / 'defi-daily.json'))
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ['kimi.json'])

    def test_atomic_rollback_restores_original_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / 'saved.json', Path(root) / 'current.json'
            source.write_text('{"original":true}\n', encoding='utf-8')
            target.write_text('{"new":true}', encoding='utf-8')
            self.module.atomic_restore(source, target)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(sorted(p.name for p in Path(root).iterdir()), ['current.json', 'saved.json'])

    def test_apply_and_rollback_require_a_stopped_service(self):
        for state in ('active', 'activating', 'reloading', 'deactivating'):
            with mock.patch.object(self.module.subprocess, 'check_output', return_value=state.encode()):
                with self.assertRaises(ValueError):
                    self.module.require_stopped()
        for state in ('inactive', 'failed'):
            with mock.patch.object(self.module.subprocess, 'check_output', return_value=state.encode()):
                self.module.require_stopped()


if __name__ == '__main__':
    unittest.main()
