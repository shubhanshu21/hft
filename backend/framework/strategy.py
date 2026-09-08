"""
framework/strategy.py — Abstract Base Class for Multi-Asset Strategies.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Any
import pandas as pd

from .types import AssetClass, Direction, OrderSide, ExitReason
from .models import Order, Position, Trade, CostBreakdown, Bar, Quote, OptionChainSnapshot
from .config import FrameworkConfig, GLOBAL_CONFIG
from .costs import compute_statutory_costs
from .risk import size_position


class BaseStrategy(ABC):
    """
    Standardized strategy interface for Equities, Futures, and Options.
    """

    def __init__(
        self,
        name: str,
        asset_class: AssetClass,
        symbols: list[str],
        config: Optional[FrameworkConfig] = None,
        custom_params: Optional[dict[str, Any]] = None,
    ):
        self.name = name
        self.asset_class = asset_class
        self.symbols = symbols
        self.config = config or GLOBAL_CONFIG
        self.params = custom_params or {}

    @abstractmethod
    def on_start(self, initial_capital: float) -> None:
        """Called once before backtest or live trading starts."""
        pass

    @abstractmethod
    def compute_features(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Computes technical indicators and features for a given historical/live bar DataFrame."""
        pass

    @abstractmethod
    def generate_signal(
        self,
        symbol: str,
        feat_df: pd.DataFrame,
        current_bar: pd.Series,
        bar_idx: int,
        quote: Optional[Quote] = None,
        option_chain: Optional[OptionChainSnapshot] = None,
    ) -> Optional[dict]:
        """
        Evaluates strategy rules on current bar and returns a signal dict if triggered:
        {
            "symbol": str,
            "direction": Direction,
            "entry_price": float,
            "stop_loss": float,
            "take_profit": float,
            "breakeven": float,
            "stop_distance": float,
            "score": float,
            "metadata": dict
        }
        """
        pass

    @abstractmethod
    def manage_position(
        self,
        position: Position,
        latest_bar: pd.Series,
        now: datetime,
        bar_idx: int,
        quote: Optional[Quote] = None,
    ) -> tuple[Optional[float], Optional[ExitReason]]:
        """
        Evaluates open position against take profit, stops, trailing stops, timeout, and market close.
        Returns (exit_price, exit_reason) if position should exit, or (None, None) to keep holding.
        """
        pass

    def calculate_order_size(
        self,
        symbol: str,
        capital: float,
        entry_price: float,
        stop_dist: float,
        lot_size: int = 1,
        multiplier: float = 1.0,
        margin_per_lot: Optional[float] = None,
    ) -> int:
        """Calculates position size using the unified risk manager."""
        return size_position(
            asset_class=self.asset_class,
            capital=capital,
            entry_price=entry_price,
            stop_distance=stop_dist,
            lot_size=lot_size,
            multiplier=multiplier,
            margin_per_lot=margin_per_lot,
            risk_config=self.config.risk,
        )

    def calculate_trade_costs(
        self,
        direction: Direction,
        entry_price: float,
        exit_price: float,
        qty: int,
        multiplier: float = 1.0,
    ) -> CostBreakdown:
        """Calculates exact statutory taxes and brokerage for this strategy's asset class."""
        return compute_statutory_costs(
            asset_class=self.asset_class,
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            multiplier=multiplier,
            config=self.config.costs,
        )
