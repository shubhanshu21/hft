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


def is_open(market: str, now: datetime) -> bool:
    lo, hi = SESSION_WINDOWS[market]
    return lo <= now.hour * 60 + now.minute <= hi
