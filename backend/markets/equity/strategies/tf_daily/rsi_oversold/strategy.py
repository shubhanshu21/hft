"""equity / rsi_oversold -- buy the bounce out of an oversold dip inside an uptrend, sell when the dip has recovered.

Rules (fixed before testing, daily bars, long only, delivery):
  * RSI(14), Wilder smoothing. Oversold = RSI below 30;
  * signal: yesterday RSI was below 30 and today it closed back above 30 (the dip is turning), AND the close is above the 200-day EMA
    (a dip inside an uptrend, not a falling knife);
  * entry at the next open (the engine fills it), stop 2.5 ATR under the close;
  * exit at the close when RSI rises above 70 (was 60 until 2026-10-13: 30/70 tested +4.3% vs -0.4% over 2023-26, 41 trades each),
    or after 15 trading days if nothing else happened.
No tested edge is claimed; it is here to be judged on the page with real fees and slippage. Not switched on for trading (default_enabled stays False).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.equity.costs import compute_nse_equity_delivery_costs

RSI_LEN, OVERSOLD, EXIT_RSI, STOP_ATR, MAX_HOLD_DAYS = 14, 30.0, 70.0, 2.5, 15


def rsi(close: pd.Series, n: int = RSI_LEN) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


class RsiOversoldEquity(Strategy):
    name = "rsi_oversold"
    market = "equity"
    intraday = False
    product = "D"
    uses_leverage = False
    allow_short = False
    timeframe = ("days", 1)
    lookback_days = 250
    id_prefix = "RSI"

    def entry(self, ctx: EntryContext) -> Signal | None:
        if len(ctx.candles) < 200:
            return None
        df = pd.DataFrame(ctx.candles)
        r = rsi(df["close"])
        if not (r.iloc[-2] < OVERSOLD <= r.iloc[-1]):
            return None
        e200 = df["close"].ewm(span=200, adjust=False).mean().iloc[-1]
        entry = float(df["close"].iloc[-1])
        if entry <= e200:
            return None
        prev = df["close"].shift()
        tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        stop = entry - STOP_ATR * atr
        dist = entry - stop
        if not dist > 0:
            return None
        qty = int(min(ctx.capital * ctx.risk_pct / 100 / dist, ctx.capital / entry))
        if qty < 1:
            return None
        return Signal(symbol=ctx.symbol, direction="long", entry_price=entry, stop_loss=stop, qty=qty, stop_dist=dist, setup_type="rsi oversold bounce")

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        df = pd.DataFrame(ctx.candles)
        if rsi(df["close"]).iloc[-1] > EXIT_RSI:
            return ExitDecision(float(df["close"].iloc[-1]), "rsi_recovered")
        if (ctx.now - pos["entry_time"]).days >= MAX_HOLD_DAYS * 7 // 5:           # 15 trading days is about 21 calendar days
            return ExitDecision(float(df["close"].iloc[-1]), "time_stop")
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(direction, entry, exit_price, qty, symbol=symbol)


STRATEGY = RsiOversoldEquity()
