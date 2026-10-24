"""engine/mcx_trend_carry.py: the monthly rebalance runs once per month end -- and again after the account was reset (2026-10-24 fix:
the 2026-10-21 Rs5L -> Rs2.5L reset wiped the positions but kept rebalanced_for, leaving the account in cash for the rest of the month)."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from engine import mcx_trend_carry as M
from engine.database import TradingDB

MONTH_END = pd.Timestamp("2026-09-30")
NOW = datetime(2026, 10, 26, 9, 10, tzinfo=M.IST)
CONTRACTS = pd.DataFrame({"symbol": ["GOLDTEN", "SILVERMIC"], "expiry": pd.to_datetime(["2026-12-31", "2026-11-30"]),
                          "instrument_key": ["MCX_FO|GOLDTEN", "MCX_FO|SILVERMIC"], "tradingsymbol": ["GOLDTEN26DECFUT", "SILVERMIC26NOVFUT"]})


class FakeBroker:
    def get_ltp_batch(self, keys):
        return {"MCX_FO|GOLDTEN": 12_000.0, "MCX_FO|SILVERMIC": 150_000.0}


class TestNeedsRebalance(unittest.TestCase):
    def test_a_new_month_end_always_rebalances(self):
        self.assertTrue(M.needs_rebalance({"rebalanced_for": "2026-08-31", "opened": ["P1"]}, MONTH_END, [{"position_id": "P1"}], {"P1"}))

    def test_same_month_with_positions_open_does_not(self):
        self.assertFalse(M.needs_rebalance({"rebalanced_for": "2026-09-30", "opened": ["P1"]}, MONTH_END, [{"position_id": "P1"}], {"P1"}))

    def test_a_reset_that_wiped_the_positions_rebalances_again(self):
        self.assertTrue(M.needs_rebalance({"rebalanced_for": "2026-09-30", "opened": ["P1", "P2"]}, MONTH_END, [], set()))

    def test_old_state_without_ids_and_nothing_open_counts_as_reset(self):
        self.assertTrue(M.needs_rebalance({"rebalanced_for": "2026-09-30"}, MONTH_END, [], set()))

    def test_a_rebalance_that_bought_nothing_is_not_repeated(self):
        self.assertFalse(M.needs_rebalance({"rebalanced_for": "2026-09-30", "opened": []}, MONTH_END, [], set()))

    def test_positions_closed_by_the_engine_itself_are_not_a_reset(self):
        # a roll closed P1 but could not buy the next contract: P1 is still in the DB (closed), so this is not a reset
        self.assertFalse(M.needs_rebalance({"rebalanced_for": "2026-09-30", "opened": ["P1"]}, MONTH_END, [], {"P1"}))


class TestRunAfterReset(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = TradingDB(self.tmp / "t.db")
        patches = [
            mock.patch.object(M, "STATE", self.tmp / "state.json"),
            mock.patch.object(M.futures_eod, "load", return_value=pd.DataFrame()),
            mock.patch.object(M.futures_eod, "active_contracts", return_value=CONTRACTS),
            mock.patch.object(M.S, "month_ends", return_value=[pd.Timestamp("2026-08-31"), MONTH_END]),
            mock.patch.object(M.S, "panel", return_value=(None, None)),
            mock.patch.object(M.S, "targets", return_value={"GOLDM": 0.6, "SILVERM": 0.4}),
            mock.patch.dict("os.environ", {"MCX_TREND_CARRY_CAPITAL": "250000", "MCX_TREND_CARRY_EXPOSURE": "1.5"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_once(self):
        return M.run(FakeBroker(), self.db, NOW, report=False, send=lambda m: None)

    def test_reset_account_buys_the_current_signal_again(self):
        M.STATE.write_text(json.dumps({"rebalanced_for": "2026-09-30"}))      # exactly the live state after the 2026-10-21 reset
        out = self.run_once()
        self.assertEqual(out["did"], "rebalance")
        held = {p["symbol"] for p in self.db.get_open_positions(M.ACCOUNT)}
        self.assertEqual(held, {"GOLDTEN26DECFUT", "SILVERMIC26NOVFUT"})
        st = json.loads(M.STATE.read_text())
        self.assertEqual(st["rebalanced_for"], "2026-09-30")
        self.assertEqual(set(st["opened"]), {p["position_id"] for p in self.db.get_open_positions(M.ACCOUNT)})

    def test_second_run_the_same_month_does_not_trade_again(self):
        self.run_once()
        n = len(self.db.get_open_positions(M.ACCOUNT))
        out = self.run_once()
        self.assertIsNone(out["did"])
        self.assertEqual(len(self.db.get_open_positions(M.ACCOUNT)), n)

    def test_wiping_the_positions_mid_month_triggers_a_new_rebalance(self):
        self.run_once()
        with self.db._get_conn() as conn:
            conn.execute("DELETE FROM positions WHERE account_id = ?", (M.ACCOUNT,))   # what a manual account reset does (temp test DB only)
        out = self.run_once()
        self.assertEqual(out["did"], "rebalance")
        self.assertEqual(len(self.db.get_open_positions(M.ACCOUNT)), 2)


class TestPositionIds(unittest.TestCase):
    def test_returns_open_and_closed_ids_for_that_account_only(self):
        db = TradingDB(Path(tempfile.mkdtemp()) / "t.db")
        db.open_position("A1", "X", "long", 1, 100.0, 0.0, 0.0, 0.0, account_id="ACC")
        db.open_position("A2", "Y", "long", 1, 100.0, 0.0, 0.0, 0.0, account_id="ACC")
        db.open_position("B1", "Z", "long", 1, 100.0, 0.0, 0.0, 0.0, account_id="OTHER")
        db.close_position("A2", 101.0, "test", 1.0, 1.0, 0.0)
        self.assertEqual(db.get_position_ids("ACC"), {"A1", "A2"})


if __name__ == "__main__":
    unittest.main()
