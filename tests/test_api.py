"""BitpinClient tests with a fake transport (no network)."""
import base64
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import api  # noqa: E402
from bitpin.api import (AuthBudget, AuthBudgetExceeded, AuthError, BitpinAPIError, BitpinClient,  # noqa: E402
                        OrderNotSent, OrderStatusUnknown, TransportError, jwt_exp, throttle_wait)

logging.getLogger("bitpin").addHandler(logging.NullHandler())  # keep test output quiet

API_KEY = "APIKEY-1234567890-abcdef"
SECRET = "SECRET-0987654321-zyxwvu"
NOW = 1790000000.0


def make_jwt(exp, tag="x", iat=None):
    def enc(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    claims = {"exp": exp, "jti": tag, "token_type": tag}
    if iat is not None:
        claims["iat"] = iat
    return "%s.%s.%s" % (enc({"alg": "HS256", "typ": "JWT"}), enc(claims), "c2ln")


class Clock:
    def __init__(self, t=NOW):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeTransport:
    """Routes (method, path) to a queue of responses. The last response repeats.
    A response is (status, obj), an Exception to raise, or a callable(call) -> (status, obj)."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, method, path, *responses):
        self.routes[(method, path)] = list(responses)
        return self

    def __call__(self, method, url, headers, body, timeout):
        u = urllib.parse.urlparse(url)
        call = {"method": method, "path": u.path, "query": dict(urllib.parse.parse_qsl(u.query)),
                "headers": dict(headers), "body": json.loads(body.decode()) if body else None, "raw_body": body}
        self.calls.append(call)
        q = self.routes.get((method, u.path))
        if not q:
            raise AssertionError("unexpected call %s %s" % (method, u.path))
        r = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(r, Exception):
            raise r
        if callable(r):
            r = r(call)
        status, obj = r
        return status, (json.dumps(obj).encode() if obj is not None else b"")

    def count(self, method, path):
        return sum(1 for c in self.calls if c["method"] == method and c["path"] == path)


AUTH = "/api/v1/usr/authenticate/"
REFRESH = "/api/v1/usr/refresh_token/"
WALLETS = "/api/v1/wlt/wallets/"
ORDERS = "/api/v1/odr/orders/"


def auth_ok(clock, access_ttl=900):
    def r(call):
        return 200, {"access": make_jwt(int(clock.t + access_ttl), "access"),
                     "refresh": make_jwt(int(clock.t + 30 * 86400), "refresh")}
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_api_test_")
        self.clock = Clock()
        self.t = FakeTransport()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def client(self, **kw):
        kw.setdefault("api_key", API_KEY)
        kw.setdefault("secret_key", SECRET)
        return BitpinClient(transport=self.t, state_dir=self.dir, clock=self.clock, sleep=self.clock.sleep,
                            order_lookup_delay=1.0, **kw)


class HelpersTest(unittest.TestCase):
    def test_jwt_exp(self):
        self.assertEqual(jwt_exp(make_jwt(1234567890)), 1234567890)
        self.assertIsNone(jwt_exp("not-a-jwt"))

    def test_throttle_wait_parses_hint_and_caps(self):
        p = {"detail": "Request was throttled. Expected available in 7 seconds."}
        self.assertEqual(throttle_wait(p, 1, 60), 7.5)
        self.assertEqual(throttle_wait({"detail": "Expected available in 3600 seconds."}, 1, 60), 60)
        self.assertEqual(throttle_wait({"detail": "slow down"}, 4, 60), 4)
        self.assertEqual(throttle_wait(None, 2, 60), 2)

    def test_https_required(self):
        with self.assertRaises(ValueError):
            BitpinClient("http://api.bitpin.org")

    def test_no_money_movement_endpoints(self):
        for path in ("/api/v1/wlt/withdraws/", "/api/v1/wlt/withdraw/", "/api/v1/wlt/deposits/",
                     "/api/v1/wlt/addresses/", "/api/v1/wlt/transfer/", "/api/v1/wlt/wallets/1/"):
            for m in ("GET", "POST", "PUT", "DELETE"):
                with self.assertRaises(ValueError):
                    BitpinClient._check_allowed(m, path)
        bad = [n for n in dir(BitpinClient) if any(w in n.lower() for w in ("withdraw", "deposit", "transfer", "address"))]
        self.assertEqual(bad, [])


class AuthTest(Base):
    def test_lazy_auth_caches_tokens_not_keys(self):
        self.t.add("POST", AUTH, auth_ok(self.clock)).add("GET", WALLETS, (200, []))
        c = self.client()
        self.assertEqual(self.t.calls, [])  # lazy: nothing until a private call
        c.wallets()
        self.assertEqual(self.t.count("POST", AUTH), 1)
        w = [x for x in self.t.calls if x["path"] == WALLETS][0]
        self.assertTrue(w["headers"]["Authorization"].startswith("Bearer "))
        with open(os.path.join(self.dir, api.TOKEN_FILE)) as f:
            text = f.read()
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(SECRET, text)
        self.assertIn("access", json.loads(text))
        # a new process reuses the cached token: no second authenticate
        c2 = self.client()
        c2.wallets()
        self.assertEqual(self.t.count("POST", AUTH), 1)

    def test_cached_token_not_reused_with_other_keys(self):
        self.t.add("POST", AUTH, auth_ok(self.clock)).add("GET", WALLETS, (200, []))
        self.client().wallets()
        self.client(api_key="OTHERKEY-123456").wallets()
        self.assertEqual(self.t.count("POST", AUTH), 2)

    def test_refresh_before_expiry(self):
        self.t.add("POST", AUTH, auth_ok(self.clock, access_ttl=900))
        self.t.add("POST", REFRESH, lambda call: (200, {"access": make_jwt(int(self.clock.t + 900), "access2")}))
        self.t.add("GET", WALLETS, (200, []))
        c = self.client()
        c.wallets()
        self.clock.t += 900 - 30  # 30 s before expiry: inside the 60 s margin
        c.wallets()
        self.assertEqual(self.t.count("POST", AUTH), 1)
        self.assertEqual(self.t.count("POST", REFRESH), 1)
        self.assertEqual(c.auth_budget.used(), 2)

    def test_refresh_failure_reauthenticates(self):
        self.t.add("POST", AUTH, auth_ok(self.clock))
        self.t.add("POST", REFRESH, (401, {"detail": "Token is invalid or expired", "code": "token_not_valid"}))
        self.t.add("GET", WALLETS, (200, []))
        c = self.client()
        c.wallets()
        self.clock.t += 1000
        c.wallets()
        self.assertEqual(self.t.count("POST", REFRESH), 1)
        self.assertEqual(self.t.count("POST", AUTH), 2)

    def test_401_on_private_call_refreshes_and_retries_once(self):
        self.t.add("POST", AUTH, auth_ok(self.clock))
        self.t.add("POST", REFRESH, lambda call: (200, {"access": make_jwt(int(self.clock.t + 900), "a2")}))
        self.t.add("GET", WALLETS, (401, {"detail": "Given token not valid", "code": "token_not_valid"}),
                   (200, [{"asset": "IRT", "balance": "5", "frozen": "0", "service": "main"}]))
        rows = self.client().wallets()
        self.assertEqual(rows[0]["asset"], "IRT")
        self.assertEqual(self.t.count("GET", WALLETS), 2)
        self.assertEqual(self.t.count("POST", REFRESH), 1)

    def test_auth_budget_hard_stop_makes_no_call(self):
        api.atomic_write_json(os.path.join(self.dir, api.AUTH_BUDGET_FILE),
                              {"calls": [self.clock.t - 10 * i for i in range(150)]})
        self.t.add("POST", AUTH, auth_ok(self.clock)).add("GET", WALLETS, (200, []))
        with self.assertRaises(AuthBudgetExceeded):
            self.client().wallets()
        self.assertEqual(self.t.calls, [])

    def test_auth_budget_rolls_over_after_24h(self):
        path = os.path.join(self.dir, api.AUTH_BUDGET_FILE)
        api.atomic_write_json(path, {"calls": [self.clock.t - 86400 - i for i in range(150)]})
        b = AuthBudget(path, clock=self.clock)
        self.assertEqual(b.used(), 0)
        b.consume("authenticate")
        self.assertEqual(AuthBudget(path, clock=self.clock).used(), 1)  # persisted

    def test_wrong_credentials_not_retried(self):
        self.t.add("POST", AUTH, (406, {"code": "api_credential_wrong", "detail": "wrong"}))
        with self.assertRaises(AuthError):
            self.client().wallets()
        self.assertEqual(self.t.count("POST", AUTH), 1)

    def test_no_credentials_for_private_call(self):
        c = BitpinClient(transport=self.t, clock=self.clock, sleep=self.clock.sleep)
        with self.assertRaises(AuthError):
            c.wallets()
        self.assertEqual(self.t.calls, [])

    # ---- review round 2: temporary auth failures are not fatal, the refresh token survives them
    def test_5xx_on_refresh_and_authenticate_is_temporary_not_fatal(self):
        from bitpin.runner import FATAL_ERRORS
        self.t.add("POST", AUTH, auth_ok(self.clock), (502, {"_raw": "<html>Bad Gateway</html>"}))
        self.t.add("POST", REFRESH, (502, {"_raw": "<html>Bad Gateway</html>"}),
                   lambda call: (200, {"access": make_jwt(int(self.clock.t + 900), "access2")}))
        self.t.add("GET", WALLETS, (200, []))
        c = self.client()
        c.wallets()
        self.clock.t += 20 * 60                        # access token expired, refresh token valid
        self.t.routes[("POST", REFRESH)] = [(502, {"_raw": "bad gateway"})]
        with self.assertRaises(BitpinAPIError) as cm:
            c.wallets()
        self.assertNotIsInstance(cm.exception, FATAL_ERRORS)
        self.assertEqual(self.t.count("POST", REFRESH), 3)     # retried inside the call
        self.assertEqual(self.t.count("POST", AUTH), 1)        # no re-authentication during an outage
        with open(os.path.join(self.dir, api.TOKEN_FILE)) as f:
            self.assertTrue(json.load(f).get("refresh"))        # refresh token kept
        self.t.routes[("POST", REFRESH)] = [lambda call: (200, {"access": make_jwt(int(self.clock.t + 900), "a3")})]
        self.clock.t += api.TokenManager.COOLDOWN_START          # after the renewal cool-down
        c.wallets()                                               # recovers with the kept refresh token
        self.assertEqual(self.t.count("POST", AUTH), 1)

    def test_auth_outage_does_not_burn_the_budget_call_by_call(self):
        from bitpin.runner import FATAL_ERRORS
        self.t.add("POST", AUTH, auth_ok(self.clock))
        self.t.add("GET", WALLETS, (200, []))
        self.t.add("GET", ORDERS + "7/", (200, order_obj("x", oid=7, state="active")))
        c = self.client()
        c.wallets()
        used = c.auth_budget.used()
        self.t.add("POST", REFRESH, (502, {"_raw": "bad gateway"}))
        # inside the refresh margin: the renewal fails, but the still-valid access token is used
        self.clock.t += 900 - 50
        for _ in range(30):                               # e.g. LiveBroker polling an order every second
            c.get_order(7)
            self.clock.t += 1
        self.assertEqual(self.t.count("POST", REFRESH), 3)   # one renewal attempt (3 tries), not 30
        self.assertEqual(self.t.count("GET", ORDERS + "7/"), 30)
        # the access token has expired (cool-down still running): calls fail fast, temporary, no auth call
        self.clock.t += 15
        self.assertLess(c._tokens._access_exp(), self.clock.t)
        for _ in range(10):
            with self.assertRaises(api.AuthUnavailable) as cm:
                c.get_order(7)
            self.assertNotIsInstance(cm.exception, FATAL_ERRORS)
        self.assertEqual(self.t.count("POST", REFRESH), 3)
        self.assertEqual(c.auth_budget.used(), used + 3)
        # after the cool-down: one new attempt; while it keeps failing the cool-down grows
        self.clock.t += api.TokenManager.COOLDOWN_START
        with self.assertRaises(BitpinAPIError):
            c.get_order(7)
        self.assertEqual(self.t.count("POST", REFRESH), 6)
        self.clock.t += api.TokenManager.COOLDOWN_START + 1
        with self.assertRaises(api.AuthUnavailable):
            c.get_order(7)                                # 2nd cool-down is 120 s
        self.assertEqual(self.t.count("POST", REFRESH), 6)
        # recovery resets the cool-down
        self.t.routes[("POST", REFRESH)] = [lambda call: (200, {"access": make_jwt(int(self.clock.t + 900), "a2")})]
        self.clock.t += 2 * api.TokenManager.COOLDOWN_START
        c.get_order(7)
        self.assertEqual(self.t.count("POST", AUTH), 1)
        self.assertEqual(c._tokens._cooldown, 0)

    def test_order_is_not_sent_while_token_renewal_cools_down(self):
        self.t.add("POST", AUTH, auth_ok(self.clock))
        self.t.add("GET", WALLETS, (200, []))
        self.t.add("POST", REFRESH, (503, None))
        c = self.client()
        c.wallets()
        self.clock.t += 1000
        with self.assertRaises(BitpinAPIError):
            c.wallets()
        with self.assertRaises(OrderNotSent):
            c.place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-cool")
        self.assertEqual(self.t.count("POST", ORDERS), 0)

    def test_throttled_authenticate_is_temporary_not_fatal(self):
        from bitpin.runner import FATAL_ERRORS
        self.t.add("POST", AUTH, (429, {"detail": "Request was throttled. Expected available in 5 seconds."}))
        with self.assertRaises(BitpinAPIError) as cm:
            self.client().wallets()
        self.assertEqual(cm.exception.status, 429)
        self.assertNotIsInstance(cm.exception, FATAL_ERRORS)
        self.t.routes[("POST", AUTH)] = [(503, None)]
        with self.assertRaises(BitpinAPIError) as cm:
            self.client().wallets()
        self.assertNotIsInstance(cm.exception, FATAL_ERRORS)

    def test_credential_rejections_are_still_fatal(self):
        for status, code in ((406, None), (403, "permission_denied"), (401, "api_credential_wrong")):
            self.t.routes[("POST", AUTH)] = [(status, {"code": code} if code else {"detail": "x"})]
            with self.assertRaises(AuthError):
                self.client().wallets()

    def test_rejected_refresh_token_is_dropped_and_reauthenticates(self):
        self.t.add("POST", AUTH, auth_ok(self.clock))
        self.t.add("POST", REFRESH, (401, {"code": "token_not_valid"}))
        self.t.add("GET", WALLETS, (200, []))
        c = self.client()
        c.wallets()
        self.clock.t += 20 * 60
        c.wallets()
        self.assertEqual(self.t.count("POST", AUTH), 2)

    # ---- review round 2: token expiry does not depend on the local clock matching Bitpin's
    def test_clock_ahead_does_not_refresh_on_every_call(self):
        server = Clock()
        self.clock.t = server.t + 20 * 60              # this computer runs 20 min ahead
        self.t.add("POST", AUTH, lambda call: (200, {
            "access": make_jwt(int(server.t + 900), "access", iat=int(server.t)),
            "refresh": make_jwt(int(server.t + 30 * 86400), "refresh", iat=int(server.t))}))
        self.t.add("POST", REFRESH, lambda call: (200, {"access": make_jwt(int(server.t + 900), "a", iat=int(server.t))}))
        self.t.add("GET", WALLETS, (200, []))
        c = self.client()
        with self.assertLogs("bitpin.api", level="WARNING") as cm:
            for _ in range(30):
                c.wallets()
                self.clock.t += 1
                server.t += 1
        self.assertEqual((self.t.count("POST", AUTH), self.t.count("POST", REFRESH)), (1, 0))
        self.assertTrue(any("clock" in m for m in cm.output))
        self.clock.t += 900                               # the real lifetime has passed: refreshed once
        server.t += 900
        c.wallets()
        self.assertEqual(self.t.count("POST", REFRESH), 1)

    def test_clock_ahead_without_iat_claim_uses_a_default_lifetime(self):
        server = Clock()
        self.clock.t = server.t + 20 * 60
        self.t.add("POST", AUTH, lambda call: (200, {"access": make_jwt(int(server.t + 900), "access"),
                                                     "refresh": make_jwt(int(server.t + 30 * 86400), "refresh")}))
        self.t.add("GET", WALLETS, (200, []))
        c = self.client()
        for _ in range(30):
            c.wallets()
            self.clock.t += 1
        self.assertEqual(self.t.count("POST", AUTH), 1)
        self.assertEqual(self.t.count("POST", REFRESH), 0)

    def test_keys_and_tokens_never_logged(self):
        self.t.add("POST", AUTH, auth_ok(self.clock))
        self.t.add("GET", WALLETS, (500, {"detail": "boom"}), (200, []))
        c = self.client()
        with self.assertLogs("bitpin", level="DEBUG") as cm:
            c.wallets()
        with open(os.path.join(self.dir, api.TOKEN_FILE)) as f:
            tok = json.load(f)
        text = "\n".join(cm.output) + repr(c) + repr(c._tokens)
        for secret in (API_KEY, SECRET, tok["access"], tok["refresh"]):
            self.assertNotIn(secret, text)


class RetryTest(Base):
    def test_429_honours_hint(self):
        self.t.add("GET", "/api/v1/mkt/markets/",
                   (429, {"detail": "Request was throttled. Expected available in 7 seconds."}), (200, []))
        BitpinClient(transport=self.t, clock=self.clock, sleep=self.clock.sleep).markets()
        self.assertEqual(self.clock.sleeps, [7.5])

    def test_429_hint_is_capped(self):
        self.t.add("GET", "/api/v1/mkt/markets/",
                   (429, {"detail": "Request was throttled. Expected available in 86400 seconds."}), (200, []))
        BitpinClient(transport=self.t, clock=self.clock, sleep=self.clock.sleep, throttle_cap=45).markets()
        self.assertEqual(self.clock.sleeps, [45])

    def test_5xx_and_network_errors_retried_with_backoff(self):
        self.t.add("GET", "/api/v1/mkt/tickers/", (502, None), TransportError("reset"), (503, None), (200, [1]))
        out = BitpinClient(transport=self.t, clock=self.clock, sleep=self.clock.sleep).tickers()
        self.assertEqual(out, [1])
        self.assertEqual(self.clock.sleeps, [1.0, 2.0, 4.0])

    def test_gives_up_after_max_retries(self):
        self.t.add("GET", "/api/v1/mkt/tickers/", (500, {"detail": "down"}))
        with self.assertRaises(BitpinAPIError):
            BitpinClient(transport=self.t, clock=self.clock, sleep=self.clock.sleep, max_retries=2).tickers()
        self.assertEqual(self.t.count("GET", "/api/v1/mkt/tickers/"), 3)

    def test_decimal_parsing(self):
        self.t.add("GET", "/api/v1/mkt/commissions/", (200, [{"symbol": "BTC_IRT", "maker": 0.003, "taker": 0.0035}]))
        rows = BitpinClient(transport=self.t, clock=self.clock, sleep=self.clock.sleep).commissions()
        self.assertIsInstance(rows[0]["taker"], Decimal)
        self.assertEqual(rows[0]["taker"], Decimal("0.0035"))

    def test_paginated_results(self):
        self.t.add("GET", "/api/v1/mkt/markets/",
                   lambda call: (200, {"results": [{"symbol": "A_IRT"}], "next": "https://api.bitpin.org/api/v1/mkt/markets/?page=2"})
                   if call["query"].get("page") != "2" else (200, {"results": [{"symbol": "B_IRT"}], "next": None}))
        rows = BitpinClient(transport=self.t, clock=self.clock, sleep=self.clock.sleep).markets()
        self.assertEqual([r["symbol"] for r in rows], ["A_IRT", "B_IRT"])

    def test_pagination_to_other_host_refused(self):
        self.t.add("GET", "/api/v1/mkt/markets/", (200, {"results": [], "next": "https://evil.example/api/v1/mkt/markets/"}))
        with self.assertRaises(BitpinAPIError):
            BitpinClient(transport=self.t, clock=self.clock, sleep=self.clock.sleep).markets()

    def test_http_next_link_on_the_same_host_is_followed_over_https(self):
        urls = []
        t = self.t

        def route(call):
            if call["query"].get("page") != "2":
                return 200, {"results": [{"symbol": "A_IRT"}], "next": "http://api.bitpin.org/api/v1/mkt/markets/?page=2"}
            return 200, {"results": [{"symbol": "B_IRT"}], "next": None}
        t.add("GET", "/api/v1/mkt/markets/", route)
        real = t.__call__

        def spy(method, url, headers, body, timeout):
            urls.append(url)
            return real(method, url, headers, body, timeout)
        rows = BitpinClient(transport=spy, clock=self.clock, sleep=self.clock.sleep).markets()
        self.assertEqual([r["symbol"] for r in rows], ["A_IRT", "B_IRT"])
        self.assertTrue(all(u.startswith("https://api.bitpin.org/") for u in urls))

    def test_redirect_is_an_error(self):
        self.t.add("GET", "/api/v1/mkt/markets/", (302, None))
        with self.assertRaises(BitpinAPIError) as cm:
            BitpinClient(transport=self.t, clock=self.clock, sleep=self.clock.sleep).markets()
        self.assertEqual(cm.exception.status, 302)
        self.assertEqual(self.t.count("GET", "/api/v1/mkt/markets/"), 1)


class RedirectTransportTest(unittest.TestCase):
    def test_default_transport_never_follows_a_redirect(self):
        import http.server
        import threading
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.path, self.headers.get("Authorization")))
                if self.path == "/api/v1/wlt/wallets/":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:%d/elsewhere/" % self.server.server_port)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    body = b"[]"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            url = "http://127.0.0.1:%d/api/v1/wlt/wallets/" % srv.server_port
            status, _ = api.default_transport("GET", url, {"Authorization": "Bearer dummy-token"}, None, 5)
            self.assertEqual(status, 302)
            self.assertEqual([h[0] for h in hits], ["/api/v1/wlt/wallets/"])   # the target never saw the token
        finally:
            srv.shutdown()
            srv.server_close()


def order_obj(identifier, oid=42, state="closed", **kw):
    o = {"id": oid, "symbol": "USDT_IRT", "type": "market", "side": "buy", "identifier": identifier, "state": state,
         "dealed_base_amount": "10.00", "dealed_quote_amount": "2300000", "commission": "0.035"}
    o.update(kw)
    return o


class OrderPostTest(Base):
    def setUp(self):
        super().setUp()
        self.t.add("POST", AUTH, auth_ok(self.clock))

    def test_timeout_then_found_by_identifier_no_duplicate(self):
        def lookup(call):
            return 200, [order_obj(call["query"]["identifier"])]
        self.t.add("POST", ORDERS, TransportError("timed out", timeout=True))
        self.t.add("GET", ORDERS, lookup)
        o = self.client().place_order("USDT_IRT", "buy", quote_amount=Decimal("2300000"), identifier="ident-1")
        self.assertEqual(o["id"], 42)
        self.assertEqual(self.t.count("POST", ORDERS), 1)
        post = [c for c in self.t.calls if c["method"] == "POST" and c["path"] == ORDERS][0]
        self.assertEqual(post["body"]["identifier"], "ident-1")
        self.assertEqual(post["body"]["quote_amount"], "2300000")
        self.assertEqual(post["body"]["type"], "market")

    def test_5xx_and_not_found_raises_unknown_without_resubmitting(self):
        self.t.add("POST", ORDERS, (502, {"detail": "bad gateway"}))
        self.t.add("GET", ORDERS, (200, []))
        with self.assertRaises(OrderStatusUnknown) as cm:
            self.client().place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-2")
        self.assertEqual(cm.exception.identifier, "ident-2")
        self.assertEqual(self.t.count("POST", ORDERS), 1)
        self.assertGreaterEqual(self.t.count("GET", ORDERS), 4)

    def test_lookup_matches_identifier_client_side(self):
        # a server that ignores ?identifier= returns unrelated orders: they must not be taken as ours
        self.t.add("POST", ORDERS, TransportError("reset"))
        self.t.add("GET", ORDERS, (200, [order_obj("someone-else")]))
        with self.assertRaises(OrderStatusUnknown):
            self.client().place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="mine")
        self.assertEqual(self.t.count("POST", ORDERS), 1)

    def test_429_on_post_looks_up_then_resends_same_identifier(self):
        self.t.add("POST", ORDERS, (429, {"detail": "Request was throttled. Expected available in 2 seconds."}),
                   lambda call: (201, order_obj(call["body"]["identifier"], state="active")))
        self.t.add("GET", ORDERS, (200, []))
        o = self.client().place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-3")
        self.assertEqual(o["identifier"], "ident-3")
        posts = [c for c in self.t.calls if c["method"] == "POST" and c["path"] == ORDERS]
        self.assertEqual(len(posts), 2)
        self.assertEqual({p["body"]["identifier"] for p in posts}, {"ident-3"})
        self.assertIn(2.5, self.clock.sleeps)

    def test_429_on_post_found_by_lookup_is_not_resent(self):
        self.t.add("POST", ORDERS, (429, {"detail": "Expected available in 1 seconds."}))
        self.t.add("GET", ORDERS, lambda call: (200, [order_obj(call["query"]["identifier"])]))
        self.client().place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-4")
        self.assertEqual(self.t.count("POST", ORDERS), 1)

    def test_4xx_is_a_definite_rejection(self):
        self.t.add("POST", ORDERS, (400, {"code": "insufficient_balance"}))
        with self.assertRaises(BitpinAPIError) as cm:
            self.client().place_order("USDT_IRT", "buy", quote_amount="2300000")
        self.assertEqual(cm.exception.code, "insufficient_balance")
        self.assertEqual(self.t.count("GET", ORDERS), 0)

    def test_429_with_long_hint_is_not_sent_minutes_later(self):
        self.t.add("POST", ORDERS, (429, {"detail": "Request was throttled. Expected available in 90 seconds."}))
        self.t.add("GET", ORDERS, (200, []))
        with self.assertRaises(OrderNotSent):
            self.client().place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-5")
        self.assertEqual(self.t.count("POST", ORDERS), 1)
        self.assertEqual(self.clock.sleeps, [])           # gave up at once instead of re-sending 4 minutes later

    def test_429_resends_only_within_the_time_budget(self):
        self.t.add("POST", ORDERS, (429, {"detail": "Expected available in 4 seconds."}))
        self.t.add("GET", ORDERS, (200, []))
        t0 = self.clock.t
        with self.assertRaises(OrderNotSent):
            self.client(order_throttle_max_wait=10).place_order("USDT_IRT", "buy", quote_amount="2300000",
                                                                identifier="ident-6")
        self.assertEqual(self.t.count("POST", ORDERS), 3)    # t=0, 4.5, 9: a 4th would be past 10 s
        self.assertLessEqual(self.clock.t - t0, 10)

    def test_token_failure_before_the_post_is_not_sent(self):
        self.t.routes[("POST", AUTH)] = [TransportError("dns failure")]
        with self.assertRaises(OrderNotSent):
            self.client().place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-7")
        self.assertEqual(self.t.count("POST", ORDERS), 0)

    def test_406_on_an_order_is_an_ordinary_rejection(self):
        self.t.add("POST", ORDERS, (406, {"code": "order_value_too_small"}))
        with self.assertRaises(BitpinAPIError) as cm:
            self.client().place_order("USDT_IRT", "buy", quote_amount="2300000")
        self.assertNotIsInstance(cm.exception, AuthError)
        self.t.add("GET", WALLETS, (406, {"code": "api_credential_wrong"}))
        with self.assertRaises(AuthError):
            self.client().wallets()

    # ---- review round 2
    def test_lookup_refused_by_the_allow_list_is_unknown_not_not_sent(self):
        # the order executed, the POST timed out, and the lookup's `next` link is outside the allow-list
        self.t.add("POST", ORDERS, TransportError("timed out", timeout=True))
        self.t.add("GET", ORDERS, (200, {"results": [], "next": "https://api.bitpin.org/api/v2/odr/orders/?page=2"}))
        with self.assertRaises(OrderStatusUnknown):
            self.client().place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-8")
        self.assertEqual(self.t.count("POST", ORDERS), 1)

    def test_v1_next_link_is_followed_on_the_api_v1_path(self):
        # Bitpin serves /v1/ and /api/v1/: a /v1/ pagination link must not break the identifier lookup
        def lookup(call):
            if call["query"].get("page") != "2":
                return 200, {"results": [order_obj("other", oid=1)], "next": "https://api.bitpin.org/v1/odr/orders/?page=2"}
            return 200, {"results": [order_obj("ident-8b")], "next": None}
        self.t.add("POST", ORDERS, TransportError("timed out", timeout=True))
        self.t.add("GET", ORDERS, lookup)
        o = self.client().place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-8b")
        self.assertEqual((o["id"], o["identifier"]), (42, "ident-8b"))
        self.assertEqual(self.t.count("POST", ORDERS), 1)

    def test_unexpected_exception_after_the_post_is_unknown(self):
        self.t.add("POST", ORDERS, RuntimeError("TLS library exploded"))
        with self.assertRaises(OrderStatusUnknown):
            self.client().place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-9")
        with self.assertRaises(ValueError):                 # before any POST: still a plain ValueError
            self.client().place_order("USDT_IRT", "buy", quote_amount="-1")

    def test_market_order_is_not_sent_after_a_slow_token_refresh(self):
        self.t.routes[("POST", AUTH)] = [auth_ok(self.clock)]
        self.t.add("GET", WALLETS, (200, []))
        self.t.add("POST", REFRESH, (429, {"detail": "Expected available in 55 seconds."}),
                   lambda call: (200, {"access": make_jwt(int(self.clock.t + 900), "a2")}))
        self.t.add("POST", ORDERS, lambda call: (201, order_obj(call["body"]["identifier"])))
        c = self.client()
        c.wallets()
        self.clock.t += 900 - 30                          # the access token is due when the order is placed
        with self.assertRaises(OrderNotSent):
            c.place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-10")
        self.assertEqual(self.t.count("POST", ORDERS), 0)
        # renewing the token first (the runner calls ensure_token before the pre-trade checks)
        self.clock.t += 900
        self.t.routes[("POST", REFRESH)] = [(429, {"detail": "Expected available in 55 seconds."}),
                                            lambda call: (200, {"access": make_jwt(int(self.clock.t + 900), "a3")})]
        c.ensure_token()
        t0 = self.clock.t
        c.place_order("USDT_IRT", "buy", quote_amount="2300000", identifier="ident-11")
        self.assertEqual(self.clock.t, t0)                # POSTed at once
        self.assertEqual(self.t.count("POST", ORDERS), 1)

    def test_order_validation(self):
        c = self.client()
        with self.assertRaises(ValueError):
            c.place_order("USDT_IRT", "hold", quote_amount="1")
        with self.assertRaises(ValueError):
            c.place_order("USDT_IRT", "buy")
        with self.assertRaises(ValueError):
            c.place_order("USDT_IRT", "buy", quote_amount="-5")
        with self.assertRaises(ValueError):
            c.place_order("../wlt/withdraw", "buy", quote_amount="5")
        self.assertEqual(self.t.calls, [])

    def test_limit_order_body_and_validation(self):
        self.t.add("POST", ORDERS, (201, order_obj("lim-1", symbol="BTC_USDT", type="limit", state="active")))
        o = self.client().place_order("BTC_USDT", "buy", "limit", base_amount=Decimal("0.00100000"),
                                      price=Decimal("68800.12"), identifier="lim-1")
        self.assertEqual(o["state"], "active")
        post = [c for c in self.t.calls if c["method"] == "POST" and c["path"] == ORDERS][0]
        self.assertEqual(post["body"], {"symbol": "BTC_USDT", "type": "limit", "side": "buy", "identifier": "lim-1",
                                        "base_amount": "0.00100000", "price": "68800.12"})
        c = self.client()
        n = len(self.t.calls)
        for kw in ({"base_amount": "1"}, {"price": "1", "quote_amount": "5"},
                   {"price": "1", "base_amount": "1", "quote_amount": "5"}, {"price": "0", "base_amount": "1"}):
            with self.assertRaises(ValueError):
                c.place_order("BTC_USDT", "buy", "limit", **kw)
        with self.assertRaises(ValueError):
            c.place_order("BTC_USDT", "buy", "market", quote_amount="5", price="1")
        for t in ("stop_limit", "oco"):              # deliberately unsupported
            with self.assertRaises(ValueError):
                c.place_order("BTC_USDT", "sell", t, base_amount="1", price="1")
        self.assertEqual(len(self.t.calls), n)

    def test_open_orders_reads_active_orders_of_the_whole_account(self):
        rows = [order_obj("a", 1, "active", symbol="BTC_USDT"), order_obj("b", 2, "closed", symbol="BTC_USDT"),
                order_obj(None, 3, "initial", symbol="ETH_USDT")]
        self.t.add("GET", ORDERS, (200, rows))
        c = self.client()
        self.assertEqual([o["id"] for o in c.open_orders()], [1, 3])
        q = self.t.calls[-1]["query"]
        self.assertEqual((q["state"], q["limit"]), ("active", "100"))
        self.assertEqual([o["id"] for o in c.open_orders("btc_usdt")], [1])
        self.assertEqual(self.t.calls[-1]["query"]["symbol"], "BTC_USDT")

    def test_cancel_order_semantics(self):
        path = ORDERS + "77/"
        self.t.add("DELETE", path, (204, None), (404, {"detail": "Not found."}), (406, {"detail": "not allowed"}))
        c = self.client()
        self.assertTrue(c.cancel_order(77))
        self.assertFalse(c.cancel_order("77"))
        with self.assertRaises(BitpinAPIError) as cm:
            c.cancel_order(77)
        self.assertEqual(cm.exception.status, 406)
        with self.assertRaises(ValueError):
            c.cancel_order("../wlt/withdraws")


if __name__ == "__main__":
    unittest.main()
