"""equity/breakout_pair + engine/equity_breakout.py (own paper account EQUITY_BREAKOUT, re-added 2026-11-17).
Synthetic indicators, fake clock and broker, temp DB -- no network, no archive."""
import contextlib
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd

from engine import equity_breakout as E
from engine.database import TradingDB
from markets.equity.strategies.tf_daily.breakout_pair import strategy as S

IST = E.IST
DAYS = pd.bdate_range(end="2026-11-16", periods=30)          # last completed day Mon 16 Nov -> the session is Tue 17 Nov
SYMS = ["DFIVE", "NRSEVEN", "HELD"]


def make_ind(breadth=80.0):
    f = lambda v: pd.DataFrame(v, index=DAYS, columns=SYMS, dtype=float)
    H = f(100.0); H.loc[DAYS[-1], "NRSEVEN"] = 105.0               # NRSEVEN's buy-stop trigger = its last high
    mask = lambda s: f(0.0).astype(bool).assign(**{s: [False] * (len(DAYS) - 1) + [True]})
    return {"C": f(100.0), "H": H, "atr": f(2.0), "breadth": pd.Series(breadth, index=DAYS), "mom126": f(0.1),
            "d55": mask("DFIVE"), "nr7": mask("NRSEVEN")}


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def sleep(self, s): self.t += timedelta(seconds=max(float(s), 1.0))


class Broker:
    """Opens at 100. NRSEVEN trades up to 106 from 10:00 (crosses 105); HELD trades down to 93 from 11:00 (crosses its 93.5 stop)."""
    def __init__(self, clock): self.clock, self.calls = clock, 0
    def get_quote_batch(self, keys):
        self.calls += 1
        t = self.clock().strftime("%H:%M")
        out = {}
        for k in keys:
            s = k.split("|")[1]
            hi = 106.0 if s == "NRSEVEN" and t >= "10:00" else 101.0
            lo = 93.0 if s == "HELD" and t >= "11:00" else 102.0 if s == "NRSEVEN" else 99.0   # NRSEVEN stays above its 100 stop
            out[k] = {"ltp": 100.0, "today_open": 100.0, "today_high": hi, "today_low": lo}
        return out


class TestStrategy(unittest.TestCase):
    def test_paper_costs_match_backtest_fees(self):
        c = S.costs(100.0, 110.0, 100)
        fee, slip = S._fee(10_000, 11_000)
        self.assertAlmostEqual(c["total"], fee + slip, places=2)
        self.assertAlmostEqual(c["gross"], 1000.0)
        self.assertAlmostEqual(c["net"], 1000.0 - c["total"], places=2)

    def test_size_caps(self):
        self.assertEqual(S.size(50_000, 50_000, 3, 100.0, 95.0), 166)   # 1/3 of the sleeve caps it (risk alone would allow 200)
        self.assertEqual(S.size(50_000, 1_000, 3, 100.0, 95.0), 10)     # never more than the sleeve's cash
        self.assertEqual(S.size(50_000, 50_000, 3, 100.0, 100.0), 0)    # no room to a stop -> no trade

    def test_candidates_strongest_first_and_health(self):
        c = S.candidates(make_ind(), DAYS[-1])
        self.assertEqual((c["d55"], c["nr7"], c["healthy"]), (["DFIVE"], [("NRSEVEN", 105.0)], True))
        self.assertFalse(S.candidates(make_ind(30.0), DAYS[-1])["healthy"])


class TestSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = TradingDB(Path(self.tmp.name) / "t.db")
        patcher = mock.patch.object(E, "STATE", Path(self.tmp.name) / "bo_state.json")      # never the real var/db state file
        patcher.start(); self.addCleanup(patcher.stop)
        # another account's rows must come out byte-for-byte the same
        self.db.init_account("RESULTS_GAP", name="rg", capital=100_000, leverage=1.0, risk_pct=1.0)
        self.db.open_position("POS_RG_X", "OTHER", "long", 5, 50.0, 0.0, 0.0, 0.0, account_id="RESULTS_GAP", strategy="earnings_drift")
        self.db.init_account(E.ACCOUNT, name="b", capital=100_000, leverage=1.0, risk_pct=2.0)
        st = {"sleeve": "nr7", "stop": 90.0, "hi": 100.0, "entry_day": str(DAYS[-1].date()), "n": 0, "done_through": None}
        self.db.open_position("POS_EQBO_OLD_HELD", "HELD", "long", 10, 100.0, 90.0, 0.0, 0.0, account_id=E.ACCOUNT,
                              instrument_key="NSE_EQ|HELD", strategy="nr7_breakout", state=json.dumps(st))

    def tearDown(self):
        self.tmp.cleanup()

    def _other(self):
        with self.db._get_conn() as con:
            return [tuple(r) for t in ("accounts", "positions", "trades") for r in con.execute(f"SELECT * FROM {t} WHERE account_id != ?", (E.ACCOUNT,))]

    def run_day(self, ind, broker=Broker):
        clock = Clock(datetime(2026, 11, 17, 9, 8, tzinfo=IST)); locks = []
        @contextlib.contextmanager
        def lock():
            locks.append(1); yield
        sent = []
        with mock.patch.object(E, "expected_last_day", return_value=DAYS[-1].date()):
            E.session(self.db, broker(clock), send=sent.append, log=lambda *a: None, sleep=clock.sleep, clock=clock,
                      ind=ind, keys_all={s: f"NSE_EQ|{s}" for s in SYMS}, lock=lock)
        return sent, locks

    def trades(self):
        with self.db._get_conn() as con:
            return [tuple(r) for r in con.execute("SELECT symbol, exit_price, exit_reason FROM trades WHERE account_id = ?", (E.ACCOUNT,))]

    def test_a_full_day(self):
        before = self._other()
        sent, locks = self.run_day(make_ind())
        held = {p["symbol"]: p for p in self.db.get_open_positions(E.ACCOUNT)}
        self.assertEqual(held["DFIVE"]["qty"], 166)                       # d55 at the 100 open, 1/3 of its Rs50k sleeve
        self.assertEqual(held["DFIVE"]["entry_price"], 100.0)
        self.assertEqual(held["NRSEVEN"]["entry_price"], 105.0)          # buy-stop fills at max(open, trigger)
        self.assertEqual(self.trades(), [("HELD", 93.5, "stop")])        # trailing stop raised to 100 - 3.25*2 = 93.5, sold there
        self.assertEqual(self._other(), before)                           # RESULTS_GAP untouched
        self.assertGreater(len(locks), 10)                                # locked per step, not once for the whole session
        self.assertIn("Breakout · end of day", sent[-1])
        self.assertTrue(any(m.startswith("🟢 <b>BOUGHT · Breakout</b>") and "<b>DFIVE</b> · 166 shares at 100.00" in m for m in sent))
        self.assertTrue(any(m.startswith("❌ <b>SOLD · Breakout</b>") and "hit the stop-loss" in m for m in sent))
        st = json.loads(E.STATE.read_text())                              # the account page's "Today" box
        self.assertEqual((st["day"], st["healthy"], st["d55"], st["nr7"]), ("2026-11-17", True, ["DFIVE"], [["NRSEVEN", 105.0]]))
        booked = self.db.get_account(E.ACCOUNT)["current_capital"]
        self.assertAlmostEqual(booked, 100_000 + S.costs(100.0, 93.5, 10)["net"], places=2)

    def test_buy_day_low_below_the_new_stop_sells_it_like_the_backtest(self):
        class Dip(Broker):                                   # NRSEVEN's day low 99 is under its 100 stop: the backtest sells at the stop too
            def get_quote_batch(self, keys):
                out = super().get_quote_batch(keys)
                for k, v in out.items():
                    if k.endswith("NRSEVEN"):
                        v["today_low"] = 99.0
                return out
        self.run_day(make_ind(), broker=Dip)
        self.assertIn(("NRSEVEN", 100.0, "stop"), self.trades())

    def test_unhealthy_market_buys_nothing_but_still_sells_stops(self):
        self.run_day(make_ind(breadth=30.0))
        self.assertEqual([p["symbol"] for p in self.db.get_open_positions(E.ACCOUNT)], [])
        self.assertEqual(self.trades(), [("HELD", 93.5, "stop")])

    def test_stale_archive_buys_nothing(self):
        with mock.patch.object(E, "expected_last_day", return_value=date(2026, 11, 17)):
            clock = Clock(datetime(2026, 11, 17, 9, 8, tzinfo=IST))
            E.session(self.db, Broker(clock), send=lambda m: None, log=lambda *a: None, sleep=clock.sleep, clock=clock,
                      ind=make_ind(), keys_all={s: f"NSE_EQ|{s}" for s in SYMS}, lock=contextlib.nullcontext)
        self.assertEqual({p["symbol"] for p in self.db.get_open_positions(E.ACCOUNT)}, set())


if __name__ == "__main__":
    unittest.main()


class TestPlumbing(unittest.TestCase):
    """The account reaches the website, the reset/restore lock list and the nightly price download."""

    def test_registered_everywhere(self):
        from engine import account_reset, backtest_api as api
        self.assertIn(E.ACCOUNT, dict(api.PAPER_ACCOUNTS))
        self.assertEqual(api.ACCOUNT_STRATEGIES[E.ACCOUNT], "breakout_pair")
        self.assertIn(E.ACCOUNT, account_reset.ACCOUNTS)                 # backup restore takes this account's lock too

    def test_open_position_valued_with_delivery_fees(self):
        from engine import backtest_api as api
        p = {"symbol": "X", "qty": 10, "entry_price": 100.0, "current_stop": 95.0, "instrument_key": "NSE_EQ|X",
             "state": json.dumps({"sleeve": "d55", "entry_day": "2026-11-18"})}
        api._value_breakout(p, {"NSE_EQ|X": {"ltp": 110.0}})
        c = S.costs(100.0, 110.0, 10)
        self.assertEqual((p["profit_now"], p["net_if_sold"], p["margin_used"], p["risk_to_stop"]), (100.0, c["net"], 1000.0, 50.0))
        self.assertIn("55-day high", p["size_text"])
        q = {"symbol": "X", "qty": 10, "entry_price": 100.0, "current_stop": 95.0, "instrument_key": "NSE_EQ|X", "state": None}
        api._value_breakout(q, {})                                          # no live price: unpriced, never an invented one
        self.assertIsNone(q["profit_now"])

    def test_nightly_download_keeps_breakout_holdings_fresh(self):
        from markets.equity import all_daily
        db = mock.Mock()
        db.get_open_positions.side_effect = lambda acc: ([{"symbol": "ED1", "strategy": "earnings_drift"}] if acc == "RESULTS_GAP"
                                                         else [{"symbol": "BO1", "strategy": "d55_breakout"}])
        with mock.patch("engine.database.TradingDB", return_value=db), mock.patch.object(all_daily, "run", return_value=0) as run:
            all_daily.main([])
        self.assertEqual(run.call_args.kwargs["extra"], ["ED1", "BO1"])
