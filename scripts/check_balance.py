"""Read-only Bitpin balance check. Places NO orders and moves NO money.

Usage (from the project root, any Python 3.7+ - stdlib only):
    python3 scripts/check_balance.py /path/to/api.txt          (Linux / the server)
    py scripts/check_balance.py api.txt                           (Windows)

Key file: a 2-line text file (API key and secret key, one per line, in either order - both orders are
tried) or JSON {"api_key": "...", "secret_key": "..."}. Invisible characters (BOM, zero-width spaces,
NBSP) and surrounding quotes are stripped, because keys pasted from a chat or a PDF often carry them.

What it prints: the shape of each key (length, symbol classes; never the key itself), the wallet rows
with a non-zero balance and their value in toman at the current COIN_IRT ticker, and the total of the
assets that have a known toman price. Bitpin is reached DIRECTLY (proxy variables ignored, redirects
never followed): the API key is IP-whitelisted to this machine. Each run uses 1-2 of the 200 daily
authentications, so it is a diagnostic, not something to loop. The bot itself never calls this file;
`sudo bitpin-bot status` reads the same balances through the running service.
"""
import json
import sys
import urllib.error
import urllib.request
from decimal import Decimal

BASE = "https://api.bitpin.org"
# Bitpin is always reached DIRECTLY: http_proxy / https_proxy / ALL_PROXY from the environment are
# ignored (the API key is IP-whitelisted to this machine's own IP), and redirects are not followed.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "bitpin-balance-check/1.0")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _OPENER.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode(), parse_float=Decimal)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"detail": raw[:200]}


INVISIBLE = "﻿​‌‍⁠\xa0"


def clean(s):
    s = s.strip().strip(INVISIBLE).strip().strip("'\"").strip()
    return "".join(ch for ch in s if ch not in INVISIBLE)


def describe(s):
    """Shape of a key without revealing it."""
    kinds = []
    if any(c.isspace() for c in s):
        kinds.append("CONTAINS SPACES")
    if any(ord(c) > 127 for c in s):
        kinds.append("CONTAINS NON-ASCII")
    extra = sorted(set(c for c in s if not c.isalnum()))
    return "%d chars%s%s" % (len(s), ", symbols: " + " ".join(extra) if extra else ", letters/digits only",
                            (", " + ", ".join(kinds)) if kinds else "")


def read_keys(path):
    text = open(path, encoding="utf-8-sig").read().strip()
    if text.startswith("{"):
        d = json.loads(text)
        pairs = [(clean(d["api_key"]), clean(d["secret_key"]))]
    else:
        lines = [clean(x) for x in text.splitlines() if clean(x)]
        if len(lines) != 2:
            sys.exit("key file must have exactly 2 non-empty lines (API key and secret key); found %d" % len(lines))
        pairs = [(lines[0], lines[1]), (lines[1], lines[0])]  # try both orders
    print("Key file: line 1 = %s; line 2 = %s" % (describe(pairs[0][0]), describe(pairs[0][1])))
    return pairs


def authenticate(pairs):
    last = None
    for i, (k, s) in enumerate(pairs):
        status, body = call("POST", "/api/v1/usr/authenticate/", {"api_key": k, "secret_key": s})
        if status == 200 and "access" in body:
            print("Authenticated (key order: %s)." % ("line 1 = API key" if i == 0 else "line 2 = API key"))
            return body["access"]
        last = (status, body)
    status, body = last
    print("Authentication failed: HTTP %s %s" % (status, body.get("code", "")))
    print(body.get("detail", ""))
    print("Check that the keys are correct and that THIS computer's public IP is in the key's IP whitelist on bitpin.")
    sys.exit(1)


def wallets(token):
    out, offset = [], None
    while True:
        q = "/api/v1/wlt/wallets/?limit=200" + ("&offset=%d" % offset if offset else "")
        status, body = call("GET", q, token=token)
        if status != 200:
            sys.exit("wallets request failed: HTTP %s %s" % (status, body))
        rows = body if isinstance(body, list) else body.get("results", [])
        out += rows
        if len(rows) < 200:
            return out
        offset = min(int(r["id"]) for r in rows)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    token = authenticate(read_keys(sys.argv[1]))
    rows = wallets(token)
    status, tickers = call("GET", "/api/v1/mkt/tickers/")
    price = {}
    if status == 200:
        for t in tickers:  # some inactive markets report "price": null
            try:
                p = Decimal(str(t.get("price")))
            except ArithmeticError:
                continue
            if p.is_finite() and p > 0:
                price[t["symbol"]] = p
    total = Decimal(0)
    print()
    print("%-10s %-8s %22s %22s %22s" % ("asset", "service", "available", "frozen", "value (IRT)"))
    for r in sorted(rows, key=lambda r: r["asset"]):
        bal, frz = Decimal(str(r["balance"])), Decimal(str(r["frozen"]))
        if bal == 0 and frz == 0:
            continue
        a = r["asset"]
        if a == "IRT":
            v = bal + frz
        elif a + "_IRT" in price:
            v = (bal + frz) * price[a + "_IRT"]
        else:
            v = None
        if v is not None:
            total += v
        print("%-10s %-8s %22s %22s %22s" % (a, r.get("service", ""), bal, frz, "%.0f" % v if v is not None else "?"))
    print()
    print("Estimated total value: %s IRT (toman), assets with a known IRT price only." % format(int(total), ","))
    if any(r["asset"] == "RIAL" for r in rows):
        print("Note: an asset named RIAL exists; its unit (rial vs toman) is unverified, so it is not counted in the total.")


if __name__ == "__main__":
    main()
