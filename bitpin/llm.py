"""Client for an OpenAI-compatible chat-completions API (Moonshot AI / Kimi, OpenRouter, or another
OpenAI-compatible host). Stdlib only, 3.8+.

* Endpoint: POST {base_url}/chat/completions. Default base_url https://api.moonshot.ai/v1
  (international); the China platform is https://api.moonshot.cn/v1 (its keys only work there);
  OpenRouter is https://openrouter.ai/api/v1 (see PROVIDERS below).
* The model id is REQUIRED in the config - there is no guessed default. `list_models()` calls
  GET {base_url}/models so the user can pick one; `list_models_detailed()` adds the name, context
  length, prices per million tokens and JSON / reasoning support where the API tells them (OpenRouter).
* The API key comes ONLY from the environment (KIMI_API_KEY, or the variable named by
  `api_key_env`, which must look like KIMI_* / MOONSHOT_* for Moonshot and OPENROUTER_* for OpenRouter,
  so a Bitpin secret can never be sent to an LLM API and a key only goes to the platform that issued
  it). It is sent only in the Authorization header, never logged, never put into an
  exception message, and `redact()` scrubs it from any text the caller wants to persist.
* PROVIDERS (llm.provider, default "auto"; bitpin.news.detect_provider, re-exported here): "auto"
  picks the API dialect from the base_url host - openrouter.ai -> "openrouter", api.moonshot.ai /
  api.moonshot.cn -> "moonshot", any other host -> "openai" (a generic OpenAI-compatible API, sent
  exactly what Moonshot gets). An explicit value is for a relay host auto cannot recognise; one that
  contradicts a known host is refused. MOONSHOT behaviour is unchanged. OPENROUTER: the reasoning
  effort goes as "reasoning": {"effort": "low" | "medium" | "high"} ("max" -> "high") instead of the
  top-level reasoning_effort; every request carries "usage": {"include": true} (the token usage and
  the billed cost in the reply; the cost meter records that cost, bitpin.news.provider_cost) and the
  X-Title attribution header; web_search=True uses OpenRouter's web plugin ({"id": "web"}: one
  request, no tool loop - Moonshot's builtin $web_search does not exist there); the reasoning arrives
  as delta.reasoning / delta.reasoning_details and is kept like Moonshot's reasoning_content; SSE
  comment lines (": OPENROUTER PROCESSING") are skipped and an error chunk inside the stream is a
  cut stream (retried); HTTP 402 (no credits) is an exhausted quota exactly like Moonshot's 429
  (kind "llm_quota", logged {"error": "llm_quota"}; a generic host too, Moonshot's own 402 handling is
  unchanged); an error body {"error": {"code", "message",
  "metadata": {"raw", "provider_name"}}} is turned into its message, [code] and the upstream provider's
  own message. The thinking-model rules (THINKING_DEFAULTS: temperature not sent ...) are keyed on the
  model's base name, a vendor prefix stripped ("moonshotai/kimi-k3" -> "kimi-k3", model_base_name);
  other models get temperature and max_tokens as configured. An OpenRouter key (sk-or-...) found in the
  variable of another platform is never used (bitpin.news.key_mismatch).
* JSON mode: response_format {"type": "json_object"}. Web search: Moonshot's builtin tool
  {"type": "builtin_function", "function": {"name": "$web_search"}}. When the model answers with
  finish_reason == "tool_calls", the assistant turn is echoed back in the format VERIFIED on the
  server (bitpin.news.assistant_echo: content "" instead of null, each call only id / type /
  function {name, arguments unchanged}; no index, no reasoning_content) and every $web_search call
  gets a {"role": "tool", "tool_call_id", "name": "$web_search", ...} message whose content is the
  call's `arguments` string as-is (Moonshot runs the search server-side). After `max_tool_rounds`
  rounds (or when one round's prompt grew beyond `max_prompt_tokens_per_call`) the tools are
  withdrawn and the model is told to answer now, so the searches already paid for are not thrown
  away. Verified on the server: kimi-k2.6 can search; kimi-k3 cannot (HTTP 400 "tokenization
  failed" in round 2), so the decision model runs without tools and the news comes from
  bitpin/news.py (stage 1).
* Thinking models (kimi-k3...): when llm.temperature / max_tokens / timeout / deadline_seconds are
  ABSENT from the config, THINKING_DEFAULTS apply (temperature not sent, 32000 tokens, 300 s / 540 s);
  an explicit value always wins. temperature null = not sent.
* chat(model=..., max_tokens=...) overrides the configured model / max_tokens for one call (the
  budget is shared).
* If the API rejects response_format together with tools (HTTP 400 whose message mentions
  response_format / json / tools) the call is retried without response_format and the caller
  extracts the JSON object from the text. The client remembers this for later calls. Any other
  HTTP 400 is an error and changes nothing.
* Time: one chat() never runs longer than `deadline_seconds` (or the caller's `time_limit`), retries
  and tool rounds included; each request's timeout is cut to the time left, and the default
  transport enforces it on the whole response (a slowly trickling proxy cannot stretch it). A POST
  that timed out is retried at most `max_timeout_retries` times per call (Moonshot may already have
  processed and billed it). `abort()` (e.g. the STOP kill-switch check) is polled before every request.
  The default transport's `timeout` is a HARD wall-clock limit of one request (DNS, connect,
  headers and body run in a worker thread; bitpin.news.make_news_transport), and each request's
  timeout is cut to the time left minus one second, so the last request cannot overrun the deadline.
* 429 / 5xx / network errors - http.client.RemoteDisconnected ("Remote end closed connection without
  response": the Kimi proxy drops connections now and then; urllib raises it unwrapped),
  ConnectionResetError, http.client.IncompleteRead, socket.timeout, URLError, from the default
  transport or raised raw by any injected transport: up to `max_retries` retries with exponential
  backoff (backoff_seconds 3, 6, 12 ...), inside the deadline. A timed-out POST counts against
  `max_timeout_retries`. 401 / 403 -> LLMAuthError. An exhausted-quota 429 fails immediately. An
  urllib HTTPError raised by a transport is handled like the returned status.
* Daily budget per UTC day: `max_calls_per_day` chat() calls (one call, however many tool rounds)
  and `max_tokens_per_day` total tokens, persisted in state_dir/kimi_budget.json (in memory only when
  state_dir is None - then a restart resets it). Every HTTP request's token usage is appended to
  state_dir/kimi_usage.jsonl. Per call, `max_tokens_per_call` bounds the tokens of all its rounds
  together (search results are re-sent every round): beyond it the model must answer without
  searching further.
* Proxy: ONLY an explicitly configured proxy is used - the environment variable KIMI_HTTPS_PROXY
  (wins) or llm.proxy, an http:// or https:// URL such as http://127.0.0.1:1081. Without one every
  request goes DIRECT: http_proxy / https_proxy / HTTPS_PROXY / ALL_PROXY set system-wide are never
  picked up, and no_proxy / NO_PROXY (or the Windows registry) can never send Kimi traffic around
  a configured proxy (bitpin.news.StrictProxyHandler with an explicit mapping). Proxy credentials
  (user:pass@) are never logged and are scrubbed by `redact()`. Bitpin traffic never uses this module.
* `reasoning_content` (thinking models) is RETURNED as res["reasoning"] (the deltas of every round
  joined, redacted, capped at REASONING_MAX_CHARS) so the brain can keep the model's thinking in
  state_dir/decisions/<decided_at_utc>.reasoning.txt (KimiBrain._write_reasoning / _prune_reasoning). It is
  never parsed for the answer, never echoed back into a prompt (assistant_echo copies content and
  tool calls only), never written to the usage log and never logged to journald.
* llm.reasoning_effort (null | "low" | "high" | "max"; on OpenRouter also "medium"): kimi-k3's thinking
  depth, sent top-level in the request body when set (the API default is "max"; OpenRouter gets
  "reasoning": {"effort": ...} with "max" sent as "high"); chat(reasoning_effort=...) overrides it for
  one call (brain.slot_reasoning_effort lets the 13:00 slot think at "max" while wake-ups use the
  cheaper configured level). A `thinking` key (the kimi-k2.x parameter, refused by kimi-k3) is
  rejected at startup with a message that names reasoning_effort.
* COST METER (bitpin.spend): every usage record carries "usd", priced with llm.price_in_per_m /
  price_out_per_m / price_cached_in_per_m (kimi-k3 defaults 3 / 15 / 0.30 USD per million tokens;
  cached input from the usage's cached_tokens), and after every request state_dir/llm_spend.json is
  refreshed (today / month / total USD, the exhausted-quota streak for the notifier's alert). An
  exhausted-quota 429 is logged as {"error": "llm_quota"} so two in a row can be alerted.
* STREAMING (llm.stream, default true): chat requests are sent with "stream": true (plus
  "stream_options": {"include_usage": true}) and the server-sent events are rebuilt into the normal
  reply by bitpin.news.parse_chat_stream, so bytes keep flowing while kimi-k3 reasons - the tunnel
  drops connections that stay silent for about 2 minutes (a 125 s silent call was cut on 2026-09-23).
  The deadline, timeouts, retries, budgets and proxy isolation are unchanged. A stream cut before its
  end or carrying an error event is a network error (retried; charged to the budget as a lost reply
  when it was already generating). reasoning_content deltas are joined into res["reasoning"] (and
  counted for the usage estimate). A reply without usage gets an ESTIMATE (usage_estimated=True in
  the result), charged to the daily token budget.
  When the API refuses streaming (HTTP 400) the client falls back once, for the life of the process
  (bitpin.news.StreamPolicy); llm.stream=false never streams.
"""
import http.client
import json
import logging
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .api import TransportError, atomic_write_json
# news.py imports nothing from bitpin but spend.py, which imports only api.py (no cycle): the verified
# $web_search echo format, the proxy handler that never consults no_proxy, and the hard-time-limit
# transport are shared with stage 1
from .news import (STREAM_MAX_RESPONSE_BYTES, StreamError, StreamPolicy, StrictProxyHandler, assistant_echo,
                   check_base_url, estimate_usage, looks_like_sse, make_news_transport, parse_chat_stream,
                   stream_char_counts, tool_message)
from .news import _CallError as _NewsCallError
# the provider rules are shared with stage 1 (re-exported: from bitpin.llm import detect_provider ...)
from .news import (KEY_ENV_PREFIXES, MOONSHOT_HOSTS, OPENROUTER_BASE_URL, OPENROUTER_TITLE,  # noqa: F401
                   PROVIDER_CHOICES, PROVIDERS, check_key_env, check_provider, detect_provider, key_mismatch,
                   openrouter_reasoning, provider_cost, provider_of_host, upstream_error_text)
from .spend import cost_usd, prices_from_config
from .spend import refresh as refresh_spend

log = logging.getLogger("bitpin.llm")

DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"
CHINA_BASE_URL = "https://api.moonshot.cn/v1"
# MOONSHOT_HOSTS (imported from bitpin.news): the hosts of Moonshot's two platforms (set-model refuses any
# other --base-url host without --any-host: its --list / --test would send the Kimi key there at once)
API_KEY_ENV = "KIMI_API_KEY"
PROXY_ENV = "KIMI_HTTPS_PROXY"      # the ONLY proxy setting (besides llm.proxy); used for LLM traffic only
# Any variable name one of the providers accepts; validate_llm_config checks the name against the resolved
# provider (bitpin.news.check_key_env: KIMI_* / MOONSHOT_* for Moonshot, OPENROUTER_* for OpenRouter)
API_KEY_ENV_RE = re.compile(r"^(KIMI|MOONSHOT|OPENROUTER|OPENAI)_[A-Z0-9_]*$")
USER_AGENT = "bitpin-bot-kimi/1.0"
BUDGET_FILE = "kimi_budget.json"
USAGE_LOG = "kimi_usage.jsonl"
WEB_SEARCH_TOOL = {"type": "builtin_function", "function": {"name": "$web_search"}}
WEB_SEARCH_NAME = "$web_search"
MIN_REQUEST_SECONDS = 10.0          # no new request is started with less time than this left
TRANSPORT_OVERRUN_SECONDS = 1.0     # a request's timeout is cut to (time left - this): the hard limit + grace fits
# A POST that dies this long after it was sent was almost certainly received - and billed - by
# Moonshot: the tunnel dropped a connection that was waiting for a reply. Such a drop is treated like
# a timeout (it counts against max_timeout_retries) and its estimated tokens are charged to the daily
# budget, which otherwise only counts the usage of replies that arrived.
LOST_REPLY_SECONDS = 30.0
# v3.5.2: a STREAMED POST that got no answer at all (not even a status line) is retried up to this many
# more times on top of max_timeout_retries, inside the deadline (still charged as a lost reply)
SILENT_EXTRA_RETRIES = 3
CHARS_PER_TOKEN = 3.5               # rough prompt-size estimate for a reply that never arrived
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
FINAL_ANSWER_NUDGE = ("SEARCH LIMIT REACHED: do not call any tool again. Using the market context and the search "
                      "results you already have, reply now with ONLY the JSON object in the required format.")
# kimi-k3's thinking depth (top-level request field "reasoning_effort"; the API default is "max").
# The K2.x "thinking" parameter is refused by kimi-k3 and therefore refused here at startup.
REASONING_EFFORTS = ("low", "high", "max")
REFUSED_THINKING_KEYS = ("thinking",)
# OpenRouter takes "reasoning": {"effort": "low" | "medium" | "high"}: "medium" is accepted there too, and
# Moonshot's "max" is sent as OpenRouter's highest level
OPENROUTER_REASONING_EFFORTS = ("low", "medium", "high", "max")
OPENROUTER_EFFORT = {"low": "low", "medium": "medium", "high": "high", "max": "high"}
OPENROUTER_WEB_PLUGIN = {"id": "web"}       # web_search=True on OpenRouter (Moonshot's builtin tool is not there)
MODEL_ID_MAX = 200                          # longer ids from GET /models are skipped (list_models_detailed)
MODEL_NAME_MAX = 200
# The model's reasoning kept for the owner's post-mortems: res["reasoning"] is capped at this many
# characters (32k reasoning tokens are about 120k characters; the cap only guards against a runaway),
# written by the brain as state_dir/decisions/<decided_at_utc>.reasoning.txt and pruned after
# REASONING_KEEP_DAYS (a year of daily files would otherwise pile up in the state directory).
REASONING_MAX_CHARS = 400000
REASONING_DIR = "decisions"
REASONING_SUFFIX = ".reasoning.txt"
REASONING_KEEP_DAYS = 60
_REASONING_TRUNCATED = "\n[reasoning truncated]"

DEFAULT_LLM_CONFIG = {
    "base_url": DEFAULT_BASE_URL,
    "provider": "auto",            # "auto" (by the base_url host) | "moonshot" | "openrouter" | "openai"
    "model": None,                 # REQUIRED; see list_models()
    "api_key_env": API_KEY_ENV,
    "temperature": 0.3,
    "max_tokens": 4096,
    "timeout": 120,
    "deadline_seconds": 480,
    "max_calls_per_day": 60,
    "max_tokens_per_day": 4000000,
    "max_prompt_tokens_per_call": 100000,
    "max_tokens_per_call": 250000,
    "max_retries": 3,
    "max_timeout_retries": 1,
    "backoff_seconds": 2.0,
    "backoff_max_seconds": 30.0,
    "max_tool_rounds": 6,
    "proxy": None,                 # e.g. "http://127.0.0.1:1081"; the env var KIMI_HTTPS_PROXY overrides it
    "stream": True,                # stream=true (SSE) so the tunnel never sees a silent connection
    "reasoning_effort": None,      # kimi-k3 thinking depth: null = not sent (API default "max"), "low" | "high" | "max"
    # USD per million tokens for the cost meter (bitpin.spend): kimi-k3 list prices 2026-09
    "price_in_per_m": 3.0,
    "price_out_per_m": 15.0,
    "price_cached_in_per_m": 0.30,
}
_NULLABLE = ("max_tokens", "model", "temperature", "max_tokens_per_day", "max_prompt_tokens_per_call",
             "max_tokens_per_call", "proxy", "reasoning_effort")
# Thinking models (measured on the server 2026-09-22): kimi-k3 answers in JSON mode only WITHOUT a
# temperature, and its reasoning tokens count against max_tokens (small values give empty replies).
# Applied by validate_llm_config only to keys ABSENT from the user's llm section.
THINKING_MODEL_PREFIXES = ("kimi-k3",)
THINKING_DEFAULTS = {"temperature": None, "max_tokens": 32000, "timeout": 300, "deadline_seconds": 540}

_FORBIDDEN_KEYS = ("api_key", "apikey", "key", "secret", "token", "authorization", "password")
_REQUIRED = object()


class ConfigError(ValueError):
    """A bad setting in kimi.json / the constructor arguments. Retrying cannot help: the caller
    should stop at startup (the runner maps it to exit status 78)."""


class LLMError(Exception):
    """Any failure talking to the LLM. Messages never contain the API key."""

    def __init__(self, message, status=None, kind="llm", detail=""):
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.detail = detail or ""     # the API's own (redacted) error message, if any


class LLMAuthError(LLMError):
    """401/403: wrong/expired key, key from the other region (moonshot.ai vs .cn), or blocked."""

    def __init__(self, message, status=None):
        super().__init__(message, status, "llm_auth")


class LLMBudgetExceeded(LLMError):
    def __init__(self, message):
        super().__init__(message, None, "llm_budget")


# --------------------------------------------------------------------------- helpers

def utc_day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),                       # OpenAI/Moonshot style keys
    re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}"),            # JWTs (Bitpin tokens)
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    # user:password@ in any URL, e.g. a KIMI_HTTPS_PROXY with credentials quoted back in an
    # exception message (same pattern as news._SECRET_PATTERNS; the host itself is kept)
    re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"),
]


def redact_text(text, secrets=()):
    """Remove known secrets and anything that looks like an API key / JWT / bearer token."""
    if not isinstance(text, str):
        return text
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "<redacted>")
    for rx in _SECRET_PATTERNS:
        text = rx.sub("<redacted>", text)
    return text


def _reject_constant(name):
    raise ValueError("non-finite number %s is not allowed" % name)


def _decoder():
    return json.JSONDecoder(parse_constant=_reject_constant)


_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```$", re.S)


def parse_json_object_strict(text):
    """The whole reply must be ONE JSON object (surrounding whitespace or one ``` fence allowed).
    Returns the dict or None."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        obj = _decoder().decode(s)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def find_json_objects(text, required_key=None):
    """Every TOP-LEVEL JSON object embedded in `text` (objects nested inside another object are
    not listed separately), in order. With `required_key`, only objects having that key.
    NaN / Infinity literals make an object invalid."""
    if not isinstance(text, str) or not text.strip():
        return []
    dec = _decoder()
    s = text.strip()
    out = []
    try:
        obj = dec.decode(s)
        found = [obj] if isinstance(obj, dict) else []
    except ValueError:
        found = []
        i = 0
        while True:
            j = s.find("{", i)
            if j < 0:
                break
            try:
                obj, end = dec.raw_decode(s, j)
            except ValueError:
                i = j + 1
                continue
            if isinstance(obj, dict):
                found.append(obj)
                i = end
            else:
                i = j + 1
    for obj in found:
        if required_key is None or required_key in obj:
            out.append(obj)
    return out


def extract_json_object(text, required_key=None):
    """The LAST top-level JSON object in `text` (with `required_key`, the last one having that key),
    or None. Accepts a bare object, one wrapped in ``` fences, or one embedded in prose. The last one
    is used because a model that quotes something (e.g. an allocation it read on a web page) does so
    before giving its own answer; callers that need certainty use find_json_objects() and reject
    replies with more than one candidate."""
    objs = find_json_objects(text, required_key)
    return objs[-1] if objs else None


def _short(text, n=300):
    text = text if isinstance(text, str) else repr(text)
    return text if len(text) <= n else text[:n] + "..."


def _add_usage(total, usage):
    for k, v in (usage or {}).items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
        elif isinstance(v, dict):  # e.g. prompt_tokens_details
            sub = total.setdefault(k, {})
            if isinstance(sub, dict):
                _add_usage(sub, v)
    return total


def check_number(name, value, lo=None, hi=None, integer=False, allow_none=False, lo_open=False):
    """Validate one numeric setting (JSON number, not a string or bool). Returns int/float."""
    if value is None:
        if allow_none:
            return None
        raise ConfigError("%s must be a number, got null" % name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s must be a number (without quotes), got %r" % (name, value))
    try:
        v = float(value)
    except OverflowError:
        raise ConfigError("%s is out of range" % name)
    if not math.isfinite(v):
        raise ConfigError("%s must be finite, got %r" % (name, value))
    if integer:
        if v != int(v):
            raise ConfigError("%s must be a whole number, got %r" % (name, value))
        v = int(v)
    if lo is not None and (v < lo or (lo_open and v == lo)):
        raise ConfigError("%s=%r is too small (must be %s %s)" % (name, value, ">" if lo_open else ">=", lo))
    if hi is not None and v > hi:
        raise ConfigError("%s=%r is too large (max %s)" % (name, value, hi))
    return v


def check_bool(name, value):
    if not isinstance(value, bool):
        raise ConfigError("%s must be true or false (without quotes), got %r" % (name, value))
    return value


def ensure_writable_dir(path, what="state_dir"):
    """Create `path` if needed and prove that files can be written there (fail at startup, not at
    the first decision). Raises ConfigError."""
    if not isinstance(path, str) or not path.strip():
        raise ConfigError("%s must be a directory path, got %r" % (what, path))
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test.%d" % os.getpid())
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        raise ConfigError("%s %s is not writable (%s). Pass an absolute, writable state directory "
                          "(the service uses /var/lib/bitpin-bot)." % (what, os.path.abspath(path), e.strerror or e))
    return path


# --------------------------------------------------------------------------- transport

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would re-send the Authorization header to the new URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_proxy_url(value, source="llm.proxy"):
    """Validate a proxy setting. Returns the normalised URL ("http://host:port" style) or None when
    empty. Only http:// and https:// proxies are supported (urllib has no SOCKS support); a bare
    host:port means http://host:port. Raises ConfigError with a clear message."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("%s must be a proxy URL string like \"http://127.0.0.1:1081\" or null, got %r"
                          % (source, value))
    p = value.strip()
    if not p:
        return None
    if "://" not in p:
        p = "http://" + p
    u = urllib.parse.urlsplit(p)
    scheme = (u.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ConfigError("%s must be an http:// or https:// proxy URL like http://127.0.0.1:1081 (got scheme %r; "
                          "SOCKS and other proxy types are not supported)" % (source, u.scheme))
    try:
        port = u.port
    except ValueError:
        raise ConfigError("%s has an invalid port: %s" % (source, mask_proxy_url(p)))
    if not u.hostname or not port:
        raise ConfigError("%s must look like http://127.0.0.1:1081 (a host and a port), got %s"
                          % (source, mask_proxy_url(p)))
    if (u.path or "") not in ("", "/") or u.query or u.fragment:
        raise ConfigError("%s must be just scheme://host:port (no path or query), got %s" % (source, mask_proxy_url(p)))
    return p.rstrip("/")


def mask_proxy_url(url):
    """Proxy URL for logs and messages: user:password@ is replaced by ***@."""
    if not url:
        return "none (direct)"
    try:
        u = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
        host = u.hostname or "?"
        try:
            port = ":%d" % u.port if u.port else ""
        except ValueError:
            port = ":?"
        cred = "***@" if (u.username is not None or u.password is not None or "@" in (u.netloc or "")) else ""
        return "%s://%s%s%s" % (u.scheme or "http", cred, host, port)
    except Exception:  # noqa: BLE001 - never leak the raw value
        return "<proxy>"


def proxy_secrets(url):
    """Strings to scrub from any text: the proxy URL itself and its password (if it has credentials)."""
    if not url or "@" not in url:
        return []
    out = [url]
    try:
        u = urllib.parse.urlsplit(url)
        for part in (u.password, u.username, (u.netloc or "").rsplit("@", 1)[0]):
            if part and len(part) >= 3:
                out.append(part)
                out.append(urllib.parse.unquote(part))
    except Exception:  # noqa: BLE001
        pass
    return out


def build_llm_opener(proxy=None):
    """urllib opener for the LLM API: through `proxy` when given, else DIRECT. Environment proxy
    variables (http_proxy / https_proxy / ALL_PROXY, and on Windows the registry) are never used,
    and no_proxy / NO_PROXY can never bypass a configured proxy: the explicit StrictProxyHandler
    (bitpin.news) replaces urllib's default one and never calls proxy_bypass(). Redirects are never
    followed."""
    ph = StrictProxyHandler({"https": proxy, "http": proxy} if proxy else {})
    return urllib.request.build_opener(ph, _NoRedirect)


def _as_transport_error(e, via="", secrets=()):
    """A raw network exception (http.client.RemoteDisconnected - raised unwrapped by urllib's
    getresponse() when the Kimi proxy drops the connection -, ConnectionResetError,
    http.client.IncompleteRead, socket.timeout, URLError ...) as TransportError; timeouts keep
    timeout=True. The text names the exception type and is scrubbed of proxy credentials."""
    reason = getattr(e, "reason", None)
    to = isinstance(e, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError))
    if isinstance(e, urllib.error.URLError) and isinstance(reason, BaseException):
        text = "%s: %s" % (type(reason).__name__, reason)     # e.g. ConnectionRefusedError under a URLError
    else:
        text = "%s: %s" % (type(e).__name__, e)
    return TransportError(redact_text("%s%s: %s" % ("timeout" if to else "network error", via, text), list(secrets)),
                          timeout=to)


def _http_error_body(he):
    """The (capped) body of an urllib HTTPError, closed afterwards; b"" when it has none."""
    try:
        raw = he.read(1024 * 1024) or b""
    except Exception:  # noqa: BLE001 - HTTPError without a body (fp=None on older Pythons)
        raw = b""
    try:
        he.close()
    except Exception:  # noqa: BLE001
        pass
    return raw if isinstance(raw, bytes) else b""


def make_llm_transport(proxy=None, opener=None, max_bytes=STREAM_MAX_RESPONSE_BYTES):
    """transport(method, url, headers, body, timeout) -> (status, body_bytes) for LLMClient; raises
    TransportError. Routed through `proxy` (already validated) or direct, never through environment
    proxy variables (no_proxy included). `timeout` is a HARD wall-clock limit of the WHOLE request:
    bitpin.news.make_news_transport runs it in a worker thread (DNS, connect, trickled headers and
    the body; each read is cut to the time left) and gives up after timeout + 0.5 s, so neither a
    slowly trickling server nor a stalled proxy can stretch it. Every network exception
    (RemoteDisconnected, ConnectionResetError, IncompleteRead, URLError, socket.timeout ...) becomes
    TransportError (timeout=True for timeouts). `opener`: for tests. max_bytes: the response size cap
    (streamed replies carry every reasoning token as its own event: megabytes for kimi-k3)."""
    op = opener if opener is not None else build_llm_opener(proxy)
    inner = make_news_transport(proxy, opener=op, max_bytes=max_bytes)
    via = " (via proxy %s)" % mask_proxy_url(proxy) if proxy else ""
    secrets = proxy_secrets(proxy)

    def transport(method, url, headers, body, timeout):
        try:
            return inner(method, url, headers, body, timeout)
        except TransportError:
            raise
        except urllib.error.HTTPError as e:        # normally returned as (status, body) by the inner transport
            return e.code, _http_error_body(e)
        except _NewsCallError as e:                # e.g. a response larger than the limit
            raise TransportError(redact_text("bad response%s: %s" % (via, e), secrets))
        except (http.client.HTTPException, OSError) as e:   # socket.timeout / URLError / ConnectionError are OSErrors
            te = _as_transport_error(e, via, secrets)
            te.after_headers = bool(getattr(e, "after_headers", False))
            te.no_answer = bool(getattr(e, "no_answer", False))      # v3.5.2: not one byte came back
            te.partial_body = getattr(e, "partial_body", None)      # what arrived before the cut (a stream)
            raise te

    transport.proxy = proxy
    transport.opener = op
    transport.max_bytes = getattr(inner, "max_bytes", max_bytes)
    return transport


# Direct (no proxy, environment proxy variables ignored). LLMClient builds its own transport with
# the configured proxy; this module-level one is kept for callers that want a direct connection.
llm_transport = make_llm_transport(None)


# --------------------------------------------------------------------------- daily budget

BUDGET_FORMAT = 2       # the day records this version writes carry "v": 2


def _rebase_day(rec):
    """A day record written by an OLDER version (no "v"): the live bot's hourly profile counted up to 60
    calls / 4M tokens a day; counted against the daily profile's 24 calls / 300k tokens that usage would
    block every Kimi call - the 13:00 slot, fill reviews, vetoes - until the next UTC day on the day of
    the upgrade. That day starts fresh (the old counts are kept under "legacy" for the record)."""
    rec = dict(rec) if isinstance(rec, dict) else {}
    if rec and "v" not in rec:
        return {"v": BUDGET_FORMAT, "legacy": {k: rec[k] for k in ("calls", "prompt_tokens", "completion_tokens",
                                                                   "total_tokens") if k in rec}}
    rec.setdefault("v", BUDGET_FORMAT)
    return rec


class DailyBudget:
    """Per-UTC-day counter of chat() calls and tokens. Persisted in `path`; with path=None it is
    kept in memory (still enforced, but reset by a restart). A day record of an older version (no
    "v") does not count (_rebase_day)."""

    def __init__(self, path, limit, clock=time.time, max_tokens=None):
        self.path, self.limit, self.clock = path, int(limit), clock
        self.max_tokens = int(max_tokens) if max_tokens else None
        self._mem = {}

    def _load(self):
        if not self.path:
            return {k: dict(v) for k, v in self._mem.items()}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d):
        if self.path:
            atomic_write_json(self.path, d)
        else:
            self._mem = {k: dict(v) for k, v in d.items()}

    def today(self):
        d = self._load()
        day = utc_day(self.clock())
        rec = _rebase_day(d.get(day) or {})
        return day, rec

    def used(self):
        return int(self.today()[1].get("calls", 0))

    def tokens_used(self):
        v = self.today()[1].get("total_tokens", 0)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    def remaining(self):
        return max(0, self.limit - self.used())

    def tokens_exhausted(self):
        return bool(self.max_tokens) and self.tokens_used() >= self.max_tokens

    def tokens_fraction(self):
        """Share of today's (UTC) token budget already used: 0.0 .. 1.0+ (0.0 without a token limit)."""
        return float(self.tokens_used()) / self.max_tokens if self.max_tokens else 0.0

    def consume(self):
        d = self._load()
        day = utc_day(self.clock())
        rec = _rebase_day(d.get(day) or {})
        if int(rec.get("calls", 0)) >= self.limit:
            raise LLMBudgetExceeded("daily LLM call budget exhausted (%d calls on %s UTC, max_calls_per_day=%d)"
                                    % (int(rec.get("calls", 0)), day, self.limit))
        tok = rec.get("total_tokens", 0)
        if self.max_tokens and isinstance(tok, (int, float)) and tok >= self.max_tokens:
            raise LLMBudgetExceeded("daily LLM token budget exhausted (%d tokens on %s UTC, max_tokens_per_day=%d)"
                                    % (tok, day, self.max_tokens))
        rec["calls"] = int(rec.get("calls", 0)) + 1
        # keep only the last 7 days
        keep = sorted(k for k in d if k != day)[-6:]
        out = {k: d[k] for k in keep}
        out[day] = rec
        self._save(out)
        return rec["calls"]

    def add_tokens(self, usage):
        if not usage:
            return
        d = self._load()
        day = utc_day(self.clock())
        rec = _rebase_day(d.get(day) or {})
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = usage.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                rec[k] = rec.get(k, 0) + v
        d[day] = rec
        self._save(d)


# --------------------------------------------------------------------------- client

def model_base_name(model):
    """The model id the model-specific rules are keyed on: lower case, without a vendor prefix -
    OpenRouter names models "vendor/model" ("moonshotai/kimi-k3" -> "kimi-k3", the name Moonshot's own
    API uses; a ":variant" suffix stays). "" for anything that is not a string."""
    if not isinstance(model, str):
        return ""
    return model.strip().lower().rsplit("/", 1)[-1].strip()


def is_thinking_model(model):
    """True for the thinking models whose settings differ (THINKING_MODEL_PREFIXES, e.g. kimi-k3), with
    or without a vendor prefix (model_base_name: "moonshotai/kimi-k3" on OpenRouter is kimi-k3)."""
    return model_base_name(model).startswith(THINKING_MODEL_PREFIXES)


def validate_llm_config(config):
    """Merge `config` over DEFAULT_LLM_CONFIG and check every value (type and range). For a
    thinking model (kimi-k3...) every THINKING_DEFAULTS key that is ABSENT from `config` gets the
    thinking default instead (no temperature, 32000 tokens, 300 s / 540 s): a server kimi.json that
    only switched "model" to kimi-k3 must not send temperature 0.3 / max_tokens 4096 (empty replies).
    An explicit value always wins. The thinking rule is keyed on the model's base name, so OpenRouter's
    "moonshotai/kimi-k3" gets it too. llm.provider is checked against the base_url host, api_key_env
    against the resolved provider (KIMI_* / MOONSHOT_* for Moonshot, OPENROUTER_* for OpenRouter) and
    reasoning_effort against what that API takes. Returns the merged dict; raises ConfigError."""
    cfg = dict(DEFAULT_LLM_CONFIG)
    if config is not None and not isinstance(config, dict):
        raise ConfigError("the llm config must be an object")
    # set explicitly (a null counts only for the nullable keys: elsewhere null means "the default")
    given = {k for k, v in (config or {}).items() if not str(k).startswith("_") and (v is not None or k in _NULLABLE)}
    model = (config or {}).get("model")
    if is_thinking_model(model):
        for k, v in THINKING_DEFAULTS.items():
            if k not in given:
                cfg[k] = v
    for k, v in (config or {}).items():
        if k.startswith("_"):
            continue
        if k.lower() in _FORBIDDEN_KEYS or "secret" in k.lower():
            raise ConfigError("config key %r refused: the API key must come from the environment variable %s"
                              % (k, cfg["api_key_env"]))
        if k.lower() in REFUSED_THINKING_KEYS:
            # the kimi-k2.x "thinking" parameter is rejected by kimi-k3 (HTTP 400): the depth of its
            # thinking is set by reasoning_effort only, so the wrong key fails at startup, not at 13:00
            raise ConfigError("llm config key %r refused: kimi-k3 does not accept the thinking parameter; set "
                              "llm.reasoning_effort to null, \"low\", \"high\" or \"max\" instead" % (k,))
        if k not in DEFAULT_LLM_CONFIG:
            raise ConfigError("unknown llm config key %r (known: %s)" % (k, sorted(DEFAULT_LLM_CONFIG)))
        if v is not None or k in _NULLABLE:
            cfg[k] = v
    check_base_url(cfg["base_url"], "llm.base_url", ConfigError)
    # the API dialect: "auto" = by the base_url host; an explicit value may not contradict a known host
    cfg["provider"] = check_provider(cfg["provider"], cfg["base_url"], "llm.provider", ConfigError)
    provider = detect_provider(cfg["base_url"], cfg["provider"])
    m = cfg["model"]
    if m is not None and (not isinstance(m, str) or not m.strip()):
        raise ConfigError("llm.model must be a model id string (in quotes) or null, got %r" % (m,))
    # the key variable must belong to the platform the key is sent to (KIMI_* / MOONSHOT_* -> Moonshot,
    # OPENROUTER_* -> OpenRouter): a Bitpin secret never, and a key never to another platform
    cfg["api_key_env"] = check_key_env(cfg["api_key_env"], provider, "llm.api_key_env", ConfigError)
    cfg["temperature"] = check_number("llm.temperature", cfg["temperature"], 0, 2, allow_none=True)
    cfg["max_tokens"] = check_number("llm.max_tokens", cfg["max_tokens"], 1, 262144, integer=True, allow_none=True)
    cfg["timeout"] = check_number("llm.timeout", cfg["timeout"], 5, 1800)   # v3.5: 600 -> 1800
    cfg["deadline_seconds"] = check_number("llm.deadline_seconds", cfg["deadline_seconds"], 30, 3600)
    cfg["max_calls_per_day"] = check_number("llm.max_calls_per_day", cfg["max_calls_per_day"], 0, 10000, integer=True)
    cfg["max_tokens_per_day"] = check_number("llm.max_tokens_per_day", cfg["max_tokens_per_day"], 1000, None,
                                             integer=True, allow_none=True)
    cfg["max_prompt_tokens_per_call"] = check_number("llm.max_prompt_tokens_per_call",
                                                     cfg["max_prompt_tokens_per_call"], 1000, None, integer=True,
                                                     allow_none=True)
    cfg["max_tokens_per_call"] = check_number("llm.max_tokens_per_call", cfg["max_tokens_per_call"], 1000, None,
                                              integer=True, allow_none=True)
    cfg["proxy"] = parse_proxy_url(cfg["proxy"], "llm.proxy")
    cfg["max_retries"] = check_number("llm.max_retries", cfg["max_retries"], 0, 10, integer=True)
    cfg["max_timeout_retries"] = check_number("llm.max_timeout_retries", cfg["max_timeout_retries"], 0, 5,
                                              integer=True)
    cfg["backoff_seconds"] = check_number("llm.backoff_seconds", cfg["backoff_seconds"], 0, 300)
    cfg["backoff_max_seconds"] = check_number("llm.backoff_max_seconds", cfg["backoff_max_seconds"], 0, 3600)
    cfg["max_tool_rounds"] = check_number("llm.max_tool_rounds", cfg["max_tool_rounds"], 0, 20, integer=True)
    check_bool("llm.stream", cfg["stream"])
    cfg["reasoning_effort"] = check_reasoning_effort(cfg["reasoning_effort"], "llm.reasoning_effort", provider)
    for k in ("price_in_per_m", "price_out_per_m", "price_cached_in_per_m"):
        cfg[k] = check_number("llm." + k, cfg[k], 0, 1000)
    return cfg


def check_reasoning_effort(value, name="llm.reasoning_effort", provider=None):
    """None, or one of REASONING_EFFORTS ("low" / "high" / "max", case-insensitive, normalised to
    lower case); on OpenRouter (provider "openrouter") also "medium" (OPENROUTER_REASONING_EFFORTS).
    Raises ConfigError for anything else (a number, "medium" for Moonshot, "" ...): the API would
    answer HTTP 400 at the first decision otherwise."""
    allowed = OPENROUTER_REASONING_EFFORTS if provider == "openrouter" else REASONING_EFFORTS
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    raise ConfigError("%s must be null or one of %s (in quotes), got %r" % (name, "/".join(allowed), value))


def _price_per_million(value):
    """USD per token (OpenRouter's pricing strings, e.g. "0.000003") -> USD per million tokens (3.0),
    rounded to 6 decimals. None for a missing, malformed, non-finite, negative (OpenRouter's "-1" = a
    variable price, e.g. openrouter/auto) or absurd (more than $1M per million tokens) value."""
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        v = float(value.strip() if isinstance(value, str) else value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    per_m = round(v * 1e6, 6)
    return per_m if math.isfinite(per_m) and per_m <= 1e6 else None


def _positive_int(value, hi=10 ** 9):
    """A positive whole number (an int, an integral float or a string of digits) up to `hi`, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 12:
        value = int(value.strip())
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        value = int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= hi else None


def _model_text(value, limit):
    """A display string from the API (a model name): characters that are not printable removed,
    whitespace joined, cut to `limit`; None when empty or not a string."""
    if not isinstance(value, str):
        return None
    s = " ".join("".join(ch if ch.isprintable() else " " for ch in value[:4 * limit]).split())
    return s[:limit] or None


def parse_model_list(payload):
    """The models of a GET /models reply as [{"id", "name", "context_length", "price_in_per_m",
    "price_out_per_m", "price_cached_in_per_m", "supports_json", "supports_reasoning"}], in the API's
    order. OpenRouter describes each model: name, context_length (or top_provider.context_length),
    pricing.prompt / completion / input_cache_read (USD per TOKEN, as strings) as USD per MILLION tokens,
    and from supported_parameters: supports_json ("response_format" or "structured_outputs") and
    supports_reasoning ("reasoning" or "include_reasoning"). A field the API does not give is None
    (Moonshot lists ids only). An entry without a usable id (missing, not a string, empty, blanks or
    unprintable characters inside, longer than MODEL_ID_MAX, or a repeat) is skipped; a malformed field
    becomes None. Never raises."""
    out, seen = [], set()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return out
    for m in data[:10000]:
        try:
            if not isinstance(m, dict) or not isinstance(m.get("id"), str):
                continue
            mid = m["id"].strip()
            if not mid or len(mid) > MODEL_ID_MAX or not mid.isprintable() or any(ch.isspace() for ch in mid) \
                    or mid in seen:
                continue
            pricing = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
            top = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
            params = m.get("supported_parameters")
            params = set(p for p in params if isinstance(p, str)) if isinstance(params, list) else None
            ctx = _positive_int(m.get("context_length"))
            rec = {"id": mid, "name": _model_text(m.get("name"), MODEL_NAME_MAX),
                   "context_length": ctx if ctx is not None else _positive_int(top.get("context_length")),
                   "price_in_per_m": _price_per_million(pricing.get("prompt")),
                   "price_out_per_m": _price_per_million(pricing.get("completion")),
                   "price_cached_in_per_m": _price_per_million(pricing.get("input_cache_read")),
                   "supports_json": None if params is None else bool(params & {"response_format",
                                                                               "structured_outputs"}),
                   "supports_reasoning": None if params is None else bool(params & {"reasoning",
                                                                                    "include_reasoning"})}
        except Exception:  # noqa: BLE001 - one odd entry never breaks the list
            continue
        seen.add(mid)
        out.append(rec)
    return out


class LLMClient:
    """OpenAI-compatible chat client for Moonshot/Kimi, OpenRouter and other OpenAI-compatible APIs.

    config: dict with the keys of DEFAULT_LLM_CONFIG (unknown keys are rejected, and a key
    literally holding a secret such as "api_key" is refused: keys come from the environment).
    state_dir: REQUIRED. A writable directory for the daily budget and the usage log; pass None
    explicitly only for tests / one-off scripts (the budget is then kept in memory only).
    transport(method, url, headers, body, timeout) -> (status, body_bytes), like BitpinClient.
    Default: make_llm_transport(self.proxy) - the proxy from $KIMI_HTTPS_PROXY (wins) or llm.proxy,
    else direct; environment proxy variables are never used. A transport passed in is used as is.
    provider: the API dialect of base_url (detect_provider): "moonshot", "openrouter" or "openai".
    """

    def __init__(self, config=None, state_dir=_REQUIRED, transport=None, env=None, sleep=time.sleep,
                 clock=time.time, monotonic=time.monotonic):
        cfg = validate_llm_config(config)
        if state_dir is _REQUIRED:
            raise TypeError("LLMClient needs state_dir (the bot's state directory, e.g. /var/lib/bitpin-bot), "
                            "or state_dir=None for an in-memory budget")
        self.cfg = cfg
        self.base_url = cfg["base_url"].strip().rstrip("/")
        self.model = cfg["model"].strip() if cfg["model"] else None
        # the API dialect (detect_provider): the shape of the requests, see the module docstring
        self.provider = detect_provider(self.base_url, cfg["provider"])
        env = os.environ if env is None else env
        key = (env.get(cfg["api_key_env"]) or "").strip() or None
        refused = key_mismatch(key, self.provider)
        # an OpenRouter key found in the variable of another platform is never sent there
        self._key_refused = ("the key in %s is not used: %s, and llm.base_url is %s"
                             % (cfg["api_key_env"], refused, self.base_url)) if refused else None
        if refused:
            log.warning("LLM key: %s", self._key_refused)
        self._api_key = None if refused else key
        self._refused_key = key if refused else None      # never sent, still scrubbed by redact()
        env_proxy = parse_proxy_url(env.get(PROXY_ENV), "the environment variable %s" % PROXY_ENV)
        self.proxy = env_proxy or cfg["proxy"]          # validated URL or None (= direct)
        self.proxy_source = PROXY_ENV if env_proxy else ("llm.proxy" if cfg["proxy"] else None)
        self._proxy_secrets = proxy_secrets(self.proxy)
        if env_proxy and cfg["proxy"] and env_proxy != cfg["proxy"]:
            log.warning("LLM proxy: %s (%s) overrides llm.proxy %s", mask_proxy_url(env_proxy), PROXY_ENV,
                        mask_proxy_url(cfg["proxy"]))
        self._transport = transport or make_llm_transport(self.proxy)
        if transport is None:
            log.info("LLM route: %s", ("proxy %s (from %s)" % (self.proxy_display, self.proxy_source)) if self.proxy
                     else "direct (environment proxy variables are ignored)")
        self._sleep, self._clock, self._mono = sleep, clock, monotonic
        self.state_dir = state_dir
        if state_dir is not None:
            ensure_writable_dir(state_dir)
        else:
            log.warning("LLMClient without state_dir: the daily budget is kept in memory only (reset by a restart)")
        self.budget = DailyBudget(os.path.join(state_dir, BUDGET_FILE) if state_dir else None,
                                  cfg["max_calls_per_day"], clock, cfg["max_tokens_per_day"])
        self._usage_path = os.path.join(state_dir, USAGE_LOG) if state_dir else None
        self._no_response_format_with_tools = False
        self._stream = StreamPolicy(cfg["stream"], "llm")
        self._prices = prices_from_config(cfg, "llm")     # USD per million tokens for the usage log's "usd"

    def __repr__(self):
        return "LLMClient(base_url=%r, model=%r, api_key=%s, proxy=%s)" % (
            self.base_url, self.model, "<set>" if self._api_key else "<not set>", self.proxy_display)

    @property
    def has_key(self):
        return bool(self._api_key)

    @property
    def proxy_display(self):
        """The proxy for logs: credentials masked; 'none (direct)' without a proxy."""
        return mask_proxy_url(self.proxy)

    @property
    def streaming(self):
        """True while chat requests are streamed (llm.stream and the API has not refused it)."""
        return self._stream.streaming

    @property
    def stream_note(self):
        """Why streaming is off or reduced ('' while it is fully on)."""
        return self._stream.note

    def redact(self, text):
        keys = [k for k in (self._api_key, self._refused_key) if k]
        return redact_text(text, keys + list(self._proxy_secrets))

    # ---- HTTP
    def _headers(self, with_body):
        if not self._api_key:
            raise LLMAuthError(self._key_refused or "no API key: set the environment variable %s"
                               % self.cfg["api_key_env"])
        h = {"Authorization": "Bearer " + self._api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
        if with_body:
            h["Content-Type"] = "application/json"
        if self.provider == "openrouter":
            h["X-Title"] = OPENROUTER_TITLE          # OpenRouter's app attribution (no Referer is sent)
        return h

    @staticmethod
    def _error_message(payload, raw):
        """The API's own error text: {"error": {"message", "type"}} (Moonshot / OpenAI) -> "message
        [type]"; OpenRouter's {"error": {"code", "message", "metadata": {"raw", "provider_name"}}} ->
        "message [code] (provider: the upstream provider's own message)"; else the start of the body."""
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                tag = err.get("type") or err.get("code")
                return "%s%s%s" % (err.get("message") or "", (" [%s]" % tag) if tag else "", upstream_error_text(err))
            if isinstance(err, str):
                return err
            if payload.get("message"):
                return str(payload["message"])
        return raw[:300] if raw else ""

    def _time_left(self, deadline):
        return None if deadline is None else deadline - self._mono()

    def _check_go(self, deadline, abort, what):
        """Raise instead of starting a request after the deadline or when abort() says so."""
        if abort is not None:
            reason = abort()
            if reason:
                raise LLMError("aborted before %s: %s" % (what, reason if isinstance(reason, str) else "abort requested"),
                               kind="llm_aborted")
        left = self._time_left(deadline)
        if left is not None and left < MIN_REQUEST_SECONDS:
            raise LLMError("LLM call deadline reached (%.0f s left) before %s" % (max(0.0, left), what),
                           kind="llm_timeout")
        return left

    def _wait(self, seconds, deadline, what):
        left = self._time_left(deadline)
        if left is not None and seconds + MIN_REQUEST_SECONDS > left:
            raise LLMError("LLM call deadline reached: no time left to retry %s" % what, kind="llm_timeout")
        self._sleep(seconds)

    def _request(self, method, path, body=None, deadline=None, abort=None, timeouts=None, stream=False):
        """One logical request with retries, inside `deadline` (monotonic seconds). Returns
        (status, payload). Raises LLMError. `timeouts`: [count] of POST timeouts already retried in
        this chat() call (shared across its requests). stream: the body asked for stream=true - a 2xx
        event stream is rebuilt into the normal reply (parse_chat_stream; "_streamed" set, and
        "_usage_estimated" when the usage had to be estimated); a cut stream is a network error."""
        url = self.base_url + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        retries = int(self.cfg["max_retries"])
        attempt = 0
        while True:
            attempt += 1
            left = self._check_go(deadline, abort, "%s %s" % (method, path))
            headers = self._headers(data is not None)
            # the last request must end before the deadline: the default transport returns at the
            # latest timeout + 0.5 s (hard limit), so its timeout is the time left minus 1 s
            timeout = float(self.cfg["timeout"]) if left is None else \
                min(float(self.cfg["timeout"]), left - TRANSPORT_OVERRUN_SECONDS)
            t_req = self._mono()
            streamed = smeta = None
            raw = None
            try:
                status, raw = self._transport(method, url, headers, data, timeout)
                if stream and 200 <= status < 300 and looks_like_sse(raw):
                    streamed, smeta = parse_chat_stream(raw)
            except urllib.error.HTTPError as he:
                # a transport that RAISES an HTTP error instead of returning it: handled like the
                # returned status below (401/403, 429, 5xx retries, other 4xx). Must come before
                # OSError: HTTPError is a URLError, which is an OSError.
                status, raw = he.code, _http_error_body(he)
            except (TransportError, http.client.HTTPException, OSError, StreamError) as e0:
                # RemoteDisconnected / ConnectionResetError / IncompleteRead / socket.timeout / URLError
                # raised raw by an injected transport are network errors too (retried, not a crash);
                # so is a stream that was cut or carried an error event
                if isinstance(e0, StreamError):
                    e = TransportError(self.redact("stream cut: %s" % e0))
                else:
                    e = e0 if isinstance(e0, TransportError) else _as_transport_error(e0)
                waited = self._mono() - t_req
                # A timeout, OR a connection the proxy dropped while we were waiting for the reply
                # (RemoteDisconnected / ConnectionReset / IncompleteRead after LOST_REPLY_SECONDS), OR
                # a stream that had already started: Moonshot most likely processed and billed it.
                lost = method == "POST" and (bool(getattr(e, "timeout", False)) or waited >= LOST_REPLY_SECONDS
                                             or bool(getattr(e0, "billed", False))
                                             or bool(getattr(e0, "after_headers", False)))
                if lost:
                    # what arrived of a cut STREAM (the tokens produced before the cut); None for a reply
                    # that never came (a non-streamed request: the whole reply was most likely made)
                    partial = raw if isinstance(e0, StreamError) else getattr(e0, "partial_body", None)
                    if partial is None:
                        partial = getattr(e, "partial_body", None)
                    self._charge_lost_reply(body, data, waited, e, partial if stream else None)
                # v3.5.2: a streamed request that got NO answer at all (not even a status line within
                # STREAM_HEADERS_SECONDS) was most likely swallowed by the tunnel before Moonshot saw it
                # (on the server the first large request after a quiet spell often hangs, the next one
                # passes): it gets up to SILENT_EXTRA_RETRIES retries of its own, inside the deadline
                silent = lost and stream and bool(getattr(e, "no_answer", False) or getattr(e0, "no_answer", False))
                spare = silent and timeouts is not None and len(timeouts) > 1 and timeouts[1] < SILENT_EXTRA_RETRIES
                retry = attempt <= retries or spare
                if retry and lost and spare:
                    timeouts[1] += 1
                    log.warning("LLM POST %s: no answer at all after %.0f s (the connection went silent, most likely "
                                "never processed): extra retry %d/%d", path, waited, timeouts[1],
                                SILENT_EXTRA_RETRIES)
                elif retry and lost:
                    # the server may have received (and billed) the request: retry such requests sparingly
                    used = timeouts[0] if timeouts is not None else 0
                    if used >= int(self.cfg["max_timeout_retries"]):
                        retry = False
                        log.error("LLM POST %s lost after %.0f s (%s) and max_timeout_retries=%d is used up: giving "
                                  "up instead of paying for the same request again", path, waited,
                                  self.redact(str(e)), int(self.cfg["max_timeout_retries"]))
                    elif timeouts is not None:
                        timeouts[0] += 1
                if retry:
                    w = self._backoff(attempt)
                    log.warning("LLM %s %s: %s; retry %d/%d in %.1fs", method, path, self.redact(str(e)),
                                attempt, retries, w)
                    self._wait(w, deadline, "%s %s" % (method, path))
                    continue
                raise LLMError("network error talking to %s: %s" % (self.base_url, self.redact(str(e))),
                               kind="llm_timeout" if getattr(e, "timeout", False) else "llm")
            if streamed is not None:
                if not smeta.get("usage_in_stream"):
                    streamed["usage"] = estimate_usage(len(data or b""), smeta)
                    streamed["_usage_estimated"] = True
                streamed["_streamed"] = True
                return status, streamed
            text = raw.decode("utf-8", "replace") if raw else ""
            try:
                payload = json.loads(text) if text else None
            except ValueError:
                payload = None
            detail = self.redact(_short(self._error_message(payload, text), 300)) if status >= 300 else ""
            if status in (401, 403):
                if self.provider == "openrouter":
                    hint = ("valid OpenRouter key? a key that is disabled or over its credit limit on openrouter.ai "
                            "fails too; a 403 can also be a moderation refusal of the input")
                else:
                    hint = ("valid key? key from the same platform as base_url: api.moonshot.ai keys do not work on "
                            "api.moonshot.cn and vice versa; region access")
                raise LLMAuthError("HTTP %d from %s: %s. Check %s (%s)" % (
                    status, self.base_url, _short(detail, 200), self.cfg["api_key_env"], hint), status)
            # HTTP 402 (Payment Required) is OpenRouter's "insufficient credits"; Moonshot's path is unchanged
            quota_402 = status == 402 and self.provider != "moonshot"
            if quota_402 or status == 429:
                msg = self._error_message(payload, text)
                if quota_402 or re.search(r"quota|insufficient|balance|suspended", msg, re.I):
                    # logged as a request without usage: two in a row is the notifier's "balance is
                    # gone" alert (bitpin.spend.quota_streak); a later success ends the streak. A 402 is the
                    # same alert, never retried
                    self._log_usage({"time": round(self._clock(), 3), "error": "llm_quota", "status": status,
                                     "model": (body or {}).get("model") if isinstance(body, dict) else None,
                                     "usage": {}, "usd": 0.0})
                    raise LLMError("HTTP %d (account %s): %s" % (status, "quota/balance" if status == 429 else
                                                                 "credits/balance, payment required",
                                                                 self.redact(_short(msg, 200))), status,
                                   "llm_quota", detail)
            if (status == 429 or status >= 500) and attempt <= retries:
                w = self._backoff(attempt)
                log.warning("LLM %s %s: HTTP %d; retry %d/%d in %.1fs", method, path, status, attempt, retries, w)
                self._wait(w, deadline, "%s %s" % (method, path))
                continue
            if not 200 <= status < 300:  # includes 3xx: redirects are never followed
                raise LLMError("HTTP %d from %s%s: %s" % (status, self.base_url, path, detail), status, "llm", detail)
            if not isinstance(payload, dict):
                raise LLMError("unexpected non-JSON response from %s%s" % (self.base_url, path), status)
            return status, payload

    def _charge_lost_reply(self, body, data, waited, err, partial=None):
        """Charge an ESTIMATE of a reply that never arrived to the daily token budget. budget.add_tokens()
        only ever sees the usage of responses that came back, so a tunnel that drops connections would
        otherwise cost money the budget never counts.
        * a cut STREAM (partial: the bytes that arrived): the prompt plus what the stream carried before
          the cut (estimate_usage of its content / reasoning / tool characters) - the model stops when the
          client is gone, so charging prompt + max_tokens for every cut stream (about 41k tokens for a
          kimi-k3 decision) would lock the daily token budget after a few tunnel drops;
        * a reply that never came (no stream, or nothing readable arrived): prompt + max_tokens - the
          server most likely produced the whole reply."""
        try:
            meta = stream_char_counts(partial) if partial else None
            model = (body or {}).get("model") if isinstance(body, dict) else None
            if meta and meta.get("chunks"):
                est = estimate_usage(len(data or b""), meta)
                self.budget.add_tokens(est)
                log.warning("LLM stream cut after %.0f s (%s): charging the %d tokens it carried (prompt ~%d + "
                            "streamed ~%d) to today's budget", waited, self.redact(str(err)), est["total_tokens"],
                            est["prompt_tokens"], est["completion_tokens"])
                # the cost meter sees the lost reply too (the daily line must not read cheaper than the bill)
                self._log_usage({"time": round(self._clock(), 3), "model": model, "usage": est, "usage_estimated": True,
                                 "lost": True, "seconds": round(waited, 2)})
                return
            prompt = int(len(data or b"") / CHARS_PER_TOKEN)
            completion = int((body or {}).get("max_tokens") or 0) if isinstance(body, dict) else 0
            est = {"prompt_tokens": prompt, "completion_tokens": completion,
                   "total_tokens": prompt + completion}
            self.budget.add_tokens(est)
            log.warning("LLM reply lost after %.0f s (%s): charging an estimated %d tokens (prompt ~%d + max_tokens "
                        "%d) to today's budget - Moonshot most likely billed this request",
                        waited, self.redact(str(err)), est["total_tokens"], prompt, completion)
            self._log_usage({"time": round(self._clock(), 3), "model": model, "usage": est, "usage_estimated": True,
                             "lost": True, "seconds": round(waited, 2)})
        except Exception as e:  # noqa: BLE001 - accounting must never break the call
            log.debug("cannot charge the lost reply to the budget: %s", e)

    def _backoff(self, attempt):
        return min(float(self.cfg["backoff_max_seconds"]), float(self.cfg["backoff_seconds"]) * (2 ** (attempt - 1)))

    def _log_usage(self, rec):
        """Append one request's record to state_dir/kimi_usage.jsonl - priced ("usd", the configured
        llm.price_* per million tokens, cached input cheaper) and tagged "stage": "llm" for the cost
        meter - then refresh state_dir/llm_spend.json (bitpin.spend). Never the reasoning, never a
        prompt. On OpenRouter the "usd" is the cost the reply says was billed (provider_cost; the
        configured prices only when it has none), and a record of any provider other than Moonshot
        carries "provider". Informational only: a write error is logged, never raised."""
        if not self._usage_path:
            return
        rec = dict(rec)
        rec.setdefault("stage", "llm")
        if self.provider != "moonshot":
            rec.setdefault("provider", self.provider)
        if "usd" not in rec and not rec.get("error"):
            billed = provider_cost(rec.get("usage")) if self.provider == "openrouter" else None
            rec["usd"] = billed if billed is not None else cost_usd(rec.get("usage"), self._prices)
        try:
            with open(self._usage_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError as e:  # informational only
            log.warning("cannot write %s: %s", self._usage_path, e)
            return
        try:
            refresh_spend(self.state_dir, self._clock())
        except Exception as e:  # noqa: BLE001 - the meter must never break a call
            log.debug("LLM spend refresh failed: %s", e)

    # ---- public API
    def list_models(self):
        """GET {base_url}/models -> list of model ids (does not use the daily budget)."""
        deadline = self._mono() + float(self.cfg["deadline_seconds"])
        _, payload = self._request("GET", "/models", deadline=deadline)
        data = payload.get("data") if isinstance(payload, dict) else None
        return [m.get("id") for m in (data if isinstance(data, list) else []) if isinstance(m, dict) and m.get("id")]

    def list_models_detailed(self):
        """GET {base_url}/models -> one dict per model, for a model picker (does not use the daily
        budget): {"id", "name", "context_length", "price_in_per_m", "price_out_per_m",
        "price_cached_in_per_m", "supports_json", "supports_reasoning"} (parse_model_list). OpenRouter
        describes every model (its USD-per-token prices become USD per million tokens, its
        supported_parameters tell JSON mode and reasoning); Moonshot lists ids only, so the other fields
        are None there. A malformed entry is skipped, never fatal. Raises LLMError like list_models()."""
        deadline = self._mono() + float(self.cfg["deadline_seconds"])
        _, payload = self._request("GET", "/models", deadline=deadline)
        return parse_model_list(payload)

    @staticmethod
    def _rejects_response_format(e):
        return e.status == 400 and bool(re.search(r"response_format|json|tool", e.detail or "", re.I))

    def chat(self, messages, json_mode=True, web_search=False, time_limit=None, abort=None, model=None,
             max_tokens=None, reasoning_effort=None):
        """Run one chat completion (with the $web_search tool loop when web_search=True; on OpenRouter
        web_search=True adds OpenRouter's web plugin instead: one request, no tool loop).

        time_limit: seconds this call may take at most (default and upper bound: deadline_seconds).
        abort: optional callable polled before every request; a truthy return value (e.g. "STOP
               file present") ends the call with LLMError(kind="llm_aborted").
        model: model id for THIS call only (default: llm.model), e.g. "kimi-k2.6" for a search call
               next to a kimi-k3 decision model. max_tokens: completion-token cap for this call only
               (default: llm.max_tokens; None/0 there = not sent). The daily budget (calls and tokens)
               is shared by every call of this client.
        reasoning_effort: "low" / "high" / "max" for THIS call only (default: llm.reasoning_effort;
               None there = not sent, the API's own default). Sent top-level in every request of the
               call (OpenRouter: as "reasoning": {"effort": ...}, "max" sent as "high"; "medium" is
               accepted there). Anything else raises ValueError before any request is made.
        Returns {"content": str, "usage": {...summed over rounds...}, "tool_rounds": int,
                 "requests": int, "finish_reason": str, "model": str, "json_fallback": bool,
                 "json_mode_used": bool, "search_calls": int, "forced_final": bool,
                 "streamed": bool, "usage_estimated": bool, "reasoning": str,
                 "reasoning_effort": str or None}.
        reasoning: the model's reasoning_content of every round (joined, redacted, at most
        REASONING_MAX_CHARS; "" for a model that sends none) - for the brain's reasoning file only,
        never part of the answer, never echoed into a prompt and never logged here.
        streamed: at least one reply of this call arrived as an event stream; usage_estimated: the
        usage of at least one reply was missing and estimated (it is charged to the daily budget).
        json_mode_used: the reply was produced with response_format json_object (so it must be pure
        JSON); json_fallback: JSON mode was requested but could not be used. temperature null in the
        config = not sent (required for kimi-k3).
        Raises LLMError / LLMAuthError / LLMBudgetExceeded."""
        use_model = model.strip() if isinstance(model, str) and model.strip() else self.model
        if not use_model:
            raise LLMError("no model configured: set llm.model in the config (see LLMClient.list_models())",
                           kind="llm_config")
        if not self._api_key:
            raise LLMAuthError(self._key_refused or "no API key: set the environment variable %s"
                               % self.cfg["api_key_env"])
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list")
        try:
            effort = check_reasoning_effort(reasoning_effort, "reasoning_effort", self.provider) \
                if reasoning_effort is not None else self.cfg.get("reasoning_effort")
        except ConfigError as e:
            raise ValueError(str(e))
        openrouter = self.provider == "openrouter"
        limit = float(self.cfg["deadline_seconds"])
        if time_limit is not None:
            limit = min(limit, max(0.0, float(time_limit)))
        deadline = self._mono() + limit
        self._check_go(deadline, abort, "the first request")
        self.budget.consume()
        msgs = [dict(m) for m in messages]
        # web search: Moonshot's builtin tool (a tool loop); OpenRouter has no such tool, its web plugin
        # searches for the request itself (one request, no tool rounds)
        plugin = bool(web_search) and openrouter
        tools = [dict(WEB_SEARCH_TOOL)] if web_search and not plugin else None
        usage, rounds, requests, search_calls = {}, 0, 0, 0
        max_rounds = int(self.cfg["max_tool_rounds"])
        max_prompt = self.cfg.get("max_prompt_tokens_per_call")
        max_call = self.cfg.get("max_tokens_per_call")
        final = False             # tools withdrawn: the model has to answer now
        final_keeps_tools = False  # the API refused the final request without 'tools'
        timeouts = [0, 0]         # [timed-out POSTs retried, silent POSTs retried (SILENT_EXTRA_RETRIES)]
        sa = self._stream.attempt()   # the streaming level of this call (StreamPolicy)
        streamed_any = estimated = False
        reasoning_parts = []          # reasoning_content of every round, for res["reasoning"] only
        while True:
            offer = bool(tools) and (not final or final_keeps_tools)
            use_rf = bool(json_mode) and not (offer and self._no_response_format_with_tools)
            body = {"model": use_model, "messages": msgs}
            if self.cfg.get("temperature") is not None:      # null = not sent (kimi-k3 needs that)
                body["temperature"] = float(self.cfg["temperature"])
            if effort and openrouter:                         # OpenRouter's form; "max" is its "high"
                body["reasoning"] = {"effort": OPENROUTER_EFFORT.get(effort, effort)}
            elif effort:                                      # null = not sent (the API default, "max")
                body["reasoning_effort"] = effort
            mt = max_tokens if max_tokens is not None else self.cfg.get("max_tokens")
            if mt:
                body["max_tokens"] = int(mt)
            if use_rf:
                body["response_format"] = {"type": "json_object"}
            if offer:
                body["tools"] = tools
            if plugin:
                body["plugins"] = [dict(OPENROUTER_WEB_PLUGIN)]
            if openrouter:
                body["usage"] = {"include": True}             # the usage (and the billed cost) in the reply
            StreamPolicy.apply(body, sa.level)
            t0 = self._clock()
            try:
                _, payload = self._request("POST", "/chat/completions", body, deadline, abort, timeouts,
                                           stream=sa.streaming)
            except LLMError as e:
                requests += 1
                if sa.explicit(e.status, e.detail):
                    continue                  # the API named stream / stream_options: one step down, for good
                if use_rf and offer and self._rejects_response_format(e):
                    log.warning("LLM rejected response_format together with tools (HTTP 400: %s); retrying without "
                                "response_format and extracting the JSON from the text", _short(e.detail, 120))
                    self._no_response_format_with_tools = True
                    continue
                if e.status == 400 and final and tools and not final_keeps_tools:
                    log.warning("LLM refused the final request without tools (HTTP 400: %s); retrying with tools "
                                "offered", _short(e.detail, 120))
                    final_keeps_tools = True
                    continue
                if sa.generic(e.status):
                    continue                  # streaming not confirmed yet: the same request one level lower
                raise
            requests += 1
            sa.succeeded()
            est = bool(payload.pop("_usage_estimated", False))
            estimated = estimated or est
            streamed_any = bool(payload.pop("_streamed", False)) or streamed_any
            u = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            _add_usage(usage, u)
            self.budget.add_tokens(u)
            choices = payload.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                raise LLMError("response has no choices")
            ch = choices[0]
            msg = ch.get("message") if isinstance(ch.get("message"), dict) else {}
            finish = ch.get("finish_reason")
            # the thinking of this round, kept for the brain's reasoning file: never a log line
            rc = msg.get("reasoning_content")
            if not isinstance(rc, str):
                rc = openrouter_reasoning(msg)                # OpenRouter: message.reasoning / reasoning_details
            if isinstance(rc, str) and rc.strip() and sum(len(p) for p in reasoning_parts) < REASONING_MAX_CHARS:
                reasoning_parts.append(rc)
            if plugin:
                search_calls = 1                              # the web plugin searched once for this request
            self._log_usage({"time": round(t0, 3), "model": payload.get("model") or use_model,
                             "round": rounds, "finish_reason": finish, "usage": u,
                             "seconds": round(self._clock() - t0, 2), "web_search": offer or plugin,
                             "response_format": use_rf, "final": final, "stream": sa.streaming,
                             "usage_estimated": est, "reasoning_effort": effort})
            tool_calls = msg.get("tool_calls") or []
            if finish == "tool_calls" or (tool_calls and finish not in ("stop", "length")):
                if final:
                    raise LLMError("model still calling tools after %d rounds (told to answer without tools)" % rounds)
                if not offer:
                    raise LLMError("model requested tool calls although no tools were offered")
                if not tool_calls:
                    raise LLMError("finish_reason=tool_calls without any tool call")
                if not isinstance(tool_calls, list) or not all(isinstance(tc, dict) for tc in tool_calls):
                    raise LLMError("malformed tool calls in the response")
                rounds += 1
                # the VERIFIED format (scratch/next_requirements.md): content "" instead of null; each call
                # only id / type / function {name, arguments unchanged}; no index, no reasoning_content
                msgs.append(assistant_echo(msg, tool_calls))
                for tc in tool_calls:
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    if fn.get("name") == WEB_SEARCH_NAME:
                        search_calls += 1
                        args = fn.get("arguments")
                        try:  # informational: Moonshot reports the search result tokens here
                            a = json.loads(args) if isinstance(args, str) else None
                            if isinstance(a, dict) and isinstance(a.get("usage"), dict):
                                _add_usage(usage.setdefault("search", {}), a["usage"])
                        except (ValueError, RecursionError):
                            pass
                    msgs.append(tool_message(tc))
                pt = u.get("prompt_tokens")
                spent = usage.get("total_tokens")
                why = None
                if rounds >= max_rounds:
                    why = "max_tool_rounds=%d reached" % max_rounds
                elif max_prompt and isinstance(pt, (int, float)) and not isinstance(pt, bool) and pt > max_prompt:
                    why = "prompt of %d tokens > max_prompt_tokens_per_call=%d" % (pt, max_prompt)
                elif max_call and isinstance(spent, (int, float)) and not isinstance(spent, bool) and spent >= max_call:
                    why = "%d tokens used by this call >= max_tokens_per_call=%d" % (spent, max_call)
                elif self.budget.tokens_exhausted():
                    why = "daily token budget reached"
                if why:
                    log.info("LLM: %s - no more searches, asking for the answer now", why)
                    final = True
                    msgs.append({"role": "user", "content": FINAL_ANSWER_NUDGE})
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                content = "" if content is None else json.dumps(content)
            reasoning = self.redact("\n\n".join(reasoning_parts))
            if len(reasoning) > REASONING_MAX_CHARS:
                reasoning = reasoning[:REASONING_MAX_CHARS] + _REASONING_TRUNCATED
            return {"content": content, "usage": usage, "tool_rounds": rounds, "requests": requests,
                    "finish_reason": finish, "model": payload.get("model") or use_model,
                    "json_fallback": bool(json_mode) and not use_rf, "json_mode_used": use_rf,
                    "search_calls": search_calls, "forced_final": final, "streamed": streamed_any,
                    "usage_estimated": estimated, "reasoning": reasoning, "reasoning_effort": effort}
