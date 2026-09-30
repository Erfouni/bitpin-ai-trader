"""v3.1: the panel's setup script (scripts/panel_setup.py) and its place in the deploy kit - the units,
the command line, update / rollback / uninstall. No root, no systemctl: pure functions and file checks."""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ps = load_module("panel_setup_under_test", os.path.join("scripts", "panel_setup.py"))


def read(*p):
    with open(os.path.join(ROOT, *p), "r", encoding="utf-8") as f:
        return f.read()


class TestSetupFunctions(unittest.TestCase):
    def test_usernames(self):
        for bad in ("ab", "admin", "Root", "a b", "x" * 33, "na;me", None):
            self.assertIsNotNone(ps.username_problem(bad), bad)
        for good in ("ops.k", "boss_42", "Kx-9"):
            self.assertIsNone(ps.username_problem(good), good)

    def test_build_conf_keeps_old_values_and_replaces_given_ones(self):
        c = ps.build_conf({}, username="boss_42", password_hash="h1", totp_secret="ABC", port=9443)
        self.assertEqual((c["username"], c["password_hash"], c["totp_secret"], c["port"]),
                         ("boss_42", "h1", "ABC", 9443))
        self.assertEqual(c["tls_cert"], "/etc/bitpin-bot-panel/tls/cert.pem")
        self.assertEqual(c["helper_socket"], "/run/bitpin-panel/helper.sock")
        self.assertIs(c["trusted_proxy"], False)
        c2 = ps.build_conf(dict(c, unknown_key=1), password_hash="h2")
        self.assertEqual((c2["username"], c2["password_hash"], c2["totp_secret"], c2["port"]),
                         ("boss_42", "h2", "ABC", 9443))
        self.assertNotIn("unknown_key", c2)
        self.assertIsNone(ps.build_conf(c2, totp_secret=None)["totp_secret"])
        self.assertEqual(sorted(c), sorted(ps.default_conf()))

    def test_write_and_read_conf(self):
        t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, t, True)
        p = os.path.join(t, "panel.json")
        self.assertEqual(ps.read_conf(p), {})
        ps.write_conf(ps.build_conf({}, username="boss_42"), p)
        self.assertEqual(ps.read_conf(p)["username"], "boss_42")
        if os.name == "posix":
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)
        self.assertEqual([f for f in os.listdir(t) if f.startswith(".panel.json.")], [])
        with open(p, "w", encoding="utf-8") as f:
            f.write("{broken")
        with self.assertRaises(ps.SetupError):
            ps.read_conf(p)

    def test_openssl_command_and_fingerprint(self):
        argv = ps.openssl_argv("/k.pem", "/c.pem", "203.0.113.5", ["203.0.113.5", "2001:db8::5"], "bot-server")
        self.assertEqual(argv[:3], ["openssl", "req", "-x509"])
        self.assertIn("-nodes", argv)
        self.assertEqual(argv[-1], "subjectAltName=IP:203.0.113.5,IP:2001:db8::5,DNS:bot-server")
        self.assertNotIn("DNS:bad host", " ".join(ps.openssl_argv("/k", "/c", "x", [], "bad host")))
        if shutil.which("openssl") is None:
            self.skipTest("no openssl here")
        t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, t, True)
        key, cert = os.path.join(t, "key.pem"), os.path.join(t, "cert.pem")
        r = subprocess.run(ps.openssl_argv(key, cert, "127.0.0.1", ["127.0.0.1"], "localhost"),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if r.returncode != 0:
            self.skipTest("this openssl cannot make the certificate: %s" % r.stdout[-200:])
        with open(cert, "r", encoding="ascii") as f:
            fp = ps.cert_fingerprint(f.read())
        self.assertRegex(fp, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        out = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-fingerprint", "-sha256"],
                             stdout=subprocess.PIPE).stdout.decode("ascii")
        self.assertIn(fp, out.upper())

    def test_server_addresses_skip_loopback(self):
        class R(object):
            stdout = b"203.0.113.5 127.0.0.1 2001:db8::5 fe80::1 10.0.0.2\n"
        self.assertEqual(ps.server_addresses(run=lambda *a, **k: R()), ["203.0.113.5", "10.0.0.2", "2001:db8::5"])

    def test_the_totp_question_with_the_real_auth_module(self):
        try:
            from bitpin import panel_auth as auth
        except ImportError:
            self.skipTest("bitpin/panel_auth.py is not there yet")
        state = {}
        answers = iter(["", "000000", "CODE"])

        def ask(q):
            a = next(answers)
            return auth.totp(state["secret"], 1700000000) if a == "CODE" else a

        real_new = auth.new_totp_secret

        def new_secret():
            state["secret"] = real_new()
            return state["secret"]
        auth.new_totp_secret = new_secret
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                secret = ps.ask_totp(ask, "boss_42", auth, lambda: 1700000000)
                self.assertIsNone(ps.ask_totp(lambda q: "n", "boss_42", auth, lambda: 0))
        finally:
            auth.new_totp_secret = real_new
        self.assertEqual(secret, state["secret"])
        self.assertIn("wrong code", out.getvalue())
        self.assertIn("2FA ON", out.getvalue())


class TestDeployKit(unittest.TestCase):
    def test_units(self):
        web = read("deploy", "bitpin-bot-panel.service")
        for s in ("User=bitpin-panel", "NoNewPrivileges=yes", "ProtectSystem=strict", "CapabilityBoundingSet=\n",
                  "--config /etc/bitpin-bot-panel/panel.json", "StateDirectory=bitpin-bot-panel",
                  "-/etc/bitpin-bot/bitpin-bot.env", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6"):
            self.assertIn(s, web)
        self.assertNotIn("User=root", web)
        sock = read("deploy", "bitpin-bot-panel-helper.socket")
        for s in ("ListenStream=/run/bitpin-panel/helper.sock", "SocketGroup=bitpin-panel", "SocketMode=0660",
                  "Accept=yes"):
            self.assertIn(s, sock)
        helper = read("deploy", "bitpin-bot-panel-helper@.service")
        for s in ("StandardInput=socket", "StandardOutput=socket", "scripts/panel_helper.py", "ProtectSystem=strict",
                  "ReadWritePaths=/etc/bitpin-bot /etc/bitpin-bot-panel /run/bitpin-panel", "RuntimeMaxSec=",
                  "MemoryMax=768M", "TasksMax=64"):
            self.assertIn(s, helper)

    def test_the_kit_installs_but_never_enables_the_panel(self):
        lib = read("deploy", "lib.sh")
        self.assertIn('PANEL_UNITS="bitpin-bot-panel.service bitpin-bot-panel-helper.socket '
                      'bitpin-bot-panel-helper@.service"', lib)
        self.assertIn("for u in $PANEL_UNITS; do", lib)
        for f in ("install.sh", "update.sh", "lib.sh"):
            self.assertNotIn("enable --now bitpin-bot-panel", read("deploy", f), f)
            self.assertNotIn("enable bitpin-bot-panel", read("deploy", f), f)
        upd = read("deploy", "update.sh")
        self.assertIn("for u in $OPS_UNITS $PANEL_UNITS; do", upd)             # a rollback drops them too
        self.assertEqual(upd.count("    restart_panel\n"), 2)
        self.assertIn("systemctl try-restart bitpin-bot-panel.service", upd)
        un = read("deploy", "uninstall.sh")
        self.assertIn("for u in $PANEL_UNITS; do", un)
        self.assertIn('"$PANEL_ETC_DIR" "$PANEL_STATE_DIR"', un)

    def test_the_command_line(self):
        cli = read("deploy", "bitpin-bot")
        self.assertIn("panel-setup|panel-password|panel-totp|panel-status)", cli)
        self.assertIn('exec "$PYTHON" "$APP_DIR/scripts/panel_setup.py" "${cmd#panel-}" "$@"', cli)
        self.assertIn("--check|--show) ;;", cli)
        self.assertIn("sudo bitpin-bot panel-setup", cli)

    def test_setup_needs_a_sane_port_and_a_command(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ps.main(["setup", "--port", "80"]), 2)
            self.assertEqual(ps.main([]), 2)


if __name__ == "__main__":
    unittest.main()
