"""2026-11-20: one equity account (RESULTS_GAP) that the user tops up (Rs10-30k whenever they have it). A deposit is money put in, not
profit: it raises both the money put in (initial_capital) and the booked money, every strategy sizes from it, and the nightly
paper-vs-backtest check grows each deposit from its own day."""
import argparse
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from engine import earnings_drift as ED
from engine import paper_vs_backtest as P
from engine import results_gap as RG
from engine.database import TradingDB


def setUpModule():
    """The live backend/.env may switch strategies off; these tests start from all-on (2026-11-22: drift's share depends on RG_MOMENTUM)."""
    global _ENV
    from core import switches
    _ENV = mock.patch.dict("os.environ", {f"STRATEGY_{n}": "on" for n in switches.NAMES}); _ENV.start()


def tearDownModule():
    _ENV.stop()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name) / "t.db"
        self.db = TradingDB(self.path)
        self.db.init_account("RESULTS_GAP", name="x", capital=100_000.0)

    def tearDown(self):
        self.tmp.cleanup()


class TestDeposit(Base):
    def test_money_put_in_and_booked_money_both_rise_profit_does_not(self):
        self.db.update_capital("RESULTS_GAP", 103_000.0)                    # Rs3k profit so far
        self.db.add_money("RESULTS_GAP", 20_000, at="2026-11-20T10:00:00+05:30")
        a = self.db.get_account("RESULTS_GAP")
        self.assertEqual((a["initial_capital"], a["current_capital"]), (120_000.0, 123_000.0))
        self.assertEqual(a["current_capital"] - a["initial_capital"], 3_000.0)
        self.assertEqual(self.db.deposits("RESULTS_GAP"), [("2026-11-20", 20_000.0)])
        self.assertEqual(self.db.deposits_total("RESULTS_GAP"), 20_000.0)

    def test_only_positive_amounts_and_existing_accounts(self):
        for bad in (0, -5_000):
            with self.assertRaises(ValueError):
                self.db.add_money("RESULTS_GAP", bad)
        with self.assertRaises(ValueError):
            self.db.add_money("NO_SUCH_ACCOUNT", 10_000)
        self.assertEqual(self.db.get_account("RESULTS_GAP")["initial_capital"], 100_000.0)

    def test_a_later_engine_start_keeps_the_deposit(self):
        self.db.add_money("RESULTS_GAP", 30_000)
        self.db.init_account("RESULTS_GAP", name="x", capital=100_000.0)   # no trades yet: this used to reset the money to 1L
        a = self.db.get_account("RESULTS_GAP")
        self.assertEqual((a["initial_capital"], a["current_capital"]), (130_000.0, 130_000.0))


class TestStrategiesSizeFromIt(Base):
    def test_earnings_drift_money_includes_deposits(self):
        before = ED.sleeve_value(self.db)
        self.db.add_money("RESULTS_GAP", 20_000)
        self.assertEqual(ED.sleeve_value(self.db), before + 20_000 * ED.ACCOUNT_SHARE)   # drift's half of the account (2026-11-22)

    def test_results_gap_free_cash_includes_deposits(self):
        with mock.patch.object(RG, "ED", mock.Mock(account_locked=lambda db: 0.0)):
            before = RG.free_cash(self.db)
            self.db.add_money("RESULTS_GAP", 10_000)
            self.assertEqual(RG.free_cash(self.db), before + 10_000)


class TestPaperVsBacktest(Base):
    def test_backtest_starts_from_the_first_money_and_each_deposit_grows_from_its_day(self):
        with sqlite3.connect(self.path) as con:
            con.execute("UPDATE accounts SET created_at = '2026-11-10T09:00:00+05:30' WHERE account_id = 'RESULTS_GAP'")
        self.db.add_money("RESULTS_GAP", 50_000, at="2026-11-12T20:00:00+05:30")
        seen = []
        days = ["2026-11-11", "2026-11-12", "2026-11-13"]

        def run(body):
            seen.append(body["capital"])
            eq = [100_000, 110_000, 121_000] if body["strategy"] == "results_gap" else [body["capital"]] * 3   # the swing sleeves flat
            return {"days": days, "equity": eq, "events": [[], [], []]}
        with sqlite3.connect(self.path) as con:
            r = P.check_account(con, "RESULTS_GAP", date(2026, 11, 18), run)
        self.assertEqual(seen[0], 100_000.0)                               # not 1.5L from the first day
        self.assertEqual(r["expected"]["2026-11-11"], 100_000)
        self.assertEqual(r["expected"]["2026-11-12"], 160_000)               # 110k + the 50k put in that day
        self.assertAlmostEqual(r["expected"]["2026-11-13"], 121_000 + 55_000)   # the 50k grew 10% with the account


class TestCommand(Base):
    def test_cli_backs_up_takes_the_account_lock_and_adds(self):
        import cli
        calls = []
        with mock.patch("engine.backup_db.backup_once", side_effect=lambda *a, **k: calls.append("backup")), \
                mock.patch("engine.account_reset.account_lock", side_effect=lambda aid, **k: (calls.append(f"lock {aid}"), mock.MagicMock())[1]):
            cli.cmd_add_money(argparse.Namespace(db=str(self.path), account="RESULTS_GAP", amount=25_000.0, note="salary"))
        self.assertEqual(calls, ["backup", "lock RESULTS_GAP"])
        self.assertEqual(self.db.get_account("RESULTS_GAP")["initial_capital"], 125_000.0)


if __name__ == "__main__":
    unittest.main()
