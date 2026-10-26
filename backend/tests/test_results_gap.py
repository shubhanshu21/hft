"""equity/results_gap strategy + engine/results_gap.py paper trader (2026-10-24). Fake broker, temp DB, temp calendar -- no network."""
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from engine import results_gap as E
from engine.database import TradingDB
from markets.equity.strategies.tf_intraday.results_gap import strategy as S

IST = E.IST
UNI = [f"S{i:02d}" for i in range(30)] + ["WIN", "FLAT", "LOCK", "CHEAP"]


def quotes(extra):
    q = {s: {"ltp": 101.0, "prev_close": 100.0, "today_open": 100.5, "today_high": 102.0, "today_low": 99.5} for s in UNI}   # market gap +0.5%
    q.update(extra)
    return q


BIG = {"WIN": {"ltp": 112.0, "prev_close": 100.0, "today_open": 110.0, "today_high": 113.0, "today_low": 109.0},     # +10% gap, still rising
       "FLAT": {"ltp": 108.0, "prev_close": 100.0, "today_open": 110.0, "today_high": 111.0, "today_low": 107.0},    # +10% gap, fell back
       "LOCK": {"ltp": 110.0, "prev_close": 100.0, "today_open": 110.0, "today_high": 110.0, "today_low": 110.0},    # frozen at the band
       "CHEAP": {"ltp": 15.0, "prev_close": 12.0, "today_open": 14.0, "today_high": 15.5, "today_low": 13.9}}        # under Rs20


class TestSignals(unittest.TestCase):
    def test_only_big_gap_that_keeps_rising_and_is_tradable(self):
        with mock.patch.object(S, "universe", return_value=UNI):
            buys, mkt = S.signals(quotes(BIG), ["WIN", "FLAT", "LOCK", "CHEAP", "S01"])
        self.assertAlmostEqual(mkt, 0.005)
        self.assertEqual([b["symbol"] for b in buys], ["WIN"])
        self.assertAlmostEqual(buys[0]["gap_vs_mkt"], 0.095)

    def test_gap_is_measured_against_the_market(self):
        q = {s: {"ltp": 105.0, "prev_close": 100.0, "today_open": 104.0, "today_high": 106, "today_low": 103} for s in UNI}   # market +4%
        q["WIN"] = {"ltp": 110.0, "prev_close": 100.0, "today_open": 109.0, "today_high": 111, "today_low": 108}              # +9% raw, +5% vs mkt
        self.assertEqual(S.signals(q, ["WIN"])[0], [])

    def test_too_few_quotes_means_no_trade(self):
        self.assertEqual(S.signals({"WIN": BIG["WIN"]}, ["WIN"]), ([], None))


class TestCalendar(unittest.TestCase):
    def test_candidates_since_previous_trading_day(self):
        cal = pd.DataFrame({"bm_symbol": ["WIN", "S01", "S02", "NOTINLIST"], "bm_date": ["23-Oct-2026", "25-Oct-2026", "22-Oct-2026", "24-Oct-2026"]})
        with mock.patch.object(S, "universe", return_value=UNI):
            got = S.candidates(date(2026, 10, 26), date(2026, 10, 23), cal)     # Monday; previous trading day Friday
        self.assertEqual(got, ["S01", "WIN"])

    def test_prev_trading_day_skips_weekend_and_holiday(self):
        self.assertEqual(E.prev_trading_day(date(2026, 10, 26), set()), date(2026, 10, 23))
        self.assertEqual(E.prev_trading_day(date(2026, 10, 27), {date(2026, 10, 26)}), date(2026, 10, 23))


class TestCosts(unittest.TestCase):
    def test_round_trip_costs(self):
        c = S.costs(100.0, 102.0, 1000)          # Rs1L in, Rs1.02L out
        self.assertEqual(c["brokerage"], 60.0)    # Rs30 an order, capped
        self.assertAlmostEqual(c["stt"], 25.5)    # 0.025% of the sale
        self.assertAlmostEqual(c["slippage"], 202.0)
        self.assertGreater(c["total"], 300)


class FakeBroker:
    def __init__(self, q, ltp=None):
        self.q, self.ltp = q, ltp or {}
    def get_quote_batch(self, keys):
        return {k: self.q.get(k.split("|")[1]) for k in keys}
    def get_ltp_batch(self, keys):
        return {k: self.ltp.get(k.split("|")[1]) for k in keys}


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = TradingDB(self.tmp / "t.db")
        cal = self.tmp / "cal.csv"
        pd.DataFrame({"bm_symbol": ["WIN", "FLAT"], "bm_date": ["23-Oct-2026", "23-Oct-2026"], "bm_purpose": ["Financial Results"] * 2}).to_csv(cal, index=False)
        for p in (mock.patch.object(E, "STATE", self.tmp / "state.json"), mock.patch.object(S, "CALENDAR", cal),
                  mock.patch.object(S, "universe", return_value=UNI), mock.patch.object(S, "refresh_calendar", return_value=2)):
            p.start(); self.addCleanup(p.stop)
        self.lookup = lambda s: f"NSE_EQ|{s}"
        cal.touch()

    def at(self, hh, mm, day=26):
        return datetime(2026, 10, day, hh, mm, 5, tzinfo=IST)

    def test_buy_at_0930_then_sell_at_1520(self):
        E.run(self.db, FakeBroker(quotes(BIG)), self.at(9, 30), set(), self.lookup, send=None)
        pos = self.db.get_open_positions(E.ACCOUNT)
        self.assertEqual([p["symbol"] for p in pos], ["WIN"])
        self.assertEqual(int(pos[0]["qty"]), int(100_000 * S.LEVERAGE // 112.0))   # 3x the account, one signal
        E.run(self.db, FakeBroker(quotes(BIG)), self.at(9, 31), set(), self.lookup, send=None)   # a second morning run buys nothing more
        self.assertEqual(len(self.db.get_open_positions(E.ACCOUNT)), 1)
        E.run(self.db, FakeBroker(quotes(BIG), ltp={"WIN": 115.0}), self.at(15, 20), set(), self.lookup, send=None)
        self.assertEqual(self.db.get_open_positions(E.ACCOUNT), [])
        t = self.db.get_trades(account_id=E.ACCOUNT)[0]
        qty = int(100_000 * S.LEVERAGE // 112.0)
        gross = (115.0 - 112.0) * qty
        self.assertAlmostEqual(t["net_pnl"], gross - S.costs(112.0, 115.0, qty)["total"], places=2)
        self.assertAlmostEqual(self.db.get_account(E.ACCOUNT)["current_capital"], 100_000 + t["net_pnl"], places=2)

    def test_no_price_keeps_it_open_and_next_morning_sells_it(self):
        E.run(self.db, FakeBroker(quotes(BIG)), self.at(9, 30), set(), self.lookup, send=None)
        E.run(self.db, FakeBroker(quotes(BIG), ltp={}), self.at(15, 20), set(), self.lookup, send=None)
        self.assertEqual(len(self.db.get_open_positions(E.ACCOUNT)), 1)
        E.run(self.db, FakeBroker(quotes({}), ltp={"WIN": 111.0}), self.at(9, 30, day=27), set(), self.lookup, send=None)
        self.assertEqual(self.db.get_open_positions(E.ACCOUNT), [])
        self.assertEqual(self.db.get_trades(account_id=E.ACCOUNT)[0]["exit_reason"], "missed_exit")

    def test_weekend_and_holiday_do_nothing(self):
        self.assertEqual(E.run(self.db, FakeBroker(quotes(BIG)), datetime(2026, 10, 24, 9, 30, tzinfo=IST), set(), self.lookup, send=None), ["market closed today"])
        self.assertEqual(E.run(self.db, FakeBroker(quotes(BIG)), self.at(9, 30), {date(2026, 10, 26)}, self.lookup, send=None), ["market closed today"])
        self.assertEqual(self.db.get_open_positions(E.ACCOUNT), [])

    def test_no_quotes_no_trades(self):
        out = E.run(self.db, FakeBroker({}), self.at(9, 30), set(), self.lookup, send=None)
        self.assertIn("No usable 09:30 quotes", out[0])

    def test_a_failed_quote_fetch_does_not_burn_the_day(self):
        # 2026-10-26 in the live account: the 09:30 run got no quotes, marked the day done, and took no trade even though prices
        # were back minutes later. The day must stay open for the 09:40 retry.
        out = E.run(self.db, FakeBroker({}), self.at(9, 30), set(), self.lookup, send=None)
        self.assertIn("retrying at the next slot", out[0])
        self.assertNotEqual(E._state().get("entered_on"), "2026-10-26")
        E.run(self.db, FakeBroker(quotes(BIG)), self.at(9, 40), set(), self.lookup, send=None)
        pos = self.db.get_open_positions(E.ACCOUNT)
        self.assertEqual([p["symbol"] for p in pos], ["WIN"])
        state = json.loads(pos[0]["state"])
        self.assertTrue(state["late"])
        self.assertEqual(state["entry_slot"], "09:40")

    def test_the_retried_entry_says_it_was_late(self):
        E.run(self.db, FakeBroker({}), self.at(9, 30), set(), self.lookup, send=None)
        out = E.run(self.db, FakeBroker(quotes(BIG)), self.at(9, 40), set(), self.lookup, send=None)
        self.assertTrue(any("DELAYED" in ln and "BOUGHT WIN" in ln for ln in out))

    def test_it_gives_the_day_up_once_the_retry_window_closes(self):
        E.run(self.db, FakeBroker({}), self.at(9, 30), set(), self.lookup, send=None)
        out = E.run(self.db, FakeBroker({}), self.at(9, 46), set(), self.lookup, send=None)
        self.assertIn("giving up on today", out[0])
        self.assertEqual(E._state()["entered_on"], "2026-10-26")
        E.run(self.db, FakeBroker(quotes(BIG)), self.at(9, 50), set(), self.lookup, send=None)   # and it really is done
        self.assertEqual(self.db.get_open_positions(E.ACCOUNT), [])

    def test_three_failed_attempts_is_enough(self):
        for hh, mm in ((9, 30), (9, 35), (9, 40)):
            E.run(self.db, FakeBroker({}), self.at(hh, mm), set(), self.lookup, send=None)
        self.assertEqual(E._state()["entered_on"], "2026-10-26")
        self.assertEqual(E._state()["entry_attempts"], E.MAX_ENTRY_ATTEMPTS)


class TestResetKnowsTheAccount(unittest.TestCase):
    def test_reset_registry(self):
        from engine.account_reset import ACCOUNTS
        self.assertEqual(ACCOUNTS["RESULTS_GAP"], "results_gap_state.json")


if __name__ == "__main__":
    unittest.main()


class TestWebBacktest(unittest.TestCase):
    """markets/.../results_gap.backtest on a tiny hand-made day summary (no cache, no files)."""
    def frame(self):
        rows = []
        for i, d in enumerate(["2026-01-05", "2026-01-06", "2026-01-07"]):
            for s in UNI[:25]:
                rows.append((s, d, 100.5, 101.0, 101.0, 101.5, 100.8, 1e8, 101.2, 101.0, 102.0, 100.0, 100.0))
        # WIN reports on 01-05: next day it opens +10% (market +0.5%), still up at 09:25, liquid -> buy 112, sell 118
        rows.append(("WIN", "2026-01-05", 100, 100, 100, 100.5, 99.5, 1e8, 100, 100, 101, 99, 100.0))
        rows.append(("WIN", "2026-01-06", 110.0, 112.0, 112.0, 112.5, 111.5, 1e8, 118.0, 118.0, 119, 109, 100.0))
        rows.append(("WIN", "2026-01-07", 118, 118, 118, 118.5, 117.5, 1e8, 118, 118, 119, 117, 118.0))
        # LOCK: same gap but frozen at 09:30 (high == low) -> skipped; THIN: too little traded -> skipped
        rows.append(("LOCK", "2026-01-06", 110.0, 112.0, 112.0, 112.0, 112.0, 1e8, 118.0, 118.0, 119, 109, 100.0))
        rows.append(("THIN", "2026-01-06", 110.0, 112.0, 112.0, 112.5, 111.5, 1e5, 118.0, 118.0, 119, 109, 100.0))
        return pd.DataFrame(rows, columns=["sym", "day", "o915", "c925", "o930", "h930", "l930", "v930", "c1520", "close", "hi", "lo", "prev_close"])

    def test_one_trade_with_real_costs_and_replay_shape(self):
        res = {("WIN", "2026-01-05"), ("LOCK", "2026-01-05"), ("THIN", "2026-01-05")}
        r = S.backtest(100_000, "2026-01-05", "2026-01-07", leverage=3.0, days_df=self.frame(), results=res)
        self.assertEqual([t["symbol"] for t in r["trades"]], ["WIN"])
        qty = int(300_000 // 112.0)
        net = (118.0 - 112.0) * qty - S.costs(112.0, 118.0, qty)["total"]
        self.assertAlmostEqual(r["equity"][-1], round(100_000 + net), delta=1)
        self.assertEqual(len(r["days"]), 3)
        self.assertEqual(len(r["events"]), 3)
        self.assertEqual([e["t"] for e in r["events"][1]], ["buy", "sell"])
        self.assertEqual(len(r["bars"]["WIN"]), 3)

    def test_no_results_no_trades(self):
        r = S.backtest(100_000, "2026-01-05", "2026-01-07", days_df=self.frame(), results=set())
        self.assertEqual(r["trades"], [])
        self.assertEqual(r["equity"][-1], 100_000)

    def test_dates_outside_the_data(self):
        with self.assertRaises(ValueError):
            S.backtest(100_000, "2030-01-01", "2030-02-01", days_df=self.frame(), results=set())


class TestWebsiteCatalog(unittest.TestCase):
    def test_listed_and_validated(self):
        from engine import backtest_api as B
        self.assertIn(("equity", "results_gap"), [(x["market"], x["name"]) for x in B.catalog()])
        p = B.validate({"market": "equity", "strategy": "results_gap", "capital": 100000, "exposure": 3, "start": "2026-01-01", "end": "2026-10-14"})["params"]
        self.assertEqual((p["leverage"], p["capital"]), (3.0, 100000.0))
        with self.assertRaises(ValueError):
            B.validate({"market": "equity", "strategy": "results_gap", "capital": 100000, "exposure": 8, "start": "2026-01-01", "end": "2026-10-14"})


class TestQuoteBatchPrevClose(unittest.TestCase):
    """services/broker/upstox_broker.UpstoxBroker.get_quote_batch: prev close = last price - net change (the v3 OHLC quote has no
    previous close for NSE equities -- the 2026-10-26 morning got no usable prices because of it)."""
    def broker(self, data):
        from services.broker.upstox_broker import UpstoxBroker
        b = UpstoxBroker.__new__(UpstoxBroker)
        resp = mock.Mock(); resp.data = data
        b._market_quote = mock.Mock(); b._market_quote.get_full_market_quote.return_value = resp
        return b

    def q(self, key, ltp, chg, o=100.0):
        return mock.Mock(instrument_token=key, last_price=ltp, net_change=chg, ohlc=mock.Mock(open=o, high=o + 2, low=o - 2))

    def test_prev_close_from_net_change(self):
        b = self.broker({"NSE_EQ:A": self.q("NSE_EQ|A", 975.0, -22.1, 990.0)})
        with mock.patch("services.broker.upstox_broker._circuit_is_open", return_value=False):
            out = b.get_quote_batch(["NSE_EQ|A", "NSE_EQ|B"])
        self.assertAlmostEqual(out["NSE_EQ|A"]["prev_close"], 997.1)
        self.assertEqual(out["NSE_EQ|A"]["today_open"], 990.0)
        self.assertIsNone(out["NSE_EQ|B"])                      # not returned by Upstox -> None, never a made-up price

    def test_missing_net_change_gives_no_prev_close(self):
        b = self.broker({"NSE_EQ:A": self.q("NSE_EQ|A", 975.0, None)})
        with mock.patch("services.broker.upstox_broker._circuit_is_open", return_value=False):
            self.assertIsNone(b.get_quote_batch(["NSE_EQ|A"])["NSE_EQ|A"]["prev_close"])

    def test_engine_uses_the_full_quote(self):
        self.assertIn("get_quote_batch", open(E.__file__).read())
