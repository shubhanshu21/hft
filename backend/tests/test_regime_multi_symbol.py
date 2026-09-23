"""
tests/test_regime_multi_symbol.py — Tests multi-symbol commodity regime filtering and equity regime gate.
"""
from __future__ import annotations

import unittest
from core.regime import regime_ok


class TestRegimeMultiSymbol(unittest.TestCase):
    def test_trending_series_returns_true(self):
        # Monotonically increasing prices -> positive autocorrelation -> regime_ok True
        closes = [100.0 + i * 2.0 for i in range(20)]
        self.assertTrue(regime_ok(closes, window=15, min_autocorr=0.0))

    def test_mean_reverting_series_returns_false(self):
        # Oscillating series (+1, -1, +1, -1) -> negative autocorrelation -> regime_ok False
        closes = [100.0 if i % 2 == 0 else 105.0 for i in range(30)]
        self.assertFalse(regime_ok(closes, window=15, min_autocorr=0.0))

    def test_short_series_returns_none(self):
        # Fewer candles than window+1 -> returns None (callers fail open / do not block)
        closes = [100.0, 101.0, 102.0]
        self.assertIsNone(regime_ok(closes, window=15, min_autocorr=0.0))


if __name__ == "__main__":
    unittest.main()
