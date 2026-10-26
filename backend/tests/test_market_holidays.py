"""services/utils/market_holidays.py -- decides whether a live engine is allowed to trade today.

Three live engines (results_gap, mcx_trend_carry, preopen_recorder) gate on this, so the failure mode matters as much as the happy
path: a miss means a trade on a closed exchange, a wrong "holiday" means a silently skipped trading day. No network here.
"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from services.utils import market_holidays as mh


def _entry(day, kind="TRADING_HOLIDAY", exchanges=("NSE",)):
    return {"date": day, "holiday_type": kind, "closed_exchanges": list(exchanges)}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name) / "market_holidays.json"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(mh, "_CACHE_PATH", self.cache).start()

    def write_cache(self, data, fetched_at=None):
        self.cache.write_text(json.dumps({"fetched_at": (fetched_at or datetime.now()).isoformat(), "data": data}))

    def fetched(self, data):
        return mock.patch.object(mh, "_fetch_from_upstox", return_value=data)


class TestFiltering(Base):
    def test_only_full_trading_holidays_count(self):
        self.write_cache([_entry("2026-10-02"), _entry("2026-10-03", kind="SETTLEMENT_HOLIDAY"), _entry("2026-10-04", kind="SPECIAL_TIMING")])
        self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 2)})

    def test_either_nse_or_cds_closing_counts_but_a_bse_only_holiday_does_not(self):
        self.write_cache([_entry("2026-10-02", exchanges=["NSE"]), _entry("2026-10-03", exchanges=["CDS"]),
                          _entry("2026-10-04", exchanges=["BSE"]), _entry("2026-10-05", exchanges=["NSE", "BSE"])])
        self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 5)})

    def test_a_malformed_entry_is_skipped_not_fatal(self):
        self.write_cache([{"holiday_type": "TRADING_HOLIDAY", "closed_exchanges": ["NSE"]},                       # no date
                          {"date": "not-a-date", "holiday_type": "TRADING_HOLIDAY", "closed_exchanges": ["NSE"]},
                          {"date": "2026-10-02", "holiday_type": "TRADING_HOLIDAY", "closed_exchanges": None},   # exchange list is null
                          "a string, not an entry",
                          _entry("2026-10-05")])
        self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 5)})

    def test_a_null_exchange_list_does_not_switch_the_whole_calendar_off(self):
        # It used to raise TypeError; every engine calls this inside try/except, so one such row silently traded on holidays all day.
        self.write_cache([_entry("2026-10-06", exchanges=[]), {"date": "2026-10-07", "holiday_type": "TRADING_HOLIDAY", "closed_exchanges": None},
                          _entry("2026-10-08")])
        self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 8)})


class TestCaching(Base):
    def test_a_fresh_cache_means_no_network_call(self):
        self.write_cache([_entry("2026-10-02")])
        with self.fetched(None) as fetch:
            self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 2)})
        fetch.assert_not_called()

    def test_a_stale_cache_is_refreshed_and_rewritten(self):
        self.write_cache([_entry("2026-10-02")], fetched_at=datetime.now() - timedelta(hours=25))
        with self.fetched([_entry("2026-10-03")]) as fetch:
            self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 3)})
        fetch.assert_called_once_with()
        self.assertEqual(json.loads(self.cache.read_text())["data"], [_entry("2026-10-03")])

    def test_a_year_rollover_forces_a_refresh_even_if_fetched_minutes_ago(self):
        self.write_cache([_entry("2026-10-02")], fetched_at=datetime(date.today().year - 1, 12, 31, 23, 59))
        with self.fetched([_entry("2026-10-03")]) as fetch:
            self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 3)})
        fetch.assert_called_once_with()

    def test_force_refresh_ignores_a_fresh_cache(self):
        self.write_cache([_entry("2026-10-02")])
        with self.fetched([_entry("2026-10-03")]) as fetch:
            self.assertEqual(mh.get_trading_holidays(force_refresh=True), {date(2026, 10, 3)})
        fetch.assert_called_once_with()

    def test_a_corrupt_cache_is_treated_as_no_cache(self):
        self.cache.write_text("{not json")
        with self.fetched([_entry("2026-10-02")]):
            self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 2)})

    def test_a_cache_missing_its_fields_is_treated_as_no_cache(self):
        for body in ('[]', '{"data": null, "fetched_at": "%s"}' % datetime.now().isoformat(), '{"data": []}'):
            self.cache.write_text(body)
            with self.fetched([_entry("2026-10-02")]) as fetch:
                self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 2)})
            fetch.assert_called_once_with()


class TestFailsSafe(Base):
    def test_no_cache_and_a_failed_fetch_yields_no_holidays_instead_of_crashing(self):
        # Weekday-only gating: the engine keeps running, it just cannot know about holidays. Crashing a live loop would be worse.
        with self.fetched(None):
            self.assertEqual(mh.get_trading_holidays(), set())

    def test_a_failed_refresh_still_uses_the_stale_cache(self):
        self.write_cache([_entry("2026-10-02")], fetched_at=datetime.now() - timedelta(hours=25))
        with self.fetched(None):
            self.assertEqual(mh.get_trading_holidays(), {date(2026, 10, 2)})

    def test_a_network_error_inside_the_fetcher_returns_none(self):
        with mock.patch.object(mh.requests, "get", side_effect=OSError("boom")):
            self.assertIsNone(mh._fetch_from_upstox())

    def test_a_non_success_payload_returns_none(self):
        with mock.patch.object(mh.requests, "get", return_value=mock.Mock(raise_for_status=lambda: None, json=lambda: {"status": "error"})):
            self.assertIsNone(mh._fetch_from_upstox())


if __name__ == "__main__":
    unittest.main()
