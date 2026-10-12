"""
tests/test_fibonacci_strategy.py -- Unit test for FibonacciDailySwing strategy.
"""
from datetime import datetime
import numpy as np
import pandas as pd
import unittest

from core.strategy import EntryContext, ExitContext
from markets.equity.strategies.tf_daily.fibonacci.strategy import FibonacciDailySwing


class TestFibonacciStrategy(unittest.TestCase):
    def setUp(self):
        self.strategy = FibonacciDailySwing(swing_lookback=5, min_swing_pct=0.05)

    def test_insufficient_candles(self):
        ctx = EntryContext(
            symbol="TATASTEEL",
            candles=[{"timestamp": "2026-01-01", "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000}],
            now=datetime(2026, 1, 1),
            instrument_key=None,
            capital=100000.0,
            risk_pct=2.0,
            leverage=1.0,
            direction_filter="both",
        )
        self.assertIsNone(self.strategy.entry(ctx))

    def test_trailing_stop_to_breakeven(self):
        pos = {
            "symbol": "TATASTEEL",
            "entry_price": 100.0,
            "sl": 90.0,
            "target_price": 120.0,
            "exit_state": {"orig_sl": 90.0},
        }
        # Price hit +1R (entry + 10 = 110)
        ctx = ExitContext(
            symbol="TATASTEEL",
            candles=[{"timestamp": "2026-01-05", "open": 105, "high": 111, "low": 104, "close": 109, "volume": 1000}],
            now=datetime(2026, 1, 5),
        )
        dec = self.strategy.manage(pos, ctx)
        self.assertIsNone(dec)
        # SL should now be trailed to breakeven (100.0)
        self.assertEqual(pos["sl"], 100.0)

        # Later, price falls back to breakeven stop
        ctx2 = ExitContext(
            symbol="TATASTEEL",
            candles=[{"timestamp": "2026-01-06", "open": 108, "high": 108, "low": 99.5, "close": 100, "volume": 1000}],
            now=datetime(2026, 1, 6),
        )
        dec2 = self.strategy.manage(pos, ctx2)
        self.assertIsNotNone(dec2)
        self.assertEqual(dec2.reason, "breakeven")
        self.assertEqual(dec2.price, 100.0)

    def test_take_profit_hit(self):
        pos = {
            "symbol": "TATASTEEL",
            "entry_price": 100.0,
            "sl": 90.0,
            "target_price": 120.0,
            "exit_state": {"orig_sl": 90.0},
        }
        ctx = ExitContext(
            symbol="TATASTEEL",
            candles=[{"timestamp": "2026-01-05", "open": 115, "high": 121.0, "low": 114, "close": 120.5, "volume": 1000}],
            now=datetime(2026, 1, 5),
        )
        dec = self.strategy.manage(pos, ctx)
        self.assertIsNotNone(dec)
        self.assertEqual(dec.reason, "take_profit")
        self.assertEqual(dec.price, 120.0)


if __name__ == "__main__":
    unittest.main()
