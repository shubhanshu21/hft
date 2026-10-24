"""The nightly job must run every step even when one fails, and must notice a stale archive."""
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from engine import nightly


class TestRunSteps(unittest.TestCase):
    def _steps(self):
        return [("a", ["a"], {0}), ("b", ["b"], {0}), ("c", ["c"], {0}), ("audit", ["audit"], {0, 1})]

    def test_a_failing_step_does_not_skip_the_ones_after_it(self):
        ran = []
        def run(argv, timeout=None):
            ran.append(argv[0])
            return SimpleNamespace(returncode=2 if argv[0] == "a" else 0)
        failed = nightly.run_steps(self._steps(), run=run, log=lambda *_: None)
        self.assertEqual(ran, ["a", "b", "c", "audit"])          # the old one-ExecStart-per-step unit would have stopped after "a"
        self.assertEqual(failed, ["a"])

    def test_a_timeout_or_a_crash_counts_as_failed_and_the_rest_still_run(self):
        def run(argv, timeout=None):
            if argv[0] == "a":
                raise subprocess.TimeoutExpired(argv, 1)
            if argv[0] == "b":
                raise FileNotFoundError("no python")
            return SimpleNamespace(returncode=0)
        self.assertEqual(nightly.run_steps(self._steps(), run=run, log=lambda *_: None), ["a", "b"])

    def test_the_audit_exiting_1_means_flagged_trades_not_a_failed_job(self):
        run = lambda argv, timeout=None: SimpleNamespace(returncode=1 if argv[0] == "audit" else 0)
        self.assertEqual(nightly.run_steps(self._steps(), run=run, log=lambda *_: None), [])
        run = lambda argv, timeout=None: SimpleNamespace(returncode=3 if argv[0] == "audit" else 0)
        self.assertEqual(nightly.run_steps(self._steps(), run=run, log=lambda *_: None), ["audit"])


class TestFreshness(unittest.TestCase):
    def _root(self, newest):
        root = Path(tempfile.mkdtemp())
        (root / "commodity").mkdir()
        (root / "commodity" / "X_1minute.csv").write_text(f"timestamp,open,high,low,close,volume\n{newest}T10:00:00+05:30,1,1,1,1,1\n{newest}T23:29:00+05:30,1,1,1,1,1\n")
        return root

    FILES = [("commodity", "X_1minute.csv")]

    def test_last_trading_day_skips_weekends_and_holidays(self):
        self.assertEqual(nightly.last_trading_day(date(2026, 9, 28), set()), date(2026, 9, 25))                 # Monday -> Friday
        self.assertEqual(nightly.last_trading_day(date(2026, 9, 25), {date(2026, 9, 24)}), date(2026, 9, 23))   # holiday skipped

    def test_one_trading_day_of_lag_is_tolerated_two_is_not(self):
        today, none = date(2026, 9, 24), set()            # expected = Sep 23; tolerated newest >= Sep 22
        self.assertEqual(nightly.stale_archives(today, none, self.FILES, self._root("2026-09-23")), [])
        self.assertEqual(nightly.stale_archives(today, none, self.FILES, self._root("2026-09-22")), [])
        self.assertEqual(len(nightly.stale_archives(today, none, self.FILES, self._root("2026-09-21"))), 1)

    def test_a_weekend_is_not_stale(self):
        self.assertEqual(nightly.stale_archives(date(2026, 9, 28), set(), self.FILES, self._root("2026-09-24")), [])   # Mon: Fri expected, Thu ok

    def test_a_missing_file_is_reported(self):
        out = nightly.stale_archives(date(2026, 9, 24), set(), [("commodity", "NOPE.csv")], self._root("2026-09-23"))
        self.assertIn("unreadable or missing", out[0])

    def test_newest_candle_date_reads_only_the_tail(self):
        self.assertEqual(nightly.newest_candle_date(self._root("2026-09-23") / "commodity" / "X_1minute.csv"), date(2026, 9, 23))


if __name__ == "__main__":
    unittest.main()
