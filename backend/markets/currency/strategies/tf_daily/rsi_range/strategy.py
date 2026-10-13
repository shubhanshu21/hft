"""currency / rsi_range -- fade the extremes of a managed currency: buy when RSI says oversold, short when it says overbought, exit back at the mean.

Rules ("M1", fixed before testing 2026-10-13), daily bars of the back-adjusted futures series (markets/currency/daily_series.py), long and short:
  * buy when RSI(14) is below 30, short when it is above 70 (decided on the close, filled at the next open);
  * exit when the close is back at the 20-day average, or after about 15 trading days; stop 2.5 ATR(14) from the entry;
  * size: 2% of the account at risk, in whole lots; costs: NSE currency futures INCLUDING the measured bid-ask spread (markets/currency/costs.py),
    so run it with the backtester's slippage at 0 (the page sets 0 for currency; 5 bp would be ~16 USDINR ticks per side).
Tested on USDINR, EURINR, GBPINR, JPYINR (Rs1 lakh, 20x margin cap, 3 positions): 2023-25 +9.1% (54 trades, 61% winners), 2026 +4.7% (17 trades),
BUT 2013-22 -19.1% (worst fall -28%). It works only while the RBI keeps the rupee in a tight range (since 2023); when the rupee trends it loses.
Not switched on for paper trading (default_enabled stays False; it would also need the live handling trend_ema_cross has).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.currency.costs import compute_ncd_currency_costs, get_contract_multiplier

RSI_LEN, LOW, HIGH = 14, 30.0, 70.0
MEAN_DAYS, STOP_ATR, MAX_HOLD_DAYS = 20, 2.5, 15


def rsi(close: pd.Series, n: int = RSI_LEN) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int = 14) -> float:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


class RsiRangeCurrency(Strategy):
    name = "rsi_range"
    market = "currency"
    intraday = False
    product = "D"
    uses_leverage = True
    allow_short = True
    timeframe = ("days", 1)
    lookback_days = 260
    max_positions = 3
    id_prefix = "RRG"

    def lot_size(self, symbol: str) -> int:
        return get_contract_multiplier(symbol)

    def entry(self, ctx: EntryContext) -> Signal | None:
        bars = ctx.candles[-260:]
        if len(bars) < 60:
            return None
        df = pd.DataFrame(bars)
        r = float(rsi(df["close"]).iloc[-1])
        d = 1 if r < LOW else (-1 if r > HIGH else 0)
        if d == 0:
            return None
        last = float(df["close"].iloc[-1])
        stop = last - d * STOP_ATR * atr(df)
        dist = abs(last - stop)
        lot = self.lot_size(ctx.symbol)
        account = ctx.account_equity or ctx.capital
        lots = int(account * ctx.risk_pct / 100 / (dist * lot))
        if lots < 1:
            return None
        return Signal(symbol=ctx.symbol, direction="long" if d == 1 else "short", entry_price=last, stop_loss=stop, qty=lots, stop_dist=dist,
                      lot_size=lot, setup_type="rsi oversold" if d == 1 else "rsi overbought")

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        c = pd.DataFrame(ctx.candles[-260:])["close"]
        last, mean = float(c.iloc[-1]), float(c.rolling(MEAN_DAYS).mean().iloc[-1])
        long_ = pos["direction"] == "long"
        if (long_ and last >= mean) or (not long_ and last <= mean):
            return ExitDecision(last, "back_to_20_day_average")
        if (ctx.now - pos["entry_time"]).days >= MAX_HOLD_DAYS * 7 // 5:
            return ExitDecision(last, "time_stop")
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_ncd_currency_costs(symbol, direction, entry, exit_price, qty)    # charges + measured half-spread slippage


STRATEGY = RsiRangeCurrency()
