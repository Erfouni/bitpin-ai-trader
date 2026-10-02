# -*- coding: utf-8 -*-
"""v3.8.2: a trusted certificate for the panel's domain - bitpin/panel_cert.py, 'bitpin-bot panel-cert' in
scripts/panel_setup.py (no root, no certbot, no network: fakes), the panel's reload on SIGHUP, the helper's
read-only panel_cert command and the certificate card of the Security page. Real certificates are made with the
openssl command line (skipped where there is none)."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from bitpin import panel_cert as pc  # noqa: E402


def load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ps = load_module("panel_setup_cert_under_test", os.path.join("scripts", "panel_setup.py"))
srv = load_module("panel_server_cert_under_test", os.path.join("scripts", "panel_server.py"))
TOKEN = "Tk_" + "x" * 37                       # the shape of a Cloudflare API token, nothing real


def read(path):
    with open(path) as f:
        return f.read()


def openssl(*args):
    r = subprocess.run(["openssl"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise unittest.SkipTest("this openssl cannot do it: %s" % r.stdout[-200:])


class Certs(object):
    """A self-signed pair like panel-setup's (the panel's own) and a CA-issued chain like a Let's Encrypt lineage."""

    def __init__(self, d):
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("no openssl here")
        ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
        self.self_cert, self.self_key = os.path.join(d, "self.pem"), os.path.join(d, "self.key")
        openssl("req", "-x509", *ec, "-days", "825", "-subj", "/CN=127.0.0.1", "-keyout", self.self_key,
                "-out", self.self_cert, "-addext", "subjectAltName=IP:127.0.0.1")
        ca_key, ca_cert = os.path.join(d, "ca.key"), os.path.join(d, "ca.pem")
        openssl("req", "-x509", *ec, "-days", "30", "-subj", "/O=Test CA/CN=T1", "-keyout", ca_key, "-out", ca_cert)
        self.lineage = os.path.join(d, "lineage")
        os.makedirs(self.lineage)
        key, csr, leaf = (os.path.join(self.lineage, "privkey.pem"), os.path.join(d, "leaf.csr"),
                          os.path.join(d, "leaf.pem"))
        openssl("req", *ec, "-subj", "/CN=panel.example.com", "-keyout", key, "-out", csr)
        ext = os.path.join(d, "ext.cnf")
        with open(ext, "w") as f:
            f.write("subjectAltName=DNS:panel.example.com\n")
        openssl("x509", "-req", "-in", csr, "-CA", ca_cert, "-CAkey", ca_key, "-CAcreateserial", "-days", "20",
                "-out", leaf, "-extfile", ext)
        with open(leaf) as a, open(ca_cert) as b, open(os.path.join(self.lineage, "fullchain.pem"), "w") as f:
            f.write(a.read() + b.read())


class TempDir(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def certs(self):
        return Certs(self.d)

    def panel_tls(self, certs):
        """The panel's own cert.pem / key.pem: copies of the self-signed pair."""
        tls = os.path.join(self.d, "tls")
        os.makedirs(tls, exist_ok=True)
        cert, key = os.path.join(tls, "cert.pem"), os.path.join(tls, "key.pem")
        shutil.copyfile(certs.self_cert, cert)
        shutil.copyfile(certs.self_key, key)
        return cert, key


# --------------------------------------------------------------------------- bitpin/panel_cert.py
class TestNamesAndToken(TempDir):
    def test_domains(self):
        self.assertEqual(pc.clean_domain("  Panel.Example.COM. "), "panel.example.com")
        self.assertIsNone(pc.domain_problem("panel.example.com"))
        self.assertIsNone(pc.domain_problem("a-b.xn--mgbai9azgqp6j"))
        for bad in ("", "203.0.113.5", "2001:db8::5", "localhost", "-bad.example.com", "a..example.com",
                    "exa mple.com", "example.123", "x" * 64 + ".com", "*.example.com", "a_b.example.com"):
            self.assertIsNotNone(pc.domain_problem(pc.clean_domain(bad)), bad)

    def test_tokens(self):
        self.assertEqual(pc.clean_token("  Bearer %s \n" % TOKEN), TOKEN)
        self.assertIsNone(pc.token_problem(TOKEN))
        for bad in ("", "short", TOKEN[:10] + " " + TOKEN[10:], '"%s"' % TOKEN, TOKEN + "ـ"):
            self.assertIsNotNone(pc.token_problem(bad), bad)

    def test_the_credentials_file(self):
        path = os.path.join(self.d, "acme", "cloudflare.ini")
        self.assertFalse(pc.credentials_present(path))
        pc.write_credentials(path, TOKEN)
        self.assertTrue(pc.credentials_present(path))
        with open(path) as f:
            self.assertIn("dns_cloudflare_api_token = %s\n" % TOKEN, f.read())
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.startswith(".cloudflare.")], [])


class TestNetwork(unittest.TestCase):
    def test_prefer_ipv4_puts_ipv4_first_and_keeps_ipv6(self):
        v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 443, 0, 0))
        v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", 443))
        real = socket.getaddrinfo
        socket.getaddrinfo = lambda *a, **k: [v6, v4]
        try:
            pc.prefer_ipv4()
            pc.prefer_ipv4()                                   # once is enough: not wrapped twice
            self.assertEqual(socket.getaddrinfo("api.example", 443), [v4, v6])
            self.assertTrue(getattr(socket.getaddrinfo, "_ipv4_first", False))
        finally:
            socket.getaddrinfo = real

    def test_verify_token(self):
        seen = []

        class Resp(object):
            status = 200

            def __init__(self, doc):
                self.body = json.dumps(doc).encode()

            def read(self, n=-1):
                return self.body

        def opener_for(doc=None, error=None):
            def opener(req, timeout=None):
                seen.append((req.full_url, req.get_header("Authorization"), timeout))
                if error is not None:
                    raise error
                return Resp(doc)
            return opener

        ok = {"success": True, "result": {"id": "abc", "status": "active"}, "errors": []}
        self.assertEqual(pc.verify_token(TOKEN, opener_for(ok)), (True, ""))
        self.assertEqual(seen[-1], (pc.CF_VERIFY_URL, "Bearer " + TOKEN, 20))
        body = json.dumps({"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]}).encode()
        err = urllib.error.HTTPError(pc.CF_VERIFY_URL, 401, "Unauthorized", {}, io.BytesIO(body))
        self.assertEqual(pc.verify_token(TOKEN, opener_for(error=err)), (False, "Invalid API Token (1000)"))
        echo = {"success": False, "errors": [{"code": 9109, "message": "Cannot use the access token %s from location: "
                                                                      "198.51.100.7" % TOKEN}]}
        ok_, why = pc.verify_token(TOKEN, opener_for(echo))
        self.assertFalse(ok_)
        self.assertNotIn(TOKEN, why)                           # never echoed
        self.assertIn("198.51.100.7", why)
        self.assertEqual(pc.verify_token(TOKEN, opener_for({"success": True, "result": {"status": "disabled"}})),
                         (False, "the token is disabled"))
        ok_, why = pc.verify_token(TOKEN, opener_for(error=urllib.error.URLError("timed out")))
        self.assertEqual((ok_, why), (False, "Cloudflare could not be reached: timed out"))

    def test_resolve_addresses(self):
        def resolver(name, port):
            return [(socket.AF_INET6, 1, 6, "", ("2001:db8::5", 0, 0, 0)), (socket.AF_INET, 1, 6, "", ("203.0.113.5", 0)),
                    (socket.AF_INET, 2, 17, "", ("203.0.113.5", 0))]
        self.assertEqual(pc.resolve_addresses("panel.example.com", resolver), ["203.0.113.5", "2001:db8::5"])

        def fails(name, port):
            raise socket.gaierror(-2, "Name or service not known")
        self.assertEqual(pc.resolve_addresses("panel.example.com", fails), [])

    def test_certbot_stays_in_its_own_directories(self):
        a = pc.certbot_args("panel.example.com", "/usr/bin/python3 /opt/bitpin-bot/scripts/panel_setup.py cert-deploy")
        self.assertEqual(a[0], "certonly")
        for flag in ("--non-interactive", "--agree-tos", "--register-unsafely-without-email", "--keep-until-expiring"):
            self.assertIn(flag, a)
        pairs = dict(zip(a, a[1:]))
        self.assertEqual(pairs["--config-dir"], "/etc/bitpin-bot-panel/acme")
        self.assertEqual(pairs["--work-dir"], "/var/lib/bitpin-bot-panel-acme")
        self.assertEqual(pairs["--logs-dir"], "/var/log/bitpin-bot-panel-acme")
        self.assertEqual(pairs["--authenticator"], "dns-cloudflare")
        self.assertEqual(pairs["--dns-cloudflare-credentials"], "/etc/bitpin-bot-panel/acme/cloudflare.ini")
        self.assertEqual(pairs["--cert-name"], "bitpin-panel")
        self.assertEqual(pairs["--key-type"], "ecdsa")
        self.assertEqual(pairs["-d"], "panel.example.com")
        self.assertTrue(pairs["--deploy-hook"].endswith("panel_setup.py cert-deploy"))
        r = pc.renew_args()
        self.assertEqual(r[:2], ["renew", "--non-interactive"])
        self.assertEqual(dict(zip(r, r[1:]))["--config-dir"], "/etc/bitpin-bot-panel/acme")
        for args in (a, r):
            self.assertFalse(any("letsencrypt" in x for x in args), args)    # never the other sites' certbot
            self.assertNotIn("--email", args)


class TestCertificates(TempDir):
    def test_cert_info(self):
        c = self.certs()
        now = time.time()
        own = pc.cert_info(c.self_cert, now)
        self.assertTrue(own["self_signed"])
        self.assertEqual(own["names"], ["127.0.0.1"])
        self.assertIn(own["days_left"], (823, 824, 825))
        self.assertRegex(own["fingerprint"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        issued = pc.cert_info(os.path.join(c.lineage, "fullchain.pem"), now)     # the first one of the chain
        self.assertFalse(issued["self_signed"])
        self.assertEqual(issued["names"], ["panel.example.com"])
        self.assertEqual(issued["issuer"], "Test CA (T1)")
        self.assertIn(issued["days_left"], (19, 20))
        self.assertIn("error", pc.cert_info(os.path.join(self.d, "missing.pem")))
        self.assertIn("error", pc.cert_info(c.self_key))                        # a key holds no certificate

    def test_summary(self):
        t = time.time()
        base = {"names": ["panel.example.com"], "issuer": "Test CA (T1)", "self_signed": False, "not_after": t}
        self.assertTrue(pc.summary(dict(base, days_left=60), "on").startswith("Test CA (T1) for panel.example.com, "
                                                                              "valid until "))
        self.assertTrue(pc.summary(dict(base, days_left=60), "on").endswith("(60 days left), automatic renewal on"))
        self.assertTrue(pc.summary(dict(base, days_left=5)).startswith("WARN: "))
        self.assertTrue(pc.summary(dict(base, days_left=-1)).startswith("EXPIRED: "))
        self.assertTrue(pc.summary(dict(base, days_left=5, self_signed=True)).startswith("self-signed for "))
        self.assertTrue(pc.summary({"error": "cannot read x"}).startswith("unreadable (cannot read x)"))

    def test_install_keeps_the_self_signed_pair_once_and_restores_it(self):
        c = self.certs()
        cert, key = self.panel_tls(c)
        own_fp = pc.cert_info(cert)["fingerprint"]
        info = pc.install_cert(c.lineage, cert, key)
        self.assertEqual(info["names"], ["panel.example.com"])
        pc.check_pair(cert, key)
        self.assertEqual(pc.cert_info(pc.backup_path(cert))["fingerprint"], own_fp)
        self.assertTrue(os.path.isfile(pc.backup_path(key)))
        if os.name == "posix":
            for p in (cert, key):
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o640)
        pc.install_cert(c.lineage, cert, key)                       # a renewal: the kept pair stays the self-signed one
        self.assertEqual(pc.cert_info(pc.backup_path(cert))["fingerprint"], own_fp)
        self.assertTrue(pc.restore_selfsigned(cert, key))
        self.assertEqual(pc.cert_info(cert)["fingerprint"], own_fp)
        pc.check_pair(cert, key)
        self.assertEqual([n for n in os.listdir(os.path.dirname(cert)) if n.startswith(".panel-tls.")], [])

    def test_a_pair_that_does_not_match_changes_nothing(self):
        c = self.certs()
        cert, key = self.panel_tls(c)
        before = read(cert)
        shutil.copyfile(c.self_key, os.path.join(c.lineage, "privkey.pem"))       # the wrong key
        with self.assertRaises(ssl.SSLError):
            pc.install_cert(c.lineage, cert, key)
        self.assertEqual(read(cert), before)
        self.assertFalse(os.path.exists(pc.backup_path(cert)))
        self.assertFalse(pc.restore_selfsigned(cert, key))                         # nothing kept: nothing restored


# --------------------------------------------------------------------------- scripts/panel_setup.py
class Recorder(object):
    def __init__(self):
        self.calls = []

    def __call__(self, argv, check=True):
        self.calls.append(list(argv))
        return 0, ""


class TestPanelCertCommand(TempDir):
    def setUp(self):
        TempDir.setUp(self)
        self.etc = os.path.join(self.d, "etc")
        os.makedirs(self.etc)
        acme = os.path.join(self.etc, "acme")
        for name, value in (("ACME_CONFIG", acme), ("ACME_WORK", os.path.join(self.d, "work")),
                            ("ACME_LOGS", os.path.join(self.d, "logs")),
                            ("ACME_CREDENTIALS", os.path.join(acme, "cloudflare.ini"))):
            old = getattr(pc, name)
            setattr(pc, name, value)
            self.addCleanup(setattr, pc, name, old)
        for name, value in (("conf_path", lambda etc=None: os.path.join(self.etc, "panel.json")),
                            ("server_addresses", lambda run=None: ["203.0.113.5"])):
            old = getattr(ps, name)
            setattr(ps, name, value)
            self.addCleanup(setattr, ps, name, old)

    def write_conf(self, cert, key):
        ps.write_conf(ps.build_conf({"tls_cert": cert, "tls_key": key}, username="boss_42"),
                      os.path.join(self.etc, "panel.json"))

    def args(self, domain="panel.example.com", new_token=False, off=False):
        return type("A", (), {"domain": domain, "new_token": new_token, "off": off})()

    def run_cert(self, args, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ps.cmd_cert(args, **kw)
        return rc, out.getvalue()

    def test_the_whole_way(self):
        c = self.certs()
        cert, key = self.panel_tls(c)
        self.write_conf(cert, key)
        asked, certbot = [], []

        def fake_certbot(argv, env=None):
            certbot.append(argv)
            self.assertEqual(env["HOME"], pc.ACME_WORK)          # no ~/.cloudflare.cfg of another setup
            shutil.copytree(c.lineage, pc.live_dir())
            return 0
        runner = Recorder()
        rc, out = self.run_cert(self.args(), secret_ask=lambda q: asked.append(q) or "  " + TOKEN + "\n",
                                runner=runner, call=fake_certbot, verify=lambda t: (t == TOKEN, "wrong token"),
                                resolve=lambda n, p: [(socket.AF_INET, 1, 6, "", ("203.0.113.5", 0))],
                                plugin=lambda: True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(asked), 1)
        self.assertNotIn(TOKEN, out)                                         # never printed
        self.assertTrue(pc.credentials_present(pc.ACME_CREDENTIALS))
        self.assertEqual(certbot[0][:3], ps.certbot_cmd())
        self.assertEqual(certbot[0][3:], pc.certbot_args("panel.example.com", ps.deploy_hook_cmd()))
        self.assertEqual(pc.cert_info(cert)["names"], ["panel.example.com"])
        self.assertIn(["systemctl", "reload", "bitpin-bot-panel.service"], runner.calls)
        self.assertIn(["systemctl", "enable", "--now", "bitpin-bot-panel-cert.timer"], runner.calls)
        self.assertIn("installed for the panel (reloaded): Test CA (T1) for panel.example.com", out)
        self.assertIn("https://panel.example.com:8443/", out)
        self.assertNotIn("WARNING", out)
        # a second run keeps the token (no question) and the self-signed pair kept the first time
        own_fp = pc.cert_info(c.self_cert)["fingerprint"]
        shutil.rmtree(pc.live_dir())
        rc, out = self.run_cert(self.args(), secret_ask=lambda q: self.fail("asked again"), runner=Recorder(),
                                call=fake_certbot, resolve=lambda n, p: [], plugin=lambda: True)
        self.assertIn("using the Cloudflare token in", out)
        self.assertIn("WARNING: panel.example.com has no address yet", out)
        self.assertEqual(pc.cert_info(pc.backup_path(cert))["fingerprint"], own_fp)
        # --off: the self-signed pair is back and the timer stops
        runner = Recorder()
        rc, out = self.run_cert(self.args(domain=None, off=True), runner=runner)
        self.assertEqual(pc.cert_info(cert)["fingerprint"], own_fp)
        self.assertIn(["systemctl", "disable", "--now", "bitpin-bot-panel-cert.timer"], runner.calls)
        self.assertIn("back on its self-signed certificate", out)

    def test_refusals_change_nothing(self):
        c = self.certs()
        cert, key = self.panel_tls(c)
        self.write_conf(cert, key)
        before = read(cert)

        def no_certbot(argv, env=None):
            raise AssertionError("certbot must not run")
        with self.assertRaises(ps.SetupError):
            self.run_cert(self.args(domain="203.0.113.5"), call=no_certbot, plugin=lambda: True)
        with self.assertRaises(ps.SetupError) as cm:
            self.run_cert(self.args(), call=no_certbot, plugin=lambda: False)
        self.assertIn("python3-certbot-dns-cloudflare", str(cm.exception))
        with self.assertRaises(ps.SetupError) as cm:
            self.run_cert(self.args(), secret_ask=lambda q: TOKEN, call=no_certbot, plugin=lambda: True,
                          verify=lambda t: (False, "Invalid API Token (1000)"), resolve=lambda n, p: [])
        self.assertIn("Cloudflare does not accept this token: Invalid API Token (1000)", str(cm.exception))
        self.assertFalse(pc.credentials_present(pc.ACME_CREDENTIALS))
        with self.assertRaises(ps.SetupError):
            self.run_cert(self.args(), secret_ask=lambda q: "two words", call=no_certbot, plugin=lambda: True,
                          resolve=lambda n, p: [])
        with self.assertRaises(ps.SetupError) as cm:                 # certbot fails: the panel keeps its certificate
            self.run_cert(self.args(), secret_ask=lambda q: TOKEN, call=lambda argv, env=None: 1, plugin=lambda: True,
                          verify=lambda t: (True, ""), resolve=lambda n, p: [], runner=Recorder())
        self.assertIn("certbot failed (exit 1)", str(cm.exception))
        self.assertEqual(read(cert), before)

    def test_a_proxied_name_is_warned_about(self):
        c = self.certs()
        cert, key = self.panel_tls(c)
        self.write_conf(cert, key)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(ps.SetupError):
            ps.cmd_cert(self.args(), secret_ask=lambda q: TOKEN, call=lambda argv, env=None: 1, plugin=lambda: True,
                        verify=lambda t: (True, ""), runner=Recorder(),
                        resolve=lambda n, p: [(socket.AF_INET, 1, 6, "", ("198.51.100.20", 0))])
        self.assertIn("WARNING: panel.example.com points to 198.51.100.20, not to this server (203.0.113.5)",
                      out.getvalue())
        self.assertIn("'DNS only'", out.getvalue())

    def test_the_deploy_hook_takes_only_its_own_lineage(self):
        c = self.certs()
        cert, key = self.panel_tls(c)
        self.write_conf(cert, key)
        with self.assertRaises(ps.SetupError):
            ps.cmd_cert_deploy(None, env={"RENEWED_LINEAGE": c.lineage}, runner=Recorder())
        shutil.copytree(c.lineage, pc.live_dir())
        runner = Recorder()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ps.cmd_cert_deploy(None, env={"RENEWED_LINEAGE": pc.live_dir()}, runner=runner), 0)
        self.assertEqual(pc.cert_info(cert)["names"], ["panel.example.com"])
        self.assertEqual(runner.calls, [["systemctl", "reload", "bitpin-bot-panel.service"]])
        self.assertIn("renewed and installed (reloaded)", out.getvalue())

    def test_an_old_panel_unit_is_restarted_instead(self):
        calls = []

        def runner(argv, check=True):
            calls.append(argv)
            return (1, "Job type reload is not applicable") if argv[1] == "reload" else (0, "")
        self.assertEqual(ps.reload_panel(runner), "restarted")
        self.assertEqual(calls[-1], ["systemctl", "try-restart", "bitpin-bot-panel.service"])

    def test_renew_runs_only_with_a_certificate_of_its_own(self):
        seen = []
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ps.cmd_cert_renew(None, call=lambda argv, env=None: seen.append(argv) or 0), 0)
        self.assertEqual(seen, [])
        os.makedirs(os.path.dirname(pc.renewal_conf()))
        open(pc.renewal_conf(), "w").close()
        self.assertEqual(ps.cmd_cert_renew(None, call=lambda argv, env=None: seen.append(argv) or 3), 3)
        self.assertEqual(seen, [ps.certbot_cmd() + pc.renew_args()])

    def test_the_command_line(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ps.main(["cert"]), 2)                      # which domain?
        self.assertEqual(ps.certbot_cmd()[1:], [os.path.abspath(ps.__file__), "certbot"])
        hook = ps.deploy_hook_cmd()                                 # a shell command line (quoted on Windows)
        self.assertTrue(hook.endswith(" cert-deploy") and "panel_setup.py" in hook, hook)


class TestDeployKit(unittest.TestCase):
    def read(self, *p):
        with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
            return f.read()

    def test_the_renewal_units(self):
        svc = self.read("deploy", "bitpin-bot-panel-cert.service")
        for s in ("Type=oneshot", "ExecStart=/usr/bin/python3 /opt/bitpin-bot/scripts/panel_setup.py cert-renew",
                  "OnFailure=bitpin-bot-failed@%n.service", "ProtectSystem=strict", "NoNewPrivileges=yes",
                  "ReadWritePaths=/etc/bitpin-bot-panel -/var/lib/bitpin-bot-panel-acme -/var/log/bitpin-bot-panel-acme",
                  "ConditionPathExists=/etc/bitpin-bot-panel/acme/renewal/bitpin-panel.conf", "-/etc/letsencrypt",
                  "-/etc/bitpin-bot "):
            self.assertIn(s, svc)
        timer = self.read("deploy", "bitpin-bot-panel-cert.timer")
        for s in ("OnCalendar=*-*-* 02,14:23:00 UTC", "Persistent=true", "Unit=bitpin-bot-panel-cert.service"):
            self.assertIn(s, timer)
        web = self.read("deploy", "bitpin-bot-panel.service")
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", web)
        for f in ("install.sh", "update.sh", "lib.sh"):
            self.assertNotIn("bitpin-bot-panel-cert.timer", self.read("deploy", f).replace(
                'bitpin-bot-panel-cert.service bitpin-bot-panel-cert.timer"', ""), f)     # listed, never enabled
        self.assertIn("$PANEL_ACME_DIRS", self.read("deploy", "uninstall.sh"))

    def test_health_shows_the_certificate(self):
        cli = self.read("deploy", "bitpin-bot")
        self.assertIn('scripts/panel_setup.py" cert-info', cli)
        self.assertIn("sudo bitpin-bot panel-cert DOMAIN [--new-token]", cli)


# --------------------------------------------------------------------------- the panel: reload, helper, page
class TestReload(TempDir):
    def test_sighup_swaps_the_context_and_keeps_the_old_one_on_a_bad_pair(self):
        c = self.certs()
        cert, key = self.panel_tls(c)
        server = type("S", (), {"ssl_context": "old"})()
        cfg = {"tls_cert": cert, "tls_key": key}
        with self.assertLogs("bitpin.panel", "INFO"):
            self.assertTrue(srv.reload_certificate(server, cfg))
        self.assertIsInstance(server.ssl_context, ssl.SSLContext)
        good = server.ssl_context
        with self.assertLogs("bitpin.panel", "ERROR"):
            self.assertFalse(srv.reload_certificate(server, {"tls_cert": cert, "tls_key": os.path.join(self.d, "no")}))
        self.assertIs(server.ssl_context, good)


class TestHelperCommand(TempDir):
    def test_panel_cert(self):
        ph = load_module("panel_helper_cert_under_test", os.path.join("scripts", "panel_helper.py"))
        c = self.certs()
        cert, key = self.panel_tls(c)
        etc, acme = os.path.join(self.d, "panel-etc"), os.path.join(self.d, "panel-etc", "acme")
        os.makedirs(etc)
        with open(os.path.join(etc, "panel.json"), "w") as f:
            json.dump({"tls_cert": cert, "tls_key": key, "password_hash": "x", "totp_secret": "Y"}, f)
        shows = {"bitpin-bot-panel-cert.timer": "ActiveState=active\nSubState=waiting\nUnitFileState=enabled\n"
                                                "LoadState=loaded\nNextElapseUSecRealtime=Sat 2026-10-03 02:41:00 UTC\n",
                 "bitpin-bot-panel-cert.service": "Result=success\nExecMainStatus=0\n"
                                                  "ExecMainExitTimestamp=Fri 2026-10-02 14:30:05 UTC\n"}

        def run(argv, timeout=None, **kw):
            return 0, shows.get(argv[2], "") if argv[:2] == ["systemctl", "show"] else ""
        h = ph.Helper(paths=ph.Paths(panel_etc_dir=etc, acme_dir=acme), run=run)
        r = h.handle({"cmd": "panel_cert", "args": {}})
        self.assertTrue(r["ok"], r)
        d = r["data"]
        self.assertTrue(d["cert"]["self_signed"])
        self.assertFalse(d["managed"])
        self.assertIsNone(d["timer"])
        self.assertNotIn("password_hash", json.dumps(d))
        os.makedirs(os.path.join(acme, "renewal"))
        open(os.path.join(acme, "renewal", "bitpin-panel.conf"), "w").close()
        d = h.handle({"cmd": "panel_cert", "args": {}})["data"]
        self.assertTrue(d["managed"])
        self.assertEqual((d["timer"]["state"], d["timer"]["enabled"]), ("active", "enabled"))
        self.assertEqual(d["next"], "Sat 2026-10-03 02:41:00 UTC")
        self.assertEqual(d["last_run"], {"result": "success", "status": "0", "at": "Fri 2026-10-02 14:30:05 UTC"})


import test_panel_web as tw  # noqa: E402  (its PanelCase: the app with the fake helper)


class TestSecurityPage(tw.PanelCase):
    def page(self, cert_data=None, error=None):
        if error is not None:
            self.helper.fail["panel_cert"] = error
        elif cert_data is not None:
            self.helper.responses["panel_cert"] = cert_data
        c = tw.Client(self.app, lang="en")
        self.login(c)
        r = c.get("/security")
        self.assertEqual(r.status, 200)
        return r.text

    def test_a_trusted_certificate(self):
        t = self.page()
        for s in ("panel.example.com", "Test CA (T1)", "60 days left", "next check", "Sat 2026-10-03 02:41:00 UTC",
                  'class="fp">AB:CD:EF', "It renews itself from 30 days before its end", "Done"):
            self.assertIn(s, t)
        self.assertNotIn("box err", t)

    def test_self_signed_expiring_and_expired(self):
        own = {"cert": {"names": ["203.0.113.5"], "issuer": "203.0.113.5", "self_signed": True,
                        "not_after": 1790000000.0, "days_left": 700, "fingerprint": "11:22"},
               "managed": False, "timer": None, "next": None, "last_run": None}
        t = self.page(own)
        self.assertIn("Self-signed", t)
        self.assertIn("<code>sudo bitpin-bot panel-cert DOMAIN</code>", t)
        self.assertNotIn("Automatic renewal", t)
        soon = tw.default_responses()["panel_cert"]
        soon["cert"]["days_left"] = 5
        soon["last_run"] = {"result": "exit-code", "status": "1", "at": "Fri 2026-10-02 14:30:05 UTC"}
        t = self.page(soon)
        self.assertIn("The certificate ends soon and was not renewed", t)
        self.assertIn("Failed (exit 1)", t)
        soon["cert"]["days_left"] = -2
        t = self.page(soon)
        self.assertIn("The certificate has expired", t)
        self.assertIn(">Expired<", t)

    def test_a_helper_error_is_shown_in_the_card(self):
        t = self.page(error="the helper is down")
        self.assertIn("The panel&#x27;s certificate", t)
        self.assertIn("the helper is down", t)


if __name__ == "__main__":
    unittest.main()
