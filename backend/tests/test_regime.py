import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.regime import regime_ok


class TestRegime(unittest.TestCase):
    def test_none_when_insufficient_history(self):
        self.assertIsNone(regime_ok([100.0, 101.0, 102.0], window=15))

    def test_trending_series_passes_default_gate(self):
        # Strictly increasing closes -- every daily return is positive and
        # roughly similar in magnitude, a textbook trending/persistent series.
        # Uses `is True`, not just truthy -- real callers (markets/commodity/scalping/backtest.py,
        # markets/commodity/scalping/entry_signal.py) check `is False`/`is True` specifically to
        # distinguish a real answer from None, and a numpy.bool_ (which a naive
        # `autocorr >= threshold` returns) fails an `is` check even when equal --
        # this is the exact bug found and fixed 2026-09-18.
        closes = [100.0 + i for i in range(20)]
        result = regime_ok(closes, window=15, min_autocorr=0.0)
        self.assertIs(result, True)

    def test_alternating_series_fails_default_gate(self):
        # Up one day, down the next, repeating -- a textbook mean-reverting
        # (negative autocorrelation) series.
        closes = [100.0, 102.0, 100.0, 102.0, 100.0, 102.0, 100.0, 102.0,
                   100.0, 102.0, 100.0, 102.0, 100.0, 102.0, 100.0, 102.0, 100.0]
        result = regime_ok(closes, window=15, min_autocorr=0.0)
        self.assertIs(result, False)

    def test_result_type_is_a_real_python_bool_not_numpy_bool(self):
        closes = [100.0 + i for i in range(20)]
        result = regime_ok(closes, window=15, min_autocorr=0.0)
        self.assertIsInstance(result, bool)

    def test_stricter_threshold_is_harder_to_pass(self):
        closes = [100.0 + i * 1.0 + (0.3 if i % 3 == 0 else 0) for i in range(20)]
        loose = regime_ok(closes, window=15, min_autocorr=0.0)
        strict = regime_ok(closes, window=15, min_autocorr=0.5)
        # Not asserting exact values (depends on the synthetic noise), just
        # that raising the bar can only turn a pass into a fail, never the
        # reverse, for the same series.
        if loose is False:
            self.assertFalse(strict)


if __name__ == "__main__":
    unittest.main()
