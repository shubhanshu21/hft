"""Session hours come from Upstox; exits are relative to the day's real close and always leave the account flat before Upstox's announced auto square-off."""
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from core import sessions

IST = timezone(timedelta(hours=5, minutes=30))


def _ms(h, m, day=date(2026, 9, 24)):
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=IST).timestamp() * 1000)


class FakeTimingsApi:
    """Stands in for upstox_client.MarketHolidaysAndTimingsApi."""
    rows = None
    fail = False

    def __init__(self, client):
        pass

    def get_exchange_timings(self, d):
        if FakeTimingsApi.fail:
            raise TimeoutError("read timed out")
        return SimpleNamespace(data=FakeTimingsApi.rows)


def _row(exchange, start, end):
    return SimpleNamespace(exchange=exchange, start_time=_ms(*start), end_time=_ms(*end))


class TestDefaultsAreInsideUpstoxsAutoSquareOff(unittest.TestCase):
    def setUp(self):
        sessions.reset_for_tests()
        self.addCleanup(sessions.reset_for_tests)

    def test_every_forced_exit_is_before_the_earliest_announced_auto_square_off(self):
        for market, announced in sessions.UPSTOX_ANNOUNCED_AUTO_SQUAREOFF.items():
            self.assertLess(sessions.squareoff_min(market), announced[0] * 60 + announced[1], market)

    def test_the_last_entry_is_strictly_before_the_forced_exit(self):
        for market in sessions.DEFAULT_WINDOWS:
            self.assertLess(sessions.last_entry_min(market), sessions.squareoff_min(market), market)      # no entry at the very minute of the exit

    def test_the_documented_defaults(self):
        self.assertEqual(sessions.squareoff_clock("commodity"), (22, 45))
        self.assertEqual(sessions.squareoff_clock("currency"), (16, 25))
        self.assertEqual(sessions.squareoff_clock("equity"), (14, 55))
        self.assertEqual(sessions.last_entry_since_open("commodity"), 810)        # 22:30 in minutes after 09:00
        self.assertEqual(sessions.last_entry_since_open("currency"), 430)         # 16:10
        self.assertEqual(sessions.squareoff_since_open("equity"), 340)            # 14:55 in minutes after 09:15
        self.assertEqual(sessions.last_entry_since_open("equity"), 330)           # 14:45

    def test_strategies_and_the_backtest_use_the_same_source(self):
        from markets.commodity.scalping.strategy import STRATEGY as mcx
        from markets.currency.scalping.strategy import STRATEGY as ncd
        from markets.equity.scalping.strategy import STRATEGY as eq
        self.assertEqual((mcx.close_at, ncd.close_at, eq.close_at), ((22, 45), (16, 25), (14, 55)))
        import inspect
        from markets.currency.scalping import backtest
        self.assertIn("sessions.CURRENCY_SQUAREOFF_MIN", inspect.getsource(backtest.run_currency_backtest))


class TestHoursAreReadFromUpstox(unittest.TestCase):
    def setUp(self):
        sessions.reset_for_tests()
        self.addCleanup(sessions.reset_for_tests)
        FakeTimingsApi.fail = False
        p = patch("upstox_client.MarketHolidaysAndTimingsApi", FakeTimingsApi)
        p.start()
        self.addCleanup(p.stop)

    def test_todays_real_hours_replace_the_defaults(self):
        FakeTimingsApi.rows = [_row("MCX", (9, 0), (23, 55)), _row("CDS", (9, 0), (17, 0)), _row("NSE", (9, 15), (15, 30))]
        changes = sessions.refresh(object(), date(2026, 9, 24))
        self.assertEqual(sessions.window("commodity"), (540, 23 * 60 + 55))
        self.assertEqual(len(changes), 1)
        self.assertIn("commodity (MCX) hours 09:00-23:30 -> 09:00-23:55", changes[0])

    def test_exits_follow_the_days_real_close(self):
        FakeTimingsApi.rows = [_row("MCX", (9, 0), (23, 55)), _row("NSE", (9, 15), (13, 0))]        # MCX daylight-saving close, and a shortened equity session
        sessions.refresh(object(), date(2026, 11, 1))
        self.assertEqual(sessions.squareoff_clock("commodity"), (23, 10))            # close - 45 min
        self.assertEqual(sessions.squareoff_clock("equity"), (12, 25))               # close - 35 min: a half-day session is handled without code changes
        self.assertLess(sessions.last_entry_min("equity"), sessions.window("equity")[1])

    def test_an_unchanged_answer_reports_nothing(self):
        FakeTimingsApi.rows = [_row("MCX", (9, 0), (23, 30)), _row("CDS", (9, 0), (17, 0)), _row("NSE", (9, 15), (15, 30))]
        self.assertEqual(sessions.refresh(object(), date(2026, 9, 24)), [])

    def test_a_failed_call_keeps_the_last_known_hours_and_never_raises(self):
        FakeTimingsApi.rows = [_row("MCX", (9, 0), (23, 55))]
        sessions.refresh(object(), date(2026, 9, 24))
        FakeTimingsApi.fail = True
        self.assertEqual(sessions.refresh(object(), date(2026, 9, 25)), [])
        self.assertEqual(sessions.window("commodity"), (540, 23 * 60 + 55))

    def test_a_market_with_no_session_that_day_keeps_its_previous_hours(self):
        FakeTimingsApi.rows = [_row("MCX", (9, 0), (23, 55))]
        sessions.refresh(object(), date(2026, 9, 24))
        FakeTimingsApi.rows = []                                                     # holiday: Upstox lists no exchanges
        sessions.refresh(object(), date(2026, 10, 2))
        self.assertEqual(sessions.window("commodity"), (540, 23 * 60 + 55))

    def test_is_open_uses_the_live_window(self):
        FakeTimingsApi.rows = [_row("MCX", (9, 0), (23, 55))]
        sessions.refresh(object(), date(2026, 9, 24))
        self.assertTrue(sessions.is_open("commodity", datetime(2026, 9, 24, 23, 45, tzinfo=IST)))
        self.assertFalse(sessions.is_open("currency", datetime(2026, 9, 24, 23, 45, tzinfo=IST)))

    def test_policy_can_be_overridden_from_the_environment_if_upstox_announces_a_new_time(self):
        import os
        with patch.dict(os.environ, {"CURRENCY_SQUAREOFF_BEFORE_CLOSE_MIN": "60"}):
            self.assertEqual(sessions.squareoff_clock("currency"), (16, 0))


if __name__ == "__main__":
    unittest.main()
