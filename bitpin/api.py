"""Bitpin REST API client (stdlib only, Python 3.8+).

Safety properties (this module is the only code that talks to the authenticated API):

* Endpoint allow-list (`_ALLOWED`): market data, wallet BALANCES, orders, fills and the two auth
  endpoints. There is deliberately no withdrawal / deposit / transfer / address code anywhere,
  and any other path is refused before a request is built.
* Order POSTs are never retried blindly. After a timeout, network error or 5xx on
  POST /api/v1/odr/orders/ the client looks the order up by its client `identifier` and only
  returns once it knows the outcome; if it cannot find out it raises `OrderStatusUnknown` (the
  caller must NOT resubmit - see LiveBroker's order journal). A 429 on the POST is followed by a
  lookup as well before the same identifier is sent again, but only within
  `order_throttle_max_wait` seconds of the first attempt (default 10 s): a market order is never
  sent minutes after the runner's pre-trade checks. After that the client raises `OrderNotSent`
  (every response was a 429, so nothing was accepted).
* Order types: market and limit only (no stop_limit / OCO). Cancels go by order id
  (DELETE /api/v1/odr/orders/{id}/); open orders are read with GET ?state=active, which lists the
  whole account - callers filter by their own identifiers.
* HTTP redirects are never followed (the Authorization header must not travel to another URL);
  a 3xx answer is an error.
* Bitpin traffic never uses a proxy: the opener is built with ProxyHandler({}), so http_proxy /
  https_proxy / ALL_PROXY environment variables are ignored (the API key is IP-whitelisted to this
  server's own address).
* Authentication is lazy; {access, refresh, exp} are cached in `state_dir/bitpin_token.json`
  (never the API keys). Expiry is kept on the LOCAL clock (token lifetime = exp - iat, counted
  from receipt), so a skewed computer clock does not cause a refresh per request. The access token
  is refreshed ~60 s before expiry; if the refresh token is REJECTED (4xx) the client
  re-authenticates, while a temporary failure (network, 5xx, 429) keeps the refresh token and
  raises an ordinary (non-fatal) error. After such a failure no renewal is attempted for a
  cool-down (60 s, doubling to 30 min while failures continue; an access token that has not
  expired yet keeps being used), so an auth outage cannot burn the budget with one renewal per API
  call. AuthError - fatal for the bot - means a credential rejection only. Every
  authenticate/refresh call is counted in `state_dir/auth_budget.json` and the client hard-stops
  at 150 calls per rolling 24 h (Bitpin allows 200/day).
* API keys and tokens are never logged or put into exception messages.
* JSON is parsed with parse_float=Decimal; all amounts are handled as Decimal / str.
"""
import base64
import hashlib
import http.client
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal

log = logging.getLogger("bitpin.api")

DEFAULT_BASE_URL = "https://api.bitpin.org"
USER_AGENT = "bitpin-bot/1.0"
TOKEN_FILE = "bitpin_token.json"
AUTH_BUDGET_FILE = "auth_budget.json"

AUTH_PATH = "/api/v1/usr/authenticate/"
REFRESH_PATH = "/api/v1/usr/refresh_token/"
ORDERS_PATH = "/api/v1/odr/orders/"

_SYM = r"[A-Z0-9]+_[A-Z0-9]+"
_ALLOWED = [
    ("GET", re.compile(r"^/api/v1/mkt/markets/$")),
    ("GET", re.compile(r"^/api/v1/mkt/tickers/$")),
    ("GET", re.compile(r"^/api/v1/mkt/commissions/$")),
    ("GET", re.compile(r"^/api/v1/mth/orderbook/%s/$" % _SYM)),
    ("GET", re.compile(r"^/api/v1/mth/matches/%s/$" % _SYM)),
    ("POST", re.compile(r"^/api/v1/usr/authenticate/$")),
    ("POST", re.compile(r"^/api/v1/usr/refresh_token/$")),
    ("GET", re.compile(r"^/api/v1/wlt/wallets/$")),            # balances only
    ("GET", re.compile(r"^/api/v1/odr/orders/$")),
    ("POST", re.compile(r"^/api/v1/odr/orders/$")),
    ("GET", re.compile(r"^/api/v1/odr/orders/[0-9A-Za-z_-]+/$")),
    ("DELETE", re.compile(r"^/api/v1/odr/orders/[0-9A-Za-z_-]+/$")),
    ("GET", re.compile(r"^/api/v1/odr/fills/$")),
]

OPEN_ORDER_STATES = ("initial", "active")


# --------------------------------------------------------------------------- errors

class BitpinError(Exception):
    """Base class for every error raised by this module."""


class TransportError(BitpinError):
    """The request may or may not have reached the server (timeout, connection reset, DNS...)."""

    def __init__(self, message, timeout=False):
        super().__init__(message)
        self.timeout = timeout


class BitpinAPIError(BitpinError):
    """HTTP error response from Bitpin."""

    def __init__(self, status, code=None, payload=None, message=None):
        self.status = status
        self.code = code
        self.payload = payload
        super().__init__(message or "HTTP %s%s: %s" % (status, " [%s]" % code if code else "", _short(payload)))


class AuthError(BitpinAPIError):
    """Credentials missing or rejected (HTTP 406 api_credential_wrong, or another 4xx from the
    authenticate endpoint). Never retried. Throttling / server errors are NOT AuthError."""


class AuthBudgetExceeded(BitpinError):
    """Local hard stop: too many authenticate/refresh calls in the last 24 h."""


class RefreshRejected(BitpinAPIError):
    """The refresh token itself was rejected (4xx other than 429): re-authenticate."""


class AuthUnavailable(BitpinError):
    """Token renewal failed temporarily a moment ago (network, 5xx, 429) and is not retried until
    its cool-down has passed, so an outage of Bitpin's /usr/ service cannot burn the daily auth
    budget with one renewal attempt per API call. Temporary, NOT fatal."""


class OrderStatusUnknown(BitpinError):
    """An order POST failed ambiguously and the order could not be found by identifier.
    The order may or may not exist. Do NOT resubmit; resolve by identifier later."""

    def __init__(self, identifier, message):
        super().__init__(message)
        self.identifier = identifier


class OrderNotSent(BitpinError):
    """The order was definitely NOT accepted by the exchange: it failed before any POST was sent
    (e.g. no access token), or every POST was answered with HTTP 429/401 (rejected before
    processing). Safe to treat as 'nothing happened'; the runner re-vets it on a later bar."""

    def __init__(self, identifier, message):
        super().__init__(message)
        self.identifier = identifier


# --------------------------------------------------------------------------- helpers

def _short(payload, n=300):
    try:
        s = json.dumps(payload, default=str, ensure_ascii=True)
    except Exception:  # noqa: BLE001
        s = repr(payload)
    return s if len(s) <= n else s[:n] + "..."


def _json_default(o):
    if isinstance(o, Decimal):
        return format(o, "f")
    raise TypeError("not JSON serialisable: %r" % type(o))


def mask(secret):
    """Never show a secret; only whether it is set."""
    return "<set>" if secret else "<not set>"


def key_fingerprint(api_key):
    """Short one-way fingerprint so cached tokens are not reused with different keys."""
    return hashlib.sha256(("bitpin-bot:" + (api_key or "")).encode("utf-8")).hexdigest()[:16]


def jwt_claims(token):
    """The payload of a JWT as a dict (no signature check), or None."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")).decode("utf-8"))
        return claims if isinstance(claims, dict) else None
    except Exception:  # noqa: BLE001
        return None


def jwt_exp(token):
    """Read the `exp` claim of a JWT (no signature check). Returns int epoch seconds or None."""
    try:
        return int((jwt_claims(token) or {})["exp"])
    except Exception:  # noqa: BLE001
        return None


def _is_temporary_status(status):
    """HTTP statuses that say nothing about the credentials: throttling, server errors, redirects."""
    return status == 429 or status >= 500 or 300 <= status < 400


_THROTTLE_RE = re.compile(r"available in\s+(\d+(?:\.\d+)?)\s*second", re.I)


def throttle_wait(payload, default, cap):
    """Seconds to wait after HTTP 429: the 'Expected available in N seconds' hint (+0.5 s),
    else `default`; always capped at `cap`."""
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    m = _THROTTLE_RE.search(str(detail or ""))
    wait = float(m.group(1)) + 0.5 if m else float(default)
    return max(0.0, min(wait, float(cap)))


def atomic_write_json(path, obj, private=False):
    """Write JSON atomically: temp file in the same directory + fsync + os.replace."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True, default=_json_default)
        f.flush()
        os.fsync(f.fileno())
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    for attempt in range(5):  # Windows: the target may be briefly locked by a scanner
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f, parse_float=Decimal)
    except FileNotFoundError:
        return default


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would re-send the Authorization header to the new URL
    (possibly another host, or plain http). The 3xx is returned to the caller as a status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def direct_opener():
    """urllib opener for Bitpin: redirects are never followed and NO proxy is ever used.

    ProxyHandler({}) replaces urllib's default ProxyHandler, which would read http_proxy /
    https_proxy / ALL_PROXY from the environment (and the Windows registry). Bitpin API keys are
    whitelisted to this server's own IP, so Bitpin traffic must always go direct, even when such
    variables are set system-wide (a proxy for the Kimi LLM is configured separately:
    KIMI_HTTPS_PROXY, used only by bitpin/llm.py)."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


_OPENER = direct_opener()


def default_transport(method, url, headers, body, timeout):
    """urllib transport: returns (status, body_bytes); raises TransportError on network failure.
    Redirects are NOT followed (a 3xx comes back as the status)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:  # noqa: BLE001
            data = b""
        return e.code, data
    except (socket.timeout, TimeoutError) as e:
        raise TransportError("timeout: %s" % e, timeout=True)
    except urllib.error.URLError as e:
        is_to = isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError))
        raise TransportError("network error: %s" % getattr(e, "reason", e), timeout=is_to)
    except (http.client.HTTPException, ConnectionError, OSError) as e:
        raise TransportError("network error: %s" % e)


# --------------------------------------------------------------------------- auth budget + tokens

class AuthBudget:
    """Persisted count of authenticate+refresh calls over a rolling 24 h window."""

    def __init__(self, path=None, limit=150, window=86400, clock=time.time):
        self.path, self.limit, self.window, self.clock = path, limit, window, clock
        self._mem = []

    def _calls(self):
        now = self.clock()
        if self.path:
            data = read_json(self.path, {}) or {}
            calls = [float(t) for t in data.get("calls", [])]
        else:
            calls = list(self._mem)
        return [t for t in calls if now - t < self.window]

    def used(self):
        return len(self._calls())

    def consume(self, what):
        calls = self._calls()
        if len(calls) >= self.limit:
            raise AuthBudgetExceeded(
                "auth budget exhausted: %d authenticate/refresh calls in the last 24h (hard stop %d; "
                "Bitpin allows 200/day). Wait, or check why tokens are not being reused." % (len(calls), self.limit))
        calls.append(self.clock())
        if self.path:
            atomic_write_json(self.path, {"calls": calls, "limit": self.limit, "last": what})
        else:
            self._mem = calls


class TokenManager:
    """Lazily obtains and caches JWTs. `post(path, body) -> (status, payload)` is a single
    unauthenticated attempt that may raise TransportError."""

    COOLDOWN_START = 60.0     # seconds without a renewal attempt after a temporary failure...
    COOLDOWN_MAX = 1800.0     # ...doubling up to this while the failures continue

    def __init__(self, post, api_key, secret_key, state_dir=None, clock=time.time, sleep=time.sleep,
                 budget=None, refresh_margin=60):
        self._post = post
        self._api_key = api_key
        self._secret_key = secret_key
        self.clock, self.sleep = clock, sleep
        self.refresh_margin = refresh_margin
        self._cooldown = 0.0
        self._cooldown_until = 0.0
        self._last_temp_error = None
        self.path = os.path.join(state_dir, TOKEN_FILE) if state_dir else None
        self.budget = budget or AuthBudget(os.path.join(state_dir, AUTH_BUDGET_FILE) if state_dir else None, clock=clock)
        self._fp = key_fingerprint(api_key)
        self._tok = self._load()

    def __repr__(self):
        return "TokenManager(keys=%s, cached_token=%s)" % (mask(self._api_key), bool(self._tok.get("access")))

    def _load(self):
        if not self.path:
            return {}
        t = read_json(self.path, {}) or {}
        if t.get("key_fp") != self._fp:
            return {}
        return t

    def _save(self):
        self._tok["key_fp"] = self._fp
        if self.path:
            atomic_write_json(self.path, self._tok, private=True)

    def invalidate_access(self):
        if self._tok:
            self._tok["access_exp"] = 0
            self._save()

    def _access_exp(self):
        t = self._tok
        return float(t.get("access_exp") or 0) if t.get("access") else 0.0

    def access_token(self):
        now = self.clock()
        if self._access_exp() - self.refresh_margin > now:
            return self._tok["access"]
        if now < self._cooldown_until:
            # a renewal failed temporarily a moment ago: no new attempt (and no auth budget) until the
            # cool-down has passed; an access token that has not expired yet is still used
            if self._access_exp() > now:
                return self._tok["access"]
            raise AuthUnavailable("token renewal failed temporarily (%s); next attempt in %.0f s"
                                  % (self._last_temp_error, self._cooldown_until - now))
        try:
            self._renew(now)
        except (AuthError, RefreshRejected):
            raise
        except (BitpinAPIError, TransportError) as e:
            # network, 5xx, 429 or an odd response: says nothing about the credentials (not fatal)
            self._cooldown = min(max(self._cooldown * 2, self.COOLDOWN_START), self.COOLDOWN_MAX)
            self._cooldown_until = self.clock() + self._cooldown
            self._last_temp_error = "%s: %s" % (type(e).__name__, str(e)[:120])
            log.warning("token renewal failed temporarily (%s); next attempt in %.0f s", self._last_temp_error,
                        self._cooldown)
            if self._access_exp() > self.clock():
                return self._tok["access"]
            raise
        self._cooldown, self._cooldown_until, self._last_temp_error = 0.0, 0.0, None
        return self._tok["access"]

    def _renew(self, now):
        t = self._tok
        if t.get("refresh") and float(t.get("refresh_exp") or 0) - self.refresh_margin > now:
            try:
                self._refresh()
                return
            except RefreshRejected as e:
                log.warning("refresh token rejected (HTTP %s); re-authenticating", e.status)
            # Any other failure (network, 5xx, 429, odd response) says nothing about the refresh
            # token: it is kept and the error propagates as a TEMPORARY error (the runner backs off
            # and retries). Re-authenticating here would only burn the daily auth budget during an
            # outage of Bitpin's /usr/ service.
        self._authenticate()

    def _auth_post(self, path, body, what):
        """One budgeted call with limited retries on throttling / server / network errors."""
        delay = 2.0
        for attempt in range(3):
            self.budget.consume(what)
            try:
                status, payload = self._post(path, body)
            except TransportError:
                if attempt == 2:
                    raise
                self.sleep(delay)
                delay *= 2
                continue
            if (status == 429 or status >= 500) and attempt < 2:
                self.sleep(throttle_wait(payload, delay, 60) if status == 429 else delay)
                delay *= 2
                continue
            return status, payload
        raise BitpinError("unreachable")

    def _expiry(self, token, now, default_ttl, what):
        """LOCAL-clock expiry of a freshly received token. The server's `exp` is converted into a
        lifetime (exp - iat) that starts now, so a local clock that runs ahead or behind the server
        neither makes every token look expired (a refresh per request) nor keeps expired ones."""
        claims = jwt_claims(token) or {}
        try:
            exp = float(claims["exp"])
        except (KeyError, TypeError, ValueError):
            return int(now + default_ttl)
        try:
            iat = float(claims["iat"])
        except (KeyError, TypeError, ValueError):
            iat = None
        if iat is not None and exp > iat:
            skew = now - iat
            if abs(skew) > 300:
                log.warning("this computer's clock differs from Bitpin's by about %+.0f min (from the %s token's "
                            "issue time): fix the clock; token expiry is computed from the token lifetime instead",
                            skew / 60, what)
            return int(now + (exp - iat))
        if exp - now < 120:
            # already (nearly) expired by the local clock right after it was issued: the clock runs
            # ahead. Assume the usual lifetime; an HTTP 401 renews it if that is too long.
            log.warning("the new %s token expires %.0f s from now by this computer's clock: the clock is probably "
                        "ahead of Bitpin's. Fix the clock.", what, exp - now)
            return int(now + default_ttl)
        return int(exp)

    def _authenticate(self):
        if not self._api_key or not self._secret_key:
            raise AuthError(0, "no_credentials", None, "no API credentials configured")
        status, payload = self._auth_post(AUTH_PATH, {"api_key": self._api_key, "secret_key": self._secret_key},
                                          "authenticate")
        code = payload.get("code") if isinstance(payload, dict) else None
        if status == 406 or code == "api_credential_wrong":
            raise AuthError(status, "api_credential_wrong", None,
                            "Bitpin rejected the API credentials (HTTP %s). Check the keys and the IP whitelist." % status)
        if _is_temporary_status(status):
            # throttling / outage: NOT a credential problem, so not fatal for the bot
            raise BitpinAPIError(status, code, None, "authenticate failed temporarily: HTTP %s %s" % (status, code or ""))
        if status >= 400:
            raise AuthError(status, code, None, "authenticate rejected: HTTP %s %s" % (status, code or ""))
        if not isinstance(payload, dict) or not payload.get("access") or not payload.get("refresh"):
            keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
            raise BitpinAPIError(status, "bad_response", None, "unexpected authenticate response (keys: %s)" % (keys,))
        now = self.clock()
        self._tok = {
            "access": payload["access"], "refresh": payload["refresh"],
            "access_exp": self._expiry(payload["access"], now, 14 * 60, "access"),
            "refresh_exp": self._expiry(payload["refresh"], now, 29 * 86400, "refresh"),
            "obtained_at": int(now),
        }
        self._save()
        log.info("authenticated with Bitpin (auth budget used %d/%d in 24h)", self.budget.used(), self.budget.limit)

    def _refresh(self):
        status, payload = self._auth_post(REFRESH_PATH, {"refresh": self._tok["refresh"]}, "refresh")
        code = payload.get("code") if isinstance(payload, dict) else None
        if _is_temporary_status(status):
            raise BitpinAPIError(status, code, None, "token refresh failed temporarily: HTTP %s %s (refresh token "
                                                     "kept)" % (status, code or ""))
        if status >= 400:
            self._tok = {}
            self._save()
            raise RefreshRejected(status, code, None, "token refresh rejected: HTTP %s %s" % (status, code or ""))
        if not isinstance(payload, dict) or not payload.get("access"):
            raise BitpinAPIError(status, "bad_response", None, "unexpected token refresh response (refresh token kept)")
        now = self.clock()
        self._tok["access"] = payload["access"]
        self._tok["access_exp"] = self._expiry(payload["access"], now, 14 * 60, "access")
        if payload.get("refresh"):  # rotated refresh token
            self._tok["refresh"] = payload["refresh"]
            self._tok["refresh_exp"] = self._expiry(payload["refresh"], now, 29 * 86400, "refresh")
        self._save()
        log.debug("access token refreshed")


# --------------------------------------------------------------------------- client

class BitpinClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, api_key=None, secret_key=None, state_dir=None,
                 transport=None, timeout=15, sleep=time.sleep, clock=time.time, max_retries=4,
                 backoff_base=1.0, backoff_max=30.0, throttle_cap=60.0, auth_budget_limit=150,
                 order_lookup_attempts=4, order_lookup_delay=2.0, order_throttle_max_wait=10.0):
        u = urllib.parse.urlparse(base_url)
        if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("base_url must be https")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport or default_transport
        self._sleep, self._clock = sleep, clock
        self.max_retries, self.backoff_base, self.backoff_max = max_retries, backoff_base, backoff_max
        self.throttle_cap = throttle_cap
        self.order_throttle_max_wait = float(order_throttle_max_wait)
        self.order_lookup_attempts, self.order_lookup_delay = order_lookup_attempts, order_lookup_delay
        self.state_dir = state_dir
        self.has_credentials = bool(api_key and secret_key)
        self._tokens = None
        if self.has_credentials:
            if not state_dir:
                raise ValueError("state_dir is required when credentials are given (token cache + auth budget)")
            os.makedirs(state_dir, exist_ok=True)
            budget = AuthBudget(os.path.join(state_dir, AUTH_BUDGET_FILE), limit=auth_budget_limit, clock=clock)
            self._tokens = TokenManager(self._post_unauth, api_key, secret_key, state_dir, clock, sleep, budget)

    def __repr__(self):
        return "BitpinClient(base_url=%r, credentials=%s)" % (self.base_url, "<set>" if self.has_credentials else "<none>")

    @property
    def auth_budget(self):
        return self._tokens.budget if self._tokens else None

    # ---- low level
    @staticmethod
    def _check_allowed(method, path):
        for m, rx in _ALLOWED:
            if m == method and rx.match(path):
                return
        raise ValueError("endpoint not allowed by this client: %s %s" % (method, path))

    def _send(self, method, path, params=None, body=None, token=None):
        self._check_allowed(method, path)
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode([(k, v) for k, v in params.items() if v is not None])
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, default=_json_default).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        status, raw = self._transport(method, url, headers, data, self.timeout)
        payload = None
        if raw:
            text = raw.decode("utf-8", "replace")
            try:
                payload = json.loads(text, parse_float=Decimal)
            except ValueError:
                payload = {"_raw": text[:300]}
        log.debug("%s %s -> %s", method, path, status)
        return status, payload

    def _post_unauth(self, path, body):
        return self._send("POST", path, body=body)

    def _token(self):
        if not self._tokens:
            raise AuthError(0, "no_credentials", None, "this client has no API credentials (private endpoint)")
        return self._tokens.access_token()

    def ensure_token(self):
        """Renew the access token now if it is due (called before an order's pre-trade checks, so
        the order POST does not wait for a token refresh)."""
        if self._tokens:
            self._tokens.access_token()

    def _backoff(self, attempt):
        return min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))

    def _call(self, method, path, params=None, body=None, auth=False, idempotent=True):
        attempt = 0
        reauthed = False
        while True:
            attempt += 1
            token = self._token() if auth else None
            try:
                status, payload = self._send(method, path, params, body, token)
            except TransportError as e:
                if idempotent and attempt <= self.max_retries:
                    w = self._backoff(attempt)
                    log.warning("%s %s: %s; retry in %.1fs", method, path, e, w)
                    self._sleep(w)
                    continue
                raise
            if status == 401 and auth and not reauthed:
                reauthed = True
                self._tokens.invalidate_access()
                continue
            if status == 429 and attempt <= self.max_retries:
                w = throttle_wait(payload, self._backoff(attempt), self.throttle_cap)
                log.warning("%s %s throttled; waiting %.1fs", method, path, w)
                self._sleep(w)
                continue
            if status >= 500 and idempotent and attempt <= self.max_retries:
                w = self._backoff(attempt)
                log.warning("%s %s: HTTP %s; retry in %.1fs", method, path, status, w)
                self._sleep(w)
                continue
            if 300 <= status < 400:
                raise BitpinAPIError(status, "redirect", None,
                                     "refusing to follow HTTP %s redirect from %s %s" % (status, method, path))
            if status >= 400:
                raise _api_error(status, payload, path)
            return payload

    def _get_list(self, path, params=None, auth=False, max_pages=10):
        """GET a list endpoint; accepts a bare list or a paginated {"results": [...], "next": url}."""
        out = []
        for _ in range(max_pages):
            payload = self._call("GET", path, params, auth=auth)
            if isinstance(payload, list):
                return out + payload
            if not isinstance(payload, dict) or "results" not in payload:
                raise BitpinAPIError(200, "bad_response", payload, "unexpected list response from %s" % path)
            out.extend(payload["results"] or [])
            nxt = payload.get("next")
            if not nxt:
                return out
            u = urllib.parse.urlparse(nxt)
            base = urllib.parse.urlparse(self.base_url)
            # Only the path + query of `next` are used; the request always goes to self.base_url
            # (https). So a server behind a TLS proxy that emits http:// next links is fine, but a
            # link to another host is refused.
            if u.netloc and (u.hostname or "").lower() != (base.hostname or "").lower():
                raise BitpinAPIError(200, "bad_next", None, "refusing to follow pagination to another host")
            path = u.path
            if path.startswith("/v1/"):
                # Bitpin serves the same API under /v1/ and /api/v1/; a `next` link may use either.
                # Map it to the /api/v1/ form (still subject to the allow-list in _send).
                path = "/api" + path
            params = dict(urllib.parse.parse_qsl(u.query))
        log.warning("%s: stopped after %d pages", path, max_pages)
        return out

    # ---- public market data
    def markets(self):
        return self._get_list("/api/v1/mkt/markets/")

    def tickers(self):
        return self._get_list("/api/v1/mkt/tickers/")

    def commissions(self):
        return self._get_list("/api/v1/mkt/commissions/")

    def orderbook(self, symbol):
        return self._call("GET", "/api/v1/mth/orderbook/%s/" % _sym(symbol))

    def matches(self, symbol):
        return self._call("GET", "/api/v1/mth/matches/%s/" % _sym(symbol))

    # ---- private: balances (read-only)
    def wallets(self, assets=None, service="main", limit=200):
        params = {"service": service, "limit": limit}
        if assets:
            params["assets"] = ",".join(sorted({a.upper() for a in assets}))
        return self._get_list("/api/v1/wlt/wallets/", params, auth=True)

    # ---- private: orders
    def get_order(self, order_id):
        return self._call("GET", "%s%s/" % (ORDERS_PATH, _oid(order_id)), auth=True)

    def list_orders(self, identifier=None, state=None, symbol=None, limit=None):
        params = {"identifier": identifier, "state": state, "symbol": _sym(symbol) if symbol else None,
                  "limit": int(limit) if limit else None}
        return self._get_list(ORDERS_PATH, params, auth=True)

    def find_order_by_identifier(self, identifier):
        """Exact client-side match on `identifier` (never trust the server-side filter alone:
        if it were ignored we would otherwise pick up an unrelated order)."""
        for state in (None, "closed", "active", "initial"):
            for o in self.list_orders(identifier=identifier, state=state):
                if isinstance(o, dict) and str(o.get("identifier")) == str(identifier):
                    return o
        return None

    def open_orders(self, symbol=None):
        """The account's open orders (state initial/active), all markets or one. This includes orders
        the bot did not place: callers filter by their own identifiers and never touch the rest."""
        sym = _sym(symbol) if symbol else None
        rows = self.list_orders(state="active", symbol=sym, limit=100)
        return [o for o in rows if isinstance(o, dict) and o.get("state") in OPEN_ORDER_STATES
                and (sym is None or str(o.get("symbol") or "").upper() == sym)]

    def cancel_order(self, order_id):
        """DELETE an order. Returns True if the cancel was accepted (HTTP 2xx; Bitpin then closes the
        order asynchronously, and it may still fill in between), False if the order no longer
        exists (HTTP 404). Any other error (e.g. 406 'not allowed' for an order that just closed)
        is raised."""
        try:
            self._call("DELETE", "%s%s/" % (ORDERS_PATH, _oid(order_id)), auth=True)
            return True
        except BitpinAPIError as e:
            if e.status == 404:
                return False
            raise

    def fills(self, symbol=None, limit=None):
        return self._get_list("/api/v1/odr/fills/", {"symbol": _sym(symbol) if symbol else None, "limit": limit},
                              auth=True)

    def place_order(self, symbol, side, type="market", base_amount=None, quote_amount=None, price=None,
                    identifier=None):
        """Submit one order: type "market" (base_amount or quote_amount) or "limit" (price and
        base_amount). Never re-POSTs after an ambiguous failure: looks the order up by `identifier`
        instead and raises OrderStatusUnknown if it cannot be found. (stop_limit / oco are not
        supported on purpose.)"""
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if type not in ("market", "limit"):
            raise ValueError("only market and limit orders are supported")
        if base_amount is None and quote_amount is None:
            raise ValueError("base_amount or quote_amount is required")
        if type == "limit" and (price is None or base_amount is None or quote_amount is not None):
            raise ValueError("limit orders need a price and a base_amount (no quote_amount)")
        if type == "market" and price is not None:
            raise ValueError("market orders take no price")
        identifier = identifier or str(uuid.uuid4())
        body = {"symbol": _sym(symbol), "type": type, "side": side, "identifier": identifier}
        for k, v in (("base_amount", base_amount), ("quote_amount", quote_amount), ("price", price)):
            if v is not None:
                v = Decimal(str(v))
                if not v.is_finite() or v <= 0:
                    raise ValueError("%s must be a positive number" % k)
                body[k] = format(v, "f")
        progress = {"sent": False}
        try:
            return self._place_order_loop(symbol, side, body, identifier, progress)
        except BitpinError:
            raise
        except Exception as e:  # noqa: BLE001
            # Anything unexpected AFTER the first POST (e.g. a ValueError from a lookup that the
            # endpoint allow-list refused) must never look like "not sent" to the caller.
            if not progress["sent"]:
                raise
            raise OrderStatusUnknown(identifier, "unexpected error after the order POST (%s: %s); the order may "
                                                 "exist - do not resubmit" % (e.__class__.__name__, e)) from e

    def _place_order_loop(self, symbol, side, body, identifier, progress):
        reauthed = False
        throttles = 0
        first_throttle_at = None
        started = self._clock()
        while True:
            # Everything that happened before this point was a definite rejection (401/429) or
            # nothing at all, so a failure to get a token means the order was NOT sent.
            try:
                token = self._token()
            except (AuthError, AuthBudgetExceeded):
                raise
            except BitpinError as e:
                raise OrderNotSent(identifier, "order not sent: could not obtain an access token (%s)" % e)
            took = self._clock() - started
            if took > self.order_throttle_max_wait:
                # a token refresh / re-authentication was throttled: a MARKET order must not go out
                # minutes after the runner's pre-trade price and slippage checks
                raise OrderNotSent(identifier, "order not sent: obtaining an access token took %.0fs, longer than "
                                               "order_throttle_max_wait %.0fs; it will be re-planned"
                                   % (took, self.order_throttle_max_wait))
            progress["sent"] = True
            try:
                status, payload = self._send("POST", ORDERS_PATH, body=body, token=token)
            except TransportError as e:
                return self._resolve_ambiguous(identifier, "POST order failed: %s" % e)
            if 200 <= status < 300:
                if not isinstance(payload, dict) or "id" not in payload:
                    return self._resolve_ambiguous(identifier, "unexpected order response: %s" % _short(payload))
                log.info("order accepted: %s %s %s id=%s identifier=%s", side, symbol,
                         body.get("base_amount") or body.get("quote_amount"), payload.get("id"), identifier)
                return payload
            if status >= 500 or 300 <= status < 400:
                return self._resolve_ambiguous(identifier, "POST order returned HTTP %s" % status)
            if status == 401 and not reauthed:  # rejected before processing; safe to retry once
                reauthed = True
                self._tokens.invalidate_access()
                continue
            if status == 429:
                # Throttled = rejected before processing. Re-send only within a short budget: the
                # runner vetted price/slippage just before the first POST, and a market order has
                # no price limit of its own.
                now = self._clock()
                if first_throttle_at is None:
                    first_throttle_at = now
                w = throttle_wait(payload, self._backoff(throttles + 1), self.throttle_cap)
                if throttles >= self.max_retries or (now - first_throttle_at) + w > self.order_throttle_max_wait:
                    found = self._lookup_quiet(identifier)
                    if found is not None:
                        return found
                    raise OrderNotSent(identifier, "order not sent: throttled (HTTP 429) for %.0fs, longer than "
                                                   "order_throttle_max_wait %.0fs; it will be re-planned on a later bar"
                                       % (now - first_throttle_at + w, self.order_throttle_max_wait))
                throttles += 1
                self._sleep(w)
                found = self._lookup_quiet(identifier)
                if found is not None:
                    return found
                if self._clock() - first_throttle_at > self.order_throttle_max_wait:
                    raise OrderNotSent(identifier, "order not sent: throttled (HTTP 429) and the identifier lookup "
                                                   "took past order_throttle_max_wait %.0fs" % self.order_throttle_max_wait)
                continue
            raise _api_error(status, payload, ORDERS_PATH)

    def _lookup_quiet(self, identifier):
        try:
            return self.find_order_by_identifier(identifier)
        except Exception as e:  # noqa: BLE001 - any lookup failure means "not found yet", never "not sent"
            log.warning("order lookup by identifier failed: %s: %s", type(e).__name__, e)
            return None

    def _resolve_ambiguous(self, identifier, why):
        log.warning("%s; looking up identifier %s instead of resubmitting", why, identifier)
        delay = self.order_lookup_delay
        for _ in range(self.order_lookup_attempts):
            self._sleep(delay)
            delay *= 2
            found = self._lookup_quiet(identifier)
            if found is not None:
                log.info("order %s found after ambiguous submit (id=%s, state=%s)", identifier, found.get("id"),
                         found.get("state"))
                return found
        raise OrderStatusUnknown(identifier, "%s; order with identifier %s not found after %d lookups. "
                                             "It may still exist - do not resubmit." % (why, identifier,
                                                                                         self.order_lookup_attempts))


def _api_error(status, payload, path=None):
    """AuthError (fatal for the bot) only for a credential rejection: code api_credential_wrong,
    or a 406 from the authenticate/refresh endpoints. Any other 4xx (including a 406 with another
    code on e.g. the orders endpoint) is an ordinary BitpinAPIError."""
    code = payload.get("code") if isinstance(payload, dict) else None
    if code == "api_credential_wrong" or (status == 406 and path in (AUTH_PATH, REFRESH_PATH)):
        return AuthError(status, code, payload)
    return BitpinAPIError(status, code, payload)


def _sym(symbol):
    s = str(symbol).strip().upper()
    if not re.match(r"^%s$" % _SYM, s):
        raise ValueError("bad symbol %r" % (symbol,))
    return s


def _oid(order_id):
    s = str(order_id).strip()
    if not re.match(r"^[0-9A-Za-z_-]+$", s):
        raise ValueError("bad order id %r" % (order_id,))
    return s
