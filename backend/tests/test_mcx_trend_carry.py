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


PRICES = {"MCX_FO|GOLDTEN": 12_000.0, "MCX_FO|SILVERMIC": 150_000.0}


class FakeBroker:
    def __init__(self, prices=None):
        self.prices = dict(PRICES if prices is None else prices)

    def get_ltp_batch(self, keys):
        return {k: v for k, v in self.prices.items() if k in keys}


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


class RunCase(unittest.TestCase):
    """Shared fixture only (no tests), so TestRebalanceLikeTheBacktest does not re-run TestRun's tests (2026-10-30 test audit)."""
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

    def run_once(self, now=NOW, prices=None):
        return M.run(FakeBroker(prices), self.db, now, report=False, send=lambda m: None)

    def due_again(self):
        st = json.loads(M.STATE.read_text()); st["rebalanced_on"] = str(DAYS[-11].date()); M.STATE.write_text(json.dumps(st))

    def gold(self):
        return [p for p in self.db.get_open_positions(M.ACCOUNT) if p["symbol"].startswith("GOLD")]


class TestRun(RunCase):
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
        self.assertNotIn("GOLDTEN26DECFUT", reasons)          # gold's weight went 60% -> 100%: lots ADDED, nothing sold (2026-10-29)

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



class TestRebalanceLikeTheBacktest(RunCase):
    """2026-10-29 code review: the live re-check must trade like trend_carry.backtest -- resize by the difference, size from the
    marked-to-market account value, and never sell a holding before the contract it moves into has a price."""

    def test_a_bigger_share_buys_only_the_extra_lots(self):
        self.run_once()
        before = sum(int(p["qty"]) for p in self.gold())
        self.due_again(); self.w = {"GOLDM": 0.9, "SILVERM": 0.1}
        self.run_once()
        self.assertGreater(sum(int(p["qty"]) for p in self.gold()), before)
        self.assertNotIn("GOLDTEN26DECFUT", {t["symbol"] for t in self.db.get_trades(account_id=M.ACCOUNT)})

    def test_a_smaller_share_sells_only_the_difference(self):
        self.w = {"GOLDM": 0.9, "SILVERM": 0.1}
        self.run_once()
        before = sum(int(p["qty"]) for p in self.gold())
        self.due_again(); self.w = {"GOLDM": 0.3, "SILVERM": 0.7}
        self.run_once()
        after = sum(int(p["qty"]) for p in self.gold())
        sold = [t for t in self.db.get_trades(account_id=M.ACCOUNT) if t["symbol"] == "GOLDTEN26DECFUT"]
        self.assertTrue(0 < after < before)
        self.assertEqual(sum(t["qty"] for t in sold), before - after)            # fees on the lots sold only
        self.assertEqual({t["exit_reason"] for t in sold}, {"rebalance_resize"})

    def test_sizing_uses_the_account_value_with_open_profit(self):
        self.run_once()
        seen = []
        real = M.S.lots_for
        self.due_again()
        with mock.patch.object(M.S, "lots_for", side_effect=lambda cap, *a, **k: seen.append(cap) or real(cap, *a, **k)):
            self.run_once(prices={"MCX_FO|GOLDTEN": 13_000.0, "MCX_FO|SILVERMIC": 150_000.0})     # gold up: open profit
        g = self.gold()
        open_profit = sum((13_000.0 - p["entry_price"]) * p["qty"] * json.loads(p["state"])["lot_units"] for p in g)
        self.assertGreater(open_profit, 0)
        self.assertAlmostEqual(seen[0], 250_000 + open_profit, delta=1)

    def test_no_price_for_the_next_contract_keeps_the_old_one(self):
        self.run_once()
        roll_day = datetime(2026, 11, 27, 9, 10, tzinfo=M.IST)            # SILVERMIC expires 30 Nov: inside the 3-day roll window
        nxt = pd.DataFrame({"symbol": ["SILVERMIC"], "expiry": pd.to_datetime(["2027-02-26"]), "instrument_key": ["MCX_FO|SILVERMIC_FEB"],
                            "tradingsymbol": ["SILVERMIC27FEBFUT"]})
        with mock.patch.object(M.futures_eod, "active_contracts", return_value=pd.concat([CONTRACTS, nxt], ignore_index=True)), \
             mock.patch.object(M, "needs_rebalance", return_value=False):
            self.run_once(now=roll_day, prices=PRICES)                     # no price at all for the February contract
        held = {p["symbol"] for p in self.db.get_open_positions(M.ACCOUNT)}
        self.assertIn("SILVERMIC26NOVFUT", held)                          # not sold into nothing
        self.assertEqual([t for t in self.db.get_trades(account_id=M.ACCOUNT) if t["symbol"] == "SILVERMIC26NOVFUT"], [])
