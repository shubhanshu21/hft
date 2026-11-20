"""dip_reversal costs = Delta India's real order book (2026-11-10 check): each coin's own half-spread, and DOT/ADA dropped for costing 6-10bp a side."""
import unittest

from markets.crypto.strategies.tf_5min.dip_reversal import strategy as S


class RealCostsTest(unittest.TestCase):
    def test_every_traded_coin_has_its_own_measured_slippage(self):
        self.assertEqual(set(S.COINS) - set(S.SLIP), set(), "no coin may fall back to the 1.5bp default")
        self.assertGreaterEqual(S.SLIP["AVAX"], 0.0004)            # ~4bp a side on Delta, not 1.5bp
        self.assertGreaterEqual(S.SLIP["LTC"], 0.0002)

    def test_dot_and_ada_are_not_traded(self):
        self.assertNotIn("DOT", S.COINS)
        self.assertNotIn("ADA", S.COINS)
        self.assertEqual(len(S.COINS), 10)

    def test_zec_is_traded_at_its_measured_spread(self):
        """2026-11-20 (user's yes): ZEC added -- Delta half-spread 0.2bp measured, charged 0.5bp (as BTC)."""
        self.assertIn("ZEC", S.COINS)
        self.assertGreaterEqual(S.SLIP["ZEC"], 0.00005)

    def test_volatility_basket_stays_the_nine_tested_coins(self):
        """Every result is sized from the 9-coin basket's volatility; ZEC (very volatile) must not change everyone's size."""
        self.assertEqual(S.VOL_COINS, ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LTC", "TRX"))
        self.assertNotIn("ZEC", S.VOL_COINS)


class DataAndContractsTest(unittest.TestCase):
    def test_every_traded_coin_is_topped_up_nightly_and_has_a_delta_contract_size(self):
        from markets.crypto import data as D
        from engine import crypto_dip_reversal as E
        self.assertEqual(set(S.COINS) - set(D.COINS), set())
        self.assertEqual(set(S.COINS) - set(E.CONTRACT_VALUE), set())
        self.assertEqual(E.CONTRACT_VALUE["ZEC"], 0.1)                 # api.india.delta.exchange/v2/products/ZECUSD, 2026-11-20


if __name__ == "__main__":
    unittest.main()
