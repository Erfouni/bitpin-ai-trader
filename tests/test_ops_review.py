"""Regression tests of the ladder-round OPS review findings (scratch/review_ladder_ops_1): update.sh
--rollback on a tree that rejects the current settings or leaves resting orders unmanaged, the abort
messages of a failed update, the secrets files' permissions, and 'bitpin-bot check' keeping the keys
out of /proc.

The update.sh tests run the real shell functions in bash (Git Bash on Windows) with systemctl,
runuser and the service directories replaced by stubs / temp directories; skipped without bash.
No network, no root, no systemd.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def find_bash():
    probe = "test -f '%s/deploy/lib.sh'" % ROOT.replace("\\", "/")
    pf = os.environ.get("ProgramFiles", "C:\\Program Files")
    for cand in (os.path.join(pf, "Git", "bin", "bash.exe"), shutil.which("bash"), "/bin/bash"):
        if cand and os.path.exists(cand):
            try:
                if subprocess.call([cand, "-c", probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE) == 0:
                    return cand
            except OSError:
                continue
    return None


def posix(path):
    """A path bash on this machine understands (Git Bash: C:/x -> /c/x)."""
    p = path.replace("\\", "/")
    if len(p) > 2 and p[1] == ":" and os.name == "nt":
        p = "/%s%s" % (p[0].lower(), p[2:])
    return p


HARNESS = r'''
set -euo pipefail
. "$KIT/deploy/update_nomain.sh"
APP_DIR="$T/opt/bitpin-bot"; ETC_DIR="$T/etc"; STATE_DIR="$T/state"; UNIT_DIR="$T/units"
BACKUP_DIR="$T/backups"; NOTIFY_CONFIG="$T/etc/notify.json"; PYTHON="$PY"; APP_USER=bitpin
systemctl() {
    echo "systemctl $*" >> "$T/calls"
    case "$1" in
        show) echo "${UNIT_STATE:-inactive}" ;;
        is-enabled) echo "${UNIT_ENABLED:-enabled}" ;;
        is-active) return "${IS_ACTIVE_RC:-0}" ;;
    esac
    return 0
}
runuser() { while [ "$1" != "--" ]; do shift; done; shift; "$@"; }
timeout() { shift; "$@"; }
install_units() { echo "install_units $1" >> "$T/calls"; }
install_cli_link() { :; }
check_cli_flags() { return 0; }
sleep() { :; }
journalctl() { :; }
running_units() { echo "${RUNNING:-}"; }
"$@"
'''

CHECK_STUB = """import sys
sys.exit(int(open(__file__.replace("check_server.py", "rc.txt")).read() or 0))
"""


class RollbackHarness(unittest.TestCase):
    def setUp(self):
        self.sh = find_bash()
        if self.sh is None:
            self.skipTest("no bash that can read this checkout")
        self.t = tempfile.mkdtemp(prefix="rollback_")
        self.addCleanup(shutil.rmtree, self.t, True)
        kit = os.path.join(self.t, "kit", "deploy")
        os.makedirs(kit)
        shutil.copy(os.path.join(ROOT, "deploy", "lib.sh"), kit)
        with open(os.path.join(ROOT, "deploy", "update.sh"), encoding="utf-8") as f:
            src = f.read()
        self.assertTrue(src.rstrip().endswith('main "$@"'))
        with open(os.path.join(kit, "update_nomain.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write(src.rstrip()[:-len('main "$@"')])
        with open(os.path.join(kit, "harness.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write(HARNESS)
        # the current (new) tree and the backup (older) tree
        self.app = os.path.join(self.t, "opt", "bitpin-bot")
        self.bak = self.app + ".bak-20260920-000000"
        for tree, name in ((self.app, "new"), (self.bak, "old")):
            os.makedirs(os.path.join(tree, "deploy"))
            os.makedirs(os.path.join(tree, "scripts"))
            with open(os.path.join(tree, "VERSION"), "w") as f:
                f.write(name)
        with open(os.path.join(self.app, "scripts", "notify_bot.py"), "w") as f:
            f.write("# notifier\n")
        with open(os.path.join(self.bak, "deploy", "check_server.py"), "w") as f:
            f.write(CHECK_STUB)
        self.set_check_rc(0)
        for d in ("etc", "state", "units", "backups"):
            os.makedirs(os.path.join(self.t, d))
        for n in ("config.json", "kimi.json"):
            with open(os.path.join(self.t, "etc", n), "w") as f:
                json.dump({}, f)
        with open(os.path.join(self.t, "units", "bitpin-bot-notify.service"), "w") as f:
            f.write("[Unit]\n")

    def set_check_rc(self, rc):
        with open(os.path.join(self.bak, "deploy", "rc.txt"), "w") as f:
            f.write(str(rc))

    def journal(self, orders):
        with open(os.path.join(self.t, "state", "live_orders.json"), "w") as f:
            if isinstance(orders, str):
                f.write(orders)
            else:
                json.dump({"orders": orders}, f)

    def run_sh(self, *args, **env):
        e = dict(os.environ, KIT=posix(os.path.join(self.t, "kit")), T=posix(self.t), PY=posix(sys.executable))
        e.update(env)
        p = subprocess.run([self.sh, posix(os.path.join(self.t, "kit", "deploy", "harness.sh"))] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def version(self):
        with open(os.path.join(self.app, "VERSION")) as f:
            return f.read()

    def calls(self):
        p = os.path.join(self.t, "calls")
        if not os.path.exists(p):
            return ""
        with open(p) as f:
            return f.read()

    def code_copy(self, suffix, version="saved"):
        """A complete synthetic code copy, with the same offline config checker as the old tree."""
        path = self.app + suffix
        shutil.copytree(self.bak, path)
        with open(os.path.join(path, "VERSION"), "w") as f:
            f.write(version)
        return path

    def test_manual_backup_names_cannot_evict_the_latest_timestamp_backups(self):
        managed = [self.code_copy(".bak-2035010%d-000000" % day) for day in range(1, 5)]
        manual = [self.code_copy(".bak-" + name) for name in ("eth-recovery", "live-evidence", "panel")]
        # Moving an installed tree preserves its directory mtime: that is not the backup creation time.
        os.utime(managed[-1], (1, 1))
        rc, out = self.run_sh("prune_backups", posix(managed[-1]))
        self.assertEqual(rc, 0, out)
        self.assertEqual([os.path.isdir(p) for p in managed], [False, True, True, True], out)
        self.assertTrue(all(os.path.isdir(p) for p in manual), out)

    def test_rollback_selects_the_latest_managed_timestamp_not_a_manual_label(self):
        latest = self.code_copy(".bak-20350104-000000", "latest")
        self.code_copy(".bak-z-manual", "manual")
        os.utime(latest, (1, 1))
        rc, out = self.run_sh("rollback", "0")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.version(), "latest", out)
        self.assertTrue(os.path.isdir(self.app + ".bak-z-manual"), out)

    def test_rollback_refuses_when_only_manual_copies_exist(self):
        manual = self.app + ".bak-z-manual"
        os.rename(self.bak, manual)
        rc, out = self.run_sh("rollback", "0")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.version(), "new", out)
        self.assertTrue(os.path.isdir(manual), out)
        self.assertNotIn("systemctl stop", self.calls())

    def test_pruning_always_preserves_the_immediate_previous_tree_after_clock_regression(self):
        future = [self.code_copy(".bak-2035010%d-000000" % day) for day in range(1, 5)]
        rc, out = self.run_sh("prune_backups", posix(self.bak))
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.bak), out)
        self.assertEqual([os.path.isdir(p) for p in future], [False, False, True, True], out)

    def test_unmanaged_names_and_regular_files_are_not_pruned(self):
        paths = [self.code_copy(suffix) for suffix in
                 (".bak-99999999-999999-extra", ".bak-99999999", ".failed-z-manual")]
        files = [self.app + ".bak-2035010%d-000000" % day for day in range(1, 5)]
        for path in files:
            with open(path, "w") as f:
                f.write("not a code directory")
        rc, out = self.run_sh("prune_backups")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.bak), out)
        self.assertTrue(all(os.path.isdir(p) for p in paths), out)
        self.assertTrue(all(os.path.isfile(p) for p in files), out)

    def test_failed_copy_retention_ignores_manual_labels(self):
        failed = [self.code_copy(".failed-2035010%d-000000" % day) for day in range(1, 4)]
        manual = self.code_copy(".failed-z-manual")
        rc, out = self.run_sh("prune_backups")
        self.assertEqual(rc, 0, out)
        self.assertEqual([os.path.isdir(p) for p in failed], [False, False, True], out)
        self.assertTrue(os.path.isdir(manual), out)

    def test_auto_rollback_can_restore_the_tree_after_retention_with_manual_backups(self):
        for name in ("eth-recovery", "live-evidence", "panel"):
            self.code_copy(".bak-" + name)
        rc, out = self.run_sh("eval", 'prune_backups "$APP_DIR.bak-20260920-000000"; '
                             'auto_rollback "$APP_DIR.bak-20260920-000000" 1')
        self.assertEqual(rc, 1, out)  # a rolled-back update is still reported as a failed update
        self.assertTrue(os.path.isdir(self.app), out)
        self.assertEqual(self.version(), "old", out)
        self.assertIn("update ROLLED BACK", out)

    def test_main_update_passes_its_previous_tree_to_retention_before_auto_rollback(self):
        for day in range(1, 5):
            self.code_copy(".bak-2035010%d-000000" % day)
        self.code_copy(".bak-z-manual")
        # Run the real update/swap/retention/auto-rollback control flow. External host operations
        # are replaced at their boundaries; actual directory moves and removals remain real.
        script = r'''
STAMP=20300101-000000
require_root() { :; }
check_systemd() { :; }
getent() { :; }
check_source_tree() { :; }
check_python() { :; }
stage_code() {
    cp -R "$APP_DIR" "$2"
    printf '%s' updated > "$2/VERSION"
}
syntax_check() { :; }
normalize_tree() { :; }
compile_tree() { :; }
check_configs() { :; }
ensure_dirs() { :; }
install_config_files() { :; }
install_ops_files() { :; }
enable_backup_timer() { :; }
drop_foreign_ops() { :; }
restart_panel() { :; }
# A bad new CLI is a normal automatic rollback trigger, even with no bot unit running.
check_cli_flags() { return 1; }
main --skip-tests
'''
        rc, out = self.run_sh("eval", script)
        self.assertEqual(rc, 1, out)
        self.assertTrue(os.path.isdir(self.app), out)
        self.assertEqual(self.version(), "new", out)
        self.assertIn("update ROLLED BACK", out)
        self.assertTrue(os.path.isdir(self.app + ".bak-z-manual"), out)

    def test_backup_retention_handles_an_app_path_containing_spaces(self):
        new_app = os.path.join(self.t, "opt", "bot with spaces")
        os.rename(self.app, new_app)
        os.rename(self.bak, new_app + ".bak-20260920-000000")
        for day in range(1, 5):
            os.mkdir(new_app + ".bak-2035010%d-000000" % day)
        rc, out = self.run_sh("eval", 'APP_DIR="$T/opt/bot with spaces"; prune_backups')
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(new_app), out)
        self.assertEqual(sorted(n for n in os.listdir(os.path.dirname(new_app)) if ".bak-" in n),
                         ["bot with spaces.bak-20350102-000000", "bot with spaces.bak-20350103-000000",
                          "bot with spaces.bak-20350104-000000"], out)

    def code_symlink(self):
        target = os.path.join(self.t, "unrelated-code")
        shutil.copytree(self.bak, target)
        with open(os.path.join(target, "VERSION"), "w") as f:
            f.write("unrelated")
        link = self.app + ".bak-20360101-000000"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        # Never let test cleanup recurse into a directory link on older Windows Python.
        self.addCleanup(lambda: os.unlink(link) if os.path.islink(link) else None)
        return link, target

    def test_symlink_backups_are_not_counted_or_removed(self):
        link, target = self.code_symlink()
        managed = [self.code_copy(".bak-2035010%d-000000" % day) for day in range(1, 5)]
        rc, out = self.run_sh("prune_backups")
        self.assertEqual(rc, 0, out)
        self.assertEqual([os.path.isdir(p) for p in managed], [False, True, True, True], out)
        self.assertTrue(os.path.islink(link), out)
        self.assertTrue(os.path.isdir(target), out)

    def test_rollback_does_not_select_a_timestamp_shaped_symlink(self):
        link, target = self.code_symlink()
        rc, out = self.run_sh("rollback", "0")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.version(), "old", out)
        self.assertFalse(os.path.islink(self.app), out)
        self.assertTrue(os.path.islink(link), out)
        self.assertTrue(os.path.isdir(target), out)

    def test_a_backup_that_rejects_the_current_settings_is_not_restored(self):
        """Review finding (ops, high): after apply_profile.py the old code rejects config.json
        ("unknown config key 'ladder'") and kimi.json, exits 78 and never trades."""
        self.set_check_rc(1)
        rc, out = self.run_sh("rollback", "0")
        self.assertEqual(rc, 1, out)
        self.assertIn("rejects your current", out)
        self.assertIn(".before-profile", out)
        self.assertEqual(self.version(), "new")                          # nothing was changed
        self.assertNotIn("systemctl stop", self.calls())

    def test_resting_orders_of_the_new_version_block_the_rollback(self):
        self.journal({"a": {"kind": "limit", "status": "resting", "tag": "ladder", "side": "buy"},
                      "b": {"kind": "limit", "status": "submitting", "tag": "target", "side": "sell"},
                      "c": {"kind": "limit", "status": "cancelled"}, "d": {"kind": "market", "status": "open"}})
        rc, out = self.run_sh("rollback", "0")
        self.assertEqual(rc, 1, out)
        self.assertIn("shows 2 resting order(s)", out)
        self.assertIn("sudo bitpin-bot cancel-resting", out)
        self.assertEqual(self.version(), "new")
        self.journal("{not json")
        rc, out = self.run_sh("rollback", "0")
        self.assertEqual(rc, 1, out)
        self.assertIn("shows ? resting order(s)", out)

    def test_force_rolls_back_and_disables_a_notifier_the_old_tree_does_not_have(self):
        self.set_check_rc(1)
        self.journal({"a": {"kind": "limit", "status": "resting"}})
        rc, out = self.run_sh("rollback", "1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.version(), "old")
        self.assertTrue([d for d in os.listdir(os.path.join(self.t, "opt")) if ".failed-" in d])
        self.assertIn("--force: rolling back with 1 resting", out)
        self.assertIn("will NOT start until they match it", out)
        self.assertIn("systemctl disable --now bitpin-bot-notify.service", self.calls())
        self.assertIn("is NOT started", out)                             # nothing was running before

    def test_a_clean_rollback_keeps_the_notifier_of_a_tree_that_has_one(self):
        with open(os.path.join(self.bak, "scripts", "notify_bot.py"), "w") as f:
            f.write("# notifier\n")
        self.journal({"c": {"kind": "limit", "status": "filled"}})
        rc, out = self.run_sh("rollback", "0")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.version(), "old")
        self.assertNotIn("disable", self.calls())

    def test_a_restored_version_that_does_not_stay_up_does_not_suggest_rollback_again(self):
        with open(os.path.join(self.bak, "scripts", "notify_bot.py"), "w") as f:
            f.write("# notifier\n")
        rc, out = self.run_sh("rollback", "0", RUNNING="bitpin-bot.service", IS_ACTIVE_RC="3")
        self.assertEqual(rc, 1, out)
        self.assertIn("Do NOT run --rollback again", out)
        self.assertNotIn("to go back to the previous version", out)

    def test_an_aborted_update_tells_the_owner_to_start_the_stopped_bot_again(self):
        """Review finding (ops, low): update.sh said 'the running bot was NOT touched' about a bot the
        owner had stopped for the update (DEPLOY_FA step 1), and the guide gave no way back."""
        rc, out = self.run_sh("aborted", "tests failed", UNIT_STATE="inactive", UNIT_ENABLED="enabled")
        self.assertEqual(rc, 1)
        self.assertIn("nothing was changed", out)
        self.assertIn("sudo systemctl start bitpin-bot", out)
        self.assertIn("crash-ladder bids stay on Bitpin", out)
        rc, out = self.run_sh("aborted", "tests failed", UNIT_STATE="active")
        self.assertIn("the running bot was NOT touched", out)
        self.assertNotIn("systemctl start", out)
        open(os.path.join(self.t, "state", "STOP"), "w").close()        # stopped on purpose: no nudge
        rc, out = self.run_sh("aborted", "tests failed", UNIT_STATE="inactive", UNIT_ENABLED="enabled")
        self.assertNotIn("systemctl start", out)

    def test_force_is_only_accepted_with_rollback(self):
        upd = read("deploy", "update.sh")
        self.assertIn('rollback "$force"', upd)
        self.assertIn('die "--force is only used with --rollback"', upd)


class SecretsFilesTest(unittest.TestCase):
    def test_the_secrets_files_are_root_only_and_hidden_from_the_other_services(self):
        """Review finding (ops, low): bitpin-bot.env / notify.env were 0640 root:bitpin although no
        process of user bitpin opens them - the paper bot could read the Bitpin keys straight from it."""
        lib = read("deploy", "lib.sh")
        self.assertIn('install_if_absent "$app/deploy/bitpin-bot.env.example" "$ENV_FILE" 0600 root', lib)
        self.assertIn('install_if_absent "$app/deploy/notify.env.example" "$NOTIFY_ENV_FILE" 0600 root', lib)
        self.assertIn('install_if_absent "$app/config.example.json" "$ETC_DIR/config.json" 0640\n', lib)
        self.assertIn('grp="${4:-$APP_GROUP}"', lib)
        self.assertIn("InaccessiblePaths=-/etc/bitpin-bot/notify.env", read("deploy", "bitpin-bot.service"))
        self.assertIn("InaccessiblePaths=-/var/lib/bitpin-bot -/etc/bitpin-bot/notify.env",
                      read("deploy", "bitpin-bot-paper.service"))
        self.assertIn("mode 0600, owner root, group root", read("deploy", "bitpin-bot.env.example"))
        self.assertIn("Mode 0600 root:root", read("deploy", "notify.env.example"))
        for name in ("DEPLOY_FA.md", "TELEGRAM_FA.md"):
            doc = read("docs", name)
            self.assertIn("0600", doc)
            self.assertIn("root:root", doc)
            self.assertNotIn("root bitpin`", doc)                         # the old "-rw-r----- 1 root bitpin"
            self.assertEqual([ln for ln in doc.splitlines() if "0640" in ln and ".env" in ln], [], name)
        self.assertNotIn("sudo chown root:bitpin /etc/bitpin-bot/notify.env", read("docs", "TELEGRAM_FA.md"))


class ServerCheckDumpableTest(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("check_server_dumpable",
                                                      os.path.join(ROOT, "deploy", "check_server.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_server_check_makes_itself_non_dumpable_first(self):
        """Review finding (ops, low): 'sudo bitpin-bot check' runs as user bitpin with the service's keys
        in its environment; the same-user notifier could read them from /proc/<pid>/environ."""
        cs = self.load()
        calls = []

        class Libc(object):
            def prctl(self, *a):
                calls.append(a)
                return 0
        fake_ctypes = mock.MagicMock()
        fake_ctypes.CDLL.return_value = Libc()
        with mock.patch.object(cs.sys, "platform", "linux"), mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertTrue(cs.not_dumpable())
        self.assertEqual(calls, [(4, 0, 0, 0, 0)])
        with mock.patch.object(cs.sys, "platform", "win32"):
            self.assertFalse(cs.not_dumpable())
        order = []
        with mock.patch.object(cs, "not_dumpable", side_effect=lambda: order.append("dumpable")), \
                mock.patch.object(cs, "check_configs", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                cs.main(["--config-only"])
        self.assertEqual(order, ["dumpable"])


class HelperTextTest(unittest.TestCase):
    def test_the_helper_says_what_stop_cancels(self):
        h = read("deploy", "bitpin-bot")
        self.assertIn("the RUNNING bot cancels its", h)
        self.assertIn("systemctl stop / a crashed bot do NOT cancel them", h)
        self.assertNotIn("(STOP / systemctl stop do NOT cancel them)", h)


if __name__ == "__main__":
    unittest.main()
