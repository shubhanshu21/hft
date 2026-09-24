"""Trading-session windows per market (IST wall clock). One definition, used by strategies (Strategy.in_session)
and by the runner's spread sampler, instead of each keeping its own copy."""
from __future__ import annotations

from datetime import datetime

# (open, close) in minutes since midnight IST -- the OUTER session in which the broker feed is live,
# not a strategy's own (usually narrower) entry window.
SESSION_WINDOWS: dict[str, tuple[int, int]] = {
    "commodity": (9 * 60, 23 * 60 + 30),     # MCX 09:00-23:30
    "currency": (9 * 60, 17 * 60),           # NSE currency 09:00-17:00
    "equity": (9 * 60 + 15, 15 * 60 + 30),   # NSE equity 09:15-15:30
}

# NSE currency, minutes after the 09:00 open (entry gate and forced exit). Upstox auto-squares-off currency MIS at 16:30 IST (its 2021 announcement,
# "until further notice") and charges for it, so the bot must be flat BEFORE that -- see tests/test_sessions.py. Moved from 16:40 / 16:50 on
# 2026-09-24 to 16:10 / 16:25: USDINR backtest PF 1.00 -> 1.08 at 5x and 1.48 -> 1.57 at 42x, lower drawdown
# (markets/currency/experiments/squareoff_study.py). Chosen for compliance, deliberately NOT the best-scoring earlier cutoff (15:00 / 16:00).
UPSTOX_CURRENCY_AUTO_SQUAREOFF = (16, 30)
CURRENCY_LAST_ENTRY_MIN = 430      # 16:10
CURRENCY_SQUAREOFF_MIN = 445       # 16:25


def is_open(market: str, now: datetime) -> bool:
    lo, hi = SESSION_WINDOWS[market]
    return lo <= now.hour * 60 + now.minute <= hi
