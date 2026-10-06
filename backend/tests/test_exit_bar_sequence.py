"""core/exits *_bars: the stop is checked on every scan but moves only from COMPLETED 5-minute bars.

The bug this pins down (2026-10-05): live scans every ~30 s saw the still-forming bar. A scan that saw the bar's high raised the stop; the
next scan of the SAME bar tested that bar's earlier low against the raised stop and closed the trade at a price that never traded after
the stop moved. Same entries replayed at 1-minute resolution: commodity+currency +Rs63k with backtest-style exits vs -Rs57k live.
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.exits import activation_trail_bars, fixed_tp_breakeven_trail, fixed_tp_breakeven_trail_bars

IST = timezone(timedelta(hours=5, minutes=30))
ENTRY_BAR = datetime(2026, 9, 30, 10, 0, tzinfo=IST)


def _bar(minute: int, high: float, low: float, close: float) -> dict:
    return {"timestamp": (ENTRY_BAR + timedelta(minutes=minute)).isoformat(), "open": close, "high": high, "low": low, "close": close, "volume": 1}


def _long() -> dict:
    # entry 100, stop 99 (R = 1), breakeven arm at 100.6, take-profit 101.8
    return {"direction": "long", "entry_price": 100.0, "entry_time": ENTRY_BAR + timedelta(minutes=5, seconds=20), "entry_bar_ts": ENTRY_BAR.isoformat(),
            "tp": 101.8, "be": 100.6, "armed_be": False, "current_stop": 99.0, "best_price": 100.0, "stop_dist": 1.0}


def _manage(pos, candles, at_minute: float):
    return fixed_tp_breakeven_trail_bars(pos, candles, ENTRY_BAR + timedelta(minutes=at_minute), close_at=(22, 45), max_hold_s=80 * 60, lag_s=30)


class TestStopMovesOnlyOnCompletedBars(unittest.TestCase):
    def test_a_later_high_in_the_same_bar_never_turns_its_earlier_low_into_a_stop_out(self):
        # bar 10:05 dips to 99.9 in its first minutes, then runs to 100.9 -- the live trace that produced the phantom exits
        early = [_bar(0, 100, 100, 100), _bar(5, 100.2, 99.9, 100.1)]
        later = [_bar(0, 100, 100, 100), _bar(5, 100.9, 99.9, 100.85)]

        old = _long()
        fixed_tp_breakeven_trail(old, later[-1], ENTRY_BAR + timedelta(minutes=8), close_at=(22, 45), max_hold_s=None)   # arms on the high...
        again = fixed_tp_breakeven_trail(old, later[-1], ENTRY_BAR + timedelta(minutes=8, seconds=30), close_at=(22, 45), max_hold_s=None)
        self.assertEqual(again.reason, "be_stop")                     # ...and the old per-scan function then exits on the bar's earlier low

        pos = _long()
        self.assertIsNone(_manage(pos, early, 7))
        self.assertIsNone(_manage(pos, later, 8))
        self.assertIsNone(_manage(pos, later, 8.5))                   # same bar, next scan: no exit
        self.assertFalse(pos["armed_be"])                             # and nothing has moved while the bar is forming
        self.assertEqual(pos["current_stop"], 99.0)

    def test_the_stop_moves_once_the_bar_completes_and_applies_from_the_next_bar(self):
        pos = _long()
        candles = [_bar(0, 100, 100, 100), _bar(5, 100.9, 99.9, 100.85)]
        self.assertIsNone(_manage(pos, candles, 10.6))                # 10:10:36 >= 10:10 + 30 s lag: the 10:05 bar is complete
        self.assertTrue(pos["armed_be"])
        self.assertAlmostEqual(pos["current_stop"], 100.6)            # trail 100.9 - 0.3 = 100.6 (above the 100.2 lock)
        self.assertEqual(pos["trail_bar_ts"], candles[-1]["timestamp"])
        candles.append(_bar(10, 100.95, 100.55, 100.7))               # the NEXT bar trades through 100.6
        d = _manage(pos, candles, 11.5)
        self.assertEqual(d.reason, "be_stop")
        self.assertAlmostEqual(d.price, 100.6)

    def test_a_newer_candle_completes_the_previous_one_without_waiting_for_the_lag(self):
        pos = _long()
        candles = [_bar(0, 100, 100, 100), _bar(5, 100.9, 99.9, 100.85), _bar(10, 100.85, 100.8, 100.82)]
        self.assertIsNone(_manage(pos, candles, 10.1))                # only 6 s past 10:10, but the 10:10 candle exists
        self.assertTrue(pos["armed_be"])

    def test_the_initial_stop_still_fires_inside_a_forming_bar(self):
        pos = _long()
        d = _manage(pos, [_bar(0, 100, 100, 100), _bar(5, 100.1, 98.9, 99.0)], 7)
        self.assertEqual((d.reason, d.price), ("initial_stop", 99.0))

    def test_inside_the_entry_bar_only_its_latest_price_counts(self):
        pos = _long()
        self.assertIsNone(_manage(pos, [_bar(0, 100.3, 98.5, 100.1)], 4))   # the entry bar's 98.5 low came before the entry
        d = _manage(pos, [_bar(0, 100.3, 98.5, 98.9)], 4.5)
        self.assertEqual(d.reason, "initial_stop")                    # a post-entry price at/below the stop still exits

    def test_missed_scans_catch_up_bar_by_bar_in_order(self):
        # three bars completed while the process was not scanning: each is tested against the stop as it stood before it, then applied
        pos = _long()
        candles = [_bar(0, 100, 100, 100), _bar(5, 100.9, 99.9, 100.85), _bar(10, 101.2, 100.7, 101.1), _bar(15, 101.3, 100.95, 101.0)]
        self.assertIsNone(_manage(pos, candles, 20.6))
        self.assertAlmostEqual(pos["current_stop"], 101.0)           # 101.3 - 0.3
        pos2 = _long()
        candles[2] = _bar(10, 101.2, 100.55, 101.1)                  # bar 10:10 dips to 100.55 after 10:05 set the stop at 100.6
        d = _manage(pos2, candles, 20.6)
        self.assertEqual(d.reason, "be_stop")
        self.assertAlmostEqual(d.price, 100.6)

    def test_take_profit_is_checked_on_the_forming_bar(self):
        pos = _long()
        d = _manage(pos, [_bar(0, 100, 100, 100), _bar(5, 101.9, 100.1, 101.5)], 7)
        self.assertEqual((d.reason, d.price), ("take_profit", 101.8))

    def test_activation_trail_uses_the_atr_of_the_bar_that_moves_it(self):
        pos = {"direction": "short", "entry_price": 100.0, "entry_time": ENTRY_BAR + timedelta(minutes=5), "entry_bar_ts": ENTRY_BAR.isoformat(),
               "activation_price": 99.4, "trail_mult": 0.5, "armed_trail": False, "current_stop": 101.0, "best_price": 100.0, "stop_dist": 1.0}
        atrs = {_bar(5, 0, 0, 0)["timestamp"]: 0.4}
        candles = [_bar(0, 100, 100, 100), _bar(5, 100.1, 99.2, 99.3)]
        self.assertIsNone(activation_trail_bars(pos, candles, lambda c: atrs.get(c["timestamp"], 1.0), ENTRY_BAR + timedelta(minutes=8),
                                                close_at=(14, 55), max_hold_s=None, lag_s=30))
        self.assertFalse(pos["armed_trail"])                          # forming: no move
        self.assertIsNone(activation_trail_bars(pos, candles, lambda c: atrs.get(c["timestamp"], 1.0), ENTRY_BAR + timedelta(minutes=10, seconds=40),
                                                close_at=(14, 55), max_hold_s=None, lag_s=30))
        self.assertTrue(pos["armed_trail"])
        self.assertAlmostEqual(pos["current_stop"], 99.2 + 0.5 * 0.4)  # best 99.2 + trail_mult x that bar's ATR

    def test_a_restored_armed_position_without_trail_bar_ts_does_not_retest_old_bars(self):
        # stop already raised to 100.6 by bars before a restart; an old bar's 99.9 low must not be tested against it
        pos = {**_long(), "armed_be": True, "current_stop": 100.6, "best_price": 100.9}
        candles = [_bar(0, 100, 100, 100), _bar(5, 100.9, 99.9, 100.85), _bar(10, 100.95, 100.7, 100.9)]
        self.assertIsNone(_manage(pos, candles, 14))
        self.assertEqual(pos["trail_bar_ts"], candles[1]["timestamp"])   # resumed from the newest completed bar (10:10 is still forming)


class TestTrailBarIsPersistedAndRestoredArmed(unittest.TestCase):
    def test_state_updates_merge_into_the_saved_state(self):
        from engine.database import TradingDB
        path = Path(tempfile.mkdtemp()) / "t.db"
        db = TradingDB(str(path))
        db.open_position(position_id="P1", symbol="CRUDEOILM", direction="long", qty=1, entry_price=100.0, current_stop=99.0,
                         target_price=101.8, breakeven_price=100.6, account_id="A", instrument_key="K", entry_order_id="O",
                         strategy="scalping", state=json.dumps({"tp": 101.8, "be": 100.6, "armed_be": False, "entry_bar_ts": "x", "lot_size": 10}))
        db.update_position_stop(position_id="P1", current_stop=100.6, best_price=100.9, armed_be=True,
                                state_updates={"armed_be": True, "trail_bar_ts": "2026-09-30T10:05:00+05:30"})
        con = sqlite3.connect(str(path))
        state = json.loads(con.execute("SELECT state FROM positions WHERE position_id='P1'").fetchone()[0])
        con.close()
        self.assertEqual(state, {"tp": 101.8, "be": 100.6, "armed_be": True, "entry_bar_ts": "x", "lot_size": 10, "trail_bar_ts": "2026-09-30T10:05:00+05:30"})


if __name__ == "__main__":
    unittest.main()
