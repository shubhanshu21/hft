"""Signals that could not become trades must leave a trace: 'does crude block silver?' has to be answerable from data."""
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
        self.holding = [{"symbol": "CRUDEOILM", "margin_used": 83000.4}]

    def _rec(self, sym="SILVERMIC", bar="2026-09-25T11:15:00+05:30", reason="margin_pool", holding=None):
        return blocked_log.record(sym, "commodity", reason, "free margin Rs17,000 of Rs100,000", self.holding if holding is None else holding, bar_ts=bar,
                                  direction="long", wanted_qty=3, got_qty=0, path=self.path)

    def test_a_blocked_signal_is_written_with_what_held_the_margin(self):
        self.assertTrue(self._rec())
        (row,) = blocked_log.read(path=self.path)
        self.assertEqual((row["symbol"], row["reason"], row["wanted_qty"], row["got_qty"]), ("SILVERMIC", "margin_pool", 3, 0))
        self.assertEqual(row["holding"], [{"symbol": "CRUDEOILM", "margin": 83000}])

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
        self._rec(sym="USDINR", bar="x", holding=[{"symbol": "SILVERMIC", "margin_used": 30000}])
        s = blocked_log.summarize(blocked_log.read(path=self.path))
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["by_symbol"][0], {"symbol": "SILVERMIC", "n": 3})
        self.assertEqual(s["by_blocker"][0], {"blocker": "CRUDEOILM", "n": 3})       # crude held the margin for three of them
        self.assertEqual(s["recent"][0]["symbol"], "USDINR")                            # newest first

    def test_a_missing_file_and_bad_lines_are_harmless_and_a_write_failure_never_raises(self):
        self.assertEqual(blocked_log.read(path=Path("/no/such/file")), [])
        self.path.write_text('{"symbol": "X", "holding": []}\nnot json\n')
        self.assertEqual(len(blocked_log.read(path=self.path)), 1)
        self.assertFalse(blocked_log.record("X", "equity", "r", "d", [], path=Path("/no/such/dir/b.jsonl")))


@unittest.skipUnless(os.environ.get("RUN_REPLAY"), "slow: runs the real scan loop; set RUN_REPLAY=1")
class TestTheRealScanLoopRecordsBlockedSignals(unittest.TestCase):
    """Real DryRunner.scan over recorded market data with a tight margin pool: an open position must leave later signals recorded as blocked."""

    def test_signals_that_cannot_be_sized_to_the_free_margin_are_recorded(self):
        import tests.replay_harness as h
        blocked_log._seen.clear()
        path = Path(tempfile.mkdtemp()) / "b.jsonl"
        tight = dict(h.ENV, MAX_MARGIN_UTILIZATION_PCT="100.0", MAX_MARKET_MARGIN_UTILIZATION_PCT="100.0")
        with patch.object(h, "ENV", tight), patch.object(blocked_log, "PATH", path):
            out = h.run_replay(days=h.QUICK_DAYS, instruments=[i for i in h.INSTRUMENTS if i[2] in ("CRUDEOILM", "TATASTEEL", "SBIN", "JSWSTEEL", "USDINR")])
        rows = blocked_log.read(path=path)
        self.assertGreater(len(out["trades"]), 0)
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(r["reason"] in ("margin_pool", "margin_cut", "margin_check") and r["holding"] for r in rows if r["reason"] == "margin_pool"))
        # every recorded pool block names the position that was holding the margin, and never the symbol itself
        self.assertTrue(all(r["symbol"] not in [h["symbol"] for h in r["holding"]] for r in rows if r["reason"] == "margin_pool"))


if __name__ == "__main__":
    unittest.main()
