"""equity / ema_cross_demo -- DEMO strategy for the web backtester, not a trading idea.

Buys when the 20-day EMA crosses above the 50-day EMA and the close is above both; stop 2 ATR below, exit when the 20-day EMA falls back under the 50-day.
It exists so the "New backtest" page has something to run end to end. It has no tested edge and is never switched on for paper trading
(default_enabled stays False; it is not named in EQUITY_STRATEGIES).
"""
from __future__ import annotations

import pandas as pd

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.equity.costs import compute_nse_equity_delivery_costs


def _frame(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["e20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["e50"] = df["close"].ewm(span=50, adjust=False).mean()
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df


class EmaCrossDemoEquity(Strategy):
    name = "ema_cross_demo"
    market = "equity"
    intraday = False
    product = "D"
    uses_leverage = False
    allow_short = False
    timeframe = ("days", 1)
    lookback_days = 250
    id_prefix = "EMX"

    def entry(self, ctx: EntryContext) -> Signal | None:
        if len(ctx.candles) < 60:
            return None
        df = _frame(ctx.candles)
        a, b = df.iloc[-1], df.iloc[-2]
        if pd.isna(a["atr"]) or not (b["e20"] <= b["e50"] and a["e20"] > a["e50"] and a["close"] > a["e20"]):
            return None
        entry = float(a["close"])
        stop = entry - 2 * float(a["atr"])
        dist = entry - stop
        qty = int(min(ctx.capital * ctx.risk_pct / 100 / dist, ctx.capital / entry))
        if qty < 1:
            return None
        return Signal(symbol=ctx.symbol, direction="long", entry_price=entry, stop_loss=stop, qty=qty, stop_dist=dist, setup_type="ema_cross")

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        df = _frame(ctx.candles)
        a = df.iloc[-1]
        if a["e20"] < a["e50"]:
            return ExitDecision(float(a["close"]), "trend_end")
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(direction, entry, exit_price, qty, symbol=symbol)


STRATEGY = EmaCrossDemoEquity()
