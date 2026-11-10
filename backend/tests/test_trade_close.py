"""record_trade(close=True): a sale closes the position, writes the trade and moves the balance in ONE save (2026-10-31 audit)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine.database import TradingDB


class CloseAndRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TradingDB(Path(self.tmp.name) / "t.db")
        self.db.init_account("A", name="a", capital=100_000.0, leverage=1.0, risk_pct=0.0)
        self.db.open_position("P", "X", "long", 10, 100.0, 0.0, 0.0, 100.0, account_id="A")

    def tearDown(self):
        self.tmp.cleanup()

    def _sell(self, symbol="X"):
        self.db.record_trade("P", symbol, "long", 10, 100.0, 110.0, "2026-10-31T09:30:00+05:30", "2026-10-31T15:20:00+05:30", 350.0,
                             "end_of_day", 100.0, {"total": 5.0}, 95.0, 100_095.0, account_id="A", strategy="s", close=True)

    def test_a_sale_closes_the_position_books_the_trade_and_the_balance(self):
        self._sell()
        p = self.db.get_position("P")
        self.assertEqual((p["status"], p["exit_price"], p["exit_reason"], p["net_pnl"], p["total_fees"]), ("CLOSED", 110.0, "end_of_day", 95.0, 5.0))
        self.assertEqual(len(self.db.get_trades(account_id="A")), 1)
        self.assertEqual(self.db.get_account("A")["current_capital"], 100_095.0)

    def test_a_failed_trade_write_leaves_the_position_open_and_the_balance_alone(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._sell(symbol=None)                                 # trades.symbol is NOT NULL: the trade insert fails
        self.assertEqual(self.db.get_position("P")["status"], "OPEN", "the close must roll back with the trade, so the next run sells again")
        self.assertEqual(self.db.get_trades(account_id="A"), [])
        self.assertEqual(self.db.get_account("A")["current_capital"], 100_000.0)


    def _trim(self, symbol="X"):
        self.db.record_trade("P", symbol, "long", 4, 100.0, 110.0, "2026-10-31T09:30:00+05:30", "2026-11-10T09:10:00+05:30", 350.0,
                             "trim", 40.0, {"total": 2.0}, 38.0, 100_038.0, account_id="A", strategy="s",
                             keep_qty=6, state_updates={"margin_used": 600.0})

    def test_a_partial_sale_shrinks_the_position_in_the_same_save(self):
        self._trim()
        p = self.db.get_position("P")
        self.assertEqual((p["status"], p["qty"], json.loads(p["state"])["margin_used"]), ("OPEN", 6, 600.0))
        self.assertEqual(len(self.db.get_trades(account_id="A")), 1)
        self.assertEqual(self.db.get_account("A")["current_capital"], 100_038.0)

    def test_a_failed_partial_sale_keeps_every_lot(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._trim(symbol=None)
        self.assertEqual(self.db.get_position("P")["qty"], 10, "the resize must roll back with the trade")
        self.assertEqual(self.db.get_account("A")["current_capital"], 100_000.0)


if __name__ == "__main__":
    unittest.main()
