import lzma
import struct
import unittest
from datetime import date

import numpy as np
import pandas as pd


class DukascopyDecodeTests(unittest.TestCase):
    def test_decode_one_day(self):
        from services.data import dukascopy as d
        # two 1-minute records: (seconds since 00:00 UTC, open, close, low, high, volume); gold is scaled by 1000
        payload = lzma.compress(struct.pack(">IIIIIf", 0, 2915148, 2914858, 2914428, 2915248, 0.5) + struct.pack(">IIIIIf", 60, 2914858, 2915000, 2914800, 2915100, 0.25))
        df = d.decode(payload, date(2025, 3, 12), d.SCALE["XAUUSD"])
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(df.loc[0, "open"], 2915.148)
        self.assertAlmostEqual(df.loc[0, "high"], 2915.248)          # record order is open, close, low, high
        self.assertAlmostEqual(df.loc[0, "low"], 2914.428)
        self.assertEqual(str(df.loc[1, "timestamp"]), "2025-03-12 00:01:00+00:00")

    def test_closed_day_is_empty_not_an_error(self):
        from services.data import dukascopy as d
        self.assertEqual(len(d.decode(b"", date(2025, 3, 15), 1e3)), 0)


class EquityPoolTests(unittest.TestCase):
    def test_pool_respects_the_concurrency_cap_and_one_position_per_symbol(self):
        from markets.equity.experiments import multi_strategy_study as m
        base = {"exit_price": 101.0, "stop_dist": 1.0, "direction": "long", "entry_price": 100.0}
        cands = [{**base, "symbol": s, "entry_time": "2026-01-05T10:00:00", "exit_time": "2026-01-05T11:00:00"} for s in ("A", "B", "C", "D")]
        cands.append({**base, "symbol": "A", "entry_time": "2026-01-05T10:30:00", "exit_time": "2026-01-05T11:30:00"})      # A is already open
        cands.append({**base, "symbol": "E", "entry_time": "2026-01-05T11:00:00", "exit_time": "2026-01-05T12:00:00"})      # slots freed at 11:00
        got = [t["symbol"] for t in m.pool(cands)]
        self.assertEqual(got, ["A", "B", "C", "E"])

    def test_orb_fires_once_per_day_and_fade_is_the_opposite(self):
        from markets.equity.experiments import multi_strategy_study as m
        n = 20
        ts = [f"2026-01-05T{9 + (15 + 5 * i) // 60:02d}:{(15 + 5 * i) % 60:02d}:00+05:30" for i in range(n)]
        close = np.array([100.0] * 6 + [100.1, 100.2, 101.0, 101.5] + [101.0] * 10)      # breaks the 6-bar range upward once
        f = pd.DataFrame({"timestamp": ts, "high": close + 0.05, "low": close - 0.05, "close": close, "minutes_since_open": [5 * i for i in range(n)]})
        brk, fade = m.sig_orb(f, False), m.sig_orb(f, True)
        self.assertEqual(int((brk != 0).sum()), 1)
        self.assertEqual(int(brk.sum()), 1)
        self.assertEqual(int(fade.sum()), -1)


if __name__ == "__main__":
    unittest.main()


class EntryPullbackTests(unittest.TestCase):
    def test_limit_fills_only_after_a_pullback_and_never_through_the_stop(self):
        from core import entry_pullback as ep
        # long signal at 100 with stop distance 2 -> resting limit 0.25*2 = 0.5 better = 99.5
        pending, d, sd, px = ep.step(None, 10, "long", 2.0, 100.0, 99.0, 101.0, 0.25, 3)
        self.assertIsNone(d)
        self.assertEqual(pending["limit"], 99.5)
        pending, d, sd, px = ep.step(pending, 11, None, 2.0, 100.5, 100.0, 101.0, 0.25, 3)          # no pullback yet
        self.assertIsNone(d)
        _, d, sd, px = ep.step(pending, 12, None, 2.0, 100.0, 99.4, 100.6, 0.25, 3)                 # touches 99.5
        self.assertEqual((d, px, sd), ("long", 99.5, 2.0))
        pending, *_ = ep.step(None, 10, "long", 2.0, 100.0, 99.0, 101.0, 0.25, 3)
        pending2, d, *_ = ep.step(pending, 11, None, 2.0, 98.0, 97.5, 99.0, 0.25, 3)                # bar trades through the 97.5 stop: dropped, not filled
        self.assertIsNone(d)
        self.assertIsNone(pending2)

    def test_expires_and_haircut_requires_trading_through(self):
        from core import entry_pullback as ep
        pending, *_ = ep.step(None, 10, "short", 2.0, 100.0, 99.0, 101.0, 0.25, 3, 0.1)              # limit 100.5, needs high >= 100.7
        _, d, *_ = ep.step(pending, 11, None, 2.0, 100.0, 99.5, 100.6, 0.25, 3, 0.1)
        self.assertIsNone(d)
        _, d, _, px = ep.step(pending, 12, None, 2.0, 100.0, 99.5, 100.8, 0.25, 3, 0.1)
        self.assertEqual((d, px), ("short", 100.5))
        expired, d, *_ = ep.step(pending, 14, None, 2.0, 100.0, 99.5, 100.9, 0.25, 3, 0.1)          # 4 bars later: too late
        self.assertIsNone(d)
        self.assertIsNone(expired)


class PendingBookTests(unittest.TestCase):
    def _signal(self, direction="long"):
        from core.strategy import Signal
        sd = 2.0
        if direction == "long":
            return Signal(symbol="X", direction="long", entry_price=100.0, stop_loss=98.0, qty=10, stop_dist=sd, exit_state={"activation_price": 101.0, "armed_trail": False},
                          price_levels=("activation_price",), target_price=101.0, breakeven_price=101.0, alert_levels={"tp": 101.0})
        return Signal(symbol="X", direction="short", entry_price=100.0, stop_loss=102.0, qty=10, stop_dist=sd, exit_state={"activation_price": 99.0, "armed_trail": False},
                      price_levels=("activation_price",), target_price=99.0, breakeven_price=99.0, alert_levels={"tp": 99.0})

    @staticmethod
    def _bar(ts, low, high):
        return {"timestamp": f"2026-09-25T10:{ts:02d}:00+05:30", "open": (low + high) / 2, "high": high, "low": low, "close": (low + high) / 2}

    def test_fill_reanchors_every_level_to_the_limit(self):
        from datetime import datetime
        from core import entry_pullback as ep
        book = ep.PendingBook()
        book.register("X", self._signal(), "2026-09-25T10:00:00+05:30", (0.25, 3, 0.0))
        self.assertIn("X", book)
        status, sig = book.check("X", [self._bar(0, 99.9, 100.4), self._bar(5, 100.1, 100.6)], datetime.fromisoformat("2026-09-25T10:07:00+05:30"))
        self.assertEqual(status, "wait")                                             # limit is 99.5, not touched yet
        status, sig = book.check("X", [self._bar(0, 99.9, 100.4), self._bar(5, 99.4, 100.6)], datetime.fromisoformat("2026-09-25T10:09:00+05:30"))
        self.assertEqual(status, "filled")
        self.assertEqual((sig.entry_price, sig.stop_loss, sig.exit_state["activation_price"], sig.target_price, sig.alert_levels["tp"]), (99.5, 97.5, 100.5, 100.5, 100.5))
        self.assertNotIn("X", book)                                                  # consumed

    def test_short_side_dropped_when_the_stop_is_run_through_and_expired_after_the_window(self):
        from datetime import datetime
        from core import entry_pullback as ep
        book = ep.PendingBook()
        book.register("X", self._signal("short"), "2026-09-25T10:00:00+05:30", (0.25, 3, 0.0))       # limit 100.5, stop level 102.5
        status, sig = book.check("X", [self._bar(5, 100.0, 102.7)], datetime.fromisoformat("2026-09-25T10:08:00+05:30"))
        self.assertEqual((status, sig), ("dropped", None))
        book.register("X", self._signal("short"), "2026-09-25T10:00:00+05:30", (0.25, 3, 0.0))
        quiet = [self._bar(5 * k, 99.0, 100.2) for k in range(1, 5)]                             # 4 bars, never reaches 100.5
        status, _ = book.check("X", quiet, datetime.fromisoformat("2026-09-25T10:25:00+05:30"))
        self.assertEqual(status, "none")
        self.assertNotIn("X", book)

    def test_a_new_day_clears_a_stale_pending_and_config_is_off_by_default(self):
        import os
        from datetime import datetime
        from unittest.mock import patch
        from core import entry_pullback as ep
        book = ep.PendingBook()
        book.register("X", self._signal(), "2026-09-25T10:00:00+05:30", (0.25, 3, 0.0))
        nxt = {"timestamp": "2026-09-28T09:20:00+05:30", "low": 99.0, "high": 100.0, "open": 99.5, "close": 99.5}
        self.assertEqual(book.check("X", [nxt], datetime.fromisoformat("2026-09-28T09:21:00+05:30"))[0], "none")
        def clean(extra):
            keep = {k: v for k, v in os.environ.items() if "PULLBACK" not in k}      # the operator's .env may switch pullback entries on
            return patch.dict(os.environ, {**keep, **extra}, clear=True)
        with clean({}):
            self.assertIsNone(ep.config_for("RELIANCE", "equity"))
        with clean({"EQUITY_PULLBACK_FRAC": "0.15", "EQUITY_PULLBACK_THROUGH": "0.02", "CRUDEOILM_PULLBACK_FRAC": "0.2"}):
            self.assertEqual(ep.config_for("RELIANCE", "equity"), (0.15, 3, 0.02))
            self.assertEqual(ep.config_for("CRUDEOILM", "commodity"), (0.2, 3, 0.0))
            self.assertIsNone(ep.config_for("SILVERMIC", "commodity"))
