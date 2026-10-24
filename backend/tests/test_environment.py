"""The runtime environment must not silently change what the indicators compute."""
import unittest

import numpy as np
import pandas as pd


class TestIndicatorsDoNotDependOnWhatIsInstalled(unittest.TestCase):
    """pandas-ta can compute ADX with TA-Lib or in pure Python, and the two differ by several points (measured: up to ~7). Every ADX
    entry threshold (min_adx 15-22) was calibrated on the pure-Python path, which is pandas-ta-classic's default (`talib=None` -> off).
    If a future version flips that default, or the code starts passing talib=True, a machine WITH TA-Lib would trade a different
    strategy from one without -- this fails loudly instead."""

    def test_default_adx_is_the_pure_python_implementation(self):
        import pandas_ta_classic as ta
        rng = np.random.default_rng(1)
        c = 100 + np.cumsum(rng.normal(0, 1, 400))
        df = pd.DataFrame({"high": c + rng.random(400), "low": c - rng.random(400), "close": c})
        default = ta.adx(df.high, df.low, df.close, length=14).iloc[:, 0]
        pure = ta.adx(df.high, df.low, df.close, length=14, talib=False).iloc[:, 0]
        pd.testing.assert_series_equal(default, pure)


if __name__ == "__main__":
    unittest.main()
