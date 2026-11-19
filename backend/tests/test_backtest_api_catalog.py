"""After the generic daily-bar framework was removed (2026-11-10): the website offers exactly the live strategies (breakout_pair back 2026-11-17) and refuses
anything else up front, instead of reaching code that no longer exists."""
import unittest

from engine import backtest_api as api


class CatalogTest(unittest.TestCase):
    def test_only_the_live_strategies_are_offered(self):
        self.assertEqual({(r["market"], r["name"]) for r in api.catalog()},
                         {("commodity", "trend_carry"), ("crypto", "dip_reversal"), ("equity", "results_gap"), ("equity", "earnings_drift"),
                          ("equity", "breakout_pair"), ("equity", "momentum_12_1")})   # momentum: half of EQUITY_BREAKOUT since 2026-11-19

    def test_an_old_generic_strategy_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Unknown market or strategy"):
            api.validate({"market": "equity", "strategy": "trend_ema_cross", "start": "2024-01-01", "end": "2025-01-01"})

    def test_the_crypto_page_lists_nine_coins(self):
        row = next(r for r in api.catalog() if r["name"] == "dip_reversal")
        self.assertNotIn("DOT", row["default_symbols"])
        self.assertEqual(len(row["default_symbols"]), 9)

    def test_breakout_pair_takes_money_risk_slippage_and_dates(self):
        run = api.validate({"market": "equity", "strategy": "breakout_pair", "capital": 100000, "risk_pct": 2, "slippage_bp": 5,
                            "start": "2024-01-01", "end": "2025-01-01"})
        self.assertEqual((run["strategy"], run["params"]["risk_pct"], run["params"]["capital"]), ("breakout_pair", 2.0, 100000.0))


if __name__ == "__main__":
    unittest.main()
