"""Owner activation adds explicit data paths without changing trading policy."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class EnableWalletTests(unittest.TestCase):
    def module(self):
        path = ROOT / 'deploy' / 'enable_wallet_context.py'
        self.assertTrue(path.is_file(), 'owner wallet activation helper is missing')
        spec = importlib.util.spec_from_file_location('enable_wallet_context', str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_only_two_opt_in_paths_change_and_policy_and_notes_survive(self):
        module = self.module()
        raw = {'llm': {'model': 'kimi-k3'}, 'brain': {'confirm_buys': True},
               'context': {'_note': 'keep', 'max_context_chars': 36000}, 'news': {'enabled': False}}
        original = copy.deepcopy(raw)
        root = Path(tempfile.gettempdir())
        result = module.with_wallet_context(raw, str(root / 'personal-whale-shadow.json'), str(root / 'whale-shadow.json'))
        self.assertEqual(raw, original)
        self.assertEqual(result['context'].pop('personal_whale_snapshot'), str(root / 'personal-whale-shadow.json'))
        self.assertEqual(result['context'].pop('custody_whale_snapshot'), str(root / 'whale-shadow.json'))
        self.assertEqual(result, original)

    def test_invalid_paths_and_non_object_sections_are_rejected(self):
        module = self.module()
        for raw, personal, custody in (([], '/tmp/personal-whale-shadow.json', '/tmp/whale-shadow.json'),
                ({'context': []}, '/tmp/personal-whale-shadow.json', '/tmp/whale-shadow.json'),
                ({}, 'relative/personal-whale-shadow.json', '/tmp/whale-shadow.json'),
                ({}, '/tmp/kimi.env', '/tmp/whale-shadow.json')):
            with self.assertRaises(ValueError):
                module.with_wallet_context(raw, personal, custody)

    def test_setting_change_invalidates_live_confirmation_digest(self):
        module = self.module()
        spec = importlib.util.spec_from_file_location('wallet_run_bot', str(ROOT / 'scripts' / 'run_bot.py'))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        before = {'llm': {'model': 'kimi-k3'}, 'context': {}}
        root = Path(tempfile.gettempdir())
        after = module.with_wallet_context(before, str(root / 'personal-whale-shadow.json'), str(root / 'whale-shadow.json'))
        self.assertNotEqual(cli.live_digest({}, before), cli.live_digest({}, after))

    def test_apply_preserves_existing_settings_bytes_on_validation_failure(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'kimi.json'
            path.write_text('{"llm":{"model":"kimi-k3"}}\n', encoding='utf-8')
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                module.atomic_settings(path, {'llm': {'model': 'kimi-k3'}, 'context': {'personal_whale_snapshot': '/tmp/kimi.env'}})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual([p.name for p in Path(root).iterdir()], ['kimi.json'])


if __name__ == '__main__':
    unittest.main()
