"""engine/research_data.py: chunking, merging and resuming 1-minute archives (no network: a fake broker)."""
import gzip
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from engine import research_data as rd


class FakeBroker:
    def __init__(self):
        self.calls = []

    def get_historical_candles(self, key, unit, interval, to_date, from_date):
        self.calls.append((from_date, to_date))
        days = pd.bdate_range(from_date, to_date)
        return [{"timestamp": f"{d:%Y-%m-%d}T09:15:00+05:30", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10} for d in days][::-1]


class TestHelpers(unittest.TestCase):
    def test_chunks_cover_the_range_without_overlap(self):
        cs = list(rd.chunks(date(2026, 1, 1), date(2026, 2, 15), 21))
        self.assertEqual(cs[0], (date(2026, 1, 1), date(2026, 1, 21)))
        self.assertEqual(cs[-1][1], date(2026, 2, 15))
        self.assertTrue(all(b[0] > a[1] for a, b in zip(cs, cs[1:])))

    def test_sdk_lists_and_broker_dicts_become_the_same_columns(self):
        a = rd.candles_to_df([["2026-01-01T09:15:00+05:30", 1, 2, 0, 1, 5, 7]])
        b = rd.candles_to_df([{"timestamp": "2026-01-01T09:15:00+05:30", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 5}])
        self.assertEqual(list(a.columns), rd.COLS)
        self.assertEqual(a.loc[0, "oi"], 7)
        self.assertTrue(pd.isna(b.loc[0, "oi"]))


class TestMinuteSeries(unittest.TestCase):
    def test_backfill_then_resume_fetches_only_missing_dates_and_folds_the_plain_csv_in(self):
        root = Path(tempfile.mkdtemp())
        plain = root / "X_1minute.csv"
        pd.DataFrame([["2026-03-02T09:15:00+05:30", 1, 1, 1, 1, 1]], columns=rd.COLS[:-1]).to_csv(plain, index=False)
        b = FakeBroker()
        with patch.object(rd, "PACE_S", 0):
            rd.minute_series(b, "K", root / "X_1minute.csv.gz", date(2026, 2, 2), date(2026, 3, 6), "X")
        self.assertFalse(plain.exists())                                      # merged into the gz, then removed
        with gzip.open(root / "X_1minute.csv.gz", "rt") as fh:
            df = pd.read_csv(fh)
        self.assertEqual(df["timestamp"].is_unique, True)
        self.assertEqual(df["timestamp"].min()[:10], "2026-02-02")
        self.assertEqual(df["timestamp"].max()[:10], "2026-03-06")
        b2 = FakeBroker()
        with patch.object(rd, "PACE_S", 0):
            rd.minute_series(b2, "K", root / "X_1minute.csv.gz", date(2026, 2, 2), date(2026, 3, 10), "X")
        self.assertEqual(b2.calls, [("2026-03-06", "2026-03-10")])            # only the new days (and the last held day, re-checked)


if __name__ == "__main__":
    unittest.main()


class TestTimeBudget(unittest.TestCase):
    """The nightly job kills a step at 45 minutes (2026-10-10: the first backfill was killed, failing the whole nightly service).
    With --max-minutes the downloader stops starting new work in time and exits 0; progress is saved per unit."""

    def tearDown(self):
        rd._deadline = None

    def test_an_exhausted_budget_starts_no_new_work(self):
        import time
        b = FakeBroker()
        rd._deadline = time.time() - 1
        with patch.object(rd, "broker", lambda: b):
            rd.job_equity_1m(["RELIANCE", "TCS"])
            rd.job_index()
        self.assertEqual(b.calls, [])

    def test_running_out_of_time_is_not_a_failure(self):
        with patch.dict(rd.JOBS, {"index": lambda *a: self.fail("should not start")}):
            self.assertEqual(rd.main(["index", "--max-minutes", "-1"]), 0)

    def test_concat_skips_empty_pieces(self):
        full = rd.candles_to_df([["2026-01-01T09:15:00+05:30", 1, 2, 0, 1, 5, 7]])
        self.assertEqual(len(rd.concat([rd.candles_to_df([]), full, None])), 1)
        self.assertEqual(list(rd.concat([]).columns), rd.COLS)


class TestNseAndGlobalParsers(unittest.TestCase):
    def test_bhavcopy_keeps_eq_rows_of_the_universe_and_strips_padding(self):
        text = ("SYMBOL, SERIES, DATE1, DELIV_QTY, DELIV_PER\n"
                "RELIANCE, EQ, 08-Oct-2026, 100, 45.2\nRELIANCE, BE, 08-Oct-2026, 1, 1\nZZZ, EQ, 08-Oct-2026, 5, 9\n")
        df = rd.parse_bhavcopy(text, {"RELIANCE"})
        self.assertEqual(df[["SYMBOL", "SERIES", "DELIV_PER"]].values.tolist(), [["RELIANCE", "EQ", "45.2"]])

    def test_deals_file_with_no_records_is_empty(self):
        self.assertTrue(rd.parse_deals_csv("Date,Symbol,Security Name\nNO RECORDS,,\n").empty)
        df = rd.parse_deals_csv("Date,Symbol,Security Name\n09-OCT-2026,ACME,Acme Ltd\n")
        self.assertEqual(df["Symbol"].tolist(), ["ACME"])

    def test_yahoo_bars_get_ist_timestamps(self):
        payload = {"chart": {"result": [{"timestamp": [1791604680], "indicators": {"quote": [
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]}]}}]}}

        class R:
            def json(self):
                return payload
        with patch("requests.get", lambda *a, **k: R()):
            df = rd.yahoo_1m("ES=F")
        self.assertEqual(list(df.columns), rd.COLS)
        self.assertTrue(df.loc[0, "timestamp"].endswith("+05:30"))

    def test_append_unique_counts_only_new_rows(self):
        path = Path(tempfile.mkdtemp()) / "x.csv"
        a = pd.DataFrame({"date": ["d1", "d1"], "category": ["FII", "DII"], "net": ["1", "2"]})
        self.assertEqual(rd.append_unique(path, a, ["date", "category"]), 2)
        self.assertEqual(rd.append_unique(path, a, ["date", "category"]), 0)
