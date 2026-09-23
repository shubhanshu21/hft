"""
core/regime.py — daily-timeframe regime gate for MCX commodity momentum
strategies.

Built 2026-09-18 after finding CRUDEOILM's intraday momentum thresholds
were profitable on Aug17-Sep17 (+520%) but a clear net loser on the newly-
revealed May18-Jul31 (-100%), on the EXACT SAME thresholds -- proving the
edge is regime-dependent, not threshold-dependent (see
markets/commodity/scalping/backtest.py's ENTRY_THRESHOLDS["crude"] comment for the full
finding). A daily ADX trend-strength gate was tried first and found
BACKWARDS (the bad period actually had HIGHER daily ADX, 31.4 vs 21.6 --
high intraday volatility without persistent direction, not the "choppy/low-
ADX" story the naive hypothesis assumed). The signal that actually worked:
rolling daily-return AUTOCORRELATION -- positive means consecutive days
tend to continue (favorable to an intraday momentum strategy), negative
means they tend to reverse (day-to-day mean reversion, unfavorable). Real
levels found: May-Jul mean daily-return autocorr(1) = -0.10 (mean-
reverting), Aug-Sep = +0.18 (trending).

Gating CRUDEOILM's backtested trades by "trailing 15-day autocorr >= 0.0,
computed as of the prior day's close" took TRAIN from PF 0.75 (net
-Rs25,127 at the loosest gate) toward breakeven/positive (PF 0.99-2.50
depending on gate strictness) while barely touching TEST's own strong
performance (PF 1.34-1.45, net +Rs80k-95k vs the full ungated amount) --
see conversation history for the full sweep. This is NOT a claim that
day-to-day return autocorrelation is a universal edge -- it's specifically
the feature that happened to separate these two real, already-observed
periods. Revisit if it stops working as more real data accumulates.
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
    # with `is False`/`is True` (see markets/commodity/scalping/backtest.py, entry_signal.py)
    # specifically to distinguish a real answer from None -- without this
    # cast, `is False` never matches and the gate silently never blocks
    # anything. Found 2026-09-18 by the gate producing zero effect at all
    # once wired into the real backtest, not by inspection.
    return bool(autocorr >= min_autocorr)
