"""LLM cost meter: USD per Kimi request and per UTC day / month / all time, from the usage logs
(stdlib only, Python 3.7+). Imports nothing from bitpin except the atomic JSON writer.

WHY: the one-year competition is paid for out of a prepaid Moonshot balance (about $50 for 13 months
in the year review's plan). The daily token budgets in kimi_budget.json / news_budget.json guard one
day's damage but say nothing in dollars, keep only 7 days, and do not know the three prices (uncached
input, cached input, output) that decide what a day really cost. This module turns the per-request
usage records into money, so the owner sees a daily line and an outage of the account balance (an
exhausted-quota 429 twice in a row) is alerted before the fallback derisks the book.

* Inputs: state_dir/kimi_usage.jsonl (stage 2, written by bitpin.llm) and state_dir/news_usage.jsonl
  (stage 1, written by bitpin.news). One JSON object per line, one line per HTTP request:
  {"time": epoch, "model": str, "usage": {prompt_tokens, completion_tokens, total_tokens, cached
  tokens when the API reports them}, "usd": float (priced by the writer with its configured
  prices), "stage": "llm" | "news", "usage_estimated": bool, ...}. A request that was refused with
  an exhausted-quota 429 is logged as {"error": "llm_quota" | "news_quota", "usage": {}} so the
  quota streak below can be measured from the same file.
* Prices: USD per million tokens, three per model - uncached input, cached input (Moonshot's
  automatic prompt cache above 256 prompt tokens bills the hit part lower), output (reasoning tokens
  included). Config keys llm.price_in_per_m / price_out_per_m / price_cached_in_per_m (kimi-k3
  defaults 3 / 15 / 0.30) and the same three under news (kimi-k2.6 defaults 1 / 4 / 0.16). The
  writer prices every record at write time ("usd"), so a later price change never rewrites history;
  records of an older version without "usd" are priced with the DEFAULT prices of their stage
  (cost_usd, with the kimi-k2.x prices for a kimi-k2 model found in the stage-2 log).
* Output: summary() -> the dict documented there; refresh() also writes state_dir/llm_spend.json
  {today_usd, month_usd, total_usd, ... quota_errors_in_a_row, last_quota_error_at} atomically. The
  Telegram notifier (bitpin/notify.py, read-only) reads that file for its daily line and the quota
  alert; it never recomputes. Everything here never raises into a Kimi call: refresh() logs and
  returns what it could compute.
* Size: a year of the bot is a few hundred records per log; only the LAST MAX_LOG_BYTES of a log
  are read (a runaway file cannot stall a Kimi call), unreadable lines are skipped and counted.
"""
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

from .api import atomic_write_json

log = logging.getLogger("bitpin.spend")

SPEND_FILE = "llm_spend.json"
LLM_USAGE_LOG = "kimi_usage.jsonl"       # stage 2 (bitpin.llm.USAGE_LOG)
NEWS_USAGE_LOG = "news_usage.jsonl"      # stage 1 (bitpin.news.USAGE_LOG)
STAGES = ("llm", "news")
MAX_LOG_BYTES = 16 * 1024 * 1024         # only the tail of a log this large is read (~50k records)
PRICE_KEYS = ("price_in_per_m", "price_out_per_m", "price_cached_in_per_m")
MAX_PRICE_PER_M = 1000.0                 # a price above this USD/M is a typo (kimi-k3 output is 15)
QUOTA_ERROR_KINDS = ("llm_quota", "news_quota")
QUOTA_ALERT_STREAK = 2                   # consecutive exhausted-quota errors that mean "the balance is gone"
# Moonshot list prices, USD per million tokens (year_review/Y3-cost-plan/cost_tiers.py, 2026-09):
# kimi-k3 $3 in / $0.30 cached in / $15 out (reasoning included); kimi-k2.6 $1 in (rounded from
# $0.95) / $0.16 cached in / $4 out. The config keys above override them per stage.
DEFAULT_PRICES = {"llm": {"in": 3.0, "cached_in": 0.30, "out": 15.0},
                  "news": {"in": 1.0, "cached_in": 0.16, "out": 4.0}}
# a model id with this prefix is priced with the news (kimi-k2.x) prices even when stage 2 made the
# call (chat(model="kimi-k2.6") for a cheaper call next to the kimi-k3 decision model)
K2_PREFIXES = ("kimi-k2",)

_MSG_FA_QUOTA = "اعتبار Moonshot تمام شده"   # "the Moonshot balance is used up"
_MSG_FA_DERISK = "تا %s ساعت دیگر derisk"                    # "derisk in N hours"
_MSG_FA_TOPUP = "شارژ کنید"                                              # "top it up"


# --------------------------------------------------------------------------- prices

def _fnum(x):
    if x is None or isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        v = float(x)
    except (OverflowError, ValueError):
        return None
    return v if math.isfinite(v) else None


def prices_from_config(cfg, stage="llm"):
    """{"in", "cached_in", "out"} USD per million tokens from a VALIDATED llm / news config section
    (its price_in_per_m / price_out_per_m / price_cached_in_per_m keys), falling back to the
    DEFAULT_PRICES of `stage` for a key that is absent or not a number. Never raises: the meter must
    never stop a Kimi call."""
    base = dict(DEFAULT_PRICES.get(stage) or DEFAULT_PRICES["llm"])
    cfg = cfg if isinstance(cfg, dict) else {}
    for key, name in (("price_in_per_m", "in"), ("price_out_per_m", "out"), ("price_cached_in_per_m", "cached_in")):
        v = _fnum(cfg.get(key))
        if v is not None and 0 <= v <= MAX_PRICE_PER_M:
            base[name] = v
    return base


def prices_for(stage, model=None, prices=None):
    """The price table of one record: `prices` ({"llm": {...}, "news": {...}}, default
    DEFAULT_PRICES) by `stage`, except that a kimi-k2.x model is always priced as news (stage 2 may
    call kimi-k2.6 for a cheaper call; those tokens do not cost kimi-k3 money)."""
    table = prices if isinstance(prices, dict) else DEFAULT_PRICES
    st = stage if stage in STAGES else "llm"
    if isinstance(model, str) and model.strip().lower().startswith(K2_PREFIXES):
        st = "news"
    p = table.get(st) if isinstance(table.get(st), dict) else None
    return dict(p) if p else dict(DEFAULT_PRICES[st])


def cached_tokens(usage):
    """The cached (prompt-cache hit) input tokens of a usage dict: Moonshot's top-level
    "cached_tokens", the OpenAI-style "prompt_tokens_details": {"cached_tokens"}, or the DeepSeek-style
    "prompt_cache_hit_tokens"; 0 when none is there. Never more than prompt_tokens."""
    if not isinstance(usage, dict):
        return 0
    v = _fnum(usage.get("cached_tokens"))
    if v is None:
        det = usage.get("prompt_tokens_details")
        v = _fnum(det.get("cached_tokens")) if isinstance(det, dict) else None
    if v is None:
        v = _fnum(usage.get("prompt_cache_hit_tokens"))
    v = max(0.0, v or 0.0)
    prompt = _fnum(usage.get("prompt_tokens"))
    if prompt is not None:
        v = min(v, max(0.0, prompt))
    return int(v)


def cost_usd(usage, prices):
    """USD of one request: (prompt - cached) * in + cached * cached_in + completion * out, all per
    million tokens, rounded to 6 decimals (a kimi-k3 decision is about $0.1..0.2). A usage without
    numbers costs 0. `prices`: {"in", "cached_in", "out"} (prices_for / prices_from_config)."""
    if not isinstance(usage, dict) or not isinstance(prices, dict):
        return 0.0
    prompt = max(0.0, _fnum(usage.get("prompt_tokens")) or 0.0)
    completion = max(0.0, _fnum(usage.get("completion_tokens")) or 0.0)
    if prompt == 0.0 and completion == 0.0:
        # total_tokens only (an odd record): billed as uncached input, the conservative choice
        prompt = max(0.0, _fnum(usage.get("total_tokens")) or 0.0)
    cached = float(cached_tokens(usage))
    p_in = max(0.0, _fnum(prices.get("in")) or 0.0)
    p_cached = max(0.0, _fnum(prices.get("cached_in")) or 0.0)
    p_out = max(0.0, _fnum(prices.get("out")) or 0.0)
    usd = ((prompt - cached) * p_in + cached * p_cached + completion * p_out) / 1e6
    return round(max(0.0, usd), 6)


# --------------------------------------------------------------------------- the logs

def _tail_bytes(path, max_bytes=MAX_LOG_BYTES):
    """The last `max_bytes` of a file (from the first complete line), b"" when it cannot be read."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()            # drop the partial first line
            return f.read(max_bytes + 1024)
    except OSError:
        return b""


def read_records(path, stage=None, max_bytes=MAX_LOG_BYTES):
    """(records, skipped) of one usage log: every readable JSON object line as a dict (with "stage"
    set to `stage` when the line has none), oldest first as written; `skipped` counts the lines that
    were not a JSON object. A missing or unreadable file gives ([], 0)."""
    out, skipped = [], 0
    raw = _tail_bytes(path, max_bytes)
    if not raw:
        return out, skipped
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):
            skipped += 1
            continue
        if not isinstance(rec, dict):
            skipped += 1
            continue
        if stage and not isinstance(rec.get("stage"), str):
            rec["stage"] = stage
        out.append(rec)
    return out, skipped


def _record_usd(rec, prices):
    """The USD of one record: its own "usd" when the writer priced it, else priced now (an older
    record) with the DEFAULT / given prices of its stage and model. Error records cost 0."""
    if rec.get("error"):
        return 0.0
    v = _fnum(rec.get("usd"))
    if v is not None and v >= 0:
        return v
    return cost_usd(rec.get("usage"), prices_for(rec.get("stage"), rec.get("model"), prices))


def _tokens(rec):
    u = rec.get("usage") if isinstance(rec.get("usage"), dict) else {}
    v = _fnum(u.get("total_tokens"))
    if v is None:
        v = (_fnum(u.get("prompt_tokens")) or 0.0) + (_fnum(u.get("completion_tokens")) or 0.0)
    return int(max(0.0, v))


def utc_day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def utc_month(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def quota_streak(records):
    """(count, last_at) of the exhausted-quota errors at the END of `records` (time order): how many
    of the latest requests in a row were refused with a 429 quota / balance error, and the time of
    the latest one (None without any). A successful request resets the streak: the account has
    money again."""
    n, last = 0, None
    for rec in reversed(records):
        if rec.get("error") in QUOTA_ERROR_KINDS:
            n += 1
            t = _fnum(rec.get("time"))
            if last is None and t is not None:
                last = t
            continue
        break
    return n, last


def _empty_bucket():
    return {"usd": 0.0, "requests": 0, "tokens": 0}


def _add(bucket, usd, tokens):
    bucket["usd"] = round(bucket["usd"] + usd, 6)
    bucket["requests"] += 1
    bucket["tokens"] += tokens


def summary(state_dir, now=None, prices=None):
    """The spend summary of a state directory, computed from its usage logs (read-only):
    {"today_usd", "month_usd", "total_usd": USD of the requests of the current UTC day / calendar
     month / all records, "today_requests", "month_requests", "total_requests", "today_tokens",
     "month_tokens", "total_tokens", "day": "YYYY-MM-DD", "month": "YYYY-MM",
     "by_stage": {"llm": {"today_usd", "month_usd", "total_usd", "total_requests"}, "news": {...}},
     "days": {"YYYY-MM-DD": {"usd", "requests", "tokens"}} of the last 31 UTC days with records,
     "since": epoch of the oldest record (None without any), "records": int, "skipped_lines": int,
     "estimated_requests": records whose usage was estimated (a lost or usage-less reply),
     "quota_errors_in_a_row": int, "last_quota_error_at": epoch or None,
     "quota_alert": bool (the streak reached QUOTA_ALERT_STREAK), "updated_at": now}.
    `prices` ({"llm": {...}, "news": {...}}) prices only the records that carry no "usd". A request
    is one HTTP request (a chat() call with tool rounds is several). Never raises."""
    now = float(time.time() if now is None else now)
    day, month = utc_day(now), utc_month(now)
    out = {"day": day, "month": month, "updated_at": round(now, 3), "since": None, "records": 0, "skipped_lines": 0,
           "estimated_requests": 0, "days": {}, "by_stage": {}}
    totals = {"today": _empty_bucket(), "month": _empty_bucket(), "total": _empty_bucket()}
    per_stage = {s: {"today": _empty_bucket(), "month": _empty_bucket(), "total": _empty_bucket()} for s in STAGES}
    records = []
    if isinstance(state_dir, str) and state_dir:
        for name, stage in ((LLM_USAGE_LOG, "llm"), (NEWS_USAGE_LOG, "news")):
            recs, skipped = read_records(os.path.join(state_dir, name), stage)
            records.extend(recs)
            out["skipped_lines"] += skipped
    records.sort(key=lambda r: _fnum(r.get("time")) or 0.0)
    days = {}
    for rec in records:
        t = _fnum(rec.get("time"))
        if t is None or t < 0:
            out["skipped_lines"] += 1
            continue
        out["records"] += 1
        if out["since"] is None or t < out["since"]:
            out["since"] = t
        if rec.get("error"):
            continue                       # a refused request: no tokens, no money
        usd, tokens = _record_usd(rec, prices), _tokens(rec)
        if rec.get("usage_estimated") is True:
            out["estimated_requests"] += 1
        stage = rec.get("stage") if rec.get("stage") in STAGES else "llm"
        d = utc_day(t)
        _add(totals["total"], usd, tokens)
        _add(per_stage[stage]["total"], usd, tokens)
        _add(days.setdefault(d, _empty_bucket()), usd, tokens)
        if d == day:
            _add(totals["today"], usd, tokens)
            _add(per_stage[stage]["today"], usd, tokens)
        if utc_month(t) == month:
            _add(totals["month"], usd, tokens)
            _add(per_stage[stage]["month"], usd, tokens)
    for period, bucket in totals.items():
        out["%s_usd" % period] = bucket["usd"]
        out["%s_requests" % period] = bucket["requests"]
        out["%s_tokens" % period] = bucket["tokens"]
    for stage in STAGES:
        out["by_stage"][stage] = {"today_usd": per_stage[stage]["today"]["usd"],
                                  "month_usd": per_stage[stage]["month"]["usd"],
                                  "total_usd": per_stage[stage]["total"]["usd"],
                                  "total_requests": per_stage[stage]["total"]["requests"]}
    out["days"] = {k: days[k] for k in sorted(days)[-31:]}
    n, last = quota_streak(records)
    out["quota_errors_in_a_row"], out["last_quota_error_at"] = n, last
    out["quota_alert"] = n >= QUOTA_ALERT_STREAK
    return out


def refresh(state_dir, now=None, prices=None):
    """summary() of `state_dir`, also written to state_dir/llm_spend.json (atomically) for the
    notifier. Returns the summary; on a write error it logs a warning and still returns it. With
    state_dir None (an in-memory client) nothing is read or written and {} is returned."""
    if not isinstance(state_dir, str) or not state_dir:
        return {}
    try:
        s = summary(state_dir, now, prices)
    except Exception as e:  # noqa: BLE001 - the meter must never break a Kimi call
        log.warning("LLM spend meter failed: %s: %s", type(e).__name__, e)
        return {}
    try:
        atomic_write_json(os.path.join(state_dir, SPEND_FILE), s)
    except OSError as e:
        log.warning("cannot write %s: %s", os.path.join(state_dir, SPEND_FILE), e)
    return s


def read_summary(state_dir):
    """The last written state_dir/llm_spend.json as a dict ({} when missing or unreadable): what a
    READ-ONLY reader (the notifier) uses instead of recomputing."""
    if not isinstance(state_dir, str) or not state_dir:
        return {}
    try:
        with open(os.path.join(state_dir, SPEND_FILE), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def quota_message_fa(hours_to_derisk=None):
    """The owner's Persian alert for an exhausted Moonshot balance: 'the Moonshot balance is used up;
    derisk in N hours' (N = hours until the fallback's derisk_after_hours sells every coin into
    USDT_IRT, from the last valid Kimi decision), or '...; top it up' when N is unknown."""
    h = _fnum(hours_to_derisk)
    if h is None:
        return "%s؛ %s" % (_MSG_FA_QUOTA, _MSG_FA_TOPUP)
    h = max(0.0, h)
    txt = ("%d" % int(round(h))) if abs(h - round(h)) < 0.05 or h >= 10 else ("%.1f" % h)
    return "%s؛ %s" % (_MSG_FA_QUOTA, _MSG_FA_DERISK % txt)


def format_usd(v):
    """'$0.12' style, 2 decimals below $100 and none above; '$0' for nothing."""
    v = _fnum(v) or 0.0
    return ("$%.0f" % v) if v >= 100 else ("$%.2f" % v)
