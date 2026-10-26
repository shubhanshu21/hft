"""services/broker/delta_broker.py -- the only code path that places a (demo) order anywhere.

Nothing here touches the network: requests is patched out. What is pinned down is the part a wrong edit would silently break --
the signature string, the testnet-only host, and the guards that stop a bad size or an unknown coin from reaching the API.
"""
import hashlib
import hmac
import unittest
from unittest import mock

from services.broker import delta_broker as db

ENV = {"DELTA_DEMO_API_KEY": "key-123", "DELTA_DEMO_API_SECRET": "secret-456"}


def _broker():
    with mock.patch.dict("os.environ", ENV):
        return db.DeltaDemoBroker()


class TestConstruction(unittest.TestCase):
    def test_missing_keys_raise_instead_of_calling_an_anonymous_api(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                db.DeltaDemoBroker()

    def test_keys_come_from_the_environment(self):
        b = _broker()
        self.assertEqual((b.key, b.secret), (ENV["DELTA_DEMO_API_KEY"], ENV["DELTA_DEMO_API_SECRET"]))


class TestTestnetOnly(unittest.TestCase):
    def test_the_client_is_hardcoded_to_the_demo_host(self):
        # There is deliberately no live host anywhere in the module: a wrong-sized order can only ever be a demo order.
        self.assertEqual(db.HOST, "https://cdn-ind.testnet.deltaex.org")
        self.assertIn("testnet", db.HOST)

    def test_no_live_host_is_defined(self):
        hosts = [v for k, v in vars(db).items() if k.isupper() and isinstance(v, str) and v.startswith("http")]
        self.assertEqual(hosts, [db.HOST])


class TestSigning(unittest.TestCase):
    def test_signature_is_hmac_sha256_over_method_timestamp_path_query_body(self):
        b = _broker()
        ts = "1700000000"
        expected = hmac.new(b"secret-456", b"POST" + ts.encode() + b"/v2/orders" + b"?" + b'{"size":1}', hashlib.sha256).hexdigest()
        self.assertEqual(b._sign("POST", "/v2/orders", "?", '{"size":1}', ts), expected)

    def test_a_request_signs_what_it_sends(self):
        b = _broker()
        sent = {}

        def fake_request(method, url, headers, data, timeout):
            sent.update(method=method, url=url, headers=headers, data=data)
            return mock.Mock(json=lambda: {"success": True, "result": {"id": 1}})

        with mock.patch.object(db.requests, "request", side_effect=fake_request):
            b.place_market_order("BTC", "buy", 3)

        body = sent["data"]
        self.assertEqual(body, '{"product_id":84,"size":3,"side":"buy","order_type":"market_order"}')       # compact: the signature covers these exact bytes
        self.assertEqual(sent["url"], db.HOST + "/v2/orders")
        self.assertEqual(sent["headers"]["api-key"], "key-123")
        self.assertEqual(sent["headers"]["signature"], b._sign("POST", "/v2/orders", "", body, sent["headers"]["timestamp"]))

    def test_an_error_response_raises_with_the_parsed_error(self):
        b = _broker()
        payload = {"success": False, "error": {"code": "insufficient_margin", "context": {}}}
        with mock.patch.object(db.requests, "request", return_value=mock.Mock(status_code=400, json=lambda: payload)):
            with self.assertRaises(db.DeltaAPIError) as ctx:
                b.place_market_order("BTC", "buy", 1)
        self.assertEqual(ctx.exception.error["code"], "insufficient_margin")
        self.assertEqual(ctx.exception.status, 400)


class TestOrderGuards(unittest.TestCase):
    def setUp(self):
        self.b = _broker()

    def test_an_unknown_coin_never_reaches_the_api(self):
        with mock.patch.object(db.requests, "request") as req:
            with self.assertRaises(ValueError):
                self.b.place_market_order("LINK", "buy", 1)          # LINK is not on Delta's testnet product list
            with self.assertRaises(ValueError):
                self.b.place_limit_order("LINK", "sell", 1, 100.0)
            with self.assertRaises(ValueError):
                self.b.cancel_order("LINK", 7)                        # used to be an unguarded PRODUCTS[...] KeyError
        req.assert_not_called()

    def test_a_zero_or_negative_size_is_refused(self):
        with mock.patch.object(db.requests, "request") as req:
            for bad in (0, -1):
                with self.assertRaises(ValueError):
                    self.b.place_market_order("BTC", "buy", bad)
                with self.assertRaises(ValueError):
                    self.b.place_limit_order("BTC", "sell", bad, 100.0)
        req.assert_not_called()

    def test_known_products_are_the_six_testnet_coins(self):
        self.assertEqual(set(db.PRODUCTS), {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA"})


class TestFitMargin(unittest.TestCase):
    """Delta demo funds are small, so a too-big order is scaled down to what the account can afford -- but never below 1 contract."""

    def setUp(self):
        self.b = _broker()

    def _insufficient(self, avail, extra):
        return db.DeltaAPIError(400, "POST", "/v2/orders", {"code": "insufficient_margin", "context": {"available_balance": avail, "required_additional_balance": extra}})

    def test_it_places_the_full_size_when_it_fits(self):
        with mock.patch.object(self.b, "place_market_order", return_value={"id": 1}) as place:
            self.assertEqual(self.b.place_market_order_fit_margin("BTC", "buy", 10), {"id": 1})
        place.assert_called_once_with("BTC", "buy", 10)

    def test_it_scales_down_to_what_the_account_can_afford(self):
        with mock.patch.object(self.b, "place_market_order") as place:
            place.side_effect = [self._insufficient(avail=50.0, extra=50.0), {"id": 2}]
            order = self.b.place_market_order_fit_margin("BTC", "buy", 10)
        self.assertEqual(order, {"id": 2})
        self.assertEqual(place.call_args_list[1].args, ("BTC", "buy", 4))     # 10 * 50/100 * 0.95 = 4 (5% headroom)
        self.assertLess(place.call_args_list[1].args[2], 10)

    def test_it_never_scales_up_or_repeats_the_same_size(self):
        with mock.patch.object(self.b, "place_market_order") as place:
            place.side_effect = [self._insufficient(avail=99.0, extra=1.0), {"id": 3}]
            self.b.place_market_order_fit_margin("BTC", "buy", 3)
        self.assertLessEqual(place.call_args_list[1].args[2], 2)

    def test_a_single_contract_that_does_not_fit_is_not_retried(self):
        with mock.patch.object(self.b, "place_market_order", side_effect=self._insufficient(1.0, 1.0)) as place:
            with self.assertRaises(db.DeltaAPIError):
                self.b.place_market_order_fit_margin("BTC", "buy", 1)
        self.assertEqual(place.call_count, 1)

    def test_other_errors_are_not_retried(self):
        err = db.DeltaAPIError(400, "POST", "/v2/orders", {"code": "invalid_size", "context": {}})
        with mock.patch.object(self.b, "place_market_order", side_effect=err) as place:
            with self.assertRaises(db.DeltaAPIError):
                self.b.place_market_order_fit_margin("BTC", "buy", 10)
        self.assertEqual(place.call_count, 1)


class TestPositionsAndClose(unittest.TestCase):
    def setUp(self):
        self.b = _broker()

    def test_positions_are_keyed_by_coin_with_signed_size(self):
        rows = [
            {"size": "5", "entry_price": "100.5", "product": {"underlying_asset": {"symbol": "BTC"}}},
            {"size": "-3", "entry_price": "2.0", "product": {"underlying_asset": {"symbol": "ADA"}}},
            {"size": "0", "entry_price": "1", "product": {"underlying_asset": {"symbol": "DOGE"}}},      # flat: skipped
        ]
        with mock.patch.object(self.b, "_request", return_value=rows):
            self.assertEqual(self.b.positions(), {"BTC": {"size": 5.0, "entry_price": 100.5}, "ADA": {"size": -3.0, "entry_price": 2.0}})

    def test_closing_a_flat_coin_does_nothing(self):
        with mock.patch.object(self.b, "positions", return_value={}), mock.patch.object(self.b, "place_market_order") as place:
            self.assertIsNone(self.b.close_position("BTC"))
        place.assert_not_called()

    def test_closing_a_long_sells_and_a_short_buys(self):
        with mock.patch.object(self.b, "positions", return_value={"BTC": {"size": 4.0, "entry_price": 1}}), \
             mock.patch.object(self.b, "place_market_order", return_value={"id": 9}) as place:
            self.b.close_position("BTC")
        place.assert_called_once_with("BTC", "sell", 4)

        with mock.patch.object(self.b, "positions", return_value={"ADA": {"size": -2.0, "entry_price": 1}}), \
             mock.patch.object(self.b, "place_market_order", return_value={"id": 10}) as place:
            self.b.close_position("ADA")
        place.assert_called_once_with("ADA", "buy", 2)


if __name__ == "__main__":
    unittest.main()
