"""2026-11-18: Upstox publishes a day's daily candle only the NEXT day, so weekday mornings had yesterday's bar for a handful of
stocks only -- the earnings_drift / breakout engines saw "fresh" data and found no signals (KALYANKJIL 13 Nov missed). Now
markets/equity/all_daily.fill_from_bhav appends the day from NSE's bhavcopy, and trim_partial_days drops a day few stocks have."""
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from markets.equity import all_daily as A
from markets.equity.strategies.tf_daily.earnings_drift.strategy import trim_partial_days

BHAV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS
KALYANKJIL, EQ, 12-Nov-2026, 663.75, 675.00, 717.00, 669.00, 710.00, 710.90, 700.1, 51316917, 359264.4
KALYANKJIL, BE, 12-Nov-2026, 1, 1, 1, 1, 1, 1, 1, 1, 1
OLDONE, EQ, 12-Nov-2026, 10, 10, 11, 9, 10, 10.5, 10, 100, 1
"""


class Resp:
    def __init__(self, text, code=200): self.text, self.status_code = text, code


class TestFill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)
        for sym, last in (("KALYANKJIL", "2026-11-11"), ("OLDONE", "2026-11-12"), ("NOBHAV", "2026-11-11")):
            pd.DataFrame([{"timestamp": f"{last}T00:00:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "oi": None}]
                         ).to_csv(self.dir / f"{sym}_daily.csv.gz", index=False, compression="gzip")

    def tearDown(self):
        self.tmp.cleanup()

    def test_appends_the_missing_day_only_where_it_is_missing(self):
        n = A.fill_from_bhav([date(2026, 11, 12)], ["KALYANKJIL", "OLDONE", "NOBHAV"], get=lambda url: Resp(BHAV), data_dir=self.dir, log=lambda *a: None)
        self.assertEqual(n, 1)
        k = pd.read_csv(self.dir / "KALYANKJIL_daily.csv.gz")
        self.assertEqual(k["timestamp"].iloc[-1], "2026-11-12T00:00:00+05:30")
        self.assertEqual(list(k.iloc[-1][["open", "high", "low", "close", "volume"]]), [675.0, 717.0, 669.0, 710.9, 51316917])   # EQ row, not BE
        self.assertEqual(len(pd.read_csv(self.dir / "OLDONE_daily.csv.gz")), 1)               # already had the day: untouched
        self.assertEqual(len(pd.read_csv(self.dir / "NOBHAV_daily.csv.gz")), 1)               # not in the bhavcopy: untouched

    def test_unpublished_bhavcopy_changes_nothing(self):
        logs = []
        n = A.fill_from_bhav([date(2026, 11, 12)], ["KALYANKJIL"], get=lambda url: Resp("<html>404</html>", 404), data_dir=self.dir, log=logs.append)
        self.assertEqual(n, 0); self.assertIn("not published", logs[0])

    def test_upstox_candle_later_replaces_the_bhav_row(self):
        A.fill_from_bhav([date(2026, 11, 12)], ["KALYANKJIL"], get=lambda url: Resp(BHAV), data_dir=self.dir, log=lambda *a: None)
        have = pd.read_csv(self.dir / "KALYANKJIL_daily.csv.gz")
        upstox = pd.DataFrame([{"timestamp": "2026-11-12T00:00:00+05:30", "open": 675, "high": 717, "low": 669, "close": 710.9, "volume": 51316918, "oi": None}])
        out = pd.concat([have, upstox]).drop_duplicates("timestamp", keep="last")      # what topup_one does
        self.assertEqual(len(out), 2); self.assertEqual(out["volume"].iloc[-1], 51316918)


class TestTrim(unittest.TestCase):
    def frames(self, last_day_count):
        idx = pd.bdate_range("2026-11-09", periods=4)
        C = pd.DataFrame(100.0, index=idx, columns=[f"S{i}" for i in range(40)])
        C.iloc[-1, last_day_count:] = float("nan")
        return {"C": C, "O": C.copy()}

    def test_a_day_few_stocks_have_is_dropped(self):
        p = trim_partial_days(self.frames(4))                       # 4 of 40 have the last day (what 18 Nov looked like)
        self.assertEqual(len(p["C"]), 3); self.assertEqual(len(p["O"]), 3)

    def test_a_normal_day_is_kept(self):
        self.assertEqual(len(trim_partial_days(self.frames(38))["C"]), 4)

    def test_small_loads_are_left_alone(self):
        C = pd.DataFrame({"A": [1.0, 2.0], "B": [1.0, float("nan")]}, index=pd.bdate_range("2026-11-16", periods=2))
        self.assertEqual(len(trim_partial_days({"C": C})["C"]), 2)


if __name__ == "__main__":
    unittest.main()


class TestRunFillsEveryStock(unittest.TestCase):
    """2026-11-19: run() filled the bhavcopy day for the 400-stock liquid pool only, while trim_partial_days compares a day with ALL
    ~2,500 stocks of the day before -- 17/18 Nov (1,036 / 439 stocks) were dropped and the swing engines again saw data ending 16 Nov."""

    def test_bhav_fill_covers_every_instrument(self):
        from unittest.mock import patch
        seen = {}
        with patch.object(A, "instruments", return_value={"AAA": "k1", "BBB": "k2", "CCC": "k3"}), \
             patch.object(A, "liquid_pool", return_value=["AAA"]), patch.object(A, "topup_one", return_value=0), \
             patch("engine.research_data.broker", return_value=None), \
             patch.object(A, "fill_from_bhav", side_effect=lambda days, syms, log=None: seen.setdefault("syms", sorted(syms)) and 0):
            A.run(max_minutes=0, log=lambda *a: None)
        self.assertEqual(seen["syms"], ["AAA", "BBB", "CCC"])
