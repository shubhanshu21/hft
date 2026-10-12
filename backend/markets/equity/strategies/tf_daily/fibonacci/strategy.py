"""
markets/equity/strategies/tf_daily/fibonacci/strategy.py -- Daily Fibonacci Retracement Swing Strategy.

High-Conviction Dynamic Relative Strength Momentum + Golden Pocket Confluence:
- Traded on top relative strength leaders in the NIFTY 50 universe.
- Rules:
  1. Dynamic Leader Qualification: Macro uptrend (Close > EMA 50) and positive 60-day Relative Strength.
  2. Library-Grade Swing Detection: Utilizes `scipy.signal.argrelextrema` for robust swing high/low identification.
  3. Fibonacci Retracement Engine: Canonical mathematical golden ratios (50.0%, 61.8%, 78.6%).
  4. Golden Pocket Entry: Retracement into the 50.0% - 61.8% Fibonacci zone with bullish candle rejection.
  5. Target: Retest of Prior Swing High (0.0% Fibonacci level).
  6. Protection: Trailing Stop moved to Breakeven once +1R profit is achieved.
  7. Sizing: Sized for concentrated positions (up to 50% capital per trade) with dynamic compounding.
"""
from __future__ import annotations

from datetime import datetime
import numpy as np
import pandas as pd
import pandas_ta_classic as ta
from scipy.signal import argrelextrema

from core.strategy import Strategy, Signal, ExitDecision, EntryContext, ExitContext
from markets.equity.costs import compute_nse_equity_delivery_costs, size_equity_shares


def compute_fibonacci_retracements(swing_low: float, swing_high: float) -> dict[str, float]:
    """Calculates canonical Fibonacci Retracement levels from Swing Low to Swing High."""
    swing_range = swing_high - swing_low
    return {
        "0.0": swing_high,                                         # Prior Swing High (retest target)
        "23.6": swing_high - 0.236 * swing_range,                  # Shallow pullback
        "38.2": swing_high - 0.382 * swing_range,                  # Moderate pullback
        "50.0": swing_high - 0.500 * swing_range,                  # Equilibrium midpoint
        "61.8": swing_high - 0.6180339887 * swing_range,           # Golden Pocket
        "78.6": swing_high - 0.7861513778 * swing_range,           # Deep discount boundary
        "100.0": swing_low,                                        # Swing Low
    }


class FibonacciDailySwing(Strategy):
    name = "fibonacci"
    market = "equity"
    intraday = False
    product = "D"               # Delivery product (CNC)
    uses_leverage = False        # 1x cash sizing for delivery
    allow_short = False          # Delivery is long-only
    timeframe = ("days", 1)
    default_enabled = False      # Stays off until explicitly named in EQUITY_STRATEGIES

    def __init__(
        self,
        swing_lookback: int = 15,
        target_mode: str = "prior_high",
        rr_ratio: float = 2.0,
        min_swing_pct: float = 0.06,
        max_alloc_pct: float = 0.50,
    ):
        self.swing_lookback = swing_lookback
        self.target_mode = target_mode
        self.rr_ratio = rr_ratio
        self.min_swing_pct = min_swing_pct
        self.max_alloc_pct = max_alloc_pct

    def entry(self, ctx: EntryContext) -> Signal | None:
        if not ctx.candles or len(ctx.candles) < self.swing_lookback * 2:
            return None

        # Build DataFrame
        df = pd.DataFrame(ctx.candles)
        df["datetime"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("datetime").reset_index(drop=True)

        highs = df["high"].to_numpy(dtype=np.float64)
        lows = df["low"].to_numpy(dtype=np.float64)
        closes = df["close"].to_numpy(dtype=np.float64)
        opens = df["open"].to_numpy(dtype=np.float64)

        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
        df["ema50"] = ta.ema(df["close"], length=50)

        atrs = df["atr"].fillna(df["close"] * 0.015).to_numpy(dtype=np.float64)
        ema50s = df["ema50"].fillna(df["close"]).to_numpy(dtype=np.float64)

        n = len(df)
        lookback = self.swing_lookback
        
        # 1. Macro Trend & Relative Strength Filter
        curr_bar = n - 1
        curr_close = closes[curr_bar]
        curr_high = highs[curr_bar]
        curr_low = lows[curr_bar]
        curr_open = opens[curr_bar]
        atr_val = atrs[curr_bar]
        ema50_val = ema50s[curr_bar]

        # Must be above 50-day EMA
        if curr_close < ema50_val:
            return None

        # 60-day Relative Strength momentum check if enough history exists
        if n >= 60:
            mom60 = (curr_close - closes[curr_bar - 60]) / closes[curr_bar - 60]
            if mom60 < 0:
                return None

        # 2. Scientific Swing Peak and Trough Detection via scipy.signal.argrelextrema
        # We examine completed past bars up to the previous bar
        high_peaks = argrelextrema(highs[:curr_bar], np.greater_equal, order=lookback)[0]
        low_troughs = argrelextrema(lows[:curr_bar], np.less_equal, order=lookback)[0]

        last_swing_high_idx = int(high_peaks[-1]) if len(high_peaks) > 0 else None
        last_swing_high_val = float(highs[last_swing_high_idx]) if last_swing_high_idx is not None else None

        last_swing_low_idx = int(low_troughs[-1]) if len(low_troughs) > 0 else None
        last_swing_low_val = float(lows[last_swing_low_idx]) if last_swing_low_idx is not None else None

        # Fallback to rolling window search if scipy finds no extrema within sparse window
        if last_swing_high_val is None or last_swing_low_val is None:
            for idx in range(lookback, curr_bar):
                w_h = highs[max(0, idx - lookback) : min(curr_bar + 1, idx + lookback + 1)]
                if highs[idx] == np.max(w_h):
                    last_swing_high_val = highs[idx]
                    last_swing_high_idx = idx

                w_l = lows[max(0, idx - lookback) : min(curr_bar + 1, idx + lookback + 1)]
                if lows[idx] == np.min(w_l):
                    last_swing_low_val = lows[idx]
                    last_swing_low_idx = idx

        # Need an established upward impulse (Low followed by High)
        if (
            last_swing_low_val is None
            or last_swing_high_val is None
            or last_swing_high_idx <= last_swing_low_idx
        ):
            return None

        swing_range = last_swing_high_val - last_swing_low_val
        
        # Impulse wave must have expanded at least min_swing_pct (6%)
        if (swing_range / last_swing_low_val) < self.min_swing_pct:
            return None

        # 3. Canonical Fibonacci Retracement Levels
        fib_levels = compute_fibonacci_retracements(last_swing_low_val, last_swing_high_val)
        fib_500 = fib_levels["50.0"]
        fib_618 = fib_levels["61.8"]

        # Pullback must have touched into the 50.0% - 61.8% zone
        in_golden_zone = (curr_low <= fib_500) and (curr_high >= fib_618)
        if not in_golden_zone:
            return None

        # Bullish rejection candle confirmation:
        # Rejection wick >= 25% or green candle (close > open), with close holding above 61.8%
        candle_range = max(curr_high - curr_low, 1e-4)
        lower_wick = min(curr_open, curr_close) - curr_low
        bullish_rejection = (
            ((lower_wick / candle_range >= 0.25) or (curr_close > curr_open))
            and (curr_close >= fib_618)
        )

        if not bullish_rejection:
            return None

        entry_price = curr_close
        sl = round(min(fib_618, curr_low) - (0.5 * atr_val), 2)
        stop_dist = entry_price - sl

        if stop_dist <= 0:
            return None

        # Target: Prior Swing High (Retest) or RR ratio
        if self.target_mode == "prior_high":
            tp = round(last_swing_high_val, 2)
        else:
            tp = round(entry_price + (stop_dist * self.rr_ratio), 2)

        if tp <= entry_price:
            return None

        # Minimum 1:1 Reward-to-Risk check
        if (tp - entry_price) / stop_dist < 1.0:
            return None

        # 4. Position Sizing (Max alloc per position, 1x cash delivery)
        alloc_capital = ctx.capital * self.max_alloc_pct
        shares_by_alloc = int(alloc_capital / entry_price)
        shares_by_risk = size_equity_shares(
            capital=alloc_capital,
            entry_price=entry_price,
            stop_distance=stop_dist,
            risk_pct=ctx.risk_pct,
            leverage=1.0,
        )

        qty = max(1, min(shares_by_alloc, shares_by_risk)) if shares_by_alloc > 0 else 0
        if qty <= 0 or (qty * entry_price) > ctx.capital:
            return None

        return Signal(
            symbol=ctx.symbol,
            direction="long",
            entry_price=entry_price,
            stop_loss=sl,
            qty=qty,
            stop_dist=stop_dist,
            target_price=tp,
            setup_type="fibonacci_golden_pocket",
            exit_state={
                "sl": sl,
                "orig_sl": sl,
                "tp": tp,
                "swing_high": last_swing_high_val,
                "swing_low": last_swing_low_val,
            },
            price_levels=("sl", "tp"),
            diagnostics={
                "swing_range": round(swing_range, 2),
                "fib_500": round(fib_500, 2),
                "fib_618": round(fib_618, 2),
                "atr": round(atr_val, 2),
            },
        )

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        if not ctx.candles:
            return None

        latest_bar = ctx.candles[-1]
        high_p = latest_bar["high"]
        low_p = latest_bar["low"]
        sl = pos.get("sl", pos.get("current_stop", 0.0))
        tp = pos.get("target_price", pos.get("tp", 0.0))
        entry_p = pos.get("entry_price", 0.0)
        orig_sl = pos.get("exit_state", {}).get("orig_sl", sl)

        # 1. Trailing Stop to Breakeven at +1R
        risk_unit = entry_p - orig_sl
        if risk_unit > 0 and high_p >= (entry_p + risk_unit):
            sl = max(sl, entry_p)
            pos["sl"] = sl

        # 2. Check Stop Loss / Breakeven
        if sl > 0 and low_p <= sl:
            reason = "breakeven" if sl >= entry_p else "initial_stop"
            return ExitDecision(price=sl, reason=reason)

        # 3. Check Take Profit Target
        if tp > 0 and high_p >= tp:
            return ExitDecision(price=tp, reason="take_profit")

        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(
            direction=direction,
            entry=entry,
            exit_p=exit_price,
            qty=qty,
            symbol=symbol,
        )


STRATEGY = FibonacciDailySwing()
