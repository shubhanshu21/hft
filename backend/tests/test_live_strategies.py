"""crypto/dip_reversal (entry rule, crowd rule, business-income tax) and the MCX cost model kept for research
(commodity/trend_carry itself was removed 2026-11-19 at the user's request)."""
import unittest

import pandas as pd

from markets.commodity.costs import compute_mcx_costs
from markets.crypto.strategies.tf_5min.dip_reversal import strategy as DR


class TestMcxCosts(unittest.TestCase):
    def test_long_round_trip(self):
        c = compute_mcx_costs("GOLDTEN", "long", 12_000, 12_100, 10)      # 10 lots x 1 unit
        self.assertEqual(c["qty"], 10)
        self.assertEqual(c["gross"], 1_000.0)
        self.assertEqual(c["brokerage"], 60.0)                             # Rs30 per order (Upstox Plus) x 2
        self.assertAlmostEqual(c["ctt"], 121_000 * 0.0001, places=2)        # CTT on the SELL side only
        self.assertAlmostEqual(c["stamp_duty"], 120_000 * 0.00002, places=2)
        self.assertAlmostEqual(c["net"], c["gross"] - c["total"], places=2)

    def test_short_pays_ctt_on_its_entry_sell(self):
        c = compute_mcx_costs("GOLDTEN", "short", 12_000, 11_900, 10)
        self.assertEqual(c["gross"], 1_000.0)
        self.assertAlmostEqual(c["ctt"], 120_000 * 0.0001, places=2)


class TestDipReversalRule(unittest.TestCase):
    def _bars(self, close, ema50, bb_lower20):
        n = len(close)
        return pd.DataFrame({"close": close, "ema50": ema50, "bb_lower20": bb_lower20, "lower40": [0.0] * n,
                             "bbdelta": [0.0] * n, "closedelta": [0.0] * n, "tail": [0.0] * n})

    def test_cluc_rule_needs_close_under_ema_and_well_under_low_band(self):
        df = self._bars(close=[97.0, 98.9, 97.0], ema50=[100.0, 100.0, 95.0], bb_lower20=[100.0, 100.0, 100.0])
        # bar 0: under EMA and under 0.985 x band -> entry; bar 1: above 98.5 -> no; bar 2: above its EMA -> no
        self.assertEqual(DR.entry_signal(df).tolist(), [True, False, False])

    def test_crowd_rule_is_off_in_production(self):
        self.assertIsNone(DR.CROWD_K)
        self.assertEqual(DR.crowd_scale(10), 1.0)
        self.assertAlmostEqual(DR.crowd_scale(8, k=4), 0.5)
        self.assertEqual(DR.crowd_scale(3, k=4), 1.0)


class TestBusinessTax(unittest.TestCase):
    def test_profit_year_is_taxed_and_loss_is_carried_forward(self):
        idx = pd.to_datetime(["2023-03-31", "2024-03-31", "2025-03-31"])
        curve = pd.Series([80_000.0, 120_000.0, 120_000.0], index=idx)     # FY23 -20k, FY24 +40k
        after, paid = DR.business_tax(curve, 100_000)
        # FY24 profit 40k minus the 20k carried loss -> 20k taxed at 31.2%
        self.assertAlmostEqual(paid, 0.312 * 20_000, places=2)
        self.assertAlmostEqual(after.iloc[-1], 120_000 - 0.312 * 20_000, places=2)


if __name__ == "__main__":
    unittest.main()
