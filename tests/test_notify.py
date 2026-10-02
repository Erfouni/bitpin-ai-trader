"""Telegram notifier tests: fake Telegram / Kimi transports, temporary state dirs and local sockets
only (never calls the real Telegram, Moonshot or Bitpin)."""
import csv
import io
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin import notify  # noqa: E402
from bitpin.notify import (Notifier, Renderer, Secrets, TelegramClient, TelegramError, Translator,  # noqa: E402
                           apply_stop_request, decision_view, read_new_lines, split_message)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
KIMI_KEY = "sk-NOTIFYTESTKEY1234567890abcdef"
CHAT = 111222333
CHAT2 = 444555666
OTHER = 999888777
T0 = 1790121600.0          # 2026-09-23 00:00 UTC = 03:30 Tehran (1 Mehr 1405)
CSV_HEADER = ["time_utc", "mode", "symbol", "side", "requested", "base", "quote", "avg_price", "fee", "fee_asset",
              "order_id", "partial", "status", "note"]
FA_PERSIAN = ("دلیل: بازار پس از تصمیم فدرال رزرو آرام است و ورود پول به صندوق‌های بیت‌کوین ادامه دارد؛ "
              "بیشتر سرمایه در تتر می‌ماند.")


class FakeClock:
    def __init__(self, t):
        self.t = float(t)
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(float(s), 3))
        self.t += float(s)


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.fail = []
        self.updates = []
        self.offsets = []

    def send_message(self, chat_id, text, html_mode=True, silent=False, max_retries=None):
        if self.fail:
            e = self.fail.pop(0)
            if e is not None:
                raise e
        self.sent.append((chat_id, text, html_mode, silent))
        return {"message_id": len(self.sent)}

    def get_updates(self, offset=None, timeout=0):
        self.offsets.append(offset)
        ups, self.updates = self.updates, []
        return ups

    def get_me(self, max_retries=None):
        return {"username": "MyBot", "first_name": "my bot"}

    def texts(self, chat=None):
        return [t for c, t, _, _ in self.sent if chat is None or c == chat]


def decision_rec(t, digest="d1g3st000000aaaa", valid=True, hold=False, targets=None, current=None, cash=0.0,
                 reasoning="Keep most in USDT_IRT as the toman hedge; add BTC on ETF inflows.", fallback=None,
                 error_kind="", error="", trigger="scheduled (2.0h since last decision)", report_fa=None,
                 response="", equity=3800000, usdt=225587.0, origin="live", news_ok=True):
    targets = {"BTC_IRT": 0.25, "ETH_IRT": 0.0, "USDT_IRT": 0.75} if targets is None else targets
    d = {"adjustments": [], "attempts": 1, "cash_irt": cash, "computed_against": current or {},
         "computed_cash": max(0.0, 1.0 - sum((current or {}).values())), "confidence": 0.62, "decided_at": t,
         "error": error, "error_kind": error_kind, "expires_at": t + 3600, "fallback": bool(fallback),
         "fallback_reason": fallback, "hold": hold, "key_risks": "sharp crypto sell-off", "low_confidence": False,
         "model": "kimi-k3", "news_summary": "Fed on hold; ETF inflows", "next_review_hours": 2, "origin": origin,
         "proposed": dict(targets), "reasoning": reasoning if valid else "",
         "snapshot": {"equity_irt": equity, "usdt_irt": usdt}, "targets": targets if valid else {},
         "usage": {"total_tokens": 1200}, "valid": valid}
    if report_fa is not None:
        d["report_fa"] = report_fa
    return {"context_digest": digest, "context_summary": {"chars": 1000, "data_errors": [], "drawdown_pct": 0.0,
                                                          "equity_irt": equity, "usdt_irt": usdt},
            "current_weights": current or {}, "decision": d, "model": "kimi-k3",
            "news": {"ok": news_ok, "stale": False, "error": "" if news_ok else "network error", "items": 3},
            "response": response, "risk_profile": "full", "time": round(t, 3),
            "time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)), "trigger": trigger}


def fill(symbol, side, base, quote, fee, fee_asset, order_id):
    return {"base": base, "fee": fee, "fee_asset": fee_asset, "order_id": order_id, "partial": False, "quote": quote,
            "side": side, "symbol": symbol}


def runner_rec(t, fills=(), action="kimi", decided_at=None, status="ok", mode="live", **extra):
    rec = {"action": action, "bar_ts": int(t) - 1800, "brain_error": None, "current": {"USDT_IRT": 0.5},
           "decided": True, "decision": {"decided_at": decided_at, "valid": True} if decided_at else None,
           "equity_irt": "3800000.0000", "errors": [], "fallback": None, "fills": list(fills), "irt_cash_w": 0.5,
           "mode": mode, "plan": ["BUY BTC with 950,000 IRT"] if fills else [], "skipped": [], "status": status,
           "targets": {}, "time": t, "trigger": "scheduled", "why": "valid Kimi decision", "cycle_min": 60}
    rec.update(extra)
    return rec


class Base(unittest.TestCase):
    def setUp(self):
        notify.REDACT.reset()
        self.tmp = tempfile.mkdtemp(prefix="notify-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sd = os.path.join(self.tmp, "bot")
        os.makedirs(self.sd)
        self.nd = os.path.join(self.tmp, "notify")
        self.clock = FakeClock(T0)
        self.tg = FakeTelegram()

    def cfg(self, **over):
        base = {"state_dir": self.sd, "notify_state_dir": self.nd, "mode": "live", "daily_summary_time": None,
                "telegram": {"min_send_interval_seconds": 0, "max_retries": 0}}
        tg = over.pop("telegram", None)
        base.update(over)
        if tg:
            base["telegram"].update(tg)
        return notify.load_config(None, overrides=base)

    def notifier(self, cfg=None, chats=(CHAT,), translator=None, fresh=True):
        n = Notifier(cfg or self.cfg(), Secrets(TOKEN, chats), telegram=self.tg, translator=translator,
                     clock=self.clock, sleep=self.clock.sleep)
        if fresh:
            n.activity = lambda: self.clock.t
        return n

    def p(self, name):
        return os.path.join(self.sd, name)

    def append(self, name, rec):
        with open(self.p(name), "a", encoding="utf-8", newline="") as f:
            f.write((rec if isinstance(rec, str) else json.dumps(rec)) + "\n")

    def add_trade(self, t, symbol, side, base, quote, avg, fee, fee_asset, order_id, note="", status="filled",
                  name="live_trades.csv"):
        new = not os.path.exists(self.p(name))
        with open(self.p(name), "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(CSV_HEADER)
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)), "live", symbol, side, quote, base, quote,
                        avg, fee, fee_asset, order_id, "False", status, note])

    def write_json(self, name, obj):
        with open(self.p(name), "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def ids(self, n):
        return [i["id"] for i in n.st["outbox"]]


# --------------------------------------------------------------------------- config / secrets

class TestConfigAndSecrets(Base):
    def test_example_file_documents_every_setting_and_equals_the_defaults(self):
        path = os.path.join(ROOT, "notify.example.json")
        with open(path, encoding="utf-8") as f:
            ex = json.load(f)
        self.assertEqual(notify.load_config(path), notify.load_config(None))

        def check(defaults, section, where):
            for k in defaults:
                self.assertIn(k, section, "%s%s missing in notify.example.json" % (where, k))
                if k in ("telegram", "translation"):
                    self.assertIn("_section", section[k])
                    check(defaults[k], section[k], k + ".")
                else:
                    self.assertIn("_" + k, section, "%s%s has no '_%s' comment" % (where, k, k))
            for k in section:
                if not k.startswith("_"):
                    self.assertIn(k, defaults, "%s%s is not a setting" % (where, k))
        check(notify.DEFAULT_CONFIG, ex, "")

    def test_unknown_and_secret_keys_and_bad_values_are_refused(self):
        for over in ({"typo_key": 1}, {"bot_token": TOKEN}, {"telegram": {"api_key": "x"}}, {"poll_seconds": 0},
                     {"mode": "demo"}, {"daily_summary_time": "25:00"}, {"telegram": {"api_base": "http://x.org"}},
                     {"translation": {"base_url": "https://u:p@x.org"}}, {"notify_state_dir": self.sd},
                     {"coin_names_fa": {"btc": "x"}}):
            with self.subTest(over=over):
                with self.assertRaises(notify.NotifyConfigError) as cm:
                    self.cfg(**over)
                self.assertNotIn(TOKEN, str(cm.exception))

    def test_secrets_come_only_from_the_notifier_variables(self):
        env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": " 111222333, -100444 ,111222333",
               "TELEGRAM_HTTPS_PROXY": "http://127.0.0.1:1081", "KIMI_API_KEY": KIMI_KEY,
               "BITPIN_API_KEY": "bitpin-key-VALUE-123456", "BITPIN_SECRET_KEY": "bitpin-secret-VALUE-654321"}
        s = notify.load_secrets(env)
        self.assertEqual(s.chat_ids, [111222333, -100444])
        self.assertEqual(s.telegram_proxy, "http://127.0.0.1:1081")
        blob = repr(s) + json.dumps(vars(s), default=str)
        self.assertNotIn("bitpin-key-VALUE", blob)
        self.assertNotIn("bitpin-secret-VALUE", blob)
        self.assertNotIn(TOKEN, repr(s))
        self.assertNotIn(KIMI_KEY, repr(s))
        gone = notify.drop_trading_secrets(env)
        self.assertEqual(sorted(gone), ["BITPIN_API_KEY", "BITPIN_SECRET_KEY"])
        self.assertNotIn("BITPIN_API_KEY", env)

    def test_bad_secrets_are_reported_without_their_value(self):
        for env in ({"TELEGRAM_BOT_TOKEN": "123:short"}, {"TELEGRAM_BOT_TOKEN": TOKEN + " x"},
                    {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "@me"},
                    {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "1", "TELEGRAM_HTTPS_PROXY": "socks5://x:1"},
                    {"TELEGRAM_BOT_TOKEN": TOKEN}):
            with self.subTest(env=sorted(env)):
                with self.assertRaises(notify.NotifyConfigError) as cm:
                    notify.load_secrets(env)
                self.assertNotIn(TOKEN, str(cm.exception))
                self.assertNotIn("123:short", str(cm.exception))


# --------------------------------------------------------------------------- redaction

class TestRedaction(Base):
    def test_redactor_patterns(self):
        r = notify.Redactor([TOKEN])
        url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
        self.assertNotIn(TOKEN, r(url))
        other = "987654321:ZZHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"   # a token we were never told about
        self.assertNotIn(other, notify.Redactor()("error at bot%s/getMe" % other))
        self.assertNotIn(other, notify.Redactor()("token " + other))
        self.assertNotIn(KIMI_KEY, notify.Redactor()("Authorization: Bearer " + KIMI_KEY))
        self.assertEqual(notify.Redactor()("http://user:pass@127.0.0.1:1081"), "http://<redacted>@127.0.0.1:1081")
        self.assertEqual(r("12:30 and 1,234"), "12:30 and 1,234")

    def test_logs_never_contain_the_token_even_with_tracebacks(self):
        notify.REDACT.add(TOKEN)
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setFormatter(logging.Formatter("%(message)s"))      # a plain formatter: the logger filter must redact
        notify.log.addHandler(h)
        old, prop = notify.log.level, notify.log.propagate
        notify.log.setLevel(logging.DEBUG)
        notify.log.propagate = False
        self.addCleanup(notify.log.removeHandler, h)
        self.addCleanup(notify.log.setLevel, old)
        self.addCleanup(setattr, notify.log, "propagate", prop)
        url = "https://api.telegram.org/bot%s/getUpdates" % TOKEN
        notify.log.error("request to %s failed", url)
        try:
            raise OSError("cannot open %s" % url)
        except OSError:
            notify.log.exception("boom")
        out = buf.getvalue()
        self.assertIn("request to", out)
        self.assertIn("Traceback", out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(TOKEN.split(":")[1], out)

    def test_client_errors_and_repr_never_contain_the_token(self):
        def transport(method, url, headers, body, timeout):
            self.assertIn(TOKEN, url)                  # the token is only in the URL path ...
            raise OSError("failed: %s" % url)          # ... and a library error may quote the URL

        c = TelegramClient(TOKEN, transport=transport, max_retries=0, sleep=lambda s: None)
        with self.assertRaises(TelegramError) as cm:
            c.get_me()
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN, repr(c))

        def echo(method, url, headers, body, timeout):
            return 400, {}, json.dumps({"ok": False, "description": "bad url %s" % url}).encode()
        with self.assertRaises(TelegramError) as cm:
            TelegramClient(TOKEN, transport=echo, max_retries=0).get_me()
        self.assertEqual(cm.exception.status, 400)
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_state_files_and_status_output_never_contain_the_token(self):
        n = self.notifier()
        n.start()
        self.tg.fail = [TelegramError("network error at https://api.telegram.org/bot%s/sendMessage" % TOKEN,
                                      retryable=True)]
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60, reasoning="token %s leaked?" % TOKEN))
        n.step()
        with open(os.path.join(self.nd, notify.STATE_FILE), encoding="utf-8") as f:
            data = f.read()
        self.assertIn("network error", data)
        self.assertNotIn(TOKEN, data)
        self.assertNotIn(TOKEN, notify.status_report(n.cfg))


# --------------------------------------------------------------------------- HTTP, proxy isolation

class _RecordingServer:
    """A TCP server that records what it receives and answers every connection with `reply`."""

    def __init__(self, reply=b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", hang=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.reply = reply
        self.hang = hang
        self.stop = False
        self.th = threading.Thread(target=self._serve, daemon=True)
        self.th.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(2)
            try:
                data = conn.recv(65536)
                self.received.append(data)
                if self.hang:
                    time.sleep(4)
                else:
                    conn.sendall(self.reply)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.stop = True
        self.sock.close()


class TestHttpAndProxy(Base):
    def test_opener_uses_only_the_configured_proxy(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://10.9.9.9:3128", "https_proxy": "http://10.9.9.9:3128",
                                          "ALL_PROXY": "http://10.9.9.9:3128", "NO_PROXY": "*"}):
            op = notify.build_opener("http://127.0.0.1:1081")
            ph = [h for h in op.handlers if isinstance(h, urllib.request.ProxyHandler)]
            self.assertEqual(len(ph), 1)
            self.assertIsInstance(ph[0], notify.StrictProxyHandler)
            self.assertEqual(ph[0].proxies, {"https": "http://127.0.0.1:1081", "http": "http://127.0.0.1:1081"})
            op2 = notify.build_opener(None)
            # direct: the empty StrictProxyHandler replaces urllib's default (environment-reading) one, and
            # having no *_open method it is not installed at all - so no proxy of any kind is used
            self.assertEqual([h for h in op2.handlers if isinstance(h, urllib.request.ProxyHandler)], [])
            self.assertFalse(any(getattr(h, "proxies", None) for h in op2.handlers))
            self.assertTrue(any(isinstance(h, notify.NoRedirect) for h in op.handlers))

    def test_requests_go_through_the_proxy_as_a_tunnel_and_the_token_stays_inside_tls(self):
        srv = _RecordingServer()
        self.addCleanup(srv.close)
        c = TelegramClient(TOKEN, proxy="http://127.0.0.1:%d" % srv.port, timeout=5, max_retries=0)
        with mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            with self.assertRaises(TelegramError) as cm:
                c.get_me()
        self.assertTrue(cm.exception.retryable)
        self.assertNotIn(TOKEN, str(cm.exception))
        seen = b"".join(srv.received)
        self.assertTrue(seen.startswith(b"CONNECT api.telegram.org:443 HTTP/1."), seen[:80])
        self.assertNotIn(TOKEN.encode(), seen)

    def test_environment_proxy_is_ignored_without_a_configured_proxy(self):
        srv = _RecordingServer()
        self.addCleanup(srv.close)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        env = {"HTTPS_PROXY": "http://127.0.0.1:%d" % srv.port, "https_proxy": "http://127.0.0.1:%d" % srv.port,
               "ALL_PROXY": "http://127.0.0.1:%d" % srv.port}
        with mock.patch.dict(os.environ, env):
            c = TelegramClient(TOKEN, api_base="https://127.0.0.1:%d" % port, timeout=5, max_retries=0)
            with self.assertRaises(TelegramError):
                c.get_me()
        time.sleep(0.3)
        self.assertEqual(srv.received, [])

    def test_https_only_and_redirects_are_never_followed(self):
        with self.assertRaises(ValueError):
            notify.make_transport()("POST", "http://api.telegram.org/x", {}, b"", 5)
        with self.assertRaises(notify.NotifyConfigError):
            TelegramClient(TOKEN, api_base="http://api.telegram.org")
        self.assertIsNone(notify.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))
        calls = []

        def redirect(method, url, headers, body, timeout):
            calls.append(url)
            return 302, {"Location": "https://evil.example/"}, b""
        with self.assertRaises(TelegramError) as cm:
            TelegramClient(TOKEN, transport=redirect, max_retries=3, sleep=lambda s: None).get_me()
        self.assertEqual(len(calls), 1)
        self.assertFalse(cm.exception.retryable)

    def test_the_transport_enforces_a_hard_time_limit(self):
        srv = _RecordingServer(hang=True)
        self.addCleanup(srv.close)
        tr = notify.make_transport(allow_http=True)
        t = time.monotonic()
        with self.assertRaises((socket.timeout, OSError)):
            tr("POST", "http://127.0.0.1:%d/x" % srv.port, {}, b"{}", 1)
        self.assertLess(time.monotonic() - t, 3.9)


# --------------------------------------------------------------------------- retry / backoff

class TestRetry(Base):
    def client(self, responses, **kw):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(json.loads(body.decode("utf-8")))
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        clock = FakeClock(0)
        kw.setdefault("max_retries", 4)
        c = TelegramClient(TOKEN, transport=transport, sleep=clock.sleep, backoff=2.0, backoff_max=60.0, **kw)
        return c, calls, clock

    @staticmethod
    def ok(result=True):
        return 200, {}, json.dumps({"ok": True, "result": result}).encode()

    @staticmethod
    def err(status, desc="x", **params):
        body = {"ok": False, "error_code": status, "description": desc}
        if params:
            body["parameters"] = params
        return status, {}, json.dumps(body).encode()

    def test_network_errors_are_retried_with_exponential_backoff(self):
        c, calls, clock = self.client([ConnectionResetError("reset"), OSError("down"), self.ok({"id": 1})])
        self.assertEqual(c.get_me(), {"id": 1})
        self.assertEqual(len(calls), 3)
        self.assertEqual(clock.sleeps, [2.0, 4.0])

    def test_429_honours_retry_after(self):
        c, calls, clock = self.client([self.err(429, "Too Many Requests: retry after 7", retry_after=7), self.ok()])
        self.assertTrue(c.send_message(CHAT, "x"))
        self.assertEqual(clock.sleeps, [7.0])
        c, calls, clock = self.client([self.err(429, retry_after=500)])
        with self.assertRaises(TelegramError) as cm:
            c.send_message(CHAT, "x")
        self.assertEqual(cm.exception.retry_after, 500)
        self.assertEqual(clock.sleeps, [])               # a long retry_after is left to the outbox

    def test_5xx_is_retried_and_4xx_never(self):
        c, calls, clock = self.client([self.err(502), self.err(500), self.ok()])
        c.get_me()
        self.assertEqual(len(calls), 3)
        for status in (400, 401, 403, 404, 409):
            with self.subTest(status=status):
                c, calls, clock = self.client([self.err(status), self.ok()])
                with self.assertRaises(TelegramError) as cm:
                    c.get_me()
                self.assertEqual(len(calls), 1)
                self.assertFalse(cm.exception.retryable)
                self.assertEqual(cm.exception.status, status)

    def test_retries_are_bounded(self):
        c, calls, clock = self.client([OSError("x")] * 3, max_retries=2)
        with self.assertRaises(TelegramError) as cm:
            c.get_me()
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(calls), 3)

    def test_send_parameters(self):
        c, calls, clock = self.client([self.ok()])
        c.send_message(CHAT, "<b>x</b>", silent=True)
        self.assertEqual(calls[0]["parse_mode"], "HTML")
        self.assertTrue(calls[0]["disable_notification"])
        self.assertTrue(calls[0]["disable_web_page_preview"])

    def test_outbox_keeps_messages_while_telegram_is_down_and_delivers_them_in_order_once(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60, digest="a" * 16))
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 120, digest="b" * 16, hold=True))
        self.tg.fail = [TelegramError("network error", retryable=True)]
        n.step()
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(len(n.st["outbox"]), 2)
        n.step()                                      # still inside the delivery backoff: nothing is tried
        self.assertEqual(self.tg.sent, [])
        self.clock.t += 20
        n.step()
        self.assertEqual(len(self.tg.sent), 2)
        self.assertIn("aaaaaaaa", self.tg.sent[0][1])
        self.assertIn("bbbbbbbb", self.tg.sent[1][1])
        n2 = self.notifier()
        n2.start()
        n2.step()
        self.assertEqual(len(self.tg.sent), 2)

    def test_html_error_is_resent_as_plain_text(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60))
        self.tg.fail = [TelegramError("Bad Request: can't parse entities", status=400)]
        n.step()
        self.assertEqual(len(self.tg.sent), 1)
        chat, text, html_mode, _ = self.tg.sent[0]
        self.assertFalse(html_mode)
        self.assertNotIn("<b>", text)
        self.assertIn("تصمیم جدید Kimi", text)
        self.assertEqual(n.st["outbox"], [])

    def test_bad_token_keeps_the_queue(self):
        n = self.notifier()
        n.start()
        self.tg.fail = [TelegramError("Unauthorized", status=401)]
        n.step()
        self.assertEqual(len(n.st["outbox"]), 1)      # the start message waits for a fixed token
        self.assertEqual(self.tg.sent, [])


# --------------------------------------------------------------------------- tailing / exactly once

class TestTailing(Base):
    def test_read_new_lines_partial_truncation_rotation(self):
        path = self.p("x.jsonl")
        with open(path, "wb") as f:
            f.write(b'{"a": 1}\n{"b": 2')
        lines, cur, ev = read_new_lines(path, None)
        self.assertEqual(lines, ['{"a": 1}'])
        self.assertIsNone(ev)
        with open(path, "ab") as f:
            f.write(b'}\n')
        lines, cur, ev = read_new_lines(path, cur)
        self.assertEqual(lines, ['{"b": 2}'])
        lines, cur2, ev = read_new_lines(path, cur)
        self.assertEqual(lines, [])
        with open(path, "wb") as f:                    # truncated and rewritten, shorter
            f.write(b'{"c": 3}\n')
        lines, cur, ev = read_new_lines(path, cur)
        self.assertEqual(ev, "truncated")
        self.assertEqual(lines, ['{"c": 3}'])
        tmp = path + ".new"                            # replaced by another file (rotation)
        with open(tmp, "wb") as f:
            f.write(b'{"d": 4}\n{"e": 5}\n{"f": 6}\n')
        os.replace(tmp, path)
        lines, cur, ev = read_new_lines(path, cur)
        self.assertEqual(ev, "rotated")
        self.assertEqual(lines, ['{"d": 4}', '{"e": 5}', '{"f": 6}'])
        lines, cur, ev = read_new_lines(self.p("missing"), cur)
        self.assertEqual(ev, "missing")

    def test_a_line_longer_than_the_read_limit_is_skipped(self):
        path = self.p("x.jsonl")
        with open(path, "wb") as f:
            f.write(b"x" * 5000)
        lines, cur, ev = read_new_lines(path, None, max_bytes=1000)
        self.assertEqual(lines, [])
        self.assertTrue(cur["skipping"])
        with open(path, "ab") as f:
            f.write(b'\n{"ok": 1}\n')
        for _ in range(6):
            lines, cur, ev = read_new_lines(path, cur, max_bytes=1000)
            if lines:
                break
        self.assertEqual(lines, ['{"ok": 1}'])

    def test_first_start_sends_no_history_and_later_events_exactly_once_across_restarts(self):
        for i in range(3):
            self.append("kimi_decisions.jsonl", decision_rec(T0 - 3600 * (3 - i), digest="old%013d" % i))
        n = self.notifier()
        self.assertTrue(n.start())
        n.step()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("اطلاع‌رسان تلگرام ربات بیت‌پین روشن شد", self.tg.sent[0][1])
        self.assertIn("آخرین تصمیم", self.tg.sent[0][1])       # the current status is in the start message
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60, digest="new1" + "0" * 12))
        n.step()
        n.step()
        self.assertEqual(len(self.tg.sent), 2)
        self.assertIn("new10000", self.tg.sent[1][1])
        n2 = self.notifier()                          # restart: nothing again, no second start message
        self.assertFalse(n2.start())
        n2.step()
        self.assertEqual(len(self.tg.sent), 2)
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 120, digest="new2" + "0" * 12))
        n2.step()
        self.assertEqual(len(self.tg.sent), 3)

    def test_backfill_of_the_last_decisions(self):
        for i in range(4):
            self.append("kimi_decisions.jsonl", decision_rec(T0 - 3600 * (4 - i), digest="old%013d" % i))
        n = self.notifier(self.cfg(backfill_decisions=2))
        n.start()
        n.step()
        texts = self.tg.texts()
        self.assertEqual(len(texts), 3)
        self.assertIn("old0000000000002", "".join(texts[1:]).replace("<code>", "").replace("</code>", "") + "old0000000000002")
        self.assertIn("از تاریخچه", texts[1])

    def test_a_crash_before_the_state_write_re_reads_but_sends_once(self):
        n = self.notifier()
        n.start()
        n.step()
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60))
        n._scan_decisions(self.clock.t)               # queued in memory, cursor moved in memory - then "crash"
        n2 = self.notifier()
        n2.start()
        n2.step()
        n2.step()
        dec = [t for t in self.tg.texts() if "تصمیم جدید Kimi" in t]
        self.assertEqual(len(dec), 1)

    def test_a_crash_after_telegram_accepted_resends_once_marked_as_possible_duplicate(self):
        n = self.notifier()
        n.start()
        n.step()
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60))
        n._scan_decisions(self.clock.t)
        item = n.st["outbox"][0]
        n.st["inflight"] = {"id": item["id"], "chat": CHAT, "chunk": 0}   # as written just before the send
        n.state.save()
        self.tg.send_message(CHAT, item["chunks"][0])                   # Telegram got it, then the process died
        n2 = self.notifier()
        n2.start()
        n2.step()
        dec = [t for t in self.tg.texts() if "تصمیم جدید Kimi" in t]
        self.assertEqual(len(dec), 2)
        self.assertIn("شاید تکراری", dec[1])
        n2.step()
        self.assertEqual(len([t for t in self.tg.texts() if "تصمیم جدید Kimi" in t]), 2)

    def test_broken_and_unknown_records_are_skipped_with_a_log_line(self):
        n = self.notifier()
        n.start()
        n.step()
        self.append("kimi_decisions.jsonl", '{"context_digest": "x", "decision": {"valid": tr')
        self.append("kimi_decisions.jsonl", "[1, 2, 3]")
        self.append("kimi_decisions.jsonl", {"hello": "world"})
        self.append("kimi_decisions.jsonl", {"decision": {"valid": "yes", "targets": "all", "decided_at": "soon"},
                                             "time": "x"})
        self.append("kimi_decisions.jsonl", {"decision": {"valid": True, "decided_at": T0 + 5, "targets": [1],
                                                          "confidence": "high", "reasoning": {"x": 1},
                                                          "next_review_hours": None, "new_field": {"a": 1}}})
        self.append("kimi_runner.jsonl", "not json at all")
        self.append("kimi_runner.jsonl", {"strange": True})
        self.append("kimi_runner.jsonl", runner_rec(T0 + 7, fills=[{"side": "buy"}, "x", fill("BTC_IRT", "buy", "0.1",
                                                                                              "1000", "0", "BTC", "7")]))
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60, digest="good" + "0" * 12))
        with self.assertLogs("bitpin.notify", level="WARNING") as cm:
            n.step()
        logs = "\n".join(cm.output)
        self.assertIn("broken JSON line skipped", logs)
        self.assertIn("unknown record skipped", logs)
        texts = self.tg.texts()
        self.assertTrue(any("good0000" in t for t in texts))
        self.assertTrue(any("💱" in t for t in texts))         # the one usable fill still arrives

    def test_repeated_invalid_decisions_of_the_same_kind_are_silent(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        for i, kind in enumerate(("llm", "llm", "llm", "validation")):
            self.append("kimi_decisions.jsonl", decision_rec(T0 + 1800 * (i + 1), digest="inv%013d" % i, valid=False,
                                                             error_kind=kind, error=kind + ": x"))
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 9000, digest="ok" + "0" * 14))
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 9900, digest="inv9" + "0" * 12, valid=False,
                                                         error_kind="llm", error="llm: x"))
        n.step()
        silent = [s for _, _, _, s in self.tg.sent]
        self.assertEqual(silent, [False, True, True, False, False, False])
        self.assertIn("تکرار ۳ از همین خطا", self.tg.sent[2][1])

    def test_rotation_does_not_resend_old_events(self):
        n = self.notifier()
        n.start()
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60, digest="first" + "0" * 11))
        n.step()
        path = self.p("kimi_decisions.jsonl")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(decision_rec(T0 - 10 * 3600, digest="ancient" + "0" * 9)) + "\n")
            f.write(json.dumps(decision_rec(T0 + 60, digest="first" + "0" * 11)) + "\n")
            f.write(json.dumps(decision_rec(T0 + 90, digest="fresh" + "0" * 11)) + "\n")
        os.replace(tmp, path)
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertNotIn("ancient0", joined)
        self.assertEqual(joined.count("first000"), 1)
        self.assertIn("fresh000", joined)


# --------------------------------------------------------------------------- fills

class TestFills(Base):
    def test_fills_of_a_cycle_are_grouped_and_linked_to_their_decision(self):
        n = self.notifier()
        n.start()
        n.step()
        t = T0 + 60
        self.append("kimi_decisions.jsonl", decision_rec(t, digest="cafe0000" * 2,
                                                         targets={"BTC_IRT": 0.25, "USDT_IRT": 0.25, "ETH_IRT": 0.0},
                                                         current={"ETH_IRT": 0.2, "USDT_IRT": 0.3}))
        self.add_trade(t + 5, "ETH_IRT", "sell", "0.001", "620000", "620000000", "2170", "IRT", "o-1")
        self.add_trade(t + 9, "BTC_IRT", "buy", "0.00005", "970000", "19400000000", "0.0000002", "BTC", "o-2")
        n.step()
        self.assertEqual(len(self.tg.sent), 2)              # the decision; the fills wait for their cycle record
        self.append("kimi_runner.jsonl", runner_rec(t - 30, decided_at=t, fills=[
            fill("ETH_IRT", "sell", "0.001", "620000", "2170", "IRT", "o-1"),
            fill("BTC_IRT", "buy", "0.00005", "970000", "2E-7", "BTC", "o-2"),
            fill("USDT_IRT", "buy", "10", "2300000", "0.04", "USDT", "o-3")],
            skipped=[["XRP_IRT", "below minimum order: 90,000 IRT < min_order_irt 100,000"]]))
        n.step()
        trades = [x for x in self.tg.texts() if "💱" in x]
        self.assertEqual(len(trades), 1)
        m = trades[0]
        for s in ("🔴 فروش", "🟢 خرید", "اتریوم (ETH)", "بیت‌کوین (BTC)", "تتر (USDT)", "<code>o-1</code>",
                  "<code>o-3</code>", "۶۲۰٬۰۰۰٬۰۰۰", "۰٫۰۰۰۰۰۰۲ BTC", "۲٬۱۷۰ تومان", "۳ سفارش",
                  "بر اساس: تصمیم Kimi ساعت", "cafe0000", "انجام‌نشده", "below minimum order"):
            self.assertIn(s, m)
        self.clock.t += 3600                               # nothing left over for the orphan flush
        n.step()
        self.assertEqual(len([x for x in self.tg.texts() if "💱" in x]), 1)
        n2 = self.notifier()
        n2.start()
        n2.step()
        self.assertEqual(len([x for x in self.tg.texts() if "💱" in x]), 1)

    def test_fills_without_a_cycle_record_are_sent_after_the_wait(self):
        n = self.notifier()
        n.start()
        n.step()
        self.add_trade(T0 + 5, "BTC_IRT", "sell", "0.0001", "1950000", "19500000000", "6825", "IRT", "o-9",
                       note="resolved later")
        n.step()
        self.assertFalse(any("💱" in x for x in self.tg.texts()))
        self.clock.t += 60
        self.append("kimi_runner.jsonl", runner_rec(self.clock.t, action="hold"))   # its cycle ended, not claimed
        n.step()
        self.assertFalse(any("💱" in x for x in self.tg.texts()))
        self.clock.t += 541
        n.step()
        m = [x for x in self.tg.texts() if "💱" in x]
        self.assertEqual(len(m), 1)
        self.assertIn("تسویهٔ بعدی", m[0])
        self.assertIn("در گزارش یک چرخه نیامده", m[0])

    def test_without_any_cycle_record_orphans_wait_two_cycles(self):
        n = self.notifier()
        n.start()
        n.step()
        self.add_trade(T0 + 5, "BTC_IRT", "sell", "0.0001", "1950000", "19500000000", "6825", "IRT", "o-9")
        n.step()
        self.clock.t += 3600                               # < 2 x 60-min cycle: the record may still come
        n.step()
        self.assertFalse(any("💱" in x for x in self.tg.texts()))
        self.clock.t += 3601
        n.step()
        self.assertEqual(len([x for x in self.tg.texts() if "💱" in x]), 1)

    def test_a_slow_cycle_is_one_message_not_an_orphan_plus_the_rest(self):
        """Bitpin answers slowly: the cycle's first fills are in the CSV at 12:01, its record comes at
        12:12 (after every order and retry). Before: the first fills went out as an 'orphan' group at
        12:11 and the rest at 12:12."""
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        t = T0 + 60
        self.add_trade(t + 5, "ETH_IRT", "sell", "0.001", "620000", "620000000", "2170", "IRT", "s-1")
        self.add_trade(t + 30, "SOL_IRT", "sell", "0.1", "900000", "9000000", "3150", "IRT", "s-2")
        for _ in range(44):                                # 11 minutes of 15-s steps, no record yet
            self.clock.t += 15
            n.step()
        self.assertEqual([x for x in self.tg.texts() if "💱" in x], [])
        self.add_trade(self.clock.t, "BTC_IRT", "buy", "0.0001", "900000", "9000000000", "0.0000002", "BTC", "s-3")
        self.append("kimi_runner.jsonl", runner_rec(t, decided_at=t - 30, fills=[
            fill("ETH_IRT", "sell", "0.001", "620000", "2170", "IRT", "s-1"),
            fill("SOL_IRT", "sell", "0.1", "900000", "3150", "IRT", "s-2"),
            fill("BTC_IRT", "buy", "0.0001", "900000", "0.0000002", "BTC", "s-3")]))
        self.clock.t += 15
        n.step()
        self.clock.t += 7200
        n.step()
        m = [x for x in self.tg.texts() if "💱" in x]
        self.assertEqual(len(m), 1)
        self.assertIn("۳ سفارش", m[0])
        self.assertNotIn("در گزارش یک چرخه نیامده", m[0])

    def test_skipped_plan_and_not_executed_decisions_are_reported(self):
        n = self.notifier()
        n.start()
        n.step()
        self.append("kimi_runner.jsonl", runner_rec(T0 + 60, action="kimi", decided_at=T0 + 50,
                                                    plan=["BUY SOL with 120,000 IRT"],
                                                    skipped=[["SOL_IRT", "price sanity: book mid deviates 6%"]]))
        self.append("kimi_runner.jsonl", runner_rec(T0 + 3660, action="expired", decided_at=T0 + 50))
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("برنامهٔ معاملهٔ این چرخه اجرا نشد", joined)
        self.assertIn("price sanity", joined)
        self.assertIn("مهلت اجرای آن تمام شده بود", joined)


# --------------------------------------------------------------------------- rendering

class TestRendering(Base):
    def view(self, **kw):
        return decision_view(decision_rec(T0 + 60, **kw))

    def test_formatting_helpers(self):
        self.assertEqual(notify.gregorian_to_jalali(2026, 9, 23), (1405, 7, 1))
        self.assertEqual(notify.gregorian_to_jalali(2026, 10, 22), (1405, 7, 30))
        self.assertEqual(notify.fmt_when(T0), "۱ مهر ۱۴۰۵، ساعت ۰۳:۳۰")
        self.assertEqual(notify.fmt_int(1234567), "۱٬۲۳۴٬۵۶۷")
        self.assertEqual(notify.fmt_pct(0.25), "۲۵٪")
        self.assertEqual(notify.fmt_pct(-0.0004, signed=True, digits=2), "-۰٫۰۴٪")
        self.assertEqual(notify.fmt_amount(0.00004321), "۰٫۰۰۰۰۴۳۲۱")
        self.assertEqual(notify.fmt_price(1.1312), "۱٫۱۳")
        for trig, want in (("first decision", "اولین تصمیم"), ("scheduled (2.0h since last decision)", "زمان‌بندی‌شده"),
                           ("event: BTC_IRT moved +3.1% (USDT terms) since last decision", "بیت‌کوین"),
                           ("event: drawdown 1.0% -> 4.0%", "افت سرمایه"),
                           ("next review requested by the model (1h; 1.2h since last decision)", "بازبینی"),
                           ("retry after invalid decision", "تلاش دوباره"), ("fallback: cash_sweep", "جایگزین"),
                           ("owner instruction", "دستور مالک"), ("something new", "something new")):
            self.assertIn(want, notify.trigger_fa(trig))

    def test_the_us_market_message_names_the_session_or_the_spread(self):
        r = Renderer(self.cfg())
        base = {"t": 1790000000.0, "kind": "session_closed", "symbol": "NVDAX_IRT", "coin": "NVDAX", "scope": "allocation"}
        kind, text, _ = r.event(notify.event_view(dict(base, reason="session", next_open=1790010000.0)))
        self.assertIn("بازار آمریکا بسته است", text)   # the US market is closed
        self.assertIn("نیویورک", text)      # New York hours, both seasons
        kind, text, _ = r.event(notify.event_view(dict(base, reason="spread")))
        self.assertIn("اسپرد بالای ۱٪", text)      # the spread above 1%
        self.assertNotIn("بسته است", text)

    def test_valid_rebalance_message(self):
        v = self.view(targets={"BTC_IRT": 0.25, "ETH_IRT": 0.0, "USDT_IRT": 0.7}, cash=0.05,
                      current={"ETH_IRT": 0.3, "USDT_IRT": 0.6})
        m = Renderer(self.cfg()).decision(v)
        for s in ("🧠 <b>تصمیم جدید Kimi</b> — بازچینش سبد", "۱ مهر ۱۴۰۵", "زمان‌بندی‌شده", "اطمینان مدل: ۶۲٪",
                  "ارزش حساب: ۳٬۸۰۰٬۰۰۰ تومان", "بیت‌کوین (BTC): <b>۲۵٪</b> (اکنون ۰٪، +۲۵٪)",
                  "اتریوم (ETH): <b>۰٪</b> (اکنون ۳۰٪، -۳۰٪)", "تومان نقد: <b>۵٪</b> (اکنون ۱۰٪، -۵٪)",
                  "🔴 <b>فروش:</b> اتریوم ۳۰٪ (~۱٬۱۴۰٬۰۰۰ تومان)", "🟢 <b>خرید:</b> بیت‌کوین ۲۵٪",
                  "💬 <b>دلیل:</b>", "📰 <b>اخبار:</b>", "⚠️ <b>ریسک‌ها:</b>", "بازبینی بعدی: حدود ۲ ساعت",
                  "<code>d1g3st00</code>"):
            self.assertIn(s, m)

    def test_hold_invalid_aborted_and_fallback_messages(self):
        r = Renderer(self.cfg())
        m = r.decision(self.view(hold=True))
        self.assertIn("نگه‌داری (بدون معامله)", m)
        self.assertIn("معامله‌ای انجام نمی‌شود", m)
        m = r.decision(self.view(valid=False, error_kind="validation", error="validation: targets sum to 1.4",
                                 news_ok=False))
        for s in ("تصمیم Kimi نامعتبر بود", "<code>validation</code>", "رد شد", "targets sum to 1.4",
                  "پژوهش اخبار در دسترس نبود"):
            self.assertIn(s, m)
        self.assertIn("لغو شد", r.decision(self.view(valid=False, error_kind="llm_aborted")))
        m = r.decision(self.view(fallback="cash_sweep", targets={"USDT_IRT": 0.95}, cash=0.05))
        self.assertIn("انتقال مازاد تومان به تتر", m)
        self.assertIn("هدف تتر: ۹۵٪", m)
        self.assertIn("کاهش ریسک خودکار", r.decision(self.view(fallback="derisk", targets={"USDT_IRT": 0.95})))

    def test_model_text_is_escaped_and_links_defanged(self):
        v = self.view(reasoning="<script>alert(1)</script> & <b>bold</b> see https://evil.example/phish?x=1 now")
        m = Renderer(self.cfg()).decision(v)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;", m)
        self.assertNotIn("<script>", m)
        self.assertIn("evil[.]example", m)
        self.assertNotIn("evil.example", m)
        self.assertNotIn("phish", m)

    @staticmethod
    def telegram_entities(text):
        """What Telegram's own entity finder (tdlib match_mentions / match_bot_commands / match_tg_urls,
        plus dotted hosts) would make tappable in `text` - an ORACLE written from Telegram's rules,
        deliberately not from the notifier's regexes: a mention is '@' + a name character when the
        character before '@' is not a Unicode letter / digit / '_' (so '@@name' IS a mention); a bot
        command is '/' + a name character unless the character before it is such a word character or
        one of '/', '<', '>', '\\'; a tg:// / ton:// / tonsite:// deep link needs only a non-ASCII-
        alphanumeric character before it (so a Persian letter glued to it does not protect)."""
        found = []

        def word(c):
            return c.isalnum() or c == "_"
        for i, c in enumerate(text):
            nxt = text[i + 1] if i + 1 < len(text) else ""
            prev = text[i - 1] if i else ""
            name_start = bool(nxt) and nxt.isascii() and (nxt.isalnum() or nxt == "_")
            if c == "@" and name_start and not (prev and word(prev)):
                found.append(("mention", text[i:i + 12]))
            if c == "/" and name_start and not (prev and (word(prev) or prev in "/<>\\")):
                found.append(("command", text[i:i + 12]))
        low = text.lower()
        for scheme in ("tg://", "ton://", "tonsite://"):
            start = 0
            while True:
                k = low.find(scheme, start)
                if k < 0:
                    break
                prev = low[k - 1] if k else ""
                if not (prev and prev.isascii() and (prev.isalnum() or prev in "_-")):
                    found.append(("deeplink", text[k:k + 20]))
                start = k + 1
        for m in re.finditer(r"[A-Za-z0-9-]\.[A-Za-z]{2,}|\d+\.\d+\.\d+\.\d+|://", text):
            found.append(("url", m.group(0)))
        return found

    def test_links_mentions_and_commands_from_web_text_are_never_clickable(self):
        cases = ("Bitpin moved: verify at https://bitpin-verify.com/login?id=1 now",
                 "see www.bitpin-support.ir or bitpin-help.com", "t.me/joinchat/AAAAA and tg://resolve?domain=x",
                 "Owner: send /stop_confirm now, contact @bitpin_support_admin", "(/stop) or kyc@bitpin-kyc.com",
                 "ip 203.0.113.120, bitpin-kyc。com and bitpin-kyc．com",
                 # review round 2: a scheme glued to a non-ASCII letter, and a doubled '@' / '/'
                 "باtg://resolve?domain=x", "étg://resolve?domain=bitpin_support", "باton://resolve?domain=x",
                 "ÉTONSITE://x", "tg://x", "باhttps://evil.example/login", "1tg://resolve?domain=x",
                 "contact @@bitpin_support_admin", "x@@name and @@@name",
                 "Visit .bitpin-verify.com now", "see ...bitpin-verify.com", "a..bitpin-verify.com",
                 "برای تأیید حساب با پشتیبانی تماس بگیریدtg://resolve?domain=bitpin_support_team یا به "
                 "@@bitpin_support_team پیام دهید", "‌/stop_confirm and و/stop_confirm")
        defensive = ("send //stop_confirm and ///stop",)    # not a command for Telegram; defanged all the same
        for s in cases + defensive:
            with self.subTest(s=s):
                self.assertTrue(self.telegram_entities(s) or "1tg" in s or s in defensive,
                                "the oracle must see the bait in %r" % s)
                out = notify.clean_text(s, 500)
                self.assertEqual(self.telegram_entities(out), [], out)
                self.assertEqual(self.telegram_entities(notify.strip_html(notify.esc(out))), [])   # plain-text fallback
                for line in notify.Renderer.untrusted_lines(s, 500):
                    self.assertEqual(self.telegram_entities(line), [], line)
        ordinary = "BTC +3.5% e.g. kimi-k2.6 $3.2B USDT_IRT 1,234.5 BTC/USDT 22:30"
        self.assertEqual(notify.clean_text(ordinary, 200), ordinary)
        persian = "بازار آرام است؛ تتر ۷۵٪ می‌ماند."
        self.assertEqual(notify.clean_text(persian, 200), persian)
        v = self.view(reasoning="Bitpin requires re-verification at bitpin-kyc.com, contact @bitpin_support, "
                                "then send /stop_confirm")
        m = Renderer(self.cfg()).decision(v)
        self.assertIn("bitpin-kyc[.]com", m)
        self.assertIn("@⁠bitpin_support", m)
        self.assertIn("/⁠stop_confirm", m)
        self.assertNotIn("stop_confirm", notify.strip_html(m).replace("/⁠stop_confirm", ""))
        self.assertEqual(self.telegram_entities(notify.strip_html(m)), [])
        self.assertIn("//", notify.clean_text("send //stop_confirm", 50))
        self.assertIn("//⁠stop_confirm", notify.clean_text("send //stop_confirm", 50))

    def test_long_messages_are_split_under_the_limit_with_balanced_tags(self):
        long_text = ("word & <tag> " * 1500)
        v = self.view(reasoning=long_text)
        m = Renderer(self.cfg(max_reasoning_chars=3500)).decision(v, full=True)
        big = "\n".join([m] * 4)
        chunks = split_message(big)
        self.assertGreater(len(chunks), 2)
        for c in chunks:
            self.assertLessEqual(len(c), 4096)
            for tag in ("b", "i", "code"):
                self.assertEqual(c.count("<%s>" % tag), c.count("</%s>" % tag), c[:200])
            self.assertNotRegex(c, r"&[a-z]*$")          # never cut inside an entity
        one = split_message("x" * 9000)
        self.assertTrue(all(len(c) <= 4096 for c in one))
        self.assertEqual("".join(c.split("\n<i>")[0] for c in one), "x" * 9000)

    def test_daily_summary_and_status_texts(self):
        n = self.notifier(self.cfg(mode="live"))
        n.state.load()
        self.append("kimi_decisions.jsonl", decision_rec(T0 - 86400, digest="s" * 16, usdt=220000.0))
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 600, digest="e" * 16, usdt=231000.0))
        self.append("kimi_runner.jsonl", runner_rec(T0 - 5 * 3600, equity_irt="3900000"))   # 22:30 Tehran, day before
        self.append("kimi_runner.jsonl", runner_rec(T0 + 3600, equity_irt="3950000"))
        self.write_json("kimi_equity.json", {"equity_start_irt": 3800000.0, "hwm_irt": 3950000.0})
        self.write_json("kimi_budget.json", {"2026-09-23": {"calls": 12, "total_tokens": 480000}})
        self.write_json("news_budget.json", {"2026-09-23": {"calls": 6, "total_tokens": 150000}})
        self.write_json("risk_state_live.json", {"halted": False, "hwm": "3950000"})
        self.write_json("live_orders.json", {"orders": {}})
        self.add_trade(T0 + 3000, "BTC_IRT", "buy", "0.00005", "970000", "19400000000", "0.0000002", "BTC", "o-1")
        self.add_trade(T0 + 3100, "ETH_IRT", "sell", "0.001", "620000", "620000000", "2170", "IRT", "o-2")
        self.add_trade(T0 - 5 * 3600, "ETH_IRT", "sell", "0.001", "620000", "620000000", "2170", "IRT", "o-0")
        n._scan_decisions(T0 + 700)
        self.clock.t = T0 + 20 * 3600 + 60
        s = n.summary_text(self.clock.t)
        for x in ("خلاصهٔ روزانه — ۱ مهر ۱۴۰۵", "بازه: از ۳۱ شهریور ۱۴۰۵، ساعت ۲۳:۳۱", "۳٬۹۵۰٬۰۰۰ تومان",
                  "از شروع: +۳٫۹۵٪", "در این بازه: +۱٫۲۸٪", "نگه‌داشتن تتر", "۲۲۰٬۰۰۰ در ۳۱ شهریور ۱۴۰۵ ← ۲۳۱٬۰۰۰",
                  "معاملات این بازه: ۲ سفارش", "خرید ۹۷۰٬۰۰۰", "فروش ۶۲۰٬۰۰۰", "کارمزد ~۶٬۰۵۰ تومان",
                  "تصمیم ۱۲ فراخوان / ۴۸۰٬۰۰۰ توکن", "اخبار ۶ / ۱۵۰٬۰۰۰", "سلامت"):
            self.assertIn(x, s)
        st = n.status_text(self.clock.t)
        for x in ("وضعیت ربات", "۳٬۹۵۰٬۰۰۰", "ترکیب سبد", "تتر (USDT): ۵۰٪", "آخرین تصمیم", "سفارش نامعلوم: ۰"):
            self.assertIn(x, st)


# --------------------------------------------------------------------------- commands and /stop

def upd(uid, chat, text, date):
    return {"update_id": uid, "message": {"message_id": uid, "date": int(date), "text": text,
                                          "chat": {"id": chat, "type": "private", "username": "u%d" % chat}}}


class TestCommands(Base):
    def setUp(self):
        super().setUp()
        self.n = self.notifier(chats=(CHAT, CHAT2))
        self.n.start()
        self.n.step()
        self.tg.sent.clear()

    def poll(self, *updates):
        self.tg.updates = list(updates)
        self.n.poll_commands(wait=1)

    def test_only_configured_chats_are_answered(self):
        self.poll(upd(1, OTHER, "/status", self.clock.t), upd(2, OTHER, "/stop", self.clock.t),
                  upd(3, OTHER, "/stop_confirm", self.clock.t))
        self.assertEqual(self.tg.sent, [])
        self.assertIsNone(self.n.st["pending_stop"])
        self.assertIn(str(OTHER), self.n.st["unknown_chats"])
        self.assertEqual(self.n.st["update_offset"], 4)
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))

    def test_status_help_last_and_unknown(self):
        self.append("kimi_decisions.jsonl", decision_rec(T0 - 60, digest="last" + "0" * 12))
        self.poll(upd(1, CHAT, "/status", self.clock.t), upd(2, CHAT, "/help@MyBot", self.clock.t),
                  upd(3, CHAT, "/last", self.clock.t), upd(4, CHAT, "rm -rf / ; /status", self.clock.t),
                  upd(5, CHAT, "/STATUS extra args", self.clock.t))
        t = self.tg.texts(CHAT)
        self.assertEqual(len(t), 5)
        self.assertIn("وضعیت ربات", t[0])
        self.assertIn("/stop", t[1])
        self.assertIn("متن اصلی", t[2] + "متن اصلی")
        self.assertIn("last0000", t[2])
        self.assertIn("دستور شناخته نشد", t[3])
        self.assertIn("وضعیت ربات", t[4])
        self.assertEqual(self.tg.offsets[-1], None)
        self.poll()
        self.assertEqual(self.tg.offsets[-1], 6)

    def test_commands_sent_while_the_notifier_was_off_are_not_executed_but_answered(self):
        self.poll(upd(1, CHAT, "/stop", self.clock.t - 3600), upd(2, CHAT, "/stop_confirm", self.clock.t - 3590))
        self.assertEqual(len(self.tg.sent), 1)                  # one reply per chat and poll, not one per command
        self.assertIn("اجرا نشد", self.tg.texts(CHAT)[0])
        self.assertIn("ربات متوقف نشد", self.tg.texts(CHAT)[0])
        self.assertIsNone(self.n.st["pending_stop"])
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))

    def test_commands_are_judged_by_the_start_of_the_notifier_not_by_its_delays(self):
        sent_at = self.clock.t + 5
        self.clock.t += 300                                     # the notifier was busy for 5 minutes
        self.poll(upd(1, CHAT, "/stop", sent_at))
        self.assertIn("stop_confirm", self.tg.texts(CHAT)[-1])
        self.clock.t += 200                                     # busy again: confirmation sent 8 s after /stop
        self.poll(upd(2, CHAT, "/stop_confirm", sent_at + 8))
        self.assertTrue(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))

    def test_the_confirmation_window_is_measured_between_the_two_messages(self):
        self.poll(upd(1, CHAT, "/stop", self.clock.t))
        self.poll(upd(2, CHAT, "/stop_confirm", self.clock.t + 70))   # handled at once, but sent 70 s later
        self.assertIn("مهلت تأیید تمام شده", self.tg.texts(CHAT)[-1])
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))

    def test_commands_only_in_a_private_chat_and_never_for_another_bot(self):
        n = self.notifier(chats=(CHAT, -100555))
        n.start()
        self.tg.sent.clear()
        grp = {"id": -100555, "type": "supergroup", "title": "family"}
        self.tg.updates = [{"update_id": 1, "message": {"date": int(self.clock.t), "text": "/stop", "chat": grp,
                                                        "from": {"id": 424242}}},
                           {"update_id": 2, "message": {"date": int(self.clock.t), "text": "/stop_confirm", "chat": grp,
                                                        "from": {"id": 424242}}}]
        n.poll_commands(wait=1)
        self.assertIsNone(n.st["pending_stop"])
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))
        self.assertEqual(len(self.tg.texts(-100555)), 1)        # one refusal, rate-limited
        self.assertIn("فقط در چت خصوصی", self.tg.texts(-100555)[0])
        self.tg.sent.clear()
        other = {"update_id": 3, "message": {"date": int(self.clock.t), "text": "/status",
                                             "chat": {"id": CHAT, "type": "private"}, "from": {"id": 5}}}
        self.tg.updates = [upd(4, CHAT, "/stop@OtherBot", self.clock.t), other]
        n.poll_commands(wait=1)
        self.assertEqual(self.tg.sent, [])
        self.tg.updates = [upd(5, CHAT, "/status@MyBot", self.clock.t)]
        n.poll_commands(wait=1)
        self.assertIn("وضعیت ربات", self.tg.texts(CHAT)[-1])
        with self.assertLogs("bitpin.notify", level="WARNING") as cm:
            notify.load_secrets({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "-100555"})
        self.assertIn("group", "\n".join(cm.output))

    def test_stop_needs_confirmation_and_the_relay_creates_the_kill_switch(self):
        self.poll(upd(1, CHAT, "/stop", self.clock.t))
        self.assertIn("stop_confirm", self.tg.texts(CHAT)[-1])
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))
        self.clock.t += 30
        self.poll(upd(2, CHAT, "/stop_confirm", self.clock.t))
        req = os.path.join(self.nd, notify.STOP_REQUEST_FILE)
        self.assertTrue(os.path.exists(req))
        with open(req, encoding="utf-8") as f:
            r = json.load(f)
        self.assertEqual((r["kind"], r["chat_id"]), ("stop", CHAT))
        self.assertFalse(os.path.exists(self.p("STOP")))       # the notifier itself never writes there
        ok, detail = apply_stop_request(self.sd, self.nd, clock=self.clock)
        self.assertTrue(ok, detail)
        self.assertTrue(os.path.exists(self.p("STOP")))
        self.assertFalse(os.path.exists(req))
        with open(self.p("STOP"), encoding="utf-8") as f:
            self.assertIn("Telegram /stop", f.read())
        self.n.step()
        joined = "\n".join(self.tg.texts(CHAT))
        self.assertIn("کلید قطع STOP فعال شد", joined)
        self.assertEqual(joined.count("کلید قطع STOP"), 1)      # no second, generic STOP alert
        self.assertIn("کلید قطع STOP فعال شد", "\n".join(self.tg.texts(CHAT2)))   # every owner chat is told
        self.assertIsNone(self.n.st["stop_verify"])
        ok, detail = apply_stop_request(self.sd, self.nd, clock=self.clock)
        self.assertEqual(detail, "no stop request")

    def test_stop_confirmation_expires_and_must_come_from_the_same_chat(self):
        self.poll(upd(1, CHAT, "/stop_confirm", self.clock.t))
        self.assertIn("اول /stop", self.tg.texts(CHAT)[-1])
        self.poll(upd(2, CHAT, "/stop", self.clock.t))
        self.poll(upd(3, CHAT2, "/stop_confirm", self.clock.t + 5))
        self.assertIn("اول /stop", self.tg.texts(CHAT2)[-1])
        self.clock.t += 61
        self.poll(upd(4, CHAT, "/stop_confirm", self.clock.t))
        self.assertIn("مهلت تأیید تمام شده", self.tg.texts(CHAT)[-1])
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))

    def test_without_the_relay_the_owner_is_told_the_bot_was_not_stopped(self):
        self.poll(upd(1, CHAT, "/stop", self.clock.t), upd(2, CHAT, "/stop_confirm", self.clock.t + 2))
        self.clock.t += 50
        self.n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("فایل STOP ساخته نشد", joined)
        self.assertIn("sudo bitpin-bot stop", joined)
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))

    def test_stop_can_be_disabled(self):
        n = self.notifier(self.cfg(allow_stop_command=False))
        n.start()
        self.tg.updates = [upd(1, CHAT, "/stop", self.clock.t), upd(2, CHAT, "/stop_confirm", self.clock.t)]
        n.poll_commands(wait=1)
        self.assertIn("خاموش است", self.tg.texts(CHAT)[-1])
        self.assertIsNone(n.st["pending_stop"])
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))

    def test_relay_ignores_stale_or_invalid_requests(self):
        notify.write_stop_request(self.nd, {"kind": "stop", "request_id": "r1", "time": self.clock.t - 3600,
                                            "chat_id": CHAT})
        ok, detail = apply_stop_request(self.sd, self.nd, max_age=300, clock=self.clock)
        self.assertFalse(ok)
        self.assertIn("stale", detail)
        self.assertFalse(os.path.exists(self.p("STOP")))
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))
        notify.write_stop_request(self.nd, {"kind": "resume", "time": self.clock.t})
        self.assertFalse(apply_stop_request(self.sd, self.nd, clock=self.clock)[0])
        with open(self.p("STOP"), "w") as f:
            f.write("created by run_bot.py stop\n")
        notify.write_stop_request(self.nd, {"kind": "stop", "request_id": "r2", "time": self.clock.t})
        ok, detail = apply_stop_request(self.sd, self.nd, clock=self.clock)
        self.assertTrue(ok)
        self.assertEqual(detail, "STOP already present")
        with open(self.p("STOP")) as f:
            self.assertIn("run_bot.py stop", f.read())                  # never overwritten

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_relay_never_follows_a_symlink(self):
        target = os.path.join(self.tmp, "elsewhere")
        os.symlink(target, self.p("STOP"))
        notify.write_stop_request(self.nd, {"kind": "stop", "request_id": "r3", "time": self.clock.t})
        ok, detail = apply_stop_request(self.sd, self.nd, clock=self.clock)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(detail, "STOP already present")

    def test_getupdates_conflict_is_survived(self):
        def boom(offset=None, timeout=0):
            raise TelegramError("Conflict: terminated by other getUpdates request", status=409)
        self.tg.get_updates = boom
        self.n.poll_commands(wait=1)
        self.n.poll_commands(wait=1)
        self.assertGreater(self.n._next_updates, self.clock.t - 1)


# --------------------------------------------------------------------------- alerts

class TestAlerts(Base):
    def test_heartbeat_alert_and_recovery(self):
        n = self.notifier(fresh=False)
        n.start()
        self.append("kimi_runner.jsonl", runner_rec(T0 - 300, cycle_min=30))
        n.step()
        os.utime(self.p("kimi_runner.jsonl"), (T0 - 300, T0 - 300))
        self.tg.sent.clear()
        self.clock.t = T0 + 30 * 60
        n.step()
        self.assertEqual(self.tg.sent, [])                   # 35 min < 2.5 x 30 min
        self.clock.t = T0 + 75 * 60
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("ربات مدتی است فعالیتی ثبت نکرده", joined)
        self.assertIn("چرخهٔ ربات ۳۰ دقیقه", joined)
        self.assertIn("sudo bitpin-bot health", joined)
        n.step()
        self.assertEqual(len(self.tg.sent), 1)
        self.clock.t += 7 * 3600                              # still down: repeated after alert_repeat_minutes
        n.step()
        self.assertEqual(len(self.tg.sent), 2)
        with open(self.p("STOP"), "w") as f:                  # now stopped ON PURPOSE: the STOP alert, no repeats
            f.write("x")
        n.step()
        self.clock.t += 7 * 3600
        n.step()
        texts = self.tg.texts()
        self.assertEqual(len(texts), 3)
        self.assertIn("🛑", texts[2])
        os.remove(self.p("STOP"))
        os.utime(self.p("kimi_runner.jsonl"), (self.clock.t, self.clock.t))
        n.step()
        joined = "\n".join(self.tg.texts()[3:])
        self.assertIn("ربات دوباره فعال است", joined)
        self.assertIn("کلید قطع STOP برداشته شد", joined)

    def test_heartbeat_names_the_stop_file_as_the_reason(self):
        n = self.notifier(fresh=False)
        n.start()
        self.append("bot_live.log", "2026-09-23 00:00:00 INFO    next check at ...")
        os.utime(self.p("bot_live.log"), (T0 - 4 * 3600, T0 - 4 * 3600))
        with open(self.p("STOP"), "w") as f:
            f.write("x")
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("ربات مدتی است فعالیتی ثبت نکرده", joined)
        self.assertIn("(ربات عمداً متوقف شده)", joined)
        self.assertIn("چرخهٔ ربات ۶۰ دقیقه", joined)          # no cycle_min record yet: 60 min

    def test_stop_and_halt_alerts_once_and_recovery(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        with open(self.p("STOP"), "w") as f:
            f.write("x")
        self.write_json("risk_state_live.json", {"halted": True, "halt_reason": "drawdown 31.0% from high-water mark",
                                                 "halted_at": T0 + 10})
        n.step()
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertEqual(joined.count("کلید قطع STOP فعال است"), 1)
        self.assertEqual(joined.count("حد ضرر (drawdown) فعال شد"), 1)
        self.assertIn("drawdown 31.0%", joined)
        os.remove(self.p("STOP"))
        self.write_json("risk_state_live.json", {"halted": False})
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("کلید قطع STOP برداشته شد", joined)
        self.assertIn("توقف حد ضرر برداشته شد", joined)

    def test_kimi_and_news_outage_alerts(self):
        n = self.notifier()
        n.start()
        self.write_json("kimi_brain_state.json", {"invalid_since": T0 - 30 * 60, "invalid_count": 2,
                                                  "last_decision": {"error_kind": "llm", "valid": False}})
        self.write_json("news_cache.json", {"brief": {"fetched_at": T0 - 5 * 3600}, "last_error": "network error",
                                            "last_error_at": T0 - 60})
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertNotIn("Kimi مدتی است", joined)              # 30 min < 120 min
        self.assertIn("پژوهش اخبار مدتی است کار نمی‌کند", joined)
        self.clock.t += 2 * 3600
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertEqual(joined.count("Kimi مدتی است تصمیم معتبری نداده"), 1)
        self.assertIn("خطای شبکه یا سرویس Kimi", joined)
        self.write_json("kimi_brain_state.json", {"invalid_since": None, "last_valid_at": self.clock.t})
        self.write_json("news_cache.json", {"brief": {"fetched_at": self.clock.t}, "last_error": "",
                                            "last_error_at": None})
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("Kimi دوباره تصمیم معتبر داد", joined)
        self.assertIn("پژوهش اخبار دوباره کار می‌کند", joined)

    def test_unknown_order_alert_escalation_and_resolution(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        j = {"orders": {"ident-A": {"created_at": T0 - 60, "side": "buy", "status": "unknown", "symbol": "BTC_IRT"},
                        "ident-B": {"created_at": T0 - 600, "side": "sell", "status": "closed", "symbol": "ETH_IRT"}}}
        self.write_json("live_orders.json", j)
        n.step()
        self.assertEqual(self.tg.sent, [])                   # younger than unknown_order_alert_minutes
        self.clock.t += 6 * 60
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertEqual(joined.count("سفارش با نتیجهٔ نامعلوم"), 1)
        self.assertIn("ident-A", joined)
        self.assertNotIn("ident-B", joined)
        j["orders"]["ident-A"].update(lookup_failures=3, last_lookup_error="HTTP 500")
        self.write_json("live_orders.json", j)
        n.step()
        self.assertNotIn("روشن نشده", "\n".join(self.tg.texts()))     # 3 failures: the bot is still on it
        j["orders"]["ident-A"].update(lookup_failures=notify.STUCK_LOOKUP_FAILURES)
        self.write_json("live_orders.json", j)
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("نتیجهٔ سفارش مدتی است روشن نشده", joined)
        self.assertIn("resolve-order", joined)
        self.assertIn("ORDER UNRESOLVED", joined)                  # manual steps only if the bot says so
        self.assertIn("فقط اگر چنین خطی آمده بود", joined)
        os.remove(self.p("live_orders.json"))                # unreadable journal: nothing is assumed resolved
        n.step()
        self.assertNotIn("مشخص شد", "\n".join(self.tg.texts()))
        j["orders"]["ident-A"]["status"] = "closed"
        self.write_json("live_orders.json", j)
        n.step()
        self.assertIn("وضعیت سفارش <code>ident-A</code> مشخص شد: <code>closed</code>", self.tg.texts()[-1])


# --------------------------------------------------------------------------- daily summary scheduling

class TestDailySummary(Base):
    def test_summary_once_per_day_at_the_configured_tehran_time(self):
        self.clock.t = T0 + 19 * 3600 + 58 * 60              # 23:28 Tehran
        n = self.notifier(self.cfg(daily_summary_time="23:30"))
        n.start()
        n.step()
        self.assertFalse(any("خلاصهٔ روزانه —" in t for t in self.tg.texts()))
        self.clock.t = T0 + 20 * 3600 - 60                   # 23:29
        n.step()
        self.assertFalse(any("خلاصهٔ روزانه —" in t for t in self.tg.texts()))
        self.clock.t = T0 + 20 * 3600 + 60                   # 23:31
        n.step()
        n.step()
        self.assertEqual(sum("خلاصهٔ روزانه — ۱ مهر ۱۴۰۵" in t for t in self.tg.texts()), 1)
        n2 = self.notifier(self.cfg(daily_summary_time="23:30"))
        n2.start()
        n2.step()
        self.assertEqual(sum("خلاصهٔ روزانه —" in t for t in self.tg.texts()), 1)
        self.clock.t += 86400
        n2.step()
        self.assertEqual(sum("خلاصهٔ روزانه — ۲ مهر ۱۴۰۵" in t for t in self.tg.texts()), 1)

    def test_first_start_after_the_summary_time_waits_for_the_next_day(self):
        self.clock.t = T0 + 20 * 3600 + 600                  # 23:40 Tehran
        n = self.notifier(self.cfg(daily_summary_time="23:30"))
        n.start()
        n.step()
        self.assertFalse(any("خلاصهٔ روزانه —" in t for t in self.tg.texts()))


# --------------------------------------------------------------------------- translation fallback chain

class FakeKimi:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body.decode("utf-8")), timeout))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            return r
        return 200, {}, json.dumps({"choices": [{"message": {"content": r}}],
                                    "usage": {"total_tokens": 900}}).encode()


class TestTranslation(Base):
    def translator(self, replies, **over):
        cfg = self.cfg(translation=dict({"max_calls_per_day": 5}, **over))["translation"]
        k = FakeKimi(replies)
        return Translator(cfg, KIMI_KEY, "http://127.0.0.1:1081", self.nd, transport=k, clock=self.clock), k

    def test_report_fa_in_the_decision_wins_without_a_call(self):
        tr, k = self.translator([FA_PERSIAN])
        n = self.notifier(translator=tr)
        for rec in (decision_rec(T0, report_fa=FA_PERSIAN),
                    decision_rec(T0, response=json.dumps({"targets": {}, "report_fa": FA_PERSIAN}))):
            m = n.decision_message(decision_view(rec))
            self.assertIn("توضیح Kimi", m)
            self.assertIn("فدرال رزرو", m)
            self.assertNotIn("💬", m)
        self.assertEqual(k.calls, [])
        v = decision_view(decision_rec(T0, report_fa="just English, not Persian at all here"))
        self.assertIsNone(v["report_fa"])

    def test_translation_is_used_cached_and_sent_through_the_kimi_proxy(self):
        tr, k = self.translator([FA_PERSIAN])
        n = self.notifier(translator=tr)
        v = decision_view(decision_rec(T0 + 60))
        m1 = n.decision_message(v)
        m2 = n.decision_message(v)
        self.assertEqual(len(k.calls), 1)
        self.assertIn("ترجمهٔ خودکار", m1)
        self.assertIn("فدرال رزرو", m1)
        self.assertEqual(m1, m2)
        url, headers, body, timeout = k.calls[0]
        self.assertTrue(url.startswith("https://api.moonshot.ai/v1/chat/completions"))
        self.assertEqual(body["model"], "kimi-k2.6")
        self.assertNotIn("temperature", body)
        self.assertIn("Reasoning: Keep most in USDT_IRT", body["messages"][1]["content"])
        self.assertLessEqual(timeout, 45)
        with open(os.path.join(self.nd, notify.TRANSLATIONS_FILE), encoding="utf-8") as f:
            self.assertNotIn(KIMI_KEY, f.read())
        real = Translator(self.cfg()["translation"], KIMI_KEY, "http://127.0.0.1:1081", self.nd)
        self.assertEqual(real.transport.proxy, "http://127.0.0.1:1081")
        self.assertNotIn(KIMI_KEY, repr(real))

    def test_failures_fall_back_to_english(self):
        cases = (("timeout", [socket.timeout("slow"), socket.timeout("slow")]),
                 ("http error", [(401, {}, b'{"error": "bad key"}')]),
                 ("not persian", ["Sure! Here is the translation: keep USDT."]),
                 ("garbage", [(200, {}, b"not json")]))
        for name, replies in cases:
            with self.subTest(name):
                tr, k = self.translator(replies)
                n = self.notifier(translator=tr)
                m = n.decision_message(decision_view(decision_rec(T0 + 60, digest=name[:4] + "0" * 12)))
                self.assertIn("💬 <b>دلیل:</b> Keep most in USDT_IRT", m)
                self.assertNotIn("ترجمهٔ خودکار", m)
                m = n.decision_message(decision_view(decision_rec(T0 + 90, digest="z" * 16)))
                self.assertIn("💬", m)
                self.assertLessEqual(len(k.calls), 2)       # paused after the failure: no call for the next one

    def test_daily_budget(self):
        tr, k = self.translator([FA_PERSIAN, FA_PERSIAN], max_calls_per_day=1)
        n = self.notifier(translator=tr)
        n._tr_budget = 5                               # the per-step limit is not what this test is about
        self.assertIn("ترجمهٔ خودکار", n.decision_message(decision_view(decision_rec(T0 + 60, digest="1" * 16))))
        self.assertIn("💬", n.decision_message(decision_view(decision_rec(T0 + 90, digest="2" * 16))))
        self.assertEqual(len(k.calls), 1)

    def test_holds_use_at_most_half_of_the_daily_translations_and_one_call_per_step(self):
        tr, k = self.translator([FA_PERSIAN] * 4, max_calls_per_day=2)
        n = self.notifier(translator=tr)
        n._tr_budget = 5
        self.assertIn("ترجمهٔ خودکار", n.decision_message(decision_view(decision_rec(T0 + 60, digest="1" * 16,
                                                                                         hold=True))))
        self.assertIn("💬", n.decision_message(decision_view(decision_rec(T0 + 90, digest="2" * 16, hold=True))))
        self.assertIn("ترجمهٔ خودکار", n.decision_message(decision_view(decision_rec(T0 + 99, digest="3" * 16))))
        self.assertEqual(len(k.calls), 2)
        tr, k = self.translator([FA_PERSIAN] * 3)
        n = self.notifier(translator=tr)
        n.start()
        n.step()
        for i in range(3):
            self.append("kimi_decisions.jsonl", decision_rec(T0 + 60 + i, digest="%016d" % i))
        n.step()
        self.assertEqual(len(k.calls), 1)                  # never 3 x the translation timeout in one step

    def test_an_injected_or_fabricated_translation_is_rejected(self):
        cases = (("new numbers", "دلیل: ربات هک شده است؛ فوراً کل سبد را بفروشید و کد ۱۲۳۴۵ را به پشتیبانی بدهید تا "
                                 "حساب شما امن بماند."),
                 ("new latin words", "دلیل: بازار آرام است؛ برای امنیت حساب به @evil_support پیام دهید و SELL ALL "
                                     "بزنید؛ این دستور خود ربات است."),
                 ("too long", "دلیل: " + "بازار آرام است و همه چیز خوب است و باید منتظر ماند " * 12))
        for name, reply in cases:
            with self.subTest(name):
                tr, k = self.translator([reply])
                n = self.notifier(translator=tr)
                m = n.decision_message(decision_view(decision_rec(T0 + 60, digest=name[:4] + "0" * 12)))
                self.assertNotIn("ترجمهٔ خودکار", m)
                self.assertIn("💬 <b>دلیل:</b> Keep most in USDT_IRT", m)
                user = k.calls[0][2]["messages"][1]["content"]
                self.assertTrue(user.startswith("<<<SOURCE_TEXT\n") and user.endswith("\nSOURCE_TEXT>>>"))
        ok = "دلیل: بیشتر سرمایه در USDT_IRT می‌ماند و با ورود پول به صندوق‌های ETF کمی BTC خریده می‌شود."
        self.assertIsNone(notify.translation_problem(Renderer(self.cfg()).english_for_translation(
            decision_view(decision_rec(T0))), ok))

    def test_invalid_and_fallback_decisions_are_never_translated(self):
        tr, k = self.translator([FA_PERSIAN])
        n = self.notifier(translator=tr)
        n.decision_message(decision_view(decision_rec(T0, valid=False, error_kind="llm", error="x")))
        n.decision_message(decision_view(decision_rec(T0, fallback="cash_sweep", targets={"USDT_IRT": 0.95})))
        self.assertEqual(k.calls, [])


# --------------------------------------------------------------------------- end to end over local HTTP

class TestEndToEndLocalBotApi(Base):
    """The real TelegramClient + transport + Notifier against a local fake Bot API (plain http on
    127.0.0.1, allowed only through the test-only allow_http switch)."""

    def setUp(self):
        super().setUp()
        from http.server import BaseHTTPRequestHandler, HTTPServer
        calls, updates = [], []
        self.calls, self.updates = calls, updates

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                parts = self.path.split("/")
                ok = len(parts) == 3 and parts[1] == "bot" + TOKEN
                method = parts[2] if ok else "?"
                calls.append((method, body))
                if not ok:
                    res = (404, {"ok": False, "error_code": 404, "description": "Not Found"})
                elif method == "getUpdates":
                    ups = [u for u in updates if u["update_id"] >= (body.get("offset") or 0)]
                    res = (200, {"ok": True, "result": ups})
                else:
                    res = (200, {"ok": True, "result": {"message_id": len(calls)}})
                raw = json.dumps(res[1]).encode()
                self.send_response(res[0])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def test_messages_and_commands_over_http(self):
        client = TelegramClient(TOKEN, api_base="http://127.0.0.1:%d" % self.srv.server_address[1], timeout=5,
                                max_retries=0, allow_http=True)
        n = Notifier(self.cfg(), Secrets(TOKEN, [CHAT]), telegram=client, clock=self.clock, sleep=self.clock.sleep)
        n.activity = lambda: self.clock.t
        n.start()
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60, digest="e2e0" + "0" * 12))
        n.step()
        sends = [b for m, b in self.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 2)
        self.assertEqual({b["chat_id"] for b in sends}, {CHAT})
        self.assertTrue(all(b["parse_mode"] == "HTML" for b in sends))
        self.assertIn("e2e00000", sends[1]["text"])
        self.assertTrue(sends[0]["disable_notification"])        # the start message is silent
        self.updates += [upd(7, OTHER, "/status", self.clock.t), upd(8, CHAT, "/status", self.clock.t)]
        n.poll_commands(wait=0)
        replies = [b for m, b in self.calls if m == "sendMessage"][2:]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["chat_id"], CHAT)
        self.assertIn("وضعیت ربات", replies[0]["text"])
        n.poll_commands(wait=0)
        gu = [b for m, b in self.calls if m == "getUpdates"]
        self.assertEqual(gu[-1]["offset"], 9)
        self.assertEqual(gu[-1]["allowed_updates"], ["message"])
        self.assertEqual(len([b for m, b in self.calls if m == "sendMessage"]), 3)


# --------------------------------------------------------------------------- CLI

class TestCli(Base):
    def setUp(self):
        super().setUp()
        root = logging.getLogger()
        saved = (list(root.handlers), root.level)

        def restore():                                  # main() installs its own root handler
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in saved[0]:
                root.addHandler(h)
            root.setLevel(saved[1])
        self.addCleanup(restore)

    def test_dry_run_renders_without_a_token(self):
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60))
        self.append("kimi_runner.jsonl", runner_rec(T0 + 30, decided_at=T0 + 60, fills=[
            fill("BTC_IRT", "buy", "0.00005", "970000", "2E-7", "BTC", "o-2")]))
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import notify_bot
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.stdout", out):
            rc = notify_bot.main(["dry-run", "--state-dir", self.sd, "--last", "2", "--at", "2026-09-23 23:31",
                                  "--notify-state-dir", self.nd, "--log-level", "ERROR"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("تصمیم جدید Kimi", text)
        self.assertIn("معاملات انجام‌شده", text)
        self.assertIn("خلاصهٔ روزانه", text)
        self.assertIn("وضعیت ربات", text)
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.STATE_FILE)))   # nothing written

    def test_run_refuses_to_start_without_secrets_with_exit_78(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import notify_bot
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}, clear=True):
            rc = notify_bot.main(["run", "--state-dir", self.sd, "--notify-state-dir", self.nd, "--once",
                                  "--log-level", "CRITICAL"])
        self.assertEqual(rc, notify.EXIT_CONFIG)


# --------------------------------------------------------------------------- review fixes (regressions)

class TestBenchmarkStart(Base):
    def history(self, days=10, first_extra=0):
        t_start = T0 - days * 86400
        n = days * 24
        for i in range(n):
            resp = ("y" * first_extra) if i == 0 else "x" * 6000       # a raw Kimi reply per record
            self.append("kimi_decisions.jsonl", decision_rec(t_start + i * 3600, digest="%016x" % i,
                                                             usdt=100000.0 + 30000.0 * i / n, response=resp))
        return t_start

    def test_the_usdt_start_is_the_first_decision_even_with_a_long_history(self):
        t_start = self.history(first_extra=300 * 1024)        # the first line is longer than 256 KB
        self.write_json("kimi_equity.json", {"equity_start_irt": 10000000})
        self.append("kimi_runner.jsonl", runner_rec(T0 - 60, equity_irt="11000000"))
        n = self.notifier(self.cfg(daily_summary_time="23:30"))
        n.start()
        self.assertEqual((n.st["usdt_start"]["px"], n.st["usdt_start"]["t"]), (100000.0, t_start))
        s = notify.strip_html(n.summary_text(T0 + 20 * 3600))
        self.assertIn("قیمت تتر: ۱۰۰٬۰۰۰ در %s" % notify.fmt_date(t_start), s)
        self.assertIn("عقب‌تر", s)                           # +10 % against ~+30 % of holding USDT

    def test_a_start_price_of_an_older_version_is_repaired(self):
        self.history(days=2)
        n = self.notifier()
        n.start()
        n.st["usdt_start"] = {"px": 123958.3, "t": T0 - 3600}  # chosen by the old code from the tail window
        n.state.save()
        n2 = self.notifier()
        n2.start()
        self.assertEqual(n2.st["usdt_start"]["px"], 100000.0)


class TestRobustRecords(Base):
    def test_a_bad_record_never_stops_the_reading_of_a_file(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        for bad in ({"unexecutable": 2}, {"fills": 5}, {"skipped": True}, {"plan": 3, "errors": "x"},
                    {"stale_prices": 7}, {"decision": {"decided_at": 1.79e12, "valid": True}}):
            rec = runner_rec(T0 + 60)
            rec.update(bad)
            self.append("kimi_runner.jsonl", rec)
        deep = "[" * 100000 + "]" * 100000                # json raises RecursionError, not ValueError
        self.append("kimi_runner.jsonl", deep)
        self.append("kimi_runner.jsonl", runner_rec(T0 + 1800, decided_at=T0 + 1700, fills=[
            fill("BTC_IRT", "buy", "0.0001", "950000", "0.0000003", "BTC", "7001")]))
        self.append("kimi_decisions.jsonl", deep)
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 100, digest="deep" + "0" * 12,
                                                         response='x {"report_fa": ' + "[" * 50000 + "]" * 50000))
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 200, digest="after" + "0" * 11))
        with self.assertLogs("bitpin.notify", level="WARNING"):
            n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("<code>7001</code>", joined)
        self.assertIn("deep0000", joined)
        self.assertIn("after000", joined)
        for key, name in (("runner", "kimi_runner.jsonl"), ("decisions", "kimi_decisions.jsonl")):
            self.assertEqual(n.st["cursors"][key]["offset"], os.path.getsize(self.p(name)))
        count = len(self.tg.sent)
        n.step()
        self.assertEqual(len(self.tg.sent), count)

    def test_out_of_range_timestamps_never_stop_the_notifier(self):
        self.write_json("news_cache.json", {"brief": {"fetched_at": 1.79e12}, "last_error": "x",
                                            "last_error_at": 1.79e12 + 5})
        self.write_json("kimi_brain_state.json", {"invalid_since": 1.79e12, "last_decision": {}})
        self.write_json("runner_state_live.json", {"last_cycle": {"time": 1.79e12, "equity_after": 3900000}})
        n = self.notifier()
        n.start()                                          # the start message renders "unknown time"
        n.step()
        self.assertIn("اطلاع‌رسان تلگرام ربات بیت‌پین روشن شد", self.tg.texts()[0])
        self.assertIn("وضعیت ربات", n.status_text(self.clock.t))
        rec = decision_rec(T0 + 5, digest="ms" + "0" * 14)
        rec["time"] = rec["decision"]["decided_at"] = 1.79e12                    # milliseconds
        self.append("kimi_decisions.jsonl", rec)
        self.append("kimi_runner.jsonl", runner_rec(T0 + 60, decided_at=1.79e12, fills=[
            fill("ETH_IRT", "sell", "0.001", "620000", "2170", "IRT", "o-ms")]))
        n.step()
        m = [t for t in self.tg.texts() if "💱" in t]
        self.assertEqual(len(m), 1)
        self.assertIn("o-ms", m[0])
        self.assertEqual(notify.fmt_when(1.79e12), "زمان نامشخص")

    def test_a_rendering_failure_never_loses_the_fills(self):
        n = self.notifier()
        n.start()
        n.step()
        self.append("kimi_runner.jsonl", runner_rec(T0 + 60, decided_at=T0 + 50, fills=[
            fill("ETH_IRT", "sell", "0.001", "620000", "2170", "IRT", "o-a"),
            fill("BTC_IRT", "buy", "0.00005", "970000", "0.0000002", "BTC", "o-b")]))
        with mock.patch.object(notify.Renderer, "trades", side_effect=RuntimeError("boom")):
            with self.assertLogs("bitpin.notify", level="ERROR"):
                n.step()
        m = [t for t in self.tg.texts() if "💱" in t]
        self.assertEqual(len(m), 1)
        self.assertIn("قابل نمایش نبود", m[0])
        self.assertIn("o-a", m[0])
        self.assertIn("o-b", m[0])


class TestResponsiveness(Base):
    def test_a_backlog_is_sent_in_batches_and_a_stop_during_it_works_and_goes_first(self):
        n = self.notifier(self.cfg(telegram={"min_send_interval_seconds": 1.1}))
        n.start()
        n.step()
        self.tg.sent.clear()
        for i in range(150):                               # queued while the proxy was down
            n.enqueue("x:%d" % i, "decision", "message %d" % i)
        t0 = self.clock.t
        n.deliver()
        self.assertLess(self.clock.t - t0, notify.DELIVERY_BUDGET_SECONDS + 3)
        self.assertLess(len(self.tg.sent), 20)
        self.assertEqual(n._poll_wait(), 1.0)              # the loop comes back quickly for the next batch
        self.tg.updates = [upd(1, CHAT, "/stop", t0 + 5)]
        n.poll_commands(wait=n._poll_wait())
        self.assertIn("stop_confirm", self.tg.texts(CHAT)[-1])
        for _ in range(3):
            n.deliver()
        self.tg.updates = [upd(2, CHAT, "/stop_confirm", t0 + 12)]
        n.poll_commands(wait=n._poll_wait())
        self.assertTrue(os.path.exists(os.path.join(self.nd, notify.STOP_REQUEST_FILE)))
        self.assertTrue(apply_stop_request(self.sd, self.nd, clock=self.clock)[0])
        before = len(self.tg.sent)
        n.step()
        self.assertIn("کلید قطع STOP فعال شد", self.tg.texts()[before])       # first, before the backlog
        self.assertGreater(len(n.st["outbox"]), 50)       # ... long before the backlog is through

    def test_outbox_sends_are_single_attempts(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append(url)
            raise OSError("proxy down")
        c = TelegramClient(TOKEN, transport=transport, max_retries=4, sleep=self.clock.sleep)
        n = Notifier(self.cfg(), Secrets(TOKEN, [CHAT]), telegram=c, clock=self.clock, sleep=self.clock.sleep)
        n.activity = lambda: self.clock.t
        n.start()
        n.step()
        self.assertEqual(len(calls), 1)                    # no 4 blocking retries inside one step
        self.assertEqual(self.clock.sleeps, [])


class TestPersistence(Base):
    def test_idle_steps_do_not_rewrite_the_state(self):
        self.append("kimi_decisions.jsonl", decision_rec(T0 - 60))
        n = self.notifier()
        n.start()
        n.step()
        with mock.patch.object(notify, "atomic_write_text", wraps=notify.atomic_write_text) as w:
            for _ in range(5):
                self.clock.t += 15
                n.step()
        self.assertEqual(w.call_count, 0)
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 100, digest="n" * 16))
        with mock.patch.object(notify, "atomic_write_text", wraps=notify.atomic_write_text) as w:
            n.step()
        self.assertGreater(w.call_count, 0)

    def test_nothing_but_alerts_is_sent_while_the_state_cannot_be_saved(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        with mock.patch.object(notify, "atomic_write_text", side_effect=OSError(28, "No space left on device")):
            for i in range(3):
                self.append("kimi_decisions.jsonl", decision_rec(T0 + 600 * (i + 1), digest="%016x" % (i + 1)))
                self.clock.t += 600
                n.step()
            self.write_json("risk_state_live.json", {"halted": True, "halt_reason": "drawdown 31%",
                                                     "halted_at": self.clock.t})
            n.step()
            texts = self.tg.texts()
            self.assertFalse(any("تصمیم جدید Kimi" in t for t in texts))
            self.assertEqual(sum("نمی‌تواند وضعیتش را روی دیسک ذخیره کند" in t for t in texts), 1)
            self.assertTrue(any("حد ضرر (drawdown) فعال شد" in t for t in texts))
        self.tg.sent.clear()
        n.step()                                           # the disk works again
        texts = self.tg.texts()
        self.assertEqual(sum("تصمیم جدید Kimi" in t for t in texts), 3)
        self.assertTrue(any("دوباره می‌تواند وضعیتش را ذخیره کند" in t for t in texts))
        n2 = self.notifier()                               # a restart re-sends nothing
        n2.start()
        n2.step()
        self.assertEqual(sum("تصمیم جدید Kimi" in t for t in self.tg.texts()), 3)

    def test_a_crash_mid_message_resends_only_the_unconfirmed_chunk_marked_and_never_finished_ones(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        n.enqueue("one", "info", "first message")
        n.enqueue("two", "info", "second message")
        n.enqueue("big", "info", "\n".join("line %03d %s" % (i, "x" * 100) for i in range(90)))
        n._save()
        self.assertEqual(len(n.st["outbox"][2]["chunks"]), 3)
        orig = self.tg.send_message

        def crash_on_fourth(chat_id, text, **kw):
            r = orig(chat_id, text, **kw)
            if len(self.tg.sent) == 4:                     # Telegram got it, then the process was killed
                raise KeyboardInterrupt
            return r
        self.tg.send_message = crash_on_fourth
        with self.assertRaises(KeyboardInterrupt):
            n.deliver()
        self.tg.send_message = orig
        n2 = self.notifier()
        n2.start()
        n2.step()
        texts = self.tg.texts()
        self.assertEqual(sum("first message" in t for t in texts), 1)
        self.assertEqual(sum("second message" in t for t in texts), 1)
        self.assertEqual(sum("line 000" in t for t in texts), 1)
        big2 = [t for t in texts if "(۲/۳)" in t]
        self.assertEqual(len(big2), 2)
        self.assertIn("شاید تکراری", big2[1])
        self.assertEqual(sum("(۳/۳)" in t for t in texts), 1)
        self.assertEqual(n2.st["outbox"], [])
        self.assertFalse(os.path.exists(os.path.join(self.nd, notify.JOURNAL_FILE)))

    def test_outgoing_messages_and_replies_are_redacted_and_numbers_never_corrupted(self):
        notify.REDACT.add(KIMI_KEY, "179012")
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abcdefghijklmnop"
        n.enqueue("x1", "warning", "error: Authorization: Bearer %s / key %s" % (jwt, KIMI_KEY))
        n.deliver()
        n.reply(CHAT, "echo %s and bot%s" % (KIMI_KEY, TOKEN))
        blob = "\n".join(self.tg.texts())
        for secret in (jwt, KIMI_KEY, TOKEN):
            self.assertNotIn(secret, blob)
        self.assertIn("&lt;redacted&gt;", blob)
        n.st["created"] = T0                               # 1790121600.0 contains the registered "179012"
        n.state.save()
        ids = sorted(n.st["sent"])
        n3 = self.notifier()
        n3.start()
        self.assertEqual(n3.st["created"], T0)
        self.assertEqual(sorted(n3.st["sent"]), ids)
        self.assertEqual(notify.REDACT("password 179012 leaked"), "password <redacted> leaked")


class TestFallbacksAndDigest(Base):
    def test_repeated_fallbacks_ring_once_and_holds_are_silent(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        for i in range(4):                                 # a derisk of leftover dust, written every cycle
            self.append("kimi_decisions.jsonl", decision_rec(T0 + 1800 * (i + 1), digest="fb%014d" % i,
                                                             fallback="derisk",
                                                             targets={"USDT_IRT": 0.95 + 0.001 * i, "BTC_IRT": 0.0}))
        n.step()
        self.assertEqual([s for _, _, _, s in self.tg.sent], [False, True, True, True])
        self.assertIn("تکرار ۲", self.tg.sent[1][1])
        self.clock.t = T0 + 8 * 3600
        self.append("kimi_decisions.jsonl", decision_rec(self.clock.t, digest="fb9" + "0" * 13, fallback="derisk",
                                                         targets={"USDT_IRT": 0.95}))
        n.step()
        self.assertFalse(self.tg.sent[-1][3])              # a reminder after alert_repeat_minutes
        self.append("kimi_decisions.jsonl", decision_rec(self.clock.t + 60, digest="hold" + "0" * 12, hold=True))
        n.step()
        self.assertTrue(self.tg.sent[-1][3])
        self.assertIn("نگه‌داری", self.tg.sent[-1][1])

    def test_events_of_a_long_downtime_arrive_as_one_digest(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        for i in range(96):                                # two days at 30-minute cycles
            self.append("kimi_decisions.jsonl", decision_rec(T0 + 1800 * (i + 1), digest="dt%014d" % i))
        self.add_trade(T0 + 3600, "BTC_IRT", "buy", "0.00005", "970000", "19400000000", "0.0000002", "BTC", "o-old")
        self.append("kimi_runner.jsonl", runner_rec(T0 + 3600, decided_at=T0 + 1800, fills=[
            fill("BTC_IRT", "buy", "0.00005", "970000", "0.0000002", "BTC", "o-old")]))
        self.clock.t = T0 + 49 * 3600
        n.step()
        texts = self.tg.texts()
        digests = [t for t in texts if "گزارش فشرده" in t]
        self.assertEqual(len(digests), 1)
        self.assertIn("۸۵ بازچینش", digests[0])
        self.assertIn("معاملات: ۱ سفارش", digests[0])
        self.assertEqual(sum("تصمیم جدید Kimi" in t for t in texts), 11)   # the last 6 hours one by one
        self.assertFalse(any("معاملات انجام‌شده" in t for t in texts))
        self.clock.t += 3600
        n.step()
        self.assertEqual(len(self.tg.texts()), len(texts))

    def test_the_summary_window_starts_at_the_previous_summary(self):
        self.clock.t = T0 + 19 * 3600                      # 22:30 Tehran
        n = self.notifier(self.cfg(daily_summary_time="23:30"))
        n.start()
        self.clock.t = T0 + 20 * 3600 + 5                  # 23:30:05: summary 1
        n.step()
        self.add_trade(T0 + 20 * 3600 + 120, "ETH_IRT", "sell", "0.001", "620000", "620000000", "2170", "IRT",
                       "o-late")                           # the 23:30 cycle trades at 23:32
        self.clock.t = T0 + 44 * 3600 + 5                  # the next day 23:30:05: summary 2
        n.step()
        s = [t for t in self.tg.texts() if "خلاصهٔ روزانه —" in t]
        self.assertEqual(len(s), 2)
        self.assertIn("معاملات این بازه: ۰ سفارش", s[0])
        self.assertIn("معاملات این بازه: ۱ سفارش", s[1])
        self.assertIn("بازه: از ۱ مهر ۱۴۰۵، ساعت ۲۳:۳۰", s[1])


class TestCommandsDownAndRelay(Base):
    def test_a_lasting_getupdates_conflict_raises_an_alert_and_a_recovery(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        good = self.tg.get_updates

        def conflict(offset=None, timeout=0):
            raise TelegramError("Conflict: can't use getUpdates method while webhook is active", status=409)
        self.tg.get_updates = conflict
        n.poll_commands(wait=1)
        n.step()
        self.assertEqual(self.tg.sent, [])
        self.clock.t += notify.CMD_DOWN_ALERT_SECONDS + 1
        n._next_updates = 0
        n.poll_commands(wait=1)
        n.step()
        alert = self.tg.texts()[-1]
        self.assertIn("کار نمی‌کنند", alert)
        self.assertIn("sudo bitpin-bot stop", alert)
        self.assertIn("revoke", alert)
        self.tg.get_updates = good
        n._next_updates = 0
        n.poll_commands(wait=1)
        n.step()
        self.assertIn("دوباره کار می‌کنند", self.tg.texts()[-1])

    def test_the_relay_refuses_anything_but_a_small_regular_file(self):
        req = os.path.join(self.nd, notify.STOP_REQUEST_FILE)
        os.makedirs(req)                                   # a directory at the request path
        ok, detail = apply_stop_request(self.sd, self.nd, clock=self.clock)
        self.assertFalse(ok)
        self.assertIn("refused", detail)
        self.assertFalse(os.path.exists(req))
        with open(req, "wb") as f:
            f.write(json.dumps({"kind": "stop", "time": self.clock.t, "pad": "x" * 5000}).encode())
        ok, detail = apply_stop_request(self.sd, self.nd, clock=self.clock)
        self.assertFalse(ok)
        self.assertIn("larger", detail)
        self.assertFalse(os.path.exists(self.p("STOP")))
        with open(os.path.join(self.nd, notify.STOP_RESULT_FILE), encoding="utf-8") as f:
            self.assertFalse(json.load(f)["ok"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this system")
    def test_the_relay_never_blocks_on_a_fifo(self):
        req = os.path.join(self.nd, notify.STOP_REQUEST_FILE)
        os.makedirs(self.nd, exist_ok=True)
        os.mkfifo(req)
        box = {}
        th = threading.Thread(target=lambda: box.update(r=apply_stop_request(self.sd, self.nd, clock=self.clock)),
                              daemon=True)
        th.start()
        th.join(5)
        self.assertFalse(th.is_alive(), "apply_stop_request hangs on a FIFO")
        self.assertFalse(box["r"][0])
        self.assertFalse(os.path.exists(req))

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_the_relay_never_follows_a_symlinked_request(self):
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as f:
            json.dump({"kind": "stop", "time": self.clock.t}, f)
        os.makedirs(self.nd, exist_ok=True)
        os.symlink(target, os.path.join(self.nd, notify.STOP_REQUEST_FILE))
        ok, detail = apply_stop_request(self.sd, self.nd, clock=self.clock)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.p("STOP")))
        self.assertTrue(os.path.exists(target))


class TestSetup(Base):
    class FakeTG:
        def __init__(self, chats):
            self.chats = chats

        def get_me(self):
            return {"username": "my_bitpin_alerts_bot", "first_name": "x"}

        def get_updates(self, offset=None, timeout=0):
            return [{"update_id": i, "message": {"date": int(T0), "text": "/start", "chat": c}}
                    for i, c in enumerate(self.chats)]

    def setup_output(self, chats):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import notify_bot
        out = io.StringIO()
        root = logging.getLogger()
        saved = (list(root.handlers), root.level)
        try:
            with mock.patch.object(notify_bot, "_telegram", lambda cfg, s: self.FakeTG(chats)), \
                    mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}, clear=True), \
                    mock.patch("sys.stdout", out):
                rc = notify_bot.main(["setup", "--state-dir", self.sd, "--notify-state-dir", self.nd,
                                      "--log-level", "ERROR"])
        finally:
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in saved[0]:
                root.addHandler(h)
            root.setLevel(saved[1])
        return rc, out.getvalue()

    def test_a_ready_line_only_for_a_single_private_chat_and_names_are_sanitised(self):
        owner = {"id": 911222333, "type": "private", "username": "owner", "first_name": "Owner"}
        stranger = {"id": 7005001, "type": "private", "username": "stranger", "first_name": "\u202eevil\x1b[2J"}
        group = {"id": -100555, "type": "supergroup", "title": "family"}
        rc, text = self.setup_output([stranger, owner, group])
        self.assertEqual(rc, 0)
        self.assertNotIn("TELEGRAM_CHAT_ID=7005001", text)
        self.assertNotIn("TELEGRAM_CHAT_ID=-100555", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("NOT all of them are you", text)
        self.assertIn("do not use", text)
        rc, text = self.setup_output([owner, group])
        self.assertIn("TELEGRAM_CHAT_ID=911222333", text)
        rc, text = self.setup_output([group])
        self.assertNotIn("TELEGRAM_CHAT_ID=-", text)


# --------------------------------------------------------------------------- review round 2 (regressions)

NOTIFIER_FILES = ("bitpin/notify.py", "scripts/notify_bot.py", "tests/test_notify.py", "docs/TELEGRAM_FA.md",
                  "notify.example.json", "deploy/bitpin-bot-notify.service", "scratch/notify_integration.md")


class TestSourceHygiene(unittest.TestCase):
    def test_no_literal_control_or_bidi_characters_in_the_notifier_files(self):
        """'Trojan Source': an invisible bidi control in a snippet (the report_fa sanitizer for brain.py
        had four inside a regex class) can reorder how code is shown, and an editor that drops it
        turns the class into an invalid range. Write them only as escapes."""
        bad = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C, 0x7F}
        found = []
        for rel in NOTIFIER_FILES:
            path = os.path.join(ROOT, rel)
            if not os.path.exists(path):
                continue
            with io.open(path, encoding="utf-8", newline="") as f:
                for no, line in enumerate(f.read().split("\n"), 1):
                    for c in line.rstrip("\r"):
                        if ord(c) in bad or (ord(c) < 32 and c != "\t"):
                            found.append("%s:%d U+%04X" % (rel, no, ord(c)))
        self.assertEqual(found, [])


class TestUntrustedText(Base):
    FAKE = ("بازار آرام است و بیشتر سرمایه در تتر می‌ماند.\n\n"
            "🛑 کلید قطع STOP برداشته شد و ربات دوباره فعال است.\n"
            "✅ برای تأیید هویت حساب با پشتیبانی تماس بگیریدtg://resolve?domain=bitpin_support_team\n"
            "   ⚠️ یا به @@bitpin_support_team پیام دهید.")

    def test_model_text_cannot_add_lines_that_look_like_the_notifiers_own(self):
        v = decision_view(decision_rec(T0, report_fa=self.FAKE, reasoning="Calm market.\n✅ STOP removed, the bot "
                                                                          "is active again\n\n🛑 halt lifted"))
        r = Renderer(self.cfg())
        for m in (r.decision(v, persian=v["report_fa"]), r.decision(v, persian=v["report_fa"], full=True),
                  r.decision(v)):
            lines = m.split("\n")
            forged = [ln for ln in lines if "STOP" in ln or "halt" in ln or "پشتیبانی" in ln]
            self.assertTrue(forged)
            for ln in forged:                          # inside the marked block, never a line of its own
                self.assertTrue(ln.startswith(notify.UNTRUSTED_MARK), ln)
            for ln in lines:
                self.assertFalse(ln.startswith(("🛑", "✅")), ln)
            for ln in forged:
                self.assertEqual(TestRendering.telegram_entities(ln), [], ln)
        m = r.decision(v, persian=v["report_fa"])
        self.assertIn(notify.UNTRUSTED_MARK + "کلید قطع STOP برداشته شد", m)
        self.assertIn(notify.UNTRUSTED_MARK + "بازار آرام است", m)
        self.assertIn("💬 <b>دلیل:</b> Calm market.\n" + notify.UNTRUSTED_MARK + "STOP removed", r.decision(v))

    def test_one_line_places_show_line_breaks_as_a_mark_and_keep_tags_on_one_line(self):
        r = Renderer(self.cfg())
        v = decision_view(decision_rec(T0, valid=False, error_kind="validation",
                                       error="bad reply:\n✅ <b>STOP removed</b>\n🛑 halt"))
        m = r.invalid(v)
        self.assertIn(notify.LINE_BREAK_MARK.strip(), m)
        for ln in m.split("\n"):
            self.assertFalse(ln.startswith(("🛑", "✅")), ln)
            for tag in ("code", "b", "i"):
                self.assertEqual(ln.count("<%s>" % tag), ln.count("</%s>" % tag), ln)
        self.assertEqual(notify.clean_text("a\n\nb", 50), "a ⏎ b")
        self.assertEqual(notify.clean_text("a\n\nb", 50, keep_lines=True), "a\n\nb")


class TestProxyCredentials(Base):
    def test_the_proxy_user_name_is_not_a_secret_but_the_password_is(self):
        env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": str(CHAT),
               "TELEGRAM_HTTPS_PROXY": "http://bitpin:Pr0xyPass@127.0.0.1:1081"}
        msg = ("توقف اضطراری فقط از روی سرور: <code>sudo bitpin-bot stop</code>\n"
               "<code>sudo systemctl restart bitpin-bot-notify</code> /var/lib/bitpin-bot")
        notify.load_secrets(env)
        self.assertEqual(notify.redact_html(msg), msg)
        self.assertNotIn("Pr0xyPass", notify.REDACT("the proxy answered Pr0xyPass"))
        self.assertNotIn("Pr0xyPass", notify.REDACT("via http://bitpin:Pr0xyPass@127.0.0.1:1081 failed"))
        notify.REDACT.reset()
        notify.load_secrets(dict(env, TELEGRAM_HTTPS_PROXY="http://bitpin:p%40ssW0rd9@127.0.0.1:1081"))
        self.assertNotIn("p@ssW0rd9", notify.REDACT("x p@ssW0rd9 y"))
        self.assertNotIn("p%40ssW0rd9", notify.REDACT("x p%40ssW0rd9 y"))
        self.assertEqual(notify.redact_html(msg), msg)
        notify.REDACT.reset()                         # a password that is one of our own words: URLs only
        with self.assertLogs("bitpin.notify", level="WARNING"):
            notify.load_secrets(dict(env, TELEGRAM_HTTPS_PROXY="http://u:bitpin@127.0.0.1:1081"))
        self.assertEqual(notify.redact_html(msg), msg)
        self.assertNotIn("u:bitpin@", notify.REDACT("error at http://u:bitpin@127.0.0.1:1081"))


class TestLateCommands(Base):
    def setUp(self):
        super().setUp()
        self.n = self.notifier()
        self.n.start()
        self.n.step()
        self.tg.sent.clear()
        self.req = os.path.join(self.nd, notify.STOP_REQUEST_FILE)

    def test_a_stop_that_reaches_the_notifier_hours_later_is_not_executed(self):
        sent_at = T0 + 600                         # the proxy went down: sent then, fetched 3 h 50 min later
        self.clock.t = T0 + 4 * 3600
        self.tg.updates = [upd(1, CHAT, "/stop", sent_at), upd(2, CHAT, "/stop_confirm", sent_at + 15)]
        self.n.poll_commands(wait=0)
        self.assertFalse(os.path.exists(self.req))
        self.assertIsNone(self.n.st["pending_stop"])
        t = self.tg.texts(CHAT)
        self.assertEqual(len(t), 1)                # one reply per chat and poll
        for s in ("اجرا نشد", "ربات متوقف نشد", "دیر به اطلاع‌رسان رسید"):
            self.assertIn(s, t[0])
        self.assertEqual(apply_stop_request(self.sd, self.nd, clock=self.clock)[1], "no stop request")
        self.assertFalse(os.path.exists(self.p("STOP")))
        self.tg.updates = [upd(3, CHAT, "/status", self.clock.t - notify.STALE_COMMAND_SECONDS - 5)]
        self.n.poll_commands(wait=0)
        self.assertIn("اجرا نشد", self.tg.texts(CHAT)[-1])

    def test_the_stop_request_is_stamped_with_the_time_of_the_confirmation(self):
        t0 = self.clock.t
        self.tg.updates = [upd(1, CHAT, "/stop", t0), upd(2, CHAT, "/stop_confirm", t0 + 10)]
        self.clock.t = t0 + 290                    # a slow loop: handled 290 s later, still executed
        self.n.poll_commands(wait=0)
        with open(self.req, encoding="utf-8") as f:
            r = json.load(f)
        self.assertEqual(r["time"], t0 + 10)
        self.clock.t = t0 + 10 + 301               # the relay's max age counts from the confirmation
        ok, detail = apply_stop_request(self.sd, self.nd, max_age=300, clock=self.clock)
        self.assertFalse(ok)
        self.assertIn("stale", detail)
        self.assertFalse(os.path.exists(self.p("STOP")))


class TestAlertOrderAndOutage(Base):
    def telegram_down(self):
        down = [True]
        real = self.tg.send_message

        def send(*a, **k):
            if down[0]:
                raise TelegramError("network error", retryable=True)
            return real(*a, **k)
        self.tg.send_message = send
        return down

    def drain(self, n, steps=150):
        for _ in range(steps):
            n._next_delivery = 0
            n.step()
            self.clock.t += 15
            if not n.st["outbox"]:
                return

    def test_an_alert_and_its_recovery_never_swap_while_telegram_is_down(self):
        n = self.notifier(fresh=False)
        act = [self.clock.t]
        n.activity = lambda: act[0]
        n.start()
        n.step()
        self.tg.sent.clear()
        down = self.telegram_down()

        def run(until, alive):
            while self.clock.t < until:
                self.clock.t += 300
                if alive:
                    act[0] = self.clock.t
                if int(self.clock.t) % 1800 == 0:
                    self.append("kimi_decisions.jsonl", decision_rec(self.clock.t - 60, hold=True,
                                                                     digest="%016d" % int(self.clock.t)))
                n.step()
        t0 = self.clock.t
        run(t0 + 4 * 3600, False)                  # the bot is silent 4 h -> alert
        run(t0 + 5 * 3600, True)                   # back for 1 h -> recovery
        run(t0 + 9 * 3600, False)                  # silent again -> alert (the bot is DOWN now)
        down[0] = False
        self.drain(n)
        hb = [t for t in self.tg.texts() if "فعالیتی ثبت نکرده" in t or "دوباره فعال است" in t]
        self.assertEqual(len(hb), 3)
        self.assertIn("دوباره فعال است", hb[1])
        self.assertIn("فعالیتی ثبت نکرده", hb[-1])  # the LAST word is the current state

    def test_recoveries_and_alerts_survive_a_full_outbox_and_nothing_is_dropped_silently(self):
        n = self.notifier(self.cfg(max_outbox=20))
        n.start()
        n.step()
        self.tg.sent.clear()
        n._next_delivery = self.clock.t + 10 ** 6       # Telegram down
        n.enqueue("a:x:1", "alert", "🔌 down")
        n.enqueue("a:xok:1", "recovery", "✅ up")
        for i in range(60):
            n.enqueue("d:%d" % i, "decision", "decision %d" % i, meta={"dec": "kimi", "t": self.clock.t})
        ids = self.ids(n)
        self.assertLessEqual(len(ids), 20)
        self.assertEqual(ids[:2], ["a:x:1", "a:xok:1"])
        dg = [i for i in n.st["outbox"] if i["kind"] == "digest"]
        self.assertEqual(len(dg), 1)
        folded = dg[0]["meta"]["digest"]["decisions"]["kimi"]
        self.assertEqual(folded + sum(1 for i in ids if i.startswith("d:")), 60)
        self.assertEqual(n.st["stats"]["folded"], folded)
        self.assertEqual(n.st["stats"]["dropped"], 0)
        self.assertIn("گزارش فشرده", dg[0]["chunks"][0])

    def test_a_long_telegram_outage_while_the_notifier_runs_ends_in_one_digest(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        down = self.telegram_down()
        oid = 1000
        for c in range(24):                        # 12 h at 30-minute cycles, the notifier running
            self.clock.t += 1800
            t = self.clock.t
            hold = c % 2 == 1
            self.append("kimi_decisions.jsonl", decision_rec(t - 60, digest="dg%014d" % c, hold=hold))
            fills = []
            if not hold:
                oid += 1
                self.add_trade(t - 30, "BTC_IRT", "buy", "0.0001", "900000", "9000000000", "0.0000002", "BTC", str(oid))
                fills = [fill("BTC_IRT", "buy", "0.0001", "900000", "0.0000002", "BTC", str(oid))]
            self.append("kimi_runner.jsonl", runner_rec(t - 20, fills=fills, decided_at=t - 60, cycle_min=30))
            for _ in range(4):
                n.step()
                self.clock.t += 60
        items = n.st["outbox"]
        dgi = [i for i in items if i["kind"] == "digest"]
        self.assertEqual(len(dgi), 1)
        dg = dgi[0]["meta"]["digest"]
        indiv_dec = sum(1 for i in items if i["kind"] in ("decision", "hold"))
        indiv_tr = sum(1 for i in items if i["kind"] == "trades")
        self.assertEqual(sum(dg["decisions"].values()) + indiv_dec, 24)    # nothing lost ...
        self.assertEqual(dg["trades"] + indiv_tr, 12)
        self.assertLessEqual(indiv_dec, 14)                                  # ... one message each: ~6 h only
        down[0] = False
        self.drain(n)
        sent = self.tg.sent
        digests = [s for s in sent if "گزارش فشرده" in s[1]]
        self.assertEqual(len(digests), 1)
        self.assertFalse(digests[0][3])                                      # the digest rings once
        late = [s for s in sent if "با تأخیر فرستاده شد" in s[1]]
        self.assertGreater(len(late), 5)
        self.assertTrue(all(s[3] for s in late))                             # late ordinary messages: silent
        self.assertLessEqual(sum(1 for s in sent if not s[3]), 4)            # the digest + the newest cycle
        self.assertEqual(n.st["stats"]["dropped"], 0)


class TestOrderAndFrozenAlerts(Base):
    def test_the_bots_own_resolution_window_is_not_called_stuck(self):
        cases = (("never appeared, 905 s", {"status": "unknown", "lookup_failures": 0}, 905, "unknown"),
                 ("3 failed lookups, 240 s", {"status": "unknown", "lookup_failures": 3,
                                              "last_lookup_error": "timed out"}, 240, None),
                 ("10 failed lookups", {"status": "unknown", "lookup_failures": 10,
                                        "last_lookup_error": "timed out"}, 240, "stuck"),
                 ("unknown for 21 min", {"status": "unknown"}, 1260, "stuck"),
                 ("the bot marked it stuck", {"status": "unknown", "stuck": True}, 400, "stuck"))
        for i, (label, entry, age, want) in enumerate(cases):
            with self.subTest(label):
                self.tg = FakeTelegram()
                n = self.notifier(self.cfg(notify_state_dir=os.path.join(self.tmp, "n%d" % i)))
                n.start()
                n.step()
                self.tg.sent.clear()
                ent = dict(entry, symbol="BTC_IRT", side="buy", created_at=self.clock.t - age)
                self.write_json("live_orders.json", {"orders": {"a1b2c3": ent}})
                n.step()
                joined = "\n".join(self.tg.texts())
                if want is None:
                    self.assertEqual(joined, "")
                elif want == "unknown":
                    self.assertIn("سفارش با نتیجهٔ نامعلوم", joined)
                    self.assertNotIn("systemctl stop bitpin-bot", joined)
                else:
                    self.assertIn("نتیجهٔ سفارش مدتی است روشن نشده", joined)
                    self.assertIn("فقط اگر چنین خطی آمده بود", joined)
                self.write_json("live_orders.json", {"orders": {"a1b2c3": dict(ent, status="not_found")}})
                self.clock.t += 60
                n.step()
                if want is not None:
                    self.assertIn("مشخص شد: <code>not_found</code>", self.tg.texts()[-1])

    def test_kimi_and_news_alerts_are_not_repeated_while_the_bot_is_stopped_on_purpose(self):
        n = self.notifier(fresh=False)
        act = [self.clock.t]
        n.activity = lambda: act[0]
        t0 = self.clock.t
        self.write_json("kimi_brain_state.json", {"invalid_since": t0 - 3 * 3600, "invalid_count": 6,
                                                  "last_decision": {"error_kind": "llm", "valid": False}})
        self.write_json("news_cache.json", {"brief": {"fetched_at": t0 - 5 * 3600}, "last_error": "network error",
                                            "last_error_at": t0 - 600})
        n.start()
        n.step()
        texts = self.tg.texts()
        self.assertEqual(sum("Kimi مدتی است تصمیم معتبری نداده" in t for t in texts), 1)
        self.assertEqual(sum("پژوهش اخبار مدتی است" in t for t in texts), 1)
        with open(self.p("STOP"), "w") as f:       # the owner stops the bot; it writes nothing any more
            f.write("x")
        for h in range(1, 49):                     # two days of STOP
            self.clock.t = t0 + h * 3600
            n.step()
        texts = self.tg.texts()
        self.assertEqual(sum("Kimi مدتی است تصمیم معتبری نداده" in t for t in texts), 1)
        self.assertEqual(sum("پژوهش اخبار مدتی است" in t for t in texts), 1)
        os.remove(self.p("STOP"))                  # resumed, and Kimi still fails: reminders come back
        act[0] = self.clock.t
        n.step()
        self.clock.t += 7 * 3600
        act[0] = self.clock.t
        n.step()
        self.assertEqual(sum("Kimi مدتی است تصمیم معتبری نداده" in t for t in self.tg.texts()), 2)

    def test_an_outage_is_measured_only_until_the_bot_stopped(self):
        n = self.notifier(fresh=False)
        t0 = self.clock.t
        n.activity = lambda: t0 - 10 * 3600         # the bot stopped 10 h ago (STOP)
        with open(self.p("STOP"), "w") as f:
            f.write("x")
        self.write_json("kimi_brain_state.json", {"invalid_since": t0 - 11 * 3600, "invalid_count": 2,
                                                  "last_decision": {"error_kind": "llm"}})
        n.start()
        n.step()
        self.assertFalse(any("Kimi مدتی است" in t for t in self.tg.texts()))   # 1 h < 2 h until the stop
        self.write_json("kimi_brain_state.json", {"invalid_since": t0 - 13 * 3600, "invalid_count": 6,
                                                  "last_decision": {"error_kind": "llm"}})
        n.step()
        k = [t for t in self.tg.texts() if "Kimi مدتی است" in t]
        self.assertEqual(len(k), 1)
        self.assertIn("متوقف است", k[0])
        self.assertIn("تا توقف ربات", k[0])
        self.assertNotIn("فقط قاعده‌های ایمنی", k[0])


class TestBlindNotifier(Base):
    def test_permission_denied_on_the_bots_files_raises_an_alert_instead_of_silence(self):
        self.append("kimi_decisions.jsonl", decision_rec(T0 - 60))
        self.append("kimi_runner.jsonl", runner_rec(T0 - 60))
        n = self.notifier(fresh=False)
        n.start()
        n.step()
        self.tg.sent.clear()
        real_open, real_stat = open, os.stat
        sd = os.path.abspath(self.sd) + os.sep

        def denied(p):
            return isinstance(p, str) and os.path.abspath(p).startswith(sd)

        def fake_open(p, *a, **k):
            if denied(p):
                raise PermissionError(13, "Permission denied", p)
            return real_open(p, *a, **k)

        def fake_stat(p, *a, **k):
            if denied(p):
                raise PermissionError(13, "Permission denied", p)
            return real_stat(p, *a, **k)
        with mock.patch.object(notify, "open", fake_open, create=True), mock.patch.object(notify.os, "stat", fake_stat):
            self.clock.t += 60
            with self.assertLogs("bitpin.notify", level="ERROR"):
                n.step()
            n.step()
        alerts = [t for t in self.tg.texts() if "فایل‌های ربات را نمی‌تواند بخواند" in t]
        self.assertEqual(len(alerts), 1)
        self.assertIn("kimi_decisions.jsonl", alerts[0])
        self.assertFalse(any(t.startswith("💤") for t in self.tg.texts()))        # no false "bot inactive"
        n.step()                                   # readable again
        self.assertIn("دوباره فایل‌های ربات را می‌خواند", self.tg.texts()[-1])

    def test_the_bots_files_disappearing_raises_the_same_alert(self):
        self.append("kimi_decisions.jsonl", decision_rec(T0 - 60))
        self.append("kimi_runner.jsonl", runner_rec(T0 - 60))
        n = self.notifier(fresh=False)
        n.start()
        n.step()
        self.tg.sent.clear()
        for name in os.listdir(self.sd):
            os.remove(self.p(name))
        n.step()
        self.assertIn("دیگر پیدا نمی‌شوند", "\n".join(self.tg.texts()))
        self.append("kimi_runner.jsonl", runner_rec(self.clock.t))
        self.append("kimi_decisions.jsonl", decision_rec(self.clock.t))
        n.step()
        self.assertTrue(any("دوباره فایل‌های ربات را می‌خواند" in t for t in self.tg.texts()))


# --------------------------------------------------------------------------- the daily-schedule + ladder version

def event(t, kind, eid, **fields):
    rec = {"t": t, "kind": kind, "mode": "live", "id": eid}
    rec.update(fields)
    return rec


class TestDailyLadderVersion(Base):
    """bot_events.jsonl (resting-order fills, code exits, positions, notices, endgame), decision modes,
    COIN_USDT fills priced in USDT, and limit orders that never count as 'the bot cannot trade'."""

    def started(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        return n

    def test_first_start_sends_no_old_events(self):
        self.append("bot_events.jsonl", event(T0 - 600, "exit", "old-exit", symbol="BTC_IRT", reason="stop",
                                              close_usdt=70000, level_usdt=72000, entry_px_usdt=80000))
        n = self.started()
        n.step()
        self.assertEqual(self.tg.sent, [])
        self.append("bot_events.jsonl", event(T0 + 60, "exit", "new-exit", symbol="BTC_IRT", reason="stop",
                                              close_usdt=70000, level_usdt=72000, entry_px_usdt=80000))
        n.step()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("خروج خودکار: حد ضرر", self.tg.sent[0][1])

    def test_a_ladder_fill_is_one_message_and_its_csv_row_is_not_reported_again(self):
        n = self.started()
        t = T0 + 120
        self.add_trade(t, "BTC_USDT", "buy", "0.00003", "2.1", "70000", "0.0000001", "BTC", "9001", note="limit fill")
        self.append("bot_events.jsonl", event(t, "fill", "f-1", reason="ladder", route="resting", symbol="BTC_USDT",
                                              side="buy", base="0.00003", quote="2.1", quote_asset="USDT",
                                              avg_price="70000", fee="0.0000001", fee_asset="BTC", order_id="9001",
                                              identifier="lad-1", state="filled", level_pct=-20.0, coin="BTC"))
        n.step()
        msgs = self.tg.texts()
        self.assertEqual(len(msgs), 1, msgs)
        m = msgs[0]
        for s in ("خرید پله‌ای (نردبان) پر شد", "بیت‌کوین (BTC)", "۲۰٪ زیر بالاترین قیمت ۴۸ ساعت", "۷۰٬۰۰۰ تتر",
                  "کامل پر شد", "بازبینی"):
            self.assertIn(s, m)
        self.assertNotIn("تومان", m)                      # a COIN_USDT fill is never shown in toman
        self.clock.t += 3600                               # the CSV row is not flushed as an orphan later
        self.append("kimi_runner.jsonl", runner_rec(self.clock.t, action="none"))
        n.step()
        self.clock.t += 3600
        n.step()
        self.assertEqual(len(self.tg.sent), 1)
        n2 = self.notifier()                               # a restart re-reads nothing
        n2.start()
        n2.step()
        self.assertEqual(len([x for x in self.tg.texts() if "نردبان" in x]), 1)

    def test_a_target_fill_and_position_events(self):
        n = self.started()
        t = T0 + 60
        self.append("bot_events.jsonl", event(t, "position_open", "p-1", symbol="ETH_IRT", source="ladder",
                                              amount=0.001, entry_px_usdt=2240, stop_px_usdt=1971.2,
                                              target_px_usdt=2520, max_hold_until=T0 + 7 * 86400))
        self.append("bot_events.jsonl", event(t + 3600, "fill", "f-2", reason="target", route="resting",
                                              symbol="ETH_USDT", side="sell", base="0.001", quote="2.52",
                                              quote_asset="USDT", avg_price="2520", order_id="9002", state="filled"))
        self.append("bot_events.jsonl", event(t + 3600, "position_close", "p-1c", symbol="ETH_IRT",
                                              why="sold (holding below one minimum order)"))
        n.step()
        texts = self.tg.texts()
        self.assertEqual(len(texts), 3, texts)
        self.assertIn("موقعیت تحت محافظت کد: اتریوم (ETH) (منبع: خرید پله‌ای)", texts[0])
        self.assertIn("حد ضرر ۱٬۹۷۱", texts[0])
        self.assertTrue(self.tg.sent[0][3])                 # silent
        self.assertIn("فروش هدف انجام شد", texts[1])
        self.assertIn("۲٬۵۲۰ تتر", texts[1])
        self.assertIn("بسته شد", texts[2])

    def test_code_exit_and_its_usdt_fill_are_labelled_and_never_counted_as_toman(self):
        n = self.started()
        t = T0 + 60
        self.append("bot_events.jsonl", event(t, "exit", "x-1", symbol="BTC_IRT", reason="stop", close_usdt=70000,
                                              level_usdt=70400, entry_px_usdt=80000, source="ladder"))
        self.append("kimi_runner.jsonl", runner_rec(t, action="none", fills=[
            dict(fill("BTC_USDT", "sell", "0.00003", "2.1", "0.00735", "USDT", "o-7"), quote_asset="USDT",
                 route="direct", reason="stop")]))
        n.step()
        texts = self.tg.texts()
        alert = [x for x in texts if "خروج خودکار" in x]
        trades = [x for x in texts if "💱" in x]
        self.assertEqual((len(alert), len(trades)), (1, 1), texts)
        self.assertIn("حد ضرر", alert[0])
        self.assertIn("۷۰٬۴۰۰", alert[0])
        m = trades[0]
        for s in ("🛑 حد ضرر", "مستقیم در بازار BTC_USDT", "میانگین قیمت: ۷۰٬۰۰۰ تتر", "ارزش: ۲٫۱ تتر",
                  "جمع: خرید ۰ · فروش ۰ ·", "جمع بازارهای تتری: خرید ۰ · فروش ۲٫۱ تتر"):
            self.assertIn(s, m)
        self.assertEqual(notify.fills_meta([{"side": "sell", "quote": "2.1", "symbol": "BTC_USDT"}], t)["sell"], 0.0)

    def test_decision_modes_triggers_ladder_scales_and_exits(self):
        n = self.started()
        rec = decision_rec(T0 + 60, digest="veto" * 4, targets={"BTC_IRT": 0.0, "USDT_IRT": 1.0},
                           trigger="veto: SOL closed 15.3% below its 48 h high (USDT terms) with its ladder bids resting")
        d = rec["decision"]
        d.update(mode="veto", trigger_kind="veto", ladder={"BTC": 1.0, "ETH": 1.0, "XRP": 0.5, "SOL": 0.0},
                 events=[{"kind": "veto", "coin": "SOL", "text": "SOL closed 15.3% below its 48 h high"}],
                 exits={"BTC_IRT": {"stop_pct": 12.0, "target_price": 95000.0, "max_hold_until": T0 + 86400}})
        self.append("kimi_decisions.jsonl", rec)
        n.step()
        m = self.tg.texts()[-1]
        for s in ("نوع تصمیم: <b>وتو پس از افت شدید</b>", "خرید مجاز نیست", "وتو: سولانا ۱۵٫۳٪ زیر سقف ۴۸ ساعته",
                  "نردبان خرید", "سولانا خاموش", "ریپل ۵۰٪", "بیت‌کوین کامل", "خروج خودکار بیت‌کوین: حد ضرر ۱۲٪",
                  "هدف ۹۵٬۰۰۰ تتر", "رویداد: <code>SOL closed 15.3% below its 48 h high</code>"):
            self.assertIn(s, m)
        self.assertEqual(notify.trigger_fa("scheduled decision slot 2026-09-24 13:00 Tehran"),
                         "تصمیم روزانهٔ زمان‌بندی‌شده (ساعت ۱۳:۰۰ تهران)")
        self.assertEqual(notify.trigger_fa("final decision slot 2026-10-21 13:00 Tehran"),
                         "تصمیم نهایی مسابقه (ساعت ۱۳:۰۰ تهران)")
        self.assertIn("بیت‌کوین", notify.trigger_fa("review: ladder bid FILLED: BTC at 20% below its 48 h high"))
        self.assertIn("کاهش ریسک", notify.trigger_fa("risk reduce: the drawdown worsened from 1.0% to 5.0%"))
        self.assertIn("اتریوم", notify.trigger_fa("held coin: ETH_IRT moved +8.4% (USDT terms) since the last decision"))

    def test_notify_only_wake_ups_and_the_endgame(self):
        n = self.started()
        t = T0 + 60
        self.append("bot_events.jsonl", event(t, "notify", "n-1", notify_kind="usdt",
                                              text="USDT_IRT moved -4.2% since the last decision (notification only)"))
        self.append("bot_events.jsonl", event(t, "notify", "n-2", notify_kind="drawdown",
                                              text="the drawdown worsened from 1.0% to 4.5% since the last decision"))
        self.append("bot_events.jsonl", event(t, "endgame", "g-1", step="ladder_off"))
        self.append("bot_events.jsonl", event(t, "endgame", "g-2", step="final_decision",
                                              targets={"USDT_IRT": 0.8, "BTC_IRT": 0.2}))
        self.append("bot_events.jsonl", event(t, "watchdog", "w-1", trigger_kind="veto"))        # not on its own
        self.append("bot_events.jsonl", event(t, "order_place", "o-1", tag="ladder", symbol="BTC_USDT"))  # noise
        self.append("bot_events.jsonl", "{not json")
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertEqual(len(self.tg.sent), 4, joined)
        for s in ("قیمت تتر به تومان تغییر زیادی کرد", "USDT_IRT moved -4.2%", "افت سرمایه بیشتر شد",
                  "مرحلهٔ پایانی مسابقه شروع شد", "تصمیم نهایی مسابقه ثبت شد", "ترکیب پایانی: تتر ۸۰٪", "بیت‌کوین ۲۰٪"):
            self.assertIn(s, joined)

    def test_resting_limit_orders_never_raise_the_unknown_order_alert(self):
        n = self.started()
        j = {"orders": {
            "lad-1": {"kind": "limit", "created_at": T0 - 3600, "side": "buy", "status": "resting", "symbol": "BTC_USDT",
                      "tag": "ladder"},
            "lad-2": {"kind": "limit", "created_at": T0 - 3600, "side": "buy", "status": "unknown",
                      "symbol": "ETH_USDT", "tag": "ladder"},
            "mkt-1": {"created_at": T0 - 3600, "side": "sell", "status": "unknown", "symbol": "BTC_IRT"}}}
        self.write_json("live_orders.json", j)
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("mkt-1", joined)
        self.assertNotIn("lad-1", joined)
        self.assertNotIn("lad-2", joined)
        self.write_json("runner_state_live.json", {"positions": {"BTC_IRT": {
            "entry_px_usdt": 70000, "stop_px_usdt": 61600, "target_px_usdt": 78750, "max_hold_until": T0 + 86400}}})
        st = n.status_text(self.clock.t)
        for s in ("سفارش نامعلوم: ۱", "سفارش‌های باز ربات روی بیت‌پین: ۱ (خرید پله‌ای بیت‌کوین)",
                  "موقعیت‌های تحت محافظت کد", "حد ضرر ۶۱٬۶۰۰"):
            self.assertIn(s, st)



class TestLadderReviewNotify(Base):
    """Ladder-round review findings on the notifier: COIN_USDT fees, the daily-cadence news alert and
    the /stop texts."""

    def started(self):
        n = self.notifier()
        n.start()
        n.step()
        self.tg.sent.clear()
        return n

    def test_coin_usdt_fees_are_valued_through_usdt_irt_not_read_as_toman(self):
        """Review finding (money, low): a BTC_USDT buy's fee (in BTC) was valued as fee x the USDT average
        price and summed as TOMAN (~230,000x too small), and a BTC_USDT sell's fee (in USDT) as unknown."""
        self.append("kimi_decisions.jsonl", decision_rec(T0 - 600, usdt=230000.0))
        n = self.started()
        t = T0 + 60
        self.append("kimi_runner.jsonl", runner_rec(t, action="none", fills=[
            dict(fill("BTC_USDT", "buy", "0.00003", "2.1", "0.00000009", "BTC", "o-1"), quote_asset="USDT",
                 avg_price="70000", reason="ladder"),
            dict(fill("BTC_USDT", "sell", "0.00003", "2.1", "0.00735", "USDT", "o-2"), quote_asset="USDT",
                 avg_price="70000", reason="stop")]))
        n.step()
        m = [x for x in self.tg.texts() if "💱" in x][0]
        self.assertIn("(~۰٫۰۰۶۳ تتر) (~۱٬۴۴۹ تومان)", m)            # 0.00000009 BTC x 70,000 x 230,000
        self.assertIn("۰٫۰۰۷۳۵ USDT (~۱٬۶۹۰ تومان)", m)                # the USDT fee of the sell
        self.assertIn("کارمزد ~۳٬۱۴۰ تومان", m)
        self.assertNotIn("کارمزد با ارز دیگر", m)
        r = n.render
        self.assertAlmostEqual(r._fee_irt({"symbol": "BTC_USDT", "quote_asset": "USDT", "fee": 1e-7,
                                           "fee_asset": "BTC", "avg": 70000.0}), 1610.0)
        self.assertAlmostEqual(r._fee_irt({"symbol": "BTC_IRT", "fee": 1e-7, "fee_asset": "BTC",
                                           "avg": 16100000000.0}), 1610.0)          # toman market: unchanged
        self.assertEqual(r._fee_irt({"symbol": "BTC_IRT", "fee": 5.0, "fee_asset": "IRT"}), 5.0)
        r.usdt_irt = lambda: None                                   # no USDT price known: unknown, never toman
        self.assertIsNone(r._fee_irt({"symbol": "BTC_USDT", "quote_asset": "USDT", "fee": 1e-7,
                                      "fee_asset": "BTC", "avg": 70000.0}))

    def test_the_news_outage_is_measured_from_the_first_failed_attempt(self):
        """Review finding (ops, low): with the daily brief (cache ~23 h) the outage was measured from the
        last good brief's fetched_at, so ONE failed 13:00 research alerted at once and then every 6 h."""
        n = self.started()
        self.write_json("news_cache.json", {"brief": {"fetched_at": T0 - 23 * 3600}, "last_error": "network error",
                                            "last_error_at": T0 - 60, "error_since": T0 - 60})
        n.step()
        self.assertFalse(any("پژوهش اخبار مدتی است" in x for x in self.tg.texts()))
        self.clock.t = T0 + 5 * 3600                                 # still down 5 h after the first failure
        n.step()
        self.assertEqual(sum("پژوهش اخبار مدتی است" in x for x in self.tg.texts()), 1)
        self.clock.t = T0 + 20 * 3600                                # no new attempt: no repeat
        n.step()
        self.assertEqual(sum("پژوهش اخبار مدتی است" in x for x in self.tg.texts()), 1)
        self.write_json("news_cache.json", {"brief": {"fetched_at": T0 - 23 * 3600}, "last_error": "network error",
                                            "last_error_at": self.clock.t - 60, "error_since": T0 - 60})
        n.step()                                                     # the next attempt failed too: a repeat
        self.assertEqual(sum("پژوهش اخبار مدتی است" in x for x in self.tg.texts()), 2)
        self.write_json("news_cache.json", {"brief": {"fetched_at": self.clock.t}, "last_error": "",
                                            "last_error_at": None, "error_since": None})
        n.step()
        self.assertIn("پژوهش اخبار دوباره کار می‌کند", "\n".join(self.tg.texts()))

    def test_stop_texts_say_the_crash_bids_are_cancelled(self):
        with open(notify.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("سفارش‌های خرید نردبان"), 3)
        self.assertIn("دارایی‌ها و سفارش‌های فروش هدف دست نمی‌خورند", src)

# --------------------------------------------------------------------------- release v3: ops alerts, weekly summary

FRIDAY_20 = 1790353800.0    # 2026-09-25 16:30 UTC = Friday 20:00 Tehran (T0 is Wednesday 03:30 Tehran)


class TestReleaseV3Ops(Base):
    """Spec F1 / F2 / B4 (the notifier's side) / D5 / A2 text: the 'running but cycles failing' alert,
    the 30 h without a valid decision alert, the Moonshot-quota alert, the local-proxy outage report,
    the daily drawdown / LLM-spend lines, the weekly summary on Friday 20:00 Tehran, the unit-failure
    relay, the per-coin analysis numbers and 'no stop' positions."""

    # ---- cycles failing
    def test_alive_bot_without_a_successful_cycle_for_two_hours_is_an_alert(self):
        n = self.notifier()                                   # activity() is "now": the bot is alive
        n.start()
        self.append("kimi_runner.jsonl", runner_rec(T0 - 300, status="ok"))
        n.step()
        self.assertEqual(n.last_ok_cycle(), T0 - 300)
        self.tg.sent.clear()
        self.clock.t = T0 + 3600
        self.append("kimi_runner.jsonl", runner_rec(self.clock.t, status="error", errors=["boom: HTTP 500"], plan=[]))
        n.step()
        self.assertFalse(any("چرخه‌هایش با خطا" in t for t in self.tg.texts()))     # 1 h: not yet
        self.clock.t = T0 + 2 * 3600 + 600
        n.step()
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertEqual(joined.count("ربات روشن است ولی چرخه‌هایش با خطا تمام می‌شوند"), 1)
        self.assertIn("boom: HTTP 500", joined)
        self.assertIn("sudo bitpin-bot health", joined)
        self.assertIn("آخرین چرخهٔ موفق", n.status_text(self.clock.t))
        self.clock.t += 7 * 3600                              # repeated after alert_repeat_minutes
        n.step()
        self.assertEqual("\n".join(self.tg.texts()).count("چرخه‌هایش با خطا"), 2)
        self.append("kimi_runner.jsonl", runner_rec(self.clock.t, status="ok"))
        n.step()
        self.assertIn("چرخه‌های ربات دوباره موفق‌اند", self.tg.texts()[-1])
        self.assertFalse(n.st["flags"]["cycles"]["active"])

    def test_runner_state_last_cycle_counts_as_a_successful_cycle_and_a_stopped_bot_is_not_failing(self):
        n = self.notifier()
        n.start()
        self.write_json("runner_state_live.json", {"last_cycle": {"time": T0 - 3 * 3600, "equity": 3800000}})
        with open(self.p("STOP"), "w") as f:                  # stopped on purpose: cycles end early by design
            f.write("x")
        n.step()
        self.assertEqual(n.last_ok_cycle(), T0 - 3 * 3600)
        self.assertFalse(any("چرخه‌هایش با خطا" in t for t in self.tg.texts()))
        os.remove(self.p("STOP"))
        n.step()
        self.assertTrue(any("چرخه‌هایش با خطا" in t for t in self.tg.texts()))
        # a bot that is not alive at all is the heartbeat alert's business, not this one's
        n2 = self.notifier(fresh=False)
        n2.start()
        self.tg.sent.clear()
        n2.step()
        self.assertFalse(any("چرخه‌هایش با خطا" in t for t in self.tg.texts()))

    # ---- no valid decision for 30 h
    def test_thirty_hours_without_a_valid_decision(self):
        n = self.notifier()
        n.start()
        self.write_json("kimi_brain_state.json", {"last_valid_at": T0 - 31 * 3600, "invalid_since": None})
        n.step()
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertEqual(joined.count("ساعت است تصمیم معتبری از Kimi ثبت نشده"), 1)
        self.assertIn("۳۱ ساعت است", joined)
        self.assertIn("تصمیمی نخواسته یا نگرفته", joined)
        self.assertIn("۱۷ ساعت دیگر ربات کوین‌ها را به تتر می‌برد", joined)   # 48 - 31
        self.write_json("kimi_brain_state.json", {"last_valid_at": self.clock.t})
        n.step()
        self.assertIn("تصمیم معتبر تازه‌ای ثبت شد", self.tg.texts()[-1])

    def test_the_decision_index_dates_the_last_valid_decision_and_a_frozen_bot_is_quiet(self):
        self.append("kimi_decisions.jsonl", decision_rec(T0 - 40 * 3600))
        self.append("kimi_decisions.jsonl", decision_rec(T0 - 35 * 3600, valid=False, error_kind="llm"))
        n = self.notifier()
        n.start()                                             # the history is indexed, not sent
        self.assertEqual(n.last_valid_decision_at(), T0 - 40 * 3600)
        self.write_json("risk_state_live.json", {"halted": True, "halt_reason": "drawdown", "halted_at": T0 - 10})
        n.step()
        self.assertFalse(any("تصمیم معتبری از Kimi ثبت نشده" in t for t in self.tg.texts()))
        self.write_json("risk_state_live.json", {"halted": False})
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("۴۰ ساعت است تصمیم معتبری", joined)
        self.assertIn("۸ ساعت دیگر ربات کوین‌ها را به تتر می‌برد", joined)   # 48 - 40

    # ---- Moonshot quota
    def test_two_quota_errors_in_a_row_raise_the_balance_alert(self):
        n = self.notifier()
        n.start()
        self.write_json("kimi_brain_state.json", {"invalid_since": T0 - 3600, "invalid_count": 2,
                                                  "last_decision": {"error_kind": "llm_quota", "valid": False}})
        self.append("kimi_decisions.jsonl", decision_rec(T0 - 3600, valid=False, error_kind="llm_quota",
                                                          error="HTTP 429 (account quota/balance)"))
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("اعتبار (شارژ) حساب Moonshot تمام شده", joined)     # the invalid-decision message
        self.assertNotIn("اعتبار Moonshot تمام شده؛", joined)              # one error: not yet the alert
        self.append("kimi_decisions.jsonl", decision_rec(T0, valid=False, error_kind="llm_quota",
                                                          error="HTTP 429 (account quota/balance)"))
        n.step()
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertEqual(joined.count("اعتبار Moonshot تمام شده؛ تا ۴۷ ساعت دیگر derisk"), 1)
        self.assertIn("۲ خطای پشت سر هم", joined)
        self.assertIn("llm_quota", joined)
        self.assertIn("پنل Moonshot", joined)
        self.append("kimi_decisions.jsonl", decision_rec(T0 + 60))          # a valid decision ends it
        self.write_json("kimi_brain_state.json", {"invalid_since": None, "last_valid_at": T0 + 60})
        n.step()
        self.assertTrue(any("اعتبار Moonshot برقرار است" in t for t in self.tg.texts()))   # a recovery: sent first

    def test_the_cost_meters_quota_streak_alone_raises_it_and_the_text_survives_without_the_module(self):
        n = self.notifier()
        n.start()
        self.write_json("llm_spend.json", {"today_usd": 0, "month_usd": 1, "total_usd": 2,
                                           "quota_errors_in_a_row": 2, "quota_alert": True})
        with mock.patch.dict(sys.modules, {"bitpin.spend": None}):       # ImportError inside the notifier
            n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("اعتبار Moonshot تمام شده؛ تا ۴۸ ساعت دیگر derisk", joined)
        self.assertEqual(notify._quota_text_fa(None).split("؛")[0], "اعتبار Moonshot تمام شده")

    # ---- daily lines
    def test_daily_summary_has_the_spend_and_drawdown_lines(self):
        n = self.notifier()
        n.start()
        self.write_json("llm_spend.json", {"today_usd": 0.42, "month_usd": 3.5, "total_usd": 12.25,
                                           "quota_errors_in_a_row": 1})
        self.write_json("risk_state_live.json", {"hwm": "4000000", "last_equity": "3600000", "drawdown": 0.1,
                                                 "max_drawdown": 0.5, "hwm_window_days": 90, "halted": False})
        text = n.summary_text(self.clock.t)
        self.assertIn("💸 هزینهٔ Kimi (دلار): امروز $۰٫۴۲ · این ماه $۳٫۵۰ · از شروع $۱۲٫۲۵", text)
        self.assertIn("۱ خطای پشت سر هم «اعتبار تمام شده»", text)
        self.assertIn("📉 افت از سقف ۹۰ روزه: ۱۰٪ (سقف ۴٬۰۰۰٬۰۰۰ تومان) · توقف در ۵۰٪؛ فاصله تا توقف ۴۰ واحد درصد", text)
        with mock.patch.dict(sys.modules, {"bitpin.spend": None}):
            self.assertIn("$۰٫۴۲", n.summary_text(self.clock.t))
        # an older bot's risk state (no drawdown field) is computed from hwm / last_equity; no spend file: no line
        os.remove(self.p("llm_spend.json"))
        self.write_json("risk_state_live.json", {"hwm": "4000000", "last_equity": "3000000"})
        text = n.summary_text(self.clock.t)
        self.assertNotIn("هزینهٔ Kimi", text)
        self.assertIn("📉 افت از سقف: ۲۵٪ (سقف ۴٬۰۰۰٬۰۰۰ تومان)", text)
        titles = [t for t, _ in notify.dry_run(self.cfg(), now=self.clock.t)]
        self.assertIn("weekly summary", titles)

    def test_the_halt_alert_names_the_limit_and_positions_without_a_stop_say_so(self):
        n = self.notifier()
        n.start()
        self.write_json("risk_state_live.json", {"halted": True, "halt_reason": "drawdown 50.2% from high-water mark",
                                                 "halted_at": T0 + 10, "max_drawdown": 0.5, "hwm_window_days": 90})
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertIn("حد توقف: افت ۵۰٪ از سقف ۹۰ روزهٔ ارزش حساب", joined)
        self.assertIn("توقف پایان بازی نیست", joined)
        self.write_json("runner_state_live.json", {"positions": {"BTC_IRT": {
            "entry_px_usdt": 70000, "stop_px_usdt": None, "target_px_usdt": 78750, "max_hold_until": T0 + 86400}}})
        st = n.status_text(self.clock.t)
        self.assertIn("بیت‌کوین: ورود ۷۰٬۰۰۰ تتر · بدون حد ضرر · هدف ۷۸٬۷۵۰", st)

    # ---- weekly summary
    def test_weekly_summary_on_friday_20_tehran_once_per_week(self):
        n = self.notifier()
        n.start()
        n.enqueue("a:test:1", "alert", "x")                   # counted for the week
        n.step()
        self.clock.t = FRIDAY_20 - 60
        n.step()
        self.assertFalse(any("خلاصهٔ هفتگی" in t for t in self.tg.texts()))
        self.clock.t = FRIDAY_20 + 60
        n.step()
        n.step()
        texts = [t for t in self.tg.texts() if "خلاصهٔ هفتگی" in t]
        self.assertEqual(len(texts), 1)
        self.assertIn("خلاصهٔ هفتگی — ۳ مهر ۱۴۰۵", texts[0])
        self.assertIn("🚨 هشدارهای این هفته: ۱", texts[0])
        self.assertIn("معاملات این هفته", texts[0])
        self.assertIn("تصمیم‌های این هفته", texts[0])
        self.assertIn("🩺 <b>سلامت</b>", texts[0])
        n2 = self.notifier()                                  # a restart the same evening: not again
        n2.start()
        n2.step()
        self.assertEqual(sum("خلاصهٔ هفتگی" in t for t in self.tg.texts()), 1)
        self.clock.t += 7 * 86400
        n2.step()
        texts = [t for t in self.tg.texts() if "خلاصهٔ هفتگی" in t]
        self.assertEqual(len(texts), 2)
        self.assertIn("هشدارهای این هفته: ۰", texts[1])
        self.assertIn("بازه: از ۳ مهر", texts[1])             # from the previous weekly summary

    def test_first_start_after_the_friday_slot_waits_for_the_next_week(self):
        self.clock.t = FRIDAY_20 + 3600
        n = self.notifier()
        n.start()
        n.step()
        self.assertFalse(any("خلاصهٔ هفتگی" in t for t in self.tg.texts()))
        self.clock.t = FRIDAY_20 + 7 * 86400 + 60
        n.step()
        self.assertTrue(any("خلاصهٔ هفتگی" in t for t in self.tg.texts()))

    # ---- the unit-failure relay
    def test_a_unit_failure_file_becomes_one_alert_and_is_deleted(self):
        n = self.notifier()
        n.start()
        path = os.path.join(self.nd, notify.UNIT_FAILURE_FILE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"unit": "bitpin-bot.service", "exit_status": 78, "result": "exit-code", "time": T0 - 10}, f)
        n.step()
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertEqual(joined.count("سرویس ربات از کار افتاد و دیگر ری‌استارت نمی‌شود"), 1)
        self.assertIn("کد خروج <code>78</code>", joined)
        self.assertIn("کد ۷۸ یعنی مشکلی که ری‌استارت حلش نمی‌کند", joined)
        self.assertIn("sudo systemctl reset-failed bitpin-bot.service", joined)
        self.assertFalse(os.path.exists(path))
        with open(path, "w", encoding="utf-8") as f:          # a broken file: the failure is still reported
            f.write("{not json")
        n.step()
        self.assertIn("سرویس ربات از کار افتاد", self.tg.texts()[-1])
        self.assertFalse(os.path.exists(path))
        with open(path, "wb") as f:                           # too large: refused, removed, logged
            f.write(b"x" * (notify.UNIT_FAILURE_MAX_BYTES + 1))
        before = len(self.tg.sent)
        n.step()
        self.assertEqual(len(self.tg.sent), before)
        self.assertFalse(os.path.exists(path))

    def test_a_failed_certificate_renewal_is_not_called_a_bot_failure(self):
        """v3.8.2: bitpin-bot-panel-cert.service (the panel certificate's renewal) has its own alert."""
        n = self.notifier()
        n.start()
        path = os.path.join(self.nd, notify.UNIT_FAILURE_FILE)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"unit": "bitpin-bot-panel-cert.service", "exit_status": 1, "result": "exit-code",
                       "time": T0 - 10}, f)
        n.step()
        n.step()
        text = " | ".join(t for t in self.tg.texts() if "گواهی" in t)
        self.assertEqual(text.count("تمدید گواهی پنل مدیریت انجام نشد"), 1)
        self.assertIn("ربات و معامله‌هایش به این ربطی ندارند", text)
        self.assertIn("sudo journalctl -u bitpin-bot-panel-cert.service", text)
        self.assertNotIn("سرویس ربات از کار افتاد", text)
        self.assertFalse(os.path.exists(path))

    # ---- v3.1: the management panel's events
    def test_panel_events_become_alerts_and_are_deleted(self):
        n = self.notifier()
        n.start()
        self.tg.sent.clear()

        def put(name, rec, raw=None):
            with open(os.path.join(self.nd, name), "w", encoding="utf-8") as f:
                f.write(raw if raw is not None else json.dumps(rec))

        put("panel_event.1700000000001.aa01.json", {"event": "login_ok", "user": "owner", "ip": "203.0.113.7",
                                                    "time": T0 - 5})
        put("panel_event.1700000000002.aa02.json", {"event": "change", "what": "secret", "arg": "OPENROUTER_API_KEY",
                                                    "user": "owner", "ip": "203.0.113.7", "time": T0 - 4})
        put("panel_event.1700000000003.aa03.json", {"event": "login_locked", "ip": "<b>x</b>", "time": T0 - 3})
        put("panel_event.1700000000004.aa04.json", {"event": "change", "what": "apply_live", "arg": "failed"})
        put("panel_event.1700000000005.aa05.json", {"event": "change", "what": "vpn", "arg": "rolled back"})
        put("panel_event.1700000000006.aa06.json", {"event": "nothing"})
        put("panel_event.1700000000007.aa07.json", None, raw="{not json")
        put("panel_event.other.json", {"event": "login_ok"})             # not the helper's name: left alone
        n.step()
        joined = "\n".join(self.tg.texts())
        self.assertEqual(joined.count("ورود به پنل مدیریت</b>"), 1)
        self.assertIn("کاربر <code>owner</code> · IP <code>203[.]0[.]113[.]7</code>", joined)       # dotted values are defanged (no Telegram link)
        self.assertIn("کلید محرمانه عوض شد <code>OPENROUTER_API_KEY</code>", joined)
        self.assertIn("ورود به پنل مدیریت قفل شد", joined)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", joined)
        self.assertIn("اعمال تنظیمات از پنل ناموفق بود", joined)
        self.assertIn("تنظیم قبلی برگردانده شد", joined)
        left = sorted(x for x in os.listdir(self.nd) if x.startswith("panel_event."))
        self.assertEqual(left, ["panel_event.other.json"])
        n.step()
        self.assertEqual("\n".join(self.tg.texts()).count("ورود به پنل مدیریت</b>"), 1)   # sent once
        for i in range(notify.PANEL_EVENTS_PER_STEP + 3):                                   # a burst: 10 per step
            put("panel_event.17000000001%02d.bb%02d.json" % (i, i), {"event": "change", "what": "service",
                                                                   "arg": "bitpin-bot restart"})
        n.step()
        self.assertEqual(len([x for x in os.listdir(self.nd) if notify.PANEL_EVENT_RE.match(x)]), 3)
        n.step()
        self.assertEqual([x for x in os.listdir(self.nd) if notify.PANEL_EVENT_RE.match(x)], [])

    # ---- the local-proxy outage report
    def _outage(self, kimi_state, fails=3, retry_after=None):
        n = self.notifier()
        n.start()
        self.write_json("kimi_brain_state.json", kimi_state)
        n.step()
        self.tg.sent.clear()
        self.append("kimi_decisions.jsonl", decision_rec(self.clock.t))
        self.tg.fail = [TelegramError("network error", retryable=True, retry_after=retry_after)] * fails
        for _ in range(fails):
            self.clock.t += 3600
            n.step()
        self.assertEqual(self.tg.sent, [])
        self.clock.t += 3600
        n.step()
        n.step()
        return "\n".join(self.tg.texts())

    def test_three_failed_sends_plus_kimi_connection_errors_are_reported_once_the_proxy_is_back(self):
        joined = self._outage({"invalid_since": T0 + 600, "invalid_count": 2, "last_decision": {
            "valid": False, "error_kind": "llm", "error": "network error: RemoteDisconnected", "decided_at": T0 + 3600}})
        self.assertEqual(joined.count("مسیر اینترنت خارجی (پراکسی محلی) قطع بود"), 1)
        self.assertIn("تلگرام ۳ بار پشت سر هم در دسترس نبود", joined)
        self.assertIn("RemoteDisconnected", joined)
        self.assertIn("تصمیم جدید Kimi", joined)              # the queued message went out too

    def test_no_report_without_kimi_connection_errors_or_with_fewer_failures(self):
        joined = self._outage({"invalid_since": T0 + 600, "last_decision": {
            "valid": False, "error_kind": "validation", "error": "bad json", "decided_at": T0 + 3600}})
        self.assertNotIn("پراکسی محلی) قطع بود", joined)
        self.tg = FakeTelegram()
        joined = self._outage({"last_decision": {"valid": False, "error_kind": "llm_timeout", "error": "",
                                                 "decided_at": T0 + 3600}}, fails=2)
        self.assertNotIn("پراکسی محلی) قطع بود", joined)
        self.tg = FakeTelegram()                              # Telegram's own 429 is not the proxy
        joined = self._outage({"last_decision": {"valid": False, "error_kind": "llm_timeout", "error": "",
                                                 "decided_at": T0 + 3600}}, retry_after=5)
        self.assertNotIn("پراکسی محلی) قطع بود", joined)

    # ---- the analysis block (D5)
    def test_decision_message_shows_the_per_coin_analysis_numbers(self):
        n = self.notifier()
        rec = decision_rec(T0)
        rec["decision"]["analysis"] = {
            "candidates": {"BTC_IRT": {"p": 0.55, "ev_pct": 1.2, "cost_pct": 1.0, "verdict": "add", "pass": True},
                           "ETH": {"p": 0.3, "ev_pct": -0.5, "cost_pct": 2.2, "verdict": "reject", "pass": False},
                           "??": {"p": 0.1}, "SOL_IRT": "garbage"},
            "scenario_loss_pct": 12.5, "headroom_pct": 40, "usdt_case": "USDT_IRT +2% on toman weakness http://x.io",
            "evidence": "never shown"}
        v = decision_view(rec)
        self.assertEqual(sorted(v["analysis"]["candidates"]), ["BTC_IRT", "ETH"])
        text = n.render.decision(v)
        for s in ("🔬 <b>تحلیل Kimi برای هر کوین</b>", "بیت‌کوین: p ۰٫۵۵ · ev +۱٫۲٪ · هزینه ۱٪ · حکم: افزایش · ✅ گذشت",
                  "اتریوم: p ۰٫۳ · ev -۰٫۵٪ · هزینه ۲٫۲٪ · حکم: رد · ❌ رد شد",
                  "📐 زیان سناریوی بد: ۱۲٫۵٪ · فضای تا توقف: ۴۰٪", "💵 <b>حالت تتر:</b>", "x[.]io"):
            self.assertIn(s, text)
        self.assertNotIn("never shown", text)
        rec["decision"]["analysis"] = "garbage"
        self.assertIsNone(decision_view(rec)["analysis"])
        self.assertNotIn("تحلیل Kimi", n.render.decision(decision_view(rec)))


if __name__ == "__main__":
    unittest.main()
