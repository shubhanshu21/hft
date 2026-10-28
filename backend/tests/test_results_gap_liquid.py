"""results_gap's liquidity-based stock list (2026-10-29): instead of the fixed 414-stock list (hindsight: today's liquid names), any NSE
EQ stock whose median daily turnover over the 20 trading days BEFORE the day is >= Rs10 crore. Temp folders only."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from markets.equity.strategies.tf_intraday.results_gap import strategy as S

HEAD = "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER"


def bhav(rows):
    """rows: (symbol, series, close, turnover in lakh)."""
    return HEAD + "\n" + "\n".join(f"{s}, {ser}, 28-Oct-2026, {c}, {c}, {c}, {c}, {c}, {c}, {c}, 1000, {t}, 10, 500, 50.0" for s, ser, c, t in rows)


class TestParse(unittest.TestCase):
    def test_keeps_eq_series_and_turnover_in_rupees(self):
        df = S.parse_bhav(bhav([("BIG", "EQ", 500.0, 2000.0), ("TTRADE", "BE", 100.0, 5000.0)]))
        self.assertEqual(list(df.SYMBOL), ["BIG"])
        self.assertEqual(df.TURNOVER.iloc[0], 2000.0 * 1e5)          # lakh -> rupees
        self.assertEqual(df.CLOSE.iloc[0], 500.0)

    def test_garbage_gives_empty(self):
        self.assertTrue(S.parse_bhav("<html>blocked</html>").empty)


class TestLiquidList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, day, rows):
        S.parse_bhav(bhav(rows)).to_csv(self.dir / f"{day}.csv.gz", index=False)

    def fill(self, n, rows_for):
        days = [date(2026, 9, 1 + i).isoformat() for i in range(n)]
        for i, d in enumerate(days):
            self.write(d, rows_for(i))
        return days

    def test_median_turnover_over_the_20_days_before(self):
        # BIG: Rs20 cr every day; SPIKE: Rs50 cr on 3 days only (median 0 -> out); MID: Rs10 cr exactly (in); CHEAP: liquid but under Rs20
        self.fill(25, lambda i: [("BIG", "EQ", 500, 2000), ("MID", "EQ", 300, 1000), ("CHEAP", "EQ", 15, 3000)] + ([("SPIKE", "EQ", 80, 5000)] if i < 3 else []))
        self.assertEqual(S.liquid_list(date(2026, 9, 30), root=self.dir), ["BIG", "MID"])

    def test_only_days_strictly_before_asof(self):
        self.fill(21, lambda i: [("BIG", "EQ", 500, 2000)] + ([("LATE", "EQ", 100, 90000)] * (i == 20)))
        self.assertEqual(S.liquid_list(date(2026, 9, 21), root=self.dir), ["BIG"])        # 21 Sep's own file is not used

    def test_too_little_history_gives_nothing(self):
        self.fill(5, lambda i: [("BIG", "EQ", 500, 2000)])
        self.assertEqual(S.liquid_list(date(2026, 9, 30), root=self.dir), [])

    def test_universe_falls_back_to_the_tested_list(self):
        with mock.patch.object(S, "BHAV_DIR", self.dir), mock.patch.object(S, "market_panel", return_value=["AAA", "BBB"]):
            self.assertEqual(S.universe(date(2026, 9, 30)), ["AAA", "BBB"])
            self.fill(25, lambda i: [("BIG", "EQ", 500, 2000)])
            self.assertEqual(S.universe(date(2026, 9, 30)), ["BIG"])


class TestRefresh(unittest.TestCase):
    def test_downloads_missing_weekdays_only(self):
        calls = []

        class R:
            def __init__(self, code, text): self.status_code, self.text = code, text

        def get(url):
            calls.append(url)
            return R(200, bhav([("BIG", "EQ", 500, 2000)]))

        with tempfile.TemporaryDirectory() as d:
            n = S.refresh_bhavcopy(date(2026, 10, 29), get=get, days=7, root=Path(d))
            self.assertEqual(n, 5)                                           # the 7 days before Thu 29 Oct: 22, 23, 26, 27, 28 Oct
            self.assertTrue(all("sec_bhavdata_full_" in u for u in calls))
            again = S.refresh_bhavcopy(date(2026, 10, 29), get=get, days=7, root=Path(d))
            self.assertEqual(again, 0)                                       # already held: no second download


class TestMarketGapPanel(unittest.TestCase):
    def test_median_is_measured_on_the_panel_only(self):
        q = {f"P{i}": {"ltp": 101, "prev_close": 100, "today_open": 100.5, "today_high": 102, "today_low": 99} for i in range(25)}
        q.update({f"X{i}": {"ltp": 110, "prev_close": 100, "today_open": 105, "today_high": 111, "today_low": 104} for i in range(40)})
        q["WIN"] = {"ltp": 112, "prev_close": 100, "today_open": 110, "today_high": 113, "today_low": 109}
        _, mkt = S.signals(q, ["WIN"], panel={f"P{i}" for i in range(25)})
        self.assertAlmostEqual(mkt, 0.005)


if __name__ == "__main__":
    unittest.main()


class TestEngineTradesTheLiquidList(unittest.TestCase):
    def test_a_stock_outside_the_old_list_can_be_bought(self):
        import json
        import pandas as pd
        from datetime import datetime
        from engine import results_gap as E
        from engine.database import TradingDB
        from tests.test_results_gap import FakeBroker, quotes, UNI
        tmp = Path(tempfile.mkdtemp())
        cal = tmp / "cal.csv"
        pd.DataFrame({"bm_symbol": ["NEWCO"], "bm_date": ["23-Oct-2026"], "bm_purpose": ["Financial Results"]}).to_csv(cal, index=False)
        q = quotes({"NEWCO": {"ltp": 112.0, "prev_close": 100.0, "today_open": 110.0, "today_high": 113.0, "today_low": 109.0}})
        with mock.patch.object(E, "STATE", tmp / "state.json"), mock.patch.object(S, "CALENDAR", cal), \
             mock.patch.object(S, "refresh_calendar", return_value=1), \
             mock.patch.object(S, "universe", return_value=["NEWCO"] + list(UNI)), mock.patch.object(S, "market_panel", return_value=list(UNI)):
            db = TradingDB(tmp / "t.db")
            E.run(db, FakeBroker(q), datetime(2026, 10, 26, 9, 30, 5, tzinfo=E.IST), set(), lambda s: f"NSE_EQ|{s}", send=None)
            self.assertEqual([p["symbol"] for p in db.get_open_positions(E.ACCOUNT)], ["NEWCO"])
            self.assertAlmostEqual(json.loads(E.STATE.read_text())["last_check"]["market_gap"], 0.005)   # measured on the panel only


class TestRunup(unittest.TestCase):
    def test_20_day_rise_before_the_day(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(25):                                         # BIG closes 100, 101, ... 124 on 1..25 Sep
                S.parse_bhav(bhav([("BIG", "EQ", 100 + i, 2000)])).to_csv(root / f"{date(2026, 9, 1 + i).isoformat()}.csv.gz", index=False)
            # before 26 Sep: last close 124 (25 Sep), 20 trading files earlier 104 (5 Sep)
            self.assertAlmostEqual(S.runup20("BIG", date(2026, 9, 26), root=root), 124 / 104 - 1)
            self.assertIsNone(S.runup20("NOPE", date(2026, 9, 26), root=root))
            self.assertIsNone(S.runup20("BIG", date(2026, 9, 10), root=root))      # not 21 files yet


class TestEntryRecordsMarketContext(unittest.TestCase):
    """2026-10-29: buys on mornings when Nifty FELL 09:15->09:30 averaged +2.0%/trade vs -0.5% on rising mornings (fair 2022-26 test,
    t 2.4, one of ~15 filters tried -> not proven). Nothing is filtered; each live buy records the facts so it can be checked later."""

    def run_entry(self, nifty):
        import json
        import pandas as pd
        from datetime import datetime
        from engine import results_gap as E
        from engine.database import TradingDB
        from tests.test_results_gap import FakeBroker, quotes, UNI
        tmp = Path(tempfile.mkdtemp())
        cal = tmp / "cal.csv"
        pd.DataFrame({"bm_symbol": ["WIN"], "bm_date": ["23-Oct-2026"], "bm_purpose": ["Financial Results"]}).to_csv(cal, index=False)
        q = quotes({"WIN": {"ltp": 112.0, "prev_close": 100.0, "today_open": 110.0, "today_high": 113.0, "today_low": 109.0}})
        if nifty:
            q["Nifty 50"] = nifty
        with mock.patch.object(E, "STATE", tmp / "state.json"), mock.patch.object(S, "CALENDAR", cal), \
             mock.patch.object(S, "refresh_calendar", return_value=1), mock.patch.object(S, "universe", return_value=list(UNI)), \
             mock.patch.object(S, "market_panel", return_value=list(UNI)), mock.patch.object(S, "runup20", return_value=0.12):
            db = TradingDB(tmp / "t.db")
            out = E.run(db, FakeBroker(q), datetime(2026, 10, 26, 9, 30, 5, tzinfo=E.IST), set(), lambda s: f"NSE_EQ|{s}", send=None)
            return json.loads(db.get_open_positions(E.ACCOUNT)[0]["state"]), out

    def test_nifty_first_15_minutes_and_runup_are_saved(self):
        st, out = self.run_entry({"ltp": 22650.0, "prev_close": 22700.0, "today_open": 22700.0, "today_high": 22710.0, "today_low": 22640.0})
        self.assertAlmostEqual(st["mkt_first15"], round(22650 / 22700 - 1, 5))
        self.assertAlmostEqual(st["runup20"], 0.12)
        self.assertTrue(any("Nifty 09:15->now" in ln for ln in out))

    def test_missing_index_quote_still_buys(self):
        st, _ = self.run_entry(None)
        self.assertIsNone(st["mkt_first15"])
