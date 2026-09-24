"""Trading-session windows per market (IST wall clock), read from Upstox.

Exchange hours are the exchange's to change (special / muhurat / shortened sessions, MCX's US-daylight-saving shift), so the daemon asks Upstox
each day: `MarketHolidaysAndTimingsApi.get_exchange_timings(date)` returns every exchange's start and end for that date (measured 2026-09-24:
MCX 09:00-23:30, CDS 09:00-17:00, NSE 09:15-15:30). `refresh()` stores them; `window()` returns today's real hours, or the last values Upstox
gave, or -- only if Upstox has never answered -- DEFAULT_WINDOWS, which are a fallback and never override an answer.

What Upstox does NOT publish through any API is when it auto-squares-off intraday (MIS) positions and charges for it; that is only announced
(currency 16:30, MCX 22:50, equity 15:00-15:15 in its announcements). So the bot's exits are defined RELATIVE TO THE DAY'S REAL CLOSE by two
documented policy values per market -- SQUAREOFF_BEFORE_CLOSE_MIN and LAST_ENTRY_BEFORE_CLOSE_MIN -- chosen to leave the account flat before
those announced times (tests/test_sessions.py ties them together). They can be overridden per market from the environment
(<MARKET>_SQUAREOFF_BEFORE_CLOSE_MIN / <MARKET>_LAST_ENTRY_BEFORE_CLOSE_MIN) if Upstox announces a new time.

Backtests replay history and use the DEFAULT values (CURRENCY_*_MIN, and the market defaults); only the live daemon calls `refresh`.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("sessions")
IST = timezone(timedelta(hours=5, minutes=30))

UPSTOX_EXCHANGE = {"commodity": "MCX", "currency": "CDS", "equity": "NSE"}

# Fallback ONLY (minutes since midnight IST): used until Upstox has answered once. Never overrides an Upstox answer.
DEFAULT_WINDOWS: dict[str, tuple[int, int]] = {
    "commodity": (9 * 60, 23 * 60 + 30),     # MCX 09:00-23:30
    "currency": (9 * 60, 17 * 60),           # NSE currency 09:00-17:00
    "equity": (9 * 60 + 15, 15 * 60 + 30),   # NSE equity 09:15-15:30
}
SESSION_WINDOWS = DEFAULT_WINDOWS            # older name, kept for imports

# Policy (Upstox does not publish square-off times via an API). Minutes BEFORE the day's real close.
#   commodity: exit 22:45 (Upstox announced MCX MIS auto square-off at 22:50), last entry 22:30
#   currency:  exit 16:25 (Upstox announced currency auto square-off at 16:30), last entry 16:10
#   equity:    exit 14:55 (Upstox announced 15:00 in 2021, later sources say 15:15; 14:55 is inside both), last entry 14:45
# Currency was 16:40 / 16:50 until 2026-09-24: USDINR backtest PF 1.00 -> 1.08 at 5x and 1.48 -> 1.57 at 42x when moved
# (markets/currency/experiments/squareoff_study.py); equity 15:15 -> 14:55 is PF-neutral (markets/equity/experiments/squareoff_study.py).
SQUAREOFF_BEFORE_CLOSE_MIN = {"commodity": 45, "currency": 35, "equity": 35}
LAST_ENTRY_BEFORE_CLOSE_MIN = {"commodity": 60, "currency": 50, "equity": 45}
UPSTOX_ANNOUNCED_AUTO_SQUAREOFF = {"commodity": (22, 50), "currency": (16, 30), "equity": (15, 0)}    # earliest announced; used by the tests

# The feature pipeline anchors `minutes_since_open` at these clock minutes (markets/*/features.py), independent of what Upstox reports as the open.
FEATURE_ANCHOR = {"commodity": 9 * 60, "currency": 9 * 60, "equity": 9 * 60 + 15}

# Static defaults, minutes AFTER the market's open, for the historical backtests (which have no "today" to ask Upstox about).
CURRENCY_LAST_ENTRY_MIN = DEFAULT_WINDOWS["currency"][1] - LAST_ENTRY_BEFORE_CLOSE_MIN["currency"] - DEFAULT_WINDOWS["currency"][0]       # 430
CURRENCY_SQUAREOFF_MIN = DEFAULT_WINDOWS["currency"][1] - SQUAREOFF_BEFORE_CLOSE_MIN["currency"] - DEFAULT_WINDOWS["currency"][0]         # 445
EQUITY_LAST_ENTRY_SINCE_OPEN = DEFAULT_WINDOWS["equity"][1] - LAST_ENTRY_BEFORE_CLOSE_MIN["equity"] - DEFAULT_WINDOWS["equity"][0]            # 330 (14:45)
EQUITY_SQUAREOFF_SINCE_OPEN = DEFAULT_WINDOWS["equity"][1] - SQUAREOFF_BEFORE_CLOSE_MIN["equity"] - DEFAULT_WINDOWS["equity"][0]          # 340
COMMODITY_LAST_ENTRY_SINCE_OPEN = DEFAULT_WINDOWS["commodity"][1] - LAST_ENTRY_BEFORE_CLOSE_MIN["commodity"] - DEFAULT_WINDOWS["commodity"][0]   # 810
UPSTOX_CURRENCY_AUTO_SQUAREOFF = UPSTOX_ANNOUNCED_AUTO_SQUAREOFF["currency"]

_live: dict[str, dict] = {}                  # market -> {"open": min, "close": min, "date": iso, "asof": iso}


def _policy(table: dict, market: str, kind: str) -> int:
    return int(os.environ.get(f"{market.upper()}_{kind}", table[market]))


def window(market: str) -> tuple[int, int]:
    """(open, close) in minutes since midnight IST: Upstox's latest answer, else the fallback."""
    got = _live.get(market)
    return (got["open"], got["close"]) if got else DEFAULT_WINDOWS[market]


def squareoff_min(market: str) -> int:
    """Forced-exit time, minutes since midnight IST."""
    return window(market)[1] - _policy(SQUAREOFF_BEFORE_CLOSE_MIN, market, "SQUAREOFF_BEFORE_CLOSE_MIN")


def last_entry_min(market: str) -> int:
    """Last minute a new entry may be taken, minutes since midnight IST."""
    return window(market)[1] - _policy(LAST_ENTRY_BEFORE_CLOSE_MIN, market, "LAST_ENTRY_BEFORE_CLOSE_MIN")


def squareoff_clock(market: str) -> tuple[int, int]:
    m = squareoff_min(market)
    return m // 60, m % 60


def last_entry_since_open(market: str) -> int:
    """Last entry in the feature pipeline's unit (`minutes_since_open`, anchored at FEATURE_ANCHOR)."""
    return last_entry_min(market) - FEATURE_ANCHOR[market]


def squareoff_since_open(market: str) -> int:
    return squareoff_min(market) - FEATURE_ANCHOR[market]


def is_open(market: str, now: datetime) -> bool:
    lo, hi = window(market)
    return lo <= now.hour * 60 + now.minute <= hi


def _fmt(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def refresh(api_client, day: date | None = None) -> list[str]:
    """Ask Upstox for `day`'s exchange hours and remember them. Returns a line for every market whose hours CHANGED versus what was in use.
    On any failure the previous values are kept and nothing is raised."""
    import upstox_client
    day = day or datetime.now(IST).date()
    changes: list[str] = []
    try:
        rows = upstox_client.MarketHolidaysAndTimingsApi(api_client).get_exchange_timings(day.isoformat()).data or []
    except Exception as exc:
        log.warning("could not read exchange timings from Upstox (keeping %s): %s", "the last known" if _live else "the defaults", exc)
        return changes
    by_exchange = {r.exchange: r for r in rows}
    for market, exch in UPSTOX_EXCHANGE.items():
        r = by_exchange.get(exch)
        if r is None:
            continue                                   # no session that day (weekend / holiday): keep what we had
        def to_min(ms):
            t = datetime.fromtimestamp(ms / 1000, tz=IST)
            return t.hour * 60 + t.minute
        new = (to_min(r.start_time), to_min(r.end_time))
        old = window(market)
        _live[market] = {"open": new[0], "close": new[1], "date": day.isoformat(), "asof": datetime.now(IST).isoformat(timespec="seconds")}
        if new != old:
            changes.append(f"{market} ({exch}) hours {_fmt(old[0])}-{_fmt(old[1])} -> {_fmt(new[0])}-{_fmt(new[1])}; "
                           f"last entry {_fmt(last_entry_min(market))}, forced exit {_fmt(squareoff_min(market))}")
    return changes


def reset_for_tests() -> None:
    _live.clear()
