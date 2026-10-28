"""engine/backtest_api.py -- GET /api/overview (the website's Today page, 2026-10-29) and the `live` flag on saved runs.
paper_summary is replaced by a fake per account, so no database or live prices are touched."""
import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from engine import backtest_api as A

NOW = datetime(2026, 10, 29, 14, 0, tzinfo=A.IST)


def fake_summary(aid):
    base = {"available": True, "account_id": aid, "engine": {"ok": True, "text": "Running"},
            "accounts": [{"id": a, "label": lbl, "strategies": A.ACCOUNT_STRATEGIES[a], "start": 100000.0, "booked": 100000.0, "open": 0,
                          "market_open": True, "market_text": "open"} for a, lbl in A.PAPER_ACCOUNTS]}
    if aid == "RESULTS_GAP":
        return base | {"account": {"initial_capital": 100000.0, "current_capital": 115728.0},
                       "positions": [{"symbol": "ABC", "profit_now": 2000.0, "move_pct": 1.5}],
                       "trades": [{"symbol": "PPLPHARMA", "exit_dt": "2026-10-29T15:20:05+05:30", "net_pnl": 1500.0, "strategy": "results_gap"},
                                  {"symbol": "OLD", "exit_dt": "2026-10-28T15:20:05+05:30", "net_pnl": 15728.0, "strategy": "results_gap"}]}
    if aid == "MCX_TREND_CARRY":
        return base | {"account": {"initial_capital": 250000.0, "current_capital": 250000.0},
                       "positions": [{"symbol": "CRUDEOILM", "profit_now": None, "move_pct": None}],       # no live price: not counted, not a crash
                       "trades": []}
    return base | {"account": {"initial_capital": 100000.0, "current_capital": 100000.0}, "positions": [], "trades": [],
                   "engine": {"ok": False, "text": "Crypto engine has not run for 40 min"}}


class TestOverview(unittest.TestCase):
    def setUp(self):
        self.o = A.overview(now=NOW, summary=fake_summary)

    def test_every_paper_account_in_order(self):
        self.assertEqual([a["id"] for a in self.o["accounts"]], [a for a, _ in A.PAPER_ACCOUNTS])

    def test_value_is_booked_plus_open_profit(self):
        rg = next(a for a in self.o["accounts"] if a["id"] == "RESULTS_GAP")
        self.assertEqual((rg["booked"], rg["open_pnl"], rg["value"]), (115728.0, 2000.0, 117728.0))

    def test_today_counts_only_trades_sold_today_plus_open_profit(self):
        rg = next(a for a in self.o["accounts"] if a["id"] == "RESULTS_GAP")
        self.assertEqual(rg["today_pnl"], 3500.0)                   # 1500 sold today + 2000 open; yesterday's 15,728 is not today

    def test_unpriced_position_counts_as_zero(self):
        mcx = next(a for a in self.o["accounts"] if a["id"] == "MCX_TREND_CARRY")
        self.assertEqual((mcx["open_pnl"], mcx["value"], mcx["open_count"], mcx["unpriced"]), (0.0, 250000.0, 1, 1))

    def test_totals(self):
        self.assertEqual(self.o["totals"], {"start": 450000.0, "value": 467728.0, "today_pnl": 3500.0, "open_count": 2})

    def test_engine_problem_is_passed_through(self):
        crypto = next(a for a in self.o["accounts"] if a["id"] == "CRYPTO_DIP_REVERSAL")
        self.assertFalse(crypto["engine"]["ok"])

    def test_recent_trades_newest_first_with_account(self):
        self.assertEqual([(t["symbol"], t["account"]) for t in self.o["recent"]], [("PPLPHARMA", "RESULTS_GAP"), ("OLD", "RESULTS_GAP")])

    def test_one_broken_account_does_not_break_the_page(self):
        def flaky(aid):
            if aid == "MCX_TREND_CARRY":
                raise OSError("db locked")
            return fake_summary(aid)
        o = A.overview(now=NOW, summary=flaky)
        mcx = next(a for a in o["accounts"] if a["id"] == "MCX_TREND_CARRY")
        self.assertIn("unavailable", mcx["error"])
        self.assertEqual(o["totals"]["value"], 217728.0)            # the other two still add up


class TestLiveFlag(unittest.TestCase):
    def test_saved_runs_say_whether_their_strategy_is_paper_traded(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(A, "RUNS_DIR", Path(d)):
            for i, strat in enumerate(("results_gap", "pairs_cash")):
                (Path(d) / f"r{i}.json").write_text(json.dumps({"id": f"r{i}", "status": "done", "created": f"2026-10-2{i}", "market": "equity",
                                                                "strategy": strat, "params": {}}))
            flags = {r["strategy"]: r["live"] for r in A.summaries()}
        self.assertEqual(flags, {"results_gap": True, "pairs_cash": False})


class TestEndpoint(unittest.TestCase):
    def test_get_overview(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), A.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(A, "paper_summary", fake_summary), \
                 urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/overview") as r:
                body = json.loads(r.read())
        finally:
            srv.shutdown(); srv.server_close()
        self.assertEqual(len(body["accounts"]), 3)
        self.assertIn("totals", body)


if __name__ == "__main__":
    unittest.main()
