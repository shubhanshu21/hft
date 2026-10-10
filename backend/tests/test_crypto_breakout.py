"""Crypto breakout sleeve (markets/crypto/strategies/breakout.py + engine/crypto_breakout.py): the rule, the ledger, and the split of the shared demo position."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from engine import crypto_breakout as cb
from markets.crypto.strategies import breakout


def setUpModule():
    cb.DB_PATH = Path(tempfile.mkdtemp()) / "breakout.db"           # never the operator's ledger


def _days(n_days: int, base: float = 100.0, drift: float = 0.5) -> pd.DataFrame:
    """n_days of flat-ish 15-minute bars, each day closing `drift` higher (a clean uptrend for the 20-day filter)."""
    idx = pd.date_range("2026-01-01", periods=96 * n_days, freq="15min", tz="UTC")
    day = np.repeat(np.arange(n_days), 96)
    close = base + day * drift
    return pd.DataFrame({"open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0}, index=idx)


class TestRule(unittest.TestCase):
    def test_long_after_the_breakout_bar_until_the_day_ends_and_only_once(self):
        d = _days(30)
        last_day = d.index[-1].floor("1D")
        today = d.index >= last_day
        o = d.loc[today, "open"].iloc[0]
        level = o + breakout.K * 0.4                                   # yesterday's range is 0.4 (high - low)
        bars = np.flatnonzero(today)
        d.iloc[bars[40], d.columns.get_loc("close")] = level + 0.01   # the breakout close
        d.iloc[bars[40], d.columns.get_loc("high")] = level + 0.05
        d.iloc[bars[60], d.columns.get_loc("close")] = level - 1.0    # falls back below: still held (no intraday exit)
        p = breakout.positions(d)
        self.assertEqual(p[bars[39]], 0)
        self.assertTrue(all(p[bars[40]:bars[-1]] == 1))               # long from the breakout close...
        self.assertEqual(p[bars[-1]], 0)                               # ...flat on the bar that ends at 00:00 UTC
        self.assertEqual(breakout.target(d.iloc[:bars[50] + 1]), 1)    # live decision mid-day = backtest's
        self.assertEqual(breakout.target(d), 0)

    def test_no_trade_without_the_uptrend(self):
        d = _days(30, drift=-0.5)
        bars = np.flatnonzero(d.index >= d.index[-1].floor("1D"))
        d.iloc[bars[40], d.columns.get_loc("close")] += 5.0
        self.assertEqual(breakout.positions(d)[bars[40]], 0)

    def test_the_end_of_day_is_found_by_time_not_by_array_position(self):
        d = _days(30)
        bars = np.flatnonzero(d.index >= d.index[-1].floor("1D"))
        d.iloc[bars[10], d.columns.get_loc("close")] += 5.0
        self.assertEqual(breakout.target(d.iloc[:bars[20] + 1]), 1)     # the array ends mid-day: still long, not "last bar of the day"


class FakeBars:
    def __init__(self, frames):
        self.frames = frames

    def __call__(self, symbol, now):
        d = self.frames[symbol]
        return d[d.index + breakout.BAR <= pd.Timestamp(now)]


class TestSleeve(unittest.TestCase):
    def setUp(self):
        self.con = cb.connect(Path(tempfile.mkdtemp()) / "b.db")
        cb.reset(self.con, 300.0)
        d = _days(30)
        bars = np.flatnonzero(d.index >= d.index[-1].floor("1D"))
        d.iloc[bars[40]:, d.columns.get_loc("close")] = d["close"].iloc[bars[0]] + 2.0       # breaks out at bar 40, closes the day +2
        self.d, self.bars = d, bars

    def test_enters_on_the_breakout_and_exits_at_the_day_end_with_fees_and_funding(self):
        frames = {"BTCUSDT": self.d}
        t_entry = (self.d.index[self.bars[40]] + breakout.BAR + timedelta(seconds=20)).to_pydatetime()
        done = cb.run_cycle(self.con, ("BTCUSDT",), cb.SimFills(), now=t_entry, get_bars=FakeBars(frames), get_funding=lambda s, a, b: [], alert=lambda m: None)
        self.assertEqual(done[0]["side"], "BUY")
        q, e = self.con.execute("SELECT qty, entry_price FROM positions").fetchone()
        self.assertAlmostEqual(q * e, 300.0)                            # 1x the sleeve's equity (one coin)
        t_mid = t_entry + timedelta(hours=2)
        cb.run_cycle(self.con, ("BTCUSDT",), cb.SimFills(), now=t_mid, get_bars=FakeBars(frames), get_funding=lambda s, a, b: [0.0001], alert=lambda m: None)
        self.assertAlmostEqual(cb.funding_total(self.con_path()), -q * e * 0.0001)
        t_end = (self.d.index[-1] + breakout.BAR + timedelta(seconds=20)).to_pydatetime()
        done = cb.run_cycle(self.con, ("BTCUSDT",), cb.SimFills(), now=t_end, get_bars=FakeBars(frames), get_funding=lambda s, a, b: [], alert=lambda m: None)
        self.assertEqual(done[0]["side"], "SELL")
        self.assertIsNone(self.con.execute("SELECT qty FROM positions").fetchone())
        pnl = self.con.execute("SELECT pnl FROM trades").fetchone()[0]
        x = self.d["close"].iloc[-1]
        self.assertAlmostEqual(pnl, q * (x - e) - q * e * cb.SIM_COST - q * x * cb.SIM_COST - q * e * 0.0001, places=6)

    def con_path(self):
        return Path(self.con.execute("PRAGMA database_list").fetchone()[2])


class TestSharedDemoPosition(unittest.TestCase):
    def test_the_blend_does_not_count_the_sleeves_perpetual_as_its_own(self):
        from engine import crypto_paper as cp
        from tests.test_crypto_paper import FakeDemo
        con_b = cb.connect()
        cb.reset(con_b, 300.0)
        con_b.execute("INSERT INTO positions(symbol, qty, entry_price, entry_ts, funding) VALUES('BTCUSDT', 0.003, 100000, 'x', 0)")
        con_b.commit()
        demo = FakeDemo({"BTCUSDT": 100000.0}, spot={"BTC": 0.002}, perp={"BTCUSDT": 0.004})      # exchange: blend 0.001 perp + sleeve 0.003
        ex = cp.DemoExecutor(demo, ("BTCUSDT",))
        con = cp.connect(Path(tempfile.mkdtemp()) / "c.db")
        qty = {}
        ex.sync(con, qty, ("BTCUSDT",))
        self.assertAlmostEqual(qty["BTCUSDT"], 0.003)                  # 0.002 spot + 0.001 perp -- not 0.007
        con_b.execute("DELETE FROM positions"); con_b.commit()


if __name__ == "__main__":
    unittest.main()
