"""Public flow contracts; expectations are independent hand-calculated fixtures."""
import importlib
import importlib.util
import io
import json
import unittest
from decimal import Decimal
from urllib.error import HTTPError


def trade(tid="a", side="buy", quote="200", ts=990):
    return {"id": tid, "price": "100", "base_amount": str(Decimal(quote) / 100), "quote_amount": quote,
            "side": side, "time": ts}


def liquidity():
    return {"peggedAssets": [
        {"id": "1", "symbol": "USDT", "pegType": "peggedUSD", "price": 0.99,
         "circulating": {"peggedUSD": 110}, "circulatingPrevDay": {"peggedUSD": 100},
         "circulatingPrevWeek": {"peggedUSD": 80}, "circulatingPrevMonth": {"peggedUSD": 50}},
        {"id": "2", "symbol": "USDC", "pegType": "peggedUSD", "price": 1.01,
         "circulating": {"peggedUSD": 90}, "circulatingPrevDay": {"peggedUSD": 100},
         "circulatingPrevWeek": {"peggedUSD": 120}, "circulatingPrevMonth": {"peggedUSD": 150}}]}


class FlowFeaturesTests(unittest.TestCase):
    def setUp(self):
        for name in ("bitpin.flow_features", "bitpin.flow_public"):
            if importlib.util.find_spec(name) is None:
                self.fail("shadow feature is not implemented: " + name)
        self.f = importlib.import_module("bitpin.flow_features")
        self.p = importlib.import_module("bitpin.flow_public")

    def test_taker_sides_preserved_and_duplicates_collapse(self):
        rows = self.f.normalize_trades([trade(), trade(), trade("b", "sell", "100", 989)], 1000)
        self.assertEqual([x["id"] for x in rows], ["b", "a"])
        self.assertEqual([x["side"] for x in rows], ["sell", "buy"])
        self.assertEqual(rows[1]["quote"], "200")

    def test_invalid_trade_never_partly_accepted(self):
        cases = [("quote_amount", "NaN"), ("base_amount", "-1"), ("price", True),
                 ("time", 1020), ("side", "maker"), ("id", ""), ("quote_amount", "Infinity")]
        for field, value in cases:
            bad = trade("bad"); bad[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.f.normalize_trades([trade(), bad], 1000)

    def test_conflicting_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            self.f.normalize_trades([trade(), trade(side="sell")], 1000)

    def test_dust_trade_with_rounded_zero_quote_uses_price_times_base(self):
        # Real public XRP_USDT payload: reported quote rounds a real fill down to 0.00.
        row = {"id": "dust", "price": "1.40578", "base_amount": "0.0070",
               "quote_amount": "0.00", "side": "buy", "time": 990}
        result = self.f.normalize_trades([row], 1000)
        self.assertEqual(result[0]["quote"], "0.00984046")

    def test_quote_notional_is_reproducible_from_price_and_base(self):
        row = {"id": "rounded", "price": "1.40578", "base_amount": "1.0070",
               "quote_amount": "1.41", "side": "sell", "time": 990}
        self.assertEqual(self.f.normalize_trades([row], 1000)[0]["quote"], "1.41562046")

    def test_book_sorts_sides_and_marks_truncated_depth(self):
        result = self.f.book_features({"bids": [["99.5", "2"], ["99.9", "1"]],
                                       "asks": [["100.5", "1"], ["100.1", "1"]]})
        self.assertAlmostEqual(result["spread_pct"], 0.2)
        self.assertEqual(result["bands"]["1"]["complete"], False)
        self.assertAlmostEqual(result["bands"]["1"]["bid_quote"], 298.9)
        self.assertAlmostEqual(result["bands"]["1"]["ask_quote"], 200.6)
        self.assertEqual(result["bands"]["0.1"]["complete"], True)

    def test_crossed_or_nonfinite_book_rejected(self):
        for bids, asks in [([["101", "1"]], [["100", "1"]]),
                           ([["99", "NaN"]], [["100", "1"]]), ([], [["100", "1"]])]:
            with self.assertRaises(ValueError):
                self.f.book_features({"bids": bids, "asks": asks})

    def test_supply_delta_ignores_price_and_does_not_sum_chains(self):
        payload = liquidity()
        payload["peggedAssets"][0]["chainCirculating"] = {"bridge": {"current": {"peggedUSD": 9000}}}
        result = self.f.liquidity_features(payload)
        self.assertEqual(result["assets"]["USDT"]["change_1d_units"], 10)
        self.assertEqual(result["assets"]["USDT"]["change_1d_pct"], 10)
        self.assertEqual(result["assets"]["USDC"]["change_1d_units"], -10)
        self.assertEqual(result["combined_change_1d_units"], 0)
        self.assertIsNone(result["provider_time"])

    def test_missing_previous_supply_is_unknown(self):
        payload = liquidity(); del payload["peggedAssets"][0]["circulatingPrevDay"]
        result = self.f.liquidity_features(payload)
        self.assertIsNone(result["assets"]["USDT"]["change_1d_units"])
        self.assertIsNone(result["combined_change_1d_units"])

    def test_wrong_identity_and_missing_stablecoin_rejected(self):
        for payload in ({"peggedAssets": []}, {"peggedAssets": [dict(liquidity()["peggedAssets"][0], id="other")]}):
            with self.assertRaises(ValueError):
                self.f.liquidity_features(payload)

    def test_transport_refuses_private_paths_and_external_hosts(self):
        client = self.p.PublicFlowClient()
        for url in ("https://api.bitpin.org/api/v1/wlt/wallets/", "http://api.bitpin.org/api/v1/mth/matches/BTC_IRT/",
                    "https://evil.test/", "https://api.bitpin.org@evil.test/api/v1/mth/matches/BTC_IRT/"):
            with self.assertRaises(ValueError):
                client.get_json(url)
        with self.assertRaises(ValueError):
            client.matches("../private")

    def test_transport_has_no_auth_and_bounds_response(self):
        class Response(io.BytesIO):
            status = 200
        class Opener:
            def open(self, req, timeout):
                self.request = req
                return Response(b'[]')
        opener = Opener(); client = self.p.PublicFlowClient(opener=opener)
        self.assertEqual(client.matches("BTC_USDT"), [])
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertIsNone(opener.request.get_header("Authorization"))
        self.assertIsNone(opener.request.data)
        class Huge:
            def open(self, req, timeout):
                return Response(b' ' * 101)
        with self.assertRaises(self.p.PublicDataError):
            self.p.PublicFlowClient(opener=Huge(), max_bytes=100).matches("BTC_USDT")

    def test_rate_limit_preserves_retry_after(self):
        body = io.BytesIO()
        class Opener:
            def open(self, req, timeout):
                raise HTTPError(req.full_url, 429, "rate limited", {"Retry-After": "120"}, body)
        with self.assertRaises(self.p.PublicDataError) as caught:
            self.p.PublicFlowClient(opener=Opener()).matches("BTC_USDT")
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertTrue(body.closed)

    def test_redirects_are_never_followed(self):
        with self.assertRaises(self.p.PublicDataError):
            self.p.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test")


if __name__ == "__main__":
    unittest.main()
