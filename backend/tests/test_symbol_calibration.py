import unittest

from markets.commodity.experiments import symbol_calibration_study as s


class StatsTests(unittest.TestCase):
    def test_stats_profit_factor_and_credibility(self):
        trades = [{"net_pnl": 100.0}] * 15 + [{"net_pnl": -50.0}] * 5
        st = s.stats(trades)
        self.assertEqual(st["n"], 20)
        self.assertEqual(st["net"], 1250)
        self.assertEqual(st["pf"], 6.0)
        self.assertTrue(s.credible(st))
        self.assertFalse(s.credible(s.stats(trades[:14])))

    def test_every_candidate_has_a_threshold_row(self):
        from markets.commodity.scalping import backtest as bt
        for sym, key in s.CANDIDATES.items():
            self.assertIn(key, bt.ENTRY_THRESHOLDS, sym)
            for k in s.GRID:
                self.assertIn(k, bt.ENTRY_THRESHOLDS[key])

    def test_empty_trades(self):
        self.assertEqual(s.stats([])["n"], 0)


class LiquidityGateTests(unittest.TestCase):
    def test_illiquid_contracts_are_excluded_before_any_backtest(self):
        from markets.commodity.experiments import alt_strategy_study as a
        for sym in ("LEADMINI", "NICKEL"):          # 80-90% zero-volume bars in the archive
            self.assertEqual(a.study_symbol(sym, {})["verdict"], "ILLIQUID")
        self.assertLess(a.liquidity("CRUDEOILM")["zero_volume_share"], a.MAX_ZERO_VOLUME_SHARE)


class SwitchTests(unittest.TestCase):
    def test_performance_switch_uses_only_past_days(self):
        import numpy as np
        from markets.commodity.experiments import regime_switch_study as r
        a = np.array([10.0] * 5 + [-100.0] * 5)      # strategy A wins first, then collapses
        b = np.array([-1.0] * 10)
        pnl, picks = r.performance_switch({"A": a, "B": b}, 3)
        self.assertEqual(picks[:2], ["A", "A"])          # day 3, 4 follow A's past wins
        self.assertEqual(pnl[2], -100.0)                 # day 5 is the first bad A day: the switch only reacts afterwards
        self.assertIn("flat", picks)                     # once every trailing sum is negative it stays out

    def test_max_drawdown(self):
        import numpy as np
        from markets.commodity.experiments import regime_switch_study as r
        self.assertEqual(r.max_drawdown(np.array([0.0, 0.0])), 0.0)
        self.assertAlmostEqual(r.max_drawdown(np.array([10000.0, -20000.0])), 20000 / 110000 * 100, places=3)


if __name__ == "__main__":
    unittest.main()
