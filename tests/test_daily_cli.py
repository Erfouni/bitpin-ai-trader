"""The daily-schedule + crash-ladder version from the operator's side: the shipped server profile
(kimi.example.json / config.example.json), deploy/apply_profile.py, and the run_bot.py commands
(confirm-live, status helpers, ladder, cancel-resting, stop, kimi-check streaming).

No network and no real credentials: candles, Moonshot and the brokers are fakes."""
import contextlib
import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import data as data_mod  # noqa: E402
from bitpin import runner as runner_mod  # noqa: E402
from bitpin.api import atomic_write_json, read_json  # noqa: E402
from bitpin.brain import (AGGRESSIVE_STYLE_INSTRUCTIONS, build_kimi, check_kimi_config,  # noqa: E402
                          load_kimi_config)
from bitpin.broker import LiveBroker, OrderJournal  # noqa: E402
from bitpin.data import Bar  # noqa: E402
from bitpin.risk import RiskManager  # noqa: E402
from bitpin.runner import StateLock, load_config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "sk-DAILYCLITESTKEY0123456789"


def load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_daily_cli_")
        self.handlers = list(logging.getLogger().handlers)
        self.level = logging.getLogger().level

    def tearDown(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            if h not in self.handlers:
                root.removeHandler(h)
                h.close()
        root.setLevel(self.level)
        shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------------------------------- the shipped profile

class ShippedProfileTest(Base):
    def test_config_example_loads_from_the_file_with_every_new_section(self):
        """Regression: read_json gives Decimals for 0.125 / 7.5 / 0.5, and the section validation
        accepted only int / float - the shipped ladder section made the bot refuse its own config."""
        cfg = load_config(os.path.join(ROOT, "config.example.json"))
        # v3 (E1): one level, a quarter of the equity per bid (two levels halved the bids under Bitpin's minimum)
        self.assertEqual(cfg["ladder"], {"enabled": True, "coins": None, "levels_pct": [-20.0],
                                         "size_frac": 0.25, "rearm_pct": 7.5, "lookback_hours": 48,
                                         "reprice_pct": 0.5, "resize_pct": 10.0})
        self.assertTrue(all(isinstance(v, float) for v in (cfg["ladder"]["size_frac"], cfg["ladder"]["rearm_pct"],
                                                            cfg["exits"]["reprice_pct"])))
        self.assertEqual(cfg["exits"], {"enabled": True, "target_orders": True, "reprice_pct": 0.5,
                                        "resize_pct": 10.0})
        self.assertEqual(cfg["routing"], {"enabled": True, "coins": ["BTC", "ETH", "XRP", "SOL"]})
        risk = RiskManager(cfg["risk"], self.dir, "live", persist=False)
        self.assertEqual((risk.min_order_usdt, risk.min_order_irt, risk.max_limit_orders_per_day),
                         (Decimal("1.05"), Decimal("100000"), 120))        # v3: Bitpin rejected smaller USDT orders
        doc = json.loads(read_text(os.path.join(ROOT, "config.example.json")))
        for sec in ("ladder", "exits", "routing"):
            self.assertIn("_section", doc[sec])
            for k in doc[sec]:
                if not k.startswith("_"):
                    self.assertIn("_" + k, doc[sec], "no comment for %s.%s" % (sec, k))
        for k in ("min_order_usdt", "max_limit_orders_per_day", "max_limit_distance"):
            self.assertIn("_" + k, doc["risk"])

    def test_kimi_example_is_the_daily_profile(self):
        path = os.path.join(ROOT, "kimi.example.json")
        warnings = []
        self.assertEqual(check_kimi_config(path, warnings=warnings), [])
        self.assertEqual(warnings, [])
        cfg = load_kimi_config(path)
        llm, brain, builder = build_kimi(cfg, None, self.dir, env={})
        self.assertEqual(brain.cfg["decision_times_local"], ["19:00"])
        self.assertTrue(brain.clock_schedule)
        self.assertEqual(brain.cfg["max_gap_hours"], 26)
        self.assertEqual(brain.cfg["fallback"], {"derisk_after_hours": 48, "sweep_irt_above": 0.05})
        self.assertEqual((brain.cfg["min_decision_spacing_minutes"], brain.cfg["max_decisions_per_day"],
                          brain.cfg["max_early_decisions_per_day"], brain.cfg["reserve_llm_calls"],
                          brain.cfg["next_review_min_hours"]), (55, 4, 3, 3, 6))
        self.assertEqual(brain.cfg["extra_instructions"], AGGRESSIVE_STYLE_INSTRUCTIONS)
        self.assertEqual(brain.ladder_coins, ["BTC", "ETH", "XRP", "SOL"])
        # v3: the one-year competition (2026-09-22 .. 2027-09-21 Tehran); the endgame sits 1-5 days before its end
        self.assertFalse(brain.endgame_flags(time.mktime((2026, 10, 18, 12, 0, 0, 0, 0, 0)))["no_new_entries"])
        eg = brain.endgame_flags(time.mktime((2027, 9, 17, 12, 0, 0, 0, 0, 0)))
        self.assertTrue(eg["no_new_entries"])
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.gmtime(eg["final_at"] + 3.5 * 3600)), "2027-09-20 13:00")
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.gmtime(eg["end_at"])), "2027-09-21 20:30")
        self.assertEqual((brain.cfg["analysis_policy"], brain.cfg["slot_reasoning_effort"]), ("off", None))
        self.assertTrue(brain.cfg["log_full_context"])
        self.assertEqual(llm.cfg.get("reasoning_effort"), "high")
        self.assertEqual((llm.cfg["timeout"], llm.cfg["deadline_seconds"], llm.cfg["max_calls_per_day"],
                          llm.cfg["max_tokens_per_day"], llm.cfg["stream"], llm.model),
                         (900, 1200, 24, 300000, True, "kimi-k3"))
        self.assertGreaterEqual(brain.cfg["decision_deadline_seconds"], llm.cfg["deadline_seconds"])
        self.assertEqual({k: cfg["news"][k] for k in ("timeout", "deadline_seconds", "cache_minutes",
                                                     "max_calls_per_day", "stream", "model")},
                         {"timeout": 300, "deadline_seconds": 600, "cache_minutes": 1380, "max_calls_per_day": 2,
                          "stream": True, "model": "kimi-k2.6"})
        self.assertIs(cfg["news"].get("after_hold_only"), True)
        doc = json.loads(read_text(path))
        for sec in ("llm", "brain", "news"):
            for k in doc[sec]:
                if not k.startswith("_") and k not in ("honor_next_review_hours", "next_review_min_hours",
                                                       "maker_fee", "taker_fee", "backoff_max_seconds"):
                    self.assertIn("_" + k, doc[sec], "no comment for %s.%s" % (sec, k))


# --------------------------------------------------------------------------- apply_profile.py

def old_server_kimi():
    """A kimi.json like the one on the server before this version: the hourly schedule, no new keys,
    plus the owner's own edits that the profile must keep."""
    doc = json.loads(read_text(os.path.join(ROOT, "kimi.example.json")))
    b, llm, news = doc["brain"], doc["llm"], doc["news"]
    for k in ("decision_times_local", "max_gap_hours", "held_move_pct", "risk_reduce_min_coin_weight",
              "usdt_notify_pct", "veto_drop_pct", "veto_rearm_pct", "ladder_coins", "next_review_min_hours",
              "reserve_llm_calls", "endgame"):
        b.pop(k, None)
    b.update(decision_interval_hours=1, event_move_pct=5.0, min_decision_spacing_minutes=40, max_decisions_per_day=28,
             max_early_decisions_per_day=6, decision_deadline_seconds=600, extra_instructions="",
             fallback={"derisk_after_hours": 12, "sweep_irt_above": 0.1})
    b["allowed_symbols"] = ["USDT_IRT", "BTC_IRT", "ETH_IRT", "XRP_IRT", "SOL_IRT", "DOGE_IRT"]
    b["_decision_interval_hours"] = "A new decision every N hours (1 = every hour)."
    llm.update(max_calls_per_day=60, max_tokens_per_day=4000000)
    llm.pop("stream", None)
    news.update(cache_minutes=110, max_calls_per_day=16, max_stale_minutes=360)
    news.pop("stream", None)
    doc["context"]["equity_start_irt"] = 1234567
    return doc


def old_server_config():
    doc = json.loads(read_text(os.path.join(ROOT, "config.example.json")))
    for k in ("ladder", "exits", "routing", "_endgame_and_derisk"):
        doc.pop(k, None)
    for k in ("min_order_usdt", "_min_order_usdt", "max_limit_orders_per_day", "_max_limit_orders_per_day",
              "max_limit_distance", "_max_limit_distance"):
        doc["risk"].pop(k, None)
    doc["risk"]["min_order_irt"] = 200000
    doc["risk"]["max_drawdown"] = 0.3                        # the v2 halt (all-time high-water mark)
    doc["risk"].pop("hwm_window_days", None)
    doc["risk"].pop("_hwm_window_days", None)
    return doc


class ApplyProfileTest(Base):
    def setUp(self):
        super().setUp()
        self.ap = load_module("apply_profile_under_test", os.path.join("deploy", "apply_profile.py"))
        self.etc = os.path.join(self.dir, "etc")
        self.bk = os.path.join(self.dir, "backups")
        os.makedirs(self.etc)
        self.kp, self.cp = os.path.join(self.etc, "kimi.json"), os.path.join(self.etc, "config.json")
        write_json(self.kp, old_server_kimi())
        write_json(self.cp, old_server_config())

    def run_ap(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            # hermetic: the machine's own /opt/bitpin-bot (on the server: the OLD version while update.sh runs
            # these tests) is never consulted unless a test names an installed tree
            if "--installed-dir" not in extra:
                extra = ("--installed-dir", "") + tuple(extra)
            code = self.ap.main(["--etc", self.etc, "--backup-dir", self.bk] + list(extra))
        return code, out.getvalue()

    def test_applies_only_the_profile_keys_backs_up_and_validates(self):
        before_k, before_c = read_text(self.kp), read_text(self.cp)
        code, out = self.run_ap()
        self.assertEqual(code, 0, out)
        self.assertIn("confirm-live", out)
        self.assertIn("check    : the bot's own checks accept both new files", out)
        k, c = json.loads(read_text(self.kp)), json.loads(read_text(self.cp))
        ex = json.loads(read_text(os.path.join(ROOT, "kimi.example.json")))
        b = k["brain"]
        self.assertEqual(b["decision_times_local"], ["19:00"])
        self.assertEqual(b["fallback"], {"derisk_after_hours": 48, "sweep_irt_above": 0.1})     # sweep kept
        self.assertEqual(b["extra_instructions"], AGGRESSIVE_STYLE_INSTRUCTIONS)
        self.assertEqual(b["endgame"], ex["brain"]["endgame"])
        self.assertEqual((b["max_decisions_per_day"], b["min_decision_spacing_minutes"]), (4, 55))
        self.assertEqual(b["_decision_interval_hours"], ex["brain"]["_decision_interval_hours"])   # comment follows
        self.assertEqual(b["allowed_symbols"], ex["brain"]["allowed_symbols"])        # the owner's wide universe
        self.assertEqual(len(b["allowed_symbols"]), 53)                            # + the 15 RWA tokens (2026-09-26)
        self.assertEqual(k["context"]["universe"], ex["context"]["universe"])
        self.assertEqual(k["context"]["equity_start_irt"], 1234567)                                # kept
        self.assertEqual(b["risk_profile"], "full")
        self.assertEqual((k["llm"]["max_calls_per_day"], k["llm"]["max_tokens_per_day"], k["llm"]["stream"]),
                         (24, 300000, True))
        self.assertEqual((k["news"]["cache_minutes"], k["news"]["max_calls_per_day"]), (1380, 2))
        self.assertIs(k["news"].get("after_hold_only"), True)                                       # v3 (B3)
        self.assertEqual(c["ladder"]["levels_pct"], [-20])
        # v3 (E1 / E2): the server's 30% all-time halt becomes the one-year value (PROFILE_CONFIG_SET, shown)
        self.assertEqual((c["risk"]["max_drawdown"], c["risk"]["hwm_window_days"], c["risk"]["drawdown_action"]),
                         (0.5, 90, "halt"))
        self.assertEqual(c["ladder"]["size_frac"], 0.25)
        self.assertIn("risk.max_drawdown", out)
        self.assertEqual(c["routing"]["coins"], ["BTC", "ETH", "XRP", "SOL"])
        self.assertEqual(c["risk"]["min_order_irt"], 200000)                                       # kept
        self.assertEqual(c["risk"]["min_order_usdt"], 1.05)
        # v3: the one-year dates and the new keys are part of the profile; the start date / equity are kept
        self.assertEqual(k["context"]["competition_end_utc"], "2027-09-21T20:30:00Z")
        self.assertEqual(b["endgame"]["final_at"], "2027-09-20T13:00:00+03:30")
        self.assertEqual((b["analysis_policy"], b["slot_reasoning_effort"], b["log_full_context"]), ("off", None, True))
        self.assertEqual(k["llm"]["reasoning_effort"], "high")
        self.assertEqual(check_kimi_config(self.kp, runner_cfg=load_config(self.cp)), [])
        # backups hold the old files
        names = sorted(os.listdir(self.bk))
        self.assertEqual(len(names), 2)
        self.assertEqual(read_text(os.path.join(self.bk, [n for n in names if n.startswith("kimi")][0])), before_k)
        self.assertEqual(read_text(os.path.join(self.bk, [n for n in names if n.startswith("config")][0])), before_c)
        # a second run changes nothing
        after_k, after_c = read_text(self.kp), read_text(self.cp)
        code, out = self.run_ap()
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing to change", out)
        self.assertEqual((read_text(self.kp), read_text(self.cp)), (after_k, after_c))
        self.assertEqual(len(os.listdir(self.bk)), 2)

    def test_existing_config_sections_are_completed_not_overwritten(self):
        """An existing section is completed with the missing keys and the owner's values are kept - except
        the v3 SET keys (PROFILE_CONFIG_SET: the one-year ladder size / levels and the halt), which follow
        the example like the kimi.json profile does, and are listed as changes."""
        c = old_server_config()
        c["ladder"] = {"enabled": False, "size_frac": 0.05, "rearm_pct": 5.0, "levels_pct": [-20, -25]}
        write_json(self.cp, c)
        code, out = self.run_ap()
        self.assertEqual(code, 0, out)
        lad = json.loads(read_text(self.cp))["ladder"]
        self.assertEqual((lad["enabled"], lad["rearm_pct"], lad["lookback_hours"]), (False, 5.0, 48))   # kept / added
        self.assertEqual((lad["size_frac"], lad["levels_pct"]), (0.25, [-20]))                          # v3 SET keys
        self.assertIn("ladder.size_frac", out)
        self.assertIn("ladder.levels_pct", out)

    def test_dry_run_writes_nothing(self):
        before = (read_text(self.kp), read_text(self.cp))
        code, out = self.run_ap("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN", out)
        self.assertIn("brain.decision_times_local", out)
        self.assertEqual((read_text(self.kp), read_text(self.cp)), before)
        self.assertFalse(os.path.exists(self.bk))

    def test_a_result_the_bot_would_refuse_is_not_written(self):
        k = old_server_kimi()
        # not a date the bot accepts: refused (the START date is the owner's own value, which the v3 profile keeps;
        # the end date is part of the profile since v3 and would be replaced by 2027-09-21)
        k["context"]["competition_start_utc"] = "22 Sep 2026"
        write_json(self.kp, k)
        before = (read_text(self.kp), read_text(self.cp))
        code, out = self.run_ap()
        self.assertEqual(code, 1, out)
        self.assertIn("REFUSED", out)
        self.assertIn("competition_start_utc", out)
        self.assertEqual((read_text(self.kp), read_text(self.cp)), before)
        with open(self.kp, "w", encoding="utf-8") as f:
            f.write('{"brain": {"risk_profile": "full",}}')
        code, out = self.run_ap()
        self.assertEqual(code, 1, out)
        self.assertIn("not valid JSON", out)

    def test_a_comments_only_run_keeps_the_live_confirmation_valid(self):
        """Review finding (ops, medium): a run that only refreshed comments re-wrote "max_drawdown": 0.30
        as 0.3; the digest hashed the Decimal text, so the confirmation broke silently (exit 78 at the
        next restart) while apply_profile said 'Nothing to change'."""
        rb = load_module("run_bot_apply_digest", os.path.join("scripts", "run_bot.py"))
        self.assertEqual(self.run_ap()[0], 0)
        c = json.loads(read_text(self.cp))
        c["risk"]["max_drawdown"] = 0.5                    # v3: the profile's own value, written as "0.50"
        c["_README"] = "an older comment"
        raw = json.dumps(c, indent=2).replace('"max_drawdown": 0.5', '"max_drawdown": 0.50')
        with open(self.cp, "w", encoding="utf-8") as f:
            f.write(raw)
        self.assertIn('"max_drawdown": 0.50', read_text(self.cp))
        kcfg = load_kimi_config(self.kp)

        def digest():
            return rb.live_digest({"config": read_json(self.cp)}, kcfg, load_config(self.cp))
        before = digest()
        code, out = self.run_ap()
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing to change", out)
        self.assertNotIn('"max_drawdown": 0.50', read_text(self.cp))              # re-written as 0.5 ...
        self.assertEqual(digest(), before)                                         # ... same confirmation
        self.assertIn("Only comments were refreshed", out)

    def test_a_news_section_the_owner_switched_off_is_never_switched_back_on(self):
        """Review finding (ops, low): "news": null was treated like a missing section and the whole example
        section was copied back in (stage-1 research silently on again). null is kept - the bot's own
        check then refuses the file with a clear message, nothing is written - and news.enabled false (the
        bot's way to switch it off) is not a profile key: kept."""
        k = old_server_kimi()
        k["news"] = None
        write_json(self.kp, k)
        before = read_text(self.kp)
        code, out = self.run_ap()
        self.assertEqual(code, 1, out)
        self.assertIn("news is null (switched off): kept", out)
        self.assertIn("REFUSED", out)
        self.assertEqual(read_text(self.kp), before)                              # never re-enabled
        k = old_server_kimi()
        k["news"]["enabled"] = False
        write_json(self.kp, k)
        self.assertEqual(self.run_ap()[0], 0)
        news = json.loads(read_text(self.kp))["news"]
        self.assertEqual((news["enabled"], news["cache_minutes"]), (False, 1380))
        k = old_server_kimi()
        k.pop("news")                                                              # missing: copied whole
        write_json(self.kp, k)
        self.assertEqual(self.run_ap()[0], 0)
        self.assertEqual(json.loads(read_text(self.kp))["news"]["cache_minutes"], 1380)

    def test_the_profile_never_names_a_secret(self):
        src = read_text(os.path.join(ROOT, "deploy", "apply_profile.py"))
        self.assertNotIn("bitpin-bot.env\"", src)
        for k in ("BITPIN_API_KEY", "BITPIN_SECRET_KEY", "KIMI_API_KEY"):
            self.assertNotIn(k, src)


# --------------------------------------------------------------------------- run_bot.py

class CliBase(Base):
    def setUp(self):
        super().setUp()
        self.lockdir = tempfile.mkdtemp(prefix="bitpin_daily_locks_")
        self.addCleanup(shutil.rmtree, self.lockdir, True)
        self.rb = load_module("run_bot_daily_cli", os.path.join("scripts", "run_bot.py"))
        self.sd = os.path.join(self.dir, "state")
        os.makedirs(self.sd)
        self.kimi_path = os.path.join(self.dir, "kimi.json")
        shutil.copy(os.path.join(ROOT, "kimi.example.json"), self.kimi_path)
        self.cfg_path = os.path.join(self.dir, "config.json")
        shutil.copy(os.path.join(ROOT, "config.example.json"), self.cfg_path)

    def run_main(self, argv, stdin=None, inputs=None, env=None):
        out = io.StringIO()
        code = None
        e = {"BITPIN_API_KEY": "DUMMYAPIKEY123", "BITPIN_SECRET_KEY": "DUMMYSECRET456",
             "BITPIN_BOT_LOCK_DIR": self.lockdir, "KIMI_API_KEY": KEY}
        e.update(env or {})
        with mock.patch.dict(os.environ, e), mock.patch("sys.stdin", stdin or io.StringIO()), \
                mock.patch("builtins.input", side_effect=list(inputs or [])), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = self.rb.main(argv)
            except SystemExit as ex:
                code = ex.code
        return code, out.getvalue()

    def confirm_irt(self):
        atomic_write_json(os.path.join(self.sd, self.rb.CONFIRM_FILE),
                          {"confirmed": True, "irt_asset_code": "IRT", "irt_unit_divisor": "1"})


class ConfirmLiveTest(CliBase):
    def confirm(self):
        return self.run_main(["confirm-live", "--config", self.cfg_path, "--kimi-config", self.kimi_path,
                              "--state-dir", self.sd], stdin=FakeTTY(), inputs=["I ACCEPT THE RISK"])

    def live(self):
        return self.run_main(["live", "--brain", "kimi", "--config", self.cfg_path, "--kimi-config", self.kimi_path,
                              "--state-dir", self.sd, "--i-accept-the-risk", "--non-interactive"])

    def test_the_summary_lists_the_new_rules(self):
        self.confirm_irt()
        code, text = self.confirm()
        self.assertEqual(code, 0, text)
        for want in ("schedule       : ONCE A DAY at 19:00 Tehran", "VETO", "REVIEW", "pacing         : at least 55 min",
                     "crash ladder   : ON - resting maker BUY limits on BTC_USDT ETH_USDT XRP_USDT SOL_USDT at -20%",
                     "25% of equity", "code exits     : ON - every coin position: STOP only where Kimi set stop_pct "
                     "(NO default stop; 5..40% below the average entry", "MAX HOLD 720 h",
                     "routing        : ON", "endgame        : from 2027-09-16 13:00 Tehran",
                     "is FINAL", "derisk delay (48 h)", "1.05 USDT on USDT markets", "RESTING ORDERS",
                     "streamed (SSE), timeout 900s per request, 1200s per call",
                     "news: 300s / 600s", "REAL market and resting limit orders"):
            self.assertIn(want, text)
        rec = read_json(os.path.join(self.sd, self.rb.LIVE_CONFIRMED_FILE))
        self.assertEqual(rec["digest_version"], self.rb.DIGEST_VERSION)

    def test_an_older_confirmation_must_be_renewed(self):
        """The server's LIVE_CONFIRMED was written by the version without resting orders (digest v2):
        the new version must not start the live bot on it."""
        self.confirm_irt()
        called = []
        self.rb.make_live = lambda *a, **k: called.append(a) or (_ for _ in ()).throw(SystemExit(0))
        atomic_write_json(os.path.join(self.sd, self.rb.LIVE_CONFIRMED_FILE),
                          {"version": 1, "confirmed_utc": "2026-09-22 10:00:00", "config_digest": "0" * 64,
                           "brain": "kimi"})
        code, text = self.live()
        self.assertEqual((code, called), (78, []))
        self.assertIn("older version of the bot", text)
        self.assertIn("RESTING crash-ladder bids and target sells, the rules of the code exits", text)
        code, text = self.run_main(["confirm-live", "--check", "--config", self.cfg_path, "--kimi-config",
                                    self.kimi_path, "--state-dir", self.sd])
        self.assertEqual(code, 1)
        self.assertIn("older version", text)
        code, text = self.confirm()
        self.assertEqual(code, 0, text)
        code, text = self.live()
        self.assertEqual((code, len(called)), (0, 1), text)

    def test_the_resting_order_settings_in_force_are_confirmed(self):
        """A code default of the ladder counts (it makes the bot place orders on its own); other code
        defaults still do not."""
        us = {"config": {}, "overrides": {}}
        kcfg = load_kimi_config(self.kimi_path)
        base = self.rb.live_digest(us, kcfg, load_config(None))
        with mock.patch.dict(runner_mod.DEFAULT_CONFIG, {"wake_delay_seconds": 90}):
            self.assertEqual(self.rb.live_digest(us, kcfg, load_config(None)), base)
        lad = dict(runner_mod.DEFAULT_LADDER, size_frac=0.25)
        with mock.patch.dict(runner_mod.SECTION_DEFAULTS, {"ladder": lad}):
            self.assertNotEqual(self.rb.live_digest(us, kcfg, load_config(None)), base)
        with mock.patch.dict(self.rb.DEFAULT_RISK, {"min_order_usdt": 1.0}):
            self.assertNotEqual(self.rb.live_digest(us, kcfg, load_config(None)), base)
        # a strategy run has no resting orders: unchanged by them
        s = self.rb.live_digest(us, None, load_config(None))
        with mock.patch.dict(runner_mod.SECTION_DEFAULTS, {"ladder": lad}):
            self.assertEqual(self.rb.live_digest(us, None, load_config(None)), s)


class LiveWiringTest(CliBase):
    def test_make_live_passes_both_minimum_orders(self):
        seen = {}

        class FakeLive(object):
            def __init__(self, client, markets, sd, **kw):
                seen.update(kw)
        with mock.patch.object(self.rb, "connect_client", lambda *a, **k: object()), \
                mock.patch.object(self.rb, "LiveBroker", FakeLive):
            self.rb.make_live(load_config(self.cfg_path), self.sd, "k", "s")
        self.assertEqual((Decimal(str(seen["min_order_usdt"])), Decimal(str(seen["min_order_irt"]))),
                         (Decimal("1.05"), Decimal("100000")))                  # the v3 example's 1.05 USDT
        with mock.patch.object(self.rb, "connect_client", lambda *a, **k: object()), \
                mock.patch.object(self.rb, "LiveBroker", FakeLive):
            self.rb.make_live(load_config(None), self.sd, "k", "s")
        self.assertEqual(Decimal(str(seen["min_order_usdt"])), Decimal("0.5"))     # the built-in default

    def test_banner_names_the_schedule_and_the_resting_orders(self):
        kcfg = load_kimi_config(self.kimi_path)
        llm, brain, _ = build_kimi(kcfg, None, self.sd, env={"KIMI_API_KEY": KEY})

        class R(object):
            cfg = load_config(self.cfg_path)
            ladder_on = exits_on = routing_on = True
            ladder_coins = ["BTC", "ETH", "XRP", "SOL"]
        text = "\n".join(self.rb.brain_banner_lines(llm, brain, runner=R()))
        for want in ("ONCE A DAY at 19:00 Tehran", "Crash ladder  : ON", "Code exits    : ON", "Routing       : ON",
                     "Endgame       : from 2027-09-16 13:00", "after 48 h", "streamed",
                     # v3: the release the code is, the analysis check and the reasoning effort are named
                     "Version       : bitpin-bot ", "Kimi analysis : the reply's analysis block", "policy off",
                     "reasoning effort high", "Code exits    : ON - every coin position: STOP only where Kimi set "
                     "stop_pct", "MAX HOLD 720 h",
                     "at most 4 decisions / 3 early ones",
                     # the kill-switch semantics of this version: STOP cancels the ladder bids (review finding)
                     "STOP, seen by the running bot, cancels the crash-ladder bids", "BOTH stay on Bitpin"):
            self.assertIn(want, text)
        self.assertNotIn("STOP does not cancel", text)
        R.ladder_on = False
        text = "\n".join(self.rb.brain_banner_lines(llm, brain, runner=R()))
        self.assertIn("Crash ladder  : OFF (needs the Kimi brain", text)

    def test_resting_state_lines_show_bids_levels_and_positions(self):
        j = OrderJournal(os.path.join(self.sd, LiveBroker.JOURNAL_FILE))
        j.add("lad-1", kind="limit", symbol="BTC_USDT", side="buy", price="69664.4", base_amount="0.00003",
              status="resting", tag="ladder", meta={"coin": "BTC", "level_pct": -20.0})
        atomic_write_json(os.path.join(self.sd, "runner_state_live.json"), {
            "ladder": {"scales": {"BTC": 1.0, "ETH": 0.5},
                       "coins": {"BTC": {"levels": {"-20": {"armed": True},
                                                    "-25": {"armed": False, "filled_at": 1790000000.0,
                                                            "fill_px_usdt": 65000.0}}}}},
            "positions": {"BTC_IRT": {"amount": 0.00003, "entry_px_usdt": 65000.0, "source": "ladder",
                                      "entry_ts": 1790000000.0, "stop_px_usdt": 57200.0, "target_px_usdt": 73125.0,
                                      "max_hold_until": 1790604800.0}}})
        lines = "\n".join(self.rb.resting_state_lines(self.sd, "live", LiveBroker(None, None, self.sd)))
        for want in ("bot resting orders (its own journal): 1", "ladder buy  BTC_USDT", "level -20%",
                     "BTC scale 1: -20% armed, -25% FILLED", "ETH scale 0.5", "BTC_IRT", "entry 65000 USDT",
                     "stop 57200", "target 73125 USDT"):
            self.assertIn(want, lines)

    def test_stop_says_what_it_cancels_and_what_stays(self):
        """Review finding (money, high): STOP now cancels the running bot's crash-ladder bids; the
        target sells stay, and a bot that is not running cancels nothing."""
        j = OrderJournal(os.path.join(self.sd, LiveBroker.JOURNAL_FILE))
        j.add("lad-1", kind="limit", symbol="ETH_USDT", side="buy", price="2240", base_amount="0.001",
              status="resting", tag="ladder", meta={"coin": "ETH", "level_pct": -20.0})
        with StateLock(self.sd):
            code, text = self.run_main(["stop", "--state-dir", self.sd])
        self.assertEqual(code, 0, text)
        self.assertIn("the running bot CANCELS its crash-ladder bids when it sees STOP", text)
        self.assertIn("Its target sells stay on Bitpin", text)
        self.assertIn("ladder buy ETH_USDT @ 2240", text)
        self.assertIn("If NO bot is running, nothing cancels the bids: run cancel-resting yourself", text)

    def test_kimi_check_reports_streaming(self):
        def transport(method, url, headers, body, timeout):
            return 200, json.dumps({"data": [{"id": "kimi-k3"}, {"id": "kimi-k2.6"}]}).encode("utf-8")
        with mock.patch("bitpin.llm.make_llm_transport", lambda proxy=None, opener=None: transport):
            code, text = self.run_main(["kimi-check", "--kimi-config", self.kimi_path])
        self.assertEqual(code, 0, text)
        self.assertIn("streaming     : decision calls streamed", text)
        self.assertIn("news calls streamed", text)
        self.assertNotIn(KEY, text)
        k = json.loads(read_text(self.kimi_path))
        k["llm"]["stream"] = False
        write_json(self.kimi_path, k)
        with mock.patch("bitpin.llm.make_llm_transport", lambda proxy=None, opener=None: transport):
            code, text = self.run_main(["kimi-check", "--kimi-config", self.kimi_path])
        self.assertIn("NOT streamed (llm.stream false)", text)


def fake_bars(now, prices):
    """fetch_bars stand-in: hourly bars for each symbol, closes from prices[symbol](i) (i = hours ago)."""
    def fetch(symbol, res="60", start=None, end=None):
        last_open = int((now - 1800) // 3600) * 3600 + 1800 - 3600    # the newest CLOSED bar (bars open at hh:30)
        out = []
        for i in range(70, -1, -1):
            ts = last_open - i * 3600
            c = float(prices[symbol](i))
            out.append(Bar(ts, c, c, c, c, 1.0))
        out.append(Bar(last_open + 3600, 1.0, 1.0, 1.0, 1.0, 1.0))      # the forming bar (dropped)
        return out
    return fetch


class LadderCommandTest(CliBase):
    NOW = 1790000000.0 - (1790000000.0 % 3600) + 1800 + 120        # 2 min after an hourly close

    def prices(self, btc_hi=90000.0):
        rate = 200000.0
        return {"USDT_IRT": lambda i: rate,
                "BTC_IRT": lambda i: (btc_hi if i == 30 else btc_hi * 0.95) * rate,
                "ETH_IRT": lambda i: 2000.0 * rate, "XRP_IRT": lambda i: 1.5 * rate, "SOL_IRT": lambda i: 100.0 * rate}

    def ladder(self, *extra, now=None):
        with mock.patch.object(data_mod, "fetch_bars", fake_bars(now or self.NOW, self.prices())), \
                mock.patch.object(self.rb.time, "time", lambda: now or self.NOW):
            return self.run_main(["ladder", "--config", self.cfg_path, "--kimi-config", self.kimi_path,
                                  "--state-dir", self.sd, "--equity-irt", "4000000"] + list(extra))

    def test_the_plan_matches_the_runner_rules(self):
        code, text = self.ladder()
        self.assertEqual(code, 0, text)
        self.assertIn("read-only: nothing is placed or cancelled", text)
        # BTC: the 48 h high 90000 (30 h ago), one level -20% (v3); 25% of 20 USDT = 5 USDT each (x0.998 buffer)
        self.assertIn("72000", text)
        self.assertNotIn("67500", text)                 # no -25% level in the v3 profile
        self.assertIn("1600", text)                     # ETH -20%
        self.assertIn("4 bid(s), 19.96 USDT in total", text)
        self.assertIn("-5.00%", text)                   # BTC dd48

    def test_filled_levels_scales_and_the_endgame(self):
        atomic_write_json(os.path.join(self.sd, "runner_state_live.json"), {
            "ladder": {"scales": {"BTC": 1.0, "ETH": 0.0, "XRP": 1.0, "SOL": 1.0},
                       "coins": {"BTC": {"levels": {"-20": {"armed": False, "filled_at": self.NOW - 3600,
                                                            "fill_px_usdt": 72000.0}}}}}})
        code, text = self.ladder()
        self.assertEqual(code, 0, text)
        self.assertIn("FILLED - disarmed", text)
        self.assertIn("scale 0", text)
        self.assertIn("2 bid(s)", text)                 # 4 coins x 1 level, minus BTC (filled) and ETH (scale 0)
        # after the endgame cut-off (v3: 2027-09-16 13:00 Tehran) every scale is 0: no bid
        end = time.mktime((2027, 9, 17, 12, 0, 0, 0, 0, 0))
        code, text = self.ladder(now=end)
        self.assertEqual(code, 0, text)
        self.assertIn("endgame: no new coin entries", text)
        self.assertIn("0 bid(s)", text)

    def test_without_an_equity_it_says_what_to_pass(self):
        with mock.patch.object(data_mod, "fetch_bars", fake_bars(self.NOW, self.prices())), \
                mock.patch.object(self.rb.time, "time", lambda: self.NOW):
            code, text = self.run_main(["ladder", "--config", self.cfg_path, "--state-dir", self.sd])
        self.assertEqual(code, 1)
        self.assertIn("--equity-irt", text)


class CancelRestingTest(CliBase):
    def test_cancels_only_the_bots_own_orders_and_only_when_stopped(self):
        views = [{"identifier": "lad-1", "tag": "ladder", "side": "buy", "symbol": "BTC_USDT", "price": Decimal("70000"),
                  "base_amount": Decimal("0.00003"), "remaining_base": Decimal("0.00003"), "state": "open"},
                 {"identifier": "tgt-1", "tag": "target", "side": "sell", "symbol": "BTC_USDT",
                  "price": Decimal("95000"), "base_amount": Decimal("0.00003"),
                  "remaining_base": Decimal("0.00003"), "state": "open"}]
        cancelled = []

        class FakePaper(object):
            def __init__(self, *a, **k):
                pass

            def limit_orders(self, tag=None, active_only=False):
                return [v for v in views if tag is None or v["tag"] == tag]

            def cancel(self, ident, wait=True):
                cancelled.append(ident)
                return {"state": "cancelled", "filled_base": Decimal("0")}
        atomic_write_json(os.path.join(self.sd, "paper_state.json"), {"balances": {}})
        with mock.patch.object(self.rb, "PaperBroker", FakePaper):
            FakePaper.STATE_FILE = "paper_state.json"
            with StateLock(self.sd):
                code, text = self.run_main(["cancel-resting", "--mode", "paper", "--state-dir", self.sd, "--yes"])
            self.assertNotEqual(code, 0)
            self.assertIn("stop it first", str(code) + text)
            self.assertEqual(cancelled, [])
            code, text = self.run_main(["cancel-resting", "--mode", "paper", "--state-dir", self.sd, "--tag",
                                        "ladder"], inputs=["no"])
            self.assertEqual((code, cancelled), (1, []))
            code, text = self.run_main(["cancel-resting", "--mode", "paper", "--state-dir", self.sd, "--tag",
                                        "ladder", "--yes"])
            self.assertEqual((code, cancelled), (0, ["lad-1"]), text)
            code, text = self.run_main(["cancel-resting", "--mode", "paper", "--state-dir", self.sd, "--yes"])
            self.assertEqual((code, cancelled), (0, ["lad-1", "lad-1", "tgt-1"]), text)
            self.assertIn("places the ladder bids again", text)


class LadderReviewCliTest(CliBase):
    """Regression tests of the ladder-round review findings in scripts/run_bot.py (cancel-resting,
    the live-confirmation digest, the paper fill model)."""

    def test_a_cancel_that_is_not_confirmed_is_reported_as_still_open(self):
        """Review finding (ops, medium): LiveBroker.cancel() swallows a failed DELETE; cancel-resting
        printed 'cancelled ...: open' and 'done.' with exit 0 for an order still on Bitpin."""
        views = [{"identifier": "lad-1", "tag": "ladder", "side": "buy", "symbol": "BTC_USDT",
                  "price": Decimal("70000"), "base_amount": Decimal("0.00003"), "state": "open"},
                 {"identifier": "tgt-1", "tag": "target", "side": "sell", "symbol": "BTC_USDT",
                  "price": Decimal("95000"), "base_amount": Decimal("0.00003"), "state": "open"}]
        calls = []

        class FakePaper(object):
            STATE_FILE = "paper_state.json"

            def __init__(self, *a, **k):
                pass

            def limit_orders(self, tag=None, active_only=False):
                return [v for v in views if tag is None or v["tag"] == tag]

            def cancel(self, ident, wait=True):
                calls.append(ident)
                if ident == "tgt-1":          # the DELETE timed out: the order is still open
                    return {"state": "open", "filled_base": Decimal("0"), "final": False}
                return {"state": "cancelled", "filled_base": Decimal("0"), "final": True}
        atomic_write_json(os.path.join(self.sd, "paper_state.json"), {"balances": {}})
        with mock.patch.object(self.rb, "PaperBroker", FakePaper), mock.patch.object(self.rb.time, "sleep"):
            code, text = self.run_main(["cancel-resting", "--mode", "paper", "--state-dir", self.sd, "--yes"])
        self.assertEqual(code, 1, text)
        self.assertIn("cancelled lad-1: cancelled", text)
        self.assertIn("tgt-1 sell BTC_USDT @ 95000 requested, but it is STILL OPEN", text)
        self.assertIn("NOT DONE: 1 order(s) are not confirmed as cancelled", text)
        self.assertNotIn("done: every order is confirmed closed", text)
        self.assertEqual(calls, ["lad-1", "tgt-1", "tgt-1"])            # re-sent once before the verdict

    def test_the_digest_compares_numbers_by_value(self):
        """Review finding (ops, medium): a comments-only apply_profile run rewrote 0.30 as 0.3 and silently
        invalidated the live confirmation (exit 78 at the next restart)."""
        kcfg = load_kimi_config(self.kimi_path)
        a = self.rb.live_digest({"config": {"risk": {"max_drawdown": Decimal("0.30"), "n": Decimal("24.0")}}}, kcfg,
                                load_config(None))
        b = self.rb.live_digest({"config": {"risk": {"max_drawdown": 0.3, "n": 24}, "_README": "x"}}, kcfg,
                                load_config(None))
        c = self.rb.live_digest({"config": {"risk": {"max_drawdown": 0.25, "n": 24}}}, kcfg, load_config(None))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertGreaterEqual(self.rb.DIGEST_VERSION, 4)

    def test_the_digest_covers_the_code_exit_rules_the_code_applies_on_its_own(self):
        """Review finding (ops, low): a later version that changes the default stop / max hold / ladder
        coins would sell or buy under rules nobody confirmed."""
        from bitpin import brain as brain_mod
        kcfg = load_kimi_config(self.kimi_path)
        us = {"config": {}}
        base = self.rb.live_digest(us, kcfg, load_config(None))
        for name, value in (("STOP_PCT_DEFAULT", 8.0), ("MAX_HOLD_HOURS", 72.0)):
            with mock.patch.object(brain_mod, name, value):
                self.assertNotEqual(self.rb.live_digest(us, kcfg, load_config(None)), base, name)
        k2 = json.loads(json.dumps(kcfg, default=str))
        k2.setdefault("brain", {}).pop("ladder_coins", None)
        rules = self.rb.code_exit_rules(k2)
        self.assertEqual(rules["ladder_coins"], list(brain_mod.LADDER_COINS))   # the default AS RESOLVED
        self.assertEqual(rules["stop_pct"][0], brain_mod.STOP_PCT_DEFAULT)

    def test_paper_limit_fills_need_a_trade_through_by_default(self):
        """Review finding (money, low): paper mode filled a whole resting order on a mere touch, so the
        48 h paper check overstated the ladder fills on thin XRP_USDT / SOL_USDT books."""
        seen = []

        class Stop(Exception):
            pass

        def fake_paper(*a, **k):
            seen.append(k.get("fill_through"))
            raise Stop()
        base = ["paper", "--config", self.cfg_path, "--state-dir", self.sd, "--capital-irt", "5000000", "--once",
                "--dry-run"]
        with mock.patch.object(self.rb, "PaperBroker", fake_paper):
            for extra in ([], ["--fill-through", "0"], ["--fill-through", "0.01"]):
                with self.assertRaises(Stop):
                    self.run_main(base + extra)
            code, text = self.run_main(base + ["--fill-through", "0.5"])
        self.assertEqual(seen, [Decimal("0.005"), Decimal("0"), Decimal("0.01")])
        self.assertNotEqual(code, 0)
        self.assertIn("--fill-through must be between 0 and 0.2", text)
        code, text = self.run_main(["paper", "--help"])
        self.assertIn("--fill-through", text)


class DeployKitTest(Base):
    """The Telegram notifier in the install / update / uninstall scripts and the helper, and the
    helper's daily-schedule health check (read as text; the shell parts run under bash when one is
    available)."""

    def read(self, *rel):
        return read_text(os.path.join(ROOT, *rel))

    @staticmethod
    def bash():
        probe = "test -f '%s/deploy/lib.sh'" % ROOT.replace("\\", "/")
        pf = os.environ.get("ProgramFiles", "C:\\Program Files")
        for cand in (os.path.join(pf, "Git", "bin", "bash.exe"), shutil.which("bash"), "/bin/bash"):
            if cand and os.path.exists(cand):
                try:
                    import subprocess
                    if subprocess.call([cand, "-c", probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE) == 0:
                        return cand
                except OSError:
                    continue
        return None

    def test_notifier_files_and_units(self):
        env = self.read("deploy", "notify.env.example")
        active = [ln for ln in env.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertEqual(sorted(ln.split("=")[0] for ln in active),
                         ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_HTTPS_PROXY"])
        self.assertNotIn("BITPIN_API_KEY=", env)
        self.assertIn("0600 root:root", env)
        path = self.read("deploy", "bitpin-bot-notify-stop.path")
        self.assertIn("PathExists=/var/lib/bitpin-bot-notify/stop_request.json", path)
        self.assertIn("Unit=bitpin-bot-notify-stop.service", path)
        relay = self.read("deploy", "bitpin-bot-notify-stop.service")
        for want in ("notify_bot.py apply-stop --config /etc/bitpin-bot/notify.json",
                     "CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_CHOWN", "PrivateNetwork=yes",
                     "ReadWritePaths=/var/lib/bitpin-bot /var/lib/bitpin-bot-notify",
                     "InaccessiblePaths=-/etc/bitpin-bot/bitpin-bot.env -/etc/bitpin-bot/notify.env"):
            self.assertIn(want, relay)
        self.assertNotIn("\nUser=", relay)                       # root on purpose (see its comment; the v2 /stop relay)

    def test_install_update_uninstall_handle_the_notifier(self):
        lib = self.read("deploy", "lib.sh")
        self.assertIn('UNITS="bitpin-bot.service bitpin-bot-paper.service"\n', lib)     # never in UNITS
        self.assertIn('NOTIFY_UNITS="bitpin-bot-notify.service bitpin-bot-notify-stop.service '
                      'bitpin-bot-notify-stop.path"', lib)
        self.assertIn('install_if_absent "$app/deploy/notify.env.example" "$NOTIFY_ENV_FILE" 0600 root', lib)
        self.assertIn('install_if_absent "$app/notify.example.json" "$NOTIFY_CONFIG" 0640', lib)
        self.assertIn('install -d -m 0700 -o "$APP_USER" -g "$APP_GROUP" "$NOTIFY_STATE_DIR"', lib)
        self.assertIn('"$d"/deploy/*.path', lib)
        self.assertIn('warn "no $u in this version (Telegram notifier not installed)"', lib)
        upd = self.read("deploy", "update.sh")
        self.assertLess(upd.index("    stop_notifier\n"), upd.index('mv "$APP_DIR" "$backup"'))
        # started again on every way out of main() / the rollbacks, never a reason for a rollback
        self.assertGreaterEqual(upd.count("restart_notifier\n"), 5)
        self.assertIn("notify.load_config(sys.argv[2])", upd)
        self.assertIn("apply_profile.py", upd)
        un = self.read("deploy", "uninstall.sh")
        self.assertIn("for u in $UNITS $NOTIFY_UNITS; do", un)
        self.assertIn('"$NOTIFY_STATE_DIR"', un)
        self.assertIn("cancel-resting", un)
        ins = self.read("deploy", "install.sh")
        self.assertIn("sudo bitpin-bot notify-setup", ins)
        self.assertIn("for u in $UNITS $NOTIFY_UNITS; do", ins)

    def test_helper_commands(self):
        h = self.read("deploy", "bitpin-bot")
        for cmd in ("ladder)", "cancel-resting)", "notify-setup)", "notify-test)", "notify-status)", "notify-logs)"):
            self.assertIn(cmd, h)
        nb = h[h.index("as_notify() {"):]
        nb = nb[:nb.index("\n}\n")]
        self.assertIn("unset BITPIN_API_KEY BITPIN_SECRET_KEY", nb)
        self.assertIn('load_env_file "$NOTIFY_ENV_FILE"', nb)
        self.assertNotIn("load_env\n", nb)                        # never the trading keys
        cr = h[h.index("    cancel-resting)"):]
        cr = cr[:cr.index("    notify-setup)")]
        self.assertIn("refuse_if_live_running cancel-resting", cr)

    def test_health_expects_one_valid_decision_a_day(self):
        h = self.read("deploy", "bitpin-bot")
        code = h[h.index("import json, sys\ntry:\n    with open(sys.argv[1], encoding=\"utf-8-sig\")"):]
        code = code[:code.index("\nPY\n")]
        script = os.path.join(self.dir, "limits.py")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(code + "\n")
        import subprocess
        kimi = os.path.join(ROOT, "kimi.example.json")
        out = subprocess.run([sys.executable, script, kimi], stdout=subprocess.PIPE).stdout.decode().split()
        self.assertEqual(out, ["1620", "48"])                    # (26 + 1) h, derisk 48 h
        legacy = os.path.join(self.dir, "legacy.json")
        write_json(legacy, {"brain": {"decision_times_local": [], "fallback": {"derisk_after_hours": 12}}})
        out = subprocess.run([sys.executable, script, legacy], stdout=subprocess.PIPE).stdout.decode().split()
        self.assertEqual(out, ["360", "12"])

    def test_scripts_parse(self):
        sh = self.bash()
        if sh is None:
            self.skipTest("no bash that can read this checkout")
        import subprocess
        for rel in ("deploy/bitpin-bot", "deploy/install.sh", "deploy/update.sh", "deploy/uninstall.sh",
                    "deploy/lib.sh"):
            p = subprocess.run([sh, "-n", "%s/%s" % (ROOT.replace("\\", "/"), rel)], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            self.assertEqual(p.returncode, 0, (rel, p.stdout))

    def test_the_bot_makes_itself_non_dumpable_on_linux(self):
        rb = load_module("run_bot_dumpable", os.path.join("scripts", "run_bot.py"))
        calls = []

        class Libc(object):
            def prctl(self, *a):
                calls.append(a)
                return 0
        fake_ctypes = mock.MagicMock()
        fake_ctypes.CDLL.return_value = Libc()
        with mock.patch.object(rb.sys, "platform", "linux"), mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertTrue(rb.not_dumpable())
        self.assertEqual(calls, [(4, 0, 0, 0, 0)])               # PR_SET_DUMPABLE, 0
        with mock.patch.object(rb.sys, "platform", "win32"):
            self.assertFalse(rb.not_dumpable())
        fake_ctypes.CDLL.side_effect = OSError("no libc")
        with mock.patch.object(rb.sys, "platform", "linux"), mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertFalse(rb.not_dumpable())                  # never fatal


if __name__ == "__main__":
    unittest.main()
