#!/usr/bin/env python3
"""Apply the DAILY schedule + crash ladder profile to the server's settings (stdlib only, 3.8+).

Run it as root on the server, from the unpacked new version, AFTER update.sh:
    sudo python3 deploy/apply_profile.py              (shows every change, backs up, writes, validates)
    sudo python3 deploy/apply_profile.py --dry-run    (only shows what would change; writes nothing)

What it does, in this order:
  1. reads /etc/bitpin-bot/kimi.json and /etc/bitpin-bot/config.json and this version's
     kimi.example.json / config.example.json (the profile's values come ONLY from them);
  2. changes ONLY the keys of the profile (PROFILE_KIMI / PROFILE_CONFIG below):
       kimi.json   llm      timeouts 420/900 s, streaming, budgets 24 calls / 300,000 tokens a day, the
                            kimi-k3 reasoning effort (v3)
                   brain    the daily 19:00 schedule, wake-ups, pacing, derisk 48 h, the ONE-YEAR endgame
                            (2027-09-16 / 2027-09-20, v3), ladder coins, the owner's AGGRESSIVE style
                            (extra_instructions, with the asset-class note), the owner's WIDE universe
                            (allowed_symbols = context.universe), the analysis-check policy and the
                            slot reasoning effort (v3)
                   context  universe, compact_symbols_above (compact rows) and the one-year
                            competition_end_utc 2027-09-21T20:30Z (v3; the start date and the start
                            equity are kept)
                   news     timeouts 300/600 s, streaming, the brief once a day (cache 1380 min), the
                            after-HOLD reuse (v3), the daily call budget
       config.json the new sections ladder / exits / routing and the risk keys min_order_usdt,
                   max_limit_orders_per_day, max_limit_distance - only where they are MISSING (a value
                   you already set there is kept); PLUS, since v3 (the one-year release), the keys the
                   owner re-decided for the year, SET to the example's values like the kimi.json profile
                   (PROFILE_CONFIG_SET, every change is shown): risk.max_drawdown 0.50 on a rolling
                   risk.hwm_window_days 90 high-water mark, drawdown_action halt, min_order_usdt 1.05,
                   ladder.levels_pct [-20], ladder.size_frac 0.25
     Every other value (the model, the competition START date, the start equity, the risk profile and
     limits, your own edits) is kept as it is. A missing "news" section is copied whole. The
     matching "_..." comments are updated too. Secrets are never touched: they live in
     bitpin-bot.env, which this script never opens;
  3. validates the NEW files with the bot's own code (exactly like 'sudo bitpin-bot check' /
     update.sh: runner.load_config, the risk manager, brain.check_kimi_config) BEFORE anything is
     written - a refused file changes nothing. The files must ALSO be accepted by the INSTALLED bot
     (/opt/bitpin-bot, the code the service runs): when it is not this version (update.sh has not
     run, or it failed and rolled back), its own offline check (deploy/check_server.py --config-only)
     must accept them, or nothing is written - an older bot would stop at its next restart (exit 78)
     on the new keys;
  4. copies the old files to /var/backups/bitpin-bot/<name>.<UTC time>.before-profile (0600 root) and
     replaces them atomically, keeping their owner and mode (0640 root:bitpin);
  5. prints that confirm-live must be run again: the live confirmation covers every value of both
     files, so the live service refuses to start (exit 78, no trading) until 'sudo bitpin-bot
     confirm-live' - and a running bot keeps its OLD settings until it is restarted.
Running it again changes nothing (it says so). Exit status: 0 = applied / nothing to do, 1 = refused
(nothing written), 2 = usage.
"""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import tempfile
import time
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
DEFAULT_ETC = "/etc/bitpin-bot"
DEFAULT_BACKUP_DIR = "/var/backups/bitpin-bot"
DEFAULT_INSTALLED = "/opt/bitpin-bot"          # the code the bitpin-bot service runs (deploy/lib.sh APP_DIR)
# the files that make two trees "the same version" for the settings (the bot code, the checks, the examples)
VERSION_FILES = ("config.example.json", "kimi.example.json", "deploy/apply_profile.py", "deploy/check_server.py",
                 "scripts/run_bot.py")
INSTALLED_CHECK_TIMEOUT = 180

# kimi.json: these keys are SET to the profile's value (kimi.example.json). "a.b" = only key b of the
# object a (brain.fallback keeps its sweep_irt_above).
PROFILE_KIMI = OrderedDict([
    ("llm", ["timeout", "deadline_seconds", "max_calls_per_day", "max_tokens_per_day", "stream", "reasoning_effort"]),
    ("brain", ["allowed_symbols", "decision_times_local", "max_gap_hours", "decision_interval_hours", "event_move_pct",
               "held_move_pct", "drawdown_trigger_points", "risk_reduce_min_coin_weight", "usdt_notify_pct",
               "veto_drop_pct", "veto_rearm_pct", "ladder_coins", "honor_next_review_hours", "next_review_min_hours",
               "min_decision_spacing_minutes", "max_decisions_per_day", "max_early_decisions_per_day",
               "reserve_llm_calls", "fallback.derisk_after_hours", "endgame", "decision_deadline_seconds",
               "extra_instructions", "log_full_context", "analysis_policy", "slot_reasoning_effort"]),
    # v3 (one year): the competition end moves to 2027-09-21T20:30Z with the endgame dates above; the start
    # date and the start equity stay the owner's own values
    ("context", ["universe", "compact_symbols_above", "competition_end_utc", "matches"]),
    # v3: the after-HOLD reuse of the brief (news.after_hold_only) and the daily call budget of 2
    ("news", ["timeout", "deadline_seconds", "max_tokens", "cache_minutes", "max_stale_minutes", "max_calls_per_day",
              "stream",
              "after_hold_only"]),
])
# config.json: these sections / keys are ADDED where missing (existing values are kept).
PROFILE_CONFIG = OrderedDict([
    ("ladder", None),          # None = every key of the example's section
    ("exits", None),
    ("routing", None),
    ("risk", ["min_order_usdt", "max_limit_orders_per_day", "max_limit_distance", "max_drawdown", "hwm_window_days"]),
])
# config.json: these keys are SET to the example's values (v3, spec E1 / E2: the owner's one-year decisions -
# the halt at 50% of a rolling 90-day high-water mark instead of 30% of the all-time mark, one ladder level
# at -20% sized 25%, the 1.05 USDT minimum). Like the kimi.json profile every change is listed before it is
# written; a server whose value already matches sees no change. Everything else in config.json stays the
# owner's (min_order_irt, max_orders_per_day, the wallet caps ...).
PROFILE_CONFIG_SET = OrderedDict([
    ("risk", ["max_drawdown", "hwm_window_days", "drawdown_action", "min_order_usdt"]),
    ("ladder", ["levels_pct", "size_frac"]),
])

_ITEM = r'(?:"[^"\n]*"|null|true|false|-?[0-9.eE+-]+)'
_LIST_RE = re.compile(r'\[\n\s+(' + _ITEM + r'(?:,\n\s+' + _ITEM + r')*)\n\s+\]')


class Refused(Exception):
    pass


def _dup_check(pairs):
    d = OrderedDict()
    for k, v in pairs:
        if k in d:
            raise Refused("key %r appears twice in the same object" % k)
        d[k] = v
    return d


def load(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            doc = json.load(f, object_pairs_hook=_dup_check)
    except OSError as e:
        raise Refused("cannot read %s: %s" % (path, e.strerror or e))
    except ValueError as e:
        raise Refused("%s is not valid JSON: %s - fix it first (sudo nano %s), then run this again" % (path, e, path))
    if not isinstance(doc, dict):
        raise Refused("%s must contain a JSON object {...}" % path)
    return doc


def dumps(doc):
    text = json.dumps(doc, indent=2, ensure_ascii=False)
    text = _LIST_RE.sub(lambda m: "[" + ", ".join(x.strip() for x in m.group(1).split(",\n")) + "]", text)
    return text + "\n"


def show(v, n=90):
    s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= n else s[:n - 3] + "..."


MISSING = "(missing)"
# kimi.json sections whose explicit null means "switched off" (kept); a missing one is copied whole
NULL_KEEPS = ("news",)
# comment keys of the example that document profile keys without being named "_<key>"
EXTRA_COMMENTS = {"brain": ["_next_review", "_section"], "llm": ["_section"], "news": ["_section"],
                  "context": ["_section"]}


def same(a, b):
    """Equal as settings: 24 == 24.0, but True is not 1 and "1" is not 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    if isinstance(a, dict) and isinstance(b, dict):
        ka = {k for k in a if not str(k).startswith("_")}
        kb = {k for k in b if not str(k).startswith("_")}
        return ka == kb and all(same(a[k], b[k]) for k in ka)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def apply_kimi(cur, ex, changes, notes=None):
    """cur: the server's kimi.json (modified in place); ex: kimi.example.json. The profile's keys are
    SET to the example's values; their "_" comments follow. A section the owner set to null on
    purpose (NULL_KEEPS: "news") is kept null; a MISSING section is copied whole."""
    notes = [] if notes is None else notes
    if "_README" in ex:
        cur["_README"] = ex["_README"]
    for sec, keys in PROFILE_KIMI.items():
        exs = ex.get(sec)
        if not isinstance(exs, dict):
            raise Refused("kimi.example.json has no %r section (a broken download?)" % sec)
        if sec in cur and cur[sec] is None and sec in NULL_KEEPS:
            # an explicit null is the owner's choice (e.g. "news": null = no stage-1 research): kept
            notes.append("kimi.json %s is null (switched off): kept as it is" % sec)
            continue
        if cur.get(sec) is None:
            cur[sec] = copy.deepcopy(exs)
            changes.append(("kimi.json", sec, MISSING, "the whole section of kimi.example.json"))
            continue
        if not isinstance(cur[sec], dict):
            raise Refused("kimi.json section %r is not an object {...}: fix it first" % sec)
        cs = cur[sec]
        for key in keys:
            obj, sub = key.split(".", 1) if "." in key else (key, None)
            if obj not in exs or (sub is not None and not (isinstance(exs[obj], dict) and sub in exs[obj])):
                raise Refused("kimi.example.json has no %s.%s (a broken download?)" % (sec, key))
            if sub is not None and isinstance(cs.get(obj), dict):
                old, want = cs[obj].get(sub, MISSING), exs[obj][sub]
                if old is MISSING or not same(old, want):
                    changes.append(("kimi.json", "%s.%s" % (sec, key), old, want))
                    cs[obj][sub] = copy.deepcopy(want)
            else:                               # a whole key (or an object that is missing / not an object)
                old, want = cs.get(obj, MISSING), exs[obj]
                if old is MISSING or not same(old, want):
                    changes.append(("kimi.json", "%s.%s" % (sec, obj), old, want))
                    cs[obj] = copy.deepcopy(want)
            if "_" + obj in exs:
                cs["_" + obj] = exs["_" + obj]
        for ck in EXTRA_COMMENTS.get(sec, []):
            if ck in exs:
                cs[ck] = exs[ck]
    return cur


def apply_config(cur, ex, changes):
    """cur: the server's config.json (modified in place); ex: config.example.json. Missing keys only."""
    for sec, keys in PROFILE_CONFIG.items():
        exs = ex.get(sec)
        if not isinstance(exs, dict):
            raise Refused("config.example.json has no %r section (a broken download?)" % sec)
        if sec not in cur or cur[sec] is None:
            if keys is None:
                cur[sec] = copy.deepcopy(exs)
                changes.append(("config.json", sec, MISSING, OrderedDict((k, v) for k, v in exs.items()
                                                                         if not k.startswith("_"))))
                continue
            cur[sec] = OrderedDict()
        if not isinstance(cur[sec], dict):
            raise Refused("config.json %r is not an object: fix it first" % sec)
        cs = cur[sec]
        for key in (keys if keys is not None else [k for k in exs if not k.startswith("_")]):
            if key not in exs:
                raise Refused("config.example.json has no %s.%s (a broken download?)" % (sec, key))
            if key in cs:
                continue                      # the user's value is kept
            cs[key] = copy.deepcopy(exs[key])
            if "_" + key in exs:
                cs["_" + key] = exs["_" + key]
            changes.append(("config.json", "%s.%s" % (sec, key), MISSING, exs[key]))
    # the v3 SET keys (after the missing-key pass, so a section copied whole above is simply confirmed)
    for sec, keys in PROFILE_CONFIG_SET.items():
        exs = ex.get(sec)
        if not isinstance(exs, dict):
            raise Refused("config.example.json has no %r section (a broken download?)" % sec)
        if not isinstance(cur.get(sec), dict):
            raise Refused("config.json %r is not an object: fix it first" % sec)
        cs = cur[sec]
        for key in keys:
            if key not in exs:
                raise Refused("config.example.json has no %s.%s (a broken download?)" % (sec, key))
            old, want = cs.get(key, MISSING), exs[key]
            if old is MISSING or not same(old, want):
                changes.append(("config.json", "%s.%s" % (sec, key), old, want))
                cs[key] = copy.deepcopy(want)
            if "_" + key in exs:
                cs["_" + key] = exs["_" + key]
    if "_endgame_and_derisk" in ex and "_endgame_and_derisk" not in cur:
        cur["_endgame_and_derisk"] = ex["_endgame_and_derisk"]
    if "_README" in ex and cur.get("_README") != ex["_README"]:
        cur["_README"] = ex["_README"]
    return cur


def validate(kimi_text, config_text, project_root):
    """The bot's own checks on the NEW files (in a private temporary directory). Returns (problems,
    warnings)."""
    sys.path.insert(0, project_root)
    try:
        from bitpin import brain as brain_mod
        from bitpin import risk as risk_mod
        from bitpin import runner as runner_mod
    except Exception as e:  # noqa: BLE001
        return ["cannot load the bot code from %s (%s: %s)" % (project_root, type(e).__name__, e)], []
    finally:
        sys.path.pop(0)
    d = tempfile.mkdtemp(prefix="bitpin_profile_")
    problems, warnings = [], []
    try:
        os.chmod(d, 0o700)
        kp, cp = os.path.join(d, "kimi.json"), os.path.join(d, "config.json")
        for p, t in ((kp, kimi_text), (cp, config_text)):
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
        runner_cfg = None
        try:
            runner_cfg = runner_mod.load_config(cp)
            risk_mod.RiskManager(runner_cfg.get("risk"), d, "live", persist=False)
        except Exception as e:  # noqa: BLE001 - whatever the bot would die of
            problems.append("config.json: the bot would refuse it - %s: %s" % (type(e).__name__, e))
        problems += ["kimi.json: " + p for p in brain_mod.check_kimi_config(kp, require_model=True,
                                                                            runner_cfg=runner_cfg,
                                                                            warnings=warnings)]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return problems, warnings


def version_fingerprint(root):
    """A hash of the files that decide which settings a tree accepts (VERSION_FILES and bitpin/*.py);
    None when root is not a bot tree."""
    import hashlib
    if not os.path.isfile(os.path.join(root, "scripts", "run_bot.py")):
        return None
    h = hashlib.sha256()
    names = list(VERSION_FILES)
    try:
        names += sorted("bitpin/" + n for n in os.listdir(os.path.join(root, "bitpin")) if n.endswith(".py"))
    except OSError:
        pass
    for n in names:
        h.update(n.encode("utf-8") + b"\0")
        try:
            with open(os.path.join(root, *n.split("/")), "rb") as f:
                h.update(f.read().replace(b"\r\n", b"\n"))
        except OSError:
            h.update(b"(missing)")
        h.update(b"\0")
    return h.hexdigest()


def installed_check(kimi_text, config_text, installed, profile_dir):
    """(problems, notes) of the INSTALLED bot for the new files. The same version as profile_dir: nothing
    more to check. Another version (update.sh not run yet, or rolled back): its own offline check
    (deploy/check_server.py --config-only, run with this Python) must accept both files."""
    if not installed or not os.path.isdir(installed):
        return [], ["no installed bot at %s (nothing else to check)" % installed] if installed else []
    if os.path.realpath(installed) == os.path.realpath(profile_dir):
        return [], []
    mine, theirs = version_fingerprint(profile_dir), version_fingerprint(installed)
    if theirs is not None and theirs == mine:
        return [], ["the installed bot (%s) is this version" % installed]
    cs = os.path.join(installed, "deploy", "check_server.py")
    if theirs is None or not os.path.isfile(cs):
        return ["the installed bot at %s is not this version and has no deploy/check_server.py to test the new files "
                "with: run update.sh from this version first (it must end with OK), then this script again"
                % installed], []
    d = tempfile.mkdtemp(prefix="bitpin_profile_installed_")
    try:
        os.chmod(d, 0o700)
        kp, cp = os.path.join(d, "kimi.json"), os.path.join(d, "config.json")
        for p, t in ((kp, kimi_text), (cp, config_text)):
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
        try:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("BITPIN_", "KIMI_"))}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            out = subprocess.run([sys.executable, cs, "--config-only", "--config", cp, "--kimi-config", kp],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=INSTALLED_CHECK_TIMEOUT,
                                 cwd=installed, env=env)
            rc, text = out.returncode, out.stdout.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - cannot tell: refuse (an old bot may stop at its next restart)
            rc, text = None, "%s: %s" % (type(e).__name__, e)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if rc == 0:
        return [], ["the installed bot (%s) is ANOTHER version, but its own check accepts the new files" % installed]
    fails = [ln.strip() for ln in text.splitlines() if "FAIL" in ln or "unknown" in ln.lower()][:6] or \
        [ln.strip() for ln in text.splitlines() if ln.strip()][-3:]
    return ["the INSTALLED bot (%s) is another version and REJECTS the new files (exit %s) - it would stop at its next "
            "restart (exit 78, no trading). Run update.sh from this version first (it must end with OK), then this "
            "script again. Its check said: %s" % (installed, rc if rc is not None else "?",
                                                  " | ".join(fails) or "(no output)")], []


def atomic_replace(path, text):
    """Write text to path atomically, keeping the file's owner and mode."""
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(prefix=".profile.", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if hasattr(os, "chown"):
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup(path, backup_dir, stamp):
    os.makedirs(backup_dir, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)
    except OSError:
        pass
    dst = os.path.join(backup_dir, "%s.%s.before-profile" % (os.path.basename(path), stamp))
    shutil.copy2(path, dst)
    os.chmod(dst, 0o600)
    return dst


def bot_running():
    try:
        out = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", "bitpin-bot"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        return out.stdout.decode("utf-8", "replace").strip() in ("active", "activating", "reloading", "deactivating")
    except Exception:  # noqa: BLE001 - not a systemd machine
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="apply the daily-schedule + crash-ladder profile to kimi.json / "
                                             "config.json (see the top of this file)")
    ap.add_argument("--etc", default=DEFAULT_ETC, help="directory of kimi.json / config.json (default %s)" % DEFAULT_ETC)
    ap.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help="where the old files are copied (default %s)"
                                                                     % DEFAULT_BACKUP_DIR)
    ap.add_argument("--profile-dir", default=PROJECT_ROOT,
                    help="the version whose kimi.example.json / config.example.json / code are used (default: this "
                         "script's own project)")
    ap.add_argument("--installed-dir", default=DEFAULT_INSTALLED,
                    help="the code the bitpin-bot service runs (default %s): it must accept the new files too "
                         "('' = do not check)" % DEFAULT_INSTALLED)
    ap.add_argument("--dry-run", action="store_true", help="only show what would change")
    args = ap.parse_args(argv)

    kpath, cpath = os.path.join(args.etc, "kimi.json"), os.path.join(args.etc, "config.json")
    if not args.dry_run and os.name == "posix" and os.path.abspath(args.etc) == DEFAULT_ETC and os.geteuid() != 0:
        print("run it with sudo: sudo python3 %s" % os.path.relpath(__file__), file=sys.stderr)
        return 2
    try:
        kimi, conf = load(kpath), load(cpath)
        kex = load(os.path.join(args.profile_dir, "kimi.example.json"))
        cex = load(os.path.join(args.profile_dir, "config.example.json"))
        changes, notes = [], []
        new_kimi = apply_kimi(copy.deepcopy(kimi), kex, changes, notes)
        new_conf = apply_config(copy.deepcopy(conf), cex, changes)
    except Refused as e:
        print("REFUSED: %s\nNothing was changed." % e, file=sys.stderr)
        return 1
    kimi_text, conf_text = dumps(new_kimi), dumps(new_conf)
    print("Profile: daily Kimi decision at 19:00 Tehran + crash ladder + code exits + COIN_USDT routing")
    print("  settings : %s, %s" % (kpath, cpath))
    print("  profile  : %s (kimi.example.json, config.example.json)" % args.profile_dir)
    for n in notes:
        print("  kept     : %s" % n)
    if not changes:
        comments_only = kimi_text != dumps(kimi) or conf_text != dumps(conf)
        print("Nothing to change: the settings already follow the profile%s." % (
            " (only comments would be refreshed)" if comments_only else ""))
    else:
        print("Changes (%d):" % len(changes))
        for f, key, old, new in changes:
            print("  %-11s %-40s %s -> %s" % (f, key, MISSING if old is MISSING else show(old, 60), show(new, 80)))
            # a long text (extra_instructions) is shown whole, so the owner can read what is replaced
            for label, val in (("before", old), ("after", new)):
                if isinstance(val, str) and len(val) > 80:
                    print("      %s:" % label)
                    for line in textwrap.wrap(val, 100):
                        print("        " + line)
    problems, warnings = validate(kimi_text, conf_text, args.profile_dir)
    if not problems:
        more, inotes = installed_check(kimi_text, conf_text, args.installed_dir, args.profile_dir)
        problems += more
        for n in inotes:
            print("  installed: %s" % n)
    for w in warnings:
        print("  WARNING: %s" % w)
    if problems:
        print("REFUSED: the bot's own checks reject the result - nothing was written:")
        for p in problems:
            print("  - %s" % p)
        return 1
    print("  check    : the bot's own checks accept both new files")
    if args.dry_run:
        print("DRY RUN: nothing was written.")
        return 0
    if not changes and kimi_text == dumps(kimi) and conf_text == dumps(conf):
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    written = []
    for path, text, old in ((kpath, kimi_text, kimi), (cpath, conf_text, conf)):
        if text == dumps(old):
            continue
        b = backup(path, args.backup_dir, stamp)
        atomic_replace(path, text)
        written.append((path, b))
    for path, b in written:
        print("  written  : %s (old copy: %s)" % (path, b))
    if written:
        print("  undo     : %s" % "; ".join("sudo cp %s %s" % (b, p) for p, b in written))
    if written and not changes:
        print("Only comments were refreshed (no value changed). The live confirmation compares values, not "
              "their formatting, so it is still valid - 'sudo bitpin-bot confirm-live --check' tells for sure.")
    if changes:
        print()
        print("NEXT (required): the live confirmation covers every value of both files, so the live bot now "
              "refuses to start (exit 78, no trading) until you confirm the new settings:")
        print("    sudo bitpin-bot check              (must end with RESULT: OK)")
        print("    sudo bitpin-bot confirm-live       (read the summary: schedule, crash ladder, exits, routing)")
        print("    sudo systemctl restart bitpin-bot")
        if bot_running():
            print("NOTE: bitpin-bot is running NOW with its OLD settings, and at its next restart (a crash, a reboot) "
                  "it refuses to start until confirm-live has run: do the three steps above now (stop it before "
                  "confirm-live: sudo systemctl stop bitpin-bot).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
