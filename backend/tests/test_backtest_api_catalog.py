"""After the generic daily-bar framework was removed (2026-11-10): the website offers exactly the four live strategies and refuses
anything else up front, instead of reaching code that no longer exists."""
import unittest

from engine import backtest_api as api


class CatalogTest(unittest.TestCase):
    def test_only_the_live_strategies_are_offered(self):
        self.assertEqual({(r["market"], r["name"]) for r in api.catalog()},
                         {("commodity", "trend_carry"), ("crypto", "dip_reversal"), ("equity", "results_gap"), ("equity", "earnings_drift")})

    def test_an_old_generic_strategy_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Unknown market or strategy"):
            api.validate({"market": "equity", "strategy": "trend_ema_cross", "start": "2024-01-01", "end": "2025-01-01"})

    def test_the_crypto_page_lists_nine_coins(self):
        row = next(r for r in api.catalog() if r["name"] == "dip_reversal")
        self.assertNotIn("DOT", row["default_symbols"])
        self.assertEqual(len(row["default_symbols"]), 9)


if __name__ == "__main__":
    unittest.main()
