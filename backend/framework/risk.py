"""
framework/risk.py — Multi-Asset Risk Management & Dynamic Sizing Engine.
"""
from __future__ import annotations

import math
from typing import Optional
from .types import AssetClass
from .config import RiskBudgetConfig, GLOBAL_CONFIG


def size_position(
    asset_class: AssetClass,
    capital: float,
    entry_price: float,
    stop_distance: float,
    lot_size: int = 1,
    multiplier: float = 1.0,
    margin_per_lot: Optional[float] = None,
    risk_config: Optional[RiskBudgetConfig] = None,
) -> int:
    """
    Computes mathematically rigorous position sizing:
      - Equities: Number of shares capped by risk budget & margin leverage.
      - Futures: Number of lots capped by contract multiplier, risk budget & margin limits.
      - Options: Number of lots based on option premium margin & risk allocation.
    """
    cfg = risk_config or GLOBAL_CONFIG.risk

    if capital <= 0 or entry_price <= 0 or stop_distance <= 0:
        return 0

    risk_budget = capital * (cfg.risk_pct_per_trade / 100.0)

    if asset_class == AssetClass.EQUITY:
        qty_risk = math.floor(risk_budget / stop_distance)
        qty_margin = math.floor((capital * cfg.default_leverage) / entry_price)
        return max(0, min(qty_risk, qty_margin))

    elif asset_class in (AssetClass.COMMODITY_FUTURES, AssetClass.INDEX_FUTURES, AssetClass.STOCK_FUTURES):
        risk_per_lot = stop_distance * multiplier
        if risk_per_lot <= 0:
            return 0
        lots_risk = math.floor(risk_budget / risk_per_lot)

        # Margin requirement per lot
        notional_val = entry_price * multiplier
        margin_required = margin_per_lot if margin_per_lot and margin_per_lot > 0 else (notional_val / cfg.default_leverage)
        lots_margin = math.floor(capital / margin_required) if margin_required > 0 else 1

        return max(0, min(lots_risk, lots_margin))

    elif asset_class in (AssetClass.INDEX_OPTIONS, AssetClass.STOCK_OPTIONS, AssetClass.COMMODITY_OPTIONS):
        # Options buying: Max risk is either premium paid or stop loss on premium
        option_risk_per_lot = stop_distance * lot_size
        if option_risk_per_lot <= 0:
            return 0
        lots_risk = math.floor(risk_budget / option_risk_per_lot)
        premium_per_lot = entry_price * lot_size
        lots_capital = math.floor(capital / premium_per_lot) if premium_per_lot > 0 else 1
        return max(0, min(lots_risk, lots_capital))

    return 0
