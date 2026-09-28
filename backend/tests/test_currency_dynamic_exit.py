"""The opt-in ADX-scaled exit for the currency scalper (markets/currency/scalping/strategy.py)."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import PropertyMock, patch

from core.strategy import ExitContext, Signal

IST = timezone(timedelta(hours=5, minutes=30))


def _signal(adx=30.0, direction="long"):
    up = direction == "long"
    return Signal(symbol="USDINR", direction=direction, entry_price=96.0, stop_loss=95.94 if up else 96.06, qty=10, stop_dist=0.06, lot_size=1000,
                  exit_state={"tp": 96.2, "be": 96.03, "armed_be": False}, price_levels=("tp", "be"), target_price=96.2, breakeven_price=96.03,
                  alert_levels={"tp": 96.2, "be": 96.03}, diagnostics={"adx": adx, "rsi": 60.0, "vol_surge": 2.0, "vwap_dist_pct": 0.1, "ema_slope_pct": 0.01})


class TestDynamicExit(unittest.TestCase):
    def test_off_unless_asked_for(self):
        from markets.currency.scalping import strategy as s
        with patch.dict(os.environ, {}, clear=False):
            for k in ("CURRENCY_EXIT_MODE", "USDINR_EXIT_MODE"):
                os.environ.pop(k, None)
            self.assertEqual(s.exit_mode("USDINR"), "fixed")
        with patch.dict(os.environ, {"CURRENCY_EXIT_MODE": "dynamic"}):
            self.assertEqual(s.exit_mode("USDINR"), "dynamic")
        with patch.dict(os.environ, {"CURRENCY_EXIT_MODE": "dynamic", "USDINR_EXIT_MODE": "fixed"}):
            self.assertEqual(s.exit_mode("USDINR"), "fixed")                     # the symbol setting wins

    def test_signal_conversion_scales_activation_and_trail_by_adx(self):
        from markets.currency.scalping.strategy import to_dynamic
        strong = to_dynamic(_signal(adx=30.0))                                   # scale 1.2
        self.assertAlmostEqual(strong.exit_state["activation_price"], 96.0 + (0.6 / 1.2) * 0.06, places=4)
        self.assertAlmostEqual(strong.exit_state["trail_mult"], 0.3 * 1.2, places=6)
        self.assertEqual((strong.price_levels, strong.exit_state["armed_trail"]), (("activation_price",), False))
        weak_short = to_dynamic(_signal(adx=10.0, direction="short"))            # clipped to 0.7
        self.assertAlmostEqual(weak_short.exit_state["activation_price"], 96.0 - (0.6 / 0.7) * 0.06, places=4)
        capped = to_dynamic(_signal(adx=90.0))                                   # clipped to 1.8
        self.assertAlmostEqual(capped.exit_state["trail_mult"], 0.3 * 1.8, places=6)

    def test_dynamic_position_arms_then_trails_and_a_fixed_position_keeps_the_fixed_exit(self):
        from markets.currency.scalping.strategy import STRATEGY, to_dynamic
        sig = to_dynamic(_signal(adx=25.0))                                       # activation 96.036, trail 0.3
        entry_time = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
        pos = {"direction": "long", "entry_price": 96.0, "entry_time": entry_time, "stop_dist": 0.06, "current_stop": 95.94, "best_price": 96.0,
               "entry_bar_ts": "2026-09-28T10:00:00+05:30", **sig.exit_state}
        candles = [{"timestamp": f"2026-09-28T09:{m:02d}:00+05:30", "open": 96.0, "high": 96.01, "low": 95.99, "close": 96.0, "volume": 1000} for m in range(0, 60, 5)]
        candles.append({"timestamp": "2026-09-28T10:05:00+05:30", "open": 96.0, "high": 96.10, "low": 96.0, "close": 96.08, "volume": 3000})
        with patch.object(type(STRATEGY), "close_at", new_callable=PropertyMock, return_value=(16, 25)):
            decision = STRATEGY.manage(pos, ExitContext(symbol="USDINR", candles=candles, now=entry_time + timedelta(minutes=8)))
        self.assertIsNone(decision)
        self.assertTrue(pos["armed_trail"])                                       # 96.10 passed the 96.036 activation
        self.assertGreater(pos["current_stop"], 95.94)                            # the stop moved up to the lock / trail
        fixed = {"direction": "long", "entry_price": 96.0, "entry_time": entry_time, "stop_dist": 0.06, "current_stop": 95.94, "best_price": 96.0, "tp": 96.2, "be": 96.03, "armed_be": False,
                 "entry_bar_ts": "2026-09-28T10:00:00+05:30"}
        with patch.object(type(STRATEGY), "close_at", new_callable=PropertyMock, return_value=(16, 25)):
            STRATEGY.manage(fixed, ExitContext(symbol="USDINR", candles=candles, now=entry_time + timedelta(minutes=8)))
        self.assertTrue(fixed["armed_be"])                                        # the original break-even path

    def test_restore_reads_the_persisted_state(self):
        import json
        from markets.currency.scalping.strategy import STRATEGY
        got = STRATEGY.restore({"state": json.dumps({"activation_price": 96.03, "trail_mult": 0.36, "armed_trail": False}), "armed_be": 1, "target_price": 96.03, "breakeven_price": 96.03})
        self.assertEqual(got, {"activation_price": 96.03, "trail_mult": 0.36, "armed_trail": True})
        legacy = STRATEGY.restore({"state": None, "target_price": 96.2, "breakeven_price": 96.03, "armed_be": 0})
        self.assertEqual(legacy, {"tp": 96.2, "be": 96.03, "armed_be": False})


if __name__ == "__main__":
    unittest.main()


class TestLiveCandlesIncludePreviousSessions(unittest.TestCase):
    """engine/live_dryrun._fetch_candles: today's bars are put behind the previous sessions so live indicators match the backtests."""

    class FakeBroker:
        def __init__(self):
            self.history_calls = 0

        def get_intraday_candles(self, key, unit, interval):
            return [{"timestamp": f"2026-09-28T09:{m:02d}:00+05:30", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10} for m in (10, 5, 0)]       # most-recent-first

        def get_historical_candles(self, key, unit, interval, to_date, from_date=None):
            self.history_calls += 1
            assert to_date == "2026-09-27" and from_date == "2026-09-22", (to_date, from_date)
            return [{"timestamp": f"2026-09-26T15:{m}:00+05:30", "open": 1, "high": 2, "low": 0.5, "close": 1.4, "volume": 9} for m in (15, 20, 25)]           # unsorted on purpose

    def _run(self, days="6"):
        from engine import live_dryrun
        live_dryrun._HISTORY_CACHE.clear()
        broker = self.FakeBroker()
        with patch.dict(live_dryrun.SYMBOL_MAP, {"TESTSYM": "KEY"}), patch.dict(os.environ, {"LIVE_CANDLE_HISTORY_DAYS": days}):
            first = live_dryrun._fetch_candles(broker, "TESTSYM", "2026-09-28")
            second = live_dryrun._fetch_candles(broker, "TESTSYM", "2026-09-28")
        return broker, first, second

    def test_history_then_today_in_time_order_and_fetched_once_per_day(self):
        broker, first, second = self._run()
        stamps = [c["timestamp"][5:16] for c in first]
        self.assertEqual(stamps, ["09-26T15:15", "09-26T15:20", "09-26T15:25", "09-28T09:00", "09-28T09:05", "09-28T09:10"])
        self.assertEqual(second, first)
        self.assertEqual(broker.history_calls, 1)                                   # cached: one extra call per symbol per day

    def test_zero_days_is_todays_bars_only(self):
        broker, first, _ = self._run("0")
        self.assertEqual(len(first), 3)
        self.assertEqual(broker.history_calls, 0)
