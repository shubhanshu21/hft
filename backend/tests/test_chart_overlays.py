"""engine/chart_overlays.py -- the Backtest page's strategy lines and markers (breakout_pair back 2026-11-17). Synthetic data, no archive."""
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from engine import chart_overlays as O

DAYS = pd.bdate_range("2025-01-01", periods=260)


class TestOverlays(unittest.TestCase):
    def test_breakout_lines_are_the_55_day_high_before_each_day(self):
        h = pd.Series(np.arange(260, dtype=float), index=DAYS)            # rising highs: the prior 55-day high = yesterday's high
        p = {"C": pd.DataFrame({"X": h}), "H": pd.DataFrame({"X": h})}
        res = {"days": [str(d.date()) for d in DAYS[-5:]], "bars": {"X": [], "GONE": []}}
        with mock.patch("markets.equity.strategies.tf_daily.breakout_pair.strategy.load", return_value=p):
            out = O.overlays("breakout_pair", "equity", res)
        self.assertEqual(set(out), {"X"})                                   # a symbol without data is left out, not an error
        lines = {ln["name"]: ln["values"] for ln in out["X"]["lines"]}
        self.assertEqual(lines["55-day high"], [254.0, 255.0, 256.0, 257.0, 258.0])
        self.assertEqual(len(lines["EMA 200"]), 5)

    def test_earnings_markers_on_the_results_day_or_next_trading_day(self):
        ev = pd.DataFrame({"sym": ["X", "X", "Y"], "d": pd.to_datetime(["2025-01-04", "2024-06-01", "2025-01-06"])})   # Sat -> Mon
        res = {"days": ["2025-01-06", "2025-01-07"], "bars": {"X": []}}
        with mock.patch("markets.equity.strategies.tf_daily.earnings_drift.strategy.results_events", return_value=ev):
            out = O.overlays("earnings_drift", "equity", res)
        self.assertEqual(out["X"]["markers"], [{"i": 0, "text": "R"}])

    def test_unknown_strategy_or_empty_result(self):
        self.assertEqual(O.overlays("dip_reversal", "crypto", {"days": ["2025-01-06"], "bars": {"BTC": []}}), {})
        self.assertEqual(O.overlays("breakout_pair", "equity", {}), {})

    def test_missing_values_become_none(self):
        self.assertEqual(O._r(pd.Series([1.23456, np.nan, np.inf])), [1.2346, None, None])


if __name__ == "__main__":
    unittest.main()
