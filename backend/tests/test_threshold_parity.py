import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import markets.commodity.scalping.backtest as backtest_commodity
import markets.currency.scalping.backtest as backtest_currency
import markets.commodity.scalping.entry_signal as entry_signal
import engine.live_dryrun as live_dryrun
import engine.live_trading as live_trading
class TestThresholdParity(unittest.TestCase):
    """live_dryrun.py (and, since 2026-09-18, live_trading.py) used to duplicate
    markets/commodity/scalping/backtest.py's/backtest_currency.py's ENTRY_THRESHOLDS as hand-copied
    literals, with only a comment ("keep these two/three in sync by hand") standing
    between them and silent drift -- exactly the kind of bug that let a currency
    EOD-squareoff mismatch and gold's own stale thresholds ship unnoticed earlier in
    this project. Fixed by routing everything through markets.commodity.scalping.entry_signal, which
    imports the dicts directly rather than duplicating them, and is itself the one
    place both live_dryrun.py's DryRunner and live_trading.py's LiveTrader call for
    entry decisions. These are identity checks (`is`), not equality checks --
    equality would still pass if someone reintroduced a second, independently-
    maintained copy; identity proves there is only ever one dict in memory."""

    def test_commodity_thresholds_are_the_same_object(self):
        self.assertIs(entry_signal.COMMODITY_ENTRY_THRESHOLDS, backtest_commodity.ENTRY_THRESHOLDS)

    def test_currency_thresholds_are_the_same_object(self):
        self.assertIs(entry_signal.CURRENCY_ENTRY_THRESHOLDS, backtest_currency.ENTRY_THRESHOLDS)

    def test_currency_min_orb_is_the_same_value(self):
        self.assertEqual(entry_signal.CURRENCY_MIN_ORB, backtest_currency._MIN_ORB)

    def test_live_dryrun_and_live_trading_use_the_same_entry_signal_function(self):
        self.assertIs(live_dryrun.compute_entry_signal, entry_signal.compute_entry_signal)
        self.assertIs(live_trading.compute_entry_signal, entry_signal.compute_entry_signal)

    def test_all_live_traded_commodity_symbols_have_dedicated_thresholds(self):
        # Anything live_dryrun.py's is_natgas/is_gold/is_silver detection can match
        # must resolve to a real ENTRY_THRESHOLDS key, or it silently falls through
        # to "crude" -- which is correct for actual crude, but would be silently
        # wrong for a new symbol added to DRYRUN_SYMBOLS without a matching branch.
        for key in ("crude", "natgas", "gold", "silver"):
            self.assertIn(key, backtest_commodity.ENTRY_THRESHOLDS)
            for field in ("min_adx", "min_vol", "min_vwap", "min_ema_slope",
                          "min_stop_pct", "min_orb", "tp_mult", "stop_mult"):
                self.assertIn(field, backtest_commodity.ENTRY_THRESHOLDS[key])

    def test_all_live_traded_currency_pairs_have_dedicated_thresholds(self):
        for pair in ("USDINR", "EURINR", "GBPINR"):  # JPYINR intentionally excluded -- not live
            self.assertIn(pair, backtest_currency.ENTRY_THRESHOLDS)
            for field in ("min_adx", "min_vol", "min_vwap", "min_ema_slope",
                          "min_stop_pct", "tp_mult", "stop_mult"):
                self.assertIn(field, backtest_currency.ENTRY_THRESHOLDS[pair])


if __name__ == "__main__":
    unittest.main()
