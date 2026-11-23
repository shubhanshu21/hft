"""2026-11-23 (user: "why 2 seperate accounts for equity" -> "ok"): an account whose strategies are all set to close in backend/.env
(EQUITY_BREAKOUT: STRATEGY_BREAKOUT=close + STRATEGY_MOMENTUM=close) is closed -- off the left list and the Desk total, out of the
nightly paper-vs-backtest check and the engine monitor; its page and trades stay (nothing deleted) and Ctrl+K still finds it."""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from core import switches
from engine import backtest_api as A
from engine import engine_monitor as M
from engine import paper_vs_backtest as P

CLOSED = {"STRATEGY_BREAKOUT": "close", "STRATEGY_MOMENTUM": "close"}
MON_10 = datetime(2026, 10, 26, 10, 0, tzinfo=M.IST)


class TestClosed(unittest.TestCase):
    def test_closed_only_when_every_strategy_of_the_account_is_close(self):
        with mock.patch.dict(os.environ, CLOSED):
            self.assertTrue(switches.account_closed("EQUITY_BREAKOUT"))
            self.assertFalse(switches.account_closed("RESULTS_GAP"))
            self.assertFalse(switches.account_closed("CRYPTO_DIP_REVERSAL"))
        with mock.patch.dict(os.environ, {"STRATEGY_BREAKOUT": "close", "STRATEGY_MOMENTUM": "off"}):
            self.assertFalse(switches.account_closed("EQUITY_BREAKOUT"))      # off still sells by its own rules: still trading
        with mock.patch.dict(os.environ, {"STRATEGY_BREAKOUT": "on", "STRATEGY_MOMENTUM": "on"}):
            self.assertFalse(switches.account_closed("EQUITY_BREAKOUT"))

    def test_desk_and_rail_leave_it_out_but_search_still_has_it(self):
        def summary(aid):
            return {"account": {"initial_capital": 100000.0, "current_capital": 100000.0}, "positions": [], "trades": [],
                    "engine": {"ok": True, "text": "Running"}}
        with mock.patch.dict(os.environ, CLOSED):
            o = A.overview(summary=summary)
        self.assertEqual([a["id"] for a in o["accounts"]], ["CRYPTO_DIP_REVERSAL", "RESULTS_GAP"])
        self.assertEqual(o["totals"]["start"], 200000.0)
        self.assertEqual(o["closed"], [{"id": "EQUITY_BREAKOUT", "label": "Equity breakout pair", "strategy": "breakout_pair, momentum_12_1"}])

    def test_one_equity_account_name(self):
        self.assertEqual(dict(A.PAPER_ACCOUNTS)["RESULTS_GAP"], "Equity: results gap, drift, momentum")
        self.assertEqual(A.ACCOUNT_STRATEGIES["RESULTS_GAP"], "results_gap, earnings_drift, momentum_12_1")

    def test_nightly_check_skips_it(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            sqlite3.connect(db).close()
            seen = []
            with mock.patch.dict(os.environ, CLOSED), mock.patch.object(P, "check_account", side_effect=lambda con, a, t, r: seen.append(a) or {}):
                P.run_all(db, date(2026, 11, 24))
        self.assertEqual(seen, ["CRYPTO_DIP_REVERSAL", "RESULTS_GAP"])

    def test_monitor_does_not_alert_on_its_stopped_timer(self):
        sh = lambda *a: "active" if a[0] == "is-active" and "breakout" not in a[1] else "inactive" if a[0] == "is-active" else "Result=success\nExecMainStartTimestamp=@0"
        with tempfile.TemporaryDirectory() as d, mock.patch.object(M, "_holidays", return_value=set()):
            crypto = Path(d) / "c.json"; crypto.write_text('{"updated_at": "%s"}' % MON_10.isoformat())
            with mock.patch.dict(os.environ, CLOSED):
                found = M.problems(MON_10, sh=sh, crypto_state=crypto, token_ok=lambda: True, journal=lambda cmd: "")
                st = M.engine_status("EQUITY_BREAKOUT", MON_10, sh=sh)
            self.assertFalse({k for k in found if k.startswith("bo_")})
            self.assertTrue(st["ok"]); self.assertIn("Closed", st["text"])
            with mock.patch.dict(os.environ, {"STRATEGY_BREAKOUT": "on", "STRATEGY_MOMENTUM": "on"}):   # re-opened: the stopped timer alerts again
                self.assertIn("bo_timer", M.problems(MON_10, sh=sh, crypto_state=crypto, token_ok=lambda: True, journal=lambda cmd: ""))


if __name__ == "__main__":
    unittest.main()
