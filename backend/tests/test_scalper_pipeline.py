"""
tests/test_scalper_pipeline.py — Unit and integration tests for the scalping system.
Tests screener logic, Indian statutory cost engine, feature building, and trade execution.
"""
from __future__ import annotations

import unittest
import pandas as pd
import numpy as np

from strategy import costs
from strategy.screener import _first_n_minutes_stats
from backtest.portfolio import simulate_portfolio, summarize_portfolio


class TestIndianCostEngine(unittest.TestCase):
    def test_brokerage_cap_upstox(self):
        # Trade value ₹500,000 -> 0.1% is ₹500, capped at ₹20 per leg + 18% GST = ₹23.60
        buy_brokerage = costs.brokerage_rupees(500000)
        self.assertAlmostEqual(buy_brokerage, 20.0 * 1.18, places=2)

        # Small trade value ₹10,000 -> 0.1% is ₹10.0 + 18% GST = ₹11.80
        small_brokerage = costs.brokerage_rupees(10000)
        self.assertAlmostEqual(small_brokerage, 10.0 * 1.18, places=2)

    def test_regulatory_cost_constants(self):
        # Verify statutory tax percentages
        self.assertEqual(costs.STT_PCT_SELL_SIDE, 0.025)
        self.assertEqual(costs.STAMP_DUTY_PCT_BUY_SIDE, 0.003)
        self.assertEqual(costs.EXCHANGE_TXN_PCT_PER_SIDE, 0.00297)
        self.assertEqual(costs.SEBI_PCT_PER_SIDE, 0.0001)
        self.assertEqual(costs.GST_RATE, 0.18)

    def test_slippage_model(self):
        # High priced stock (₹2,000): spread is 0.05/2000 = 0.0025% + 0.02% impact = ~0.0225%
        slip_high = costs.slippage_pct_per_leg(2000.0)
        self.assertAlmostEqual(slip_high, 0.05 / 2000.0 * 100 + 0.02, places=4)

        # Low priced stock (₹100): spread is 0.05/100 = 0.05% + 0.02% impact = 0.07%
        slip_low = costs.slippage_pct_per_leg(100.0)
        self.assertAlmostEqual(slip_low, 0.07, places=4)


class TestScreenerFunctions(unittest.TestCase):
    def test_opening_stats(self):
        candles = [
            {"timestamp": "2025-01-01 09:15:00", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"timestamp": "2025-01-01 09:20:00", "open": 101, "high": 105, "low": 100, "close": 104, "volume": 1500},
            {"timestamp": "2025-01-01 09:25:00", "open": 104, "high": 106, "low": 103, "close": 105, "volume": 2000},
            {"timestamp": "2025-01-01 09:30:00", "open": 105, "high": 105.5, "low": 104, "close": 104.5, "volume": 800},
        ]
        vol, high, low = _first_n_minutes_stats(candles, minutes=20, interval_minutes=5)
        self.assertEqual(vol, 5300)
        self.assertEqual(high, 106)
        self.assertEqual(low, 99)


class TestPortfolioSimulation(unittest.TestCase):
    def test_portfolio_execution(self):
        trade_log = pd.DataFrame([
            {
                "symbol": "SBIN",
                "entry_dt": "2025-01-01 10:00:00",
                "exit_dt": "2025-01-01 10:30:00",
                "direction": "long",
                "entry_price": 800.0,
                "exit_price": 816.0,
                "stop": 792.0,
                "gross_pct": 2.0,
                "net_pct": 1.85,
                "exit_reason": "take_profit",
                "confidence": 0.20,
            }
        ])
        executed, eq = simulate_portfolio(trade_log, starting_capital=200000, leverage=1.0)
        self.assertEqual(len(executed), 1)
        self.assertGreater(executed.iloc[0]["pnl_rupees"], 0)
        self.assertGreater(executed.iloc[0]["gross_pnl_rupees"], 0)

        stats = summarize_portfolio(executed, eq, starting_capital=200000)
        self.assertEqual(stats["trades_taken"], 1)
        self.assertGreater(stats["total_return_pct"], 0)


if __name__ == "__main__":
    unittest.main()
