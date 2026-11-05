"""The results_gap backtest table keeps up with the market (2026-11-05): it was built once and stopped at 2026-10-15."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from markets.equity.strategies.tf_intraday.results_gap import liquid_days as L
from markets.equity.strategies.tf_intraday.results_gap import strategy as S

DAYS = pd.to_datetime(["2026-11-02", "2026-11-03", "2026-11-04"])


def bars(day):
    return pd.DataFrame([{"timestamp": f"{day}T{hm}:00+05:30", "open": o, "high": o + 1, "low": o - 1, "close": o, "volume": 100000}
                         for hm, o in (("09:15", 110.0), ("09:20", 111.0), ("09:25", 112.0), ("09:30", 113.0), ("15:20", 100.0))])


class NewRowsTest(unittest.TestCase):
    def setUp(self):
        c = {s: [100.0, 100.0, 100.0] for s in ("P1", "P2", "X", "NORESULT", "SMALL", "OLD")}
        o = {s: list(v) for s, v in c.items()}
        o["X"][2] = 110.0                                 # +10% on a results day: wanted
        o["NORESULT"][2] = 110.0                          # +10% without results
        o["SMALL"][2] = 103.0                             # results, but only +3%
        o["OLD"][1] = 110.0                               # +10% with results, but a day the table already has
        self.O, self.C = pd.DataFrame(o, index=DAYS), pd.DataFrame(c, index=DAYS)
        self.hit = {("X", "2026-11-04"), ("SMALL", "2026-11-04"), ("OLD", "2026-11-03")}
        self.fetched = []

    def fetch(self, sym, day):
        self.fetched.append((sym, day))
        return bars(day)

    def test_only_new_results_day_gap_ups_are_fetched_and_kept(self):
        with mock.patch.object(L, "turnover20", return_value=20e7):
            rows = L.new_rows(self.O, self.C, self.hit, ["P1", "P2"], "2026-11-03", self.fetch)
        self.assertEqual(self.fetched, [("X", "2026-11-04")])
        self.assertEqual(rows[0]["sym"], "X")
        self.assertAlmostEqual(rows[0]["xgap"], 0.10, places=6)
        self.assertEqual((rows[0]["o930"], rows[0]["c1520"], rows[0]["turn20"]), (113.0, 100.0, 20e7))
        self.assertEqual(sorted(rows[0]), sorted(L.COLS))

    def test_a_stock_with_no_candles_is_skipped_not_fatal(self):
        with mock.patch.object(L, "turnover20", return_value=20e7):
            self.assertEqual(L.new_rows(self.O, self.C, self.hit, ["P1", "P2"], "2026-11-03", lambda s, d: pd.DataFrame()), [])


class BacktestCalendarTest(unittest.TestCase):
    def test_a_trade_after_the_5_minute_archive_ends_is_counted(self):
        last = pd.DataFrame([{"sym": "P1", "day": "2026-11-03", "o915": 100.0, "c925": 100.0, "o930": 100.0, "h930": 100.0, "l930": 100.0, "v930": 1e7,
                              "c1520": 100.0, "close": 100.0, "hi": 100.0, "lo": 100.0, "v_pre": 1e8, "moved_pre": True, "prev_close": 100.0}])
        rows = pd.DataFrame([{"sym": "X", "day": "2026-11-05", "xgap": 0.1, "o915": 110.0, "c925": 112.0, "o930": 113.0, "h930": 114.0, "l930": 112.0,
                              "v930": 1e7, "c1520": 120.0, "turn20": 20e7, "v_pre": 1e8, "moved_pre": True}])
        with tempfile.TemporaryDirectory() as tmp:
            for d in ("2026-11-03", "2026-11-04", "2026-11-05"):
                (Path(tmp) / f"{d}.csv.gz").write_text("")
            with mock.patch.object(S, "BHAV_DIR", Path(tmp)), mock.patch.object(S, "day_summary", return_value=last):
                r = S.backtest(100_000, "2026-11-01", "2026-11-30", leverage=1.0, results=set(), liquid_rows=rows)
        self.assertEqual(r["days"], ["2026-11-03", "2026-11-04", "2026-11-05"])
        self.assertEqual([t["symbol"] for t in r["trades"]], ["X"])


if __name__ == "__main__":
    unittest.main()
