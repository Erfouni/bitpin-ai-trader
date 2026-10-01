"""v3.1: the management panel's root helper (scripts/panel_helper.py) - the closed command allowlist, the
write-only secrets, validated settings writes with backups, confirm-live through the command line, the
services, the panel's own login settings, the notifier relay and the owner's VPN with its automatic
rollback. Every system command goes to a fake (no systemctl, no network)."""
import contextlib
import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

KEY = "sk-panel-test-0123456789abcdefSECRET"
OR_KEY = "sk-or-v1-panel-test-fedcba9876543210"


def load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ph = load_module("panel_helper_under_test", os.path.join("scripts", "panel_helper.py"))
logging.getLogger("bitpin.panel_helper").setLevel(logging.CRITICAL)     # refusals are expected here

try:
    from bitpin import vpn as vpn_mod        # noqa: F401 - built in parallel (v3.1)
    HAVE_VPN = hasattr(vpn_mod, "parse_share_link")
except ImportError:
    HAVE_VPN = False


class FakeRun(object):
    """systemctl / journalctl / the bitpin-bot command line, as a small state machine."""

    def __init__(self, t):
        self.calls = []
        self.states = {"bitpin-bot": "active", "bitpin-bot-notify": "active", "xray-tunnel": "active",
                       "bitpin-bot-panel": "active"}
        self.confirmed = True
        self.show_rc = 0
        self.stop_rc = 0
        self.worker_out = '{"ok": true, "models": [{"id": "moonshotai/kimi-k3"}]}'
        self.perf_out = 'a warning on stderr\n{"totals":{"trades":2},"history":[]}'
        self.limits = []
        self.cli = os.path.join(t, "bitpin-bot")

    def __call__(self, argv, timeout, env=None, cwd=None, limit=None):
        self.calls.append((list(argv), dict(env or {})))
        self.limits.append(limit)
        a = list(argv)
        if a[:2] == ["systemctl", "show"]:
            st = self.states.get(a[2], "inactive")
            return 0, "ActiveState=%s\nSubState=x\nUnitFileState=enabled\nStateChangeTimestamp=t\nLoadState=loaded\n" % st
        if a[0] == "systemctl" and a[1] in ("stop", "start", "restart"):
            if a[1] == "stop" and self.stop_rc:
                return self.stop_rc, "stop failed"
            self.states[a[2]] = "inactive" if a[1] == "stop" else "active"
            return 0, ""
        if a[0] == "systemctl":
            return 0, ""
        if a[0] == self.cli:
            if a[1:3] == ["confirm-live", "--check"]:
                return (0, "CONFIRMED: ok") if self.confirmed else (1, "NOT CONFIRMED: the settings differ")
            if a[1:3] == ["confirm-live", "--show"]:
                return self.show_rc, "Live trading confirmation ...\nsettings digest: abc"
            if a[1:3] == ["confirm-live", "--typed-phrase"]:
                if a[3] == ph.LIVE_PHRASE:
                    self.confirmed = True
                    return 0, "confirmed"
                return 1, "not confirmed"
            if a[1] == "health":
                return 0, "RESULT: OK"
            if a[1] == "check":
                return 0, "RESULT: OK"
        if a[0] == "journalctl":
            return 0, "line 1\nline 2\n"
        if a[0] == "runuser" and "perf-worker" in a:
            return 0, self.perf_out + "\n"
        if a[0] == "runuser" and "state-worker" in a:
            return 0, '{"equity": {"irt": 4000000}, "positions": [], "state_error": null}\n'
        if a[0] == "runuser":
            return 0, self.worker_out + "\n"
        return 127, "unexpected command %r" % (a,)

    def argvs(self):
        return [c[0] for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="bitpin_panel_helper_")
        self.addCleanup(shutil.rmtree, self.t, True)
        d = lambda *p: os.path.join(self.t, *p)  # noqa: E731
        for sub in ("etc", "panel-etc", "state", "notify", "backups", "xray"):
            os.makedirs(d(sub))
        shutil.copy(os.path.join(ROOT, "config.example.json"), d("etc", "config.json"))
        shutil.copy(os.path.join(ROOT, "kimi.example.json"), d("etc", "kimi.json"))
        with open(d("etc", "bitpin-bot.env"), "w", encoding="utf-8") as f:
            f.write("# the service environment\nBITPIN_API_KEY=abc\nKIMI_API_KEY=%s\n"
                    "KIMI_HTTPS_PROXY=http://127.0.0.1:1081\n" % KEY)
        with open(d("panel-etc", "panel.json"), "w", encoding="utf-8") as f:
            json.dump({"port": 8443, "username": "owner", "password_hash": "x", "totp_secret": None}, f)
        self.run_fake = FakeRun(self.t)
        self.paths = ph.Paths(app_dir=ROOT, etc_dir=d("etc"), panel_etc_dir=d("panel-etc"), state_dir=d("state"), notify_state_dir=d("notify"),
                              backup_dir=d("backups"), xray_conf=d("xray", "config.json"), cli=self.run_fake.cli,
                              lock_file=d("helper.lock"))
        self.paths.state_in_process = True                 # production: the state-worker as user bitpin
        self.h = ph.Helper(self.paths, run=self.run_fake, sleep=lambda s: None)

    def call(self, cmd, actor=None, **args):
        resp = self.h.handle({"cmd": cmd, "args": args, "actor": actor or {"user": "owner", "ip": "203.0.113.7"}})
        self.assertNotIn(KEY, json.dumps(resp, default=str))
        return resp

    def ok(self, cmd, **args):
        resp = self.call(cmd, **args)
        self.assertTrue(resp["ok"], resp)
        return resp["data"]

    def err(self, cmd, **args):
        resp = self.call(cmd, **args)
        self.assertFalse(resp["ok"], resp)
        return resp["error"]

    def text(self, *p):
        with open(os.path.join(self.t, *p), "r", encoding="utf-8") as f:
            return f.read()

    def events(self):
        out = []
        for name in sorted(os.listdir(os.path.join(self.t, "notify"))):
            if name.startswith(ph.EVENT_PREFIX):
                out.append(json.loads(self.text("notify", name)))
        return out


class TestProtocol(Base):
    def test_only_the_allowlist_exists(self):
        for cmd in ("rm", "shell", "__init__", "", None, 5, "cmd_status"):
            self.assertEqual(self.h.handle({"cmd": cmd, "args": {}}), {"ok": False, "error": "unknown command"})
        self.assertFalse(self.h.handle({"cmd": "status", "args": []})["ok"])
        self.assertFalse(self.h.handle("status")["ok"])
        self.assertEqual(sorted(ph.COMMANDS), sorted([
            "status", "config_get", "secrets_status", "health", "check", "logs", "confirm_show", "models", "vpn_get",
            "vpn_test", "audit_notify", "config_put", "settings_set", "model_set", "secret_set", "apply_live",
            "service", "panel_password_set", "panel_totp_set", "vpn_put", "performance", "technical"]))

    def test_serve_reads_one_bounded_json_line(self):
        out = io.BytesIO()
        ph.serve(io.BytesIO(b'{"cmd": "config_get", "args": {}}\n'), out, self.h)
        resp = json.loads(out.getvalue().decode("utf-8"))
        self.assertTrue(resp["ok"])
        self.assertIn('"risk"', resp["data"]["config"])
        out = io.BytesIO()
        ph.serve(io.BytesIO(b"not json\n"), out, self.h)
        self.assertFalse(json.loads(out.getvalue().decode("utf-8"))["ok"])
        out = io.BytesIO()
        ph.serve(io.BytesIO(b"x" * (ph.MAX_REQUEST + 10)), out, self.h)
        self.assertIn("too large", json.loads(out.getvalue().decode("utf-8"))["error"])

    def test_peers_and_actor(self):
        self.assertTrue(ph.allowed_peer(None, "bitpin-panel"))
        self.assertTrue(ph.allowed_peer(0, "bitpin-panel"))
        self.assertFalse(ph.allowed_peer(4242, "no-such-user-xyz"))
        self.assertEqual(ph.clean_actor({"user": "owner", "ip": "2001:db8::1"}), {"user": "owner", "ip": "2001:db8::1"})
        self.assertEqual(ph.clean_actor({"user": "<b>x</b>", "ip": "1.2.3.4; rm"}), {"user": "", "ip": ""})
        self.assertEqual(ph.clean_actor("x"), {"user": "", "ip": ""})

    def test_the_phrase_is_the_bots_phrase(self):
        rb = load_module("run_bot_for_panel_helper", os.path.join("scripts", "run_bot.py"))
        self.assertEqual(ph.LIVE_PHRASE, rb.LIVE_PHRASE)


class TestSecrets(Base):
    def test_status_never_shows_a_value(self):
        data = self.ok("secrets_status")
        self.assertEqual(data["KIMI_API_KEY"], {"set": True, "length": len(KEY)})
        self.assertEqual(data["OPENROUTER_API_KEY"], {"set": False, "length": 0})
        self.assertEqual(list(data), list(ph.SECRET_NAMES))

    def test_set_replace_and_remove(self):
        data = self.ok("secret_set", name="OPENROUTER_API_KEY", value=" %s " % OR_KEY)
        self.assertEqual(data, {"name": "OPENROUTER_API_KEY", "set": True, "length": len(OR_KEY),
                                "restart_needed": True})
        env = self.text("etc", "bitpin-bot.env")
        self.assertIn("OPENROUTER_API_KEY=%s\n" % OR_KEY, env)
        self.assertTrue(env.startswith("# the service environment\n"))
        self.ok("secret_set", name="KIMI_API_KEY", value="sk-new-value-123")
        env = self.text("etc", "bitpin-bot.env")
        self.assertIn("KIMI_API_KEY=sk-new-value-123\n", env)
        self.assertNotIn(KEY, env)
        self.ok("secret_set", name="OPENROUTER_API_KEY", value="")
        self.assertNotIn("OPENROUTER_API_KEY", self.text("etc", "bitpin-bot.env"))
        backups = [f for f in os.listdir(os.path.join(self.t, "backups")) if f.startswith("bitpin-bot.env.")]
        self.assertEqual(len(backups), 3)
        if os.name == "posix":
            for f in backups:
                self.assertEqual(os.stat(os.path.join(self.t, "backups", f)).st_mode & 0o777, 0o600)
        ev = self.events()
        self.assertEqual([(e["event"], e["what"], e["arg"]) for e in ev][:1],
                         [("change", "secret", "OPENROUTER_API_KEY")])
        for e in ev:
            self.assertNotIn(OR_KEY, json.dumps(e))
            self.assertEqual((e["user"], e["ip"]), ("owner", "203.0.113.7"))

    def test_refused_names_and_values(self):
        self.assertIn("name must be", self.err("secret_set", name="PATH", value="x"))
        self.assertIn("name must be", self.err("secret_set", name="TELEGRAM_BOT_TOKEN", value="x"))
        for bad in ("two words", "a\nFOO=bar", 'q"uote', "back\\slash", "$HOME", "x" * 5000, 5):
            self.assertFalse(self.call("secret_set", name="LLM_API_KEY", value=bad)["ok"], bad)
        self.assertIn("proxy URL", self.err("secret_set", name="KIMI_HTTPS_PROXY", value="127.0.0.1:1081"))
        self.ok("secret_set", name="KIMI_HTTPS_PROXY", value="socks5://127.0.0.1:1080")
        self.assertNotIn("LLM_API_KEY", self.text("etc", "bitpin-bot.env"))


class TestSettings(Base):
    def test_config_put_validates_backs_up_and_reports_the_confirmation(self):
        old = self.text("etc", "config.json")
        doc = json.loads(old)
        doc["risk"]["max_drawdown"] = 0.4
        new = json.dumps(doc, indent=2)
        data = self.ok("config_put", file="config", text=new, dry_run=True)
        self.assertEqual((data["problems"], data["written"]), ([], False))
        self.assertIn('+    "max_drawdown": 0.4', data["diff"])
        self.assertEqual(self.text("etc", "config.json"), old)
        self.run_fake.confirmed = False                    # the new settings are not confirmed yet
        data = self.ok("config_put", file="config", text=new)
        self.assertTrue(data["written"])
        self.assertTrue(data["confirm_needed"])
        self.assertTrue(os.path.basename(data["backup"]).startswith("config.json."))
        self.assertEqual(json.loads(self.text("etc", "config.json"))["risk"]["max_drawdown"], 0.4)
        self.assertEqual(self.events()[-1]["what"], "settings")

    def test_config_put_refusals(self):
        self.assertIn("not valid JSON", self.err("config_put", file="config", text="{"))
        self.assertIn("duplicate", self.err("config_put", file="config", text='{"a": 1, "a": 2}'))
        self.assertIn("file must be", self.err("config_put", file="bitpin-bot.env", text="{}"))
        doc = json.loads(self.text("etc", "config.json"))
        doc["risk"]["max_drawdown"] = 7
        data = self.ok("config_put", file="config", text=json.dumps(doc))
        self.assertTrue(data["problems"])
        self.assertFalse(data["written"])
        self.assertEqual(json.loads(self.text("etc", "config.json"))["risk"]["max_drawdown"], 0.5)

    def test_settings_set_writes_only_real_changes(self):
        c = lambda f, p, v: {"file": f, "path": p.split("."), "value": v}  # noqa: E731
        data = self.ok("settings_set", changes=[c("config", "risk.max_drawdown", 0.5)])
        self.assertEqual((data["written"], data["changed"], data["diff"]), (False, [], ""))
        data = self.ok("settings_set", changes=[c("config", "risk.max_drawdown", 0.45),
                                                c("kimi", "brain.held_move_pct", 10.0)], dry_run=True)
        self.assertEqual(len(data["changed"]), 2)
        self.assertFalse(data["written"])
        self.assertIn("config.json", data["diff"])
        self.assertIn("kimi.json", data["diff"])
        data = self.ok("settings_set", changes=[c("config", "risk.max_drawdown", 0.45),
                                                c("kimi", "brain.held_move_pct", 10.0)])
        self.assertTrue(data["written"])
        self.assertEqual(json.loads(self.text("etc", "kimi.json"))["brain"]["held_move_pct"], 10.0)
        self.assertEqual(len(data["backup"].split(", ")), 2)
        data = self.ok("settings_set", changes=[c("config", "state_dir", "/tmp")])
        self.assertTrue(data["problems"])
        self.assertFalse(data["written"])

    def test_model_set_openrouter(self):
        self.assertIn("OPENROUTER_API_KEY", self.err("model_set", stage="llm", provider="openrouter",
                                                     base_url="https://openrouter.ai/api/v1", key_name="KIMI_API_KEY",
                                                     model="moonshotai/kimi-k3"))
        self.assertIn("does not match", self.err("model_set", stage="llm", provider="moonshot",
                                                 base_url="https://openrouter.ai/api/v1", model="x"))
        self.assertIn("https", self.err("model_set", stage="llm", base_url="http://openrouter.ai/api/v1", model="x"))
        self.assertIn("https", self.err("model_set", stage="llm", base_url="https://u:p@openrouter.ai/api/v1",
                                        model="x"))
        self.assertIn("model", self.err("model_set", stage="llm", base_url="https://openrouter.ai/api/v1",
                                        model="a b"))
        data = self.ok("model_set", stage="llm", provider="openrouter", base_url="https://openrouter.ai/api/v1/",
                       model="moonshotai/kimi-k3", reasoning_effort="high",
                       prices={"price_in_per_m": 0.6, "price_out_per_m": 2.5, "price_cached_in_per_m": None},
                       dry_run=True)
        paths = [x["path"] for x in data["changed"]]
        self.assertIn("llm.base_url", paths)
        self.assertIn("llm.model", paths)
        self.assertIn("llm.price_in_per_m", paths)
        self.assertFalse(data["written"])
        from bitpin import llm as llm_mod
        if "api_key_env" in llm_mod.DEFAULT_LLM_CONFIG:
            self.assertIn("llm.api_key_env", paths)
        self.assertIn('"https://openrouter.ai/api/v1"', data["diff"])


class TestLiveAndServices(Base):
    def test_apply_live_sequence(self):
        self.assertIn("type exactly", self.err("apply_live", phrase="i accept"))
        self.assertEqual(self.run_fake.calls, [])
        self.run_fake.confirmed = False
        data = self.ok("apply_live", phrase=" I ACCEPT THE RISK ")
        self.assertTrue(data["ok"], data)
        seq = [a[1:3] if a[0] == self.run_fake.cli else a[:2] for a in self.run_fake.argvs()
               if a[:2] != ["systemctl", "show"]]
        self.assertEqual(seq, [["confirm-live", "--show"], ["systemctl", "stop"], ["confirm-live", "--typed-phrase"],
                               ["systemctl", "reset-failed"], ["systemctl", "start"], ["health"]])
        self.assertEqual([s["step"] for s in data["steps"]], ["check", "stop", "confirm", "start"])
        self.assertEqual(data["bot_state"], "active")
        self.assertEqual(self.events()[-1]["what"], "apply_live")

    def test_apply_live_stops_nothing_when_the_settings_cannot_be_confirmed(self):
        self.run_fake.show_rc = 78
        data = self.ok("apply_live", phrase="I ACCEPT THE RISK")
        self.assertFalse(data["ok"])
        self.assertNotIn(["systemctl", "stop", "bitpin-bot"], self.run_fake.argvs())
        self.assertEqual(self.run_fake.states["bitpin-bot"], "active")

    def test_apply_live_without_start(self):
        data = self.ok("apply_live", phrase="I ACCEPT THE RISK", start=False)
        self.assertNotIn(["systemctl", "start", "bitpin-bot"], self.run_fake.argvs())
        self.assertEqual(data["bot_state"], "inactive")

    def test_service_allowlist_and_the_confirmation_guard(self):
        self.assertIn("unit must be", self.err("service", unit="ssh", action="restart"))
        self.assertIn("unit must be", self.err("service", unit="nginx", action="stop"))
        self.assertIn("action must be", self.err("service", unit="bitpin-bot", action="disable"))
        self.run_fake.confirmed = False
        self.assertIn("would not start", self.err("service", unit="bitpin-bot", action="restart"))
        self.ok("service", unit="bitpin-bot", action="stop")
        self.run_fake.confirmed = True
        data = self.ok("service", unit="xray-tunnel", action="restart")
        self.assertEqual(data["state"], "active")
        self.assertIn(["systemctl", "restart", "xray-tunnel"], self.run_fake.argvs())

    def test_logs_health_check_and_status(self):
        self.assertIn("unit must be", self.err("logs", unit="sshd", lines=10))
        for bad in (0, 501, "10", True):
            self.assertFalse(self.call("logs", unit="bitpin-bot", lines=bad)["ok"])
        self.assertEqual(self.ok("logs", unit="bitpin-bot", lines=20)["text"], "line 1\nline 2\n")
        self.assertIn(["journalctl", "-u", "bitpin-bot", "-n", "20", "--no-pager", "-o", "short-iso"],
                      self.run_fake.argvs())
        self.assertEqual(self.ok("health"), {"ok": True, "text": "RESULT: OK"})
        self.assertTrue(self.ok("check")["ok"])
        self.assertTrue(self.ok("confirm_show")["ok"])
        st = self.ok("status")
        self.assertEqual(st["bot"]["state"], "active")
        self.assertTrue(st["bot"]["version"])
        self.assertEqual(st["live_confirmed"], {"ok": True, "why": "CONFIRMED: ok"})
        for k in ("equity", "last_decision", "positions", "resting_orders", "spend", "status_fa", "last_decision_fa"):
            self.assertIn(k, st)


class TestPrivilegeSeparation(Base):
    """Security review (v3.1): root never opens a path inside a directory the user bitpin controls, except
    through a file descriptor it created itself."""

    def test_the_bot_state_is_read_by_a_worker_running_as_bitpin(self):
        self.paths.state_in_process = False
        st = self.ok("status")
        self.assertEqual(st["equity"], {"irt": 4000000})
        argv = [c[0] for c in self.run_fake.calls if c[0][0] == "runuser" and "state-worker" in c[0]][-1]
        self.assertEqual(argv[:4], ["runuser", "-u", "bitpin", "--"])
        self.assertEqual(argv[argv.index("--state-dir") + 1], self.paths.state_dir)

    def test_the_state_worker_prints_one_json_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ph.main(["state-worker", "--state-dir", self.paths.state_dir, "--notify-state-dir",
                          self.paths.notify_state_dir, "--notify-conf", os.path.join(self.t, "missing.json")])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue().strip().splitlines()[-1])
        for k in ("equity", "last_decision", "positions", "resting_orders", "spend", "status_fa", "state_error"):
            self.assertIn(k, data)

    @unittest.skipUnless(os.name == "posix", "symlinks")
    def test_a_planted_symlink_is_never_followed_by_the_relay(self):
        victim = os.path.join(self.t, "victim.txt")
        with open(victim, "w", encoding="utf-8") as f:
            f.write("keep me")
        os.chmod(victim, 0o600)
        real = ph.secrets.token_hex
        ph.secrets.token_hex = lambda n: "abcd1234"
        self.h.clock = lambda: 1700000000.0
        try:
            name = "%s%d.abcd1234.json" % (ph.EVENT_PREFIX, 1700000000000)
            os.symlink(victim, os.path.join(self.t, "notify", ".%s.tmp" % name))
            self.assertFalse(self.h.relay("change", "password"))           # O_EXCL | O_NOFOLLOW: refused
        finally:
            ph.secrets.token_hex = real
        with open(victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep me")
        self.assertEqual(os.stat(victim).st_mode & 0o777, 0o600)
        self.assertTrue(self.h.relay("change", "password"))                # a fresh name works
        ev = [f for f in os.listdir(os.path.join(self.t, "notify")) if f.startswith(ph.EVENT_PREFIX)]
        self.assertEqual(len(ev), 1)
        self.assertEqual(os.stat(os.path.join(self.t, "notify", ev[0])).st_mode & 0o777, 0o644)


class TestPerformance(Base):
    """v3.6: the P&L report of a time range, read like the bot state: by a worker running as the user bitpin,
    with public candles only (tests/test_performance.py checks the numbers)."""

    def setUp(self):
        super(TestPerformance, self).setUp()
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import test_performance as tp                      # the synthetic world of the report's own tests
        self.tp = tp
        with open(os.path.join(self.paths.state_dir, "kimi_runner.jsonl"), "w", encoding="utf-8") as f:
            for rec in tp.world_records():
                f.write(json.dumps(rec) + "\n")
        with open(os.path.join(self.paths.state_dir, "kimi_equity.json"), "w", encoding="utf-8") as f:
            json.dump({"equity_start_irt": tp.START}, f)
        self.now = tp.cycle(29) + 600
        self.h.clock = lambda: self.now
        self.fetched = []

        def fetch(symbol, res, start, end):
            self.fetched.append((symbol, res))
            return tp.make_bars(tp.PRICE[symbol.split("_")[0]], start, end, now=self.now)
        self.h.fetch_bars = fetch

    def test_the_range_is_checked(self):
        now = int(self.now)
        for a in ({}, {"from": "1", "to": now}, {"from": now - 10, "to": float(now)}, {"from": True, "to": now},
                  {"from": now, "to": now - 10}, {"from": now - 10, "to": now + 7200}, {"from": 1000, "to": now},
                  {"from": now - 401 * 86400, "to": now}):
            self.assertFalse(self.call("performance", **a)["ok"], a)
        self.assertEqual(self.fetched, [])

    def test_the_report_of_a_range(self):
        data = self.ok("performance", **{"from": int(self.tp.T0), "to": int(self.now)})
        self.assertEqual(data["totals"]["trades"], 4)
        self.assertEqual(len(data["history"]), 4)
        self.assertEqual(sorted(set(self.fetched)), [("BTC_IRT", "60"), ("USDT_IRT", "60")])
        self.assertAlmostEqual(sum(a["pnl_irt"] for a in data["assets"]), data["rebuilt_value_to_irt"] - self.tp.START,
                               places=3)
        json.dumps(data)

    def test_production_reads_as_bitpin_with_a_larger_output_limit(self):
        self.paths.state_in_process = False
        data = self.ok("performance", **{"from": int(self.now) - 86400, "to": int(self.now)})
        self.assertEqual(data, {"totals": {"trades": 2}, "history": []})
        argv = [c[0] for c in self.run_fake.calls if c[0][0] == "runuser" and "perf-worker" in c[0]][-1]
        self.assertEqual(argv[:4], ["runuser", "-u", "bitpin", "--"])
        self.assertEqual(argv[argv.index("--state-dir") + 1], self.paths.state_dir)
        self.assertEqual(argv[argv.index("--from") + 1:argv.index("--from") + 4],
                         [str(int(self.now) - 86400), "--to", str(int(self.now))])
        self.assertEqual(self.run_fake.limits[-1], ph.PERF_MAX_OUTPUT)
        self.run_fake.perf_out = "Traceback: boom"
        self.assertIn("the performance report failed", self.err("performance", **{"from": int(self.now) - 86400,
                                                                                    "to": int(self.now)}))

    def test_the_worker_prints_one_compact_json_line(self):
        from unittest import mock
        out = io.StringIO()
        with mock.patch("bitpin.data.fetch_bars", self.h.fetch_bars), mock.patch("time.time", lambda: self.now):
            with contextlib.redirect_stdout(out):
                rc = ph.main(["perf-worker", "--state-dir", self.paths.state_dir, "--from", str(int(self.tp.T0)),
                              "--to", str(int(self.now))])
        self.assertEqual(rc, 0)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertNotIn(", ", lines[0][:200])
        self.assertEqual(json.loads(lines[0])["totals"]["trades"], 4)

    def test_a_long_output_keeps_its_end_up_to_the_limit(self):
        self.assertEqual(ph.cap("x" * 10, 20), "x" * 10)
        self.assertTrue(ph.cap("a" * 50 + "{}", 20).endswith("a" * 18 + "{}"))


class TestPanelLogin(Base):
    def test_password_hash_and_totp(self):
        good = "pbkdf2_sha256$600000$" + "A" * 22 + "$" + "B" * 43
        self.assertIn("password_hash", self.err("panel_password_set", password_hash="plaintext"))
        self.assertIn("password_hash", self.err("panel_password_set",
                                                password_hash="pbkdf2_sha256$1000$" + "A" * 22 + "$" + "B" * 43))
        self.ok("panel_password_set", password_hash=good)
        conf = json.loads(self.text("panel-etc", "panel.json"))
        self.assertEqual((conf["password_hash"], conf["username"], conf["port"]), (good, "owner", 8443))
        self.assertIn("base32", self.err("panel_totp_set", totp_secret="not base32!"))
        self.ok("panel_totp_set", totp_secret="JBSWY3DPEHPK3PXPJBSWY3DP")
        self.assertEqual(json.loads(self.text("panel-etc", "panel.json"))["totp_secret"], "JBSWY3DPEHPK3PXPJBSWY3DP")
        self.ok("panel_totp_set", totp_secret=None)
        self.assertIsNone(json.loads(self.text("panel-etc", "panel.json"))["totp_secret"])
        self.assertEqual([(e["what"], e["arg"]) for e in self.events()],
                         [("password", ""), ("totp", "on"), ("totp", "off")])

    def test_audit_notify_and_the_relay_cap(self):
        self.assertIn("event must be", self.err("audit_notify", event="anything"))
        self.assertTrue(self.ok("audit_notify", event="login_ok", user="owner", ip="198.51.100.4")["relayed"])
        e = self.events()[-1]
        self.assertEqual((e["event"], e["user"], e["ip"]), ("login_ok", "owner", "198.51.100.4"))
        self.ok("audit_notify", event="login_locked", user="<script>", ip="x; y")
        e = [x for x in self.events() if x["event"] == "login_locked"][-1]
        self.assertEqual((e["user"], e["ip"]), ("", ""))
        for _ in range(ph.MAX_PENDING_EVENTS):
            self.ok("audit_notify", event="login_ok", user="owner", ip="1.2.3.4")
        self.assertEqual(len(self.events()), ph.MAX_PENDING_EVENTS)
        shutil.rmtree(os.path.join(self.t, "notify"))
        self.assertFalse(self.ok("audit_notify", event="login_ok", user="owner", ip="1.2.3.4")["relayed"])


class TestModels(Base):
    def test_models_runs_the_worker_as_the_bot_user_with_the_key_in_the_environment(self):
        self.assertIn("not set", self.err("models", base_url="https://openrouter.ai/api/v1"))
        self.ok("secret_set", name="OPENROUTER_API_KEY", value=OR_KEY)
        data = self.ok("models", base_url="https://openrouter.ai/api/v1")
        self.assertEqual((data["provider"], data["key_name"]), ("openrouter", "OPENROUTER_API_KEY"))
        self.assertEqual(data["models"], [{"id": "moonshotai/kimi-k3"}])
        argv, env = [c for c in self.run_fake.calls if c[0][0] == "runuser"][-1]
        self.assertEqual(argv[:4], ["runuser", "-u", "bitpin", "--"])
        self.assertIn("models-worker", argv)
        self.assertNotIn(OR_KEY, " ".join(argv))
        self.assertEqual(env["OPENROUTER_API_KEY"], OR_KEY)
        self.assertNotIn("KIMI_API_KEY", env)
        self.assertNotIn("BITPIN_API_KEY", env)
        self.run_fake.worker_out = '{"ok": false, "error": "HTTP 401"}'
        self.assertIn("HTTP 401", self.err("models", base_url="https://openrouter.ai/api/v1"))
        self.assertIn("KIMI_API_KEY", self.err("models", base_url="https://api.moonshot.ai/v1",
                                               key_name="OPENROUTER_API_KEY"))


class TestKeysStayWithTheirHosts(Base):
    """Security review: a stolen panel session must not be able to send a key to another server."""

    def test_the_bitpin_url_cannot_be_changed_from_the_panel(self):
        doc = json.loads(self.text("etc", "config.json"))
        doc["base_url"] = "https://api.bitpin.org.evil.example"
        data = self.ok("config_put", file="config", text=json.dumps(doc))
        self.assertFalse(data["written"])
        self.assertTrue(any("base_url must be https://api.bitpin.org" in p for p in data["problems"]), data)

    def test_a_llm_stage_must_use_the_key_of_its_own_platform(self):
        doc = json.loads(self.text("etc", "kimi.json"))
        doc["llm"]["base_url"] = "https://openrouter.ai/api/v1"          # still KIMI_API_KEY
        data = self.ok("config_put", file="kimi", text=json.dumps(doc))
        self.assertFalse(data["written"])
        self.assertTrue(any("api_key_env must be OPENROUTER_API_KEY" in p for p in data["problems"]), data)
        doc["llm"]["base_url"] = "https://collector.evil.example/v1"
        doc["llm"]["api_key_env"] = "LLM_API_KEY"
        data = self.ok("config_put", file="kimi", text=json.dumps(doc))
        self.assertFalse(data["written"])
        self.assertTrue(any("LLM_API_KEY belongs to" in p for p in data["problems"]), data)
        self.assertEqual(ph.endpoint_problems({}, {}), [])                  # the built-in defaults are fine
        # a news stage without its own base_url / key follows the llm stage to OpenRouter with its key
        both = {"llm": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
                "news": {"enabled": True, "model": "moonshotai/kimi-k2.6"}}
        self.assertEqual(ph.endpoint_problems({}, both), [])
        both["news"]["api_key_env"] = "KIMI_API_KEY"
        self.assertEqual(len(ph.endpoint_problems({}, both)), 1)

    def test_a_generic_key_is_bound_to_one_host(self):
        self.assertIn("base_url", self.err("secret_set", name="LLM_API_KEY", value="sk-generic-123"))
        self.assertIn("OPENROUTER_API_KEY", self.err("secret_set", name="LLM_API_KEY", value="sk-generic-123",
                                                     base_url="https://openrouter.ai/api/v1"))
        self.ok("secret_set", name="LLM_API_KEY", value="sk-generic-123", base_url="https://api.example.com/v1")
        env = self.text("etc", "bitpin-bot.env")
        self.assertIn("LLM_API_KEY=sk-generic-123\n", env)
        self.assertIn("LLM_API_HOST=api.example.com\n", env)
        self.assertIn("belongs to api.example.com", self.err("models", base_url="https://other.example.com/v1"))
        self.assertEqual(self.ok("models", base_url="https://api.example.com/v1")["key_name"], "LLM_API_KEY")
        self.ok("secret_set", name="LLM_API_KEY", value="")
        env = self.text("etc", "bitpin-bot.env")
        self.assertNotIn("LLM_API_KEY", env)
        self.assertNotIn("LLM_API_HOST", env)


XRAY_CONF = {"log": {"loglevel": "warning"},
             "inbounds": [{"listen": "127.0.0.1", "port": 1081, "protocol": "http"}],
             "outbounds": [{"tag": "proxy", "protocol": "vless",
                            "settings": {"vnext": [{"address": "old.example.com", "port": 443,
                                                    "users": [{"id": "11111111-2222-3333-4444-555555555555",
                                                               "encryption": "none"}]}]},
                            "streamSettings": {"network": "tcp", "security": "tls"}},
                           {"tag": "direct", "protocol": "freedom"}]}
NEW_LINK = ("vless://99999999-8888-7777-6666-555555555555@new.example.com:443?encryption=none&security=tls"
            "&sni=new.example.com&type=ws&host=new.example.com&path=%2Fws#new")


@unittest.skipUnless(HAVE_VPN, "bitpin/vpn.py is not there yet")
class TestVpn(Base):
    def setUp(self):
        super().setUp()
        with open(self.paths.xray_conf, "w", encoding="utf-8") as f:
            json.dump(XRAY_CONF, f, indent=2)
        fake = os.path.join(self.t, "fake_xray.py")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.exit(1 if 'BROKEN' in open(sys.argv[-1]).read() else 0)\n")
        self.paths.xray_cmd = [sys.executable, fake]
        self.proxy_ok = True
        self.h._proxy_test = lambda: [{"target": "%s:443" % h, "ok": self.proxy_ok, "ms": 5, "error": None}
                                      for h in ("api.moonshot.ai", "api.telegram.org", "openrouter.ai")]

    def test_get_is_masked_unless_raw(self):
        data = self.ok("vpn_get")
        self.assertIsNone(data["raw"])
        self.assertNotIn("11111111-2222-3333-4444-555555555555", json.dumps(data))
        self.assertIn("old.example.com", json.dumps(data["summary"]))
        self.assertIn("11111111-2222-3333-4444-555555555555", self.ok("vpn_get", raw=True)["raw"])

    def test_link_import_dry_run_then_apply(self):
        data = self.ok("vpn_put", link=NEW_LINK, dry_run=True)
        self.assertTrue(data["xray_test"]["ok"], data)
        self.assertFalse(data["written"])
        self.assertNotIn("99999999-8888", data["diff"])
        self.assertEqual(json.loads(self.text("xray", "config.json")), XRAY_CONF)
        data = self.ok("vpn_put", link=NEW_LINK)
        self.assertTrue(data["written"])
        self.assertFalse(data["rolled_back"])
        new = json.loads(self.text("xray", "config.json"))
        self.assertEqual(new["inbounds"], XRAY_CONF["inbounds"])
        self.assertEqual(new["outbounds"][0]["tag"], "proxy")
        self.assertIn("new.example.com", json.dumps(new["outbounds"][0]))
        self.assertEqual(new["outbounds"][1], XRAY_CONF["outbounds"][1])
        self.assertIn(["systemctl", "restart", "xray-tunnel"], self.run_fake.argvs())
        self.assertTrue(os.path.basename(data["backup"]).startswith("xray-config.json."))
        self.assertEqual(self.events()[-1]["what"], "vpn")

    def test_a_config_that_does_not_work_is_rolled_back(self):
        self.proxy_ok = False
        data = self.ok("vpn_put", link=NEW_LINK)
        self.assertTrue(data["written"])
        self.assertTrue(data["rolled_back"])
        self.assertEqual(json.loads(self.text("xray", "config.json")), XRAY_CONF)
        self.assertEqual(self.run_fake.argvs().count(["systemctl", "restart", "xray-tunnel"]), 2)
        data = self.ok("vpn_put", link=NEW_LINK, keep_on_failure=True)
        self.assertFalse(data["rolled_back"])
        self.assertIn("new.example.com", self.text("xray", "config.json"))

    def test_a_config_with_comments_still_takes_a_link(self):
        commented = "// the owner notes\n" + json.dumps(XRAY_CONF, indent=2)
        with open(self.paths.xray_conf, "w", encoding="utf-8") as f:
            f.write(commented)
        self.assertIn("old.example.com", json.dumps(self.ok("vpn_get")["summary"]))
        data = self.ok("vpn_put", link=NEW_LINK)
        self.assertTrue(data["written"])
        self.assertIn("new.example.com", self.text("xray", "config.json"))

    def test_new_inbounds_must_stay_on_the_loopback(self):
        pub = json.loads(json.dumps(XRAY_CONF))
        pub["inbounds"].append({"port": 1090, "protocol": "socks", "settings": {"auth": "noauth"}})
        self.assertIn("must listen on 127.0.0.1", self.err("vpn_put", text=json.dumps(pub)))
        pub["inbounds"][-1]["listen"] = "0.0.0.0"
        self.assertIn("must listen on 127.0.0.1", self.err("vpn_put", text=json.dumps(pub)))
        pub["inbounds"][-1]["listen"] = "127.0.0.1"
        self.assertTrue(self.ok("vpn_put", text=json.dumps(pub), dry_run=True)["xray_test"]["ok"])
        self.assertEqual(ph.inbound_problems(pub, pub), [])                  # an existing inbound is kept as is

    def test_xray_test_failure_and_bad_input_write_nothing(self):
        bad = json.loads(json.dumps(XRAY_CONF))
        bad["log"]["loglevel"] = "BROKEN"
        data = self.ok("vpn_put", text=json.dumps(bad))
        self.assertFalse(data["xray_test"]["ok"])
        self.assertFalse(data["written"])
        self.assertIn("give a share link", self.err("vpn_put"))
        self.assertIn("inbounds", self.err("vpn_put", text='{"outbounds": []}'))
        self.assertFalse(self.call("vpn_put", link="vmess://not-base64!")["ok"])
        self.assertEqual(json.loads(self.text("xray", "config.json")), XRAY_CONF)


if __name__ == "__main__":
    unittest.main()
