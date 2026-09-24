"""Upstox margins and lot sizes change at the broker's discretion: nothing may be assumed, and an order the account cannot carry is cut or skipped."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from engine import margin_rates as mr
from markets.commodity.costs import size_commodity_lots
from markets.currency.costs import size_currency_lots
from markets.equity.costs import size_equity_shares


class FakeBroker:
    """margin_per_lot can be changed between calls, like a broker revising SPAN margin mid-day."""

    def __init__(self, margin_per_lot=27_623.0, price=9142.0, lot_size=10, fail=False):
        self.margin_per_lot, self.price, self.fail = margin_per_lot, price, fail
        self.calls = []
        self._cache = SimpleNamespace(get_or_refresh=lambda: pd.DataFrame({"instrument_key": ["MCX_FO|1"], "lot_size": [lot_size]}))

    def get_required_margin(self, key, qty, side, product="D"):
        self.calls.append((key, qty, side, product))
        if self.fail:
            raise TimeoutError("read timed out")
        return self.margin_per_lot * qty

    def get_ltp(self, key):
        return self.price


class TestSizingRespectsWhatFits(unittest.TestCase):
    """Sizing used to force max(1, lots): GOLDM (Rs140k/lot) and SILVER (Rs901k/lot) 'traded' on Rs100k."""

    def test_a_lot_that_does_not_fit_the_account_is_zero_not_one(self):
        self.assertEqual(size_commodity_lots(100_000, 150_000, 500, 4.0, "GOLDM", leverage=10.7), 0)        # margin ~Rs140k > Rs100k
        self.assertEqual(size_commodity_lots(100_000, 232_000, 1_000, 5.0, "SILVER", leverage=7.7), 0)      # margin ~Rs901k
        self.assertEqual(size_currency_lots(1_000, 96.0, 0.1, 4.0, "USDINR", leverage=42.0), 0)             # a tiny account cannot carry one lot
        self.assertEqual(size_equity_shares(100, 1_500.0, 5.0, 4.0, leverage=5.0), 0)                       # one share needs Rs300 of margin

    def test_what_fits_is_still_sized_by_the_smaller_of_risk_and_margin(self):
        self.assertEqual(size_commodity_lots(100_000, 9_142, 60, 10.0, "CRUDEOILM", leverage=3.3), 3)       # real crude: 3 lots on Rs100k
        self.assertGreaterEqual(size_currency_lots(100_000, 96.0, 0.1, 4.0, "USDINR", leverage=42.0), 1)
        self.assertEqual(size_equity_shares(100_000, 1_000.0, 5.0, 4.0, leverage=5.0), 500)                  # risk allows 800 shares, margin (Rs200 each) only 500

class TestConfirmOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = patch.object(mr, "RATES_PATH", self.tmp / "rates.json")
        p.start()
        self.addCleanup(p.stop)
        mr._rates = {}
        self.addCleanup(lambda: setattr(mr, "_rates", None))
        env = patch.dict(os.environ, {"MARGIN_VERIFY": "strict", "MARGIN_MAX_AGE_HOURS": "6"})
        env.start()
        self.addCleanup(env.stop)

    def test_an_order_that_fits_passes_and_reports_the_real_margin(self):
        r = mr.confirm_order(FakeBroker(), "CRUDEOILM", "MCX_FO|1", 3, "BUY", 100_000.0)
        self.assertEqual((r["qty"], r["source"]), (3, "upstox"))
        self.assertAlmostEqual(r["margin"], 3 * 27_623.0)

    def test_an_order_that_is_too_big_is_cut_to_what_upstox_would_accept(self):
        r = mr.confirm_order(FakeBroker(), "CRUDEOILM", "MCX_FO|1", 8, "BUY", 100_000.0)         # 8 lots needs Rs221k
        self.assertEqual(r["qty"], 3)
        self.assertLessEqual(r["margin"], 100_000.0)
        self.assertIn("cut to 3", r["note"])

    def test_a_lot_that_cannot_be_afforded_is_skipped(self):
        r = mr.confirm_order(FakeBroker(margin_per_lot=139_806.0), "GOLDM", "MCX_FO|1", 1, "SELL", 100_000.0)
        self.assertEqual(r["qty"], 0)
        self.assertIn("only Rs100,000 is free", r["note"])

    def test_a_margin_increase_since_the_last_refresh_is_caught_on_the_very_next_order(self):
        broker = FakeBroker(margin_per_lot=27_623.0)
        self.assertEqual(mr.confirm_order(broker, "CRUDEOILM", "MCX_FO|1", 3, "BUY", 100_000.0)["qty"], 3)
        broker.margin_per_lot = 55_000.0                                   # the broker doubled SPAN margin
        self.assertEqual(mr.confirm_order(broker, "CRUDEOILM", "MCX_FO|1", 3, "BUY", 100_000.0)["qty"], 1)

    def test_the_exact_order_is_priced_intraday_with_the_right_side_and_quantity(self):
        broker = FakeBroker()
        mr.confirm_order(broker, "CRUDEOILM", "MCX_FO|1", 2, "SELL", 100_000.0)
        self.assertEqual(broker.calls, [("MCX_FO|1", 2, "SELL", "I")])

    def test_when_upstox_cannot_answer_a_fresh_cached_rate_is_used_and_a_stale_one_is_not(self):
        fresh = datetime.now().isoformat(timespec="seconds")
        mr._rates = {"CRUDEOILM": {"margin": 27_623.0, "asof": fresh}}
        down = FakeBroker(fail=True)
        r = mr.confirm_order(down, "CRUDEOILM", "MCX_FO|1", 3, "BUY", 100_000.0)
        self.assertEqual((r["qty"], r["source"]), (3, "cached"))
        mr._rates = {"CRUDEOILM": {"margin": 27_623.0, "asof": (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds")}}
        r = mr.confirm_order(down, "CRUDEOILM", "MCX_FO|1", 3, "BUY", 100_000.0)
        self.assertEqual((r["qty"], r["source"]), (0, "unverified"))          # unverifiable: skipped, not guessed
        mr._rates = {}
        self.assertEqual(mr.confirm_order(down, "CRUDEOILM", "MCX_FO|1", 3, "BUY", 100_000.0)["qty"], 0)

    def test_verification_can_be_switched_off_for_tests_only(self):
        with patch.dict(os.environ, {"MARGIN_VERIFY": "off"}):
            r = mr.confirm_order(FakeBroker(fail=True), "CRUDEOILM", "MCX_FO|1", 8, "BUY", 100_000.0)
        self.assertEqual((r["qty"], r["source"]), (8, "off"))


class TestRefreshAndLotSize(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "rates.json"
        mr._rates = {}
        self.addCleanup(lambda: setattr(mr, "_rates", None))

    def _notional(self, sym, price):
        return price * 10

    def test_leverage_is_notional_over_the_real_margin_and_caps_the_configured_value(self):
        b = FakeBroker(margin_per_lot=27_623.0, price=9142.0)
        rates = mr.refresh(b, {"CRUDEOILM": "MCX_FO|1"}, self._notional, path=self.path, sleep_s=0)
        self.assertAlmostEqual(rates["CRUDEOILM"]["leverage"], 91_420 / 27_623, places=2)                 # 3.31x
        self.assertAlmostEqual(mr.cap_leverage("CRUDEOILM", 10.0), 91_420 / 27_623, places=2)             # 10x configured -> 3.3x allowed
        self.assertEqual(mr.cap_leverage("CRUDEOILM", 2.0), 2.0)                                          # configured below the limit stays
        self.assertEqual(mr.cap_leverage("UNKNOWN", 5.0), 5.0)                                            # nothing known: configured value, not a guess

    def test_a_lot_size_change_in_the_master_is_detected_against_what_it_said_last_time(self):
        changed = []
        mr.refresh(FakeBroker(lot_size=100), {"GOLDM": "MCX_FO|1"}, self._notional, path=self.path, sleep_s=0, mismatches=changed)
        self.assertEqual(changed, [])                                   # first sight: a baseline, not a change
        mr.refresh(FakeBroker(lot_size=100), {"GOLDM": "MCX_FO|1"}, self._notional, path=self.path, sleep_s=0, mismatches=changed)
        self.assertEqual(changed, [])                                   # same value again: still nothing
        mr.refresh(FakeBroker(lot_size=50), {"GOLDM": "MCX_FO|1"}, self._notional, path=self.path, sleep_s=0, mismatches=changed)
        self.assertEqual(changed, [("GOLDM", 100, 50)])                 # the broker revised the contract

    def test_the_masters_lot_size_is_never_compared_with_the_cost_models_units(self):
        """The first version compared master lot 100 (GOLDM, grams) with the cost model's 10x price multiplier, cried 'changed', and rescaled the
        notional to 107x leverage; USDINR (master 1 vs 1000) went to 0.0x. Units differ by design: the notional must stay as computed."""
        rates = mr.refresh(FakeBroker(lot_size=100, price=150_000.0, margin_per_lot=139_806.0), {"GOLDM": "MCX_FO|1"}, self._notional,
                           path=self.path, sleep_s=0, mismatches=[])
        self.assertAlmostEqual(rates["GOLDM"]["notional"], 1_500_000.0)
        self.assertAlmostEqual(rates["GOLDM"]["leverage"], 1_500_000 / 139_806, places=2)         # 10.7x, not 107x

    def test_an_implausible_ratio_is_ignored_and_the_previous_rate_kept(self):
        good = mr.refresh(FakeBroker(), {"A": "MCX_FO|1"}, self._notional, path=self.path, sleep_s=0)
        bad = mr.refresh(FakeBroker(margin_per_lot=1e12), {"A": "MCX_FO|1"}, self._notional, path=self.path, sleep_s=0)        # 0.0x leverage
        self.assertEqual(bad["A"]["leverage"], good["A"]["leverage"])

    def test_a_failing_symbol_keeps_its_previous_rate_and_does_not_lose_the_others(self):
        good = FakeBroker()
        mr.refresh(good, {"A": "MCX_FO|1", "B": "MCX_FO|1"}, self._notional, path=self.path, sleep_s=0)
        good.fail = True
        rates = mr.refresh(good, {"A": "MCX_FO|1", "B": "MCX_FO|1"}, self._notional, path=self.path, sleep_s=0)
        self.assertEqual(set(rates), {"A", "B"})

    def test_unaffordable_lists_symbols_whose_single_unit_exceeds_the_capital(self):
        mr._rates = {"GOLDM": {"margin": 139_806.0}, "CRUDEOILM": {"margin": 27_623.0}, "SILVER": {"margin": 900_765.0}}
        self.assertEqual([s for s, _ in mr.unaffordable(100_000.0, ["CRUDEOILM", "GOLDM", "SILVER"])], ["GOLDM", "SILVER"])


if __name__ == "__main__":
    unittest.main()


class TestSmallCapitalContracts(unittest.TestCase):
    """At Rs100k the full GOLDM / SILVER contracts cannot trade; the micro contracts use the same price series and fit."""

    def test_the_micro_contracts_have_cost_multipliers_matching_their_lot_and_quote_basis(self):
        from markets.commodity.costs import get_contract_multiplier
        self.assertEqual(get_contract_multiplier("SILVERMIC"), 1)          # 1 kg lot, quoted per kg
        self.assertEqual(get_contract_multiplier("GOLDTEN"), 1)            # 10 g lot, quoted per 10 g
        self.assertEqual(get_contract_multiplier("GOLDM"), 10)             # 100 g lot, quoted per 10 g

    def test_real_margins_decide_what_fits(self):
        # margins measured on Upstox 2026-09-24 (MIS, per lot) and prices at that time
        self.assertEqual(size_commodity_lots(100_000, 235_800, 800, 10.0, "SILVERMIC", leverage=7.8), 3)      # Rs30k/lot -> 3 lots
        self.assertEqual(size_commodity_lots(100_000, 150_970, 300, 10.0, "GOLDTEN", leverage=10.8), 7)        # Rs14k/lot -> 7 lots
        self.assertEqual(size_commodity_lots(100_000, 150_970, 300, 10.0, "GOLDM", leverage=10.8), 0)          # Rs140k/lot -> cannot trade
        self.assertEqual(size_commodity_lots(100_000, 7_011_000 / 30, 800, 10.0, "SILVER", leverage=7.8), 0)   # Rs901k/lot -> cannot trade

    def test_the_backtest_reads_the_gold_archive_for_goldten_and_the_silver_archive_for_silvermic(self):
        import inspect
        from markets.commodity.scalping import backtest
        src = inspect.getsource(backtest.run_commodity_backtest)
        self.assertIn('"GOLDTEN": "GOLD"', src)
        self.assertIn('"SILVERMIC": "SILVER"', src)

    def test_the_gold_prefix_does_not_swallow_the_smaller_gold_contracts(self):
        from pathlib import Path
        src = Path(__file__).resolve().parent.parent.joinpath("services", "broker", "instruments.py").read_text()
        order = src[src.index('for base in ["CRUDEOILM"'):]
        self.assertLess(order.index('"GOLDTEN"'), order.index('"GOLD",'))         # startswith(): the longer names must be tried first
        self.assertLess(order.index('"GOLDPETAL"'), order.index('"GOLD",'))


class TestLotAndTickChangesFlowIntoSizingAndCosts(unittest.TestCase):
    """Lot size and tick size are Upstox's to change: a revision in the instrument master must scale multipliers, sizing and costs by itself."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "rates.json"
        p = patch.object(mr, "RATES_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)
        mr._rates = {}
        self.addCleanup(lambda: setattr(mr, "_rates", None))

    def _refresh(self, lot, tick=1.0):
        class B(FakeBroker):
            def __init__(s):
                super().__init__(lot_size=lot)
                s._cache = SimpleNamespace(get_or_refresh=lambda: pd.DataFrame({"instrument_key": ["MCX_FO|1"], "lot_size": [lot], "tick_size": [tick]}))
        mr.refresh(B(), {"CRUDEOILM": "MCX_FO|1"}, lambda s, p: p * 10, path=self.path, sleep_s=0, mismatches=[])

    def test_the_first_lot_size_seen_is_the_baseline_and_scale_is_one(self):
        self._refresh(10)
        self.assertEqual(mr.lot_scale("CRUDEOILM"), 1.0)

    def test_a_revised_lot_size_scales_the_multiplier_used_for_pnl_and_sizing(self):
        from markets.commodity.costs import get_contract_multiplier
        self._refresh(10)
        self.assertEqual(get_contract_multiplier("CRUDEOILM"), 10)
        self._refresh(20)                                              # Upstox doubles the lot
        self.assertEqual(mr.lot_scale("CRUDEOILM"), 2.0)
        self.assertEqual(get_contract_multiplier("CRUDEOILM"), 20)
        # a lot now costs twice the margin and risks twice the money per point: fewer lots fit the same capital
        self.assertLess(size_commodity_lots(100_000, 9_142, 60, 10.0, "CRUDEOILM", leverage=3.3), 3)

    def test_a_symbol_never_seen_by_the_master_is_unscaled(self):
        from markets.commodity.costs import get_contract_multiplier
        self.assertEqual(get_contract_multiplier("SILVERMIC"), 1)
        self.assertEqual(mr.lot_scale("SILVERMIC"), 1.0)

    def test_the_tick_size_comes_from_the_master_and_drives_the_slippage_fallback(self):
        self._refresh(10, tick=0.5)
        self.assertEqual(mr.tick_size("CRUDEOILM"), 0.5)
        import core.slippage as sl
        from markets.commodity.costs import compute_mcx_commodity_costs
        with patch.object(sl, "_cache", {}), patch.object(sl, "_cache_loaded_at", 1e18):
            wide = compute_mcx_commodity_costs("CRUDEOILM", "long", 9000.0, 9010.0, 1)["slippage"]
        self._refresh(10, tick=0.05)
        with patch.object(sl, "_cache", {}), patch.object(sl, "_cache_loaded_at", 1e18):
            narrow = compute_mcx_commodity_costs("CRUDEOILM", "long", 9000.0, 9010.0, 1)["slippage"]
        self.assertGreater(wide, narrow)
