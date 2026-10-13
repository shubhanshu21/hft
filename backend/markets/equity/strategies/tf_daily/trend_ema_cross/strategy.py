"""equity / trend_ema_cross -- ride rallies: buy when a short-term uptrend starts inside a long-term uptrend in a healthy market, let it run.

Rules (fixed before testing; "T4B" in the 2026-10-13 trend comparison), daily bars, long only, delivery:
  * entry: the 20-day EMA crosses above the 50-day EMA today, the close is above the 200-day EMA, and at least 50% of the 49 NIFTY stocks are
    above their own 50-day EMA (a healthy market; skipped when the runner gives no other-symbol data);
  * stop: 2.5 ATR(14) under the buy; then a 3-ATR trailing stop under the highest price since the buy (a Chandelier stop) that only moves up.
    No profit target and no time limit, so a big rally can run.
Tested on the 49 stocks, Rs1 lakh, Upstox delivery fees and 5 bp slippage, chosen among 8 trend designs on 2023-25:
  2023-25 +29.7% (54 trades, 41% winners, profit factor 1.65, worst fall -18.4%); judged on 2026: +8.3% (12 trades, 58% winners).
Stocks traded (2026-10-13, user: "only use those"): the 20 stocks whose signals made the most in 2023-25 (BEST_SYMBOLS below). Ranked on 2023-25 only,
then judged on 2026: top 20 +11.2% (9 trades) vs all 49 +8.3%, and it beat all 8 random groups of 20 stocks (-8.6% to +10.9%); top 10 and top 15 also
beat every random group of their size. A backtest of 2023-25 with this list is NOT a fair test (the list was chosen on those years). Re-rank it each
January on the latest three years. The market check still uses all 49 stocks.
Not switched on for paper trading (default_enabled stays False).
"""
from __future__ import annotations

import pandas as pd

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.equity.costs import compute_nse_equity_delivery_costs
from markets.equity.universe import NIFTY50_SYMBOLS

FAST, SLOW, LONG = 20, 50, 200
MIN_BREADTH_PCT = 50.0
STOP_ATR, TRAIL_ATR = 2.5, 3.0
BEST_SYMBOLS = ("ADANIPORTS", "BPCL", "COALINDIA", "IOC", "SBILIFE", "ONGC", "TITAN", "WIPRO", "EICHERMOT", "SUNPHARMA",
                "INFY", "ASIANPAINT", "APOLLOHOSP", "HEROMOTOCO", "MARUTI", "TCS", "BAJAJFINSV", "BRITANNIA", "RELIANCE", "HCLTECH")   # ranked on 2023-25


def atr(df: pd.DataFrame, n: int = 14) -> float:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def breadth(ctx: EntryContext) -> float:
    """% of the 49 NIFTY stocks whose close is above their own 50-day EMA today."""
    above = total = 0
    for sym in NIFTY50_SYMBOLS:
        cs = ctx.data.candles(sym)
        if len(cs) < 60:
            continue
        c = pd.Series([x["close"] for x in cs])
        total += 1
        above += c.iloc[-1] > c.ewm(span=50, adjust=False).mean().iloc[-1]
    return 100.0 * above / total if total else 100.0


class TrendEmaCrossEquity(Strategy):
    name = "trend_ema_cross"
    market = "equity"
    intraday = False
    product = "D"
    uses_leverage = False
    allow_short = False
    timeframe = ("days", 1)
    lookback_days = 260
    id_prefix = "TRD"

    def trades(self, symbol: str) -> bool:
        """Only the 20 best stocks of 2023-25 (an EQUITY_TREND_EMA_CROSS_SYMBOLS list in .env still overrides it)."""
        import os
        if os.environ.get("EQUITY_TREND_EMA_CROSS_SYMBOLS", "").strip():
            return super().trades(symbol)
        return symbol.upper() in BEST_SYMBOLS

    def entry(self, ctx: EntryContext) -> Signal | None:
        if len(ctx.candles) < 210:
            return None
        df = pd.DataFrame(ctx.candles)
        c = df["close"]
        fast, slow, long_ = (c.ewm(span=n, adjust=False).mean() for n in (FAST, SLOW, LONG))
        last = float(c.iloc[-1])
        if not (fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1] and last > long_.iloc[-1]):
            return None
        if ctx.data is not None and breadth(ctx) < MIN_BREADTH_PCT:
            return None                                              # weak market: most stocks below their 50-day average
        stop = last - STOP_ATR * atr(df)
        dist = last - stop
        if not dist > 0:
            return None
        qty = int(min(ctx.capital * ctx.risk_pct / 100 / dist, ctx.capital / last))
        if qty < 1:
            return None
        return Signal(symbol=ctx.symbol, direction="long", entry_price=last, stop_loss=stop, qty=qty, stop_dist=dist, setup_type="trend ema cross")

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        """Raise the trailing stop to 3 ATR under the highest price since the buy; the engine sells when a later bar reaches it."""
        pos["current_stop"] = max(pos["current_stop"], pos["best_price"] - TRAIL_ATR * atr(pd.DataFrame(ctx.candles)))
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(direction, entry, exit_price, qty, symbol=symbol)


STRATEGY = TrendEmaCrossEquity()
