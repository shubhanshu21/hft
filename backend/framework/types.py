"""
framework/types.py — Multi-Asset Quantitative Trading Framework Enums & Types.
"""
from __future__ import annotations

from enum import Enum


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    COMMODITY_FUTURES = "COMMODITY_FUTURES"
    INDEX_FUTURES = "INDEX_FUTURES"
    STOCK_FUTURES = "STOCK_FUTURES"
    INDEX_OPTIONS = "INDEX_OPTIONS"
    STOCK_OPTIONS = "STOCK_OPTIONS"
    COMMODITY_OPTIONS = "COMMODITY_OPTIONS"


class Exchange(str, Enum):
    NSE_EQ = "NSE_EQ"
    NSE_FO = "NSE_FO"
    BSE_EQ = "BSE_EQ"
    BSE_FO = "BSE_FO"
    MCX_FO = "MCX_FO"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class ProductType(str, Enum):
    MIS = "MIS"      # Intraday margin
    NRML = "NRML"    # Normal overnight futures/options
    CNC = "CNC"      # Cash & carry delivery equity


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    TRIGGERED = "TRIGGERED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


class OptionType(str, Enum):
    CALL = "CE"
    PUT = "PE"


class StrikeMode(str, Enum):
    ATM = "ATM"
    ITM1 = "ITM1"
    ITM2 = "ITM2"
    OTM1 = "OTM1"
    OTM2 = "OTM2"
    DELTA_TARGET = "DELTA_TARGET"


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    INITIAL_STOP = "initial_stop"
    BE_STOP = "be_stop"
    TRAILING_STOP = "trailing_stop"
    TIMEOUT = "timeout_exit"
    EOD_SQUAREOFF = "eod_squareoff"
    TARGET_DELTA = "target_delta"
    SIGNAL_REVERSAL = "signal_reversal"
    MANUAL = "manual"
