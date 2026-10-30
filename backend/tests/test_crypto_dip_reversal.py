"""engine/crypto_dip_reversal.py follows the backtest's exit rule (strategy.web_backtest) -- 2026-10-24 fixes: the mid-band exit fills at
the NEXT candle's open (the live price for the newest candle), not the signal candle's own open; the trade starts on the candle it buys
into; every candle since the last check is walked, so a missed run cannot skip a stop/target; labels show the real 15%/7%. Also: the
volatility size scale uses finished days only, and the nightly job keeps the crypto archive current."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from engine import crypto_dip_reversal as E
from engine import nightly
from engine.database import TradingDB
from markets.crypto.strategies.tf_5min.dip_reversal import strategy as S

T0 = 1_792_761_300_000          # a 5-minute candle start (ms)
BAR = E.BAR_MS


def candles(rows):
    """rows: (open, high, low, close, mid20) per 5-minute candle starting at T0."""
    return pd.DataFrame([{"t": T0 + i * BAR, "open": o, "high": h, "low": l, "close": c, "bb_mid20": m} for i, (o, h, l, c, m) in enumerate(rows)])


def position(entry=100.0, entry_bar=1):
    return {"position_id": "P", "entry_price": entry, "sl_price": entry * (1 - S.STOPLOSS), "tp_price": entry * (1 + S.ROI),
            "pos_usd": 1000.0, "qty": 10.0, "entry_bar_t": T0 + entry_bar * BAR, "entry_time": "2026-10-24T10:00:00+05:30", "real": False}


FLAT = (100, 101, 99, 100, 105)          # no exit: inside stop/target, close under the mid-band


class TestBarExit(unittest.TestCase):
    def test_mid_band_exit_fills_at_next_candles_open_not_the_signal_candles_open(self):
        # the DOT case 2026-10-23: signal candle opened 1.1253, closed 1.1275 above the mid-band; next open 1.1276
        df = candles([FLAT, FLAT, (99.0, 101.5, 98.8, 101.2, 100.5), (101.3, 101.6, 101.0, 101.4, 100.6)])
        px, fee, why, t = E.bar_exit(position(), df, live_px=None, coin="DOT")
        self.assertEqual((px, why, t), (101.3, E.SIGNAL_WHY, T0 + 3 * BAR))

    def test_mid_band_exit_on_the_newest_candle_uses_the_live_price(self):
        df = candles([FLAT, FLAT, (99.0, 101.5, 98.8, 101.2, 100.5)])
        px, _, why, _ = E.bar_exit(position(), df, live_px=101.25, coin="DOT")
        self.assertEqual((px, why), (101.25, E.SIGNAL_WHY))

    def test_no_mid_band_exit_on_the_entry_candle(self):
        df = candles([FLAT, (100, 101.5, 99.5, 101.2, 100.5)])        # entry candle (index 1) closes above the mid-band
        self.assertIsNone(E.bar_exit(position(entry_bar=1), df, live_px=101.0, coin="DOT")[0])

    def test_target_on_the_entry_candle_fills_at_the_target(self):
        df = candles([FLAT, (100, 116, 99.5, 112, 105)])
        px, fee, why, _ = E.bar_exit(position(), df, None, "DOT")
        self.assertAlmostEqual(px, 115.0)
        self.assertEqual((fee, why), (S.MAKER, "TAKE PROFIT (15%)"))

    def test_target_gapped_through_on_a_later_candle_fills_at_the_open(self):
        df = candles([FLAT, FLAT, (117, 118, 116, 117, 105)])
        self.assertEqual(E.bar_exit(position(), df, None, "DOT")[0], 117)

    def test_stop_fills_at_the_open_when_the_candle_opens_below_it(self):
        df = candles([FLAT, FLAT, (90, 91, 89, 90, 105)])
        px, _, why, _ = E.bar_exit(position(), df, None, "DOT")
        self.assertEqual((px, why), (90, "STOP LOSS (7%)"))

    def test_stop_comes_before_target_in_one_candle(self):
        df = candles([FLAT, FLAT, (100, 116, 92, 100, 105)])
        self.assertEqual(E.bar_exit(position(), df, None, "DOT")[2], E.STOP_WHY)

    def test_a_stop_in_a_missed_candle_is_still_found(self):
        # last checked at candle 2; the run for candle 3 was missed (stop hit there), now candle 4 is the newest
        df = candles([FLAT, FLAT, FLAT, (99, 99.5, 92.5, 93, 105), (94, 95, 93.5, 94, 105)])
        pos = dict(position(), last_checked_t=T0 + 2 * BAR)
        px, _, why, t = E.bar_exit(pos, df, None, "DOT")
        self.assertEqual((px, why, t), (93.0, E.STOP_WHY, T0 + 3 * BAR))

    def test_already_checked_candles_are_not_rechecked(self):
        df = candles([FLAT, FLAT, (99, 99.5, 92.5, 93, 105), FLAT])
        pos = dict(position(), last_checked_t=T0 + 2 * BAR)
        self.assertIsNone(E.bar_exit(pos, df, None, "DOT")[0])

    def test_time_stop_after_24_hours_at_the_close(self):
        rows = [FLAT] * (S.TIME_STOP_BARS + 2)
        px, _, why, t = E.bar_exit(position(), candles(rows), None, "DOT")
        self.assertEqual((px, why, t), (100, E.TIME_WHY, T0 + (1 + S.TIME_STOP_BARS) * BAR))


class TestDeltaOrdersNeedTheSafetyGates(unittest.TestCase):
    """2026-10-26: DELTA_PLACE_REAL_ORDERS alone used to be the whole defence before an order went to an exchange. The three
    gates in engine/safety_gate.py must also agree (and they are all shut by default)."""

    def test_orders_are_simulated_when_only_the_env_flag_is_on(self):
        with mock.patch.dict(E.os.environ, {"DELTA_PLACE_REAL_ORDERS": "true"}), \
             mock.patch.object(E, "DeltaDemoBroker") as broker, \
             mock.patch("engine.safety_gate.live_trading_allowed", return_value=False):
            self.assertIsNone(E._delta())
            broker.assert_not_called()

    def test_orders_go_out_only_when_the_flag_and_all_gates_agree(self):
        with mock.patch.dict(E.os.environ, {"DELTA_PLACE_REAL_ORDERS": "true"}), \
             mock.patch.object(E, "DeltaDemoBroker") as broker, \
             mock.patch("engine.safety_gate.live_trading_allowed", return_value=True):
            E._delta()
            broker.assert_called_once_with()

    def test_the_flag_being_off_never_constructs_a_broker(self):
        with mock.patch.dict(E.os.environ, {"DELTA_PLACE_REAL_ORDERS": "false"}), \
             mock.patch.object(E, "DeltaDemoBroker") as broker, \
             mock.patch("engine.safety_gate.live_trading_allowed", return_value=True):
            self.assertIsNone(E._delta())
            broker.assert_not_called()

    def test_the_real_safety_gate_blocks_by_default(self):
        from engine.safety_gate import KILL_SWITCH_ENGAGED
        self.assertTrue(KILL_SWITCH_ENGAGED, "the source-code kill switch must stay on unless someone deliberately disarms it")


class TestEngineRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = TradingDB(self.tmp / "t.db")
        for p in (mock.patch.object(E, "STATE_FILE", self.tmp / "state.json"),
                  mock.patch.object(E.requests, "get", side_effect=OSError("no network in tests")),   # funding fetch -> 0
                  mock.patch.object(S, "current_vol_scale", return_value=1.0)):
            p.start()
            self.addCleanup(p.stop)

    def test_entry_starts_on_the_candle_after_the_signal(self):
        state = E.load_state()
        df = candles([FLAT] * 60)
        with mock.patch.object(S, "entry_signal", side_effect=lambda d: pd.Series([False] * (len(d) - 1) + [True])):
            E.process_entries(self.db, state, {"DOT": df}, {"usdtinr": 90.0, "prices": {"DOT": 100.0}}, [], None)
        self.assertEqual(state["open_positions"]["DOT"]["entry_bar_t"], int(df["t"].iloc[-1]) + BAR)

    def test_entry_risks_six_percent_of_the_account(self):
        # 2026-10-24: risk per trade cut from 8% to 6% (8% added ~3-4% for a -88% worst fall vs -75%)
        self.assertEqual(S.RISK_PCT, 0.06)
        state = E.load_state()
        df = candles([FLAT] * 60)
        with mock.patch.object(S, "entry_signal", side_effect=lambda d: pd.Series([False] * (len(d) - 1) + [True])):
            E.process_entries(self.db, state, {"DOT": df}, {"usdtinr": 90.0, "prices": {"DOT": 100.0}}, [], None)
        p = state["open_positions"]["DOT"]
        want_usd = state["trading_capital"] * 0.06 / S.STOPLOSS / 90.0          # risk 6% of the account / the 7% stop, in USD
        self.assertLessEqual(p["pos_usd"], want_usd)
        self.assertGreater(p["pos_usd"], want_usd - E.CONTRACT_VALUE["DOT"] * 100.0)   # whole DOT contracts, rounded down

    def test_exit_is_booked_at_the_live_price_and_recorded(self):
        state = E.load_state()
        state["open_positions"]["DOT"] = position()
        self.db.init_account(E.ACCOUNT_ID, capital=100_000)
        self.db.open_position("P", "DOT", "long", 10.0, 100.0, 93.0, 115.0, 100.0, account_id=E.ACCOUNT_ID)
        df = candles([FLAT, FLAT, (99.0, 101.5, 98.8, 101.2, 100.5)])
        lines = []
        E.process_exits(self.db, state, {"DOT": df}, {"usdtinr": 90.0, "prices": {"DOT": 101.25}}, lines, None)
        self.assertNotIn("DOT", state["open_positions"])
        self.assertIn("Exit 101.2500", lines[0])
        gross = (101.25 - 100) / 100 * 1000 * 90
        fees = 1000 * 2 * (S.TAKER + S.SLIP.get("DOT", 0.00015)) * 90
        self.assertAlmostEqual(state["trading_capital"], 100_000 + gross - fees, places=4)

    def test_no_exit_moves_the_checked_mark_forward(self):
        state = E.load_state()
        state["open_positions"]["DOT"] = position()
        df = candles([FLAT, FLAT, FLAT])
        E.process_exits(self.db, state, {"DOT": df}, {"usdtinr": 90.0, "prices": {}}, [], None)
        self.assertEqual(state["open_positions"]["DOT"]["last_checked_t"], T0 + 2 * BAR)


class TestFxFallbackIsLoud(unittest.TestCase):
    """2026-10-26: the USD/INR rate fell back to a hardcoded 92.5 with nothing said, so rupee P&L silently used a stale rate."""

    def test_a_dead_fx_feed_is_flagged(self):
        with mock.patch.object(E.requests, "get", side_effect=OSError("down")):
            info = E.fetch_live_prices()
        self.assertTrue(info["fx_fallback"])
        self.assertEqual(info["usdtinr"], E.USDINR_FALLBACK)

    def test_a_live_fx_feed_clears_the_flag(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"rates": {"INR": 91.37}, "result": {"close": 100.0}}
        with mock.patch.object(E.requests, "get", return_value=resp):
            info = E.fetch_live_prices()
        self.assertFalse(info["fx_fallback"])
        self.assertEqual(info["usdtinr"], 91.37)

    def test_a_run_that_uses_the_fallback_says_so(self):
        db = TradingDB(Path(tempfile.mkdtemp()) / "t.db")
        live = {"prices": {}, "usdtinr": E.USDINR_FALLBACK, "fx_fallback": True, "timestamp": ""}
        with mock.patch.object(E, "STATE_FILE", Path(tempfile.mkdtemp()) / "s.json"), \
             mock.patch.object(E, "fetch_live_prices", return_value=live), \
             mock.patch.object(E.requests, "get", side_effect=OSError("no network in tests")):
            out = E.run_iteration(db, send_fn=None)
        self.assertTrue(any("fallback rate" in ln for ln in out["lines"]))


class TestLedgerSurvivesACrash(unittest.TestCase):
    """2026-10-26: the state file is the account's ledger. A half-written file used to be replaced with the defaults (Rs1L) and the
    database overwritten to match; a crash between the DB write and the state write booked the same exit twice."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state_path = self.tmp / "state.json"
        self.db = TradingDB(self.tmp / "t.db")
        self.db.init_account(E.ACCOUNT_ID, capital=100_000)
        for p in (mock.patch.object(E, "STATE_FILE", self.state_path),
                  mock.patch.object(E.requests, "get", side_effect=OSError("no network in tests")),
                  mock.patch.object(S, "current_vol_scale", return_value=1.0)):
            p.start()
            self.addCleanup(p.stop)

    def test_an_unreadable_state_file_keeps_the_database_capital(self):
        self.db.update_capital(E.ACCOUNT_ID, 173_500.0)
        self.state_path.write_text('{"trading_capital": 1735')          # truncated by a crash mid-write
        state = E.load_state(self.db)
        self.assertEqual(state["trading_capital"], 173_500.0)
        self.assertTrue(state["state_recovered"])

    def test_a_recovered_state_says_so_out_loud(self):
        self.state_path.write_text("not json at all")
        out = E.run_iteration(self.db, send_fn=None)
        self.assertTrue(any("recovered from the database" in ln for ln in out["lines"]))

    def test_saving_is_atomic_and_keeps_the_previous_version(self):
        E.save_state({"trading_capital": 1.0, "open_positions": {}})
        E.save_state({"trading_capital": 2.0, "open_positions": {}})
        self.assertEqual(json.loads(self.state_path.read_text())["trading_capital"], 2.0)
        self.assertEqual(json.loads((self.tmp / "state.json.bak").read_text())["trading_capital"], 1.0)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])

    def test_a_position_already_closed_in_the_database_is_not_booked_twice(self):
        self.db.open_position("P", "DOT", "long", 10.0, 100.0, 93.0, 115.0, 100.0, account_id=E.ACCOUNT_ID)
        self.db.close_position("P", 101.0, "stop", 10.0, 9.0, 1.0)      # the run that died before saving the state file
        state = {"trading_capital": 100_000.0, "open_positions": {"DOT": position()}, "completed_trades_count": 0,
                 "winning_trades_count": 0, "total_fees_paid": 0.0}
        df = candles([FLAT, FLAT, (99.0, 101.5, 98.8, 101.2, 100.5)])
        lines = []
        E.process_exits(self.db, state, {"DOT": df}, {"usdtinr": 90.0, "prices": {"DOT": 101.25}}, lines, None)
        self.assertNotIn("DOT", state["open_positions"])
        self.assertEqual(state["trading_capital"], 100_000.0, "a re-run must not credit the same exit again")
        self.assertEqual(len(self.db.get_trades(account_id=E.ACCOUNT_ID)), 0)

    def test_an_exit_booked_before_a_crash_still_reaches_the_capital(self):
        """2026-10-30 review: the trade row was written, then the run died before saving the state (e.g. an error while looking for
        entries). The next run dropped the position without booking it twice -- but also never added its profit to the capital, and
        then overwrote the database's capital with that stale number. Now it credits the recorded trade's result once."""
        self.db.open_position("P", "DOT", "long", 10.0, 100.0, 93.0, 115.0, 100.0, account_id=E.ACCOUNT_ID)
        self.db.close_position("P", 105.0, "target", 500.0, 450.0, 50.0)
        self.db.record_trade(position_id="P", symbol="DOT", direction="long", qty=10.0, entry_price=100.0, exit_price=105.0,
                             entry_dt="2026-10-30T01:00:00+05:30", exit_dt="2026-10-30T02:00:00+05:30", hold_minutes=60, exit_reason="target",
                             gross_pnl=500.0, costs_dict={"total": 50.0}, net_pnl=450.0, capital_after=100_450.0,
                             account_id=E.ACCOUNT_ID, strategy=E.STRATEGY_NAME)
        state = {"trading_capital": 100_000.0, "open_positions": {"DOT": position()}, "completed_trades_count": 0,
                 "winning_trades_count": 0, "total_fees_paid": 0.0}
        df = candles([FLAT, FLAT, (99.0, 101.5, 98.8, 101.2, 100.5)])
        E.process_exits(self.db, state, {"DOT": df}, {"usdtinr": 90.0, "prices": {"DOT": 101.25}}, [], None)
        self.assertNotIn("DOT", state["open_positions"])
        self.assertEqual(state["trading_capital"], 100_450.0)
        self.assertEqual((state["completed_trades_count"], state["winning_trades_count"], state["total_fees_paid"]), (1, 1, 50.0))
        self.assertEqual(len(self.db.get_trades(account_id=E.ACCOUNT_ID)), 1)          # still one trade row

    def test_a_normal_exit_is_booked_once(self):
        self.db.open_position("P", "DOT", "long", 10.0, 100.0, 93.0, 115.0, 100.0, account_id=E.ACCOUNT_ID)
        state = {"trading_capital": 100_000.0, "open_positions": {"DOT": position()}, "completed_trades_count": 0,
                 "winning_trades_count": 0, "total_fees_paid": 0.0}
        df = candles([FLAT, FLAT, (99.0, 101.5, 98.8, 101.2, 100.5)])
        E.process_exits(self.db, state, {"DOT": df}, {"usdtinr": 90.0, "prices": {"DOT": 101.25}}, [], None)
        self.assertEqual(len(self.db.get_trades(account_id=E.ACCOUNT_ID)), 1)
        self.assertGreater(state["completed_trades_count"], 0)


class TestVolScaleUsesFinishedDaysOnly(unittest.TestCase):
    def test_a_day_is_sized_from_the_day_before(self):
        tmp = Path(tempfile.mkdtemp())
        idx = pd.date_range("2026-01-01", periods=40 * 288, freq="5min")
        closes = pd.Series(100.0, index=idx)
        closes[idx >= pd.Timestamp("2026-02-05")] = [100.0 * (1.2 if i % 2 else 0.8) for i in range((idx >= pd.Timestamp("2026-02-05")).sum())]
        pd.DataFrame({"date": idx, "close": closes.values}).to_csv(tmp / "BTC_5m.csv", index=False)
        with mock.patch.object(S, "DATA_DIR", tmp), mock.patch.object(S, "COINS", ("BTC",)):
            s = S.market_vol_scale_series()
        # 2026-02-05 is the first wild day: its own close must not shrink its own size, only the NEXT day's
        self.assertAlmostEqual(s[pd.Timestamp("2026-02-05")], S.VOL_HI)
        self.assertLess(s[pd.Timestamp("2026-02-06")], S.VOL_HI)


class TestPortfolioCaps(unittest.TestCase):
    """2026-10-26: MAX_LEVERAGE is per position, so five positions could run at ~4.3x the account with ~30% of it lost if every
    stop hit at once. The book-level caps exist now but ship OFF -- turning them on changes how the paper record would have traded."""

    def test_both_caps_are_off_unless_asked_for(self):
        self.assertEqual(E.MAX_TOTAL_NOTIONAL_X, 0.0)
        self.assertEqual(E.MAX_TOTAL_RISK_PCT, 0.0)
        big = {"BTC": {"pos_usd": 1e6}, "ETH": {"pos_usd": 1e6}}
        self.assertIsNone(E.portfolio_cap_block(100_000, 90.0, big, 1e6))

    def test_the_notional_cap_blocks_the_trade_that_would_breach_it(self):
        with mock.patch.object(E, "MAX_TOTAL_NOTIONAL_X", 3.0):
            open_pos = {"BTC": {"pos_usd": 2000.0}, "ETH": {"pos_usd": 1000.0}}       # 3000 USD = Rs2.7L = 2.7x
            self.assertIsNone(E.portfolio_cap_block(100_000, 90.0, open_pos, 300.0))   # 2.97x: still under
            reason = E.portfolio_cap_block(100_000, 90.0, open_pos, 400.0)             # 3.06x: over
        self.assertIn("portfolio notional cap", reason)

    def test_the_risk_cap_counts_a_stop_on_every_open_position(self):
        with mock.patch.object(E, "MAX_TOTAL_RISK_PCT", 0.15):
            open_pos = {"BTC": {"pos_usd": 2000.0}}                                   # stop hit = 7% of Rs1.8L = 12.6% risked
            self.assertIsNone(E.portfolio_cap_block(100_000, 90.0, open_pos, 0.0))
            reason = E.portfolio_cap_block(100_000, 90.0, open_pos, 500.0)              # + Rs45k notional = 16.1% risked
        self.assertIn("portfolio risk cap", reason)

    def test_a_blocked_entry_is_reported_and_no_position_is_opened(self):
        db = TradingDB(Path(tempfile.mkdtemp()) / "t.db")
        with mock.patch.object(E, "MAX_TOTAL_NOTIONAL_X", 1.5), \
             mock.patch.object(S, "current_vol_scale", return_value=1.0):
            state = {"trading_capital": 100_000.0, "open_positions": {}, "completed_trades_count": 0,
                     "winning_trades_count": 0, "total_fees_paid": 0.0}
            df = candles([FLAT] * 60)
            with mock.patch.object(S, "entry_signal", side_effect=lambda d: pd.Series([False] * (len(d) - 1) + [True])):
                lines = []
                E.process_entries(db, state, {"DOT": df, "ADA": df}, {"usdtinr": 90.0, "prices": {"DOT": 100.0, "ADA": 100.0}}, lines, None)
        # each entry is ~0.86x the account, so the second would take the book past 1.5x and must be refused
        self.assertEqual(len(state["open_positions"]), 1)
        self.assertTrue(any("portfolio notional cap" in ln and "skipped" in ln for ln in lines))


class TestVolScaleIsCached(unittest.TestCase):
    """2026-10-26: rebuilding the scale reads ~265 MB of 5-minute CSVs (measured 17.3 s) and the live engine paid that on EVERY
    entry, twice. It now reads once and is cached in var/cache, keyed by the archive files themselves so a nightly top-up still
    invalidates it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "vol_scale.json"
        for coin in ("BTC", "ETH"):
            (self.tmp / f"{coin}_5m.csv").write_text("date,close\n")
        self.calls = 0

        def fake_series():
            self.calls += 1
            return pd.Series([1.25])

        for p in (mock.patch.object(S, "DATA_DIR", self.tmp),
                  mock.patch.object(S, "COINS", ("BTC", "ETH")),
                  mock.patch.object(S, "_VOL_CACHE_FILE", self.cache),
                  mock.patch.object(S, "_VOL_MEMO", {}),
                  mock.patch.object(S, "market_vol_scale_series", side_effect=fake_series)):
            p.start()
            self.addCleanup(p.stop)

    def test_the_archive_is_read_once_per_process(self):
        self.assertEqual(S.current_vol_scale(), 1.25)
        self.assertEqual(S.current_vol_scale(), 1.25)
        self.assertEqual(self.calls, 1, "the second call must come from the in-process memo")

    def test_a_new_process_reuses_the_disk_cache(self):
        S.current_vol_scale()
        self.assertTrue(self.cache.exists())
        S._VOL_MEMO.clear()                                     # what a fresh `python3 -m engine...` sees
        self.assertEqual(S.current_vol_scale(), 1.25)
        self.assertEqual(self.calls, 1, "a warm cache must not re-read the CSVs")

    def test_a_new_archive_file_invalidates_the_cache(self):
        S.current_vol_scale()
        S._VOL_MEMO.clear()
        f = self.tmp / "BTC_5m.csv"
        with open(f, "a", encoding="utf-8") as fh:              # the nightly top-up appends to the archive
            fh.write("2026-10-26 00:00:00+00:00,100\n")
        S.current_vol_scale()
        self.assertEqual(self.calls, 2, "an appended candle must be recomputed, never served stale")

    def test_the_cached_value_is_the_freshly_computed_one(self):
        fresh = S.current_vol_scale(use_cache=False)
        self.assertEqual(self.calls, 1)
        self.assertEqual(S.current_vol_scale(), fresh)


class TestNightlyKeepsCryptoCurrent(unittest.TestCase):
    def test_crypto_top_up_runs_nightly_and_is_freshness_checked(self):
        self.assertIn(("crypto data", [nightly.PY, "-m", "markets.crypto.data"], {0}), nightly.STEPS)
        self.assertIn(("crypto", "BTC_5m.csv"), nightly.FRESHNESS)


if __name__ == "__main__":
    unittest.main()


class TestBtcBenchmark(unittest.TestCase):
    def test_benchmark_is_capital_times_btc_growth(self):
        idx = pd.date_range("2026-03-01", periods=3 * 288, freq="5min")
        closes = [100.0] * 288 + [110.0] * 288 + [90.0] * 288
        df = pd.DataFrame({"date": idx, "close": closes})
        with mock.patch.object(S, "load_5m_data", return_value={"BTC": df}):
            b = S.btc_hold_benchmark(["2026-03-01", "2026-03-02", "2026-03-03"], 100_000)
        self.assertEqual(b, [100_000, 110_000, 90_000])

    def test_no_days_no_benchmark(self):
        self.assertIsNone(S.btc_hold_benchmark([], 100_000))
