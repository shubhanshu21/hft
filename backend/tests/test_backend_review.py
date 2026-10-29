"""Fixes from the 2026-10-29 backend review (security-reviewer + code-reviewer skills)."""
import json
import unittest
import warnings
from datetime import date

import pandas as pd

from engine import backtest_api as A


class FakeHandler:
    """Just enough of BaseHTTPRequestHandler for Handler._send."""
    def __init__(self, exc):
        self.exc, self.headers_sent = exc, []
        self.wfile = self
    def send_response(self, code): self.code = code
    def send_header(self, k, v): self.headers_sent.append((k, v))
    def end_headers(self): pass
    def write(self, body): raise self.exc


class TestClientGone(unittest.TestCase):
    def test_browser_closing_early_is_not_a_crash(self):
        # 2026-10-28 journal: a closed tab raised BrokenPipeError inside _send, then the 503 fallback hit the same dead socket
        for exc in (BrokenPipeError(), ConnectionResetError()):
            A.Handler._send(FakeHandler(exc), 200, {"ok": True})          # must not raise

    def test_other_errors_still_surface(self):
        with self.assertRaises(OSError):
            A.Handler._send(FakeHandler(OSError("disk")), 200, {})


class TestNoDeprecatedTimedelta(unittest.TestCase):
    def test_mcx_roll_check_without_numpy_generic_units(self):
        from engine.mcx_trend_carry import due_to_roll
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertTrue(due_to_roll("2026-11-19", date(2026, 11, 16), 3))
            self.assertFalse(due_to_roll("2026-11-19", date(2026, 11, 15), 3))
            self.assertFalse(due_to_roll(None, date(2026, 11, 16), 3))

    def test_futures_eod_rows_concat_without_future_warning(self):
        from markets.commodity import futures_eod as F
        old = pd.DataFrame([{c: None for c in F.COLS} | {"date": pd.Timestamp("2026-10-01"), "oi": 5.0, "source": "bhavcopy"}], columns=F.COLS)
        new = F.rows_frame([{c: None for c in F.COLS} | {"date": pd.Timestamp("2026-10-02"), "source": "upstox"}])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = pd.concat([old.astype({"oi": "float64"}), new], ignore_index=True)
        self.assertEqual(len(out), 2)


class TestEquityMarginShown(unittest.TestCase):
    def pos(self, state):
        return {"symbol": "X", "qty": 100, "entry_price": 500.0, "state": json.dumps(state), "instrument_key": "NSE_EQ|X"}

    def test_margin_follows_upstox_leverage_for_that_stock(self):
        p = self.pos({"upstox_leverage": 1.0}); A._value_results_gap(p, {})
        self.assertEqual(p["margin_used"], 50000.0)                      # a 1x stock blocks the full Rs50,000
        p = self.pos({"upstox_leverage": 5.0}); A._value_results_gap(p, {})
        self.assertEqual(p["margin_used"], 10000.0)
        p = self.pos({}); A._value_results_gap(p, {})                    # older positions without the field: Upstox's usual 5x
        self.assertEqual(p["margin_used"], 10000.0)


if __name__ == "__main__":
    unittest.main()


class TestBackupsArePrivate(unittest.TestCase):
    def test_new_backup_file_is_readable_by_its_owner_only(self):
        # 2026-10-29 review: the DB and its 99 backups were world-readable (644); existing files were chmod 600, new ones must be too
        import os
        import sqlite3
        import stat
        import tempfile
        from pathlib import Path
        from engine import backup_db
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "t.db"
            sqlite3.connect(db).close()
            out = backup_db.backup_once(db, Path(d) / "b", kind="hourly")
            self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
