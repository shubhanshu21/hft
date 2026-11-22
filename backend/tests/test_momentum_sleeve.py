"""2026-11-22 (user: "Drift grows + momentum 50/50"): momentum_12_1 runs inside the ONE equity account (RESULTS_GAP) next to earnings
drift and results gap. Each swing sleeve sizes from half the account's money (so results-gap profits get invested too), cash only;
results gap still trades 3x the whole account intraday. Joint backtest Rs1L Jan 2023-Nov 2026: Rs11.40L / -23.0% -> Rs17.13L / -28.0%."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

from core import switches
from engine import earnings_drift as ED
from engine import momentum as MO
from engine import results_gap as RG
from engine.database import TradingDB

IST = timezone(timedelta(hours=5, minutes=30))


def setUpModule():
    global _env
    _env = mock.patch.dict(os.environ, {f"STRATEGY_{n}": "on" for n in switches.NAMES}); _env.start()


def tearDownModule():
    _env.stop()


def scores_row(order):
    """A momentum score row: the given symbols strongest first."""
    return pd.Series({s: 1.0 - i / 100 for i, s in enumerate(order)})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = TradingDB(self.tmp / "t.db")
        self.db.init_account("RESULTS_GAP", name="x", capital=100_000.0)
        for p in (mock.patch.object(MO, "STATE", self.tmp / "mom.json"), mock.patch.object(ED, "STATE", self.tmp / "ed.json"),
                  mock.patch.object(MO.M, "N", 4)):
            p.start(); self.addCleanup(p.stop)

    def plan(self, today, order, last=None):
        last = last or (today - timedelta(days=1))
        with mock.patch.object(MO, "_latest_scores", return_value=(pd.Timestamp(last), scores_row(order))):
            return MO.plan(self.db, today, set())

    def hold(self, sym, qty, px, strategy="momentum_12_1"):
        self.db.open_position(f"P_{sym}", sym, "long", qty, px, 0.0, 0.0, px, account_id="RESULTS_GAP", instrument_key=f"NSE_EQ|{sym}",
                              strategy=strategy, state=json.dumps({"entry_date": "2026-11-02"}))

    def fill(self, opens, day=date(2026, 12, 1)):
        broker = mock.Mock(get_quote_batch=lambda keys: {k: {"today_open": opens[k.split("|")[1]]} for k in keys if k.split("|")[1] in opens})
        lines = []
        MO.fill(self.db, broker, datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=30), lambda s: f"NSE_EQ|{s}", lines)
        return lines


class TestPlan(Base):
    def test_first_run_buys_the_top_n_skipping_stocks_drift_holds(self):
        self.hold("B", 10, 100.0, strategy="earnings_drift")
        self.plan(date(2026, 11, 23), ["A", "B", "C", "D", "E"])
        st = json.loads((self.tmp / "mom.json").read_text())
        self.assertEqual(st["month"], "2026-11")
        self.assertEqual(st["targets"], ["A", "C", "D", "E"])
        self.assertEqual(st["buy"], ["A", "C", "D", "E"])
        self.assertEqual(st["sell"], {})

    def test_new_month_sells_the_dropouts_and_buys_the_new_names(self):
        (self.tmp / "mom.json").write_text(json.dumps({"month": "2026-11", "targets": ["A", "B", "C", "D"]}))
        for s in ("A", "B", "C", "D"):
            self.hold(s, 1, 100.0)
        self.plan(date(2026, 12, 1), ["A", "C", "X", "Y", "B"])
        st = json.loads((self.tmp / "mom.json").read_text())
        self.assertEqual(sorted(self.db.get_position(pid)["symbol"] for pid in st["sell"]), ["B", "D"])
        self.assertEqual(st["buy"], ["X", "Y"])
        self.assertEqual(st["month"], "2026-12")

    def test_same_month_only_tops_up_from_the_saved_list(self):
        (self.tmp / "mom.json").write_text(json.dumps({"month": "2026-11", "targets": ["A", "B", "C", "D"]}))
        self.hold("A", 1, 100.0)
        self.plan(date(2026, 11, 24), ["Z", "Y", "X", "W"])             # a different ranking today changes nothing mid-month
        st = json.loads((self.tmp / "mom.json").read_text())
        self.assertEqual((st["sell"], st["buy"], st["targets"]), ({}, ["B", "C", "D"], ["A", "B", "C", "D"]))

    def test_stale_prices_mean_no_orders(self):
        lines = self.plan(date(2026, 12, 1), ["A", "B"], last=date(2026, 11, 26))
        self.assertTrue(any("not up to date" in ln for ln in lines))
        st = json.loads((self.tmp / "mom.json").read_text())
        self.assertEqual((st["buy"], st["sell"]), ([], {}))

    def test_switched_off_still_sells_at_the_refresh_but_buys_nothing(self):
        (self.tmp / "mom.json").write_text(json.dumps({"month": "2026-11", "targets": ["A", "B", "C", "D"]}))
        self.hold("D", 1, 100.0)
        with mock.patch.dict(os.environ, {"STRATEGY_RG_MOMENTUM": "off"}):
            self.plan(date(2026, 12, 1), ["A", "B", "C", "E"])
        st = json.loads((self.tmp / "mom.json").read_text())
        self.assertEqual((len(st["sell"]), st["buy"]), (1, []))


class TestFill(Base):
    def test_buys_half_the_account_split_over_n_at_the_official_open(self):
        self.plan(date(2026, 12, 1), ["A", "B", "C", "D"])
        lines = self.fill({"A": 100.0, "B": 250.0, "C": 3000.0, "D": 50.0})
        held = {p["symbol"]: int(p["qty"]) for p in MO._mine(self.db)}
        slot = 100_000 * MO.SHARE / 4                                       # Rs12,500 each
        self.assertEqual(held, {"A": int(slot // 100), "B": int(slot // 250), "C": int(slot // 3000), "D": int(slot // 50)})
        self.assertTrue(any(ln.startswith("BOUGHT ") for ln in lines))

    def test_a_buy_never_spends_more_than_the_free_cash(self):
        self.hold("ED1", 900, 100.0, strategy="earnings_drift")             # Rs90k already in drift stocks
        self.plan(date(2026, 12, 1), ["A", "B", "C", "D"])
        self.fill({"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0})
        spent = sum(float(p["qty"]) * float(p["entry_price"]) for p in MO._mine(self.db))
        self.assertLessEqual(spent, 10_000.0)

    def test_dropouts_are_sold_at_the_open_with_delivery_costs(self):
        (self.tmp / "mom.json").write_text(json.dumps({"month": "2026-11", "targets": ["A", "B", "C", "D"]}))
        self.hold("D", 10, 100.0)
        self.plan(date(2026, 12, 1), ["A", "B", "C", "E"])
        self.fill({"A": 100.0, "B": 100.0, "C": 100.0, "E": 100.0, "D": 120.0})
        t = next(t for t in self.db.get_trades(account_id="RESULTS_GAP") if t["symbol"] == "D")
        self.assertEqual((t["strategy"], t["exit_reason"]), ("momentum_12_1", "momentum_out"))
        self.assertAlmostEqual(t["net_pnl"], 200.0 - MO.delivery_costs(100.0, 120.0, 10)["total"], places=2)


class TestSharedAccount(Base):
    def test_each_swing_sleeve_sizes_from_half_the_account(self):
        self.assertEqual(ED.sleeve_value(self.db), 100_000 * ED.ACCOUNT_SHARE)
        self.assertEqual(MO.SHARE + ED.ACCOUNT_SHARE, 1.0)
        with mock.patch.dict(os.environ, {"STRATEGY_RG_MOMENTUM": "off"}):
            self.assertEqual(ED.sleeve_value(self.db), 100_000.0)            # momentum off: drift may use the whole account

    def test_results_gap_counts_both_sleeves_cash(self):
        self.hold("ED1", 100, 100.0, strategy="earnings_drift")
        self.hold("M1", 200, 100.0)
        booked, free, margin = RG.margin_money(self.db)
        self.assertEqual((booked, free), (100_000.0, 70_000.0))
        self.assertAlmostEqual(margin, 70_000.0 + 0.75 * 30_000.0)
        self.assertEqual(RG.free_cash(self.db), 70_000.0)

    def test_results_gap_never_sells_momentum_holdings(self):
        self.hold("M1", 200, 100.0)
        self.assertEqual(RG._own(self.db), [])


if __name__ == "__main__":
    unittest.main()


class TestAroundTheSleeve(unittest.TestCase):
    def test_paper_vs_backtest_runs_all_three_on_their_shares(self):
        from engine import paper_vs_backtest as P
        b = {x["strategy"]: x["capital"] for x in P.bodies("RESULTS_GAP", 100_000.0)}
        self.assertEqual(b, {"results_gap": 100_000.0, "earnings_drift": 100_000.0 * ED.ACCOUNT_SHARE, "momentum_12_1": 100_000.0 * MO.SHARE})

    def test_today_box_says_what_momentum_does_this_morning(self):
        from engine import backtest_api as api
        tmp = Path(tempfile.mkdtemp())
        (tmp / "momentum_state.json").write_text(json.dumps({"day": "2026-12-01", "due": True, "fresh": True, "targets": ["A", "B"],
                                                             "buy": ["B"], "sell": {"P1": "momentum_out"}}))
        out = api.today_plan("RESULTS_GAP", datetime(2026, 12, 1, 9, 0, tzinfo=IST), state_dir=tmp)
        self.assertTrue(any(ln.startswith("Momentum") and "A, B" in ln for ln in out))
        (tmp / "momentum_state.json").write_text(json.dumps({"day": "2026-12-02", "due": False, "fresh": True, "targets": ["A", "B"], "buy": [], "sell": {}}))
        out = api.today_plan("RESULTS_GAP", datetime(2026, 12, 2, 9, 0, tzinfo=IST), state_dir=tmp)
        self.assertTrue(any("next refresh" in ln for ln in out))
