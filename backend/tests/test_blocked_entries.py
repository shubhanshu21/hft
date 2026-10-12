"""Signals that could not become trades must leave a trace: 'does one open position block another?' has to be answerable from data."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import blocked_log


class TestBlockedLog(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "b.jsonl"
        blocked_log._seen.clear()
        self.addCleanup(blocked_log._seen.clear)
        self.holding = [{"symbol": "EURINR", "margin_used": 83000.4}]

    def _rec(self, sym="GBPINR", bar="2026-09-25T11:15:00+05:30", reason="margin_pool", holding=None):
        return blocked_log.record(sym, "currency", reason, "free margin Rs17,000 of Rs100,000", self.holding if holding is None else holding, bar_ts=bar,
                                  direction="long", wanted_qty=3, got_qty=0, path=self.path)

    def test_a_blocked_signal_is_written_with_what_held_the_margin(self):
        self.assertTrue(self._rec())
        (row,) = blocked_log.read(path=self.path)
        self.assertEqual((row["symbol"], row["reason"], row["wanted_qty"], row["got_qty"]), ("GBPINR", "margin_pool", 3, 0))
        self.assertEqual(row["holding"], [{"symbol": "EURINR", "margin": 83000}])

    def test_the_same_signal_on_the_same_bar_is_recorded_once_not_every_30_seconds(self):
        self.assertTrue(self._rec())
        self.assertFalse(self._rec())
        self.assertFalse(self._rec())
        self.assertTrue(self._rec(bar="2026-09-25T11:20:00+05:30"))              # a new bar is a new signal
        self.assertTrue(self._rec(reason="margin_cut"))                          # a different reason on the same bar is separate information
        self.assertEqual(len(blocked_log.read(path=self.path)), 3)

    def test_summary_names_the_blocker(self):
        for i in range(3):
            self._rec(bar=f"b{i}")
        self._rec(sym="USDINR", bar="x", holding=[{"symbol": "GBPINR", "margin_used": 30000}])
        s = blocked_log.summarize(blocked_log.read(path=self.path))
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["by_symbol"][0], {"symbol": "GBPINR", "n": 3})
        self.assertEqual(s["by_blocker"][0], {"blocker": "EURINR", "n": 3})       # EURINR held the margin for three of them
        self.assertEqual(s["recent"][0]["symbol"], "USDINR")                            # newest first

    def test_a_missing_file_and_bad_lines_are_harmless_and_a_write_failure_never_raises(self):
        self.assertEqual(blocked_log.read(path=Path("/no/such/file")), [])
        self.path.write_text('{"symbol": "X", "holding": []}\nnot json\n')
        self.assertEqual(len(blocked_log.read(path=self.path)), 1)
        self.assertFalse(blocked_log.record("X", "equity", "r", "d", [], path=Path("/no/such/dir/b.jsonl")))


if __name__ == "__main__":
    unittest.main()
