"""Drive the daemon's real main() loop for a few scans with a fake broker and a controlled clock.

Recent bugs lived in the loop's ORDER and timing, which unit tests of the pieces cannot see: the hourly margin refresh ran before the token check (a wall of
401s at the open), and a failed login waited a full check interval. This runs the actual loop and asserts the sequence of calls."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

IST = timezone(timedelta(hours=5, minutes=30))
SIM_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=IST)                     # a Thursday, mid-session for every market


class Stop(BaseException):
    """Ends the loop after N sleeps (main() treats KeyboardInterrupt as a clean stop; this is raised through the same door)."""


def run_main(token_results, scans, env=None):
    """Returns (call_log, heartbeat_phases, scan_count). `token_results`: statuses check_token returns in order (the last one repeats)."""
    from engine import heartbeat, live_dryrun
    from services.broker.upstox_broker import token_invalid_event

    tmp = Path(tempfile.mkdtemp())
    log, beats, clock, sleeps = [], [], {"t": 1000.0}, {"n": 0}
    results = list(token_results)

    class SimDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return SIM_NOW if tz is None else SIM_NOW.astimezone(tz)

    def fake_check_token(broker):
        log.append("check_token")
        return results.pop(0) if len(results) > 1 else results[0]

    def fake_sleep(secs):
        sleeps["n"] += 1
        clock["t"] += max(secs, 0)
        if sleeps["n"] >= scans:
            raise KeyboardInterrupt()                                      # main() handles this as a normal stop

    fake_time = SimpleNamespace(monotonic=lambda: clock["t"], sleep=fake_sleep, time=lambda: clock["t"])
    tg = SimpleNamespace(send=lambda *a, **k: log.append("telegram"), get_updates=lambda **k: [], alert_error=lambda *a, **k: None,
                         send_photo=lambda *a, **k: None, is_authorized_chat=lambda c: False, alert_entry=lambda *a, **k: None,
                         alert_exit=lambda *a, **k: None)
    scan_count = {"n": 0}

    def fake_scan(self):
        log.append("scan")
        scan_count["n"] += 1
        return []

    base_env = {"DRYRUN_INCLUDE_EQUITY": "false", "TOKEN_CHECK_INTERVAL_MIN": "10", "TOKEN_RETRY_SEC": "30", "TOKEN_MAX_FAST_RETRIES": "5",
                "MARGIN_REFRESH_MIN": "1", "MARGIN_VERIFY": "off", "USE_COMMODITY_REGIME_FILTER": "false", "USE_EQUITY_REGIME_FILTER": "false"}
    base_env.update(env or {})
    argv = ["live_dryrun", "--db", str(tmp / "t.db"), "--interval", "30", "--symbols", "CRUDEOILM", "USDINR", "--account", "SMOKE"]
    patches = [
        patch.object(sys, "argv", argv), patch.dict("os.environ", base_env),
        patch.object(live_dryrun, "setup_logger"), patch.object(live_dryrun, "_acquire_process_lock", return_value=object()),
        patch.object(live_dryrun, "_load_token", return_value="tok"), patch("services.auth.upstox_auto_login._token_is_valid", return_value=True),
        patch.object(live_dryrun, "UpstoxBroker", lambda access_token, dry_run: MagicMock()),
        patch.object(live_dryrun, "datetime", SimDatetime), patch.object(live_dryrun, "time", fake_time), patch.object(live_dryrun, "telegram", tg),
        patch.object(live_dryrun, "get_trading_holidays", lambda: set()), patch.object(live_dryrun, "check_token", fake_check_token),
        patch.object(live_dryrun, "refresh_market_hours", lambda *a, **k: log.append("refresh_hours")),
        patch.object(live_dryrun, "refresh_margin_rates", lambda *a, **k: log.append("refresh_margin")),
        patch.object(live_dryrun, "check_cost_drift", lambda *a, **k: log.append("cost_drift")),
        patch.object(live_dryrun.DryRunner, "scan", fake_scan), patch.object(heartbeat, "HEARTBEAT_PATH", tmp / "hb.json"),
        patch.object(heartbeat, "beat", lambda phase, nxt=0.0, **k: beats.append(phase)), patch("signal.signal"),
        patch("services.utils.chart.generate_equity_curve", return_value=None),
    ]
    from contextlib import ExitStack
    with ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        token_invalid_event.set()                                           # make the first iteration check the token, as after a 401
        try:
            live_dryrun.main()
        except (KeyboardInterrupt, SystemExit):
            pass
    return log, beats, scan_count["n"]


class TestDaemonMainLoop(unittest.TestCase):
    def test_the_token_is_checked_before_the_hourly_refresh_and_before_the_scan(self):
        # both the token check and the hourly refresh come due on the same iteration (60 s intervals, 30 s scans): the token must be checked FIRST
        log, _, _ = run_main(["valid"], scans=4, env={"TOKEN_CHECK_INTERVAL_MIN": "1", "MARGIN_REFRESH_MIN": "1"})
        loop = log[log.index("scan"):]                                          # everything after the first scan (the start-up one-offs are before it)
        i = loop.index("refresh_margin")
        self.assertEqual(loop[i - 2:i + 2], ["check_token", "refresh_hours", "refresh_margin", "scan"])        # it used to run refresh first: a wall of 401s at the open
        self.assertLess(log.index("check_token"), log.index("scan"))            # and the very first scan follows a token check (the 401 event)

    def test_startup_does_the_one_off_checks_once_and_before_any_scan(self):
        log, _, _ = run_main(["valid"], scans=2)
        for name in ("refresh_hours", "refresh_margin", "cost_drift"):
            self.assertIn(name, log)
        self.assertLess(log.index("cost_drift"), log.index("scan"))

    def test_a_dead_token_skips_the_refresh_and_retries_quickly_but_only_a_few_times(self):
        log, _, _ = run_main(["failed"], scans=10)
        # no margin/hours refresh on a dead token (after the start-up one): everything after the first scan
        after_start = log[log.index("scan"):]
        self.assertNotIn("refresh_margin", after_start)
        self.assertEqual(log.count("check_token"), 6)                          # 5 fast retries (30 s) then back to the 10-minute interval
        self.assertEqual(log.count("scan"), 10)                                # scanning continues meanwhile: exits are still managed

    def test_a_recovered_token_returns_to_the_normal_interval_and_refreshes_resume(self):
        log, _, _ = run_main(["failed", "failed", "valid"], scans=8)
        self.assertEqual(log.count("check_token"), 3)                          # two failed attempts, one success, then quiet for the 10-minute interval
        self.assertIn("refresh_margin", log[log.index("scan"):])                # and the hourly refresh is back

    def test_the_heartbeat_is_written_every_scan_and_before_every_sleep(self):
        _, beats, scans = run_main(["valid"], scans=3)
        self.assertEqual(scans, 3)
        self.assertEqual(beats, ["scan", "sleep"] * 3)                          # a scan beat, then a sleep beat promising the next one: what the watchdog reads


if __name__ == "__main__":
    unittest.main()
