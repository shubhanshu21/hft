"""engine/crypto_dip_reversal.py follows the backtest's exit rule (strategy.web_backtest) -- 2026-10-24 fixes: the mid-band exit fills at
the NEXT candle's open (the live price for the newest candle), not the signal candle's own open; the trade starts on the candle it buys
into; every candle since the last check is walked, so a missed run cannot skip a stop/target; labels show the real 15%/7%. Also: the
volatility size scale uses finished days only, and the nightly job keeps the crypto archive current."""
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


class TestNightlyKeepsCryptoCurrent(unittest.TestCase):
    def test_crypto_top_up_runs_nightly_and_is_freshness_checked(self):
        self.assertIn(("crypto data", [nightly.PY, "-m", "markets.crypto.data"], {0}), nightly.STEPS)
        self.assertIn(("crypto", "BTC_5m.csv"), nightly.FRESHNESS)


if __name__ == "__main__":
    unittest.main()
