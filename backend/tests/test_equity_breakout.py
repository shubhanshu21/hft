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
SYMS = ["DFIVE", "NRSEVEN", "HELD", "MOMA", "MOMB", "MOMC"]


def make_ind(breadth=80.0):
    f = lambda v: pd.DataFrame(v, index=DAYS, columns=SYMS, dtype=float)
    H = f(100.0); H.loc[DAYS[-1], "NRSEVEN"] = 105.0               # NRSEVEN's buy-stop trigger = its last high
    mask = lambda s: f(0.0).astype(bool).assign(**{s: [False] * (len(DAYS) - 1) + [True]})
    return {"C": f(100.0), "H": H, "atr": f(2.0), "breadth": pd.Series(breadth, index=DAYS), "mom126": f(0.1), "VAL": f(1e9),
            "d55": mask("DFIVE"), "nr7": mask("NRSEVEN")}



_ENV = None


def setUpModule():
    """The live backend/.env may switch strategies off (2026-11-20: breakout + momentum are); tests start from all-on and set their own."""
    global _ENV
    from core import switches
    _ENV = mock.patch.dict("os.environ", {f"STRATEGY_{n}": "on" for n in switches.NAMES}); _ENV.start()


def tearDownModule():
    _ENV.stop()

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
        for name, f in (("STATE", "bo_state.json"), ("MOM_STATE", "bo_mom.json")):     # never the real var/db state files
            patcher = mock.patch.object(E, name, Path(self.tmp.name) / f)
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
        self.assertEqual(held["DFIVE"]["qty"], 83)                        # d55 at the 100 open, 1/3 of its Rs25k sleeve (half the account
                                                                          # is momentum since 2026-11-19, so each breakout sleeve has 25%)
        self.assertEqual(held["DFIVE"]["entry_price"], 100.0)
        self.assertEqual(held["NRSEVEN"]["entry_price"], 105.0)          # buy-stop fills at max(open, trigger)
        self.assertEqual(self.trades(), [("HELD", 93.5, "stop")])        # trailing stop raised to 100 - 3.25*2 = 93.5, sold there
        self.assertEqual(self._other(), before)                           # RESULTS_GAP untouched
        self.assertGreater(len(locks), 10)                                # locked per step, not once for the whole session
        self.assertIn("Breakout · end of day", sent[-1])
        self.assertTrue(any(m.startswith("🟢 <b>BOUGHT · Breakout</b>") and "<b>DFIVE</b> · 83 shares at 100.00" in m for m in sent))
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


class TestMomentumSleeve(TestSession):
    """2026-11-19 (user: "inside Breakout, 50/50"): half the account holds the top 20 by 12-month momentum, refreshed at the open of the
    first session of each month. Scores are faked; M.N = 2 keeps the numbers small."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(E.M, "N", 2); p.start(); self.addCleanup(p.stop)

    def scores(self, **v):
        row = pd.DataFrame(float("nan"), index=DAYS, columns=SYMS)
        for k, x in v.items():
            row.loc[DAYS[-1], k] = x
        return mock.patch.object(E.M, "scores", return_value=row)

    def mom(self):
        return {p["symbol"]: p for p in self.db.get_open_positions(E.ACCOUNT) if json.loads(p["state"] or "{}").get("sleeve") == "mom"}

    def add_mom(self, sym, entry=100.0, qty=10, day="2026-09-01"):
        st = {"sleeve": "mom", "entry_day": day, "stop": 0.0}
        self.db.open_position(f"POS_EQBO_MOM_{sym}", sym, "long", qty, entry, 0.0, 0.0, 0.0, account_id=E.ACCOUNT,
                              instrument_key=f"NSE_EQ|{sym}", strategy=E.M.NAME, state=json.dumps(st))

    def test_first_session_of_the_month_buys_the_top_n_at_the_open(self):
        with self.scores(MOMA=0.9, MOMB=0.5, MOMC=0.1, HELD=0.95):        # HELD is a breakout holding: skipped, the next one taken
            sent, _ = self.run_day(make_ind())
        m = self.mom()
        self.assertEqual(sorted(m), ["MOMA", "MOMB"])
        self.assertEqual((m["MOMA"]["qty"], m["MOMA"]["entry_price"]), (250, 100.0))   # 1/2 of momentum's Rs50k half at the 100 open
        self.assertEqual(m["MOMA"]["strategy"], "momentum_12_1")
        bought = [x for x in sent if "BOUGHT · Breakout momentum" in x]
        self.assertEqual(len(bought), 1)                                   # one message for the whole refresh
        self.assertIn("<b>MOMA</b>", bought[0]); self.assertIn("<b>MOMB</b>", bought[0])
        self.assertEqual(json.loads(E.MOM_STATE.read_text())["month"], "2026-11")
        st = json.loads(E.STATE.read_text())["momentum"]
        self.assertEqual((st["due"], st["targets"]), (True, ["MOMA", "MOMB"]))

    def test_no_second_refresh_in_the_same_month(self):
        E.MOM_STATE.write_text(json.dumps({"month": "2026-11"}))
        with self.scores(MOMA=0.9, MOMB=0.5):
            self.run_day(make_ind())
        self.assertEqual(self.mom(), {})

    def test_a_holding_that_left_the_top_n_is_sold_at_the_open(self):
        E.MOM_STATE.write_text(json.dumps({"month": "2026-10"}))
        self.add_mom("MOMC", entry=90.0)
        with self.scores(MOMA=0.9, MOMB=0.5, MOMC=0.1):
            sent, _ = self.run_day(make_ind())
        self.assertEqual(sorted(self.mom()), ["MOMA", "MOMB"])
        self.assertIn(("MOMC", 100.0, "momentum_out"), self.trades())
        self.assertTrue(any("SOLD · Breakout momentum" in x and "left the top 20" in x for x in sent))

    def test_momentum_holdings_have_no_stop_and_no_dead_money_rule(self):
        E.MOM_STATE.write_text(json.dumps({"month": "2026-11"}))           # not a refresh day
        self.add_mom("MOMC", entry=120.0, day=str(DAYS[0].date()))        # 30 days held, still below its buy price
        self.run_day(make_ind())
        self.assertIn("MOMC", self.mom())
        self.assertEqual(self.mom()["MOMC"]["current_stop"], 0.0)
        self.assertNotIn("MOMC", [t[0] for t in self.trades()])

    def test_buys_never_spend_more_than_the_accounts_real_cash(self):
        st = {"sleeve": "nr7", "stop": 1.0, "hi": 100.0, "entry_day": str(DAYS[-1].date()), "n": 0, "done_through": None}
        self.db.open_position("POS_EQBO_BIG", "BIG", "long", 900, 100.0, 1.0, 0.0, 0.0, account_id=E.ACCOUNT,
                              instrument_key="NSE_EQ|BIG", strategy="nr7_breakout", state=json.dumps(st))   # Rs90k already invested
        with self.scores(MOMA=0.9, MOMB=0.5):
            self.run_day(make_ind())
        held = self.db.get_open_positions(E.ACCOUNT)
        invested = sum(p["qty"] * p["entry_price"] for p in held)
        self.assertLessEqual(invested, 100_000 + 1e-6)
        self.assertLess(sum(p["qty"] for p in self.mom().values()), 100)

    def test_later_sessions_fill_empty_slots_from_this_months_list(self):
        """2026-11-19: the first refresh had cash for only ~half of the top 20; later sessions buy the rest of THAT month's list as cash
        frees (buys only -- nothing is sold, the list is not re-ranked, the month stays done)."""
        E.MOM_STATE.write_text(json.dumps({"month": "2026-11", "targets": ["MOMA", "MOMB"]}))
        self.add_mom("MOMA", entry=100.0, qty=10)
        with self.scores(MOMC=0.99):                                        # today's ranking is ignored until next month
            sent, _ = self.run_day(make_ind())
        m = self.mom()
        self.assertEqual(sorted(m), ["MOMA", "MOMB"])
        self.assertEqual(m["MOMA"]["qty"], 10)                              # the existing holding is not touched
        self.assertTrue(any("BOUGHT · Breakout momentum" in x and "<b>MOMB</b>" in x for x in sent))
        self.assertEqual(json.loads(E.MOM_STATE.read_text()), {"month": "2026-11", "targets": ["MOMA", "MOMB"]})
        self.assertEqual(json.loads(E.STATE.read_text())["momentum"]["topup"], ["MOMB"])

    def test_the_refresh_saves_its_list_for_the_month(self):
        with self.scores(MOMA=0.9, MOMB=0.5):
            self.run_day(make_ind())
        self.assertEqual(json.loads(E.MOM_STATE.read_text()), {"month": "2026-11", "targets": ["MOMA", "MOMB"]})

    def test_breakout_off_buys_no_breakouts_but_stops_still_sell(self):
        with mock.patch.dict("os.environ", {"STRATEGY_BREAKOUT": "off"}), self.scores(MOMA=0.9):
            self.run_day(make_ind())
        held = {p["symbol"] for p in self.db.get_open_positions(E.ACCOUNT)}
        self.assertNotIn("DFIVE", held); self.assertNotIn("NRSEVEN", held)
        self.assertIn("MOMA", held)                                          # momentum is still on
        self.assertEqual(self.trades(), [("HELD", 93.5, "stop")])            # the existing holding still follows its stop
        self.assertEqual(json.loads(E.STATE.read_text())["off"], ["breakout"])

    def test_momentum_off_buys_no_momentum(self):
        with mock.patch.dict("os.environ", {"STRATEGY_MOMENTUM": "off"}), self.scores(MOMA=0.9, MOMB=0.5):
            self.run_day(make_ind())
        self.assertEqual(self.mom(), {})
        self.assertIn("DFIVE", {p["symbol"] for p in self.db.get_open_positions(E.ACCOUNT)})   # breakout still on

    def test_momentum_off_still_sells_at_the_monthly_refresh(self):
        """2026-11-20: off used to skip the whole refresh, so holdings (no stop) would never be sold. Off = sells only."""
        E.MOM_STATE.write_text(json.dumps({"month": "2026-10"}))
        self.add_mom("MOMC", entry=90.0)
        self.add_mom("MOMA", entry=95.0)
        with mock.patch.dict("os.environ", {"STRATEGY_MOMENTUM": "off"}), self.scores(MOMA=0.9, MOMB=0.5, MOMC=0.1):
            sent, _ = self.run_day(make_ind())
        self.assertEqual(sorted(self.mom()), ["MOMA"])                       # MOMC left the top 20 -> sold; MOMB not bought
        self.assertIn(("MOMC", 100.0, "momentum_out"), self.trades())
        self.assertFalse(any("BOUGHT · Breakout momentum" in x for x in sent))
        self.assertEqual(json.loads(E.MOM_STATE.read_text())["month"], "2026-11")   # sold once this month, not every day

    def test_close_sells_every_holding_at_the_open_and_buys_nothing(self):
        """2026-11-20 (user: one real Upstox account, "sell all at next open"): STRATEGY_*=close empties that strategy at the open."""
        E.MOM_STATE.write_text(json.dumps({"month": "2026-11", "targets": ["MOMA"]}))
        self.add_mom("MOMC", entry=90.0)
        env = {"STRATEGY_BREAKOUT": "close", "STRATEGY_MOMENTUM": "close"}
        with mock.patch.dict("os.environ", env), self.scores(MOMA=0.9, MOMB=0.5, MOMC=0.8):
            sent, _ = self.run_day(make_ind())
        self.assertEqual(self.db.get_open_positions(E.ACCOUNT), [])
        self.assertEqual(sorted(self.trades()), [("HELD", 100.0, "strategy_closed"), ("MOMC", 100.0, "strategy_closed")])
        closed = [x for x in sent if "strategy closed" in x]
        self.assertEqual(len(closed), 1)                                     # one message for the whole close-out
        self.assertIn("<b>HELD</b>", closed[0]); self.assertIn("<b>MOMC</b>", closed[0])

    def test_close_only_touches_its_own_strategy(self):
        self.add_mom("MOMC", entry=90.0)
        E.MOM_STATE.write_text(json.dumps({"month": "2026-11", "targets": ["MOMC"]}))
        with mock.patch.dict("os.environ", {"STRATEGY_MOMENTUM": "close", "STRATEGY_BREAKOUT": "off"}), self.scores(MOMC=0.9):
            self.run_day(make_ind())
        self.assertIn(("MOMC", 100.0, "strategy_closed"), self.trades())
        self.assertIn(("HELD", 93.5, "stop"), self.trades())                # breakout (off) still follows its stop, not closed

    def test_stale_data_waits_for_the_next_session(self):
        with mock.patch.object(E, "expected_last_day", return_value=date(2026, 11, 17)), self.scores(MOMA=0.9):
            clock = Clock(datetime(2026, 11, 17, 9, 8, tzinfo=IST))
            E.session(self.db, Broker(clock), send=lambda m: None, log=lambda *a: None, sleep=clock.sleep, clock=clock,
                      ind=make_ind(), keys_all={s: f"NSE_EQ|{s}" for s in SYMS}, lock=contextlib.nullcontext)
        self.assertEqual(self.mom(), {})
        self.assertFalse(E.MOM_STATE.exists())                             # not marked done: tomorrow tries again


class TestPlumbing(unittest.TestCase):
    """The account reaches the website, the reset/restore lock list and the nightly price download."""

    def test_registered_everywhere(self):
        from engine import account_reset, backtest_api as api
        self.assertIn(E.ACCOUNT, dict(api.PAPER_ACCOUNTS))
        self.assertEqual(api.ACCOUNT_STRATEGIES[E.ACCOUNT], "breakout_pair, momentum_12_1")
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

    def test_momentum_holding_valued_and_explained(self):
        from engine import backtest_api as api
        p = {"symbol": "X", "qty": 10, "entry_price": 100.0, "current_stop": 0.0, "instrument_key": "NSE_EQ|X",
             "state": json.dumps({"sleeve": "mom", "entry_day": "2026-11-20"})}
        api._value_breakout(p, {"NSE_EQ|X": {"ltp": 110.0}})
        self.assertIn("12-month momentum", p["size_text"]); self.assertNotIn("trailing stop", p["size_text"])
        self.assertEqual(p["risk_to_stop"], 1000.0)                         # no stop: the whole cost is at risk

    def test_today_box_explains_the_momentum_half(self):
        from engine import backtest_api as api
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "equity_breakout_state.json").write_text(json.dumps({"day": "2026-12-01", "fresh": True, "healthy": True, "breadth": 60,
                                                                            "d55": [], "nr7": [], "momentum": {"due": True, "targets": ["A", "B"]}}))
            lines = api.today_plan("EQUITY_BREAKOUT", now=datetime(2026, 12, 1, 10, 0, tzinfo=IST), state_dir=Path(d))
        self.assertTrue(any("Momentum" in x and "A, B" in x for x in lines))

    def test_nightly_check_backtests_both_halves(self):
        from engine import backtest_api as api, paper_vs_backtest as P
        b = P.bodies("EQUITY_BREAKOUT", 100_000)
        self.assertEqual([(x["strategy"], x["capital"]) for x in b], [("breakout_pair", 50_000.0), ("momentum_12_1", 50_000.0)])
        for body in b:
            api.validate(body | {"start": "2026-11-01", "end": "2026-11-18"})

    def test_nightly_download_keeps_breakout_holdings_fresh(self):
        from markets.equity import all_daily
        db = mock.Mock()
        db.get_open_positions.side_effect = lambda acc: ([{"symbol": "ED1", "strategy": "earnings_drift"}] if acc == "RESULTS_GAP"
                                                         else [{"symbol": "BO1", "strategy": "d55_breakout"}])
        with mock.patch("engine.database.TradingDB", return_value=db), mock.patch.object(all_daily, "run", return_value=0) as run:
            all_daily.main([])
        self.assertEqual(run.call_args.kwargs["extra"], ["ED1", "BO1"])


class TestWebBacktestBenchmark(unittest.TestCase):
    """2026-11-19: the benchmark read NIFTYBEES_daily.csv, which stopped updating on 13 Oct -- a window after that was all NaN and the
    website/nightly backtest crashed ("cannot convert float NaN to integer"). It now uses the nightly NIFTY 50 series, carried forward."""

    def test_benchmark_carries_the_last_nifty_close_forward(self):
        import pandas as pd
        from unittest import mock
        from markets.equity.strategies.tf_daily.breakout_pair import strategy as BP
        from markets.equity.strategies.tf_daily.earnings_drift import strategy as ED
        fake = {"replay": {"days": ["2026-11-17", "2026-11-18"], "equity": [100_000, 100_000]}, "trades": pd.DataFrame(), "open": {}}
        nifty = pd.Series([200.0, 210.0], index=pd.to_datetime(["2026-11-13", "2026-11-14"]))
        with mock.patch.object(BP, "backtest", return_value=fake), mock.patch.object(ED, "nifty_daily", return_value=nifty):
            rep = BP.web_backtest(100_000, "2026-11-17", "2026-11-18")
        self.assertEqual(rep["benchmark"], [100_000, 100_000])


class TestLoginRecovery(unittest.TestCase):
    """2026-11-19: a second login at 11:01 cancelled the running session's Upstox login; every quote after 11:02 failed (401) and the
    session watched no stops or buy-stops until 15:31. Now, when the broker flags a dead login, the session adopts the newest saved one
    (or logs in again) -- at most once every 10 minutes."""

    def setUp(self):
        from services.broker import upstox_broker
        self.ev = upstox_broker.token_invalid_event
        self.ev.clear(); self.addCleanup(self.ev.clear)
        E._last_relogin[0] = -1e9

    def test_dead_login_adopts_the_new_one_and_is_rate_limited(self):
        broker, calls, t = mock.Mock(), [], [1000.0]
        def fresh(on_token_refreshed=None):
            calls.append(1); on_token_refreshed("NEW"); return "NEW"
        with mock.patch("services.auth.upstox_auto_login.ensure_fresh_upstox_token", side_effect=fresh):
            E._recover_login(broker, log=lambda *a: None, mono=lambda: t[0])        # nothing flagged: no call
            self.assertEqual(calls, [])
            self.ev.set()
            E._recover_login(broker, log=lambda *a: None, mono=lambda: t[0])
            broker.set_access_token.assert_called_once_with("NEW")
            self.assertFalse(self.ev.is_set())
            self.ev.set(); t[0] += 60                                                  # flagged again a minute later: wait
            E._recover_login(broker, log=lambda *a: None, mono=lambda: t[0])
            self.assertEqual(len(calls), 1)
            t[0] += E.RELOGIN_EVERY_S
            E._recover_login(broker, log=lambda *a: None, mono=lambda: t[0])
            self.assertEqual(len(calls), 2)

    def test_a_failing_refresh_never_breaks_the_session(self):
        self.ev.set()
        with mock.patch("services.auth.upstox_auto_login.ensure_fresh_upstox_token", side_effect=RuntimeError("chrome died")):
            E._recover_login(mock.Mock(), log=lambda *a: None, mono=lambda: 5000.0)   # must not raise
        self.assertTrue(self.ev.is_set())                                             # still flagged for the next try
