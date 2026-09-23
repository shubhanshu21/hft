"""Margin/heat gates shared by the paper and live runners (core/risk.py).

Regression: on 2026-09-23 a 90% total / 50% per-market margin cap REJECTED virtually every entry,
because one margin-bound trade already uses 80-100% of capital. Entries are now sized to the margin
still available instead, and the default limits (100%) never bind on a single position."""
import os
import unittest
from unittest.mock import patch

from core.risk import RiskGates
from core.strategy import Signal


class _Runner(RiskGates):
    def __init__(self, capital=100_000.0, positions=None, heat=0.0):
        self.capital = capital
        self.positions = positions or {}
        self.max_portfolio_heat_pct = 12.0
        self._heat = heat
        self._init_margin_limits()

    def _portfolio_heat_pct(self):
        return self._heat


def _signal(qty=4, lot_size=1000, price=95.0, stop_dist=0.1):
    return Signal(symbol="USDINR", direction="long", entry_price=price, stop_loss=price - stop_dist,
                  qty=qty, stop_dist=stop_dist, lot_size=lot_size)


class TestMarginRoom(unittest.TestCase):
    def test_default_limits_do_not_bind_on_a_single_margin_bound_position(self):
        # 4 lots x 1000 x 95 / 5x = Rs 76,000 = 76% of a 100k account: the normal case that was being rejected.
        r = _Runner()
        self.assertEqual(r._sizing_capital("currency"), 100_000.0)
        self.assertIsNone(r._entry_gate_rejection("USDINR", "currency", _signal(), leverage=5.0))

    def test_full_account_margin_position_is_still_allowed(self):
        r = _Runner()
        self.assertIsNone(r._entry_gate_rejection("USDINR", "currency", _signal(qty=5, price=100.0), leverage=5.0))

    def test_a_second_position_is_sized_to_the_margin_left_not_the_whole_account(self):
        r = _Runner(positions={"USDINR": {"margin_used": 76_000.0}})
        self.assertAlmostEqual(r._sizing_capital("commodity"), 24_000.0)

    def test_no_margin_left_means_no_entry_is_even_evaluated(self):
        r = _Runner(positions={"USDINR": {"margin_used": 100_000.0}})
        self.assertEqual(r._sizing_capital("commodity"), 0.0)

    def test_per_market_cap_limits_that_market_only(self):
        with patch.dict(os.environ, {"MAX_MARKET_MARGIN_UTILIZATION_PCT": "40"}):
            r = _Runner(positions={"USDINR": {"margin_used": 30_000.0}})
        self.assertAlmostEqual(r._sizing_capital("currency"), 10_000.0)     # 40k market cap - 30k used
        self.assertAlmostEqual(r._sizing_capital("equity"), 40_000.0)       # equity's own 40k cap is untouched...
        # ...but the account-wide pool still has only 70k left, so a market can never exceed that either
        self.assertLessEqual(r._sizing_capital("equity"), 100_000.0 - 30_000.0)

    def test_backstop_rejects_a_signal_that_still_does_not_fit(self):
        r = _Runner(positions={"USDINR": {"margin_used": 90_000.0}})
        reason = r._entry_gate_rejection("GOLDM", "commodity", _signal(qty=1, lot_size=10, price=15000.0), leverage=5.0)
        self.assertIn("margin", reason)

    def test_heat_cap_still_applies(self):
        r = _Runner(heat=11.5)
        self.assertIn("heat cap", r._entry_gate_rejection("USDINR", "currency", _signal(stop_dist=1.0), leverage=5.0))


if __name__ == "__main__":
    unittest.main()
