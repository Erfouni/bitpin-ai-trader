"""Authentication primitives of the web panel (stdlib only, Python 3.7+): password hashes, password rules,
TOTP two-factor codes, the login rate limiter and the in-memory session store.

Nothing here touches the network or the disk; the only outside input is the OS random source (secrets).
Every class is thread-safe: scripts/panel_server.py serves bitpin/panel_web.py from a ThreadingHTTPServer.

* Password hash: "pbkdf2_sha256$<iterations>$<base64 salt>$<base64 hash>" - PBKDF2-HMAC-SHA256 of the UTF-8
  password, 600000 iterations, a 16-byte random salt, a 32-byte derived key, standard base64 (RFC 4648 with
  its "=" padding; verify also accepts it without the padding). verify_password() compares in constant time
  and returns False for anything it does not recognise (never raises).
* TOTP: RFC 6238 over RFC 4226 HOTP, HMAC-SHA1, 30-second steps, 6 digits, base32 secrets of 20 random
  bytes. verify_totp() accepts the previous / current / next step and refuses a step at or below the last
  one used (a replayed code). Persian and Arabic-Indic digits typed on a phone are accepted.
* LoginLimiter: 5 failures of one IP within 15 minutes lock that IP for 15 minutes; 30 failures of all IPs
  within an hour lock every login for an hour. IPv6 addresses count per /64 (one host usually owns a whole
  /64, so counting single addresses would let it try 2**64 times).
* SessionStore: random session ids (secrets.token_urlsafe(32)), kept only as their SHA-256, with an idle
  and an absolute expiry and a per-session CSRF token.
"""
import base64
import binascii
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import struct
import threading
from urllib.parse import quote

HASH_SCHEME = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600000
SALT_BYTES = 16
DK_BYTES = 32
MIN_ITERATIONS = 1000            # a stored hash weaker than this was not made by hash_password(): refused
MAX_ITERATIONS = 10000000        # a stored hash above this would stall every login attempt: refused
MIN_PASSWORD_CHARS = 12
MAX_PASSWORD_CHARS = 1024        # longer inputs are refused before hashing (form junk, not a password)

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_SECRET_BYTES = 20

# The small built-in list of common passwords (compared lower case). Most well-known passwords are shorter
# than 12 characters and fail the length rule anyway; this list holds the long ones people pick to pass it.
COMMON_PASSWORDS = frozenset("""
123456789012 1234567890123 12345678901234 123456789000 111111111111 000000000000 123123123123
password1234 password12345 password123456 passw0rd1234 p@ssw0rd1234 p@ssword1234 password@123
qwerty123456 qwertyuiop12 qwertyuiop123 qwertyuiop1234 1q2w3e4r5t6y 1q2w3e4r5t6y7u 1qaz2wsx3edc
qazwsxedcrfv zaq12wsxcde3 asdfghjkl123 zxcvbnm12345 abc123456789 abcdefghijkl abcd12345678
iloveyou1234 administrator admin1234567 admin@123456 welcome12345 letmein12345 changeme1234
trustno1trustno1 football1234 baseball1234 sunshine1234 princess1234 superman1234 starwars1234
masterkey123 bitpin123456 bitpin@12345 bitcoin12345 bitcoin123456
""".split())
# A password that is one of these words with only digits / symbols around it is refused too
# ("Password123!", "Bitpin@2026!!", "Qwerty!23456789").
COMMON_WORDS = frozenset("""
password passw passwd pass qwerty qwertyuiop asdf asdfgh asdfghjkl zxcvbnm qazwsx qazwsxedc azerty
admin administrator root user login welcome letmein changeme secret iloveyou love trustno monkey dragon
master football baseball soccer sunshine princess superman batman starwars shadow freedom whatever
abc abcd abcdef abcdefgh bitpin bitcoin crypto trader trading kimi panel server ubuntu
""".split())

_DIGITS_FA = {ord(c): str(i) for i, c in enumerate(u"۰۱۲۳۴۵۶۷۸۹")}
_DIGITS_FA.update({ord(c): str(i) for i, c in enumerate(u"٠١٢٣٤٥٦٧٨٩")})


# --------------------------------------------------------------------------------------------- passwords

def _b64e(raw):
    return base64.b64encode(raw).decode("ascii")


def _b64d(text):
    if not text or len(text) > 256:
        raise ValueError("bad base64 length")
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def hash_password(pw, iterations=PBKDF2_ITERATIONS, salt=None):
    """The stored form of `pw` (see the module docstring). `iterations` / `salt` exist for tests only."""
    if not isinstance(pw, str):
        raise TypeError("password must be a str")
    iterations = int(iterations)
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise ValueError("iterations out of range")
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (HASH_SCHEME, iterations, _b64e(salt), _b64e(dk))


def parse_password_hash(stored):
    """(iterations, salt, derived key) of a stored hash, or None when it is not a hash of this format."""
    if not isinstance(stored, str) or len(stored) > 512:
        return None
    parts = stored.strip().split("$")
    if len(parts) != 4 or parts[0] != HASH_SCHEME:
        return None
    if not parts[1].isdigit() or len(parts[1]) > 9:          # ASCII digits only ("isdigit" also takes '٣')
        return None
    try:
        iterations = int(parts[1].encode("ascii"))
        salt = _b64d(parts[2])
        dk = _b64d(parts[3])
    except (ValueError, UnicodeEncodeError, binascii.Error):
        return None
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS or not 8 <= len(salt) <= 64 or len(dk) != DK_BYTES:
        return None
    return iterations, salt, dk


def is_password_hash(stored):
    return parse_password_hash(stored) is not None


def verify_password(pw, stored):
    """True when `pw` matches the stored hash. Unknown / broken formats and non-str input -> False."""
    parsed = parse_password_hash(stored)
    if parsed is None or not isinstance(pw, str) or len(pw) > MAX_PASSWORD_CHARS:
        return False
    iterations, salt, dk = parsed
    try:
        candidate = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    except UnicodeEncodeError:                               # a lone surrogate: cannot be the password
        return False
    return hmac.compare_digest(candidate, dk)


def _char_classes(pw):
    lower = upper = digit = other = False
    for ch in pw:
        if ch.islower():
            lower = True
        elif ch.isupper():
            upper = True
        elif ch.isdigit():
            digit = True
        else:                                                # symbols, spaces, Persian letters ...
            other = True
    return int(lower) + int(upper) + int(digit) + int(other)


def password_problems(pw, username, lang="fa"):
    """Why `pw` is not acceptable as the panel password ([] = acceptable), in Persian (lang="fa", the setup
    command) or English (lang="en"; the web panel passes the page's language)."""
    def say(fa, en):
        return en if lang == "en" else fa

    if not isinstance(pw, str) or not pw:
        return [say(u"رمز خالی است.", "The password is empty.")]
    problems = []
    if len(pw) < MIN_PASSWORD_CHARS:
        problems.append(say(u"رمز باید دست‌کم %d نویسه باشد." % MIN_PASSWORD_CHARS,
                            "The password must have at least %d characters." % MIN_PASSWORD_CHARS))
    if len(pw) > MAX_PASSWORD_CHARS:
        problems.append(say(u"رمز بیش از %d نویسه است." % MAX_PASSWORD_CHARS,
                            "The password is longer than %d characters." % MAX_PASSWORD_CHARS))
    if _char_classes(pw) < 3:
        problems.append(say(u"رمز باید دست‌کم سه نوع از این‌ها را داشته باشد: حرف کوچک، حرف بزرگ، رقم، "
                            u"نماد (یا حرف فارسی).",
                            "The password needs at least three of: lower-case letters, upper-case letters, digits, "
                            "symbols (or Persian letters)."))
    if len(set(pw)) < 6:
        problems.append(say(u"رمز تکراری است: دست‌کم ۶ نویسهٔ متفاوت لازم است.",
                            "The password repeats itself: at least 6 different characters are needed."))
    user = (username or "").strip().lower()
    if len(user) >= 3 and user in pw.lower():
        problems.append(say(u"رمز نباید نام کاربری را در خود داشته باشد.",
                            "The password must not contain the username."))
    low = pw.lower()
    letters = "".join(ch for ch in low if ch.isalpha())
    if low in COMMON_PASSWORDS or letters in COMMON_WORDS or low.strip("0123456789!@#$%^&*()_-+=.?~ ") in COMMON_WORDS:
        problems.append(say(u"این رمز جزو رمزهای رایج و حدس‌زدنی است.",
                            "This is a common, guessable password."))
    return problems


# --------------------------------------------------------------------------------------------- TOTP

def new_totp_secret():
    """A fresh base32 secret of 20 random bytes (32 characters, no padding)."""
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode("ascii")


def _secret_bytes(secret):
    if not isinstance(secret, str):
        raise ValueError("secret must be a str")
    s = "".join(secret.split()).replace("-", "").upper()
    if not s or len(s) > 256:
        raise ValueError("bad secret length")
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(key, counter, digits=TOTP_DIGITS):
    """RFC 4226 HOTP value of `counter` (a zero-padded decimal str) for the raw key bytes."""
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def totp(secret, t, digits=TOTP_DIGITS, step=TOTP_STEP):
    """RFC 6238 TOTP code of the base32 `secret` at unix time `t`."""
    return hotp(_secret_bytes(secret), int(t // step), digits)


def normalize_code(code, digits=TOTP_DIGITS):
    """The code as ASCII digits (spaces removed, Persian / Arabic-Indic digits mapped), or None."""
    if not isinstance(code, str) or len(code) > 32:
        return None
    c = "".join(code.split()).translate(_DIGITS_FA)
    if len(c) != digits or any(ch not in "0123456789" for ch in c):
        return None
    return c


def verify_totp(secret, code, t, last_counter=None):
    """(ok, counter): the code matches the previous, current or next 30 s step of `t` and that step is above
    `last_counter` (the step of the last code accepted: a replay is refused). counter is None when not ok."""
    c = normalize_code(code)
    try:
        key = _secret_bytes(secret)
    except (ValueError, TypeError, binascii.Error):
        return False, None
    if c is None or not key:
        return False, None
    now_step = int(t // TOTP_STEP)
    found = None
    for step in (now_step - 1, now_step, now_step + 1):     # all three computed: no early exit on a match
        if step < 0:
            continue
        match = hmac.compare_digest(hotp(key, step).encode("ascii"), c.encode("ascii"))
        fresh = last_counter is None or step > last_counter
        if match and fresh and found is None:
            found = step
    return (found is not None), found


def otpauth_uri(secret, username, issuer="bitpin-bot"):
    """The otpauth:// URI authenticator apps import (as text or a QR code)."""
    label = "%s:%s" % (quote(issuer, safe=""), quote(username or "", safe=""))
    return "otpauth://totp/%s?secret=%s&issuer=%s&algorithm=SHA1&digits=%d&period=%d" % (
        label, quote(secret, safe=""), quote(issuer, safe=""), TOTP_DIGITS, TOTP_STEP)


# --------------------------------------------------------------------------------------------- rate limit

def ip_key(ip):
    """The limiter's key of a client address: the IPv4 address, or the /64 of an IPv6 address."""
    text = str(ip or "").strip()
    try:
        addr = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return text[:64] or "?"
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network("%s/64" % addr, strict=False))
    return str(addr)


class LoginLimiter(object):
    """Failed-login throttle. check() before verifying anything, failure() / success() after."""

    def __init__(self, ip_max=5, ip_window=900.0, ip_lock=900.0, global_max=30, global_window=3600.0,
                 global_lock=3600.0):
        self.ip_max, self.ip_window, self.ip_lock = int(ip_max), float(ip_window), float(ip_lock)
        self.global_max, self.global_window = int(global_max), float(global_window)
        self.global_lock = float(global_lock)
        self._lock = threading.Lock()
        self._fails = {}            # ip key -> [failure times]
        self._locked = {}           # ip key -> locked until
        self._global = []           # failure times of all IPs
        self._global_until = 0.0

    def _prune(self, now):
        for k in list(self._fails):
            kept = [t for t in self._fails[k] if t > now - self.ip_window]
            if kept:
                self._fails[k] = kept
            else:
                del self._fails[k]
        for k in list(self._locked):
            if self._locked[k] <= now:
                del self._locked[k]
        self._global = [t for t in self._global if t > now - self.global_window]

    def check(self, ip, now, trusted=False):
        """(allowed, why): why is "" when allowed, "global" (every login locked) or "ip" (this IP locked).
        trusted: a browser with a valid device cookie (DeviceTrust) - the GLOBAL lock does not apply to it, so a
        guessing flood from many addresses cannot lock the owner out (the per-IP lock still does)."""
        with self._lock:
            if now < self._global_until and not trusted:
                return False, "global"
            until = self._locked.get(ip_key(ip))
            if until is not None and now < until:
                return False, "ip"
            return True, ""

    def locked_until(self, ip, now, trusted=False):
        """When the lock that applies to `ip` ends (0.0 = not locked)."""
        with self._lock:
            ends = [self._global_until] if now < self._global_until and not trusted else []
            until = self._locked.get(ip_key(ip))
            if until is not None and now < until:
                ends.append(until)
            return max(ends) if ends else 0.0

    def failure(self, ip, now):
        """Records a failed attempt. Returns the locks this failure started: [], ["ip"], ["global"] or both."""
        started = []
        with self._lock:
            self._prune(now)
            k = ip_key(ip)
            fails = self._fails.setdefault(k, [])
            fails.append(now)
            if len(fails) >= self.ip_max and not self._locked.get(k, 0.0) > now:
                self._locked[k] = now + self.ip_lock
                self._fails.pop(k, None)                    # the count starts again after the lock
                started.append("ip")
            self._global.append(now)
            if len(self._global) >= self.global_max and now >= self._global_until:
                self._global_until = now + self.global_lock
                self._global = []
                started.append("global")
        return started

    def success(self, ip, now):
        """A successful login forgets that IP's failures (the global count stays)."""
        with self._lock:
            self._fails.pop(ip_key(ip), None)
            self._prune(now)


# --------------------------------------------------------------------------------------------- trusted browsers

_DEVICE_RE = re.compile(r"^([A-Za-z0-9_-]{16,64})\.(\d{9,12})\.([0-9a-f]{64})$")
DEVICE_MAX_AGE = 365 * 86400
log = logging.getLogger("bitpin.panel")


class DeviceTrust(object):
    """Trusted browsers ("device cookies"): a browser that logged in successfully gets a signed, long-lived
    cookie "<id>.<issued>.<hmac>". While the GLOBAL login lock is on (a guessing flood from many addresses), a
    login from such a browser is still checked - the per-IP lock and every credential check stay - so an attacker
    population cannot lock the owner out of the panel. The signing key is derived from a server secret AND the
    current password hash: changing the password invalidates every device cookie at once. A device with
    device_max failed attempts is not trusted any more (until the panel restarts or it logs in again)."""

    def __init__(self, secret, max_age=DEVICE_MAX_AGE, device_max=5):
        self.secret = bytes(secret)
        self.max_age = float(max_age)
        self.device_max = int(device_max)
        self._lock = threading.Lock()
        self._fails = {}
        self._revoked = set()

    def _sig(self, password_hash, dev_id, issued):
        key = hmac.new(self.secret, ("bitpin-panel device|" + str(password_hash or "")).encode("utf-8"),
                       hashlib.sha256).digest()
        return hmac.new(key, ("%s.%d" % (dev_id, issued)).encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self, password_hash, now):
        dev_id, issued = secrets.token_urlsafe(18), int(now)
        return "%s.%d.%s" % (dev_id, issued, self._sig(password_hash, dev_id, issued))

    def check(self, token, password_hash, now):
        """The device id of a valid, unexpired, not revoked cookie, else None."""
        m = _DEVICE_RE.match(token or "") if isinstance(token, str) else None
        if not m:
            return None
        dev_id, issued, sig = m.group(1), int(m.group(2)), m.group(3)
        if not 0 <= now - issued <= self.max_age:
            return None
        if not hmac.compare_digest(sig, self._sig(password_hash, dev_id, issued)):
            return None
        with self._lock:
            return None if dev_id in self._revoked else dev_id

    def failure(self, dev_id):
        if not dev_id:
            return
        with self._lock:
            n = self._fails.get(dev_id, 0) + 1
            if n >= self.device_max:
                self._revoked.add(dev_id)
                self._fails.pop(dev_id, None)
            else:
                self._fails[dev_id] = n

    def success(self, dev_id):
        with self._lock:
            self._fails.pop(dev_id, None)


def load_device_secret(path):
    """32 random bytes kept in `path` (0600; created on first use), so device cookies survive a restart. When the
    file cannot be read or written, an in-memory secret (device cookies then end with the process)."""
    try:
        with open(path, "rb") as f:
            data = f.read(64)
        if len(data) >= 32:
            return data[:32]
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("panel: cannot read %s (%s): device cookies last until the next restart", path, e)
        return secrets.token_bytes(32)
    data = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError as e:
        log.warning("panel: cannot write %s (%s): device cookies last until the next restart", path, e)
    return data


# --------------------------------------------------------------------------------------------- sessions

def _sid_key(sid):
    return hashlib.sha256(sid.encode("utf-8", "replace")).hexdigest()


class SessionStore(object):
    """In-memory sessions: sid -> {user, ip, created, last_seen, csrf} (plus page state the web app keeps:
    flash messages, a pending 2FA secret ...). Only the SHA-256 of a sid is kept as the key."""

    def __init__(self, idle_minutes=30, max_hours=12, max_sessions=50):
        self.idle_seconds = max(60.0, float(idle_minutes) * 60.0)
        self.max_seconds = max(60.0, float(max_hours) * 3600.0)
        self.max_sessions = max(1, int(max_sessions))
        self._lock = threading.Lock()
        self._sessions = {}

    def _expired(self, s, now):
        return now - s["last_seen"] > self.idle_seconds or now - s["created"] > self.max_seconds

    def create(self, user, ip, now):
        """A new session; returns its sid (the cookie value)."""
        sid = secrets.token_urlsafe(32)
        sess = {"user": user, "ip": ip, "created": now, "last_seen": now, "csrf": secrets.token_urlsafe(32)}
        with self._lock:
            for k in [k for k, s in self._sessions.items() if self._expired(s, now)]:
                del self._sessions[k]
            while len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions, key=lambda k: self._sessions[k]["last_seen"])
                del self._sessions[oldest]
            self._sessions[_sid_key(sid)] = sess
        return sid

    def get(self, sid, now, touch=True):
        """The live session of `sid` (its last_seen touched, unless touch is False: a page that reloads itself
        is not the owner at the keyboard) or None (unknown or expired: then removed)."""
        if not isinstance(sid, str) or not sid or len(sid) > 128:
            return None
        k = _sid_key(sid)
        with self._lock:
            sess = self._sessions.get(k)
            if sess is None:
                return None
            if self._expired(sess, now):
                del self._sessions[k]
                return None
            if touch:
                sess["last_seen"] = now
            return sess

    def destroy(self, sid):
        if isinstance(sid, str) and sid:
            with self._lock:
                self._sessions.pop(_sid_key(sid), None)

    def destroy_all(self):
        with self._lock:
            self._sessions.clear()

    def __len__(self):
        with self._lock:
            return len(self._sessions)
