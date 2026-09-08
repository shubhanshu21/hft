"""
strategies/options/directional_buyer.py — Multi-Asset Directional Options Buying Strategy.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Any
import pandas as pd

from framework.types import AssetClass, Direction, OptionType, StrikeMode, ExitReason
from framework.models import Position, Quote, OptionChainSnapshot, OptionContract
from framework.strategy import BaseStrategy
from framework.config import FrameworkConfig
from features.technical import compute_technical_indicators
from features.microstructure import compute_microstructure_features


class DirectionalOptionBuyer(BaseStrategy):
    """
    Momentum-driven Directional Option Buyer.
    Buys ATM or slightly OTM Call (CE) or Put (PE) options on Underlying Breakouts with Greeks risk control.
    """

    def __init__(
        self,
        symbols: list[str],
        config: Optional[FrameworkConfig] = None,
        custom_params: Optional[dict[str, Any]] = None,
    ):
        super().__init__(
            name="DIRECTIONAL_OPTION_BUYER",
            asset_class=AssetClass.INDEX_OPTIONS,
            symbols=symbols,
            config=config,
            custom_params=custom_params,
        )
        self.strike_mode = self.params.get("strike_mode", StrikeMode.ATM)
        self.strike_offset = self.params.get("strike_offset", 0)  # 0=ATM, 1=OTM1
        self.target_delta = self.params.get("target_delta", 0.50)
        self.profit_target_pct = self.params.get("profit_target_pct", 30.0)  # +30% on premium
        self.stop_loss_pct = self.params.get("stop_loss_pct", 15.0)          # -15% on premium

    def on_start(self, initial_capital: float) -> None:
        pass

    def compute_features(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        df_tech = compute_technical_indicators(df)
        return compute_microstructure_features(df_tech)

    def generate_signal(
        self,
        symbol: str,
        feat_df: pd.DataFrame,
        current_bar: pd.Series,
        bar_idx: int,
        quote: Optional[Quote] = None,
        option_chain: Optional[OptionChainSnapshot] = None,
    ) -> Optional[dict]:
        if bar_idx < 25 or option_chain is None:
            return None

        # Underlying breakout analysis
        rsi = float(current_bar.get("intraday_rsi", 50.0))
        ema_s = float(current_bar.get("ema_slope_pct", 0.0))
        vwap_d = float(current_bar.get("vwap_dist_pct", 0.0))
        adx = float(current_bar.get("adx", 25.0))

        underlying_direction = None
        opt_type = None

        if adx >= 20.0 and ema_s > 0.015 and vwap_d > 0.05 and 52 <= rsi <= 75:
            underlying_direction = Direction.LONG
            opt_type = OptionType.CALL
        elif adx >= 20.0 and ema_s < -0.015 and vwap_d < -0.05 and 25 <= rsi <= 48:
            underlying_direction = Direction.SHORT
            opt_type = OptionType.PUT

        if not opt_type:
            return None

        # Select strike from OptionChain
        selected_contract: Optional[OptionContract] = None
        # Look in option_chain.contracts
        strikes = sorted(option_chain.contracts.keys())
        if not strikes:
            return None

        atm_strike = min(strikes, key=lambda s: abs(s - option_chain.underlying_price))
        atm_idx = strikes.index(atm_strike)

        target_idx = atm_idx + (self.strike_offset if opt_type == OptionType.CALL else -self.strike_offset)
        target_idx = max(0, min(len(strikes) - 1, target_idx))
        target_strike = strikes[target_idx]

        selected_contract = option_chain.contracts.get(target_strike, {}).get(opt_type)
        if not selected_contract or selected_contract.ltp <= 0:
            return None

        opt_price = selected_contract.ltp
        sl_price = round(opt_price * (1.0 - self.stop_loss_pct / 100.0), 2)
        tp_price = round(opt_price * (1.0 + self.profit_target_pct / 100.0), 2)
        sdist = abs(opt_price - sl_price)

        return {
            "symbol": selected_contract.symbol,
            "underlying_symbol": symbol,
            "direction": Direction.LONG,  # Buying option (Long CE or Long PE)
            "option_type": opt_type.value,
            "strike": target_strike,
            "entry_price": opt_price,
            "stop_loss": sl_price,
            "take_profit": tp_price,
            "breakeven": round(opt_price * 1.05, 2),
            "stop_distance": sdist,
            "lot_size": selected_contract.lot_size,
            "score": round(adx, 1),
            "metadata": {
                "underlying_price": option_chain.underlying_price,
                "strike": target_strike,
                "expiry": selected_contract.expiry,
                "delta": selected_contract.greeks.delta if selected_contract.greeks else 0.50,
            },
        }

    def manage_position(
        self,
        position: Position,
        latest_bar: pd.Series,
        now: datetime,
        bar_idx: int,
        quote: Optional[Quote] = None,
    ) -> tuple[Optional[float], Optional[ExitReason]]:
        current_price = quote.ltp if quote else float(latest_bar["close"])

        # Take Profit (+30% gain)
        if current_price >= position.target_price:
            return position.target_price, ExitReason.TAKE_PROFIT

        # Stop Loss (-15% loss)
        if current_price <= position.current_stop:
            return position.current_stop, (ExitReason.BE_STOP if position.armed_be else ExitReason.INITIAL_STOP)

        # 30-minute holding limit
        if (bar_idx - position.entry_bar_idx) >= 6:
            return current_price, ExitReason.TIMEOUT

        # EOD Square-off at 15:15 IST
        if now.hour == 15 and now.minute >= 15:
            return current_price, ExitReason.EOD_SQUAREOFF

        return None, None
