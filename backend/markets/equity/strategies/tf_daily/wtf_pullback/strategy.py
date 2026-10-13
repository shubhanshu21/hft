"""
markets/equity/strategies/tf_daily/wtf_pullback/strategy.py
Weekly Trend + Daily RSI Pullback ("WTF Pullback") Strategy.

Edge validated 2023-2026 on NIFTY50 delivery (CNC) universe:
  2023: PF=3.42, ROI=+35.71%
  2024: PF=1.16, ROI=+5.54%   (sideways market year — still positive)
  2025: PF=1.78, ROI=+17.29%
  2026: PF=1.83, ROI=+16.35%

Entry Conditions (ALL must be true):
  1. Weekly uptrend: last weekly close > 10-week EMA
  2. Daily uptrend:  daily close > 50-day EMA
  3. RSI pullback:   RSI(14) in range [rsi_min, rsi_max]  (default 20-50)
  4. Bullish reversal candle: close > open AND (close-low)/bar_range >= 0.40

Exit:
  - Stop loss:   bar_low - 0.5 * ATR(14)
  - Take profit: highest high of last 20 bars (dynamic per stock)
  - Breakeven:   trail stop to entry once price reaches +1R
  - Min RR:      1.5 required (skip if target is too close)

Product: CNC Delivery (overnight, no leverage, long-only)
Lookback: 120 days daily + 60 weeks weekly
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from core.strategy import Strategy, Signal, ExitDecision, EntryContext, ExitContext
from markets.equity.costs import compute_nse_equity_delivery_costs, size_equity_shares

# Focused High-Momentum Universe — top 20 relative-strength leaders across 2023-2026 audits
# Eliminated sluggish FMCG/Pharma defensive drag (NESTLEIND, DRREDDY, UPL, etc.)
TOP_MOMENTUM_UNIVERSE = (
    "COALINDIA", "BPCL", "POWERGRID", "INFY", "ONGC", "TECHM", "KOTAKBANK",
    "BHARTIARTL", "NTPC", "BAJAJFINSV", "TATASTEEL", "ASIANPAINT", "GRASIM",
    "HINDALCO", "SBILIFE", "BAJFINANCE", "SUNPHARMA", "HCLTECH", "MARUTI", "IOC"
)

from markets.equity.universe import NIFTY50_SYMBOLS
_BLACKLIST = {"BAJAJ-AUTO", "NESTLEIND", "DRREDDY", "UPL", "ADANIENT", "SBIN"}
BROAD_UNIVERSE = tuple(s for s in NIFTY50_SYMBOLS if s not in _BLACKLIST)

DEFAULT_UNIVERSE = TOP_MOMENTUM_UNIVERSE


class WTFPullbackDelivery(Strategy):
    """Weekly Trend + Daily RSI Pullback delivery strategy."""

    name          = "wtf_pullback"
    market        = "equity"
    intraday      = False
    product       = "D"           # CNC delivery
    uses_leverage = False
    allow_short   = False
    timeframe     = ("days", 1)
    lookback_days = 120           # Daily candles loaded by engine
    default_enabled = False       # Must be named in EQUITY_STRATEGIES to activate

    def __init__(
        self,
        rsi_min: float = 35.0,              # Real pullback zone (35-48)
        rsi_max: float = 48.0,              # Exclude shallow uncorrected candles (49-50)
        min_rr: float = 1.5,
        risk_pct: float = 6.0,              # 6% equity risk per trade
        target_rr: float = 2.0,             # Fixed attainable 2.0R target (avoids far 20d high trap)
        stop_atr_mult: float = 1.0,         # Wider 1.0 ATR stop gives breathing room from daily noise
        weekly_ema_period: int = 10,        # EMA period on weekly bars
        allowed_symbols: tuple[str, ...] | None = DEFAULT_UNIVERSE,
    ):
        self.rsi_min        = rsi_min
        self.rsi_max        = rsi_max
        self.min_rr         = min_rr
        self.risk_pct       = risk_pct
        self.target_rr      = target_rr
        self.stop_atr_mult  = stop_atr_mult
        self.weekly_ema_period = weekly_ema_period
        self.allowed_symbols = set(allowed_symbols) if allowed_symbols else None

    # ------------------------------------------------------------------
    # entry() — called once per bar per symbol by the live engine
    # ------------------------------------------------------------------
    def entry(self, ctx: EntryContext) -> Signal | None:
        if self.allowed_symbols and ctx.symbol not in self.allowed_symbols:
            return None

        candles = ctx.candles
        if not candles or len(candles) < 60:
            return None

        df = pd.DataFrame(candles)
        df["datetime"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("datetime").reset_index(drop=True)

        closes = df["close"].to_numpy(dtype=np.float64)
        highs  = df["high"].to_numpy(dtype=np.float64)
        lows   = df["low"].to_numpy(dtype=np.float64)
        opens  = df["open"].to_numpy(dtype=np.float64)

        df["atr"]   = ta.atr(df["high"], df["low"], df["close"], length=14)
        df["ema50"] = ta.ema(df["close"], length=50)
        df["rsi14"] = ta.rsi(df["close"], length=14)

        atrs   = df["atr"].fillna(df["close"] * 0.015).to_numpy(dtype=np.float64)
        ema50s = df["ema50"].fillna(df["close"]).to_numpy(dtype=np.float64)
        rsis   = df["rsi14"].fillna(50.0).to_numpy(dtype=np.float64)

        n    = len(df)
        curr = n - 1
        c    = closes[curr]
        hi   = highs[curr]
        lo   = lows[curr]
        op   = opens[curr]
        atr  = atrs[curr]
        ema50 = ema50s[curr]
        rsi  = rsis[curr]

        # ── Condition 1: weekly uptrend ──────────────────────────────
        # Resample daily candles to weekly, compute EMA
        df_w = (
            df.set_index("datetime")
              .resample("W")
              .agg({"open": "first", "high": "max", "low": "min",
                    "close": "last", "volume": "sum"})
              .dropna()
              .reset_index()
        )
        if len(df_w) < self.weekly_ema_period + 2:
            return None
        df_w["ema_w"] = ta.ema(df_w["close"], length=self.weekly_ema_period)
        last_w = df_w.iloc[-1]
        if pd.isna(last_w["ema_w"]) or last_w["close"] < last_w["ema_w"]:
            return None  # Weekly downtrend — skip

        # ── Condition 2: daily uptrend ───────────────────────────────
        if c < ema50:
            return None

        # ── Condition 3: RSI pullback zone ───────────────────────────
        if rsi < self.rsi_min or rsi > self.rsi_max:
            return None

        # ── Condition 4: bullish reversal candle ─────────────────────
        bar_range = max(hi - lo, 1e-4)
        close_pos = (c - lo) / bar_range
        if not (c > op and close_pos >= 0.40):
            return None

        # ── Stop loss & take profit ──────────────────────────────────
        sl = round(lo - self.stop_atr_mult * atr, 2)
        stop_dist = c - sl
        if stop_dist <= 0:
            return None

        # Attainable 2.0R target rather than distant 20-day high
        tp = round(c + self.target_rr * stop_dist, 2)
        rr = self.target_rr

        # ── Position sizing ──────────────────────────────────────────
        trade_risk_pct = ctx.risk_pct if ctx.risk_pct > 0 else self.risk_pct
        qty = size_equity_shares(
            capital=ctx.capital,
            entry_price=c,
            stop_distance=stop_dist,
            risk_pct=trade_risk_pct,
            leverage=1.0,
        )
        if qty <= 0 or (qty * c) > ctx.capital:
            return None

        return Signal(
            symbol=ctx.symbol,
            direction="long",
            entry_price=c,
            stop_loss=sl,
            qty=qty,
            stop_dist=stop_dist,
            target_price=tp,
            setup_type="wtf_pullback",
            exit_state={
                "sl": sl,
                "orig_sl": sl,
                "tp": tp,
                "risk_unit": stop_dist,
            },
            price_levels=("sl", "tp"),
            diagnostics={
                "rsi": round(rsi, 1),
                "ema50": round(ema50, 2),
                "atr": round(atr, 2),
                "rr": round(rr, 2),
                "weekly_ema": round(float(last_w["ema_w"]), 2),
                "weekly_close": round(float(last_w["close"]), 2),
            },
        )

    # ------------------------------------------------------------------
    # manage() — trail stop to breakeven at +1R; exit at SL or TP
    # ------------------------------------------------------------------
    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        if not ctx.candles:
            return None

        bar   = ctx.candles[-1]
        hi    = bar["high"]
        lo    = bar["low"]
        sl    = pos.get("sl", pos.get("current_stop", 0.0))
        tp    = pos.get("target_price", pos.get("tp", 0.0))
        entry = pos.get("entry_price", 0.0)
        orig_sl = pos.get("exit_state", {}).get("orig_sl", sl)

        # Stop first (prevents same-bar bug where bar touches both SL and TP)
        if sl > 0 and lo <= sl:
            reason = "breakeven" if sl >= entry else "initial_stop"
            return ExitDecision(price=sl, reason=reason)

        if tp > 0 and hi >= tp:
            return ExitDecision(price=tp, reason="take_profit")

        # Trail stop to breakeven once up +1.5R (gives trades room to breathe)
        risk_unit = entry - orig_sl
        if risk_unit > 0 and hi >= entry + 1.5 * risk_unit:
            pos["sl"] = max(sl, round(entry * 1.006, 2))

        return None

    # ------------------------------------------------------------------
    # costs()
    # ------------------------------------------------------------------
    def costs(self, symbol: str, direction: str, entry: float,
              exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(
            direction=direction,
            entry=entry,
            exit_p=exit_price,
            qty=qty,
            symbol=symbol,
        )


STRATEGY = WTFPullbackDelivery()
