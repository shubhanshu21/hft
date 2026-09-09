"""
strategies/equity/nse_intraday_scalper.py — Configurable 5-Minute Intraday Scalper Strategy for NSE Equities.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Any
import pandas as pd

from framework.types import AssetClass, Direction, ExitReason
from framework.models import Position, Quote, OptionChainSnapshot
from framework.strategy import BaseStrategy
from framework.config import FrameworkConfig
from features.technical import compute_technical_indicators
from features.microstructure import compute_microstructure_features


class NSEIntradayScalper(BaseStrategy):
    """
    5-Minute Intraday Momentum Scalper for Liquid Nifty Equities.
    Uses Microstructure scoring, EMA trend slopes, and dynamic trailing stops.
    """

    def __init__(
        self,
        symbols: list[str],
        config: Optional[FrameworkConfig] = None,
        custom_params: Optional[dict[str, Any]] = None,
    ):
        super().__init__(
            name="NSE_5MIN_EQUITY_SCALPER",
            asset_class=AssetClass.EQUITY,
            symbols=symbols,
            config=config,
            custom_params=custom_params,
        )
        self.up_threshold = self.params.get("up_threshold", 0.60)
        self.down_threshold = self.params.get("down_threshold", 0.40)
        self.hold_minutes = self.params.get("hold_minutes", 15)

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
        if bar_idx < 25:
            return None

        mins = int(current_bar.get("minutes_since_open", 0))
        # Trading session filter: 09:30 to 14:45 IST
        if mins < 15 or mins > 330:
            return None

        vwap_d = float(current_bar.get("vwap_dist_pct", 0.0))
        ema_s = float(current_bar.get("ema_slope_pct", 0.0))
        rsi = float(current_bar.get("intraday_rsi", 50.0))
        entry = float(current_bar["close"])

        # Microstructure p_up calculation
        p = 0.50
        p += 0.08 * (1 if current_bar.get("bar_direction", 0) > 0 else -1)
        p += 0.06 * (1 if vwap_d > 0 else -1)
        p += 0.05 * (1 if ema_s > 0 else -1)
        p = max(0.0, min(1.0, p))

        atr = float(current_bar.get("atr", 0.005 * entry))
        sdist = max(self.config.risk.stop_loss_vol_mult * atr, 0.0035 * entry)

        direction = None
        if p >= self.up_threshold and ema_s >= 0.0 and vwap_d >= 0.0 and 45 <= rsi <= 72:
            direction = Direction.LONG
        elif p <= self.down_threshold and ema_s <= -0.01 and vwap_d <= -0.01 and 25 <= rsi <= 48:
            direction = Direction.SHORT

        if not direction:
            return None

        d = 1 if direction == Direction.LONG else -1
        sl = round(entry - sdist * d, 2)
        tp = round(entry + self.config.risk.profit_target_r_mult * sdist * d, 2)
        be = round(entry + self.config.risk.breakeven_trigger_r_mult * sdist * d, 2)

        return {
            "symbol": symbol,
            "direction": direction,
            "entry_price": round(entry, 2),
            "stop_loss": sl,
            "take_profit": tp,
            "breakeven": be,
            "stop_distance": round(sdist, 4),
            "score": round(p, 3),
            "metadata": {"rsi": round(rsi, 1), "vwap_dist_pct": round(vwap_d, 4), "ema_slope_pct": round(ema_s, 4)},
        }

    def manage_position(
        self,
        position: Position,
        latest_bar: pd.Series,
        now: datetime,
        bar_idx: int,
        quote: Optional[Quote] = None,
    ) -> tuple[Optional[float], Optional[ExitReason]]:
        high = float(latest_bar["high"])
        low = float(latest_bar["low"])
        close = float(latest_bar["close"])

        d = 1 if position.direction == Direction.LONG else -1
        fav = high if d == 1 else low
        adv = low if d == 1 else high

        # Take Profit
        if (fav >= position.target_price if d == 1 else fav <= position.target_price):
            return position.target_price, ExitReason.TAKE_PROFIT

        # Stop Loss / Breakeven Stop
        if (adv <= position.current_stop if d == 1 else adv >= position.current_stop):
            return position.current_stop, (ExitReason.BE_STOP if position.armed_be else ExitReason.INITIAL_STOP)

        # Timeout Exit (Hold minutes limit)
        if (bar_idx - position.entry_bar_idx) >= (self.hold_minutes // 5):
            return close, ExitReason.TIMEOUT

        # EOD Square-off at 15:20 IST (5 mins before Upstox 15:25 RMS auto-squareoff)
        if now.hour == 15 and now.minute >= 20:
            return close, ExitReason.EOD_SQUAREOFF

        # Breakeven & Trailing Stop update
        if not position.armed_be and (fav >= position.breakeven_price if d == 1 else fav <= position.breakeven_price):
            position.armed_be = True
            position.current_stop = position.entry_price + self.config.risk.breakeven_lock_pct * position.entry_price * d

        if position.armed_be:
            position.best_price = max(position.best_price, fav) if d == 1 else min(position.best_price, fav)
            trail = position.best_price - self.config.risk.trailing_stop_dist_mult * abs(position.entry_price - position.current_stop) * d
            position.current_stop = max(position.current_stop, trail) if d == 1 else min(position.current_stop, trail)

        return None, None
