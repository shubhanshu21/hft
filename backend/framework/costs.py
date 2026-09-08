"""
framework/costs.py — Configurable Statutory Cost Engine for Multi-Asset Trading.
"""
from __future__ import annotations

from typing import Optional
from .types import AssetClass, Direction
from .models import CostBreakdown
from .config import StatutoryCostConfig, GLOBAL_CONFIG


def compute_brokerage(turnover: float, cfg: StatutoryCostConfig) -> float:
    return min(cfg.brokerage_flat_cap, turnover * (cfg.brokerage_pct / 100.0))


def compute_statutory_costs(
    asset_class: AssetClass,
    direction: Direction,
    entry_price: float,
    exit_price: float,
    qty: int,
    multiplier: float = 1.0,
    config: Optional[StatutoryCostConfig] = None,
) -> CostBreakdown:
    """
    Computes exact itemized statutory costs, taxes, and net PnL across Indian markets:
      - MCX Commodity Futures
      - NSE Equity Intraday
      - NSE Index / Stock Futures
      - NSE / MCX Options
    """
    cfg = config or GLOBAL_CONFIG.costs
    d = 1 if direction == Direction.LONG or direction == "long" else -1

    entry_val = qty * multiplier * entry_price
    exit_val = qty * multiplier * exit_price
    gross = qty * multiplier * (exit_price - entry_price) * d

    # 1. Brokerage (Cap per order leg)
    brok = compute_brokerage(entry_val, cfg) + compute_brokerage(exit_val, cfg)

    # 2. STT / CTT
    stt_ctt = 0.0
    if asset_class == AssetClass.COMMODITY_FUTURES:
        # CTT 0.01% on sell side turnover
        sell_turnover = exit_val if d == 1 else entry_val
        stt_ctt = sell_turnover * (cfg.futures_ctt_sell_pct / 100.0)
    elif asset_class in (AssetClass.INDEX_FUTURES, AssetClass.STOCK_FUTURES):
        # STT 0.02% on sell side turnover
        sell_turnover = exit_val if d == 1 else entry_val
        stt_ctt = sell_turnover * 0.0002
    elif asset_class in (AssetClass.INDEX_OPTIONS, AssetClass.STOCK_OPTIONS, AssetClass.COMMODITY_OPTIONS):
        # STT 0.0625% on sell side premium turnover (or 0.125% if exercised)
        sell_turnover = exit_val if d == 1 else entry_val
        stt_ctt = sell_turnover * (cfg.options_stt_sell_premium_pct / 100.0)
    elif asset_class == AssetClass.EQUITY:
        # STT 0.025% on sell side intraday turnover
        sell_turnover = exit_val if d == 1 else entry_val
        stt_ctt = sell_turnover * (cfg.equity_intraday_stt_sell_pct / 100.0)

    # 3. Stamp Duty (on buy side)
    buy_turnover = entry_val if d == 1 else exit_val
    if asset_class == AssetClass.COMMODITY_FUTURES:
        stamp = buy_turnover * (cfg.stamp_duty_futures_buy_pct / 100.0)
    elif asset_class in (AssetClass.INDEX_OPTIONS, AssetClass.STOCK_OPTIONS, AssetClass.COMMODITY_OPTIONS):
        stamp = buy_turnover * (cfg.stamp_duty_options_buy_pct / 100.0)
    else:
        stamp = buy_turnover * (cfg.stamp_duty_equity_buy_pct / 100.0)

    # 4. Exchange Turnover Fee
    if asset_class == AssetClass.COMMODITY_FUTURES:
        exch = (entry_val + exit_val) * (cfg.mcx_futures_exchange_fee_pct / 100.0)
    elif asset_class in (AssetClass.INDEX_OPTIONS, AssetClass.STOCK_OPTIONS, AssetClass.COMMODITY_OPTIONS):
        exch = (entry_val + exit_val) * (cfg.nse_options_exchange_fee_pct / 100.0)
    elif asset_class in (AssetClass.INDEX_FUTURES, AssetClass.STOCK_FUTURES):
        exch = (entry_val + exit_val) * (cfg.nse_futures_exchange_fee_pct / 100.0)
    else:
        exch = (entry_val + exit_val) * (cfg.nse_cash_exchange_fee_pct / 100.0)

    # 5. SEBI Regulatory Fee (₹10 per crore = 0.0001%)
    sebi = (entry_val + exit_val) * (cfg.sebi_fee_per_side_pct / 100.0)

    # 6. GST (18% on Brokerage + Exchange + SEBI fees)
    gst = (brok + exch + sebi) * (cfg.gst_rate_pct / 100.0)

    # 7. Slippage (Configurable ½ tick default per leg)
    slip_pct = 0.0002
    slip = (entry_val + exit_val) * slip_pct

    total_fees = brok + stt_ctt + stamp + exch + sebi + gst + slip
    net = gross - total_fees

    return CostBreakdown(
        gross_pnl=gross,
        brokerage=brok,
        stt_ctt=stt_ctt,
        stamp_duty=stamp,
        exchange_txn=exch,
        sebi_fee=sebi,
        gst=gst,
        slippage=slip,
        total_costs=total_fees,
        net_pnl=net,
    )
