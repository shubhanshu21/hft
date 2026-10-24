"""
tests/test_order_manager.py — Unit tests for SmartOrderManager.
"""
import unittest
from unittest.mock import MagicMock
from services.broker.order_manager import SmartOrderManager


class TestSmartOrderManager(unittest.TestCase):

    def setUp(self):
        self.mock_broker = MagicMock()
        self.mock_broker.dry_run = True
        self.manager = SmartOrderManager(self.mock_broker)

    def test_dryrun_smart_entry(self):
        order_id, fill_price = self.manager.execute_smart_entry(
            symbol="RELIANCE",
            instrument_key="NSE_EQ|INE002A01018",
            direction="long",
            quantity=10,
            target_price=2500.0,
            best_bid=2499.5,
            best_ask=2500.5,
        )
        self.assertIsNotNone(order_id)
        self.assertTrue(order_id.startswith("SIM_"))
        self.assertEqual(fill_price, 2500.0)

    def test_dryrun_place_broker_stop_loss(self):
        sl_order_id = self.manager.place_broker_stop_loss(
            symbol="CRUDEOILM",
            instrument_key="MCX_FO|12345",
            position_direction="long",
            quantity=1,
            stop_price=5900.0,
        )
        self.assertIsNotNone(sl_order_id)
        self.assertTrue(sl_order_id.startswith("SIM_SL_"))

    def test_live_market_order_fill_success(self):
        self.mock_broker.dry_run = False
        self.mock_broker.place_buy_order.return_value = "ORD_12345"
        self.mock_broker.get_order_status.return_value = "complete"
        self.mock_broker.get_fill_price.return_value = 2502.0

        order_id, fill_price = self.manager.execute_smart_entry(
            symbol="RELIANCE",
            instrument_key="NSE_EQ|INE002A01018",
            direction="long",
            quantity=10,
            target_price=2500.0,
            timeout_sec=2.0,
        )
        self.assertEqual(order_id, "ORD_12345")
        self.assertEqual(fill_price, 2502.0)


if __name__ == "__main__":
    unittest.main()
