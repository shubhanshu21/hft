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
        rec.keys = {"NCD_FO|1284": ("USDINR", None)}
        rec.on_message({"type": "live_feed", "feeds": {"NCD_FO|1284": FEED, "NSE_FO|1": FEED}})
        rec.on_message({"type": "market_info", "marketInfo": {}})
        self.assertEqual(list(rec.buf), ["USDINR"])
        self.assertEqual(rec.counts, {"USDINR": 1})



class TestSampling(unittest.TestCase):
    def _feed(self, ltp):
        return {"fullFeed": {"marketFF": {**FEED["fullFeed"]["marketFF"], "ltpc": {"ltp": ltp}}}}

    def _send(self, rec, key, ms, ltp):
        import time as _t
        from unittest.mock import patch
        with patch.object(_t, "time", lambda: ms / 1000):
            rec.on_message({"feeds": {key: self._feed(ltp)}})

    def test_contracts_keep_every_update(self):
        rec = dr.Recorder(["USDINR"])
        rec.keys = {"NCD_FO|1284": ("USDINR", None)}
        for ms, ltp in ((1_000_100, 96.1), (1_000_900, 96.2), (1_001_050, 96.3)):
            self._send(rec, "NCD_FO|1284", ms, ltp)
        self.assertEqual([r["ltp"] for r in rec.buf["USDINR"]], [96.1, 96.2, 96.3])

    def test_the_option_chain_is_sampled_per_option(self):
        rec = dr.Recorder(["NIFTYOPT"], option_every_s=15)
        rec.keys = {"NSE_FO|1": ("NIFTYOPT", "NIFTY 22500 CE"), "NSE_FO|2": ("NIFTYOPT", "NIFTY 22500 PE")}
        for ms, key, ltp in ((1_000_000, "NSE_FO|1", 10), (1_005_000, "NSE_FO|1", 11), (1_006_000, "NSE_FO|2", 20), (1_016_000, "NSE_FO|1", 12)):
            self._send(rec, key, ms, ltp)
        rows = rec.buf["NIFTYOPT"]
        self.assertEqual([(r["instrument"], r["ltp"]) for r in rows], [("NIFTY 22500 CE", 10), ("NIFTY 22500 PE", 20), ("NIFTY 22500 CE", 12)])
        self.assertEqual((rows[0]["iv"], rows[0]["delta"]), (None, None))       # FEED has no greeks; option feeds fill them


class TestOptionChain(unittest.TestCase):
    def test_nearest_expiry_and_atm_plus_minus_n_strikes(self):
        rows = []
        for exp in ("2026-10-13", "2026-10-20"):
            for k in range(21000, 24001, 50):
                for t in ("CE", "PE"):
                    rows.append({"exchange": "NSE_FO", "name": "NIFTY", "instrument_type": "OPTIDX", "option_type": t, "expiry": exp,
                                 "strike": f"{k}.0", "instrument_key": f"K{exp}{k}{t}", "tradingsymbol": f"NIFTY {exp} {k} {t}"})
        chain = dr.option_chain(rows, "NIFTY", 22512.0, 50, 2, "2026-10-12")
        strikes = sorted({int(v.split()[2]) for v in chain.values()})
        self.assertEqual(strikes, [22400, 22450, 22500, 22550, 22600])
        self.assertTrue(all("2026-10-13" in v for v in chain.values()))
        self.assertEqual(len(chain), 10)
        self.assertEqual(dr.option_chain(rows, "NIFTY", 22512.0, 50, 2, "2026-10-14"), {k: v for k, v in dr.option_chain(rows, "NIFTY", 22512.0, 50, 2, "2026-10-20").items()})


class TestFormatChange(unittest.TestCase):
    def test_a_file_begun_with_another_column_layout_is_not_appended_to(self):
        root = Path(tempfile.mkdtemp())
        (root / "USDINR").mkdir()
        (root / "USDINR" / "2026-10-12.csv").write_text("recv_ms,ltt_ms,ltp\n1,2,3\n")
        files = dr.DayFiles(root)
        files.write("USDINR", "2026-10-12", [dr.parse_feed(FEED, 9, None)])
        files.close_all()
        self.assertEqual((root / "USDINR" / "2026-10-12.csv").read_text(), "recv_ms,ltt_ms,ltp\n1,2,3\n")
        self.assertTrue((root / "USDINR" / "2026-10-12-2.csv").read_text().startswith(dr.DayFiles.HEADER))


if __name__ == "__main__":
    unittest.main()
