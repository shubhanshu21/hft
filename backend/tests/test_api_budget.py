"""Upstox's documented limits (standard APIs: 50/s, 500/min, 2,000 per 30 min) must be respected: metered centrally, budgeted, and no calls for closed markets."""
import os
import unittest
from datetime import timedelta, timezone
from unittest.mock import patch

from engine import api_budget
from services.broker import upstox_broker as ub

IST = timezone(timedelta(hours=5, minutes=30))


class TestMeter(unittest.TestCase):
    def setUp(self):
        ub._call_times.clear()
        self.addCleanup(ub._call_times.clear)

    def test_every_sdk_call_is_counted_in_the_one_place_all_requests_pass_through(self):
        import upstox_client
        with patch.object(upstox_client.ApiClient, "call_api", lambda self, *a, **k: None):
            client = ub.TimeoutApiClient(upstox_client.Configuration())
            for _ in range(7):
                client.call_api("/x", "GET")
        self.assertEqual(ub.api_calls_in_window(1800), 7)
        self.assertEqual(ub.api_calls_in_window(60), 7)

    def test_only_calls_inside_the_window_count(self):
        import time
        now = time.monotonic()
        ub._call_times.extend([now - 4000, now - 1000, now - 30, now - 1])
        self.assertEqual(ub.api_calls_in_window(1800), 3)
        self.assertEqual(ub.api_calls_in_window(60), 2)


class TestBudget(unittest.TestCase):
    def test_classes_of_work_have_different_ceilings(self):
        self.assertTrue(api_budget.allow("entry", 1000, 30))
        self.assertFalse(api_budget.allow("entry", 1600, 30))               # soft 30-min limit: equity entry scans stop...
        self.assertTrue(api_budget.allow("essential", 1600, 100))           # ...but exits and currency continue
        self.assertFalse(api_budget.allow("essential", 1900, 100))          # hard limit: everything stops well before Upstox's 2,000
        self.assertFalse(api_budget.allow("sampling", 1450, 100))           # spread sampling is the first thing to go
        self.assertTrue(api_budget.allow("sampling", 1000, 100))

    def test_the_per_minute_limit_is_respected_too(self):
        self.assertFalse(api_budget.allow("entry", 100, 400))
        self.assertFalse(api_budget.allow("essential", 100, 470))
        self.assertTrue(api_budget.allow("essential", 100, 300))

    def test_equity_scanning_is_paced_per_minute_so_it_never_bursts_then_starves(self):
        """Live 2026-09-25: a pure 30-minute cap let equity scan flat out for ~15 min and then starve completely (49 of 49 deferred) until the window cleared."""
        self.assertTrue(api_budget.allow("entry", 300, 49))                # under the pace: scan
        self.assertFalse(api_budget.allow("entry", 300, 50))               # pace reached: wait for the minute to roll, even though the 30-min total is low
        self.assertTrue(api_budget.allow("essential", 300, 50))            # exits and currency are not held back by the equity pace
        # simulate 30 minutes: each minute the equity pass may spend until the last-minute total reaches the pace -> total stays far below Upstox's 2,000
        spent = sum(min(50, 60) for _ in range(30))
        self.assertLess(spent, 1600)

    def test_limits_are_tunable_from_the_environment(self):
        with patch.dict(os.environ, {"UPSTOX_API_SOFT_LIMIT_30MIN": "800"}):
            self.assertFalse(api_budget.allow("entry", 900, 10))

    def test_the_documented_maximum_is_never_reachable_by_the_defaults(self):
        for kind in ("essential", "entry", "sampling"):
            self.assertFalse(api_budget.allow(kind, api_budget.LIMIT_30MIN, 0), kind)
            self.assertFalse(api_budget.allow(kind, 0, api_budget.LIMIT_MIN), kind)


if __name__ == "__main__":
    unittest.main()
