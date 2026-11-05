"""5-minute candle labels (2026-11-05): since 2026-10-16 the top-up stamped each candle 1 second early (12:54:59, not 12:55:00). The backtest
looks for exact 5-minute marks, so it silently had no crypto trades after that date."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from markets.crypto import data as D
from markets.crypto.strategies.tf_5min.dip_reversal import strategy as S

T0 = int(pd.Timestamp("2026-11-04 12:55", tz="UTC").timestamp() * 1000)
FIVE_MIN = 300_000


class DownloadLabelTest(unittest.TestCase):
    def test_a_candle_is_labelled_with_its_opening_time(self):
        row = [T0, "1", "2", "0.5", "1.5", "9", T0 + FIVE_MIN - 1] + ["0"] * 5
        resp = mock.Mock(json=mock.Mock(side_effect=[[row], []]))
        with mock.patch.object(D.requests, "get", return_value=resp), mock.patch.object(D.time, "sleep"), \
                mock.patch.object(D.time, "time", return_value=(T0 + 3 * FIVE_MIN) / 1000):
            df = D.candles_5m("BTC", days=1)
        self.assertEqual(df["date"].tolist(), ["2026-11-04T12:55:00Z"])


class LoadLabelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        S._CACHE.clear()
        self.addCleanup(S._CACHE.clear)
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(S, "DATA_DIR", self.dir)
        p.start(); self.addCleanup(p.stop)
        (self.dir / "BTC_5m.csv").write_text(
            "date,open,high,low,close\n"
            "2026-11-04T12:50:00Z,1,1,1,1\n"
            "2026-11-04T12:54:59Z,2,2,2,2\n"           # old label of the 12:55 candle
            "2026-11-04T12:55:00Z,3,3,3,3\n"           # the same candle, fetched again after the fix
            "2026-11-04T12:59:59Z,4,4,4,4\n")          # old label of the 13:00 candle

    def test_old_labels_land_on_the_5_minute_marks_and_a_candle_is_never_counted_twice(self):
        df = S.load_5m_data(["BTC"])["BTC"]
        self.assertEqual(df["date"].dt.strftime("%H:%M:%S").tolist(), ["12:50:00", "12:55:00", "13:00:00"])
        self.assertEqual(df["close"].tolist(), [1.0, 3.0, 4.0])

    def test_the_file_itself_is_left_alone(self):
        before = (self.dir / "BTC_5m.csv").read_text()
        S.load_5m_data(["BTC"])
        self.assertEqual((self.dir / "BTC_5m.csv").read_text(), before)


if __name__ == "__main__":
    unittest.main()
