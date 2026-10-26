"""engine/engine_monitor.py -- alerts when a paper engine stops (2026-10-24). systemctl is faked; state files live in a temp folder."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from engine import engine_monitor as M

IST = M.IST
MON_10 = datetime(2026, 10, 26, 10, 0, tzinfo=IST)          # Monday 10:00 IST
FRI_2340 = datetime(2026, 10, 23, 23, 40, 5, tzinfo=IST)
MON_0910 = datetime(2026, 10, 26, 9, 10, 4, tzinfo=IST)


MON_0930 = datetime(2026, 10, 26, 9, 30, 9, tzinfo=IST)
MON_0940 = datetime(2026, 10, 26, 9, 40, 6, tzinfo=IST)     # the buy-retry slot added 2026-10-26
ALL_TIMERS = ("hft-crypto-dip-reversal.timer", "hft-mcx-trend-carry.timer", "hft-results-gap.timer")


def fake_sh(active=ALL_TIMERS, crypto=("success", None), mcx=("success", MON_0910), rg=("success", MON_0940)):
    def sh(*args):
        if args[0] == "is-active":
            return "active" if args[1] in active else "inactive"
        result, when = crypto if "crypto" in args[1] else rg if "results-gap" in args[1] else mcx
        return f"Result={result}\nExecMainExitTimestamp=@{int(when.timestamp()) if when else 0}"
    return sh


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.crypto = self.dir / "crypto_state.json"
        self.state = self.dir / "monitor_state.json"
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def crypto_saved(self, at):
        self.crypto.write_text(json.dumps({"updated_at": at.isoformat()}))

    def check(self, now, sh=None, token_ok=lambda: True, dry_run=False, journal=None):
        return M.check(now, sh=sh or fake_sh(), crypto_state=self.crypto, token_ok=token_ok, notify=self.sent.append,
                       state_path=self.state, dry_run=dry_run, journal=journal or (lambda cmd: ""))


class TestProblems(Base):
    def test_all_fine_sends_nothing(self):
        self.crypto_saved(MON_10 - timedelta(minutes=3))
        out = self.check(MON_10)
        self.assertEqual((out["problems"], self.sent), ({}, []))

    def test_crypto_not_run_for_a_while(self):
        self.crypto_saved(MON_10 - timedelta(minutes=40))
        self.assertIn("crypto_stale", self.check(MON_10)["problems"])

    def test_missing_crypto_state(self):
        self.assertIn("crypto_stale", self.check(MON_10)["problems"])

    def test_crypto_timer_off_and_failed_run(self):
        self.crypto_saved(MON_10)
        p = self.check(MON_10, sh=fake_sh(active=("hft-mcx-trend-carry.timer", "hft-results-gap.timer"), crypto=("exit-code", MON_10)))["problems"]
        self.assertIn("crypto_timer", p)
        self.assertIn("crypto_failed", p)

    def test_mcx_missed_its_morning_run(self):
        self.crypto_saved(MON_10)
        p = self.check(MON_10, sh=fake_sh(mcx=("success", FRI_2340)))["problems"]
        self.assertIn("mcx_missed", p)
        self.assertIn("09:10", p["mcx_missed"])

    def test_mcx_not_judged_inside_the_grace_period(self):
        now = datetime(2026, 10, 26, 9, 20, tzinfo=IST)
        self.crypto_saved(now)
        self.assertNotIn("mcx_missed", self.check(now, sh=fake_sh(mcx=("success", FRI_2340)))["problems"])

    def test_weekend_counts_back_to_friday_night(self):
        sat = datetime(2026, 10, 24, 12, 0, tzinfo=IST)
        self.crypto_saved(sat)
        self.assertEqual(M.last_mcx_slot(sat), datetime(2026, 10, 23, 23, 40, tzinfo=IST))
        self.assertEqual(self.check(sat, sh=fake_sh(mcx=("success", FRI_2340)))["problems"], {})

    def test_mcx_run_failed(self):
        self.crypto_saved(MON_10)
        self.assertIn("mcx_failed", self.check(MON_10, sh=fake_sh(mcx=("exit-code", MON_0910)))["problems"])

    def test_upstox_login_checked_only_before_an_mcx_run(self):
        before = datetime(2026, 10, 26, 8, 50, tzinfo=IST)
        self.crypto_saved(before)
        self.assertIn("upstox_login", self.check(before, sh=fake_sh(mcx=("success", FRI_2340)), token_ok=lambda: False)["problems"])
        calls = []
        self.check(MON_10, token_ok=lambda: calls.append(1) or False)
        self.assertEqual(calls, [])                          # 10:00 is not within 30 min of an MCX run


class TestAlerts(Base):
    def test_alert_once_remind_after_6h_then_fixed(self):
        self.late = fake_sh(rg=("success", datetime(2026, 10, 26, 15, 20, 9, tzinfo=IST)))   # results-gap ran its 15:20 slot too
        self.crypto_saved(MON_10 - timedelta(hours=2))
        self.check(MON_10, sh=self.late)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Crypto engine has not run", self.sent[0])
        self.check(MON_10 + timedelta(minutes=15), sh=self.late)
        self.assertEqual(len(self.sent), 1)                  # no repeat 15 minutes later
        self.check(MON_10 + timedelta(hours=6, minutes=1), sh=self.late)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("still not fixed", self.sent[1])
        later = MON_10 + timedelta(hours=7)
        self.crypto_saved(later)
        self.check(later, sh=self.late)
        self.assertIn("fixed", self.sent[2])
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_dry_run_sends_and_saves_nothing(self):
        self.crypto_saved(MON_10 - timedelta(hours=2))
        out = self.check(MON_10, dry_run=True)
        self.assertEqual(len(out["sent"]), 1)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state.exists())


class TestFailedRunIsNotMasked(Base):
    """2026-10-26: `systemctl show` only reports the LAST exit, so a failed 09:30 results-gap run disappeared as soon as the 15:20
    run succeeded. The journal keeps every invocation."""

    FAILED = "Failed with result 'exit-code'.\n"

    def test_a_failed_morning_run_is_still_reported_later(self):
        self.crypto_saved(MON_10)
        p = self.check(MON_10, journal=lambda cmd: self.FAILED)["problems"]       # the 15:20 run itself succeeded
        self.assertIn("rg_failed", p)
        self.assertIn("1 failed run", p["rg_failed"])
        self.assertIn("mcx_failed", p)

    def test_the_real_journal_line_with_the_unit_name_first_counts(self):
        # what journalctl -o cat really prints (seen on this server): the unit name, then the message
        real = "hft-results-gap.service: Main process exited, code=exited, status=1/FAILURE\n" \
               "hft-results-gap.service: Failed with result 'exit-code'.\n"
        self.assertEqual(M.failed_runs("hft-results-gap.service", MON_10, run=lambda cmd: real), 1)
        self.crypto_saved(MON_10)
        self.assertIn("rg_failed", self.check(MON_10, journal=lambda cmd: real)["problems"])

    def test_a_clean_journal_raises_nothing(self):
        self.crypto_saved(MON_10)
        p = self.check(MON_10, journal=lambda cmd: "Finished with result: success")["problems"]
        self.assertNotIn("rg_failed", p)
        self.assertNotIn("mcx_failed", p)

    def test_an_unreadable_journal_is_not_an_alarm(self):
        self.crypto_saved(MON_10)

        def boom(cmd):
            raise OSError("no journal")

        self.assertNotIn("rg_failed", self.check(MON_10, journal=boom)["problems"])


class TestEngineStatus(Base):
    def test_crypto_running(self):
        now = datetime(2026, 10, 26, 10, 2, 30, tzinfo=IST)
        self.crypto_saved(now - timedelta(minutes=2))
        st = M.engine_status("CRYPTO_DIP_REVERSAL", now, sh=fake_sh(), crypto_state=self.crypto)
        self.assertTrue(st["ok"])
        self.assertIn("2 min ago", st["text"])
        self.assertIn("next at 10:05", st["text"])

    def test_crypto_stale_and_off(self):
        self.crypto_saved(MON_10 - timedelta(hours=1))
        self.assertFalse(M.engine_status("CRYPTO_DIP_REVERSAL", MON_10, sh=fake_sh(), crypto_state=self.crypto)["ok"])
        off = M.engine_status("CRYPTO_DIP_REVERSAL", MON_10, sh=fake_sh(active=("hft-mcx-trend-carry.timer", "hft-results-gap.timer")), crypto_state=self.crypto)
        self.assertFalse(off["ok"])
        self.assertIn("OFF", off["text"])

    def test_mcx_running_shows_next_slot(self):
        st = M.engine_status("MCX_TREND_CARRY", MON_10, sh=fake_sh(mcx=("success", MON_0910)))
        self.assertTrue(st["ok"])
        self.assertIn("next at Mon 23:40", st["text"])

    def test_mcx_missed(self):
        st = M.engine_status("MCX_TREND_CARRY", MON_10, sh=fake_sh(mcx=("success", FRI_2340)))
        self.assertFalse(st["ok"])
        self.assertIn("missed", st["text"])


class TestResultsGapMonitor(Base):
    def test_rg_running_and_missed(self):
        self.crypto_saved(MON_10)
        self.assertEqual(self.check(MON_10)["problems"], {})
        p = self.check(MON_10 + timedelta(minutes=10), sh=fake_sh(rg=("success", FRI_2340)))["problems"]
        self.assertIn("rg_missed", p)
        self.assertIn("09:40", p["rg_missed"])                     # the retry slot is the last one due by then

    def test_rg_status_text(self):
        st = M.engine_status("RESULTS_GAP", MON_10, sh=fake_sh())
        self.assertTrue(st["ok"])
        self.assertIn("next at Mon 15:20", st["text"])

    def test_rg_never_run_is_not_a_miss(self):
        self.crypto_saved(MON_10)
        self.assertNotIn("rg_missed", self.check(MON_10, sh=fake_sh(rg=("success", None)))["problems"])
        st = M.engine_status("RESULTS_GAP", MON_10, sh=fake_sh(rg=("success", None)))
        self.assertTrue(st["ok"])
        self.assertIn("Waiting for the first run", st["text"])

    def test_rg_no_prices_at_0930_alerts(self):
        self.crypto_saved(MON_10)
        st = self.dir / "results_gap_state.json"
        st.write_text(json.dumps({"entered_on": "2026-10-26", "last_check": {"candidates": 43, "market_gap": None, "signals": []}}))
        self.assertIn("rg_no_prices", self.check(MON_10)["problems"])
        st.write_text(json.dumps({"entered_on": "2026-10-26", "last_check": {"candidates": 43, "market_gap": -0.0035, "signals": []}}))
        self.assertNotIn("rg_no_prices", self.check(MON_10)["problems"])

    def test_rg_no_prices_before_the_retry_slot_is_not_an_alert_yet(self):
        # 09:42: the 09:40 retry may still bring prices back -- the engine alerts by itself on each failed attempt
        self.crypto_saved(MON_10)
        (self.dir / "results_gap_state.json").write_text(json.dumps(
            {"entry_date": "2026-10-26", "entry_attempts": 1, "last_check": {"market_gap": None}}))
        self.assertNotIn("rg_no_prices", self.check(datetime(2026, 10, 26, 9, 42, tzinfo=IST))["problems"])
        self.assertIn("rg_no_prices", self.check(datetime(2026, 10, 26, 9, 46, tzinfo=IST))["problems"])


if __name__ == "__main__":
    unittest.main()
