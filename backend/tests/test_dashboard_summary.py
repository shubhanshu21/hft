"""
tests/test_dashboard_summary.py — Unit tests for TradingDB dashboard summary and API payloads.
"""
import tempfile
import unittest
from pathlib import Path

from database import TradingDB


class TestDashboardSummary(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_trading.db"
        self.db = TradingDB(self.db_path)
        self.db.init_account("TEST_ACCOUNT", capital=100000.0, leverage=4.0, risk_pct=5.0)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_get_dashboard_summary_structure(self):
        summary = self.db.get_dashboard_summary("TEST_ACCOUNT")
        self.assertEqual(summary["account_id"], "TEST_ACCOUNT")
        self.assertEqual(summary["initial_capital"], 100000.0)
        self.assertEqual(summary["current_capital"], 100000.0)
        self.assertIn("open_positions", summary)
        self.assertIn("recent_trades", summary)
        self.assertIn("recent_orders", summary)
        self.assertIn("snapshots", summary)
        self.assertEqual(summary["total_trades"], 0)
        self.assertEqual(summary["win_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
