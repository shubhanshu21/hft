"""engine/mcx_trend_carry.py: the re-check runs every S.REBALANCE_DAYS trading days (~2 weeks since 2026-10-25; was month-end) -- and again
after the account was reset (2026-10-24 fix: a reset wiped the positions but kept the old marker, leaving the account in cash). A holding
still in the top 3 with the same lots is kept, not sold and bought back."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from engine import mcx_trend_carry as M
from engine.database import TradingDB

DAYS = list(pd.bdate_range("2026-10-01", "2026-10-23"))           # completed sessions before NOW
SIGNAL_DAY = DAYS[-1]
NOW = datetime(2026, 10, 26, 9, 10, tzinfo=M.IST)
TABLE = pd.DataFrame({"symbol": "GOLDM", "date": DAYS})
CONTRACTS = pd.DataFrame({"symbol": ["GOLDTEN", "SILVERMIC"], "expiry": pd.to_datetime(["2026-12-31", "2026-11-30"]),
                          "instrument_key": ["MCX_FO|GOLDTEN", "MCX_FO|SILVERMIC"], "tradingsymbol": ["GOLDTEN26DECFUT", "SILVERMIC26NOVFUT"]})


class FakeBroker:
    def get_ltp_batch(self, keys):
        return {"MCX_FO|GOLDTEN": 12_000.0, "MCX_FO|SILVERMIC": 150_000.0}


class TestNeedsRebalance(unittest.TestCase):
    def due(self, st, open_pos=(), ids=()):
        return M.needs_rebalance(st, SIGNAL_DAY, DAYS, list(open_pos), set(ids))

    def test_never_checked_is_due(self):
        self.assertTrue(self.due({}))

    def test_ten_trading_days_later_is_due(self):
        self.assertTrue(self.due({"rebalanced_on": str(DAYS[-11].date()), "opened": ["P1"]}, [{"position_id": "P1"}], {"P1"}))

    def test_a_few_days_later_with_positions_open_is_not(self):
        self.assertFalse(self.due({"rebalanced_on": str(DAYS[-3].date()), "opened": ["P1"]}, [{"position_id": "P1"}], {"P1"}))

    def test_a_reset_that_wiped_the_positions_rebalances_again(self):
        self.assertTrue(self.due({"rebalanced_on": str(DAYS[-3].date()), "opened": ["P1", "P2"]}))

    def test_old_state_without_ids_and_nothing_open_counts_as_reset(self):
        self.assertTrue(self.due({"rebalanced_on": str(DAYS[-3].date())}))

    def test_a_check_that_bought_nothing_is_not_repeated(self):
        self.assertFalse(self.due({"rebalanced_on": str(DAYS[-3].date()), "opened": []}))

    def test_positions_closed_by_the_engine_itself_are_not_a_reset(self):
        self.assertFalse(self.due({"rebalanced_on": str(DAYS[-3].date()), "opened": ["P1"]}, ids={"P1"}))


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = TradingDB(self.tmp / "t.db")
        self.w = {"GOLDM": 0.6, "SILVERM": 0.4}
        patches = [
            mock.patch.object(M, "STATE", self.tmp / "state.json"),
            mock.patch.object(M.futures_eod, "load", return_value=TABLE),
            mock.patch.object(M.futures_eod, "active_contracts", return_value=CONTRACTS),
            mock.patch.object(M.S, "daily_panel", return_value=(None, None)),
            mock.patch.object(M.S, "targets", side_effect=lambda R, C, t: dict(self.w)),
            mock.patch.dict("os.environ", {"MCX_TREND_CARRY_CAPITAL": "250000", "MCX_TREND_CARRY_EXPOSURE": "1.5"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_once(self, now=NOW):
        return M.run(FakeBroker(), self.db, now, report=False, send=lambda m: None)

    def test_reset_account_buys_the_current_signal(self):
        M.STATE.write_text(json.dumps({"reset_at": "2026-10-24T20:16:20+05:30"}))      # the live state after the 2026-10-24 reset
        out = self.run_once()
        self.assertEqual(out["did"], "rebalance")
        held = {p["symbol"] for p in self.db.get_open_positions(M.ACCOUNT)}
        self.assertEqual(held, {"GOLDTEN26DECFUT", "SILVERMIC26NOVFUT"})
        st = json.loads(M.STATE.read_text())
        self.assertEqual(st["rebalanced_on"], str(SIGNAL_DAY.date()))
        self.assertEqual(set(st["opened"]), {p["position_id"] for p in self.db.get_open_positions(M.ACCOUNT)})

    def test_second_run_before_ten_days_does_not_trade(self):
        self.run_once()
        ids = {p["position_id"] for p in self.db.get_open_positions(M.ACCOUNT)}
        out = self.run_once(datetime(2026, 10, 26, 23, 40, tzinfo=M.IST))
        self.assertIsNone(out["did"])
        self.assertEqual({p["position_id"] for p in self.db.get_open_positions(M.ACCOUNT)}, ids)

    def test_a_holding_still_in_the_top_3_is_kept(self):
        self.run_once()
        gold = next(p for p in self.db.get_open_positions(M.ACCOUNT) if p["symbol"].startswith("GOLD"))
        st = json.loads(M.STATE.read_text()); st["rebalanced_on"] = str(DAYS[-11].date()); M.STATE.write_text(json.dumps(st))
        out = self.run_once()                                               # due again: same targets, same prices -> same lots
        self.assertEqual(out["did"], "rebalance")
        self.assertIn(gold["position_id"], {p["position_id"] for p in self.db.get_open_positions(M.ACCOUNT)})
        self.assertTrue(any("KEPT" in line for line in out["lines"]))
        self.assertEqual(self.db.get_trades(account_id=M.ACCOUNT), [])      # nothing sold, so no fees paid

    def test_a_dropped_holding_is_sold(self):
        self.run_once()
        st = json.loads(M.STATE.read_text()); st["rebalanced_on"] = str(DAYS[-11].date()); M.STATE.write_text(json.dumps(st))
        self.w = {"GOLDM": 1.0}                                             # silver falls out of the top picks
        self.run_once()
        held = {p["symbol"] for p in self.db.get_open_positions(M.ACCOUNT)}
        self.assertNotIn("SILVERMIC26NOVFUT", held)
        reasons = {t["symbol"]: t["exit_reason"] for t in self.db.get_trades(account_id=M.ACCOUNT)}
        self.assertEqual(reasons["SILVERMIC26NOVFUT"], "rebalance_dropped")
        self.assertEqual(reasons["GOLDTEN26DECFUT"], "rebalance_resize")             # gold's weight went 60% -> 100%: more lots

    def test_wiping_the_positions_triggers_a_new_rebalance(self):
        self.run_once()
        with self.db._get_conn() as conn:
            conn.execute("DELETE FROM positions WHERE account_id = ?", (M.ACCOUNT,))   # what an account reset does (temp test DB only)
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
