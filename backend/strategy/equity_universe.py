"""
strategy/equity_universe.py -- fixed NIFTY50 constituent list.

Rebuilt 2026-09-19 after the original equity scalper was removed
(a3e33785, "equityremoved") once it failed to show a real OOS edge on the
full NIFTY50 with a genuine train/test split. That prior removal is the only
preserved finding -- the specific numbers never made it into git or docs, only
the conclusion did (see CLAUDE.md).

The PRIOR version's universe (strategy/universe.py, deleted in the same
commit) was a hand-picked 9-stock "curated" list, explicitly selected BECAUSE
each stock already backtested well ("Selected based on... empirical
multi-year edge", with each stock's own win-rate/PF cited as the selection
reason). That is circular: picking survivors after seeing backtest results
guarantees an in-sample edge regardless of whether one is real, and is
almost certainly why the original small-universe numbers looked far better
than what held up once tested on the full, unbiased NIFTY50.

This list is fixed BEFORE any backtest is run and must never be edited based
on how any individual stock performs in a sweep -- if a stock looks bad, drop
the whole exercise or flag the finding, don't quietly prune the loser out of
the universe after the fact. It's a plain, widely-known NIFTY50 snapshot, not
adjusted for the current live index composition (constituents drift slowly;
this is not re-fetched dynamically, same static-list approach the old
strategy/universe.py used for its own list before curation was applied).
"""
from __future__ import annotations

# TATAMOTORS deliberately excluded: it no longer resolves in Upstox's current
# NSE instrument master (likely a corporate action renamed/split it since this
# list's reference point) and no confident replacement ticker was available.
# Dropped BEFORE any backtest was run, for a data-availability reason, not a
# performance one -- 49 names, not 50.
NIFTY50_SYMBOLS: list[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC", "SBIN",
    "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "HCLTECH", "SUNPHARMA", "TITAN", "ULTRACEMCO", "NESTLEIND", "WIPRO", "ONGC", "NTPC",
    "POWERGRID", "M&M", "TATASTEEL", "ADANIENT", "ADANIPORTS", "JSWSTEEL",
    "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "DRREDDY", "CIPLA", "DIVISLAB", "GRASIM",
    "BRITANNIA", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "COALINDIA", "INDUSINDBK",
    "TECHM", "UPL", "APOLLOHOSP", "BPCL", "HINDALCO", "SHREECEM", "IOC",
]
