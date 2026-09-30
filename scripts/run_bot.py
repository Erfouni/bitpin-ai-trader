"""Bitpin trading bot - command line.

Paper trading (public data only, no keys needed):
  py scripts/run_bot.py paper --strategy hold_usdt --capital-irt 100000000 --once --state-dir state/paper
  py scripts/run_bot.py paper --state-dir state/paper                      (loop, resumes the paper account)

Live account (keys from env BITPIN_API_KEY / BITPIN_SECRET_KEY, or --keys-file):
  py scripts/run_bot.py status --state-dir state/live                      (read-only; confirm the IRT balance once)
  py scripts/run_bot.py live --strategy hold_usdt --dry-run --once --state-dir state/live   (plan only)
  py scripts/run_bot.py live --strategy hold_usdt --i-accept-the-risk --state-dir state/live (REAL orders)

Kimi brain (the Kimi LLM decides the allocation, bounded by the brain's limits and the risk manager).
Two stages: stage 1 = the news researcher ("news" section of kimi.json, kimi-k2.6 with web search) writes
a short news brief (refreshed once a day before the 13:00 decision with news.cache_minutes 1380, and
forced fresh for a veto / fill review); stage 2 = the decision model (llm.model, kimi-k3, JSON mode, no tools)
decides from the Bitpin market context + the news brief (untrusted) + docs/STRATEGY_KNOWLEDGE.md.
  py scripts/run_bot.py kimi-check --kimi-config kimi.json                   (key set? route/proxy, model ids)
  py scripts/run_bot.py kimi-check --kimi-config kimi.json --news            (+ ONE real news research call,
        ~20-50k tokens, in a fresh temporary directory: "NEWS: OK" only for a newly fetched brief)
  py scripts/run_bot.py set-model --kimi-config kimi.json --decision-model ID [--news-model ID] [--base-url URL]
        [--thinking] [--list] [--test] [--dry-run]     (server: sudo bitpin-bot set-model ...; switches ONLY the
        model keys - thinking models get temperature null + a large max_tokens -, validates the result with the
        bot's own checks, backs the old file up; then confirm-live again and restart the service)
  py scripts/run_bot.py paper --brain kimi --kimi-config kimi.json --capital-irt 5000000 --state-dir state/paper
  py scripts/run_bot.py live --brain kimi --kimi-config kimi.json --dry-run --once --state-dir state/live
  py scripts/run_bot.py confirm-live --config config.json --kimi-config kimi.json --state-dir state/live
        (once, interactively, after 'status'; allows the service's non-interactive start:)
  py scripts/run_bot.py live --brain kimi --config config.json --kimi-config kimi.json --state-dir state/live
        --i-accept-the-risk --non-interactive
        (LIVE_CONFIRMED holds a digest of the values in config.json + kimi.json - not of comments or of
        the code's defaults - plus the resting-order settings IN FORCE with the Kimi brain (ladder / exits
        / routing and the limit-order risk keys, defaults included: they make the bot place orders on
        its own); any changed value needs a new confirm-live. 'confirm-live --check' with the same
        options only reports whether the confirmation still matches.)
  Without a valid Kimi decision the bot does not trade, except the brain's fallback: toman above the
  cash limit (brain.fallback.sweep_irt_above, 5%; the toman share of the last valid decision is kept
  while it is younger than derisk_after_hours) is swept into USDT_IRT, and after
  fallback.derisk_after_hours (48 in kimi.example.json) without a valid decision the coins are moved
  into USDT_IRT - except coins whose code exits are live (they are left to their stop / target).
  When only the news research fails, stage 2 decides without news (the prompt says so).
  Network: Bitpin is ALWAYS reached directly (http_proxy / https_proxy / ALL_PROXY are ignored).
  Kimi (both stages) goes through $KIMI_HTTPS_PROXY (e.g. http://127.0.0.1:1081) when it is set, else
  directly; NO_PROXY / no_proxy never bypass it. Dropped connections are retried.
  Test hooks (paper only; the options do not exist for live): --test-kimi-reply FILE makes every
  stage-2 reply the text of FILE, --test-kimi-reply unreachable simulates a Kimi outage;
  --test-kimi-news FILE|unreachable does the same for the stage-1 news researcher (one simulated
  $web_search round, then the text of FILE). No request goes to Moonshot then.

Daily schedule + crash ladder (kimi.json brain.decision_times_local ["13:00"]; config.json sections
"ladder", "exits", "routing"): Kimi decides ONCE A DAY at 13:00 Tehran plus rare wake-ups (a ladder
fill -> REVIEW, a ladder coin -15% -> VETO, a held coin +-8%, a worse drawdown); every hourly check the
CODE keeps resting USDT-backed limit bids at -20% / -25% below the 48 h high on BTC/ETH/XRP/SOL_USDT,
enforces stops / targets / max holds of every coin position, and routes USDT <-> coin legs through
COIN_USDT when that is cheaper.
  py scripts/run_bot.py ladder --config config.json --kimi-config kimi.json --state-dir state/live
        (read-only, public data only: the ladder bids the bot would keep now, their prices and sizes)
  py scripts/run_bot.py cancel-resting --mode live --state-dir state/live [--tag ladder|target]
        (bot stopped: cancel the bot's own resting orders on Bitpin - the target sells, and every order a bot
        that crashed or was stopped by systemctl left there; a running bot cancels its ladder bids at STOP)

Safety:
  py scripts/run_bot.py stop   --state-dir state/live     (kill switch: creates state/live/STOP; the RUNNING
                                                          bot cancels its crash-ladder bids when it sees it,
                                                          its target sells stay: cancel-resting)
  py scripts/run_bot.py resume --state-dir state/live     (removes STOP)
  py scripts/run_bot.py risk-reset --mode live --state-dir state/live   (clear a drawdown halt + HWM;
                                                          refused while a bot runs on that state dir)
  py scripts/run_bot.py resolve-order --state-dir state/live            (list bot orders of unknown outcome)
  py scripts/run_bot.py resolve-order --state-dir state/live --identifier ID --order-id N
        (record the order's final fill, read from Bitpin by the order id shown in the Bitpin app)
  py scripts/run_bot.py resolve-order --state-dir state/live --identifier ID --not-executed
        (record that the order never executed). Both are refused while a bot runs on that state dir.

Only one live bot per API key runs at a time (account lock in %LOCALAPPDATA%/bitpin-bot), and one
bot per state dir; a live state dir is bound to the account whose keys first used it
(account.json). Locks are taken before any state file is read. --dry-run never sends, cancels or
journals an order and never changes runner / risk / paper state.

Exit codes: 0 ok / stopped on purpose, 2 a --once cycle did not complete, 78 configuration or
credential problem (retrying cannot help), 130 interrupted, 1 other errors.

Common options: --strategy NAME --params '{"k": v}' --config config.json --state-dir DIR.
Settings are documented in config.example.json. This tool has no withdrawal/transfer features.
"""
import sys

if __name__ == "__main__" and sys.platform.startswith("linux"):
    # prctl(PR_SET_DUMPABLE, 0) FIRST - before the other imports and the bitpin package (0.1-0.3 s): the
    # environment holds the Bitpin key pair, and a process of the same user (the notifier runs as user bitpin)
    # must not be able to read /proc/<pid>/environ during the start either. main() sets it again and logs a
    # failure (not_dumpable). The interpreter's own start before this line stays readable: it is not a
    # boundary between processes of the same user (see deploy/bitpin-bot-notify.service).
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)      # PR_SET_DUMPABLE = 4
    except Exception:  # noqa: BLE001 - never fatal; main() retries and logs
        pass

import argparse  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from decimal import Decimal  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin.api import (AuthBudgetExceeded, AuthError, BitpinClient, BitpinError, TransportError,  # noqa: E402
                        atomic_write_json, key_fingerprint, read_json)
from bitpin.broker import LIMIT_FINAL_STATES, BrokerError, LiveBroker, PaperBroker  # noqa: E402
from bitpin.markets import DEFAULT_MIN_ORDER_USDT, ZERO, D, MarketCache, fmt_amount, parse_symbol  # noqa: E402
from bitpin.risk import DEFAULT_RISK, KILL_SWITCH_FILE, RiskManager  # noqa: E402
from bitpin.runner import (LADDER_TAG, TARGET_TAG, BrainStrategy, Runner, RunnerError,  # noqa: E402
                           StateLock, account_lock, format_report, ladder_plan, load_config, lvl_key)

log = logging.getLogger("bitpin.cli")

IRT_CANDIDATES = ("IRT", "RIAL", "IRR", "TMN", "TOMAN")
CONFIRM_FILE = "irt_confirmation.json"
ACCOUNT_FILE = "account.json"
LIVE_CONFIRMED_FILE = "LIVE_CONFIRMED"
LIVE_PHRASE = "I ACCEPT THE RISK"
EXIT_CONFIG = 78   # EX_CONFIG: configuration / credential problem - restarting cannot help
KIMI_PROXY_ENV = "KIMI_HTTPS_PROXY"   # the ONLY proxy setting: read by bitpin/llm.py LLMClient for Moonshot
                                     # (Kimi) traffic; Bitpin traffic never uses a proxy
TEST_HOOK_KEY = "test-hook-key-not-a-real-key"
TEST_HOOK_UNREACHABLE = "unreachable"
TEST_HOOK_DEAD_PROXY = "http://127.0.0.1:9"   # discard port: a request through it fails locally
NEWS_CHECK_OVERRIDES = {"max_tool_rounds": 2, "deadline_seconds": 480, "cache_minutes": 0, "max_calls_per_day": 2,
                        "max_items": 5, "max_retries": 2}   # kimi-check --news: one small, bounded research call
# (v3.5.1: 480 s instead of 150: a web search and a 16000-token answer through the tunnel need minutes)


def die(msg, code=EXIT_CONFIG):
    """Exit for a configuration / credential problem (exit code 78, which the systemd unit does
    not restart)."""
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def optional_decimal(value):
    """Decimal, but an EMPTY string means "not given". systemd expands an unset or emptied variable
    in ExecStart (`--capital-irt ${PAPER_CAPITAL_IRT}`) to an empty ARGUMENT rather than dropping
    it, and bitpin-bot.env is read after the unit's own Environment= default: a user who uncomments
    PAPER_CAPITAL_IRT= and clears the number would otherwise get "invalid D value: ''" and argparse
    exit 2, which RestartPreventExitStatus does not cover, so the unit restarts for ever."""
    if value is None or not str(value).strip():
        return None
    return D(value)


# --------------------------------------------------------------------------- logging with secret scrubbing

class SecretFilter(logging.Filter):
    JWT = re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}")

    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record):
        msg = record.getMessage()
        clean = self.JWT.sub("<jwt>", msg)
        for s in self.secrets:
            clean = clean.replace(s, "<secret>")
        if clean != msg:
            record.msg, record.args = clean, ()
        return True


def setup_logging(state_dir, mode, level="INFO", secrets=()):
    os.makedirs(state_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    flt = SecretFilter(secrets)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    con = logging.StreamHandler(sys.stdout)
    con.setLevel(getattr(logging, level.upper(), logging.INFO))
    con.setFormatter(fmt)
    con.addFilter(flt)
    fh = logging.FileHandler(os.path.join(state_dir, "bot_%s.log" % mode), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(flt)
    root.addHandler(con)
    root.addHandler(fh)


# --------------------------------------------------------------------------- helpers

# "api_key: X", "API Key = X", "secret: Y", "Bitpin secret key: Y" ... (a label is whole words ending
# in key/secret, so a secret value that merely contains "key" and ends in "=" is not a label)
KEY_LABEL = re.compile(r"^\s*((?:[A-Za-z]+[ _-])*(?:api[ _-]?key|key|secret[ _-]?key|secret|api[ _-]?secret))"
                       r"\s*[:=]\s*(?=\S)", re.I)


def load_credentials(keys_file=None):
    """From --keys-file (JSON {"api_key","secret_key"}; or 2 lines, either labelled like
    'api_key: ...' / 'secret_key: ...' in any order, or unlabelled) or the BITPIN_API_KEY /
    BITPIN_SECRET_KEY environment variables. Returns (api_key, secret_key, swappable):
    swappable=True for 2 unlabelled lines, whose order is then verified at the first
    authentication (see connect_client). Values are never printed."""
    swappable = False
    if keys_file:
        try:
            with open(keys_file, "r", encoding="utf-8-sig") as f:
                text = f.read().strip()
        except OSError as e:
            die("cannot read keys file %s: %s" % (keys_file, e.strerror))
        if text.startswith("{"):
            try:
                d = json.loads(text)
            except ValueError:
                die("keys file %s is not valid JSON" % keys_file)
            k, s = d.get("api_key"), d.get("secret_key")
        else:
            parsed = []
            for ln in (x.strip() for x in text.splitlines()):
                if not ln:
                    continue
                m = KEY_LABEL.match(ln)
                kind = None
                if m:
                    kind = "secret" if "secret" in m.group(1).lower() else "api"
                    ln = ln[m.end():].strip()
                parsed.append((kind, ln))
            if len(parsed) < 2:
                die("keys file %s must have 2 lines (api key, secret key) or be JSON" % keys_file)
            kinds = {kind: v for kind, v in parsed[:2] if kind}
            if set(kinds) == {"api", "secret"}:
                k, s = kinds["api"], kinds["secret"]
            else:
                k, s = parsed[0][1], parsed[1][1]
                swappable = True
    else:
        k, s = os.environ.get("BITPIN_API_KEY"), os.environ.get("BITPIN_SECRET_KEY")
    k, s = (k or "").strip(), (s or "").strip()
    if not k or not s:
        die("no credentials: set BITPIN_API_KEY and BITPIN_SECRET_KEY, or pass --keys-file PATH")
    if any(c.isspace() for c in k + s):
        die("credentials contain whitespace - check the keys file format")
    return k, s, swappable


def account_fingerprint(key, secret):
    """Same value whichever line of the keys file holds the API key."""
    return key_fingerprint("|".join(sorted((key, secret))))


def check_account_binding(state_dir, fingerprint, write):
    """A live state dir (sleeve, high-water mark, order journal, IRT confirmation) belongs to ONE
    Bitpin account: refuse keys of another account. write=True binds an unbound dir (a one-way key
    fingerprint is stored, never the keys)."""
    path = os.path.join(state_dir, ACCOUNT_FILE)
    rec = read_json(path) or {}
    bound = rec.get("account_fingerprint")
    if bound and bound != fingerprint:
        die("state dir %s belongs to a DIFFERENT Bitpin account: its bot sleeve, drawdown high-water mark and order "
            "journal were made with other API keys, and using them with these keys could sell coins that are not the "
            "bot's. Use a separate --state-dir for each account (or the keys this dir was made with)."
            % os.path.abspath(state_dir))
    if not bound and write:
        atomic_write_json(path, {"account_fingerprint": fingerprint,
                                 "bound_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())})


def connect_client(cfg, state_dir, key, secret, swappable=False, client_factory=BitpinClient):
    """BitpinClient with verified credentials. For an unlabelled 2-line keys file, a rejection of
    line 1 as the API key (api_credential_wrong) is retried ONCE with the lines swapped, like
    check_balance.py does. Uses 1-2 authentications of the daily budget."""
    irt = [str(cfg["irt_asset_code"]).upper()]
    client = client_factory(cfg["base_url"], key, secret, state_dir=state_dir)
    if not swappable:
        return client
    try:
        client.wallets(assets=irt)
        return client
    except AuthError as e:
        if e.code != "api_credential_wrong":
            raise
    log.warning("Bitpin rejected line 1 of the keys file as the API key; trying the other line order once")
    client = client_factory(cfg["base_url"], secret, key, state_dir=state_dir)
    client.wallets(assets=irt)
    print("note: authenticated with line 2 of the keys file as the API key. Label the lines "
          "('api_key: ...' / 'secret_key: ...') to skip this extra authentication.")
    return client


def build_config(args):
    over = {}
    if getattr(args, "brain", None) and args.strategy:
        die("--brain kimi and --strategy cannot be combined (the Kimi brain replaces the strategy)")
    if getattr(args, "brain", None) and not getattr(args, "kimi_config", None):
        die("--brain kimi needs --kimi-config PATH (see kimi.example.json)")
    if args.strategy:
        over["strategy"] = args.strategy
    if args.params:
        try:
            over["params"] = json.loads(args.params)
        except ValueError as e:
            die("--params is not valid JSON: %s" % e)
    try:
        cfg = load_config(args.config, over)
        raw = (read_json(args.config) or {}) if args.config else {}
    except (RunnerError, ValueError) as e:
        die("config error: %s" % e)
    cfg["state_dir"] = args.state_dir or cfg.get("state_dir") or "state"
    # what the user chose for this run (for the live confirmation digest): config.json as written,
    # without comments and state_dir, plus the command-line overrides - NOT the built-in defaults
    args.user_settings = {"config": {k: v for k, v in raw.items() if not k.startswith("_") and k != "state_dir"},
                          "overrides": over}
    return cfg


def make_strategy(cfg):
    from bitpin.research import discover_strategies
    strategies = discover_strategies()
    name = cfg["strategy"]
    if name not in strategies:
        die("unknown strategy %r; available: %s" % (name, ", ".join(sorted(strategies))))
    try:
        return strategies[name](**(cfg.get("params") or {}))
    except (TypeError, ValueError) as e:
        die("bad params for %s: %s" % (name, e))


def make_risk(cfg, state_dir, mode, persist=True):
    try:
        return RiskManager(cfg.get("risk"), state_dir, mode, persist=persist)
    except ValueError as e:
        die("risk config error: %s" % e)


def make_live(cfg, state_dir, key, secret, swappable=False):
    try:
        client = connect_client(cfg, state_dir, key, secret, swappable)
    except AuthError as e:
        die("authentication problem: %s" % e)
    except AuthBudgetExceeded as e:
        # exit 78, not 1: the ~150/24 h login budget refills only when the window rolls, so an
        # immediate restart just burns the next start too. RestartPreventExitStatus=0 78 stops
        # systemd retrying every 60 s..15 min, and `health` shows last-exit=78 instead of a loop.
        die("authentication problem: %s\nThe bot stops (exit %d) until the 24 h window rolls; start it again "
            "afterwards: sudo systemctl start bitpin-bot" % (e, EXIT_CONFIG))
    markets = MarketCache(client, state_dir)
    risk_cfg = cfg.get("risk") or {}
    broker = LiveBroker(client, markets, state_dir, irt_asset_code=cfg["irt_asset_code"],
                        irt_unit_divisor=cfg["irt_unit_divisor"],
                        min_order_irt=risk_cfg.get("min_order_irt", DEFAULT_RISK["min_order_irt"]),
                        min_order_usdt=risk_cfg.get("min_order_usdt", DEFAULT_MIN_ORDER_USDT),
                        poll_interval=cfg["order_poll_interval_seconds"],
                        poll_timeout=cfg["order_poll_timeout_seconds"],
                        unknown_order_max_age=cfg["unknown_order_max_age_seconds"])
    return client, markets, broker


def confirmation_ok(state_dir, cfg):
    c = read_json(os.path.join(state_dir, CONFIRM_FILE)) or {}
    return (c.get("irt_asset_code") == str(cfg["irt_asset_code"]).upper()
            and str(c.get("irt_unit_divisor")) == str(cfg["irt_unit_divisor"])
            and c.get("confirmed") is True), c


# --------------------------------------------------------------------------- Kimi (Moonshot) wiring

def make_test_transport(spec, model):
    """PAPER-ONLY test hook (--test-kimi-reply): no request ever reaches Moonshot. spec is a file whose
    text is returned as the model's reply, or 'unreachable' (every request fails like a blocked network)."""
    text = None
    if spec != TEST_HOOK_UNREACHABLE:
        try:
            with open(spec, "r", encoding="utf-8-sig") as f:
                text = f.read()
        except OSError as e:
            die("--test-kimi-reply: cannot read %s: %s" % (spec, e.strerror or e))

    def transport(method, url, headers, body, timeout):
        if text is None:
            raise TransportError("network error: [test hook] Moonshot unreachable")
        if method == "GET" and url.endswith("/models"):
            return 200, json.dumps({"data": [{"id": model or "test-model"}]}).encode("utf-8")
        payload = {"id": "test-hook", "model": model or "test-model",
                   "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": text}}],
                   "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}}
        return 200, json.dumps(payload).encode("utf-8")

    transport.proxy = None
    return transport


def make_test_news_transport(spec, model):
    """PAPER-ONLY test hook (--test-kimi-news) for the stage-1 news researcher: no request ever reaches
    Moonshot. spec is a file whose text is the researcher's final reply (after one simulated $web_search
    round, echoed back in the verified format), or 'unreachable' (every request fails like the Kimi
    proxy dropping the connection: http.client.RemoteDisconnected, retried, then given up)."""
    import http.client
    text = None
    if spec != TEST_HOOK_UNREACHABLE:
        try:
            with open(spec, "r", encoding="utf-8-sig") as f:
                text = f.read()
        except OSError as e:
            die("--test-kimi-news: cannot read %s: %s" % (spec, e.strerror or e))

    def transport(method, url, headers, body, timeout):
        if text is None:
            raise http.client.RemoteDisconnected("Remote end closed connection without response [test hook]")
        try:
            msgs = json.loads(body.decode("utf-8")).get("messages") or []
        except (ValueError, AttributeError):
            msgs = []
        if not any(isinstance(m, dict) and m.get("role") == "tool" for m in msgs):
            args = json.dumps({"search_result": {"search_id": "test-hook"}, "usage": {"total_tokens": 5000}})
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{"index": 0, "id": "test-hook-search-1", "type": "builtin_function",
                                   "function": {"name": "$web_search", "arguments": args}}]}
            payload = {"id": "test-hook", "model": model, "choices": [{"index": 0, "finish_reason": "tool_calls",
                                                                        "message": msg}],
                       "usage": {"prompt_tokens": 1500, "completion_tokens": 60, "total_tokens": 1560}}
        else:
            payload = {"id": "test-hook", "model": model,
                       "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": text}}],
                       "usage": {"prompt_tokens": 7100, "completion_tokens": 400, "total_tokens": 7500}}
        return 200, json.dumps(payload).encode("utf-8")

    transport.proxy = None
    return transport


def load_kimi(path):
    """load_kimi_config(path) with exit 78 on a bad file."""
    from bitpin.brain import load_kimi_config
    from bitpin.llm import ConfigError
    if not path:
        die("--brain kimi needs --kimi-config PATH (see kimi.example.json)")
    try:
        return load_kimi_config(path)
    except (ConfigError, OSError, UnicodeDecodeError) as e:
        die("kimi config error: %s" % e)


def kimi_symbols(path):
    """The symbols the Kimi brain may trade (brain.allowed_symbols, USDT_IRT first), for 'status'.
    None (with a note) if the kimi config cannot be read: status then shows the strategy universe."""
    from bitpin.analysis import SAFE
    from bitpin.brain import load_kimi_config, validate_brain_config
    try:
        allowed = validate_brain_config(load_kimi_config(path).get("brain") or {})["allowed_symbols"]
    except Exception as e:  # noqa: BLE001 - status is read-only; 'check' reports config errors
        print("  (kimi config %s not usable here: %s)" % (path, e))
        return None
    return list(dict.fromkeys([SAFE] + [str(s).strip().upper() for s in allowed]))


def kimi_secrets(kcfg):
    """Values the log filter must scrub: the Kimi API key and proxy credentials (user:pass@)."""
    from bitpin.llm import proxy_secrets
    from bitpin.news import news_key_env
    llm = (kcfg or {}).get("llm") or {}
    name = str(llm.get("api_key_env") or "KIMI_API_KEY")
    # v3.1: the news stage may read another variable (news.api_key_env, e.g. OPENROUTER_API_KEY)
    out = [(os.environ.get(n) or "").strip() for n in sorted({name, news_key_env(kcfg or {})})]
    for p in (os.environ.get(KIMI_PROXY_ENV), llm.get("proxy"), ((kcfg or {}).get("news") or {}).get("proxy")):
        if isinstance(p, str) and p.strip():
            out.extend(proxy_secrets(p.strip()))
    return tuple(x for x in out if x)


def _canon_num(v):
    """A number as the digest sees it: 0.30 == 0.3 and 24 == 24.0 (read_json parses fractions as
    Decimal, whose text keeps the trailing zeros; a file re-written by apply_profile or a hand edit
    that only changes the number's formatting must not invalidate the live confirmation)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        return v
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return str(v)
    if f != f or f in (float("inf"), float("-inf")):
        return str(v)
    if f == int(f) and abs(f) < 2 ** 53:
        return int(f)
    return f


def _strip_comments(obj):
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, (list, tuple)):
        return [_strip_comments(v) for v in obj]
    return _canon_num(obj)


# 3: + the resting-order settings in force (crash ladder / code exits / routing)
# 4: numbers are compared by value (0.30 == 0.3), and the brain-side rules the code applies on its own
#    are covered too (the default stop / target / max hold of the code exits, the ladder coins, the endgame)
DIGEST_VERSION = 5          # v3 (2026-09-27): a v2 confirmation reads as 'an older version'
RESTING_RISK_KEYS = ("min_order_usdt", "max_limit_orders_per_day", "max_limit_distance")


def code_exit_rules(kcfg):
    """What the code sells / buys on its own besides config.json: the default code-exit rules
    (bitpin/brain.py constants) and the brain's ladder coins, endgame and wake-up levels AS RESOLVED
    (the defaults of a key kimi.json leaves out included). A later version that changes one of them
    changes the digest, so the owner confirms it before the bot trades on it."""
    from bitpin import brain as brain_mod
    out = {"stop_pct": [brain_mod.STOP_PCT_DEFAULT, brain_mod.STOP_PCT_MIN, brain_mod.STOP_PCT_MAX],
           "max_hold_hours": brain_mod.MAX_HOLD_HOURS, "target_rule": brain_mod.TARGET_RULES[0],
           "reentry_block_hours": brain_mod.REENTRY_BLOCK_SECONDS / 3600.0}
    try:
        b = brain_mod.validate_brain_config((kcfg or {}).get("brain"))
        for k in ("ladder_coins", "endgame", "veto_drop_pct", "held_move_pct", "fallback"):
            out[k] = b.get(k)
    except Exception as e:  # noqa: BLE001 - a broken kimi.json is refused elsewhere; the digest stays defined
        out["brain_error"] = str(e)[:200]
    return out


def resting_settings(cfg):
    """The settings with which the bot places and cancels RESTING orders on its own (brain mode): the
    config's ladder / exits / routing sections and the limit-order risk keys IN FORCE - the code's
    defaults included, because a code update that switches the crash ladder on (or changes its
    levels / sizes) makes the bot place real orders that nobody confirmed."""
    cfg = cfg or {}
    risk = {k: DEFAULT_RISK.get(k) for k in RESTING_RISK_KEYS}
    risk.update({k: v for k, v in (cfg.get("risk") or {}).items() if k in RESTING_RISK_KEYS})
    return {"ladder": cfg.get("ladder"), "exits": cfg.get("exits"), "routing": cfg.get("routing"), "risk": risk}


def live_digest(user_settings, kcfg=None, cfg=None):
    """Digest of the settings the user chose for a live run: config.json as written (see
    build_config: args.user_settings), the kimi.json sections when the Kimi brain is used, and - with
    the Kimi brain - resting_settings(cfg). Comments ("_..." keys), formatting and key order do not
    count; any changed value, and the choice between the Kimi brain and a strategy, does. Other
    built-in defaults of the code are not part of it, so installing a new version (update.sh) does
    not invalidate a confirmation - unless it changes what the bot places on its own (the resting
    orders) or the digest version (DIGEST_VERSION)."""
    doc = {"v": DIGEST_VERSION, "settings": _strip_comments(user_settings or {}),
           "kimi": _strip_comments(dict(kcfg)) if kcfg is not None else None,
           "resting": _strip_comments(resting_settings(cfg)) if kcfg is not None else None,
           "code_rules": _strip_comments(code_exit_rules(kcfg)) if kcfg is not None else None}
    raw = json.dumps(doc, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_brain(cfg, kcfg, sd, mode, dry_run=False, test_reply=None, test_news=None):
    """(llm, brain, builder, news) for the runner. The builder gets its own PUBLIC Bitpin client (no
    keys). news: the stage-1 NewsResearcher, or None (no "news" section, news.enabled false, or the
    stage-2 test hook without --test-kimi-news). Dry runs keep the brain's files (decision log, LLM
    budget, news cache) in a temporary directory, so they never change the schedule of the real
    bot. test_reply / test_news: paper-only test hooks (no request reaches Moonshot)."""
    from bitpin.brain import build_kimi, build_news
    from bitpin.llm import ConfigError
    if (test_reply is not None or test_news is not None) and mode != "paper":
        die("the Kimi test hook is only available in paper mode")      # defence in depth: no live flag exists
    if test_news is not None and test_reply is None:
        die("--test-kimi-news needs --test-kimi-reply (the test hooks never mix canned and real Kimi calls)")
    ctx = kcfg.get("context") or {}
    if str(ctx.get("irt_asset", "IRT")).strip().upper() != "IRT" or float(ctx.get("irt_unit_divisor", 1) or 1) != 1:
        die("kimi config: context.irt_asset must be \"IRT\" and context.irt_unit_divisor 1 when the bot runs the brain "
            "(the bot passes its own balances, already converted with config.json irt_asset_code / irt_unit_divisor)")
    brain_sd = sd
    if dry_run:
        brain_sd = tempfile.mkdtemp(prefix="kimi_dry_run_")
        for f in ("news_cache.json", "news_budget.json"):      # a dry run reuses the bot's cached brief
            if os.path.isfile(os.path.join(sd, f)):
                shutil.copy2(os.path.join(sd, f), os.path.join(brain_sd, f))
        log.info("dry run: Kimi decision log, budget and news cache in the temporary directory %s", brain_sd)
    env = transport = news_transport = None   # default: the clients' own transports ($KIMI_HTTPS_PROXY / proxy)
    if test_reply is not None:
        model = ((kcfg.get("llm") or {}).get("model")) or "test-model"
        transport = make_test_transport(test_reply, model)
        from bitpin.news import news_key_env
        key_env = str((kcfg.get("llm") or {}).get("api_key_env") or "KIMI_API_KEY")
        news_key = news_key_env(kcfg)
        env = {key_env: TEST_HOOK_KEY, news_key: TEST_HOOK_KEY}
        # Defence in depth for this (paper-only) test process: the real Kimi key is removed from the
        # environment and any other Moonshot client would go to a dead local proxy (discard port),
        # so nothing can reach - or authenticate at - the real Moonshot API.
        for name in {key_env, news_key, "KIMI_API_KEY"}:
            os.environ[name] = TEST_HOOK_KEY
        os.environ[KIMI_PROXY_ENV] = TEST_HOOK_DEAD_PROXY
        log.warning("TEST HOOK (paper only): Kimi replies come from %s, NOT from Moonshot",
                    "a simulated network failure" if test_reply == TEST_HOOK_UNREACHABLE else test_reply)
        if test_news is not None:
            nmodel = str(((kcfg.get("news") or {}).get("model")) or "kimi-k2.6")
            news_transport = make_test_news_transport(test_news, nmodel)
            log.warning("TEST HOOK (paper only): the news brief comes from %s, NOT from Moonshot",
                        "a simulated network failure" if test_news == TEST_HOOK_UNREACHABLE else test_news)
    public = BitpinClient(cfg["base_url"])   # public market data only; never has credentials
    news = None
    try:
        # Who this process is, stamped on every decision (Decision.origin). The runner refuses to execute a
        # decision made by another one, so a paper run - or a canned --test-kimi-reply - that shares a
        # state dir with the live bot can never leave a decision behind for it to trade.
        origin = mode + (":test-hook" if test_reply is not None else "") + (":dry-run" if dry_run else "")
        llm, brain, builder = build_kimi(kcfg, public, brain_sd, runner_cfg=cfg, transport=transport, env=env,
                                         origin=origin)
        if test_reply is None:
            news = build_news(kcfg, brain_sd)
        elif test_news is not None:
            news = build_news(kcfg, brain_sd, transport=news_transport, env=env)
    except (ConfigError, TypeError, ValueError, FileNotFoundError) as e:
        die("kimi config error: %s" % e)
    if test_reply is not None and test_news is None and kcfg.get("news") is not None:
        log.warning("TEST HOOK (paper only): news research disabled (no --test-kimi-news) - no request reaches "
                    "Moonshot")
    if not llm.model:
        die("kimi config: llm.model is not set. List the models your key can use with:\n"
            "  run_bot.py kimi-check --kimi-config PATH\nand write one id in quotes as llm.model.")
    if not llm.has_key:
        die("%s is not set: the bot cannot ask Kimi. Put the key in the environment (on the server: "
            "/etc/bitpin-bot/bitpin-bot.env) and check it with: run_bot.py kimi-check --kimi-config PATH"
            % llm.cfg.get("api_key_env", "KIMI_API_KEY"))
    if news is not None and not news.has_key:
        die("%s is not set: the news researcher cannot run (put the key in the environment; on the server: "
            "/etc/bitpin-bot/bitpin-bot.env)" % news.api_key_env)
    return llm, brain, builder, news


def _teh(ts):
    """An epoch as 'YYYY-MM-DD HH:MM' Tehran time ('-' for None)."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(ts) + 3.5 * 3600))
    except (TypeError, ValueError):
        return "-"


def _g(x):
    """A number without a needless '.0' (48.0 -> 48); anything else as str."""
    try:
        return "%g" % float(x)
    except (TypeError, ValueError):
        return str(x)


def _pcts(levels):
    return " / ".join("%g%%" % float(x) for x in levels or [])


def schedule_line(bcfg):
    """The brain's decision schedule in one line (validated brain config)."""
    slots = list(bcfg.get("decision_times_local") or [])
    if slots:
        return ("%s Tehran (clock slot%s; an early call never moves it; safety net: a decision after %g h without "
                "one) + wake-ups: a ladder fill -> REVIEW, a ladder coin %g%% below its 48 h high -> VETO, a held coin "
                "+-%g%% -> HELD_MOVE, the drawdown +%g points with coins >= %g%% -> RISK_REDUCE; USDT_IRT +-%s%% and a "
                "smaller drawdown only notify" % (
                    ("ONCE A DAY at %s" % slots[0]) if len(slots) == 1 else "at " + ", ".join(slots),
                    "s" if len(slots) > 1 else "", float(bcfg.get("max_gap_hours", 26)),
                    float(bcfg.get("veto_drop_pct") or 15), float(bcfg.get("held_move_pct", 8)),
                    float(bcfg.get("drawdown_trigger_points", 3)), 100 * float(bcfg.get("risk_reduce_min_coin_weight", 0.1)),
                    "%g" % float(bcfg["usdt_notify_pct"]) if bcfg.get("usdt_notify_pct") else "-"))
    return ("a decision every %sh (legacy elapsed-time schedule: brain.decision_times_local is empty; earlier on "
            "moves above %s%%)" % (bcfg.get("decision_interval_hours"), bcfg.get("event_move_pct")))


def pacing_line(bcfg, llm_cfg=None, news_cfg=None):
    llm_cfg, news_cfg = llm_cfg or {}, news_cfg or {}
    txt = ("at least %g min between Kimi calls, at most %s decisions / %s early ones per 24 h, the last %s LLM calls of "
           "the day kept for review / veto" % (float(bcfg.get("min_decision_spacing_minutes", 0)),
                                               bcfg.get("max_decisions_per_day"), bcfg.get("max_early_decisions_per_day"),
                                               bcfg.get("reserve_llm_calls")))
    if llm_cfg:
        txt += "; LLM budget %s calls and %s tokens per UTC day" % (llm_cfg.get("max_calls_per_day"),
                                                                   llm_cfg.get("max_tokens_per_day"))
    if news_cfg:
        txt += "; news brief reused for %g min, at most %s research calls per day" % (
            float(news_cfg.get("cache_minutes", 0)), news_cfg.get("max_calls_per_day"))
    return txt


def ladder_text(cfg, runner=None, coins=None):
    """The crash ladder in one line (cfg: the runner config; runner: to say whether it is really on)."""
    lc = (cfg or {}).get("ladder") or {}
    on = bool(lc.get("enabled")) if runner is None else bool(getattr(runner, "ladder_on", False))
    coins = list(coins if coins is not None else (getattr(runner, "ladder_coins", None) or lc.get("coins")
                                                   or ["BTC", "ETH", "XRP", "SOL"]))
    if not on:
        why = "ladder.enabled is false" if not lc.get("enabled") else "needs the Kimi brain (brain mode)"
        return "OFF (%s)" % why
    return ("ON - resting maker BUY limits on %s at %s below the highest hourly close of the last %sh (USDT terms), "
            "%g%% of equity each x Kimi's scale per coin, paid from USDT (never more than the free USDT), re-armed "
            "above -%g%%, re-priced only beyond %g%% / re-sized beyond %g%%"
            % (" ".join("%s_USDT" % c for c in coins), _pcts(lc.get("levels_pct")), lc.get("lookback_hours", 48),
               100 * float(lc.get("size_frac", 0.125)), float(lc.get("rearm_pct", 7.5)),
               float(lc.get("reprice_pct", 0.5)), float(lc.get("resize_pct", 10))))


def exits_text(cfg, runner=None):
    from bitpin.brain import MAX_HOLD_HOURS, STOP_PCT_DEFAULT, STOP_PCT_MAX, STOP_PCT_MIN
    ec = (cfg or {}).get("exits") or {}
    on = bool(ec.get("enabled")) if runner is None else bool(getattr(runner, "exits_on", False))
    if not on:
        return "OFF (%s)" % ("exits.enabled is false" if not ec.get("enabled") else "needs the Kimi brain (brain mode)")
    # v3: STOP_PCT_DEFAULT is None (no default stop: a stop exists only where Kimi set stop_pct); an older
    # brain module with a numeric default is described as such, so the text never lies about what runs
    stop = ("STOP only where Kimi set stop_pct (NO default stop; %g..%g%% below the average entry, on an hourly "
            "close, market sell into USDT via the cheaper route)" % (STOP_PCT_MIN, STOP_PCT_MAX)) \
        if STOP_PCT_DEFAULT is None else \
        ("STOP -%g%% from its average entry on an hourly close (market sell into USDT via the cheaper route)"
         % STOP_PCT_DEFAULT)
    return ("ON - every coin position: %s, TARGET (%s): a crash-ladder fill = half the 48 h drop regained, any other "
            "position only when Kimi sets one, MAX HOLD %g h (capped by the endgame; then Kimi is woken); a ladder "
            "fill is its own position next to the coin's allocation (own entry, stop, target); Kimi may set exits per "
            "coin (stops clamped to %g..%g%%)"
            % (stop, "a resting maker SELL on COIN_USDT" if ec.get("target_orders", True)
               else "sold at the hourly close", MAX_HOLD_HOURS, STOP_PCT_MIN, STOP_PCT_MAX))


def routing_text(cfg, runner=None):
    rc = (cfg or {}).get("routing") or {}
    on = bool(rc.get("enabled")) if runner is None else bool(getattr(runner, "routing_on", False))
    if not on:
        return "OFF (every trade through the toman markets)"
    coins = rc.get("coins")
    return ("ON - USDT <-> %s legs go directly through COIN_USDT when its book is cheaper than COIN_IRT + USDT_IRT "
            "(about 0.8-1.1%% per round trip instead of about 1.8%%)" % ("/".join(coins) if coins else "every coin"))


def endgame_text(bcfg):
    eg = (bcfg or {}).get("endgame")
    if not eg:
        return "off (brain.endgame null)"
    return ("from %s Tehran no new coin entries and every ladder bid is cancelled; the decision at %s is FINAL (coins "
            "stay only if Kimi keeps them explicitly; default USDT_IRT, never toman)"
            % (_teh(eg.get("no_new_entries_at")), _teh(eg.get("final_at"))))


def guards_line(bcfg, runner=None):
    """The anti-pump buy guard and the entry plans in one line (validated brain config + runner)."""
    from bitpin.analysis import validate_guard_config
    try:
        g = validate_guard_config((bcfg or {}).get("guard"))
    except Exception:  # noqa: BLE001 - informational; the config was validated at start
        g = validate_guard_config(None)
    if g.get("pump_rise_pct") is None:
        pump = "pump guard OFF (guard.pump_rise_pct null)"
    else:
        pump = ("pump guard ON - no buy of a coin whose USDT price rose %g%%+ within %g h in the last %g h, by a "
                "decision or the crash ladder (until %g h after the pump; selling is allowed)"
                % (float(g["pump_rise_pct"]), float(g["pump_window_hours"]), float(g["pump_lookback_hours"]),
                   float(g["pump_lookback_hours"])))
    exits = runner is None or bool(getattr(runner, "exits_on", False))
    if not exits:
        plans = "entry plans off (they need the code exits)"
    elif (bcfg or {}).get("require_plan", True):
        plans = ("entry plans REQUIRED - every new position needs Kimi's plan (setup, horizon, invalidation level), kept "
                 "with the position and shown back to Kimi; an hourly close below the invalidation wakes Kimi (nothing "
                 "is sold by it)")
    else:
        plans = "entry plans optional (brain.require_plan false)"
    return "%s; %s" % (pump, plans)


def bot_version():
    """bitpin.__version__ (the release the code is, CHANGELOG.md) for the banners; "unknown" with a
    package that does not carry one (an older tree): the banner must never fail over a label."""
    try:
        import bitpin
        v = getattr(bitpin, "__version__", None)
        return str(v) if v else "unknown"
    except Exception:  # noqa: BLE001 - informational
        return "unknown"


def brain_banner_lines(llm, brain, test_hook=False, news=None, runner=None):
    bcfg = getattr(brain, "cfg", None) or {}
    fb = bcfg.get("fallback") or {}
    lim = getattr(brain, "limits", {}) or {}
    route = "TEST HOOK (no request to Moonshot)" if test_hook else getattr(llm, "proxy_display", "none (direct)")
    fcl = getattr(brain, "fallback_cash_limit", None)
    try:
        limit = "%g%%" % round(float(fcl()) * 100, 2) if callable(fcl) else "max_irt_cash"
    except Exception:  # noqa: BLE001 - informational
        limit = "the cash limit"
    if news is not None:
        nstream = getattr(getattr(news, "_stream", None), "streaming", news.cfg.get("stream"))
        news_txt = "stage 1 model %s (web search), reused %g min, max %d calls/day, route %s, %s" % (
            news.model, news.cfg["cache_minutes"], news.cfg["max_calls_per_day"],
            "TEST HOOK (no request to Moonshot)" if test_hook else news.proxy_display,
            "streamed" if nstream else "NOT streamed")
    else:
        news_txt = "none (decisions without news)"
    streaming = getattr(llm, "streaming", None)
    effort = getattr(llm, "cfg", None) or {}
    effort = effort.get("reasoning_effort") if isinstance(effort, dict) else None
    slot_effort = bcfg.get("slot_reasoning_effort")
    lines = [
        "Version       : bitpin-bot %s" % bot_version(),
        "Kimi          : model %s at %s (key %s), Kimi proxy %s, %s, reasoning effort %s%s" % (
            llm.model, llm.base_url, "set" if llm.has_key else "NOT SET", route,
            "streamed (the tunnel never sees a silent connection)" if streaming else
            "NOT streamed (a silent thinking call can be cut by the tunnel after ~2 min)",
            effort or "API default", (" (the 13:00 slot: %s)" % slot_effort) if slot_effort else ""),
        "Kimi news     : %s" % news_txt,
        "Kimi analysis : the reply's analysis block (p / ev / hurdle / headroom) is checked by code, policy %s (%s)"
        % (bcfg.get("analysis_policy", "off"),
           {"off": "notes only", "block": "sent back once, then the failing coin is not increased",
            "error": "rejected on every attempt"}.get(bcfg.get("analysis_policy", "off"), "?")),
        "Bitpin route  : direct (environment proxy variables are ignored)",
        "Kimi limits   : risk_profile %s, %s" % (bcfg.get("risk_profile"), json.dumps(lim, sort_keys=True)),
        "Kimi schedule : %s; allowed %s" % (schedule_line(bcfg), ", ".join(getattr(brain, "allowed", []) or [])),
        "Kimi pacing   : %s" % pacing_line(bcfg, getattr(llm, "cfg", None), getattr(news, "cfg", None)),
        "Kimi guards   : %s" % guards_line(bcfg, runner),
    ]
    if runner is not None and getattr(runner, "cfg", None):
        rcfg = runner.cfg
        lines += ["Crash ladder  : %s" % ladder_text(rcfg, runner),
                  "Code exits    : %s" % exits_text(rcfg, runner),
                  "Routing       : %s" % routing_text(rcfg, runner)]
    lines += [
        "Endgame       : %s" % endgame_text(bcfg),
        "Without Kimi  : toman above %s is swept into USDT_IRT when there is no valid Kimi decision; after %s h "
        "without a valid decision the coins are moved into USDT_IRT (derisk; coins with live code exits are left to "
        "them)" % (limit, _g(fb.get("derisk_after_hours", 12))),
    ]
    if runner is not None and (getattr(runner, "ladder_on", False) or getattr(runner, "exits_on", False)):
        lines.append("Resting orders: STOP, seen by the running bot, cancels the crash-ladder bids; the target sells "
                     "stay. After systemctl stop or a crash BOTH stay on Bitpin: run_bot.py cancel-resting")
    return lines


# paper mode: a resting order's remainder fills on a trade 0.5% THROUGH its price (the research spec's
# robustness variant), not on a touch - there is no queue position in the paper model, and the thin
# XRP_USDT / SOL_USDT books would otherwise overstate the ladder fills
PAPER_FILL_THROUGH = "0.005"


def run(runner, args, state_dir, dry_run):
    if args.once:
        rep = runner.run_once_with_retry(max_wait=args.max_wait)
        print()
        print(format_report(rep))
        return 0 if rep.get("status") in ("ok", "dry_run", "already_processed") else 2
    status = runner.loop()
    print("loop ended: %s" % status)
    return 0


# --------------------------------------------------------------------------- commands

def release_all(locks):
    for lk in reversed(locks):
        lk.release()


def cmd_paper(args):
    cfg = build_config(args)
    sd = cfg["state_dir"]
    kcfg = load_kimi(args.kimi_config) if args.brain else None
    if (args.test_kimi_reply is not None or args.test_kimi_news is not None) and not args.brain:
        die("--test-kimi-reply / --test-kimi-news need --brain kimi")
    locks = []
    try:
        if not args.dry_run:  # the lock comes BEFORE any state file is read or written
            locks.append(StateLock(sd).acquire())
        setup_logging(sd, "paper", args.log_level, secrets=kimi_secrets(kcfg) if kcfg else ())
        if args.reset_paper and args.dry_run:
            print("--reset-paper is ignored with --dry-run (nothing is archived or created)")
        elif args.reset_paper:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for f in (PaperBroker.STATE_FILE, "runner_state_paper.json", "risk_state_paper.json"):
                p = os.path.join(sd, f)
                if os.path.exists(p):
                    os.replace(p, "%s.bak-%s" % (p, stamp))
                    print("moved %s -> %s.bak-%s" % (p, p, stamp))
        strategy = None if args.brain else make_strategy(cfg)
        client = BitpinClient(cfg["base_url"])  # public endpoints only - no keys in paper mode
        markets = MarketCache(client, sd)
        # dry run: an existing account is read, a missing one lives in memory only
        ft = getattr(args, "fill_through", None)
        ft = D(PAPER_FILL_THROUGH) if ft is None else ft
        if not ZERO <= ft < D("0.2"):
            die("--fill-through must be between 0 and 0.2 (a fraction: 0.005 = 0.5%%), got %s" % ft)
        broker = PaperBroker(markets, sd, capital_irt=args.capital_irt, fee_rate=cfg["fee_rate"], client=client,
                             persist=not args.dry_run, fill_through=ft)
        log.info("paper fill model: a resting order fills when the live book crosses it, or when the market traded "
                 "%s beyond its price (--fill-through %s; no queue position is modelled)",
                 "at or" if ft == 0 else "%s%%" % (ft * 100).normalize(), ft)
        risk = make_risk(cfg, sd, "paper", persist=not args.dry_run)
        brain = builder = news = llm = None
        if args.brain:
            llm, brain, builder, news = make_brain(cfg, kcfg, sd, "paper", dry_run=args.dry_run,
                                                   test_reply=args.test_kimi_reply, test_news=args.test_kimi_news)
        runner = Runner(strategy, broker, risk, cfg, sd, "paper", dry_run=args.dry_run, brain=brain,
                        context_builder=builder, news=news)
        if brain is not None:
            for line in brain_banner_lines(llm, brain, test_hook=args.test_kimi_reply is not None, news=news,
                                           runner=runner):
                log.info(line)
        return run(runner, args, sd, args.dry_run)
    finally:
        release_all(locks)


def print_banner(strategy, sd, broker, risk, runner, cfg, bal):
    print()
    print("Version       : bitpin-bot %s" % bot_version())
    print("Strategy      : %r (res %s)" % (strategy, strategy.res))
    print("State dir     : %s" % os.path.abspath(sd))
    print("IRT (toman)   : %s total, %s available" % (fmt_amount(bal.get("IRT", ZERO)),
                                                      fmt_amount(broker.available().get("IRT", ZERO))))
    for a in sorted(bal):
        if a != "IRT" and bal[a]:
            print("%-14s: %s" % (a, bal[a]))
    print("Risk limits   : %s" % json.dumps(risk.cfg, default=str))
    print("Equity cap    : %s" % (("%s IRT (the bot trades only its own sleeve)" % fmt_amount(cfg["max_equity_irt"]))
                                  if cfg.get("max_equity_irt") is not None
                                  else "none (whole IRT balance + strategy coins)"))
    for line in runner.management_summary(bal):
        print("Managed       : %s" % line)
    print("Kill switch   : create %s to stop" % os.path.join(os.path.abspath(sd), KILL_SWITCH_FILE))


def _mismatch_detail(rec, kcfg):
    """Why an existing LIVE_CONFIRMED does not match (for the refusal message)."""
    was = "the Kimi brain" if rec.get("brain") else "a strategy"
    now = "the Kimi brain" if kcfg is not None else "a strategy"
    if was != now:
        return "it was confirmed for %s, this run uses %s" % (was, now)
    try:
        old = int(rec.get("digest_version") or 2)
    except (TypeError, ValueError):
        old = 2
    if old < DIGEST_VERSION:
        return ("it was confirmed on %s by an older version of the bot; this version's confirmation also covers what "
                "the bot does on its own (RESTING crash-ladder bids and target sells, the rules of the code exits), so "
                "the settings must be confirmed again" % rec.get("confirmed_utc"))
    return "config.json%s changed since %s%s" % (" or kimi.json" if kcfg is not None else "", rec.get("confirmed_utc"),
                                                 " (or the resting-order settings in force)" if kcfg is not None else "")


def check_live_confirmed(sd, user_settings, kcfg, cfg=None):
    """--non-interactive: the LIVE_CONFIRMED written by confirm-live must exist and match the
    settings of this run. Exit 78 (not restarted by systemd) otherwise."""
    path = os.path.join(sd, LIVE_CONFIRMED_FILE)
    rec = read_json(path)
    how = "Run once, interactively:  sudo bitpin-bot confirm-live   (or: run_bot.py confirm-live --state-dir %s " \
          "--config ... [--kimi-config ...], with the SAME files as this command)" % sd
    if not isinstance(rec, dict) or not rec.get("config_digest"):
        die("non-interactive live trading is not confirmed for %s (no %s).\n%s" % (os.path.abspath(sd),
                                                                                 LIVE_CONFIRMED_FILE, how))
    want = live_digest(user_settings, kcfg, cfg)
    if rec.get("config_digest") != want:
        die("the settings differ from the ones confirmed with confirm-live (%s): not trading.\n%s"
            % (_mismatch_detail(rec, kcfg), how))
    return rec


def cmd_live(args):
    cfg = build_config(args)
    sd = cfg["state_dir"]
    kcfg = load_kimi(args.kimi_config) if args.brain else None
    strategy = None if args.brain else make_strategy(cfg)
    confirmed_rec = None
    if not args.dry_run:
        if not args.i_accept_the_risk:
            die("live trading sends REAL orders. Re-run with --i-accept-the-risk (or use --dry-run).")
        ok, conf = confirmation_ok(sd, cfg)
        if not ok:
            die("the IRT balance has not been confirmed for irt_asset_code=%s irt_unit_divisor=%s.\n"
                     "Run first:  py scripts/run_bot.py status --state-dir %s  and confirm the toman balance."
                     % (cfg["irt_asset_code"], cfg["irt_unit_divisor"], sd))
        if args.non_interactive:
            confirmed_rec = check_live_confirmed(sd, args.user_settings, kcfg, cfg)
        elif not sys.stdin.isatty():
            die("live trading needs an interactive terminal for the typed confirmation (for a service: run "
                "confirm-live once, then live --i-accept-the-risk --non-interactive)")
    key, secret, swappable = load_credentials(args.keys_file)
    fp = account_fingerprint(key, secret)
    setup_logging(sd, args.cmd, args.log_level, secrets=(key, secret) + (kimi_secrets(kcfg) if kcfg else ()))
    locks = []
    try:
        if not args.dry_run:
            # BEFORE the order journal, risk state and runner state are read: a second instance can
            # never act on state that the first one is still writing (also across state dirs).
            locks.append(account_lock(fp).acquire())
            locks.append(StateLock(sd).acquire())
        check_account_binding(sd, fp, write=not args.dry_run)
        client, markets, broker = make_live(cfg, sd, key, secret, swappable)
        del key, secret
        risk = make_risk(cfg, sd, "live", persist=not args.dry_run)
        brain = builder = llm = news = None
        if args.brain:
            llm, brain, builder, news = make_brain(cfg, kcfg, sd, "live", dry_run=args.dry_run)
        runner = Runner(strategy, broker, risk, cfg, sd, "live", dry_run=args.dry_run, brain=brain,
                        context_builder=builder, news=news)
        strategy = runner.strategy
        try:
            broker.refresh()
            bal = broker.balances()
        except AuthError as e:
            die("authentication problem: %s" % e)
        except AuthBudgetExceeded as e:
            # exit 78 (not 1): a restart cannot refill the 24 h login budget
            die("authentication problem: %s" % e)
        print_banner(strategy, sd, broker, risk, runner, cfg, bal)
        if brain is not None:
            for line in brain_banner_lines(llm, brain, news=news, runner=runner):
                print(line)
            for line in resting_state_lines(sd, "live", broker):
                print(line)
        if args.dry_run:
            if not confirmation_ok(sd, cfg)[0]:
                print("NOTE: the IRT balance is not confirmed yet (run 'status'); real trading would be refused.")
            print("DRY RUN: no orders will be sent or cancelled and no bot state is changed.\n")
            return run(runner, args, sd, True)
        print()
        if args.non_interactive:
            log.warning("LIVE trading, non-interactive: authorized by %s (confirmed %s, settings digest %s...)",
                        LIVE_CONFIRMED_FILE, confirmed_rec.get("confirmed_utc"), confirmed_rec["config_digest"][:12])
            return run(runner, args, sd, False)
        print("This will place REAL market orders on Bitpin with the balances above.")
        try:
            typed = input('Type "%s" to start: ' % LIVE_PHRASE)
        except EOFError:
            typed = ""
        if typed.strip() != LIVE_PHRASE:
            sys.exit("not confirmed - nothing was sent")
        return run(runner, args, sd, False)
    finally:
        release_all(locks)


def check_confirmation_only(sd, cfg, user_settings, kcfg):
    """confirm-live --check: would 'live --non-interactive' with these settings start? Read-only,
    no terminal needed (update.sh runs it with the NEW code before it stops the running bot).
    Exit 0 = yes, 1 = no (the reason is printed)."""
    ok, _ = confirmation_ok(sd, cfg)
    rec = read_json(os.path.join(sd, LIVE_CONFIRMED_FILE))
    if not ok:
        print("NOT CONFIRMED: the toman (IRT) balance is not confirmed in %s (run status first)" % os.path.abspath(sd))
        return 1
    if not isinstance(rec, dict) or not rec.get("config_digest"):
        print("NOT CONFIRMED: no %s in %s (run confirm-live)" % (LIVE_CONFIRMED_FILE, os.path.abspath(sd)))
        return 1
    if rec.get("config_digest") != live_digest(user_settings, kcfg, cfg):
        print("NOT CONFIRMED: the settings differ from the ones confirmed on %s (%s; run confirm-live again)"
              % (rec.get("confirmed_utc"), _mismatch_detail(rec, kcfg)))
        return 1
    print("CONFIRMED: these settings were confirmed for live trading on %s" % rec.get("confirmed_utc"))
    return 0


def cmd_confirm_live(args):
    """One-time interactive confirmation that allows 'live --i-accept-the-risk --non-interactive'
    (the systemd service) for exactly these settings. No network, no keys needed."""
    cfg = build_config(args)
    sd = cfg["state_dir"]
    kcfg = None
    if args.kimi_config:
        from bitpin.brain import check_kimi_config
        kcfg = load_kimi(args.kimi_config)
        warnings = []
        problems = check_kimi_config(args.kimi_config, require_model=True, runner_cfg=cfg, warnings=warnings)
        if problems:
            die("kimi config %s is not usable:\n  %s" % (args.kimi_config, "\n  ".join(problems)))
        for w in warnings:
            print("WARNING: kimi config: %s" % w)
    if getattr(args, "check", False):
        return check_confirmation_only(sd, cfg, args.user_settings, kcfg)
    show_only = bool(getattr(args, "show", False))
    typed_arg = getattr(args, "typed_phrase", None)
    if not show_only and typed_arg is None and not sys.stdin.isatty():
        die("confirm-live is interactive: run it in a terminal (on the server: sudo bitpin-bot confirm-live), "
            "or give the phrase the owner typed with --typed-phrase (the management panel does)")
    ok, conf = confirmation_ok(sd, cfg)
    if not ok:
        die("the IRT (toman) balance has not been confirmed for irt_asset_code=%s irt_unit_divisor=%s in %s.\n"
            "Run first:  status --state-dir %s  (on the server: sudo bitpin-bot status) and confirm the toman balance."
            % (cfg["irt_asset_code"], cfg["irt_unit_divisor"], os.path.abspath(sd), sd))
    digest = live_digest(args.user_settings, kcfg, cfg)
    print("Live trading confirmation for the service (live --i-accept-the-risk --non-interactive)")
    print("  state dir      : %s" % os.path.abspath(sd))
    print("  config         : %s" % (os.path.abspath(args.config) if args.config else "(built-in defaults)"))
    if kcfg is not None:
        llm, brain, news = kcfg.get("llm") or {}, kcfg.get("brain") or {}, kcfg.get("news")
        print("  decisions      : Kimi brain (%s), model %s, risk_profile %s" % (
            os.path.abspath(args.kimi_config), llm.get("model"), brain.get("risk_profile", "balanced")))
        print("  news           : %s" % (
            "stage 1 model %s (web search)" % (news.get("model") or "kimi-k2.6")
            if isinstance(news, dict) and news.get("enabled", True) is not False else "none (decisions without news)"))
        fb = brain.get("fallback") if isinstance(brain.get("fallback"), dict) else {}
        sweep = fb.get("sweep_irt_above", 0.05)
        derisk_h = fb.get("derisk_after_hours", 12)
        cash_txt = ("%g%%" % (float(sweep) * 100)) if isinstance(sweep, (int, float)) else str(sweep)
        if brain.get("risk_profile") == "full":
            cash_txt += " (or the toman share of the last valid decision while it is younger than the derisk delay)"
        print("                   without a valid Kimi decision: toman above the cash limit %s goes into USDT_IRT,"
              % cash_txt)
        print("                   and after the derisk delay (%s) the coins are moved into USDT_IRT (coins with live "
              "code exits are left to them)" % ("disabled" if not derisk_h else "%g h" % float(derisk_h)))
        orders = (cfg.get("risk") or {}).get("max_orders_per_day")
        if orders is not None:
            print("  order budget   : at most %s orders in any rolling 24 h (a rebalance that does not fit is "
                  "skipped whole)" % orders)
        from bitpin.brain import validate_brain_config
        from bitpin.llm import ConfigError, validate_llm_config
        from bitpin.news import NewsConfigError, validate_news_config
        try:                             # check_kimi_config above already refused an invalid file
            bv, lv = validate_brain_config(brain), validate_llm_config(llm)
            nv = validate_news_config(news) if isinstance(news, dict) and news.get("enabled", True) is not False \
                else None
        except (ConfigError, NewsConfigError, ValueError):
            bv, lv, nv = {}, {}, None
        rs = resting_settings(cfg)
        print("  schedule       : %s" % schedule_line(bv))
        print("  pacing         : %s" % pacing_line(bv, lv, nv))
        print("  Kimi calls     : %s, timeout %ss per request, %ss per call%s" % (
            "streamed (SSE)" if lv.get("stream") else "NOT streamed", _g(lv.get("timeout")),
            _g(lv.get("deadline_seconds")),
            ("; news: %ss / %ss, %s" % (_g(nv.get("timeout")), _g(nv.get("deadline_seconds")),
                                       "streamed" if nv.get("stream") else "NOT streamed")) if nv else ""))
        print("  crash ladder   : %s" % ladder_text(cfg, coins=(cfg.get("ladder") or {}).get("coins")
                                                     or bv.get("ladder_coins")))
        print("  code exits     : %s" % exits_text(cfg))
        print("  routing        : %s" % routing_text(cfg))
        print("  endgame        : %s" % endgame_text(bv))
        print("  minimum orders : %s toman on IRT markets, %s USDT on USDT markets; resting limit orders: at most %s "
              "placements per rolling 24 h, never further than %g%% from the mid"
              % (fmt_amount(D((cfg.get("risk") or {}).get("min_order_irt", DEFAULT_RISK["min_order_irt"]))),
                 rs["risk"]["min_order_usdt"], rs["risk"]["max_limit_orders_per_day"],
                 100 * float(rs["risk"]["max_limit_distance"])))
        if (cfg.get("ladder") or {}).get("enabled") or (cfg.get("exits") or {}).get("enabled"):
            print("  RESTING ORDERS : the bot places real limit orders that REST on Bitpin (ladder bids from its USDT, "
                  "target sells of its coins) - right after the start. STOP, seen by the running bot, cancels the "
                  "ladder bids; the target sells stay. After systemctl stop or a crash BOTH stay on Bitpin: "
                  "run_bot.py cancel-resting cancels them")
    else:
        print("  decisions      : strategy %s %s" % (cfg["strategy"], json.dumps(cfg.get("params") or {})))
    print("  toman          : %s x%s confirmed on %s (%s IRT seen then)" % (
        cfg["irt_asset_code"], cfg["irt_unit_divisor"], conf.get("confirmed_utc"), conf.get("irt_balance_seen")))
    print("  equity cap     : %s" % ("%s IRT (bot sleeve)" % fmt_amount(D(cfg["max_equity_irt"]))
                                     if cfg.get("max_equity_irt") is not None else "none: the WHOLE toman balance"))
    print("  risk limits    : %s" % json.dumps(cfg.get("risk") or {}, default=str, sort_keys=True))
    print("  settings digest: %s" % digest)
    print()
    print("After this, the service may place REAL %sorders on Bitpin without asking again, as long as"
          % ("market and resting limit " if kcfg is not None else "market "))
    print("these settings stay the same (any change of config.json%s needs a new confirm-live)."
          % (" or kimi.json" if kcfg is not None else ""))
    if show_only:                       # v3.1: the panel shows this summary before the owner types the phrase
        print("(--show: the summary only - nothing was asked or written)")
        return 0
    if typed_arg is not None:           # v3.1: the phrase the owner typed in the management panel
        typed = typed_arg
        print('Typed phrase given with --typed-phrase: "%s"' % typed.strip()[:40])
    else:
        try:
            typed = input('Type "%s" to confirm: ' % LIVE_PHRASE)
        except EOFError:
            typed = ""
    if typed.strip() != LIVE_PHRASE:
        print("not confirmed - nothing was written")
        return 1
    rec = {"version": 1, "confirmed_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
           "confirmed_ts": int(time.time()), "config_digest": digest, "digest_version": DIGEST_VERSION,
           "config_path": os.path.abspath(args.config) if args.config else None,
           "kimi_config_path": os.path.abspath(args.kimi_config) if args.kimi_config else None,
           "brain": "kimi" if kcfg is not None else None,
           "strategy": None if kcfg is not None else cfg["strategy"],
           "irt_asset_code": str(cfg["irt_asset_code"]).upper(), "irt_unit_divisor": str(cfg["irt_unit_divisor"]),
           "confirmed_how": "typed-phrase" if typed_arg is not None else "terminal"}
    os.makedirs(sd, exist_ok=True)
    atomic_write_json(os.path.join(sd, LIVE_CONFIRMED_FILE), rec)
    print("confirmed: %s written. Start the service with: sudo systemctl enable --now bitpin-bot"
          % os.path.join(os.path.abspath(sd), LIVE_CONFIRMED_FILE))
    return 0


def stream_text(llm_cfg, news_cfg=None):
    """kimi-check: whether the chat calls of both stages are streamed (llm.stream / news.stream)."""
    s_llm = (llm_cfg or {}).get("stream", True) is not False
    txt = ("decision calls streamed (stream=true: bytes keep flowing while kimi-k3 thinks; the tunnel cuts a "
           "connection that stays silent ~2 min)" if s_llm else
           "decision calls NOT streamed (llm.stream false): a kimi-k3 call that thinks silently for ~2 min is cut by "
           "the tunnel - set llm.stream to true")
    if isinstance(news_cfg, dict) and news_cfg.get("enabled", True) is not False:
        txt += "; news calls %s" % ("streamed" if news_cfg.get("stream", True) is not False else
                                    "NOT streamed (news.stream false)")
    return txt


def cmd_kimi_check(args):
    """Kimi reachability / key / model check: no trading, no Bitpin call, the key is never shown.
    The route is exactly the bot's: LLMClient's own transport ($KIMI_HTTPS_PROXY or llm.proxy, else
    direct; http_proxy / https_proxy / ALL_PROXY are never used)."""
    from bitpin.llm import ConfigError, LLMAuthError, LLMClient, LLMError
    kcfg = load_kimi(args.kimi_config)
    llm_cfg = kcfg.get("llm") or {}
    key_env = str(llm_cfg.get("api_key_env") or "KIMI_API_KEY")
    raw = os.environ.get(key_env) or ""
    key = raw.strip()
    print("Kimi (Moonshot) check - no trading, no Bitpin requests")
    print("  config        : %s" % os.path.abspath(args.kimi_config))
    if key:
        extra = " (WARNING: leading/trailing spaces or a Windows line ending - fix the env file)" if raw != key else ""
        print("  %-13s : set (%d characters; the value is never shown)%s" % (key_env, len(key), extra))
    else:
        print("  %-13s : NOT SET" % key_env)
    env_px = sorted(n for n in os.environ if n.lower() in ("http_proxy", "https_proxy", "all_proxy") and os.environ[n])
    print("  Bitpin route  : direct%s" % ((" (%s set in the environment: ignored by the bot)" % ", ".join(env_px))
                                         if env_px else ""))
    tmp = None
    sd = args.state_dir
    if not sd:
        tmp = sd = tempfile.mkdtemp(prefix="kimi_check_")
    try:
        # a check must answer within about a minute, whatever the bot's own retry settings are
        quick = dict(llm_cfg, max_retries=1, timeout=30, deadline_seconds=90)
        try:
            llm = LLMClient(quick, state_dir=sd)
        except (ConfigError, TypeError, ValueError) as e:
            print("RESULT: config error - %s" % e)
            return EXIT_CONFIG
        proxy = llm.proxy
        print("  Kimi proxy    : %s%s" % (llm.proxy_display, (" (from %s)" % llm.proxy_source) if proxy else
                                          "  (to use a local HTTP proxy for Kimi only, set %s=http://127.0.0.1:PORT)"
                                          % KIMI_PROXY_ENV))
        print("  base_url      : %s" % llm.base_url)
        print("  model         : %s" % (llm.model or "NOT SET (pick one of the ids below)"))
        print("  streaming     : %s" % stream_text(llm_cfg, kcfg.get("news")))
        if not key:
            print("RESULT: FAILED - %s is not set. Put the key in the environment (server: /etc/bitpin-bot/bitpin-bot.env, "
                  "KIMI_API_KEY=...) and run kimi-check again." % key_env)
            return 1
        try:
            ids = sorted(set(llm.list_models()))
        except LLMAuthError as e:
            st = getattr(e, "status", None)
            if st == 401:
                why = ("HTTP 401: the key was REJECTED. Wrong/revoked key, or a key of the other platform (keys from "
                       "platform.moonshot.ai work only with https://api.moonshot.ai/v1, keys from platform.moonshot.cn "
                       "only with https://api.moonshot.cn/v1: check llm.base_url).")
            else:
                why = ("HTTP %s: access denied - most likely a REGION BLOCK of this server's IP. Kimi needs a proxy: if "
                       "you already run a local HTTP proxy, set %s=http://127.0.0.1:PORT (only Kimi traffic uses it; "
                       "Bitpin always goes direct)." % (st, KIMI_PROXY_ENV))
            print("RESULT: FAILED - %s" % llm.redact(why))
            return 1
        except LLMError as e:
            st = getattr(e, "status", None)
            if st in (403, 451):
                why = ("HTTP %s: blocked for this region. If you already run a local HTTP proxy, set "
                       "%s=http://127.0.0.1:PORT (only Kimi traffic uses it; Bitpin always goes direct)."
                       % (st, KIMI_PROXY_ENV))
            elif st == 429:
                why = "HTTP 429: rate limited, or the Moonshot account has no balance / quota (%s)" % e
            elif st is None:
                if proxy:
                    why = ("CONNECTION ERROR through the Kimi proxy %s: %s. Is the proxy running and does it allow "
                           "HTTPS (CONNECT)?" % (llm.proxy_display, e))
                else:
                    why = ("CONNECTION ERROR: %s. Moonshot is not reachable directly from this server (blocked or "
                           "filtered). If you already run a local HTTP proxy, set %s=http://127.0.0.1:PORT."
                           % (e, KIMI_PROXY_ENV))
            else:
                why = "HTTP %s: %s" % (st, e)
            print("RESULT: FAILED - %s" % llm.redact(why))
            return 1
        print("  reachable     : yes, the key is accepted; %d model ids:" % len(ids))
        for i in ids:
            print("    %s %s" % ("*" if i == llm.model else "-", i))
        if not llm.model:
            print("RESULT: FAILED - llm.model is not set: write one of the ids above in quotes in the kimi config, "
                  "e.g. \"model\": \"%s\" (a general kimi-k2 chat model that supports tool calls and JSON mode)"
                  % (ids[0] if ids else "kimi-k2-..."))
            return 1
        if llm.model not in ids:
            print("RESULT: FAILED - the configured model %r is not offered to this key; pick one of the ids above"
                  % llm.model)
            return 1
        ok_text = ("Kimi is reachable %s, the key works and model %s is listed for this key"
                   % (("through the proxy %s" % llm.proxy_display) if proxy else "directly", llm.model))
        want_news = bool(getattr(args, "news", False))
        want_decide = bool(getattr(args, "decide", False)) or want_news
        if not want_news and not want_decide:
            print("RESULT: OK - %s (no chat call was made: add --news to test both stages for real)" % ok_text)
            return 0
        print("  stage 2       : %s" % ok_text)
        rc = check_decision(kcfg, sd)          # ONE real, bounded kimi-k3 decision call
        if rc != 0:
            return rc
        if not want_news:
            print("RESULT: OK - the decision model %s answered a real decision request (stage 1 not tested: "
                  "add --news)" % llm.model)
            return 0
        return check_news(kcfg, ids, llm.model)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


DECIDE_CHECK_CONTEXT = {
    "clock": {"now_utc": "2026-09-23 12:00:00", "now_tehran": "2026-09-23 15:30", "days_left": 29},
    "portfolio": {"equity_irt": 5000000.0, "weights": {}, "balances": {"IRT": 5000000.0}, "drawdown_pct": 0.0,
                  "high_water_mark_irt": 5000000.0},
    "symbols": {"USDT_IRT": {"px": 230000.0, "ret_24h_pct": 0.4, "spread_pct": 0.05, "depth_irt": 900000000.0,
                             "stale": False},
                "BTC_IRT": {"px": 19800000000.0, "px_usdt": 86087.0, "ret_24h_pct": -1.2, "spread_pct": 0.2,
                            "depth_irt": 400000000.0, "stale": False}},
    # the bot's code exits run: the prompt then asks for an entry plan for every new position (as live)
    "features": {"ladder": True, "code_exits": True},
    "data_errors": {},
}


def check_decision(kcfg, state_dir):
    """kimi-check: ONE real stage-2 decision call with the SHIPPED settings (the configured model,
    JSON mode, no temperature, the configured max_tokens) and the real system prompt over a small
    synthetic market context. Being listed in GET /models says nothing about whether kimi-k3 answers
    a 15k-token decision request inside the timeout, stops thinking before max_tokens, and returns
    JSON the validator accepts - and until this ran, the FIRST real decision of the live bot was the
    first test of all three. Its own budget files stay in a fresh temporary directory."""
    from bitpin.analysis import usdt_prices
    from bitpin.brain import KimiBrain, ValidationError, validate_response
    from bitpin.llm import ConfigError, LLMClient, LLMError
    sd = tempfile.mkdtemp(prefix="kimi_decide_check_")
    try:
        llm_cfg = dict(kcfg.get("llm") or {})
        # the real max_tokens and JSON mode; only the retry/deadline budget is cut so a check ends
        llm_cfg.update(max_retries=1, deadline_seconds=min(float(llm_cfg.get("deadline_seconds") or 540), 300))
        try:
            llm = LLMClient(llm_cfg, state_dir=sd)
            brain = KimiBrain(llm, kcfg.get("brain") or {}, sd, origin="kimi-check")
        except (ConfigError, TypeError, ValueError, FileNotFoundError) as e:
            print("STAGE 2: config error - %s" % e)
            return EXIT_CONFIG
        ctx = json.loads(json.dumps(DECIDE_CHECK_CONTEXT))
        msgs = brain.build_messages(ctx, {}, web_search=False, news=None, now=time.time())
        chars = sum(len(m.get("content") or "") for m in msgs)
        print("  stage 2 test  : one real decision call, %s, JSON mode %s, max_tokens %s, prompt ~%d chars"
              % (llm.model, bool(brain.cfg["json_mode"]), llm.cfg.get("max_tokens"), chars))
        t0 = time.time()
        try:
            res = llm.chat(msgs, json_mode=bool(brain.cfg["json_mode"]))
        except LLMError as e:
            print("STAGE 2: FAILED - %s" % llm.redact(str(e)))
            print("RESULT: FAILED (decision) - the key and the model id are fine, but %s did not answer a real "
                  "decision request. Check llm.timeout / llm.deadline_seconds and the Kimi proxy." % llm.model)
            return 1
        secs = time.time() - t0
        u = res.get("usage") or {}
        reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens") \
            if isinstance(u.get("completion_tokens_details"), dict) else None
        print("  stage 2 reply : %.0f s, finish_reason %s, %s prompt + %s completion tokens%s"
              % (secs, res.get("finish_reason"), u.get("prompt_tokens", "?"), u.get("completion_tokens", "?"),
                 (", %s of them reasoning" % reasoning) if reasoning else ""))
        if res.get("streamed"):
            print("  streaming     : the reply arrived as an event stream (SSE)%s" % (
                " - usage estimated (the stream carried none)" if res.get("usage_estimated") else ""))
        elif llm_cfg.get("stream", True) is not False:
            note = getattr(llm, "stream_note", "") or "the API answered with one JSON object"
            print("  streaming     : WARNING - streaming was requested but the reply was NOT streamed (%s). A reply "
                  "that takes more than ~2 min can then be cut by the tunnel." % note)
        else:
            print("  streaming     : off (llm.stream false)")
        content = res.get("content") or ""
        if not content.strip() or res.get("finish_reason") == "length":
            print("STAGE 2: FAILED - the reply is EMPTY or was cut off at max_tokens=%s (finish_reason %s): the "
                  "model spent its whole budget on reasoning." % (llm.cfg.get("max_tokens"), res.get("finish_reason")))
            print("RESULT: FAILED (decision) - raise llm.max_tokens (and llm.timeout) in the kimi config")
            return 1
        try:
            obj = brain._parse_reply(content, res)
            # as the live bot validates it: with code exits (positions) every NEW position needs a plan
            v = validate_response(obj, {}, brain.allowed, brain.safe, brain.limits,
                                  float(brain.cfg["sum_tolerance"]), brain.cfg["decision_interval_hours"],
                                  float(brain.cfg["rebalance_threshold"]), px_usdt=usdt_prices(ctx), positions={},
                                  plan_policy="error" if brain.cfg.get("require_plan", True) else None)
        except ValidationError as e:
            print("STAGE 2: FAILED - the reply was REJECTED by the validator: %s" % llm.redact(str(e)))
            print("  reply began   : %s" % llm.redact(content[:200].replace("\n", " ")))
            print("RESULT: FAILED (decision) - %s answered, but not in the required format" % llm.model)
            return 1
        print("  stage 2 OK    : the validator accepted the reply; targets %s, cash_irt %.2f, confidence %.2f"
              % ({k: round(w, 3) for k, w in v["targets"].items() if w}, v["cash_irt"], v["confidence"]))
        for s, p in sorted((v.get("plans") or {}).items()):
            print("  entry plan    : %s %s, %d h, invalid %g USDT%s" % (
                s, p["setup"], p["horizon_hours"], p["invalidation_usdt"],
                (", target %g" % p["take_profit_usdt"]) if p.get("take_profit_usdt") else ""))
        print("  (this was a synthetic 2-symbol context, not a trading signal: nothing was executed)")
        return 0
    finally:
        shutil.rmtree(sd, ignore_errors=True)


def check_news(kcfg, ids, decision_model):
    """kimi-check --news: ONE forced, small research call of the stage-1 news researcher in a FRESH
    temporary directory (the bot's news budget, cool-down and cached brief can never turn it into a
    false OK, and the bot's news_cache.json is not touched). OK only for a NEWLY fetched brief:
    research(force=True) returns the old brief with ok=True, stale=True when a budget is used up."""
    from bitpin.brain import build_news
    from bitpin.llm import ConfigError
    if kcfg.get("news") is None:          # tested BEFORE building a quick override (it would create one)
        print("NEWS: not configured (no \"news\" section in the kimi config): the bot decides WITHOUT news")
        print("RESULT: FAILED (news) - copy the \"news\" section of kimi.example.json into the kimi config")
        return 1
    if (kcfg.get("news") or {}).get("enabled") is False:
        print("NEWS: disabled (news.enabled=false): the bot decides WITHOUT news")
        print("RESULT: FAILED (news) - set news.enabled to true")
        return 1
    quick = dict(kcfg, news=dict(kcfg["news"], **NEWS_CHECK_OVERRIDES))
    news_sd = tempfile.mkdtemp(prefix="kimi_news_check_")   # ALWAYS fresh, even with --state-dir
    try:
        try:
            news = build_news(quick, news_sd)
        except (ConfigError, ValueError) as e:
            print("NEWS: config error - %s" % e)
            return EXIT_CONFIG
        print("  news model    : %s (stage 1, web search; route %s)" % (news.model, news.proxy_display))
        llm_base = str((kcfg.get("llm") or {}).get("base_url") or "https://api.moonshot.ai/v1").strip().rstrip("/")
        # the ids were listed on the decision model's platform: they say nothing about a news stage elsewhere
        if news.base_url == llm_base and news.model not in ids:
            print("NEWS: FAILED - the news model %r is not offered to this key; pick one of the ids above "
                  "(kimi-k2.6 was verified with web search)" % news.model)
            print("RESULT: FAILED (news) - the decision model %s works, the news research does not" % decision_model)
            return 1
        b = news.research(time.time(), force=True)
        if not (b.ok and not b.cached and not b.stale):     # force does NOT bypass a budget / missing key
            print("NEWS: FAILED - %s" % news.redact(b.error or "no fresh brief"))
            print("RESULT: FAILED (news) - the decision model %s works, but the bot would decide WITHOUT news "
                  "(retried %d times; if the Kimi proxy dropped the connection, run the check again)"
                  % (decision_model, int(news.cfg["max_retries"])))
            return 1
        print("NEWS: OK - %d items, %d searches, %s tokens, %.0f s" % (
            len(b.items), b.searches, (b.usage or {}).get("total_tokens", "?"), b.seconds))
        print(b.prompt_block(time.time(), max_chars=4000))
        print("RESULT: OK - both stages were exercised for real: the decision model %s answered a decision "
              "request in the required format, and the news model %s researched a fresh brief"
              % (decision_model, news.model))
        # A command that actually works: `--brain kimi` without `--kimi-config` exits 78, and the
        # bitpin-bot wrapper's `run` branch only adds --state-dir.
        print("Recommended before live trading: one paper rehearsal (DEPLOY_FA step 6):\n"
              "  sudo systemctl start bitpin-bot-paper ; sudo journalctl -u bitpin-bot-paper -f\n"
              "or a single bar:\n"
              "  sudo bitpin-bot run paper --brain kimi --once \\\n"
              "      --config /etc/bitpin-bot/config.json --kimi-config /etc/bitpin-bot/kimi.json")
        return 0
    finally:
        shutil.rmtree(news_sd, ignore_errors=True)


# --------------------------------------------------------------------------- set-model

SET_MODEL_ETC = "/etc/bitpin-bot"
SET_MODEL_BACKUP_DIR = "/var/backups/bitpin-bot"
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,99}$")
# models known to reason before they answer (their reasoning tokens count in max_tokens, and they answer in
# JSON mode only without a temperature): measured on the server 2026-09-22 for kimi-k3 and kimi-k2.6
KNOWN_THINKING_PREFIXES = ("kimi-k3", "kimi-k2.6", "kimi-k2-thinking")
THINKING_DECISION_MAX_TOKENS = 32000
THINKING_NEWS_MAX_TOKENS = 8000
# a switch from a thinking model to one that is not: its 32000 max_tokens would not fit a smaller context
# (every call rejected) - the decision reply (one JSON object) needs far less. At most this is kept, and the
# llm default temperature replaces null (a thinking model's "not sent")
NON_THINKING_MAX_TOKENS = {"llm": 8000, "news": 8000}
NON_THINKING_TEMPERATURE = {"llm": 0.3}
SET_MODEL_TEST_MAX_TOKENS = {True: 4000, False: 300}     # thinking / not: enough for a one-word answer


def _euid():
    """The effective user id (None where there is none, e.g. Windows)."""
    return os.geteuid() if hasattr(os, "geteuid") else None


def known_thinking(model):
    from bitpin.llm import is_thinking_model
    m = str(model or "").strip().lower()
    return is_thinking_model(m) or m.startswith(KNOWN_THINKING_PREFIXES) or "thinking" in m


def _load_ordered(path):
    """kimi.json as an ordered document (comments and key order kept; duplicate keys refused)."""
    from collections import OrderedDict

    def pairs(items):
        d = OrderedDict()
        for k, v in items:
            if k in d:
                raise ValueError("key %r appears twice in the same object" % k)
            d[k] = v
        return d
    with open(path, "r", encoding="utf-8-sig") as f:
        doc = json.load(f, object_pairs_hook=pairs)
    if not isinstance(doc, dict):
        raise ValueError("%s must contain a JSON object {...}" % path)
    return doc


_LIST_ITEM = r'(?:"[^"\n]*"|null|true|false|-?[0-9.eE+-]+)'
_LIST_RE = re.compile(r'\[\n\s+(' + _LIST_ITEM + r'(?:,\n\s+' + _LIST_ITEM + r')*)\n\s+\]')


def _dump_ordered(doc):
    """The same layout deploy/apply_profile.py writes (2-space indent, short lists on one line)."""
    text = json.dumps(doc, indent=2, ensure_ascii=False)
    text = _LIST_RE.sub(lambda m: "[" + ", ".join(x.strip() for x in m.group(1).split(",\n")) + "]", text)
    return text + "\n"


def _atomic_replace_keep_mode(path, text):
    """Write text to path atomically, keeping the file's owner and mode (0640 root:bitpin on the server)."""
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(prefix=".set-model.", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if hasattr(os, "chown"):
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except (PermissionError, OSError):
                pass
        os.chmod(tmp, st.st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _set_key(section, key, value, where, changes):
    """section[key] = value when it differs (None counts as a value: null); records the change."""
    missing = key not in section
    old = section.get(key)
    same = (not missing) and (old == value and type(old) is type(value) or
                              (isinstance(old, (int, float)) and isinstance(value, (int, float))
                               and not isinstance(old, bool) and not isinstance(value, bool) and float(old) == float(value)))
    if not same:
        section[key] = value
        changes.append((where + "." + key, "(missing)" if missing else json.dumps(old), json.dumps(value)))


def set_model_changes(doc, decision_model=None, news_model=None, base_url=None, thinking=False, notes=None,
                      any_host=False):
    """Apply the set-model options to the kimi.json document `doc` IN PLACE. Returns [(key, old, new)].
    Only llm.model / news.model / llm.base_url (news.base_url when the news section has its own) change -
    plus, for a thinking model (known_thinking, or thinking=True), its temperature (null = not sent) and a
    large max_tokens (never lowered); and for a switch FROM a thinking model TO one that is not, max_tokens
    lowered to NON_THINKING_MAX_TOKENS and a null temperature set to the default (a thinking model's 32000
    tokens would be rejected by a smaller-context model: no valid decision at all) - explained in `notes`
    (a list, filled when given). A --base-url must be a plain https URL (no user:password@, ?query, #fragment)
    on a Moonshot host (MOONSHOT_HOSTS) unless any_host. Raises ValueError for a refused option (nothing is
    written then)."""
    import urllib.parse
    from bitpin.brain import NO_WEB_SEARCH_MODEL_PREFIXES
    from bitpin.llm import MOONSHOT_HOSTS, ConfigError, validate_llm_config
    changes = []
    for what, m in (("--decision-model", decision_model), ("--news-model", news_model)):
        if m is not None and not MODEL_ID_RE.match(m):
            raise ValueError("%s %r is not a model id (letters, digits, . _ - : / only; see --list)" % (what, m))
    if news_model is not None and news_model.lower().startswith(NO_WEB_SEARCH_MODEL_PREFIXES):
        raise ValueError("--news-model %s refused: the news researcher needs Moonshot's $web_search, which does not "
                         "work on %s (HTTP 400 'tokenization failed', measured 2026-09-22); kimi-k2.6 works"
                         % (news_model, news_model))
    if base_url is not None:
        try:
            validate_llm_config({"base_url": base_url})
        except ConfigError as e:
            raise ValueError("--base-url refused: %s" % e)
        base_url = base_url.strip().rstrip("/")
        host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
        if host not in MOONSHOT_HOSTS and not any_host:
            raise ValueError("--base-url refused: %s is not a Moonshot API host (%s). The bot sends the Kimi key to "
                             "this host (--list / --test at once): a typo would hand it to someone else. Add "
                             "--any-host only if this host is really meant to receive it"
                             % (host, " / ".join(MOONSHOT_HOSTS)))
    llm = doc.get("llm")
    if not isinstance(llm, dict):
        raise ValueError("the kimi config has no \"llm\" section {...}")
    news = doc.get("news") if isinstance(doc.get("news"), dict) else None
    if news_model is not None and news is None:
        raise ValueError("--news-model: the kimi config has no \"news\" section (copy it from kimi.example.json first)")
    old_models = {"llm": llm.get("model"), "news": news.get("model") if news is not None else None}
    if decision_model is not None:
        _set_key(llm, "model", decision_model, "llm", changes)
        if not (thinking or known_thinking(decision_model)):
            _non_thinking(llm, "llm", old_models["llm"], decision_model, changes, notes)
        if thinking or known_thinking(decision_model):
            _set_key(llm, "temperature", None, "llm", changes)
            try:
                cur = int(llm.get("max_tokens") or 0)
            except (TypeError, ValueError):
                cur = 0
            _set_key(llm, "max_tokens", max(cur, THINKING_DECISION_MAX_TOKENS), "llm", changes)
    if news_model is not None:
        _set_key(news, "model", news_model, "news", changes)
        if not (thinking or known_thinking(news_model)):
            _non_thinking(news, "news", old_models["news"], news_model, changes, notes)
        if thinking or known_thinking(news_model):
            _set_key(news, "temperature", None, "news", changes)
            try:
                cur = int(news.get("max_tokens") or 0)
            except (TypeError, ValueError):
                cur = 0
            _set_key(news, "max_tokens", max(cur, THINKING_NEWS_MAX_TOKENS), "news", changes)
    if base_url is not None:
        _set_key(llm, "base_url", base_url, "llm", changes)
        if news is not None and "base_url" in news:
            # the key is issued per platform: both stages must talk to the same one
            _set_key(news, "base_url", base_url, "news", changes)
    return changes


def _non_thinking(section, where, old_model, new_model, changes, notes):
    """A switch FROM a thinking model TO one that is not (set_model_changes): max_tokens lowered to
    NON_THINKING_MAX_TOKENS and a null temperature set to the default, with a note. Without a thinking
    model before, nothing changes (an explicit value of the owner stays) - only a note when max_tokens is
    above what a non-thinking model needs."""
    try:
        cur = int(section.get("max_tokens") or 0)
    except (TypeError, ValueError):
        cur = 0
    cap = NON_THINKING_MAX_TOKENS[where]
    was = known_thinking(old_model) and str(old_model) != str(new_model)
    if not was:
        if cur > cap and notes is not None:
            notes.append("%s.max_tokens is %d although %s is not a known thinking model: a model with a smaller "
                         "context rejects such calls (no valid decision). Lower it in kimi.json unless the model "
                         "needs it, or run again with --thinking if it reasons before it answers."
                         % (where, cur, new_model))
        return
    n0 = len(changes)
    if cur > cap:
        _set_key(section, "max_tokens", cap, where, changes)
    if where in NON_THINKING_TEMPERATURE and section.get("temperature") is None:
        _set_key(section, "temperature", NON_THINKING_TEMPERATURE[where], where, changes)
    if notes is not None and len(changes) > n0:
        notes.append("%s is not a known thinking model (the previous %s is): %s - a thinking model's large "
                     "max_tokens would be rejected by a smaller-context model and no decision would be valid. If "
                     "%s DOES reason before it answers, run again with --thinking."
                     % (new_model, old_model, ", ".join("%s %s -> %s" % c for c in changes[n0:]), new_model))


def _set_model_validate(text, config_path):
    """The bot's own offline checks (brain.check_kimi_config, with config.json when it can be read) on the
    NEW kimi.json text, in a private temporary directory. Returns (problems, warnings)."""
    from bitpin.brain import check_kimi_config
    runner_cfg, warnings = None, []
    if config_path and os.path.isfile(config_path):
        try:
            runner_cfg = load_config(config_path)
        except Exception as e:  # noqa: BLE001 - the kimi check still runs (without the runner's threshold)
            warnings.append("config.json not readable here (%s): checked without it" % e)
    d = tempfile.mkdtemp(prefix="bitpin_set_model_")
    try:
        os.chmod(d, 0o700)
        p = os.path.join(d, "kimi.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        problems = check_kimi_config(p, require_model=True, runner_cfg=runner_cfg, warnings=warnings)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return problems, warnings


def _set_model_client(llm_cfg, sd, **over):
    """An LLMClient on the bot's route (KIMI_HTTPS_PROXY / llm.proxy, the key from its env variable) with
    a short retry / deadline budget, for --list / --test."""
    from bitpin.llm import LLMClient
    cfg = dict(llm_cfg)
    cfg.update(max_retries=1, timeout=120, deadline_seconds=180)
    cfg.update(over)
    return LLMClient(cfg, state_dir=sd)


def _set_model_test(label, llm_cfg, model, ids, sd, json_mode):
    """ONE real, tiny chat call to `model` with the configured key / route (a few tokens; the key is never
    shown). Returns True when it answered."""
    from bitpin.llm import LLMError
    thinking = known_thinking(model)
    try:
        llm = _set_model_client(llm_cfg, sd, model=model, max_tokens=SET_MODEL_TEST_MAX_TOKENS[thinking],
                                temperature=None)
    except (ValueError, TypeError) as e:
        print("  TEST %s: config error - %s" % (label, e))
        return False
    if ids is not None and model not in ids:
        print("  TEST %s: FAILED - %s is not offered to this key (see --list)" % (label, model))
        return False
    msgs = [{"role": "system", "content": "This is a connectivity test. Reply with only the JSON object "
                                          "{\"ok\": true}." if json_mode else
             "This is a connectivity test. Reply with the single word OK."},
            {"role": "user", "content": "test"}]
    t0 = time.time()
    try:
        res = llm.chat(msgs, json_mode=json_mode)
    except LLMError as e:
        print("  TEST %s: FAILED - %s" % (label, llm.redact(str(e))))
        return False
    u = res.get("usage") or {}
    content = (res.get("content") or "").strip()
    print("  TEST %s: %s answered in %.0f s (%s tokens, finish_reason %s): %s" % (
        label, model, time.time() - t0, u.get("total_tokens", "?"), res.get("finish_reason"),
        llm.redact(content[:80].replace("\n", " ")) or "(empty)"))
    print("  TEST %s: the test asked for max_tokens %s; the bot will ask for max_tokens %s per call (this test "
          "cannot tell whether the model's context takes that)" % (label, SET_MODEL_TEST_MAX_TOKENS[thinking],
                                                                   llm_cfg.get("max_tokens")))
    if not content:
        print("  TEST %s: FAILED - the reply is EMPTY (a thinking model that spent max_tokens on reasoning?)" % label)
        return False
    return True


def cmd_set_model(args):
    """Switch the Kimi model(s) in kimi.json (run as root on the server: sudo bitpin-bot set-model ...).
    Changes ONLY llm.model / news.model / llm.base_url (news.base_url when the news section has one) - and
    for a thinking model its temperature (null) and a large max_tokens, for a switch from a thinking model
    to one that is not a smaller max_tokens and the default temperature - keeps every other key, value and
    comment, validates the result with the bot's own checks, optionally tests the new model(s) with one
    tiny real call each (--test), backs the old file up with a timestamp and replaces it atomically
    (owner and mode kept). Idempotent: nothing to change -> nothing is written. Exit 2: not root for
    /etc/bitpin-bot; exit 1: refused, the model list could not be read (--list / --test) or a test failed.
    --live-running (the wrapper sets it while the live service is active): prominent warnings that the
    bot must be confirmed and restarted before any automatic restart."""
    path = args.kimi_config
    want = [x for x in (args.decision_model, args.news_model, args.base_url) if x is not None]
    if not want and not args.list:
        print("nothing to do: give --decision-model ID, --news-model ID and/or --base-url URL, or --list",
              file=sys.stderr)
        return 2
    writes = bool(want) and not args.dry_run
    euid = _euid()
    if writes and euid is not None and euid != 0 \
            and os.path.abspath(os.path.dirname(os.path.abspath(path))) == os.path.abspath(SET_MODEL_ETC):
        print("run it as root: sudo bitpin-bot set-model ... (%s is root:bitpin 0640)" % path, file=sys.stderr)
        return 2
    try:
        doc = _load_ordered(path)
        with open(path, "r", encoding="utf-8-sig") as f:
            raw_text = f.read()
    except (OSError, ValueError, UnicodeDecodeError) as e:
        print("REFUSED: cannot read %s: %s\nNothing was changed." % (path, e), file=sys.stderr)
        return 1
    new = copy.deepcopy(doc)
    notes = []
    try:
        changes = set_model_changes(new, args.decision_model, args.news_model, args.base_url, args.thinking,
                                    notes=notes, any_host=bool(getattr(args, "any_host", False)))
    except ValueError as e:
        print("REFUSED: %s\nNothing was changed." % e, file=sys.stderr)
        return 1
    new_text = _dump_ordered(new)
    llm_new = dict(new.get("llm") or {})
    news_new = new.get("news") if isinstance(new.get("news"), dict) else None
    print("Kimi model switch - %s" % os.path.abspath(path))
    print("  decision model: %s%s" % (llm_new.get("model"), "  (thinking: temperature null, max_tokens %s)"
                                      % llm_new.get("max_tokens") if known_thinking(llm_new.get("model")) or
                                      args.thinking else ""))
    if news_new is not None:
        print("  news model    : %s" % news_new.get("model"))
    print("  base_url      : %s%s" % (llm_new.get("base_url"), ("  (news: %s)" % news_new["base_url"])
                                      if news_new is not None and "base_url" in news_new else ""))
    for n in notes:
        print("  NOTE: %s" % n)
    live = bool(getattr(args, "live_running", False))
    if live and writes:
        print("  WARNING: the LIVE bot (bitpin-bot.service) is RUNNING. It keeps the OLD model until it restarts, "
              "and after this change ANY restart before 'sudo bitpin-bot confirm-live' - a crash, systemd's "
              "automatic restart, a reboot - leaves it STOPPED (exit 78): no stops, targets or max holds are "
              "enforced then. Do the whole sequence below NOW (safest: stop the bot first).")
    problems, warnings = _set_model_validate(new_text, args.config)
    for w in warnings:
        print("  WARNING: %s" % w)
    if problems:
        print("REFUSED: the bot's own checks reject the result - nothing was written:")
        for p in problems:
            print("  - %s" % p)
        return 1
    print("  check         : the bot's own checks accept the %s kimi.json" % ("new" if changes else "current"))
    ids = None
    sd = tempfile.mkdtemp(prefix="bitpin_set_model_llm_")
    try:
        if args.list or args.test:
            from bitpin.llm import LLMError
            llm = None
            try:
                llm = _set_model_client(llm_new, sd)
                print("  route         : %s%s" % (llm.proxy_display, (" (from %s)" % llm.proxy_source)
                                                  if llm.proxy else ""))
                if not llm.has_key:
                    raise LLMError("no API key: %s is not set (the server's bitpin-bot.env; sudo bitpin-bot set-model "
                                   "loads it)" % llm.cfg["api_key_env"])
                ids = sorted(set(llm.list_models()))
            except (LLMError, ValueError, TypeError) as e:
                print("  models        : FAILED - %s" % (llm.redact(str(e)) if llm is not None else str(e)))
                if args.test:
                    print("RESULT: FAILED - the model list could not be read, so the new model(s) were not tested; "
                          "nothing was written (run again, or without --test)")
                    return 1
                print("RESULT: FAILED - the model list could not be read (--list); nothing was written (check the "
                      "key and the Kimi route, then run again)")
                return 1
            if ids is not None:
                from bitpin.brain import NO_WEB_SEARCH_MODEL_PREFIXES
                print("  models        : %d ids offered to this key (GET %s/models):" % (len(ids), llm.base_url))
                for i in ids:
                    tags = []
                    if i == llm_new.get("model"):
                        tags.append("decision")
                    if news_new is not None and i == news_new.get("model"):
                        tags.append("news")
                    if known_thinking(i):
                        tags.append("thinking")
                    if i.lower().startswith(NO_WEB_SEARCH_MODEL_PREFIXES):
                        tags.append("no web search")
                    print("    %s %s%s" % ("*" if {"decision", "news"} & set(tags) else "-", i,
                                         ("  [%s]" % ", ".join(tags)) if tags else ""))
                for label, m in (("decision", llm_new.get("model")),
                                 ("news", news_new.get("model") if news_new is not None else None)):
                    if m and m not in ids:
                        print("  WARNING: the %s model %s is NOT offered to this key" % (label, m))
        if args.test:
            ok = True
            if args.decision_model is not None or not args.news_model:
                ok = _set_model_test("decision", llm_new, llm_new.get("model"), ids, sd, json_mode=True) and ok
            if args.news_model is not None and news_new is not None:
                ncfg = dict(llm_new, base_url=news_new.get("base_url") or llm_new.get("base_url"))
                ok = _set_model_test("news", ncfg, news_new.get("model"), ids, sd, json_mode=False) and ok
            if not ok:
                print("RESULT: FAILED - the test call did not succeed; nothing was written (fix the model id / the "
                      "route and run again, or switch without --test)")
                return 1
    finally:
        shutil.rmtree(sd, ignore_errors=True)
    if not changes:
        print("Nothing to change: kimi.json already has these settings.")
        return 0
    print("Changes (%d):" % len(changes))
    for key, old, new_v in changes:
        print("  %-22s %s -> %s" % (key, old, new_v))
    if args.dry_run:
        print("DRY RUN: nothing was written.")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    bdir = args.backup_dir or (SET_MODEL_BACKUP_DIR if os.path.abspath(os.path.dirname(os.path.abspath(path)))
                               == SET_MODEL_ETC else os.path.dirname(os.path.abspath(path)))
    try:
        os.makedirs(bdir, exist_ok=True)
        try:
            os.chmod(bdir, 0o700)
        except OSError:
            pass
        backup = os.path.join(bdir, "%s.%s.before-set-model" % (os.path.basename(path), stamp))
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        _atomic_replace_keep_mode(path, new_text)
    except OSError as e:
        print("FAILED to write: %s - %s is unchanged" % (e, path), file=sys.stderr)
        return 1
    print("  written       : %s (old copy: %s)" % (path, backup))
    print("  undo          : sudo cp %s %s" % (backup, path))
    if raw_text != _dump_ordered(doc):
        print("  note          : the file was re-formatted like apply_profile.py writes it (every value and comment "
              "is kept)")
    print()
    print("NEXT (required): the live confirmation covers every value of kimi.json, so the live bot refuses to start "
          "(exit 78, no trading) until you confirm again, and a running bot keeps the OLD model until it restarts:")
    print("    sudo bitpin-bot check")
    print("    sudo systemctl stop bitpin-bot")
    print("    sudo bitpin-bot confirm-live")
    print("    sudo systemctl start bitpin-bot")
    if live:
        print("WARNING: the live bot is RUNNING right now: until the four commands above are done, any automatic "
              "restart leaves it stopped (exit 78) and no code exit (stop / target / max hold) runs.")
    return 0


def _num(x, fmt="%.8g"):
    try:
        return "-" if x is None else fmt % float(x)
    except (TypeError, ValueError):
        return str(x)


def resting_state_lines(sd, mode, broker=None, views=None):
    """Read-only lines about the bot's own resting orders (from its order journal / paper state:
    no exchange request), the crash-ladder levels and the positions guarded by code exits (from
    runner_state_<mode>.json)."""
    out = []
    if views is None:
        try:
            views = list(broker.limit_orders(active_only=True)) if broker is not None else []
        except Exception as e:  # noqa: BLE001 - informational
            out.append("  bot resting orders: could not be read (%s)" % e)
            views = []
    live = [v for v in views if not v.get("cancel_requested")]
    out.append("  bot resting orders (its own journal): %s" % (len(live) if live else "none"))
    for v in sorted(live, key=lambda v: (str(v.get("tag")), str(v.get("symbol")), -float(v.get("price") or 0))):
        meta = v.get("meta") if isinstance(v.get("meta"), dict) else {}
        what = ("level %s%%" % lvl_key(meta.get("level_pct"))) if v.get("tag") == LADDER_TAG else \
            ("target %s USDT" % _num(meta.get("target_usdt"))) if v.get("tag") == TARGET_TAG else ""
        out.append("    %-6s %-4s %-9s %s @ %s (%s left, %s)%s%s" % (
            v.get("tag") or "?", v.get("side"), v.get("symbol"), _num(v.get("base_amount")), _num(v.get("price")),
            _num(v.get("remaining_base")), v.get("state"), (" " + what) if what else "",
            (" filled %s" % _num(v.get("filled_base"))) if v.get("filled_base") else ""))
    st = read_json(os.path.join(sd, "runner_state_%s.json" % mode)) or {}
    lad = st.get("ladder") if isinstance(st.get("ladder"), dict) else {}
    coins = lad.get("coins") if isinstance(lad.get("coins"), dict) else {}
    if coins or isinstance(lad.get("scales"), dict):
        sc = lad.get("scales") if isinstance(lad.get("scales"), dict) else {}
        parts = []
        for c in sorted(set(coins) | set(sc)):
            lv = (coins.get(c) or {}).get("levels") if isinstance(coins.get(c), dict) else {}
            desc = []
            for k, s in sorted((lv or {}).items(), key=lambda kv: -float(kv[0]) if _isnum(kv[0]) else 0):
                if not isinstance(s, dict):
                    continue
                if s.get("armed", True) is False:
                    desc.append("%s%% FILLED %s at %s USDT" % (k, _teh(s.get("filled_at")), _num(s.get("fill_px_usdt"))))
                else:
                    desc.append("%s%% armed" % k)
            parts.append("%s scale %s%s" % (c, _num(sc.get(c, 1.0), "%g"), (": " + ", ".join(desc)) if desc else ""))
        out.append("  crash ladder levels: %s%s" % ("; ".join(parts) or "-",
                                                    " (OFF since %s: endgame)" % _teh(lad["endgame_off_at"])
                                                    if lad.get("endgame_off_at") else ""))
    pos = st.get("positions") if isinstance(st.get("positions"), dict) else {}
    lpos = st.get("ladder_positions") if isinstance(st.get("ladder_positions"), dict) else {}
    if pos or lpos:
        out.append("  positions guarded by code exits (runner state; a crash-ladder fill is its own position):")
        rows = [(s, "", p) for s, p in pos.items()] + [(s, " (crash ladder)", p) for s, p in lpos.items()]
        for s, what, p in sorted(rows, key=lambda r: (r[0], r[1])):
            if not isinstance(p, dict):
                continue
            out.append("    %-9s %s units%s, entry %s USDT (%s, since %s): stop %s, target %s USDT, max hold until %s "
                       "Tehran" % (s, _num(p.get("amount")), what, _num(p.get("entry_px_usdt")), p.get("source"),
                                   _teh(p.get("entry_ts")), _num(p.get("stop_px_usdt")),
                                   _num(p.get("target_px_usdt")), _teh(p.get("max_hold_until"))))
    elif "positions" in st:
        out.append("  positions guarded by code exits: none (the bot holds no coin)")
    return out


def _isnum(x):
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def cmd_status(args):
    cfg = build_config(args)
    sd = cfg["state_dir"]
    key, secret, swappable = load_credentials(args.keys_file)
    check_account_binding(sd, account_fingerprint(key, secret), write=False)
    setup_logging(sd, args.cmd, args.log_level, secrets=(key, secret))
    client, markets, broker = make_live(cfg, sd, key, secret, swappable)
    del key, secret
    print("Bitpin account status (read-only)")
    try:
        rows = client.wallets()
    except AuthError as e:
        die("authentication failed: %s" % e)
    except AuthBudgetExceeded as e:
        sys.exit(str(e))
    budget = client.auth_budget
    print("  authenticated OK; auth budget used %d/%d in the last 24h" % (budget.used(), budget.limit))
    main = [r for r in rows if isinstance(r, dict) and r.get("service") in (None, "", "main")]
    print("  wallets (service=main, non-zero):")
    for r in sorted(main, key=lambda r: str(r.get("asset"))):
        try:
            b, f = D(r.get("balance") or 0), D(r.get("frozen") or 0)
        except ValueError:
            continue
        if b or f:
            print("    %-8s balance %-22s frozen %s" % (r.get("asset"), r.get("balance"), r.get("frozen")))
    code = str(cfg["irt_asset_code"]).upper()
    div = D(cfg["irt_unit_divisor"])
    found = [str(r.get("asset")).upper() for r in main if str(r.get("asset")).upper() in IRT_CANDIDATES]
    print("  toman asset detection: candidate codes present: %s" % (", ".join(found) or "NONE"))
    print("  configured: irt_asset_code=%s irt_unit_divisor=%s" % (code, cfg["irt_unit_divisor"]))
    irt_rows = [r for r in main if str(r.get("asset")).upper() == code]
    if not irt_rows:
        print("  !! no wallet with asset code %s. Set irt_asset_code in the config to one of: %s"
              % (code, ", ".join(found) or "(none found)"))
    try:
        broker.refresh()
    except BrokerError as e:
        print("  !! %s" % e)
        return 1
    bal, avail = broker.balances(), broker.available()
    irt = bal.get("IRT", ZERO)
    print("  => the bot sees %s IRT (toman) total, %s available" % (fmt_amount(irt), fmt_amount(avail.get("IRT", ZERO))))
    if div != 1:
        print("     (raw %s value divided by %s)" % (code, cfg["irt_unit_divisor"]))

    # portfolio weights for the universe the bot will manage: the Kimi brain's allowed symbols with
    # --kimi-config (what 'live --brain kimi' trades), else the configured strategy's
    try:
        syms_kimi = kimi_symbols(args.kimi_config) if getattr(args, "kimi_config", None) else None
        strategy = BrainStrategy(syms_kimi) if syms_kimi else make_strategy(cfg)
        prices = {}
        for t in client.tickers():
            try:
                prices[str(t["symbol"]).upper()] = D(t["price"])
            except (KeyError, ValueError):
                continue
        syms = list(dict.fromkeys(list(strategy.symbols)))
        eq = irt + sum((bal.get(parse_symbol(s)[0], ZERO) * prices.get(s, ZERO) for s in syms), ZERO)
        print("  portfolio managed by %s (valued at ticker prices): %s IRT" % (
            "the Kimi brain (%s)" % args.kimi_config if syms_kimi else strategy.name, fmt_amount(eq)))
        if eq > 0:
            print("    %-10s weight %.4f" % ("IRT cash", irt / eq))
            for s in syms:
                v = bal.get(parse_symbol(s)[0], ZERO) * prices.get(s, ZERO)
                print("    %-10s weight %.4f  (%s IRT)" % (s, v / eq, fmt_amount(v)))
        others = [a for a in bal if a not in ("IRT",) and bal[a] and a not in {parse_symbol(s)[0] for s in syms}]
        if others:
            print("    not managed by the bot (never traded): %s" % ", ".join(sorted(others)))
        preview = Runner(strategy, broker, RiskManager(cfg.get("risk"), sd, "live", persist=False), cfg, sd, "live",
                         dry_run=True)
        for line in preview.management_summary(bal):
            print("  %s" % line)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print("  (could not compute weights: %s)" % e)
    try:
        oo = client.open_orders()
        print("  open orders: %s" % (len(oo) if oo else "none"))
        for o in oo:
            print("    id=%s %s %s %s base=%s quote=%s price=%s state=%s" % (
                o.get("id"), o.get("symbol"), o.get("side"), o.get("type"), o.get("base_amount"),
                o.get("quote_amount"), o.get("price"), o.get("state")))
    except BitpinError as e:
        print("  open orders: could not list (%s)" % e)
    for line in resting_state_lines(sd, "live", broker):
        print(line)
    pend = broker.journal.unresolved()
    if pend:
        print("  !! %d journaled bot order(s) with unresolved outcome: %s" % (len(pend), ", ".join(k for k, _ in pend)))
        print("     details / record the outcome: run_bot.py resolve-order --state-dir %s" % sd)

    ok, conf = confirmation_ok(sd, cfg)
    if ok:
        print("  IRT balance already confirmed on %s (%s IRT seen then)." % (conf.get("confirmed_utc"),
                                                                           conf.get("irt_balance_seen")))
    if not irt_rows:
        print("  Not asking for confirmation until irt_asset_code matches a wallet.")
        return 1
    if irt <= 0:
        print("  IRT balance is 0: the unit (toman vs rial) cannot be verified. Deposit some toman and run "
              "status again to confirm.")
        return 1
    if args.no_confirm or not sys.stdin.isatty():
        if not ok:
            print("  IRT balance NOT confirmed yet (run status in an interactive terminal).")
        return 0
    print()
    print("Open the Bitpin app/site and look at your toman balance (total incl. amounts in open orders).")
    try:
        a = input("Does it match %s toman? type yes / no: " % fmt_amount(irt))
    except EOFError:
        a = ""
    if a.strip().lower() in ("yes", "y"):
        atomic_write_json(os.path.join(sd, CONFIRM_FILE), {
            "confirmed": True, "irt_asset_code": code, "irt_unit_divisor": str(cfg["irt_unit_divisor"]),
            "irt_balance_seen": irt, "confirmed_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())})
        print("Confirmed and saved to %s. Live trading is now allowed for this state dir." % os.path.join(sd, CONFIRM_FILE))
    else:
        atomic_write_json(os.path.join(sd, CONFIRM_FILE), {"confirmed": False, "irt_asset_code": code,
                                                            "irt_unit_divisor": str(cfg["irt_unit_divisor"])})
        print("Not confirmed. If the bot shows 10x your toman balance the wallet is in rial: set "
              "irt_unit_divisor to 10. If the asset code is different, set irt_asset_code. Then run status again.")
    return 0


def cmd_stop(args):
    sd = build_config(args)["state_dir"]
    os.makedirs(sd, exist_ok=True)
    p = os.path.join(sd, KILL_SWITCH_FILE)
    with open(p, "w") as f:
        f.write("created %s UTC by run_bot.py stop\n" % time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()))
    print("kill switch created: %s" % os.path.abspath(p))
    try:
        from bitpin.broker import OrderJournal
        from bitpin.broker import limit_view as _lv
        resting = [(k, e) for k, e in OrderJournal(os.path.join(sd, LiveBroker.JOURNAL_FILE)).limits()
                   if e.get("status") in ("resting", "submitting", "unknown")] \
            if os.path.exists(os.path.join(sd, LiveBroker.JOURNAL_FILE)) else []
    except Exception:  # noqa: BLE001 - informational
        resting = []
    print("NOTE: the running bot CANCELS its crash-ladder bids when it sees STOP (within 30 s when idle), so a "
          "stopped bot does not keep buying without its code exits. Its target sells stay on Bitpin (they only sell "
          "a position at its target)%s. To remove them too, after the bot has exited: run_bot.py cancel-resting "
          "--mode live --state-dir %s (on the server: sudo bitpin-bot cancel-resting). If NO bot is running, nothing "
          "cancels the bids: run cancel-resting yourself."
          % ((" (%d resting in the journal now: %s)" % (len(resting), ", ".join(
              "%s %s %s @ %s" % (e.get("tag") or "-", _lv(k, e)["side"], e.get("symbol"), e.get("price"))
              for k, e in resting[:8]))) if resting else "", sd))
    lock = StateLock(sd)
    if lock.is_free():
        print("WARNING: no running bot holds %s. If your bot was started with another --state-dir or --config, "
              "run 'stop' again with the SAME options - the kill switch only works for its own state dir."
              % os.path.abspath(lock.path))
        return 1
    print("a running bot uses this state dir: it stops before its next order / within 30 s when idle")
    return 0


def cmd_resume(args):
    p = os.path.join(build_config(args)["state_dir"], KILL_SWITCH_FILE)
    if os.path.exists(p):
        os.remove(p)
        print("kill switch removed: %s" % p)
    else:
        print("no kill switch at %s" % p)
    return 0


def cmd_risk_reset(args):
    cfg = build_config(args)
    sd = cfg["state_dir"]
    lock = StateLock(sd)
    try:
        lock.acquire()
    except RunnerError:
        sys.exit("a bot is running with state dir %s: it would overwrite the reset with its own high-water mark. "
                 "Stop it first (run_bot.py stop, wait until it exits), then risk-reset, then start it again."
                 % os.path.abspath(sd))
    try:
        r = make_risk(cfg, sd, args.mode)
        print("before: halted=%s reason=%s hwm=%s flatten_pending=%s" % (r.is_halted(), r.halt_reason(),
                                                                        r._st.get("hwm"), r.flatten_pending))
        r.reset()
        print("risk state for mode %s reset (halt cleared, high-water mark will restart from current equity)"
              % args.mode)
        return 0
    finally:
        lock.release()


def cmd_resolve_order(args):
    """List the bot's orders of unknown outcome, or record the outcome of one of them."""
    cfg = build_config(args)
    sd = cfg["state_dir"]
    lock = StateLock(sd)
    try:
        lock.acquire()
    except RunnerError:
        sys.exit("a bot is running with state dir %s: stop it first (run_bot.py stop, wait until it exits), then "
                 "resolve the order, then start it again." % os.path.abspath(sd))
    try:
        journal_only = LiveBroker(None, None, sd)
        pend = journal_only.journal.unresolved()
        if not args.identifier:
            if not pend:
                print("no bot orders with unresolved outcome in %s" % os.path.abspath(sd))
                return 0
            print("bot orders with unresolved outcome in %s:" % os.path.abspath(sd))
            for ident, e in pend:
                print("  identifier %s: %s %s base_amount=%s quote_amount=%s status=%s order_id=%s created %s UTC%s" % (
                    ident, e.get("side"), e.get("symbol"), e.get("base_amount"), e.get("quote_amount"),
                    e.get("status"), e.get("order_id") or "-",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(e.get("created_at") or 0))),
                    ("; already recorded fill base %s quote %s" % (e.get("reported_base"), e.get("reported_quote"))
                     if e.get("reported_base") is not None else "")))
            print("Find each order in the Bitpin app (same market, side and time), then run:\n"
                  "  run_bot.py resolve-order --state-dir %s --identifier ID --order-id <order id from the app>\n"
                  "or, if no such order exists / nothing was traded:\n"
                  "  run_bot.py resolve-order --state-dir %s --identifier ID --not-executed" % (sd, sd))
            return 0
        if bool(args.order_id) == bool(args.not_executed):
            die("give exactly one of --order-id or --not-executed")
        e = journal_only.journal.get(args.identifier)
        if e is None:
            die("no journaled bot order with identifier %s in %s" % (args.identifier, os.path.abspath(sd)))
        if args.not_executed:
            print("Order %s: %s %s base_amount=%s quote_amount=%s, status %s." % (
                args.identifier, e.get("side"), e.get("symbol"), e.get("base_amount"), e.get("quote_amount"),
                e.get("status")))
            print("Recording it as NOT executed. If it did execute, the bot's books will be wrong (with max_equity_irt "
                  "the bot may buy again, or leave coins it bought unmanaged).")
            if not args.yes:
                try:
                    a = input("Type yes to record it as not executed: ")
                except EOFError:
                    a = ""
                if a.strip().lower() != "yes":
                    print("nothing recorded")
                    return 1
            journal_only.resolve_manually(args.identifier, not_executed=True)
            print("recorded: order %s did not execute" % args.identifier)
            return 0
        key, secret, swappable = load_credentials(args.keys_file)
        check_account_binding(sd, account_fingerprint(key, secret), write=False)
        setup_logging(sd, "live", args.log_level, secrets=(key, secret))
        client, markets, broker = make_live(cfg, sd, key, secret, swappable)
        del key, secret
        try:
            fill = broker.resolve_manually(args.identifier, order_id=args.order_id)
        except AuthError as ex:
            die("authentication problem: %s" % ex)
        except BitpinError as ex:
            sys.exit("could not read order %s from Bitpin (%s); nothing recorded" % (args.order_id, ex))
        print("recorded: order %s (id %s) %s %s base %s quote %s fee %s %s. The bot books it into its sleeve at the "
              "next start." % (args.identifier, args.order_id, fill["side"], fill["symbol"], fill["base"],
                               fill["quote"], fill["fee"], fill["fee_asset"]))
        return 0
    finally:
        lock.release()


LADDER_BARS_BACK = 60        # hourly candles fetched for the ladder's 48 h reference (a few spare)


def _ladder_equity(args, sd):
    """(equity_irt, source) for the ladder command: --equity-irt, else the bot's last processed cycle."""
    if args.equity_irt is not None:
        return float(args.equity_irt), "--equity-irt"
    st = read_json(os.path.join(sd, "runner_state_%s.json" % args.mode)) or {}
    lc = st.get("last_cycle") if isinstance(st.get("last_cycle"), dict) else {}
    try:
        eq = float(lc.get("equity"))
    except (TypeError, ValueError):
        eq = None
    if eq and eq > 0:
        return eq, "the bot's last cycle (%s UTC)" % time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(lc.get("time")
                                                                                                    or 0)))
    return None, None


def _last_ladder_budget(sd, max_bytes=512 * 1024):
    """(USDT budget, source) of the bot's last ladder maintenance (kimi_runner.jsonl "ladder_state"),
    or None. The budget is the free USDT plus what its own bids lock, minus the cash buffer."""
    p = os.path.join(sd, "kimi_runner.jsonl")
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        ls = rec.get("ladder_state") if isinstance(rec, dict) else None
        if isinstance(ls, dict) and isinstance(ls.get("budget_usdt"), (int, float)) and ls.get("on"):
            return float(ls["budget_usdt"]), "the bot's last hourly check (%s UTC)" % time.strftime(
                "%Y-%m-%d %H:%M", time.gmtime(float(rec.get("time") or 0)))
    return None


def _ladder_scales(sd, mode, coins, bcfg, now):
    """{coin: scale} in force: the runner's applied Kimi scales, else the brain's (after a restart),
    else 1.0; every coin 0 from the endgame cut-off."""
    eg = (bcfg or {}).get("endgame")
    if eg and eg.get("no_new_entries_at") and now >= float(eg["no_new_entries_at"]) and \
            now < float(eg.get("end_at") or float(eg["final_at"]) + 36 * 3600):
        return {c: 0.0 for c in coins}, "endgame: no new coin entries since %s Tehran" % _teh(eg["no_new_entries_at"])
    st = read_json(os.path.join(sd, "runner_state_%s.json" % mode)) or {}
    lad = st.get("ladder") if isinstance(st.get("ladder"), dict) else {}
    src = lad.get("scales") if isinstance(lad.get("scales"), dict) else None
    how = "the last applied Kimi decision"
    if src is None:
        bs = read_json(os.path.join(sd, "kimi_brain_state.json")) or {}
        src = bs.get("ladder_in_force") if isinstance(bs.get("ladder_in_force"), dict) else {}
        how = "Kimi's last valid decision" if src else "default (no Kimi decision has set a scale yet)"
    out = {}
    for c in coins:
        try:
            out[c] = min(1.0, max(0.0, float(src.get(c, 1.0))))
        except (TypeError, ValueError):
            out[c] = 0.0
    return out, how


def cmd_ladder(args):
    """Read-only: the crash-ladder bids the bot would keep NOW (public candles, no keys, nothing
    placed or cancelled), next to the bids it really has resting (its own journal) and the state of
    each level (armed / filled)."""
    from bitpin.analysis import dd48_usdt
    from bitpin import data as data_mod
    cfg = build_config(args)
    sd = cfg["state_dir"]
    lc = cfg["ladder"]
    bcfg = {}
    if args.kimi_config:
        from bitpin.brain import validate_brain_config
        from bitpin.llm import ConfigError
        try:
            bcfg = validate_brain_config(load_kimi(args.kimi_config).get("brain") or {})
        except ConfigError as e:
            die("kimi config error: %s" % e)
    coins = list(lc["coins"] or bcfg.get("ladder_coins") or ["BTC", "ETH", "XRP", "SOL"])
    now = time.time()
    print("Crash ladder plan - read-only: nothing is placed or cancelled; public Bitpin data only, no keys")
    print("  settings : %s" % ladder_text(cfg, coins=coins))
    if not lc["enabled"]:
        print("  ladder.enabled is false in the config: the bot keeps NO ladder bids (the plan below is what it "
              "would do if it were on)")
    if not args.kimi_config:
        print("  (without --kimi-config the brain's ladder coins, endgame and Kimi's scales from its state are "
              "partly unknown)")
    scales, how = _ladder_scales(sd, args.mode, coins, bcfg, now)
    print("  scales   : %s (%s)" % (", ".join("%s %g" % (c, scales[c]) for c in coins), how))
    lookback = int(lc["lookback_hours"])
    start = int(now) - LADDER_BARS_BACK * 3600 - 7200
    try:
        ub = data_mod.closed_only(data_mod.fetch_bars("USDT_IRT", "60", start, int(now) + 3600), "60", now)
    except Exception as e:  # noqa: BLE001
        print("RESULT: FAILED - USDT_IRT candles unavailable (%s)" % e)
        return 1
    if not ub:
        print("RESULT: FAILED - no USDT_IRT candles")
        return 1
    rate = float(ub[-1].close)
    print("  USDT_IRT : %s toman (hourly close of %s Tehran)" % (fmt_amount(D(repr(rate))), _teh(ub[-1].ts + 3600)))
    equity, src = _ladder_equity(args, sd)
    if equity is None:
        print("RESULT: FAILED - no equity known: pass --equity-irt N (the account value in toman), or run it on the "
              "bot's state dir after its first cycle")
        return 1
    # the runner keeps cash_buffer_frac of the USDT free (rounding / fee headroom)
    last_budget = _last_ladder_budget(sd) if args.usdt is None else None
    if args.usdt is not None:
        usdt_budget, bsrc = float(args.usdt) * (1 - float(cfg["cash_buffer_frac"])), "--usdt"
    elif last_budget is not None:
        usdt_budget, bsrc = last_budget
    else:
        usdt_budget = equity / rate * (1 - float(cfg["cash_buffer_frac"]))
        bsrc = "assumed: the whole account is in USDT (no ladder cycle of the bot yet; --usdt N to set it)"
    print("  equity   : %s toman = %.2f USDT (from %s); USDT the ladder may use: %.2f (%s)" % (
        fmt_amount(D(repr(round(equity)))), equity / rate, src, usdt_budget, bsrc))
    refs = {}
    closes = {}
    uts, uc = [b.ts for b in ub], [b.close for b in ub]
    for c in coins:
        try:
            bars = data_mod.closed_only(data_mod.fetch_bars("%s_IRT" % c, "60", start, int(now) + 3600), "60", now)
            r = dd48_usdt(bars, uts, uc, "%s_IRT" % c, hours=lookback)
        except Exception as e:  # noqa: BLE001
            print("  %s: candles unavailable (%s): no bid would be (re)placed for it this hour" % (c, e))
            continue
        if r:
            refs[c] = {"dd48": float(r[0]), "hi48": float(r[1]), "close": float(r[2])}
            closes[c] = float(r[2])
    st = read_json(os.path.join(sd, "runner_state_%s.json" % args.mode)) or {}
    lad = st.get("ladder") if isinstance(st.get("ladder"), dict) else {}
    lcoins = lad.get("coins") if isinstance(lad.get("coins"), dict) else {}
    armed = {}
    for c in coins:
        lvs = (lcoins.get(c) or {}).get("levels") if isinstance(lcoins.get(c), dict) else {}
        for lv in lc["levels_pct"]:
            s = (lvs or {}).get(lvl_key(lv))
            armed[(c, lvl_key(lv))] = not (isinstance(s, dict) and s.get("armed", True) is False)
    risk_cfg = cfg.get("risk") or {}
    min_usdt = float(risk_cfg.get("min_order_usdt", DEFAULT_RISK["min_order_usdt"]))
    want, factor = ladder_plan(refs, scales, armed, lc["levels_pct"], lc["size_frac"], equity, rate, usdt_budget,
                               min_usdt)
    views = []
    jp = os.path.join(sd, LiveBroker.JOURNAL_FILE if args.mode == "live" else PaperBroker.STATE_FILE)
    if os.path.exists(jp):
        try:
            if args.mode == "live":
                views = list(LiveBroker(None, None, sd).limit_orders(tag=LADDER_TAG, active_only=True))
            else:
                from bitpin.broker import limit_view
                ps = read_json(jp) or {}
                views = [limit_view(k, e) for k, e in (ps.get("limits") or {}).items()
                         if isinstance(e, dict) and e.get("tag") == LADDER_TAG and e.get("status") == "resting"]
        except Exception as e:  # noqa: BLE001 - informational
            print("  (the bot's resting bids could not be read: %s)" % e)
    resting = {}
    for v in views:
        meta = v.get("meta") if isinstance(v.get("meta"), dict) else {}
        resting[(str(meta.get("coin") or parse_symbol(v["symbol"])[0]).upper(), lvl_key(meta.get("level_pct")))] = v
    print()
    print("  %-5s %12s %12s %8s %5s %6s %14s %9s %11s  %s" % ("coin", "close USDT", "48h high", "dd48", "scale",
                                                           "level", "bid USDT", "size USDT", "size toman",
                                                           "status"))
    for c in coins:
        ref = refs.get(c)
        for lv in lc["levels_pct"]:
            k = (c, lvl_key(lv))
            w = want.get(k)
            v = resting.get(k)
            if not armed.get(k, True):
                status = "FILLED - disarmed until %s is back above -%g%%" % (c, float(lc["rearm_pct"]))
            elif v is not None:
                status = "resting: %s @ %s%s" % (_num(v.get("remaining_base")), _num(v.get("price")),
                                                 " (would be re-priced)" if w and abs(float(v["price"]) / w["price_usdt"]
                                                                                         - 1) * 100 > float(lc["reprice_pct"])
                                                 else "")
            elif ref is None:
                status = "no 48 h reference (candles missing)"
            elif w is None:
                status = "not placed (%s)" % ("scale 0" if scales.get(c, 0) <= 0 else "below min_order_usdt %g after "
                                              "the pro-rata cut" % min_usdt)
            else:
                status = "not resting now: the bot would place it" if lc["enabled"] else "ladder off"
            px = w["price_usdt"] if w else (ref["hi48"] * (1 + float(lv) / 100.0) if ref else None)
            usdt = w["usdt"] if w else None
            print("  %-5s %12s %12s %8s %5s %6s %14s %9s %11s  %s" % (
                c, _num(ref and ref["close"], "%.6g"), _num(ref and ref["hi48"], "%.6g"),
                ("%+.2f%%" % ref["dd48"]) if ref else "-", "%g" % scales.get(c, 0), "%g%%" % float(lv),
                _num(px, "%.6g"), _num(usdt, "%.2f"), fmt_amount(D(repr(round(usdt * rate)))) if usdt else "-",
                status))
    total = sum(w["usdt"] for w in want.values())
    print()
    print("  %d bid(s), %.2f USDT in total (%.0f%% of equity); pro-rata factor %.2f (the ladder never plans more "
          "USDT than the account holds); a bid below min_order_usdt %g is dropped" % (
              len(want), total, 100 * total / (equity / rate) if equity else 0, factor, min_usdt))
    if min_usdt <= 0.5:
        print("  NOTE: Bitpin publishes no minimum order size; %g USDT is assumed. If the exchange rejects the bids "
              "(log: 'ladder limit buy SYM failed: HTTP 4..'), the bot waits 6 h per market. Each bid is size_frac x "
              "equity whatever the number of coins, so fewer coins alone do NOT make a bid larger: raise "
              "ladder.size_frac (e.g. 0.25 with ladder.coins [\"BTC\", \"ETH\"]: the same total, bids twice as "
              "large) or set ladder.enabled false." % min_usdt)
    return 0


def cmd_cancel_resting(args):
    """Cancel the bot's OWN resting limit orders (all, or one tag) - the bot must be stopped. Foreign
    orders are never touched (the broker refuses orders that are not in its journal / paper state)."""
    cfg = build_config(args)
    sd = cfg["state_dir"]
    lock = StateLock(sd)
    try:
        lock.acquire()
    except RunnerError:
        sys.exit("a bot is running with state dir %s: stop it first (sudo systemctl stop bitpin-bot), then run "
                 "cancel-resting. A running bot would place its ladder bids again at its next hourly check."
                 % os.path.abspath(sd))
    locks = [lock]
    try:
        if args.mode == "live":
            key, secret, swappable = load_credentials(args.keys_file)
            fp = account_fingerprint(key, secret)
            check_account_binding(sd, fp, write=False)
            locks.append(account_lock(fp).acquire())
            setup_logging(sd, "live", args.log_level, secrets=(key, secret))
            client, markets, broker = make_live(cfg, sd, key, secret, swappable)
            del key, secret
        else:
            setup_logging(sd, "paper", args.log_level)
            client = BitpinClient(cfg["base_url"])
            markets = MarketCache(client, sd)
            if not os.path.exists(os.path.join(sd, PaperBroker.STATE_FILE)):
                print("no paper account in %s: nothing to cancel" % os.path.abspath(sd))
                return 0
            broker = PaperBroker(markets, sd, fee_rate=cfg["fee_rate"], client=client)
        views = [v for v in broker.limit_orders(tag=args.tag, active_only=True)]
        if not views:
            print("no resting %sorders of the bot in %s" % ((args.tag + " ") if args.tag else "", os.path.abspath(sd)))
            return 0
        print("the bot's resting orders (%s):" % args.mode)
        for v in views:
            print("  %s %s %s %s @ %s (%s left, %s) identifier %s" % (v.get("tag"), v.get("side"), v.get("symbol"),
                                                                     _num(v.get("base_amount")), _num(v.get("price")),
                                                                     _num(v.get("remaining_base")), v.get("state"),
                                                                     v.get("identifier")))
        if not args.yes:
            try:
                a = input("Type yes to cancel them: ")
            except EOFError:
                a = ""
            if a.strip().lower() != "yes":
                print("nothing cancelled")
                return 1
        bad = 0
        pending = []
        for v in views:
            try:
                after = broker.cancel(v["identifier"]) or {}
            except Exception as e:  # noqa: BLE001 - reported; the bot's next sync resolves it
                bad += 1
                print("  cancel of %s FAILED: %s (the bot's next sync finds its real state)" % (v["identifier"], e))
                continue
            if after.get("final") or after.get("state") in LIMIT_FINAL_STATES:
                print("  cancelled %s: %s (filled %s)" % (v["identifier"], after.get("state"),
                                                          _num(after.get("filled_base"))))
            else:
                # LiveBroker.cancel() swallows a failed DELETE (timeout, 5xx) and Bitpin closes cancels
                # asynchronously: an order that is not final yet is NOT confirmed as cancelled
                pending.append(v)
        if pending:
            broker_wait = getattr(broker, "sleep", None) or time.sleep
            try:
                broker_wait(5.0)
            except Exception:  # noqa: BLE001
                pass
            for v in pending:
                try:
                    after = broker.cancel(v["identifier"]) or {}      # re-sends the cancel, reads the state again
                except Exception as e:  # noqa: BLE001
                    after = {"state": "unknown (%s)" % e}
                if after.get("final") or after.get("state") in LIMIT_FINAL_STATES:
                    print("  cancelled %s: %s (filled %s)" % (v["identifier"], after.get("state"),
                                                              _num(after.get("filled_base"))))
                    continue
                bad += 1
                print("  cancel of %s %s %s @ %s requested, but it is STILL OPEN (%s). Check it in the Bitpin app "
                      "(cancel it there if needed) or run cancel-resting again; do NOT delete the API key before it "
                      "is gone." % (v["identifier"], v.get("side"), v.get("symbol"), _num(v.get("price")),
                                    after.get("state")))
        if bad:
            print("NOT DONE: %d order(s) are not confirmed as cancelled (see above)." % bad)
        else:
            print("done: every order is confirmed closed.")
        print("A fill that happened before a cancel is booked by the bot at its next start. With the Kimi brain "
              "the bot places the ladder bids again at its next hourly check unless ladder.enabled is false in "
              "config.json (then run confirm-live) or Kimi's scale for the coin is 0.")
        return 1 if bad else 0
    finally:
        release_all(locks)


def not_dumpable():
    """prctl(PR_SET_DUMPABLE, 0) on Linux (never fatal): other processes of the SAME user - e.g. the
    Telegram notifier, which runs as user bitpin too - can then no longer read /proc/<pid>/environ
    (the Bitpin keys), /proc/<pid>/mem or walk /proc/<pid>/root of the bot (they would need
    CAP_SYS_PTRACE). No core dumps either (the unit already has LimitCORE=0). Returns True when set."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0) != 0:          # PR_SET_DUMPABLE = 4
            log.warning("prctl(PR_SET_DUMPABLE, 0) failed: errno %d", ctypes.get_errno())
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("prctl(PR_SET_DUMPABLE, 0) unavailable: %s", e)
        return False


def main(argv=None):
    not_dumpable()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--strategy", help="strategy name (default from config, else selected = hold_usdt)")
    common.add_argument("--params", help="strategy params as JSON")
    common.add_argument("--config", help="config JSON (see config.example.json)")
    common.add_argument("--state-dir", help="state directory (default from config, else ./state)")
    common.add_argument("--log-level", default="INFO")

    ap = argparse.ArgumentParser(description="Bitpin trading bot", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    p = sub.add_parser("status", parents=[common], help="read-only account status + IRT confirmation")
    p.add_argument("--keys-file")
    p.add_argument("--no-confirm", action="store_true", help="do not ask for the IRT balance confirmation")
    p.add_argument("--kimi-config", help="kimi.json: show the weights of the Kimi brain's symbols (what "
                                         "'live --brain kimi' manages) instead of the strategy's")
    p.set_defaults(func=cmd_status)

    brain_args = argparse.ArgumentParser(add_help=False)
    brain_args.add_argument("--brain", choices=["kimi"],
                            help="let the Kimi LLM decide the allocation (instead of --strategy)")
    brain_args.add_argument("--kimi-config", help="kimi.json (see kimi.example.json); required with --brain kimi")

    p = sub.add_parser("paper", parents=[common, brain_args], help="paper trading against the live order book")
    p.add_argument("--capital-irt", type=optional_decimal,
                   help="starting IRT for a NEW paper account (empty = the paper broker's own default)")
    p.add_argument("--reset-paper", action="store_true", help="archive the existing paper account and start over")
    p.add_argument("--once", action="store_true", help="process the latest closed bar once and exit")
    p.add_argument("--dry-run", action="store_true", help="print the plan only")
    p.add_argument("--fill-through", type=optional_decimal, default=None,
                   # built by concatenation, never %-formatted here: argparse %-expands help strings itself
                   # (Python 3.14 checks that in add_argument), so the literal percent sign stays doubled
                   help="paper limit-fill model: a resting order fills on a trade this far THROUGH its price "
                        "(fraction, default " + PAPER_FILL_THROUGH + " = 0.5%%; 0 = a touch fills the whole "
                        "remainder), or when the live book crosses it")
    p.add_argument("--max-wait", type=float, default=180, help="--once: seconds to wait for a fresh candle")
    p.add_argument("--test-kimi-reply", metavar="FILE|unreachable",
                   help="TEST ONLY (paper mode only, does not exist for live): no request goes to Moonshot; every "
                        "Kimi reply is the text of FILE, or 'unreachable' simulates a Kimi outage")
    p.add_argument("--test-kimi-news", metavar="FILE|unreachable",
                   help="TEST ONLY (paper mode only, with --test-kimi-reply): the stage-1 news researcher gets the "
                        "text of FILE as its reply after one simulated web-search round, or 'unreachable' simulates "
                        "dropped connections. No request goes to Moonshot")
    p.set_defaults(func=cmd_paper)

    p = sub.add_parser("live", parents=[common, brain_args], help="LIVE trading with real orders")
    p.add_argument("--keys-file")
    p.add_argument("--i-accept-the-risk", action="store_true")
    p.add_argument("--non-interactive", action="store_true",
                   help="for the systemd service: no typed confirmation; needs --i-accept-the-risk and a matching "
                        "LIVE_CONFIRMED from confirm-live")
    p.add_argument("--dry-run", action="store_true", help="authenticate + plan, never send orders")
    p.add_argument("--once", action="store_true")
    p.add_argument("--max-wait", type=float, default=180)
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("confirm-live", parents=[common],
                       help="one-time interactive confirmation that allows 'live --non-interactive' (the service)")
    p.add_argument("--kimi-config", help="the kimi.json the service uses (with --brain kimi)")
    how = p.add_mutually_exclusive_group()
    how.add_argument("--check", action="store_true",
                     help="only check (read-only, no terminal): exit 0 if an existing confirmation matches these "
                          "settings, else 1")
    how.add_argument("--show", action="store_true",
                     help="print the confirmation summary only (read-only, no terminal, nothing is asked or written)")
    how.add_argument("--typed-phrase", metavar="PHRASE",
                     help="the phrase the owner typed elsewhere (the management panel) instead of asking in a "
                          "terminal; it must be exactly \"%s\"" % LIVE_PHRASE)
    p.set_defaults(func=cmd_confirm_live, brain=None)

    p = sub.add_parser("kimi-check", parents=[common],
                       help="check the Kimi key, the route (proxy) and the model ids - no trading")
    p.add_argument("--kimi-config", required=True, help="kimi.json")
    p.add_argument("--decide", action="store_true",
                   help="also make ONE real stage-2 decision call with the shipped settings (model, JSON mode, "
                        "max_tokens) over a small synthetic context, and check that the validator accepts the reply")
    p.add_argument("--news", action="store_true",
                   help="also run ONE small news research call (stage 1, the news model with web search; about "
                        "20-50k tokens) in a fresh temporary directory, AND the --decide call; NEWS: OK only for a "
                        "newly fetched brief")
    p.set_defaults(func=cmd_kimi_check, brain=None)

    p = sub.add_parser("set-model", help="switch the Kimi model(s) in kimi.json (root on the server: sudo bitpin-bot "
                                         "set-model ...): backup, only the model keys change, validated")
    p.add_argument("--kimi-config", required=True, help="the kimi.json to change (server: /etc/bitpin-bot/kimi.json)")
    p.add_argument("--config", help="config.json (for the bot's own check of the result)")
    p.add_argument("--decision-model", metavar="ID", help="stage 2: llm.model")
    p.add_argument("--news-model", metavar="ID", help="stage 1: news.model (needs $web_search: kimi-k3 is refused)")
    p.add_argument("--base-url", metavar="URL", help="llm.base_url (a plain https URL on api.moonshot.ai / "
                                                    "api.moonshot.cn; news.base_url too when set there)")
    p.add_argument("--any-host", action="store_true",
                   help="accept a --base-url host other than api.moonshot.ai / api.moonshot.cn (the Kimi key is sent "
                        "there)")
    p.add_argument("--thinking", action="store_true",
                   help="treat the new model(s) as thinking models: temperature null, max_tokens at least 32000 "
                        "(decision) / 8000 (news); automatic for kimi-k3 / kimi-k2.6 / *thinking*")
    p.add_argument("--list", action="store_true", help="list the model ids of GET {base_url}/models (the bot's key and "
                                                      "Kimi route)")
    p.add_argument("--test", action="store_true",
                   help="ONE tiny real chat call per new model (a few tokens) before anything is written; nothing is "
                        "written when it fails")
    p.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    p.add_argument("--backup-dir", help="where the old kimi.json is copied (default /var/backups/bitpin-bot for "
                                        "/etc/bitpin-bot/kimi.json, else next to the file)")
    p.add_argument("--live-running", action="store_true",
                   help="set by 'bitpin-bot set-model' when the live service is active: a prominent warning (the "
                        "bot must be confirmed and restarted before any automatic restart)")
    p.set_defaults(func=cmd_set_model)

    p = sub.add_parser("ladder", parents=[common],
                       help="read-only: the crash-ladder bids the bot would keep now (public data, no keys)")
    p.add_argument("--kimi-config", help="kimi.json (ladder coins, endgame)")
    p.add_argument("--mode", choices=["paper", "live"], default="live", help="whose state to read (default live)")
    p.add_argument("--equity-irt", type=optional_decimal, help="account value in toman (default: the bot's last cycle)")
    p.add_argument("--usdt", type=optional_decimal,
                   help="USDT the ladder may use (default: equity / USDT_IRT, i.e. the whole account in USDT)")
    p.set_defaults(func=cmd_ladder, brain=None)

    p = sub.add_parser("cancel-resting", parents=[common],
                       help="cancel the bot's own resting limit orders (ladder bids / target sells); bot stopped")
    p.add_argument("--mode", choices=["paper", "live"], required=True)
    p.add_argument("--tag", choices=[LADDER_TAG, TARGET_TAG], help="only this kind (default: all)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--keys-file")
    p.set_defaults(func=cmd_cancel_resting)

    for name, fn, hlp in (("stop", cmd_stop, "create the kill-switch file"),
                          ("resume", cmd_resume, "remove the kill-switch file")):
        p = sub.add_parser(name, parents=[common], help=hlp)
        p.set_defaults(func=fn)
    p = sub.add_parser("risk-reset", parents=[common], help="clear a drawdown halt and the high-water mark")
    p.add_argument("--mode", choices=["paper", "live"], required=True)
    p.set_defaults(func=cmd_risk_reset)

    p = sub.add_parser("resolve-order", parents=[common],
                       help="list bot orders of unknown outcome, or record the outcome of one")
    p.add_argument("--identifier", help="the order's identifier (from the log or the listing)")
    p.add_argument("--order-id", help="the order's id in the Bitpin app: its final fill is read from Bitpin")
    p.add_argument("--not-executed", action="store_true", help="record that the order never executed")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation (--not-executed)")
    p.add_argument("--keys-file")
    p.set_defaults(func=cmd_resolve_order)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except (AuthError, AuthBudgetExceeded) as e:
        # Runner.FATAL_ERRORS: deliberately re-raised out of Runner.loop(). Neither subclasses
        # RunnerError / BrokerError, so both used to escape as an uncaught traceback with status 1 -
        # which RestartPreventExitStatus=0 78 does not cover, so systemd restarted the unit for
        # ever. A revoked key, a changed outbound IP and an exhausted auth budget are all things a
        # restart cannot fix: exit 78 and let health report last-exit=78.
        die("authentication problem: %s\nThe bot stops (exit %d): a restart cannot fix this. Check the Bitpin key "
            "and that this server's IP is the whitelisted one, then: sudo systemctl start bitpin-bot"
            % (e, EXIT_CONFIG))
    except BitpinError as e:
        sys.exit("error: %s" % e)
    except (RunnerError, BrokerError) as e:
        sys.exit("error: %s" % e)


if __name__ == "__main__":
    sys.exit(main())
