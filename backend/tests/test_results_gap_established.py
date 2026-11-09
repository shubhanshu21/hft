"""results_gap buys only stocks traded heavily for a year (2026-11-09): median daily turnover over the previous 250 trading days >= Rs10 cr,
with at least 120 days of history (a new listing fails). Study: kept 49 trades at +1.61% each, skipped 19 at -1.13% (KANOHAR among them)."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

from engine import results_gap as E
from markets.equity.strategies.tf_intraday.results_gap import strategy as S
from tests.test_results_gap import BIG, EngineCase, FakeBroker, quotes


def daily(path: Path, n: int, turnover: float, start="2025-01-01", last_turnover=None):
    days = pd.bdate_range(start, periods=n)
    vol = [turnover / 100.0] * n
    if last_turnover is not None:
        vol[-1] = last_turnover / 100.0
    pd.DataFrame({"timestamp": [f"{d:%Y-%m-%d}T00:00:00+05:30" for d in days], "open": 100.0, "high": 100.0, "low": 100.0,
                  "close": 100.0, "volume": vol, "oi": None}).to_csv(path, index=False, compression="gzip")
    return days


class EstablishedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        S._year_turnover.cache_clear()
        self.addCleanup(S._year_turnover.cache_clear)

    def check(self, sym, asof):
        return S.established(sym, asof, root=self.root)

    def test_a_year_of_heavy_trading_passes(self):
        daily(self.root / "OLD_daily.csv.gz", 300, 20e7)
        self.assertTrue(self.check("OLD", date(2026, 6, 1)))

    def test_light_trading_fails(self):
        daily(self.root / "QUIET_daily.csv.gz", 300, 5e7)
        self.assertFalse(self.check("QUIET", date(2026, 6, 1)))

    def test_a_new_listing_fails_however_busy(self):
        daily(self.root / "NEW_daily.csv.gz", 100, 500e7)
        self.assertFalse(self.check("NEW", date(2026, 6, 1)))

    def test_no_history_fails(self):
        self.assertFalse(self.check("MISSING", date(2026, 6, 1)))

    def test_only_days_before_the_trade_count(self):
        days = daily(self.root / "LATE_daily.csv.gz", 130, 5e7)
        self.assertFalse(self.check("LATE", (days[119] + pd.Timedelta(days=1)).date()))     # 120 quiet days: fails
        daily(self.root / "LATE_daily.csv.gz", 130, 5e7, last_turnover=1e12)
        S._year_turnover.cache_clear()
        self.assertFalse(self.check("LATE", days[-1].date()))                                 # the trade day's own huge volume is not used


class EngineSkipsNewStocksTest(EngineCase):
    def test_a_signal_in_a_stock_not_traded_heavily_for_a_year_is_skipped(self):
        lines = []
        with mock.patch.object(S, "established", return_value=False):
            lines = E.run(self.db, FakeBroker(quotes(BIG)), self.at(9, 30), set(), self.lookup, send=None)
        self.assertEqual(self.db.get_open_positions(E.ACCOUNT), [])
        self.assertIn("WIN: not traded heavily for a year", " ".join(lines))


class BacktestSkipsNewStocksTest(unittest.TestCase):
    def test_the_backtest_applies_the_same_rule(self):
        last = pd.DataFrame([{"sym": "P1", "day": "2026-11-03", "o915": 100.0, "c925": 100.0, "o930": 100.0, "h930": 100.0, "l930": 100.0, "v930": 1e7,
                              "c1520": 100.0, "close": 100.0, "hi": 100.0, "lo": 100.0, "v_pre": 1e8, "moved_pre": True, "prev_close": 100.0}])
        row = {"day": "2026-11-03", "xgap": 0.1, "o915": 110.0, "c925": 112.0, "o930": 113.0, "h930": 114.0, "l930": 112.0, "v930": 1e7,
               "c1520": 120.0, "turn20": 20e7, "v_pre": 1e8, "moved_pre": True}
        rows = pd.DataFrame([{"sym": "OLD", **row}, {"sym": "NEW", **row}])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(S, "day_summary", return_value=last), \
                mock.patch.object(S, "BHAV_DIR", Path(tmp)), mock.patch.object(S, "established", side_effect=lambda s, d: s == "OLD"):
            r = S.backtest(100_000, "2026-11-01", "2026-11-30", leverage=1.0, results=set(), liquid_rows=rows)
        self.assertEqual([t["symbol"] for t in r["trades"]], ["OLD"])


if __name__ == "__main__":
    unittest.main()
