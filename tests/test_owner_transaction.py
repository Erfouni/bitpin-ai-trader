"""Owner activation shares the panel mutation transaction lock."""
import contextlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

try:
    import fcntl
except ImportError:
    fcntl = None

ROOT = Path(__file__).resolve().parents[1]


def activation_module():
    spec = importlib.util.spec_from_file_location('locked_wallet_activation',
        str(ROOT / 'deploy' / 'enable_wallet_context.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ActivationBoundaryTests(unittest.TestCase):
    def test_direct_apply_locks_before_loading_or_changing_settings(self):
        module = activation_module()
        held = []

        @contextlib.contextmanager
        def transaction():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def load(path):
            self.assertTrue(held, 'settings were read before the transaction lock')
            return {'context': {}}

        atomic_settings = module.atomic_settings

        def write(path, doc):
            self.assertTrue(held, 'settings write escaped the transaction lock')
            return atomic_settings(path, doc)

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'kimi.json'
            path.write_text('{"llm":{"model":"kimi-k3"},"context":{}}', encoding='utf-8')
            personal, custody = str(Path(root) / 'personal-whale-shadow.json'), str(Path(root) / 'whale-shadow.json')
            with mock.patch.object(module, 'owner_transaction', transaction, create=True), \
                    mock.patch.object(module, 'load_kimi_config', load), \
                    mock.patch.object(module, 'load_wallet_context', return_value={'personal': {'available_wallets': 1}}), \
                    mock.patch.object(module, 'require_stopped'), \
                    mock.patch.object(module, 'atomic_settings', write), \
                    mock.patch.object(module.os, 'geteuid', return_value=0, create=True), \
                    mock.patch.object(sys, 'argv', ['enable_wallet_context', '--apply', '--kimi-config', str(path),
                        '--personal-snapshot', personal, '--custody-snapshot', custody]):
                self.assertEqual(module.main(), 0)
                import json
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['context'],
                    {'personal_whale_snapshot': personal, 'custody_whale_snapshot': custody})
                self.assertFalse(held)

    def test_direct_rollback_locks_before_stopped_check(self):
        module = activation_module()
        held = []

        @contextlib.contextmanager
        def transaction():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def stopped():
            self.assertTrue(held, 'rollback started outside the transaction lock')
            raise ValueError('controlled stop-check exit')

        with mock.patch.object(module, 'owner_transaction', transaction, create=True), \
                mock.patch.object(module, 'require_stopped', stopped), \
                mock.patch.object(module.os, 'geteuid', return_value=0, create=True), \
                mock.patch.object(sys, 'argv', ['enable_wallet_context', '--restore-from', '/unused/offline-fixture']):
            with self.assertRaisesRegex(ValueError, 'controlled stop-check exit'):
                module.main()
        self.assertFalse(held)

    def test_unavailable_flock_fails_closed_without_reading_files(self):
        path = ROOT / 'deploy' / 'owner_transaction.py'
        self.assertTrue(path.is_file(), 'owner transaction lock implementation is missing')
        from deploy import owner_transaction as owner
        with mock.patch.object(owner, 'fcntl', None), mock.patch.object(owner.os, 'open') as opened:
            with self.assertRaisesRegex(ValueError, 'Linux flock'):
                with owner.owner_transaction():
                    self.fail('unsupported platform admitted a mutation')
            opened.assert_not_called()


@unittest.skipUnless(fcntl is not None and hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'Linux root-only transaction integration')
class LinuxOwnerTransactionTests(unittest.TestCase):
    def setUp(self):
        from deploy import owner_transaction as owner
        self.owner = owner
        self.tmp = tempfile.TemporaryDirectory(prefix='bitpin-owner-lock-test-', dir='/run')
        self.addCleanup(self.tmp.cleanup)
        self.lock = os.path.join(self.tmp.name, 'bitpin-panel', 'helper.lock')
        patcher = mock.patch.object(owner, 'LOCK_PATH', self.lock)
        patcher.start()
        self.addCleanup(patcher.stop)
        env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop(owner.FD_ENV, None)

    def child(self, inherited_fd=None):
        script = ('import os,sys; from deploy import owner_transaction as owner; '
                  'owner.LOCK_PATH=sys.argv[1]\n'
                  'try:\n'
                  ' with owner.owner_transaction(timeout=.15): print("acquired")\n'
                  'except TimeoutError: print("blocked")\n')
        env = dict(os.environ)
        if inherited_fd is None:
            env.pop(self.owner.FD_ENV, None)
        else:
            env[self.owner.FD_ENV] = str(inherited_fd)
        return subprocess.run([sys.executable, '-c', script, self.lock], cwd=str(ROOT), env=env,
            pass_fds=() if inherited_fd is None else (inherited_fd,), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=5, check=True).stdout.strip()

    def test_separate_owner_or_panel_process_is_excluded_for_whole_scope(self):
        with self.owner.owner_transaction(timeout=0):
            self.assertEqual(self.child(), 'blocked')
        self.assertEqual(self.child(), 'acquired')

    def test_real_panel_helper_process_cannot_interleave_owner_transaction(self):
        script = ('import sys; from scripts.panel_helper import Helper,HelperError,Paths; '
                  'ticks=iter([0,121]); helper=Helper(Paths(lock_file=sys.argv[1]),clock=lambda:next(ticks))\n'
                  'try:\n'
                  ' lock=helper._lock(); print("acquired"); lock.close()\n'
                  'except HelperError: print("blocked")\n')
        def panel():
            return subprocess.run([sys.executable, '-c', script, self.lock], cwd=str(ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5,
                check=True).stdout.strip()
        with self.owner.owner_transaction(timeout=0):
            self.assertEqual(panel(), 'blocked')
        self.assertEqual(panel(), 'acquired')

    def test_new_runtime_directory_is_traversable_with_owner_private_umask(self):
        previous = os.umask(0o077)
        try:
            with self.owner.owner_transaction(timeout=0):
                self.assertEqual(os.stat(os.path.dirname(self.lock)).st_mode & 0o777, 0o755)
                self.assertEqual(os.stat(self.lock).st_mode & 0o777, 0o600)
        finally:
            os.umask(previous)

    def test_inherited_child_reuses_lock_and_cannot_unlock_parent(self):
        with self.owner.owner_transaction(timeout=0) as descriptor:
            self.assertEqual(self.child(descriptor), 'acquired')
            self.assertEqual(self.child(), 'blocked')
        self.assertEqual(self.child(), 'acquired')

    def test_bootstrap_exec_keeps_lock_through_shell_and_python_children(self):
        wallet = os.path.join(self.tmp.name, 'offline-wallet.sh')
        # Actual bash exec and two Python children; no bot, config or model calls.
        probe = ('import os,sys; from deploy import owner_transaction as owner; '
                 'owner.LOCK_PATH=sys.argv[1]\n'
                 'with owner.owner_transaction(timeout=0): print("inherited")\n')
        outsider = ('import os,sys; from deploy import owner_transaction as owner; '
                    'owner.LOCK_PATH=sys.argv[1]; os.environ.pop(owner.FD_ENV,None)\n'
                    'try:\n'
                    ' with owner.owner_transaction(timeout=.1): print("unexpected")\n'
                    'except TimeoutError: print("excluded")\n')
        import shlex
        command = lambda code: ' '.join(shlex.quote(v) for v in (sys.executable, '-c', code, self.lock))
        Path(wallet).write_text('#!/bin/bash\nset -eu\n' + command(probe) + '\n' + command(outsider) + '\n', encoding='utf-8')
        bootstrap = ('import sys; from deploy import owner_transaction as owner; '
                     'owner.LOCK_PATH=sys.argv[1]; owner.WALLET_SCRIPT=sys.argv[2]; '
                     'sys.argv=["owner", "--run-wallet-activation"]; owner.main()')
        output = subprocess.run([sys.executable, '-c', bootstrap, self.lock, wallet], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5, check=True)
        self.assertEqual(output.stdout.splitlines(), ['inherited', 'excluded'])
        self.assertEqual(self.child(), 'acquired')

    def test_inherited_unrelated_descriptor_cannot_authorize_transaction(self):
        other = os.open(os.path.join(self.tmp.name, 'unrelated'), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, other)
        with mock.patch.dict(os.environ, {self.owner.FD_ENV: str(other)}):
            with self.assertRaisesRegex(ValueError, 'descriptor'):
                with self.owner.owner_transaction(timeout=0):
                    self.fail('wrong lock descriptor accepted')

    def test_lock_symlink_and_parent_symlink_are_rejected(self):
        os.mkdir(os.path.dirname(self.lock))
        target = os.path.join(self.tmp.name, 'target')
        Path(target).write_text('', encoding='utf-8')
        os.symlink(target, self.lock)
        with self.assertRaises((OSError, ValueError)):
            self.owner.open_owner_lock(timeout=0)
        os.unlink(self.lock)
        os.rmdir(os.path.dirname(self.lock))
        os.mkdir(os.path.join(self.tmp.name, 'other-dir'))
        os.symlink(os.path.join(self.tmp.name, 'other-dir'), os.path.dirname(self.lock))
        with self.assertRaises((OSError, ValueError)):
            self.owner.open_owner_lock(timeout=0)

    def test_insecure_permissions_are_rejected_without_chmod_or_chown(self):
        os.mkdir(os.path.dirname(self.lock))
        Path(self.lock).write_text('', encoding='utf-8')
        os.chmod(self.lock, 0o666)
        with self.assertRaises(ValueError):
            self.owner.open_owner_lock(timeout=0)
        self.assertEqual(os.stat(self.lock).st_mode & 0o777, 0o666)
        os.chmod(self.lock, 0o600)
        os.chmod(os.path.dirname(self.lock), 0o777)
        with self.assertRaises(ValueError):
            self.owner.open_owner_lock(timeout=0)
        self.assertEqual(os.stat(os.path.dirname(self.lock)).st_mode & 0o777, 0o777)


if __name__ == '__main__':
    unittest.main()
