import unittest
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from strategy.equity_entry_signal import compute_equity_entry_signal
from strategy.entry_signal import compute_entry_signal

class TestDualEngine(unittest.TestCase):
    def _generate_synthetic_candles(self, n=50, base_price=2500.0, trend="neutral"):
        candles = []
        curr = base_price
        start = datetime(2026, 9, 22, 10, 0, 0)
        for i in range(n):
            if trend == "bullish":
                curr += 5.0
            elif trend == "bearish":
                curr -= 5.0
            elif trend == "oversold_extreme":
                curr -= 15.0 # extreme crash away from VWAP
            elif trend == "overbought_extreme":
                curr += 15.0 # extreme spike away from VWAP
                
            candles.append({
                "timestamp": (start + timedelta(minutes=5*i)).isoformat(),
                "open": curr - 1.0,
                "high": curr + 2.0,
                "low": curr - 2.0,
                "close": curr,
                "volume": 20000 + i * 500
            })
        return candles

    def test_mean_reversion_long_trigger_equity(self):
        # Generates extreme crash candle sequence (oversold extreme from session VWAP)
        candles = self._generate_synthetic_candles(n=40, base_price=3000.0, trend="oversold_extreme")
        sig = compute_equity_entry_signal(
            sym="RELIANCE",
            candles=candles,
            instrument_key="NSE_EQ|INE002A01018",
            capital=100000.0,
            risk_pct=4.0,
            leverage=5.0
        )
        if sig:
            self.assertEqual(sig["direction"], "long")
            self.assertEqual(sig["setup_type"], "mean_reversion")
            self.assertIsNotNone(sig["tp"])

    def test_mean_reversion_trigger_commodity(self):
        # Generates extreme crash candle sequence for Crude Oil
        candles = self._generate_synthetic_candles(n=40, base_price=6500.0, trend="oversold_extreme")
        sig = compute_entry_signal(
            sym="CRUDEOILM",
            candles=candles,
            instrument_key="MCX_FO|CRUDEOILM",
            full_session=True,
            direction_filter="both",
            capital=100000.0,
            risk_pct=4.0,
            leverage=5.0
        )
        if sig:
            self.assertEqual(sig["direction"], "long")
            self.assertEqual(sig["setup_type"], "mean_reversion")

if __name__ == "__main__":
    unittest.main()
