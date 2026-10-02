# -*- coding: utf-8 -*-
"""bitpin.panel_web (the management panel) and scripts/panel_server.py.

The pure request handler runs against a fake root helper: login (with and without 2FA), the limiter, sessions,
CSRF / Origin / Host checks, every page, hostile strings, the audit log, secrets that must never be shown or
logged, the trade settings form; the helper client runs over a fake socket. No network: one test starts the real
HTTPS server on 127.0.0.1, and only when an openssl binary can make a throwaway certificate."""
import copy
import csv
import http.client
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bitpin import panel_settings as ps  # noqa: E402
from bitpin import panel_web as pw  # noqa: E402
from bitpin import performance as perf  # noqa: E402
import test_performance as tp  # noqa: E402  (the synthetic world of the P&L report)
from bitpin.panel_auth import hash_password, totp, verify_password  # noqa: E402
from bitpin.panel_web import HelperClient, HelperError, PanelApp  # noqa: E402

USER = "owner"
PW = "Harbor-Light-4271!"
NEW_PW = "Tq7!mZp2#Lw9-River"
T0 = 1790074800.0
HOST = "panel.example.org:8443"
IP = "203.0.113.7"
HOSTILE = '<script>alert("x")</script>'
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
KEY_VALUE = "sk-TOPSECRET-0123456789abcdef"
VPN_LINK = "vless://11111111-2222-3333-4444-555555555555@vpn.example.net:443?security=tls#home"
RAW_VPN = '{"inbounds": [], "outbounds": [{"protocol": "vless", "settings": {"id": "RAWSECRET-UUID-9"}}]}'
CSP = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
REQUIRED_HEADERS = {"Strict-Transport-Security": "max-age=31536000", "Content-Security-Policy": CSP,
                    "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                    "Cache-Control": "no-store"}
PAGES = ("/", "/models", "/settings", "/trade", "/apply", "/vpn", "/logs", "/health", "/security", "/performance",
         "/history")
BANNER = u"تنظیمات ذخیره‌شده هنوز اعمال نشده"


logging.getLogger("bitpin.panel").addHandler(logging.NullHandler())   # the expected internal-error log stays quiet


def read_text(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


CONFIG_TEXT = read_text("config.example.json")
KIMI_TEXT = read_text("kimi.example.json")


def load_module(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ----------------------------------------------------------------------------------------------- the fake helper

def status_data():
    return {
        "bot": {"state": "active", "sub": "running", "enabled": "enabled", "since": "Sun 2026-09-27 19:30:00 UTC",
                "version": "3.1.0"},
        "notifier": {"state": "active", "sub": "running", "enabled": "enabled", "since": None},
        "tunnel": {"state": "failed", "sub": "failed", "enabled": "enabled", "since": None},
        "panel": {"state": "active", "sub": "running", "enabled": "enabled", "since": None},
        "live_confirmed": {"ok": True, "why": "LIVE_CONFIRMED matches the settings"},
        "equity": {"irt": 3990000.0, "time": T0 - 300, "hwm_irt": 4100000.0, "drawdown_pct": 2.68, "halt_pct": 50.0,
                   "halted": False, "start_irt": 3800000.0},
        "last_decision": {"time": T0 - 600, "mode": "slot", "hold": False, "valid": True, "fallback": False,
                          "confidence": 0.62, "model": "kimi-k3", "targets": {"BTC_IRT": 0.4, "USDT_IRT": 0.6, "DOGE_IRT": 0.0},
                          "report_fa": u"خرید بیت‌کوین با وزن ۴۰٪", "error": None, "error_kind": None},
        "positions": [{"symbol": "BTC_USDT", "kind": "allocation", "amount": 0.0012, "entry_px_usdt": 65000.5,
                       "stop_px_usdt": 60000.0, "target_px_usdt": 72000.0, "max_hold_until": T0 + 86400}],
        "resting_orders": 2, "spend": {"today_usd": 0.171, "month_usd": 3.2, "total_usd": 5.5},
        "status_fa": u"وضعیت ربات: فعال", "last_decision_fa": u"آخرین تصمیم: خرید", "state_error": None}


VPN_SUMMARY = {"inbounds": [{"tag": "http-in", "protocol": "http", "listen": "127.0.0.1", "port": 1081, "local_only": True}],
               "outbounds": [{"tag": "proxy", "protocol": "vless"}, {"tag": "direct", "protocol": "freedom"}],
               "proxy": {"tag": "proxy", "protocol": "vless", "address": "vpn.example.net", "port": 443,
                         "id": "1111...5555", "security": "tls", "sni": "", "flow": ""},
               "proxy_index": 0, "local_proxies": ["http://127.0.0.1:1081"]}


def put_result(a, **extra):
    dry = bool(a.get("dry_run"))
    r = {"problems": [], "warnings": ["a warning"], "written": not dry, "confirm_needed": not dry,
         "diff": "--- a/kimi.json\n+++ b/kimi.json\n@@ -1 +1 @@\n-\"model\": \"kimi-k3\"\n+\"model\": \"kimi-k4\"\n",
         "backup": None if dry else "/var/backups/bitpin-bot/kimi.json.20260928.before-panel"}
    r.update(extra)
    return r


def vpn_put(a):
    dry = bool(a.get("dry_run"))
    return {"summary": VPN_SUMMARY, "xray_test": {"ok": True, "output": "Configuration OK."}, "written": not dry,
            "backup": None if dry else "/var/backups/bitpin-bot/xray-config.json.1.before-panel", "tunnel_state": "active",
            "proxy_test": [] if dry else [{"target": "api.moonshot.ai:443", "ok": True, "ms": 120, "error": ""}],
            "rolled_back": False, "diff": "--- a\n+++ b\n-\"id\": \"****\"\n+\"id\": \"1111...5555\"\n"}


def perf_report(a):
    """bitpin/performance.py's report of the synthetic world of tests/test_performance.py, at the panel's T0."""
    return perf.report(tp.world_records(21), tp.START, tp.world_prices(T0), a["from"], a["to"], T0,
                       perf.resting_orders(tp.ORDERS_DOC), tp.world_analyses())


def default_responses():
    return {
        "status": lambda a: status_data(),
        "performance": perf_report,
        "config_get": {"config": CONFIG_TEXT, "kimi": KIMI_TEXT},
        "secrets_status": dict((n, {"set": n in ("KIMI_API_KEY", "BITPIN_API_KEY", "BITPIN_SECRET_KEY"),
                                    "length": 32 if n in ("KIMI_API_KEY", "BITPIN_API_KEY", "BITPIN_SECRET_KEY") else 0})
                               for n in pw.SECRET_NAMES),
        "health": {"ok": True, "text": "bot healthy"},
        "check": {"ok": True, "text": "check passed: 0 problems"},
        "logs": lambda a: {"text": "log of %s (%d lines)" % (a["unit"], a["lines"])},
        "confirm_show": {"ok": True, "text": "confirm LIVE trading with settings digest abc123"},
        "models": lambda a: {"models": [
            {"id": "kimi-k3", "name": "Kimi K3", "context_length": 262144, "price_in_per_m": 3.0, "price_out_per_m": 15.0,
             "price_cached_in_per_m": 0.3, "supports_json": True, "supports_reasoning": True},
            {"id": "moonshotai/kimi-k2.6", "name": "Kimi K2.6", "context_length": 131072, "price_in_per_m": 0.95,
             "price_out_per_m": 4.0, "price_cached_in_per_m": None, "supports_json": True, "supports_reasoning": False}],
            "provider": a.get("provider") if a.get("provider") != "auto" else "openai",
            "key_name": {"moonshot": "KIMI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(a.get("provider"), "LLM_API_KEY")},
        "model_set": lambda a: put_result(a, changed=[{"file": "kimi", "path": "llm.model", "old": "kimi-k3",
                                                        "new": a.get("model")}]),
        "config_put": lambda a: put_result(a),
        "settings_set": lambda a: put_result(a, changed=[{"file": "config", "path": "risk.max_drawdown", "old": 0.5,
                                                           "new": 0.3}]),
        "secret_set": lambda a: {"name": a["name"], "set": bool(a["value"]), "length": len(a["value"]),
                                 "restart_needed": True},
        "apply_live": lambda a: {"ok": True, "steps": [{"step": "check", "ok": True, "output": "ok"},
                                                       {"step": "stop", "ok": True, "output": "bitpin-bot stopped"},
                                                       {"step": "confirm", "ok": True, "output": "LIVE_CONFIRMED written"},
                                                       {"step": "start", "ok": True, "output": "bitpin-bot started"}],
                                 "health": "bot healthy after apply", "bot_state": "active"},
        "service": lambda a: {"ok": True, "output": "%s %s: done" % (a["unit"], a["action"]), "state": "active"},
        "vpn_get": lambda a: {"unit_state": "active", "config_path": "/opt/xray/config.json", "summary": VPN_SUMMARY,
                              "raw": RAW_VPN if a.get("raw") else None, "error": None},
        "vpn_test": {"proxy": "http://127.0.0.1:1081", "unit_state": "active",
                     "required": ["api.moonshot.ai", "api.telegram.org"],
                     "results": [{"target": "api.moonshot.ai:443", "ok": True, "ms": 210, "error": ""},
                                 {"target": "api.telegram.org:443", "ok": False, "ms": 12000, "error": "timed out"}]},
        "vpn_put": vpn_put,
        "panel_password_set": {},
        "panel_totp_set": {},
        "panel_cert": {"cert": {"names": ["panel.example.com"], "issuer": "Test CA (T1)", "self_signed": False,
                                "not_after": 1790000000.0, "days_left": 60, "fingerprint": "AB:CD:EF"},
                       "managed": True, "timer": {"state": "active", "enabled": "enabled", "sub": "waiting", "since": None},
                       "next": "Sat 2026-10-03 02:41:00 UTC",
                       "last_run": {"result": "success", "status": "0", "at": "Fri 2026-10-02 14:30:05 UTC"}},
        "audit_notify": {"relayed": True},
    }


class FakeHelper(object):
    """Stands in for scripts/panel_helper.py; panel_*_set also rewrite panel.json like the real helper."""

    def __init__(self, panel_conf=None):
        self.calls = []
        self.fail = {}                  # cmd -> error text (HelperError, as the real client raises)
        self.explode = {}               # cmd -> any exception (a broken client)
        self.panel_conf = panel_conf
        self.responses = default_responses()

    def call(self, cmd, actor=None, timeout=None, **args):
        self.calls.append((cmd, copy.deepcopy(args), copy.deepcopy(actor)))
        if cmd in self.explode:
            raise self.explode[cmd]
        if cmd in self.fail:
            raise HelperError(self.fail[cmd])
        if cmd in ("panel_password_set", "panel_totp_set") and self.panel_conf:
            with open(self.panel_conf, encoding="utf-8") as f:
                doc = json.load(f)
            if cmd == "panel_password_set":
                doc["password_hash"] = args["password_hash"]
            else:
                doc["totp_secret"] = args["totp_secret"]
            with open(self.panel_conf, "w", encoding="utf-8") as f:
                json.dump(doc, f)
        r = self.responses[cmd]
        return copy.deepcopy(r(args) if callable(r) else r)

    def cmds(self):
        return [c[0] for c in self.calls]

    def calls_of(self, cmd):
        return [c for c in self.calls if c[0] == cmd]


# ----------------------------------------------------------------------------------------------- a tiny browser

class Resp(object):
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def text(self):
        return self.body.decode("utf-8")

    def header(self, name):
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    def headers_all(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower()]


class Client(object):
    """Keeps cookies like a browser, sends Host / Origin / Content-Type, remembers the CSRF token."""

    def __init__(self, app, ip=IP, host=HOST, lang="fa"):
        self.app, self.ip, self.host = app, ip, host
        self.cookies = {"__Host-bplang": lang} if lang else {}     # most checks read the Persian texts
        self.token = None

    def request(self, method, path, form=None, query="", headers=None, origin=True, body=None,
                content_type="application/x-www-form-urlencoded"):
        h = [("Host", self.host)]
        if self.cookies:
            h.append(("Cookie", "; ".join("%s=%s" % kv for kv in sorted(self.cookies.items()))))
        if method == "POST":
            if content_type:
                h.append(("Content-Type", content_type))
            if origin:
                h.append(("Origin", "https://" + self.host if origin is True else origin))
            if body is None:
                body = urlencode(form or {}).encode("utf-8")
        for k, v in (headers or {}).items():
            h.append((k, v))
        st, hdrs, out = self.app.handle(method, path, query, h, body or b"", self.ip)
        for k, v in hdrs:
            if k.lower() == "set-cookie":
                name, _, rest = v.partition("=")
                if "Max-Age=0" in v:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = rest.split(";", 1)[0]
        return Resp(st, hdrs, out)

    def get(self, path, query=""):
        return self.request("GET", path, query=query)

    def post(self, path, form=None, csrf=True, **kw):
        form = dict(form or {})
        if csrf is True:
            form["csrf"] = self.token
        elif csrf:
            form["csrf"] = csrf
        return self.request("POST", path, form, **kw)

    def refresh_token(self):
        r = self.get("/logs")                                 # no helper call: just the page with the forms
        m = re.search(r'name="csrf" value="([^"]+)"', r.text)
        self.token = m.group(1) if m else None
        return self.token

    def login(self, user=USER, password=PW, code=None):
        r = self.get("/login")
        m = re.search(r'name="lt" value="([^"]+)"', r.text)
        form = {"lt": m.group(1) if m else "", "username": user, "password": password}
        if code is not None:
            form["code"] = code
        r = self.request("POST", "/login", form)
        if r.status == 303:
            self.refresh_token()
        return r


class PanelCase(unittest.TestCase):
    totp_secret = None
    allowed_hosts = None

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="panel_web_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = [T0]
        self.sleeps = []
        self.cfg_path = os.path.join(self.dir, "panel.json")
        self.cfg = {"bind": "127.0.0.1", "port": 8443, "username": USER,
                    "password_hash": hash_password(PW, iterations=1000), "totp_secret": self.totp_secret,
                    "allowed_hosts": list(self.allowed_hosts or []), "session_idle_minutes": 30, "session_max_hours": 12,
                    "helper_socket": os.path.join(self.dir, "helper.sock"),
                    "audit_log": os.path.join(self.dir, "audit.jsonl"), "trusted_proxy": False}
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f)
        self.helper = FakeHelper(self.cfg_path)
        self.app = self.make_app()
        self.c = Client(self.app)

    def make_app(self, cfg=None, **kw):
        kw.setdefault("spawn", lambda fn: fn())
        app = PanelApp(cfg or self.cfg, self.helper, clock=lambda: self.now[0], sleep=self.sleeps.append,
                       config_path=self.cfg_path, **kw)
        app.hash_iterations = 1000
        return app

    def audit_text(self):
        try:
            with open(self.cfg["audit_log"], encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def audit(self, event=None):
        recs = [json.loads(ln) for ln in self.audit_text().splitlines() if ln.strip()]
        return [r for r in recs if event is None or r.get("event") == event]

    def login(self, client=None, **kw):
        c = client or self.c
        r = c.login(**kw)
        self.assertEqual(r.status, 303, r.text[:500])
        return c

    def assertEscaped(self, text, where=""):
        self.assertNotIn(HOSTILE, text, where)
        self.assertNotIn("<script", text, where)
        self.assertIn("&lt;script&gt;", text, where)


# ----------------------------------------------------------------------------------------------- headers, host

class TestHeadersAndHost(PanelCase):
    def test_security_headers_on_every_kind_of_response(self):
        rs = [self.c.get("/login"), self.c.get("/static/panel.css"), self.c.get("/"), self.c.get("/robots.txt"),
              self.c.get("/favicon.ico"), self.c.request("PUT", "/"), Client(self.app, host="evil host").get("/login")]
        self.login()
        rs += [self.c.get("/"), self.c.get("/nope"), self.c.post("/service", {"unit": "bitpin-bot"}, csrf="wrong"),
               self.c.post("/logout", origin="https://evil.example"), self.c.post("/service", content_type="text/plain")]
        with mock.patch.dict(self.app._get_routes, {"/": mock.Mock(side_effect=RuntimeError("boom-internal"))}):
            crash = self.c.get("/")
        rs.append(crash)
        self.assertEqual(crash.status, 500)
        self.assertNotIn("boom-internal", crash.text)
        self.assertNotIn("Traceback", crash.text)
        self.assertEqual(sorted(set(r.status for r in rs)), [200, 303, 400, 403, 404, 405, 415, 500])
        for r in rs:
            static = r is rs[1]                                          # the stylesheet may be cached
            for k, v in REQUIRED_HEADERS.items():
                want = pw.STATIC_CACHE if (static and k == "Cache-Control") else v
                self.assertEqual(r.header(k), want, (r.status, k))

    def test_css_is_served_and_pages_carry_no_inline_script_or_style(self):
        r = self.c.get("/static/panel.css")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.header("Content-Type").startswith("text/css"))
        self.assertIn("--accent", r.text)
        texts = [("/login", self.c.get("/login").text)]
        self.login()
        texts += [(path, self.c.get(path).text) for path in PAGES]
        for path, text in texts:
            self.assertRegex(text, r'<link rel="stylesheet" href="/static/panel\.css\?v=[0-9a-f]{12}">', path)
            self.assertNotIn("<script", text, path)
            self.assertNotIn(" style=", text, path)
            self.assertNotRegex(text, r'(?:src|href|action)="(?:https?:)?//', path)       # nothing external

    def test_host_header_must_be_allowed(self):
        cfg = dict(self.cfg, allowed_hosts=["panel.example.org", "[2001:db8::1]"])
        app = self.make_app(cfg)
        self.assertEqual(Client(app, host="panel.example.org:8443").get("/login").status, 200)
        self.assertEqual(Client(app, host="PANEL.example.org").get("/login").status, 200)
        self.assertEqual(Client(app, host="[2001:db8::1]:8443").get("/login").status, 200)
        for bad in ("evil.example", "panel.example.org.evil.example", "", "a b", "panel.example.org/x", "x" * 300):
            r = Client(app, host=bad).get("/login")
            self.assertEqual(r.status, 400, bad)
            self.assertEqual(r.header("X-Frame-Options"), "DENY")
        st, _h, _b = app.handle("GET", "/login", "", [("Host", "panel.example.org"), ("Host", "evil.example")], b"", IP)
        self.assertEqual(st, 400)                                       # two Host headers
        st, _h, _b = app.handle("GET", "/login", "", {}, b"", IP)
        self.assertEqual(st, 400)                                       # none
        self.assertEqual(Client(self.app, host="anything.example:1").get("/login").status, 200)   # [] = any

    def test_other_methods_are_405(self):
        for m in ("PUT", "DELETE", "HEAD", "OPTIONS", "TRACE"):
            r = self.c.request(m, "/")
            self.assertEqual(r.status, 405, m)
            self.assertEqual(r.header("Allow"), "GET, POST")


class TestLanguages(PanelCase):
    """v3.2: English and Persian. The switch sets __Host-bplang; without it the browser's Accept-Language
    decides; nothing else about a request changes."""

    PERSIAN_DATA = (u"خرید بیت‌کوین با وزن ۴۰٪", u"وضعیت ربات: فعال", u"آخرین تصمیم: خرید", u"فارسی")

    def page(self, client, path, **headers):
        r = client.request("GET", path, headers=headers)
        self.assertEqual(r.status, 200, (path, r.text[:300]))
        return r.text

    def test_english_without_a_cookie_or_a_preference(self):
        c = Client(self.app, lang=None)
        t = self.page(c, "/login")
        self.assertIn('<html lang="en" dir="ltr">', t)
        self.assertIn(">Sign in<", t)
        self.assertIn('href="/lang?to=fa&amp;next=%2Flogin"', t)

    def test_the_browser_preference_decides_without_a_cookie(self):
        c = Client(self.app, lang=None)
        t = self.page(c, "/login", **{"Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8"})
        self.assertIn('<html lang="fa" dir="rtl">', t)
        self.assertIn(u"نام کاربری", t)
        c.cookies["__Host-bplang"] = "en"                                  # the owner's choice wins
        t = self.page(c, "/login", **{"Accept-Language": "fa"})
        self.assertIn('<html lang="en" dir="ltr">', t)
        c.cookies["__Host-bplang"] = "xx"                                  # an unknown value is ignored
        self.assertIn('<html lang="fa" dir="rtl">', self.page(c, "/login", **{"Accept-Language": "fa"}))

    def test_the_switch_sets_a_strict_cookie_and_goes_back(self):
        c = Client(self.app, lang=None)
        r = c.get("/lang", "to=fa&next=%2Ftrade")
        self.assertEqual((r.status, r.header("Location")), (303, "/trade"))
        self.assertEqual(r.headers_all("Set-Cookie"),
                         ["__Host-bplang=fa; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"])
        self.assertEqual(c.cookies.get("__Host-bplang"), "fa")
        self.assertIn('<html lang="fa" dir="rtl">', self.page(c, "/login"))   # works before signing in
        r = c.get("/lang", "to=en&next=%2Flogs%3Funit%3Dbitpin-bot%26lines%3D50")
        self.assertEqual(r.header("Location"), "/logs?lines=50&unit=bitpin-bot")
        r = c.get("/lang", "to=klingon&next=%2F")
        self.assertEqual((r.status, r.header("Location"), r.headers_all("Set-Cookie")), (303, "/", []))
        self.assertEqual(self.audit(), [])                                 # not an action: nothing to audit

    def test_the_switch_never_sends_the_browser_elsewhere(self):
        c = Client(self.app, lang=None)
        for nxt in ("//evil.example/x", "https://evil.example/", "/\\evil.example", "/nope", "javascript:alert(1)",
                    "", "http:/trade", "/login/../trade", "%2F%2Fevil.example"):
            r = c.get("/lang", urlencode({"to": "fa", "next": nxt}))
            self.assertEqual((r.status, r.header("Location")), (303, "/"), nxt)

    def test_every_page_in_english_shows_no_persian_interface_text(self):
        c = Client(self.app, lang="en")
        self.login(c)
        for path in PAGES:
            if path == "/settings":                                       # the raw files, as they are
                continue
            t = self.page(c, path)
            self.assertIn('<html lang="en" dir="ltr">', t, path)
            for s in self.PERSIAN_DATA:
                t = t.replace(s, "")
            left = sorted(set(ch for ch in t if u"\u0600" <= ch <= u"\u06ff"))
            self.assertEqual(left, [], path)
        t = self.page(c, "/trade")
        for f in ps.FIELDS:
            self.assertIn(pw.esc(f.label.en), t, f.key)

    def test_every_page_in_persian_uses_the_translations(self):
        self.login()
        t = self.page(self.c, "/trade")
        for f in ps.FIELDS:
            self.assertIn(pw.esc(f.label.fa), t, f.key)
        t = self.page(self.c, "/")
        for s in (u"داشبورد", u"ارزش حساب", u"آخرین تصمیم کیمی", u"سرویس‌ها", u"در حال اجرا"):
            self.assertIn(s, t, s)

    def test_the_new_password_rules_follow_the_language(self):
        c = Client(self.app, lang="en")
        self.login(c)
        c.post("/security/password", {"current": PW, "new1": "short", "new2": "short"})
        t = self.page(c, "/security")
        self.assertIn("The password must have at least 12 characters.", t)
        self.assertEqual(self.helper.calls_of("panel_password_set"), [])

    def test_a_request_leaves_no_language_behind(self):
        from bitpin import panel_i18n
        self.page(self.c, "/login")                                         # a Persian request
        self.assertEqual(panel_i18n.current(), "en")
        with mock.patch.dict(self.app._get_routes, {"/": mock.Mock(side_effect=RuntimeError("boom"))}):
            self.login()
            r = self.c.get("/")
        self.assertEqual(r.status, 500)
        self.assertIn(u"خطای داخلی", r.text)
        self.assertEqual(panel_i18n.current(), "en")

    def test_a_missing_stylesheet_is_reported_not_fatal(self):
        with mock.patch.object(pw, "STATIC_DIR", os.path.join(self.dir, "nowhere")):
            with self.assertLogs("bitpin.panel", "WARNING"):
                app = self.make_app()
        self.assertEqual(app.css_version, "missing")
        r = Client(app).get("/static/panel.css")
        self.assertEqual((r.status, r.body), (200, b""))
        self.assertEqual(Client(app).get("/login").status, 200)


# ----------------------------------------------------------------------------------------------- login

class TestLogin(PanelCase):
    def test_login_page(self):
        r = self.c.get("/login")
        self.assertEqual(r.status, 200)
        self.assertIn('<html lang="fa" dir="rtl">', r.text)
        self.assertIn('name="username"', r.text)
        self.assertIn('type="password" name="password"', r.text)
        self.assertNotIn('name="code"', r.text)                          # 2FA is off
        lt = [v for v in r.headers_all("Set-Cookie") if v.startswith("__Host-bplogin=")]
        self.assertEqual(len(lt), 1)
        self.assertTrue(lt[0].endswith("; Secure; HttpOnly; SameSite=Strict; Path=/"), lt[0])

    def test_a_successful_login_sets_a_strict_session_cookie(self):
        r = self.c.login()
        self.assertEqual(r.status, 303)
        self.assertEqual(r.header("Location"), "/")
        sess = [v for v in r.headers_all("Set-Cookie") if v.startswith("__Host-bpsid=")]
        self.assertEqual(len(sess), 1)
        self.assertRegex(sess[0], r"^__Host-bpsid=[A-Za-z0-9_-]{40,}; Secure; HttpOnly; SameSite=Strict; Path=/$")
        self.assertTrue(any(v.startswith("__Host-bplogin=;") and "Max-Age=0" in v for v in r.headers_all("Set-Cookie")))
        page = self.c.get("/")
        self.assertEqual(page.status, 200)
        self.assertIn(u"داشبورد", page.text)
        ok = self.audit("login_ok")
        self.assertEqual([(a["ip"], a["user"]) for a in ok], [(IP, USER)])
        notes = self.helper.calls_of("audit_notify")
        self.assertEqual([(n[1]["event"], n[1]["ip"], n[1]["user"]) for n in notes], [("login_ok", IP, USER)])
        self.assertEqual(self.c.get("/login").status, 303)                # already logged in

    def test_a_new_session_id_on_every_login(self):
        self.c.cookies["__Host-bpsid"] = "attacker-chosen-session-id-0123456789abcdef"
        self.login()
        self.assertNotEqual(self.c.cookies["__Host-bpsid"], "attacker-chosen-session-id-0123456789abcdef")
        first = self.c.cookies["__Host-bpsid"]
        self.assertEqual(self.c.get("/login").status, 303)                  # logged in: /login sends to /
        self.c.cookies["__Host-bplogin"] = "t" * 32                         # a login posted anyway (double submit)
        r = self.c.request("POST", "/login", {"lt": "t" * 32, "username": USER, "password": PW})
        self.assertEqual(r.status, 303)
        self.assertNotEqual(self.c.cookies["__Host-bpsid"], first)
        c2 = Client(self.app)
        c2.cookies["__Host-bpsid"] = first
        self.assertEqual(c2.get("/").status, 303)                          # the old id died with the new login

    def test_failures_are_identical_for_any_wrong_field_and_take_half_a_second(self):
        a = self.c.login(user="nobody")
        b = self.c.login(password="wrong password")
        c = self.c.login(user="nobody", password="")
        self.assertEqual((a.status, b.status, c.status), (403, 403, 403))
        self.assertEqual(a.body, b.body)
        self.assertEqual(a.body, c.body)
        self.assertIn(u"نادرست", a.text)
        self.assertEqual(len(self.sleeps), 3)
        self.assertTrue(all(0.4 <= s <= 0.5 for s in self.sleeps), self.sleeps)
        fails = self.audit("login_failed")
        self.assertEqual([f["reason"] for f in fails], ["password"] * 3)
        text = self.audit_text()
        self.assertNotIn("wrong password", text)
        self.assertNotIn("nobody", text)                                   # a mistyped name may be a password
        self.assertEqual([f["user"] for f in fails], ["(unknown)", USER, "(unknown)"])

    def test_the_login_form_token_is_required_and_not_counted_as_a_failure(self):
        c = Client(self.app)
        for _ in range(6):
            r = c.request("POST", "/login", {"lt": "x" * 32, "username": USER, "password": PW})
            self.assertEqual(r.status, 403)
            self.assertIn(u"منقضی", r.text)
        self.assertEqual(self.audit("login_failed"), [])
        self.login(c)                                                     # the IP is not locked

    def test_five_failures_lock_the_ip_notify_once_and_unlock_after_15_minutes(self):
        for i in range(5):
            self.assertEqual(self.c.login(password="bad-%d" % i).status, 403)
        locked = self.c.login()                                            # the right password, but locked
        self.assertEqual(locked.status, 429)
        self.assertIn(u"قفل", locked.text)
        self.assertEqual(self.c.login().status, 429)
        other = Client(self.app, ip="198.51.100.20")
        self.login(other)                                                  # another IP still gets in
        notes = [n for n in self.helper.calls_of("audit_notify") if n[1]["event"] == "login_locked"]
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0][1]["ip"], notes[0][1]["user"]), (IP, USER))
        self.assertEqual(len(self.audit("login_locked")), 1)
        self.now[0] += 15 * 60 + 1
        self.login()

    def test_thirty_failures_from_many_ips_lock_every_login_for_an_hour(self):
        for i in range(30):
            self.assertEqual(Client(self.app, ip="10.9.%d.1" % i).login(password="nope").status, 403)
        self.assertEqual(Client(self.app, ip="192.0.2.99").login().status, 429)
        notes = [n for n in self.helper.calls_of("audit_notify") if n[1]["event"] == "login_locked"]
        self.assertEqual(len(notes), 1)
        self.now[0] += 3600
        self.login(Client(self.app, ip="192.0.2.99"))

    def test_a_dead_or_slow_helper_never_blocks_a_login(self):
        self.helper.fail["audit_notify"] = "cannot connect to the helper socket"
        self.login()
        app = self.make_app(spawn=mock.Mock(side_effect=RuntimeError("can't start new thread")))
        self.login(Client(app))
        self.helper.explode["audit_notify"] = OSError("broken pipe")
        self.login(Client(self.app))

    def test_login_posts_from_another_origin_are_refused(self):
        r = self.c.get("/login")
        lt = re.search(r'name="lt" value="([^"]+)"', r.text).group(1)
        r = self.c.request("POST", "/login", {"lt": lt, "username": USER, "password": PW}, origin="https://evil.example")
        self.assertEqual(r.status, 403)
        self.assertEqual(self.audit(), [])


class TestTrustedBrowserLogin(PanelCase):
    """Security review F1: a flood of failed logins from many addresses locks every NEW browser for an hour,
    but not the owner's browser, which logged in before (its device cookie)."""

    def flood(self):
        for i in range(30):
            c = Client(self.app, ip="198.51.100.%d" % (i // 4 + 1))
            c.login(password="wrong-password-%d" % i)

    def test_the_owner_still_logs_in_during_a_global_lock(self):
        self.login()
        self.assertTrue(self.c.cookies.get("__Host-bpdev"))
        self.c.post("/logout")
        self.flood()
        stranger = Client(self.app, ip="203.0.113.99")
        self.assertEqual(stranger.login().status, 429)                 # even with the right password
        r = self.c.login()
        self.assertEqual(r.status, 303, r.text[:300])                   # the trusted browser gets in
        forged = Client(self.app, ip="203.0.113.98")
        forged.cookies["__Host-bpdev"] = self.c.cookies["__Host-bpdev"][:-4] + "0000"
        self.assertEqual(forged.login().status, 429)

    def test_a_password_change_ends_the_trust(self):
        self.login()
        old = self.c.cookies["__Host-bpdev"]
        self.assertIsNotNone(self.app.devices.check(old, self.app._password_hash, self.now[0]))
        self.app._password_hash = hash_password("An0ther-Str0ng-Pass!", iterations=1000)
        self.assertIsNone(self.app.devices.check(old, self.app._password_hash, self.now[0]))

    def test_the_device_cookie_is_strict_and_long_lived(self):
        r = self.c.login()
        v = [h for h in r.headers_all("Set-Cookie") if h.startswith("__Host-bpdev=")][0]
        for part in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=31536000"):
            self.assertIn(part, v)

    def test_a_panel_behind_a_proxy_must_bind_the_loopback(self):
        from bitpin.panel_web import config_problems
        cfg = dict(self.cfg, trusted_proxy=True, bind="0.0.0.0")
        self.assertTrue(any("trusted_proxy" in p for p in config_problems(cfg)))
        cfg["bind"] = "127.0.0.1"
        self.assertFalse(any("trusted_proxy" in p for p in config_problems(cfg)))


class TestRealBrowserHeaders(PanelCase):
    """3.1.1: what a real browser sends. The test Client above sends Origin: https://<host>; a browser applies the
    page's Referrer-Policy, and under no-referrer it sends "Origin: null" with every form POST (Fetch standard) - the
    owner's first login on the server got 403 (origin)."""

    def browser_post(self, path, form, origin="null", site="same-origin"):
        headers = {}
        if site is not None:
            headers["Sec-Fetch-Site"] = site
        return self.c.request("POST", path, form, origin=origin, headers=headers)

    def login_form(self):
        r = self.c.get("/login")
        self.assertEqual(r.header("Referrer-Policy"), "same-origin")
        token = re.search(r'name="lt" value="([^"]+)"', r.text).group(1)
        return {"lt": token, "username": USER, "password": PW}

    def test_a_browser_login_with_origin_null_and_same_origin_fetch_metadata(self):
        r = self.browser_post("/login", self.login_form())
        self.assertEqual(r.status, 303, r.text[:300])

    def test_origin_null_without_fetch_metadata_is_refused(self):
        r = self.browser_post("/login", self.login_form(), site=None)
        self.assertEqual(r.status, 403)
        self.assertIn("origin", r.text)

    def test_a_cross_site_post_is_refused_whatever_its_origin(self):
        for origin, site in (("null", "cross-site"), ("https://" + HOST, "cross-site"), ("null", "same-site"),
                             ("https://evil.example", "same-origin")):
            r = self.browser_post("/login", self.login_form(), origin=origin, site=site)
            self.assertEqual(r.status, 403, (origin, site))

    def test_a_browser_post_after_login(self):
        self.assertEqual(self.browser_post("/login", self.login_form()).status, 303)
        self.c.refresh_token()
        r = self.browser_post("/logout", {"csrf": self.c.token})
        self.assertEqual(r.status, 303)


class TestTotpLogin(PanelCase):
    totp_secret = TOTP_SECRET

    def test_the_code_field_is_shown_with_2fa(self):
        self.assertIn('name="code"', self.c.get("/login").text)

    def test_the_current_code_is_needed_and_works_only_once(self):
        self.assertEqual(self.c.login().status, 403)                       # no code
        self.assertEqual(self.c.login(code="000000" if totp(TOTP_SECRET, T0) != "000000" else "111111").status, 403)
        code = totp(TOTP_SECRET, T0)
        self.login(code=code)
        self.c.post("/logout")
        again = self.c.login(code=code)
        self.assertEqual(again.status, 403)                                # replay refused
        self.now[0] += 30
        self.login(code=totp(TOTP_SECRET, self.now[0]))

    def test_failures_are_identical_whichever_field_is_wrong(self):
        good = totp(TOTP_SECRET, T0)
        bad_code = "%06d" % ((int(good) + 1) % 1000000)
        a = self.c.login(user="nobody", code=good)
        b = self.c.login(password="wrong", code=good)
        c = self.c.login(code=bad_code)
        self.assertEqual((a.status, b.status, c.status), (403, 403, 403))
        self.assertEqual(a.body, b.body)
        self.assertEqual(b.body, c.body)
        self.assertEqual([f["reason"] for f in self.audit("login_failed")], ["password", "password", "code"])
        self.login(code=good)                                              # the good code was never consumed

    def test_persian_digits_are_accepted(self):
        code = totp(TOTP_SECRET, T0).translate(dict((ord(str(i)), c) for i, c in enumerate(u"۰۱۲۳۴۵۶۷۸۹")))
        self.login(code=code)


# ----------------------------------------------------------------------------------------------- sessions, CSRF

class TestSessionsAndCsrf(PanelCase):
    def test_every_page_needs_a_session(self):
        for path in PAGES:
            r = self.c.get(path)
            self.assertEqual((r.status, r.header("Location")), (303, "/login"), path)
        r = self.c.post("/service", {"unit": "bitpin-bot", "action": "restart"}, csrf="x")
        self.assertEqual((r.status, r.header("Location")), (303, "/login"))
        self.assertEqual(self.helper.calls, [])

    def test_an_unknown_session_cookie_is_cleared(self):
        self.c.cookies["__Host-bpsid"] = "garbage"
        r = self.c.get("/")
        self.assertEqual(r.status, 303)
        self.assertTrue(any("__Host-bpsid=;" in v and "Max-Age=0" in v for v in r.headers_all("Set-Cookie")))

    def test_idle_expiry(self):
        self.login()
        self.now[0] += 29 * 60
        self.assertEqual(self.c.get("/logs").status, 200)
        self.now[0] += 30 * 60 + 1
        self.assertEqual(self.c.get("/logs").status, 303)

    def test_absolute_expiry(self):
        self.login()
        for _ in range(35):
            self.now[0] += 20 * 60
            self.assertEqual(self.c.get("/logs").status, 200)
        self.now[0] += 20 * 60 + 1                                         # 12 h 0 min 1 s after the login
        self.assertEqual(self.c.get("/logs").status, 303)

    def test_missing_or_wrong_csrf_is_403(self):
        self.login()
        form = {"unit": "bitpin-bot", "action": "restart", "back": "/"}
        self.assertEqual(self.c.post("/service", form, csrf=False).status, 403)
        self.assertEqual(self.c.post("/service", form, csrf="not-the-token").status, 403)
        other = self.login(Client(self.app, ip="198.51.100.3"))
        self.assertEqual(self.c.post("/service", form, csrf=other.token).status, 403)   # another session's token
        self.assertEqual(self.helper.calls_of("service"), [])
        self.assertEqual(len(self.audit("csrf_refused")), 3)
        self.assertEqual(self.c.post("/service", form).status, 303)
        self.assertEqual(len(self.helper.calls_of("service")), 1)

    def test_origin_must_match_the_host(self):
        self.login()
        form = {"unit": "bitpin-bot", "action": "restart", "back": "/"}
        for origin in ("https://evil.example", "null", "http://" + HOST, "https://panel.example.org:9999",
                       "https://" + HOST + ".evil.example"):
            self.assertEqual(self.c.post("/service", form, origin=origin).status, 403, origin)
        self.assertEqual(self.helper.calls_of("service"), [])
        self.assertEqual(self.c.post("/service", form, origin="https://" + HOST.upper()).status, 303)
        self.assertEqual(self.c.post("/service", form, origin=False).status, 303)       # no Origin header: allowed
        c443 = self.login(Client(self.app, host="panel.example.org"))
        self.assertEqual(c443.post("/service", form, origin="https://panel.example.org:443").status, 303)

    def test_cross_site_fetch_metadata_is_refused(self):
        self.login()
        form = {"unit": "bitpin-bot", "action": "restart"}
        self.assertEqual(self.c.post("/service", form, headers={"Sec-Fetch-Site": "cross-site"}).status, 403)
        self.assertEqual(self.c.post("/service", form, headers={"Sec-Fetch-Site": "same-site"}).status, 403)
        self.assertEqual(self.c.post("/service", form, headers={"Sec-Fetch-Site": "same-origin"}).status, 303)

    def test_only_urlencoded_posts(self):
        self.login()
        for ct in ("text/plain", "multipart/form-data; boundary=x", "application/json", None):
            self.assertEqual(self.c.post("/service", {"unit": "bitpin-bot"}, content_type=ct).status, 415, ct)
        r = self.c.post("/service", {"unit": "bitpin-bot", "action": "restart"},
                        content_type="application/x-www-form-urlencoded; charset=UTF-8")
        self.assertEqual(r.status, 303)

    def test_get_never_changes_state(self):
        self.login()
        for path, q in (("/service", "unit=bitpin-bot&action=restart"), ("/logout", ""), ("/security/totp/new", ""),
                        ("/apply/check", ""), ("/vpn/raw", ""), ("/models/key", "name=KIMI_API_KEY&value=x")):
            self.assertEqual(self.c.get(path, q).status, 404, path)
        self.assertEqual(self.c.get("/logs").status, 200)                  # still logged in
        self.assertNotIn("totp_pending", self.app.sessions.get(self.c.cookies["__Host-bpsid"], self.now[0]))
        self.assertEqual([c for c in self.helper.cmds() if c not in ("audit_notify", "status", "config_get")], [])

    def test_logout(self):
        self.login()
        sid = self.c.cookies["__Host-bpsid"]
        r = self.c.post("/logout")
        self.assertEqual((r.status, r.header("Location")), (303, "/login"))
        self.assertNotIn("__Host-bpsid", self.c.cookies)
        self.assertTrue(any("__Host-bpsid=;" in v and "Max-Age=0" in v for v in r.headers_all("Set-Cookie")))
        self.c.cookies["__Host-bpsid"] = sid
        self.assertEqual(self.c.get("/").status, 303)
        self.assertEqual(len(self.audit("logout")), 1)

    def test_too_many_form_fields_is_400(self):
        self.login()
        body = "&".join("f%d=1" % i for i in range(pw.MAX_FORM_FIELDS + 5)).encode()
        self.assertEqual(self.c.request("POST", "/service", body=body).status, 400)


# ----------------------------------------------------------------------------------------------- pages

class TestPages(PanelCase):
    def test_every_page_renders_with_the_fake_helper(self):
        self.login()
        for path, q in [(p, "") for p in PAGES] + [("/logs", "unit=bitpin-bot&lines=50")]:
            r = self.c.get(path, q)
            self.assertEqual(r.status, 200, (path, r.text[:300]))
            self.assertIn('<html lang="fa" dir="rtl">', r.text, path)
            self.assertIn('href="/security"', r.text, path)
            self.assertIn('action="/logout"', r.text, path)
            self.assertNotIn("Traceback", r.text, path)
            self.assertNotIn('class="box err"', r.text, path)
        self.assertIn("log of bitpin-bot (50 lines)", self.c.get("/logs", "unit=bitpin-bot&lines=50").text)
        self.assertIn("bot healthy", self.c.get("/health").text)

    def test_the_actor_goes_to_the_helper(self):
        self.login()
        self.c.get("/")
        cmd, args, actor = self.helper.calls_of("status")[-1]
        self.assertEqual(actor, {"user": USER, "ip": IP})

    def test_dashboard(self):
        self.login()
        t = self.c.get("/").text
        for s in ("BTC_IRT", "40.0%", "60.0%", u"خرید بیت‌کوین با وزن ۴۰٪", "3.1.0", "slot", "kimi-k3", "3,990,000",
                  "2.68", "+5.00%", "BTC_USDT", "65000.5", "0.17", u"معامله (تغییر وزن‌ها)", "active (running)",
                  "failed (failed)", "bitpin-bot-panel", '<pre dir="rtl" class="fa">', u"وضعیت ربات: فعال",
                  "2026-09-27 23:00"):                                     # systemd's UTC time, shown as Tehran time
            self.assertIn(s, t, s)
        self.assertNotIn("DOGE_IRT", t)                                    # only targets above 0
        self.assertNotIn(BANNER, t)
        self.assertRegex(t, r'<form method="post" action="/service" class="inline"><input type="hidden" name="csrf" '
                            r'value="[^"]+"><input type="hidden" name="unit" value="bitpin-bot"><input type="hidden" '
                            r'name="action" value="restart">')

    def test_dashboard_banner_when_saved_settings_are_not_applied(self):
        self.login()
        st = status_data()
        st["live_confirmed"] = {"ok": False, "why": "settings changed since the last confirm-live"}
        self.helper.responses["status"] = st
        t = self.c.get("/").text
        self.assertIn(BANNER, t)
        self.assertIn('<a href="/apply">', t)
        self.assertIn("settings changed since the last confirm-live", t)

    def test_dashboard_survives_nulls(self):
        self.login()
        st = status_data()
        st.update(equity=None, last_decision=None, positions=None, resting_orders=None, spend=None, status_fa=None,
                  last_decision_fa=None, state_error="OSError: state dir unreadable", live_confirmed=None,
                  bot={"state": "inactive", "sub": "dead", "enabled": "disabled", "since": None})
        self.helper.responses["status"] = st
        r = self.c.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("state dir unreadable", r.text)
        self.assertIn('value="start"', r.text)                             # a stopped bot gets a start button
        st["equity"] = {"irt": None, "time": None, "hwm_irt": None, "drawdown_pct": None, "halt_pct": None,
                        "halted": True, "start_irt": None}
        st["last_decision"] = {"time": None, "mode": None, "hold": None, "valid": None, "fallback": True,
                               "confidence": None, "model": None, "targets": None, "report_fa": None,
                               "error": "LLM timeout", "error_kind": "llm_timeout"}
        r = self.c.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("LLM timeout", r.text)
        self.assertIn(u"معامله متوقف است", r.text)

    def test_bot_buttons_follow_its_state(self):
        self.login()
        t = self.c.get("/").text
        self.assertIn('name="action" value="restart"', t)
        self.assertIn('name="action" value="stop"', t)
        self.assertNotIn('name="unit" value="bitpin-bot"><input type="hidden" name="action" value="start"', t)
        self.helper.fail["status"] = "helper down"
        t = self.c.get("/").text                                            # unknown state: every action offered
        for action in ("restart", "start", "stop"):
            self.assertIn('name="unit" value="bitpin-bot"><input type="hidden" name="action" value="%s"' % action, t)

    def test_helper_failures_render_a_persian_error_box_never_a_trace(self):
        self.login()
        for cmd in ("status", "config_get", "secrets_status", "confirm_show", "vpn_get", "health", "performance"):
            self.helper.fail[cmd] = "cannot connect to the helper socket /run/bitpin-panel/helper.sock"
        for path in PAGES + ("/logs?unit=bitpin-bot",):
            path, _, q = path.partition("?")
            if path == "/logs":
                self.helper.fail["logs"] = "journalctl failed"
            r = self.c.get(path, q)
            self.assertIn(r.status, (200,), path)
            if path not in ("/logs", "/security"):
                self.assertIn('class="box err"', r.text, path)
                self.assertIn(u"خطا از سرویس کمکی پنل", r.text, path)
            self.assertNotIn("Traceback", r.text, path)
        self.assertIn("journalctl failed", self.c.get("/logs", "unit=bitpin-bot").text)
        self.helper.fail.clear()
        self.helper.explode["status"] = ValueError("a bug in the client")
        r = self.c.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn('class="box err"', r.text)
        self.assertIn("a bug in the client", r.text)

    def test_hostile_strings_are_escaped_everywhere(self):
        self.login()
        st = status_data()
        st["bot"]["version"] = HOSTILE
        st["live_confirmed"] = {"ok": False, "why": HOSTILE}
        st["last_decision"].update(report_fa=HOSTILE, error=HOSTILE, error_kind=HOSTILE, mode=HOSTILE, model=HOSTILE,
                                   targets={HOSTILE: 0.5})
        st["positions"][0]["symbol"] = HOSTILE
        st["status_fa"] = HOSTILE
        st["state_error"] = HOSTILE
        st["notifier"]["since"] = HOSTILE
        self.helper.responses["status"] = st
        self.assertEscaped(self.c.get("/").text, "/")
        self.helper.responses["health"] = {"ok": False, "text": HOSTILE}
        self.assertEscaped(self.c.get("/health").text, "/health")
        self.helper.responses["logs"] = {"text": HOSTILE}
        self.assertEscaped(self.c.get("/logs", "unit=bitpin-bot").text, "/logs")
        self.helper.responses["confirm_show"] = {"ok": False, "text": HOSTILE}
        self.assertEscaped(self.c.get("/apply").text, "/apply")
        kimi = json.loads(KIMI_TEXT)
        kimi["llm"]["model"] = HOSTILE
        kimi["brain"]["extra_instructions"] = "</textarea>" + HOSTILE
        self.helper.responses["config_get"] = {"config": CONFIG_TEXT, "kimi": json.dumps(kimi)}
        for path in ("/models", "/settings", "/trade"):
            t = self.c.get(path).text
            self.assertEscaped(t, path)
            self.assertNotIn("</textarea><script", t, path)
        self.helper.responses["config_get"] = {"config": CONFIG_TEXT, "kimi": KIMI_TEXT}
        self.helper.responses["models"] = {"models": [{"id": HOSTILE, "name": HOSTILE, "context_length": HOSTILE}],
                                           "provider": "moonshot", "key_name": HOSTILE}
        self.assertEscaped(self.c.post("/models/list", {"provider": "moonshot"}).text, "/models/list")
        self.helper.responses["vpn_get"] = {"unit_state": HOSTILE, "config_path": HOSTILE, "error": HOSTILE, "raw": None,
                                            "summary": {"proxy": {"address": HOSTILE}, "inbounds": [{"tag": HOSTILE}],
                                                        "outbounds": [{"protocol": HOSTILE}], "local_proxies": [HOSTILE],
                                                        HOSTILE: HOSTILE}}
        self.assertEscaped(self.c.get("/vpn").text, "/vpn")
        self.helper.responses["vpn_get"] = default_responses()["vpn_get"]
        self.helper.responses["vpn_test"] = {"proxy": HOSTILE, "required": [HOSTILE], "unit_state": HOSTILE,
                                             "results": [{"target": HOSTILE, "ok": False, "ms": HOSTILE, "error": HOSTILE}]}
        self.assertEscaped(self.c.post("/vpn/test").text, "/vpn/test")
        self.helper.fail["service"] = HOSTILE                              # a helper error text, then the audit table
        self.c.post("/service", {"unit": "bitpin-bot", "action": "restart", "back": "/logs"})
        self.assertEscaped(self.c.get("/logs").text, "flash")
        self.assertEscaped(self.c.get("/security").text, "/security audit table")


# ----------------------------------------------------------------------------------------------- performance (v3.6)

class TestPerformancePages(PanelCase):
    """The P&L report, its live charts and the trade history: a range of time, per asset, per position."""

    def perf_calls(self):
        return [c[1] for c in self.helper.calls_of("performance")]

    def history_report(self, n=120):
        rep = perf_report({"from": int(T0) - 86400, "to": int(T0)})
        base = rep["history"][0]
        rep["history"] = [dict(base, t=T0 - 60 * i, side="buy" if i % 3 else "sell", asset="BTC" if i % 2 else "USDT",
                               symbol="BTC_IRT" if i % 2 else "USDT_IRT", order_id="id%d" % i) for i in range(n)]
        rep["history_total"] = n
        return rep

    def test_the_performance_page(self):
        self.login()
        r = self.c.get("/performance")
        self.assertEqual(r.status, 200)
        t = r.text
        self.assertEqual(self.perf_calls(), [{"from": int(T0) - 400 * 86400, "to": int(T0)}])
        for s in (u"عملکرد", u"ارزش سبد", u"سود / زیان به تومان", u"سود / زیان به تتر", u"اگر فقط تتر نگه می‌داشتیم",
                  u"سود و زیان هر دارایی", u"تومان (نقد)", u"جمع", u"موقعیت‌های باز و سفارش‌ها", "BTC / USDT",
                  u"سفارش فروش", u"سفارش خرید", u"حد ضرر", u"ادامهٔ روند", "A test plan &lt;b&gt;", u"زنده",
                  '<polyline class="l s1"', '<polyline class="l s2"', '<polyline class="l s3"',
                  '<polyline class="l pl"', 'class="h stop"', 'class="h target"', 'class="h entry"', 'class="h sell"',
                  'class="h buy"', 'class="v set"', 'preserveAspectRatio="none"', '<figure class="chart" dir="ltr">',
                  '<span class="gain">', '<a href="/performance" aria-current="page">',
                  u"نقطهٔ آخر تخمینی برای همین دقیقه است"):
            self.assertIn(s, t, s)
        self.assertIn('<meta http-equiv="refresh" content="60; url=/performance?range=all&amp;auto=1">', t)
        self.assertNotIn("auto%3D1", t)                              # the language switch comes back without it
        self.assertEqual(t.count('<article class="position">'), 2)   # BTC held, ETH only an order
        self.assertIn(u"هنوز دادهٔ کافی برای نمودار نیست.", t)      # no ETH candles in this world

    def test_the_outlook_and_the_analysis_of_a_position(self):
        """v3.6.1: the future of each position chart (the normal range, Kimi's two scenarios and their weighted
        price) and its strategy and analysis in words."""
        self.login()
        t = self.c.get("/performance").text
        for s in ('<rect class="future"', '<polygon class="a cone2"', '<polygon class="a cone1"', 'class="v now"',
                  '<polyline class="l fc-tp"', '<polyline class="l fc-inv"', '<polyline class="l fc-ev"',
                  'class="h inv"', u"پیش‌بینی تا", u"سناریوی هدف <bdi dir=\"ltr\">60,000.00</bdi> &middot; احتمال "
                  u"<bdi dir=\"ltr\">35%</bdi>", u"سناریوی ابطال یا پایان مهلت <bdi dir=\"ltr\">47,000.00</bdi> &middot; "
                  u"احتمال <bdi dir=\"ltr\">65%</bdi>", u"قیمت مورد انتظار (وزن‌دار با احتمال) <bdi dir=\"ltr\">51,550.00"
                  u"</bdi>", u"محدودهٔ نوسان عادی قیمت", u"استراتژی و تحلیل", u"<b>ادامهٔ روند</b>",
                  u"<bdi dir=\"ltr\">B10</bdi> &middot; وضعیت روند ۸۴ روزهٔ بیت‌کوین", u"<b>تکنیکال:</b> میانگین‌های متحرک "
                  u"نمایی (EMA) چهارساعته، بازده به تتر", u"<b>کلان:</b> روند ۸۴ روزهٔ بیت‌کوین، بتا نسبت به بیت‌کوین",
                  u"<b>آماری:</b> نرخ پایهٔ تاریخی، احتمال و ارزش انتظاری",
                  u"رسیدن به هدف پیش از سطح ابطال: <bdi dir=\"ltr\">35%</bdi> (در بازار بی‌جهت: <bdi dir=\"ltr\">30%</bdi>)",
                  u"تا هدف", u"<span class=\"gain\"><bdi dir=\"ltr\">+2.60%</bdi></span>", u"نگه‌داشتن (HOLD)",
                  u"ema_dev_pct=[0.5,0.9,5.0] all&gt;0", u"متن اصلی کیمی",
                  u"کیمی در تصمیم‌های اخیرش این کوین را دوباره نسنجیده"):        # ETH: no analysis
            self.assertIn(s, t, s)
        c = Client(self.app, lang="en")
        self.login(c)
        t = c.get("/performance").text
        for s in ("Target scenario", "probability <bdi dir=\"ltr\">35%</bdi>", "Trend continuation",
                  "the 84-day trend state of BTC", "<b>Technical:</b> 4h EMAs, returns in USDT",
                  "take profit before the invalidation: <bdi dir=\"ltr\">35%</bdi> (without an edge: "
                  "<bdi dir=\"ltr\">30%</bdi>)"):
            self.assertIn(s, t, s)

    def test_the_numbers_on_the_page_are_the_report(self):
        self.login()
        c = Client(self.app, lang="en")
        self.login(c)
        t = c.get("/performance", "range=24h").text
        rep = perf_report({"from": int(T0) - 86400, "to": int(T0)})
        tot = rep["totals"]
        for s in ("%+.2f%%" % tot["pnl_irt_pct"], "%+.2f%%" % tot["pnl_usdt_pct"], "%+.2f%%" % tot["rial_fall_pct"],
                  "{:,.0f}".format(tot["value_to_irt"]), "Profit and loss per asset", "Holding USDT instead",
                  "Toman (cash)", "Live", "Stop live updates"):
            self.assertIn(s, t, s)
        rows = dict((a["asset"], a) for a in rep["assets"])
        self.assertIn("{:+,.0f}".format(rows["USDT"]["pnl_irt"]), t)
        self.assertIn("{:+,.2f}".format(rows["IRT"]["pnl_usdt"]), t)

    def test_the_ranges(self):
        self.login()
        now = int(T0)
        for q, want in (("range=24h", now - 86400), ("range=7d", now - 7 * 86400), ("range=30d", now - 30 * 86400),
                        ("range=bogus", now - 400 * 86400)):
            self.app._perf_cache.clear()
            self.assertEqual(self.c.get("/performance", q).status, 200, q)
            self.assertEqual(self.perf_calls()[-1], {"from": want, "to": now}, q)
        # days in Tehran time (Persian digits too): from 00:00 of the first day to the end of the last one
        day0 = pw._tehran_day("2026-09-20")
        t = self.c.get("/performance", urlencode([("range", "custom"), ("from", "2026-09-20"),
                                                  ("to", u"۲۰۲۶-۰۹-۲۱")])).text
        self.assertEqual(self.perf_calls()[-1], {"from": int(day0), "to": int(day0 + 2 * 86400)})
        self.assertNotIn('http-equiv="refresh"', t)                 # a range in the past does not change
        self.assertIn(u"موقعیت‌های باز و نمودارهایشان فقط", t)
        self.assertIn('value="2026-09-21"', t)
        self.c.get("/performance", "range=custom&from=2026-09-20")      # no last day: until now
        self.assertEqual(self.perf_calls()[-1], {"from": int(day0), "to": now})
        n = len(self.perf_calls())
        for q in ([("range", "custom")], [("range", "custom"), ("from", "2026-02-30")],
                  [("range", "custom"), ("from", "2026-09-21"), ("to", "2026-09-20")],
                  [("range", "custom"), ("from", "2026-12-01")],
                  [("range", "custom"), ("from", "2025-01-01"), ("to", "2026-09-21")],
                  [("range", "custom"), ("from", "2026-09-20"), ("to", "not a day")]):
            for path in ("/performance", "/history"):
                r = self.c.get(path, urlencode(q))
                self.assertEqual(r.status, 400, (path, q))
                self.assertIn('class="box err"', r.text, q)
            self.assertEqual(self.c.get("/history.csv", urlencode(q)).status, 400, q)
        self.assertEqual(len(self.perf_calls()), n)                  # a bad range never reaches the helper

    def test_live_updates_can_be_stopped(self):
        self.login()
        t = self.c.get("/performance", "range=7d").text
        self.assertIn('content="60; url=/performance?range=7d&amp;auto=1"', t)
        self.assertIn('href="/performance?range=7d&amp;live=0"', t)
        t = self.c.get("/performance", "range=7d&live=0").text
        self.assertNotIn('http-equiv="refresh"', t)
        self.assertIn('href="/performance?range=7d"', t)             # start them again
        self.assertIn('href="/performance?range=24h&amp;live=0"', t)  # the other ranges keep them off
        self.assertIn('<input type="hidden" name="live" value="0">', t)

    def test_a_page_that_reloads_itself_does_not_keep_the_session_alive(self):
        self.login()
        for _ in range(3):
            self.now[0] += 600
            self.assertEqual(self.c.get("/performance", "range=all&auto=1").status, 200)
        self.now[0] += 1201                                          # 30 minutes after the owner's last request
        r = self.c.get("/performance", "range=all&auto=1")
        self.assertEqual((r.status, r.header("Location")), (303, "/login"))
        self.login()                                                 # the owner's own requests do keep it
        for _ in range(3):
            self.now[0] += 1500
            self.assertEqual(self.c.get("/performance").status, 200)

    def test_a_report_is_reused_for_a_while_but_never_an_error(self):
        self.login()
        self.c.get("/performance")
        self.c.get("/history")                                       # the same range: the same report
        self.now[0] += 30
        self.c.get("/history", "page=2")
        self.c.get("/history.csv")
        self.assertEqual(len(self.perf_calls()), 1)
        self.now[0] += 20
        self.c.get("/performance")
        self.assertEqual(len(self.perf_calls()), 2)
        self.helper.fail["performance"] = "the report failed"
        self.now[0] += 60
        t = self.c.get("/performance").text
        self.assertIn('class="box err"', t)
        self.assertIn('http-equiv="refresh"', t)                     # a live page tries again by itself
        self.helper.fail.clear()
        self.assertNotIn('class="box err"', self.c.get("/performance").text)
        self.assertEqual(len(self.perf_calls()), 4)

    def test_the_history_pages_and_filters(self):
        self.login()
        self.helper.responses["performance"] = self.history_report()
        t = self.c.get("/history").text
        self.assertEqual(t.count("<tr>"), 1 + 50)
        self.assertIn(u"<bdi dir=\"ltr\">120</bdi> معامله: <bdi dir=\"ltr\">80</bdi> خرید و <bdi dir=\"ltr\">40</bdi> فروش.", t)
        self.assertIn('href="/history?range=all&amp;page=2"', t)
        self.assertNotIn("page=0", t)
        t = self.c.get("/history", "page=2").text
        self.assertIn('href="/history?range=all&amp;page=1"', t)
        self.assertIn('href="/history?range=all&amp;page=3"', t)
        self.assertEqual(self.c.get("/history", "page=3").text.count("<tr>"), 1 + 20)
        self.assertEqual(self.c.get("/history", "page=99").text.count("<tr>"), 1 + 20)     # the last page
        self.assertEqual(self.c.get("/history", "page=x").text.count("<tr>"), 1 + 50)
        t = self.c.get("/history", "asset=btc&side=sell").text
        self.assertEqual(t.count("<tr>"), 1 + 20)
        self.assertIn('<option value="BTC" selected>BTC</option>', t)
        self.assertIn('<option value="sell" selected>', t)
        self.assertIn('href="/history.csv?range=all&amp;asset=BTC&amp;side=sell"', t)
        self.assertIn(u"<span class=\"badge err\">فروش</span>", t)
        self.assertEqual(self.c.get("/history", "side=bogus").text.count("<tr>"), 1 + 50)
        self.assertNotIn(u"فقط", self.c.get("/history").text)
        rep = self.history_report()
        rep["history_total"] = 5000
        self.helper.responses["performance"] = rep
        self.app._perf_cache.clear()
        self.assertIn(u"فقط <bdi dir=\"ltr\">120</bdi> معاملهٔ جدیدتر", self.c.get("/history").text)

    def test_the_csv_download(self):
        self.login()
        rep = self.history_report(10)
        rep["history"][1].update(reason="=cmd|' /C calc'!A0", order_id="+1", route="@x")
        self.helper.responses["performance"] = rep
        r = self.c.get("/history.csv", "range=all&side=sell")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.header("Content-Type"), "text/csv; charset=utf-8")
        self.assertRegex(r.header("Content-Disposition"), r'^attachment; filename="bitpin-trades-\d{8}-\d{4}\.csv"$')
        self.assertEqual(r.header("X-Content-Type-Options"), "nosniff")
        text = r.body.decode("utf-8")
        self.assertTrue(text.startswith(u"﻿"))                  # a spreadsheet reads it as UTF-8
        rows = list(csv.reader(io.StringIO(text[1:])))
        self.assertEqual(rows[0], ["time_utc", "time_tehran", "market", "side", "amount", "price", "quote_asset",
                                   "value_irt", "value_usdt", "fee", "fee_asset", "reason", "route", "order_id"])
        self.assertEqual([x[3] for x in rows[1:]], ["sell"] * 4)      # i = 0, 3, 6, 9
        all_rows = list(csv.reader(io.StringIO(self.c.get("/history.csv").body.decode("utf-8")[1:])))
        self.assertEqual(len(all_rows), 11)
        risky = [x for x in all_rows if x[-1] == "'+1"][0]
        self.assertEqual((risky[11], risky[12]), ("'=cmd|' /C calc'!A0", "'@x"))
        self.assertRegex(all_rows[1][0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertNotIn("e-", all_rows[1][4])                       # plain decimals, not 1e-05

    def test_hostile_strings_are_escaped(self):
        self.login()
        rep = perf_report({"from": int(T0) - 86400, "to": int(T0)})
        rep["warnings"] = [HOSTILE]
        rep["assets"][0]["asset"] = HOSTILE
        rep["positions"][0].update(asset=HOSTILE, note=HOSTILE, setup=HOSTILE)
        rep["positions"][0]["analysis"].update(evidence=HOSTILE, bear=HOSTILE, row=HOSTILE, verdict=HOSTILE,
                                               setup=HOSTILE)
        for h in rep["history"]:
            h.update(symbol=HOSTILE, reason=HOSTILE, quote_asset=HOSTILE, fee_asset=HOSTILE, asset=HOSTILE)
        self.helper.responses["performance"] = rep
        self.assertEscaped(self.c.get("/performance").text, "/performance")
        self.assertEscaped(self.c.get("/history").text, "/history")
        self.assertEscaped(self.c.get("/performance", urlencode([("range", "custom"), ("from", HOSTILE)])).text,
                           "a hostile range")

    def test_odd_reports_still_render(self):
        self.login()
        for rep in ({}, {"totals": None, "assets": None, "equity": [[1, None]], "positions": [None, {}],
                         "history": [None, {"t": "x", "side": None}, {"t": 1e300, "side": "buy", "price": 1e300}],
                         "warnings": [None, 5], "live": True},
                    {"equity": [[T0, 5.0, None, None], [T0 + 60, 6.0, None, None]], "totals": {"trades": "x"}},
                    {"equity": [[1e300, 5.0, 1.0, 5.0], [T0, 6.0, 1.0, 6.0], [T0 + 60, 7.0, 1.0, 7.0]], "live": True,
                     "positions": [{"asset": "BTC", "prices": [[1e300, 5.0], [T0, 1.0], [T0 + 9, 2.0]],
                                    "set_at": 1e300, "max_hold_until": 1e300},
                                   {"asset": "SOL", "prices": [[T0, 1.0], [T0 + 9, 2.0]], "analysis": {
                                       "p": "x", "row": 5, "evidence": 7, "time": "y", "verdict": None},
                                    "outlook": {"cone": [[1, 2], "x"], "origin": "x", "to": "y", "p": "z"}},
                                   {"asset": "XRP", "prices": [[T0, 1.0], [T0 + 9, 2.0]], "analysis": "x",
                                    "outlook": {"cone": [[T0, 1, 2, 0, 3], [T0 + 99, 1, 2, 0, None]],
                                                "origin": [T0, None], "to": T0 + 99, "target": 3, "p": 2}}]}):
            self.helper.responses["performance"] = rep
            self.app._perf_cache.clear()
            for path in ("/performance", "/history", "/history.csv"):
                r = self.c.get(path)
                self.assertEqual(r.status, 200, (path, rep))
                self.assertNotIn("Traceback", r.text)

    def test_the_chart_helpers(self):
        self.assertEqual(pw.nice_ticks(-1.3, 11.2)[:2], (-2.5, 12.5))
        b, t, ticks, step = pw.nice_ticks(61234.5, 83000.0)
        self.assertEqual((b, t, step), (60000.0, 85000.0, 5000.0))
        self.assertEqual([pw.tick_text(v, step) for v in ticks][:2], ["60,000", "65,000"])
        self.assertEqual(pw.tick_text(-2.5, 2.5, pct=True), "-2.5%")
        self.assertEqual(pw.tick_text(1e-12, 2.5, pct=True), "0.0%")
        b, t, ticks, step = pw.nice_ticks(5.0, 5.0)                             # a flat line still gets an axis
        self.assertTrue(b < 5.0 < t and len(ticks) >= 3)
        self.assertEqual((pw.price_text(65000.504), pw.price_text(142.1236), pw.price_text(2.34567),
                          pw.price_text(0.00001234)), ("65,000.50", "142.124", "2.3457", "0.00001234"))
        self.assertEqual((pw.qty_text(0.00012345), pw.qty_text(3612345.4, "IRT"), pw.qty_text(-1e-13)),
                         ("0.00012345", "3,612,345", "0"))
        self.assertIn("muted empty", pw.line_chart([("s1", [(1.0, 2.0)])]))    # one point is no line
        one_time = pw.line_chart([("s1", [(5.0, 1.0), (5.0, 2.0)])])           # a single moment: no division by 0
        self.assertIn("<polyline", one_time)
        self.assertIsNone(pw._tehran_day("2026-9-31"))
        self.assertEqual(pw.csv_cell(None), "")


# ----------------------------------------------------------------------------------------------- secrets

class TestSecretsNeverLeak(PanelCase):
    def all_text(self, responses):
        return "\n".join(r.text for r in responses) + "\n" + "\n".join(v for r in responses for _k, v in r.headers)

    def test_a_key_value_is_never_shown_or_logged(self):
        self.login()
        rs = [self.c.post("/models/key", {"name": "OPENROUTER_API_KEY", "value": KEY_VALUE})]
        self.assertEqual(rs[0].status, 303)
        rs.append(self.c.get("/models"))
        self.assertIn(u"ثبت شد", rs[1].text)
        self.assertIn('<bdi dir="ltr">%d</bdi>' % len(KEY_VALUE), rs[1].text)
        rs.append(self.c.get("/security"))
        self.assertNotIn(KEY_VALUE, self.all_text(rs))
        self.assertNotIn(KEY_VALUE, self.audit_text())
        cmd, args, actor = self.helper.calls_of("secret_set")[0]
        self.assertEqual((args["name"], args["value"]), ("OPENROUTER_API_KEY", KEY_VALUE))
        line = [a for a in self.audit("action") if a["cmd"] == "secret_set"][0]
        self.assertEqual((line["name"], line["result"]), ("OPENROUTER_API_KEY", "ok"))
        self.assertNotIn('value="%s"' % KEY_VALUE, rs[1].text)
        self.assertRegex(rs[1].text, r'<input id="k-value" type="password" name="value" class="ltr" '
                                     r'autocomplete="new-password"')        # write-only, never pre-filled

    def test_a_value_echoed_by_the_helper_is_redacted(self):
        self.login()
        self.helper.fail["secret_set"] = "the value %s may not contain quotes" % KEY_VALUE
        self.c.post("/models/key", {"name": "KIMI_API_KEY", "value": KEY_VALUE})
        t = self.c.get("/models").text
        self.assertNotIn(KEY_VALUE, t)
        self.assertIn("***", t)
        self.assertNotIn(KEY_VALUE, self.audit_text())

    def test_key_form_rules(self):
        self.login()
        self.c.post("/models/key", {"name": "KIMI_API_KEY", "value": ""})
        self.assertIn(u"مقدار خالی است", self.c.get("/models").text)
        self.c.post("/models/key", {"name": "KIMI_API_KEY", "value": "abc def"})
        self.c.post("/models/key", {"name": "KIMI_API_KEY", "value": "abcdef", "remove": "1"})
        self.c.post("/models/key", {"name": "TELEGRAM_BOT_TOKEN", "value": "abcdef"})
        self.assertEqual(self.helper.calls_of("secret_set"), [])
        self.c.post("/models/key", {"name": "LLM_API_KEY", "remove": "1"})
        self.assertEqual(self.helper.calls_of("secret_set")[0][1], {"name": "LLM_API_KEY", "value": ""})
        self.assertIn(u"حذف شد", self.c.get("/models").text)

    def test_the_generic_key_is_bound_to_its_service_address(self):
        self.login()
        self.c.post("/models/key", {"name": "LLM_API_KEY", "value": KEY_VALUE, "base_url": ""})
        self.assertIn("LLM_API_KEY", self.c.get("/models").text)
        self.assertEqual(self.helper.calls_of("secret_set"), [])             # no address: refused here
        self.c.post("/models/key", {"name": "LLM_API_KEY", "value": KEY_VALUE, "base_url": "https://api.deepseek.com/v1"})
        self.assertEqual(self.helper.calls_of("secret_set")[0][1],
                         {"name": "LLM_API_KEY", "value": KEY_VALUE, "base_url": "https://api.deepseek.com/v1"})
        self.c.post("/models/key", {"name": "KIMI_API_KEY", "value": KEY_VALUE, "base_url": "https://evil.example"})
        self.assertEqual(self.helper.calls_of("secret_set")[1][1], {"name": "KIMI_API_KEY", "value": KEY_VALUE})
        self.assertNotIn(KEY_VALUE, self.audit_text())
        # the address field is pre-filled from a stage that already uses an "other" provider
        kimi = json.loads(KIMI_TEXT)
        kimi["news"]["base_url"] = "https://api.deepseek.com/v1"
        self.helper.responses["config_get"] = {"config": CONFIG_TEXT, "kimi": json.dumps(kimi)}
        self.assertIn('name="base_url" class="ltr" value="https://api.deepseek.com/v1"', self.c.get("/models").text)

    def test_passwords_hashes_totp_secrets_and_vpn_links_never_reach_the_audit_log(self):
        self.assertEqual(Client(self.app, ip="198.51.100.77").login(password="Typed-Wrong-Password-1").status, 403)
        self.login()
        r = self.c.post("/security/password", {"current": PW, "new1": NEW_PW, "new2": NEW_PW})
        self.assertEqual(r.status, 303)
        self.c.refresh_token()
        new_hash = self.helper.calls_of("panel_password_set")[0][1]["password_hash"]
        self.c.post("/security/totp/new")
        secret = self.app.sessions.get(self.c.cookies["__Host-bpsid"], self.now[0])["totp_pending"]
        self.c.post("/security/totp/enable", {"code": totp(secret, self.now[0])})
        self.c.refresh_token()
        self.assertTrue(self.app.totp_enabled)
        preview = self.c.post("/vpn/put", {"source": "link", "link": VPN_LINK, "do": "check"})
        self.assertNotIn(VPN_LINK, preview.text)
        self.assertNotIn("11111111-2222-3333-4444-555555555555", preview.text)
        self.c.post("/vpn/put", {"source": "pending", "do": "save"})
        self.c.post("/vpn/put", {"source": "text", "text": RAW_VPN, "do": "save"})
        text = self.audit_text()
        for s in (PW, NEW_PW, "Typed-Wrong-Password-1", new_hash, new_hash.split("$")[3], secret, VPN_LINK,
                  "RAWSECRET-UUID-9", "11111111-2222"):
            self.assertNotIn(s, text, s)
        cmds = [a.get("cmd") for a in self.audit("action")]
        for c in ("panel_password_set", "panel_totp_set", "vpn_put"):
            self.assertIn(c, cmds)


# ----------------------------------------------------------------------------------------------- models

class TestModels(PanelCase):
    def test_models_page_shows_the_stages_and_the_keys(self):
        self.login()
        t = self.c.get("/models").text
        for s in ("kimi-k3", "kimi-k2.6", "https://api.moonshot.ai/v1", "KIMI_API_KEY", "OPENROUTER_API_KEY",
                  "LLM_API_KEY", u"ثبت شده، <bdi dir=\"ltr\">32</bdi> نویسه", u"ثبت نشده"):
            self.assertIn(s, t, s)
        self.assertNotIn('name="key_name"', t)                             # the key follows the provider

    def test_listing_models_derives_the_key_and_links_each_model(self):
        self.login()
        r = self.c.post("/models/list", {"provider": "openrouter", "base_url": "", "q": ""})
        self.assertEqual(r.status, 200)
        cmd, args, actor = self.helper.calls_of("models")[0]
        self.assertEqual(args, {"base_url": "https://openrouter.ai/api/v1", "provider": "openrouter"})
        self.assertEqual(actor, {"user": USER, "ip": IP})
        self.assertIn("moonshotai/kimi-k2.6", r.text)
        self.assertIn("OPENROUTER_API_KEY", r.text)
        self.assertIn("model=moonshotai%2Fkimi-k2.6", r.text)
        self.assertIn("price_in_per_m=0.95", r.text)
        filtered = self.c.post("/models/list", {"provider": "openrouter", "q": "K3"})
        self.assertNotIn("moonshotai/kimi-k2.6", filtered.text.split(u"انتخاب مدل")[0])
        bad = self.c.post("/models/list", {"provider": "openai", "base_url": ""})
        self.assertEqual(bad.status, 400)
        self.assertEqual(len(self.helper.calls_of("models")), 2)

    def test_a_picked_model_prefills_the_form(self):
        self.login()
        q = urlencode({"stage": "news", "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                       "model": "moonshotai/kimi-k2.6", "price_in_per_m": "0.95", "price_out_per_m": "4"})
        t = self.c.get("/models", q).text
        self.assertIn('name="model" class="ltr" value="moonshotai/kimi-k2.6"', t)
        self.assertIn('value="0.95"', t)
        self.assertIn('<option value="news" selected>', t)
        self.assertIn('<option value="openrouter" selected>', t)

    def test_model_set_check_then_save(self):
        self.login()
        form = {"stage": "llm", "provider": "moonshot", "base_url": "", "model": "kimi-k4", "reasoning_effort": "high",
                "price_in_per_m": "2.5", "price_out_per_m": "12", "price_cached_in_per_m": "", "do": "check"}
        r = self.c.post("/models/set", form)
        self.assertEqual(r.status, 200)
        args = self.helper.calls_of("model_set")[0][1]
        self.assertEqual(args, {"stage": "llm", "provider": "moonshot", "base_url": "https://api.moonshot.ai/v1",
                                "model": "kimi-k4", "reasoning_effort": "high", "dry_run": True,
                                "prices": {"price_in_per_m": 2.5, "price_out_per_m": 12.0, "price_cached_in_per_m": 2.5}})
        for s in ("a warning", "llm.model", "kimi-k4", 'class="diff"', u"هنوز چیزی ذخیره نشده"):
            self.assertIn(s, r.text, s)
        form["do"] = "save"
        r = self.c.post("/models/set", form)
        self.assertEqual((r.status, r.header("Location")), (303, "/models"))
        self.assertFalse(self.helper.calls_of("model_set")[1][1]["dry_run"])
        t = self.c.get("/models").text
        self.assertIn(u"ذخیره شد", t)
        self.assertIn('<a href="/apply">', t)
        line = [a for a in self.audit("action") if a["cmd"] == "model_set"][-1]
        self.assertEqual((line["model"], line["dry_run"], line["result"]), ("kimi-k4", False, "ok"))

    def test_model_set_input_is_checked_before_the_helper(self):
        self.login()
        base = {"stage": "llm", "provider": "moonshot", "model": "kimi-k4", "do": "check"}
        for bad in ({"base_url": "http://api.moonshot.ai/v1"}, {"model": ""}, {"model": "a b"}, {"stage": "x"},
                    {"reasoning_effort": "extreme"}, {"price_in_per_m": "2"}, {"price_in_per_m": "x", "price_out_per_m": "1"},
                    {"price_in_per_m": "-1", "price_out_per_m": "1"}, {"provider": "evil"},
                    {"provider": "openai", "base_url": ""}):
            form = dict(base)
            form.update(bad)
            self.assertEqual(self.c.post("/models/set", form).status, 400, bad)
        self.assertEqual(self.helper.calls_of("model_set"), [])


# ----------------------------------------------------------------------------------------------- JSON editors

class TestSettingsEditors(PanelCase):
    def test_check_shows_problems_warnings_and_diff(self):
        self.login()
        self.helper.responses["config_put"] = lambda a: put_result(a, problems=["unknown key <ladderz>"], written=False,
                                                                  confirm_needed=False)
        r = self.c.post("/settings", {"file": "kimi", "text": '{\r\n  "llm": {}\r\n}\r\n', "do": "check"})
        self.assertEqual(r.status, 200)
        args = self.helper.calls_of("config_put")[0][1]
        self.assertEqual(args, {"file": "kimi", "text": '{\n  "llm": {}\n}\n', "dry_run": True})
        self.assertIn("unknown key &lt;ladderz&gt;", r.text)
        self.assertIn("a warning", r.text)
        self.assertIn('class="diff"', r.text)
        self.assertIn('>\n{\n  &quot;llm&quot;: {}\n}\n</textarea>', r.text)          # the edited text is kept

    def test_a_save_that_needs_confirmation_links_to_apply(self):
        self.login()
        r = self.c.post("/settings", {"file": "config", "text": CONFIG_TEXT, "do": "save"})
        self.assertEqual((r.status, r.header("Location")), (303, "/settings"))
        self.assertFalse(self.helper.calls_of("config_put")[0][1]["dry_run"])
        t = self.c.get("/settings").text
        self.assertIn(u"ذخیره شد", t)
        self.assertIn('<a href="/apply">', t)
        self.assertIn("kimi.json.20260928.before-panel", t)

    def test_bad_requests(self):
        self.login()
        self.assertEqual(self.c.post("/settings", {"file": "notify", "text": "{}", "do": "save"}).status, 400)
        self.assertEqual(self.c.post("/settings", {"file": "kimi", "text": "  ", "do": "save"}).status, 400)
        self.assertEqual(self.helper.calls_of("config_put"), [])


# ----------------------------------------------------------------------------------------------- trade form

class TestTradeForm(PanelCase):
    def current_form(self):
        cdoc, kdoc = ps.load_docs(CONFIG_TEXT, KIMI_TEXT)
        vals = ps.form_values(cdoc, kdoc)
        return dict((k, v) for k, v in vals.items() if not (ps.BY_KEY[k].kind == "bool" and v != "on"))

    @staticmethod
    def change(changes, key):
        f = ps.BY_KEY[key]
        return [c for c in changes if c["file"] == f.file and tuple(c["path"]) == f.path][0]["value"]

    def test_the_form_has_every_field_with_its_value_and_help(self):
        self.login()
        t = self.c.get("/trade").text
        for f in ps.FIELDS:
            self.assertIn('name="%s"' % f.key, t, f.key)
        for gid, title, intro in ps.GROUPS:
            self.assertIn('id="g-%s"' % gid, t)
            self.assertIn(pw.esc(title.fa), t)                           # the Persian page
        self.assertRegex(t, r'name="config:risk\.max_drawdown" value="50"')
        self.assertRegex(t, r'name="config:ladder\.enabled" value="on" checked>')
        self.assertIn('<textarea id="f-kimi-brain-extra_instructions" name="kimi:brain.extra_instructions" '
                      'class="ltr short" dir="ltr" rows="6" maxlength="4000">\nOWNER', t)
        self.assertIn('<details class="more"><summary>', t)
        self.assertIn("Circuit breaker", t)                                # help_en from config.example.json
        self.assertIn('name="action" value="preview"', t)
        self.assertIn('name="action" value="save"', t)

    def test_an_unknown_current_choice_is_kept_not_silently_replaced(self):
        self.login()
        kimi = json.loads(KIMI_TEXT)
        kimi["brain"]["slot_reasoning_effort"] = "medium"                  # not one of the form's choices
        self.helper.responses["config_get"] = {"config": CONFIG_TEXT, "kimi": json.dumps(kimi)}
        t = self.c.get("/trade").text
        self.assertRegex(t, r'<select name="kimi:brain\.slot_reasoning_effort" id="[^"]+"><option value="medium" selected>')
        cdoc, kdoc = ps.load_docs(CONFIG_TEXT, json.dumps(kimi))
        form = dict((k, v) for k, v in ps.form_values(cdoc, kdoc).items()
                    if not (ps.BY_KEY[k].kind == "bool" and v != "on"))
        form["action"] = "save"
        r = self.c.post("/trade", form)                                     # posted back as it was shown
        self.assertEqual(r.status, 400)
        self.assertEqual(self.helper.calls_of("settings_set"), [])

    def test_a_percent_change_is_previewed(self):
        self.login()
        form = self.current_form()
        form["config:risk.max_drawdown"] = "30"
        form["action"] = "preview"
        r = self.c.post("/trade", form)
        self.assertEqual(r.status, 200)
        cmd, args, actor = self.helper.calls_of("settings_set")[0]
        self.assertTrue(args["dry_run"])
        self.assertAlmostEqual(self.change(args["changes"], "config:risk.max_drawdown"), 0.3)
        self.assertEqual(len(args["changes"]), len(ps.FIELDS))
        self.assertIn("config:risk.max_drawdown", r.text)
        self.assertIn(u"پیش‌نمایش", r.text)
        self.assertRegex(r.text, r'name="config:risk\.max_drawdown" value="30"')      # the posted value stays

    def test_an_unchecked_box_is_false(self):
        self.login()
        form = self.current_form()
        form.pop("config:ladder.enabled")
        form["action"] = "preview"
        self.c.post("/trade", form)
        changes = self.helper.calls_of("settings_set")[0][1]["changes"]
        self.assertIs(self.change(changes, "config:ladder.enabled"), False)
        self.assertIs(self.change(changes, "config:exits.enabled"), True)

    def test_a_parse_error_re_renders_the_posted_value_with_the_error(self):
        self.login()
        form = self.current_form()
        form["config:risk.max_drawdown"] = "abc<x>"
        form.pop("config:ladder.enabled")
        form["action"] = "save"
        r = self.c.post("/trade", form)
        self.assertEqual(r.status, 400)
        self.assertEqual(self.helper.calls_of("settings_set"), [])
        self.assertIn('name="config:risk.max_drawdown" value="abc&lt;x&gt;"', r.text)
        self.assertIn('class="field has-err"', r.text)
        self.assertIn('<p class="field-err">', r.text)
        self.assertRegex(r.text, r'name="config:ladder\.enabled" value="on">')          # unchecked stays unchecked
        self.assertNotIn("<x>", r.text)

    def test_a_save_that_needs_confirmation_shows_the_apply_form(self):
        self.login()
        form = self.current_form()
        form["config:risk.max_drawdown"] = "30"
        form["action"] = "save"
        r = self.c.post("/trade", form)
        self.assertEqual((r.status, r.header("Location")), (303, "/trade"))
        self.assertFalse(self.helper.calls_of("settings_set")[0][1]["dry_run"])
        t = self.c.get("/trade").text
        self.assertIn(u"قبلی", t)                                          # runs with the OLD settings until applied
        self.assertIn("confirm LIVE trading with settings digest abc123", t)
        self.assertRegex(t, r'<form method="post" action="/apply"><input type="hidden" name="csrf" value="[^"]+">')
        self.assertIn('name="phrase"', t)
        line = [a for a in self.audit("action") if a["cmd"] == "settings_set"][-1]
        self.assertEqual(line["changed"], ["config:risk.max_drawdown"])
        self.assertNotIn("0.3", json.dumps(line))                           # keys only, no values
        self.assertEqual(line["result"], "ok")

    def test_hostile_strings_in_the_result_are_escaped(self):
        self.login()
        self.helper.responses["settings_set"] = lambda a: put_result(
            a, problems=[HOSTILE], warnings=[HOSTILE], diff=HOSTILE, written=False,
            changed=[{"file": "config", "path": HOSTILE, "old": HOSTILE, "new": {"x": HOSTILE}}])
        form = self.current_form()
        form["action"] = "preview"
        self.assertEscaped(self.c.post("/trade", form).text)

    def test_a_helper_error_keeps_the_posted_values(self):
        self.login()
        self.helper.fail["settings_set"] = "another change is still running"
        form = self.current_form()
        form["config:risk.max_drawdown"] = "31"
        form["action"] = "save"
        r = self.c.post("/trade", form)
        self.assertEqual(r.status, 200)
        self.assertIn("another change is still running", r.text)
        self.assertRegex(r.text, r'name="config:risk\.max_drawdown" value="31"')


# ----------------------------------------------------------------------------------------------- apply, services

class TestApplyAndServices(PanelCase):
    def test_apply_page(self):
        self.login()
        t = self.c.get("/apply").text
        self.assertIn("confirm LIVE trading with settings digest abc123", t)
        self.assertIn('name="phrase"', t)
        self.assertIn('<input type="checkbox" class="switch" name="start" value="1" checked>', t)
        self.assertIn('action="/apply/check"', t)
        self.assertLess(t.index('action="/apply/check"'), t.index('name="phrase"'))

    def test_a_wrong_phrase_never_reaches_the_helper(self):
        self.login()
        for phrase in ("i accept the risk", "I ACCEPT", ""):
            self.assertEqual(self.c.post("/apply", {"phrase": phrase, "start": "1"}).status, 400)
        self.assertEqual(self.helper.calls_of("apply_live"), [])
        self.assertEqual([a["error"] for a in self.audit("action") if a["cmd"] == "apply_live"], ["phrase mismatch"] * 3)

    def test_apply_with_and_without_start(self):
        self.login()
        r = self.c.post("/apply", {"phrase": " I ACCEPT THE RISK ", "start": "1"})
        self.assertEqual((r.status, r.header("Location")), (303, "/apply"))
        self.assertEqual(self.helper.calls_of("apply_live")[0][1], {"phrase": "I ACCEPT THE RISK", "start": True})
        t = self.c.get("/apply").text
        for s in ("LIVE_CONFIRMED written", "bot healthy after apply", u"تنظیمات تأیید و اعمال شد"):
            self.assertIn(s, t)
        self.c.post("/apply", {"phrase": "I ACCEPT THE RISK"})
        self.assertEqual(self.helper.calls_of("apply_live")[1][1], {"phrase": "I ACCEPT THE RISK", "start": False})

    def test_a_failed_apply_is_shown_as_failed(self):
        self.login()
        self.helper.responses["apply_live"] = {"ok": False, "steps": [{"step": "check", "ok": False, "output": "bad"}],
                                               "health": "", "bot_state": "inactive"}
        self.c.post("/apply", {"phrase": "I ACCEPT THE RISK", "start": "1"})
        t = self.c.get("/apply").text
        self.assertIn(u"اعمال تنظیمات کامل نشد", t)
        self.assertEqual([a["result"] for a in self.audit("action") if a["cmd"] == "apply_live"], ["error"])

    def test_the_server_check_button(self):
        self.login()
        r = self.c.post("/apply/check")
        self.assertEqual(r.status, 200)
        self.assertIn("check passed: 0 problems", r.text)
        self.helper.responses["check"] = {"ok": False, "text": "2 problems"}
        self.assertIn(u"بررسی سرور مشکل پیدا کرد", self.c.post("/apply/check").text)

    def test_confirm_show_not_ok_is_explained(self):
        self.login()
        self.helper.responses["confirm_show"] = {"ok": False, "text": "config.json has problems"}
        t = self.c.get("/apply").text
        self.assertIn(u"در حال حاضر قابل تأیید نیست", t)
        self.assertIn("config.json has problems", t)

    def test_service_buttons(self):
        self.login()
        r = self.c.post("/service", {"unit": "bitpin-bot", "action": "restart", "back": "/"})
        self.assertEqual((r.status, r.header("Location")), (303, "/"))
        self.assertEqual(self.helper.calls_of("service")[0][1], {"unit": "bitpin-bot", "action": "restart"})
        self.assertIn("bitpin-bot restart: done", self.c.get("/").text)
        r = self.c.post("/service", {"unit": "xray-tunnel", "action": "stop", "back": "https://evil.example/"})
        self.assertEqual(r.header("Location"), "/")
        self.c.post("/service", {"unit": "sshd", "action": "stop", "back": "/vpn"})
        self.c.post("/service", {"unit": "bitpin-bot", "action": "kill", "back": "/vpn"})
        self.assertEqual(len(self.helper.calls_of("service")), 2)

    def test_a_refused_bot_start_links_to_apply(self):
        self.login()
        self.helper.fail["service"] = "the bot would not start: settings not confirmed - apply the settings first"
        self.c.post("/service", {"unit": "bitpin-bot", "action": "start", "back": "/"})
        t = self.c.get("/").text
        self.assertIn("the bot would not start", t)
        self.assertIn(u'<a href="/apply">رفتن به اعمال تنظیمات</a>', t)

    def test_logs(self):
        self.login()
        self.c.get("/logs", "unit=bitpin-bot-panel&lines=9999")
        self.assertEqual(self.helper.calls_of("logs")[0][1], {"unit": "bitpin-bot-panel", "lines": 500})
        self.c.get("/logs", "unit=xray-tunnel&lines=abc")
        self.assertEqual(self.helper.calls_of("logs")[1][1], {"unit": "xray-tunnel", "lines": 200})
        self.assertEqual(self.c.get("/logs", "unit=sshd").status, 400)
        self.assertEqual(len(self.helper.calls_of("logs")), 2)


# ----------------------------------------------------------------------------------------------- VPN

class TestVpn(PanelCase):
    def test_the_page_shows_the_masked_summary(self):
        self.login()
        t = self.c.get("/vpn").text
        for s in ("vpn.example.net", "1111...5555", "http://127.0.0.1:1081", "/opt/xray/config.json", "http-in",
                  'name="unit" value="xray-tunnel"', 'action="/vpn/test"', 'action="/vpn/raw"'):
            self.assertIn(s, t, s)
        self.assertEqual(self.helper.calls_of("vpn_get")[0][1], {"raw": False})
        self.assertNotIn("RAWSECRET", t)

    def test_a_link_is_previewed_then_applied_from_the_session(self):
        self.login()
        r = self.c.post("/vpn/put", {"source": "link", "link": " %s " % VPN_LINK, "do": "check"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.helper.calls_of("vpn_put")[0][1], {"link": VPN_LINK, "dry_run": True})
        self.assertNotIn(VPN_LINK, r.text)
        self.assertIn("Configuration OK.", r.text)
        self.assertIn('name="source" value="pending"', r.text)
        r = self.c.post("/vpn/put", {"source": "pending", "do": "save", "keep_on_failure": "1"})
        self.assertEqual((r.status, r.header("Location")), (303, "/vpn"))
        self.assertEqual(self.helper.calls_of("vpn_put")[1][1], {"link": VPN_LINK, "dry_run": False, "keep_on_failure": True})
        t = self.c.get("/vpn").text
        self.assertIn(u"نوشته شد", t)
        self.assertIn("xray-config.json.1.before-panel", t)
        self.c.post("/vpn/put", {"source": "pending", "do": "save"})         # used up
        self.assertEqual(len(self.helper.calls_of("vpn_put")), 2)
        self.assertIn(u"پیش‌نمایشی در انتظار نیست", self.c.get("/vpn").text)

    def test_a_pending_link_expires(self):
        self.login()
        self.c.post("/vpn/put", {"source": "link", "link": VPN_LINK, "do": "check"})
        self.now[0] += pw.VPN_PENDING_SECONDS + 1
        self.c.get("/logs")                                                # keep the session alive
        self.c.post("/vpn/put", {"source": "pending", "do": "save"})
        self.assertEqual(len(self.helper.calls_of("vpn_put")), 1)

    def test_invalid_links_never_reach_the_helper(self):
        self.login()
        for link in ("", "http://example.com", "vless://a b", "javascript:alert(1)", "vmess://" + "A" * 20000):
            self.assertEqual(self.c.post("/vpn/put", {"source": "link", "link": link, "do": "check"}).status, 400, link[:20])
        self.assertEqual(self.helper.calls_of("vpn_put"), [])

    def test_the_raw_editor(self):
        self.login()
        r = self.c.post("/vpn/raw")
        self.assertEqual(self.helper.calls_of("vpn_get")[0][1], {"raw": True})
        self.assertIn("RAWSECRET-UUID-9", r.text)
        self.assertIn("&quot;outbounds&quot;", r.text)
        self.assertIn(u"اطلاعات محرمانهٔ اتصال VPN", r.text)
        self.assertIn('name="keep_on_failure"', r.text)
        line = [a for a in self.audit("action") if a["cmd"] == "vpn_get"][0]
        self.assertTrue(line["raw"])
        r = self.c.post("/vpn/put", {"source": "text", "text": RAW_VPN, "do": "check"})
        self.assertEqual(self.helper.calls_of("vpn_put")[0][1], {"text": RAW_VPN, "dry_run": True})
        self.assertIn("RAWSECRET-UUID-9", r.text)                          # still in the editor being checked
        self.c.post("/vpn/put", {"source": "text", "text": RAW_VPN, "do": "save"})
        self.assertEqual(self.helper.calls_of("vpn_put")[1][1], {"text": RAW_VPN, "dry_run": False, "keep_on_failure": False})

    def test_a_rolled_back_change_is_reported(self):
        self.login()

        def rolled(a):
            r = vpn_put(a)
            r.update(rolled_back=True, proxy_test=[{"target": "api.telegram.org:443", "ok": False, "ms": 12000,
                                                   "error": "timed out"}],
                     proxy_test_after_rollback=[{"target": "api.telegram.org:443", "ok": True, "ms": 90, "error": ""}])
            return r
        self.helper.responses["vpn_put"] = rolled
        self.c.post("/vpn/put", {"source": "text", "text": RAW_VPN, "do": "save"})
        t = self.c.get("/vpn").text
        self.assertIn(u"تنظیمات قبلی برگردانده شد", t)
        self.assertIn("timed out", t)
        self.assertIn(u"آزمایش پس از برگرداندن", t)

    def test_the_proxy_test(self):
        self.login()
        t = self.c.post("/vpn/test").text
        for s in ("http://127.0.0.1:1081", "api.telegram.org", "timed out", "210"):
            self.assertIn(s, t)
        self.assertEqual([a["cmd"] for a in self.audit("action")], ["vpn_test"])


# ----------------------------------------------------------------------------------------------- security page

class TestSecurityPage(PanelCase):
    def test_changing_the_password_ends_every_other_session(self):
        self.login()
        other = self.login(Client(self.app, ip="198.51.100.30"))
        r = self.c.post("/security/password", {"current": PW, "new1": NEW_PW, "new2": NEW_PW})
        self.assertEqual((r.status, r.header("Location")), (303, "/security"))
        self.assertTrue(any(v.startswith("__Host-bpsid=") for v in r.headers_all("Set-Cookie")))
        new_hash = self.helper.calls_of("panel_password_set")[0][1]["password_hash"]
        self.assertTrue(verify_password(NEW_PW, new_hash))
        with open(self.cfg_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["password_hash"], new_hash)
        self.assertEqual(other.get("/").status, 303)                        # the other session is gone
        page = self.c.get("/security")
        self.assertEqual(page.status, 200)
        self.assertIn(u"رمز عبور عوض شد", page.text)
        self.c.refresh_token()
        self.c.post("/logout")
        self.assertEqual(self.c.login().status, 403)
        self.login(password=NEW_PW)

    def test_the_file_is_the_truth_after_a_change(self):
        self.login()
        on_disk = hash_password("Disk-Wins-Pass-99", iterations=1000)

        def helper_writes_something_else(a):
            with open(self.cfg_path, encoding="utf-8") as f:
                doc = json.load(f)
            doc["password_hash"] = on_disk
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            return {}
        self.helper.panel_conf = None
        self.helper.responses["panel_password_set"] = helper_writes_something_else
        self.c.post("/security/password", {"current": PW, "new1": NEW_PW, "new2": NEW_PW})
        self.assertEqual(Client(self.app, ip="198.51.100.31").login(password=NEW_PW).status, 403)
        self.login(Client(self.app, ip="198.51.100.32"), password="Disk-Wins-Pass-99")

    def test_password_change_rules(self):
        self.login()
        self.c.post("/security/password", {"current": PW, "new1": NEW_PW, "new2": NEW_PW + "x"})
        self.assertIn(u"یکی نیستند", self.c.get("/security").text)
        self.c.post("/security/password", {"current": PW, "new1": "short", "new2": "short"})
        self.assertIn(u"۱۲", self.c.get("/security").text)
        self.c.post("/security/password", {"current": PW, "new1": PW, "new2": PW})
        self.assertIn(u"فرق داشته باشد", self.c.get("/security").text)
        self.c.post("/security/password", {"current": "not-my-password", "new1": NEW_PW, "new2": NEW_PW})
        self.assertIn(u"رمز فعلی یا کد نادرست است", self.c.get("/security").text)
        self.assertEqual(self.helper.calls_of("panel_password_set"), [])

    def test_wrong_current_passwords_count_toward_the_lock(self):
        self.login()
        for _ in range(5):
            self.c.post("/security/password", {"current": "guess", "new1": NEW_PW, "new2": NEW_PW})
        self.assertEqual(len(self.audit("login_locked")), 1)
        self.c.post("/security/password", {"current": PW, "new1": NEW_PW, "new2": NEW_PW})
        self.assertEqual(self.helper.calls_of("panel_password_set"), [])     # locked even with the right one
        self.assertEqual(Client(self.app).login().status, 429)

    def test_turning_2fa_on_and_off(self):
        self.login()
        self.assertEqual(self.c.post("/security/totp/new").status, 303)
        t = self.c.get("/security").text
        m = re.search(r"otpauth://totp/bitpin-bot:owner\?secret=([A-Z2-7]{32})&amp;issuer=bitpin-bot", t)
        self.assertTrue(m, t[-3000:])
        secret = m.group(1)
        self.assertIn(" ".join(secret[i:i + 4] for i in range(0, 32, 4)), t)
        self.assertFalse(self.app.totp_enabled)
        bad = "%06d" % ((int(totp(secret, self.now[0])) + 1) % 1000000)
        self.c.post("/security/totp/enable", {"code": bad})
        self.assertEqual(self.helper.calls_of("panel_totp_set"), [])
        code = totp(secret, self.now[0])
        r = self.c.post("/security/totp/enable", {"code": code})
        self.assertEqual(r.status, 303)
        self.assertEqual(self.helper.calls_of("panel_totp_set")[0][1], {"totp_secret": secret})
        self.assertTrue(self.app.totp_enabled)
        t = self.c.get("/security").text
        self.assertIn(u"ورود دومرحله‌ای روشن شد", t)
        self.assertNotIn(secret, t)                                        # never shown again
        with open(self.cfg_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["totp_secret"], secret)
        self.c.refresh_token()
        self.c.post("/logout")
        login_page = self.c.get("/login").text
        self.assertIn('name="code"', login_page)
        self.assertEqual(self.c.login().status, 403)
        self.assertEqual(self.c.login(code=code).status, 403)              # the enabling code was used up
        self.now[0] += 30
        self.login(code=totp(secret, self.now[0]))
        self.c.post("/security/totp/disable", {"code": totp(secret, self.now[0])})   # replay: refused
        self.assertTrue(self.app.totp_enabled)
        self.now[0] += 30
        r = self.c.post("/security/totp/disable", {"code": totp(secret, self.now[0])})
        self.assertEqual(r.status, 303)
        self.assertEqual(self.helper.calls_of("panel_totp_set")[1][1], {"totp_secret": None})
        self.assertFalse(self.app.totp_enabled)
        self.assertNotIn('name="code"', Client(self.app).get("/login").text)

    def test_the_audit_table(self):
        self.login()
        self.c.post("/service", {"unit": "bitpin-bot", "action": "restart"})
        t = self.c.get("/security").text
        for s in (u"ورود موفق", u"عملیات", "cmd=service", "unit=bitpin-bot", IP):
            self.assertIn(s, t)


# ----------------------------------------------------------------------------------------------- helper client

class FakeSock(object):
    def __init__(self, chunks, exc=None):
        self.chunks = list(chunks)
        self.exc = exc
        self.sent = b""
        self.shut = False
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        self.sent += data

    def shutdown(self, how):
        self.shut = True

    def recv(self, n):
        if self.exc is not None:
            raise self.exc
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


class ScriptedClient(HelperClient):
    def __init__(self, sock, **kw):
        HelperClient.__init__(self, "/run/test.sock", **kw)
        self.sock = sock
        self.timeouts = []

    def _connect(self, timeout):
        self.timeouts.append(timeout)
        return self.sock


class TestHelperClient(unittest.TestCase):
    def test_request_line_and_answer(self):
        sock = FakeSock([b'{"ok": true, "data": {"te', b'xt": "hi"}}\n'])
        cl = ScriptedClient(sock)
        self.assertEqual(cl.call("health", actor={"user": USER, "ip": IP}), {"text": "hi"})
        line = sock.sent
        self.assertTrue(line.endswith(b"\n") and line.count(b"\n") == 1)
        self.assertEqual(json.loads(line.decode()), {"cmd": "health", "args": {}, "actor": {"user": USER, "ip": IP}})
        self.assertTrue(sock.shut and sock.closed)
        sock2 = FakeSock([b'{"ok": true, "data": {}}\n'])
        ScriptedClient(sock2).call("logs", unit="bitpin-bot", lines=5)
        self.assertEqual(json.loads(sock2.sent.decode()), {"cmd": "logs", "args": {"unit": "bitpin-bot", "lines": 5}})

    def test_timeouts_per_command(self):
        cl = HelperClient("/x")
        # v3.1 integration: everything that may run a bot tool, wait for the change lock or stop a service
        for cmd in ("apply_live", "vpn_put", "check", "models", "status", "health", "confirm_show", "config_put",
                    "settings_set", "service", "vpn_test"):
            self.assertEqual(cl.timeout_for(cmd), 300.0, cmd)
        for cmd in ("config_get", "secrets_status", "vpn_get", "audit_notify"):
            self.assertEqual(cl.timeout_for(cmd), 60.0, cmd)
        sc = ScriptedClient(FakeSock([b'{"ok": true, "data": {}}\n']))
        sc.call("apply_live", phrase="x")
        self.assertEqual(sc.timeouts, [300.0])

    def test_refusals_and_broken_answers_raise_helper_error(self):
        cases = [([b'{"ok": false, "error": "unit must be one of ..."}\n'], "unit must be one of"),
                 ([b'{"ok": false}\n'], "no reason"),
                 ([b"not json\n"], "not valid JSON"),
                 ([b"[1, 2]\n"], "not a JSON object"),
                 ([], "without an answer"),
                 ([b"\xff\xfe\n"], "not valid JSON")]
        for chunks, text in cases:
            with self.assertRaises(HelperError) as cm:
                ScriptedClient(FakeSock(chunks)).call("status")
            self.assertIn(text, str(cm.exception))
        big = [b"x" * 65536] * 70
        with self.assertRaises(HelperError) as cm:
            ScriptedClient(FakeSock(big)).call("status")
        self.assertIn("larger than", str(cm.exception))
        with self.assertRaises(HelperError) as cm:
            ScriptedClient(FakeSock([], exc=socket.timeout())).call("config_get")
        self.assertIn("did not answer within 60 s", str(cm.exception))
        with self.assertRaises(HelperError) as cm:
            ScriptedClient(FakeSock([], exc=ConnectionResetError(104, "Connection reset by peer"))).call("status")
        self.assertIn("connection to the helper failed", str(cm.exception))
        with self.assertRaises(HelperError):
            ScriptedClient(FakeSock([b'{"ok": true, "data": {}}\n'])).call("config_put", text="x" * (5 * 1024 * 1024))
        self.assertEqual(ScriptedClient(FakeSock([b'{"ok": true, "data": [1]}\n'])).call("status"), {})

    def test_no_af_unix_is_a_clear_error(self):
        with mock.patch.object(pw.socket, "AF_UNIX", None, create=True):
            with self.assertRaises(HelperError) as cm:
                HelperClient("/run/bitpin-panel/helper.sock").call("status")
        self.assertIn("AF_UNIX", str(cm.exception))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "no AF_UNIX sockets on this platform")
    def test_a_real_unix_socket_round_trip(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "h.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        seen = []

        def serve():
            conn, _ = srv.accept()
            f = conn.makefile("rb")
            seen.append(json.loads(f.readline().decode()))
            conn.sendall(b'{"ok": true, "data": {"state": "active"}}\n')
            conn.close()
            srv.close()
        t = threading.Thread(target=serve)
        t.daemon = True
        t.start()
        self.assertEqual(HelperClient(path).call("status", actor={"user": USER, "ip": IP}), {"state": "active"})
        t.join(5)
        self.assertEqual(seen[0]["cmd"], "status")
        with self.assertRaises(HelperError) as cm:
            HelperClient(os.path.join(d, "missing.sock")).call("status")
        self.assertIn("cannot connect", str(cm.exception))


# ----------------------------------------------------------------------------------------------- the server

class TestPanelSegments(unittest.TestCase):
    """3.1.1: small TCP segments on the listening socket (a VPN path that drops full-size packets)."""

    @unittest.skipUnless(hasattr(socket, "TCP_MAXSEG") and sys.platform.startswith("linux"), "Linux TCP_MAXSEG")
    def test_the_listening_socket_uses_small_segments(self):
        srv = load_module("panel_server_mss", os.path.join("scripts", "panel_server.py"))
        server = srv.PanelServer(("127.0.0.1", 0), None, None)
        try:
            mss = server.socket.getsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG)
            self.assertLessEqual(mss, srv.PANEL_MSS)
        finally:
            server.server_close()

    def test_the_segment_size_is_small_but_sane(self):
        srv = load_module("panel_server_mss2", os.path.join("scripts", "panel_server.py"))
        self.assertTrue(536 <= srv.PANEL_MSS <= 1400)


class TestPanelServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = load_module("panel_server_under_test", os.path.join("scripts", "panel_server.py"))

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="panel_srv_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_body_length_rules(self):
        from email.message import Message

        def headers(**kw):
            m = Message()
            for k, v in kw.items():
                m[k.replace("_", "-")] = v
            return m
        bl = self.srv.body_length
        self.assertEqual(bl("GET", headers()), (0, None))
        self.assertEqual(bl("POST", headers(Content_Length="12")), (12, None))
        self.assertEqual(bl("POST", headers())[1][0], 411)
        self.assertEqual(bl("POST", headers(Content_Length=str(5 * 1024 * 1024 + 1)))[1][0], 413)
        self.assertEqual(bl("POST", headers(Content_Length=str(5 * 1024 * 1024))), (5 * 1024 * 1024, None))
        for bad in ("-1", "1e3", "abc", "", " 12x"):
            self.assertEqual(bl("POST", headers(Content_Length=bad))[1][0], 400, bad)
        self.assertEqual(bl("POST", headers(Transfer_Encoding="chunked"))[1][0], 501)
        two = Message()
        two["Content-Length"] = "5"
        two["Content-Length"] = "6"
        self.assertEqual(bl("POST", two)[1][0], 400)

    def test_client_ip(self):
        ci = self.srv.client_ip
        self.assertEqual(ci("10.0.0.1", "1.2.3.4", False), "10.0.0.1")
        self.assertEqual(ci("127.0.0.1", "6.6.6.6, 203.0.113.9", True), "203.0.113.9")
        self.assertEqual(ci("127.0.0.1", "garbage", True), "127.0.0.1")
        self.assertEqual(ci("127.0.0.1", None, True), "127.0.0.1")

    def run_main(self, cfg_or_text, extra=()):
        path = os.path.join(self.dir, "panel.json")
        if cfg_or_text is not None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(cfg_or_text if isinstance(cfg_or_text, str) else json.dumps(cfg_or_text))
        err = []
        with mock.patch.object(self.srv.sys, "stderr") as fake_err:
            fake_err.write.side_effect = err.append
            code = self.srv.main(["--config", path] + list(extra))
        return code, "".join(err)

    def good_cfg(self):
        cert, key = os.path.join(self.dir, "cert.pem"), os.path.join(self.dir, "key.pem")
        for p in (cert, key):
            with open(p, "w") as f:
                f.write("not a real pem\n")
        return {"bind": "127.0.0.1", "port": 8443, "tls_cert": cert, "tls_key": key, "username": USER,
                "password_hash": hash_password(PW, iterations=1000), "totp_secret": None, "allowed_hosts": [],
                "helper_socket": os.path.join(self.dir, "h.sock"), "audit_log": os.path.join(self.dir, "a.jsonl")}

    def test_clear_exits_for_a_missing_or_broken_config(self):
        code, err = self.run_main(None)
        self.assertEqual(code, 78)
        self.assertIn("does not exist", err)
        code, err = self.run_main("{not json")
        self.assertEqual((code, "not valid JSON" in err), (78, True))
        cfg = self.good_cfg()
        cfg["password_hash"] = ""
        code, err = self.run_main(cfg)
        self.assertEqual(code, 78)
        self.assertIn("password_hash is not set", err)
        cfg = self.good_cfg()
        cfg["tls_cert"] = os.path.join(self.dir, "missing.pem")
        code, err = self.run_main(cfg)
        self.assertEqual(code, 78)
        self.assertIn("does not exist", err)
        cfg = self.good_cfg()
        cfg.update(username="", totp_secret="!!", port="8443")
        code, err = self.run_main(cfg)
        for s in ("username is not set", "totp_secret", "port must be"):
            self.assertIn(s, err)
        code, err = self.run_main(self.good_cfg())                        # a file that is not a certificate
        self.assertEqual(code, 78)
        self.assertIn("TLS certificate", err)
        self.assertNotIn("pbkdf2", err)                                    # never the hash in the log

    def test_the_default_paths(self):
        self.assertEqual(self.srv.DEFAULT_CONFIG, "/etc/bitpin-bot-panel/panel.json")
        self.assertEqual(self.srv.MAX_BODY, 5 * 1024 * 1024)

    def test_the_real_https_server(self):
        exe = shutil.which("openssl")
        if not exe:
            self.skipTest("no openssl binary for a throwaway certificate")
        cfg = self.good_cfg()
        cert, key, cnf = (os.path.join(self.dir, n) for n in ("tls-cert.pem", "tls-key.pem", "openssl.cnf"))
        with open(cnf, "w") as f:
            f.write("[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = localhost\n")
        try:
            p = subprocess.run([exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                                "-days", "1", "-config", cnf], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            self.skipTest("openssl failed: %s" % e)
        if p.returncode != 0 or not (os.path.exists(cert) and os.path.exists(key)):
            self.skipTest("openssl could not make a certificate")
        cfg.update(tls_cert=cert, tls_key=key)
        app = PanelApp(cfg, FakeHelper(), sleep=lambda s: None, spawn=lambda fn: fn())
        server = self.srv.PanelServer(("127.0.0.1", 0), app, self.srv.make_ssl_context(cert, key))
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        t.daemon = True
        t.start()
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=30)
            conn.request("GET", "/login", headers={"Host": "localhost"})
            r = conn.getresponse()
            body = r.read()
            conn.close()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.getheader("Content-Security-Policy"), CSP)
            self.assertEqual(r.getheader("Strict-Transport-Security"), "max-age=31536000")
            self.assertEqual(r.getheader("Server"), "bitpin-panel")
            self.assertEqual(int(r.getheader("Content-Length")), len(body))
            self.assertIn(b'<html lang="en" dir="ltr">', body)                 # no cookie, no Accept-Language
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=30)
            conn.putrequest("POST", "/login", skip_host=True)
            conn.putheader("Host", "localhost")
            conn.putheader("Content-Type", "application/x-www-form-urlencoded")
            conn.putheader("Content-Length", str(6 * 1024 * 1024))
            conn.endheaders()
            r = conn.getresponse()
            r.read()
            conn.close()
            self.assertEqual(r.status, 413)
            self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
