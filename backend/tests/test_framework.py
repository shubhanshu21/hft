"""
tests/test_framework.py — Comprehensive Unit & Integration Tests for Multi-Asset Trading Framework.
"""
from __future__ import annotations

import unittest
from datetime import datetime
import pandas as pd
import numpy as np

from framework.types import AssetClass, Direction, OptionType, StrikeMode, ExitReason
from framework.models import OptionContract, Greeks
from framework.config import FrameworkConfig, GLOBAL_CONFIG
from framework.costs import compute_statutory_costs
from framework.risk import size_position
from framework.greeks import black_scholes_price, compute_greeks, solve_implied_volatility
from framework.option_chain import OptionChain
from features.technical import compute_technical_indicators
from features.microstructure import compute_microstructure_features
from features.options_features import compute_iv_rank_percentile
from strategies.equity.nse_intraday_scalper import NSEIntradayScalper
from strategies.futures.mcx_commodity_scalper import MCXCommodityScalper


class TestMultiAssetFramework(unittest.TestCase):

    def test_black_scholes_pricing_and_greeks(self):
        spot, strike = 24500.0, 24500.0
        t_years = 7.0 / 365.0
        vol = 0.15

        price_ce = black_scholes_price(spot, strike, t_years, vol, 0.07, OptionType.CALL)
        price_pe = black_scholes_price(spot, strike, t_years, vol, 0.07, OptionType.PUT)
        self.assertGreater(price_ce, 0.0)
        self.assertGreater(price_pe, 0.0)

        greeks = compute_greeks(spot, strike, t_years, vol, 0.07, OptionType.CALL)
        self.assertAlmostEqual(greeks.delta, 0.53, delta=0.05)
        self.assertLess(greeks.theta, 0.0)
        self.assertGreater(greeks.vega, 0.0)

        # IV Inversion
        solved_iv = solve_implied_volatility(price_ce, spot, strike, t_years, 0.07, OptionType.CALL)
        self.assertAlmostEqual(solved_iv, vol, delta=0.01)

    def test_option_chain_management(self):
        chain = OptionChain(underlying_symbol="NIFTY", underlying_price=24520.0, expiry="2026-09-17")
        for s in [24400, 24500, 24600]:
            chain.add_contract(OptionContract(
                symbol=f"NIFTY26SEP{s}CE", underlying_symbol="NIFTY", instrument_key=f"NSE_FO|{s}CE",
                strike=float(s), option_type=OptionType.CALL, expiry="2026-09-17", lot_size=25, ltp=150.0, oi=10000.0, volume=5000.0
            ))
            chain.add_contract(OptionContract(
                symbol=f"NIFTY26SEP{s}PE", underlying_symbol="NIFTY", instrument_key=f"NSE_FO|{s}PE",
                strike=float(s), option_type=OptionType.PUT, expiry="2026-09-17", lot_size=25, ltp=130.0, oi=12000.0, volume=6000.0
            ))

        self.assertEqual(chain.get_atm_strike(), 24500.0)
        otm1_ce = chain.select_strike(OptionType.CALL, offset=1)
        self.assertEqual(otm1_ce.strike, 24600.0)
        pcr_oi, _ = chain.calculate_pcr()
        self.assertGreater(pcr_oi, 0.0)

    def test_statutory_costs_multi_asset(self):
        cost_comm = compute_statutory_costs(AssetClass.COMMODITY_FUTURES, Direction.LONG, entry_price=8750.0, exit_price=8850.0, qty=2, multiplier=10.0)
        self.assertGreater(cost_comm.gross_pnl, 0.0)
        self.assertGreater(cost_comm.total_costs, 0.0)
        self.assertAlmostEqual(cost_comm.net_pnl, cost_comm.gross_pnl - cost_comm.total_costs, places=2)

        cost_opt = compute_statutory_costs(AssetClass.INDEX_OPTIONS, Direction.LONG, entry_price=120.0, exit_price=160.0, qty=4, multiplier=25.0)
        self.assertGreater(cost_opt.gross_pnl, 0.0)
        self.assertGreater(cost_opt.total_costs, 0.0)

    def test_risk_position_sizing(self):
        shares = size_position(AssetClass.EQUITY, capital=100_000.0, entry_price=2500.0, stop_distance=25.0)
        self.assertGreater(shares, 0)
        lots_comm = size_position(AssetClass.COMMODITY_FUTURES, capital=100_000.0, entry_price=8750.0, stop_distance=50.0, multiplier=10.0)
        self.assertGreater(lots_comm, 0)


if __name__ == "__main__":
    unittest.main()
