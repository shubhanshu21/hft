"""Extended swing rules: no lookahead, pivot confirmation, and the specific defects found while building them."""
import unittest

import numpy as np
import pandas as pd

from core.swing_engine import Rule, run_symbol, with_indicators
from core.swing_rules_ext import PIVOT_K, _pivots, _supertrend

EXT_COLS = ["st_10_3", "st_14_2", "tenkan", "kijun", "cloud_top", "cloud_bot", "macd", "macds", "bb_mid", "bb_up", "bw_rank",
            "kc_up", "ibs", "f_hi", "pole_up", "sw_hi", "retr_up", "bull_engulf", "hammer", "morning_star",
            "ph", "pl", "ph_age", "db_neck", "db_age", "dt_neck"]


def _frame(n=500, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0.05, 1.5, n))
    o = np.concatenate([[c[0]], c[:-1]]) + rng.normal(0, 0.3, n)
    h = np.maximum(o, c) + rng.random(n)
    l = np.minimum(o, c) - rng.random(n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": rng.integers(1000, 5000, n).astype(float)},
                        index=pd.bdate_range("2018-01-01", periods=n))


class TestNoLookahead(unittest.TestCase):
    def test_no_extended_indicator_at_bar_i_depends_on_later_bars(self):
        base = _frame()
        altered = base.copy()
        altered.iloc[-40:] = altered.iloc[-40:] * 1.7             # change ONLY the last 40 bars
        a, b = with_indicators(base, extended=True), with_indicators(altered, extended=True)
        i = len(base) - 41
        for col in EXT_COLS:
            x, y = a[col].iloc[:i + 1], b[col].iloc[:i + 1]
            pd.testing.assert_series_equal(x, y, check_names=False, obj=col)

    def test_a_pivot_is_only_known_k_bars_after_it_happened(self):
        l = np.array([10, 9, 8, 5, 8, 9, 10, 11, 12, 13.0])                # trough at index 3
        h = l + 1
        cols = _pivots(h, l, k=3)
        self.assertTrue(np.isnan(cols["pl_i"][5]))                          # bar 5: only 2 bars after the trough -> not yet a pivot
        self.assertEqual(cols["pl_i"][6], 3)                                # bar 6: 3 bars after -> confirmed
        self.assertEqual(cols["pl"][6], 5)


class TestDefectsFoundWhileBuilding(unittest.TestCase):
    def test_supertrend_flips_even_though_the_atr_starts_as_nan(self):
        c = np.concatenate([np.linspace(100, 200, 120), np.linspace(200, 80, 120)])
        h, l = c + 1, c - 1
        atr = np.concatenate([np.full(14, np.nan), np.full(len(c) - 14, 3.0)])
        d = _supertrend(h, l, c, atr, 3.0)
        self.assertEqual(d[100], 1)
        self.assertEqual(d[-1], -1)                                          # it used to stay stuck at +1 forever (NaN band)

    def test_the_random_control_rule_only_uses_its_own_column_and_time_stop(self):
        f = _frame()
        ind = with_indicators(f, extended=True)
        ind["rnd0"] = np.random.default_rng(0).random(len(ind))
        trades = run_symbol("X", ind, Rule("random", {"p": 0.05, "seed": 0}, stop_mult=3.0, max_hold=10), False, 0.0)
        self.assertTrue(trades)
        self.assertTrue(all(t.bars <= 11 for t in trades))                    # exits only by time stop / ATR stop

    def test_the_core_indicators_are_unchanged_by_asking_for_the_extended_ones(self):
        f = _frame()
        plain, ext = with_indicators(f), with_indicators(f, extended=True)
        for col in plain.columns:
            pd.testing.assert_series_equal(plain[col], ext[col], check_names=False, obj=col)


class TestPatterns(unittest.TestCase):
    def test_bullish_engulfing_is_flagged_on_the_engulfing_bar_only(self):
        f = _frame(300)
        i = 250
        f.iloc[i - 1, f.columns.get_loc("open")], f.iloc[i - 1, f.columns.get_loc("close")] = 110.0, 108.0       # red bar
        f.iloc[i, f.columns.get_loc("open")], f.iloc[i, f.columns.get_loc("close")] = 107.5, 111.0              # green, engulfs it
        d = with_indicators(f, extended=True)
        self.assertEqual(d["bull_engulf"].iloc[i], 1.0)
        self.assertEqual(d["bull_engulf"].iloc[i - 1], 0.0)


if __name__ == "__main__":
    unittest.main()
