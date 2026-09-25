"""The dashboard API must compute the right statistics, be strictly read-only, and not leak files or data."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import dashboard_api as api
from engine.database import TradingDB

IST = timezone(timedelta(hours=5, minutes=30))


def _trade(i, symbol, net, exit_dt, capital_after, reason="be_stop", gross=None, friction=100.0, hold=10.0):
    return {"trade_id": i, "symbol": symbol, "net_pnl": net, "gross_pnl": net + friction if gross is None else gross, "total_friction": friction,
            "exit_dt": exit_dt, "capital_after": capital_after, "exit_reason": reason, "hold_minutes": hold}


TRADES = [_trade(1, "CRUDEOILM", 800, "2026-09-24T11:00:00+05:30", 100800), _trade(2, "CRUDEOILM", 800, "2026-09-24T11:30:00+05:30", 101600),
          _trade(3, "CRUDEOILM", -1500, "2026-09-24T12:00:00+05:30", 100100, "initial_stop"), _trade(4, "SILVERMIC", 1200, "2026-09-25T10:00:00+05:30", 101300),
          _trade(5, "SILVERMIC", -400, "2026-09-25T11:00:00+05:30", 100900, "timeout_exit")]


class TestStatistics(unittest.TestCase):
    def test_headline_numbers(self):
        s = api.trade_stats(TRADES)
        self.assertEqual((s["trades"], s["wins"], s["losses"]), (5, 3, 2))
        self.assertEqual(s["win_pct"], 60.0)
        self.assertEqual(s["net"], 900.0)
        self.assertEqual(s["avg_win"], 933.33)
        self.assertEqual(s["avg_loss"], 950.0)
        self.assertEqual(s["profit_factor"], round(2800 / 1900, 2))
        self.assertEqual(s["need_pct"], round(100 * 950 / (933.33 + 950), 1))            # win rate needed to break even at this payoff
        self.assertEqual((s["best"], s["worst"]), (1200.0, -1500.0))
        self.assertEqual(s["expectancy"], 180.0)

    def test_empty_history_does_not_divide_by_zero(self):
        s = api.trade_stats([])
        self.assertEqual((s["trades"], s["win_pct"], s["profit_factor"], s["payoff"]), (0, 0.0, None, None))

    def test_equity_curve_and_max_drawdown(self):
        pts = api.equity_curve(TRADES, 100000.0)
        self.assertEqual(pts[0]["equity"], 100000.0)
        self.assertEqual(len(pts), 6)
        self.assertEqual(api.max_drawdown_pct(pts), round((101600 - 100100) / 101600 * 100, 2))        # peak 101,600 -> trough 100,100

    def test_daily_pnl_groups_by_exit_date(self):
        d = api.daily_pnl(TRADES)
        self.assertEqual([(r["date"], r["net"], r["trades"], r["wins"]) for r in d], [("2026-09-24", 100.0, 3, 2), ("2026-09-25", 800.0, 2, 1)])

    def test_per_symbol_and_exit_reason(self):
        syms = {r["symbol"]: r for r in api.by_symbol(TRADES)}
        self.assertEqual(syms["CRUDEOILM"]["net"], 100.0)
        self.assertEqual(syms["SILVERMIC"]["series"], [1200.0, 800.0])                    # cumulative P&L, for the sparkline
        self.assertEqual(api.by_symbol(TRADES)[0]["symbol"], "SILVERMIC")                 # sorted by net
        reasons = {r["reason"]: r for r in api.by_exit_reason(TRADES)}
        self.assertEqual((reasons["be_stop"]["trades"], reasons["initial_stop"]["net"]), (3, -1500.0))


class TestOverviewFromARealDatabase(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "t.db"
        self.db = TradingDB(self.path)
        self.db.init_account("DRYRUN_ACCOUNT", capital=100000.0, leverage=5.0, risk_pct=4.0)
        con = self.db._get_conn()
        for t in TRADES:
            con.execute("INSERT INTO trades (position_id, account_id, symbol, direction, qty, entry_price, exit_price, entry_dt, exit_dt, hold_minutes, exit_reason,"
                        " gross_pnl, brokerage, stt, stamp_duty, exchange_txn_fee, sebi_charges, gst, slippage, total_friction, net_pnl, capital_after, created_at)"
                        " VALUES (?, 'DRYRUN_ACCOUNT', ?, 'long', 1, 1, 1, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, 0, ?, ?, ?, ?)",
                        (f"P{t['trade_id']}", t["symbol"], t["exit_dt"], t["exit_dt"], t["hold_minutes"], t["exit_reason"], t["gross_pnl"], t["total_friction"],
                         t["net_pnl"], t["capital_after"], t["exit_dt"]))
        con.execute("UPDATE accounts SET current_capital = 100900")
        con.commit()
        con.close()

    def test_overview(self):
        con = api.connect(self.path)
        try:
            o = api.overview(con, now=datetime(2026, 9, 25, 15, 0, tzinfo=IST))
        finally:
            con.close()
        self.assertEqual((o["account"]["initial"], o["account"]["current"], o["account"]["net"]), (100000.0, 100900.0, 900.0))
        self.assertEqual(o["account"]["return_pct"], 0.9)
        self.assertEqual((o["today"]["trades"], o["today"]["net"]), (2, 800.0))           # only Sep 25's trades
        self.assertEqual(o["kpis"]["max_drawdown_pct"], round(1500 / 101600 * 100, 2))
        self.assertEqual(o["open_positions"], [])

    def test_the_connection_is_read_only(self):
        con = api.connect(self.path)
        try:
            with self.assertRaises(Exception):
                con.execute("DELETE FROM trades")
        finally:
            con.close()
        self.assertEqual(len(api.route("/api/trades", {"limit": ["50"]}, self.path)[1]), 5)      # nothing was removed

    def test_routes(self):
        status, body = api.route("/api/symbols", {}, self.path)
        self.assertEqual(status, 200)
        self.assertEqual({r["symbol"] for r in body}, {"CRUDEOILM", "SILVERMIC"})
        self.assertEqual(api.route("/api/nope", {}, self.path)[0], 404)
        self.assertEqual(api.route("/api/overview", {}, Path("/no/such.db"))[0], 503)
        self.assertEqual(len(api.route("/api/trades", {"limit": ["2"]}, self.path)[1]), 2)
        self.assertEqual(api.route("/api/trades", {"limit": ["999999"]}, self.path)[0], 200)      # clamped, not an error


class TestServerSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "static").mkdir()
        (self.tmp / "static" / "index.html").write_text("<html>spa</html>")
        (self.tmp / "static" / "main.js").write_text("console.log(1)")
        (self.tmp / "secret.txt").write_text("TOP SECRET")                       # outside the static root
        self.db = self.tmp / "t.db"
        TradingDB(self.db).init_account("DRYRUN_ACCOUNT", capital=1000.0, leverage=5.0, risk_pct=4.0)
        self.server = api.make_server("127.0.0.1", 0, token="s3cret", db_path=self.db, static_dir=self.tmp / "static")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _get(self, path, headers=None, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_api_requires_the_token_but_static_files_do_not(self):
        self.assertEqual(self._get("/api/overview")[0], 401)
        self.assertEqual(self._get("/api/overview", {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self._get("/api/overview", {"Authorization": "Bearer s3cret"})[0], 200)
        self.assertEqual(self._get("/api/overview?token=s3cret")[0], 200)
        self.assertEqual(self._get("/main.js")[0], 200)

    def test_spa_routes_fall_back_to_index_html(self):
        status, body = self._get("/performance")
        self.assertEqual((status, body), (200, "<html>spa</html>"))

    def test_directory_traversal_is_blocked(self):
        for p in ("/../secret.txt", "/..%2Fsecret.txt", "/%2e%2e/secret.txt", "/static/../../secret.txt"):
            status, body = self._get(p)
            self.assertNotIn("TOP SECRET", body, p)

    def test_nothing_can_be_changed_over_http(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self._get("/api/overview", {"Authorization": "Bearer s3cret"}, method=m)[0], 405, m)

    def test_no_token_configured_means_open_api(self):
        self.assertTrue(api.authorized({}, {}, None))
        self.assertFalse(api.authorized({}, {}, "x"))


if __name__ == "__main__":
    unittest.main()
