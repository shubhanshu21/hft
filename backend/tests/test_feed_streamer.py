"""
tests/test_feed_streamer.py — Unit tests for QuoteCache and WebSocket Streaming helpers.
"""
import time
import unittest
from services.broker.feed_streamer import QuoteCache


class TestQuoteCache(unittest.TestCase):

    def setUp(self):
        self.cache = QuoteCache()

    def test_cache_update_and_get_ltp(self):
        self.cache.update("NSE_EQ|INE002A01018", {"ltp": 2500.50, "close": 2490.00})
        ltp = self.cache.get_ltp("NSE_EQ|INE002A01018")
        self.assertEqual(ltp, 2500.50)

    def test_cache_fallback_to_close(self):
        self.cache.update("MCX_FO|12345", {"close": 5800.0})
        ltp = self.cache.get_ltp("MCX_FO|12345")
        self.assertEqual(ltp, 5800.0)

    def test_cache_stale_expiry(self):
        self.cache.update("NSE_EQ|TEST", {"ltp": 100.0})
        # Fresh retrieval works
        self.assertEqual(self.cache.get_ltp("NSE_EQ|TEST", max_age_sec=5.0), 100.0)

        # Force timestamp back in time
        self.cache._last_update_ts["NSE_EQ|TEST"] = time.time() - 20.0
        # Now retrieval with max_age_sec=5.0 should return None (stale)
        self.assertIsNone(self.cache.get_ltp("NSE_EQ|TEST", max_age_sec=5.0))

    def test_cache_missing_key(self):
        self.assertIsNone(self.cache.get_ltp("NON_EXISTENT_KEY"))
        self.assertIsNone(self.cache.get_quote("NON_EXISTENT_KEY"))


if __name__ == "__main__":
    unittest.main()
