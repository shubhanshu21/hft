"""
core/regime.py — daily-timeframe regime gate: trailing autocorrelation of daily returns.

Positive autocorrelation means consecutive days tend to continue (favourable to momentum entries), negative means
they tend to reverse. Used by the runners' optional equity regime filter (USE_EQUITY_REGIME_FILTER, on NIFTY50).
It was first built for MCX crude scalping (2026-09-18), where it separated two observed periods; that strategy and
the whole commodity market were removed on 2026-10-12. It is not a validated universal edge.
"""
from __future__ import annotations

import pandas as pd


def daily_closes_to_returns(daily_closes: list[float]) -> pd.Series:
    return pd.Series(daily_closes).pct_change().dropna() * 100


def regime_ok(daily_closes: list[float], window: int = 15, min_autocorr: float = 0.0) -> bool | None:
    """
    `daily_closes`: real daily closes, oldest-first, ending at the LAST
    fully-closed trading day (i.e. NOT including today -- callers must pass
    data through yesterday's close only, computed once per day before
    today's session, never mid-day with today's still-forming price).

    Returns True if the trailing `window`-day daily-return autocorrelation
    (lag 1) is >= `min_autocorr`, False if it's below, or None if there
    isn't enough history yet to compute it (fewer than window+2 real daily
    closes) -- callers must treat None as "can't gate yet" and fail OPEN
    (let the trade through) rather than silently blocking every symbol
    whenever the archive is short, which would be a much larger behavior
    change than intended.
    """
    rets = daily_closes_to_returns(daily_closes)
    if len(rets) < window + 1:
        return None
    trailing = rets.iloc[-window:]
    autocorr = trailing.autocorr(1)
    if pd.isna(autocorr):
        return None
    # bool(...) matters here, not just style -- pandas/numpy comparisons
    # return numpy.bool_, and `numpy.bool_(False) is False` is False (a
    # different object from Python's own bool singleton) even though
    # `==` would agree. Every caller of this function checks the result
    # with `is False`/`is True`
    # specifically to distinguish a real answer from None -- without this
    # cast, `is False` never matches and the gate silently never blocks
    # anything. Found 2026-09-18 by the gate producing zero effect at all
    # once wired into the real backtest, not by inspection.
    return bool(autocorr >= min_autocorr)
