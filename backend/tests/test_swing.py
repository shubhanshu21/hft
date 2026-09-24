"""Swing engine (no-lookahead, execution timing, stops, costs) and the commodity swing strategy."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd

from core.swing_engine import Rule, entry_signal, run_symbol, summarize, with_indicators

IST = timezone(timedelta(hours=5, minutes=30))


def _frame(closes, spread=0.5, start="2020-01-01"):
    """Daily bars: open = previous close, high/low = +-spread around the higher/lower of open and close."""
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    idx = pd.bdate_range(start, periods=len(c))
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + spread, "low": np.minimum(o, c) - spread, "close": c}, index=idx)


def _trend_then_break(n_up=320, n_down=120):
    up = np.linspace(100, 220, n_up) + np.sin(np.arange(n_up)) * 0.8
    down = np.linspace(220, 120, n_down) + np.sin(np.arange(n_down)) * 0.8
    return np.concatenate([up, down])


TSMOM = Rule("tsmom", {"lookback": 126}, stop_mult=3.0)
DONCH = Rule("donchian", {"n": 20, "exit_n": 10}, stop_mult=3.0)


class TestNoLookahead(unittest.TestCase):
    def test_trades_do_not_change_when_future_data_is_appended(self):
        base = _frame(_trend_then_break())
        rng = np.random.default_rng(0)
        extra = _frame(base["close"].iloc[-1] + np.cumsum(rng.normal(0, 2, 200)), start=str(base.index[-1].date() + timedelta(days=1)))
        cut = base.index[-1]
        short = run_symbol("X", with_indicators(base), DONCH, True, 0.001)
        full = run_symbol("X", with_indicators(pd.concat([base, extra])), DONCH, True, 0.001, end=str(cut.date()))
        closed_before_cut = [t for t in full if t.exit_date <= cut]
        # a position still open when the short data ends is force-closed there (end_of_data); the longer run keeps it open
        self.assertEqual([(t.entry_date, t.exit_date, round(t.net_ret, 10)) for t in short if t.reason != "end_of_data"],
                         [(t.entry_date, t.exit_date, round(t.net_ret, 10)) for t in closed_before_cut])

    def test_a_signal_uses_only_bars_up_to_and_including_that_day(self):
        frame = with_indicators(_frame(_trend_then_break()))
        altered = _frame(_trend_then_break())
        altered.iloc[-1] = altered.iloc[-1] * 3                       # change ONLY the last bar
        other = with_indicators(altered)
        i = len(frame) - 2
        for col in ("atr", "sma200", "ema50", "rsi2", "hh20", "ll20", "ret126"):
            self.assertAlmostEqual(float(frame[col].iloc[i]), float(other[col].iloc[i]), places=9, msg=col)


class TestExecution(unittest.TestCase):
    def test_entry_is_at_the_next_open_not_the_signal_close(self):
        frame = _frame(_trend_then_break())
        ind = with_indicators(frame)
        trades = run_symbol("X", ind, TSMOM, False, 0.0)
        self.assertTrue(trades)
        t = trades[0]
        signal_day = ind.index[ind.index.get_loc(t.entry_date) - 1]
        self.assertEqual(t.entry, float(frame.loc[t.entry_date, "open"]))         # filled at the NEXT day's open
        self.assertGreater(t.entry_date, signal_day)

    def test_costs_are_subtracted_once_per_round_trip(self):
        ind = with_indicators(_frame(_trend_then_break()))
        free = run_symbol("X", ind, TSMOM, False, 0.0)
        paid = run_symbol("X", ind, TSMOM, False, 0.004)
        self.assertEqual(len(free), len(paid))
        for a, b in zip(free, paid):
            self.assertAlmostEqual(a.net_ret - b.net_ret, 0.004, places=9)

    def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(self):
        closes = list(np.linspace(100, 200, 300)) + [201, 202, 203, 204] + [120.0] * 5          # then a big gap down
        frame = _frame(closes)
        frame.iloc[304, frame.columns.get_loc("open")] = 120.0                                    # the gap day opens far below any stop
        trades = run_symbol("X", with_indicators(frame), TSMOM, False, 0.0)
        gap = [t for t in trades if t.reason == "stop"]
        self.assertTrue(gap)
        self.assertLess(gap[0].exit, gap[0].entry * (1 - gap[0].stop_pct))                        # worse than the stop: filled at the gap open

    def test_shorts_are_only_taken_when_allowed(self):
        frame = with_indicators(_frame(_trend_then_break(n_up=260, n_down=260)))
        longs_only = run_symbol("X", frame, TSMOM, False, 0.0)
        both = run_symbol("X", frame, TSMOM, True, 0.0)
        self.assertTrue(all(t.direction == 1 for t in longs_only))
        self.assertTrue(any(t.direction == -1 for t in both))

    def test_the_donchian_breakout_level_excludes_todays_own_bar(self):
        ind = with_indicators(_frame(_trend_then_break()))
        i = 100
        self.assertEqual(ind["hh20"].iloc[i], ind["high"].iloc[i - 20:i].max())


class TestSummaryAndFilters(unittest.TestCase):
    def test_summary_numbers(self):
        ind = with_indicators(_frame(_trend_then_break()))
        s = summarize(run_symbol("X", ind, TSMOM, True, 0.0))
        self.assertGreater(s["n"], 0)
        self.assertGreaterEqual(s["win"], 0)

    def test_market_filter_blocks_new_entries(self):
        frame = _frame(_trend_then_break())
        row = with_indicators(frame, market_ok=pd.Series(False, index=frame.index)).iloc[-150]
        self.assertEqual(entry_signal(Rule("tsmom", {"lookback": 126, "regime": True}), row, False), 0)


class TestCommoditySwingStrategy(unittest.TestCase):
    def _ctx(self, sym="GOLDM", now=None, price=150000.0, cash=2_000_000.0):        # one GOLDM lot needs ~Rs140k of Upstox margin: Rs100k cannot trade it
        from core.strategy import EntryContext
        bar = {"timestamp": "2026-09-24T09:10:00+05:30", "open": price, "high": price, "low": price, "close": price, "volume": 1}
        return EntryContext(symbol=sym, candles=[bar], now=now or datetime(2026, 9, 24, 9, 10, tzinfo=IST), instrument_key="K",
                            capital=cash, risk_pct=4.0, leverage=5.0, direction_filter="both", full_session=True)

    def _strategy(self, closes):
        from markets.commodity.swing import strategy as mod
        frame = _frame(closes, start="2025-01-01")
        frame.index = pd.bdate_range(end="2026-09-23", periods=len(frame))
        s = mod.CommoditySwing()
        patcher = patch.object(mod, "inr_frame", lambda sym: frame)
        patcher.start()
        self.addCleanup(patcher.stop)
        return s

    def test_it_is_discovered_but_never_runs_unless_named_in_env(self):
        from core import registry
        self.assertIn(("commodity", "swing"), registry.discover())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMMODITY_STRATEGIES", None)
            self.assertEqual([x.name for x in registry.active("commodity")], ["scalping"])

    def test_long_signal_in_an_uptrend_is_sized_and_stopped_from_the_atr(self):
        s = self._strategy(np.linspace(100, 300, 400))
        sig = s.entry(self._ctx())
        self.assertEqual(sig.direction, "long")
        self.assertLess(sig.stop_loss, sig.entry_price)
        self.assertAlmostEqual(sig.stop_dist, sig.entry_price - sig.stop_loss, places=6)
        self.assertGreaterEqual(sig.qty, 1)
        self.assertFalse(s.intraday)
        self.assertEqual(s.product, "D")

    def test_short_signal_in_a_downtrend(self):
        s = self._strategy(np.linspace(300, 100, 400))
        sig = s.entry(self._ctx())
        self.assertEqual(sig.direction, "short")
        self.assertGreater(sig.stop_loss, sig.entry_price)

    def test_direction_filter_blocks_the_other_side(self):
        s = self._strategy(np.linspace(300, 100, 400))
        ctx = self._ctx()
        ctx.direction_filter = "long"
        self.assertIsNone(s.entry(ctx))

    def test_it_only_evaluates_in_the_morning_window_of_the_session(self):
        s = self._strategy(np.linspace(100, 300, 400))
        self.assertTrue(s.due(datetime(2026, 9, 24, 9, 30, tzinfo=IST)))
        self.assertFalse(s.due(datetime(2026, 9, 24, 8, 30, tzinfo=IST)))       # before the session
        self.assertFalse(s.due(datetime(2026, 9, 24, 14, 0, tzinfo=IST)))       # not re-evaluated all day

    def test_if_the_proxy_series_is_unavailable_it_does_not_trade(self):
        from markets.commodity.swing import strategy as mod
        with patch.object(mod, "inr_frame", side_effect=RuntimeError("download failed")):
            self.assertIsNone(mod.CommoditySwing().entry(self._ctx()))

    def test_todays_forming_bar_is_never_used_for_the_signal(self):
        from markets.commodity.swing import strategy as mod
        closes = list(np.linspace(100, 300, 400))
        frame = _frame(closes, start="2025-01-01")
        frame.index = pd.bdate_range(end="2026-09-24", periods=len(frame))     # last bar is dated TODAY
        s = mod.CommoditySwing()
        with patch.object(mod, "inr_frame", lambda sym: frame):
            row = s._latest_row("GOLDM", "2026-09-24")
        self.assertLess(row.name, pd.Timestamp("2026-09-24"))

    def test_manage_stops_out_on_a_real_post_entry_break_and_exits_on_a_sign_flip(self):
        from core.strategy import ExitContext
        s = self._strategy(np.concatenate([np.linspace(100, 300, 330), np.linspace(300, 100, 70)]))   # now in a downtrend
        now = datetime(2026, 9, 24, 9, 20, tzinfo=IST)
        pos = {"symbol": "GOLDM", "direction": "long", "entry_price": 150000.0, "current_stop": 145000.0, "entry_time": now - timedelta(days=3)}
        bar = lambda px, ts="2026-09-24T09:15:00+05:30": {"timestamp": ts, "open": px, "high": px, "low": px, "close": px, "volume": 1}
        flip = s.manage(pos, ExitContext(symbol="GOLDM", candles=[bar(149000.0)], now=now))
        self.assertEqual(flip.reason, "signal_exit")                                  # momentum turned negative while long
        stop = s.manage(pos, ExitContext(symbol="GOLDM", candles=[bar(144000.0)], now=now.replace(hour=14)))
        self.assertEqual((stop.reason, stop.price), ("initial_stop", 145000.0))       # outside the window: only the stop applies


if __name__ == "__main__":
    unittest.main()
