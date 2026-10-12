"""Isolated owner-run K3 forecast experiment; preparation/scoring never read credentials.

This module imports neither the live model client nor account/settings modules. Dollars
are integer micro-USD reservations, retained even when billing is absent or a call fails.
Synthetic transports are permanently marked fixtures, never measured Kimi evidence.
"""
import copy
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time

PURPOSE = "bitpin_kimi_architecture_research_v1"
PROMPT_VERSION = "blinded-24h-v1"
ENDPOINT = "https://api.moonshot.ai/v1/chat/completions"
MODEL = "kimi-k3"
CAP_MICRO_USD = 10000000
OUTPUT_LIMIT = 8192
INPUT_FRAMING_TOKENS = 4096
INPUT_USD_PER_M = 3
CACHE_WRITE_USD_PER_M_BOUND = 3
OUTPUT_USD_PER_M = 15
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 128 * 1024
ARMS = ("raw_history", "structured_packet")
FEATURES = ("return_1", "return_7", "return_30", "volatility_14", "volume_ratio")
GROUP_CAPS = {"technical": .50, "flow": .20, "macro": .15, "whale": .05, "news": .10}
STATUSES = ("eligible", "not_available_as_of", "missing", "untrained", "stale", "disputed")
IDENTITY_FIELDS = ("episode_id", "decision_time", "label_end", "symbol", "target", "cost_band", "return_currency")
SYSTEM_PROMPT = (
    "Forecast the next 24-hour fractional return for blinded asset_A. Return only JSON "
    "matching the schema. down is return < -cost_band; flat includes both boundaries; "
    "up is return > cost_band. Probabilities must sum to one. target_weight is an optional "
    "hypothetical spot allocation in [0,1], never an order. Use only supplied numeric "
    "evidence; no tools, web, identities, calendar inference, remembered events, or "
    "external knowledge. Cite supplied evidence IDs or an empty list. History contains "
    "60 consecutive closed daily bars: open/close normalized by the last closed close, "
    "volume normalized by the 60-bar mean. Features return_1/7/30 are fractional close "
    "returns; volatility_14 is daily population return standard deviation; volume_ratio "
    "is last volume divided by its 30-bar mean minus one. Unknown feature vector "
    "coordinates have no asserted economic meaning; acknowledge uncertainty."
)
REPLY_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {
                    "forecast_horizon_hours": {"type": "integer", "enum": [24]},
                    "probabilities": {"type": "object", "additionalProperties": False,
                                      "properties": {c: {"type": "number", "minimum": 0, "maximum": 1}
                                                     for c in ("down", "flat", "up")},
                                      "required": ["down", "flat", "up"]},
                    "target_weight": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    "decision": {"type": ["string", "null"], "enum": ["long", "cash", "hold", None]},
                    "rationale": {"type": "string", "maxLength": 2400},
                    "evidence_ids": {"type": "array", "maxItems": 128,
                                     "items": {"type": "string", "pattern": "^e[0-9]+$"}}},
                "required": ["forecast_horizon_hours", "probabilities", "target_weight",
                             "decision", "rationale", "evidence_ids"]}


class ExperimentError(ValueError):
    """Safe fixed-message experiment failure; never includes keys or server error bodies."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _number(value, bound=1000000):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperimentError("numeric model evidence required")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ExperimentError("invalid numeric evidence") from None
    if not math.isfinite(result) or abs(result) > bound:
        raise ExperimentError("numeric evidence out of bounds")
    return result


def _probabilities(value):
    if isinstance(value, dict) and set(value) == {"down", "flat", "up"}:
        value = [value[c] for c in ("down", "flat", "up")]
    if not isinstance(value, list) or len(value) != 3:
        raise ExperimentError("three forecast probabilities required")
    result = [_number(p, 1) for p in value]
    if min(result) < 0 or abs(sum(result) - 1) > 1e-8:
        raise ExperimentError("forecast probabilities must sum to one")
    return result


def _packet(value, cost_band):
    if not isinstance(value, dict) or value.get("target") != "next_24h_down_flat_up":
        raise ExperimentError("packet target mismatch")
    if _number(value.get("cost_band"), 1) != cost_band:
        raise ExperimentError("packet cost band mismatch")
    result = {"baseline_probs": _probabilities(value.get("baseline_probs")), "groups": [], "coverage": {}}
    seen, total = set(), 0
    for row in value.get("groups", []):
        if not isinstance(row, dict) or row.get("group") not in GROUP_CAPS or row["group"] in seen:
            raise ExperimentError("invalid packet group")
        group = row["group"]
        seen.add(group)
        weight = _number(row.get("weight"), 1)
        status = row.get("status")
        if status not in STATUSES or weight < 0 or weight > GROUP_CAPS[group]:
            raise ExperimentError("invalid packet influence")
        if status != "eligible" and weight != 0:
            raise ExperimentError("unavailable group cannot influence forecast")
        result["groups"].append({"group": group, "probabilities": _probabilities(row.get("probabilities")),
                                 "weight": weight, "status": status})
        total += weight
    if total > 1 + 1e-10:
        raise ExperimentError("packet influence exceeds one")
    coverage = value.get("coverage", {})
    if not isinstance(coverage, dict):
        raise ExperimentError("invalid packet coverage")
    for group in GROUP_CAPS:
        status = coverage.get(group, "missing")
        if status not in STATUSES:
            raise ExperimentError("invalid packet coverage status")
        result["coverage"][group] = status
    return result


def _input(episode):
    if not isinstance(episode, dict) or episode.get("target") != "next_24h_down_flat_up":
        raise ExperimentError("eligible 24-hour episode required")
    decision = _number(episode.get("decision_time"), 1e12)
    end = _number(episode.get("label_end"), 1e12)
    if end - decision != 86400:
        raise ExperimentError("episode horizon must equal 24 hours")
    band = _number(episode.get("cost_band"), 1)
    if band <= 0:
        raise ExperimentError("positive prespecified cost band required")
    features = episode.get("features")
    if isinstance(features, dict):
        if not all(name in features for name in FEATURES):
            raise ExperimentError("complete historical feature set required")
        features = {name: _number(features[name]) for name in FEATURES}
    elif isinstance(features, list) and 1 <= len(features) <= 64:
        features = [_number(v) for v in features]
    else:
        raise ExperimentError("normalized feature values required")
    history = episode.get("history")
    if not isinstance(history, list) or len(history) != 60:
        raise ExperimentError("60 normalized closed historical bars required")
    normalized = []
    for n, row in enumerate(history):
        if isinstance(row, dict):
            if row.get("step") != n - 59:
                raise ExperimentError("history relative ordering mismatch")
            clean = {k: _number(row.get(k)) for k in ("open", "close", "volume")}
            if clean["open"] <= 0 or clean["close"] <= 0 or clean["volume"] < 0:
                raise ExperimentError("invalid normalized historical bar")
        elif isinstance(row, list) and 1 <= len(row) <= 64:
            clean = [_number(v) for v in row]
        else:
            raise ExperimentError("numeric normalized history required")
        normalized.append(clean)
    result = {"cost_band": band, "features": features, "history": normalized}
    if episode.get("packet") is not None:
        result["packet"] = _packet(episode["packet"], band)
    return result


def _payload(case, arm, effort):
    data = case["input"]
    body = {"asset": "asset_A", "target": "next_24h_down_flat_up", "cost_band": data["cost_band"]}
    values = [(k, data["features"][k]) for k in FEATURES] if isinstance(data["features"], dict) else list(enumerate(data["features"]))
    body["features"] = [{"evidence_id": "e%d" % (61 + i), "feature": str(k), "value": v}
                        for i, (k, v) in enumerate(values)]
    if "packet" in data:
        body["packet"] = data["packet"]
    if arm == "raw_history":
        body["history"] = [{"evidence_id": "e%d" % (i + 1), "relative_age_days": 59 - i, "values": row}
                           for i, row in enumerate(data["history"])]
    return {"model": MODEL, "reasoning_effort": effort, "max_completion_tokens": OUTPUT_LIMIT,
            "prompt_cache_options": {"mode": "implicit", "ttl": "5m"},
            "stream": False, "tool_choice": "none",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": _json(body)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "blinded_spot_forecast", "strict": True, "schema": copy.deepcopy(REPLY_SCHEMA)}}}


def _request(case, arm, repeat, effort):
    payload = _payload(case, arm, effort)
    encoded = _json(payload).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ExperimentError("request exceeds bounded input size")
    # One token per UTF-8 byte, plus schema/message framing allowance; no cache discount.
    upper = len(encoded) + INPUT_FRAMING_TOKENS
    reserve = upper * (INPUT_USD_PER_M + CACHE_WRITE_USD_PER_M_BOUND) + OUTPUT_LIMIT * OUTPUT_USD_PER_M
    signature = _hash({"endpoint": ENDPOINT, "prompt_version": PROMPT_VERSION, "payload": payload})
    return {"call_id": "%s:%s:%d" % (case["case_id"], arm, repeat), "case_id": case["case_id"],
            "arm": arm, "repeat": repeat, "payload": payload, "request_hash": signature,
            "input_token_upper_bound": upper, "reserve_micro_usd": reserve}


def _compatible_cases(cases):
    if len({c["evaluation"]["return_currency"] for c in cases}) != 1 or any(
            c["evaluation"]["return_currency"] not in ("USDT", "IRT") for c in cases):
        raise ExperimentError("one explicit return currency required for a paired experiment")
    if len({c["input"]["cost_band"] for c in cases}) != 1:
        raise ExperimentError("mixed cost bands are different probability targets")
    ends = {}
    for case in sorted(cases, key=lambda c: c["evaluation"]["decision_time"]):
        metadata = case["evaluation"]
        symbol = metadata["symbol"]
        if not isinstance(symbol, str) or not symbol:
            raise ExperimentError("local evaluation symbol required")
        if metadata["decision_time"] < ends.get(symbol, -math.inf):
            raise ExperimentError("overlapping labels for the same symbol are not independent cases")
        ends[symbol] = metadata["label_end"]


def _safe_path(path):
    if not isinstance(path, str) or not path.strip():
        raise ExperimentError("separate research state directory required")
    result = os.path.abspath(path)
    components = result.replace("\\", "/").lower().split("/")
    forbidden = {"state", "live", "collector", "collectors", "bitpin-bot", "bitpin-flow-public",
                 "bitpin-whale-shadow", "bitpin-personal-whale-shadow", "bitpin-defi-shadow"}
    if forbidden.intersection(components) or os.path.dirname(result) == result:
        raise ExperimentError("live or collector state cannot be used for experiment")
    cursor = result
    while True:
        if os.path.lexists(cursor):
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ExperimentError("linked or reparse experiment paths are forbidden")
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    return result


def _artifact_path(state, filename):
    path = os.path.join(state, filename)
    if os.path.lexists(path):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ExperimentError("linked or invalid experiment artifact")
    return path


def _read(state, filename):
    path = _artifact_path(state, filename)
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ExperimentError("experiment artifact exceeds size limit")
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ExperimentError("missing or invalid experiment artifact") from None


def _write(state, filename, value):
    path = _artifact_path(state, filename)
    encoded = (_json(value) + "\n").encode("utf-8")
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ExperimentError("experiment artifact exceeds size limit")
    fd, temporary = tempfile.mkstemp(prefix=".atomic-", dir=state)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(state, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateLock:
    """Nonblocking OS process lock; released by OS on process death, never age-deleted."""
    def __init__(self, state_dir):
        self.state = _safe_path(state_dir)
        self.stream = None

    def __enter__(self):
        if not os.path.isdir(self.state):
            raise ExperimentError("experiment directory is missing")
        path = _artifact_path(self.state, "process.lock")
        self.stream = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if os.path.getsize(path) == 0:
                    self.stream.write(b"\0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise ExperimentError("experiment state is locked by another process") from None
        return self

    def __exit__(self, *_):
        if self.stream is not None:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


def _marked_state(state_dir, create=False):
    state = _safe_path(state_dir)
    if not os.path.exists(state) and create:
        os.makedirs(state)
    if not os.path.isdir(state):
        raise ExperimentError("experiment state is missing")
    marker = _artifact_path(state, "research-marker.json")
    if not os.path.exists(marker):
        if not create or any(n != "process.lock" for n in os.listdir(state)):
            raise ExperimentError("nonempty unmarked state cannot be reused")
        # Marker creation uses O_EXCL so concurrent first preparations cannot replace it.
        try:
            with os.fdopen(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(_json({"purpose": PURPOSE}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
    if _read(state, "research-marker.json") != {"purpose": PURPOSE}:
        raise ExperimentError("experiment research marker mismatch")
    return state


def _manifest(state):
    value = _read(state, "manifest.json")
    if not isinstance(value, dict) or value.get("purpose") != PURPOSE or value.get("prompt_version") != PROMPT_VERSION:
        raise ExperimentError("experiment manifest version mismatch")
    original = value.get("manifest_hash")
    unhashed = dict(value)
    unhashed.pop("manifest_hash", None)
    if original != _hash(unhashed) or value.get("cap_micro_usd") != CAP_MICRO_USD:
        raise ExperimentError("experiment manifest hash or ceiling mismatch")
    cases = value.get("cases")
    requests = value.get("requests")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 16 or not isinstance(requests, list):
        raise ExperimentError("invalid experiment roster")
    if value.get("reasoning_effort") not in ("low", "high", "max") or value.get("endpoint") != ENDPOINT or value.get("model") != MODEL:
        raise ExperimentError("experiment model request policy mismatch")
    if value.get("case_count") != len(cases) or len({c["case_id"] for c in cases}) != len(cases):
        raise ExperimentError("experiment case registration mismatch")
    _compatible_cases(cases)
    expected = []
    for case in cases:
        if case["input_hash"] != _hash(case["input"]):
            raise ExperimentError("experiment input hash mismatch")
        if case["evaluation_identity_hash"] != _hash(case["evaluation"]):
            raise ExperimentError("experiment evaluation identity hash mismatch")
        # Revalidate allowlisted inputs; artifacts cannot add messages/tools via manual editing.
        clean_episode = {"target": "next_24h_down_flat_up", "decision_time": 0, "label_end": 86400,
                         "cost_band": case["input"]["cost_band"], "features": case["input"]["features"],
                         "history": [dict(row, step=n - 59) if isinstance(row, dict) else row
                                     for n, row in enumerate(case["input"]["history"])]}
        if "packet" in case["input"]:
            clean_episode["packet"] = dict(case["input"]["packet"], target="next_24h_down_flat_up",
                                          cost_band=case["input"]["cost_band"])
        if _input(clean_episode) != case["input"]:
            raise ExperimentError("experiment input allowlist mismatch")
        for arm in ARMS:
            expected.append(_request(case, arm, 0, value["reasoning_effort"]))
    repeats = value.get("stability_repeats")
    if isinstance(repeats, bool) or repeats not in (0, 1, 2):
        raise ExperimentError("invalid stability registration")
    for repeat in range(1, repeats + 1):
        for arm in ARMS:
            expected.append(_request(cases[0], arm, repeat, value["reasoning_effort"]))
    if requests != expected or value.get("worst_case_micro_usd") != sum(r["reserve_micro_usd"] for r in expected):
        raise ExperimentError("request signature or reservation mismatch")
    if value["worst_case_micro_usd"] > CAP_MICRO_USD:
        raise ExperimentError("registered worst-case charge exceeds USD 10")
    return value


def load_manifest(state_dir):
    state = _marked_state(state_dir)
    with StateLock(state):
        return _manifest(state)


def prepare_experiment(episodes, state_dir, case_ids=None, max_cases=16, stability_repeats=0, reasoning_effort="low"):
    """Freeze eligible input pairs without labels, env access, credentials, or network.

    If case_ids is omitted, take the first unique eligible inputs in the supplied frozen
    order. Selection never uses outcomes. Existing registrations cannot be replaced.
    """
    if isinstance(max_cases, bool) or not isinstance(max_cases, int) or not 1 <= max_cases <= 16:
        raise ExperimentError("case ceiling must be between 1 and 16")
    if isinstance(stability_repeats, bool) or stability_repeats not in (0, 1, 2):
        raise ExperimentError("at most two predeclared stability pairs allowed")
    if reasoning_effort not in ("low", "high", "max"):
        raise ExperimentError("documented K3 reasoning effort required")
    if not isinstance(episodes, list) or not episodes:
        raise ExperimentError("nonempty frozen episode list required")
    requested = set(case_ids) if case_ids is not None else None
    if requested is not None and len(requested) > max_cases:
        raise ExperimentError("explicit predeclared roster exceeds case ceiling")
    cases, seen, ids, duplicates = [], set(), set(), 0
    for episode in episodes:
        identity = episode.get("episode_id") if isinstance(episode, dict) else None
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            raise ExperimentError("valid episode identity required")
        if requested is not None and identity not in requested:
            continue
        if identity in ids:
            raise ExperimentError("duplicate episode identity")
        ids.add(identity)
        data = _input(episode)
        fingerprint = _hash(data)
        if fingerprint in seen:
            duplicates += 1
            continue
        seen.add(fingerprint)
        if len(cases) < max_cases:
            case_id = "case_%02d" % (len(cases) + 1)
            metadata = {k: episode.get(k) for k in IDENTITY_FIELDS}
            cases.append({"case_id": case_id, "episode_id": identity, "input": data, "input_hash": fingerprint,
                          "evaluation": metadata, "evaluation_identity_hash": _hash(metadata)})
    if requested is not None and not requested.issubset(ids):
        raise ExperimentError("predeclared case is absent from episode input")
    if not cases:
        raise ExperimentError("no unique eligible experiment cases")
    _compatible_cases(cases)
    requests = [_request(case, arm, 0, reasoning_effort) for case in cases for arm in ARMS]
    requests.extend(_request(cases[0], arm, repeat, reasoning_effort)
                    for repeat in range(1, stability_repeats + 1) for arm in ARMS)
    worst = sum(r["reserve_micro_usd"] for r in requests)
    if worst > CAP_MICRO_USD:
        raise ExperimentError("registered worst-case charge exceeds USD 10; reduce predeclared case count")
    with open(__file__, "rb") as stream:
        code_hash = hashlib.sha256(stream.read()).hexdigest()
    manifest = {"purpose": PURPOSE, "prompt_version": PROMPT_VERSION, "endpoint": ENDPOINT, "model": MODEL,
                "reasoning_effort": reasoning_effort, "cases": cases, "requests": requests,
                "case_count": len(cases), "stability_repeats": stability_repeats,
                "excluded_duplicate_inputs": duplicates, "cap_micro_usd": CAP_MICRO_USD,
                "worst_case_micro_usd": worst, "code_hash": code_hash, "fresh_status": "not_run",
                "pricing_verified_at": "2026-10-11", "prices_usd_per_million": {
                    "uncached_input": INPUT_USD_PER_M, "cache_write_5m_bound": CACHE_WRITE_USD_PER_M_BOUND,
                    "output": OUTPUT_USD_PER_M},
                "pricing_sources": ["https://platform.kimi.ai/docs/guide/context-caching",
                                    "https://forum.moonshot.ai/t/kimi-k3-is-here-our-most-capable-model/480"],
                "contamination": "Retrospective asset/date blinding cannot guarantee absence of model training knowledge."}
    manifest["manifest_hash"] = _hash(manifest)
    state = _marked_state(state_dir, create=True)
    with StateLock(state):
        if os.path.exists(_artifact_path(state, "manifest.json")):
            old = _manifest(state)
            if old != manifest:
                raise ExperimentError("registered experiment cannot be replaced or reset")
            return old
        if os.path.exists(_artifact_path(state, "ledger.json")):
            raise ExperimentError("existing reservation ledger cannot be reset")
        _write(state, "manifest.json", manifest)
        _write(state, "ledger.json", {"purpose": PURPOSE, "manifest_hash": manifest["manifest_hash"],
                                      "mode": "prepared", "reserved_micro_usd": 0, "records": [], "halted": False})
    return manifest


def _redact(value, secrets=()):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v, secrets) for k, v in value.items()}
    return value


def validate_completion(response, request):
    """Reject partial/tool/refusal replies; parse only final content, retain bounded reasoning."""
    if not isinstance(response, dict) or response.get("model") != MODEL:
        raise ExperimentError("response model mismatch")
    if len(_json(response).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ExperimentError("model response exceeds storage bound")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or choices[0].get("finish_reason") != "stop":
        raise ExperimentError("malformed or truncated model response")
    message = choices[0].get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("tool_calls") or message.get("refusal"):
        raise ExperimentError("tool calls or refusals are not forecast replies")
    if set(message) - {"role", "content", "reasoning_content", "tool_calls", "refusal"}:
        raise ExperimentError("unexpected assistant response fields")
    if not isinstance(message.get("content"), str) or not isinstance(message.get("reasoning_content", ""), str):
        raise ExperimentError("assistant text fields required")
    try:
        answer = json.loads(message["content"])
    except (ValueError, RecursionError):
        raise ExperimentError("invalid forecast JSON") from None
    if not isinstance(answer, dict) or set(answer) != set(REPLY_SCHEMA["required"]):
        raise ExperimentError("forecast schema mismatch")
    if type(answer["forecast_horizon_hours"]) is not int or answer["forecast_horizon_hours"] != 24:
        raise ExperimentError("forecast horizon mismatch")
    if not isinstance(answer["probabilities"], dict) or set(answer["probabilities"]) != {"down", "flat", "up"}:
        raise ExperimentError("forecast probability object required")
    _probabilities(answer["probabilities"])
    weight = answer["target_weight"]
    if weight is not None and (_number(weight, 1) < 0):
        raise ExperimentError("invalid hypothetical spot weight")
    if answer["decision"] not in (None, "long", "cash", "hold"):
        raise ExperimentError("invalid hypothetical decision")
    if not isinstance(answer["rationale"], str) or len(answer["rationale"]) > 2400:
        raise ExperimentError("bounded rationale required")
    body = json.loads(request["payload"]["messages"][1]["content"])
    known = {row["evidence_id"] for row in body["features"]}
    known.update(row["evidence_id"] for row in body.get("history", []))
    cited = answer["evidence_ids"]
    if not isinstance(cited, list) or len(cited) > 128 or any(not isinstance(v, str) or v not in known for v in cited):
        raise ExperimentError("reply cites unknown evidence")
    usage = response.get("usage")
    billing, safe_usage = None, None
    if isinstance(usage, dict):
        names = ("prompt_tokens", "completion_tokens", "total_tokens")
        if all(type(usage.get(k)) is int and usage[k] >= 0 for k in names):
            safe_usage = {k: usage[k] for k in names}
            if usage["prompt_tokens"] > request["input_token_upper_bound"] or usage["completion_tokens"] > OUTPUT_LIMIT:
                raise ExperimentError("reported usage exceeds reserved bound")
            # Conservative observed estimate includes an all-input cache write; no discount.
            billing = usage["prompt_tokens"] * (INPUT_USD_PER_M + CACHE_WRITE_USD_PER_M_BOUND) + usage["completion_tokens"] * OUTPUT_USD_PER_M
    return {"answer": _redact(answer), "assistant_message": _redact(message), "usage": safe_usage,
            "billed_micro_usd": billing, "billing_kind": "conservative_usage_estimate" if billing is not None else "unknown",
            "finish_reason": "stop", "model": MODEL}


def _ledger(state, manifest):
    ledger = _read(state, "ledger.json")
    if not isinstance(ledger, dict) or ledger.get("purpose") != PURPOSE or ledger.get("manifest_hash") != manifest["manifest_hash"]:
        raise ExperimentError("reservation ledger identity mismatch")
    if ledger.get("mode") not in ("prepared", "fixture", "owner") or not isinstance(ledger.get("records"), list):
        raise ExperimentError("invalid experiment reservation ledger")
    calls = {row["call_id"]: row for row in manifest["requests"]}
    seen, total = set(), 0
    for record in ledger["records"]:
        identity = record.get("call_id")
        if identity not in calls or identity in seen:
            raise ExperimentError("duplicate or unregistered reservation")
        seen.add(identity)
        request = calls[identity]
        if record.get("request_hash") != request["request_hash"] or record.get("reserve_micro_usd") != request["reserve_micro_usd"]:
            raise ExperimentError("cached response request binding mismatch")
        if record.get("status") not in ("reserved", "success", "unknown", "invalid"):
            raise ExperimentError("invalid request reservation status")
        total += request["reserve_micro_usd"]
        if record["status"] == "success":
            cached = record.get("completion")
            if not isinstance(cached, dict):
                raise ExperimentError("missing cached completion")
            validated = validate_completion({"model": MODEL, "choices": [{"finish_reason": cached["finish_reason"],
                                                                         "message": cached["assistant_message"]}],
                                             "usage": cached["usage"]}, request)
            if validated != cached:
                raise ExperimentError("cached completion validation mismatch")
    if total != ledger.get("reserved_micro_usd") or total > CAP_MICRO_USD:
        raise ExperimentError("cumulative reservation ledger mismatch")
    return ledger


def _summary(manifest, ledger, cached=0):
    successful = sum(r["status"] == "success" for r in ledger["records"])
    return {"purpose": PURPOSE, "mode": ledger["mode"], "fresh_status": "not_run" if ledger["mode"] != "owner" or not successful else
            ("completed" if successful == len(manifest["requests"]) else "partial"),
            "successful_calls": successful, "attempted_calls": len(ledger["records"]),
            "registered_calls": len(manifest["requests"]),
            "cached_calls": cached, "reserved_micro_usd": ledger["reserved_micro_usd"],
            "reserved_usd": ledger["reserved_micro_usd"] / 1e6, "cap_usd": 10, "halted": ledger["halted"]}


def _owner_transport(credential_file=None, use_kimi_proxy=False):
    """Only execute_experiment's explicit owner path calls this credential-using function."""
    import urllib.parse
    import urllib.request
    if credential_file:
        try:
            with open(credential_file, "rb") as stream:
                raw = stream.read(4097)
            key = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            raise ExperimentError("owner credential file could not be read") from None
    else:
        key = os.environ.get("KIMI_API_KEY", "").strip()
    if not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise ExperimentError("owner must supply an existing KIMI_API_KEY or private one-line credential file")
    proxies = {}
    if use_kimi_proxy:
        value = os.environ.get("KIMI_HTTPS_PROXY", "")
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ExperimentError("explicit owner KIMI_HTTPS_PROXY must be a credential-free HTTP(S) proxy URL")
        proxies["https"] = value
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ExperimentError("model endpoint redirect refused")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), NoRedirect())
    def call(payload):
        request = urllib.request.Request(ENDPOINT, data=_json(payload).encode("utf-8"), method="POST",
                                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with opener.open(request, timeout=180) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ExperimentError("model response exceeds storage bound")
        return _redact(json.loads(raw.decode("utf-8")), (key,))
    return call


def _owner_budget_directory():
    # Resolve the owner profile only in the explicit paid path, never in prepare/score.
    from pathlib import Path
    return str(Path.home()/'.bitpin-kimi-evidence-grant-20261011')


class _OwnerGrant:
    """One durable authorization and process lock across all paid pilot directories."""
    grant_id='evidence-strategy-20261011-usd10'

    def __init__(self,local_ledger):
        self.local=local_ledger
        self.state=_safe_path(_owner_budget_directory())
        self.lock=None

    def __enter__(self):
        os.makedirs(self.state,mode=0o700,exist_ok=True)
        marker=_artifact_path(self.state,'.owner-grant')
        if not os.path.exists(marker):
            if os.listdir(self.state):
                raise ExperimentError('owner grant directory is unmarked')
            try:
                with open(marker,'x',encoding='utf-8') as handle:
                    handle.write(self.grant_id+'\n');handle.flush();os.fsync(handle.fileno())
            except FileExistsError:
                pass
        with open(marker,encoding='utf-8') as handle:
            if handle.read(128)!=self.grant_id+'\n':
                raise ExperimentError('owner grant marker mismatch')
        self.lock=StateLock(self.state)
        self.lock.__enter__()
        try:
            budget_path=_artifact_path(self.state,'owner-budget.json')
            if not os.path.exists(budget_path):
                if self.local['records']:
                    raise ExperimentError('existing paid state cannot reset missing owner grant')
                _write(self.state,'owner-budget.json',{'grant_id':self.grant_id,'cap_micro_usd':CAP_MICRO_USD,
                                                      'reserved_micro_usd':0,'records':[]})
            self.budget=_read(self.state,'owner-budget.json')
            if not isinstance(self.budget,dict) or self.budget.get('grant_id')!=self.grant_id or self.budget.get('cap_micro_usd')!=CAP_MICRO_USD:
                raise ExperimentError('owner authorization identity mismatch')
            records=self.budget.get('records')
            if not isinstance(records,list): raise ExperimentError('owner reservation records invalid')
            total,keys=0,set()
            for record in records:
                key=record.get('key');reserve=record.get('reserve_micro_usd')
                if not isinstance(key,str) or len(key)!=64 or key in keys or type(reserve) is not int or reserve<=0:
                    raise ExperimentError('owner reservation identity invalid')
                keys.add(key);total+=reserve
            if total!=self.budget.get('reserved_micro_usd') or total>CAP_MICRO_USD:
                raise ExperimentError('owner cumulative reservation mismatch')
            self.keys=keys
            # Every older local charge must still exist in the common grant.
            for record in self.local['records']:
                if record.get('owner_grant_key') not in self.keys:
                    raise ExperimentError('paid state is not bound to retained owner grant')
            return self
        except BaseException:
            self.lock.__exit__();self.lock=None
            raise

    def __exit__(self,*args):
        if self.lock: self.lock.__exit__(*args)

    def key(self,request):
        return _hash([request['request_hash'],request['repeat']])

    def preflight(self,requests):
        if any(self.key(r) in self.keys for r in requests):
            raise ExperimentError('request already reserved in another paid state; use original cache, never retry')
        if self.budget['reserved_micro_usd']+sum(r['reserve_micro_usd'] for r in requests)>CAP_MICRO_USD:
            raise ExperimentError('shared owner USD 10 cumulative ceiling exceeded')

    def reserve(self,request):
        self.preflight([request])
        key=self.key(request)
        self.budget['records'].append({'key':key,'request_hash':request['request_hash'],
                                      'reserve_micro_usd':request['reserve_micro_usd'],'reserved_at':time.time()})
        self.budget['reserved_micro_usd']+=request['reserve_micro_usd']
        _write(self.state,'owner-budget.json',self.budget)
        self.keys.add(key)
        return key


def execute_experiment(state_dir, owner_execute=False, credential_file=None, transport=None, max_calls=None, use_kimi_proxy=False):
    """One request per registered call, no retry. Injected transports are offline fixtures.

    Unknown/malformed calls permanently halt this experiment and retain their full
    reservation. Normal resumes use cached successful calls without charging again.
    """
    if transport is None and owner_execute is not True:
        raise ExperimentError("fresh calls require explicit --owner-execute")
    if max_calls is not None and (type(max_calls) is not int or not 1 <= max_calls <= 36):
        raise ExperimentError("bounded positive max_calls required")
    state = _marked_state(state_dir)
    with StateLock(state):
        manifest = _manifest(state)
        ledger = _ledger(state, manifest)
        mode = "fixture" if transport is not None else "owner"
        if ledger["mode"] not in ("prepared", mode):
            raise ExperimentError("fixture and fresh owner results cannot share a ledger")
        if ledger["halted"] or any(r["status"] != "success" for r in ledger["records"]):
            raise ExperimentError("experiment halted after unknown or invalid response; reservations retained, retries disabled")
        ledger["mode"] = mode
        registered = {r["call_id"] for r in ledger["records"]}
        pending = [r for r in manifest["requests"] if r["call_id"] not in registered]
        if ledger["reserved_micro_usd"] + sum(r["reserve_micro_usd"] for r in pending) > CAP_MICRO_USD:
            raise ExperimentError("remaining experiment exceeds cumulative USD 10 ceiling")
        with contextlib.ExitStack() as stack:
            grant=stack.enter_context(_OwnerGrant(ledger)) if mode=='owner' and pending else None
            if grant:
                grant.preflight(pending[:max_calls] if max_calls is not None else pending)
            if transport is None and pending:
                transport = _owner_transport(credential_file, use_kimi_proxy)
            return _execute_pending(state,manifest,ledger,pending,transport,max_calls,grant,len(registered))


def _execute_pending(state,manifest,ledger,pending,transport,max_calls,grant,cached):
        completed=0
        for request in pending:
            if max_calls is not None and completed >= max_calls:
                break
            record = {"call_id": request["call_id"], "request_hash": request["request_hash"],
                      "reserve_micro_usd": request["reserve_micro_usd"], "status": "reserved",
                      "reserved_at": time.time(), "billed_micro_usd": None}
            if grant:
                record['owner_grant_key']=grant.reserve(request)  # Shared durable charge before local write/HTTP.
            ledger["reserved_micro_usd"] += request["reserve_micro_usd"]
            ledger["records"].append(record)
            _write(state, "ledger.json", ledger)  # Durable before the only HTTP invocation.
            try:
                response = transport(copy.deepcopy(request["payload"]))
            except Exception:
                record["status"] = "unknown"
                record["error_kind"] = "transport_failure"
                ledger["halted"] = True
                _write(state, "ledger.json", ledger)
                break
            try:
                record["completion"] = validate_completion(response, request)
                record["billed_micro_usd"] = record["completion"]["billed_micro_usd"]
                record["status"] = "success"
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                record["status"] = "invalid"
                record["error_kind"] = "invalid_or_truncated_response"
                ledger["halted"] = True
            record["finished_at"] = time.time()
            _write(state, "ledger.json", ledger)
            completed += 1
            if ledger["halted"]:
                break
        result=_summary(manifest,ledger,cached=cached)
        if grant: result['owner_reserved_usd']=grant.budget['reserved_micro_usd']/1e6
        return result


def score_experiment(state_dir, episodes, now=None):
    """Free paired accuracy/Brier/log loss, excluding stability repeats as market samples."""
    state = _marked_state(state_dir)
    with StateLock(state):
        manifest = _manifest(state)
        ledger = _ledger(state, manifest)
    if not isinstance(episodes, list) or any(not isinstance(e, dict) for e in episodes):
        raise ExperimentError("offline episode list required for scoring")
    by_id = {e["episode_id"]: e for e in episodes}
    if len(by_id) != len(episodes):
        raise ExperimentError("duplicate scoring episode identity")
    replies = {r["call_id"]: r["completion"]["answer"] for r in ledger["records"] if r["status"] == "success"}
    rows = {arm: [] for arm in ARMS}
    paired, days, differences, excluded = 0, set(), [], 0
    calendar_differences = {}
    cutoff = time.time() if now is None else _number(now, 1e12)
    for case in manifest["cases"]:
        episode = by_id.get(case["episode_id"])
        if episode is None:
            excluded += 1
            continue
        identity = _hash({k: episode.get(k) for k in IDENTITY_FIELDS})
        if identity != case["evaluation_identity_hash"] or _hash(_input(episode)) != case["input_hash"]:
            raise ExperimentError("scoring episode does not match registered model input")
        label = episode.get("label")
        if type(label) is not int or label not in (0, 1, 2) or episode["label_end"] > cutoff:
            excluded += 1
            continue
        if episode.get("realized_return") is None:
            excluded += 1
            continue
        realized = _number(episode["realized_return"])
        band = case["input"]["cost_band"]
        if label != (0 if realized < -band else 2 if realized > band else 1):
            raise ExperimentError("outcome label disagrees with declared return target")
        daily = {}
        for arm in ARMS:
            answer = replies.get("%s:%s:0" % (case["case_id"], arm))
            if answer is None:
                continue
            p = _probabilities(answer["probabilities"])
            score = {"brier": sum((v - (1 if i == label else 0)) ** 2 for i, v in enumerate(p)),
                     "log_loss": -math.log(max(1e-15, p[label])),
                     "accuracy": int(max(range(3), key=lambda i: p[i]) == label)}
            rows[arm].append(score)
            daily[arm] = score
        if len(daily) == 2:
            paired += 1
            days.add(int(episode["decision_time"] // 86400))
            difference = daily["structured_packet"]["brier"] - daily["raw_history"]["brier"]
            differences.append(difference)
            calendar_differences.setdefault(int(episode["decision_time"] // 86400), []).append(difference)
    arms = {arm: dict({"cases": len(values)}, **{key: sum(v[key] for v in values) / len(values) if values else None
                                                for key in ("accuracy", "brier", "log_loss")})
            for arm, values in rows.items()}
    status = "not_run" if not replies else "fixture_only" if ledger["mode"] == "fixture" else "inconclusive"
    stability = {}
    for arm in ARMS:
        distances = []
        primary = replies.get("%s:%s:0" % (manifest["cases"][0]["case_id"], arm))
        for repeat in range(1, manifest["stability_repeats"] + 1):
            repeated = replies.get("%s:%s:%d" % (manifest["cases"][0]["case_id"], arm, repeat))
            if primary is not None and repeated is not None:
                a, b = _probabilities(primary["probabilities"]), _probabilities(repeated["probabilities"])
                distances.append(sum(abs(x - y) for x, y in zip(a, b)))
        stability[arm] = {"repeat_calls": len(distances),
                          "mean_probability_l1": sum(distances) / len(distances) if distances else None,
                          "max_probability_l1": max(distances) if distances else None}
    return {"status": status, "fresh_status": _summary(manifest, ledger)["fresh_status"],
            "registered_cases": manifest["case_count"], "paired_cases": paired, "calendar_days": len(days),
            "excluded_outcomes": excluded, "arms": arms,
            "coverage": {arm: len(rows[arm]) / manifest["case_count"] for arm in ARMS},
            "paired_difference": {"brier_structured_minus_raw": sum(differences) / len(differences) if differences else None,
                                  "interval": None, "interval_status": "small_prespecified_pilot_not_powered"},
            "stability_repeat_calls": sum(":0" != r["call_id"][-2:] and r["status"] == "success" for r in ledger["records"]),
            "stability": stability,
            "paired_daily_differences": [{"decision_day": day, "cases": len(values),
                                          "brier_structured_minus_raw": sum(values) / len(values)}
                                         for day, values in sorted(calendar_differences.items())],
            "reserved_usd": ledger["reserved_micro_usd"] / 1e6,
            "contamination": manifest["contamination"],
            "promotion_status": "research_only", "live_allocator": "unchanged"}
