"""
markets/equity/strategies/tf_daily/fibonacci/strategy.py -- Daily Fibonacci Retracement Swing Strategy.

Features:
- Timeframe: Daily candles (held overnight, delivery product "D", long-only).
- Lookback history: 120 days loaded by the live engine (`lookback_days = 120`).
- Macro Trend: Price above 50-day EMA.
- Impulse Expansion: Upward impulse wave >= 1.5 * ATR(14) above EMA 50 (>= 5% swing).
- Golden Pocket Entry: Retracement into 50.0% - 61.8% Fibonacci zone with bullish rejection candle.
- Target: Prior Swing High (0.0% Fib) or fixed RR ratio.
- Protection: Trailing Stop moved to Breakeven once price reaches +1R in profit (armed for subsequent bars).
- Sizing: Risk-based sizing respecting 1x cash delivery margin.
"""
from __future__ import annotations

from datetime import datetime
import numpy as np
import pandas as pd
import pandas_ta_classic as ta
from scipy.signal import argrelextrema

from core.strategy import Strategy, Signal, ExitDecision, EntryContext, ExitContext
from markets.equity.costs import compute_nse_equity_delivery_costs, size_equity_shares

TOP_20_EQUITY_UNIVERSE = (
    "APOLLOHOSP", "TATASTEEL", "MARUTI", "BAJAJ-AUTO", "HINDALCO",
    "JSWSTEEL", "ADANIPORTS", "EICHERMOT", "BHARTIARTL", "SUNPHARMA",
    "IOC", "SBIN", "ULTRACEMCO", "SBILIFE", "ASIANPAINT",
    "COALINDIA", "HEROMOTOCO", "ONGC", "LT", "GRASIM"
)


def compute_fibonacci_retracements(swing_low: float, swing_high: float) -> dict[str, float]:
    """Calculates canonical Fibonacci Retracement and Extension levels from Swing Low to Swing High."""
    swing_range = swing_high - swing_low
    return {
        "-27.2": swing_high + 0.272 * swing_range,                 # Fibonacci Extension Target 2
        "0.0": swing_high,                                         # Prior Swing High (0.0% Fib Target 1)
        "23.6": swing_high - 0.236 * swing_range,                  # Shallow pullback
        "38.2": swing_high - 0.382 * swing_range,                  # Moderate pullback
        "50.0": swing_high - 0.500 * swing_range,                  # Equilibrium midpoint (Discount boundary)
        "61.8": swing_high - 0.6180339887 * swing_range,           # Golden Ratio / OTE upper boundary
        "70.5": swing_high - 0.705 * swing_range,                  # Optimal Trade Entry (OTE) sweet spot
        "78.6": swing_high - 0.7861513778 * swing_range,           # Deep discount invalidation boundary
        "100.0": swing_low,                                        # Swing Low (1.0 level)
    }


class FibonacciDailySwing(Strategy):
    name = "fibonacci"
    market = "equity"
    intraday = False
    product = "D"               # Delivery product (CNC)
    uses_leverage = False        # 1x cash sizing for delivery
    allow_short = False          # Delivery is long-only
    timeframe = ("days", 1)
    lookback_days = 120          # Required by engine/live_dryrun.py to fetch 120 days of daily candles
    default_enabled = False      # Stays off until explicitly named in EQUITY_STRATEGIES

    def __init__(
        self,
        swing_lookback: int = 15,
        target_mode: str = "prior_high",
        rr_ratio: float = 2.0,
        min_swing_pct: float = 0.05,
        risk_pct: float = 3.0,
        min_rr: float = 1.2,
        allowed_symbols: tuple[str, ...] | None = TOP_20_EQUITY_UNIVERSE,
    ):
        self.swing_lookback = swing_lookback
        self.target_mode = target_mode
        self.rr_ratio = rr_ratio
        self.min_swing_pct = min_swing_pct
        self.risk_pct = risk_pct
        self.min_rr = min_rr
        self.allowed_symbols = set(allowed_symbols) if allowed_symbols else None

    def entry(self, ctx: EntryContext) -> Signal | None:
        if self.allowed_symbols and ctx.symbol not in self.allowed_symbols:
            return None

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
        
        # 1. Macro Trend Check: Price above 50 EMA
        curr_bar = n - 1
        curr_close = closes[curr_bar]
        curr_high = highs[curr_bar]
        curr_low = lows[curr_bar]
        curr_open = opens[curr_bar]
        atr_val = atrs[curr_bar]
        ema50_val = ema50s[curr_bar]

        if curr_close < ema50_val:
            return None

        # 2. Scientific Swing Detection via scipy.signal.argrelextrema
        high_peaks = argrelextrema(highs[:curr_bar], np.greater_equal, order=lookback)[0]
        low_troughs = argrelextrema(lows[:curr_bar], np.less_equal, order=lookback)[0]

        last_swing_high_idx = int(high_peaks[-1]) if len(high_peaks) > 0 else None
        last_swing_high_val = float(highs[last_swing_high_idx]) if last_swing_high_idx is not None else None

        last_swing_low_idx = int(low_troughs[-1]) if len(low_troughs) > 0 else None
        last_swing_low_val = float(lows[last_swing_low_idx]) if last_swing_low_idx is not None else None

        # Fallback to rolling search if extrema sparse
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

        # Need established upward impulse
        if (
            last_swing_low_val is None
            or last_swing_high_val is None
            or last_swing_high_idx <= last_swing_low_idx
        ):
            return None

        swing_range = last_swing_high_val - last_swing_low_val
        
        # Impulse must expand >= 1.5 * ATR and >= min_swing_pct
        if swing_range < 1.5 * atr_val or (swing_range / last_swing_low_val) < self.min_swing_pct:
            return None

        # 3. Canonical Fibonacci Golden Pocket Levels
        fib_levels = compute_fibonacci_retracements(last_swing_low_val, last_swing_high_val)
        fib_500 = fib_levels["50.0"]
        fib_618 = fib_levels["61.8"]
        fib_786 = fib_levels["78.6"]

        # Pullback must reach the 50.0% - 61.8% zone without breaking 78.6%
        in_golden_zone = (curr_low <= fib_500) and (curr_low >= fib_786)
        if not in_golden_zone:
            return None

        # Bullish rejection candle confirmation:
        # Green candle (close > open) holding above 61.8% with close in upper 40% of range
        bar_range = max(curr_high - curr_low, 1e-4)
        bullish_rejection = (
            (curr_close > curr_open)
            and (curr_close >= fib_618)
            and ((curr_close - curr_low) / bar_range >= 0.40)
        )

        if not bullish_rejection:
            return None

        entry_price = curr_close
        sl = round(last_swing_low_val - 0.2 * atr_val, 2)
        stop_dist = entry_price - sl

        if stop_dist <= 0:
            return None

        # 4. Take Profit Target (0.0 Prior High, -0.272 Extension, or fixed RR)
        if self.target_mode == "prior_high":
            tp = round(fib_levels["0.0"], 2)
        elif self.target_mode == "extension_272":
            tp = round(fib_levels["-27.2"], 2)
        else:
            tp = round(entry_price + (stop_dist * self.rr_ratio), 2)

        if tp <= entry_price:
            return None

        # Risk-to-Reward gate: require minimum expected reward relative to stop
        rr_expected = (tp - entry_price) / stop_dist
        if self.min_rr > 0 and rr_expected < self.min_rr:
            return None

        # 5. Risk-Based Fractional Sizing
        trade_risk_pct = ctx.risk_pct if ctx.risk_pct > 0 else self.risk_pct
        qty = size_equity_shares(
            capital=ctx.capital,
            entry_price=entry_price,
            stop_distance=stop_dist,
            risk_pct=trade_risk_pct,
            leverage=1.0,
        )

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
                "risk_unit": stop_dist,
            },
            price_levels=("sl", "tp"),
            diagnostics={
                "swing_range": round(swing_range, 2),
                "fib_500": round(fib_500, 2),
                "fib_618": round(fib_618, 2),
                "atr": round(atr_val, 2),
                "rr_ratio": self.rr_ratio,
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

        # 1. Check existing Stop Loss / Breakeven FIRST (prevents same-bar dip exit bug)
        if sl > 0 and low_p <= sl:
            reason = "breakeven" if sl >= entry_p else "initial_stop"
            return ExitDecision(price=sl, reason=reason)

        # 2. Check Take Profit Target
        if tp > 0 and high_p >= tp:
            return ExitDecision(price=tp, reason="take_profit")

        # 3. Arm / trail stop to breakeven for SUBSEQUENT bars once price reaches +1R
        risk_unit = entry_p - orig_sl
        if risk_unit > 0 and high_p >= (entry_p + risk_unit):
            pos["sl"] = max(sl, entry_p)

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
