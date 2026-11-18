"""engine/paper_vs_backtest.py -- each account's backtest over its own days vs what paper really bought (2026-11-18).
Backtests are faked; temp DB and output file."""
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from core.backtester import BacktestError
from engine import nightly
from engine import paper_vs_backtest as P
from engine.database import TradingDB

TODAY = date(2026, 11, 18)


def result(days, equity, buys):
    """A website-shaped backtest result: buys = {day: [(sym, px)]}."""
    return {"days": days, "equity": equity, "events": [[{"t": "buy", "sym": s, "px": px} for s, px in buys.get(d, [])] for d in days]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name) / "t.db"
        self.db = TradingDB(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def account(self, aid, capital=100_000, created="2026-11-10T09:00:00+05:30"):
        self.db.init_account(aid, name=aid, capital=capital, leverage=1.0, risk_pct=1.0)
        with sqlite3.connect(self.path) as con:
            con.execute("UPDATE accounts SET created_at = ? WHERE account_id = ?", (created, aid))

    def buy(self, aid, sym, when, px, state=None):
        self.db.open_position(f"P_{sym}_{when}", sym, "long", 1, px, 0.0, 0.0, 0.0, account_id=aid, strategy="x", state=json.dumps(state or {}))
        with sqlite3.connect(self.path) as con:
            con.execute("UPDATE positions SET entry_time = ? WHERE position_id = ?", (f"{when}T09:30:00+05:30", f"P_{sym}_{when}"))

    def check(self, aid, run):
        with sqlite3.connect(self.path) as con:
            return P.check_account(con, aid, TODAY, run)


class TestMatch(Base):
    def test_matched_missed_and_extra(self):
        self.account("EQUITY_BREAKOUT")
        self.buy("EQUITY_BREAKOUT", "A", "2026-11-12", 101.0)
        self.buy("EQUITY_BREAKOUT", "X", "2026-11-13", 50.0)
        bt = result(["2026-11-12", "2026-11-13"], [100_000, 102_000], {"2026-11-12": [("A", 100.0)], "2026-11-13": [("B", 20.0)]})
        r = self.check("EQUITY_BREAKOUT", lambda body: bt)
        self.assertEqual([(m["sym"], m["gap_pct"]) for m in r["matched"]], [("A", 1.0)])
        self.assertEqual([x["sym"] for x in r["only_paper"]], ["X"])
        self.assertEqual([x["sym"] for x in r["only_backtest"]], ["B"])
        self.assertEqual(r["expected"], {"2026-11-12": 100_000, "2026-11-13": 102_000})
        self.assertEqual(r["start"], "2026-11-10")

    def test_mcx_contract_matches_its_family_and_crypto_allows_a_day(self):
        self.account("MCX_TREND_CARRY", 250_000)
        self.buy("MCX_TREND_CARRY", "CRUDEOILM26DECFUT", "2026-11-16", 8208.0, {"family": "CRUDEOILM"})
        r = self.check("MCX_TREND_CARRY", lambda body: result(["2026-11-16"], [250_000], {"2026-11-16": [("CRUDEOILM", 8208.0)]}))
        self.assertEqual(len(r["matched"]), 1)
        self.account("CRYPTO_DIP_REVERSAL")
        self.buy("CRYPTO_DIP_REVERSAL", "ETH", "2026-11-15", 2000.0)          # IST morning = the previous UTC day in the backtest
        r = self.check("CRYPTO_DIP_REVERSAL", lambda body: result(["2026-11-14"], [100_000], {"2026-11-14": [("ETH", 2001.0)]}))
        self.assertEqual(len(r["matched"]), 1)

    def test_two_strategies_in_one_account_add_their_profits(self):
        self.account("RESULTS_GAP")
        runs = {"results_gap": result(["2026-11-12", "2026-11-13"], [100_000, 103_000], {}),
                "earnings_drift": result(["2026-11-13"], [99_000], {})}
        r = self.check("RESULTS_GAP", lambda body: runs[body["strategy"]])
        self.assertEqual(r["expected"], {"2026-11-12": 100_000, "2026-11-13": 102_000})   # +3,000 - 1,000

    def test_new_account_and_live_settings(self):
        self.account("EQUITY_BREAKOUT", created="2026-11-17T19:34:00+05:30")
        def too_soon(body):
            raise BacktestError("No trading days in the chosen dates")
        self.assertIn("too few trading days", self.check("EQUITY_BREAKOUT", too_soon)["note"])
        b = P.bodies("CRYPTO_DIP_REVERSAL", 100_000)[0]
        from engine import backtest_api as api
        self.assertEqual(api.validate(b | {"start": "2026-11-01", "end": "2026-11-18"})["params"]["tax"], "business")
        for aid in ("MCX_TREND_CARRY", "RESULTS_GAP", "EQUITY_BREAKOUT"):           # every body passes the website's own validation
            for body in P.bodies(aid, 100_000):
                api.validate(body | {"start": "2026-11-01", "end": "2026-11-18"})

    def test_mcx_size_reaches_the_backtest(self):
        from engine import backtest_api as api
        with mock.patch.dict("os.environ", {"MCX_TREND_CARRY_EXPOSURE": "2.0"}):
            body = P.bodies("MCX_TREND_CARRY", 250_000)[0]
            self.assertEqual(api.validate(body | {"start": "2026-11-01", "end": "2026-11-18"})["params"]["leverage"], 2.0)

    def test_recent_differences_only(self):
        r = {"account": "X", "only_paper": [{"sym": "OLD", "day": "2026-10-30"}, {"sym": "NEW", "day": "2026-11-17"}],
             "only_backtest": [{"sym": "MISS", "day": "2026-11-16"}]}
        self.assertEqual(P.recent_differences(r, TODAY), ["X: paper bought NEW on 2026-11-17, the backtest did not",
                                                          "X: the backtest bought MISS on 2026-11-16, paper did not"])


class TestMain(Base):
    def test_tells_telegram_once_a_day(self):
        out = Path(self.tmp.name) / "pvb.json"
        res = {"as_of": "2026-11-18T06:40:00+05:30", "accounts": {"X": {"account": "X", "note": "n", "only_paper": [{"sym": "NEW", "day": "2026-11-17"}]}}}
        sent = []
        with mock.patch.object(P, "OUT", out), mock.patch.object(P, "run_all", return_value=res), \
             mock.patch("services.utils.telegram.send", side_effect=sent.append):
            P.main(["--telegram"]); P.main(["--telegram"])
        self.assertEqual(len(sent), 1)
        self.assertIn("NEW", sent[0])

    def test_in_the_nightly_job(self):
        self.assertIn("paper vs backtest", [n for n, _, _ in nightly.STEPS])


if __name__ == "__main__":
    unittest.main()


class TestApi(unittest.TestCase):
    def test_account_page_gets_the_trimmed_check(self):
        from engine import backtest_api as api
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "pvb.json"
            f.write_text(json.dumps({"as_of": "2026-11-18T06:40:00+05:30", "accounts": {
                "RESULTS_GAP": {"start": "2026-10-24", "expected": {"2026-11-17": 101000.0}, "matched": [{}] * 5,
                                "only_paper": [{"sym": "KANOHAR", "day": "2026-11-03"}], "only_backtest": [], "avg_fill_gap_pct": 0.0},
                "EQUITY_BREAKOUT": {"note": "too few trading days yet to compare"}}}))
            r = api.backtest_check("RESULTS_GAP", f)
            self.assertEqual((r["matched"], r["only_paper"][0]["sym"], r["expected"]["2026-11-17"]), (5, "KANOHAR", 101000.0))
            self.assertIn("too few", api.backtest_check("EQUITY_BREAKOUT", f)["note"])
            self.assertIsNone(api.backtest_check("MCX_TREND_CARRY", f))
            self.assertIsNone(api.backtest_check("MCX_TREND_CARRY", Path(d) / "missing.json"))
