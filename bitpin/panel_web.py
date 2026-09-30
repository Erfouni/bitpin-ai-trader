# -*- coding: utf-8 -*-
"""The management panel web app (v3.1; English and Persian since v3.2): a pure request handler, no sockets of its
own except the helper client.

    app = PanelApp(cfg, HelperClient(cfg["helper_socket"]), config_path="/etc/bitpin-bot-panel/panel.json")
    status, headers, body = app.handle(method, path, query, headers, body_bytes, client_ip)

scripts/panel_server.py serves it over HTTPS as the unprivileged user bitpin-panel. This process never sees an
API key, never writes a config file and never runs systemctl: everything privileged is one JSON line to the root
helper (scripts/panel_helper.py) over its UNIX socket; the helper checks every request again.

Security model (the panel is reachable from the internet):
* Login: username + password (PBKDF2, bitpin/panel_auth.py) + a TOTP code when 2FA is on. Every attempt goes
  through LoginLimiter (per-IP and global locks); credentials are checked one attempt at a time (a flood of
  parallel guesses gains nothing and cannot burn every CPU on PBKDF2); a failure answers the same page for any
  wrong field and takes at least MIN_FAIL_SECONDS. The login form carries a double-submit token (cookie
  __Host-bplogin = hidden field "lt"), so another site cannot post logins through the owner's browser.
* Session: cookie "__Host-bpsid=<sid>; Secure; HttpOnly; SameSite=Strict; Path=/", in memory only (a restart
  logs everybody out), idle and absolute expiry from the config.
* Every POST: Content-Type urlencoded, Origin (when sent) == https://<Host>, Sec-Fetch-Site (when sent)
  same-origin or none, and the session's CSRF token in the hidden field "csrf" - otherwise 403. GET never
  changes state (GET /lang only sets the language cookie); state-changing POSTs answer 303 to a GET page (a
  reload never repeats an action).
* Host header: only the configured allowed_hosts (empty = any). The security headers on every response.
* Audit log (JSON lines, audit_log in the config): logins and every POST action with the command and its
  result - never a secret value, a password, a hash, a VPN link / config or a TOTP secret.
* Every value shown is HTML-escaped; the CSP forbids inline scripts and styles (the panel has no JavaScript).

Look and languages (v3.2): a sidebar layout styled by bitpin/static/panel.css (served as
/static/panel.css?v=<hash>, the one file the panel reads besides panel.json) with inline SVG icons. Every text
is English in this file; bitpin/panel_i18n.py has the Persian translations. The language switch
(GET /lang?to=fa&next=/trade) sets the cookie __Host-bplang; without it the browser's Accept-Language decides.
"""
import base64
import binascii
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus, urlencode, urlsplit

from . import __version__
from .panel_auth import (PBKDF2_ITERATIONS, DeviceTrust, LoginLimiter, SessionStore, hash_password,
                         is_password_hash, load_device_secret, new_totp_secret, otpauth_uri, password_problems,
                         verify_password, verify_totp)
from .panel_i18n import LANG_NAMES, LANGS, N_, current, is_rtl, negotiate, reset_lang, set_lang, tr

log = logging.getLogger("bitpin.panel")

SESSION_COOKIE = "__Host-bpsid"
LOGIN_COOKIE = "__Host-bplogin"
DEVICE_COOKIE = "__Host-bpdev"         # a browser that logged in before: passes the global login lock
LANG_COOKIE = "__Host-bplang"          # the language the owner picked (en / fa)
LANG_MAX_AGE = 365 * 86400
MIN_FAIL_SECONDS = 0.5
APPLY_PHRASE = "I ACCEPT THE RISK"
SERVICE_UNITS = ("bitpin-bot", "bitpin-bot-notify", "xray-tunnel")
UNIT_ACTIONS = ("restart", "start", "stop")
LOG_UNITS = ("bitpin-bot", "bitpin-bot-notify", "xray-tunnel", "bitpin-bot-panel")
SECRET_NAMES = ("KIMI_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY", "KIMI_HTTPS_PROXY", "BITPIN_API_KEY",
                "BITPIN_SECRET_KEY")
PROVIDER_KEYS = (("moonshot", "KIMI_API_KEY"), ("openrouter", "OPENROUTER_API_KEY"), ("openai", "LLM_API_KEY"))
PROVIDER_URLS = (("moonshot", "https://api.moonshot.ai/v1"), ("openrouter", "https://openrouter.ai/api/v1"),
                 ("openai", ""))
PROVIDER_LABELS = {"auto": N_("Detect from the address"), "moonshot": "Moonshot (Kimi)", "openrouter": "OpenRouter",
                   "openai": N_("Other (OpenAI-compatible)")}
MOONSHOT_HOSTS = ("api.moonshot.ai", "api.moonshot.cn")       # bitpin/llm.py MOONSHOT_HOSTS
OPENROUTER_HOSTS = ("openrouter.ai",)
REASONING_EFFORTS = ("", "low", "medium", "high", "max")
VPN_SCHEMES = ("vmess", "vless", "trojan", "ss")
VPN_SCHEMES_TEXT = "vmess:// vless:// trojan:// ss://"
DEFAULT_AUDIT_LOG = "/var/lib/bitpin-bot-panel/audit.jsonl"
AUDIT_ROTATE_BYTES = 5 * 1024 * 1024
AUDIT_TAIL_BYTES = 256 * 1024
MAX_FORM_FIELDS = 400
MAX_CONFIG_TEXT = 1024 * 1024            # the helper's limit for config_put
MAX_VPN_TEXT = 1024 * 1024
MAX_VPN_LINK = 16 * 1024
MAX_SECRET_VALUE = 4096                  # the helper's limit
VPN_PENDING_SECONDS = 900
HELPER_TIMEOUT = 60.0
HELPER_LONG_TIMEOUT = 300.0
# every command that may run a bot tool (confirm-live, health), wait for the change lock or stop a service (up to
# 90 s): only the plain reads stay on the short timeout
HELPER_LONG_COMMANDS = ("apply_live", "vpn_put", "check", "models", "status", "health", "confirm_show", "vpn_test",
                        "logs", "config_put", "settings_set", "model_set", "secret_set", "service",
                        "panel_password_set", "panel_totp_set")
HELPER_MAX_BYTES = 4 * 1024 * 1024
TEHRAN = timezone(timedelta(hours=3, minutes=30))
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

CSP = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'"
SECURITY_HEADERS = (
    ("Strict-Transport-Security", "max-age=31536000"),
    ("Content-Security-Policy", CSP),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    # same-origin, NOT no-referrer: under no-referrer a browser sends "Origin: null" with every form POST (Fetch
    # standard, "append a request Origin header"), which the Origin check below refuses - the panel's own login
    # failed with 403 (3.1.1). same-origin still sends nothing to other sites.
    ("Referrer-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
# the stylesheet and the tab icon hold nothing private, so they may be cached (the stylesheet's address carries
# its hash: a new version is a new address)
STATIC_CACHE = "public, max-age=31536000, immutable"
ICON_CACHE = "public, max-age=86400"
HTML_TYPE = "text/html; charset=utf-8"
TEXT_TYPE = "text/plain; charset=utf-8"

APP_NAME = N_("Bitpin AI Trader")
NAV = (
    (N_("Overview"), (("/", "dashboard", N_("Dashboard")),)),
    (N_("Trading"), (("/trade", "sliders", N_("Trade settings")), ("/models", "cpu", N_("Models & keys")),
                     ("/apply", "check", N_("Apply settings")))),
    (N_("System"), (("/vpn", "globe", N_("VPN")), ("/logs", "logs", N_("Logs")),
                    ("/settings", "braces", N_("JSON editor")), ("/security", "shield", N_("Security")))),
)
BACK_PAGES = ("/", "/vpn", "/models", "/settings", "/trade", "/apply")
STATE_LABELS = {"active": N_("Running"), "activating": N_("Starting"), "deactivating": N_("Stopping"),
                "reloading": N_("Reloading"), "inactive": N_("Stopped"), "dead": N_("Stopped"),
                "failed": N_("Error"), "not-installed": N_("Not installed")}
GROUP_ICONS = {"style": "spark", "risk": "shield", "schedule": "clock", "markets": "chart", "ladder": "stairs",
               "exits": "target", "costs": "coins", "news": "news", "budget": "wallet"}

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
_HOST_RE = re.compile(r"^(?:[a-z0-9_.-]+|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?$")
_MODEL_ID_RE = re.compile(r"^[^\s\x00-\x1f\x7f<>\"'`]{1,200}$")
_URL_RE = re.compile(r"^https://[^\s\x00-\x1f\x7f<>\"'`]{3,200}$")
_SYSTEMD_UTC_RE = re.compile(r"^[A-Za-z]{3} (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC$")

# 24x24 line icons drawn for the panel (stroke = currentColor, set in panel.css)
ICONS = {
    "logo": '<path d="M6 3.5v3M6 17.5v3M12 2.5v3M12 15.5v6M18 5v3M18 16v3.5"/>'
            '<rect x="4" y="6.5" width="4" height="11" rx="1"/><rect x="10" y="5.5" width="4" height="10" rx="1"/>'
            '<rect x="16" y="8" width="4" height="8" rx="1"/>',
    "dashboard": '<rect x="3" y="3" width="7.5" height="9" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="5" '
                 'rx="1.5"/><rect x="13.5" y="11" width="7.5" height="10" rx="1.5"/><rect x="3" y="15" width="7.5" '
                 'height="6" rx="1.5"/>',
    "sliders": '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/>'
               '<circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/>'
           '<path d="M9.5 2.5v3M14.5 2.5v3M9.5 18.5v3M14.5 18.5v3M2.5 9.5h3M2.5 14.5h3M18.5 9.5h3M18.5 14.5h3"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8 5.4-5.8"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.4 2.5 3.7 5.5 3.7 9s-1.3 6.5-3.7 9'
             'c-2.4-2.5-3.7-5.5-3.7-9S9.6 5.5 12 3z"/>',
    "logs": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
            '<path d="M14 3v5h5M9 13h6M9 17h6M9 9h2"/>',
    "braces": '<path d="M8 3.5H7a2 2 0 0 0-2 2v3.8a2 2 0 0 1-2 2 2 2 0 0 1 2 2v3.7a2 2 0 0 0 2 2h1M16 3.5h1a2 2 0 0 1 '
              '2 2v3.8a2 2 0 0 0 2 2 2 2 0 0 0-2 2v3.7a2 2 0 0 1-2 2h-1"/>',
    "shield": '<path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6z"/>'
              '<path d="m9 12 2.2 2.2L15.5 10"/>',
    "logout": '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H4"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5c1.4-3.7 4.2-5.5 7.5-5.5s6.1 1.8 7.5 5.5"/>',
    "wallet": '<path d="M19 7V5.5A1.5 1.5 0 0 0 17.5 4H5a2 2 0 0 0-2 2"/><path d="M3 6v12a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1'
              'v-3M21 11V8a1 1 0 0 0-1-1H5a2 2 0 0 1-2-2"/><path d="M21 11h-4a2 2 0 0 0 0 4h4z"/>',
    "trend": '<path d="m3 17 6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
    "up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "down": '<path d="M12 5v14M6 13l6 6 6-6"/>',
    "drop": '<path d="m3 7 6 6 4-4 8 8"/><path d="M15 17h6v-6"/>',
    "coins": '<circle cx="9" cy="9" r="6"/><path d="M15.5 9.3a6 6 0 1 1-6.2 6.2"/><path d="M9 6.5v5M7 8h3M8 11h2"/>',
    "orders": '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2"/><circle cx="4.5" cy="12" r="1.2"/>'
              '<circle cx="4.5" cy="18" r="1.2"/>',
    "spark": '<path d="M12 3.5 13.9 8.6 19 10.5l-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9z"/>'
             '<path d="M19 15.5l.8 1.9 1.7.6-1.7.7-.8 1.8-.7-1.8-1.8-.7 1.8-.6z"/>',
    "server": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/>'
              '<path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    "zap": '<path d="M13 2.5 4.5 13.5h7l-1 8 8.5-11h-7z"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8"/><path d="M4 4v4h4M4 13a8 8 0 0 0 14.3 4.9L20 16"/>'
               '<path d="M20 20v-4h-4"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "play": '<path d="M8 5.5v13l10.5-6.5z"/>',
    "pulse": '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.8h.01"/>',
    "warn": '<path d="M10.3 4.2 2.7 17.5A2 2 0 0 0 4.4 20.5h15.2a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0z"/>'
            '<path d="M12 9.5v4M12 17h.01"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
    "ok": '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8 5.4-5.8"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7.5a4 4 0 0 1 8 0V11"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M10.9 12.1 19.5 3.5M15.5 7.5l2.5 2.5M13.5 9.5l2 2"/>',
    "lang": '<path d="M4 5.5h9M8.5 3.5v2M6 5.5c.7 3.2 2.8 5.8 6 7.3M11 5.5c-.9 3.8-3.4 6.8-7 8.3"/>'
            '<path d="m13 20.5 4.2-9.5 4.3 9.5M14.4 17.5h5.7"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "save": '<path d="M5 3.5h11l3.5 3.5v12a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 19V5A1.5 1.5 0 0 1 5 3.5z"/>'
            '<path d="M7.5 3.5v4.5h7V3.5M7.5 20.5v-6.5h9v6.5"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20.5 20.5-4.5-4.5"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9.5 3 2.5-3 2.5M13 15h4"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>',
    "chart": '<path d="M4 20V11M10 20V5M16 20v-7M21 20H3"/>',
    "stairs": '<path d="M3.5 20h4.5v-4.5h4.5V11h4.5V6.5h3.5"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2"/>',
    "news": '<path d="M4 5h12.5v14.5H6a2 2 0 0 1-2-2z"/>'
            '<path d="M16.5 9H20v8.5a2 2 0 0 1-2 2M7.5 9h5.5M7.5 12.5h5.5M7.5 16h3.5"/>',
    "chat": '<path d="M20.5 12a8 8 0 0 1-11.8 7.1L3.5 20.5l1.4-4.9A8 8 0 1 1 20.5 12z"/>',
    "link": '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3.2-3.2a4.5 4.5 0 0 0-6.4-6.4L12 5.6"/>'
            '<path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3.2 3.2a4.5 4.5 0 0 0 6.4 6.4l1.2-1.2"/>',
    "list": '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
}
BOX_ICONS = {"ok": "ok", "warn": "warn", "err": "error", "info": "info"}
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><defs><linearGradient id="g" x1="0" y1="0" x2="1" '
    'y2="1"><stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#06b6d4"/></linearGradient></defs>'
    '<rect width="32" height="32" rx="8" fill="url(#g)"/><g fill="#fff"><rect x="7" y="12" width="4" height="9" '
    'rx="1"/><rect x="14" y="8" width="4" height="12" rx="1"/><rect x="21" y="13" width="4" height="7" rx="1"/></g>'
    '<path d="M9 9v3M9 21v3M16 5v3M16 20v4M23 10v3M23 20v3" stroke="#fff" stroke-width="1.6" '
    'stroke-linecap="round"/></svg>')


def load_static(name):
    """(text, short hash) of a file in bitpin/static; ("", "missing") when it cannot be read (the panel still
    works, unstyled, and says so in its log)."""
    path = os.path.join(STATIC_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        log.warning("panel: cannot read %s: %s (the pages are served without their stylesheet)", path,
                    e.strerror or e)
        return "", "missing"
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class HelperError(Exception):
    """The root helper refused a request or could not be reached (the message is safe to show)."""


class HelperClient(object):
    """Client of the root helper (scripts/panel_helper.py): one JSON line per connection over a UNIX socket.

    Request {"cmd": name, "args": {...}, "actor": {"user", "ip"}} + "\\n", then the write half is shut down;
    answer: one JSON line {"ok": true, "data": {...}} or {"ok": false, "error": text}. At most 4 MB each way;
    the whole call is bounded by 300 s for HELPER_LONG_COMMANDS and 60 s for the plain reads."""

    def __init__(self, socket_path, timeout=HELPER_TIMEOUT, long_timeout=HELPER_LONG_TIMEOUT,
                 max_bytes=HELPER_MAX_BYTES):
        self.socket_path = socket_path
        self.timeout = float(timeout)
        self.long_timeout = float(long_timeout)
        self.max_bytes = int(max_bytes)

    def timeout_for(self, cmd):
        return self.long_timeout if cmd in HELPER_LONG_COMMANDS else self.timeout

    def _connect(self, timeout):
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise HelperError("this platform has no AF_UNIX sockets: the panel helper is reachable on Linux only")
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(self.socket_path)
        except OSError as e:
            sock.close()
            raise HelperError("cannot connect to the helper socket %s: %s" % (self.socket_path, e.strerror or e))
        return sock

    def call(self, cmd, actor=None, timeout=None, **args):
        req = {"cmd": cmd, "args": args}
        if isinstance(actor, dict):
            req["actor"] = {"user": str(actor.get("user") or ""), "ip": str(actor.get("ip") or "")}
        line = (json.dumps(req, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(line) > self.max_bytes:
            raise HelperError("the request is larger than %d bytes" % self.max_bytes)
        limit = float(timeout) if timeout else self.timeout_for(cmd)
        deadline = time.monotonic() + limit
        sock = self._connect(limit)
        buf = bytearray()
        try:
            sock.sendall(line)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            while b"\n" not in buf:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise socket.timeout()
                sock.settimeout(left)
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > self.max_bytes:
                    raise HelperError("the helper's answer is larger than %d bytes" % self.max_bytes)
        except socket.timeout:
            raise HelperError("the helper did not answer within %d s" % int(limit))
        except OSError as e:
            raise HelperError("the connection to the helper failed: %s" % (e.strerror or e))
        finally:
            try:
                sock.close()
            except OSError:
                pass
        raw = bytes(buf).split(b"\n", 1)[0].strip()
        if not raw:
            raise HelperError("the helper closed the connection without an answer")
        try:
            resp = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HelperError("the helper's answer is not valid JSON")
        if not isinstance(resp, dict):
            raise HelperError("the helper's answer is not a JSON object")
        if resp.get("ok") is True:
            data = resp.get("data")
            return data if isinstance(data, dict) else {}
        err = resp.get("error")
        raise HelperError(str(err) if err else "the helper refused the request (no reason given)")


# ------------------------------------------------------------------------------------------------ html bits

def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def te(msg):
    """tr() + esc(): a translated plain text."""
    return esc(tr(msg))


def ltr(v):
    """A technical value (symbol, path, model id, number) inside Persian text."""
    return '<bdi dir="ltr">%s</bdi>' % esc(v)


def bdi(v):
    """Free text of unknown direction (Persian or English)."""
    return "<bdi>%s</bdi>" % esc(v)


def code(v):
    return '<code dir="ltr">%s</code>' % esc(v)


def pre(v, cls=""):
    return '<pre dir="ltr"%s>%s</pre>' % (' class="%s"' % cls if cls else "", esc(v))


def pre_fa(v):
    return '<pre dir="rtl" class="fa">%s</pre>' % esc(v)


def icon(name, cls="i"):
    return '<svg class="%s" viewBox="0 0 24 24" aria-hidden="true" focusable="false">%s</svg>' % (cls, ICONS[name])


def box(kind, inner):
    return '<div class="box %s" role="%s">%s<div class="box-t">%s</div></div>' % (
        kind, "alert" if kind == "err" else "status", icon(BOX_ICONS.get(kind, "info")), inner)


def card(title, body, icon_name=None, actions="", cls="", id_=None):
    """A section: its title (HTML) with an icon, optional actions on the other side, then the body."""
    head = ""
    if title:
        head = '<div class="card-h"><h2>%s<span>%s</span></h2>%s</div>' % (
            icon(icon_name) if icon_name else "", title, ('<div class="card-x">%s</div>' % actions) if actions else "")
    return '<section class="card%s"%s>%s<div class="card-b">%s</div></section>' % (
        " " + cls if cls else "", ' id="%s"' % id_ if id_ else "", head, body)


def kpi(icon_name, label, value, unit="", sub="", extra="", cls=""):
    """A headline number of the dashboard (value, sub and extra are HTML)."""
    return '<div class="kpi%s"><div class="k">%s<span>%s</span></div><div class="v">%s%s</div>%s%s</div>' % (
        " " + cls if cls else "", icon(icon_name), esc(label), value,
        ('<span class="unit">%s</span>' % esc(unit)) if unit else "", extra,
        ('<div class="s">%s</div>' % sub) if sub else "")


def button(label, cls="primary", icon_name=None, name=None, value=None):
    """A submit button; name/value tell the handler which of a form's buttons was pressed."""
    attrs = ' name="%s" value="%s"' % (esc(name), esc(value or "")) if name else ""
    return '<button type="submit"%s class="btn %s">%s<span>%s</span></button>' % (
        attrs, cls, icon(icon_name) if icon_name else "", esc(label))


def link_button(href, label, cls="secondary", icon_name=None):
    return '<a class="btn %s" href="%s">%s<span>%s</span></a>' % (cls, esc(href), icon(icon_name) if icon_name else "",
                                                                  esc(label))


def field(label, control, help_html="", for_=None, cls=""):
    """One labelled form control (label and control are HTML)."""
    lab = ""
    if label:
        lab = '<label for="%s">%s</label>' % (esc(for_), label) if for_ else "<label>%s</label>" % label
    return '<div class="field%s">%s%s%s</div>' % (" " + cls if cls else "", lab, control, help_html)


def help_p(text_html):
    return '<p class="help">%s</p>' % text_html


def ul(items, text_fn=None):
    fn = text_fn or bdi
    return "<ul>%s</ul>" % "".join("<li>%s</li>" % fn(x) for x in items)


def dash():
    return '<span class="muted">&#8212;</span>'


def table(head, rows, num=(), cls=""):
    """head: the column titles (translated here); rows: lists of HTML cells; num: the columns of numbers."""
    if not rows:
        return '<p class="muted empty">%s</p>' % te("Nothing to show.")

    def cell(tag, i, v):
        return "<%s%s>%s</%s>" % (tag, ' class="num"' if i in num else "", v, tag)

    th = "".join(cell("th", i, te(h) if h else "") for i, h in enumerate(head))
    body = "".join("<tr>%s</tr>" % "".join(cell("td", i, c) for i, c in enumerate(r)) for r in rows)
    return '<div class="tbl%s"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
        " " + cls if cls else "", th, body)


def kv_table(rows):
    body = "".join("<tr><th>%s</th><td>%s</td></tr>" % (te(k), v) for k, v in rows)
    return '<div class="tbl kv"><table><tbody>%s</tbody></table></div>' % body


def facts(rows):
    """Small label / value pairs side by side (the last decision)."""
    return '<dl class="facts">%s</dl>' % "".join("<div><dt>%s</dt><dd>%s</dd></div>" % (te(k), v) for k, v in rows)


def hidden(name, value):
    return '<input type="hidden" name="%s" value="%s">' % (esc(name), esc(value))


def select(name, options, selected, id_=None, cls=""):
    """options: [(value, label)]. A current value that is not one of the options is kept as an extra, selected
    option (a browser would otherwise pick the first one and a save would silently change the setting)."""
    options = list(options)
    if selected not in ("", None) and all(v != selected for v, _l in options):
        options.insert(0, (selected, tr("%s (current value)") % selected))
    opts = "".join('<option value="%s"%s>%s</option>' % (esc(v), " selected" if v == selected else "", esc(l))
                   for v, l in options)
    return '<select name="%s"%s%s>%s</select>' % (esc(name), ' id="%s"' % esc(id_) if id_ else "",
                                                   ' class="%s"' % cls if cls else "", opts)


def textarea(name, text, cls="code", id_=None, rows=None):
    # the "\n" after the tag: an HTML parser drops ONE leading newline of a textarea's content
    return '<textarea name="%s"%s class="%s" dir="ltr" spellcheck="false" autocomplete="off"%s>\n%s</textarea>' % (
        esc(name), ' id="%s"' % esc(id_) if id_ else "", cls, ' rows="%d"' % rows if rows else "", esc(text))


def checkbox(name, label, checked=False, value="1"):
    return ('<label class="check"><input type="checkbox" class="switch" name="%s" value="%s"%s> <span>%s</span>'
            '</label>') % (esc(name), esc(value), " checked" if checked else "", esc(label))


def jtext(v):
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(v)


def yes_no(v):
    if v is True:
        return te("Yes")
    if v is False:
        return te("No")
    return dash() if v is None or v == "" else bdi(v)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def fmt_num(v, nd=None):
    if v is None or v == "":
        return dash()
    if isinstance(v, bool):
        return yes_no(v)
    if isinstance(v, int):
        return ltr("{:,}".format(v))
    if isinstance(v, float):
        if not math.isfinite(v):
            return ltr(v)
        if nd is None:
            return ltr("{:.8g}".format(v))
        return ltr("{:,.{}f}".format(v, nd))
    return ltr(v)


def pct_text(v, nd=2):
    """50.0 -> "50%", 2.68 -> "2.68%"."""
    return ("%.*f" % (nd, v)).rstrip("0").rstrip(".") + "%"


def usd(v):
    return ltr("$%.2f" % v) if _is_num(v) else dash()


def fmt_time(v):
    """Epoch seconds or an ISO time with its offset, shown as Tehran time; anything else as written."""
    if v is None or v == "":
        return dash()
    dt = None
    if _is_num(v):
        if 0 < v < 1e11:
            try:
                dt = datetime.fromtimestamp(float(v), timezone.utc)
            except (OverflowError, OSError, ValueError):
                dt = None
    elif isinstance(v, str) and 10 <= len(v.strip()) <= 40:
        s = v.strip()
        m = _SYSTEMD_UTC_RE.match(s)                          # systemctl show: "Sun 2026-09-27 19:30:00 UTC"
        if m:
            s = "%sT%s+00:00" % (m.group(1), m.group(2))
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = None
        if dt is not None and dt.tzinfo is None:
            dt = None
    if dt is None:
        return ltr(v)
    return '%s <span class="muted">%s</span>' % (ltr(dt.astimezone(TEHRAN).strftime("%Y-%m-%d %H:%M")), te("Tehran"))


def state_html(state, sub=None):
    """A systemd state as a coloured pill; the raw state (e.g. "active (running)") is its tooltip."""
    if state is None or state == "":
        return dash()
    if state == "active":
        kind = "ok"
    elif state in ("failed", "not-installed"):
        kind = "err"
    elif state in ("inactive", "dead"):
        kind = "warn"
    else:
        kind = "info"
    raw = "%s (%s)" % (state, sub) if sub else str(state)
    label = STATE_LABELS.get(state)
    return '<span class="pill %s" title="%s">%s</span>' % (kind, esc(raw), te(label) if label else ltr(state))


def ok_html(ok):
    return '<span class="pill %s">%s</span>' % ("ok" if ok else "err", te("OK") if ok else te("Failed"))


def diff_html(diff):
    out = []
    for ln in str(diff).splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append('<span class="add">%s</span>' % esc(ln))
        elif ln.startswith("-") and not ln.startswith("---"):
            out.append('<span class="del">%s</span>' % esc(ln))
        elif ln.startswith("@@"):
            out.append('<span class="hunk">%s</span>' % esc(ln))
        else:
            out.append(esc(ln))
    return '<pre class="diff" dir="ltr">%s</pre>' % "\n".join(out)


def redact(text, secrets_):
    """`text` with every occurrence of a secret value (4+ characters) replaced (defence in depth: the helper
    never echoes a value, but if it ever did, the panel neither shows nor logs it)."""
    s = str(text)
    for v in secrets_:
        if isinstance(v, str) and len(v) >= 4:
            s = s.replace(v, "***")
    return s


def parse_urlencoded(raw, max_fields=MAX_FORM_FIELDS):
    """{name: value} of an application/x-www-form-urlencoded body or a query string (the first value wins).
    Only '&' separates fields. Raises ValueError on too many fields."""
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1")
    out = {}
    if not raw:
        return out
    parts = raw.split("&")
    if len(parts) > max_fields:
        raise ValueError("too many form fields")
    for part in parts:
        if not part:
            continue
        k, _, v = part.partition("=")
        k = unquote_plus(k, encoding="utf-8", errors="replace")
        v = unquote_plus(v, encoding="utf-8", errors="replace")
        if k not in out:
            out[k] = v
    return out


def parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        if k and k not in out:
            out[k] = v
    return out


def _norm_headers(headers):
    """({lower-case name: value}, {names sent more than once}) of a dict, a list of pairs or an HTTPMessage."""
    out, dups = {}, set()
    if headers is None:
        return out, dups
    items = headers.items() if hasattr(headers, "items") else headers
    for k, v in items:
        lk = str(k).strip().lower()
        if lk in out:
            dups.add(lk)
            if lk == "cookie":
                out[lk] = out[lk] + "; " + str(v)
            continue
        out[lk] = str(v)
    return out, dups


def _cookie(name, value, clear=False, max_age=None):
    base = "%s=%s; Secure; HttpOnly; SameSite=Strict; Path=/" % (name, "" if clear else value)
    if clear:
        return base + "; Max-Age=0"
    return base + ("; Max-Age=%d" % int(max_age) if max_age else "")


def _sha(text):
    return hashlib.sha256(str(text).encode("utf-8", "replace")).digest()


def _utc_iso(t):
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def infer_provider(base_url):
    """The provider the helper derives from a base_url's host (moonshot / openrouter / openai), "" if none."""
    try:
        host = (urlsplit(base_url or "").hostname or "").lower()
    except ValueError:
        host = ""
    if not host:
        return ""
    if host in MOONSHOT_HOSTS:
        return "moonshot"
    if host in OPENROUTER_HOSTS:
        return "openrouter"
    return "openai"


def provider_url(provider):
    return dict(PROVIDER_URLS).get(provider, "")


def provider_key(provider):
    return dict(PROVIDER_KEYS).get(provider, "LLM_API_KEY")


def _default_spawn(fn):
    t = threading.Thread(target=fn, name="panel-notify")
    t.daemon = True
    t.start()


def _totp_ok(secret):
    try:
        s = "".join(secret.split()).replace("-", "").upper()
        return len(base64.b32decode(s + "=" * (-len(s) % 8))) >= 10
    except (ValueError, TypeError, AttributeError, binascii.Error):
        return False


class _Req(object):
    __slots__ = ("method", "path", "query", "headers", "cookies", "form", "ip", "now", "host", "sess", "sid")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def user(self):
        return self.sess.get("user") if self.sess else None


# ------------------------------------------------------------------------------------------------ the app

class PanelApp(object):
    """The panel. cfg: the parsed panel.json; helper: an object with call(cmd, actor=None, **args) -> dict that
    raises HelperError (HelperClient in production, a fake in tests). clock: wall time (sessions, TOTP, locks);
    sleep: the failed-login delay; spawn(fn): runs fn in the background (the Telegram login notices);
    config_path: panel.json, re-read after the helper changed the password hash or the 2FA secret."""

    def __init__(self, cfg, helper, clock=time.time, sleep=time.sleep, spawn=None, root_dir=None, config_path=None):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.cfg = cfg
        self.helper = helper
        self.clock = clock
        self._sleep = sleep
        self._spawn = spawn or _default_spawn
        self.root_dir = root_dir or ROOT_DIR
        self.config_path = config_path
        self.username = str(cfg.get("username") or "")
        self._password_hash = str(cfg.get("password_hash") or "")
        ts = cfg.get("totp_secret")
        self._totp_secret = ts if isinstance(ts, str) and _totp_ok(ts) else None
        self._totp_last = None
        self.allowed_hosts = [str(h).strip().lower() for h in (cfg.get("allowed_hosts") or []) if str(h).strip()]
        self.audit_path = str(cfg.get("audit_log") or DEFAULT_AUDIT_LOG)
        self.sessions = SessionStore(_num_or(cfg.get("session_idle_minutes"), 30), _num_or(cfg.get("session_max_hours"), 12))
        self.limiter = LoginLimiter()
        # trusted browsers (security review F1): the key next to the audit log, in the panel's own state dir
        self.devices = DeviceTrust(load_device_secret(os.path.join(os.path.dirname(os.path.abspath(self.audit_path)),
                                                                   "device_key")))
        self.hash_iterations = PBKDF2_ITERATIONS
        self.css, self.css_version = load_static("panel.css")
        self._login_lock = threading.Lock()        # one credential check at a time
        self._auth_lock = threading.Lock()         # password hash / TOTP secret / last TOTP step
        self._audit_lock = threading.Lock()
        self._get_routes = {
            "/": self._dashboard, "/models": self._models_get, "/settings": self._settings_get,
            "/trade": self._trade_get, "/apply": self._apply_get, "/vpn": self._vpn_get, "/logs": self._logs_get,
            "/health": self._health_get, "/security": self._security_get,
        }
        self._post_routes = {
            "/logout": self._logout, "/service": self._service_post, "/models/list": self._models_list,
            "/models/set": self._models_set, "/models/key": self._models_key, "/settings": self._settings_post,
            "/trade": self._trade_post, "/apply": self._apply_post, "/apply/check": self._apply_check,
            "/vpn/test": self._vpn_test, "/vpn/raw": self._vpn_raw, "/vpn/put": self._vpn_put,
            "/security/password": self._password_post, "/security/totp/new": self._totp_new,
            "/security/totp/cancel": self._totp_cancel, "/security/totp/enable": self._totp_enable,
            "/security/totp/disable": self._totp_disable,
        }

    @property
    def totp_enabled(self):
        return bool(self._totp_secret)

    # ------------------------------------------------------------------ responses
    def _resp(self, status, body, ctype=HTML_TYPE, headers=None, cache=None):
        hdrs = [(k, cache if (cache and k == "Cache-Control") else v) for k, v in SECURITY_HEADERS]
        hdrs.append(("Content-Type", ctype))
        if headers:
            hdrs.extend(headers)
        if isinstance(body, str):
            body = body.encode("utf-8")
        return status, hdrs, body

    def error_response(self, status, text):
        """A plain-text error with the security headers (also used by scripts/panel_server.py)."""
        return self._resp(status, "%d %s\n" % (status, text), TEXT_TYPE)

    def _redirect(self, location, headers=None):
        return self._resp(303, "", TEXT_TYPE, [("Location", location)] + list(headers or []))

    # ------------------------------------------------------------------ layout
    def _doc(self, title, content, req=None, active=None, sub="", here="/"):
        """A whole page: the sidebar layout for a logged-in request, else the centred card of the login page."""
        lang = current()
        head = ('<!doctype html>\n<html lang="%s" dir="%s"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta name="robots" content="noindex, nofollow"><meta name="color-scheme" content="light dark">'
                '<title>%s | %s</title><link rel="icon" href="/static/icon.svg" type="image/svg+xml">'
                '<link rel="stylesheet" href="/static/panel.css?v=%s"></head>') % (
            lang, "rtl" if is_rtl(lang) else "ltr", esc(title), te(APP_NAME), esc(self.css_version))
        if req is not None and req.sess is not None:
            body = self._shell(req, title, content, active, sub, here)
        else:
            body = self._bare(title, content, here)
        return head + body + "</html>"

    def _brand(self):
        return ('<a class="brand" href="/"><span class="logo">%s</span><span class="brand-t"><b>%s</b>'
                '<small>%s</small></span></a>') % (icon("logo"), te(APP_NAME), te("Control panel"))

    def _lang_switch(self, here):
        links = []
        for lang, name in LANG_NAMES:
            href = "/lang?" + urlencode([("to", lang), ("next", here)])
            links.append('<a href="%s" hreflang="%s" lang="%s"%s>%s</a>' % (
                esc(href), lang, lang, ' aria-current="true"' if lang == current() else "", esc(name)))
        return '<nav class="seg" aria-label="%s">%s%s</nav>' % (te("Language"), icon("lang"), "".join(links))

    def _shell(self, req, title, content, active, sub, here):
        groups = []
        for section, items in NAV:
            links = "".join('<a href="%s"%s>%s<span>%s</span></a>' % (
                href, ' aria-current="page"' if href == active else "", icon(ic), te(label)) for href, ic, label in items)
            groups.append('<p class="nav-h">%s</p>%s' % (te(section), links))
        logout = self._form(req, "/logout", '<button type="submit" class="btn ghost sm" title="%s">%s<span>%s</span>'
                                            '</button>' % (te("Log out"), icon("logout", "i flip"), te("Log out")),
                            "inline")
        return ('<body><div class="shell"><aside class="side">%s<nav class="nav" aria-label="%s">%s</nav>'
                '<div class="side-foot">%s</div></aside><div class="main"><header class="topbar"><div class="titles">'
                '<h1>%s</h1>%s</div><div class="tools">%s<span class="user">%s%s</span>%s</div></header>'
                '<main class="content">%s</main><footer class="foot">%s</footer></div></div></body>') % (
            self._brand(), te("Main menu"), "".join(groups), esc("bitpin-bot " + __version__), esc(title),
            ('<p class="sub">%s</p>' % esc(sub)) if sub else "", self._lang_switch(here), icon("user"), ltr(req.user),
            logout, content, esc("bitpin-bot panel " + __version__))

    def _bare(self, title, content, here):
        return ('<body class="bare"><div class="bare-top">%s</div><main class="bare-main"><div class="bare-card">%s'
                '<h1>%s</h1>%s</div><p class="foot">%s</p></main></body>') % (
            self._lang_switch(here), self._brand(), esc(title), content, esc("bitpin-bot panel " + __version__))

    def _here(self, req, active=None):
        """Where the language switch comes back to: this page (with its query) when it is a page, else `active`."""
        if req is not None and req.method == "GET" and (req.path in self._get_routes or req.path == "/login"):
            q = sorted((k, v) for k, v in (req.query or {}).items() if k)
            return req.path + ("?" + urlencode(q) if q else "")
        return active or "/"

    def _page(self, req, title, body, status=200, headers=None, active=None, sub=""):
        flashes = ""
        if req.sess is not None:
            fl = req.sess.pop("flash", None)
            if fl:
                flashes = "".join(fl)
        return self._resp(status, self._doc(title, flashes + body, req, active, sub, self._here(req, active)),
                          HTML_TYPE, headers)

    def _form(self, req, action, inner, cls=""):
        return '<form method="post" action="%s"%s>%s%s</form>' % (
            esc(action), ' class="%s"' % cls if cls else "", hidden("csrf", req.sess["csrf"]), inner)

    def _flash(self, req, inner):
        fl = req.sess.setdefault("flash", [])
        if len(fl) < 10:
            fl.append(inner)

    # ------------------------------------------------------------------ helper calls and the audit log
    def _call(self, req, cmd, **args):
        """(data, None) or (None, error text); never raises. The logged-in user and IP go along as the actor."""
        actor = {"user": req.user or "", "ip": req.ip or ""} if req is not None else None
        try:
            data = self.helper.call(cmd, actor=actor, **args)
        except HelperError as e:
            return None, (str(e) or "unknown error")
        except Exception as e:                   # a broken client must never become a stack trace on a page
            log.warning("panel: helper call %s failed: %s", cmd, e.__class__.__name__)
            return None, "%s: %s" % (e.__class__.__name__, e)
        return (data if isinstance(data, dict) else {}), None

    def _act(self, req, cmd, audit=None, hide=(), **args):
        """A helper call made by a POST: audited with the command and `audit` (never the arguments)."""
        data, err = self._call(req, cmd, **args)
        if err is not None:
            err = redact(err, hide)
            self._audit_action(req, cmd, err, **(audit or {}))
        else:
            self._audit_action(req, cmd, "failed (ok=false)" if data.get("ok") is False else None, **(audit or {}))
        return data, err

    def _audit_action(self, req, cmd, err=None, **extra):
        fields = dict(extra)
        fields["cmd"] = cmd
        fields["result"] = "ok" if err is None else "error"
        if err is not None:
            fields["error"] = str(err)[:300]
        self._audit("action", req.ip, req.user, **fields)

    def _audit(self, event, ip, user, **fields):
        rec = {"time": _utc_iso(self.clock()), "event": event, "ip": ip, "user": user}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=True, sort_keys=True, default=str) + "\n"
        with self._audit_lock:
            try:
                try:
                    if os.path.getsize(self.audit_path) > AUDIT_ROTATE_BYTES:
                        os.replace(self.audit_path, self.audit_path + ".1")
                except OSError:
                    pass
                fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.encode("ascii"))
                finally:
                    os.close(fd)
            except OSError as e:
                log.warning("panel: the audit log %s is not writable: %s", self.audit_path, e.strerror or e)

    def audit_tail(self, n=50):
        """The last n audit records, newest first."""
        try:
            with open(self.audit_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - AUDIT_TAIL_BYTES))
                data = f.read()
        except OSError:
            return []
        lines = data.split(b"\n")
        if size > AUDIT_TAIL_BYTES:
            lines = lines[1:]
        recs = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(r, dict):
                recs.append(r)
        return recs[-n:][::-1]

    def _notify(self, event, ip, user):
        """audit_notify in the background: a slow or dead helper never delays or fails a login."""
        def run():
            try:
                self.helper.call("audit_notify", actor={"user": user or "", "ip": ip or ""}, event=event, ip=ip,
                                 user=user or "")
            except Exception as e:
                log.warning("panel: audit_notify %s failed: %s", event, e.__class__.__name__)
        try:
            self._spawn(run)
        except Exception as e:
            log.warning("panel: cannot start the audit_notify task: %s", e)

    def _helper_error(self, err, apply_link=False):
        link = (' <a href="/apply">%s</a>' % te("Go to Apply settings")) if apply_link else ""
        return box("err", "<strong>%s</strong><p>%s</p><p>%s%s</p>" % (
            te("The panel helper returned an error"),
            te("The request was not carried out. If this repeats, check the helper service on the server "
               "(bitpin-bot-panel-helper)."), tr("Details: %s") % bdi(err), link))

    # ------------------------------------------------------------------ the entry point
    def handle(self, method, path, query, headers, body_bytes, client_ip):
        try:
            return self._handle(method, path, query, headers, body_bytes, client_ip)
        except Exception:
            log.exception("panel: internal error on %s %s", method, str(path).split("?", 1)[0][:200])
            return self._resp(500, self._doc(tr("Internal error"), box("err", te(
                "Internal panel error. The details are in the panel service log (journalctl -u bitpin-bot-panel)."))))
        finally:
            reset_lang()

    def _handle(self, method, path, query, headers, body_bytes, client_ip):
        method = str(method or "").upper()
        path = str(path or "/")
        if "?" in path:
            path, _, q2 = path.partition("?")
            query = query or q2
        hdrs, dups = _norm_headers(headers)
        host = hdrs.get("host", "").strip().lower()
        if "host" in dups or not self._host_ok(host):
            return self.error_response(400, "Bad Request (host)")
        if method not in ("GET", "POST"):
            st, h, b = self.error_response(405, "Method Not Allowed")
            return st, h + [("Allow", "GET, POST")], b
        if method == "GET" and path == "/static/panel.css":
            return self._resp(200, self.css, "text/css; charset=utf-8", cache=STATIC_CACHE)
        if method == "GET" and path == "/static/icon.svg":
            return self._resp(200, FAVICON_SVG, "image/svg+xml", cache=ICON_CACHE)
        if method == "GET" and path == "/robots.txt":
            return self._resp(200, "User-agent: *\nDisallow: /\n", TEXT_TYPE)
        if path == "/favicon.ico":
            return self.error_response(404, "Not Found")
        try:
            if isinstance(query, dict):
                q = dict((str(k), v[0] if isinstance(v, list) and v else str(v)) for k, v in query.items())
            else:
                q = parse_urlencoded(query if isinstance(query, (str, bytes)) else "")
        except ValueError:
            return self.error_response(400, "Bad Request (query)")
        req = _Req(method=method, path=path, query=q, headers=hdrs, cookies=parse_cookies(hdrs.get("cookie")),
                   form={}, ip=str(client_ip or "")[:64], now=self.clock(), host=host)
        lang = req.cookies.get(LANG_COOKIE)
        set_lang(lang if lang in LANGS else negotiate(hdrs.get("accept-language")))
        if method == "POST":
            ctype = hdrs.get("content-type", "").split(";", 1)[0].strip().lower()
            if ctype != "application/x-www-form-urlencoded":
                return self.error_response(415, "Unsupported Media Type")
            if not self._origin_ok(hdrs, host):
                return self.error_response(403, "Forbidden (origin)")
            try:
                req.form = parse_urlencoded(body_bytes or b"")
            except ValueError:
                return self.error_response(400, "Bad Request (form)")
        if path == "/lang" and method == "GET":
            return self._lang_get(req)
        if path == "/login":
            return self._login_post(req) if method == "POST" else self._login_get(req)
        sid = req.cookies.get(SESSION_COOKIE)
        sess = self.sessions.get(sid, req.now) if sid else None
        if sess is None:
            return self._redirect("/login", [("Set-Cookie", _cookie(SESSION_COOKIE, "", clear=True))] if sid else None)
        req.sess, req.sid = sess, sid
        if method == "POST":
            token = req.form.get("csrf", "")
            if not token or not hmac.compare_digest(token.encode("utf-8"), str(sess.get("csrf")).encode("utf-8")):
                self._audit("csrf_refused", req.ip, req.user, path=path[:100])
                return self.error_response(403, "Forbidden (csrf)")
            route = self._post_routes.get(path)
        else:
            route = self._get_routes.get(path)
        if route is None:
            return self._page(req, tr("Page not found"), box("warn", te("This address does not exist in the panel.")),
                              status=404)
        return route(req)

    def _host_ok(self, host):
        if not host or len(host) > 255 or not _HOST_RE.match(host):
            return False
        if not self.allowed_hosts:
            return True
        name = host[:host.index("]") + 1] if host.startswith("[") else host.split(":", 1)[0]
        return host in self.allowed_hosts or name in self.allowed_hosts

    @staticmethod
    def _origin_ok(hdrs, host):
        site = hdrs.get("sec-fetch-site")
        site = site.strip().lower() if site is not None else None
        if site is not None and site not in ("same-origin", "none"):
            return False
        origin = hdrs.get("origin")
        if origin is None:
            return True
        o = origin.strip().lower().rstrip("/")
        if o == "null":
            # a privacy setting or an older page may still make the browser send "null": accepted only together
            # with the browser's own Sec-Fetch-Site: same-origin (no page can set that header); the CSRF token and
            # the SameSite=Strict cookie are checked as always
            return site == "same-origin"
        ok = {"https://" + host}
        ok.add("https://" + host[:-4] if host.endswith(":443") else "https://" + host + ":443")
        return o in ok

    # ------------------------------------------------------------------ language
    def _lang_get(self, req):
        """GET /lang?to=fa&next=/trade: remembers the language (a cookie, nothing else) and goes back. Works
        with or without a session; `next` must be one of the panel's own pages (never another site)."""
        lang = req.query.get("to", "")
        headers = [("Set-Cookie", _cookie(LANG_COOKIE, lang, max_age=LANG_MAX_AGE))] if lang in LANGS else []
        return self._redirect(self._safe_next(req.query.get("next", "/")), headers)

    def _safe_next(self, nxt):
        try:
            parts = urlsplit(str(nxt or "/"))
        except ValueError:
            return "/"
        if parts.scheme or parts.netloc or (parts.path not in self._get_routes and parts.path != "/login"):
            return "/"
        try:
            q = parse_urlencoded(parts.query, max_fields=20)
        except ValueError:
            q = {}
        return parts.path + ("?" + urlencode(sorted(q.items())) if q else "")

    # ------------------------------------------------------------------ login / logout
    def _login_doc(self, token, message=""):
        code_field = ""
        if self.totp_enabled:
            code_field = field(te("6-digit code from your authenticator app"),
                               '<input id="code" name="code" class="ltr" inputmode="numeric" '
                               'autocomplete="one-time-code" maxlength="12" required>', for_="code")
        form = ('<form method="post" action="/login" class="login">%s%s%s%s<div class="actions">%s</div></form>') % (
            hidden("lt", token),
            field(te("Username"), '<input id="username" name="username" class="ltr" autocomplete="username" '
                                  'autocapitalize="none" spellcheck="false" maxlength="128" required>', for_="username"),
            field(te("Password"), '<input id="password" type="password" name="password" class="ltr" '
                                  'autocomplete="current-password" maxlength="1024" required>', for_="password"),
            code_field, button(tr("Sign in"), "primary block", "lock"))
        intro = '<p class="lead">%s</p>' % te("Sign in to manage the trading bot.")
        return self._doc(tr("Sign in"), intro + message + form, here="/login")

    def _login_page(self, req, status, message="", new_token=False):
        token = req.cookies.get(LOGIN_COOKIE, "")
        headers = []
        if new_token or not _TOKEN_RE.match(token):
            token = secrets.token_urlsafe(24)
            headers.append(("Set-Cookie", _cookie(LOGIN_COOKIE, token)))
        return self._resp(status, self._login_doc(token, message), HTML_TYPE, headers)

    def _login_get(self, req):
        sid = req.cookies.get(SESSION_COOKIE)
        if sid and self.sessions.get(sid, req.now) is not None:
            return self._redirect("/")
        return self._login_page(req, 200)

    def _lock_message(self, req, trusted=False):
        until = self.limiter.locked_until(req.ip, req.now, trusted=trusted)
        mins = max(1, int(math.ceil((until - req.now) / 60.0))) if until else 1
        return box("err", tr("Signing in is locked for now after too many failed attempts. Try again in about %s "
                             "minutes.") % ltr(mins))

    def _login_post(self, req):
        t0 = time.monotonic()
        f = req.form
        cookie_tok = req.cookies.get(LOGIN_COOKIE, "")
        form_tok = f.get("lt", "")
        if not (_TOKEN_RE.match(cookie_tok) and hmac.compare_digest(cookie_tok.encode(), form_tok.encode("utf-8"))):
            return self._login_page(req, 403, box("warn", te("The sign-in form had expired. Please sign in again.")),
                                    new_token=True)
        with self._auth_lock:
            device = self.devices.check(req.cookies.get(DEVICE_COOKIE, ""), self._password_hash, req.now)
        trusted = device is not None
        allowed, _why = self.limiter.check(req.ip, req.now, trusted=trusted)
        if not allowed:
            return self._login_page(req, 429, self._lock_message(req, trusted))
        username = f.get("username", "")[:256]
        password = f.get("password", "")
        code_ = f.get("code", "")
        started = []
        with self._login_lock:
            allowed, _why = self.limiter.check(req.ip, req.now, trusted=trusted)
            if not allowed:
                return self._login_page(req, 429, self._lock_message(req, trusted))
            with self._auth_lock:
                stored, secret, last = self._password_hash, self._totp_secret, self._totp_last
            user_ok = bool(self.username) and hmac.compare_digest(_sha(username), _sha(self.username))
            pw_ok = verify_password(password, stored)                   # computed even for a wrong username
            if secret:
                code_ok, counter = verify_totp(secret, code_, req.now, last)
            else:
                code_ok, counter = True, None
            ok = user_ok and pw_ok and code_ok
            if ok:
                self.limiter.success(req.ip, req.now)
                self.devices.success(device)
                self._remember_step(secret, counter)
            else:
                started = self.limiter.failure(req.ip, req.now)
                self.devices.failure(device)
        if ok:
            old = req.cookies.get(SESSION_COOKIE)
            if old:
                self.sessions.destroy(old)
            sid = self.sessions.create(self.username, req.ip, req.now)
            self._audit("login_ok", req.ip, self.username)
            self._notify("login_ok", req.ip, self.username)
            dev_cookie = self.devices.issue(stored, req.now)
            return self._redirect("/", [("Set-Cookie", _cookie(SESSION_COOKIE, sid)),
                                        ("Set-Cookie", _cookie(LOGIN_COOKIE, "", clear=True)),
                                        ("Set-Cookie", _cookie(DEVICE_COOKIE, dev_cookie,
                                                               max_age=self.devices.max_age))])
        # a failed attempt: which name was tried is not logged unless it is the owner's (it may be a password)
        self._audit("login_failed", req.ip, self.username if user_ok else "(unknown)",
                    reason="code" if (user_ok and pw_ok) else "password")
        if started:
            self._audit("login_locked", req.ip, self.username if user_ok else "(unknown)", locks=started)
            self._notify("login_locked", req.ip, self.username if user_ok else "")
        left = MIN_FAIL_SECONDS - (time.monotonic() - t0)
        if left > 0:
            self._sleep(left)
        return self._login_page(req, 403, box("err", te("Wrong username, password or code.")))

    def _remember_step(self, secret, counter):
        """The TOTP step just used can never be used again (only moves forward, only for the same secret)."""
        if counter is None:
            return
        with self._auth_lock:
            if self._totp_secret == secret:
                self._totp_last = counter if self._totp_last is None else max(self._totp_last, counter)

    def _logout(self, req):
        self.sessions.destroy(req.sid)
        self._audit("logout", req.ip, req.user)
        return self._redirect("/login", [("Set-Cookie", _cookie(SESSION_COOKIE, "", clear=True))])

    def _reauth(self, req, password, code_):
        """The current password (None: not asked) and the current TOTP code (when 2FA is on) for a security
        change, through the limiter like a login. None when accepted, else an error text (translated). The
        session is already authenticated, so the GLOBAL lock does not apply here (the per-IP lock does)."""
        allowed, _why = self.limiter.check(req.ip, req.now, trusted=True)
        if not allowed:
            return tr("Locked for now after too many failed attempts; try again later.")
        with self._login_lock:
            with self._auth_lock:
                stored, secret, last = self._password_hash, self._totp_secret, self._totp_last
            pw_ok = password is None or verify_password(password, stored)
            code_ok, counter = (verify_totp(secret, code_, req.now, last) if secret else (True, None))
            if pw_ok and code_ok:
                self._remember_step(secret, counter)
                return None
            started = self.limiter.failure(req.ip, req.now)
        if started:
            self._audit("login_locked", req.ip, req.user, locks=started, during="reauth")
            self._notify("login_locked", req.ip, req.user)
        return tr("The current password or the code is wrong.") if password is not None else tr("The code is wrong.")

    def _reload_auth(self):
        """Re-read panel.json after the helper changed it: the file is the truth for the next login."""
        if not self.config_path:
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("panel: cannot re-read %s: %s", self.config_path, e)
            return
        if not isinstance(doc, dict):
            return
        with self._auth_lock:
            h = doc.get("password_hash")
            if is_password_hash(h) and h != self._password_hash:
                log.warning("panel: password_hash in %s differs from the one just set; using the file", self.config_path)
                self._password_hash = h
            ts = doc.get("totp_secret")
            if ts is None or (isinstance(ts, str) and _totp_ok(ts)):
                if ts != self._totp_secret:
                    log.warning("panel: totp_secret in %s differs from the one just set; using the file", self.config_path)
                    self._totp_secret = ts
                    self._totp_last = None

    # ------------------------------------------------------------------ dashboard
    def _dashboard(self, req):
        title, sub = tr("Dashboard"), tr("Live state of the bot, the account and Kimi's last decision")
        data, err = self._call(req, "status")
        if err is not None:
            return self._page(req, title, self._helper_error(err) + self._actions_card(req, None), active="/", sub=sub)
        parts = []
        lc = data.get("live_confirmed")
        if isinstance(lc, dict) and lc.get("ok") is False:
            why = lc.get("why")
            parts.append(box("warn", '<strong>%s</strong> <a href="/apply">%s</a>%s' % (
                te("Saved settings are not applied yet."), te("Apply settings"),
                ('<p class="muted">%s</p>' % bdi(why)) if why else "")))
        elif isinstance(lc, dict) and lc.get("ok") is True:
            parts.append(box("ok", te("Live trading is confirmed for the settings in force.")))
        if data.get("state_error"):
            parts.append(box("warn", tr("The bot's state could not be read completely: %s") % bdi(data.get("state_error"))))
        eq = data.get("equity") if isinstance(data.get("equity"), dict) else None
        if eq and eq.get("halted") is True:
            parts.append(box("err", tr("<strong>Trading is halted:</strong> the account fell to the drawdown limit.")))
        bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
        sp = data.get("spend") if isinstance(data.get("spend"), dict) else {}
        parts.append(self._kpis(eq or {}, sp, data.get("resting_orders")))
        pos = data.get("positions") if isinstance(data.get("positions"), list) else []
        parts.append('<div class="dash"><div class="col">%s</div><div class="col">%s%s</div></div>' % (
            self._decision_card(data.get("last_decision")), self._services_card(data, bot),
            self._actions_card(req, bot.get("state"))))
        parts.append(self._positions_card(pos))
        for key, label in (("status_fa", N_("Status, as /status shows it in Telegram")),
                           ("last_decision_fa", N_("Last decision, as /last shows it in Telegram"))):
            if data.get(key):
                parts.append('<details class="card fold"><summary>%s<span>%s</span></summary><div class="card-b">%s'
                             '</div></details>' % (icon("chat"), te(label), pre_fa(data.get(key))))
        return self._page(req, title, "".join(parts), active="/", sub=sub)

    def _kpis(self, eq, sp, resting):
        irt, start, hwm = eq.get("irt"), eq.get("start_irt"), eq.get("hwm_irt")
        dd, halt = eq.get("drawdown_pct"), eq.get("halt_pct")
        cards = [kpi("wallet", tr("Account value"), fmt_num(irt, 0), tr("IRT") if _is_num(irt) else "",
                     (tr("Updated %s") % fmt_time(eq.get("time"))) if eq.get("time") else "")]
        if _is_num(irt) and _is_num(start) and start > 0:
            change = (irt / float(start) - 1.0) * 100.0
            cls = "up" if change > 0 else ("down" if change < 0 else "")
            value = '<span class="delta %s">%s%s</span>' % (cls, icon("down" if change < 0 else "up"),
                                                            ltr("%+.2f%%" % change))
            cards.append(kpi("trend", tr("Profit / loss"), value, "", tr("Started with %s IRT") % fmt_num(start, 0)))
        else:
            cards.append(kpi("trend", tr("Profit / loss"), dash()))
        bar, subs = "", []
        if _is_num(dd) and _is_num(halt) and halt > 0:
            used = max(0.0, min(100.0, dd / float(halt) * 100.0))
            bar = '<progress class="bar %s" max="100" value="%.1f">%.1f%%</progress>' % (
                "ok" if used < 50 else ("warn" if used < 80 else "err"), used, used)
        if _is_num(halt):
            subs.append(tr("Trading halts at %s") % ltr(pct_text(halt)))
        if _is_num(hwm):
            subs.append(tr("peak %s") % fmt_num(hwm, 0))
        cards.append(kpi("drop", tr("Drawdown"), ltr(pct_text(dd)) if _is_num(dd) else dash(), "",
                         " &middot; ".join(subs), bar))
        cards.append(kpi("coins", tr("Model cost today"), usd(sp.get("today_usd")), "",
                         tr("this month %s &middot; in total %s") % (usd(sp.get("month_usd")), usd(sp.get("total_usd")))))
        cards.append(kpi("orders", tr("Resting limit orders"), fmt_num(resting), "", te("on Bitpin's order book")))
        return '<div class="kpis">%s</div>' % "".join(cards)

    def _decision_card(self, d):
        title = te("Kimi's last decision")
        if not isinstance(d, dict) or not d:
            return card(title, '<p class="muted empty">%s</p>' % te("No decision recorded yet."), "spark")
        targets = d.get("targets") if isinstance(d.get("targets"), dict) else {}
        tl = sorted(((w, str(s)) for s, w in targets.items() if _is_num(w) and w > 0), key=lambda x: (-x[0], x[1]))
        hold = d.get("hold")
        if hold:
            result = '<span class="badge">%s</span>' % te("Hold")
        elif hold is False:
            result = '<span class="badge accent">%s</span>' % te("Trade (new weights)")
        else:
            result = dash()
        conf = d.get("confidence")
        conf_html = ltr(pct_text(conf * 100, 0)) if _is_num(conf) and 0 <= conf <= 1 else fmt_num(conf)
        rows = [(N_("Result"), result), (N_("Mode"), ltr(d.get("mode")) if d.get("mode") else dash()),
                (N_("Confidence"), conf_html), (N_("Model"), code(d.get("model")) if d.get("model") else dash()),
                (N_("Valid"), yes_no(d.get("valid"))), (N_("Fallback"), yes_no(d.get("fallback")))]
        out = [facts(rows)]
        if d.get("error"):
            kind = (" (%s)" % ltr(d.get("error_kind"))) if d.get("error_kind") else ""
            out.append(box("err", tr("Error%s: %s") % (kind, bdi(d.get("error")))))
        out.append("<h3>%s</h3>" % te("Target weights"))
        if tl:
            out.append('<div class="weights">%s</div>' % "".join(
                '<div class="w">%s<progress class="bar" max="100" value="%.1f">%.1f%%</progress>%s</div>' % (
                    code(sym), w * 100.0, w * 100.0, ltr("%.1f%%" % (w * 100.0))) for w, sym in tl))
        else:
            out.append('<p class="muted">%s</p>' % te("No target above zero."))
        if d.get("report_fa"):
            out.append('<h3>%s</h3><div class="report" dir="rtl" lang="fa">%s</div>' % (
                te("Kimi's report"), esc(d.get("report_fa"))))
        when = '<span class="muted small">%s</span>' % fmt_time(d.get("time")) if d.get("time") else ""
        return card(title, "".join(out), "spark", when)

    def _positions_card(self, pos):
        kinds = {"allocation": N_("Allocation"), "ladder": N_("Ladder")}
        rows = []
        for p in pos:
            if not isinstance(p, dict):
                continue
            k = p.get("kind")
            rows.append([code(p.get("symbol")), '<span class="badge">%s</span>' % te(kinds.get(k, k or "")),
                         fmt_num(p.get("amount")), fmt_num(p.get("entry_px_usdt")), fmt_num(p.get("stop_px_usdt")),
                         fmt_num(p.get("target_px_usdt")), fmt_time(p.get("max_hold_until"))])
        return card(te("Positions"), table([N_("Market"), N_("Kind"), N_("Amount"), N_("Entry (USDT)"),
                                            N_("Stop (USDT)"), N_("Target (USDT)"), N_("Hold until")], rows,
                                           num=(2, 3, 4, 5)), "layers", cls="flush")

    def _services_card(self, data, bot):
        items = []
        for key, label, unit in (("bot", N_("Trading bot"), "bitpin-bot"),
                                 ("notifier", N_("Telegram notifier"), "bitpin-bot-notify"),
                                 ("tunnel", N_("VPN tunnel"), "xray-tunnel"), ("panel", N_("Panel"), "bitpin-bot-panel")):
            s = data.get(key) if isinstance(data.get(key), dict) else {}
            en = s.get("enabled")
            boot = ""
            if en not in (None, "", "enabled"):
                boot = ' <span class="badge warn">%s</span>' % (tr("at boot: %s") % esc(en))
            since = ('<small>%s</small>' % (tr("since %s") % fmt_time(s.get("since")))) if s.get("since") else ""
            items.append('<li><div class="svc-n"><b>%s</b>%s</div><div class="svc-s">%s%s%s</div></li>' % (
                te(label), code(unit), state_html(s.get("state"), s.get("sub")), boot, since))
        ver = ('<p class="muted small">%s</p>' % (tr("Bot version %s") % ltr(bot.get("version")))) if bot.get("version") else ""
        return card(te("Services"), '<ul class="svc">%s</ul>%s' % ("".join(items), ver), "server")

    def _svc_button(self, req, unit, action, label, cls, back="/", icon_name=None):
        return self._form(req, "/service", hidden("unit", unit) + hidden("action", action) + hidden("back", back)
                          + button(tr(label), cls, icon_name), "inline")

    def _actions_card(self, req, bot_state):
        restart = self._svc_button(req, "bitpin-bot", "restart", N_("Restart the bot"), "danger", icon_name="refresh")
        stop = self._svc_button(req, "bitpin-bot", "stop", N_("Stop the bot"), "secondary", icon_name="stop")
        start = self._svc_button(req, "bitpin-bot", "start", N_("Start the bot"), "danger", icon_name="play")
        if bot_state in ("active", "activating", "reloading"):
            bot = restart + stop
        elif bot_state:
            bot = start
        else:                                                   # the state is not known (helper error)
            bot = restart + start + stop
        notifier = self._svc_button(req, "bitpin-bot-notify", "restart", N_("Restart the notifier"), "secondary",
                                    icon_name="refresh")
        return card(te("Actions"), '<div class="actions">%s%s%s</div>' % (
            bot, notifier, link_button("/health", tr("Health check"), "secondary", "pulse")), "zap")

    def _service_post(self, req):
        unit, action = req.form.get("unit", ""), req.form.get("action", "")
        back = req.form.get("back", "/")
        back = back if back in BACK_PAGES else "/"
        if unit not in SERVICE_UNITS or action not in UNIT_ACTIONS:
            self._audit_action(req, "service", "invalid unit or action")
            self._flash(req, box("err", te("Unknown service or action.")))
            return self._redirect(back)
        data, err = self._act(req, "service", {"unit": unit, "action": action}, unit=unit, action=action)
        if err is not None:
            self._flash(req, self._helper_error(err, apply_link=(unit == "bitpin-bot")))
        else:
            ok = data.get("ok") is not False
            self._flash(req, box("ok" if ok else "err", tr("%s %s: %s. State now: %s") % (
                code(unit), ltr(action), te("done") if ok else te("failed"), state_html(data.get("state"))))
                + (pre(data.get("output")) if data.get("output") else ""))
        return self._redirect(back)

    def _health_get(self, req):
        data, err = self._call(req, "health")
        if err is not None:
            body = self._helper_error(err)
        else:
            ok = data.get("ok")
            head = box("ok", te("Healthy.")) if ok is True else (
                box("err", te("A problem was reported.")) if ok is False else "")
            body = head + card(te("Report"), pre(data.get("text", ""), "term"), "pulse")
        return self._page(req, tr("Health check"), body, active="/", sub=tr("The output of bitpin-bot health"))

    # ------------------------------------------------------------------ results of a config write
    @staticmethod
    def _changed_table(changed):
        rows = []
        for c in changed if isinstance(changed, list) else []:
            if not isinstance(c, dict):
                continue
            p = c.get("path")
            p = ".".join(str(x) for x in p) if isinstance(p, list) else str(p)
            rows.append([code("%s:%s" % (c.get("file"), p)), code(jtext(c.get("old"))), code(jtext(c.get("new")))])
        return rows

    def _put_result(self, res, dry_run, confirm_html=None):
        """problems / warnings / diff / written / backup of config_put, settings_set and model_set."""
        if not isinstance(res, dict):
            return ""
        out = []
        problems = [p for p in res.get("problems") or [] if p is not None] if isinstance(res.get("problems"), list) else []
        warnings = [w for w in res.get("warnings") or [] if w is not None] if isinstance(res.get("warnings"), list) else []
        if problems:
            out.append(box("err", "<strong>%s</strong>%s" % (te("Problems (nothing is saved)"), ul(problems))))
        if warnings:
            out.append(box("warn", "<strong>%s</strong>%s" % (te("Warnings"), ul(warnings))))
        if dry_run:
            if not problems:
                out.append(box("ok", te("Checked: no problem found. Nothing is saved yet.")))
        elif res.get("written"):
            bk = res.get("backup")
            out.append(box("ok", te("Saved.") + ((" " + tr("Backup: %s") % code(bk)) if bk else "")))
            if res.get("confirm_needed"):
                out.append(confirm_html if confirm_html is not None else self._confirm_notice(True))
        elif not problems:
            out.append(box("info", te("Nothing had changed, so nothing was saved.")))
        else:
            out.append(box("err", te("Not saved.")))
        diff = res.get("diff")
        if diff:
            out.append("<h3>%s</h3>%s" % (te("Differences"), diff_html(diff)))
        elif not problems:
            out.append('<p class="muted">%s</p>' % te("No difference from the file on the server."))
        return "".join(out)

    def _confirm_notice(self, link=False):
        return box("warn", "<strong>%s</strong> %s%s" % (
            te("The settings are saved but not applied yet."),
            tr("Until the new settings are confirmed in Apply settings the bot keeps trading with the "
               "<strong>previous</strong> ones, and starting or restarting the bot fails."),
            (' <a href="/apply">%s</a>' % te("Go to Apply settings")) if link else ""))

    # ------------------------------------------------------------------ models and keys
    def _kimi_doc(self, req):
        """(kimi.json as a dict, error html)."""
        data, err = self._call(req, "config_get")
        if err is not None:
            return None, self._helper_error(err)
        try:
            doc = json.loads(data.get("kimi") or "")
        except (TypeError, ValueError):
            return None, box("err", te("kimi.json cannot be read (invalid JSON). Fix it in the JSON editor."))
        if not isinstance(doc, dict):
            return None, box("err", te("kimi.json is not a JSON object."))
        return doc, ""

    @staticmethod
    def _stage_info(doc, stage):
        sec = doc.get(stage) if isinstance(doc, dict) and isinstance(doc.get(stage), dict) else None
        if sec is None:
            return None
        base = sec.get("base_url") if isinstance(sec.get("base_url"), str) else ""
        prov = sec.get("provider") if sec.get("provider") in ("moonshot", "openrouter", "openai") else ""
        prov = prov or infer_provider(base)
        return {"provider": prov, "base_url": base, "model": sec.get("model"),
                "reasoning_effort": sec.get("reasoning_effort"),
                "key_name": sec.get("api_key_env") or "KIMI_API_KEY",
                "price_in_per_m": sec.get("price_in_per_m"), "price_out_per_m": sec.get("price_out_per_m"),
                "price_cached_in_per_m": sec.get("price_cached_in_per_m")}

    def _models_get(self, req):
        return self._models_page(req)

    def _models_page(self, req, listing="", set_values=None, set_result="", status=200, list_values=None):
        doc, err_html = self._kimi_doc(req)
        parts = []
        if doc is None:
            parts.append(err_html)
        else:
            rows = []
            for stage, label in (("llm", N_("Decisions (llm)")), ("news", N_("News (news)"))):
                si = self._stage_info(doc, stage)
                if si is None:
                    rows.append([te(label), '<span class="muted">%s</span>' % te("not in kimi.json, or switched off"),
                                 "", "", "", ""])
                    continue
                re_ = si["reasoning_effort"]
                rows.append([te(label), te(PROVIDER_LABELS.get(si["provider"], si["provider"] or "")),
                             code(si["base_url"]) if si["base_url"] else dash(),
                             code(si["model"]) if si["model"] else dash(), ltr(re_) if re_ else dash(),
                             code(si["key_name"])])
            parts.append(card(te("Models in use"), table(
                [N_("Stage"), N_("Provider"), N_("Address"), N_("Model"), N_("Reasoning"), N_("Key")], rows),
                "cpu", cls="flush"))
        if set_values is None:
            set_values = self._set_defaults(req, doc)
        parts.append('<div class="grid2">%s%s</div>' % (self._set_form(req, set_values, set_result),
                                                        self._list_form(req, list_values)))
        if listing:
            parts.append(listing)
        parts.append(self._keys_card(req, self._generic_base_url(doc, set_values)))
        return self._page(req, tr("Models & keys"), "".join(parts), status=status, active="/models",
                          sub=tr("The model that decides, the one that reads the news, and the API keys"))

    def _generic_base_url(self, doc, set_values):
        """The https address LLM_API_KEY belongs to (the helper binds that key to one host): the model form's
        address when its provider is "other", else the address of a stage that already uses that provider."""
        v = set_values or {}
        if v.get("provider") == "openai" and v.get("base_url"):
            return v.get("base_url")
        for stage in ("llm", "news"):
            si = self._stage_info(doc or {}, stage)
            if si and si["provider"] == "openai" and si["base_url"]:
                return si["base_url"]
        return ""

    def _keys_card(self, req, generic_url=""):
        data, err = self._call(req, "secrets_status")
        if err is not None:
            status_html = self._helper_error(err)
            data = {}
        else:
            rows = []
            for name in list(SECRET_NAMES) + sorted(k for k in data if k not in SECRET_NAMES):
                if isinstance(data.get(name), dict):
                    rows.append([code(name), self._key_state(data.get(name))])
            status_html = table([N_("Name"), N_("State")], rows)
        prow = [[te(PROVIDER_LABELS[p]), code(k), self._key_state(data.get(k))] for p, k in PROVIDER_KEYS]
        mapping = "<h3>%s</h3>%s%s" % (te("The key of each provider"), help_p(te(
            "The key follows the provider and is sent only to that provider.")), table(
            [N_("Provider"), N_("Key"), N_("State")], prow))
        inner = ('<div class="row">%s%s</div>%s%s<div class="actions">%s</div>') % (
            field(te("Key name"), select("name", [(n, n) for n in SECRET_NAMES], "KIMI_API_KEY", "k-name", "ltr"),
                  for_="k-name"),
            field(te("New value"), '<input id="k-value" type="password" name="value" class="ltr" '
                                   'autocomplete="new-password" spellcheck="false" maxlength="%d">' % MAX_SECRET_VALUE,
                  for_="k-value"),
            field(te("Service address (LLM_API_KEY only)"),
                  '<input id="k-base" name="base_url" class="ltr" value="%s" placeholder="https://..." maxlength="200" '
                  'spellcheck="false">' % esc(generic_url),
                  help_p(te("LLM_API_KEY is bound to this host and is never sent anywhere else.")), for_="k-base"),
            field("", checkbox("remove", tr("Remove this key")),
                  help_p(te("The value is write-only: the panel never shows it. After saving, restart the bot."))),
            button(tr("Save the key"), "primary", "key"))
        return card(te("API keys"), '<div class="grid2 flat"><div>%s%s</div><div><h3>%s</h3>%s</div></div>' % (
            status_html, mapping, te("Add or change a key"), self._form(req, "/models/key", inner)), "key")

    @staticmethod
    def _key_state(st):
        if not isinstance(st, dict):
            return dash()
        if st.get("set"):
            n = st.get("length")
            return '<span class="pill ok">%s</span>' % (tr("set, %s characters") % ltr(
                n if isinstance(n, int) and not isinstance(n, bool) else "?"))
        return '<span class="pill">%s</span>' % te("not set")

    def _list_form(self, req, values=None):
        v = values or {}
        inner = ('%s%s%s<div class="actions">%s</div>') % (
            field(te("Provider"), select("provider", [(p, tr(PROVIDER_LABELS[p])) for p in
                                                      ("moonshot", "openrouter", "openai", "auto")],
                                         v.get("provider", "moonshot"), "l-provider"), for_="l-provider"),
            field(te("API address (base_url)"),
                  '<input id="l-base" name="base_url" class="ltr" value="%s" placeholder="https://..." '
                  'maxlength="200">' % esc(v.get("base_url", "")),
                  help_p(tr("Empty = the provider's default address (Moonshot: %s, OpenRouter: %s). The key follows "
                            "the provider.") % (code(provider_url("moonshot")), code(provider_url("openrouter")))),
                  for_="l-base"),
            field(te("Filter by model name (optional)"),
                  '<input id="l-q" name="q" class="ltr" value="%s" maxlength="100">' % esc(v.get("q", "")), for_="l-q"),
            button(tr("Load the model list"), "secondary", "search"))
        return card(te("Browse a provider's models"), self._form(req, "/models/list", inner), "search")

    def _set_defaults(self, req, doc):
        q = req.query
        keys = ("stage", "provider", "base_url", "model", "reasoning_effort", "price_in_per_m", "price_out_per_m",
                "price_cached_in_per_m")
        if q.get("model"):
            vals = dict((k, q.get(k, "")) for k in keys)
            if vals["stage"] not in ("llm", "news"):
                vals["stage"] = "llm"
            return vals
        si = self._stage_info(doc or {}, "llm") or {}
        return {"stage": "llm", "provider": si.get("provider") or "moonshot", "base_url": si.get("base_url") or "",
                "model": si.get("model") or "", "reasoning_effort": si.get("reasoning_effort") or "",
                "price_in_per_m": _num_text(si.get("price_in_per_m")), "price_out_per_m": _num_text(si.get("price_out_per_m")),
                "price_cached_in_per_m": _num_text(si.get("price_cached_in_per_m"))}

    def _set_form(self, req, v, result=""):
        def inp(name, label):
            return field(te(label), '<input id="s-%s" name="%s" class="ltr" inputmode="decimal" value="%s" '
                                    'maxlength="20">' % (name, name, esc(v.get(name, ""))), for_="s-" + name)
        re_opts = [("", tr("Not sent (the API's default)"))] + [(x, x) for x in REASONING_EFFORTS if x]
        inner = ('<div class="row">%s%s</div>%s%s%s<h3>%s</h3><div class="row">%s%s%s</div>%s'
                 '<div class="actions">%s%s</div>') % (
            field(te("Stage"), select("stage", [("llm", tr("Decisions (llm)")), ("news", tr("News (news)"))],
                                      v.get("stage", "llm"), "s-stage"), for_="s-stage"),
            field(te("Provider"), select("provider", [(p, tr(PROVIDER_LABELS[p])) for p in
                                                      ("moonshot", "openrouter", "openai", "auto")],
                                         v.get("provider", "moonshot"), "s-provider"), for_="s-provider"),
            field(te("API address (base_url)"),
                  '<input id="s-base" name="base_url" class="ltr" value="%s" maxlength="200" '
                  'placeholder="https://...">' % esc(v.get("base_url", "")),
                  help_p(tr("Empty = the provider's default address. Keys: Moonshot uses %s, OpenRouter %s, "
                            "other %s.") % (code(provider_key("moonshot")), code(provider_key("openrouter")),
                                            code(provider_key("openai")))), for_="s-base"),
            field(te("Model id"), '<input id="s-model" name="model" class="ltr" value="%s" maxlength="200" required '
                                  'spellcheck="false">' % esc(v.get("model", "")), for_="s-model"),
            field(te("Reasoning effort (decision stage only)"),
                  select("reasoning_effort", re_opts, v.get("reasoning_effort", "") or "", "s-re"), for_="s-re"),
            te("Prices: US dollars per million tokens"),
            inp("price_in_per_m", N_("Input")), inp("price_out_per_m", N_("Output")),
            inp("price_cached_in_per_m", N_("Cached input")),
            help_p(te("All empty = the prices stay as they are. An empty cached price = the input price.")),
            button(tr("Check (no save)"), "secondary", "eye", "do", "check"), button(tr("Save"), "primary", "save", "do", "save"))
        return card(te("Choose a model"), result + self._form(req, "/models/set", inner), "cpu")

    def _models_list(self, req):
        f = req.form
        provider = f.get("provider", "moonshot")
        base_url = f.get("base_url", "").strip()
        vals = {"provider": provider, "base_url": base_url, "q": f.get("q", "")[:100]}
        base_url = base_url or provider_url(provider)
        problem = None
        if provider not in ("auto", "moonshot", "openrouter", "openai"):
            problem = tr("Unknown provider.")
        elif not _URL_RE.match(base_url):
            problem = tr("The API address must start with https:// (enter the address for \"Other\" and \"Detect from "
                         "the address\").")
        if problem:
            self._audit_action(req, "models", problem)
            return self._models_page(req, listing=box("err", esc(problem)), list_values=vals, status=400)
        data, err = self._act(req, "models", {"provider": provider, "base_url": base_url}, base_url=base_url,
                              provider=provider)
        if err is not None:
            return self._models_page(req, listing=self._helper_error(err), list_values=vals)
        models = data.get("models") if isinstance(data.get("models"), list) else []
        needle = vals["q"].strip().lower()
        used_provider = data.get("provider") if data.get("provider") in ("moonshot", "openrouter", "openai") else (
            provider if provider != "auto" else infer_provider(base_url) or "openai")
        rows = []
        for m in models:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            mid = str(m.get("id"))
            if needle and needle not in mid.lower() and needle not in str(m.get("name") or "").lower():
                continue
            link = "/models?" + urlencode([
                ("stage", "llm"), ("provider", used_provider), ("base_url", base_url), ("model", mid),
                ("price_in_per_m", _num_text(m.get("price_in_per_m"))),
                ("price_out_per_m", _num_text(m.get("price_out_per_m"))),
                ("price_cached_in_per_m", _num_text(m.get("price_cached_in_per_m")))])
            rows.append([code(mid), bdi(m.get("name") or ""), fmt_num(m.get("context_length")),
                         fmt_num(m.get("price_in_per_m")), fmt_num(m.get("price_out_per_m")),
                         fmt_num(m.get("price_cached_in_per_m")), yes_no(m.get("supports_json")),
                         yes_no(m.get("supports_reasoning")),
                         '<a class="btn secondary sm" href="%s">%s</a>' % (esc(link), te("Choose"))])
        head = "<p>%s</p>" % (tr("Provider: %s &#8212; key: %s") % (
            te(PROVIDER_LABELS.get(used_provider, used_provider)), code(data.get("key_name") or provider_key(used_provider))))
        listing = card(tr("Models (%s)") % ltr(len(rows)), head + table(
            [N_("Id"), N_("Name"), N_("Context"), N_("Input $/M"), N_("Output $/M"), N_("Cached $/M"), "JSON",
             N_("Reasoning"), ""], rows, num=(2, 3, 4, 5)), "list")
        return self._models_page(req, listing=listing, list_values=vals)

    def _models_set(self, req):
        f = req.form
        v = dict((k, f.get(k, "").strip()) for k in ("stage", "provider", "base_url", "model", "reasoning_effort",
                                                      "price_in_per_m", "price_out_per_m", "price_cached_in_per_m"))
        dry = f.get("do", "check") != "save"
        base_url = v["base_url"] or provider_url(v["provider"])
        problems = []
        if v["stage"] not in ("llm", "news"):
            problems.append(tr("Unknown stage."))
        if v["provider"] not in ("auto", "moonshot", "openrouter", "openai"):
            problems.append(tr("Unknown provider."))
        if not _URL_RE.match(base_url):
            problems.append(tr("The API address must start with https://."))
        if not _MODEL_ID_RE.match(v["model"]):
            problems.append(tr("The model id is empty or not valid."))
        if v["reasoning_effort"] not in REASONING_EFFORTS:
            problems.append(tr("Unknown reasoning effort."))
        prices, perr = _parse_prices(v)
        if perr:
            problems.append(perr)
        if problems:
            self._audit_action(req, "model_set", "invalid input", dry_run=dry)
            return self._models_page(req, set_values=v, set_result=box("err", ul(problems)), status=400)
        args = {"stage": v["stage"], "provider": v["provider"], "base_url": base_url, "model": v["model"],
                "reasoning_effort": v["reasoning_effort"] or None, "prices": prices, "dry_run": dry}
        data, err = self._act(req, "model_set", {"stage": v["stage"], "model": v["model"], "provider": v["provider"],
                                                 "dry_run": dry}, **args)
        if err is not None:
            return self._models_page(req, set_values=v, set_result=self._helper_error(err))
        rows = self._changed_table(data.get("changed"))
        result = (table([N_("Key"), N_("Old value"), N_("New value")], rows) if rows else "") + self._put_result(data, dry)
        if not dry and data.get("written"):
            self._flash(req, card(te("Model saved"), result, "cpu"))
            return self._redirect("/models")
        return self._models_page(req, set_values=v, set_result=result)

    def _models_key(self, req):
        f = req.form
        name = f.get("name", "")
        value = f.get("value", "")
        remove = f.get("remove") == "1"
        base_url = f.get("base_url", "").strip()
        problem = None
        if name not in SECRET_NAMES:
            problem = tr("Unknown key name.")
        elif len(value) > MAX_SECRET_VALUE:
            problem = tr("The value is too long.")
        elif value and remove:
            problem = tr("Either enter a new value or tick Remove, not both.")
        elif not value and not remove:
            problem = tr("The value is empty. To remove the key, tick Remove.")
        elif any(c.isspace() for c in value):
            problem = tr("A key must not contain spaces or line breaks.")
        elif name == "LLM_API_KEY" and value and not _URL_RE.match(base_url):
            problem = tr("For LLM_API_KEY enter the https address of the service the key belongs to.")
        if problem:
            self._audit_action(req, "secret_set", "invalid input", name=name if name in SECRET_NAMES else "?")
            self._flash(req, box("err", esc(problem)))
            return self._redirect("/models")
        args = {"name": name, "value": value}
        audit = {"name": name, "remove": remove}
        if name == "LLM_API_KEY" and value:
            args["base_url"] = audit["base_url"] = base_url
        data, err = self._act(req, "secret_set", audit, hide=(value,), **args)
        if err is not None:
            self._flash(req, self._helper_error(err))
            return self._redirect("/models")
        n = data.get("length")
        if data.get("set"):
            msg = tr("Key %s saved: %s characters.") % (code(name), ltr(
                n if isinstance(n, int) and not isinstance(n, bool) else "?"))
        else:
            msg = tr("Key %s removed.") % code(name)
        if data.get("restart_needed", True):
            msg += "<p>%s</p>%s" % (te("Restart the bot for the change to take effect."), self._svc_button(
                req, "bitpin-bot", "restart", N_("Restart the bot"), "danger sm", "/models", "refresh"))
        self._flash(req, box("ok", msg))
        return self._redirect("/models")

    # ------------------------------------------------------------------ JSON editors
    def _settings_get(self, req):
        return self._settings_page(req)

    def _settings_page(self, req, edited=None, result_for=None, result="", status=200):
        """edited: (file, text) kept in its editor; result shown above that editor."""
        title, sub = tr("JSON editor"), tr("config.json and kimi.json as they are on the server")
        data, err = self._call(req, "config_get")
        parts = []
        if err is not None and edited is None:
            return self._page(req, title, self._helper_error(err), status=status, active="/settings", sub=sub)
        if err is not None:
            parts.append(self._helper_error(err))
            data = {}
        parts.append(box("info", tr("Edit config.json and kimi.json directly. Check only validates; Save validates, "
                                    "writes and keeps a backup of the previous file. Most trade settings are easier "
                                    "to change in <a href=\"/trade\">Trade settings</a>.")))
        for fname, title_ in (("config", "config.json"), ("kimi", "kimi.json")):
            if edited is not None and edited[0] == fname:
                text = edited[1]
            else:
                text = data.get(fname) if isinstance(data.get(fname), str) else ""
            inner = hidden("file", fname) + textarea("text", text, "code", "ed-" + fname) + (
                '<div class="actions">%s%s</div>' % (button(tr("Check"), "secondary", "eye", "do", "check"),
                                                     button(tr("Save"), "primary", "save", "do", "save")))
            res = result if result_for == fname else ""
            parts.append(card(code(title_), res + self._form(req, "/settings", inner), "braces"))
        return self._page(req, title, "".join(parts), status=status, active="/settings", sub=sub)

    def _settings_post(self, req):
        fname = req.form.get("file", "")
        text = req.form.get("text", "").replace("\r\n", "\n")
        dry = req.form.get("do", "check") != "save"
        if fname not in ("config", "kimi") or not text.strip() or len(text) > MAX_CONFIG_TEXT:
            self._audit_action(req, "config_put", "invalid input", dry_run=dry)
            return self._settings_page(req, status=400, result_for=fname if fname in ("config", "kimi") else None,
                                       result=box("err", te("Invalid request (unknown file, empty text or more than "
                                                            "1 MB).")))
        data, err = self._act(req, "config_put", {"file": fname, "dry_run": dry}, file=fname, text=text, dry_run=dry)
        if err is not None:
            return self._settings_page(req, (fname, text), fname, self._helper_error(err))
        if not dry and data.get("written"):
            self._flash(req, card(code(fname + ".json"), self._put_result(data, False), "braces"))
            return self._redirect("/settings")
        return self._settings_page(req, (fname, text), fname, self._put_result(data, dry))

    # ------------------------------------------------------------------ trade settings form
    def _settings_module(self):
        try:
            from . import panel_settings
            return panel_settings, None
        except Exception as e:                   # the page reports it, the rest of the panel keeps working
            log.warning("panel: bitpin.panel_settings cannot be imported: %s", e)
            return None, box("err", tr("The trade settings form is not available: %s") % bdi(e))

    def _trade_get(self, req):
        return self._trade_page(req)

    def _trade_page(self, req, posted=None, errors=None, result="", status=200):
        title = tr("Trade settings")
        sub = tr("Kimi's style, the risk guards, the schedule, the markets and the model budget")
        ps, err_html = self._settings_module()
        if ps is None:
            return self._page(req, title, err_html, status=500, active="/trade", sub=sub)
        values = posted
        if values is None:
            data, err = self._call(req, "config_get")
            if err is not None:
                return self._page(req, title, result + self._helper_error(err), status=status, active="/trade", sub=sub)
            try:
                cdoc, kdoc = ps.load_docs(data.get("config") or "", data.get("kimi") or "")
                values = ps.form_values(cdoc, kdoc)
            except Exception as e:
                log.warning("panel: trade form values: %s", e)
                return self._page(req, title, result + box("err", tr(
                    "The settings files cannot be read: %s. Fix them in the <a href=\"/settings\">JSON editor</a>.")
                    % bdi(e)), status=status, active="/trade", sub=sub)
        errors = errors or {}
        parts = [result]
        if errors:
            parts.append(box("err", te("Some values are not valid; each error is shown next to its field. Nothing "
                                       "was saved.")))
        parts.append(box("info", te("Preview only shows what would change. After Save, the new settings must be "
                                    "confirmed in Apply settings before the bot uses them.")))
        groups = []
        for gid, gtitle, intro in ps.GROUPS:
            fields = [f for f in ps.FIELDS if f.group == gid]
            if fields:
                groups.append((gid, gtitle, intro, fields))
        toc = '<nav class="toc" aria-label="%s"><p class="nav-h">%s</p>%s</nav>' % (
            te("Sections"), te("Sections"), "".join('<a href="#g-%s">%s%s</a>' % (
                gid, icon(GROUP_ICONS.get(gid, "sliders")), esc(tr(gt))) for gid, gt, _i, _f in groups))
        sections = []
        for gid, gtitle, intro, fields in groups:
            bad = sum(1 for f in fields if f.key in errors)
            badge = ('<span class="badge err">%s</span>' % (tr("%s to fix") % ltr(bad))) if bad else ""
            inner = "".join(self._trade_field(ps, f, values.get(f.key, ""), errors.get(f.key)) for f in fields)
            sections.append(card(esc(tr(gtitle)), ('<p class="intro">%s</p>' % esc(tr(intro)) if intro else "")
                                 + '<div class="fields">%s</div>' % inner, GROUP_ICONS.get(gid, "sliders"),
                                 badge, "group", "g-" + gid))
        bar = '<div class="actionbar"><p class="muted">%s</p><div class="actions">%s%s</div></div>' % (
            te("Preview shows the changes without saving anything."),
            button(tr("Preview changes"), "secondary", "eye", "action", "preview"),
            button(tr("Save"), "primary", "save", "action", "save"))
        parts.append('<div class="with-toc">%s<div class="toc-main">%s</div></div>' % (
            toc, self._form(req, "/trade", "".join(sections) + bar, "trade")))
        return self._page(req, title, "".join(parts), status=status, active="/trade", sub=sub)

    def _trade_field(self, ps, f, value, error):
        fid = "f-" + re.sub(r"[^A-Za-z0-9_-]", "-", f.key)
        k = f.kind
        value = "" if value is None else str(value)
        keytag = ' <code class="key" dir="ltr">%s</code>' % esc(f.dotted)
        max_len = getattr(f, "max_len", None)
        maxlen = ' maxlength="%d"' % max_len if max_len else ""
        label = esc(tr(f.label))
        cls = " has-err" if error else ""
        if k == "bool":
            cls += " sw"
            ctl = ('<label class="check" for="%s"><input type="checkbox" class="switch" id="%s" name="%s" value="on"%s>'
                   ' <span>%s%s</span></label>') % (fid, fid, esc(f.key), " checked" if value == "on" else "", label,
                                                   keytag)
        else:
            if k in ("longtext", "symbols", "domains"):
                cls += " wide"
            if k in ("int", "float", "pct"):
                unit = tr(f.unit) if f.unit else ""
                ctl = ('<div class="control"><input type="text" inputmode="decimal" id="%s" name="%s" value="%s" '
                       'class="ltr" dir="ltr" spellcheck="false" autocomplete="off">%s</div>') % (
                    fid, esc(f.key), esc(value), ('<span class="unit">%s</span>' % esc(unit)) if unit else "")
            elif k == "choice":
                opts = [("null" if cv is None else str(cv), tr(cl)) for cv, cl in f.choices]
                ctl = select(f.key, opts, value, fid)
            elif k == "longtext":
                ctl = '<textarea id="%s" name="%s" class="ltr short" dir="ltr" rows="6"%s>\n%s</textarea>' % (
                    fid, esc(f.key), maxlen, esc(value))
            elif k == "domains":                          # v3.5: the trusted news sites, one per line
                ctl = ('<textarea id="%s" name="%s" class="ltr short" dir="ltr" rows="8" spellcheck="false" '
                       'autocomplete="off">\n%s</textarea>') % (fid, esc(f.key), esc(value))
            else:
                ctl = ('<input type="text" id="%s" name="%s" value="%s" class="ltr" dir="ltr" spellcheck="false" '
                       'autocomplete="off"%s>') % (fid, esc(f.key), esc(value), maxlen)
            ctl = '<label for="%s">%s%s</label>%s' % (fid, label, keytag, ctl)
        helps = help_p(esc(tr(f.help))) if f.help else ""
        try:
            more = ps.help_en(f, self.root_dir)
        except Exception:
            more = None
        if more:
            helps += '<details class="more"><summary>%s</summary><p class="ltr" dir="ltr" lang="en">%s</p></details>' % (
                te("More detail (in English)") if current() != "en" else te("More detail"), esc(more))
        err = '<p class="field-err">%s</p>' % esc(tr(error)) if error else ""
        return '<div class="field%s">%s%s%s</div>' % (cls, ctl, err, helps)

    def _trade_post(self, req):
        ps, err_html = self._settings_module()
        if ps is None:
            return self._page(req, tr("Trade settings"), err_html, status=500, active="/trade")
        action = req.form.get("action", "")
        if action not in ("preview", "save"):
            self._audit_action(req, "settings_set", "invalid action")
            return self._trade_page(req, result=box("err", te("Invalid request.")), status=400)
        form = dict((k, v) for k, v in req.form.items() if k in ps.BY_KEY)
        posted = {}
        for f in ps.FIELDS:
            if f.kind == "bool":
                posted[f.key] = "on" if str(form.get(f.key, "")).strip().lower() in ("on", "1", "true", "yes") else ""
            else:
                posted[f.key] = form.get(f.key, "")
        dry = action == "preview"
        changes, errors = ps.parse_form(form)
        if errors:
            self._audit_action(req, "settings_set", "invalid input", dry_run=dry, fields=sorted(errors)[:60])
            return self._trade_page(req, posted=posted, errors=errors, status=400)
        data, err = self._call(req, "settings_set", changes=changes, dry_run=dry)
        changed = data.get("changed") if data and isinstance(data.get("changed"), list) else []
        keys = []
        for c in changed:
            if isinstance(c, dict):
                p = c.get("path")
                keys.append("%s:%s" % (c.get("file"), ".".join(str(x) for x in p) if isinstance(p, list) else p))
        self._audit_action(req, "settings_set", err, dry_run=dry, changed=keys[:100],
                           written=bool(data.get("written")) if data else False)
        if err is not None:
            return self._trade_page(req, posted=posted, result=self._helper_error(err))
        result = self._trade_result(req, data, changed, dry)
        if not dry and data.get("written"):
            self._flash(req, result)
            return self._redirect("/trade")
        return self._trade_page(req, posted=posted, result=result)

    def _trade_result(self, req, data, changed, dry):
        rows = self._changed_table(changed)
        head = te("Changes (preview; nothing saved yet)") if dry else te("Saved changes")
        out = [table([N_("Key"), N_("Old value"), N_("New value")], rows) if rows else
               '<p class="muted">%s</p>' % te("No value changes.")]
        confirm = self._apply_inline(req) if (not dry and data.get("written") and data.get("confirm_needed")) else None
        out.append(self._put_result(data, dry, confirm))
        return card(head, "".join(out), "eye" if dry else "save")

    def _apply_inline(self, req):
        """The notice after a saved change that needs a new live confirmation, with the apply form right there."""
        data, err = self._call(req, "confirm_show")
        text = self._helper_error(err) if err is not None else self._confirm_text(data)
        return self._confirm_notice() + card(te("Apply now"), text + self._apply_form(req), "check", cls="inset")

    # ------------------------------------------------------------------ apply (confirm-live)
    @staticmethod
    def _confirm_text(data):
        out = ""
        if data.get("ok") is False:
            out = box("warn", te("This cannot be confirmed right now; see the text below."))
        return out + pre(data.get("text", ""), "term")

    def _apply_form(self, req):
        inner = '%s%s<div class="actions">%s</div>' % (
            field(tr("To confirm, type exactly: %s") % code(APPLY_PHRASE),
                  '<input id="phrase" name="phrase" class="ltr" autocomplete="off" spellcheck="false" maxlength="64" '
                  'required>', for_="phrase"),
            field("", checkbox("start", tr("Start the bot after the confirmation"), True)),
            button(tr("Confirm and apply"), "danger", "check"))
        return self._form(req, "/apply", inner)

    def _apply_get(self, req, message="", status=200, check_html=""):
        data, err = self._call(req, "confirm_show")
        parts = [message]
        steps = ('<ol class="steps"><li><b>%s</b><span>%s</span></li><li><b>%s</b><span>%s</span></li>'
                 '<li><b>%s</b><span>%s</span></li></ol>') % (
            te("Save"), te("Change the settings in Trade settings, Models & keys or the JSON editor."),
            te("Check"), te("Optionally run the server check below."),
            te("Confirm"), te("Type the phrase: the bot stops, the settings are confirmed and the bot starts again."))
        parts.append(steps)
        if err is not None:
            parts.append(self._helper_error(err))
        else:
            parts.append(card(te("What you are confirming"), self._confirm_text(data), "list"))
        check_btn = self._form(req, "/apply/check", '<div class="actions">%s</div>' % button(
            tr("Check the server"), "secondary", "terminal"))
        parts.append(card(te("Server check"), help_p(te(
            "Runs bitpin-bot check (up to about 3 minutes) before you confirm; nothing changes.")) + check_html
            + check_btn, "terminal"))
        parts.append(card(te("Confirm and apply"), "<p>%s</p>%s" % (te(
            "The bot is stopped, the saved settings are confirmed (confirm-live) and the bot starts again."),
            self._apply_form(req)), "check"))
        return self._page(req, tr("Apply settings"), "".join(parts), status=status, active="/apply",
                          sub=tr("Confirm the saved settings for live trading"))

    def _apply_check(self, req):
        data, err = self._act(req, "check")
        if err is not None:
            return self._apply_get(req, check_html=self._helper_error(err))
        ok = data.get("ok") is True
        html_ = box("ok" if ok else "err", te("The server check passed.") if ok else te(
            "The server check found a problem.")) + pre(data.get("text", ""), "term")
        return self._apply_get(req, check_html=html_)

    def _apply_post(self, req):
        phrase = req.form.get("phrase", "").strip()
        start = req.form.get("start") == "1"
        if phrase != APPLY_PHRASE:
            self._audit_action(req, "apply_live", "phrase mismatch", start=start)
            return self._apply_get(req, box("err", tr("The confirmation phrase is not right. Type exactly %s.")
                                            % code(APPLY_PHRASE)), status=400)
        data, err = self._act(req, "apply_live", {"start": start}, phrase=phrase, start=start)
        if err is not None:
            self._flash(req, self._helper_error(err))
            return self._redirect("/apply")
        steps = [s for s in data.get("steps") or [] if isinstance(s, dict)] if isinstance(data.get("steps"), list) else []
        rows = [[bdi(s.get("step")), ok_html(s.get("ok") is True), pre(s.get("output")) if s.get("output") else ""]
                for s in steps]
        ok = data.get("ok") is True if "ok" in data else bool(steps) and all(s.get("ok") is True for s in steps)
        head = box("ok" if ok else "err", te("The settings are confirmed and applied.") if ok else te(
            "Applying the settings did not finish; see the steps below."))
        state = ("<p>%s</p>" % (tr("Bot state: %s") % state_html(data.get("bot_state")))) if data.get("bot_state") else ""
        health = data.get("health")
        self._flash(req, head + card(te("Steps"), "%s%s%s" % (
            table([N_("Step"), N_("Result"), N_("Output")], rows), state,
            ("<h3>%s</h3>%s" % (te("Health"), pre(health, "term"))) if health else ""), "list"))
        return self._redirect("/apply")

    # ------------------------------------------------------------------ VPN
    def _vpn_get(self, req):
        return self._vpn_page(req)

    def _vpn_summary(self, summ):
        if not isinstance(summ, dict) or not summ:
            return '<p class="muted">%s</p>' % te("No summary available.")
        out = []
        proxy = summ.get("proxy")
        if isinstance(proxy, dict):
            rows = [(str(k), ltr(jtext(v))) for k, v in proxy.items() if v not in ("", None)]
            out.append("<h3>%s</h3>%s" % (te("Server (the proxy outbound)"), facts(rows)))
        elif "proxy" in summ:
            out.append(box("warn", te("No proxy outbound was found in the configuration.")))
        inbounds = [i for i in summ.get("inbounds") if isinstance(i, dict)] if isinstance(summ.get("inbounds"), list) else []
        if inbounds:
            rows = [[code(i.get("tag")), ltr(i.get("protocol")), code(i.get("listen")), ltr(i.get("port")),
                     yes_no(i.get("local_only"))] for i in inbounds]
            out.append("<h3>%s</h3>%s" % (te("Inbounds"), table(
                ["tag", N_("Protocol"), "listen", N_("Port"), N_("Local only")], rows)))
            if any(i.get("local_only") is False and i.get("protocol") in ("http", "socks") for i in inbounds):
                out.append(box("warn", te("A proxy inbound listens on every interface and can be reached from outside "
                                          "the server.")))
        outbounds = [o for o in summ.get("outbounds") if isinstance(o, dict)] if isinstance(summ.get("outbounds"), list) else []
        if outbounds:
            out.append("<h3>%s</h3>%s" % (te("Outbounds"), table(
                ["tag", N_("Protocol")], [[code(o.get("tag")), ltr(o.get("protocol"))] for o in outbounds])))
        lp = summ.get("local_proxies")
        if isinstance(lp, list) and lp:
            out.append("<p>%s</p>" % (tr("Local proxies: %s") % " ".join(code(x) for x in lp)))
        known = ("proxy", "inbounds", "outbounds", "local_proxies", "proxy_index")
        rest = [(str(k), ltr(jtext(summ[k]))) for k in sorted(summ, key=str) if k not in known]
        if rest:
            out.append(kv_table(rest))
        return "".join(out)

    def _vpn_page(self, req, extra="", raw_text=None, status=200):
        data, err = self._call(req, "vpn_get", raw=False)
        parts = []
        btns = [self._svc_button(req, "xray-tunnel", "restart", N_("Restart the tunnel"), "danger", "/vpn", "refresh"),
                self._svc_button(req, "xray-tunnel", "start", N_("Start"), "secondary", "/vpn", "play"),
                self._svc_button(req, "xray-tunnel", "stop", N_("Stop"), "secondary", "/vpn", "stop"),
                self._form(req, "/vpn/test", button(tr("Test the connection through the proxy"), "secondary", "pulse"),
                           "inline"),
                self._form(req, "/vpn/raw", button(tr("Show the raw configuration"), "secondary", "braces"), "inline")]
        actions = '<div class="actions">%s</div>' % "".join(btns)
        if err is not None:
            parts.append(self._helper_error(err))
            parts.append(card(te("Actions"), actions, "zap"))
        else:
            info = kv_table([(N_("Tunnel state"), state_html(data.get("unit_state"))),
                             (N_("Configuration file"), code(data.get("config_path")) if data.get("config_path") else dash())])
            warn = box("warn", tr("The current configuration could not be read: %s") % bdi(data.get("error"))) if data.get("error") else ""
            parts.append(card(te("Current connection (secrets hidden)"), info + warn + self._vpn_summary(
                data.get("summary")) + actions, "globe"))
        parts.append(extra)
        inner = '%s<div class="actions">%s</div>' % (
            field(tr("Share link (%s)") % ltr(VPN_SCHEMES_TEXT), textarea("link", "", "code short", "vpn-link", 4),
                  for_="vpn-link"),
            button(tr("Preview"), "primary", "eye", "do", "check"))
        parts.append(card(te("New connection from a link"), help_p(te(
            "Preview first, then apply. If the test after applying fails, the previous configuration is restored "
            "automatically.")) + self._form(req, "/vpn/put", hidden("source", "link") + inner), "link"))
        if raw_text is not None:
            inner = hidden("source", "text") + textarea("text", raw_text, "code", "vpn-raw") + field(
                "", checkbox("keep_on_failure", tr("Keep it even if the test fails"))) + (
                '<div class="actions">%s%s</div>' % (button(tr("Check"), "secondary", "eye", "do", "check"),
                                                     button(tr("Save and apply"), "danger", "save", "do", "save")))
            parts.append(card(te("Raw xray configuration"), box("warn", tr(
                "<strong>Warning:</strong> this text contains the VPN connection's secrets (ids and passwords). Do "
                "not copy it anywhere or show it to anyone.")) + self._form(req, "/vpn/put", inner), "braces"))
        return self._page(req, tr("VPN"), "".join(parts), status=status, active="/vpn",
                          sub=tr("The xray tunnel the bot reaches Kimi and Telegram through"))

    def _proxy_table(self, results):
        rows = []
        for r in results if isinstance(results, list) else []:
            if isinstance(r, dict):
                rows.append([code(r.get("target")), ok_html(r.get("ok") is True), fmt_num(r.get("ms")),
                             bdi(r.get("error")) if r.get("error") else ""])
        return table([N_("Target"), N_("Result"), N_("Time (ms)"), N_("Error")], rows, num=(2,))

    def _vpn_test(self, req):
        data, err = self._act(req, "vpn_test")
        if err is not None:
            return self._vpn_page(req, self._helper_error(err))
        req_hosts = data.get("required") if isinstance(data.get("required"), list) else []
        extra = card(te("Connection test"), kv_table([
            (N_("Proxy"), code(data.get("proxy")) if data.get("proxy") else dash()),
            (N_("Tunnel state"), state_html(data.get("unit_state"))),
            (N_("Hosts the bot needs"), " ".join(code(h) for h in req_hosts) or dash())])
            + self._proxy_table(data.get("results")), "pulse")
        return self._vpn_page(req, extra)

    def _vpn_raw(self, req):
        data, err = self._act(req, "vpn_get", {"raw": True}, raw=True)
        if err is not None:
            return self._vpn_page(req, self._helper_error(err))
        raw = data.get("raw")
        if not isinstance(raw, str):
            return self._vpn_page(req, box("warn", tr("The raw text is not available: %s") % bdi(data.get("error") or "")))
        return self._vpn_page(req, raw_text=raw)

    def _vpn_result(self, res, dry):
        out = []
        xt = res.get("xray_test") if isinstance(res.get("xray_test"), dict) else None
        if xt is not None:
            ok = xt.get("ok") is True
            out.append(box("ok" if ok else "err", te("xray configuration test: passed") if ok else te(
                "xray configuration test: failed")) + (pre(xt.get("output")) if xt.get("output") else ""))
        if not dry:
            if res.get("written"):
                bk = res.get("backup")
                out.append(box("ok", te("Written.") + ((" " + tr("Backup: %s") % code(bk)) if bk else "")))
            else:
                out.append(box("err", te("Not written.")))
            if res.get("rolled_back"):
                out.append(box("err", "<strong>%s</strong>" % te(
                    "The test after applying failed and the previous configuration was restored.")))
            if res.get("tunnel_state") is not None:
                out.append("<p>%s</p>" % (tr("Tunnel state: %s") % state_html(res.get("tunnel_state"))))
            if res.get("proxy_test"):
                out.append("<h3>%s</h3>%s" % (te("Test through the proxy"), self._proxy_table(res.get("proxy_test"))))
            if res.get("proxy_test_after_rollback"):
                out.append("<h3>%s</h3>%s" % (te("Test after the restore"),
                                              self._proxy_table(res.get("proxy_test_after_rollback"))))
        if isinstance(res.get("summary"), dict):
            out.append("<h3>%s</h3>%s" % (te("Summary of the new configuration"), self._vpn_summary(res.get("summary"))))
        if res.get("diff"):
            out.append("<h3>%s</h3>%s" % (te("Differences (secrets hidden)"), diff_html(res.get("diff"))))
        return "".join(out)

    def _vpn_put(self, req):
        f = req.form
        source = f.get("source", "")
        dry = f.get("do", "check") != "save"
        keep = f.get("keep_on_failure") == "1"
        pending = req.sess.get("vpn_pending")
        if isinstance(pending, dict) and req.now - pending.get("at", 0) > VPN_PENDING_SECONDS:
            req.sess.pop("vpn_pending", None)
            pending = None
        if source == "link":
            link = f.get("link", "").strip()
            scheme = link.split("://", 1)[0].lower() if "://" in link else ""
            if not link or len(link) > MAX_VPN_LINK or scheme not in VPN_SCHEMES or any(c.isspace() for c in link):
                self._audit_action(req, "vpn_put", "invalid link", source="link", dry_run=True)
                return self._vpn_page(req, box("err", tr("The link is not valid: one link starting with %s, "
                                                         "without spaces.") % ltr(VPN_SCHEMES_TEXT)), status=400)
            data, err = self._act(req, "vpn_put", {"source": "link", "dry_run": True, "scheme": scheme}, hide=(link,),
                                  link=link, dry_run=True)
            if err is not None:
                return self._vpn_page(req, self._helper_error(err))
            req.sess["vpn_pending"] = {"link": link, "at": req.now}
            apply_btn = self._form(req, "/vpn/put", hidden("source", "pending") + hidden("do", "save") + field(
                "", checkbox("keep_on_failure", tr("Keep it even if the test fails")))
                + '<div class="actions">%s</div>' % button(tr("Apply this connection"), "danger", "check"))
            extra = card(te("Preview (not applied yet)"), self._vpn_result(data, True) + apply_btn, "eye")
            return self._vpn_page(req, extra)
        if source == "pending":
            if not isinstance(pending, dict) or not pending.get("link"):
                self._audit_action(req, "vpn_put", "no pending link", source="pending", dry_run=False)
                self._flash(req, box("err", te("No preview is waiting (or it expired). Enter the link again.")))
                return self._redirect("/vpn")
            link = pending["link"]
            req.sess.pop("vpn_pending", None)
            data, err = self._act(req, "vpn_put", {"source": "link", "dry_run": False, "keep_on_failure": keep},
                                  hide=(link,), link=link, dry_run=False, keep_on_failure=keep)
            self._flash(req, self._helper_error(err) if err is not None else
                        card(te("Result"), self._vpn_result(data, False), "globe"))
            return self._redirect("/vpn")
        if source == "text":
            text = f.get("text", "").replace("\r\n", "\n")
            if not text.strip() or len(text) > MAX_VPN_TEXT:
                self._audit_action(req, "vpn_put", "invalid text", source="text", dry_run=dry)
                return self._vpn_page(req, box("err", te("The configuration text is empty or too long.")),
                                      raw_text=text, status=400)
            args = {"text": text, "dry_run": dry}
            if not dry:
                args["keep_on_failure"] = keep
            data, err = self._act(req, "vpn_put", {"source": "text", "dry_run": dry, "keep_on_failure": keep},
                                  hide=(text,), **args)
            if err is not None:
                return self._vpn_page(req, self._helper_error(err), raw_text=text)
            if dry:
                return self._vpn_page(req, card(te("Check result"), self._vpn_result(data, True), "eye"),
                                      raw_text=text)
            self._flash(req, card(te("Result"), self._vpn_result(data, False), "globe"))
            return self._redirect("/vpn")
        self._audit_action(req, "vpn_put", "invalid source")
        return self._vpn_page(req, box("err", te("Invalid request.")), status=400)

    # ------------------------------------------------------------------ logs
    def _logs_get(self, req):
        unit = req.query.get("unit", "")
        try:
            lines = int(req.query.get("lines", "200").strip())
        except ValueError:
            lines = 200
        lines = max(1, min(500, lines))
        form = ('<form method="get" action="/logs" class="filters">%s%s<div class="actions">%s</div></form>') % (
            field(te("Service"), select("unit", [(u, u) for u in LOG_UNITS], unit or "bitpin-bot", "lg-unit", "ltr"),
                  for_="lg-unit"),
            field(te("Lines (up to 500)"), '<input id="lg-lines" name="lines" type="number" min="1" max="500" '
                                           'value="%d" class="ltr">' % lines, for_="lg-lines"),
            button(tr("Show the log"), "primary", "search"))
        parts = [card("", form)]
        status = 200
        if unit:
            if unit not in LOG_UNITS:
                parts.append(box("err", te("Unknown service.")))
                status = 400
            else:
                data, err = self._call(req, "logs", unit=unit, lines=lines)
                if err is not None:
                    parts.append(self._helper_error(err))
                else:
                    parts.append(card(code(unit), pre(data.get("text", ""), "term"), "terminal"))
        return self._page(req, tr("Logs"), "".join(parts), status=status, active="/logs",
                          sub=tr("The latest lines of the services' logs (journalctl)"))

    # ------------------------------------------------------------------ security
    def _security_get(self, req):
        return self._security_page(req)

    def _security_page(self, req, status=200):
        code_field = ""
        if self.totp_enabled:
            code_field = field(te("Current code from your authenticator app"),
                               '<input id="pw-code" name="code" class="ltr" inputmode="numeric" '
                               'autocomplete="one-time-code" maxlength="12" required>', for_="pw-code")
        pw_inner = ('%s%s%s%s%s<div class="actions">%s</div>') % (
            field(te("Current password"), '<input id="pw-cur" type="password" name="current" class="ltr" '
                                          'autocomplete="current-password" maxlength="1024" required>', for_="pw-cur"),
            field(te("New password"), '<input id="pw-new1" type="password" name="new1" class="ltr" '
                                      'autocomplete="new-password" maxlength="1024" required>', for_="pw-new1"),
            field(te("New password again"), '<input id="pw-new2" type="password" name="new2" class="ltr" '
                                            'autocomplete="new-password" maxlength="1024" required>', for_="pw-new2"),
            code_field,
            help_p(te("At least 12 characters and three of: lower case, upper case, digits, symbols; not the "
                      "username or a common password. Every other session is signed out after the change.")),
            button(tr("Change the password"), "primary", "lock"))
        pw_card = card(te("Change the password"), self._form(req, "/security/password", pw_inner), "lock")
        if self.totp_enabled:
            inner = '%s<div class="actions">%s</div>' % (
                field(te("Current code"), '<input id="t-off" name="code" class="ltr" inputmode="numeric" '
                                          'autocomplete="one-time-code" maxlength="12" required>', for_="t-off"),
                button(tr("Turn off two-step login"), "danger", "error"))
            totp_card = card(te("Two-step login (TOTP)"), box("ok", te("On.")) + self._form(
                req, "/security/totp/disable", inner), "shield")
        else:
            pending = req.sess.get("totp_pending")
            if pending:
                grouped = " ".join(pending[i:i + 4] for i in range(0, len(pending), 4))
                inner = '%s<div class="actions">%s</div>' % (
                    field(te("The code the app shows"), '<input id="t-on" name="code" class="ltr" inputmode="numeric" '
                                                        'autocomplete="one-time-code" maxlength="12" required>',
                          for_="t-on"),
                    button(tr("Confirm and turn on"), "primary", "check"))
                cancel = self._form(req, "/security/totp/cancel", button(tr("Cancel"), "secondary"), "inline")
                totp_card = card(te("Turn on two-step login"), (
                    '<p>%s</p><p><code dir="ltr" class="secret">%s</code></p><p>%s</p>%s%s%s%s') % (
                    te("Add this key to your authenticator app (Google Authenticator, Aegis, ...) as a time-based "
                       "key with 6 digits and 30 seconds:"), esc(grouped), te("or this address:"),
                    pre(otpauth_uri(pending, self.username)),
                    help_p(te("This key is shown only on this page, until you confirm it.")),
                    self._form(req, "/security/totp/enable", inner), cancel), "shield")
            else:
                totp_card = card(te("Two-step login (TOTP)"), box("warn", te(
                    "Off. For a panel reachable from the internet, turning it on is recommended.")) + self._form(
                    req, "/security/totp/new", button(tr("Create a new key"), "primary", "key")), "shield")
        parts = ['<div class="grid2">%s%s</div>' % (pw_card, totp_card),
                 card(te("Recent events (50)"), self._audit_table(), "list", cls="flush")]
        return self._page(req, tr("Security"), "".join(parts), status=status, active="/security",
                          sub=tr("Password, two-step login and the audit log"))

    def _audit_table(self):
        labels = {"login_ok": N_("Signed in"), "login_failed": N_("Failed sign-in"), "login_locked": N_("Sign-in locked"),
                  "logout": N_("Signed out"), "action": N_("Action"), "csrf_refused": N_("Request without a valid token")}
        rows = []
        for r in self.audit_tail(50):
            detail = " ".join("%s=%s" % (k, jtext(r[k])) for k in sorted(r) if k not in ("time", "event", "ip", "user"))
            ev = str(r.get("event", ""))
            bad = r.get("result") == "error" or ev in ("login_failed", "login_locked", "csrf_refused")
            kind = " err" if bad else (" ok" if ev == "login_ok" else "")
            rows.append([fmt_time(r.get("time", "")), '<span class="pill%s">%s</span>' % (
                kind, te(labels[ev]) if ev in labels else esc(ev)),
                ltr(r.get("ip", "")), ltr(r.get("user") or ""), '<span class="detail">%s</span>' % ltr(detail)])
        return table([N_("Time"), N_("Event"), "IP", N_("User"), N_("Details")], rows)

    def _password_post(self, req):
        f = req.form
        current_pw, new1, new2 = f.get("current", ""), f.get("new1", ""), f.get("new2", "")
        problems = []
        if new1 != new2:
            problems.append(tr("The new password and its repetition differ."))
        problems.extend(password_problems(new1, self.username, lang=current()))
        if new1 and new1 == current_pw:
            problems.append(tr("The new password must differ from the current one."))
        if problems:
            self._audit_action(req, "panel_password_set", "weak or mismatched new password")
            self._flash(req, box("err", "<strong>%s</strong>%s" % (te("The new password was not accepted"), ul(problems))))
            return self._redirect("/security")
        why = self._reauth(req, current_pw, f.get("code", ""))
        if why is not None:
            self._audit_action(req, "panel_password_set", "re-authentication failed")
            self._flash(req, box("err", esc(why)))
            return self._redirect("/security")
        new_hash = hash_password(new1, iterations=self.hash_iterations)
        data, err = self._act(req, "panel_password_set", None, hide=(new1, current_pw, new_hash), password_hash=new_hash)
        if err is not None:
            self._flash(req, self._helper_error(err))
            return self._redirect("/security")
        with self._auth_lock:
            self._password_hash = new_hash
        self._reload_auth()
        return self._new_session(req, tr("The password is changed. Every other session was signed out."))

    def _new_session(self, req, message):
        """Every session ends; the owner continues in a fresh one (a new session id)."""
        self.sessions.destroy_all()
        sid = self.sessions.create(self.username, req.ip, req.now)
        sess = self.sessions.get(sid, req.now)
        if sess is not None:
            sess["flash"] = [box("ok", esc(message))]
        return self._redirect("/security", [("Set-Cookie", _cookie(SESSION_COOKIE, sid))])

    def _totp_new(self, req):
        if self.totp_enabled:
            self._flash(req, box("warn", te("Two-step login is on already; turn it off first to make a new key.")))
            return self._redirect("/security")
        req.sess["totp_pending"] = new_totp_secret()
        self._audit_action(req, "totp_new")
        return self._redirect("/security")

    def _totp_cancel(self, req):
        req.sess.pop("totp_pending", None)
        self._audit_action(req, "totp_cancel")
        return self._redirect("/security")

    def _totp_enable(self, req):
        pending = req.sess.get("totp_pending")
        if self.totp_enabled or not pending:
            self._audit_action(req, "panel_totp_set", "no pending secret", enable=True)
            self._flash(req, box("err", te("No new key is waiting to be confirmed.")))
            return self._redirect("/security")
        allowed, _why = self.limiter.check(req.ip, req.now)
        ok, counter = verify_totp(pending, req.form.get("code", ""), req.now) if allowed else (False, None)
        if not ok:
            if allowed:
                started = self.limiter.failure(req.ip, req.now)
                if started:
                    self._audit("login_locked", req.ip, req.user, locks=started, during="totp_enable")
                    self._notify("login_locked", req.ip, req.user)
            self._audit_action(req, "panel_totp_set", "wrong code" if allowed else "locked", enable=True)
            self._flash(req, box("err", te("The code is not right (check the phone's clock), or signing in is "
                                           "locked for now.")))
            return self._redirect("/security")
        data, err = self._act(req, "panel_totp_set", {"enable": True}, hide=(pending,), totp_secret=pending)
        if err is not None:
            self._flash(req, self._helper_error(err))
            return self._redirect("/security")
        with self._auth_lock:
            self._totp_secret = pending
            self._totp_last = counter
        self._reload_auth()
        return self._new_session(req, tr("Two-step login is on: from now on every sign-in also asks for the app's "
                                         "code. Every other session was signed out."))

    def _totp_disable(self, req):
        if not self.totp_enabled:
            self._flash(req, box("warn", te("Two-step login is off.")))
            return self._redirect("/security")
        why = self._reauth(req, None, req.form.get("code", ""))
        if why is not None:
            self._audit_action(req, "panel_totp_set", "re-authentication failed", enable=False)
            self._flash(req, box("err", esc(why)))
            return self._redirect("/security")
        data, err = self._act(req, "panel_totp_set", {"enable": False}, totp_secret=None)
        if err is not None:
            self._flash(req, self._helper_error(err))
            return self._redirect("/security")
        with self._auth_lock:
            self._totp_secret = None
            self._totp_last = None
        self._reload_auth()
        self._flash(req, box("warn", te("Two-step login is turned off.")))
        return self._redirect("/security")


# ------------------------------------------------------------------------------------------------ small parsers

def _num_or(v, default):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        return default
    return v


def _num_text(v):
    if not _is_num(v):
        return ""
    return ("%.6f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _parse_prices(v):
    """({price_in_per_m, price_out_per_m, price_cached_in_per_m} or None, error text or None)."""
    keys = ("price_in_per_m", "price_out_per_m", "price_cached_in_per_m")
    texts = [v.get(k, "").strip().replace(",", ".") for k in keys]
    if not any(texts):
        return None, None
    if not texts[0] or not texts[1]:
        return None, tr("Both the input and the output price are needed (or leave every price empty).")
    out = {}
    for k, t in zip(keys, texts):
        if not t:
            continue
        try:
            x = float(t)
        except ValueError:
            return None, tr("The price %s is not a number.") % k
        if not math.isfinite(x) or x < 0 or x > 1000:
            return None, tr("The price %s must be between 0 and 1000 dollars per million tokens.") % k
        out[k] = x
    out.setdefault("price_cached_in_per_m", out["price_in_per_m"])
    return out, None


def config_problems(cfg):
    """Why the panel cannot start with this panel.json (English, for the service log); [] = fine."""
    if not isinstance(cfg, dict):
        return ["the panel config is not a JSON object"]
    out = []
    if not str(cfg.get("username") or "").strip():
        out.append("username is not set")
    if not is_password_hash(cfg.get("password_hash")):
        out.append("password_hash is not set (or not a pbkdf2_sha256$600000$<salt>$<hash> value)")
    ts = cfg.get("totp_secret")
    if ts is not None and not (isinstance(ts, str) and _totp_ok(ts)):
        out.append("totp_secret is not a base32 secret of 10+ bytes (null = 2FA off)")
    if not isinstance(cfg.get("allowed_hosts", []), list):
        out.append("allowed_hosts must be a list")
    # security review F2: behind a reverse proxy the panel must not be reachable directly, or any client could
    # fake its address with X-Forwarded-For and escape the per-IP login lock
    if cfg.get("trusted_proxy") and str(cfg.get("bind") or "0.0.0.0") not in ("127.0.0.1", "::1", "localhost"):
        out.append("trusted_proxy is on but bind is %s: bind 127.0.0.1 behind the proxy (a direct client could fake "
                   "X-Forwarded-For)" % (cfg.get("bind") or "0.0.0.0"))
    return out
