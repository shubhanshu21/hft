"""
strategies — Multi-Asset Quantitative Strategies Library (Equities, Futures, Options).
"""
from .base import BaseStrategy
from .equity.nse_intraday_scalper import NSEIntradayScalper
from .futures.mcx_commodity_scalper import MCXCommodityScalper
from .options.directional_buyer import DirectionalOptionBuyer
from .options.credit_spread import CreditSpreadSeller

__all__ = [
    "BaseStrategy",
    "NSEIntradayScalper",
    "MCXCommodityScalper",
    "DirectionalOptionBuyer",
    "CreditSpreadSeller",
]
