"""
framework — Unified Multi-Asset Quantitative Trading Framework (Equities, Futures, Options).
"""
from .types import (
    AssetClass, Exchange, OrderSide, OrderType, ProductType,
    OrderStatus, Direction, OptionType, StrikeMode, ExitReason,
)
from .models import (
    Bar, Quote, CostBreakdown, Order, Position, Trade, Greeks,
    OptionContract, OptionChainSnapshot,
)
from .config import (
    StatutoryCostConfig, RiskBudgetConfig, SessionConfig, OptionsConfig,
    InstrumentSpec, FrameworkConfig, GLOBAL_CONFIG,
)
from .costs import compute_statutory_costs, compute_brokerage
from .risk import size_position
from .greeks import black_scholes_price, compute_greeks, solve_implied_volatility
from .option_chain import OptionChain
from .strategy import BaseStrategy
from .database import TradingDB

__all__ = [
    "AssetClass", "Exchange", "OrderSide", "OrderType", "ProductType",
    "OrderStatus", "Direction", "OptionType", "StrikeMode", "ExitReason",
    "Bar", "Quote", "CostBreakdown", "Order", "Position", "Trade", "Greeks",
    "OptionContract", "OptionChainSnapshot",
    "StatutoryCostConfig", "RiskBudgetConfig", "SessionConfig", "OptionsConfig",
    "InstrumentSpec", "FrameworkConfig", "GLOBAL_CONFIG",
    "compute_statutory_costs", "compute_brokerage",
    "size_position",
    "black_scholes_price", "compute_greeks", "solve_implied_volatility",
    "OptionChain",
    "BaseStrategy",
    "TradingDB",
]
