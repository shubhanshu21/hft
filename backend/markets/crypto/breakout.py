"""Daily volatility breakout, long-only with a trend filter -- the crypto "active" sleeve.  (docs/CRYPTO_ACTIVE_STRATEGIES.md)

On 15-minute bars of a UTC day: go long once a bar CLOSES above today's open + K x yesterday's (high - low), but only if yesterday's close
is above the TREND_DAYS-day average of daily closes; hold to the end of the UTC day, then flat. At most one entry per coin per day.

Searched 2026-10-06 among 316 active configurations (channel breakouts, intraday momentum, RSI mean reversion, squeezes, hour-of-day,
cross-sectional momentum), fitted on 2021-2024 and judged on 2025-2026 with perpetual costs and funding: frequent traders all lost after
fees; this was the most consistent survivor (k 0.7-1.0 all positive on TEST; k 0.5 not). Per year at 6 bp/side, 1x: 2021 +37%, 2022 -3.5%,
2023 +42%, 2024 +19%, 2025 +5%, 2026 +12%; daily-return correlation with the slow blend 0.32, so it runs ON TOP of it.

One function serves the backtest and the live sleeve: live takes the last element of positions() over the closed bars, so the two cannot drift.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

K = 0.7
TREND_DAYS = 20
BAR = pd.Timedelta(minutes=15)


def positions(d: pd.DataFrame, k: float = K, trend_days: int = TREND_DAYS) -> np.ndarray:
    """d: CLOSED 15-minute bars (UTC DatetimeIndex = bar start, columns open/high/low/close). pos[i] is decided at bar i's close and held
    during bar i+1. A bar whose END is a UTC midnight decides flat (the day's position is closed there)."""
    day = d.index.floor("1D")
    g = d.groupby(day)
    o = g["open"].transform("first").to_numpy()
    rng = (g["high"].max() - g["low"].min()).shift(1).reindex(day).to_numpy()
    dc = g["close"].last()
    ma = dc.rolling(trend_days).mean().shift(1).reindex(day).to_numpy()
    prev_close = dc.shift(1).reindex(day).to_numpy()
    bull = prev_close > ma                                       # NaN compares False: no trade until the average exists
    c = d["close"].to_numpy()
    up = o + k * rng
    ends_day = np.asarray((d.index + BAR).floor("1D") != day)    # the bar ending at 00:00 UTC -- by time, not by position in the array
    days = day.to_numpy()
    pos = np.zeros(len(c))
    cur, cur_day = 0, None
    for i in range(len(c)):
        if days[i] != cur_day:
            cur_day, cur = days[i], 0
        if cur == 0 and not np.isnan(up[i]) and c[i] > up[i] and bull[i]:
            cur = 1
        pos[i] = 0 if ends_day[i] else cur
    return pos


def target(closed: pd.DataFrame, k: float = K, trend_days: int = TREND_DAYS) -> int:
    """The live decision: 1 = be long during the next bar, 0 = flat. `closed` must end at the most recent CLOSED bar and hold at least
    trend_days + 2 UTC days."""
    if closed.empty:
        return 0
    return int(positions(closed, k, trend_days)[-1])
