import lzma
import struct
import unittest
from datetime import date

import numpy as np
import pandas as pd


class DukascopyDecodeTests(unittest.TestCase):
    def test_decode_one_day(self):
        from services.data import dukascopy as d
        # two 1-minute records: (seconds since 00:00 UTC, open, close, low, high, volume); gold is scaled by 1000
        payload = lzma.compress(struct.pack(">IIIIIf", 0, 2915148, 2914858, 2914428, 2915248, 0.5) + struct.pack(">IIIIIf", 60, 2914858, 2915000, 2914800, 2915100, 0.25))
        df = d.decode(payload, date(2025, 3, 12), d.SCALE["XAUUSD"])
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(df.loc[0, "open"], 2915.148)
        self.assertAlmostEqual(df.loc[0, "high"], 2915.248)          # record order is open, close, low, high
        self.assertAlmostEqual(df.loc[0, "low"], 2914.428)
        self.assertEqual(str(df.loc[1, "timestamp"]), "2025-03-12 00:01:00+00:00")

    def test_closed_day_is_empty_not_an_error(self):
        from services.data import dukascopy as d
        self.assertEqual(len(d.decode(b"", date(2025, 3, 15), 1e3)), 0)


class EquityPoolTests(unittest.TestCase):
    def test_pool_respects_the_concurrency_cap_and_one_position_per_symbol(self):
        from markets.equity.experiments import multi_strategy_study as m
        base = {"exit_price": 101.0, "stop_dist": 1.0, "direction": "long", "entry_price": 100.0}
        cands = [{**base, "symbol": s, "entry_time": "2026-01-05T10:00:00", "exit_time": "2026-01-05T11:00:00"} for s in ("A", "B", "C", "D")]
        cands.append({**base, "symbol": "A", "entry_time": "2026-01-05T10:30:00", "exit_time": "2026-01-05T11:30:00"})      # A is already open
        cands.append({**base, "symbol": "E", "entry_time": "2026-01-05T11:00:00", "exit_time": "2026-01-05T12:00:00"})      # slots freed at 11:00
        got = [t["symbol"] for t in m.pool(cands)]
        self.assertEqual(got, ["A", "B", "C", "E"])

    def test_orb_fires_once_per_day_and_fade_is_the_opposite(self):
        from markets.equity.experiments import multi_strategy_study as m
        n = 20
        ts = [f"2026-01-05T{9 + (15 + 5 * i) // 60:02d}:{(15 + 5 * i) % 60:02d}:00+05:30" for i in range(n)]
        close = np.array([100.0] * 6 + [100.1, 100.2, 101.0, 101.5] + [101.0] * 10)      # breaks the 6-bar range upward once
        f = pd.DataFrame({"timestamp": ts, "high": close + 0.05, "low": close - 0.05, "close": close, "minutes_since_open": [5 * i for i in range(n)]})
        brk, fade = m.sig_orb(f, False), m.sig_orb(f, True)
        self.assertEqual(int((brk != 0).sum()), 1)
        self.assertEqual(int(brk.sum()), 1)
        self.assertEqual(int(fade.sum()), -1)


if __name__ == "__main__":
    unittest.main()
