"""
tests/test_base_engine.py — Unit tests for BaseTradingEngine.
"""
import unittest
from datetime import datetime, time
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from core.base_engine import BaseTradingEngine, get_market_segment

IST = ZoneInfo("Asia/Kolkata")


class TestBaseTradingEngine(unittest.TestCase):

    def test_market_segment_classification(self):
        self.assertEqual(get_market_segment("RELIANCE"), "equity")
        self.assertEqual(get_market_segment("TCS"), "equity")
        self.assertEqual(get_market_segment("USDINR"), "currency")
        self.assertEqual(get_market_segment("EURINR"), "currency")
        self.assertEqual(get_market_segment("CRUDEOILM"), "commodity")
        self.assertEqual(get_market_segment("GOLDM"), "commodity")

    def test_session_time_gates(self):
        # 10:30 IST -> In all sessions
        dt_1030 = datetime(2026, 9, 22, 10, 30, tzinfo=IST)
        self.assertTrue(BaseTradingEngine.in_commodity_session(dt_1030))
        self.assertTrue(BaseTradingEngine.in_currency_session(dt_1030))
        self.assertTrue(BaseTradingEngine.in_equity_session(dt_1030))

        # 16:00 IST -> Equity closed, Currency open, Commodity open
        dt_1600 = datetime(2026, 9, 22, 16, 0, tzinfo=IST)
        self.assertTrue(BaseTradingEngine.in_commodity_session(dt_1600))
        self.assertTrue(BaseTradingEngine.in_currency_session(dt_1600))
        self.assertFalse(BaseTradingEngine.in_equity_session(dt_1600))

        # 20:00 IST -> Only Commodity open
        dt_2000 = datetime(2026, 9, 22, 20, 0, tzinfo=IST)
        self.assertTrue(BaseTradingEngine.in_commodity_session(dt_2000))
        self.assertFalse(BaseTradingEngine.in_currency_session(dt_2000))
        self.assertFalse(BaseTradingEngine.in_equity_session(dt_2000))

    def test_drawdown_scaled_risk_calculation(self):
        mock_broker = MagicMock()
        mock_broker._configuration.access_token = ""
        mock_db = MagicMock()
        # Initial 100k, current 88k (12% DD -> 0.6x scale)
        mock_db.get_account.return_value = {"initial_capital": 100000.0, "current_capital": 88000.0}

        engine = BaseTradingEngine(
            broker=mock_broker,
            db=mock_db,
            capital=100000.0,
            risk_pct=5.0,
            enable_streaming=False,
        )
        scaled_risk = engine.get_drawdown_scaled_risk_pct(5.0)
        self.assertAlmostEqual(scaled_risk, 3.0)  # 5.0 * 0.6 = 3.0


if __name__ == "__main__":
    unittest.main()
