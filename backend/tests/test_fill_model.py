"""Stops fill where the market really was (docs/FILL_MODEL_AUDIT.md).

2026-10-07: every simulator (backtests, core/exits, engine/exit_replay, the paper engine) filled a stop AT the stop price even when the bar
had opened through it. The trail sits ~0.3 ATR behind a bar's high, so a bar that closes off its high leaves the stop above the market; the
next bar opens below it and the old code still sold at the stop. 41% of equity stop exits were like that (~22 bp better than the open).
"""
import unittest
from datetime import datetime, timedelta, timezone

from core.exits import activation_trail_bars, bar_exit, fixed_tp_breakeven_trail_bars, intrabar_exit, stop_fill

IST = timezone(timedelta(hours=5, minutes=30))


def _c(ts, o, h, l, c):
    return {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1}


class TestStopFill(unittest.TestCase):
    def test_a_resting_stop_fills_at_the_stop(self):
        self.assertEqual(stop_fill(99.0, 100.0, 1), 99.0)
        self.assertEqual(stop_fill(101.0, 100.0, -1), 101.0)

    def test_a_bar_that_opens_through_the_stop_fills_at_the_open(self):
        self.assertEqual(stop_fill(99.0, 98.2, 1), 98.2)
        self.assertEqual(stop_fill(101.0, 101.7, -1), 101.7)

    def test_no_open_known_keeps_the_stop(self):
        self.assertEqual(stop_fill(99.0, None, 1), 99.0)
        self.assertEqual(stop_fill(99.0, float("nan"), 1), 99.0)


class TestBarExit(unittest.TestCase):
    def test_one_bar_reaching_both_takes_the_stop_first(self):
        self.assertEqual(bar_exit(99.0, 102.0, 100.0, fav=102.5, adv=98.5, d=1), (99.0, "stop"))
        self.assertEqual(bar_exit(101.0, 98.0, 100.0, fav=97.5, adv=101.5, d=-1), (101.0, "stop"))

    def test_a_bar_that_opens_beyond_the_target_takes_the_target(self):
        self.assertEqual(bar_exit(99.0, 102.0, 102.3, fav=102.8, adv=98.5, d=1), (102.0, "tp"))

    def test_only_one_level_reached(self):
        self.assertEqual(bar_exit(99.0, 102.0, 100.0, fav=102.1, adv=99.5, d=1), (102.0, "tp"))
        self.assertEqual(bar_exit(99.0, 102.0, 98.7, fav=99.8, adv=98.4, d=1), (98.7, "stop"))
        self.assertIsNone(bar_exit(99.0, None, 100.0, fav=101.0, adv=99.5, d=1))


class TestIntrabarExit(unittest.TestCase):
    def test_a_jump_through_the_stop_inside_the_bar_fills_at_that_minute_open(self):
        # USDINR 2026-07-03: a short's 95.81 stop; the 5-minute bar opened below it, one minute later price opened at 95.99
        minutes = [(95.79, 95.80, 95.78), (95.99, 96.00, 95.97), (95.98, 95.99, 95.95)]
        self.assertEqual(intrabar_exit(95.81, None, minutes, -1), (95.99, "stop"))

    def test_the_minute_path_decides_which_level_came_first(self):
        minutes = [(100.0, 102.2, 99.9), (101.5, 101.6, 98.5)]                      # target first, then the stop
        self.assertEqual(intrabar_exit(99.0, 102.0, minutes, 1), (102.0, "tp"))
        self.assertEqual(intrabar_exit(99.0, 102.0, list(reversed(minutes)), 1), (99.0, "stop"))
        self.assertIsNone(intrabar_exit(99.0, 102.0, [(100.0, 101.0, 99.5)], 1))

    def test_minute_archive_lines_up_with_the_five_minute_bars(self):
        import pandas as pd
        from core.minute_bars import MinuteBars
        from core.paths import ARCHIVE_ROOT
        one, five = ARCHIVE_ROOT / "currency" / "USDINR_1minute.csv", ARCHIVE_ROOT / "currency" / "USDINR_5minute.csv"
        if not (one.exists() and five.exists()):
            self.skipTest("no currency archive")
        mb, bars = MinuteBars(one), pd.read_csv(five).tail(300)
        covered = [(r, mb.get(r.timestamp)) for r in bars.itertuples() if mb.get(r.timestamp) is not None]
        self.assertGreater(len(covered), 200)
        for r, m in covered:
            self.assertEqual((m[0][0], m[:, 1].max(), m[:, 2].min()), (r.open, r.high, r.low))
        self.assertFalse(MinuteBars(None))


class TestLiveBarSequenceExitsFillAtTheOpen(unittest.TestCase):
    """The stop raised by a completed bar's high, on a bar that closed lower: the next bar opens below the new stop and must fill there."""

    def test_activation_trail(self):
        pos = {"direction": "long", "entry_price": 100.0, "current_stop": 99.0, "best_price": 100.0, "stop_dist": 1.0,
               "activation_price": 100.6, "trail_mult": 0.3, "armed_trail": False, "entry_time": datetime(2026, 9, 10, 10, 5, tzinfo=IST),
               "entry_bar_ts": "2026-09-10T10:00:00+05:30"}
        candles = [_c("2026-09-10T10:00:00+05:30", 99.8, 100.1, 99.7, 100.0),
                   _c("2026-09-10T10:05:00+05:30", 100.0, 101.5, 100.0, 100.4),     # arms; trail 101.5 - 0.3 x 1.0 = 101.2, above the 100.4 close
                   _c("2026-09-10T10:10:00+05:30", 100.4, 100.6, 100.2, 100.3)]     # opens at 100.4, under the new stop
        dec = activation_trail_bars(pos, candles, lambda c: 1.0, datetime(2026, 9, 10, 10, 11, tzinfo=IST), close_at=(15, 15), max_hold_s=None)
        self.assertEqual((dec.reason, dec.price), ("trail_stop", 100.4))

    def test_fixed_breakeven_trail(self):
        pos = {"direction": "short", "entry_price": 100.0, "current_stop": 101.0, "best_price": 100.0, "stop_dist": 1.0,
               "tp": 98.2, "be": 99.4, "armed_be": False, "entry_time": datetime(2026, 9, 10, 10, 5, tzinfo=IST),
               "entry_bar_ts": "2026-09-10T10:00:00+05:30"}
        candles = [_c("2026-09-10T10:00:00+05:30", 100.2, 100.3, 99.9, 100.0),
                   _c("2026-09-10T10:05:00+05:30", 100.0, 100.0, 98.5, 99.5),       # arms; trail 98.5 + 0.3 = 98.8, below the 99.5 close
                   _c("2026-09-10T10:10:00+05:30", 99.5, 99.7, 99.3, 99.6)]         # opens at 99.5, above the new stop
        dec = fixed_tp_breakeven_trail_bars(pos, candles, datetime(2026, 9, 10, 10, 11, tzinfo=IST), close_at=(22, 45), max_hold_s=None)
        self.assertEqual((dec.reason, dec.price), ("be_stop", 99.5))


class TestBacktestNeverFillsAStopBetterThanTheMarket(unittest.TestCase):
    def test_equity_stop_exits_are_never_better_than_their_bar_open(self):
        import pandas as pd
        from core.paths import ARCHIVE_ROOT
        from markets.equity.features import compute_equity_features
        from markets.equity.scalping import backtest as eqbt
        path = ARCHIVE_ROOT / "equity" / "RELIANCE_5minute.csv"
        if not path.exists():
            self.skipTest("no equity archive")
        cands = eqbt._simulate_symbol_candidates("RELIANCE", "2025-01-01", "2025-06-30", False, eqbt.ENTRY_THRESHOLDS,
                                                 pullback_frac=0.15, pullback_through=0.02)
        f = compute_equity_features(pd.read_csv(path))
        open_at = dict(zip(f["timestamp"].astype(str), f["open"].astype(float)))
        stops = [c for c in cands if c["reason"] in ("initial_stop", "trail_stop") and not c.get("stopped_on_fill")]
        self.assertTrue(stops)
        for c in stops:
            o, d = open_at[str(c["exit_time"])], 1 if c["direction"] == "long" else -1
            self.assertLessEqual(d * (c["exit_price"] - o), 1e-9, c)
        for c in (c for c in cands if c.get("stopped_on_fill")):
            self.assertAlmostEqual(abs(c["entry_price"] - c["exit_price"]), c["stop_dist"], places=3)   # a full one-stop loss


if __name__ == "__main__":
    unittest.main()
