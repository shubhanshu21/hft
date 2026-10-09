"""engine/depth_recorder.py: parsing Upstox full-mode feed updates and writing per-symbol daily files."""
import csv
import gzip
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from engine import depth_recorder as dr

IST = ZoneInfo("Asia/Kolkata")
FEED = {"fullFeed": {"marketFF": {
    "ltpc": {"ltp": 226433.0, "ltt": "1791542265000", "ltq": "1", "cp": 223542.0},
    "marketLevel": {"bidAskQuote": [{"bidQ": "1", "bidP": 226437.0, "askQ": "2", "askP": 226479.0},
                                    {"bidQ": "4", "bidP": 226435.0, "askQ": "9", "askP": 226480.0}]},
    "vtt": "47454", "tbq": 1200.0, "tsq": 900.0, "oi": 5000.0}}}


class TestParse(unittest.TestCase):
    def test_full_feed_becomes_a_flat_row_with_five_levels(self):
        row = dr.parse_feed(FEED, 123)
        self.assertEqual((row["recv_ms"], row["ltp"], row["ltq"], row["vtt"], row["tbq"], row["tsq"]), (123, 226433.0, "1", "47454", 1200.0, 900.0))
        self.assertEqual((row["bidp_1"], row["bidq_1"], row["askp_1"], row["askq_1"]), (226437.0, "1", 226479.0, "2"))
        self.assertEqual((row["bidp_2"], row["askq_2"]), (226435.0, "9"))
        self.assertIsNone(row["bidp_5"])                                     # missing levels stay empty
        self.assertEqual(set(row), set(dr.COLUMNS))

    def test_messages_without_market_data_are_ignored(self):
        self.assertIsNone(dr.parse_feed({"ltpc": {"ltp": 1.0}}, 1))
        self.assertIsNone(dr.parse_feed({}, 1))


class TestSession(unittest.TestCase):
    def test_weekday_market_hours_only(self):
        self.assertTrue(dr.in_session(datetime(2026, 10, 9, 9, 0, tzinfo=IST)))
        self.assertTrue(dr.in_session(datetime(2026, 10, 9, 23, 30, tzinfo=IST)))
        self.assertFalse(dr.in_session(datetime(2026, 10, 9, 23, 40, tzinfo=IST)))
        self.assertFalse(dr.in_session(datetime(2026, 10, 9, 8, 30, tzinfo=IST)))
        self.assertFalse(dr.in_session(datetime(2026, 10, 10, 12, 0, tzinfo=IST)))   # Saturday


class TestFiles(unittest.TestCase):
    def test_rows_append_per_symbol_and_day_and_old_days_get_gzipped(self):
        root = Path(tempfile.mkdtemp())
        files = dr.DayFiles(root)
        files.write("USDINR", "2026-10-09", [dr.parse_feed(FEED, 1)])
        files.write("USDINR", "2026-10-09", [dr.parse_feed(FEED, 2)])
        files.write("USDINR", "2026-10-12", [dr.parse_feed(FEED, 3)])          # a new day closes and compresses the old file
        files.close_all()
        self.assertFalse((root / "USDINR" / "2026-10-09.csv").exists())
        with gzip.open(root / "USDINR" / "2026-10-09.csv.gz", "rt") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["recv_ms"] for r in rows], ["1", "2"])
        self.assertTrue((root / "USDINR" / "2026-10-12.csv").exists())
        dr.compress_finished_days(root, today="2026-10-13")
        self.assertTrue((root / "USDINR" / "2026-10-12.csv.gz").exists())


class TestRecorderRouting(unittest.TestCase):
    def test_updates_are_routed_by_instrument_key_and_unknown_keys_dropped(self):
        rec = dr.Recorder(["USDINR"])
        rec.keys = {"NCD_FO|1284": "USDINR"}
        rec.on_message({"type": "live_feed", "feeds": {"NCD_FO|1284": FEED, "NSE_FO|1": FEED}})
        rec.on_message({"type": "market_info", "marketInfo": {}})
        self.assertEqual(list(rec.buf), ["USDINR"])
        self.assertEqual(rec.counts, {"USDINR": 1})


if __name__ == "__main__":
    unittest.main()
