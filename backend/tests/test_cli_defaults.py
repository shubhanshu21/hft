"""cli.py: report/status default to a live paper account (DRYRUN_ACCOUNT was removed 2026-10-21), and reset-db -- which wipes EVERY
account in the DB -- refuses to run without an explicit --account."""
import sys
import unittest
from unittest import mock

import cli


def _run(*argv):
    with mock.patch.object(sys, "argv", ["cli.py", *argv]):
        cli.main()


class TestCliDefaults(unittest.TestCase):
    def test_report_and_status_default_to_a_live_account(self):
        for cmd, fn in (("report", "cmd_report"), ("status", "cmd_status")):
            with mock.patch.object(cli, fn) as f:
                _run(cmd)
            self.assertEqual(f.call_args[0][0].account, "CRYPTO_DIP_REVERSAL", cmd)

    def test_reset_db_needs_an_explicit_account(self):
        with mock.patch.object(cli, "cmd_reset_db") as f, mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                _run("reset-db")
        f.assert_not_called()

    def test_reset_db_with_account_reaches_the_command(self):
        with mock.patch.object(cli, "cmd_reset_db") as f:
            _run("reset-db", "--account", "MCX_TREND_CARRY")
        self.assertEqual(f.call_args[0][0].account, "MCX_TREND_CARRY")


if __name__ == "__main__":
    unittest.main()
