"""equity/momentum_12_1 (2026-11-19, half of the EQUITY_BREAKOUT account): the 20 stocks with the best 12-month return (skipping the
latest month) among the day's 300 most-traded, refreshed on the first trading day of each month. Synthetic prices, no archive."""
import unittest
from datetime import date

import numpy as np
import pandas as pd

from markets.equity.strategies.tf_daily.momentum_12_1 import strategy as M

DAYS = pd.bdate_range("2025-01-01", periods=300)


def panel(n_stocks=5, price=100.0):
    """Stock k rises k% over the year; all trade the same value except ILLIQ (tiny turnover) and CHEAP (below Rs20)."""
    cols = [f"S{k}" for k in range(n_stocks)] + ["ILLIQ", "CHEAP"]
    growth = {c: 1 + 0.01 * k for k, c in enumerate(cols[:n_stocks])} | {"ILLIQ": 3.0, "CHEAP": 3.0}
    C = pd.DataFrame({c: price * np.linspace(1, g, len(DAYS)) for c, g in growth.items()}, index=DAYS)
    C["CHEAP"] = C["CHEAP"] / 20                                  # Rs5 -> Rs15: below MIN_PRICE
    VAL = pd.DataFrame(1e9, index=DAYS, columns=cols); VAL["ILLIQ"] = 1.0
    return {"O": C.copy(), "H": C.copy(), "L": C.copy(), "C": C, "VAL": VAL}


class TestSignals(unittest.TestCase):
    def test_score_is_12_1_momentum_on_the_liquid_universe(self):
        p = panel()
        s = M.scores(p, universe=5)
        last = s.iloc[-1]
        exp = p["C"]["S4"].iloc[-1 - M.SKIP] / p["C"]["S4"].iloc[-1 - M.LOOKBACK] - 1
        self.assertAlmostEqual(last["S4"], exp)
        self.assertTrue(np.isnan(last["ILLIQ"]))                  # not among the most traded
        self.assertTrue(np.isnan(last["CHEAP"]))                  # below Rs20
        self.assertTrue(s.iloc[M.LOOKBACK - 1].isna().all())      # not enough history yet

    def test_targets_strongest_first_and_skip_excluded(self):
        row = pd.Series({"A": 0.5, "B": 0.9, "C": np.nan, "D": 0.1})
        self.assertEqual(M.targets(row, 2), ["B", "A"])
        self.assertEqual(M.targets(row, 2, exclude={"B"}), ["A", "D"])

    def test_rebalance_once_a_month(self):
        self.assertTrue(M.rebalance_due(None, date(2026, 11, 20)))       # never rebalanced: start now
        self.assertFalse(M.rebalance_due("2026-11", date(2026, 11, 27)))
        self.assertTrue(M.rebalance_due("2026-11", date(2026, 12, 1)))


class TestBacktest(unittest.TestCase):
    def test_buys_top_n_and_rebalances_monthly(self):
        p = panel(n_stocks=6)
        r = M.backtest(100_000, str(DAYS[260].date()), str(DAYS[-1].date()), p=p, n=3, universe=6)
        buys = [(d, e["sym"]) for d, evs in zip(r["days"], r["events"]) for e in evs if e["t"] == "buy"]
        self.assertEqual(sorted(s for _, s in buys[:3]), ["S3", "S4", "S5"])   # the three strongest, bought at the start
        months = {d[:7] for d, _ in buys}
        self.assertEqual(len(months), 1)                          # same leaders every month -> no further trades
        self.assertLess(r["equity"][0], 100_000 + 1)
        self.assertEqual(len(r["days"]), len(r["equity"]))

    def test_web_backtest_shape(self):
        from unittest import mock
        p = panel(n_stocks=6)
        nifty = pd.Series(100.0, index=DAYS)
        with mock.patch.object(M, "load", return_value=p), mock.patch.object(M, "_nifty", return_value=nifty):
            rep = M.web_backtest(100_000, str(DAYS[260].date()), str(DAYS[-1].date()))
        for k in ("days", "equity", "events", "pos", "benchmark", "metrics"):
            self.assertIn(k, rep)
        self.assertEqual(rep["benchmark"][0], 100_000)


if __name__ == "__main__":
    unittest.main()
