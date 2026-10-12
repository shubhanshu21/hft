"""
tests/test_market_cooldown.py — Tests market-specific daily loss limit & cooldown logic.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from engine.live_dryrun import DryRunner, _get_market

IST = ZoneInfo("Asia/Kolkata")


class TestMarketClassification(unittest.TestCase):
    def test_equity_symbols_classification(self):
        self.assertEqual(_get_market("RELIANCE"), "equity")
        self.assertEqual(_get_market("TCS"), "equity")
        self.assertEqual(_get_market("INFY"), "equity")
        self.assertEqual(_get_market("HDFCBANK"), "equity")

    def test_currency_symbols_classification(self):
        self.assertEqual(_get_market("USDINR"), "currency")
        self.assertEqual(_get_market("EURINR"), "currency")
        self.assertEqual(_get_market("GBPINR"), "currency")
        self.assertEqual(_get_market("JPYINR"), "currency")

    def test_mcx_symbols_are_not_a_market_any_more(self):
        """MCX commodity was removed 2026-10-12: a commodity symbol is a configuration error, not a silent third market."""
        for sym in ("CRUDEOILM", "GOLDTEN", "SILVERMIC"):
            with self.assertRaises(ValueError):
                _get_market(sym)


class TestMarketCooldownLogic(unittest.TestCase):
    def _create_runner(self):
        db_instance = MagicMock()
        db_instance.get_open_positions.return_value = []
        db_instance.get_account.return_value = {"current_capital": 100_000.0}
        db_instance.get_peak_capital.return_value = 100_000.0

        broker_instance = MagicMock()
        broker_instance.get_market_depth.return_value = None
        broker_instance.get_historical_candles.return_value = []

        with patch("engine.live_dryrun.TradingDB", return_value=db_instance), \
             patch("engine.live_dryrun.UpstoxBroker", return_value=broker_instance), \
             patch("engine.live_dryrun._build_symbol_map", return_value={"USDINR": "CDS_1", "RELIANCE": "NSE_1"}):
            runner = DryRunner(
                broker=broker_instance,
                db=db_instance,
                symbols=["USDINR", "RELIANCE"],
                capital=100_000.0,
                risk_pct=5.0,
                leverage=4.0,
            )
            runner.max_market_daily_loss_pct = 3.0  # 3% loss limit = Rs 3,000
            runner.market_cooldown_minutes = 60
            return runner

    def test_market_cooldown_activation_on_loss(self):
        runner = self._create_runner()
        now = datetime(2026, 9, 22, 11, 0, 0, tzinfo=IST)

        pos_currency = {
            "position_id": "POS_1", "direction": "long", "entry_price": 96.0,
            "lots": 4, "qty": 4, "entry_time": now - timedelta(minutes=30),
            "current_stop": 95.9, "tp": 96.2, "be": 96.05, "best_price": 96.0, "armed_be": False, "strategy": "x"
        }
        runner.positions["USDINR"] = pos_currency
        strat = MagicMock()
        strat.costs.return_value = {"net": -3500.0, "gross": -3400.0, "total": 100.0}
        strat.lot_size.return_value = 1000

        # Simulate closing a currency position with a Rs 3,500 loss (3.5% of 100k capital)
        with patch.object(runner, "_strategy_of", return_value=strat), \
             patch("services.utils.telegram.send"), \
             patch("services.utils.telegram.alert_exit"):
            runner._close_position("USDINR", pos_currency, 95.9, "stop_loss", now)

        # Currency should be on cooldown
        self.assertEqual(runner.market_daily_pnl["currency"], -3500.0)
        self.assertEqual(runner.market_cooldown_until["currency"], now + timedelta(minutes=60))
        self.assertTrue(runner._is_market_on_cooldown("currency", now + timedelta(minutes=10)))

        # Equity must remain active (NOT on cooldown)
        self.assertIsNone(runner.market_cooldown_until["equity"])
        self.assertFalse(runner._is_market_on_cooldown("equity", now + timedelta(minutes=10)))

    def test_market_cooldown_expiration(self):
        runner = self._create_runner()
        now = datetime(2026, 9, 22, 11, 0, 0, tzinfo=IST)
        runner.market_cooldown_until["currency"] = now + timedelta(minutes=60)

        with patch("services.utils.telegram.send"):
            # Before 60 minutes: cooldown is active
            self.assertTrue(runner._is_market_on_cooldown("currency", now + timedelta(minutes=30)))

            # After 60 minutes: cooldown expires automatically
            self.assertFalse(runner._is_market_on_cooldown("currency", now + timedelta(minutes=61)))
            self.assertIsNone(runner.market_cooldown_until["currency"])

    def test_trading_day_rollover_resets_cooldown(self):
        runner = self._create_runner()
        now = datetime(2026, 9, 22, 11, 0, 0, tzinfo=IST)
        runner.market_daily_pnl = {"currency": -4000.0, "equity": -100.0}
        runner.market_cooldown_until = {"currency": now + timedelta(minutes=60), "equity": None}
        runner.trading_day = "2026-09-22"

        # Simulate new day scan call with patched datetime
        next_day = datetime(2026, 9, 23, 9, 15, 0, tzinfo=IST)
        with patch("engine.live_dryrun.datetime") as mock_dt, \
             patch("engine.live_dryrun._fetch_candles", return_value=[]), \
             patch("services.utils.telegram.send"):
            mock_dt.now.return_value = next_day
            mock_dt.strftime = datetime.strftime
            runner.scan()

        self.assertEqual(runner.trading_day, "2026-09-23")
        self.assertEqual(runner.market_daily_pnl, {"currency": 0.0, "equity": 0.0})
        self.assertEqual(runner.market_cooldown_until, {"currency": None, "equity": None})


if __name__ == "__main__":
    unittest.main()
