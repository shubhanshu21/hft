"""engine/account_reset.py -- the paper page's "Reset account" button (2026-10-24). Every test uses a temporary database and state folder;
the real var/db is never touched (the API test mocks reset_account itself)."""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from engine import account_reset as R
from engine.database import TradingDB


def _seed(db_path: Path) -> None:
    db = TradingDB(db_path)
    db.init_account("CRYPTO_DIP_REVERSAL", name="crypto", capital=100000.0)
    db.init_account("MCX_TREND_CARRY", name="mcx", capital=250000.0)
    con = sqlite3.connect(db_path)
    with con:
        for acc, cap in (("CRYPTO_DIP_REVERSAL", 97330.0), ("MCX_TREND_CARRY", 260000.0)):
            con.execute("UPDATE accounts SET current_capital = ? WHERE account_id = ?", (cap, acc))
            con.execute("INSERT INTO trades (position_id, account_id, symbol, direction, qty, entry_price, exit_price, entry_dt, exit_dt, exit_reason, gross_pnl,"
                        " brokerage, stt, stamp_duty, exchange_txn_fee, sebi_charges, gst, slippage, total_friction, net_pnl, capital_after, created_at)"
                        " VALUES ('P1', ?, 'DOT', 'long', 1, 1.14, 1.12, 'a', 'b', 'stop', -2, 0, 0, 0, 0, 0, 0, 0, 0, -2, ?, 'now')", (acc, cap))
            con.execute("INSERT INTO positions (position_id, account_id, symbol, direction, qty, entry_price, current_stop, target_price, breakeven_price,"
                        " best_price, entry_time, updated_at) VALUES (?, ?, 'X', 'long', 1, 1, 0, 0, 0, 1, 'now', 'now')", (f"POS_{acc}", acc))
            con.execute("INSERT INTO orders (order_id, account_id, symbol, direction, intent, order_type, quantity, requested_price, status, created_at)"
                        " VALUES (?, ?, 'X', 'BUY', 'ENTRY', 'MARKET', 1, 1, 'FILLED', 'now')", (f"O_{acc}", acc))
            con.execute("INSERT INTO portfolio_snapshots (account_id, timestamp, date, starting_capital, current_capital, gross_pnl, total_fees, net_pnl,"
                        " open_positions, total_trades, win_trades, loss_trades, win_rate) VALUES (?, 'now', '2026-10-23', 1, ?, 0, 0, 0, 0, 1, 0, 1, 0)", (acc, cap))
    con.close()


def _count(db_path: Path, table: str, acc: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = ?", (acc,)).fetchone()[0]
    finally:
        con.close()


class TestResetAccount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "paper_trading.db"
        self.backups = self.dir / "backups"
        _seed(self.db)
        (self.dir / "crypto_dip_reversal_state.json").write_text(json.dumps({"trading_capital": 97330.0, "open_positions": {}, "completed_trades_count": 1}))
        (self.dir / "mcx_trend_carry_state.json").write_text(json.dumps({"rebalanced_for": "2026-09-30", "opened": []}))

    def tearDown(self):
        self.tmp.cleanup()

    def reset(self, acc, confirm):
        return R.reset_account(acc, confirm, db_path=self.db, state_dir=self.dir, backup_dir=self.backups)

    def test_wrong_confirmation_changes_nothing(self):
        with self.assertRaises(R.ResetError):
            self.reset("CRYPTO_DIP_REVERSAL", "yes")
        self.assertEqual(_count(self.db, "trades", "CRYPTO_DIP_REVERSAL"), 1)
        self.assertFalse(self.backups.exists() and any(self.backups.iterdir()))

    def test_unknown_account_is_refused(self):
        with self.assertRaises(R.ResetError):
            self.reset("DRYRUN_ACCOUNT", "DRYRUN_ACCOUNT")

    def test_reset_wipes_only_that_account_and_restores_its_starting_money(self):
        out = self.reset("CRYPTO_DIP_REVERSAL", "CRYPTO_DIP_REVERSAL")
        self.assertEqual(out["capital"], 100000.0)
        self.assertEqual(out["deleted"], {"trades": 1, "positions": 1, "orders": 1, "portfolio_snapshots": 1})
        for t in R.TABLES:
            self.assertEqual(_count(self.db, t, "CRYPTO_DIP_REVERSAL"), 0, t)
            self.assertEqual(_count(self.db, t, "MCX_TREND_CARRY"), 1, t)          # the other account is untouched
        acc = TradingDB(self.db).get_account("CRYPTO_DIP_REVERSAL")
        self.assertEqual(acc["current_capital"], 100000.0)
        self.assertEqual(TradingDB(self.db).get_account("MCX_TREND_CARRY")["current_capital"], 260000.0)
        st = json.loads((self.dir / "crypto_dip_reversal_state.json").read_text())
        self.assertEqual((st["trading_capital"], st["open_positions"], st["completed_trades_count"]), (100000.0, {}, 0))

    def test_backup_is_taken_first_and_still_holds_the_old_history(self):
        out = self.reset("CRYPTO_DIP_REVERSAL", "CRYPTO_DIP_REVERSAL")
        backup = Path(out["backup"])
        self.assertTrue(backup.exists())
        self.assertTrue(backup.name.startswith("pre_reset_crypto_dip_reversal_"))
        self.assertEqual(_count(backup, "trades", "CRYPTO_DIP_REVERSAL"), 1)

    def test_mcx_reset_clears_the_rebalance_marker_so_it_buys_again(self):
        self.reset("MCX_TREND_CARRY", " MCX_TREND_CARRY ")
        st = json.loads((self.dir / "mcx_trend_carry_state.json").read_text())
        self.assertNotIn("rebalanced_for", st)
        self.assertEqual(TradingDB(self.db).get_account("MCX_TREND_CARRY")["current_capital"], 250000.0)

    def test_open_real_delta_demo_position_blocks_the_reset(self):
        (self.dir / "crypto_dip_reversal_state.json").write_text(json.dumps({"open_positions": {"BTC": {"real": True}}}))
        with self.assertRaises(R.ResetError):
            self.reset("CRYPTO_DIP_REVERSAL", "CRYPTO_DIP_REVERSAL")
        self.assertEqual(_count(self.db, "trades", "CRYPTO_DIP_REVERSAL"), 1)


class TestAccountLock(unittest.TestCase):
    def test_second_holder_waits_then_gives_up(self):
        with tempfile.TemporaryDirectory() as d:
            with R.account_lock("CRYPTO_DIP_REVERSAL", lock_dir=Path(d)):
                with self.assertRaises(R.ResetError):
                    with R.account_lock("CRYPTO_DIP_REVERSAL", timeout_s=0.3, lock_dir=Path(d)):
                        pass
            with R.account_lock("CRYPTO_DIP_REVERSAL", timeout_s=0.3, lock_dir=Path(d)):   # free again once released
                pass

    def test_accounts_lock_independently(self):
        with tempfile.TemporaryDirectory() as d:
            with R.account_lock("CRYPTO_DIP_REVERSAL", lock_dir=Path(d)):
                with R.account_lock("MCX_TREND_CARRY", timeout_s=0.3, lock_dir=Path(d)):
                    pass


class TestResetEndpoint(unittest.TestCase):
    def setUp(self):
        from engine.backtest_api import Handler
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/api/paper/reset"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, body):
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_passes_account_and_confirmation_through(self):
        with mock.patch("engine.account_reset.reset_account", return_value={"account": "MCX_TREND_CARRY", "capital": 250000.0}) as m:
            status, body = self.post({"account": "MCX_TREND_CARRY", "confirm": "MCX_TREND_CARRY"})
        self.assertEqual(status, 200)
        self.assertEqual(body["capital"], 250000.0)
        m.assert_called_once_with("MCX_TREND_CARRY", "MCX_TREND_CARRY")

    def test_refusal_is_a_400_with_the_reason(self):
        with mock.patch("engine.account_reset.reset_account", side_effect=R.ResetError("To reset, type the account name exactly")):
            status, body = self.post({"account": "MCX_TREND_CARRY", "confirm": "no"})
        self.assertEqual(status, 400)
        self.assertIn("type the account name", body["error"])


if __name__ == "__main__":
    unittest.main()
