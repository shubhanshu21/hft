"""2026-11-21 (user's yes): dip_reversal skips a buy when Binance open interest ROSE during the dip -- new positions piling in, not forced
selling. Those dips earned ~0 per trade in 2022-24 AND 2025-26; Rs1L 2022-26 (business tax) Rs28.2L / worst -68% -> Rs40.8L / -62%."""
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from engine import crypto_dip_reversal as E
from engine.database import TradingDB
from markets.crypto import data as D
from markets.crypto.strategies.tf_5min.dip_reversal import strategy as S


class TestRule(unittest.TestCase):
    def test_skip_only_when_oi_rose_by_more_than_a_quarter_of_the_drop(self):
        self.assertEqual(S.OI_SKIP_RATIO, 0.25)
        self.assertTrue(S.oi_blocks(0.02, -0.04))            # +2% OI on a 4% drop: 0.5 of the drop
        self.assertFalse(S.oi_blocks(0.005, -0.04))          # +0.5%: 0.125 of the drop
        self.assertFalse(S.oi_blocks(-0.03, -0.06))          # OI collapsed: forced selling, the best dips
        self.assertTrue(S.oi_blocks(0.001, 0.0))             # no price drop at all, OI up

    def test_missing_data_never_blocks(self):
        for oi, pc in ((None, -0.05), (float("nan"), -0.05), (0.05, float("nan"))):
            self.assertFalse(S.oi_blocks(oi, pc))


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(S._OI_CACHE.clear)
        p = mock.patch.object(S, "OI_DIR", self.tmp); p.start(); self.addCleanup(p.stop)

    def test_change_over_the_hour_to_each_bars_start(self):
        t = pd.date_range("2026-01-01", periods=30, freq="5min")
        pd.DataFrame({"create_time": t.strftime("%Y-%m-%d %H:%M:%S"), "sum_open_interest": np.linspace(100, 129, 30)}).to_csv(
            self.tmp / "BTC_metrics.csv.gz", index=False, compression="gzip")
        ch = S.oi_change_1h("BTC", t)
        self.assertTrue(np.isnan(ch[:12]).all())
        self.assertAlmostEqual(ch[12], 112 / 100 - 1)
        self.assertAlmostEqual(ch[29], 129 / 117 - 1)

    def test_no_file_means_no_filter(self):
        self.assertTrue(np.isnan(S.oi_change_1h("ZZZ", pd.date_range("2026-01-01", periods=5, freq="5min"))).all())


class TestBacktestUsesIt(unittest.TestCase):
    def test_a_signal_with_rising_oi_is_not_bought(self):
        n = 400
        t = pd.date_range("2026-01-01", periods=n, freq="5min")
        df = pd.DataFrame({"date": t, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "bb_mid20": 101.0})
        df.loc[205:, "bb_mid20"] = 99.0                        # price back above the mid-band: the trade closes
        with mock.patch.object(S, "load_5m_data", return_value={"BTC": df}), \
                mock.patch.object(S, "entry_signal", side_effect=lambda d: pd.Series(d.index == t[200], index=d.index)), \
                mock.patch.object(S, "market_vol_scale_series", return_value=pd.Series(dtype=float)), \
                mock.patch.object(S, "_funding", return_value=pd.Series(dtype=float)):
            def trades(oi):
                with mock.patch.object(S, "oi_change_1h", return_value=np.full(len(pd.date_range("2026-01-01", "2026-01-03", freq="5min")), oi)):
                    return S.web_backtest(100_000, "2026-01-01", "2026-01-02", symbols=["BTC"], tax="none")["trades"]
            # 2026-11-21: a trade sold the same day is NOT open at that day's close (the results page said "loss on 6 open trades"
            # for a run whose 6 last trades were all sold on its last day)
            with mock.patch.object(S, "oi_change_1h", return_value=np.full(577, -0.02)):
                r = S.web_backtest(100_000, "2026-01-01", "2026-01-02", symbols=["BTC"], tax="none")
            self.assertEqual(r["trades"][0]["exit_time"][:10], "2026-01-01")
            self.assertEqual(r["pos"][0], [])
            self.assertEqual(len(trades(-0.02)), 1)           # OI fell: bought
            self.assertEqual(len(trades(+0.02)), 0)           # OI rose on a flat price: skipped
            with mock.patch.object(S, "oi_change_1h", return_value=np.full(577, 0.02)):
                self.assertEqual(len(S.web_backtest(100_000, "2026-01-01", "2026-01-02", symbols=["BTC"], tax="none", oi_filter=False)["trades"]), 1)


class TestLiveEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = TradingDB(self.tmp / "t.db")
        for p in (mock.patch.object(E, "STATE_FILE", self.tmp / "state.json"), mock.patch.object(S, "current_vol_scale", return_value=1.0),
                  mock.patch.object(S, "entry_signal", side_effect=lambda d: pd.Series([False] * (len(d) - 1) + [True]))):
            p.start(); self.addCleanup(p.stop)
        closes = [100.0] * 47 + [96.0] * 13                   # the last hour: -4%
        self.df = pd.DataFrame([{"t": 1_792_761_300_000 + i * E.BAR_MS, "open": c, "high": c, "low": c, "close": c, "bb_mid20": 105.0}
                                for i, c in enumerate(closes)])

    def enter(self, oi):
        state, lines = E.load_state(), []
        with mock.patch.object(E, "fetch_oi_change", return_value=oi) as f:
            E.process_entries(self.db, state, {"AVAX": self.df}, {"usdtinr": 90.0, "prices": {"AVAX": 96.0}}, lines, None)
        return state, lines, f

    def test_rising_oi_skips_and_says_why(self):
        state, lines, f = self.enter(0.02)
        self.assertNotIn("AVAX", state["open_positions"])
        self.assertTrue(any("open interest rose" in ln and "skipped" in ln for ln in lines))
        f.assert_called_once_with("AVAX", int(self.df["t"].iloc[-1]) + E.BAR_MS)

    def test_falling_oi_buys(self):
        state, _, _ = self.enter(-0.03)
        self.assertIn("AVAX", state["open_positions"])

    def test_no_oi_data_buys_as_before_and_says_so(self):
        state, lines, _ = self.enter(None)
        self.assertIn("AVAX", state["open_positions"])
        self.assertTrue(any("open-interest check unavailable" in ln for ln in lines))

    def test_fetch_compares_real_time_oi_with_the_snapshot_an_hour_before_the_close(self):
        close = 1_792_761_300_000
        calls = []

        def get(url, params=None, timeout=None):
            calls.append((url, params))
            r = mock.Mock(); r.raise_for_status = lambda: None
            r.json = (lambda: {"openInterest": "110"}) if url.endswith("/openInterest") else \
                (lambda: [{"timestamp": close - 3_600_000 - 300_000, "sumOpenInterest": "90"}, {"timestamp": close - 3_600_000, "sumOpenInterest": "100"}])
            return r
        with mock.patch.object(E.requests, "get", side_effect=get):
            self.assertAlmostEqual(E.fetch_oi_change("BTC", close), 0.10)
        self.assertEqual(calls[1][1]["endTime"], close - 3_600_000)
        with mock.patch.object(E.requests, "get", side_effect=OSError("down")):
            self.assertIsNone(E.fetch_oi_change("BTC", close))


class TestNightlyTopup(unittest.TestCase):
    def zipped(self, rows):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("x.csv", "create_time,symbol,sum_open_interest\n" + "".join(f"{t},BTCUSDT,{v}\n" for t, v in rows))
        return buf.getvalue()

    def test_adds_the_missing_days_only(self):
        tmp = Path(tempfile.mkdtemp())
        pd.DataFrame({"create_time": ["2026-11-18 23:55:00"], "sum_open_interest": [1.0]}).to_csv(tmp / "BTC_metrics.csv.gz", index=False, compression="gzip")
        asked = []

        def fetch(url):
            asked.append(url)
            if "2026-11-20" in url:
                return None                                  # not published yet: fine, next night
            return self.zipped([("2026-11-19 00:00:00", 2.0), ("2026-11-19 00:05:00", 3.0)])
        with mock.patch.object(D, "METRICS_DIR", tmp):
            out = D.topup_metrics(["BTC"], today=pd.Timestamp("2026-11-21"), fetch=fetch)
        self.assertEqual(out, {"BTC": 2})
        self.assertEqual(len(asked), 2)                       # 19th and 20th, not the 18th again
        m = pd.read_csv(tmp / "BTC_metrics.csv.gz")
        self.assertEqual(m["sum_open_interest"].tolist(), [1.0, 2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
