"""commodity / gold_crude -- hold gold while it is in an uptrend; fade crude's extremes. MCX, daily bars, Rs1 lakh sized.

Two parts, one account (rules fixed 2026-10-14 after the commodity tests; see the numbers below):
  * GOLDPETAL (1 g gold, ~Rs15k a lot): hold it whenever the close is above its 100-day EMA -- not only on the day a crossover happens, because in
    2024-25 gold rose ~130% without ever closing below that average -- with half the account's value in gold (no leverage); sell when a close is below
    the 100-day EMA. A 4-ATR stop guards against a crash between closes.
  * CRUDEOILM (10 barrels, ~Rs89k a lot): buy when RSI(14) < 30, short when > 70 (the currency rsi_range rule); exit back at the 20-day average or after
    ~15 trading days; stop 2.5 ATR; 4% risk, at least 1 lot when the margin (10x) allows -- with Rs1 lakh one lot risks ~10%, which is the main danger.
  * at most 3 positions; MCX costs from markets/commodity/costs.py.
Tested (world prices converted to rupees before May 2026, real MCX after; Rs1 lakh, all costs and 3 bp slippage):
  2024-25 +50.6% (Rs1L -> Rs1.51L, worst fall -5%); 2026 to 7 Oct +13.8% (worst fall -27%); Jan 2024 - Oct 2026 Rs1L -> Rs1.66L.
  Gold's profit is mostly the 2024-25 rally (2026: -Rs651); crude has only 14 trades in all. Silver was dropped (one SILVERMIC lot is Rs2.2L).

Paper trading (COMMODITY_STRATEGIES=gold_crude). The live runner differs from the backtester, so:
  * history: the stitched daily series (markets/commodity/daily_series.py, nightly), days before today -- the same data it was tested on;
  * decisions on completed days only, looked at 09:00-09:45, so buys AND the trend / mean exits happen near the next open (the backtest exits at the
    close of the signal day -- live is up to a day later);
  * the runner does not watch a strategy's stop, so manage() sells when today's price touches it;
  * contract expiry (dates from the instrument master): GOLDPETAL is a COMPULSORY-DELIVERY contract -- its tender (delivery) period starts 3-5 days
    before expiry and brokers close positions the day before it -- so gold is sold 6 business days before expiry and not bought in the last 8.
    CRUDEOILM is cash-settled: sold 1 business day before expiry, no new entry in the last 3. Gold is bought again in the next contract once the new
    month's contract is the nearest (a few days out of gold each month that the backtest does not have).
"""
from __future__ import annotations

from datetime import datetime, time
from functools import lru_cache

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.commodity.costs import compute_mcx_costs, lot_units

ROLES = {"GOLDPETAL": "trend", "CRUDEOILM": "range"}
TREND_EMA, GOLD_SHARE, GOLD_STOP_ATR = 100, 0.5, 4.0
RSI_LEN, LOW, HIGH, MEAN_DAYS, RANGE_STOP_ATR, MAX_HOLD_DAYS = 14, 30.0, 70.0, 20, 2.5, 15
LEVERAGE_CAP, MAX_POSITIONS = 10.0, 3
ENTRY_WINDOW = (time(9, 0), time(9, 45))


def _day(c: dict) -> str:
    return str(c["timestamp"])[:10]


def _is_backtest(now: datetime) -> bool:
    return (now.hour, now.minute) == (0, 0)


@lru_cache(maxsize=16)
def _archive(symbol: str, before: str) -> tuple:
    path = ARCHIVE_ROOT / "commodity" / f"{symbol}_daily_adj.csv"
    if not path.exists():
        return ()
    df = pd.read_csv(path)
    return tuple(df[df["timestamp"].str[:10] < before].tail(260).to_dict("records"))


def history(candles: list[dict], symbol: str, now: datetime) -> list[dict]:
    return candles[-260:] if _is_backtest(now) else list(_archive(symbol, now.strftime("%Y-%m-%d")))


def rsi(close: pd.Series, n: int = RSI_LEN) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int = 14) -> float:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


EXPIRY_RULES = {"GOLDPETAL": (8, 6), "CRUDEOILM": (3, 1)}     # business days before expiry: (no new entries from, sell from); gold = physical delivery


def expiry_guard(symbol: str, now: datetime) -> tuple[bool, bool]:
    """(no new entries, sell now) from the current contract's expiry. Live only; (False, False) when the expiry is unknown."""
    try:
        from services.broker.instruments import mcx_expiry
        e = mcx_expiry(symbol)
    except Exception:
        e = None
    if not e:
        return False, False
    today, exp = pd.Timestamp(now.date()), pd.Timestamp(e)
    no_entry, sell = EXPIRY_RULES.get(symbol.upper(), (3, 1))
    return today >= exp - pd.offsets.BDay(no_entry), today >= exp - pd.offsets.BDay(sell)


class GoldCrudeCommodity(Strategy):
    name = "gold_crude"
    market = "commodity"
    intraday = False
    product = "D"
    uses_leverage = True
    allow_short = True
    timeframe = ("days", 1)
    lookback_days = 400
    max_positions = MAX_POSITIONS
    default_risk_pct = 4.0
    id_prefix = "GCR"

    def trades(self, symbol: str) -> bool:
        return symbol.upper() in ROLES

    def lot_size(self, symbol: str) -> int:
        return lot_units(symbol)

    def due(self, now: datetime) -> bool:
        return now.weekday() < 5 and ENTRY_WINDOW[0] <= now.time() <= ENTRY_WINDOW[1]

    def entry(self, ctx: EntryContext) -> Signal | None:
        role = ROLES.get(ctx.symbol.upper())
        if role is None:
            return None
        if not _is_backtest(ctx.now) and expiry_guard(ctx.symbol, ctx.now)[0]:
            return None                                                    # too close to this contract's expiry
        bars = history(ctx.candles, ctx.symbol, ctx.now)
        if len(bars) < 110:
            return None
        df = pd.DataFrame(bars)
        c = df["close"]
        last = float(c.iloc[-1])
        u = lot_units(ctx.symbol)
        account = ctx.account_equity or ctx.capital
        a = atr(df)
        if role == "trend":
            if not last > c.ewm(span=TREND_EMA, adjust=False).mean().iloc[-1]:
                return None
            d, stop = 1, last - GOLD_STOP_ATR * a
            lots = int(account * GOLD_SHARE / (last * u))                  # half the account's value in gold, no leverage
        else:
            r = float(rsi(c).iloc[-1])
            d = 1 if r < LOW else (-1 if r > HIGH else 0)
            if d == 0:
                return None
            stop = last - d * RANGE_STOP_ATR * a
            dist = abs(last - stop)
            lots = int(min(account * ctx.risk_pct / 100 / (dist * u), account * LEVERAGE_CAP / MAX_POSITIONS / (last * u)))
            if lots < 1 and last * u <= account * LEVERAGE_CAP / MAX_POSITIONS:
                lots = 1                                                   # one lot is the smallest crude trade; with Rs1 lakh it risks ~10%
        if lots < 1:
            return None
        return Signal(symbol=ctx.symbol, direction="long" if d == 1 else "short", entry_price=last, stop_loss=stop, qty=lots, stop_dist=abs(last - stop),
                      lot_size=u, setup_type="gold uptrend" if role == "trend" else ("crude oversold" if d == 1 else "crude overbought"))

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        role = ROLES.get(ctx.symbol.upper())
        long_ = pos["direction"] == "long"
        if not _is_backtest(ctx.now):
            today = ctx.candles[-1] if ctx.candles and _day(ctx.candles[-1]) == ctx.now.strftime("%Y-%m-%d") else None
            if today is not None:
                hit = float(today["low"]) <= pos["current_stop"] if long_ else float(today["high"]) >= pos["current_stop"]
                if hit:
                    o = float(today["open"])
                    return ExitDecision(min(pos["current_stop"], o) if long_ else max(pos["current_stop"], o), "initial_stop")
                if expiry_guard(ctx.symbol, ctx.now)[1]:
                    return ExitDecision(float(today["close"]), "contract_expiry")
            if not self.due(ctx.now):
                return None                                                # the daily exit rules are checked once, at the next morning
            price = float(today["close"]) if today is not None else None
            c = pd.Series([b["close"] for b in history([], ctx.symbol, ctx.now)])
            if price is None or len(c) < MEAN_DAYS:
                return None
            if role == "trend":
                return ExitDecision(price, "below_100_day_average") if c.iloc[-1] < c.ewm(span=TREND_EMA, adjust=False).mean().iloc[-1] else None
            mean = float(c.rolling(MEAN_DAYS).mean().iloc[-1])
            if (long_ and c.iloc[-1] >= mean) or (not long_ and c.iloc[-1] <= mean):
                return ExitDecision(price, "back_to_20_day_average")
            if (ctx.now - pos["entry_time"]).days >= MAX_HOLD_DAYS * 7 // 5:
                return ExitDecision(price, "time_stop")
            return None
        c = pd.DataFrame(ctx.candles[-260:])["close"]
        last = float(c.iloc[-1])
        if role == "trend":
            return ExitDecision(last, "below_100_day_average") if last < c.ewm(span=TREND_EMA, adjust=False).mean().iloc[-1] else None
        mean = float(c.rolling(MEAN_DAYS).mean().iloc[-1])
        if (long_ and last >= mean) or (not long_ and last <= mean):
            return ExitDecision(last, "back_to_20_day_average")
        if (ctx.now - pos["entry_time"]).days >= MAX_HOLD_DAYS * 7 // 5:
            return ExitDecision(last, "time_stop")
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_mcx_costs(symbol, direction, entry, exit_price, qty)


STRATEGY = GoldCrudeCommodity()
