"""equity/earnings_drift strategy + engine/earnings_drift.py, sharing the RESULTS_GAP paper account with results_gap (2026-10-29).
Synthetic daily archive, temp DB, temp state -- no network."""
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from engine import earnings_drift as E
from engine import results_gap as RGE
from engine.database import TradingDB
from markets.equity.strategies.tf_daily.earnings_drift import strategy as S
from markets.equity.strategies.tf_intraday.results_gap import strategy as RG

IST = E.IST
DAYS = pd.bdate_range("2026-05-01", "2026-10-23")           # last completed day Fri 23 Oct -> the plan is for Mon 26 Oct


def make_ind(good_jump=0.10, weak_jump=0.01, healthy=True):
    """GOOD and WEAK report on Wed 21 Oct (meeting day); the reaction is complete at Thu 22 Oct's close... and the signal day is 22 Oct.
    With the meeting on 22 Oct the signal day is 23 Oct (the last completed day), so the buy is at Mon 26 Oct's open."""
    syms = ["GOOD", "WEAK"] + [f"F{i}" for i in range(8)]
    C = pd.DataFrame(100.0, index=DAYS, columns=syms)
    VAL = pd.DataFrame(1e7, index=DAYS, columns=syms)
    m = DAYS.get_loc(pd.Timestamp("2026-10-22"))
    C.iloc[m:, 0] = 100 * (1 + good_jump); C.iloc[m:, 1] = 100 * (1 + weak_jump)
    VAL.iloc[m:m + 2, :2] = 5e7                                              # 5x the usual traded value on both days
    univ = pd.DataFrame(True, index=DAYS, columns=syms)
    breadth = pd.Series(80.0 if healthy else 30.0, index=DAYS)
    return {"C": C, "O": C.copy(), "VAL": VAL, "univ": univ, "breadth": breadth}


EVENTS = pd.DataFrame({"sym": ["GOOD", "WEAK"], "d": pd.to_datetime(["2026-10-22", "2026-10-22"])})


class TestStrategy(unittest.TestCase):
    def test_only_strong_reaction_on_heavy_volume(self):
        sig = S.signals(make_ind(), EVENTS)
        self.assertEqual(sig["sym"].tolist(), ["GOOD"])
        self.assertEqual(sig["signal_day"].iloc[0], pd.Timestamp("2026-10-23"))

    def test_no_buys_in_an_unhealthy_market(self):
        ind = make_ind(healthy=False)
        self.assertEqual(S.candidates(ind, S.signals(ind, EVENTS), pd.Timestamp("2026-10-23")), [])

    def test_delivery_costs(self):
        c = S.delivery_costs(100.0, 110.0, 100)
        fee, slip = S._fee(10_000, 11_000)
        self.assertAlmostEqual(c["total"], fee + slip, places=2)
        self.assertGreater(c["stt"], 0)

    def test_trading_days_held(self):
        self.assertEqual(S.trading_days_held(date(2026, 10, 23), date(2026, 10, 26), set()), 1)              # Fri -> Mon
        self.assertEqual(S.trading_days_held(date(2026, 10, 23), date(2026, 10, 27), {date(2026, 10, 26)}), 1)  # Mon a holiday


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = TradingDB(self.tmp / "t.db")
        RGE.init_account(self.db)
        for p in (mock.patch.object(E, "STATE", self.tmp / "ed_state.json"), mock.patch.object(RGE, "STATE", self.tmp / "rg_state.json"),
                  mock.patch.object(S, "results_events", return_value=EVENTS)):
            p.start(); self.addCleanup(p.stop)
        self.lookup = lambda s: f"NSE_EQ|{s}"

    def at(self, hh, mm, day=26):
        return datetime(2026, 10, day, hh, mm, 5, tzinfo=IST)

    def hold(self, sym, qty, price, entry_date):
        self.db.open_position(f"POS_ED_{sym}", sym, "long", qty, price, 0.0, 0.0, price, account_id=E.ACCOUNT, instrument_key=f"NSE_EQ|{sym}",
                              strategy=S.NAME, state=json.dumps({"entry_date": entry_date}))


class Broker:
    def __init__(self, opens=None, ltp=None):
        self.opens, self.ltp = opens or {}, ltp or {}
    def get_quote_batch(self, keys):
        return {k: ({"ltp": self.opens[k.split("|")[1]], "today_open": self.opens[k.split("|")[1]]} if k.split("|")[1] in self.opens else None) for k in keys}
    def get_ltp_batch(self, keys):
        return {k: self.ltp.get(k.split("|")[1]) for k in keys}


class TestEngine(Base):
    def test_plan_at_0850_then_buy_at_the_open(self):
        with mock.patch.object(E, "_indicators", return_value=make_ind()):
            lines = E.plan(self.db, self.at(8, 50).date(), set())
        self.assertIn("GOOD", " ".join(lines))
        E.fill(self.db, Broker({"GOOD": 200.0}), self.at(9, 30), self.lookup, [])
        pos = self.db.get_open_positions(E.ACCOUNT)
        self.assertEqual([(p["symbol"], p["strategy"], p["entry_price"]) for p in pos], [("GOOD", S.NAME, 200.0)])
        self.assertEqual(pos[0]["qty"], int(S.SLEEVE_CAPITAL / S.K // 200.0))      # 1/5 of the drift money
        E.fill(self.db, Broker({"GOOD": 200.0}), self.at(9, 40), self.lookup, [])  # a retry slot does not buy it twice
        self.assertEqual(len(self.db.get_open_positions(E.ACCOUNT)), 1)

    def test_stale_archive_means_no_buys(self):
        ind = make_ind(); ind = {k: (v.iloc[:-1] if hasattr(v, "iloc") else v) for k, v in ind.items()}   # archive ends a day early
        with mock.patch.object(E, "_indicators", return_value=ind):
            lines = E.plan(self.db, self.at(8, 50).date(), set())
        self.assertIn("not up to date", " ".join(lines))
        E.fill(self.db, Broker({"GOOD": 200.0}), self.at(9, 30), self.lookup, [])
        self.assertEqual(self.db.get_open_positions(E.ACCOUNT), [])

    def test_sold_at_the_open_after_40_trading_days(self):
        self.hold("OLD", 100, 100.0, "2026-08-31")                              # exactly 40 trading days before 26 Oct (no holidays)
        self.hold("YOUNG", 100, 100.0, "2026-09-01")                            # 39: one more day to go
        self.hold("NEW", 100, 100.0, "2026-10-20")
        with mock.patch.object(E, "_indicators", return_value=make_ind(good_jump=0.0)):
            E.plan(self.db, self.at(8, 50).date(), set())
        cap0 = float(self.db.get_account(E.ACCOUNT)["current_capital"])
        E.fill(self.db, Broker({"OLD": 120.0, "NEW": 120.0, "YOUNG": 120.0}), self.at(9, 30), self.lookup, [])
        self.assertEqual(sorted(p["symbol"] for p in self.db.get_open_positions(E.ACCOUNT)), ["NEW", "YOUNG"])
        t = self.db.get_trades(account_id=E.ACCOUNT)[0]
        self.assertEqual((t["symbol"], t["strategy"], t["exit_price"], t["exit_reason"]), ("OLD", S.NAME, 120.0, "held_40_days"))
        self.assertAlmostEqual(float(self.db.get_account(E.ACCOUNT)["current_capital"]), cap0 + 2000 - S.delivery_costs(100.0, 120.0, 100)["total"], places=2)

    def test_buy_limited_to_free_cash(self):
        self.hold("A", 900, 100.0, "2026-10-20")                                 # Rs90k of the Rs1L already in stocks
        with mock.patch.object(E, "_indicators", return_value=make_ind()):
            E.plan(self.db, self.at(8, 50).date(), set())
        E.fill(self.db, Broker({"GOOD": 100.0}), self.at(9, 30), self.lookup, [])
        good = [p for p in self.db.get_open_positions(E.ACCOUNT) if p["symbol"] == "GOOD"][0]
        self.assertEqual(good["qty"], 100)                                       # Rs10k free, not the Rs20k slot

    def test_no_open_price_drops_the_buy_after_the_retry_window(self):
        with mock.patch.object(E, "_indicators", return_value=make_ind()):
            E.plan(self.db, self.at(8, 50).date(), set())
        E.fill(self.db, Broker({}), self.at(9, 30), self.lookup, [])
        E.fill(self.db, Broker({}), self.at(9, 46), self.lookup, [])
        E.fill(self.db, Broker({"GOOD": 200.0}), self.at(15, 20), self.lookup, [])
        self.assertEqual(self.db.get_open_positions(E.ACCOUNT), [])


class TestSharedAccount(Base):
    """results_gap must not touch earnings_drift's holdings, and sizes its 3x trades from the cash they do not lock."""
    def test_1520_and_morning_sweep_leave_swing_stocks_alone(self):
        self.hold("SWING", 10, 100.0, "2026-10-20")
        RGE.exit_all(self.db, Broker(ltp={"SWING": 110.0}), self.at(15, 20), [])
        self.assertEqual([p["symbol"] for p in self.db.get_open_positions(E.ACCOUNT)], ["SWING"])
        with mock.patch.object(RG, "candidates", return_value=[]), mock.patch.object(RG, "refresh_calendar", return_value=0), \
                mock.patch.object(RG, "universe", return_value=[]), mock.patch.object(RG, "CALENDAR", self.tmp / "cal.csv"):
            RGE.enter(self.db, Broker(ltp={"SWING": 110.0}), self.at(9, 30, day=27), set(), self.lookup, [])
        self.assertEqual([p["symbol"] for p in self.db.get_open_positions(E.ACCOUNT)], ["SWING"])

    def test_intraday_size_uses_only_free_cash(self):
        self.hold("SWING", 600, 100.0, "2026-10-20")                             # Rs60k locked
        self.assertAlmostEqual(E.locked_cash(self.db), 60_000.0)
        self.assertAlmostEqual(RGE.free_cash(self.db), 40_000.0)


class TestPaperPageValue(unittest.TestCase):
    def test_swing_position_blocks_its_full_cost(self):
        from engine import backtest_api as api
        p = {"symbol": "GOOD", "qty": 10, "entry_price": 100.0, "instrument_key": "NSE_EQ|GOOD", "strategy": S.NAME,
             "state": json.dumps({"entry_date": "2026-10-26"})}
        api._value_results_gap(p, {"NSE_EQ|GOOD": {"ltp": 110.0}})
        self.assertEqual(p["margin_used"], 1000.0)
        self.assertAlmostEqual(p["fees_if_sold"], S.delivery_costs(100.0, 110.0, 10)["total"], places=2)
        self.assertIn("40 trading days", p["size_text"])


class TestBacktestPage(unittest.TestCase):
    def test_listed_with_its_daily_data_and_accepted(self):
        from engine import backtest_api as api
        row = {(r["market"], r["name"]): r for r in api.catalog()}[("equity", "earnings_drift")]
        self.assertIn("daily", row["data"]["text"])
        self.assertIn("5-minute", {(r["market"], r["name"]): r for r in api.catalog()}[("equity", "results_gap")]["data"]["text"])
        job = api.validate({"market": "equity", "strategy": "earnings_drift", "start": "2023-01-02", "end": "2026-10-19", "capital": 100000, "slippage_bp": 5})
        self.assertEqual((job["strategy"], job["params"]["capital"], job["params"]["slippage_bp"]), ("earnings_drift", 100000.0, 5.0))

    def test_web_backtest_replay_shape(self):
        ind = make_ind()
        days = ind["C"].index
        ind = {**ind, "H": ind["C"] * 1.01, "L": ind["C"] * 0.99}
        with mock.patch.object(S, "cached_indicators", return_value=ind), mock.patch.object(S, "results_events", return_value=EVENTS), \
                mock.patch.object(S, "nifty_daily", return_value=pd.Series(100.0, index=days)):
            r = S.web_backtest(100_000, "2026-10-01", "2026-10-26")
        self.assertEqual(r["days"][-1], "2026-10-23")
        self.assertEqual(len(r["equity"]), len(r["days"]))
        self.assertEqual(r["benchmark"][0], 100_000)
        self.assertIn("40 trading days", r["metrics"]["note"])
        self.assertEqual(r["unit"], "stock")

    def test_both_account_strategies_count_as_live(self):
        from engine import backtest_api as api
        self.assertTrue({"results_gap", "earnings_drift"} <= api.LIVE_STRATEGIES)

    def test_nightly_downloads_its_data(self):
        from engine import nightly
        names = [n for n, _, _ in nightly.STEPS]
        self.assertIn("all nse daily", names)
        self.assertIn("results dates", names)


if __name__ == "__main__":
    unittest.main()
