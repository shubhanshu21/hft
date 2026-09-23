"""Regression: a position must not be stopped out by prices from BEFORE it was entered.

2026-09-23: 5 of 17 paper trades were stopped out ~35 s after entry (84% of the day's loss). The exit check read
the current 5-minute candle's full high/low, which includes prices from before the entry: a breakout signal fires on
a wide spike bar, so the entry sits near the bar's high while its low is already past the stop. Real 1-minute candles
confirmed that none of those stops was actually reached after entry (e.g. CRUDEOILM long 8864, stop 8814: the bar's
low was 8811 from the minute BEFORE the entry; price never traded below 8868 afterwards)."""
import unittest
from datetime import datetime, timedelta, timezone

from core.exits import activation_trail, fixed_tp_breakeven_trail

IST = timezone(timedelta(hours=5, minutes=30))
ENTRY = datetime(2026, 9, 23, 20, 59, 10, tzinfo=IST)
ENTRY_BAR = "2026-09-23T20:55:00+05:30"        # the bar the entry was decided on (still forming at 20:59)
NEXT_BAR = "2026-09-23T21:00:00+05:30"


def _bar(ts, high, low, close):
    return {"timestamp": ts, "open": close, "high": high, "low": low, "close": close, "volume": 1}


def _long(**over):
    pos = {"direction": "long", "entry_price": 8864.0, "current_stop": 8814.29, "best_price": 8864.0, "stop_dist": 49.71,
           "tp": 8930.0, "be": 8894.0, "armed_be": False, "entry_time": ENTRY, "entry_bar_ts": ENTRY_BAR}
    pos.update(over)
    return pos


def _mcx(pos, bar, now=None):
    return fixed_tp_breakeven_trail(pos, bar, now or ENTRY + timedelta(seconds=35), close_at=(22, 45), max_hold_s=80 * 60)


class TestEntryBarIsNotAllowedToStopThePositionOut(unittest.TestCase):
    def test_a_pre_entry_low_on_the_entry_bar_does_not_trigger_the_stop(self):
        # the exact 2026-09-23 20:59 CRUDEOILM situation: bar low 8811 <= stop 8814.29, but price is 8864
        pos = _long()
        self.assertIsNone(_mcx(pos, _bar(ENTRY_BAR, high=8894, low=8811, close=8864)))
        self.assertEqual(pos["current_stop"], 8814.29)                       # untouched

    def test_a_pre_entry_high_on_the_entry_bar_does_not_take_profit_or_arm_breakeven(self):
        pos = _long()
        self.assertIsNone(_mcx(pos, _bar(ENTRY_BAR, high=8950, low=8850, close=8864)))   # high 8950 > tp 8930 came earlier
        self.assertFalse(pos["armed_be"])

    def test_the_latest_price_on_the_entry_bar_still_counts(self):
        pos = _long()
        decision = _mcx(pos, _bar(ENTRY_BAR, high=8894, low=8790, close=8800))          # price really fell through the stop now
        self.assertEqual((decision.reason, decision.price), ("initial_stop", 8814.29))
        pos = _long()
        self.assertEqual(_mcx(pos, _bar(ENTRY_BAR, high=8894, low=8790, close=8935)).reason, "take_profit")

    def test_a_bar_older_than_the_entry_bar_carries_no_post_entry_information(self):
        pos = _long()
        self.assertIsNone(_mcx(pos, _bar("2026-09-23T20:50:00+05:30", high=8900, low=8700, close=8864)))

    def test_a_later_bar_is_used_in_full(self):
        pos = _long()
        decision = _mcx(pos, _bar(NEXT_BAR, high=8880, low=8800, close=8870))           # low through the stop, after entry
        self.assertEqual((decision.reason, decision.price), ("initial_stop", 8814.29))

    def test_time_based_exits_still_fire_when_there_is_no_post_entry_price(self):
        pos = _long(entry_time=ENTRY - timedelta(minutes=90))
        stale = _bar("2026-09-23T20:50:00+05:30", high=8900, low=8700, close=8864)
        self.assertEqual(_mcx(pos, stale, now=ENTRY).reason, "timeout_exit")

    def test_shorts_are_symmetric(self):
        pos = _long(direction="short", entry_price=8864.0, current_stop=8913.71, tp=8798.0, be=8834.0)
        self.assertIsNone(_mcx(pos, _bar(ENTRY_BAR, high=8930, low=8850, close=8864)))  # pre-entry high above the stop
        decision = _mcx(pos, _bar(ENTRY_BAR, high=8930, low=8850, close=8920))          # price really rose through it
        self.assertEqual(decision.reason, "initial_stop")

    def test_a_position_saved_before_this_fix_keeps_the_old_behaviour(self):
        pos = _long()
        del pos["entry_bar_ts"]
        self.assertEqual(_mcx(pos, _bar(ENTRY_BAR, high=8894, low=8811, close=8864)).reason, "initial_stop")


class TestEquityActivationTrailHasTheSameProtection(unittest.TestCase):
    EQ_ENTRY = datetime(2026, 9, 23, 12, 4, 10, tzinfo=IST)         # midday: well before the equity 15:15 square-off
    EQ_BAR, EQ_NEXT = "2026-09-23T12:00:00+05:30", "2026-09-23T12:05:00+05:30"

    def _pos(self):
        return {"direction": "long", "entry_price": 189.9, "current_stop": 188.95, "best_price": 189.9, "stop_dist": 0.95,
                "activation_price": 190.6, "trail_mult": 0.4, "armed_trail": False, "entry_time": self.EQ_ENTRY,
                "entry_bar_ts": self.EQ_BAR}

    def _run(self, pos, bar):
        return activation_trail(pos, bar, atr=0.5, now=self.EQ_ENTRY + timedelta(seconds=35), close_at=(15, 15), max_hold_s=80 * 60)

    def test_the_tatasteel_case(self):
        # entry 189.90, stop 188.95; the entry bar's low (188.40) was set before the entry; price never reached the stop
        pos = self._pos()
        self.assertIsNone(self._run(pos, _bar(self.EQ_BAR, high=190.9, low=188.4, close=189.9)))
        self.assertFalse(pos["armed_trail"])                                            # the pre-entry high must not arm the trail

    def test_a_real_break_of_the_stop_after_entry_still_exits(self):
        self.assertEqual(self._run(self._pos(), _bar(self.EQ_NEXT, high=189.9, low=188.9, close=189.0)).reason, "initial_stop")


if __name__ == "__main__":
    unittest.main()
