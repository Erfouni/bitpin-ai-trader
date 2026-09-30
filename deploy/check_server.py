#!/usr/bin/env python3
"""Pre-flight check for the Bitpin bot server (stdlib only, Python 3.8+).

The bot needs BOTH of these from the same server:
  * Bitpin  (https://api.bitpin.org)  - reachable from an IRANIAN IP, and the API key only works from
    the IPs on its whitelist. The bot ALWAYS reaches Bitpin directly: http_proxy / https_proxy /
    ALL_PROXY set on the server are ignored (so Bitpin always sees the server's own IP);
  * Moonshot / Kimi (https://api.moonshot.ai/v1 or https://api.moonshot.cn/v1) - may block Iranian IPs.
    The bot sends ONLY its Kimi requests through a proxy, and only when one is configured: the
    environment variable KIMI_HTTPS_PROXY (e.g. http://127.0.0.1:1081 - a local HTTP proxy you already
    run) or llm.proxy in kimi.json. This check uses exactly the same routes.

What this script does:
  0. validates config.json and kimi.json OFFLINE with the bot's own code, exactly as the bot reads
     them at startup (JSON syntax, unknown keys, types, ranges, dates, model set). Any problem is a
     FAIL: the bot would refuse to start with that file;
  1. prints this server's public IPv4 address(es) as seen directly (the way Bitpin sees them) - add
     EVERY one to the Bitpin API key whitelist (a server with several outbound IPs shows all of them);
  2. checks HTTPS reachability and latency of Bitpin's public endpoints (tickers, order book, candles)
     and centrifugo.bitpin.org, and compares the local clock with Bitpin's HTTP Date header;
  3. with --auth and BITPIN_API_KEY / BITPIN_SECRET_KEY set: ONE authenticate call to prove the IP
     whitelist works (only the HTTP status / error code is printed, never keys or tokens);
  4. with KIMI_API_KEY set: GET <base_url>/models on the base_url configured in kimi.json only (never
     over plain http), through KIMI_HTTPS_PROXY when it is set; prints whether it accepts the key and
     the model ids it offers (never the key). "run_bot.py kimi-check" does the same with the bot's own
     LLM client.
     --kimi-both-regions also tries the other official Moonshot platform (.ai / .cn) with the key.

Usage:
  python3 check_server.py                 no keys needed: IP, Bitpin, latency, clock
  sudo bitpin-bot check                   the same with the service's env file and config files
                                          (keys, KIMI_HTTPS_PROXY, config.json and kimi.json)
  sudo bitpin-bot check --auth            + one Bitpin authenticate call (1 of ~200 per day)
  python3 check_server.py --kimi-probe    test Moonshot reachability WITHOUT a key (expects HTTP 401)
  check_server.py --config-only --config C --kimi-config K
                                          only the offline validation (update.sh uses it)

Exit status: 0 = everything the bot needs passed (warnings possible) - "RESULT: OK" means the bot can
start with these files and keys; 1 = a config, Bitpin or Kimi check failed.
"""
import argparse
import email.utils
import http.client
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone

BITPIN_URL = "https://api.bitpin.org"
TICKERS_PATH = "/api/v1/mkt/tickers/"
ORDERBOOK_PATH = "/api/v1/mth/orderbook/USDT_IRT/"
BARS_PATH = "/v1/mkt/tv/get_bars/"          # the candle endpoint used by bitpin/data.py
AUTH_PATH = "/api/v1/usr/authenticate/"
CENTRIFUGO_URL = "https://centrifugo.bitpin.org/"
KIMI_BASES = ("https://api.moonshot.ai/v1", "https://api.moonshot.cn/v1")
IP_ECHO = ("https://api.ipify.org", "https://checkip.amazonaws.com", "https://ipv4.icanhazip.com",
           "https://ifconfig.me/ip")
COUNTRY_URLS = ("https://ipinfo.io/{ip}/country", "https://api.country.is/{ip}")
DEFAULT_KIMI_CONFIG = "/etc/bitpin-bot/kimi.json"
DEFAULT_BOT_CONFIG = "/etc/bitpin-bot/config.json"
ENV_FILE_HINT = "/etc/bitpin-bot/bitpin-bot.env"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIMI_KEY_ENV_RE = re.compile(r"^(KIMI|MOONSHOT)_[A-Z0-9_]*$")   # the same rule as bitpin/llm.py
MODEL_NOT_SET = "llm.model is not set"
MAX_SKEW = 5.0
UA = "bitpin-bot-check/1.0"
KIMI_PROXY_ENV = "KIMI_HTTPS_PROXY"   # the bot's ONLY proxy setting (Kimi traffic only; bitpin/llm.py)
PROXY_HINT = ("if you already run a local HTTP proxy, set %s=http://127.0.0.1:PORT in %s (only Kimi "
              "traffic uses it; Bitpin always goes direct), then: sudo bitpin-bot kimi-check"
              % (KIMI_PROXY_ENV, "/etc/bitpin-bot/bitpin-bot.env"))
ENV_PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

_SECRETS = []  # never printed: every output line is scrubbed of these values


# --------------------------------------------------------------------------- output

class Report(object):
    def __init__(self):
        self.failed = []
        self.warnings = 0
        self.summary = []

    @staticmethod
    def _out(tag, msg):
        text = str(msg)
        for s in _SECRETS:
            text = text.replace(s, "<secret>")
        text = text.encode("ascii", "replace").decode("ascii")
        lines = text.split("\n")
        print("[%-4s] %s" % (tag, lines[0]))
        for ln in lines[1:]:
            print("       %s" % ln)
        sys.stdout.flush()

    def ok(self, msg):
        self._out("OK", msg)

    def info(self, msg):
        self._out("INFO", msg)

    def skip(self, msg):
        self._out("SKIP", msg)

    def warn(self, msg):
        self.warnings += 1
        self._out("WARN", msg)

    def fail(self, area, msg):
        if area not in self.failed:
            self.failed.append(area)
        self._out("FAIL", msg)

    def add_summary(self, label, value):
        self.summary.append((label, value))


def section(title):
    print("")
    print("== %s %s" % (title, "=" * max(3, 70 - len(title))))


# --------------------------------------------------------------------------- HTTP

class Resp(object):
    def __init__(self):
        self.status = None      # HTTP status, or None on a network error
        self.headers = None
        self.body = b""
        self.error = None       # human readable network error
        self.elapsed = 0.0      # seconds
        self.t_mid = 0.0        # wall-clock midpoint of the request

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects (a redirected authenticate call would re-send the keys elsewhere)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def describe_error(e):
    reason = getattr(e, "reason", e)
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed (%s)" % reason
    if isinstance(reason, (socket.timeout,)) or "timed out" in str(reason).lower():
        return "timeout (blocked, filtered or very slow network)"
    if isinstance(reason, ssl.SSLError):
        s = str(reason)
        if "CERTIFICATE_VERIFY_FAILED" in s:
            return ("TLS certificate verification failed - missing CA certificates "
                    "(sudo apt install ca-certificates) or a filtering middlebox (%s)" % s)
        return "TLS error (%s) - often a filtered connection" % s
    if isinstance(reason, ConnectionResetError):
        return "connection reset (typical for filtering / firewalls)"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    return str(reason) or reason.__class__.__name__


def _proxy_handler(proxy):
    """The bot's own StrictProxyHandler (bitpin/news.py: a configured proxy is ALWAYS used, no_proxy /
    NO_PROXY are never consulted), or urllib's ProxyHandler when the bot code cannot be imported."""
    mapping = {"https": proxy, "http": proxy} if proxy else {}
    try:
        if PROJECT_ROOT not in sys.path:
            sys.path.insert(0, PROJECT_ROOT)
        from bitpin.news import StrictProxyHandler
        return StrictProxyHandler(mapping)
    except Exception:  # noqa: BLE001 - the check must still run without the bot code
        return urllib.request.ProxyHandler(mapping)


def request(method, url, headers=None, body=None, timeout=15.0, proxy=None, max_bytes=8 * 1024 * 1024):
    """proxy: None = DIRECT (environment proxy variables are ignored, like the bot does for Bitpin),
    or an explicit proxy URL (the Kimi proxy, like bitpin/llm.py; no_proxy never bypasses it)."""
    ph = _proxy_handler(proxy)
    opener = urllib.request.build_opener(_NoRedirect(), ph)
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    r = Resp()
    w0, t0 = time.time(), time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as f:
            r.status, r.headers = f.status, f.headers
            r.body = f.read(max_bytes)
    except urllib.error.HTTPError as e:
        r.status, r.headers = e.code, e.headers
        try:
            r.body = e.read(max_bytes)
        except Exception:  # noqa: BLE001
            r.body = b""
    except urllib.error.URLError as e:
        r.error = describe_error(e)
    except (socket.timeout, ssl.SSLError, http.client.HTTPException, OSError) as e:
        r.error = describe_error(e)
    r.elapsed = time.monotonic() - t0
    r.t_mid = (w0 + time.time()) / 2.0
    return r


def kimi_route(cfg=None):
    """(proxy URL or None, source, error) exactly as bitpin/llm.py LLMClient picks it: the environment
    variable KIMI_HTTPS_PROXY wins, else llm.proxy of kimi.json, else direct. error: why the bot would
    refuse the setting (it then exits 78 at startup)."""
    raw = os.environ.get(KIMI_PROXY_ENV)
    source = KIMI_PROXY_ENV
    if not (raw or "").strip():
        raw, source = (cfg or {}).get("proxy"), "llm.proxy"
    if not isinstance(raw, str) or not raw.strip():
        return None, None, None
    try:
        _, llm_mod, _, _ = _import_bot()
        return llm_mod.parse_proxy_url(raw, source), source, None
    except Exception as e:  # noqa: BLE001 - ConfigError, or the bot code is not importable
        if type(e).__name__ == "ConfigError":
            return None, source, str(e)
    p = raw.strip()
    p = p if "://" in p else "http://" + p
    if urllib.parse.urlsplit(p).scheme.lower() not in ("http", "https"):
        return None, source, "%s must be an http:// proxy URL like http://127.0.0.1:1081" % source
    return p, source, None


def mask_proxy(p):
    if not p:
        return "direct"
    s = urllib.parse.urlsplit(p if "://" in p else "http://" + p)
    try:
        port = ":%d" % s.port if s.port else ""
    except ValueError:
        port = ""
    auth = "***@" if (s.username or s.password) else ""
    return "via proxy %s://%s%s%s" % (s.scheme or "http", auth, s.hostname or "?", port)


def ms(seconds):
    return "%.0f ms" % (seconds * 1000.0)


def median(values):
    v = sorted(values)
    n = len(v)
    if not n:
        return None
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def server_time(resp):
    d = resp.headers.get("Date") if resp.headers is not None else None
    if not d:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(d)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = 0
    try:
        age = max(0, int(resp.headers.get("Age") or 0))   # a CDN-cached reply keeps its original Date
    except ValueError:
        pass
    return dt.timestamp() + age + 0.5   # the header has 1 s resolution (truncated)


def safe_code(resp):
    """The short machine error code of an error response, if any (never the whole body)."""
    d = resp.json()
    if isinstance(d, dict):
        for k in ("code", "error_code", "type"):
            v = d.get(k)
            if isinstance(v, str) and re.match(r"^[A-Za-z0-9_.-]{1,64}$", v):
                return v
        err = d.get("error")
        if isinstance(err, dict):
            v = err.get("type") or err.get("code")
            if isinstance(v, str) and re.match(r"^[A-Za-z0-9_.-]{1,64}$", v):
                return v
    return None


# --------------------------------------------------------------------------- checks

def check_system(rep):
    section("System")
    v = sys.version_info
    ver = "%d.%d.%d" % (v[0], v[1], v[2])
    if v >= (3, 8):
        rep.ok("Python %s (%s) on %s" % (ver, sys.executable, platform.platform()))
    else:
        rep.warn("Python %s: the bot needs Python 3.8 or newer" % ver)
    rep.info("local time: %s UTC" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    if not sys.platform.startswith("linux"):
        rep.info("not Linux: time-sync service check skipped")
        return
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "Timezone"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                             universal_newlines=True).stdout
    except (OSError, subprocess.SubprocessError):
        rep.info("timedatectl not available: time-sync service check skipped")
        return
    vals = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    if vals.get("NTPSynchronized") == "yes":
        rep.ok("system clock is NTP-synchronised (timezone %s; the bot itself uses UTC)" % vals.get("Timezone", "?"))
    elif vals:
        rep.warn("system clock is NOT NTP-synchronised. This server runs other services too: ask its "
                 "administrator to enable time synchronisation rather than changing it yourself. The clock "
                 "skew vs Bitpin is measured below.")


def check_environment(rep, args, kimi_cfg=None):
    section("Environment and network routes")
    for name in ("BITPIN_API_KEY", "BITPIN_SECRET_KEY", "KIMI_API_KEY"):
        raw = os.environ.get(name)
        if not raw:
            rep.info("%s not set" % name)
            continue
        clean = raw.strip()
        if clean:
            _SECRETS.append(clean)
        problems = []
        if raw != clean:
            problems.append("leading/trailing whitespace or Windows line ending")
        if any(c.isspace() for c in clean):
            problems.append("contains spaces")
        if clean[:1] in "\"'" or clean[-1:] in "\"'":
            problems.append("contains quotes")
        if problems:
            rep.warn("%s is set (%d characters) but %s - fix it in %s"
                     % (name, len(clean), ", ".join(problems), ENV_FILE_HINT))
        else:
            rep.ok("%s is set (%d characters)" % (name, len(clean)))
    env_px = [n for n in ENV_PROXY_VARS if os.environ.get(n)]
    if env_px:
        rep.info("%s set on this server: IGNORED by the bot (Bitpin always goes direct; Kimi uses only %s)"
                 % (", ".join(env_px), KIMI_PROXY_ENV))
    rep.info("Bitpin  (%s): direct (always)" % urllib.parse.urlsplit(args.bitpin_url).hostname)
    proxy, source, err = kimi_route(kimi_cfg)
    if err:
        rep.fail("config", "Kimi proxy setting refused by the bot (it would not start): %s" % err)
    elif proxy:
        rep.info("Moonshot (Kimi): %s (from %s)" % (mask_proxy(proxy), source))
    else:
        rep.info("Moonshot (Kimi): direct (%s not set)" % KIMI_PROXY_ENV)
    return proxy


def ip_family_to(host, rep):
    try:
        infos = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
    except socket.gaierror as e:
        rep.warn("DNS lookup of %s failed: %s" % (host, e))
        return None
    fams = sorted(set("IPv6" if i[0] == socket.AF_INET6 else "IPv4" for i in infos))
    try:
        s = socket.create_connection((host, 443), timeout=8)
    except OSError as e:
        rep.info("%s resolves to %s addresses; TCP connect failed (%s)" % (host, "+".join(fams), describe_error(e)))
        return None
    fam = "IPv6" if s.family == socket.AF_INET6 else "IPv4"
    s.close()
    rep.info("%s resolves to %s addresses; this server connects over %s" % (host, "+".join(fams), fam))
    return fam


def check_public_ip(rep, args):
    section("Public IP (for the Bitpin API key whitelist)")
    if args.no_ip:
        rep.skip("--no-ip given")
        return None
    # DIRECT, like every Bitpin request of the bot. Every echo service is asked (twice): a server
    # behind several NAT addresses shows a different outbound IP to different connections, and
    # Bitpin must have ALL of them on the whitelist.
    seen = []
    ip, src = None, None
    for _ in range(2):
        for url in IP_ECHO:
            r = request("GET", url, headers={"Accept": "text/plain"}, timeout=min(args.timeout, 8), max_bytes=256)
            if r.status != 200:
                continue
            txt = r.body.decode("ascii", "replace").strip()
            try:
                addr = ipaddress.ip_address(txt)
            except ValueError:
                continue
            if addr.version == 4:
                if ip is None:
                    ip, src = str(addr), url
                if str(addr) not in seen:
                    seen.append(str(addr))
    if not ip:
        rep.warn("could not determine the public IPv4 (all IP echo services failed). Ask your hosting provider, "
                 "or run: curl -4 https://api.ipify.org")
        rep.add_summary("Public IPv4", "unknown")
        return None
    country = None
    for tmpl in COUNTRY_URLS:
        # DIRECT as well: this looks up the server's OWN address, and only Kimi traffic may use the proxy.
        r = request("GET", tmpl.format(ip=ip), timeout=min(args.timeout, 8), max_bytes=4096)
        if r.status != 200:
            continue
        txt = r.body.decode("utf-8", "replace").strip()
        d = r.json() if txt.startswith("{") else None
        cand = (d.get("country") if isinstance(d, dict) else txt) or ""
        if re.match(r"^[A-Z]{2}$", cand.strip()):
            country = cand.strip()
            break
    rep.ok("public IPv4: %s   [from %s%s]" % (ip, urllib.parse.urlsplit(src).hostname,
                                             ", country %s" % country if country else ""))
    if len(seen) > 1:
        rep.warn("this server leaves the Internet with SEVERAL IPv4 addresses: %s. Add EVERY one of them to the IP "
                 "whitelist of your Bitpin API key, otherwise some Bitpin requests are refused (406)."
                 % ", ".join(seen))
    else:
        rep.info("add this IP to the IP whitelist of your Bitpin API key (plus any other outbound IP your "
                 "hosting provider lists for this server)")
    if country and country != "IR":
        rep.warn("this IP is registered in %s, not Iran: Bitpin may refuse it (see the Bitpin checks below)" % country)
    rep.add_summary("Public IPv4", "%s%s  -> add %s to the Bitpin API key whitelist"
                    % (", ".join(seen), " (%s)" % country if country else "",
                       "ALL of them" if len(seen) > 1 else "it"))
    fam = ip_family_to(urllib.parse.urlsplit(args.bitpin_url).hostname, rep)
    if fam == "IPv6":
        rep.warn("Bitpin is reached over IPv6, so Bitpin sees this server's IPv6 address, not the IPv4 above. "
                 "Whitelist the IPv6 address too (curl -6 https://api64.ipify.org), or ask the server "
                 "administrator whether IPv4 can be preferred (do not change system settings of a shared server "
                 "yourself)")
    return ip


def _latency_line(label, samples):
    if not samples:
        return None
    return "%s latency: min %s / median %s / max %s (%d requests)" % (
        label, ms(min(samples)), ms(median(samples)), ms(max(samples)), len(samples))


def check_bitpin(rep, args):
    section("Bitpin public API (%s)" % args.bitpin_url)
    base = args.bitpin_url.rstrip("/")
    lat, skews = [], []
    ok_all = True

    # tickers
    good = None
    last = None
    for _ in range(max(1, args.samples)):
        r = request("GET", base + TICKERS_PATH, timeout=args.timeout)
        last = r
        if r.error:
            break
        if r.status == 200:
            lat.append(r.elapsed)
            st = server_time(r)
            if st is not None:
                skews.append(r.t_mid - st)
            good = r
    if good is None:
        ok_all = False
        if last.error:
            rep.fail("bitpin", "tickers %s: unreachable - %s" % (TICKERS_PATH, last.error))
        elif last.status in (401, 403, 451):
            rep.fail("bitpin", "tickers %s: HTTP %s - access refused. Bitpin probably serves only Iranian IPs: "
                               "use an Iranian server." % (TICKERS_PATH, last.status))
        elif last.status == 429:
            rep.fail("bitpin", "tickers: HTTP 429 (rate limited) - wait a minute and run again")
        else:
            rep.fail("bitpin", "tickers %s: HTTP %s" % (TICKERS_PATH, last.status))
    else:
        d = good.json()
        rows = d.get("results") if isinstance(d, dict) else d
        if not isinstance(rows, list) or not rows:
            ok_all = False
            rep.fail("bitpin", "tickers: HTTP 200 but not the expected JSON list (a captive portal or filter page?)")
        else:
            usdt = None
            for t in rows:
                if isinstance(t, dict) and str(t.get("symbol", "")).upper() == "USDT_IRT":
                    usdt = t.get("price")
            rep.ok("tickers: %d markets, %s, %d KB%s" % (
                len(rows), ms(good.elapsed), len(good.body) // 1024,
                ", USDT_IRT = %s toman" % usdt if usdt is not None else ""))

    # order book
    if ok_all:
        ob = None
        for _ in range(max(1, args.samples)):
            r = request("GET", base + ORDERBOOK_PATH, timeout=args.timeout)
            if r.error:
                ob = r
                break
            if r.status == 200:
                lat.append(r.elapsed)
                st = server_time(r)
                if st is not None:
                    skews.append(r.t_mid - st)
            ob = r
        d = ob.json() if ob is not None and ob.status == 200 else None
        if isinstance(d, dict) and isinstance(d.get("asks"), list) and isinstance(d.get("bids"), list):
            rep.ok("order book USDT_IRT: %d asks / %d bids, %s" % (len(d["asks"]), len(d["bids"]), ms(ob.elapsed)))
        else:
            ok_all = False
            rep.fail("bitpin", "order book %s: %s" % (ORDERBOOK_PATH, ob.error or "HTTP %s / unexpected reply" % ob.status))

    # candles (bitpin/data.py endpoint)
    if ok_all:
        now = int(time.time())
        q = urllib.parse.urlencode({"symbol": "USDT_IRT", "res": "60", "from": now - 6 * 3600, "to": now})
        r = request("GET", base + BARS_PATH + "?" + q, timeout=args.timeout)
        d = r.json() if r.status == 200 else None
        if isinstance(d, list) and d:
            try:
                newest = max(int(float(b["ts"])) for b in d)
            except (KeyError, TypeError, ValueError):
                newest = None
            if newest is None:
                rep.warn("candles: unexpected bar format")
            else:
                age = (now - newest) / 60.0
                msg = "candles USDT_IRT 1h: %d bars, newest opened %.0f min ago, %s" % (len(d), age, ms(r.elapsed))
                if age <= 120:
                    rep.ok(msg)
                else:
                    rep.warn(msg + " - candles look stale")
        else:
            ok_all = False
            rep.fail("bitpin", "candles %s: %s" % (BARS_PATH, r.error or "HTTP %s / unexpected reply" % r.status))

    # centrifugo (websocket server; any HTTP answer means TLS + routing work)
    r = request("GET", CENTRIFUGO_URL, timeout=args.timeout, max_bytes=4096)
    if r.error:
        rep.warn("centrifugo.bitpin.org: unreachable - %s (not used by the current REST-only bot)" % r.error)
    else:
        rep.ok("centrifugo.bitpin.org: reachable (HTTP %s, %s)" % (r.status, ms(r.elapsed)))

    line = _latency_line("Bitpin", lat)
    if line:
        m = median(lat)
        (rep.warn if m > 2.0 else rep.ok)(line + (" - slow" if m > 2.0 else ""))

    # clock
    if skews:
        sk = median(skews)
        if abs(sk) > MAX_SKEW:
            rep.warn("clock skew vs Bitpin: local clock is %+.1f s %s. Ask the server administrator to fix the time "
                     "synchronisation (the bot tolerates some skew, but tokens and candle timing need a correct clock)"
                     % (sk, "ahead" if sk > 0 else "behind"))
        else:
            rep.ok("clock skew vs Bitpin's Date header: %+.1f s (limit %.0f s)" % (sk, MAX_SKEW))
        rep.add_summary("Clock skew", "%+.1f s" % sk)
    elif ok_all:
        rep.warn("Bitpin sent no usable Date header: clock not compared")
    rep.add_summary("Bitpin public API", "OK (median %s)" % ms(median(lat)) if ok_all and lat else "FAILED")
    return ok_all


def check_bitpin_auth(rep, args):
    section("Bitpin authentication (IP whitelist)")
    key = (os.environ.get("BITPIN_API_KEY") or "").strip()
    secret = (os.environ.get("BITPIN_SECRET_KEY") or "").strip()
    if not args.auth:
        if key and secret:
            rep.info("keys are set; run again with --auth to verify key + IP whitelist (uses 1 authenticate call)")
        else:
            rep.skip("BITPIN_API_KEY / BITPIN_SECRET_KEY not set (and no --auth): authentication not tested")
        rep.add_summary("Bitpin auth", "not tested (use --auth)")
        return
    if not (key and secret):
        rep.fail("bitpin-auth", "--auth given but BITPIN_API_KEY and BITPIN_SECRET_KEY are not both set "
                                "(run it as: sudo bitpin-bot check --auth)")
        rep.add_summary("Bitpin auth", "FAILED (no keys)")
        return
    rep.warn("--auth uses 1 of Bitpin's ~200 authenticate calls per day (the bot keeps its own budget of "
             "150 per 24 h and does not know about this call). Do not repeat it in a loop.")
    r = request("POST", args.bitpin_url.rstrip("/") + AUTH_PATH, body={"api_key": key, "secret_key": secret},
                timeout=args.timeout, max_bytes=65536)
    code = safe_code(r) if (r.status or 0) >= 400 else None
    ok_body = False
    if r.status == 200:
        d = r.json()
        ok_body = isinstance(d, dict) and bool(d.get("access")) and bool(d.get("refresh"))
    r.body = b""  # drop the tokens right away; they are never printed or stored
    if r.error:
        rep.fail("bitpin-auth", "authenticate: unreachable - %s" % r.error)
        rep.add_summary("Bitpin auth", "FAILED (network)")
    elif r.status == 200 and ok_body:
        rep.ok("authenticate: HTTP 200 - the keys are valid and this server's IP is accepted")
        rep.add_summary("Bitpin auth", "OK")
    elif r.status == 200:
        rep.fail("bitpin-auth", "authenticate: HTTP 200 but no tokens in the reply")
        rep.add_summary("Bitpin auth", "FAILED (unexpected reply)")
    elif r.status == 406 or code == "api_credential_wrong":
        rep.fail("bitpin-auth", "authenticate: HTTP %s %s - IP not in the key's whitelist, or wrong key/secret "
                                "(typo, swapped, spaces). Check the whitelist IP above and the env file."
                 % (r.status, code or "api_credential_wrong"))
        rep.add_summary("Bitpin auth", "FAILED (406 api_credential_wrong)")
    elif r.status == 429:
        rep.fail("bitpin-auth", "authenticate: HTTP 429 - too many requests. Wait 10+ minutes; do not retry in a loop.")
        rep.add_summary("Bitpin auth", "FAILED (429)")
    else:
        rep.fail("bitpin-auth", "authenticate: HTTP %s %s" % (r.status, code or ""))
        rep.add_summary("Bitpin auth", "FAILED (HTTP %s)" % r.status)


# --------------------------------------------------------------------------- configuration files

def _import_bot():
    """The bot's own modules (from the project this script belongs to)."""
    if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)
    import bitpin.brain as brain_mod    # noqa: E402 - imported late on purpose
    import bitpin.llm as llm_mod
    import bitpin.risk as risk_mod
    import bitpin.runner as runner_mod
    return brain_mod, llm_mod, risk_mod, runner_mod


def _file_state(path):
    """'ok' | 'missing' | 'denied' | 'error: ...'"""
    try:
        with open(path, "rb") as f:
            f.read(1)
        return "ok"
    except FileNotFoundError:
        return "missing"
    except PermissionError:
        return "denied"
    except OSError as e:
        return "error: %s" % e


def _config_paths(args):
    """[(what, path, explicit)] for config.json and kimi.json."""
    return [("config.json", args.config or DEFAULT_BOT_CONFIG, args.config is not None),
            ("kimi.json", args.kimi_config or DEFAULT_KIMI_CONFIG, args.kimi_config is not None)]


def _report_unreadable(rep, what, path, state, explicit, config_only):
    if state == "missing":
        if explicit and not config_only:
            rep.fail("config", "%s %s does not exist (install.sh creates it from the example)" % (what, path))
        else:
            rep.info("%s %s not found: not checked" % (what, path))
    elif state == "denied":
        msg = "cannot read %s as this user: run it as  sudo bitpin-bot check" % path
        if explicit:
            rep.fail("config", msg)
        else:
            rep.info(msg)
    else:
        rep.fail("config", "cannot read %s (%s)" % (path, state))


def kimi_settings(llm_mod, brain_mod, path):
    """base_url / model / api_key_env / risk_profile of a kimi.json for the Moonshot check: the
    validated values if the file is valid, else whatever can be read (never trusted blindly:
    check_kimi re-checks the key variable name and https)."""
    try:
        cfg = brain_mod.load_kimi_config(path)
    except Exception:  # noqa: BLE001 - already reported by check_configs
        return {"exists": True, "invalid": True}
    llm = cfg.get("llm") or {}
    out = {"exists": True, "risk_profile": (cfg.get("brain") or {}).get("risk_profile")}
    news = cfg.get("news")
    if isinstance(news, dict) and news.get("enabled", True) is not False:
        out["news_model"] = str(news.get("model") or "kimi-k2.6").strip()
    try:
        v = llm_mod.validate_llm_config(llm)
        out.update(base_url=v["base_url"], model=v["model"], api_key_env=v["api_key_env"], proxy=v.get("proxy"),
                   provider=v.get("provider"))
    except Exception:  # noqa: BLE001
        out.update(invalid=True, base_url=llm.get("base_url"), model=llm.get("model"),
                   api_key_env=llm.get("api_key_env"), proxy=llm.get("proxy"), provider=llm.get("provider"))
    return out


def check_configs(rep, args):
    """Validate config.json and kimi.json offline with the bot's own code. Returns the kimi
    settings for check_kimi ({} when there is no readable kimi.json)."""
    section("Configuration files (checked offline with the bot's own code)")
    paths = _config_paths(args)
    strict = args.config_only or any(explicit for _, _, explicit in paths)
    try:
        brain_mod, llm_mod, risk_mod, runner_mod = _import_bot()
    except Exception as e:  # noqa: BLE001
        msg = ("cannot load the bot code from %s (%s: %s): the config files were not validated. Run the copy in "
               "/opt/bitpin-bot/deploy (sudo bitpin-bot check)." % (PROJECT_ROOT, type(e).__name__, e))
        if strict:
            rep.fail("config", msg)
        else:
            rep.warn(msg)
        return {}
    settings = {}
    runner_cfg = None
    for what, path, explicit in paths:
        state = _file_state(path)
        if state != "ok":
            _report_unreadable(rep, what, path, state, explicit, args.config_only)
            continue
        if what == "config.json":
            tmp = tempfile.mkdtemp(prefix="bitpin_check_")
            try:
                runner_cfg = runner_mod.load_config(path)
                risk_mod.RiskManager(runner_cfg.get("risk"), tmp, "live", persist=False)
                rep.ok("%s: valid (rebalance_threshold %s, irt_asset_code %s, irt_unit_divisor %s)"
                       % (path, runner_cfg.get("rebalance_threshold"), runner_cfg.get("irt_asset_code"),
                          runner_cfg.get("irt_unit_divisor")))
            except Exception as e:  # noqa: BLE001 - whatever the bot would die of
                runner_cfg = None
                rep.fail("config", "%s: the bot refuses this file - %s: %s\n(often a missing/extra comma or quote, "
                                   "or a misspelled key; fix it with: sudo nano %s)" % (path, type(e).__name__, e, path))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            continue
        warnings = []
        try:
            problems = brain_mod.check_kimi_config(path, require_model=not args.config_only, runner_cfg=runner_cfg,
                                                   warnings=warnings)
        except TypeError:          # an older bot version without the warnings argument
            problems = brain_mod.check_kimi_config(path, require_model=not args.config_only, runner_cfg=runner_cfg)
        for w in warnings:
            rep.warn("%s: %s" % (path, w))
        for p in problems:
            if p.startswith(MODEL_NOT_SET):
                rep.fail("config", "%s: %s. The live service cannot start until it is set (the model ids your key "
                                   "can use are listed in the Kimi section below)." % (path, p))
            else:
                rep.fail("config", "%s: the bot refuses this file - %s\n(fix it with: sudo nano %s)" % (path, p, path))
        settings = kimi_settings(llm_mod, brain_mod, path)
        settings["required"] = explicit and not args.config_only
        if not problems:
            rep.ok("%s: valid (base_url %s, model %s, risk_profile %s, news %s)" % (
                path, settings.get("base_url"), settings.get("model") or "(not required here)",
                settings.get("risk_profile") or "balanced", settings.get("news_model") or "none"))
    return settings


# --------------------------------------------------------------------------- Kimi / Moonshot

def _kimi_status_text(status):
    if status == 401:
        return ("HTTP 401 - key rejected here. Keys from platform.moonshot.ai work only on api.moonshot.ai, "
                "keys from platform.moonshot.cn only on api.moonshot.cn")
    if status in (403, 451):
        return ("HTTP %d - access denied, most likely a REGION BLOCK of this server's IP: %s"
                % (status, PROXY_HINT))
    if status == 429:
        return "HTTP 429 - rate limited, or the Moonshot account has no balance / quota"
    return "HTTP %s" % status


def _secure_base(url):
    """True only for https URLs with a host (the key is never sent over plain http, like bitpin/llm.py)."""
    u = urllib.parse.urlsplit(url or "")
    return bool(u.hostname) and u.scheme == "https"


def _probe_without_key(rep, args, bases, proxy=None):
    reachable = []
    route = mask_proxy(proxy)
    for b in bases:
        r = request("GET", b + "/models", timeout=args.timeout, proxy=proxy, max_bytes=65536)
        if r.error:
            rep.warn("%s: unreachable (%s) - %s" % (b, route, r.error))
        elif r.status == 401:
            reachable.append(b)
            rep.ok("%s: reachable (%s, %s); HTTP 401 without a key is expected" % (b, route, ms(r.elapsed)))
        else:
            rep.warn("%s (%s): %s" % (b, route, _kimi_status_text(r.status)))
    return reachable


def check_kimi(rep, args, cfg):
    """cfg: kimi settings from check_configs ({} = no kimi.json). The key is sent ONLY to the
    configured base_url (https), plus the other official platform with --kimi-both-regions, over the
    bot's own Kimi route (KIMI_HTTPS_PROXY / llm.proxy, else direct)."""
    proxy, _, perr = kimi_route(cfg)
    if perr:
        rep.fail("kimi", "Kimi proxy setting refused (%s): Kimi not tested" % perr)
        rep.add_summary("Kimi", "FAILED (proxy setting)")
        return
    key_env = str(cfg.get("api_key_env") or "KIMI_API_KEY")
    base = str(cfg.get("base_url") or KIMI_BASES[0]).strip().rstrip("/")
    # v3.1: the key variable must belong to the platform of base_url (KIMI_* / MOONSHOT_* -> Moonshot,
    # OPENROUTER_* -> OpenRouter): otherwise NOTHING is sent - never another variable as a fallback, which
    # would hand the Kimi key to an OpenRouter base_url
    provider = "moonshot"
    try:
        _brain, llm_mod, _risk, _runner = _import_bot()
        provider = llm_mod.detect_provider(base, cfg.get("provider") or "auto")
        llm_mod.check_key_env(key_env, provider, "llm.api_key_env", ValueError)
    except ValueError as e:
        rep.fail("config", "%s. No key was sent." % e)
        rep.add_summary("Kimi", "FAILED (llm.api_key_env)")
        return
    except Exception:  # noqa: BLE001 - the bot code cannot be loaded: the pre-v3.1 Moonshot-only rule
        if not KIMI_KEY_ENV_RE.match(key_env):
            rep.fail("config", "llm.api_key_env %r refused: it must name a KIMI_* or MOONSHOT_* variable. No key "
                               "was sent." % key_env)
            rep.add_summary("Kimi", "FAILED (llm.api_key_env)")
            return
    key = (os.environ.get(key_env) or "").strip()
    if key and key not in _SECRETS:
        _SECRETS.append(key)
    if key_env != "KIMI_API_KEY":
        rep.info("kimi.json reads the key from %s (%s)" % (key_env, "set" if key else "NOT set"))
    secure = _secure_base(base)
    if not secure:
        rep.fail("kimi", "kimi.json base_url %s is not https: the key would travel unencrypted. It was NOT sent. Use "
                         "https://api.moonshot.ai/v1 (or https://api.moonshot.cn/v1)." % base)
    # --kimi-both-regions: the other official Moonshot platform - only for a Moonshot key
    others = [b for b in KIMI_BASES if b != base] if provider == "moonshot" else []

    if not key:
        if cfg.get("required"):
            rep.fail("kimi", "%s is not set: the bot cannot ask Kimi, so every decision would be invalid and it would "
                             "never trade. Put the key in %s (KIMI_API_KEY=...) and run: sudo bitpin-bot check"
                     % (key_env, ENV_FILE_HINT))
        elif not args.kimi_probe:
            rep.skip("%s not set: Kimi check skipped. Put the key in %s and run: sudo bitpin-bot check\n"
                     "(or test reachability without a key: --kimi-probe)" % (key_env, ENV_FILE_HINT))
            rep.add_summary("Kimi", "not tested (no %s)" % key_env)
            return
        if not args.kimi_probe:
            rep.add_summary("Kimi", "FAILED (no %s)" % key_env)
            return
        reachable = _probe_without_key(rep, args, ([base] if secure else []) + others, proxy)
        if reachable:
            rep.add_summary("Kimi (probe, no key)", "reachable: %s" % ", ".join(reachable))
        else:
            rep.fail("kimi", "no Moonshot endpoint is reachable from this server")
            rep.add_summary("Kimi (probe, no key)", "FAILED")
        return

    targets = ([base] if secure else []) + (others if args.kimi_both_regions else [])
    models, statuses = {}, {}
    for b in targets:
        r = request("GET", b + "/models", headers={"Authorization": "Bearer " + key}, timeout=args.timeout,
                    proxy=proxy, max_bytes=2 * 1024 * 1024)
        route = mask_proxy(proxy)
        statuses[b] = r.status
        if r.error:
            rep.warn("%s: unreachable (%s) - %s" % (b, route, r.error))
            continue
        if r.status != 200:
            rep.warn("%s (%s): %s" % (b, route, _kimi_status_text(r.status)))
            continue
        d = r.json()
        rows = d.get("data") if isinstance(d, dict) else None
        ids = sorted(str(m.get("id")) for m in (rows or []) if isinstance(m, dict) and m.get("id"))
        if not ids:
            rep.warn("%s: HTTP 200 but no model list in the reply" % b)
            continue
        models[b] = ids
        rep.ok("%s: key accepted, %d models, %s (%s)" % (b, len(ids), ms(r.elapsed), route))

    want = str(cfg.get("model") or "").strip()
    if not models and proxy is None and any(st is None or st in (403, 451) for st in statuses.values()):
        rep.info(PROXY_HINT)
    for b, ids in models.items():
        print("       model ids offered by %s:" % b)
        for i in ids:
            print("         %s %s" % ("*" if i == want and b == base else "-", i))
    if models:
        rep.info("in /etc/bitpin-bot/kimi.json: \"llm\": {\"base_url\": \"%s\", \"model\": \"kimi-k3\"} (stage 2, the "
                 "decision: JSON mode, no tools, temperature null, max_tokens 32000) and \"news\": {\"model\": "
                 "\"kimi-k2.6\"} (stage 1, web search). Both ids must be in the list above."
                 % (base if base in models else list(models)[0]))
    if not secure:
        rep.add_summary("Kimi", "FAILED (base_url not https)")
        return
    if base not in models:
        hint = ""
        if statuses.get(base) == 401 and not args.kimi_both_regions:
            hint = ("\nIf the key comes from the other platform, test it there with: sudo bitpin-bot check "
                    "--kimi-both-regions")
            _probe_without_key(rep, args, others, proxy)
        elif args.kimi_both_regions and models:
            hint = "\nThe key works on %s: set \"base_url\": \"%s\" in kimi.json." % (list(models)[0], list(models)[0])
        rep.fail("kimi", "the configured base_url %s does not accept the key from this server%s" % (base, hint))
        rep.add_summary("Kimi", "FAILED (%s)" % base)
        return
    summary = "OK (%s)" % base
    if want:
        if want in models[base]:
            rep.ok("configured model %r is available" % want)
        else:
            rep.fail("kimi", "configured model %r is not offered by %s - pick one from the list above" % (want, base))
            summary = "FAILED (model %s not available)" % want
    news_model = str(cfg.get("news_model") or "").strip()
    if news_model:
        if news_model in models[base]:
            rep.ok("news model %r (stage 1) is available; test a real research call with: sudo bitpin-bot kimi-check "
                   "--news" % news_model)
        else:
            rep.warn("news model %r (stage 1) is not offered by %s: the bot would decide WITHOUT news" % (news_model, base))
    rep.add_summary("Kimi", summary)


# --------------------------------------------------------------------------- main

def not_dumpable():
    """prctl(PR_SET_DUMPABLE, 0) on Linux (never fatal), like run_bot.py: 'sudo bitpin-bot check' runs
    this script as user bitpin WITH the service's keys in its environment, and other processes of the
    same user (the network-facing Telegram notifier runs as bitpin too) could otherwise read them from
    /proc/<pid>/environ while it runs. Returns True when set."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0             # PR_SET_DUMPABLE = 4
    except Exception:  # noqa: BLE001 - a hardening step, never a reason to fail the check
        return False


def main(argv=None):
    not_dumpable()
    ap = argparse.ArgumentParser(description="Bitpin bot server pre-flight check (no changes are made).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--auth", action="store_true",
                    help="ONE Bitpin authenticate call with BITPIN_API_KEY/BITPIN_SECRET_KEY (IP whitelist test)")
    ap.add_argument("--config", default=None,
                    help="the bot's config.json to validate (default %s if readable)" % DEFAULT_BOT_CONFIG)
    ap.add_argument("--kimi-config", default=None,
                    help="kimi.json to validate and read base_url/model from (default %s if readable). Given "
                         "explicitly (as 'sudo bitpin-bot check' does), a missing file or KIMI key is a FAIL"
                         % DEFAULT_KIMI_CONFIG)
    ap.add_argument("--config-only", action="store_true",
                    help="only validate the config files offline (no network, llm.model may be unset)")
    ap.add_argument("--kimi-probe", action="store_true",
                    help="without KIMI_API_KEY: test Moonshot reachability anyway (no key is sent)")
    ap.add_argument("--kimi-both-regions", action="store_true",
                    help="also send the key to the other official Moonshot platform (.ai / .cn) to find where it works")
    ap.add_argument("--samples", type=int, default=3, help="requests per Bitpin endpoint for latency (default 3)")
    ap.add_argument("--timeout", type=float, default=15.0, help="seconds per request (default 15)")
    ap.add_argument("--no-ip", action="store_true", help="skip the public IP lookup")
    ap.add_argument("--bitpin-url", default=BITPIN_URL, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.samples < 1 or args.samples > 20 or args.timeout <= 0:
        ap.error("--samples must be 1..20 and --timeout > 0")

    rep = Report()
    print("Bitpin bot - %s  (%s UTC)" % ("config check" if args.config_only else "server check",
                                         time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())))
    kimi_cfg = check_configs(rep, args)
    if not args.config_only:
        check_system(rep)
        check_environment(rep, args, kimi_cfg)
        check_public_ip(rep, args)
        bitpin_ok = check_bitpin(rep, args)
        if bitpin_ok or args.auth:
            check_bitpin_auth(rep, args)
        section("Kimi / Moonshot")
        check_kimi(rep, args, kimi_cfg)

    section("Summary")
    for label, value in rep.summary:
        Report._out("----", "%-20s %s" % (label + ":", value))
    if "config" in rep.failed:
        Report._out("HINT", "A config file is not usable: the bot would refuse to start (exit 78, the service stays "
                            "stopped). Fix the [FAIL] lines of the 'Configuration files' section, then run this check again.")
    if "bitpin" in rep.failed:
        Report._out("HINT", "Bitpin is not usable from here (the bot always connects to Bitpin directly). Bitpin very "
                            "likely requires an Iranian IP.")
    if "kimi" in rep.failed:
        Report._out("HINT", "Kimi check failed: read its [FAIL] / [WARN] lines. If Moonshot is blocked or unreachable "
                            "from this server (HTTP 403/451, timeout): %s. Without Kimi the bot only moves toman "
                            "into USDT_IRT (and after 12 h of failures sells its coins into USDT_IRT)." % PROXY_HINT)
    if rep.failed:
        print("RESULT: FAILED (%s), %d warning(s)" % (", ".join(rep.failed), rep.warnings))
        return 1
    print("RESULT: OK, %d warning(s)" % rep.warnings)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
