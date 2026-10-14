"""currency / rsi_range -- fade the extremes of a managed currency: buy when RSI says oversold, short when it says overbought, exit back at the mean.

Rules ("M1", fixed before testing 2026-10-13), daily bars of the back-adjusted futures series (markets/currency/daily_series.py), long and short:
  * buy when RSI(14) is below 30, short when it is above 70 (decided on the close, filled at the next open);
  * exit when the close is back at the 20-day average, or after about 15 trading days; stop 2.5 ATR(14) from the entry;
  * size: 4% of the account at risk per trade (default_risk_pct), in whole lots; costs: NSE currency futures INCLUDING the measured bid-ask spread (markets/currency/costs.py),
    so run it with the backtester's slippage at 0 (the page sets 0 for currency; 5 bp would be ~16 USDINR ticks per side).
Tested on USDINR, EURINR, GBPINR (Rs1 lakh, 20x margin cap, 3 positions): 2023-25 +9.1% (54 trades, 61% winners), 2026 +4.7% (17 trades),
BUT 2013-22 -19.1% (worst fall -28%). It works only while the RBI keeps the rupee in a tight range (since 2023); when the rupee trends it loses.
Risk 4% (2026-10-14, user: "add 4 percent"): chosen from 1-8% as the sweet spot -- 2023-Oct 2026 +55.9% (+12.9%/yr), worst fall -24.2%; 2022 stress
-4.7% (worst fall -17.7%); about a quarter of the Kelly size (16%) measured on only 73 trades. Above 5% the falls reach -28% to -38%.
Paper trading (2026-10-14, user: "run it for live dryrun"; CURRENCY_STRATEGIES=rsi_range, CURRENCY_RSI_RANGE_RISK_PCT=4). The live runner differs, so:
  * history: Upstox's daily candles only cover the current monthly contract (~20 days), so live the strategy reads the nightly back-adjusted series
    (var/archive/currency/<PAIR>_daily_adj.csv, days before today) -- the same data it was backtested on; its newest contract is unadjusted, so prices match;
  * decided on completed days only, entries looked at 09:00-09:45 (near the open, like the backtest's next-open fill);
  * contract expiry (about 2 business days before the month's last business day): no new entry in the last 3 business days before it, and an open
    position is sold 1 business day before it (the backtest's continuous series has no expiry);
  * the runner does not watch a strategy's stop, so manage() sells when today's price touches it; lots are capped at 20x the account / 3 positions.
Pairs: USDINR, EURINR, GBPINR. JPYINR was removed from the project (2026-10-14): with correct per-100-yen math it lost (2023-26 -26%) because the
yen trends on Japan's policy rather than being held in a range by the RBI.
REAL MONEY: not allowed without a genuine currency exposure under the RBI rule of April 2024 -- research and paper only.
"""
from __future__ import annotations

from datetime import datetime, time
from functools import lru_cache

import numpy as np
import pandas as pd

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from core.paths import ARCHIVE_ROOT
from markets.currency.costs import compute_ncd_currency_costs, get_contract_multiplier

RSI_LEN, LOW, HIGH = 14, 30.0, 70.0
MEAN_DAYS, STOP_ATR, MAX_HOLD_DAYS = 20, 2.5, 15
LEVERAGE_CAP, MAX_POSITIONS = 20.0, 3
PAIRS = ("USDINR", "EURINR", "GBPINR")
ENTRY_WINDOW = (time(9, 0), time(9, 45))


def _day(c: dict) -> str:
    return str(c["timestamp"])[:10]


def _is_backtest(now: datetime) -> bool:
    return (now.hour, now.minute) == (0, 0)          # the backtester calls at midnight of the decision day


@lru_cache(maxsize=16)
def _archive(symbol: str, before: str) -> tuple:
    """Back-adjusted daily bars (markets/currency/daily_series.py) for days strictly before `before`."""
    path = ARCHIVE_ROOT / "currency" / f"{symbol}_daily_adj.csv"
    if not path.exists():
        return ()
    df = pd.read_csv(path)
    df = df[df["timestamp"].str[:10] < before].tail(260)
    return tuple(df.to_dict("records"))


def history(candles: list[dict], symbol: str, now: datetime) -> list[dict]:
    """Completed daily bars: the backtester's own (at midnight) or, live, the adjusted archive up to yesterday."""
    return candles if _is_backtest(now) else list(_archive(symbol, now.strftime("%Y-%m-%d")))


def expiry_of(now: datetime) -> pd.Timestamp:
    """This month's NSE currency futures expiry: two business days before the month's last business day (holidays ignored)."""
    last = pd.Timestamp(now.date()) + pd.offsets.BMonthEnd(0)
    return last - pd.offsets.BDay(2)


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
    default_risk_pct = 4.0
    id_prefix = "RRG"

    def trades(self, symbol: str) -> bool:
        return symbol.upper() in PAIRS

    def lot_size(self, symbol: str) -> int:
        return get_contract_multiplier(symbol)

    def due(self, now: datetime) -> bool:
        """Live: look for new entries only in the first 45 minutes, and not in the last 3 business days before expiry."""
        if not (now.weekday() < 5 and ENTRY_WINDOW[0] <= now.time() <= ENTRY_WINDOW[1]):
            return False
        return pd.Timestamp(now.date()) < expiry_of(now) - pd.offsets.BDay(3)

    def entry(self, ctx: EntryContext) -> Signal | None:
        bars = history(ctx.candles, ctx.symbol, ctx.now)[-260:]
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
        lots = int(min(account * ctx.risk_pct / 100 / (dist * lot), account * LEVERAGE_CAP / MAX_POSITIONS / (last * lot)))
        if lots < 1:
            return None
        return Signal(symbol=ctx.symbol, direction="long" if d == 1 else "short", entry_price=last, stop_loss=stop, qty=lots, stop_dist=dist,
                      lot_size=lot, setup_type="rsi oversold" if d == 1 else "rsi overbought")

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        long_ = pos["direction"] == "long"
        if not _is_backtest(ctx.now):
            today = ctx.candles[-1] if ctx.candles and _day(ctx.candles[-1]) == ctx.now.strftime("%Y-%m-%d") else None
            if today is not None:                                       # live: the stop, on today's price (the runner does not check it)
                hit = float(today["low"]) <= pos["current_stop"] if long_ else float(today["high"]) >= pos["current_stop"]
                if hit:
                    o = float(today["open"])
                    return ExitDecision(min(pos["current_stop"], o) if long_ else max(pos["current_stop"], o), "initial_stop")
                if pd.Timestamp(ctx.now.date()) >= expiry_of(ctx.now) - pd.offsets.BDay(1):
                    return ExitDecision(float(today["close"]), "contract_expiry")
            hist = [b["close"] for b in history([], ctx.symbol, ctx.now)]
            closes = pd.Series(hist + ([float(today["close"])] if today is not None else []))
            if len(closes) < MEAN_DAYS:
                return None
            # the exit tests (back at the average / 15 days) use the day's close, as in the backtest: only after 16:45
            if ctx.now.time() < time(16, 45):
                return None
            c = closes
        else:
            c = pd.DataFrame(ctx.candles[-260:])["close"]
        last, mean = float(c.iloc[-1]), float(c.rolling(MEAN_DAYS).mean().iloc[-1])
        if (long_ and last >= mean) or (not long_ and last <= mean):
            return ExitDecision(last, "back_to_20_day_average")
        if (ctx.now - pos["entry_time"]).days >= MAX_HOLD_DAYS * 7 // 5:
            return ExitDecision(last, "time_stop")
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_ncd_currency_costs(symbol, direction, entry, exit_price, qty)    # charges + measured half-spread slippage


STRATEGY = RsiRangeCurrency()
