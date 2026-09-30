"""Regression tests of the ops / upgrade review of the release (scratch/release_review_ops): the unit tests
must pass inside the copy update.sh stages (stage_code leaves files out on purpose), the Persian (Jalali)
endgame dates in the guides, the llm.base_url / news.base_url checks and the deploy notes. No network."""
import ast
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import test_integration as ti  # noqa: E402


# --------------------------------------------------------------------------- the staged copy update.sh tests

def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


def stage_excludes(with_tests=1, with_data=0):
    """The tar --exclude patterns of deploy/lib.sh's stage_code, as update.sh / install.sh call it by default
    (tests/ copied, data/ not)."""
    lib = read("deploy", "lib.sh")
    body = lib[lib.index("stage_code() {"):]
    block = body[body.index("local -a ex=("):body.index("\n    )")]
    block = "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))   # comments may quote
    pats = re.findall(r"--exclude='([^']+)'", block)
    if "--exclude-vcs" in block:
        # GNU tar's --exclude-vcs: the VCS directories and the git dot-files (the checkout is a git repository)
        pats += [".git", ".gitignore", ".gitattributes", ".gitmodules", ".hg", ".svn", "CVS"]
    if not with_tests:
        pats.append("./tests")
    if not with_data:
        pats.append("./data")
    return pats


def left_out(rel, pats):
    """True when GNU tar (default exclusion matching: not anchored, '*' matches '/') leaves `rel` out of
    `tar -C SRC ... -cf - .`: a pattern starting with ./ matches the member name ./REL, any other pattern
    matches any trailing run of its path components; an excluded directory takes its whole subtree."""
    parts = rel.split("/")
    for n in range(1, len(parts) + 1):
        q = parts[:n]
        for pat in pats:
            if pat.startswith("./"):
                if fnmatch.fnmatchcase("./" + "/".join(q), pat):
                    return True
            elif any(fnmatch.fnmatchcase("/".join(q[i:]), pat) for i in range(len(q))):
                return True
    return False


def split_tree(root, pats):
    """(files stage_code copies, files it leaves out outside the directories it leaves out whole)."""
    keep, out = [], []
    for d, dirs, files in os.walk(root):
        reld = os.path.relpath(d, root).replace(os.sep, "/")
        pre = "" if reld == "." else reld + "/"
        dirs[:] = sorted(x for x in dirs if not left_out(pre + x, pats))
        for f in sorted(files):
            (out if left_out(pre + f, pats) else keep).append(pre + f)
    return keep, out


def _strings(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n.value
        elif type(n).__name__ == "Str":                     # Python 3.7's parser
            yield n.s


def tests_naming(basenames):
    """unittest ids of the test methods under tests/ that name one of `basenames` in a string literal."""
    found = []
    for fn in sorted(os.listdir(HERE)):
        if not (fn.startswith("test_") and fn.endswith(".py")) or fn == os.path.basename(__file__):
            continue
        with open(os.path.join(HERE, fn), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for fun in cls.body:
                if not (isinstance(fun, ast.FunctionDef) and fun.name.startswith("test")):
                    continue
                if any(s in basenames or any(s.endswith("/" + b) for b in basenames) for s in _strings(fun)):
                    found.append("%s.%s.%s" % (fn[:-3], cls.name, fun.name))
    return found


class TestSuiteRunsInTheStagedCopy(unittest.TestCase):
    """update.sh runs `python -m unittest discover -s tests` inside /opt/bitpin-bot.new, the copy stage_code
    made. A test that opened scripts/kimi_check.py (left out of that copy on purpose) failed there, and every
    update stopped at its test step with "tests failed ... update aborted"."""

    def test_the_emulated_excludes_match_the_real_stage_code(self):
        pats = stage_excludes()
        self.assertIn("./scripts/kimi_check.py", pats)
        self.assertIn("./scratch", pats)
        self.assertIn("./data", pats)
        self.assertNotIn("./tests", pats)
        self.assertTrue(left_out("scripts/kimi_check.py", pats))
        self.assertTrue(left_out("deploy/bitpin-bot.env", pats))
        self.assertTrue(left_out("bitpin/__pycache__/x.cpython-314.pyc", pats))
        self.assertTrue(left_out(".git/HEAD", pats))
        # v3 (spec F4): research data, development notes and review findings stay out; the prompt template ships
        for rel in ("research/README.md", "research/03_pump_study/x.csv", "docs/dev_notes/patch_plan.md",
                    "docs/reviews/release_findings.json"):
            self.assertTrue(left_out(rel, pats), rel)
        for rel in ("bitpin/prompt_template.txt", "docs/DEPLOY_FA.md", "docs/TELEGRAM_FA.md",
                    "docs/STRATEGY_KNOWLEDGE.md", "deploy/bitpin-bot-backup.timer", "deploy/logrotate-bitpin-bot",
                    "deploy/needrestart-bitpin-bot.conf"):
            self.assertFalse(left_out(rel, pats), rel)
        self.assertFalse(left_out("deploy/bitpin-bot.env.example", pats))
        self.assertFalse(left_out("scripts/run_bot.py", pats))
        self.assertFalse(left_out("tests/test_ops_release.py", pats))
        sh = ti.DeployKitTest._bash()
        if sh is None:
            self.skipTest("no bash that can read this checkout")
        tmp = tempfile.mkdtemp(prefix="bitpin-stage-")
        try:
            stage = os.path.join(tmp, "stage")
            script = ". '%s/deploy/lib.sh'\nstage_code '%s' '%s' 1 0\n" % (
                ROOT.replace("\\", "/"), ROOT.replace("\\", "/"), stage.replace("\\", "/"))
            proc = subprocess.Popen([sh, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = proc.communicate()[0].decode("utf-8", "replace")
            if proc.returncode != 0 and "tar" in out and "not found" in out:
                self.skipTest("no tar: " + out[-200:])
            self.assertEqual(proc.returncode, 0, out)
            real = sorted(split_tree(stage, [])[0])
            self.assertEqual(real, sorted(split_tree(ROOT, pats)[0]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_tests_that_name_a_left_out_file_pass_in_the_staged_copy(self):
        pats = stage_excludes()
        keep, out = split_tree(ROOT, pats)
        names = set(os.path.basename(p) for p in out
                    if "__pycache__" not in p and not p.endswith((".pyc", ".pyo")))
        if not names:
            self.skipTest("this is a deployed copy: nothing in it is left out by stage_code")
        cands = tests_naming(names)
        if os.path.exists(os.path.join(ROOT, "scripts", "kimi_check.py")):
            # the detector finds the test that broke the update
            self.assertIn("test_integration.DeployKitTest."
                          "test_update_prunes_failed_copies_and_the_kit_never_ships_the_key_tool", cands)
        if not cands:
            return          # (an empty name list would make unittest run the whole suite again)
        tmp = tempfile.mkdtemp(prefix="bitpin-stage-")
        try:
            stage = os.path.join(tmp, "stage")
            for rel in keep:
                dst = os.path.join(stage, *rel.split("/"))
                if not os.path.isdir(os.path.dirname(dst)):
                    os.makedirs(os.path.dirname(dst))
                shutil.copyfile(os.path.join(ROOT, *rel.split("/")), dst)
            for rel in out:
                self.assertFalse(os.path.exists(os.path.join(stage, *rel.split("/"))), rel)
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TZ="UTC")
            proc = subprocess.Popen([sys.executable, "-m", "unittest", "-q"] + cands,
                                    cwd=os.path.join(stage, "tests"), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            text = proc.communicate()[0].decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 0, text[-3000:])
            self.assertIn("Ran %d test" % len(cands), text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- Persian (Jalali) endgame dates

FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
JALALI_MONTHS_FA = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن",
                    "اسفند"]


def fa_int(s):
    return int("".join(str(FA_DIGITS.index(c)) if c in FA_DIGITS else c for c in s))


def fa_num(n):
    return "".join(FA_DIGITS[int(c)] for c in str(n))


class TestJalaliDatesInTheGuides(unittest.TestCase):
    """The guides once put the endgame one day late (26 / 30 Mehr). Every '<day> <month> [<year>] 13:00'
    the guides name must be an endgame instant of the SHIPPED kimi.example.json, converted with the
    bot's own calendar - for v3 the one-year competition: no new entries 2027-09-16 13:00 Tehran =
    25 Shahrivar 1406, the FINAL decision 2027-09-20 13:00 = 29 Shahrivar 1406 (the end 2027-09-21 =
    30 Shahrivar 1406); both guides must name both."""

    @staticmethod
    def jalali(d):
        from bitpin import notify
        return notify.gregorian_to_jalali(d.year, d.month, d.day)

    def endgame_dates(self):
        import datetime
        import json
        with open(os.path.join(ROOT, "kimi.example.json"), encoding="utf-8") as f:
            eg = json.load(f)["brain"]["endgame"]
        dates = {}
        for k in ("no_new_entries_at", "final_at"):
            self.assertTrue(eg[k].endswith("T13:00:00+03:30"), eg[k])
            dates[k] = datetime.date(*[int(x) for x in eg[k][:10].split("-")])
        return dates

    def test_the_bots_own_calendar(self):
        import datetime
        self.assertEqual(self.jalali(datetime.date(2026, 9, 23)), (1405, 7, 1))
        self.assertEqual(self.jalali(datetime.date(2026, 10, 17)), (1405, 7, 25))
        self.assertEqual(self.jalali(datetime.date(2027, 9, 16)), (1406, 6, 25))
        self.assertEqual(self.jalali(datetime.date(2027, 9, 20)), (1406, 6, 29))
        self.assertEqual(self.jalali(datetime.date(2027, 9, 21)), (1406, 6, 30))

    def test_the_shipped_endgame_is_the_one_year_competition(self):
        """Spec A3: kimi.example.json and brain.DEFAULT_ENDGAME_CONFIG carry the 2027 dates."""
        import datetime
        from bitpin import brain
        eg = self.endgame_dates()
        self.assertEqual(eg["no_new_entries_at"], datetime.date(2027, 9, 16))
        self.assertEqual(eg["final_at"], datetime.date(2027, 9, 20))
        for k in ("no_new_entries_at", "final_at"):
            self.assertTrue(brain.DEFAULT_ENDGAME_CONFIG[k].startswith(eg[k].isoformat()),
                            (k, brain.DEFAULT_ENDGAME_CONFIG[k]))

    def test_every_13h_jalali_date_in_the_guides_is_an_endgame_instant(self):
        import datetime
        eg = self.endgame_dates()
        instants = {}
        for k, d in eg.items():
            y, m, day = self.jalali(d)
            instants[(day, JALALI_MONTHS_FA[m - 1], y)] = d
        months = "|".join(JALALI_MONTHS_FA)
        at13 = re.compile(r"([۰-۹]{1,2}) (%s)(?: ([۰-۹]{4}))?(?: ساعت)?\**\s*۱۳:۰۰" % months)
        found = set()
        for doc in ("DEPLOY_FA.md", "TELEGRAM_FA.md"):
            text = read("docs", doc)
            for i, line in enumerate(text.splitlines(), 1):
                for mt in at13.finditer(line):
                    day, month, year = fa_int(mt.group(1)), mt.group(2), mt.group(3)
                    cands = [d for (dd, mm, yy), d in instants.items() if dd == day and mm == month
                             and (year is None or fa_int(year) == yy)]
                    self.assertTrue(cands, "%s:%d: %s is not an endgame date (%s)" % (
                        doc, i, mt.group(0), sorted(instants)))
                    found.add((doc, cands[0]))
        for doc in ("DEPLOY_FA.md", "TELEGRAM_FA.md"):
            for k, d in eg.items():
                self.assertIn((doc, d), found, "%s never names %s (%s) in Jalali" % (doc, k, d))
        # the one-month competition's dates are gone from the guides (they would be read as this year's)
        for wrong in ("۲۵ مهر", "۲۹ مهر", "۲۶ مهر", "۳۰ مهر ۱۳:۰۰", "۳۰ مهر ساعت ۱۳:۰۰", "۲۲ اکتبر ۲۰۲۶"):
            self.assertNotIn(wrong, read("docs", "DEPLOY_FA.md"))
            self.assertNotIn(wrong, read("docs", "TELEGRAM_FA.md"))

    def test_jalali_and_gregorian_side_by_side_agree(self):
        import datetime
        months = "|".join(JALALI_MONTHS_FA)
        greg = {"ژانویه": 1, "فوریه": 2, "مارس": 3, "آوریل": 4, "مه": 5, "ژوئن": 6, "ژوئیه": 7, "اوت": 8,
                "سپتامبر": 9, "اکتبر": 10, "نوامبر": 11, "دسامبر": 12}
        pair = re.compile(r"([۰-۹]{1,2}) (%s)(?: ([۰-۹]{4}))?[^()\n]*?\(([۰-۹]{1,2}) (%s)(?: ([۰-۹]{4}))?\)"
                          % (months, "|".join(greg)))
        n = 0
        for doc in ("DEPLOY_FA.md", "TELEGRAM_FA.md"):
            for i, line in enumerate(read("docs", doc).splitlines(), 1):
                for mt in pair.finditer(line):
                    jd, jm, jy, gd, gm, gy = mt.groups()
                    jy = fa_int(jy) if jy else None
                    gy = fa_int(gy) if gy else None
                    # find the Gregorian date of that Jalali day in the plausible years
                    ok = False
                    for y in ((gy,) if gy else (2026, 2027)):
                        d = datetime.date(y, greg[gm], fa_int(gd))
                        jyy, jmm, jdd = self.jalali(d)
                        if (jdd, JALALI_MONTHS_FA[jmm - 1]) == (fa_int(jd), jm) and (jy is None or jy == jyy):
                            ok = True
                    self.assertTrue(ok, "%s:%d: %s" % (doc, i, mt.group(0)))
                    n += 1
        self.assertGreaterEqual(n, 3)       # 25 Shahrivar (16 Sep), 29 Shahrivar (20 Sep), 30 Shahrivar / 21 Sep


# --------------------------------------------------------------------------- llm.base_url / news.base_url

BAD_URLS = ("https://user:pw@api.moonshot.cn/v1", "https://api.moonshot.cn/v1?x=1", "https://api.moonshot.cn/v1#f",
            "https://api.moonshot.cn/v1?", "https://api.moonshot.cn:99999/v1", "https://api.moonshot.cn:abc/v1",
            "https://:pw@api.moonshot.ai/v1", "http://api.moonshot.ai/v1", "https:///v1")


class TestBaseUrlChecks(unittest.TestCase):
    """validate_llm_config accepted https://user:pw@host/v1?x=1: every Kimi call then went to
    '.../v1?x=1/chat/completions' (48 h later the bot derisks) and the password was printed wherever base_url is
    shown. Now both stages reject what notify._check_https rejects, without repeating the password."""

    def test_llm_and_news_reject_credentials_query_fragment_and_bad_ports(self):
        from bitpin.llm import ConfigError, validate_llm_config
        from bitpin.news import NewsConfigError, validate_news_config
        for bad in BAD_URLS:
            with self.assertRaises(ConfigError, msg=bad) as cm:
                validate_llm_config({"base_url": bad})
            self.assertNotIn("pw", str(cm.exception), bad)
            with self.assertRaises(NewsConfigError, msg=bad) as cm:
                validate_news_config({"base_url": bad})
            self.assertNotIn("pw", str(cm.exception), bad)
        for good in ("https://api.moonshot.ai/v1", "https://api.moonshot.cn/v1/", " https://api.moonshot.ai/v1 ",
                     "https://api.moonshot.ai:443/v1"):
            self.assertEqual(validate_llm_config({"base_url": good})["base_url"], good)
            validate_news_config({"base_url": good})

    def test_the_example_files_pass(self):
        import json
        from bitpin.llm import validate_llm_config
        from bitpin.news import validate_news_config
        with open(os.path.join(ROOT, "kimi.example.json"), encoding="utf-8") as f:
            k = json.load(f)
        validate_llm_config(k["llm"])
        validate_news_config(k["news"])


class TestSetModelBaseUrl(unittest.TestCase):
    def setUp(self):
        import logging
        self.dir = tempfile.mkdtemp(prefix="bitpin_set_model_")
        self.rb = ti.load_run_bot()
        self.path = os.path.join(self.dir, "kimi.json")
        shutil.copy(os.path.join(ROOT, "kimi.example.json"), self.path)
        self.bak = os.path.join(self.dir, "backups")
        self.handlers = list(logging.getLogger().handlers)

    def tearDown(self):
        import logging
        root = logging.getLogger()
        for h in list(root.handlers):
            if h not in self.handlers:
                root.removeHandler(h)
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_main(self, *argv):
        import contextlib
        import io
        from unittest import mock
        calls = []

        def transport(*a, **kw):
            calls.append(a)
            raise AssertionError("no network call expected")
        out = io.StringIO()
        argv = ["set-model", "--kimi-config", self.path, "--backup-dir", self.bak] + list(argv)
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": ti.KEY}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out), \
                mock.patch("bitpin.llm.make_llm_transport", lambda proxy=None, opener=None: transport):
            try:
                code = self.rb.main(argv)
            except SystemExit as e:
                code = e.code
        text = out.getvalue()
        self.assertNotIn(ti.KEY, text)
        self.assertEqual(calls, [])
        return code, text

    def raw(self):
        with open(self.path, "rb") as f:
            return f.read()

    def test_a_mistyped_base_url_is_refused_and_nothing_is_written(self):
        before = self.raw()
        for bad in BAD_URLS:
            code, text = self.run_main("--base-url", bad, "--dry-run")
            self.assertEqual(code, 1, text)
            self.assertIn("--base-url refused", text)
            self.assertNotIn("pw@", text)
        # a host that is not Moonshot's: --list / --test would send the key there at once
        for host in ("https://api.moonshot.ai.example.com/v1", "https://api.moonshot.co/v1"):
            code, text = self.run_main("--base-url", host, "--list")
            self.assertEqual(code, 1, text)
            self.assertIn("is not a Moonshot API host", text)
            self.assertIn("--any-host", text)
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.isdir(self.bak) and os.listdir(self.bak))

    def test_moonshot_hosts_pass_and_any_host_is_explicit(self):
        code, text = self.run_main("--base-url", "https://api.moonshot.cn/v1", "--dry-run")
        self.assertEqual(code, 0, text)
        self.assertIn("https://api.moonshot.cn/v1", text)
        code, text = self.run_main("--base-url", "https://kimi.relay.example.org/v1", "--any-host", "--dry-run")
        self.assertEqual(code, 0, text)
        self.assertIn("https://kimi.relay.example.org/v1", text)
        # --any-host does not let a password / query through
        code, text = self.run_main("--base-url", "https://u:pw@kimi.relay.example.org/v1", "--any-host", "--dry-run")
        self.assertEqual(code, 1, text)
        self.assertNotIn("pw@", text)


# --------------------------------------------------------------------------- ladder rejections, deploy headers

class TestLadderRejectionAdvice(unittest.TestCase):
    """The advice for ladder bids rejected as too small was 'set ladder.coins to [BTC, ETH]', which does not
    change the size of a bid (size_frac x scale x equity): the bids stayed below the minimum."""

    def test_fewer_coins_do_not_make_a_bid_larger_size_frac_does(self):
        from bitpin.runner import ladder_plan
        refs4 = {c: {"hi48": 100.0} for c in ("BTC", "ETH", "XRP", "SOL")}
        refs2 = {c: refs4[c] for c in ("BTC", "ETH")}
        levels = (-20, -25)
        eq, rate = 16.8 * 230000.0, 230000.0            # about 16.8 USDT, all of it USDT
        w4, f4 = ladder_plan(refs4, {c: 1.0 for c in refs4}, {}, levels, 0.125, eq, rate, 16.8, 0.5)
        w2, f2 = ladder_plan(refs2, {c: 1.0 for c in refs2}, {}, levels, 0.125, eq, rate, 16.8, 0.5)
        w2b, _ = ladder_plan(refs2, {c: 1.0 for c in refs2}, {}, levels, 0.25, eq, rate, 16.8, 0.5)
        size = lambda w: sorted(set(round(x["usdt"], 6) for x in w.values()))  # noqa: E731
        self.assertEqual(size(w4), [2.1])
        self.assertEqual(size(w2), [2.1])                   # the same bid size with half the coins
        self.assertEqual(size(w2b), [4.2])                  # size_frac 0.25 x 2 coins: the same total, 2x bids
        self.assertAlmostEqual(sum(x["usdt"] for x in w2b.values()), sum(x["usdt"] for x in w4.values()))

    def test_the_guide_and_the_ladder_command_give_the_real_fix_and_log_text(self):
        doc = read("docs", "DEPLOY_FA.md")
        rows = [ln for ln in doc.splitlines() if ln.startswith("| `ladder limit buy BTC_USDT failed: HTTP 4..`")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("size_frac" in r and "enabled" in r and "0.25" in r for r in rows), rows)
        self.assertIn("| `ladder limit buy BTC_USDT not placed: ...` |", doc)
        # the log lines the guide quotes are the runner's own formats (tag "ladder", side "buy")
        runner = read("bitpin", "runner.py")
        self.assertIn('log.error("%s limit %s %s failed: %s", tag, side, sym, e)', runner)
        self.assertIn('log.warning("%s limit %s %s not placed: %s", tag, side, sym, e)', runner)
        rb = read("scripts", "run_bot.py")
        self.assertNotIn("the research's fallback is ladder.coins", rb)
        self.assertIn("fewer coins alone do NOT make a bid larger: raise", rb)


class TestDeployHeaders(unittest.TestCase):
    def test_file_modes_in_the_headers_match_lib_sh(self):
        lib = read("deploy", "lib.sh")
        self.assertIn('install_if_absent "$app/deploy/bitpin-bot.env.example" "$ENV_FILE" 0600 root', lib)
        self.assertIn('install_if_absent "$app/deploy/notify.env.example" "$NOTIFY_ENV_FILE" 0600 root', lib)
        self.assertIn('install_if_absent "$app/config.example.json" "$ETC_DIR/config.json" 0640', lib)
        inst = read("deploy", "install.sh")
        head = inst[:inst.index("set -euo pipefail")]
        self.assertIn("bitpin-bot.env and notify.env 0600 root:root", head)
        self.assertIn("the JSON settings 0640 root:bitpin", head)
        self.assertNotIn("(mode 0640\n#     root:bitpin)", head)
        upd = read("deploy", "update.sh")
        head = upd[:upd.index("set -euo pipefail")]
        self.assertNotIn("are never modified. A kimi.json", head)
        self.assertIn("normalised", head)
        # the stray resting_count comment above run_tests is gone
        before = upd[:upd.index("run_tests() {")].rstrip().splitlines()[-2:]
        self.assertFalse(any("limit orders" in ln for ln in before), before)


if __name__ == "__main__":
    unittest.main()
