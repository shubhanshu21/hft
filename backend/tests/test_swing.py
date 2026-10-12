"""Swing engine (no-lookahead, execution timing, stops, costs), used by the swing research."""
import unittest
from datetime import timedelta

import numpy as np
import pandas as pd

from core.swing_engine import Rule, entry_signal, run_symbol, summarize, with_indicators


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


if __name__ == "__main__":
    unittest.main()
