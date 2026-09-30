"""Check the Kimi (Moonshot) API key from this server. Read-only: no trading, no orders.

Usage:
  python3 kimi_check.py                      list the models this key can use
  python3 kimi_check.py --chat MODEL_ID      also send one tiny JSON test message (costs a few tokens)

The key comes ONLY from the environment variable KIMI_API_KEY, and this script never writes it
anywhere: on the server it lives in /etc/bitpin-bot/bitpin-bot.env and nowhere else. This is a
throwaway research tool; the supported command on the server is:
    sudo bitpin-bot kimi-check --news
Only Moonshot traffic uses the proxy (default http://127.0.0.1:1081); the key is never printed.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_key():
    """The key from the environment ONLY. This script must never create a second place where the
    key is stored: the deploy kit says it lives in /etc/bitpin-bot/bitpin-bot.env and nowhere
    else, and a forgotten cleartext copy would travel into every /opt/bitpin-bot.bak-* copy."""
    k = os.environ.get("KIMI_API_KEY", "").strip()
    if not k:
        sys.exit("KIMI_API_KEY is not set. This tool never asks for - or stores - the key.\n"
                 "On the server use the supported command, which reads the key from "
                 "/etc/bitpin-bot/bitpin-bot.env:\n"
                 "  sudo bitpin-bot kimi-check --news\n"
                 "Elsewhere, export it for this shell only:  export KIMI_API_KEY=...")
    return k, "env KIMI_API_KEY"


def request(opener, method, url, key, body=None, timeout=60, attempts=3):
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 - network/proxy drop: retry
            last = "%s: %s" % (type(e).__name__, e)
            print("  attempt %d/%d failed (%s)" % (i + 1, attempts, last))
            if i < attempts - 1:
                time.sleep(3 * (i + 1))
    return None, last + " -- the proxy/tunnel dropped the connection %d times" % attempts


def diagnose(status, text):
    if status == 401:
        return "401: the key is invalid or revoked (or it belongs to the other platform: .ai vs .cn)."
    if status in (403, 451):
        return "%s: blocked. Is the proxy running and set correctly? (%s)" % (status, text[:120].replace("\n", " "))
    if status == 429:
        return "429: rate limit or no balance/quota on the Moonshot account."
    if status is None:
        return "connection failed: " + text
    return "HTTP %s: %s" % (status, text[:300].replace("\n", " "))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=os.environ.get("KIMI_HTTPS_PROXY", "http://127.0.0.1:1081"),
                    help='proxy for Moonshot only; "" to connect directly')
    ap.add_argument("--base", default="https://api.moonshot.ai/v1")
    ap.add_argument("--chat", metavar="MODEL_ID")
    ap.add_argument("--search", metavar="MODEL_ID", help="test the builtin $web_search tool with this model")
    a = ap.parse_args()
    key, source = load_key()
    proxies = {"http": a.proxy, "https": a.proxy} if a.proxy else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    print("key source: %s (length %d, ends ...%s)" % (source, len(key), key[-4:]))
    print("endpoint:   %s   proxy: %s" % (a.base, a.proxy or "none (direct)"))

    status, text = request(opener, "GET", a.base.rstrip("/") + "/models", key)
    if status != 200:
        sys.exit("MODELS FAILED -> " + diagnose(status, text))
    ids = sorted(m.get("id", "?") for m in json.loads(text).get("data", []))
    print("\nOK - the key works. Models available to this key (%d):" % len(ids))
    for i in ids:
        print("  " + i)

    if a.search:
        # Moonshot builtin web search: when the model asks for $web_search, echo the arguments back.
        msgs = [{"role": "system", "content": "Use web search when needed. Reply only with JSON."},
                {"role": "user", "content": 'Search the web for the current Bitcoin price in USD and the latest '
                 'US dollar free-market rate in Tehran. Return JSON {"btc_usd": number, "usd_irr_tehran": '
                 'number, "sources": [urls]}'}]
        base_msgs = list(msgs)

        # Each variant: (label, model, extra body fields, how to echo the assistant turn, how to fill the tool msg)
        other = "kimi-k2.6" if a.search != "kimi-k2.6" else "kimi-k3"
        variants = [
            ("thinking disabled", a.search, {"thinking": {"type": "disabled"}}, "clean", "args"),
            ("empty reasoning_content", a.search, {}, "empty_reasoning", "args"),
            ("tool content re-serialised", a.search, {}, "clean", "json"),
            ("tool msg without name", a.search, {}, "clean", "args_noname"),
            ("thinking enabled explicitly", a.search, {"thinking": {"type": "enabled"}}, "clean", "args"),
            ("other model " + other, other, {}, "clean", "args"),
            ("other model thinking disabled", other, {"thinking": {"type": "disabled"}}, "clean", "args"),
        ]

        def assistant_turn(m, how):
            out = {"role": "assistant", "content": m.get("content") or "",
                   "tool_calls": [{"id": tc["id"], "type": tc.get("type", "function"),
                                   "function": {"name": tc["function"]["name"],
                                                "arguments": tc["function"]["arguments"]}}
                                  for tc in m.get("tool_calls") or []]}
            if m.get("reasoning_content"):
                out["reasoning_content"] = m["reasoning_content"]
            elif how == "empty_reasoning":
                out["reasoning_content"] = ""
            return out

        def tool_msg(tc, how):
            content = tc["function"]["arguments"]
            if how == "json":
                content = json.dumps(json.loads(content), ensure_ascii=False)
            msg = {"role": "tool", "tool_call_id": tc["id"], "content": content}
            if how != "args_noname":
                msg["name"] = tc["function"]["name"]
            return msg

        working = None
        for label, model, extra, echo, tool_how in variants:
            msgs = list(base_msgs)
            searches, ok = 0, False
            print("\n-- web search test: %s (model %s)" % (label, model))
            for rnd in range(6):
                body = {"model": model, "max_tokens": 8000, "messages": msgs,
                        "tools": [{"type": "builtin_function", "function": {"name": "$web_search"}}]}
                body.update(extra)
                status, text = request(opener, "POST", a.base.rstrip("/") + "/chat/completions", key, body,
                                       timeout=180)
                if status != 200:
                    print("   failed in round %d -> %s" % (rnd + 1, diagnose(status, text)))
                    break
                d = json.loads(text)
                ch = d["choices"][0]
                m = ch["message"]
                if ch.get("finish_reason") != "tool_calls":
                    print("   OK: searches=%d finish=%s\n   reply=%s\n   usage=%s" % (
                        searches, ch.get("finish_reason"), (m.get("content") or "")[:600], d.get("usage")))
                    ok = searches > 0
                    if not searches:
                        print("   (answered without searching)")
                    break
                if rnd == 0:
                    tcs = m.get("tool_calls") or []
                    print("   round 1: %d tool call(s); tool_call type=%s; argument chars=%s" % (
                        len(tcs), [tc.get("type") for tc in tcs], [len(tc["function"]["arguments"]) for tc in tcs]))
                msgs.append(assistant_turn(m, echo))
                for tc in m.get("tool_calls") or []:
                    searches += 1
                    msgs.append(tool_msg(tc, tool_how))
            if ok:
                working = (label, model)
                break
        print("\nWORKING: %s" % (("%s with model %s" % working) if working else "none - web search unavailable"))

    if a.chat:
        body = {"model": a.chat, "max_tokens": 50, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": "Reply only with JSON."},
                             {"role": "user", "content": 'Return exactly {"ok": true}'}]}
        status, text = request(opener, "POST", a.base.rstrip("/") + "/chat/completions", key, body, timeout=120)
        if status != 200:
            sys.exit("CHAT FAILED -> " + diagnose(status, text))
        d = json.loads(text)
        msg = d["choices"][0]["message"].get("content")
        print("\nchat test with %s: reply=%r  usage=%s" % (a.chat, msg, d.get("usage")))


if __name__ == "__main__":
    main()
