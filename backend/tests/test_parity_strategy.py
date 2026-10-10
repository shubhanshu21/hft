"""markets/currency/strategies/tf_5min/parity: the gap, one decision per completed bar, bid/ask fills, the 30-minute exit, the stop, fees-only costs,
the per-strategy symbol filter."""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.strategy import EntryContext, ExitContext, MarketData
from markets.currency.strategies.tf_5min.parity import signal as sg
from markets.currency.strategies.tf_5min.parity.strategy import CurrencyParity

IST = ZoneInfo("Asia/Kolkata")


def bars(start, closes):
    t0 = datetime.fromisoformat(start).replace(tzinfo=IST)
    return [{"timestamp": (t0 + timedelta(minutes=5 * i)).isoformat(), "open": c, "high": c, "low": c, "close": c, "volume": 10}
            for i, c in enumerate(closes)]


class FakeData(MarketData):
    def __init__(self, usd, quote):
        self.usd, self.q = usd, quote

    def candles(self, symbol):
        return self.usd if symbol == "USDINR" else []

    def quote(self, symbol):
        return self.q


class TestGap(unittest.TestCase):
    def test_gap_is_zero_at_parity_and_signed_when_the_cross_is_rich(self):
        idx = pd.date_range("2026-10-12 09:00", periods=40, freq="5min", tz="Asia/Kolkata")
        usd = pd.Series(97.0, index=idx); fx = pd.Series(1.14, index=idx)
        cross = usd * fx
        cross.iloc[-1] *= 1.0015                              # 15 bp rich on the last bar
        g = sg.gap_bp(cross, usd, fx)
        self.assertAlmostEqual(g.iloc[-2], 0.0, places=6)
        self.assertAlmostEqual(g.iloc[-1], 1e4 * np.log(1.0015), places=3)

    def test_an_overnight_basis_jump_starts_a_new_baseline_instead_of_a_gap(self):
        idx = pd.date_range("2026-10-12 09:00", periods=30, freq="5min", tz="Asia/Kolkata").append(
            pd.date_range("2026-10-13 09:00", periods=30, freq="5min", tz="Asia/Kolkata"))
        usd = pd.Series(97.0, index=idx); fx = pd.Series(1.14, index=idx)
        cross = usd * fx
        cross.iloc[30:] *= 1.004                              # 40 bp step overnight (a contract roll on one leg)
        g = sg.gap_bp(cross, usd, fx)
        self.assertTrue(g.iloc[30:49].isna().all())           # new baseline still warming up (20 bars): no signal rather than a false 40 bp gap
        self.assertAlmostEqual(g.iloc[-1], 0.0, places=6)

    def test_closes_drop_the_forming_bar(self):
        c = bars("2026-10-12T09:00:00", [1, 2, 3])
        s = sg.closes(c, datetime(2026, 10, 12, 9, 12, tzinfo=IST))   # the 09:10 bar is still forming
        self.assertEqual(list(s), [1.0, 2.0])


class TestStrategy(unittest.TestCase):
    def setUp(self):
        self.st = CurrencyParity()
        self.now = datetime(2026, 10, 12, 11, 40, 30, tzinfo=IST)
        n = 40
        self.usd = bars("2026-10-12T08:20:00", [97.0] * n)
        cross = [97.0 * 1.14] * (n - 1) + [97.0 * 1.14 * 1.0015]      # the 11:35 bar closes 15 bp rich
        self.cross = bars("2026-10-12T08:20:00", cross)
        idx = pd.date_range("2026-10-12 08:20", periods=n, freq="5min", tz="Asia/Kolkata")
        self.fx = pd.Series(1.14, index=idx)

    def ctx(self, quote, now=None, capital=100_000):
        return EntryContext(symbol="EURINR", candles=self.cross, now=now or self.now, instrument_key="NCD_FO|1", capital=capital, risk_pct=10.0,
                            leverage=20.0, direction_filter="both", full_session=True, data=FakeData(self.usd, quote))

    def test_rich_cross_is_sold_at_the_bid_once_per_bar(self):
        q = {"bid": 110.5700, "ask": 110.5900, "bid_qty": 10, "ask_qty": 10, "ltp": 110.58}
        with patch.object(sg, "yahoo_fx_5m", return_value=self.fx), patch("markets.currency.strategies.tf_5min.parity.strategy.yahoo_fx_5m", return_value=self.fx):
            s = self.st.entry(self.ctx(q))
            self.assertIsNotNone(s)
            self.assertEqual(s.direction, "short")
            self.assertEqual(s.entry_price, 110.57)
            self.assertEqual(s.qty, 5)
            self.assertAlmostEqual(s.stop_loss, round(110.57 * 1.0025, 4))
            self.assertIsNone(self.st.entry(self.ctx(q)))     # same completed bar: no second decision

    def test_no_trade_without_a_quote_or_with_a_wide_book_or_outside_hours(self):
        with patch("markets.currency.strategies.tf_5min.parity.strategy.yahoo_fx_5m", return_value=self.fx):
            self.assertIsNone(CurrencyParity().entry(self.ctx(None)))
            wide = {"bid": 110.40, "ask": 110.70, "bid_qty": 1, "ask_qty": 1, "ltp": 110.5}       # 27 bp
            self.assertIsNone(CurrencyParity().entry(self.ctx(wide)))
            late = datetime(2026, 10, 12, 16, 0, 30, tzinfo=IST)
            self.assertIsNone(CurrencyParity().entry(self.ctx({"bid": 110.57, "ask": 110.59}, now=late)))

    def test_a_thin_book_fills_at_the_average_of_its_levels_and_only_what_it_shows(self):
        q = {"bid": 110.57, "ask": 110.59, "bids": [(110.57, 2), (110.56, 1), (110.55, 1)], "asks": [(110.59, 3)]}
        with patch("markets.currency.strategies.tf_5min.parity.strategy.yahoo_fx_5m", return_value=self.fx):
            s = CurrencyParity().entry(self.ctx(q))
        self.assertEqual(s.qty, 4)                                 # the book shows 4 of the 5 lots wanted
        self.assertAlmostEqual(s.entry_price, round((2 * 110.57 + 110.56 + 110.55) / 4, 4))

    def test_exit_walks_the_book_and_prices_lots_beyond_it_a_tick_worse(self):
        q = {"bid": 110.50, "ask": 110.52, "bids": [(110.50, 9)], "asks": [(110.52, 3), (110.53, 1)]}
        p = {**self.pos(), "lots": 5}
        d = self.st.manage(p, ExitContext("EURINR", self.cross, self.now + timedelta(minutes=31), FakeData(self.usd, q)))
        self.assertAlmostEqual(d.price, round((3 * 110.52 + 110.53 + 110.5325) / 5, 4))

    def test_lots_follow_the_env_setting(self):
        q = {"bid": 110.5700, "ask": 110.5900}
        with patch("markets.currency.strategies.tf_5min.parity.strategy.yahoo_fx_5m", return_value=self.fx), patch.dict(os.environ, {"CURRENCY_PARITY_LOTS": "3"}):
            self.assertEqual(CurrencyParity().entry(self.ctx(q)).qty, 3)

    def pos(self, direction="short", entry=110.57):
        d = 1 if direction == "long" else -1
        return {"symbol": "EURINR", "direction": direction, "entry_price": entry, "current_stop": round(entry * (1 - d * 0.0025), 4),
                "exit_after": (self.now + timedelta(minutes=30)).isoformat(), "entry_bar_ts": self.cross[-1]["timestamp"], "best_price": entry}

    def test_holds_for_30_minutes_then_buys_back_at_the_ask(self):
        q = {"bid": 110.50, "ask": 110.52}
        p = self.pos()
        self.assertIsNone(self.st.manage(p, ExitContext("EURINR", self.cross, self.now + timedelta(minutes=10), FakeData(self.usd, q))))
        d = self.st.manage(p, ExitContext("EURINR", self.cross, self.now + timedelta(minutes=31), FakeData(self.usd, q)))
        self.assertEqual((d.price, d.reason), (110.52, "time_exit"))

    def test_stop_fills_at_the_open_when_price_gaps_through_it(self):
        p = self.pos()
        later = self.cross + [{"timestamp": (datetime.fromisoformat(self.cross[-1]["timestamp"]) + timedelta(minutes=5)).isoformat(),
                               "open": 111.10, "high": 111.20, "low": 111.0, "close": 111.1, "volume": 5}]
        d = self.st.manage(p, ExitContext("EURINR", later, self.now + timedelta(minutes=6), FakeData(self.usd, None)))
        self.assertEqual((d.price, d.reason), (111.10, "initial_stop"))

    def test_costs_leave_out_the_spread_already_in_the_fills(self):
        c = self.st.costs("EURINR", "short", 110.57, 110.52, 5)
        self.assertEqual(c["slippage"], 0.0)
        self.assertAlmostEqual(c["net"], c["gross"] - c["total"], places=2)
        self.assertLess(c["total"], 110)                         # brokerage Rs70.8 + exchange/SEBI/stamp/GST


class TestSymbolFilter(unittest.TestCase):
    def test_strategies_trade_only_their_listed_symbols(self):
        from markets.currency.strategies.tf_5min.scalping.strategy import STRATEGY as scalping
        with patch.dict(os.environ, {"CURRENCY_SCALPING_SYMBOLS": "USDINR"}):
            self.assertTrue(scalping.trades("USDINR"))
            self.assertFalse(scalping.trades("EURINR"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CURRENCY_SCALPING_SYMBOLS", None)
            self.assertTrue(scalping.trades("EURINR"))          # unset = every symbol, as before
        self.assertFalse(CurrencyParity().trades("USDINR"))      # the parity strategy never touches USDINR


if __name__ == "__main__":
    unittest.main()
