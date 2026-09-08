"""
framework/models.py — Data Models for Multi-Asset Trading (Equities, Futures, Options).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any
import pandas as pd

from .types import AssetClass, OrderSide, OrderType, OrderStatus, ProductType, Direction, OptionType, ExitReason


@dataclass
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: Optional[float] = None
    minutes_since_open: int = 0


@dataclass
class Quote:
    symbol: str
    instrument_key: str
    ltp: float
    bid: float = 0.0
    ask: float = 0.0
    volume: float = 0.0
    oi: Optional[float] = None
    timestamp: Optional[datetime] = None


@dataclass
class CostBreakdown:
    gross_pnl: float
    brokerage: float
    stt_ctt: float
    stamp_duty: float
    exchange_txn: float
    sebi_fee: float
    gst: float
    slippage: float
    total_costs: float
    net_pnl: float

    def to_dict(self) -> dict[str, float]:
        return {
            "gross": round(self.gross_pnl, 2),
            "brokerage": round(self.brokerage, 2),
            "stt_ctt": round(self.stt_ctt, 2),
            "stamp_duty": round(self.stamp_duty, 2),
            "exchange_txn": round(self.exchange_txn, 2),
            "sebi": round(self.sebi_fee, 2),
            "gst": round(self.gst, 2),
            "slippage": round(self.slippage, 2),
            "total": round(self.total_costs, 2),
            "net": round(self.net_pnl, 2),
        }


@dataclass
class Order:
    order_id: str
    symbol: str
    instrument_key: str
    side: OrderSide
    order_type: OrderType
    qty: int
    requested_price: float
    fill_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    product: ProductType = ProductType.MIS
    account_id: str = "DEFAULT"
    intent: str = "ENTRY"
    tag: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Position:
    position_id: str
    symbol: str
    instrument_key: str
    asset_class: AssetClass
    direction: Direction
    qty: int
    entry_price: float
    current_stop: float
    target_price: float
    breakeven_price: float
    entry_time: datetime
    lots: int = 1
    multiplier: float = 1.0
    best_price: float = 0.0
    armed_be: bool = False
    entry_bar_idx: int = 0
    p_score: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trade:
    trade_id: str
    position_id: str
    symbol: str
    asset_class: AssetClass
    direction: Direction
    qty: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    hold_minutes: float
    exit_reason: ExitReason
    costs: CostBreakdown
    capital_after: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Greeks:
    iv: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0


@dataclass
class OptionContract:
    symbol: str
    underlying_symbol: str
    instrument_key: str
    strike: float
    option_type: OptionType
    expiry: str
    lot_size: int
    tick_size: float = 0.05
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    oi: float = 0.0
    volume: float = 0.0
    greeks: Optional[Greeks] = None


@dataclass
class OptionChainSnapshot:
    underlying_symbol: str
    underlying_price: float
    expiry: str
    timestamp: datetime
    contracts: dict[float, dict[OptionType, OptionContract]] = field(default_factory=dict)
    atm_strike: float = 0.0
    pcr_oi: float = 1.0
    pcr_vol: float = 1.0
    max_pain: float = 0.0
