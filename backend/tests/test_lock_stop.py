"""The breakeven lock must never place a stop beyond a price the market has already reached (fills at prices that never traded)."""
import unittest
from datetime import datetime, timedelta, timezone

from core.exits import BE_LOCK_BUFFER_PCT, activation_trail, fixed_tp_breakeven_trail, lock_stop

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 23, 6, 35, tzinfo=IST)


class TestLockStop(unittest.TestCase):
    def test_lock_is_the_fixed_buffer_when_the_trigger_is_farther_than_it(self):
        self.assertAlmostEqual(lock_stop(8900.0, 8920.0, 1), 8900.0 + BE_LOCK_BUFFER_PCT * 8900.0)        # commodity: trigger +0.22% > lock +0.20%
        self.assertAlmostEqual(lock_stop(8900.0, 8880.0, -1), 8900.0 - BE_LOCK_BUFFER_PCT * 8900.0)

    def test_lock_is_capped_at_the_trigger_when_the_trigger_is_nearer(self):
        # USDINR: trigger 0.036% from entry, fixed lock 0.20%: the old code put the stop at 95.906 while price only reached ~95.75
        self.assertAlmostEqual(lock_stop(95.715, 95.75, 1), 95.75)
        self.assertAlmostEqual(lock_stop(95.715, 95.68, -1), 95.68)

    def test_the_capped_stop_is_never_beyond_the_trigger_for_any_distance(self):
        for trig in (95.716, 95.75, 95.8, 96.5):
            self.assertLessEqual(lock_stop(95.715, trig, 1), trig)
        for trig in (95.714, 95.68, 95.5, 94.0):
            self.assertGreaterEqual(lock_stop(95.715, trig, -1), trig)


class TestExitsUseIt(unittest.TestCase):
    def _fixed(self):
        return {"direction": "long", "entry_price": 95.715, "current_stop": 95.65, "best_price": 95.715, "stop_dist": 0.06, "tp": 95.9,
                "be": 95.75, "armed_be": False, "entry_time": NOW - timedelta(minutes=2), "entry_bar_ts": "2026-09-23T06:30:00+05:30"}

    def test_fixed_tp_arm_locks_at_the_trigger_not_at_a_price_that_never_traded(self):
        pos = self._fixed()
        bar = {"timestamp": "2026-09-23T06:35:00+05:30", "open": 95.72, "high": 95.7525, "low": 95.71, "close": 95.74, "volume": 1}
        self.assertIsNone(fixed_tp_breakeven_trail(pos, bar, NOW, close_at=(16, 50), max_hold_s=None))
        self.assertTrue(pos["armed_be"])
        self.assertLessEqual(pos["current_stop"], 95.7525)                        # was 95.90643 (+0.2%) before the fix
        self.assertAlmostEqual(pos["current_stop"], 95.75)

    def test_a_fall_back_after_arming_exits_at_a_real_price(self):
        pos = self._fixed()
        arm = {"timestamp": "2026-09-23T06:35:00+05:30", "open": 95.72, "high": 95.7525, "low": 95.71, "close": 95.74, "volume": 1}
        fixed_tp_breakeven_trail(pos, arm, NOW, close_at=(16, 50), max_hold_s=None)
        drop = {"timestamp": "2026-09-23T06:40:00+05:30", "open": 95.74, "high": 95.745, "low": 95.68, "close": 95.69, "volume": 1}
        d = fixed_tp_breakeven_trail(pos, drop, NOW + timedelta(minutes=5), close_at=(16, 50), max_hold_s=None)
        self.assertEqual(d.reason, "be_stop")
        self.assertLessEqual(d.price, 95.7525)                                    # a level the market really traded

    def test_equity_trail_arm_uses_the_activation_price_as_its_cap(self):
        pos = {"direction": "long", "entry_price": 190.0, "current_stop": 189.0, "best_price": 190.0, "stop_dist": 1.0, "activation_price": 190.10,
               "armed_trail": False, "trail_mult": 1.0, "entry_time": NOW - timedelta(minutes=3), "entry_bar_ts": "2026-09-23T06:30:00+05:30"}
        bar = {"timestamp": "2026-09-23T06:35:00+05:30", "open": 190.0, "high": 190.12, "low": 189.95, "close": 190.05, "volume": 1}
        activation_trail(pos, bar, atr=0.05, now=NOW, close_at=(15, 15), max_hold_s=None)
        self.assertTrue(pos["armed_trail"])
        self.assertLessEqual(pos["current_stop"], 190.12)                         # never above the high the market printed


if __name__ == "__main__":
    unittest.main()
