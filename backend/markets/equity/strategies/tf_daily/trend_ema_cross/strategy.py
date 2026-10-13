"""equity / trend_ema_cross -- ride rallies: buy when a short-term uptrend starts inside a long-term uptrend in a healthy market, let it run.

Rules (fixed before testing; "T4B" in the 2026-10-13 trend comparison), daily bars, long only, delivery:
  * entry: the 20-day EMA crosses above the 50-day EMA, the close is above the 200-day EMA, and at least 50% of the 49 NIFTY stocks are
    above their own 50-day EMA (a healthy market). All of it on COMPLETED daily bars: decided on a day's close, bought at the next open;
  * size: 2% of the account at risk, never more than a third of the account in one stock, at most 3 stocks at once;
  * stop: 2.5 ATR(14) under the buy; then a 3-ATR trailing stop under the highest price since the buy (a Chandelier stop) that only moves up.
    No profit target and no time limit, so a big rally can run.
Tested on the 49 stocks, Rs1 lakh, Upstox delivery fees and 5 bp slippage, chosen among 8 trend designs on 2023-25:
  2023-25 +29.7% (54 trades, 41% winners, profit factor 1.65, worst fall -18.4%); judged on 2026: +8.3% (12 trades, 58% winners).
Stocks traded (2026-10-13, user: "only use those"): the 20 stocks whose signals made the most in 2023-25 (BEST_SYMBOLS below). Ranked on 2023-25 only,
then judged on 2026: top 20 +11.2% (9 trades) vs all 49 +8.3%, and it beat all 8 random groups of 20 stocks (-8.6% to +10.9%); top 10 and top 15 also
beat every random group of their size. A backtest of 2023-25 with this list is NOT a fair test (the list was chosen on those years). Re-rank it each
January on the latest three years. The market check still uses all 49 stocks.

Paper trading (engine/live_dryrun.py, switched on 2026-10-13 with EQUITY_STRATEGIES=trend_ema_cross). The runner differs from the backtester, so:
  * the runner's candles include today's still-forming daily bar: entry() and the trailing stop ignore it and use completed days only;
  * due(): entries are looked at only in the first 45 minutes of the session, so the buy lands near the open as in the backtest;
  * the runner's other-symbol data is 5-minute bars, so the market check reads the nightly daily archive (var/archive/equity/*_daily.csv, days before today);
  * the runner does not watch a strategy's stop, so manage() sells when today's price touches the stop (at the stop, or at the open after a gap).
"""
from __future__ import annotations

from datetime import datetime, time
from functools import lru_cache

import pandas as pd

from core.paths import ARCHIVE_ROOT
from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.equity.costs import compute_nse_equity_delivery_costs
from markets.equity.universe import NIFTY50_SYMBOLS

FAST, SLOW, LONG = 20, 50, 200
MIN_BREADTH_PCT = 50.0
STOP_ATR, TRAIL_ATR = 2.5, 3.0
MAX_POSITIONS = 3
WINDOW = 260                    # bars used for the averages, the same in the backtester and live
ENTRY_WINDOW = (time(9, 15), time(10, 0))       # live: buy near the open, like the backtest's next-open fill
BEST_SYMBOLS = ("ADANIPORTS", "BPCL", "COALINDIA", "IOC", "SBILIFE", "ONGC", "TITAN", "WIPRO", "EICHERMOT", "SUNPHARMA",
                "INFY", "ASIANPAINT", "APOLLOHOSP", "HEROMOTOCO", "MARUTI", "TCS", "BAJAJFINSV", "BRITANNIA", "RELIANCE", "HCLTECH")   # ranked on 2023-25


def _day(candle: dict) -> str:
    return str(candle["timestamp"])[:10]


def completed(candles: list[dict], now: datetime) -> list[dict]:
    """Daily candles without today's still-forming bar. A backtest calls at midnight of the decision day, whose bar is complete; the live runner calls
    during the session, when the bar dated today is not finished yet."""
    if candles and (now.hour, now.minute) != (0, 0) and _day(candles[-1]) == now.strftime("%Y-%m-%d"):
        return candles[:-1]
    return candles


def atr(df: pd.DataFrame, n: int = 14) -> float:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


@lru_cache(maxsize=64)
def _archive_closes(symbol: str, before: str) -> tuple[float, ...]:
    """Closes from the nightly daily archive for days strictly before `before` (YYYY-MM-DD)."""
    path = ARCHIVE_ROOT / "equity" / f"{symbol}_daily.csv"
    if not path.exists():
        return ()
    df = pd.read_csv(path, usecols=["timestamp", "close"])
    return tuple(df.loc[df["timestamp"].str[:10] < before, "close"].astype(float).tail(260))


def _daily_closes(ctx: EntryContext, symbol: str) -> list[float]:
    """Another stock's daily closes up to the decision day: the backtester's view when it provides daily bars, else the nightly archive."""
    cs = ctx.data.candles(symbol) if ctx.data is not None else []
    if len(cs) >= 2 and _day(cs[-1]) != _day(cs[-2]):                     # daily bars (the backtester's view)
        return [x["close"] for x in completed(cs, ctx.now)]
    return list(_archive_closes(symbol, ctx.now.strftime("%Y-%m-%d")))      # live: the runner's other-symbol data is 5-minute bars


def breadth(ctx: EntryContext) -> float:
    """% of the 49 NIFTY stocks whose close is above their own 50-day EMA on the decision day."""
    above = total = 0
    for sym in NIFTY50_SYMBOLS:
        closes = _daily_closes(ctx, sym)
        if len(closes) < 60:
            continue
        c = pd.Series(closes)
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
    lookback_days = 400              # calendar days: enough for ~260 trading days and the 200-day EMA
    max_positions = MAX_POSITIONS
    id_prefix = "TRD"

    def trades(self, symbol: str) -> bool:
        """Only the 20 best stocks of 2023-25 (an EQUITY_TREND_EMA_CROSS_SYMBOLS list in .env still overrides it)."""
        import os
        if os.environ.get("EQUITY_TREND_EMA_CROSS_SYMBOLS", "").strip():
            return super().trades(symbol)
        return symbol.upper() in BEST_SYMBOLS

    def due(self, now: datetime) -> bool:
        """Live: look for new entries only in the first 45 minutes of the session (the backtester never calls this)."""
        return now.weekday() < 5 and ENTRY_WINDOW[0] <= now.time() <= ENTRY_WINDOW[1]

    def entry(self, ctx: EntryContext) -> Signal | None:
        bars = completed(ctx.candles, ctx.now)[-WINDOW:]
        if len(bars) < 210:
            return None
        df = pd.DataFrame(bars)
        c = df["close"]
        fast, slow, long_ = (c.ewm(span=n, adjust=False).mean() for n in (FAST, SLOW, LONG))
        last = float(c.iloc[-1])
        if not (fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1] and last > long_.iloc[-1]):
            return None
        if breadth(ctx) < MIN_BREADTH_PCT:
            return None                                              # weak market: most stocks below their 50-day average
        stop = last - STOP_ATR * atr(df)
        dist = last - stop
        if not dist > 0:
            return None
        account = ctx.account_equity or ctx.capital
        qty = int(min(account * ctx.risk_pct / 100 / dist, account / MAX_POSITIONS / last, ctx.capital / last))
        if qty < 1:
            return None
        return Signal(symbol=ctx.symbol, direction="long", entry_price=last, stop_loss=stop, qty=qty, stop_dist=dist, setup_type="trend ema cross")

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        """Sell when today's price reaches the stop; after each completed day, raise the trailing stop to 3 ATR under the highest price since the buy.
        In the backtester the engine already checked today's bar against the stop, so the check below never fires there."""
        candles = ctx.candles
        if candles and _day(candles[-1]) == ctx.now.strftime("%Y-%m-%d") and (ctx.now.hour, ctx.now.minute) != (0, 0):
            today = candles[-1]                                       # live: today's forming bar
            if float(today["low"]) <= pos["current_stop"]:
                return ExitDecision(min(pos["current_stop"], float(today["open"])), "trailing_stop" if pos["current_stop"] > pos.get("sl", pos["current_stop"]) else "initial_stop")
        bars = completed(candles, ctx.now)[-WINDOW:]
        if len(bars) < 20:
            return None
        entry_day = pos["entry_time"].strftime("%Y-%m-%d") if hasattr(pos.get("entry_time"), "strftime") else ""
        since = [b["high"] for b in bars if _day(b) >= entry_day]
        if since:
            pos["best_price"] = max(pos["best_price"], max(since))
        pos["current_stop"] = max(pos["current_stop"], pos["best_price"] - TRAIL_ATR * atr(pd.DataFrame(bars)))
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(direction, entry, exit_price, qty, symbol=symbol)


STRATEGY = TrendEmaCrossEquity()
