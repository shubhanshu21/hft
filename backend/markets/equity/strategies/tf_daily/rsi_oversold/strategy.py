"""equity / rsi_oversold -- buy the bounce out of an oversold dip inside an uptrend, sell when the dip has recovered.

Rules (fixed before testing, daily bars, long only, delivery):
  * RSI(14), Wilder smoothing. Oversold = RSI below 30;
  * signal: yesterday RSI was below 30 and today it closed back above 30 (the dip is turning), AND the close is above the 200-day EMA
    (a dip inside an uptrend, not a falling knife);
  * entry at the next open (the engine fills it), stop 2.5 ATR under the close;
  * exit at the close when the price closes back above its 20-day average (where the dip started), or after 15 trading days if nothing else
    happened. RSI is used ONLY for the entry (2026-10-13, user: "this should be only used for entry signals"). Exits compared, chosen on 2023-25:
    back above the 20-day average +7.2% (59% winners, worst fall -4.8%) vs RSI 70 +6.8%, 2R target +3.4%, 1.5R +1.1%, ATR trailing stop +1.9%;
    then judged on 2026: -2.1% vs RSI 70 -2.8% (8 trades).
  * take profit at +5% if that comes first (user: "if we got enough profits then we should exit"). Chosen on 2023-25 among +3/4/5/6%:
    +5% gave +9.5% (vs +7.2% without), worst fall -2.7%; judged on 2026: -1.6% (vs -2.1% without).
No tested edge is claimed; it is here to be judged on the page with real fees and slippage. Not switched on for trading (default_enabled stays False).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.equity.costs import compute_nse_equity_delivery_costs

RSI_LEN, OVERSOLD, EXIT_SMA, STOP_ATR, MAX_HOLD_DAYS = 14, 30.0, 20, 2.5, 15
TAKE_PROFIT_PCT = 5.0       # sell as soon as the trade is up this much (chosen on 2023-25 from 3/4/5/6%)


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
        return Signal(symbol=ctx.symbol, direction="long", entry_price=entry, stop_loss=stop, qty=qty, stop_dist=dist, setup_type="rsi oversold bounce",
                      target_price=entry * (1 + TAKE_PROFIT_PCT / 100))

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        df = pd.DataFrame(ctx.candles)
        if df["close"].iloc[-1] > df["close"].rolling(EXIT_SMA).mean().iloc[-1]:
            return ExitDecision(float(df["close"].iloc[-1]), "back_above_20_day_average")
        if (ctx.now - pos["entry_time"]).days >= MAX_HOLD_DAYS * 7 // 5:           # 15 trading days is about 21 calendar days
            return ExitDecision(float(df["close"].iloc[-1]), "time_stop")
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(direction, entry, exit_price, qty, symbol=symbol)


STRATEGY = RsiOversoldEquity()
