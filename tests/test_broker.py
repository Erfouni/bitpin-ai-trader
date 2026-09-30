"""Decimal helpers, order-book walking, PaperBroker and LiveBroker (fake client, no network)."""
import csv
import logging
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bitpin.api import (BitpinAPIError, BitpinClient, BitpinError, OrderNotSent, OrderStatusUnknown,  # noqa: E402
                        TransportError)
from bitpin.broker import (InsufficientFunds, LiveBroker, MarketNotTradable, OrderTooSmall, PaperBroker,  # noqa: E402
                           WalletDataError, fallback_log_path)
from bitpin.markets import (D, Market, MarketCache, ceil_to_precision, floor_to_precision,  # noqa: E402
                            max_fill_within, mid_price, parse_book, parse_symbol, walk_book)

logging.getLogger("bitpin").addHandler(logging.NullHandler())

USDT = Market("USDT_IRT", "USDT", "IRT", True, False, 0, 2, 0)
BTC = Market("BTC_IRT", "BTC", "IRT", True, False, 0, 8, 0)
PEPE = Market("PEPE_IRT", "PEPE", "IRT", True, False, 4, 0, 0)
HALTED = Market("DEAD_IRT", "DEAD", "IRT", True, True, 0, 2, 0)
MARKETS = MarketCache(markets=[USDT, BTC, PEPE, HALTED])

BOOK = {"asks": [["100", "10"], ["101", "5"], ["105", "100"]],
        "bids": [["99", "3"], ["98", "4"], ["90", "100"]]}
THIN = {"asks": [["100", "1"], ["102", "1"]], "bids": [["99", "1"]]}


class Clock:
    def __init__(self, t=1790000000.0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class DecimalHelpersTest(unittest.TestCase):
    def test_floor_to_precision(self):
        self.assertEqual(floor_to_precision("1.23999999", 2), Decimal("1.23"))
        self.assertEqual(floor_to_precision("0.999", 0), Decimal("0"))
        self.assertEqual(floor_to_precision(Decimal("123456789.987654321"), 8), Decimal("123456789.98765432"))
        self.assertEqual(floor_to_precision("12399", -2), Decimal("12300"))
        self.assertEqual(floor_to_precision("1.29", "0.05"), Decimal("1.25"))
        self.assertEqual(str(floor_to_precision("5", 2)), "5.00")
        for x in ("0.019999", "7.777777", "99999999999.99999"):
            for p in (0, 1, 2, 4, 8):
                self.assertLessEqual(floor_to_precision(x, p), D(x))  # never rounds up

    def test_ceil_to_precision(self):
        self.assertEqual(ceil_to_precision("1.231", 2), Decimal("1.24"))
        self.assertEqual(ceil_to_precision("1.23", 2), Decimal("1.23"))

    def test_D(self):
        self.assertEqual(D("0.1") + D("0.2"), Decimal("0.3"))
        self.assertEqual(D(0.1), Decimal("0.1"))  # floats via repr, not binary expansion
        for bad in (None, True, "nan", "inf", "abc"):
            with self.assertRaises(ValueError):
                D(bad)

    def test_parse_symbol(self):
        self.assertEqual(parse_symbol("btc_irt"), ("BTC", "IRT"))
        for bad in ("BTCIRT", "BTC-IRT", "../x_y", ""):
            with self.assertRaises(ValueError):
                parse_symbol(bad)

    def test_market_from_api(self):
        m = Market.from_api({"symbol": "PEPE_IRT", "base": "PEPE", "quote": "IRT", "tradable": True,
                             "suspended": False, "price_precision": 4, "base_amount_precision": 0,
                             "quote_amount_precision": 0})
        self.assertEqual(m.floor_base("12345.9"), Decimal("12345"))
        self.assertEqual(m.floor_price("0.00123456"), Decimal("0.0012"))
        self.assertTrue(m.is_trading)
        self.assertFalse(HALTED.is_trading)


class BookTest(unittest.TestCase):
    def setUp(self):
        self.book = parse_book(BOOK)

    def test_parse_and_mid(self):
        self.assertEqual(self.book["asks"][0], (Decimal("100"), Decimal("10")))
        self.assertEqual(self.book["bids"][0], (Decimal("99"), Decimal("3")))
        self.assertEqual(mid_price(self.book), Decimal("99.5"))
        messy = parse_book({"asks": [["101", "1"], ["100", "0"], ["99.5", "2"]], "bids": [["98", "1"], ["98.5", "1"]]})
        self.assertEqual([p for p, _ in messy["asks"]], [Decimal("99.5"), Decimal("101")])
        self.assertEqual([p for p, _ in messy["bids"]], [Decimal("98.5"), Decimal("98")])

    def test_walk_buy_across_levels(self):
        w = walk_book(self.book, "buy", quote_amount=Decimal("1505"), base_precision=2)
        # 10 @ 100 = 1000, 5 @ 101 = 505 -> exactly two levels
        self.assertEqual(w["base"], Decimal("15"))
        self.assertEqual(w["quote"], Decimal("1505"))
        self.assertTrue(w["complete"])
        w = walk_book(self.book, "buy", quote_amount=Decimal("1100"), base_precision=2)
        self.assertEqual(w["base"], Decimal("10.99"))  # 10 + floor(100/101, 2)
        self.assertEqual(w["quote"], Decimal("1000") + Decimal("0.99") * 101)

    def test_walk_sell_partial_book(self):
        w = walk_book(parse_book(THIN), "sell", base_amount=Decimal("3"))
        self.assertEqual(w["base"], Decimal("1"))
        self.assertFalse(w["complete"])

    def test_max_fill_within_matches_brute_force(self):
        book = self.book
        for side, limit in (("buy", Decimal("100.4")), ("buy", Decimal("102")), ("sell", Decimal("98.5"))):
            base, quote = max_fill_within(book, side, limit)
            avg = quote / base
            if side == "buy":
                self.assertLessEqual(avg, limit + Decimal("1e-20"))
                bigger = walk_book(book, "buy", quote_amount=quote * Decimal("1.01"))
                self.assertGreater(bigger["avg_price"], limit)
            else:
                self.assertGreaterEqual(avg, limit - Decimal("1e-20"))
                bigger = walk_book(book, "sell", base_amount=base * Decimal("1.01"))
                self.assertLess(bigger["avg_price"], limit)
        self.assertEqual(max_fill_within(book, "buy", Decimal("99")), (Decimal(0), Decimal(0)))


class PaperBrokerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_paper_test_")
        self.books = {"USDT_IRT": BOOK, "PEPE_IRT": THIN}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def broker(self, capital="10000"):
        return PaperBroker(MARKETS, self.dir, capital_irt=capital, book_source=lambda s: self.books[s])

    def test_buy_fee_in_base_and_persistence(self):
        b = self.broker()
        f = b.market_buy("USDT_IRT", Decimal("1505"))
        self.assertEqual(f["base"], Decimal("15"))
        self.assertEqual(f["quote"], Decimal("1505"))
        self.assertEqual(f["fee_asset"], "USDT")
        self.assertEqual(f["fee"], Decimal("0.06"))  # ceil(15 * 0.0035, 2 dp) = 0.06
        self.assertFalse(f["partial"])
        self.assertEqual(b.balances(), {"IRT": Decimal("8495"), "USDT": Decimal("14.94")})
        # restart: state reloaded from disk, capital argument ignored
        b2 = PaperBroker(MARKETS, self.dir, capital_irt="999", book_source=lambda s: self.books[s])
        self.assertEqual(b2.balances(), {"IRT": Decimal("8495"), "USDT": Decimal("14.94")})
        with open(os.path.join(self.dir, PaperBroker.TRADES_FILE), newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["side"], rows[0]["fee_asset"], rows[0]["base"]), ("buy", "USDT", "15"))

    def test_partial_fill_when_book_too_thin(self):
        b = self.broker()
        f = b.market_buy("PEPE_IRT", Decimal("1000"))
        self.assertTrue(f["partial"])
        self.assertEqual(f["base"], Decimal("2"))       # whole visible ask side
        self.assertEqual(f["quote"], Decimal("202"))    # only what was filled is spent
        self.assertEqual(f["fee"], Decimal("1"))        # PEPE precision 0 -> fee rounded up to 1
        self.assertEqual(b.balances()["IRT"], Decimal("9798"))
        self.assertEqual(b.balances()["PEPE"], Decimal("1"))

    def test_sell_fee_in_quote(self):
        b = self.broker()
        b.market_buy("USDT_IRT", Decimal("1000"))   # 10 USDT gross, fee ceil(0.035, 2dp) = 0.04 -> 9.96 net
        have = b.balances()["USDT"]
        self.assertEqual(have, Decimal("9.96"))
        f = b.market_sell("USDT_IRT", have)
        self.assertEqual(f["fee_asset"], "IRT")
        self.assertEqual(f["base"], have)
        gross = floor_to_precision(Decimal("3") * 99 + 4 * 98 + (have - 7) * 90, 0)   # walks three bid levels
        self.assertEqual(f["quote"], gross)
        self.assertEqual(f["fee"], ceil_to_precision(gross * Decimal("0.0035"), 0))
        self.assertEqual(b.balances()["USDT"], Decimal("0"))
        self.assertEqual(b.balances()["IRT"], Decimal("9000") + gross - f["fee"])

    def test_insufficient_and_untradable(self):
        b = self.broker("100")
        with self.assertRaises(InsufficientFunds):
            b.market_buy("USDT_IRT", Decimal("101"))
        with self.assertRaises(InsufficientFunds):
            b.market_sell("USDT_IRT", Decimal("1"))
        with self.assertRaises(MarketNotTradable):
            b.market_buy("DEAD_IRT", Decimal("10"))
        with self.assertRaises(OrderTooSmall):
            b.market_buy("USDT_IRT", Decimal("0.4"))

    def test_new_account_requires_capital(self):
        from bitpin.broker import BrokerError
        with self.assertRaises(BrokerError):
            PaperBroker(MARKETS, self.dir, book_source=lambda s: BOOK)


class FakeClient:
    """Minimal stand-in for BitpinClient's private API."""

    def __init__(self, clock):
        self.clock = clock
        self.wallet = {"IRT": ("100000000", "0"), "USDT": ("0", "0")}
        self.orders = {}
        self.placed = []
        self.cancelled = []
        self.poll_states = ["active", "closed"]
        self.place_error = None
        self.next_id = 100

    def wallets(self, assets=None, service="main", limit=200):
        return [{"id": i, "asset": a, "balance": b, "frozen": f, "service": "main"}
                for i, (a, (b, f)) in enumerate(self.wallet.items())]

    def orderbook(self, symbol):
        return {"asks": [["230000", "1000"]], "bids": [["229900", "1000"]]}

    def place_order(self, symbol, side, type="market", base_amount=None, quote_amount=None, price=None, identifier=None):
        self.placed.append({"symbol": symbol, "side": side, "type": type, "base_amount": base_amount,
                            "quote_amount": quote_amount, "identifier": identifier})
        if self.place_error:
            raise self.place_error
        self.next_id += 1
        o = {"id": self.next_id, "symbol": symbol, "side": side, "identifier": identifier, "state": "initial",
             "dealed_base_amount": "0", "dealed_quote_amount": "0", "commission": "0"}
        self.orders[self.next_id] = o
        return dict(o)

    def get_order(self, oid):
        o = self.orders[int(oid)]
        st = self.poll_states.pop(0) if len(self.poll_states) > 1 else self.poll_states[0]
        o["state"] = st
        if st == "closed" and o["side"] == "buy":
            q = D(self.placed[-1]["quote_amount"])
            base = floor_to_precision(q / 230000, 2)
            o.update(dealed_base_amount=str(base), dealed_quote_amount=str(q),
                     commission=str(floor_to_precision(base * D("0.0035"), 8)))
            self.wallet["IRT"] = (str(D(self.wallet["IRT"][0]) - q), "0")
            self.wallet["USDT"] = (str(base - D(o["commission"])), "0")
        return dict(o)

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return True

    def find_order_by_identifier(self, identifier):
        for o in self.orders.values():
            if o["identifier"] == identifier:
                return dict(o)
        return None


class LiveBrokerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bitpin_live_test_")
        self.clock = Clock()
        self.client = FakeClient(self.clock)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def broker(self, **kw):
        kw.setdefault("min_order_irt", 1000000)
        return LiveBroker(self.client, MARKETS, self.dir, sleep=self.clock.sleep, clock=self.clock, **kw)

    def test_market_buy_polls_until_closed_and_reconciles(self):
        b = self.broker()
        f = b.market_buy("USDT_IRT", Decimal("23000000.9"))
        p = self.client.placed[0]
        self.assertEqual(p["quote_amount"], Decimal("23000000"))   # floored to quote precision 0
        self.assertIsNone(p["base_amount"])
        self.assertEqual(p["type"], "market")
        uuid.UUID(p["identifier"])                                  # a uuid4 per order
        self.assertEqual(f["base"], Decimal("100.00"))
        self.assertEqual(f["quote"], Decimal("23000000"))
        self.assertEqual(f["fee"], Decimal("0.35"))
        self.assertEqual(f["fee_asset"], "USDT")
        self.assertEqual(f["status"], "filled")
        self.assertEqual(b.balances()["USDT"], Decimal("99.65"))   # wallet re-read after the fill
        self.assertEqual(b.journal.get(p["identifier"])["status"], "closed")
        self.assertEqual(b.resolve_pending(), [])

    def test_min_notional_refused_locally(self):
        b = self.broker()
        with self.assertRaises(OrderTooSmall):
            b.market_buy("USDT_IRT", Decimal("999999"))
        with self.assertRaises(OrderTooSmall):
            b.market_sell("USDT_IRT", Decimal("4"), ref_price=Decimal("230000"))
        self.assertEqual(self.client.placed, [])

    def test_insufficient_balance_refused_locally(self):
        b = self.broker()
        with self.assertRaises(InsufficientFunds):
            b.market_buy("USDT_IRT", Decimal("200000000"))
        self.assertEqual(self.client.placed, [])

    def test_rial_wallet_divisor(self):
        self.client.wallet = {"RIAL": ("1000000000", "50"), "IRT": ("7", "0"), "BTC": ("0.5", "0.1")}
        b = self.broker(irt_asset_code="RIAL", irt_unit_divisor=10)
        bal, av = b.balances(), b.available()
        self.assertEqual(bal["IRT"], Decimal("100000005"))
        self.assertEqual(av["IRT"], Decimal("100000000"))
        self.assertEqual(bal["IRT_UNMAPPED"], Decimal("7"))
        self.assertEqual(bal["BTC"], Decimal("0.6"))
        self.assertEqual(av["BTC"], Decimal("0.5"))

    def test_stuck_order_is_cancelled_after_timeout(self):
        self.client.poll_states = ["active"]
        b = self.broker(poll_timeout=5, poll_interval=1)
        f = b.market_buy("USDT_IRT", Decimal("23000000"))
        self.assertEqual(self.client.cancelled, [101])
        self.assertEqual(f["status"], "open")
        self.assertEqual(len(b.resolve_pending()), 1)  # runner will not trade until it is resolved

    def test_unknown_outcome_is_journaled_and_resolved(self):
        self.client.place_error = OrderStatusUnknown("x", "timeout")
        b = self.broker()
        with self.assertRaises(OrderStatusUnknown):
            b.market_buy("USDT_IRT", Decimal("23000000"))
        ident = self.client.placed[0]["identifier"]
        self.assertEqual(b.journal.get(ident)["status"], "unknown")
        # not on the exchange yet and young: still unresolved -> runner must wait
        self.assertEqual([p["identifier"] for p in b.resolve_pending()], [ident])
        # it shows up closed: resolved
        self.client.orders[7] = {"id": 7, "identifier": ident, "symbol": "USDT_IRT", "side": "buy", "state": "closed",
                                 "dealed_base_amount": "100", "dealed_quote_amount": "23000000", "commission": "0.35"}
        self.assertEqual(b.resolve_pending(), [])
        self.assertEqual(b.journal.get(ident)["status"], "closed")

    def test_unknown_order_never_seen_expires(self):
        self.client.place_error = OrderStatusUnknown("x", "timeout")
        b = self.broker(unknown_order_max_age=900)
        with self.assertRaises(OrderStatusUnknown):
            b.market_buy("USDT_IRT", Decimal("23000000"))
        self.clock.t += 901
        self.assertEqual(b.resolve_pending(), [])
        self.assertEqual(b.journal.get(self.client.placed[0]["identifier"])["status"], "not_found")

    def test_definite_rejection_is_not_pending(self):
        self.client.place_error = BitpinAPIError(400, "invalid", {"code": "invalid"})
        b = self.broker()
        with self.assertRaises(BitpinAPIError):
            b.market_buy("USDT_IRT", Decimal("23000000"))
        self.assertEqual(b.resolve_pending(), [])

    def test_order_not_sent_is_journaled_as_not_sent(self):
        self.client.place_error = OrderNotSent("x", "throttled")
        b = self.broker()
        with self.assertRaises(OrderNotSent):
            b.market_buy("USDT_IRT", Decimal("23000000"))
        self.assertEqual(b.journal.get(self.client.placed[0]["identifier"])["status"], "not_sent")
        self.assertEqual(b.resolve_pending(), [])  # nothing blocks the next bar

    # ---- wallet sanity (a single incomplete read must never be traded on)
    def test_wallet_without_toman_row_is_an_error_not_zero(self):
        self.client.wallet = {"USDT": ("100", "0")}
        b = self.broker()
        with self.assertRaises(WalletDataError):
            b.refresh()
        self.client.wallet = {"IRT": ("0", "0"), "USDT": ("100", "0")}
        b.refresh()
        self.client.wallet = {"USDT": ("100", "0")}     # it was 0 on the last good read: absent = 0
        b.refresh()
        self.assertEqual(b.balances()["IRT"], Decimal("0"))

    def test_vanished_coin_row_is_an_error_unless_the_bot_traded_it(self):
        self.client.wallet = {"IRT": ("100000000", "0"), "USDT": ("50", "0")}
        b = self.broker(assets=["USDT"])
        b.refresh()
        del self.client.wallet["USDT"]                 # glitch: the USDT row is missing
        with self.assertRaises(WalletDataError):
            b.refresh()
        self.client.wallet["USDT"] = ("50", "0")
        b.refresh()
        self.assertEqual(b.balances()["USDT"], Decimal("50"))
        b._expect_change.add("USDT")                   # the bot just sold it all: the row may go away
        del self.client.wallet["USDT"]
        b.refresh()
        self.assertNotIn("USDT", b.balances())

    # ---- trade CSV locked (Excel): the fill is still returned
    def test_locked_trade_log_does_not_lose_the_fill(self):
        b = self.broker()
        os.makedirs(b.trades_path)                      # cannot be opened for append, like a CSV held by Excel
        f = b.market_buy("USDT_IRT", Decimal("23000000"))
        self.assertEqual(f["status"], "filled")
        with open(fallback_log_path(b.trades_path), newline="") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 1)

    # ---- resolving journaled orders
    def test_open_order_is_resolved_by_order_id_cancelled_and_never_aged_out(self):
        self.client.poll_states = ["active"]
        b = self.broker(poll_timeout=5, poll_interval=1, unknown_order_max_age=900)
        b.market_buy("USDT_IRT", Decimal("23000000"))
        self.client.find_order_by_identifier = lambda ident: None   # identifier lookup unsupported
        self.client.cancelled = []
        self.clock.t += 901
        pend = b.resolve_pending()
        self.assertEqual(len(pend), 1)                  # still pending: the exchange says 'active'
        self.assertEqual(self.client.cancelled, [101])   # and the stale remainder is cancelled by id
        self.assertEqual(b.journal.get(pend[0]["identifier"])["status"], "open")

    def test_failing_lookups_are_counted_and_escalated(self):
        self.client.place_error = OrderStatusUnknown("x", "timeout")
        b = self.broker(max_lookup_failures=3)
        with self.assertRaises(OrderStatusUnknown):
            b.market_buy("USDT_IRT", Decimal("23000000"))

        def boom(ident):
            raise BitpinError("bad_next")
        self.client.find_order_by_identifier = boom
        for n in (1, 2, 3):
            pend = b.resolve_pending()
            self.assertEqual(pend[0]["lookup_failures"], n)
        self.assertTrue(pend[0]["stuck"])

    def test_read_only_resolve_never_cancels_or_writes(self):
        self.client.poll_states = ["active"]
        b = self.broker(poll_timeout=5, poll_interval=1)
        b.market_buy("USDT_IRT", Decimal("23000000"))
        self.client.cancelled = []
        with open(b.journal.path, "rb") as fh:
            before = fh.read()
        self.clock.t += 60
        self.assertEqual(len(b.resolve_pending(read_only=True)), 1)
        self.assertEqual(self.client.cancelled, [])
        with open(b.journal.path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_late_fill_delta_is_reported_once(self):
        self.client.poll_states = ["active"]
        b = self.broker(poll_timeout=5, poll_interval=1)
        f = b.market_buy("USDT_IRT", Decimal("23000000"))
        self.assertEqual(f["base"], Decimal("0"))       # reported while still open: nothing dealt yet
        self.client.poll_states = ["closed"]           # the remainder filled before the cancel landed
        self.assertEqual(b.resolve_pending(), [])
        late = b.drain_late_fills()
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["base"], Decimal("100.00"))
        self.assertEqual(late[0]["quote"], Decimal("23000000"))
        self.assertEqual(b.drain_late_fills(), [])


    # ---- review round 2
    def test_404_for_a_known_order_id_falls_back_to_the_identifier_lookup(self):
        b = self.broker()
        b.journal.add("id-1", symbol="USDT_IRT", side="buy", quote_amount="23000000", status="open", order_id="555")

        def gone(oid):
            raise BitpinAPIError(404, "not_found", {"detail": "Not found."})
        self.client.get_order = gone
        self.client.orders[9] = {"id": 9, "identifier": "id-1", "symbol": "USDT_IRT", "side": "buy", "state": "closed",
                                 "dealed_base_amount": "100", "dealed_quote_amount": "23000000", "commission": "0.35"}
        self.assertEqual(b.resolve_pending(), [])
        e = b.journal.get("id-1")
        self.assertEqual((e["status"], D(e["reported_base"])), ("closed", Decimal("100")))

    def test_404_and_not_found_is_aged_out_only_when_expiry_is_allowed(self):
        for expire in (True, False):
            d = tempfile.mkdtemp(prefix="bitpin_live_404_")
            self.addCleanup(shutil.rmtree, d, True)
            b = LiveBroker(self.client, MARKETS, d, sleep=self.clock.sleep, clock=self.clock)
            b.expire_unknown_orders = expire
            b.journal.add("id-2", symbol="USDT_IRT", side="buy", quote_amount="23000000", status="open", order_id="556")
            self.client.get_order = lambda oid: (_ for _ in ()).throw(BitpinAPIError(404, "not_found", None))
            self.clock.t += 901
            pend = b.resolve_pending()
            if expire:
                self.assertEqual(pend, [])
                self.assertEqual(b.journal.get("id-2")["status"], "not_found")
            else:                                   # capped sleeve: never silently aged out
                self.assertEqual(len(pend), 1)
                self.assertTrue(pend[0]["stuck"])
                self.assertEqual(b.journal.get("id-2")["status"], "open")

    def test_unknown_order_is_never_aged_out_for_a_capped_sleeve(self):
        self.client.place_error = OrderStatusUnknown("x", "timeout")
        b = self.broker(unknown_order_max_age=900)
        b.expire_unknown_orders = False
        with self.assertRaises(OrderStatusUnknown):
            b.market_buy("USDT_IRT", Decimal("23000000"))
        self.clock.t += 901
        pend = b.resolve_pending()
        self.assertEqual(len(pend), 1)
        self.assertTrue(pend[0]["stuck"] and pend[0]["not_found"])
        self.assertEqual(b.journal.get(self.client.placed[0]["identifier"])["status"], "unknown")

    def test_any_lookup_exception_is_counted_not_raised(self):
        self.client.place_error = OrderStatusUnknown("x", "timeout")
        b = self.broker()
        with self.assertRaises(OrderStatusUnknown):
            b.market_buy("USDT_IRT", Decimal("23000000"))

        def refused(ident):
            raise ValueError("endpoint not allowed by this client: GET /v1/odr/orders/")
        self.client.find_order_by_identifier = refused
        pend = b.resolve_pending()
        self.assertEqual(pend[0]["lookup_failures"], 1)

    def test_an_order_is_never_closed_without_its_fill(self):
        self.client.place_error = OrderStatusUnknown("x", "timeout")
        b = self.broker()
        with self.assertRaises(OrderStatusUnknown):
            b.market_buy("USDT_IRT", Decimal("23000000"))
        ident = self.client.placed[0]["identifier"]
        self.client.orders[7] = {"id": 7, "identifier": ident, "symbol": "USDT_IRT", "side": "buy", "state": "closed",
                                 "dealed_base_amount": "garbage", "dealed_quote_amount": "23000000"}
        self.assertEqual(len(b.resolve_pending()), 1)
        self.assertEqual(b.journal.get(ident)["status"], "unknown")
        self.client.orders[7]["dealed_base_amount"] = "100"
        self.assertEqual(b.resolve_pending(), [])
        self.assertEqual(b.journal.get(ident)["status"], "closed")

    def test_failed_journal_write_is_rewritten_before_the_next_resolve(self):
        import bitpin.broker as broker_mod
        b = self.broker()
        real = broker_mod.atomic_write_json
        fails = {"n": 1}

        def flaky(path, obj, private=False):
            if fails["n"] and any(e.get("status") == "closed" for e in obj["orders"].values()):
                fails["n"] -= 1
                raise PermissionError(13, "locked by a sync client")
            return real(path, obj, private)
        broker_mod.atomic_write_json = flaky
        self.addCleanup(setattr, broker_mod, "atomic_write_json", real)
        f = b.market_buy("USDT_IRT", Decimal("23000000"))   # the 'closed' write fails: fill still returned
        self.assertEqual(f["identifier"], self.client.placed[0]["identifier"])
        self.assertTrue(b.journal.dirty)
        self.assertEqual(b.fill_records()[0]["base"], Decimal("100.00"))   # in memory
        b.resolve_pending()
        self.assertFalse(b.journal.dirty)
        from bitpin.api import read_json
        self.assertEqual(read_json(b.journal.path)["orders"][f["identifier"]]["status"], "closed")

    def test_journal_add_failure_sends_nothing(self):
        import bitpin.broker as broker_mod
        from bitpin.broker import BrokerError
        b = self.broker()
        real = broker_mod.atomic_write_json

        def broken(path, obj, private=False):
            raise PermissionError(13, "disk locked")
        broker_mod.atomic_write_json = broken
        self.addCleanup(setattr, broker_mod, "atomic_write_json", real)
        with self.assertRaises(BrokerError):
            b.market_buy("USDT_IRT", Decimal("23000000"))
        self.assertEqual(self.client.placed, [])
        self.assertEqual(b.journal.unresolved(), [])

    def test_fill_records_hold_the_cumulative_fill(self):
        b = self.broker()
        f = b.market_buy("USDT_IRT", Decimal("23000000"))
        recs = b.fill_records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["identifier"], r["symbol"], r["side"], r["base"], r["quote"], r["fee"], r["fee_asset"]),
                         (f["identifier"], "USDT_IRT", "buy", f["base"], f["quote"], f["fee"], "USDT"))
        b2 = LiveBroker(self.client, MARKETS, self.dir, sleep=self.clock.sleep, clock=self.clock)   # restart
        self.assertEqual(b2.fill_records()[0]["base"], f["base"])

    def test_quotes_other_than_irt_and_usdt_are_refused(self):
        from bitpin.broker import BrokerError
        eur = Market("BTC_EUR", "BTC", "EUR", True, False, 2, 8, 2)
        b = LiveBroker(self.client, MarketCache(markets=[eur, USDT]), self.dir, sleep=self.clock.sleep, clock=self.clock)
        with self.assertRaises(BrokerError):
            b.market_buy("BTC_EUR", Decimal("100"))
        self.assertEqual(self.client.placed, [])

    def test_fill_records_carry_kind_and_quote_asset(self):
        b = self.broker()
        b.market_buy("USDT_IRT", Decimal("23000000"))
        r = b.fill_records()[0]
        self.assertEqual((r["kind"], r["quote_asset"]), ("market", "IRT"))
        # a journal written before order kinds existed is read as market orders
        b.journal.add("old-1", symbol="USDT_IRT", side="buy", quote_amount="23000000", status="unknown")
        self.assertEqual({x["identifier"]: x["kind"] for x in b.fill_records()}["old-1"], "market")
        self.assertEqual(b.unresolved_count(), 1)

    def test_manual_resolution(self):
        from bitpin.broker import BrokerError
        self.client.place_error = OrderStatusUnknown("x", "timeout")
        b = self.broker()
        for _ in range(2):
            with self.assertRaises(OrderStatusUnknown):
                b.market_buy("USDT_IRT", Decimal("23000000"))
        id1, id2 = (p["identifier"] for p in self.client.placed)
        self.client.orders[77] = {"id": 77, "symbol": "USDT_IRT", "side": "sell", "state": "closed",
                                  "dealed_base_amount": "100", "dealed_quote_amount": "23000000", "commission": "1"}
        self.client.get_order = lambda oid: dict(self.client.orders[int(oid)])
        with self.assertRaises(BrokerError):            # wrong side: refused
            b.resolve_manually(id1, order_id=77)
        self.client.orders[77].update(side="buy", state="active")
        with self.assertRaises(BrokerError):            # not final yet: refused
            b.resolve_manually(id1, order_id=77)
        self.client.orders[77].update(state="closed")
        fill = b.resolve_manually(id1, order_id="77")
        self.assertEqual((fill["base"], fill["identifier"]), (Decimal("100"), id1))
        self.assertEqual(b.journal.get(id1)["status"], "closed")
        b.resolve_manually(id2, not_executed=True)
        self.assertEqual(b.journal.get(id2)["status"], "not_found")
        self.assertEqual(b.journal.unresolved(), [])
        with self.assertRaises(BrokerError):            # already resolved
            b.resolve_manually(id2, not_executed=True)


class PaperPersistTest(unittest.TestCase):
    def test_paper_fills_are_remembered_with_the_balances(self):
        d = tempfile.mkdtemp(prefix="bitpin_paper_rec_")
        self.addCleanup(shutil.rmtree, d, True)
        b = PaperBroker(MARKETS, d, capital_irt="10000", book_source=lambda s: BOOK)
        f = b.market_buy("USDT_IRT", Decimal("1505"))
        self.assertEqual(f["identifier"], f["order_id"])
        recs = PaperBroker(MARKETS, d, book_source=lambda s: BOOK).fill_records()   # after a restart
        self.assertEqual([(r["identifier"], r["base"], r["quote"], r["fee"]) for r in recs],
                         [(f["identifier"], f["base"], f["quote"], f["fee"])])

    def test_dry_run_paper_account_writes_nothing(self):
        d = tempfile.mkdtemp(prefix="bitpin_paper_np_")
        self.addCleanup(shutil.rmtree, d, True)
        sub = os.path.join(d, "state")
        b = PaperBroker(MARKETS, sub, capital_irt="10000", book_source=lambda s: BOOK, persist=False)
        b.market_buy("USDT_IRT", Decimal("1505"))
        self.assertEqual(b.balances()["USDT"], Decimal("14.94"))
        self.assertFalse(os.path.exists(sub))


class LiveBrokerWithRealClientTest(unittest.TestCase):
    """End to end through BitpinClient: the POST times out, the order is found by identifier,
    exactly one order is sent."""

    def test_timeout_no_duplicate(self):
        import json
        import urllib.parse
        d = tempfile.mkdtemp(prefix="bitpin_e2e_")
        self.addCleanup(shutil.rmtree, d, True)
        clock = Clock()
        calls = []
        placed = {}

        def transport(method, url, headers, body, timeout):
            u = urllib.parse.urlparse(url)
            q = dict(urllib.parse.parse_qsl(u.query))
            calls.append((method, u.path))
            if u.path == "/api/v1/usr/authenticate/":
                return 200, json.dumps({"access": "a.b.c", "refresh": "d.e.f"}).encode()
            if u.path == "/api/v1/wlt/wallets/":
                return 200, json.dumps([{"asset": "IRT", "balance": "50000000", "frozen": "0", "service": "main"}]).encode()
            if method == "POST" and u.path == "/api/v1/odr/orders/":
                b = json.loads(body.decode())
                placed["o"] = {"id": 9, "identifier": b["identifier"], "symbol": b["symbol"], "side": b["side"],
                               "state": "closed", "dealed_base_amount": "10", "dealed_quote_amount": b["quote_amount"],
                               "commission": "0.035"}
                raise TransportError("timed out", timeout=True)   # the exchange got it, we did not hear back
            if method == "GET" and u.path == "/api/v1/odr/orders/":
                o = placed.get("o")
                return 200, json.dumps([o] if o and q.get("identifier") == o["identifier"] else []).encode()
            raise AssertionError((method, url))

        client = BitpinClient(api_key="k" * 20, secret_key="s" * 20, state_dir=d, transport=transport, clock=clock,
                              sleep=clock.sleep, order_lookup_delay=0.5)
        b = LiveBroker(client, MARKETS, d, sleep=clock.sleep, clock=clock)
        f = b.market_buy("USDT_IRT", Decimal("2300000"))
        self.assertEqual(f["base"], Decimal("10"))
        self.assertEqual(sum(1 for c in calls if c == ("POST", "/api/v1/odr/orders/")), 1)


if __name__ == "__main__":
    unittest.main()
