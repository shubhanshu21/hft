"""The bot must be flat before Upstox squares positions off for it (and charges for it)."""
import unittest

from core import sessions


def _minutes(hm):
    return hm[0] * 60 + hm[1]


class TestCurrencySessionIsInsideUpstoxsAutoSquareOff(unittest.TestCase):
    OPEN_MIN = 9 * 60

    def test_the_forced_exit_is_before_upstoxs_auto_square_off(self):
        exit_clock = self.OPEN_MIN + sessions.CURRENCY_SQUAREOFF_MIN
        self.assertLess(exit_clock, _minutes(sessions.UPSTOX_CURRENCY_AUTO_SQUAREOFF))

    def test_the_last_entry_leaves_time_before_the_forced_exit(self):
        self.assertLess(sessions.CURRENCY_LAST_ENTRY_MIN, sessions.CURRENCY_SQUAREOFF_MIN - 10)

    def test_the_strategy_and_the_backtest_use_the_same_constants(self):
        from markets.currency.scalping.strategy import STRATEGY
        h, m = STRATEGY.close_at
        self.assertEqual(h * 60 + m, self.OPEN_MIN + sessions.CURRENCY_SQUAREOFF_MIN)
        import inspect
        from markets.currency.scalping import backtest
        src = inspect.getsource(backtest.run_currency_backtest)
        self.assertIn("sessions.CURRENCY_SQUAREOFF_MIN", src)
        self.assertIn("sessions.CURRENCY_LAST_ENTRY_MIN", src)


if __name__ == "__main__":
    unittest.main()
