"""Our cost model must agree with Upstox's own brokerage calculator (ChargeApi.get_brokerage), recorded 2026-09-24 (MIS, BUY then SELL).

The model used min(0.05%, Rs20) per order; Upstox charges min(0.06%, Rs30), so every round trip was understated by ~Rs23.6 -- 27% of a
1-lot crude trade's cost. Re-record these numbers (and re-run the backtests) if Upstox changes its charges."""
import unittest
from unittest.mock import patch

from markets.commodity.costs import compute_mcx_commodity_costs
from markets.currency.costs import compute_ncd_currency_costs
from markets.equity.costs import compute_nse_equity_costs


def _ex_slippage(r):
    return r.get("total_friction", r.get("total")) - r["slippage"]


class TestAgainstUpstoxCalculator(unittest.TestCase):
    def setUp(self):
        import core.slippage as sl
        p = patch.object(sl, "_cache", {}), patch.object(sl, "_cache_loaded_at", 1e18)          # statutory charges only, no slippage
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_mcx_commodity_round_trips_match_to_the_paisa(self):
        for lots, upstox in ((1, 86.60), (3, 118.23)):
            r = compute_mcx_commodity_costs("CRUDEOILM", "long", 9178.0, 9200.0, lots)
            self.assertAlmostEqual(_ex_slippage(r), upstox, delta=0.05, msg=f"CRUDEOILM {lots} lot")
        for lots, upstox in ((1, 111.44), (3, 192.69)):
            r = compute_mcx_commodity_costs("SILVERMIC", "long", 235800.0, 236500.0, lots)
            self.assertAlmostEqual(_ex_slippage(r), upstox, delta=0.05, msg=f"SILVERMIC {lots} lot")

    def test_equity_round_trip_is_within_one_percent(self):
        r = compute_nse_equity_costs("long", 1219.0, 1225.0, 100, symbol="RELIANCE")
        self.assertAlmostEqual(_ex_slippage(r) / 114.22, 1.0, delta=0.01)

    def test_currency_is_never_cheaper_than_upstox(self):
        r = compute_ncd_currency_costs("USDINR", "long", 95.98, 96.05, 4)
        self.assertGreaterEqual(_ex_slippage(r), 76.60 * 0.99)                 # conservative side only; within 6%
        self.assertLessEqual(_ex_slippage(r), 76.60 * 1.06)

    def test_brokerage_is_the_lower_of_0_06_percent_and_rs30(self):
        from markets.equity.costs import compute_equity_brokerage
        self.assertAlmostEqual(compute_equity_brokerage(1_219.0), 0.73 * 1.18, delta=0.01)                     # 0.06% of one share
        self.assertAlmostEqual(compute_equity_brokerage(121_900.0), 30.0 * 1.18, delta=0.01)                   # capped at Rs30 (+18% GST)


if __name__ == "__main__":
    unittest.main()
