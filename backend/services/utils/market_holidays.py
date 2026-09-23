#!/usr/bin/env python3
"""
utils/market_holidays.py — Real, automatically-updating Indian market holiday calendar.

Added 2026-09-18 after live_dryrun.py's overnight day-rollover loop was
found to only skip Sunday (weekday()==6) -- Saturday and every public
holiday were NOT handled, an admitted gap in that code's own old comment.
A hardcoded per-year holiday list was the first fix attempted, but the
user correctly pushed back twice: (1) Upstox itself publishes an official
holiday calendar, no reason to hand-maintain a duplicate, and (2) a
year must never be hardcoded -- this always asks for "this year's"
calendar, whatever year that is, never a literal like "2026".

Endpoint: GET https://api.upstox.com/v2/market/holidays -- public, no
auth/API key required, returns the CURRENT year's holidays (the year is
implicit in the response's own dates, never requested or assumed by us).

Not every entry is a full trading closure -- the response also includes
SETTLEMENT_HOLIDAY (clearing closed, trading open) and SPECIAL_TIMING
(modified hours, not closed) entries. Only holiday_type=="TRADING_HOLIDAY"
means the exchange doesn't trade that day at all, so only those count here.

Cached to disk with a freshness check (re-fetches once the cached data's
year no longer matches today's, or once a day) so a network hiccup at
startup doesn't repeatedly hit the endpoint, but also never goes stale
across a year boundary.
"""
from __future__ import annotations

from core.paths import CACHE_DIR
import json
import logging
from datetime import date, datetime
from pathlib import Path

import requests

log = logging.getLogger("market_holidays")

_HOLIDAYS_URL = "https://api.upstox.com/v2/market/holidays"
_CACHE_PATH = CACHE_DIR / "market_holidays.json"

# Exchanges relevant to this project's live-traded instruments (MCX commodities,
# NSE currency derivatives via CDS, NSE equities) -- a day counts as a holiday
# here if ANY of these three closes, which is the safe/conservative direction
# (worst case: skips a day one of the OTHER segments would've actually traded,
# never the reverse of trying to trade on a day that's actually closed).
_RELEVANT_EXCHANGES = {"NSE", "CDS", "MCX"}


def _fetch_from_upstox() -> list[dict] | None:
    try:
        resp = requests.get(_HOLIDAYS_URL, headers={"Accept": "application/json"}, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            return None
        return payload.get("data", [])
    except Exception as e:
        log.warning("Could not fetch market holidays from Upstox: %s", e)
        return None


def get_trading_holidays(force_refresh: bool = False) -> set[date]:
    """Returns the set of dates where at least one of NSE/CDS/MCX has a full
    TRADING_HOLIDAY closure, for whatever year "today" currently is. Never
    takes or hardcodes a year -- always reflects the live current year.
    Falls back to the cached copy (even if stale) on a fetch failure, and to
    an empty set (weekday-only gating) if there's no cache at all -- fails
    safe toward "keep running", not toward crashing the daemon."""
    today = date.today()
    cached = _load_cache()

    needs_refresh = force_refresh or cached is None
    if cached is not None:
        cached_at = datetime.fromisoformat(cached["fetched_at"])
        stale = (datetime.now() - cached_at).total_seconds() > 20 * 3600  # ~daily refresh
        year_rolled_over = cached_at.year != today.year
        needs_refresh = needs_refresh or stale or year_rolled_over

    if needs_refresh:
        data = _fetch_from_upstox()
        if data is not None:
            _save_cache(data)
            cached = {"fetched_at": datetime.now().isoformat(), "data": data}

    if cached is None:
        log.warning("No market holiday data available (fetch failed, no cache) -- "
                     "falling back to weekday-only gating (Sat/Sun), holidays won't be skipped.")
        return set()

    holidays: set[date] = set()
    for entry in cached["data"]:
        if entry.get("holiday_type") != "TRADING_HOLIDAY":
            continue
        if not _RELEVANT_EXCHANGES.intersection(entry.get("closed_exchanges", [])):
            continue
        try:
            holidays.add(date.fromisoformat(entry["date"]))
        except (KeyError, ValueError):
            continue
    return holidays


def _load_cache() -> dict | None:
    if not _CACHE_PATH.exists():
        return None
    try:
        return json.loads(_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(data: list[dict]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps({"fetched_at": datetime.now().isoformat(), "data": data}))


if __name__ == "__main__":
    holidays = get_trading_holidays(force_refresh=True)
    print(f"{len(holidays)} trading holidays for {date.today().year} (NSE/CDS/MCX, any one closing counts):")
    for d in sorted(holidays):
        print(f"  {d}")
