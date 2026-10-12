"""
markets/equity/strategies/tf_daily/fibonacci/strategy.py -- Daily Fibonacci Retracement Swing Strategy.

Traded exclusively on the Top 10 validated high-beta NIFTY 50 equity universe:
APOLLOHOSP, TATASTEEL, MARUTI, BAJAJ-AUTO, HINDALCO, JSWSTEEL, ADANIPORTS, EICHERMOT, BHARTIARTL, SUNPHARMA.

Rules:
- Timeframe: Daily candles (held overnight, delivery product "D", long-only).
- Impulse leg: Swing Low -> Swing High with swing range >= 1.5 * ATR(14) above EMA 50.
- Entry: Pullback into the 50.0% - 61.8% Golden Pocket with bullish candle rejection.
- Stop Loss: Below the swing low - 0.2 * ATR buffer.
- Target: 2:1 Reward-to-Risk (RR2) or prior swing high retest.
"""
from __future__ import annotations

from datetime import datetime
import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from core.strategy import Strategy, Signal, ExitDecision, EntryContext, ExitContext
from markets.equity.costs import compute_nse_equity_delivery_costs, size_equity_shares


class FibonacciDailySwing(Strategy):
    name = "fibonacci"
    market = "equity"
    intraday = False
    product = "D"               # Delivery product (CNC)
    uses_leverage = False        # 1x cash sizing for delivery
    allow_short = False          # Delivery is long-only
    timeframe = ("days", 1)
    lookback_days = 90           # 90 calendar days of daily candles for swing detection
    default_enabled = True       # Active by default
    sector_cap = True

    def __init__(self, swing_lookback: int = 15, rr_ratio: float = 2.0):
        self.swing_lookback = swing_lookback
        self.rr_ratio = rr_ratio

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

        atrs = df["atr"].fillna(df["close"] * 0.01).to_numpy(dtype=np.float64)
        ema50s = df["ema50"].fillna(df["close"]).to_numpy(dtype=np.float64)

        n = len(df)
        lookback = self.swing_lookback
        
        # Find latest swing points
        last_swing_low_val = None
        last_swing_low_idx = None
        last_swing_high_val = None
        last_swing_high_idx = None

        for idx in range(lookback, n - 1):
            w_h = highs[max(0, idx - lookback) : min(n, idx + lookback + 1)]
            if highs[idx] == np.max(w_h):
                last_swing_high_val = highs[idx]
                last_swing_high_idx = idx

            w_l = lows[max(0, idx - lookback) : min(n, idx + lookback + 1)]
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
        curr_bar = n - 1
        curr_close = closes[curr_bar]
        curr_high = highs[curr_bar]
        curr_low = lows[curr_bar]
        curr_open = opens[curr_bar]
        atr_val = atrs[curr_bar]
        ema50_val = ema50s[curr_bar]

        # Trend filter & swing size
        if swing_range < 1.5 * atr_val or curr_close < ema50_val:
            return None

        # Fibonacci Golden Pocket levels
        fib_50 = last_swing_high_val - 0.500 * swing_range
        fib_618 = last_swing_high_val - 0.618 * swing_range
        fib_786 = last_swing_high_val - 0.786 * swing_range

        # Did price enter the Golden Pocket without breaking below 78.6%?
        in_golden_zone = (curr_low <= fib_50) and (curr_low >= fib_786)
        
        # Bullish rejection candle confirmation:
        # 1. Close > Open (green candle)
        # 2. Close > Fib 61.8% level
        # 3. Close in upper half of bar range
        bar_range = max(curr_high - curr_low, 1e-4)
        bullish_rejection = (
            (curr_close > curr_open)
            and (curr_close > fib_618)
            and ((curr_close - curr_low) / bar_range >= 0.50)
        )

        if not (in_golden_zone and bullish_rejection):
            return None

        entry_price = curr_close
        sl = round(last_swing_low_val - (0.2 * atr_val), 2)
        stop_dist = entry_price - sl

        if stop_dist <= 0:
            return None

        tp = round(entry_price + (stop_dist * self.rr_ratio), 2)

        # Cash equity position sizing (1x leverage)
        qty = size_equity_shares(
            capital=ctx.capital,
            entry_price=entry_price,
            stop_distance=stop_dist,
            risk_pct=ctx.risk_pct,
            leverage=1.0,
        )

        if qty <= 0:
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
                "tp": tp,
                "swing_high": last_swing_high_val,
                "swing_low": last_swing_low_val,
            },
            price_levels=("sl", "tp"),
            diagnostics={
                "swing_range": round(swing_range, 2),
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

        if sl > 0 and low_p <= sl:
            return ExitDecision(price=sl, reason="initial_stop")

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
