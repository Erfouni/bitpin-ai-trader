"""Release v3 ops tests (spec F3 / F4 / F5): the failure reporter (OnFailure= -> deploy/bitpin-bot-failed ->
the notifier's relay file -> a Telegram alert), the daily state backup (script, service, timer, 14 kept,
never the refresh token), the needrestart / logrotate / journald files installed idempotently and removed
by uninstall, stage_code's v3 exclusions, and the Persian guides (git clone, monthly checklist, alerts).

The shell parts run the REAL deploy/lib.sh functions and scripts under bash (Git Bash on Windows) with
systemctl replaced by a stub and every path pointed into a temp directory; skipped without bash. No
network, no root, no systemd."""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import test_ops_review as tor  # noqa: E402  (find_bash, posix)
from bitpin import notify  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class Files(unittest.TestCase):
    """What the kit files say (read as text)."""

    def test_the_bot_units_report_a_final_failure_through_the_template(self):
        for unit in ("bitpin-bot.service", "bitpin-bot-paper.service"):
            u = read("deploy", unit)
            self.assertIn("OnFailure=bitpin-bot-failed@%n.service", u)
            self.assertIn("RestartPreventExitStatus=", u)          # exit 78 is the one that ends in "failed"
            self.assertIn("StartLimitIntervalSec=0", u)             # ... and a restart loop never does
        live = read("deploy", "bitpin-bot.service")
        exec_start = [ln for ln in live.splitlines() if ln.startswith("ExecStart=")]
        self.assertEqual(len(exec_start), 1)
        self.assertIn(exec_start[0][len("ExecStart="):], read("docs", "DEPLOY_FA.md"))   # unchanged, still quoted
        t = read("deploy", "bitpin-bot-failed@.service")
        for want in ("Type=oneshot", "ExecStart=/bin/bash /opt/bitpin-bot/deploy/bitpin-bot-failed %i",
                     "PrivateNetwork=yes", "ReadWritePaths=/var/lib/bitpin-bot-notify",
                     "\nUser=bitpin\n", "\nGroup=bitpin\n", "\nCapabilityBoundingSet=\n",
                     "InaccessiblePaths=-/etc/bitpin-bot/bitpin-bot.env -/etc/bitpin-bot/notify.env -/var/lib/bitpin-bot"):
            self.assertIn(want, t)
        # v3 security review: the relay runs as bitpin (the notifier dir's owner), never root, no chown
        self.assertNotIn("CAP_DAC_OVERRIDE", t)
        self.assertNotIn("chown", read("deploy", "bitpin-bot-failed"))
        self.assertIn("sudo systemctl start bitpin-bot-failed@test.service", read("docs", "TELEGRAM_FA.md"))
        # the relay file name is a contract between the script and the notifier
        self.assertIn('RELAY_FILE="%s"' % notify.UNIT_FAILURE_FILE, read("deploy", "bitpin-bot-failed"))

    def test_the_backup_units(self):
        svc = read("deploy", "bitpin-bot-backup.service")
        self.assertIn("ExecStart=/bin/bash /opt/bitpin-bot/deploy/bitpin-bot-backup", svc)
        self.assertIn("ReadWritePaths=/var/backups/bitpin-bot", svc)
        self.assertIn("PrivateNetwork=yes", svc)
        timer = read("deploy", "bitpin-bot-backup.timer")
        self.assertIn("OnCalendar=*-*-* 03:10:00 UTC", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("Unit=bitpin-bot-backup.service", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_the_system_files(self):
        nr = read("deploy", "needrestart-bitpin-bot.conf")
        m = re.search(r"\$nrconf\{override_rc\}\{qr\((.+)\)\} = 0;", nr)
        self.assertTrue(m, nr)
        pat = re.compile(m.group(1))
        for u in ("bitpin-bot.service", "bitpin-bot-paper.service"):                  # the two trading units only
            self.assertTrue(pat.match(u), u)
        for u in ("nginx.service", "bitpin-bot-notify.service", "bitpin-bot-notify-stop.service",
                  "bitpin-bot-backup.service", "bitpin-bot-failed@bitpin-bot.service"):
            self.assertFalse(pat.match(u), u)
        lr = read("deploy", "logrotate-bitpin-bot")
        lr = "\n".join(ln for ln in lr.splitlines() if not ln.startswith("#"))   # the header comment names them too
        self.assertIn("/var/lib/bitpin-bot/bot_live.log {", lr)
        for want in ("size 20M", "rotate 5", "copytruncate", "su bitpin bitpin", "missingok", "compress"):
            self.assertEqual(sum(1 for ln in lr.splitlines() if ln.strip() == want), 2, want)   # live + paper log
        # v3 security review: no system-wide journald drop-in (it cut the other services' log retention)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "deploy", "journald-bitpin-bot.conf")))
        self.assertNotIn("journald", read("deploy", "lib.sh").split("ops_conf_paths() {")[1].split("}")[0])

    def test_lib_install_update_uninstall_and_the_helper_carry_the_ops_units(self):
        lib = read("deploy", "lib.sh")
        self.assertIn('OPS_UNITS="bitpin-bot-failed@.service bitpin-bot-backup.service bitpin-bot-backup.timer"', lib)
        self.assertIn('NOTIFY_UNITS="bitpin-bot-notify.service bitpin-bot-notify-stop.service '
                      'bitpin-bot-notify-stop.path"', lib)          # unchanged: other tests and scripts pin it
        self.assertIn('UNITS="bitpin-bot.service bitpin-bot-paper.service"\n', lib)   # never in UNITS
        for fn in ("ops_conf_paths()", "install_conf_file()", "install_ops_files()", "enable_backup_timer()",
                   "remove_ops_files()"):
            self.assertIn(fn, lib)
        self.assertIn("for u in $OPS_UNITS; do", lib)
        # normalize_tree strips CRLF from and makes executable the two new scripts
        self.assertIn('"$d"/deploy/bitpin-bot-failed "$d"/deploy/bitpin-bot-backup; do\n        [ -f "$f" ] && chmod 0755', lib)
        self.assertIn('"$d"/deploy/*.timer "$d"/deploy/*.conf', lib)
        for name in ("install.sh", "update.sh"):
            src = read("deploy", name)
            self.assertLess(src.index('install_units "$APP_DIR/deploy"'), src.index('install_ops_files "$APP_DIR/deploy"'))
            self.assertIn("enable_backup_timer", src)
        un = read("deploy", "uninstall.sh")
        self.assertIn("for u in $OPS_UNITS; do", un)
        self.assertIn("remove_ops_files", un)
        self.assertIn("for u in $UNITS $NOTIFY_UNITS; do", un)     # the notifier loop is untouched
        h = read("deploy", "bitpin-bot")
        self.assertIn("    backup)", h)
        self.assertIn("bitpin-bot-backup.timer", h)                 # health shows the newest backup
        ins = read("deploy", "install.sh")
        self.assertIn("the only unit it enables is the daily backup timer", ins)

    def test_stage_code_leaves_out_research_and_notes_and_ships_the_prompt_template(self):
        lib = read("deploy", "lib.sh")
        block = lib[lib.index("stage_code() {"):lib.index("syntax_check() {")]
        for pat in ("--exclude='./research'", "--exclude='./docs/dev_notes'", "--exclude='./docs/reviews'"):
            self.assertIn(pat, block)
        self.assertIn('"$stage/bitpin/prompt_template.txt"', block)
        self.assertNotIn("--exclude='*.txt'", block)                # the template is a .txt next to the code
        self.assertIn('"$stage/bitpin/static/panel.css"', block)    # v3.2: the panel's stylesheet ships too
        self.assertNotIn("--exclude='*.css'", block)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "bitpin", "static", "panel.css")))

    def test_scripts_parse(self):
        sh = tor.find_bash()
        if sh is None:
            self.skipTest("no bash that can read this checkout")
        for rel in ("deploy/bitpin-bot-failed", "deploy/bitpin-bot-backup", "deploy/lib.sh", "deploy/install.sh",
                    "deploy/update.sh", "deploy/uninstall.sh", "deploy/bitpin-bot"):
            p = subprocess.run([sh, "-n", tor.posix(os.path.join(ROOT, rel))], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            self.assertEqual(p.returncode, 0, (rel, p.stdout))


class Guides(unittest.TestCase):
    def test_deploy_guide_has_the_v3_sections_and_numbers(self):
        doc = read("docs", "DEPLOY_FA.md")
        for must in ("### راه دوم: `git clone`", "git clone --branch main REPO_URL", "git pull --ff-only",
                     "## چک‌لیست ماهانهٔ مالک", "## هشدارهای تلگرام (چه معنایی دارند و چه کنید)",
                     "## پشتیبان‌گیری و بازگردانی", "sudo bitpin-bot backup", "/var/backups/bitpin-bot/state-",
                     "bitpin-bot-failed@.service", "/etc/logrotate.d/bitpin-bot", "/etc/needrestart/conf.d/bitpin-bot.conf",
                     "sudo needrestart -r l", "hwm_window_days", "`max_drawdown` = `0.50`",
                     "`min_order_usdt` = `1.05`", "`levels_pct` = `[-20]`", "`size_frac` = `0.25`",
                     "۷۲۰ ساعت", "2027-09-21T20:30:00Z", "llm_quota", "reset-failed bitpin-bot",
                     "چرخه‌هایش با خطا تمام می‌شوند", "اعتبار Moonshot تمام شده"):
            self.assertIn(must, doc)
        # the one-month numbers are gone
        for gone in ("حد ضرر ۱۲٪", "حداکثر ۱۶۸ ساعت", "`max_drawdown` = `0.30`", "`size_frac` = `0.125`",
                     "۲۲ اکتبر ۲۰۲۶", "حد ضرر ۳۰٪", "/etc/systemd/journald.conf.d/bitpin-bot.conf"):
            self.assertNotIn(gone, doc)
        low = doc.lower()
        for word in ("xray", "tunnel", "vpn", "set-ntp", "تونل", "فیلترشکن"):     # the deploy test's rule
            self.assertNotIn(word, low)
        # the monthly checklist is numbered 1..13 and names what only the owner can see
        cl = doc[doc.index("## چک‌لیست ماهانهٔ مالک"):doc.index("## هشدارهای تلگرام")]
        self.assertEqual([int(m) for m in re.findall(r"^(\d+)\. ", cl, re.M)], list(range(1, 14)))
        for must in ("kimi-check", "SERVER_IP", "authenticated with Bitpin", "df -h /var/lib", "timedatectl",
                     "127.0.0.1:1081", "/var/backups/bitpin-bot/", "risk_state_live.json", "update.sh"):
            self.assertIn(must, cl)

    def test_telegram_guide_names_the_new_messages(self):
        doc = read("docs", "TELEGRAM_FA.md")
        for must in ("🔁 ربات روشن است ولی چرخه‌هایش با خطا تمام می‌شوند", "🧠 N ساعت است تصمیم معتبری از Kimi ثبت نشده",
                     "💳 اعتبار Moonshot تمام شده؛ تا N ساعت دیگر derisk", "🌐 مسیر اینترنت خارجی (پراکسی محلی) قطع بود",
                     "🚨 سرویس ربات از کار افتاد و دیگر ری‌استارت نمی‌شود", "📆 خلاصهٔ هفتگی", "🔬 تحلیل Kimi برای هر کوین",
                     "💸 هزینهٔ Kimi", "📉 افت از سقف ۹۰ روزه", "بدون حد ضرر", "unit_failure.json", "llm_spend.json",
                     "۲۵ شهریور ۱۴۰۶ ساعت ۱۳:۰۰", "۲۹ شهریور ۱۴۰۶ ساعت ۱۳:۰۰", "۹۰ روز اخیر"):
            self.assertIn(must, doc)
        self.assertNotIn("افت ۳۰٪ از بالاترین ارزش", doc)


class ShellHarness(unittest.TestCase):
    """The real lib.sh functions and the two new scripts under bash, everything pointed into a temp dir."""

    def setUp(self):
        self.sh = tor.find_bash()
        if self.sh is None:
            self.skipTest("no bash that can read this checkout")
        self.t = tempfile.mkdtemp(prefix="ops_v3_")
        self.addCleanup(shutil.rmtree, self.t, True)
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.bin)
        # a systemctl stub on PATH (scripts) and as a function (sourced lib.sh): both record their calls
        with open(os.path.join(self.bin, "systemctl"), "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/usr/bin/env bash\necho "systemctl $*" >> "$CALLS"\n'
                    'case "$*" in *ExecMainStatus*) echo 78 ;; *Result*) echo exit-code ;; '
                    '*SubState*) echo "${FAKE_SUBSTATE:-failed}" ;; esac\nexit 0\n')
        os.chmod(os.path.join(self.bin, "systemctl"), 0o755)
        self.calls_path = os.path.join(self.t, "calls")

    def run_sh(self, script, **env):
        e = dict(os.environ, CALLS=tor.posix(self.calls_path), T=tor.posix(self.t),
                 ROOT=tor.posix(ROOT), PATH=tor.posix(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        e.update(env)
        p = subprocess.run([self.sh, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def calls(self):
        if not os.path.exists(self.calls_path):
            return ""
        with open(self.calls_path, encoding="utf-8") as f:
            return f.read()

    LIB = ('set -euo pipefail\n. "$ROOT/deploy/lib.sh"\nETC_DIR="$T/etc/bitpin-bot"; UNIT_DIR="$T/units"; '
           'BACKUP_DIR="$T/backups"\nsystemctl() { echo "systemctl $*" >> "$CALLS"; return 0; }\n')

    def test_install_ops_files_is_idempotent_and_remove_ops_files_undoes_it(self):
        os.makedirs(os.path.join(self.t, "etc", "bitpin-bot"))
        rc, out = self.run_sh(self.LIB + 'install_ops_files "$ROOT/deploy"\n')
        self.assertEqual(rc, 0, out)
        paths = {"needrestart-bitpin-bot.conf": os.path.join(self.t, "etc", "needrestart", "conf.d", "bitpin-bot.conf"),
                 "logrotate-bitpin-bot": os.path.join(self.t, "etc", "logrotate.d", "bitpin-bot")}
        for src, dst in paths.items():
            self.assertTrue(os.path.exists(dst), dst)
            with open(dst, encoding="utf-8") as f:
                self.assertEqual(f.read(), read("deploy", src).replace("\r\n", "\n"))
        self.assertFalse(os.path.exists(os.path.join(self.t, "etc", "systemd")))    # nothing system-wide
        self.assertEqual(out.count("[ OK ] installed"), 2)
        rc, out = self.run_sh(self.LIB + 'install_ops_files "$ROOT/deploy"\n')       # unchanged: silent
        self.assertEqual(rc, 0, out)
        self.assertNotIn("installed", out)
        rc, out = self.run_sh(self.LIB + 'install_ops_files "$T/empty"\n')            # an older tree: warns only
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count("[WARN] no "), 2)
        self.assertTrue(all(os.path.exists(p) for p in paths.values()))
        rc, out = self.run_sh(self.LIB + 'remove_ops_files\n')
        self.assertEqual(rc, 0, out)
        self.assertFalse(any(os.path.exists(p) for p in paths.values()))
        self.assertEqual(out.count("[ OK ] removed"), 2)
        self.assertNotIn("systemd-journald", self.calls())                          # never restarted
        # the backup timer is enabled only when its unit file is installed
        os.makedirs(os.path.join(self.t, "units"))
        rc, out = self.run_sh(self.LIB + 'enable_backup_timer\n')
        self.assertNotIn("bitpin-bot-backup.timer", self.calls())
        open(os.path.join(self.t, "units", "bitpin-bot-backup.timer"), "w").close()
        rc, out = self.run_sh(self.LIB + 'enable_backup_timer\n')
        self.assertEqual(rc, 0, out)
        self.assertIn("systemctl enable --now bitpin-bot-backup.timer", self.calls())
        self.assertIn("daily state backup enabled", out)

    def test_the_failure_reporter_writes_the_relay_file_the_notifier_turns_into_an_alert(self):
        nd = os.path.join(self.t, "nstate")
        os.makedirs(nd)
        rc, out = self.run_sh('bash "$ROOT/deploy/bitpin-bot-failed" bitpin-bot.service', NOTIFY_STATE_DIR=tor.posix(nd))
        self.assertEqual(rc, 0, out)
        self.assertIn("relayed to", out)
        path = os.path.join(nd, notify.UNIT_FAILURE_FILE)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual((d["unit"], d["exit_status"], d["result"]), ("bitpin-bot.service", 78, "exit-code"))
        self.assertIsInstance(d["time"], int)
        self.assertEqual([n for n in os.listdir(nd) if n.startswith(".unit_failure")], [])   # no temp file left
        # the notifier (its own state dir = nd) sends the alert and deletes the file
        import test_notify as tn
        sd = os.path.join(self.t, "bot")
        os.makedirs(sd)
        cfg = notify.load_config(None, overrides={"state_dir": sd, "notify_state_dir": nd, "daily_summary_time": None,
                                                  "telegram": {"min_send_interval_seconds": 0}})
        tg = tn.FakeTelegram()
        clock = tn.FakeClock(d["time"] + 30)
        n = notify.Notifier(cfg, notify.Secrets(tn.TOKEN, (tn.CHAT,)), telegram=tg, clock=clock, sleep=clock.sleep)
        n.start()
        n.step()
        joined = "\n".join(tg.texts())
        self.assertIn("سرویس ربات از کار افتاد و دیگر ری‌استارت نمی‌شود", joined)
        self.assertIn("<code>bitpin-bot.service</code>", joined)
        self.assertFalse(os.path.exists(path))
        # an odd unit name is replaced, a missing notifier dir is reported and not an error
        rc, out = self.run_sh('bash "$ROOT/deploy/bitpin-bot-failed" "x;rm -rf /"', NOTIFY_STATE_DIR=tor.posix(nd))
        self.assertEqual(rc, 0, out)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["unit"], "bitpin-bot.service")
        rc, out = self.run_sh('bash "$ROOT/deploy/bitpin-bot-failed" bitpin-bot.service',
                              NOTIFY_STATE_DIR=tor.posix(os.path.join(self.t, "missing")))
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing relayed", out)
        # v3 ops review: a unit that systemd is restarting has not failed - no relay, no false alert
        os.remove(path)
        rc, out = self.run_sh('bash "$ROOT/deploy/bitpin-bot-failed" bitpin-bot.service', NOTIFY_STATE_DIR=tor.posix(nd),
                              FAKE_SUBSTATE="auto-restart")
        self.assertEqual(rc, 0, out)
        self.assertIn("is restarting", out)
        self.assertFalse(os.path.exists(path))
        for unit in ("bitpin-bot.service", "bitpin-bot-paper.service"):
            self.assertIn("\nRestartMode=direct\n", read("deploy", unit), unit)

    def test_the_backup_script_keeps_14_archives_and_never_the_token(self):
        if shutil.which("tar") is None:
            self.skipTest("no tar")
        sd = os.path.join(self.t, "state")
        nd = os.path.join(self.t, "nstate")
        etc = os.path.join(self.t, "etc")
        bk = os.path.join(self.t, "backups")
        for d in (sd, nd, etc):
            os.makedirs(d)
        for name, body in (("kimi_decisions.jsonl", "{}\n"), ("bitpin_token.json", "SECRET"), ("x.tmp", "tmp"),
                           ("live_orders.json", "{}")):
            with open(os.path.join(sd, name), "w") as f:
                f.write(body)
        with open(os.path.join(nd, "notify_state.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(etc, "config.json"), "w") as f:
            f.write("{}")
        env = dict(STATE_DIR=tor.posix(sd), NOTIFY_STATE_DIR=tor.posix(nd), ETC_DIR=tor.posix(etc),
                   BACKUP_DIR=tor.posix(bk))
        rc, out = self.run_sh('bash "$ROOT/deploy/bitpin-bot-backup"', **env)
        self.assertEqual(rc, 0, out)
        archives = sorted(n for n in os.listdir(bk) if n.endswith(".tgz"))
        self.assertEqual(len(archives), 1)
        self.assertTrue(re.match(r"^state-\d{4}-\d{2}-\d{2}\.tgz$", archives[0]), archives[0])
        with tarfile.open(os.path.join(bk, archives[0])) as tf:
            names = tf.getnames()
        self.assertTrue(any(n.endswith("kimi_decisions.jsonl") for n in names), names)
        self.assertTrue(any(n.endswith("notify_state.json") for n in names), names)
        self.assertTrue(any(n.endswith("config.json") for n in names), names)
        self.assertFalse(any("bitpin_token.json" in n or n.endswith(".tmp") for n in names), names)
        self.assertEqual([n for n in os.listdir(bk) if n.startswith(".state")], [])          # no temp left
        # a second run the same day replaces that day's archive; old ones beyond KEEP are pruned
        for i in range(1, 16):
            open(os.path.join(bk, "state-2020-01-%02d.tgz" % i), "w").close()
        rc, out = self.run_sh('bash "$ROOT/deploy/bitpin-bot-backup"', **env)
        self.assertEqual(rc, 0, out)
        left = sorted(n for n in os.listdir(bk) if n.endswith(".tgz"))
        self.assertEqual(len(left), 14)
        self.assertEqual(left[-1], archives[0])                    # the newest kept, the oldest two gone
        self.assertNotIn("state-2020-01-01.tgz", left)
        self.assertNotIn("state-2020-01-02.tgz", left)
        self.assertIn("state-2020-01-03.tgz", left)
        # nothing to back up: an error, no archive
        rc, out = self.run_sh('bash "$ROOT/deploy/bitpin-bot-backup"', STATE_DIR=tor.posix(os.path.join(self.t, "no")),
                              NOTIFY_STATE_DIR=tor.posix(os.path.join(self.t, "no2")),
                              ETC_DIR=tor.posix(os.path.join(self.t, "no3")), BACKUP_DIR=tor.posix(os.path.join(self.t, "bk2")))
        self.assertEqual(rc, 1, out)
        self.assertIn("nothing to back up", out)


class UninstallRemovesTheOpsFiles(unittest.TestCase):
    """uninstall.sh (its real main under the harness of test_lots_review) removes the v3 units and the
    three system files; nothing outside the temp directory is touched (the paths hang off ETC_DIR)."""

    def setUp(self):
        self.sh = tor.find_bash()
        if self.sh is None:
            self.skipTest("no bash that can read this checkout")
        self.t = tempfile.mkdtemp(prefix="uninstall_v3_")
        self.addCleanup(shutil.rmtree, self.t, True)
        kit = os.path.join(self.t, "kit", "deploy")
        os.makedirs(kit)
        shutil.copy(os.path.join(ROOT, "deploy", "lib.sh"), kit)
        src = read("deploy", "uninstall.sh")
        self.assertTrue(src.rstrip().endswith('main "$@"'))
        with open(os.path.join(kit, "uninstall_nomain.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write(src.rstrip()[:-len('main "$@"')])
        harness = r'''
set -euo pipefail
. "$KIT/deploy/uninstall_nomain.sh"
APP_DIR="$T/opt/bitpin-bot"; ETC_DIR="$T/etc"; STATE_DIR="$T/state"; UNIT_DIR="$T/units"; BACKUP_DIR="$T/backups"
PAPER_STATE_DIR="$T/pstate"; NOTIFY_STATE_DIR="$T/nstate"; CLI_LINK="$T/bin/bitpin-bot"; PYTHON="$PY"
require_root() { :; }
systemctl() { echo "systemctl $*" >> "$T/calls"; return 0; }
main "$@"
'''
        with open(os.path.join(kit, "harness.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write(harness)
        self.app = os.path.join(self.t, "opt", "bitpin-bot")
        os.makedirs(os.path.join(self.app, "scripts"))
        for d in ("etc", "state", "units", "backups", "bin", "needrestart/conf.d", "logrotate.d",
                  "systemd/journald.conf.d"):
            os.makedirs(os.path.join(self.t, d))
        with open(os.path.join(self.t, "state", "live_orders.json"), "w") as f:
            json.dump({"orders": {}}, f)
        self.conf = [os.path.join(self.t, "needrestart", "conf.d", "bitpin-bot.conf"),
                     os.path.join(self.t, "logrotate.d", "bitpin-bot")]
        for p in self.conf:
            open(p, "w").close()
        for u in ("bitpin-bot-backup.timer", "bitpin-bot-backup.service", "bitpin-bot-failed@.service",
                  "bitpin-bot.service"):
            open(os.path.join(self.t, "units", u), "w").close()

    def test_uninstall_removes_the_ops_units_and_files(self):
        e = dict(os.environ, KIT=tor.posix(os.path.join(self.t, "kit")), T=tor.posix(self.t), PY=tor.posix(sys.executable))
        p = subprocess.run([self.sh, tor.posix(os.path.join(self.t, "kit", "deploy", "harness.sh"))],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e)
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out)
        self.assertFalse(any(os.path.exists(c) for c in self.conf), out)
        self.assertEqual(os.listdir(os.path.join(self.t, "units")), [])
        with open(os.path.join(self.t, "calls"), encoding="utf-8") as f:
            calls = f.read()
        for u in ("bitpin-bot-backup.timer", "bitpin-bot-backup.service", "bitpin-bot-failed@.service"):
            self.assertIn("systemctl disable --now %s" % u, calls)
        self.assertIn("systemctl reset-failed bitpin-bot-failed@bitpin-bot.service", calls)
        self.assertNotIn("systemd-journald", calls)                    # nothing system-wide to undo
        self.assertGreaterEqual(out.count("[ OK ] removed"), 3)   # the two system files, then units and code


if __name__ == "__main__":
    unittest.main()
