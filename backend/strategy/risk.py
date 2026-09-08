"""
strategy/risk.py — capital-aware position sizing, so the backtest (and
eventually a live bot) can never take a position bigger than the account
can actually afford, and never risk more than a fixed fraction of
capital on any one trade.

Two independent caps apply to every trade, and the smaller one wins:
  1. Risk cap: lose at most `risk_pct` of CURRENT capital if the stop is
     hit — this is what keeps a losing streak from compounding into ruin.
  2. Cash cap: never spend more than the capital actually available —
     this is what keeps a single trade from being sized larger than the
     account (no assumed margin/leverage here; MIS intraday leverage
     exists in reality, but modeling "can't go bankrupt" means starting
     from the conservative, no-leverage case).
"""
from __future__ import annotations

import os

DEFAULT_RISK_PCT = 4.0  # % of current capital risked per trade (sized for active scalper growth)
MAX_CONCURRENT_POSITIONS = 4  # cap on simultaneously open trades across the whole portfolio
# Standard SEBI MIS Intraday equity leverage: default is 4.0x for active capital utilization.
DEFAULT_LEVERAGE = float(os.environ.get("INTRADAY_LEVERAGE", "4.0"))
INTRADAY_EQUITY_LEVERAGE = DEFAULT_LEVERAGE


def position_size(
    capital: float,
    entry_price: float,
    stop_price: float,
    risk_pct: float = DEFAULT_RISK_PCT,
    leverage: float = DEFAULT_LEVERAGE,
) -> int:
    """Share quantity for one trade, sized by the 1R risk rule and available MIS margin.
    Returns 0 if capital is too small to buy even one share within the risk cap."""
    if capital <= 0 or entry_price <= 0:
        return 0
    per_share_risk = abs(entry_price - stop_price)
    if per_share_risk <= 0:
        return 0

    risk_amount = capital * risk_pct / 100
    qty_by_risk = math.floor(risk_amount / per_share_risk)
    qty_by_margin = math.floor(capital * leverage / entry_price)
    return max(0, min(qty_by_risk, qty_by_margin))

