"""
tests/test_crypto_costs.py — Pins the Binance USDT-M Futures fee constants and
sizing/cost math in strategy/crypto_costs.py, mirroring how
tests/test_scalper_pipeline.py pins the equity fee constants.
"""
from __future__ import annotations

import unittest

from strategy.crypto_costs import (
    BINANCE_TAKER_FEE_PCT,
    BINANCE_MAKER_FEE_PCT,
    DEFAULT_FUNDING_RATE_PCT,
    compute_binance_futures_costs,
    size_crypto_position,
    get_qty_step,
)


class TestCryptoCosts(unittest.TestCase):

    def test_fee_constants(self):
        self.assertEqual(BINANCE_TAKER_FEE_PCT, 0.05)
        self.assertEqual(BINANCE_MAKER_FEE_PCT, 0.02)
        self.assertEqual(DEFAULT_FUNDING_RATE_PCT, 0.01)

    def test_qty_step(self):
        self.assertEqual(get_qty_step("BTCUSDT"), 0.001)
        self.assertEqual(get_qty_step("ETHUSDT"), 0.01)

    def test_long_trade_cost_breakdown(self):
        result = compute_binance_futures_costs(
            symbol="BTCUSDT", direction="long", entry=50000.0, exit_p=51000.0, qty=1.0,
        )
        self.assertAlmostEqual(result["gross"], 1000.0, places=4)
        self.assertAlmostEqual(result["taker_fee_entry"], 50000.0 * 0.0005, places=4)
        self.assertAlmostEqual(result["taker_fee_exit"], 51000.0 * 0.0005, places=4)
        self.assertAlmostEqual(result["net"], result["gross"] - result["total"], places=4)

    def test_size_crypto_position_risk_mode(self):
        qty = size_crypto_position(
            capital=100000.0, entry_price=50000.0, stop_distance=500.0,
            risk_pct=2.0, symbol="BTCUSDT", size_mode="risk",
        )
        # risk_amount = 2000, stop_distance = 500 -> raw qty 4.0, step 0.001
        self.assertAlmostEqual(qty, 4.0, places=3)

    def test_maker_fee_cheaper_than_taker(self):
        taker = compute_binance_futures_costs(
            symbol="BTCUSDT", direction="long", entry=50000.0, exit_p=51000.0, qty=1.0,
        )
        maker = compute_binance_futures_costs(
            symbol="BTCUSDT", direction="long", entry=50000.0, exit_p=51000.0, qty=1.0,
            entry_fee_mode="maker", exit_fee_mode="maker",
        )
        self.assertLess(maker["total"], taker["total"])
        self.assertAlmostEqual(maker["taker_fee_entry"], 50000.0 * 0.0002, places=4)

    def test_size_crypto_position_margin_capped(self):
        qty = size_crypto_position(
            capital=1000.0, entry_price=50000.0, stop_distance=10.0,
            risk_pct=50.0, symbol="BTCUSDT", leverage=2.0, size_mode="margin",
        )
        # margin_budget = 1000*2 = 2000 -> qty_margin = 0.04, far below the risk-only qty
        self.assertLessEqual(qty, 0.04)


if __name__ == "__main__":
    unittest.main()
