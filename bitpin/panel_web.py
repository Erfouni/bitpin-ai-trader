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

Performance (v3.6): /performance (profit and loss of a time range in toman and in USDT, per asset, SVG charts of
the portfolio and of every open position; the page reloads itself every minute through <meta http-equiv=refresh>
with auto=1, which does not keep the session alive) and /history (+ /history.csv), both from the helper's
`performance` command (bitpin/performance.py, run as the user bitpin).
"""
import base64
import binascii
import csv
import hashlib
import hmac
import html
import io
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
from .panel_cert import WARN_DAYS as CERT_WARN_DAYS
from .technical import MACD_FLAT, RSI_NEUTRAL, RSI_OVERBOUGHT, RSI_STRONG, RSI_WEAK

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
                        "panel_password_set", "panel_totp_set", "performance")
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
    (N_("Overview"), (("/", "dashboard", N_("Dashboard")), ("/performance", "trend", N_("Performance")),
                      ("/technical", "spark", N_("Technical analysis")), ("/history", "list", N_("Trade history")))),
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
# v3.6 performance and trade history: the time ranges (the default: since the account's first record), how long a
# report is reused (the history's pages and the CSV of one range come from one report), the live page's reload
PERF_RANGES = (("24h", N_("24 hours"), 86400), ("7d", N_("7 days"), 7 * 86400), ("30d", N_("30 days"), 30 * 86400),
               ("all", N_("Since the start"), None))
PERF_DEFAULT_RANGE = "all"
PERF_MAX_DAYS = 400                      # bitpin/performance.py MAX_RANGE_DAYS
PERF_CACHE_SECONDS = 45
PERF_CACHE_ENTRIES = 16
LIVE_REFRESH_SECONDS = 60
HISTORY_PAGE_ROWS = 50
SIDE_LABELS = {"buy": N_("Buy"), "sell": N_("Sell")}
# v3.6.1: the strategy and the analysis behind a position in words (bitpin/prompt_template.txt SETUPS and the base
# rates B1..B12 of docs/STRATEGY_KNOWLEDGE.md; the methods are read from the fields Kimi cites as evidence)
SETUP_LABELS = {
    "dip_in_uptrend": (N_("Dip in an uptrend"),
                       N_("above the 4h EMA50 and EMA200 after a 4-12% drop in 48 hours, or with RSI (4h) under 45")),
    "trend_continuation": (N_("Trend continuation"),
                           N_("above all three 4h EMAs, near the 30-day high, with a positive 7-day return")),
    "breakout": (N_("Breakout"), N_("at the 30-day high with at least 1.5 times the usual volume")),
    "crash_rebound": (N_("Rebound after a crash"), N_("a major coin 20% or more under its 48-hour high")),
    "relative_strength": (N_("Relative strength"),
                          N_("7- and 30-day returns above those of BTC, with a named and dated catalyst")),
    "mean_reversion": (N_("Mean reversion"), N_("RSI (4h) under 30 in a longer uptrend")),
    "macro_hedge": (N_("Macro hedge"), N_("gold or silver, or a stock / ETF / oil token judged by its underlying")),
    "other": (N_("Other"), N_("a case outside the setups above")),
}
ROW_TITLES = {"B1": N_("holding USDT, the benchmark"), "B2": N_("a top coin against USDT, without a condition"),
              "B3": N_("a rise of 15% or more within 48 hours"), "B4": N_("the majors fell 8% or more"),
              "B5": N_("the crash ladder: a buy 20% under the 48-hour high"),
              "B6": N_("a pump of 30% or more within 24 hours"), "B7": N_("mechanical strategies tested out of sample"),
              "B8": N_("short-term timing at a 1% round trip"), "B9": N_("volatility and small caps"),
              "B10": N_("the 84-day trend state of BTC"), "B11": N_("gold against USDT"),
              "B12": N_("tokenized US stocks, ETFs and oil")}
VERDICT_LABELS = {"open": N_("Open"), "add": N_("Add"), "hold": N_("Hold"), "trim": N_("Trim"), "cut": N_("Cut"),
                  "reject": N_("Reject")}
NO_POSITION = N_("No open position: the account holds no coin now (only USDT and toman).")

# v3.8: the technical reading (bitpin/technical.py): the fields, their values and the method in words
TA_FIELD_LABELS = (("trend", N_("Trend")), ("long", N_("Long trend (EMA200)")), ("momentum", N_("Momentum")),
                   ("rsi", "RSI"),
                   ("bands", N_("Bollinger")), ("channel", N_("Donchian")), ("volume", N_("Volume")))
TA_VALUE_LABELS = {
    "trend": {"up": N_("Up"), "down": N_("Down"), "mixed": N_("Mixed")},
    "long": {"above": N_("Above EMA200"), "below": N_("Below EMA200")},
    "momentum": {"rising": N_("Rising"), "falling": N_("Falling"), "flat": N_("Flat")},
    "rsi": {"overbought": N_("Overbought"), "strong": N_("Strong"), "neutral": N_("Neutral"), "weak": N_("Weak"),
            "oversold": N_("Oversold")},
    "bands": {"above_upper": N_("Above the upper band"), "upper": N_("Near the upper band"), "middle": N_("Middle"),
              "lower": N_("Near the lower band"), "below_lower": N_("Below the lower band")},
    "channel": {"breakout": N_("Breakout"), "upper_half": N_("Upper half"), "lower_half": N_("Lower half"),
                "breakdown": N_("Breakdown")},
    "volume": {"high": N_("High"), "normal": N_("Normal"), "low": N_("Low")},
    "read": {"bullish": N_("Bullish"), "bearish": N_("Bearish"), "neutral": N_("Neutral")},
}
TA_METHOD_LINES = (
    N_("Trend: the price above the 4-hour EMA20 and EMA50 is up, below both is down, otherwise mixed; EMA200 shows "
       "the longer trend."),
    N_("Momentum: the MACD histogram (12, 26, 9) above 0.02% of the price is rising, below -0.02% falling, otherwise "
       "flat."),
    N_("RSI (14, 4 hours): 70 or more overbought, 55 to 70 strong, 45 to 55 neutral, 30 to 45 weak, 30 or less "
       "oversold."),
    N_("Bollinger (20, 2): the place of the price in the bands; 0.8 or more is near the upper band, 0.2 or less near "
       "the lower one."),
    N_("Donchian (20 bars of 4 hours): at or above the highest close is a breakout, at or below the lowest a "
       "breakdown, otherwise the upper or lower half."),
    N_("Volume: the last 24 hours against the 30-day daily average; 1.5 times or more is high, 0.7 or less low."),
    N_("Support and resistance: the nearest levels from the 4-hour swing lows and highs of 30 days, with the number of "
       "swing points that hold them."),
)
ANALYSIS_METHODS = (           # (group, method, pattern over Kimi's evidence and bear texts, lower case)
    ("technical", N_("4h EMAs"), r"\bema"),
    ("technical", "RSI", r"\brsi"),
    ("technical", "MACD", r"\bmacd"),
    ("technical", N_("Bollinger bands"), r"\bbb4h|bollinger"),
    ("technical", N_("Donchian channel"), r"\bdon20|donchian"),
    ("technical", "ATR", r"\batr"),
    ("technical", N_("support and resistance"), r"\bsup\b|\bres\b|support|resistance"),
    ("technical", N_("place in the 30-day range"), r"\bpos30|\bd30h"),
    ("technical", N_("48-hour drawdown"), r"\bdd48"),
    ("technical", N_("returns in USDT"), r"\br(?:et)?[ _]?usdt"),
    ("technical", N_("trading volume"), r"\bvol[ _]?ratio|\bvolume"),
    ("macro", N_("the 84-day trend of BTC"), r"trend84"),
    ("macro", N_("beta to BTC"), r"\bbeta"),
    ("macro", N_("the US session and the underlying"), r"\bus[ _]?session|\bunderlying"),
    ("stats", N_("daily volatility"), r"\bsig[ _]?d\b|\bsigma"),
    ("news", N_("the news brief"), r"\bnews|\bbrief\b|\bevent|\bcatalyst"),
)
METHOD_GROUPS = (("technical", N_("Technical")), ("macro", N_("Macro")), ("stats", N_("Statistical")),
                 ("news", N_("News")))
_DIGITS = {ord(a): b for a, b in zip(u"۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")}

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
    """50.0 -> "50%", 2.68 -> "2.68%"; v3.10.1: zeros are cut only after a decimal point (nd=0 gave "5%" for 50)."""
    s = "%.*f" % (nd, v)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s + "%"


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


# ------------------------------------------------------------------------------------------------ charts (v3.6)
# A chart is an SVG drawing stretched to its box (preserveAspectRatio="none"; panel.css keeps the strokes' width)
# with its axis labels in HTML around it: sharp at any width, readable on a phone, no script.

def nice_ticks(lo, hi, n=5):
    """(bottom, top, ticks, step) of an axis that shows lo..hi in about n round steps."""
    if not (_is_num(lo) and _is_num(hi)):
        lo, hi = 0.0, 1.0
    if hi - lo <= max(abs(lo), abs(hi)) * 1e-9:
        pad = max(abs(lo), abs(hi)) * 0.01 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / float(n)
    mag = 10.0 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if raw <= m * mag * (1 + 1e-9))
    bottom, top = math.floor(lo / step + 1e-9) * step, math.ceil(hi / step - 1e-9) * step
    return bottom, top, [bottom + i * step for i in range(int(round((top - bottom) / step)) + 1)], step


def tick_text(v, step, pct=False):
    """An axis label: as many decimals as the step needs; pct: +1.5%."""
    d = 0
    while d < 10 and abs(step * 10 ** d - round(step * 10 ** d)) > 1e-6 * step * 10 ** d:
        d += 1
    if abs(v) < step * 1e-6:
        v = 0.0
    s = "{:,.{}f}".format(v, d)
    return ("+" if v > 0 else "") + s + "%" if pct else s


def time_ticks(t0, t1, n=5):
    """n Tehran times from t0 to t1, as short as the span allows."""
    span = max(0.0, t1 - t0)
    fmt = "%H:%M" if span <= 1.5 * 86400 else ("%m/%d" if span <= 200 * 86400 else "%Y/%m")
    return [datetime.fromtimestamp(t0 + span * i / float(n - 1), TEHRAN).strftime(fmt) for i in range(n)]


def line_chart(series, hlines=(), vlines=(), pct=False, label="", cls="", areas=(), shade=None, bars=(),
               y_range=None, x_range=None):
    """series: [(class, [(time, value)])] drawn in this order; hlines: [(class, value)] across the chart (entry,
    stop, orders ...); vlines: [(class, time)]; areas: [(class, [(time, low, high)])] bands under the lines (v3.6.1:
    the outlook's volatility range); shade: (class, from, to) a stretch of time behind everything (the future).
    v3.9: bars: [(class, [(time, value)])] columns from zero, each ending at its time (a 4h bar at its close);
    y_range: (low, high) a fixed value axis (RSI 0..100); x_range: (from, to) the time axis (the panes under a
    position's chart share its range; points outside it are left out).
    The chart's HTML, or a note when there is nothing to draw."""

    def ok_t(t):
        return _is_num(t) and 0 < t < 1e11

    xr = x_range if (x_range and ok_t(x_range[0]) and ok_t(x_range[1]) and x_range[1] > x_range[0]) else None

    def inside(t):
        return ok_t(t) and (xr is None or xr[0] <= t <= xr[1])

    series = [(c, [(t, v) for t, v in s if inside(t) and _is_num(v)]) for c, s in series]
    areas = [(c, [(t, lo, hi) for t, lo, hi in a if inside(t) and _is_num(lo) and _is_num(hi)]) for c, a in areas]
    bars = [(c, [(t, v) for t, v in b if inside(t) and _is_num(v)]) for c, b in bars]
    pts = [p for _c, s in series for p in s] + [p for _c, b in bars for p in b]
    if len(pts) < 2:
        return '<p class="muted empty">%s</p>' % te("Not enough data for a chart yet.")
    pts += [(t, v) for _c, a in areas for t, lo, hi in a for v in (lo, hi)]
    t0, t1 = xr if xr else (min(t for t, _v in pts), max(t for t, _v in pts))
    vals = [v for _t, v in pts] + [v for _c, v in hlines if _is_num(v)] + ([0.0] if (pct or bars) else [])
    if y_range and _is_num(y_range[0]) and _is_num(y_range[1]) and y_range[1] > y_range[0]:
        vals = [y_range[0], y_range[1]]
    bottom, top, ticks, step = nice_ticks(min(vals), max(vals))
    w, h = 1000, 300

    def x(t):
        return (t - t0) / (t1 - t0) * w if t1 > t0 else w / 2.0

    def y(v):
        return h - (v - bottom) / (top - bottom) * h

    g = []
    if shade and ok_t(shade[1]) and ok_t(shade[2]) and shade[2] > shade[1]:
        s0, s1 = max(t0, shade[1]), min(t1, shade[2])
        if s1 > s0:
            g.append('<rect class="%s" x="%.1f" y="0" width="%.1f" height="%d"/>' % (shade[0], x(s0), x(s1) - x(s0), h))
    g += ['<line class="g%s" x1="0" y1="%.1f" x2="%d" y2="%.1f"/>' % (" z" if pct and abs(v) < step * 1e-6 else "",
                                                                       y(v), w, y(v)) for v in ticks]
    for c, a in areas:
        if len(a) >= 2:
            g.append('<polygon class="a %s" points="%s"/>' % (c, " ".join(
                ["%.1f,%.1f" % (x(t), y(hi)) for t, _lo, hi in a] + ["%.1f,%.1f" % (x(t), y(lo)) for t, lo, _hi in
                                                                    reversed(a)])))
    if bars:
        times = sorted(set(t for _c, b in bars for t, _v in b))
        gap = min([b - a for a, b in zip(times, times[1:]) if b > a] or [(t1 - t0) / 60.0])
        bw = max(1.0, (x(t0 + gap) - x(t0)) * 0.72)
        y0 = y(min(max(0.0, bottom), top))
        for c, b in bars:
            for t, v in b:
                yv, left = y(v), x(t - gap / 2.0) - bw / 2.0
                left, right = max(0.0, left), min(float(w), left + bw)        # never past the plot's edges
                if right > left:
                    g.append('<rect class="b %s" x="%.1f" y="%.1f" width="%.1f" height="%.1f"/>' % (
                        c, left, min(yv, y0), right - left, max(abs(y0 - yv), 0.6)))
    for c, t in vlines:
        if _is_num(t) and t0 <= t <= t1:
            g.append('<line class="v %s" x1="%.1f" y1="0" x2="%.1f" y2="%d"/>' % (c, x(t), x(t), h))
    for c, v in hlines:
        if _is_num(v):
            g.append('<line class="h %s" x1="0" y1="%.1f" x2="%d" y2="%.1f"/>' % (c, y(v), w, y(v)))
    for c, s in series:
        p = " ".join("%.1f,%.1f" % (x(t), y(v)) for t, v in s)
        if p:
            g.append('<polyline class="l %s" points="%s"/>' % (c, p))
    return ('<figure class="chart%s" dir="ltr"><div class="yl" aria-hidden="true">%s</div><div class="plot"><svg '
            'viewBox="0 0 %d %d" preserveAspectRatio="none" role="img" aria-label="%s" focusable="false">%s</svg>'
            '</div><div class="xl" aria-hidden="true">%s</div></figure>') % (
        " " + cls if cls else "", "".join("<span>%s</span>" % esc(tick_text(v, step, pct)) for v in reversed(ticks)),
        w, h, esc(label), "".join(g), "".join("<span>%s</span>" % esc(s) for s in time_ticks(t0, t1)))


def legend(items):
    """items: [(class of the line, label HTML)]."""
    return '<ul class="legend">%s</ul>' % "".join('<li><i class="sw %s"></i><span>%s</span></li>' % (c, l)
                                                  for c, l in items)


def delta_html(v, nd=2):
    """A change in percent with its arrow, green up / red down."""
    if not _is_num(v):
        return dash()
    cls = "up" if v > 0 else ("down" if v < 0 else "")
    return '<span class="delta %s">%s%s</span>' % (cls, icon("down" if v < 0 else "up"), ltr("%+.*f%%" % (nd, v)))


def signed_num(v, nd=0, unit=""):
    """+1,234 in green, -1,234 in red, 0 plain (unit: e.g. "%" after the number)."""
    if not _is_num(v):
        return dash()
    if abs(v) < 0.5 * 10 ** -nd:
        return ltr("{:,.{}f}{}".format(0.0, nd, unit))
    return '<span class="%s">%s</span>' % ("gain" if v > 0 else "loss", ltr("{:+,.{}f}{}".format(v, nd, unit)))


def price_text(v):
    """A price with the decimals its size needs: 65,000.50 / 142.124 / 2.3457 / 0.00001234."""
    if not _is_num(v):
        return ""
    a = abs(v)
    nd = 2 if a >= 1000 else 3 if a >= 100 else 4 if a >= 1 else min(10, 3 - int(math.floor(math.log10(a)))) if a else 2
    return "{:,.{}f}".format(v, nd)


def qty_text(v, asset=""):
    """An amount of an asset: toman without decimals, coins without trailing zeros (0.00012345, not 1.2345e-04)."""
    if not _is_num(v):
        return ""
    if asset == "IRT":
        return "{:,.0f}".format(v)
    s = "{:,.8f}".format(v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _tehran_day(text):
    """Epoch seconds of 00:00 Tehran time of a YYYY-MM-DD date (Persian digits too), else None."""
    try:
        d = datetime.strptime(str(text or "").strip().translate(_DIGITS), "%Y-%m-%d")
    except ValueError:
        return None
    if not 2020 <= d.year <= 2100:
        return None
    return d.replace(tzinfo=TEHRAN).timestamp()


def csv_cell(v):
    """A text cell a spreadsheet will not run as a formula."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _ta_points(ta, key, idx=None):
    """[(time, value)] of one chart_data() series (idx: one value of a list point, e.g. bb[0])."""
    out = []
    for t, v in zip(ta.get("t") or [], ta.get(key) or []):
        if idx is not None:
            v = v[idx] if isinstance(v, list) and len(v) > idx else None
        if _is_num(t) and _is_num(v):
            out.append((t, v))
    return out


def ta_overlay(ta):
    """v3.9: the indicators on a position's price chart (bitpin/technical.py chart_data, the bot's own numbers):
    (series, areas, hlines, legend items) - EMA 20 / 50 / 200, the Bollinger band and its middle, the Donchian
    channel, the nearest support and resistance of the rules."""
    series = [("ta-don", _ta_points(ta, "don", 1)), ("ta-don", _ta_points(ta, "don", 0)),
              ("ta-bbm", _ta_points(ta, "bb", 1)), ("ta-e200", _ta_points(ta, "ema200")),
              ("ta-e50", _ta_points(ta, "ema50")), ("ta-e20", _ta_points(ta, "ema20"))]
    band = [(t, b[0], b[2]) for t, b in zip(ta.get("t") or [], ta.get("bb") or [])
            if isinstance(b, list) and len(b) > 2 and _is_num(b[0]) and _is_num(b[2])]
    now = ta.get("now") if isinstance(ta.get("now"), dict) else {}
    devs = now.get("ema_dev_pct") if isinstance(now.get("ema_dev_pct"), list) else []
    items = []
    for i, n in enumerate((20, 50, 200)):
        pts = _ta_points(ta, "ema%d" % n)
        if pts:
            text = tr("EMA %s: %s") % (n, ltr(price_text(pts[-1][1])))
            if len(devs) > i and _is_num(devs[i]):
                text += " " + tr("(the price %s from it)") % ltr("%+.1f%%" % devs[i])
            items.append(("ta-e%d" % n, text))
    bb = now.get("bb4h") if isinstance(now.get("bb4h"), list) else []
    if band:
        text = te("Bollinger 20, 2")
        if len(bb) > 1 and _is_num(bb[0]) and _is_num(bb[1]):
            text += " " + tr("(place %s, width %s)") % (ltr("%.2f" % bb[0]), ltr("%.1f%%" % bb[1]))
        items.append(("ta-bb", text))
    don = now.get("don20_4h") if isinstance(now.get("don20_4h"), list) else []
    if len(don) > 1 and _is_num(don[0]) and _is_num(don[1]):
        items.append(("ta-don", tr("Donchian 20: %s") % ltr("%s - %s" % (price_text(don[0]), price_text(don[1])))))
    rd = ta.get("reading") if isinstance(ta.get("reading"), dict) else {}
    hlines = []
    for key, cls, label in (("support", "ta-sup", N_("Support %s")), ("resistance", "ta-res", N_("Resistance %s"))):
        lv = rd.get(key)
        if not _is_num(lv):
            continue
        hlines.append((cls, lv))
        extra = []
        if _is_num(rd.get(key + "_dist_pct")):
            extra.append("%+.1f%%" % rd[key + "_dist_pct"])
        if _is_num(rd.get(key + "_n")):
            extra.append(u"\u00d7%d" % rd[key + "_n"])
        items.append((cls, tr(label) % ltr(price_text(lv)) + ((" " + ltr("(%s)" % ", ".join(extra))) if extra else "")))
    return series, [("ta-bb", band)], hlines, items


def ta_reading_html(ta):
    """v3.9: the rules' reading of the same numbers now (bitpin/technical.py reading), in one line."""
    rd = ta.get("reading") if isinstance(ta.get("reading"), dict) else {}
    shown = dict((k, rd[k]) for k, _label in TA_FIELD_LABELS if rd.get(k))
    if rd.get("tone"):
        shown["read"] = rd["tone"]
    if not shown:
        return ""
    return '<p class="ta-now"><b>%s</b> %s</p>' % (te("The rules read it now:"), ta_summary_html(shown))


def _pane(head, chart):
    return '<div class="ta-pane"><p class="ta-h">%s</p>%s</div>' % (head, chart)


def ta_panes(ta, x_range, shade):
    """v3.9: RSI, MACD and the traded value under a position's chart, on its time axis."""
    out = []
    rsi = _ta_points(ta, "rsi")
    if rsi:
        lines = [("ta-lim", RSI_OVERBOUGHT), ("ta-mid", RSI_STRONG), ("ta-mid", RSI_NEUTRAL), ("ta-lim", RSI_WEAK)]
        out.append(_pane(tr("RSI 14: %s") % ltr("%.1f" % rsi[-1][1]),
                         line_chart([("ta-rsi", rsi)], lines, label="RSI", cls="xs noxl", shade=shade,
                                    y_range=(0, 100), x_range=x_range)))
    hist = _ta_points(ta, "macd", 2)
    if hist:
        out.append(_pane(tr("MACD 12, 26, 9 in percent of the price: histogram %s") % ltr("%+.3f%%" % hist[-1][1]),
                         line_chart([("ta-sig", _ta_points(ta, "macd", 1)), ("ta-macd", _ta_points(ta, "macd", 0))],
                                    [("ta-mid", MACD_FLAT), ("ta-mid", -MACD_FLAT)], pct=True, label="MACD",
                                    cls="xs noxl", shade=shade, x_range=x_range,
                                    bars=[("ta-hp", [(t, v) for t, v in hist if v >= 0]),
                                          ("ta-hn", [(t, v) for t, v in hist if v < 0])])))
    vol = _ta_points(ta, "vol")
    if vol:
        now = ta.get("now") if isinstance(ta.get("now"), dict) else {}
        head = te("Traded value per 4 hours (thousand USDT)")
        if _is_num(now.get("vol_ratio")):
            head += " &middot; " + tr("the last 24 hours against the 30-day average: %s") % ltr(
                u"%.2f\u00d7" % now["vol_ratio"])
        avg = ta.get("vol_avg")
        out.append(_pane(head, line_chart([], [("ta-vavg", avg)] if _is_num(avg) else [], label=tr("Traded value"),
                                          cls="xs", shade=shade, x_range=x_range, bars=[("ta-vol", vol)])))
    return '<div class="ta-panes">%s</div>' % "".join(out) if out else ""


def analysis_html(p):
    """v3.6.1: the strategy and the analysis behind a position in words - Kimi's newest analysis of the coin
    (bitpin/performance.py read_analyses: setup, base-rate row, the methods its evidence cites, p against the
    driftless p0, reward / risk / cost, expected value, verdict, its own words), else the plan's setup."""
    a = p.get("analysis") if isinstance(p.get("analysis"), dict) else None
    setup = str((a or {}).get("setup") or p.get("setup") or "")
    name, desc = SETUP_LABELS.get(setup, (None, None))
    rows = [(N_("Strategy"), ('<b>%s</b> <span class="muted">%s</span>' % (te(name), te(desc))) if name
             else (ltr(setup.replace("_", " ")) if setup else dash()))]
    extra = ""
    if a:
        row = str(a.get("row") or "").upper()
        if row:
            rows.append((N_("Base rate"), ltr(row) + ((" &middot; " + te(ROW_TITLES[row])) if row in ROW_TITLES else "")))
        text = " ".join((str(a.get("evidence") or ""), str(a.get("bear") or ""))).lower()
        groups = {}
        for group, method, pattern in ANALYSIS_METHODS:
            if re.search(pattern, text) and method not in groups.get(group, []):
                groups.setdefault(group, []).append(method)
        stats = groups.setdefault("stats", [])
        if row:
            stats.insert(0, N_("the historical base rate"))
        if _is_num(a.get("p")):
            stats.append(N_("probability and expected value"))
        rows.append((N_("Analysis methods"), " &middot; ".join(
            "<b>%s:</b> %s" % (te(label), esc(tr(", ").join(tr(m) for m in groups[key])))
            for key, label in METHOD_GROUPS if groups.get(key))))
        pk, p0 = a.get("p"), a.get("p0")
        pk = pk if (_is_num(pk) and 0 <= pk <= 1) else None
        p0 = p0 if (_is_num(p0) and 0 <= p0 <= 1) else None
        if _is_num(pk):
            rows.append((N_("Kimi's probability"), tr("take profit before the invalidation: %s (without an edge: %s)")
                         % (ltr(pct_text(pk * 100.0, 1)), ltr(pct_text(p0 * 100.0, 1)) if _is_num(p0) else dash())))
        g, l, c = a.get("gain_pct"), a.get("loss_pct"), a.get("cost_pct")
        if _is_num(g) and _is_num(l):
            rows.append((N_("Reward and risk"), tr("%s to the target, %s to the invalidation, cost %s") % (
                signed_num(g, 2, "%"), signed_num(-l, 2, "%"), ltr(pct_text(c)) if _is_num(c) else dash())))
        if _is_num(a.get("ev_pct")):
            rows.append((N_("Expected value"), signed_num(a.get("ev_pct"), 2, "%")))
        v = a.get("verdict")
        if v:
            rows.append((N_("Kimi's verdict"), te(VERDICT_LABELS[v]) if v in VERDICT_LABELS else ltr(v)))
        rows.append((N_("Analysed"), fmt_time(a.get("time"))))
        if isinstance(a.get("ta"), dict) and a.get("ta"):              # v3.8: the technical reading
            rows.append((N_("Technical reading"), ta_summary_html(a.get("ta"), a.get("ta_check"))))
        if a.get("evidence") or a.get("bear"):
            extra = '<details class="more"><summary>%s</summary><p><b>%s</b> %s</p><p><b>%s</b> %s</p></details>' % (
                te("Kimi's own words"), te("Evidence:"), bdi(a.get("evidence") or ""), te("Against it:"),
                bdi(a.get("bear") or ""))
    else:
        extra = '<p class="muted small">%s</p>' % te(
            "Kimi has not re-tested this coin in its recent decisions: the chart shows the levels of its plan.")
    if p.get("note"):
        rows.append((N_("Kimi's note on the plan"), bdi(p.get("note"))))
    return '<div class="analysis"><h4>%s%s</h4><dl class="an">%s</dl>%s</div>' % (
        icon("spark"), te("Strategy and analysis"), "".join("<dt>%s</dt><dd>%s</dd>" % (te(k), v) for k, v in rows), extra)


def ta_label(field, value):
    """The translated label of a technical-reading value (the value itself when it is not one of the method's)."""
    lab = TA_VALUE_LABELS.get(field, {}).get(value)
    return te(lab) if lab else ltr(value)


def ta_summary_html(ta, checks=None):
    """v3.8: Kimi's technical reading in one line (its overall reading first), the fields the rules read
    differently marked."""
    bad = {x.get("field") for x in (checks or []) if isinstance(x, dict)}
    parts = []
    if ta.get("read") in TA_VALUE_LABELS["read"]:
        parts.append("<b>%s</b>" % ta_label("read", ta["read"]))
    for key, label in TA_FIELD_LABELS:
        if ta.get(key):
            parts.append('<span class="%s">%s: %s</span>' % ("ta-bad" if key in bad else "ta-ok", te(label),
                                                             ta_label(key, ta[key])))
    if bad:
        parts.append('<span class="ta-bad">%s</span>' % esc(tr("%s read differently from the rules") % len(bad)))
    return " &middot; ".join(parts) if parts else dash()


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
        self._perf_lock = threading.Lock()
        self._perf_cache = {}                      # (range, from, to) -> (time, report)
        self._get_routes = {
            "/": self._dashboard, "/models": self._models_get, "/settings": self._settings_get,
            "/trade": self._trade_get, "/apply": self._apply_get, "/vpn": self._vpn_get, "/logs": self._logs_get,
            "/health": self._health_get, "/security": self._security_get, "/performance": self._performance_get,
            "/history": self._history_get, "/history.csv": self._history_csv, "/technical": self._technical_get,
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
    def _doc(self, title, content, req=None, active=None, sub="", here="/", refresh=None):
        """A whole page: the sidebar layout for a logged-in request, else the centred card of the login page.
        refresh: the address the page reloads itself from every LIVE_REFRESH_SECONDS (v3.6, no script)."""
        lang = current()
        head = ('<!doctype html>\n<html lang="%s" dir="%s"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<meta name="robots" content="noindex, nofollow"><meta name="color-scheme" content="light dark">%s'
                '<title>%s | %s</title><link rel="icon" href="/static/icon.svg" type="image/svg+xml">'
                '<link rel="stylesheet" href="/static/panel.css?v=%s"></head>') % (
            lang, "rtl" if is_rtl(lang) else "ltr",
            ('<meta http-equiv="refresh" content="%d; url=%s">' % (LIVE_REFRESH_SECONDS, esc(refresh))) if refresh else "",
            esc(title), te(APP_NAME), esc(self.css_version))
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
            q = sorted((k, v) for k, v in (req.query or {}).items() if k and k != "auto")
            return req.path + ("?" + urlencode(q) if q else "")
        return active or "/"

    def _page(self, req, title, body, status=200, headers=None, active=None, sub="", refresh=None):
        flashes = ""
        if req.sess is not None:
            fl = req.sess.pop("flash", None)
            if fl:
                flashes = "".join(fl)
        return self._resp(status, self._doc(title, flashes + body, req, active, sub, self._here(req, active), refresh),
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
        # a live page reloading itself (auto=1, v3.6) does not keep the session alive: the idle expiry still ends
        # a session nobody uses
        auto = method == "GET" and q.get("auto") == "1"
        sess = self.sessions.get(sid, req.now, touch=not auto) if sid else None
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
        parts.append(self._news_card(data.get("news")))
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
            cards.append(kpi("trend", tr("Profit / loss"), delta_html(change), "",
                             tr("Started with %s IRT") % fmt_num(start, 0)
                             + ' &middot; <a href="/performance">%s</a>' % te("Details")))
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
        out.append('<p class="small"><a href="/technical">%s</a></p>' % te("Technical analysis of this decision"))
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
        if not rows:                                    # v3.10: no table of nothing
            return card(te("Positions"), '<p class="muted empty pad">%s</p>' % te(NO_POSITION), "layers", cls="flush")
        return card(te("Positions"), table([N_("Market"), N_("Kind"), N_("Amount"), N_("Entry (USDT)"),
                                            N_("Stop (USDT)"), N_("Target (USDT)"), N_("Hold until")], rows,
                                           num=(2, 3, 4, 5)), "layers", cls="flush")

    def _news_card(self, nv):
        """v3.7: the last news brief - how and when it was made, its items with their site, time and a link to the
        article - the feeds of the last attempt and the last error (scripts/panel_helper.py news_view)."""
        title = te("News brief")
        if not isinstance(nv, dict) or not nv:
            return card(title, '<p class="muted empty">%s</p>' % te("No news brief yet."), "news")
        out, meta = [], []
        if nv.get("fetched_at"):
            meta.append(tr("Made %s") % fmt_time(nv.get("fetched_at")))
        if nv.get("mode") == "feeds":
            meta.append(te("From the trusted sources' feeds"))
        elif nv.get("mode"):
            meta.append(te("The model's web search"))
        if nv.get("model"):
            meta.append(code(nv.get("model")))
        if meta:
            out.append('<p class="muted small">%s</p>' % " &middot; ".join(meta))
        err_at, made = nv.get("last_error_at"), nv.get("fetched_at")
        if nv.get("last_error") and _is_num(err_at) and (not _is_num(made) or err_at >= made):
            out.append(box("warn", tr("The last news attempt failed (%s): %s") % (fmt_time(err_at),
                                                                                  bdi(nv.get("last_error")))))
        if nv.get("summary"):
            out.append('<p class="news-sum" dir="ltr" lang="en">%s</p>' % esc(nv.get("summary")))
        items = [it for it in (nv.get("items") or []) if isinstance(it, dict) and it.get("headline")]
        if items:
            lis = []
            for it in items:
                head = esc(it.get("headline"))
                link = it.get("link")
                if isinstance(link, str) and re.match(r"^https?://[^\s\"'<>]+$", link):
                    head = '<a href="%s" target="_blank" rel="noopener noreferrer nofollow">%s</a>' % (esc(link), head)
                when = fmt_time(it.get("time")) if it.get("time") else ""
                src = " &middot; ".join(x for x in (esc(it.get("site") or ""), when) if x)
                why = ('<p>%s</p>' % esc(it.get("why"))) if it.get("why") else ""
                lis.append('<li><b>%s</b> <span class="muted small">%s</span>%s</li>' % (head, src, why))
            out.append('<ol class="news" dir="ltr" lang="en">%s</ol>' % "".join(lis))
        elif made:
            out.append('<p class="muted">%s</p>' % te("The brief has no news items."))
        fs = nv.get("feeds") if isinstance(nv.get("feeds"), dict) else {}
        if fs.get("tried"):
            line = tr("Feeds: %s of %s read, %s headlines") % (fmt_num(fs.get("ok")), fmt_num(fs.get("tried")),
                                                                fmt_num(fs.get("headlines")))
            if nv.get("feeds_at"):
                line += " &middot; " + fmt_time(nv.get("feeds_at"))
            failed = [f for f in (fs.get("failed") or []) if isinstance(f, dict)]
            if failed:
                rows = "".join('<li>%s %s</li>' % (code(f.get("site")), esc(f.get("error"))) for f in failed)
                out.append('<details class="feeds"><summary>%s &middot; %s</summary><ul class="feeds-failed" dir="ltr">'
                           '%s</ul></details>' % (line, tr("%s could not be read") % fmt_num(fs.get("failed_count")
                                                                                            or len(failed)), rows))
            else:
                out.append('<p class="muted small">%s</p>' % line)
        return card(title, "".join(out), "news")

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
            if k in ("longtext", "symbols", "domains", "urls"):
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
            elif k in ("domains", "urls"):                # v3.5: the trusted news sites; v3.7: extra feeds
                ctl = ('<textarea id="%s" name="%s" class="ltr short" dir="ltr" rows="%d" spellcheck="false" '
                       'autocomplete="off">\n%s</textarea>') % (fid, esc(f.key), 8 if k == "domains" else 4,
                                                                 esc(value))
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

    # ------------------------------------------------------------------ performance and trade history (v3.6)
    def _perf_range(self, req):
        """(range, t_from, t_to, from text, to text, error) of the query: a preset of PERF_RANGES (default: since
        the start) or range=custom with the days from / to (YYYY-MM-DD, Tehran time; an empty to = until now)."""
        q = req.query
        key = q.get("range", "")
        if key == "custom":
            f_text = q.get("from", "").strip().translate(_DIGITS)[:10]
            t_text = q.get("to", "").strip().translate(_DIGITS)[:10]
            t0, t1 = _tehran_day(f_text), _tehran_day(t_text) if t_text else None
            if t0 is None or (t_text and t1 is None):
                return key, None, None, f_text, t_text, tr("Pick the first day of the range (and its last day, or "
                                                           "leave that empty for: until now).")
            end = min(req.now, t1 + 86400) if t1 is not None else req.now
            if end <= t0:
                return key, None, None, f_text, t_text, tr("The range must start before it ends, and before now.")
            if end - t0 > PERF_MAX_DAYS * 86400:
                return key, None, None, f_text, t_text, tr("A range may be at most %s days.") % PERF_MAX_DAYS
            return key, int(t0), int(end), f_text, t_text, None
        spans = dict((k, s) for k, _l, s in PERF_RANGES)
        if key not in spans:
            key = PERF_DEFAULT_RANGE
        return key, int(req.now - (spans[key] or PERF_MAX_DAYS * 86400)), int(req.now), "", "", None

    def _perf_data(self, req, key, f_text, t_text, t_from, t_to):
        """The report of a range (bitpin/performance.py, run by the helper as the user bitpin), reused for
        PERF_CACHE_SECONDS: (data, None) or (None, error text)."""
        ck = (key, f_text, t_text)
        with self._perf_lock:
            hit = self._perf_cache.get(ck)
            if hit is not None and 0 <= req.now - hit[0] < PERF_CACHE_SECONDS:
                return hit[1], None
        data, err = self._call(req, "performance", **{"from": t_from, "to": t_to})
        if err is None:
            with self._perf_lock:
                if len(self._perf_cache) >= PERF_CACHE_ENTRIES:
                    self._perf_cache.clear()
                self._perf_cache[ck] = (req.now, data)
        return data, err

    @staticmethod
    def _range_query(key, f_text, t_text):
        return [("range", key)] + ([("from", f_text), ("to", t_text)] if key == "custom" else [])

    def _range_bar(self, path, key, f_text, t_text, keep=()):
        """The time range picker: the presets and a form for a range of days (Tehran dates)."""
        keep = list(keep)
        links = "".join('<a href="%s"%s>%s</a>' % (
            esc(path + "?" + urlencode([("range", k)] + keep)), ' aria-current="true"' if k == key else "", te(label))
            for k, label, _s in PERF_RANGES)
        form = ('<form method="get" action="%s" class="range-f">%s%s<label for="rf-from">%s</label><input id="rf-from" '
                'type="date" name="from" value="%s" class="ltr" required><label for="rf-to">%s</label><input id="rf-to" '
                'type="date" name="to" value="%s" class="ltr">%s</form>') % (
            esc(path), hidden("range", "custom"), "".join(hidden(k, v) for k, v in keep), te("From"), esc(f_text),
            te("To"), esc(t_text), button(tr("Show"), "secondary sm", "search"))
        return '<div class="rangebar"><nav class="seg" aria-label="%s">%s%s</nav>%s</div>' % (
            te("Time range"), icon("clock"), links, form)

    def _performance_get(self, req):
        title, sub = tr("Performance"), tr("Profit and loss in toman and in USDT, per asset, with live charts")
        key, t_from, t_to, f_text, t_text, error = self._perf_range(req)
        live_on = req.query.get("live", "") != "0"
        show_ta = req.query.get("ta", "") != "0"                 # v3.9: the indicators on the position charts
        ta_q = [] if show_ta else [("ta", "0")]
        parts = [self._range_bar("/performance", key, f_text, t_text, ([] if live_on else [("live", "0")]) + ta_q)]
        if error:
            parts.append(box("err", esc(error)))
            return self._page(req, title, "".join(parts), status=400, active="/performance", sub=sub)
        rq = self._range_query(key, f_text, t_text) + ta_q
        live = t_to >= req.now - 3600                       # the range ends now
        refresh = "/performance?" + urlencode(rq + [("auto", "1")]) if (live and live_on) else None
        data, err = self._perf_data(req, key, f_text, t_text, t_from, t_to)
        if err is not None:
            parts.append(self._helper_error(err))
            return self._page(req, title, "".join(parts), active="/performance", sub=sub, refresh=refresh)
        info = [tr("From %s to %s") % (fmt_time(data.get("from")), fmt_time(data.get("to")))]
        tools = [link_button("/history?" + urlencode(rq), tr("Trade history"), "ghost sm", "list")]
        if live:
            if live_on:
                info.insert(0, '<span class="pill ok">%s</span> %s' % (te("Live"), te("The page reloads itself every "
                                                                                        "minute.")))
            tools.insert(0, link_button("/performance?" + urlencode(rq + ([("live", "0")] if live_on else [])),
                                        tr("Stop live updates") if live_on else tr("Start live updates"), "ghost sm",
                                        "stop" if live_on else "play"))
        parts.append('<div class="livebar"><p>%s</p><div class="actions">%s</div></div>' % (
            " &middot; ".join(info), "".join(tools)))
        warns = [w for w in data.get("warnings") or [] if isinstance(w, str)]
        if warns:
            parts.append(box("warn", "<strong>%s</strong>%s" % (te("Some data is missing or does not add up:"),
                                                                ul(warns))))
        parts.append(self._perf_kpis(data))
        parts.append(card(te("Portfolio value"), self._equity_chart(data), "trend"))
        parts.append(card(te("Profit and loss per asset"), self._assets_table(data), "coins"))
        if data.get("live"):
            base = self._range_query(key, f_text, t_text) + ([] if live_on else [("live", "0")])
            toggle = link_button("/performance?" + urlencode(base + ([("ta", "0")] if show_ta else [])),
                                 tr("Hide the indicators") if show_ta else tr("Show the indicators"), "ghost sm", "spark")
            parts.append(self._positions_section(data, show_ta, toggle))
        else:
            parts.append(box("info", te("Open positions and their charts are shown for a range that ends now.")))
        return self._page(req, title, "".join(parts), active="/performance", sub=sub, refresh=refresh)

    # ---------------------------------------------------------------------------------- v3.8: the technical page
    def _technical_get(self, req):
        title = tr("Technical analysis")
        sub = tr("The exact indicators the bot gave Kimi at its last decision, Kimi's reading by the fixed method and "
                 "the code's check")
        data, err = self._call(req, "technical")
        if err is not None:
            return self._page(req, title, self._helper_error(err), status=502, active="/technical", sub=sub)
        if not data.get("time"):
            return self._page(req, title, box("info", te("No decision with a market context is recorded yet.")),
                              active="/technical", sub=sub)
        info = [tr("Decision of %s") % fmt_time(data.get("time"))]
        if data.get("mode"):
            info.append(ltr(data.get("mode")))
        if data.get("model"):
            info.append(code(data.get("model")))
        parts = ['<p class="muted">%s</p>' % " &middot; ".join(info)]
        held = [str(s) for s in data.get("held") or [] if isinstance(s, str)]
        parts.append('<p>%s</p>' % (tr("Coins in the account at this decision: %s") % " ".join(code(s) for s in held)
                                    if held else te("At this decision the account held no coin: only USDT and toman.")))
        us = data.get("usdt") if isinstance(data.get("usdt"), dict) else None
        if us and _is_num(us.get("px")):
            rets = [x for x in us.get("ret_irt") or [] if _is_num(x)]
            text = tr("The toman against USDT: 1 USDT = %s toman") % fmt_num(us["px"], 0)
            if len(rets) == 3:
                text += " &middot; " + tr("24 h / 7 d / 30 d: %s") % ltr(" / ".join("%+.1f%%" % x for x in rets))
            if _is_num(data.get("usdt_weight")):
                text += " &middot; " + tr("USDT is %s of the account") % ltr(pct_text(data["usdt_weight"] * 100.0, 0))
            parts.append('<p class="muted small">%s. %s</p>' % (text, te(
                "USDT gets no technical reading: it is the toman's price, not a coin's chart. Every reading on this "
                "page is in USDT.")))
        parts.append(self._ta_reading_card(data))
        parts.append(self._ta_coins_card(data))
        parts.append('<details class="card fold"><summary>%s<span>%s</span></summary><div class="card-b"><ul class='
                     '"ta-method">%s</ul><p class="muted small">%s</p></div></details>' % (
                         icon("spark"), te("How the reading works"),
                         "".join("<li>%s</li>" % te(x) for x in TA_METHOD_LINES),
                         te("Kimi reads the same numbers by the same rules in every decision; the code checks each "
                            "field. The reading describes the chart: it is not a buy or sell signal of its own.")))
        return self._page(req, title, "".join(parts), active="/technical", sub=sub)

    def _ta_reading_card(self, data):
        cands = [c for c in (data.get("candidates") or []) if isinstance(c, dict)]
        head = [N_("Coin"), N_("Kimi's verdict"), N_("Overall reading"), N_("Check")] + [
            lab for _k, lab in TA_FIELD_LABELS] + [N_("Support"), N_("Resistance")]
        rows = []
        for c in cands:
            ta = c.get("ta") if isinstance(c.get("ta"), dict) else {}
            codev = c.get("ta_code") if isinstance(c.get("ta_code"), dict) else {}
            checks = {x.get("field"): x for x in (c.get("ta_check") or []) if isinstance(x, dict)}
            v = c.get("verdict")
            row = [code(c.get("symbol")), te(VERDICT_LABELS[v]) if v in VERDICT_LABELS else (ltr(v) if v else dash()),
                   ("<b>%s</b>" % ta_label("read", ta["read"])) if ta.get("read") else dash()]
            if not ta:
                row.append('<span class="muted">%s</span>' % te("No reading"))
            elif checks:
                row.append('<span class="badge warn">%s</span>' % esc(tr("%s differ") % len(checks)))
            else:
                row.append('<span class="badge ok">%s</span>' % te("Matches the rules"))
            for key, _lab in TA_FIELD_LABELS + (("support", ""), ("resistance", "")):
                mine = ta.get(key)
                if mine is None:
                    row.append(dash())
                    continue
                shown = price_text(mine) if key in ("support", "resistance") else ta_label(key, mine)
                if key in checks:
                    rule = codev.get(key)
                    rule = price_text(rule) if key in ("support", "resistance") else ta_label(key, rule)
                    row.append('<span class="ta-bad">%s</span><small class="rule">%s</small>' % (
                        shown, tr("rules: %s") % rule))
                else:
                    row.append('<span class="ta-ok">%s</span>' % shown)
            rows.append(row)
        if not rows:
            body = '<p class="muted empty">%s</p>' % te("Kimi analysed no coin in this decision (it changed nothing "
                                                       "and no plan was due).")
        else:
            body = table(head, rows, cls="ta-table wrap")
            if not any(isinstance(c.get("ta"), dict) and c.get("ta") for c in cands):
                body += '<p class="muted small">%s</p>' % te("This decision is older than the technical reading "
                                                            "(version 3.8): only the code's reading is shown below.")
        return card(te("Kimi's technical reading"), body, "spark", cls="flush")

    def _ta_coins_card(self, data):
        head = [N_("Coin"), N_("Price (USDT)"), N_("Trend (EMA 20 / 50 / 200)"), N_("RSI 4h / 1d"), "MACD",
                N_("Bollinger"), N_("Donchian 20 (4h)"), N_("30-day range"), N_("Volume"), N_("Support"),
                N_("Resistance"), "ATR 4h", N_("7 d / 30 d")]
        rows = []
        for c in data.get("coins") or []:
            if not isinstance(c, dict):
                continue
            v = c.get("values") if isinstance(c.get("values"), dict) else {}
            r = c.get("reading") if isinstance(c.get("reading"), dict) else {}
            sym = str(c.get("symbol") or "")
            usdt = sym.upper().startswith("USDT_")
            px = v.get("px") if usdt else v.get("px_usdt")
            tags = ""
            if c.get("held"):
                tags += ' <span class="badge accent">%s</span>' % te("Held")
            if c.get("candidate"):
                tags += ' <span class="badge">%s</span>' % te("In Kimi's analysis")

            def lab(key):
                return ('<small class="lab">%s</small>' % ta_label(key, r[key])) if r.get(key) else ""

            def nums(key, nd=1, unit=""):
                xs = v.get(key)
                if not isinstance(xs, list):
                    return dash()
                return ltr(" / ".join(("%+.*f%s" % (nd, x, unit)) if unit else ("%.*f" % (nd, x)) for x in xs))

            def plain(x, nd):
                return ("%.*f" % (nd, x)) if _is_num(x) else "-"

            dev = nums("ema_dev_pct", 1, "%")
            rsi = ltr("%s / %s" % (plain(v.get("rsi4h"), 1), plain(v.get("rsi1d"), 1)))
            macd = v.get("macd4h_pct")
            macd_html = ltr("%+.2f%%" % macd[2]) if isinstance(macd, list) and len(macd) > 2 else dash()
            bb = v.get("bb4h")
            bb_html = ltr(u"%.2f · %.1f%%" % (bb[0], bb[1])) if isinstance(bb, list) and len(bb) > 1 else dash()
            don = v.get("don20_4h")
            don_html = (ltr("%s - %s" % (price_text(don[0]), price_text(don[1])))
                        if isinstance(don, list) and len(don) > 1 else dash())

            def level(key):
                lv = r.get(key)
                if not _is_num(lv):
                    return dash()
                extra = []
                if _is_num(r.get(key + "_dist_pct")):
                    extra.append("%+.1f%%" % r[key + "_dist_pct"])
                if _is_num(r.get(key + "_n")):
                    extra.append(u"×%d" % r[key + "_n"])
                return ltr(price_text(lv)) + (('<small class="lab">%s</small>' % ltr(" ".join(extra))) if extra else "")

            ret = v.get("ret_irt" if usdt else "ret_usdt")
            ret_html = (ltr("%+.1f%% / %+.1f%%" % (ret[1], ret[2])) if isinstance(ret, list) and len(ret) > 2
                        else dash())
            rows.append([code(sym) + tags, ltr(price_text(px)) if _is_num(px) else dash(), dev + lab("trend")
                         + lab("long"), rsi + lab("rsi"), macd_html + lab("momentum"), bb_html + lab("bands"),
                         don_html + lab("channel"), ltr(plain(v.get("pos30"), 2)) if _is_num(v.get("pos30")) else dash(),
                         (ltr(u"%.2f×" % v["vol_ratio"]) if _is_num(v.get("vol_ratio")) else dash()) + lab("volume"),
                         level("support"), level("resistance"),
                         ltr(pct_text(v.get("atr4h_pct"))) if _is_num(v.get("atr4h_pct")) else dash(), ret_html])
        note = '<p class="muted small pad">%s</p>' % te(
            "Prices and levels in USDT, exactly as the bot gave them to Kimi; the small labels are the code's reading "
            "by the fixed rules.")
        return card(te("Indicators of every coin"), note + table(head, rows, num=(1, 7, 11), cls="ta-table"), "layers",
                    cls="flush")

    def _perf_kpis(self, data):
        t = data.get("totals") if isinstance(data.get("totals"), dict) else {}
        subs = []
        if _is_num(t.get("value_to_usdt")):
            subs.append(tr("about %s USDT") % fmt_num(t.get("value_to_usdt"), 2))
        if _is_num(t.get("value_now_irt")):
            subs.append(tr("now about %s") % fmt_num(t.get("value_now_irt"), 0))
        cards = [kpi("wallet", tr("Account value"), fmt_num(t.get("value_to_irt"), 0),
                     tr("IRT") if _is_num(t.get("value_to_irt")) else "", " &middot; ".join(subs)),
                 kpi("trend", tr("Profit / loss in toman"), delta_html(t.get("pnl_irt_pct")), "",
                     (tr("%s IRT") % signed_num(t.get("pnl_irt"))) if _is_num(t.get("pnl_irt")) else ""),
                 kpi("coins", tr("Profit / loss in USDT"), delta_html(t.get("pnl_usdt_pct")), "",
                     (tr("without the rial's fall: %s USDT") % signed_num(t.get("pnl_usdt"), 2))
                     if _is_num(t.get("pnl_usdt")) else ""),
                 kpi("chart", tr("Holding USDT instead"), delta_html(t.get("rial_fall_pct")), "",
                     (tr("the bot against it: %s percentage points") % signed_num(t.get("vs_usdt_points"), 2))
                     if _is_num(t.get("vs_usdt_points")) else ""),
                 kpi("orders", tr("Trades"), fmt_num(t.get("trades")), "",
                     tr("%s buys &middot; %s sells &middot; fees %s IRT") % (
                         fmt_num(t.get("buys")), fmt_num(t.get("sells")), fmt_num(t.get("fees_irt"), 0)))]
        return '<div class="kpis">%s</div>' % "".join(cards)

    @staticmethod
    def _equity_chart(data):
        pts = [p for p in data.get("equity") or [] if isinstance(p, list) and len(p) >= 4 and _is_num(p[0])
               and _is_num(p[1]) and p[1] > 0]
        if not pts:
            return '<p class="muted empty">%s</p>' % te("Not enough data for a chart yet.")
        b_irt = pts[0][1]
        b_usdt = pts[0][2] if _is_num(pts[0][2]) and pts[0][2] > 0 else None

        def pct(v, base):
            return (v / base - 1.0) * 100.0 if (_is_num(v) and base) else None

        chart = line_chart([("s3", [(p[0], pct(p[3], b_irt)) for p in pts]),
                            ("s2", [(p[0], pct(p[2], b_usdt)) for p in pts]),
                            ("s1", [(p[0], pct(p[1], b_irt)) for p in pts])],
                           pct=True, label=tr("Change of the account value in percent"))
        out = chart + legend([("s1", te("In toman")), ("s2", te("In USDT (without the rial's fall)")),
                              ("s3", te("Holding USDT instead"))])
        t = data.get("totals") if isinstance(data.get("totals"), dict) else {}
        if _is_num(t.get("value_now_irt")):
            out += help_p(te("The last point is an estimate for this minute: the last recorded value moved by the "
                             "latest prices. The numbers above are the values the bot recorded."))
        return out

    @staticmethod
    def _assets_table(data):
        rows, sums = [], [0.0, 0.0, 0.0, 0.0]
        for a in data.get("assets") or []:
            if not isinstance(a, dict):
                continue
            name = str(a.get("asset") or "")
            for i, k in enumerate(("value_to_irt", "value_to_usdt", "pnl_irt", "pnl_usdt")):
                if _is_num(a.get(k)):
                    sums[i] += a.get(k)
            rows.append([te("Toman (cash)") if name == "IRT" else code(name), fmt_num(a.get("trades")),
                         ltr(qty_text(a.get("qty_to"), name)) if _is_num(a.get("qty_to")) else dash(),
                         fmt_num(a.get("value_to_irt"), 0), fmt_num(a.get("value_to_usdt"), 2),
                         signed_num(a.get("pnl_irt")), signed_num(a.get("pnl_usdt"), 2),
                         signed_num(a.get("pnl_irt_pct"), 2, "%")])
        if rows:
            rows.append(["<b>%s</b>" % te("Total"), "", "", fmt_num(sums[0], 0), fmt_num(sums[1], 2),
                         signed_num(sums[2]), signed_num(sums[3], 2), ""])
        return table([N_("Asset"), N_("Trades"), N_("Holding now"), N_("Value (IRT)"), N_("Value (USDT)"),
                      N_("P&L (IRT)"), N_("P&L (USDT)"), N_("P&L %")], rows, num=(1, 2, 3, 4, 5, 6, 7)) + help_p(te(
            "P&L of an asset = its value at the end - its value at the start - what was paid into it + what came out "
            "of it: every trade at its own price, the fees count against the asset traded. In USDT, every toman "
            "amount is divided by the USDT price of the same hour, so the rial's fall is taken out; the toman P&L "
            "of USDT is what the rial's fall gave. The rows add up to the change of the account rebuilt from the "
            "bot's trades."))

    def _positions_section(self, data, show_ta=True, toggle=""):
        """v3.10: the positions the account holds, and apart from them the coins it does not hold (a resting order:
        what the bot waits for). A report without the flag (an older worker) shows every chart as a position."""
        pos = [p for p in data.get("positions") or [] if isinstance(p, dict)]
        held = [p for p in pos if p.get("held", True)]
        other = [p for p in pos if not p.get("held", True)]
        has_ta = any(isinstance(p.get("ta"), dict) for p in pos)
        body = ('<div class="positions">%s</div>' % "".join(self._position_panel(p, show_ta) for p in held) if held else
                '<p class="muted empty">%s</p>' % te(NO_POSITION))
        out = card(te("Open positions"), body, "target", actions=toggle if has_ta else "")
        if other:
            out += card(te("Coins we do not hold: resting orders"), help_p(te(
                "The account holds none of these coins. The bot has a resting order on each - a buy of the crash "
                "ladder fills only if the price falls to its level. The charts, the indicators and Kimi's last "
                "analysis show what the bot is waiting for.")) + '<div class="positions">%s</div>' % "".join(
                self._position_panel(p, show_ta) for p in other), "eye")
        return out

    @staticmethod
    def _position_panel(p, show_ta=True):
        asset = str(p.get("asset") or "")
        now = p.get("price_usdt")
        line = [(x[0], x[1]) for x in p.get("prices") or [] if isinstance(x, list) and len(x) >= 2]
        o = p.get("outlook") if isinstance(p.get("outlook"), dict) else {}
        hl, series = [], [("pl", line)]
        items = [("pl", (tr("Price now %s") % ltr(price_text(now))) if _is_num(now) else te("Price"))]
        for cls, key, label in (("entry", "entry_usdt", N_("Entry %s")), ("stop", "stop_usdt", N_("Stop %s")),
                                ("target", "target_usdt", N_("Target %s")),
                                ("inv", "invalidation_usdt", N_("Invalidation %s"))):
            v = p.get(key)
            if _is_num(v):
                hl.append((cls, v))
                items.append((cls, tr(label) % ltr(price_text(v))))
        for od in p.get("orders") or []:
            if not isinstance(od, dict) or not _is_num(od.get("price_usdt")):
                continue
            cls = "buy" if od.get("side") == "buy" else "sell"
            hl.append((cls, od["price_usdt"]))
            text = (tr("Buy order %s") if cls == "buy" else tr("Sell order %s")) % ltr(price_text(od["price_usdt"]))
            if _is_num(od.get("amount")):
                text += " &middot; " + ltr(qty_text(od.get("amount"), asset))
            items.append((cls, text))
        if _is_num(p.get("set_at")):
            items.append(("set", tr("Plan set %s") % fmt_time(p.get("set_at"))))
        # v3.6.1 the outlook: Kimi's two scenarios and their weighted price from its decision to the end of the
        # plan, over the range a driftless walk stays in (68% / 95%) from now; the future is shaded
        cone = [c for c in o.get("cone") or [] if isinstance(c, list) and len(c) >= 5]
        areas = [("cone2", [(c[0], c[3], c[4]) for c in cone]), ("cone1", [(c[0], c[1], c[2]) for c in cone])]
        ta = p.get("ta") if show_ta and isinstance(p.get("ta"), dict) and p["ta"].get("t") else None
        ta_items = []
        if ta:                                  # v3.9: the indicators under the price line (it stays on top)
            ta_series, ta_areas, ta_hl, ta_items = ta_overlay(ta)
            series, areas, hl = ta_series + series, ta_areas + areas, hl + ta_hl
        origin, end, prob = o.get("origin"), o.get("to"), o.get("p")
        prob = prob if (_is_num(prob) and 0 <= prob <= 1) else None
        fc = []
        if isinstance(origin, list) and len(origin) >= 2 and _is_num(origin[0]) and _is_num(origin[1]) \
                and _is_num(end):
            for cls, key, label, pr in (("fc-tp", "target", N_("Target scenario %s"), prob),
                                        ("fc-inv", "invalidation", N_("Invalidation or the max hold %s"),
                                         (1.0 - prob) if _is_num(prob) else None),
                                        ("fc-ev", "expected", N_("Probability-weighted price %s"), None)):
                v = o.get(key)
                if _is_num(v):
                    series.append((cls, [(origin[0], origin[1]), (end, v)]))
                    text = tr(label) % ltr(price_text(v))
                    if _is_num(pr):
                        text += " &middot; " + tr("probability %s") % ltr(pct_text(pr * 100.0, 1))
                    fc.append((cls, text))
        if cone:
            fc.append(("cone", te("the normal range of the price: 68% and 95% of the time")))
        ts = [t for _c, pts in series for t, _v in pts if _is_num(t)] + [t for _c, a in areas for t, _l, _h in a
                                                                          if _is_num(t)]
        xr = (min(ts), max(ts)) if len(ts) > 1 and max(ts) > min(ts) else None
        shade = ("future", o.get("from"), end)
        chart = line_chart(series, hl, [("set", p.get("set_at")), ("now", o.get("from"))],
                           label=tr("Price of %s in USDT") % asset, cls="sm ta-main" if ta else "sm", areas=areas,
                           shade=shade, x_range=xr)
        fc_html = ('<p class="fc-h">%s</p>%s' % (tr("Outlook until %s") % fmt_time(end), legend(fc))) if fc else ""
        if ta:
            fc_html += '<p class="fc-h">%s</p>%s%s%s' % (
                te("Indicators: 4-hour bars in USDT, computed like the bot's market context"), legend(ta_items),
                ta_reading_html(ta), ta_panes(ta, xr, shade))
        ch = p.get("change_pct")
        badge = (' <span class="badge %s">%s</span>' % ("ok" if ch > 0 else ("err" if ch < 0 else ""),
                                                        ltr("%+.2f%%" % ch))) if _is_num(ch) else ""
        held = p.get("held", True)                      # v3.10: what the account holds, and what it does not
        badge = (' <span class="badge accent">%s</span>' % te("Held") if held else
                 ' <span class="badge">%s</span>' % te("Not held")) + badge
        qty = p.get("qty")
        if held:
            amount = ltr(qty_text(qty, asset)) if _is_num(qty) else dash()
        elif p.get("dust") and _is_num(qty):
            amount = tr("%s (a leftover below the minimum order)") % ltr(qty_text(qty, asset))
        else:
            amount = te("None")
        rows = [(N_("Amount"), amount),
                (N_("Value (IRT)"), fmt_num(p.get("value_irt"), 0)), (N_("Value (USDT)"), fmt_num(p.get("value_usdt"), 2)),
                (N_("Hold until"), fmt_time(p.get("max_hold_until")))]
        if not held:
            rows = rows[:1]
        return '<article class="position%s"><div class="position-h">%s%s</div>%s%s%s%s%s</article>' % (
            "" if held else " watch", code(asset + " / USDT"), badge, chart, legend(items), fc_html, facts(rows),
            analysis_html(p))

    def _history_filters(self, req):
        asset = re.sub(r"[^A-Z0-9]", "", req.query.get("asset", "").upper())[:12]
        side = req.query.get("side", "")
        return asset, (side if side in SIDE_LABELS else "")

    @staticmethod
    def _history_rows(data, asset, side):
        hist = [h for h in data.get("history") or [] if isinstance(h, dict)]
        return hist, [h for h in hist if (not asset or str(h.get("asset") or "").upper() == asset)
                      and (not side or h.get("side") == side)]

    def _history_get(self, req):
        title, sub = tr("Trade history"), tr("Every buy and sell of the bot, newest first")
        key, t_from, t_to, f_text, t_text, error = self._perf_range(req)
        asset, side = self._history_filters(req)
        try:
            page = max(1, int(req.query.get("page", "1").strip().translate(_DIGITS) or "1"))
        except ValueError:
            page = 1
        keep = [(k, v) for k, v in (("asset", asset), ("side", side)) if v]
        parts = [self._range_bar("/history", key, f_text, t_text, keep)]
        if error:
            parts.append(box("err", esc(error)))
            return self._page(req, title, "".join(parts), status=400, active="/history", sub=sub)
        data, err = self._perf_data(req, key, f_text, t_text, t_from, t_to)
        if err is not None:
            parts.append(self._helper_error(err))
            return self._page(req, title, "".join(parts), active="/history", sub=sub)
        hist, rows = self._history_rows(data, asset, side)
        rq = self._range_query(key, f_text, t_text)
        assets = sorted(set(str(h.get("asset") or "") for h in hist) - {""})
        form = '<form method="get" action="/history" class="filters">%s%s%s<div class="actions">%s%s</div></form>' % (
            "".join(hidden(k, v) for k, v in rq),
            field(te("Asset"), select("asset", [("", tr("All"))] + [(a, a) for a in assets], asset, "h-asset", "ltr"),
                  for_="h-asset"),
            field(te("Side"), select("side", [("", tr("All")), ("buy", tr("Buys")), ("sell", tr("Sells"))], side,
                                     "h-side"), for_="h-side"),
            button(tr("Filter"), "primary", "search"),
            link_button("/history.csv?" + urlencode(rq + keep), tr("Download CSV"), "secondary", "save"))
        pages = max(1, (len(rows) + HISTORY_PAGE_ROWS - 1) // HISTORY_PAGE_ROWS)
        page = min(page, pages)
        trs = []
        for h in rows[(page - 1) * HISTORY_PAGE_ROWS:page * HISTORY_PAGE_ROWS]:
            s, fee = h.get("side"), h.get("fee")
            trs.append([fmt_time(h.get("t")), code(h.get("symbol")),
                        '<span class="badge %s">%s</span>' % ("ok" if s == "buy" else "err",
                                                              te(SIDE_LABELS[s]) if s in SIDE_LABELS else esc(s)),
                        ltr(qty_text(h.get("base"), str(h.get("asset") or ""))),
                        '%s <span class="muted small">%s</span>' % (ltr(price_text(h.get("price"))),
                                                                    esc(h.get("quote_asset") or "")),
                        fmt_num(h.get("value_irt"), 0), fmt_num(h.get("value_usdt"), 2),
                        ('%s <span class="muted small">%s</span>' % (ltr(qty_text(fee, h.get("fee_asset"))),
                                                                     esc(h.get("fee_asset") or "")))
                        if _is_num(fee) and fee else dash(),
                        ltr(h.get("reason")) if h.get("reason") else dash()])
        buys = sum(1 for h in rows if h.get("side") == "buy")
        summary = tr("%s trades: %s buys and %s sells.") % (ltr(len(rows)), ltr(buys), ltr(len(rows) - buys))
        total = data.get("history_total")
        if _is_num(total) and total > len(hist):
            summary += " " + tr("Only the newest %s of the range are listed.") % ltr(len(hist))
        nav = []
        if page > 1:
            nav.append(link_button("/history?" + urlencode(rq + keep + [("page", page - 1)]), tr("Newer"),
                                   "secondary sm"))
        nav.append('<span class="muted small">%s</span>' % (tr("Page %s of %s") % (ltr(page), ltr(pages))))
        if page < pages:
            nav.append(link_button("/history?" + urlencode(rq + keep + [("page", page + 1)]), tr("Older"),
                                   "secondary sm"))
        parts.append(card("", form))
        parts.append(card(te("Trades"), '<p class="muted">%s</p>%s<div class="pager">%s</div>' % (
            summary, table([N_("Time"), N_("Market"), N_("Side"), N_("Amount"), N_("Price"), N_("Value (IRT)"),
                            N_("Value (USDT)"), N_("Fee"), N_("Reason")], trs, num=(3, 4, 5, 6, 7)), "".join(nav)),
            "list"))
        return self._page(req, title, "".join(parts), active="/history", sub=sub)

    def _history_csv(self, req):
        key, t_from, t_to, f_text, t_text, error = self._perf_range(req)
        if error:
            return self.error_response(400, "Bad Request (range)")
        asset, side = self._history_filters(req)
        data, err = self._perf_data(req, key, f_text, t_text, t_from, t_to)
        if err is not None:
            return self._page(req, tr("Trade history"), self._helper_error(err), status=502, active="/history")
        _hist, rows = self._history_rows(data, asset, side)

        def num(v):
            return ("%.10f" % v).rstrip("0").rstrip(".") if _is_num(v) else ""

        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(["time_utc", "time_tehran", "market", "side", "amount", "price", "quote_asset", "value_irt",
                    "value_usdt", "fee", "fee_asset", "reason", "route", "order_id"])
        for h in rows:
            t = h.get("t")
            w.writerow([_utc_iso(t), datetime.fromtimestamp(t, TEHRAN).strftime("%Y-%m-%d %H:%M:%S")
                        if (_is_num(t) and 0 < t < 1e11) else "", csv_cell(h.get("symbol")), csv_cell(h.get("side")), num(h.get("base")),
                        num(h.get("price")), csv_cell(h.get("quote_asset")), num(h.get("value_irt")),
                        num(h.get("value_usdt")), num(h.get("fee")), csv_cell(h.get("fee_asset")),
                        csv_cell(h.get("reason")), csv_cell(h.get("route")), csv_cell(h.get("order_id"))])
        name = "bitpin-trades-%s.csv" % datetime.fromtimestamp(req.now, TEHRAN).strftime("%Y%m%d-%H%M")
        return self._resp(200, u"﻿" + buf.getvalue(), "text/csv; charset=utf-8",
                          [("Content-Disposition", 'attachment; filename="%s"' % name)])

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
        parts = ['<div class="grid2">%s%s</div>' % (pw_card, totp_card), self._cert_card(req),
                 card(te("Recent events (50)"), self._audit_table(), "list", cls="flush")]
        return self._page(req, tr("Security"), "".join(parts), status=status, active="/security",
                          sub=tr("Password, two-step login, the certificate and the audit log"))

    def _cert_card(self, req):
        """v3.8.2: the panel's certificate and its automatic renewal (helper panel_cert): its names, who issued it,
        its end, the renewal timer and its last run, the fingerprint."""
        title = te("The panel's certificate")
        data, err = self._call(req, "panel_cert")
        if err is not None:
            return card(title, self._helper_error(err), "shield")
        info = data.get("cert") if isinstance(data.get("cert"), dict) else {}
        if not info.get("names"):
            return card(title, box("warn", tr("The certificate cannot be read: %s") % bdi(info.get("error") or "?")),
                        "shield")
        days, own = info.get("days_left"), bool(info.get("self_signed"))
        left = ""
        if _is_num(days):
            kind = "err" if days < 0 else ("warn" if days < CERT_WARN_DAYS and not own else "ok")
            left = ' <span class="badge %s">%s</span>' % (kind, esc(tr("%s days left") % days) if days >= 0
                                                         else te("Expired"))
        rows = [(N_("Names"), " ".join(code(n) for n in info["names"])),
                (N_("Issued by"), te("Self-signed (made by the panel's setup)") if own else bdi(info.get("issuer"))),
                (N_("Valid until"), fmt_time(info.get("not_after")) + left)]
        if data.get("managed"):
            t = data.get("timer") if isinstance(data.get("timer"), dict) else {}
            on = t.get("enabled") == "enabled" and t.get("state") == "active"
            ren = '<span class="badge %s">%s</span>' % ("ok" if on else "warn", te("On") if on else te("Off"))
            if on and data.get("next"):
                ren += " " + tr("next check %s") % ltr(data["next"])
            rows.append((N_("Automatic renewal"), ren))
            last = data.get("last_run") if isinstance(data.get("last_run"), dict) else None
            if last:
                ok = last.get("result") == "success"
                rows.append((N_("Last renewal check"), ltr(last.get("at")) + ' <span class="badge %s">%s</span>' % (
                    "ok" if ok else "err", te("Done") if ok else esc(tr("Failed (exit %s)") % (last.get("status") or "?")))))
        rows.append((N_("Fingerprint (SHA-256)"), '<code dir="ltr" class="fp">%s</code>' % esc(info.get("fingerprint"))))
        notes = []
        if _is_num(days) and days < 0:
            notes.append(box("err", te("The certificate has expired and browsers refuse the domain. Open the panel by "
                                       "the server's IP address and run the panel-cert command with the domain "
                                       "again.")))
        elif _is_num(days) and days < CERT_WARN_DAYS and not own:
            notes.append(box("warn", te("The certificate ends soon and was not renewed: see the last renewal check "
                                        "above and the Telegram alerts.")))
        if own:
            notes.append(help_p(tr("The browser warns about a self-signed certificate: compare the fingerprint above. "
                                   "A trusted certificate for a domain: <code>sudo bitpin-bot panel-cert DOMAIN</code> "
                                   "(the panel guide).")))
        else:
            notes.append(help_p(te("It renews itself from 30 days before its end; a failed renewal is reported in "
                                   "Telegram. The bot and its trading do not depend on it.")))
        return card(title, kv_table(rows) + "".join(notes), "shield")

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
