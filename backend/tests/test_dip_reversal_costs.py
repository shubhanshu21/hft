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
        self.assertEqual(len(S.COINS), 9)


if __name__ == "__main__":
    unittest.main()
