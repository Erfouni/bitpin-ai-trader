"""v3.1 end to end: the real web panel (bitpin/panel_web.PanelApp + HelperClient) talks to the real root
helper (scripts/panel_helper.py serve) over a real UNIX socket - login, dashboard, the trade settings form
(preview, then save), a write-only key and the JSON editor refusing a foreign Bitpin URL. Only systemctl /
the bitpin-bot command line are fakes (FakeRun); settings are validated by the bot's own checks. Linux only
(AF_UNIX); the server runs it in update.sh's staged test run."""
import importlib.util
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bitpin.panel_auth import hash_password  # noqa: E402
from bitpin.panel_web import HelperClient, PanelApp  # noqa: E402

HAVE_UNIX = hasattr(socket, "AF_UNIX") and os.name == "posix"
KEY = "sk-e2e-secret-key-0123456789abcdef"
PW = "Correct-Horse-9-Battery"


def load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FormFields(HTMLParser):
    """The fields a browser posts from a page: text / hidden inputs, CHECKED checkboxes, the selected option of
    each select, textareas (one leading newline dropped, as a browser does)."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.fields, self._select, self._textarea, self._text = {}, None, None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("name"):
            kind = a.get("type", "text")
            if kind in ("text", "hidden", "password") or (kind == "checkbox" and "checked" in a):
                self.fields[a["name"]] = a.get("value", "")
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select and "selected" in a:
            self.fields[self._select] = a.get("value", "")
        elif tag == "textarea":
            self._textarea, self._text = a.get("name"), []

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "textarea" and self._textarea:
            text = "".join(self._text)
            self.fields[self._textarea] = text[1:] if text.startswith("\n") else text
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self._text.append(data)


def form_fields(page):
    p = FormFields()
    p.feed(page)
    return p.fields


class Runner(object):
    """systemctl and the bitpin-bot command line."""

    def __init__(self, cli):
        self.cli = cli
        self.calls = []

    def __call__(self, argv, timeout, env=None, cwd=None):
        self.calls.append(list(argv))
        if argv[:2] == ["systemctl", "show"]:
            return 0, "ActiveState=active\nSubState=running\nUnitFileState=enabled\nLoadState=loaded\n"
        if argv[0] == self.cli and argv[1:3] == ["confirm-live", "--check"]:
            return 1, "NOT CONFIRMED: the settings differ"
        if argv[0] == self.cli:
            return 0, "RESULT: OK"
        return 0, ""


@unittest.skipUnless(HAVE_UNIX, "needs AF_UNIX (Linux): the server runs it")
class TestPanelEndToEnd(unittest.TestCase):
    def setUp(self):
        ph = load_module("panel_helper_e2e", os.path.join("scripts", "panel_helper.py"))
        tw = load_module("test_panel_web_e2e", os.path.join("tests", "test_panel_web.py"))
        self.t = tempfile.mkdtemp(prefix="panel_e2e_")
        self.addCleanup(shutil.rmtree, self.t, True)
        d = lambda *p: os.path.join(self.t, *p)  # noqa: E731
        for sub in ("etc", "panel-etc", "state", "notify", "backups", "xray"):
            os.makedirs(d(sub))
        shutil.copy(os.path.join(ROOT, "config.example.json"), d("etc", "config.json"))
        shutil.copy(os.path.join(ROOT, "kimi.example.json"), d("etc", "kimi.json"))
        with open(d("etc", "bitpin-bot.env"), "w", encoding="utf-8") as f:
            f.write("KIMI_API_KEY=%s\n" % KEY)
        self.runner = Runner(d("bitpin-bot"))
        paths = ph.Paths(app_dir=ROOT, etc_dir=d("etc"), panel_etc_dir=d("panel-etc"), state_dir=d("state"),
                         notify_state_dir=d("notify"), backup_dir=d("backups"), xray_conf=d("xray", "config.json"),
                         cli=self.runner.cli, lock_file=d("helper.lock"))
        paths.state_in_process = True
        self.helper = ph.Helper(paths, run=self.runner, sleep=lambda s: None)
        # the socket systemd would own; one thread per connection like Accept=yes
        self.sock_path = d("helper.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(8)
        self.addCleanup(srv.close)

        def serve_forever():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    ph.serve(conn.makefile("rb"), conn.makefile("wb"), self.helper)
        threading.Thread(target=serve_forever, daemon=True).start()
        cfg = {"bind": "127.0.0.1", "port": 8443, "username": "boss_42",
               "password_hash": hash_password(PW, iterations=1000), "totp_secret": None, "allowed_hosts": [],
               "session_idle_minutes": 30, "session_max_hours": 12, "helper_socket": self.sock_path,
               "audit_log": d("audit.jsonl"), "trusted_proxy": False}
        self.app = PanelApp(cfg, HelperClient(self.sock_path), sleep=lambda s: None, spawn=lambda fn: fn())
        self.app.hash_iterations = 1000
        self.c = tw.Client(self.app)
        r = self.c.login(user="boss_42", password=PW)
        self.assertEqual(r.status, 303, r.text[:400])
        self.d = d

    def read(self, *p):
        with open(self.d(*p), "r", encoding="utf-8") as f:
            return f.read()

    def test_dashboard_and_trade_settings_round_trip(self):
        r = self.c.get("/")
        self.assertEqual(r.status, 200)
        import bitpin
        self.assertIn(bitpin.__version__, r.text)                          # the running version
        self.assertNotIn(KEY, r.text)
        r = self.c.get("/trade")
        self.assertEqual(r.status, 200)
        form = form_fields(r.text)                                   # what a browser would post
        self.assertEqual(form.get("config:risk.max_drawdown"), "50")
        before = self.read("etc", "config.json")
        form["config:risk.max_drawdown"] = "45"
        r = self.c.post("/trade", dict(form, action="preview"))
        self.assertEqual(r.status, 200, r.text[:600])
        self.assertIn("risk.max_drawdown", r.text)
        self.assertEqual(self.read("etc", "config.json"), before)            # a preview writes nothing
        r = self.c.post("/trade", dict(form, action="save"))
        self.assertIn(r.status, (200, 303), r.text[:600])
        self.assertEqual(json.loads(self.read("etc", "config.json"))["risk"]["max_drawdown"], 0.45)
        self.assertTrue([f for f in os.listdir(self.d("backups")) if f.startswith("config.json.")])
        events = sorted(json.loads(self.read("notify", f))["event"] + ":" + json.loads(self.read("notify", f))["what"]
                        for f in os.listdir(self.d("notify")) if f.startswith("panel_event."))
        self.assertEqual(events, ["change:trade_settings", "login_ok:"])       # both reach Telegram

    def test_a_key_is_write_only_and_the_bitpin_url_stays(self):
        r = self.c.post("/models/key", {"name": "OPENROUTER_API_KEY", "value": "sk-or-v1-e2e-0123456789"})
        self.assertIn(r.status, (200, 303))
        self.assertIn("OPENROUTER_API_KEY=sk-or-v1-e2e-0123456789\n", self.read("etc", "bitpin-bot.env"))
        for page in ("/", "/models", "/security", "/settings"):
            text = self.c.get(page).text
            self.assertNotIn("sk-or-v1-e2e-0123456789", text, page)
            self.assertNotIn(KEY, text, page)
        doc = json.loads(self.read("etc", "config.json"))
        doc["base_url"] = "https://collector.example.net"
        r = self.c.post("/settings", {"file": "config", "text": json.dumps(doc, indent=2), "do": "save"})
        if r.status == 303:
            r = self.c.get(r.header("Location") or "/settings")
        self.assertNotEqual(json.loads(self.read("etc", "config.json"))["base_url"], "https://collector.example.net")
        self.assertIn("api.bitpin.org", r.text)
        audit = self.read("audit.jsonl")
        self.assertNotIn("sk-or-v1-e2e-0123456789", audit)


if __name__ == "__main__":
    unittest.main()
