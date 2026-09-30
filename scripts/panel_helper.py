#!/usr/bin/env python3
"""The management panel's ROOT helper (release v3.1).

The web panel (scripts/panel_server.py) runs as the unprivileged user bitpin-panel and never sees an API
key. Everything privileged goes through this helper, one JSON request per connection on a UNIX socket
that only root and the group bitpin-panel can open (deploy/bitpin-bot-panel-helper.socket: socket
activation, Accept=yes - systemd starts one short-lived instance of this script per connection with the
connection on stdin / stdout):

    request   {"cmd": NAME, "args": {...}, "actor": {"user": ..., "ip": ...}}      one line, at most 4 MB
    response  {"ok": true, "data": {...}}  or  {"ok": false, "error": "text"}      one line

Only the commands in COMMANDS exist (a closed allowlist); every argument is checked here again (the web
side is not trusted). What they may touch:

* /etc/bitpin-bot/config.json and kimi.json - validated with the bot's own checks
  (deploy/apply_profile.validate) BEFORE anything is written, the old file copied to
  /var/backups/bitpin-bot first; the trade settings form may change only the keys of
  bitpin/panel_settings.FIELDS;
* /etc/bitpin-bot/bitpin-bot.env - only the names in SECRET_NAMES, write-only: a value is never
  returned, logged or diffed (secrets_status says "set, N characters");
* /etc/bitpin-bot-panel/panel.json - only password_hash and totp_secret;
* /opt/xray/config.json - the owner's own VPN client: tested with the xray binary before it is
  written, the tunnel restarted, the proxy tested end to end, and the old file put back automatically
  when the new one does not work;
* systemctl start / stop / restart of SERVICE_UNITS only, journalctl of LOG_UNITS only;
* confirm-live: through the bitpin-bot command line (the same guards as by hand), with the phrase the
  owner typed in the panel.

Every command is logged to the journal (its name and result, never an argument value). Changes and
panel logins are relayed to the Telegram notifier as small JSON files in its state directory
(panel_event.*.json, like deploy/bitpin-bot-failed's unit_failure.json).

    python3 panel_helper.py                  serve one request on stdin / stdout (systemd)
    python3 panel_helper.py models-worker    (internal) list a provider's models, run as user bitpin
    python3 panel_helper.py state-worker     (internal) the dashboard's view of the bot's files, as user bitpin
"""
import sys

if __name__ == "__main__" and sys.platform.startswith("linux"):
    # the helper briefly holds API keys when it edits bitpin-bot.env: not dumpable, like run_bot.py
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)      # PR_SET_DUMPABLE = 4
    except Exception:  # noqa: BLE001 - never fatal
        pass

import base64  # noqa: E402
import binascii  # noqa: E402
import copy  # noqa: E402
import difflib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import stat  # noqa: E402
import struct  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
from collections import OrderedDict  # noqa: E402

try:
    import fcntl
except ImportError:          # Windows (the tests): no lock
    fcntl = None
try:
    import pwd
except ImportError:
    pwd = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

log = logging.getLogger("bitpin.panel_helper")

MAX_REQUEST = 4 * 1024 * 1024
MAX_OUTPUT = 200 * 1024
PERF_MAX_OUTPUT = 3 * 1024 * 1024       # v3.6: the performance report (with its trade history) is one JSON line
LIVE_PHRASE = "I ACCEPT THE RISK"          # scripts/run_bot.py LIVE_PHRASE (a test keeps them equal)
SECRET_NAMES = ("KIMI_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY", "KIMI_HTTPS_PROXY", "BITPIN_API_KEY",
                "BITPIN_SECRET_KEY")
LLM_KEY_NAMES = ("KIMI_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY")
SERVICE_UNITS = ("bitpin-bot", "bitpin-bot-notify", "xray-tunnel")
SERVICE_ACTIONS = ("restart", "start", "stop")
LOG_UNITS = ("bitpin-bot", "bitpin-bot-notify", "xray-tunnel", "bitpin-bot-panel")
STATUS_UNITS = (("bot", "bitpin-bot"), ("notifier", "bitpin-bot-notify"), ("tunnel", "xray-tunnel"),
                ("panel", "bitpin-bot-panel"))
PROVIDERS = ("auto", "moonshot", "openrouter", "openai")
OPENROUTER_HOSTS = ("openrouter.ai",)
BITPIN_API_URLS = ("https://api.bitpin.org",)     # config.json base_url: the Bitpin keys go there
LOOPBACK_LISTEN = ("127.0.0.1", "::1", "localhost")
EFFORTS = (None, "low", "medium", "high", "max")
PANEL_EVENTS = ("login_ok", "login_locked")
EVENT_PREFIX = "panel_event."
EMPTY_STATE = {"equity": None, "last_decision": None, "positions": [], "resting_orders": None, "spend": None,
               "status_fa": None, "last_decision_fa": None, "state_error": None}
MAX_PENDING_EVENTS = 20
KEEP_BACKUPS = 30
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_VALUE_RE = re.compile(r"^[\x21-\x7e]{1,4096}$")
_PHASH_RE = re.compile(r"^pbkdf2_sha256\$(\d{5,8})\$[A-Za-z0-9+/=_-]{16,120}\$[A-Za-z0-9+/=_-]{32,120}$")
_TOTP_RE = re.compile(r"^[A-Z2-7]{16,64}=*$")
_USER_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")
_IP_RE = re.compile(r"^[0-9A-Fa-f:.]{2,45}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,99}$")


class HelperError(Exception):
    """A refusal: the message is shown to the owner (never a secret in it)."""


class Paths(object):
    """Where everything lives on the server (the tests point these at a temporary directory)."""

    def __init__(self, **over):
        self.app_dir = "/opt/bitpin-bot"
        self.etc_dir = "/etc/bitpin-bot"
        self.panel_etc_dir = "/etc/bitpin-bot-panel"     # root:bitpin-panel 0750: the web process reads it
        self.state_dir = "/var/lib/bitpin-bot"
        self.notify_state_dir = "/var/lib/bitpin-bot-notify"
        self.backup_dir = "/var/backups/bitpin-bot"
        self.xray_cmd = ["/opt/xray/xray"]      # a list: the tests run a fake xray with python
        self.xray_conf = "/opt/xray/config.json"
        self.cli = "/usr/local/bin/bitpin-bot"
        self.python = "/usr/bin/python3"
        self.bot_user = "bitpin"
        self.panel_user = "bitpin-panel"
        self.lock_file = "/run/bitpin-panel/helper.lock"
        self.default_proxy = "http://127.0.0.1:1081"
        self.state_in_process = False        # True only in the tests: production reads as the user bitpin
        for k, v in over.items():
            if not hasattr(self, k):
                raise TypeError("unknown path %r" % k)
            setattr(self, k, v)

    @property
    def config(self):
        return os.path.join(self.etc_dir, "config.json")

    @property
    def kimi(self):
        return os.path.join(self.etc_dir, "kimi.json")

    @property
    def env_file(self):
        return os.path.join(self.etc_dir, "bitpin-bot.env")

    @property
    def panel_conf(self):
        return os.path.join(self.panel_etc_dir, "panel.json")

    @property
    def notify_conf(self):
        return os.path.join(self.etc_dir, "notify.json")


# --------------------------------------------------------------------------- small tools
def run_command(argv, timeout, env=None, cwd=None, limit=MAX_OUTPUT):
    """(exit status, the last `limit` characters of the combined output) of a command; never raises (a timeout
    is status 124)."""
    base = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
            "TZ": "UTC", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0"}
    if env:
        base.update(env)
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           timeout=timeout, env=base, cwd=cwd)
        return p.returncode, cap(p.stdout.decode("utf-8", "replace"), limit)
    except subprocess.TimeoutExpired as e:
        out = (e.output or b"").decode("utf-8", "replace") if isinstance(e.output, bytes) else ""
        return 124, cap(out + "\n(timed out after %d s)" % timeout, limit)
    except OSError as e:
        return 127, "cannot run %s: %s" % (os.path.basename(argv[0]), e.strerror or e)


def cap(text, limit=MAX_OUTPUT):
    text = text or ""
    return text if len(text) <= limit else "...(%d characters cut)...\n" % (len(text) - limit) + text[-limit:]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _no_dup(pairs):
    d = OrderedDict()
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key %r" % (k,))
        d[k] = v
    return d


def parse_json_object(text, name):
    try:
        doc = json.loads(text, object_pairs_hook=_no_dup)
    except ValueError as e:
        raise HelperError("%s is not valid JSON: %s" % (name, e))
    if not isinstance(doc, dict):
        raise HelperError("%s must contain a JSON object {...}" % name)
    return doc


def read_text(path, limit=2 * 1024 * 1024):
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read(limit + 1)
    if len(text) > limit:
        raise HelperError("%s is larger than %d bytes" % (path, limit))
    return text


def atomic_write(path, text, mode=0o640, uid=None, gid=None):
    """Write text via a temporary file in the same directory; an existing file keeps its mode and owner."""
    d = os.path.dirname(os.path.abspath(path))
    try:
        st = os.stat(path)
    except FileNotFoundError:
        st = None
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(st.st_mode) if st is not None else mode)
        if hasattr(os, "chown"):
            if st is not None:
                os.chown(tmp, st.st_uid, st.st_gid)
            elif uid is not None or gid is not None:
                os.chown(tmp, -1 if uid is None else uid, -1 if gid is None else gid)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def unified_diff(old, new, name):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/" + name, "b/" + name, n=2))


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def code_version(app_dir):
    try:
        with open(os.path.join(app_dir, "bitpin", "__init__.py"), "r", encoding="utf-8") as f:
            m = re.search(r'^__version__ = "([^"]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def url_host(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def check_base_url(url):
    """A plain https URL (no user:password@, query or fragment): the API key is sent to this host."""
    if not isinstance(url, str) or len(url) > 200:
        raise HelperError("base_url must be an https URL")
    url = url.strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise HelperError("base_url is not a valid URL")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query \
            or parts.fragment or "@" in parts.netloc or port == 0:
        raise HelperError("base_url must be a plain https URL like https://openrouter.ai/api/v1")
    return url


def provider_for(url, provider="auto"):
    from bitpin import llm as llm_mod
    host = url_host(url)
    moonshot = tuple(getattr(llm_mod, "MOONSHOT_HOSTS", ("api.moonshot.ai", "api.moonshot.cn")))
    if provider not in PROVIDERS:
        raise HelperError("provider must be one of %s" % ", ".join(PROVIDERS))
    detected = "moonshot" if host in moonshot else ("openrouter" if host in OPENROUTER_HOSTS else "openai")
    if provider != "auto" and provider != detected:
        raise HelperError("provider %s does not match the host %s (%s)" % (provider, host, detected))
    return detected


def key_for_provider(provider):
    """The API key variable for each provider: a key is only ever sent to its own platform."""
    return {"moonshot": "KIMI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(provider, "LLM_API_KEY")


def endpoint_problems(cfg_doc, kimi_doc, llm_host=None):
    """Where the settings would send a key - checked on EVERY settings write (the JSON editor too), so a
    stolen panel session cannot point the bot at another server and collect the keys there: config.json
    base_url must be the Bitpin API (the Bitpin key pair goes there), and each LLM stage's base_url must be
    the platform of its key (KIMI_API_KEY -> Moonshot, OPENROUTER_API_KEY -> openrouter.ai, LLM_API_KEY -> the
    host saved with that key, LLM_API_HOST)."""
    problems = []
    url = (cfg_doc or {}).get("base_url", BITPIN_API_URLS[0])
    if not isinstance(url, str) or url.strip().rstrip("/") not in BITPIN_API_URLS:
        problems.append("config.json base_url must be %s (the Bitpin keys are sent there); another value is only "
                        "possible by editing the file on the server" % " or ".join(BITPIN_API_URLS))
    llm = (kimi_doc or {}).get("llm") if isinstance((kimi_doc or {}).get("llm"), dict) else {}
    news = (kimi_doc or {}).get("news")
    stages = [("llm", llm, llm.get("base_url", "https://api.moonshot.ai/v1"), llm.get("api_key_env", "KIMI_API_KEY"))]
    if isinstance(news, dict) and news.get("enabled", True) is not False:
        try:                               # the news stage as the bot resolves it (base_url / key inherited from llm)
            from bitpin.news import news_section
            ns = news_section(kimi_doc)
        except Exception:  # noqa: BLE001 - an older tree: no inheritance of the key
            ns = dict(news)
        stages.append(("news", ns, ns.get("base_url") or stages[0][2], ns.get("api_key_env") or "KIMI_API_KEY"))
    for name, sec, base, key in stages:
        try:
            base = check_base_url(base)
            provider = provider_for(base, sec.get("provider") or "auto")
        except HelperError as e:
            problems.append("kimi.json %s: %s" % (name, e))
            continue
        want = key_for_provider(provider)
        if key != want:
            problems.append("kimi.json %s.api_key_env must be %s for %s (a key is only sent to its own platform)"
                            % (name, want, url_host(base)))
        elif want == "LLM_API_KEY" and url_host(base) != (llm_host or ""):
            problems.append("kimi.json %s.base_url: LLM_API_KEY belongs to %s, not %s (save the key again with this "
                            "host)" % (name, llm_host or "no host yet", url_host(base)))
    return problems


def inbound_problems(old_doc, new_doc):
    """xray inbounds that are new or changed must listen on the loopback only: the tunnel is the local
    proxy of the bot, and a public inbound would open the server (and its localhost-only services) to the
    internet."""
    old = [json.dumps(i, sort_keys=True) for i in ((old_doc or {}).get("inbounds") or []) if isinstance(i, dict)]
    problems = []
    for i in new_doc.get("inbounds") or []:
        if not isinstance(i, dict):
            problems.append("an inbound is not an object")
            continue
        if json.dumps(i, sort_keys=True) in old:
            continue
        if str(i.get("listen") or "0.0.0.0") not in LOOPBACK_LISTEN:
            problems.append("inbound %s on port %s must listen on 127.0.0.1 (listen: %s)" % (
                str(i.get("tag") or i.get("protocol") or "?")[:30], str(i.get("port"))[:10],
                str(i.get("listen") or "all addresses")[:40]))
    return problems


def parse_env_lines(text):
    """[(name or None, raw line)] of a systemd EnvironmentFile (comments and blanks kept)."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        name = None
        if s and not s.startswith(("#", ";")) and "=" in s:
            k = s.split("=", 1)[0].strip()
            if _ENV_NAME_RE.match(k):
                name = k
        out.append((name, line))
    return out


def env_value(raw_line):
    v = raw_line.split("=", 1)[1].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def read_env(path):
    """{NAME: value} of an env file (missing file: {})."""
    try:
        text = read_text(path, 256 * 1024)
    except FileNotFoundError:
        return {}
    env = {}
    for name, line in parse_env_lines(text):
        if name:
            env[name] = env_value(line)
    return env


# --------------------------------------------------------------------------- the helper
class Helper(object):
    def __init__(self, paths=None, run=None, clock=time.time, sleep=time.sleep, actor=None):
        self.p = paths or Paths()
        self.run = run or run_command
        self.clock = clock
        self.sleep = sleep
        self.actor = actor or {}
        self._ap = None
        self._rb = None
        self.fetch_bars = None          # v3.6: the candles of the performance report (the tests set a fake)

    # ---- plumbing
    def handle(self, request):
        """The response dict for one request dict (never raises)."""
        cmd = request.get("cmd") if isinstance(request, dict) else None
        spec = COMMANDS.get(cmd) if isinstance(cmd, str) else None
        if spec is None:
            log.warning("refused an unknown command %r", str(cmd)[:40])
            return {"ok": False, "error": "unknown command"}
        args = request.get("args", {})
        if not isinstance(args, dict):
            return {"ok": False, "error": "args must be an object"}
        self.actor = clean_actor(request.get("actor"))
        mutating, fn = spec
        lock = None
        try:
            if mutating:
                lock = self._lock()
            data = fn(self, args)
            log.info("%s: ok", cmd)
            return {"ok": True, "data": data}
        except HelperError as e:
            log.info("%s: refused (%s)", cmd, str(e)[:300])
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - never a stack trace to the web side
            log.exception("%s: failed", cmd)
            return {"ok": False, "error": "internal error (%s) - see: sudo journalctl -u 'bitpin-bot-panel-helper@*'"
                                          % type(e).__name__}
        finally:
            if lock is not None:
                lock.close()

    def _lock(self):
        """One change at a time (each connection is its own process)."""
        if fcntl is None:
            return None
        f = open(self.p.lock_file, "a")
        deadline = self.clock() + 120
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except OSError:
                if self.clock() > deadline:
                    f.close()
                    raise HelperError("another change is still running - try again in a minute")
                self.sleep(0.5)

    @property
    def ap(self):
        if self._ap is None:
            self._ap = load_module("bitpin_panel_apply_profile", os.path.join(self.p.app_dir, "deploy", "apply_profile.py"))
        return self._ap

    @property
    def rb(self):
        if self._rb is None:
            self._rb = load_module("bitpin_panel_run_bot", os.path.join(self.p.app_dir, "scripts", "run_bot.py"))
        return self._rb

    def cli(self, args, timeout):
        return self.run([self.p.cli] + list(args), timeout)

    def unit_info(self, unit):
        rc, out = self.run(["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "UnitFileState",
                            "-p", "StateChangeTimestamp", "-p", "LoadState"], 20)
        kv = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        state = kv.get("ActiveState") or "unknown"
        if kv.get("LoadState") == "not-found":
            state = "not-installed"
        return {"state": state, "sub": kv.get("SubState") or None, "enabled": kv.get("UnitFileState") or None,
                "since": kv.get("StateChangeTimestamp") or None}

    def unit_state(self, unit):
        return self.unit_info(unit)["state"]

    def live_check(self):
        rc, out = self.cli(["confirm-live", "--check"], 90)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return {"ok": rc == 0, "why": lines[-1][:400] if lines else "exit %d" % rc}

    def backup(self, path, name):
        """Copy path to the backup directory (0600 root) and keep the newest KEEP_BACKUPS of that name."""
        if not os.path.exists(path):
            return None
        os.makedirs(self.p.backup_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        dst = os.path.join(self.p.backup_dir, "%s.%s.before-panel" % (name, stamp))
        n = 1
        while os.path.exists(dst):
            n += 1
            dst = os.path.join(self.p.backup_dir, "%s.%s-%d.before-panel" % (name, stamp, n))
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with open(path, "rb") as src, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(src, out)
        olds = sorted(f for f in os.listdir(self.p.backup_dir) if f.startswith(name + ".") and f.endswith(".before-panel"))
        for f in olds[:-KEEP_BACKUPS]:
            try:
                os.unlink(os.path.join(self.p.backup_dir, f))
            except OSError:
                pass
        return dst

    def relay(self, event, what="", arg=""):
        """A small JSON file for the Telegram notifier (bitpin/notify.py _scan_panel_events); never fails.
        The notifier's directory belongs to the user bitpin, so everything here works on a file descriptor
        the helper created itself (O_EXCL | O_NOFOLLOW): no chmod / chown by path that a planted symlink
        could redirect to a system file. The file stays root's, mode 0644 (the directory is 0700 bitpin:
        the notifier reads it and, owning the directory, deletes it); a rename never follows a symlink."""
        d = self.p.notify_state_dir
        try:
            if not os.path.isdir(d) or os.path.islink(d):
                return False
            pending = [f for f in os.listdir(d) if f.startswith(EVENT_PREFIX)]
            if len(pending) >= MAX_PENDING_EVENTS:
                log.warning("panel event %s not relayed: %d events are still pending", event, len(pending))
                return False
            rec = {"event": event, "what": str(what)[:40], "arg": str(arg)[:80], "user": self.actor.get("user") or "",
                   "ip": self.actor.get("ip") or "", "time": self.clock()}
            name = "%s%d.%s.json" % (EVENT_PREFIX, int(self.clock() * 1000), secrets.token_hex(4))
            tmp = os.path.join(d, ".%s.tmp" % name)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_BINARY", 0), 0o644)
            try:
                os.write(fd, (json.dumps(rec) + "\n").encode("utf-8"))
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o644)            # whatever the umask: the notifier (user bitpin) reads it
            finally:
                os.close(fd)
            os.replace(tmp, os.path.join(d, name))
            return True
        except Exception as e:  # noqa: BLE001 - an alert is never worth a failed command
            log.warning("panel event %s not relayed: %s", event, e)
            return False

    # ---- read-only
    def cmd_status(self, a):
        data = OrderedDict()
        for key, unit in STATUS_UNITS:
            data[key] = self.unit_info(unit)
        data["bot"]["version"] = code_version(self.p.app_dir)
        data["live_confirmed"] = self.live_check()
        data.update(self.bot_state())
        return data

    def bot_state(self):
        """The dashboard's view of the bot's own files, read by a worker running as the user bitpin: the bot's
        state directory belongs to that user, so root never opens a path there (a planted symlink could
        otherwise make root read - or block on - a file of its choice). The tests read in-process."""
        if self.p.state_in_process:
            return collect_bot_state(self.p.state_dir, self.p.notify_state_dir, self.p.notify_conf, self.clock())
        argv = ["runuser", "-u", self.p.bot_user, "--", self.p.python, os.path.join(self.p.app_dir, "scripts",
                "panel_helper.py"), "state-worker", "--state-dir", self.p.state_dir, "--notify-state-dir",
                self.p.notify_state_dir, "--notify-conf", self.p.notify_conf]
        rc, out = self.run(argv, 60, cwd=self.p.app_dir)
        last = [ln for ln in out.splitlines() if ln.startswith("{")]
        try:
            data = json.loads(last[-1])
            if isinstance(data, dict):
                return data
        except (IndexError, ValueError):
            pass
        return dict(EMPTY_STATE, state_error="the state worker failed (exit %d): %s" % (rc, out.strip()[-200:]))

    def cmd_performance(self, a):
        """v3.6: the P&L report, trade history and chart series of (from, to] (bitpin.performance), read by a
        worker running as the user bitpin (like bot_state), with PUBLIC Bitpin candles for the prices."""
        from bitpin import performance as perf
        now = self.clock()
        t_from, t_to = a.get("from"), a.get("to")
        for v in (t_from, t_to):
            if isinstance(v, bool) or not isinstance(v, int):
                raise HelperError("from and to must be whole epoch seconds")
        if not 1500000000 <= t_from < t_to <= now + 3600:
            raise HelperError("the range must start before it ends and end by now")
        if t_to - t_from > perf.MAX_RANGE_DAYS * 86400:
            raise HelperError("the range may be at most %d days" % perf.MAX_RANGE_DAYS)
        if self.p.state_in_process:
            return perf.collect(self.p.state_dir, t_from, t_to, now, fetch=self.fetch_bars)
        argv = ["runuser", "-u", self.p.bot_user, "--", self.p.python, os.path.join(self.p.app_dir, "scripts",
                "panel_helper.py"), "perf-worker", "--state-dir", self.p.state_dir, "--from", str(t_from),
                "--to", str(t_to)]
        rc, out = self.run(argv, 110, cwd=self.p.app_dir, limit=PERF_MAX_OUTPUT)
        last = [ln for ln in out.splitlines() if ln.startswith("{")]
        try:
            data = json.loads(last[-1])
        except (IndexError, ValueError):
            data = None
        if not isinstance(data, dict):
            raise HelperError("the performance report failed (exit %d): %s" % (rc, out.strip()[-200:]))
        return data

    def cmd_config_get(self, a):
        return {"config": read_text(self.p.config), "kimi": read_text(self.p.kimi)}

    def cmd_secrets_status(self, a):
        env = read_env(self.p.env_file)
        return OrderedDict((n, {"set": bool(env.get(n)), "length": len(env.get(n) or "")}) for n in SECRET_NAMES)

    def cmd_health(self, a):
        rc, out = self.cli(["health"], 90)
        return {"ok": rc == 0, "text": out}

    def cmd_check(self, a):
        rc, out = self.cli(["check"], 175)
        return {"ok": rc == 0, "text": out}

    def cmd_logs(self, a):
        unit = a.get("unit")
        if unit not in LOG_UNITS:
            raise HelperError("unit must be one of %s" % ", ".join(LOG_UNITS))
        lines = a.get("lines", 100)
        if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 500:
            raise HelperError("lines must be 1..500")
        rc, out = self.run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"], 30)
        return {"text": out}

    def cmd_confirm_show(self, a):
        rc, out = self.cli(["confirm-live", "--show"], 90)
        return {"ok": rc == 0, "text": out}

    # ---- the bot's settings
    def _validate_pair(self, kimi_text, config_text):
        problems, warnings = self.ap.validate(kimi_text, config_text, self.p.app_dir)
        return list(problems), list(warnings)

    def _write_settings(self, new_texts, dry_run, what):
        """new_texts {"config": text, "kimi": text} (only changed files). Validates the resulting pair,
        writes (after a backup) unless dry_run or a problem, then says whether a new confirm-live is needed."""
        cur = {"config": read_text(self.p.config), "kimi": read_text(self.p.kimi)}
        after = dict(cur)
        after.update(new_texts)
        problems, warnings = self._validate_pair(after["kimi"], after["config"])
        try:
            problems += endpoint_problems(json.loads(after["config"]), json.loads(after["kimi"]),
                                          read_env(self.p.env_file).get("LLM_API_HOST"))
        except ValueError:
            pass                                   # not JSON: the validation above already says so
        diff = "".join(unified_diff(cur[k], after[k], k + ".json") for k in ("config", "kimi") if cur[k] != after[k])
        res = {"problems": problems, "warnings": warnings, "diff": diff, "written": False, "backup": None,
               "confirm_needed": False}
        changed = [k for k in ("config", "kimi") if cur[k] != after[k]]
        if dry_run or problems or not changed:
            return res
        backups = []
        for k in changed:
            path = self.p.config if k == "config" else self.p.kimi
            backups.append(self.backup(path, k + ".json"))
            atomic_write(path, after[k])
        res.update(written=True, backup=", ".join(b for b in backups if b) or None)
        res["confirm_needed"] = not self.live_check()["ok"]
        self.relay("change", what, ",".join(changed))
        return res

    def cmd_config_put(self, a):
        f = a.get("file")
        if f not in ("config", "kimi"):
            raise HelperError('file must be "config" or "kimi"')
        text = a.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 1024 * 1024:
            raise HelperError("text must be the whole JSON file (at most 1 MB)")
        text = text.replace("\r\n", "\n")
        if not text.endswith("\n"):
            text += "\n"
        parse_json_object(text, f + ".json")
        return self._write_settings({f: text}, bool(a.get("dry_run")), "settings")

    def cmd_settings_set(self, a):
        from bitpin import panel_settings as ps
        changes = a.get("changes")
        cfg_doc = parse_json_object(read_text(self.p.config), "config.json")
        kimi_doc = parse_json_object(read_text(self.p.kimi), "kimi.json")
        new_cfg, new_kimi, applied, errors = ps.apply_changes(cfg_doc, kimi_doc, changes)
        if errors:
            return {"problems": errors, "warnings": [], "diff": "", "written": False, "backup": None,
                    "confirm_needed": False, "changed": []}
        texts = {}
        if any(x["file"] == "config" for x in applied):
            texts["config"] = self.ap.dumps(new_cfg)
        if any(x["file"] == "kimi" for x in applied):
            texts["kimi"] = self.ap.dumps(new_kimi)
        res = self._write_settings(texts, bool(a.get("dry_run")), "trade_settings")
        res["changed"] = applied
        return res

    def cmd_model_set(self, a):
        from bitpin import llm as llm_mod
        from bitpin import news as news_mod
        stage = a.get("stage")
        if stage not in ("llm", "news"):
            raise HelperError('stage must be "llm" (decisions) or "news"')
        base_url = check_base_url(a.get("base_url"))
        provider = provider_for(base_url, a.get("provider") or "auto")
        key_name = a.get("key_name") or key_for_provider(provider)
        self._check_key_host(provider, key_name, base_url)
        model = a.get("model")
        if not isinstance(model, str) or not _MODEL_RE.match(model.strip()):
            raise HelperError("model must be a model id (letters, digits, . _ - : /)")
        model = model.strip()
        effort = a.get("reasoning_effort")
        if effort not in EFFORTS:
            raise HelperError("reasoning_effort must be null, low, medium, high or max")
        prices = a.get("prices")
        if prices is not None:
            if not isinstance(prices, dict):
                raise HelperError("prices must be an object")
            for k, v in prices.items():
                if k not in ("price_in_per_m", "price_out_per_m", "price_cached_in_per_m"):
                    raise HelperError("unknown price key %s" % str(k)[:40])
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1000):
                    raise HelperError("%s must be a price in USD per million tokens (0..1000)" % k)
        text = read_text(self.p.kimi)
        doc = parse_json_object(text, "kimi.json")
        sec = doc.get(stage)
        if not isinstance(sec, dict):
            raise HelperError("kimi.json has no %s section (switched off?) - edit it in the JSON editor first" % stage)
        defaults = llm_mod.DEFAULT_LLM_CONFIG if stage == "llm" else news_mod.DEFAULT_NEWS_CONFIG
        changes, notes = [], []
        rb = self.rb
        old_model = sec.get("model")
        rb._set_key(sec, "base_url", base_url, stage, changes)
        if "provider" in defaults:
            rb._set_key(sec, "provider", provider, stage, changes)
        if "api_key_env" in defaults:
            rb._set_key(sec, "api_key_env", key_name, stage, changes)
        elif key_name != "KIMI_API_KEY":
            raise HelperError("this version of the bot reads the %s key only from KIMI_API_KEY" % stage)
        rb._set_key(sec, "model", model, stage, changes)
        base = model.split("/")[-1]
        thinking = bool(rb.known_thinking(base))
        if thinking:
            rb._set_key(sec, "temperature", None, stage, changes)
            try:
                cur = int(sec.get("max_tokens") or 0)
            except (TypeError, ValueError):
                cur = 0
            floor = rb.THINKING_DECISION_MAX_TOKENS if stage == "llm" else rb.THINKING_NEWS_MAX_TOKENS
            rb._set_key(sec, "max_tokens", max(cur, floor), stage, changes)
        else:
            rb._non_thinking(sec, stage, str(old_model or "").split("/")[-1], base, changes, notes)
        if stage == "llm" and (effort is not None or "reasoning_effort" in sec):
            rb._set_key(sec, "reasoning_effort", effort, stage, changes)
        for k, v in (prices or {}).items():
            if v is not None:
                rb._set_key(sec, k, float(v), stage, changes)
        res = self._write_settings({"kimi": self.ap.dumps(doc)}, bool(a.get("dry_run")), "model")
        res["changed"] = [{"file": "kimi", "path": c[0], "old": None if c[1] == "(missing)" else json.loads(c[1]),
                           "new": json.loads(c[2])} for c in changes]
        res["warnings"] = notes + res["warnings"]
        return res

    def _check_key_host(self, provider, key_name, base_url):
        if key_name != key_for_provider(provider):
            raise HelperError("the %s key is only sent to %s: use %s" % (provider, url_host(base_url),
                                                                       key_for_provider(provider)))
        if key_name == "LLM_API_KEY":
            host = read_env(self.p.env_file).get("LLM_API_HOST")
            if url_host(base_url) != (host or ""):
                raise HelperError("LLM_API_KEY belongs to %s, not %s: save the key again together with this host"
                                  % (host or "no host yet", url_host(base_url)))

    def cmd_models(self, a):
        base_url = check_base_url(a.get("base_url"))
        provider = provider_for(base_url, a.get("provider") or "auto")
        key_name = a.get("key_name") or key_for_provider(provider)
        self._check_key_host(provider, key_name, base_url)
        env = read_env(self.p.env_file)
        if not env.get(key_name):
            raise HelperError("%s is not set: save the key first" % key_name)
        wenv = {key_name: env[key_name]}
        if env.get("KIMI_HTTPS_PROXY"):
            wenv["KIMI_HTTPS_PROXY"] = env["KIMI_HTTPS_PROXY"]
        argv = ["runuser", "-u", self.p.bot_user, "--", self.p.python, os.path.join(self.p.app_dir, "scripts",
                "panel_helper.py"), "models-worker", "--base-url", base_url, "--key-env", key_name, "--provider",
                provider]
        rc, out = self.run(argv, 150, env=wenv, cwd=self.p.app_dir)
        last = [ln for ln in out.splitlines() if ln.startswith("{")]
        try:
            res = json.loads(last[-1])
        except (IndexError, ValueError):
            raise HelperError("the model list could not be read (exit %d): %s" % (rc, out.strip()[-300:]))
        if not res.get("ok"):
            raise HelperError("the model list could not be read: %s" % str(res.get("error"))[:400])
        return {"models": res.get("models") or [], "provider": provider, "key_name": key_name}

    # ---- secrets (write-only)
    def cmd_secret_set(self, a):
        name, value = a.get("name"), a.get("value")
        if name not in SECRET_NAMES:
            raise HelperError("name must be one of %s" % ", ".join(SECRET_NAMES))
        if not isinstance(value, str):
            raise HelperError("value must be a text")
        value = value.strip()
        if value and (not _SECRET_VALUE_RE.match(value) or any(c in value for c in "\"'`\\$")):
            raise HelperError("the value may contain only printable characters without spaces, quotes, $ or \\")
        host = None
        if name == "LLM_API_KEY" and value:
            # a generic key is bound to ONE host, saved next to it (LLM_API_HOST): it is never sent anywhere else
            try:
                host = url_host(check_base_url(a.get("base_url")))
            except HelperError:
                raise HelperError("LLM_API_KEY needs the https base_url of the service it belongs to")
            if host in OPENROUTER_HOSTS or provider_for("https://%s/" % host) == "moonshot":
                raise HelperError("use OPENROUTER_API_KEY / KIMI_API_KEY for %s" % host)
        if name == "KIMI_HTTPS_PROXY" and value:
            parts = urllib.parse.urlsplit(value)
            try:
                ok = parts.scheme in ("http", "https", "socks5", "socks5h") and parts.hostname and parts.port
            except ValueError:
                ok = False
            if not ok:
                raise HelperError("KIMI_HTTPS_PROXY must be a proxy URL like http://127.0.0.1:1081")
        path = self.p.env_file
        try:
            text = read_text(path, 256 * 1024)
        except FileNotFoundError:
            raise HelperError("%s does not exist (install the bot first)" % path)
        pairs = [(name, value)]
        if name == "LLM_API_KEY":
            pairs.append(("LLM_API_HOST", host or ""))
        lines = [line for n, line in parse_env_lines(text) if n not in [p[0] for p in pairs]]
        lines += ["%s=%s" % (n, v) for n, v in pairs if v]
        new = "\n".join(lines).rstrip("\n") + "\n"
        if new != text:
            self.backup(path, "bitpin-bot.env")
            atomic_write(path, new, mode=0o640)
            self.relay("change", "secret", name)
        return {"name": name, "set": bool(value), "length": len(value), "restart_needed": True}

    # ---- confirm-live and the services
    def cmd_apply_live(self, a):
        phrase = a.get("phrase")
        if not isinstance(phrase, str) or phrase.strip() != LIVE_PHRASE:
            raise HelperError('type exactly: %s' % LIVE_PHRASE)
        start = a.get("start", True) is not False
        steps = []

        def step(name, rc, out, ok=None):
            steps.append({"step": name, "ok": rc == 0 if ok is None else ok, "output": cap(out or "", 20000)})
            return steps[-1]["ok"]

        rc, out = self.cli(["confirm-live", "--show"], 90)
        if not step("check", rc, out):
            return {"ok": False, "steps": steps, "health": "", "bot_state": self.unit_state("bitpin-bot")}
        before = self.unit_state("bitpin-bot")
        rc, out = self.run(["systemctl", "stop", "bitpin-bot"], 150)
        if not step("stop", rc, out or "bitpin-bot stopped"):
            return {"ok": False, "steps": steps, "health": "", "bot_state": self.unit_state("bitpin-bot")}
        rc, out = self.cli(["confirm-live", "--typed-phrase", LIVE_PHRASE], 90)
        confirmed = step("confirm", rc, out)
        if confirmed or (before in ("active", "activating") and self.live_check()["ok"]):
            if start:
                self.run(["systemctl", "reset-failed", "bitpin-bot"], 30)
                rc, out = self.run(["systemctl", "start", "bitpin-bot"], 90)
                step("start", rc, out or "bitpin-bot started")
                for _ in range(20):
                    if self.unit_state("bitpin-bot") != "activating":
                        break
                    self.sleep(1)
        rc, health = self.cli(["health"], 90)
        self.relay("change", "apply_live", "ok" if confirmed else "failed")
        return {"ok": all(s["ok"] for s in steps), "steps": steps, "health": health,
                "bot_state": self.unit_state("bitpin-bot")}

    def cmd_service(self, a):
        unit, action = a.get("unit"), a.get("action")
        if unit not in SERVICE_UNITS:
            raise HelperError("unit must be one of %s" % ", ".join(SERVICE_UNITS))
        if action not in SERVICE_ACTIONS:
            raise HelperError("action must be one of %s" % ", ".join(SERVICE_ACTIONS))
        if unit == "bitpin-bot" and action in ("start", "restart"):
            chk = self.live_check()
            if not chk["ok"]:
                raise HelperError("the bot would not start: %s - apply the settings first (page: apply)" % chk["why"])
        if action != "stop":
            self.run(["systemctl", "reset-failed", unit], 30)
        rc, out = self.run(["systemctl", action, unit], 150)
        self.relay("change", "service", "%s %s" % (unit, action))
        return {"ok": rc == 0, "output": out or "%s %s: done" % (unit, action), "state": self.unit_state(unit)}

    # ---- the panel's own login
    def _panel_conf_set(self, key, value):
        path = self.p.panel_conf
        doc = parse_json_object(read_text(path, 256 * 1024), "panel.json")
        doc[key] = value
        self.backup(path, "panel.json")
        atomic_write(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n", mode=0o640)

    def cmd_panel_password_set(self, a):
        h = a.get("password_hash")
        if not isinstance(h, str) or not _PHASH_RE.match(h) or int(_PHASH_RE.match(h).group(1)) < 100000:
            raise HelperError("password_hash must be pbkdf2_sha256$<iterations>$<salt>$<hash> (100000+ iterations)")
        self._panel_conf_set("password_hash", h)
        self.relay("change", "password", "")
        return {}

    def cmd_panel_totp_set(self, a):
        s = a.get("totp_secret")
        if s is not None:
            if not isinstance(s, str) or not _TOTP_RE.match(s):
                raise HelperError("totp_secret must be base32 (A-Z, 2-7) or null")
            try:
                base64.b32decode(s + "=" * (-len(s) % 8))
            except (binascii.Error, ValueError):
                raise HelperError("totp_secret is not valid base32")
        self._panel_conf_set("totp_secret", s)
        self.relay("change", "totp", "on" if s else "off")
        return {}

    def cmd_audit_notify(self, a):
        event = a.get("event")
        if event not in PANEL_EVENTS:
            raise HelperError("event must be one of %s" % ", ".join(PANEL_EVENTS))
        actor = clean_actor({"user": a.get("user"), "ip": a.get("ip")})
        self.actor = actor
        return {"relayed": self.relay(event)}

    # ---- the owner's VPN (xray)
    def _vpn(self):
        from bitpin import vpn
        return vpn

    def _proxy_url(self):
        env = read_env(self.p.env_file)
        return env.get("KIMI_HTTPS_PROXY") or self.p.default_proxy

    def _required_targets(self):
        """The hosts the bot needs through the tunnel: its LLM host(s) and Telegram."""
        need = {"api.telegram.org"}
        try:
            doc = json.loads(read_text(self.p.kimi))
            for sec in ("llm", "news"):
                s = doc.get(sec)
                if isinstance(s, dict) and s.get("enabled", True) is not False:
                    need.add(url_host(s.get("base_url") or "https://api.moonshot.ai/v1"))
        except (OSError, ValueError, HelperError):
            need.add("api.moonshot.ai")
        return sorted(h for h in need if h)

    def _proxy_test(self):
        vpn = self._vpn()
        targets = [(h, 443) for h in sorted(set(self._required_targets()) | {"api.moonshot.ai", "openrouter.ai"})]
        return vpn.proxy_test(self._proxy_url(), targets=targets, timeout=12)

    def cmd_vpn_get(self, a):
        vpn = self._vpn()
        out = {"unit_state": self.unit_state("xray-tunnel"), "config_path": self.p.xray_conf, "summary": None,
               "raw": None, "error": None}
        try:
            text = read_text(self.p.xray_conf, 1024 * 1024)
            out["summary"] = vpn.summarize_config(vpn.loads_config(text))     # xray accepts comments
            if a.get("raw") is True:
                out["raw"] = text
        except (OSError, ValueError, HelperError) as e:
            out["error"] = str(e)[:300]
        return out

    def cmd_vpn_test(self, a):
        url = self._proxy_url()
        try:
            scheme, host, port = self._vpn().parse_proxy_url(url)
            shown = "%s://%s:%s" % (scheme, host, port)
        except ValueError:
            shown = "(invalid KIMI_HTTPS_PROXY)"
        return {"proxy": shown, "results": self._proxy_test(), "required": self._required_targets(),
                "unit_state": self.unit_state("xray-tunnel")}

    def cmd_vpn_put(self, a):
        vpn = self._vpn()
        try:
            old_text = read_text(self.p.xray_conf, 1024 * 1024)
        except FileNotFoundError:
            raise HelperError("%s does not exist" % self.p.xray_conf)
        try:
            old = vpn.loads_config(old_text)
        except ValueError:
            old = None
        link, text = a.get("link"), a.get("text")
        if isinstance(link, str) and link.strip():
            if old is None:
                raise HelperError("the current xray config is not valid JSON: paste the whole config instead")
            try:
                outbound, meta = vpn.parse_share_link(link.strip())
                new, tag = vpn.replace_proxy_outbound(old, outbound)
            except ValueError as e:
                raise HelperError(str(e)[:300])
            new_text = vpn.dumps_config(new)
        elif isinstance(text, str) and text.strip():
            if len(text) > 1024 * 1024:
                raise HelperError("the config is larger than 1 MB")
            try:
                new = vpn.loads_config(text)
            except ValueError as e:
                raise HelperError(str(e)[:300])
            if not isinstance(new.get("outbounds"), list) or not isinstance(new.get("inbounds"), list):
                raise HelperError("the xray config needs an inbounds and an outbounds list")
            bad = inbound_problems(old, new)
            if bad:
                raise HelperError("; ".join(bad))
            new_text = text.replace("\r\n", "\n")
            new_text = new_text if new_text.endswith("\n") else new_text + "\n"
        else:
            raise HelperError("give a share link (vmess:// vless:// trojan:// ss://) or the whole config")
        ok, test_out = vpn.validate_with_xray(new_text, list(self.p.xray_cmd))
        res = {"summary": vpn.summarize_config(new), "xray_test": {"ok": ok, "output": test_out}, "written": False,
               "backup": None, "tunnel_state": self.unit_state("xray-tunnel"), "proxy_test": [], "rolled_back": False,
               "diff": unified_diff(json.dumps(vpn.mask_config(old), indent=2) + "\n" if old is not None else "",
                                    json.dumps(vpn.mask_config(new), indent=2) + "\n", "config.json (masked)")}
        if not ok or a.get("dry_run"):
            return res
        bk = self.backup(self.p.xray_conf, "xray-config.json")
        vpn.atomic_write_text(self.p.xray_conf, new_text)
        res.update(written=True, backup=bk)
        self.run(["systemctl", "restart", "xray-tunnel"], 60)
        self.sleep(3)
        res["tunnel_state"] = self.unit_state("xray-tunnel")
        res["proxy_test"] = self._proxy_test()
        need = set(self._required_targets())
        works = res["tunnel_state"] == "active" and all(r["ok"] for r in res["proxy_test"] if r["target"].split(":")[0] in need)
        if not works and not a.get("keep_on_failure") and old_text is not None:
            vpn.atomic_write_text(self.p.xray_conf, old_text)
            self.run(["systemctl", "restart", "xray-tunnel"], 60)
            self.sleep(3)
            res.update(rolled_back=True, tunnel_state=self.unit_state("xray-tunnel"), proxy_test_after_rollback=self._proxy_test())
        self.relay("change", "vpn", "rolled back" if res["rolled_back"] else "changed")
        return res


def collect_bot_state(state_dir, notify_state_dir, notify_conf, now):
    """The dashboard's view of the bot's files through the notifier's own readers (bitpin.notify: an
    allowlist of files, size limits): equity and drawdown, the last decision with its Persian report,
    positions, resting orders, spend, and the Telegram /status and /last texts. Never raises. Run as the
    user bitpin (state-worker), never as root."""
    out = dict(EMPTY_STATE)
    try:
        from bitpin import notify as nt
        over = {"state_dir": state_dir, "notify_state_dir": notify_state_dir, "mode": "live"}
        try:
            cfg = nt.load_config(notify_conf if os.path.exists(notify_conf) else None, overrides=over)
        except nt.NotifyConfigError:
            cfg = nt.load_config(None, overrides=over)
        n = nt.Notifier(cfg, in_memory=True)
        eq, eq_t = n.current_equity()
        rs = nt._dict(n._read_json("risk_state"))
        hwm, dd = nt._fnum(rs.get("hwm")), nt._fnum(rs.get("drawdown"))
        if dd is None and hwm and eq:
            dd = max(0.0, 1 - eq / hwm)
        out["equity"] = {"irt": eq, "time": eq_t, "hwm_irt": hwm,
                         "drawdown_pct": None if dd is None else round(dd * 100, 2),
                         "halt_pct": None if nt._fnum(rs.get("max_drawdown")) is None
                         else round(nt._fnum(rs.get("max_drawdown")) * 100, 2),
                         "halted": rs.get("halted") is True,
                         "start_irt": nt._fnum(nt._dict(n._read_json("equity")).get("equity_start_irt"))}
        v = n.last_decision_view()
        if v:
            out["last_decision"] = {"time": v["t"], "mode": v["mode"] or v["trigger_kind"] or v["trigger"],
                                    "hold": v["hold"], "valid": v["valid"], "fallback": v["fallback"],
                                    "confidence": v["confidence"], "model": v["model"],
                                    "targets": dict((s, w) for s, w in (v["targets"] or {}).items() if w > 0),
                                    "report_fa": v["report_fa"], "error": v["error"][:400] or None,
                                    "error_kind": v["error_kind"] or None}
        st = nt._dict(n._read_json("runner_state"))
        rows = []
        for kind, key in (("allocation", "positions"), ("ladder", "ladder_positions")):
            for sym, p in sorted(nt._dict(st.get(key)).items()):
                p = nt._dict(p)
                rows.append({"symbol": str(sym)[:24], "kind": kind,
                             "amount": nt._fnum(p.get("amount") if p.get("amount") is not None else p.get("qty")),
                             "entry_px_usdt": nt._fnum(p.get("entry_px_usdt")),
                             "stop_px_usdt": nt._fnum(p.get("stop_px_usdt")),
                             "target_px_usdt": nt._fnum(p.get("target_px_usdt")),
                             "max_hold_until": nt._fnum(p.get("max_hold_until"))})
        out["positions"] = rows[:60]
        orders = nt._dict(nt._dict(n._read_json("orders")).get("orders"))
        out["resting_orders"] = sum(1 for e in orders.values() if isinstance(e, dict) and e.get("kind") == "limit"
                                    and e.get("status") in ("resting", "submitting", "unknown"))
        sp = nt._dict(n._read_json("spend"))
        if sp:
            out["spend"] = dict((k, nt._fnum(sp.get(k))) for k in ("today_usd", "month_usd", "total_usd"))
        out["status_fa"] = strip_html(n.status_text(now))
        out["last_decision_fa"] = strip_html(n.last_text())
    except Exception as e:  # noqa: BLE001 - the dashboard still shows the services
        log.warning("bot state not readable: %s: %s", type(e).__name__, e)
        out["state_error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    return out


def clean_actor(actor):
    a = actor if isinstance(actor, dict) else {}
    user = a.get("user") if isinstance(a.get("user"), str) and _USER_RE.match(a.get("user")) else ""
    ip = a.get("ip") if isinstance(a.get("ip"), str) and _IP_RE.match(a.get("ip")) else ""
    return {"user": user, "ip": ip}


# name -> (mutating: takes the change lock, function)
COMMANDS = {
    "status": (False, Helper.cmd_status),
    "performance": (False, Helper.cmd_performance),
    "config_get": (False, Helper.cmd_config_get),
    "secrets_status": (False, Helper.cmd_secrets_status),
    "health": (False, Helper.cmd_health),
    "check": (False, Helper.cmd_check),
    "logs": (False, Helper.cmd_logs),
    "confirm_show": (False, Helper.cmd_confirm_show),
    "models": (False, Helper.cmd_models),
    "vpn_get": (False, Helper.cmd_vpn_get),
    "vpn_test": (False, Helper.cmd_vpn_test),
    "audit_notify": (False, Helper.cmd_audit_notify),
    "config_put": (True, Helper.cmd_config_put),
    "settings_set": (True, Helper.cmd_settings_set),
    "model_set": (True, Helper.cmd_model_set),
    "secret_set": (True, Helper.cmd_secret_set),
    "apply_live": (True, Helper.cmd_apply_live),
    "service": (True, Helper.cmd_service),
    "panel_password_set": (True, Helper.cmd_panel_password_set),
    "panel_totp_set": (True, Helper.cmd_panel_totp_set),
    "vpn_put": (True, Helper.cmd_vpn_put),
}


# --------------------------------------------------------------------------- the connection
def peer_uid(fd):
    """The uid of the process on the other end of the UNIX socket fd (None when it cannot be told)."""
    try:
        s = socket.socket(fileno=os.dup(fd))
    except OSError:
        return None
    try:
        if not hasattr(socket, "SO_PEERCRED"):
            return None
        raw = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1]
    except OSError:
        return None
    finally:
        s.close()


def allowed_peer(uid, panel_user):
    if uid is None:
        return True          # not a socket (a test / by hand as root): the unit's socket mode is the gate
    if uid == 0:
        return True
    try:
        return pwd is not None and uid == pwd.getpwnam(panel_user).pw_uid
    except KeyError:
        return False


def read_request(rfile):
    raw = rfile.readline(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise HelperError("request too large")
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HelperError("the request is not one JSON line")
    return req


def serve(rfile, wfile, helper):
    try:
        resp = helper.handle(read_request(rfile))
    except HelperError as e:
        resp = {"ok": False, "error": str(e)}
    wfile.write((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    wfile.flush()


def state_worker(argv):
    """Run as user bitpin: print collect_bot_state() as one JSON line."""
    import argparse
    ap = argparse.ArgumentParser(prog="panel_helper.py state-worker")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--notify-state-dir", required=True)
    ap.add_argument("--notify-conf", required=True)
    args = ap.parse_args(argv)
    print(json.dumps(collect_bot_state(args.state_dir, args.notify_state_dir, args.notify_conf, time.time()),
                     default=str))
    return 0


def perf_worker(argv):
    """Run as user bitpin: print bitpin.performance.collect() as one JSON line (v3.6)."""
    import argparse
    ap = argparse.ArgumentParser(prog="panel_helper.py perf-worker")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--from", dest="t_from", type=int, required=True)
    ap.add_argument("--to", dest="t_to", type=int, required=True)
    args = ap.parse_args(argv)
    from bitpin import performance as perf
    print(json.dumps(perf.collect(args.state_dir, args.t_from, args.t_to, time.time()), default=str,
                     separators=(",", ":")))
    return 0


def models_worker(argv):
    """Run as user bitpin (the key in the environment, never on the command line): print one JSON line."""
    import argparse
    ap = argparse.ArgumentParser(prog="panel_helper.py models-worker")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--key-env", required=True, choices=LLM_KEY_NAMES)
    ap.add_argument("--provider", default="auto", choices=PROVIDERS)
    args = ap.parse_args(argv)
    from bitpin import llm as llm_mod
    client = None
    sd = tempfile.mkdtemp(prefix="bitpin_panel_models_")
    try:
        cfg = {"base_url": args.base_url, "api_key_env": args.key_env, "model": "panel-model-list",
               "max_retries": 1, "timeout": 60, "deadline_seconds": 120}
        if "provider" in llm_mod.DEFAULT_LLM_CONFIG:
            cfg["provider"] = args.provider
        client = llm_mod.LLMClient(cfg, state_dir=sd)
        if not client.has_key:
            raise llm_mod.LLMError("%s is not set" % args.key_env)
        if hasattr(client, "list_models_detailed"):
            models = client.list_models_detailed()
        else:
            models = [{"id": m, "name": None, "context_length": None, "price_in_per_m": None, "price_out_per_m": None,
                       "price_cached_in_per_m": None, "supports_json": None, "supports_reasoning": None}
                      for m in sorted(set(client.list_models()))]
        print(json.dumps({"ok": True, "models": models}, default=str))
        return 0
    except Exception as e:  # noqa: BLE001 - reported to the helper as text (redacted)
        msg = "%s: %s" % (type(e).__name__, e)
        if client is not None and hasattr(client, "redact"):
            msg = client.redact(msg)
        print(json.dumps({"ok": False, "error": msg[:500]}))
        return 1
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    if argv[:1] == ["models-worker"]:
        return models_worker(argv[1:])
    if argv[:1] == ["state-worker"]:
        return state_worker(argv[1:])
    if argv[:1] == ["perf-worker"]:
        return perf_worker(argv[1:])
    if argv:
        print(__doc__.strip())
        return 2
    paths = Paths()
    if not allowed_peer(peer_uid(0), paths.panel_user):
        log.warning("refused a connection from uid %s", peer_uid(0))
        return 1
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        log.error("panel_helper.py must run as root (systemd: bitpin-bot-panel-helper@.service)")
    os.umask(0o077)
    serve(sys.stdin.buffer, sys.stdout.buffer, Helper(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
