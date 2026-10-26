"""The two strategies that paper trade now: commodity/trend_carry (ranking, tie-break, lot sizing, MCX costs) and crypto/dip_reversal
(entry rule, crowd rule, business-income tax)."""
import unittest

import numpy as np
import pandas as pd

from markets.commodity.costs import compute_mcx_costs
from markets.commodity.strategies.tf_daily.trend_carry import strategy as TC
from markets.crypto.strategies.tf_5min.dip_reversal import strategy as DR


def _panel(cols, days=300, seed=0):
    """Daily returns and carry (2026-10-25: trend_carry ranks on 252 trading days of daily data, re-checked every 10 days)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=days)
    R = pd.DataFrame(rng.normal(0.0005, 0.015, (days, len(cols))), index=idx, columns=cols)
    C = pd.DataFrame(rng.normal(0, 0.01, (days, len(cols))), index=idx, columns=cols)
    return R, C


class TestTrendCarryTargets(unittest.TestCase):
    def test_weights_sum_to_one_and_pick_top_n(self):
        R, C = _panel(list(TC.SIGNALS))
        w = TC.targets(R, C, R.index[-1])
        self.assertEqual(len(w), TC.top_n(len(TC.SIGNALS)))
        self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_pick_does_not_depend_on_column_order(self):
        # 2026-10-23 bug: ties went to list order, so the website and the paper engine could hold different contracts
        R, C = _panel(list(TC.SIGNALS), seed=3)
        C[:] = 0.0                                     # carry all equal -> many rank-sum ties
        t = R.index[-1]
        rev = list(reversed(TC.SIGNALS))
        self.assertEqual(TC.targets(R, C, t), TC.targets(R[rev], C[rev], t))

    def test_not_enough_history_gives_no_signal(self):
        R, C = _panel(list(TC.SIGNALS), days=150)          # under ~0.8 x 252 days of trend
        self.assertEqual(TC.targets(R, C, R.index[-1]), {})


class TestTrendCarryRebalanceDue(unittest.TestCase):
    def test_every_ten_trading_days(self):
        days = list(pd.bdate_range("2026-10-01", periods=15))
        self.assertTrue(TC.rebalance_due(None, days, days[0]))                                  # never checked
        self.assertFalse(TC.rebalance_due(str(days[0].date()), days, days[9]))                 # 9 trading days later
        self.assertTrue(TC.rebalance_due(str(days[0].date()), days, days[TC.REBALANCE_DAYS]))  # 10 trading days later
        self.assertEqual(TC.REBALANCE_DAYS, 10)


class TestTrendCarryLots(unittest.TestCase):
    def test_rounds_to_nearest_lot(self):
        # Rs2.5L x 1.5 x 1/3 = Rs1.25L share; one GOLDTEN lot at 12,000 x 1 unit -> 10.4 lots -> 10
        self.assertEqual(TC.lots_for(250_000, 1.5, 1 / 3, 12_000, "GOLDM"), 10)

    def test_a_share_over_half_a_lot_buys_one(self):
        # ZINCMINI lot = 415 x 1000 = Rs4.15L; share 2.5L x 1.5 x 0.6 = Rs2.25L (> half a lot) -> 1 lot, not 0 (2026-10-22 fix)
        self.assertEqual(TC.lots_for(250_000, 1.5, 0.6, 415, "ZINC"), 1)

    def test_rounding_up_never_goes_past_two_times_the_account(self):
        # one SILVERMIC lot (Rs3L) is more than half the share, but buying it would exceed 2x a Rs1L account -> stays at 0
        self.assertEqual(TC.lots_for(100_000, 1.5, 1.0, 300_000, "SILVERM"), 0)
        # the cap only limits rounding UP: it never cuts below the old round-down lots
        floor = int(min(250_000 * 10 // 12_000, 250_000 * TC.MAX_MARGIN_USE // (12_000 * TC.MARGIN_PCT["GOLDM"])))
        self.assertEqual(TC.lots_for(250_000, 10.0, 1.0, 12_000, "GOLDM"), floor)


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
