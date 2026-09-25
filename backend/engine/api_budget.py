"""Stay inside Upstox's documented API rate limits.

Upstox (https://upstox.com/developer/api-documentation/rate-limiting/): standard APIs -- holdings, positions, funds, market quotes and HISTORICAL/INTRADAY
CANDLES -- 50 requests/second, 500/minute and **2,000 per 30 minutes**; breaching them "may result in temporary suspension of access".

Measured 2026-09-25: one scan of the 52-symbol universe makes ~57 calls (one candle fetch per symbol plus occasional quote samples), so a 30-second cadence needs
~3,400 calls per 30 minutes during equity hours -- 70% over the limit (no 429 was ever seen, so enforcement is lax, but the rule is written down). The loop also kept
fetching candles for markets that had closed. This module meters the calls (services/broker/upstox_broker.api_calls_in_window) and says whether a class of work may
spend more, with headroom for everything else this process does (margin refresh, token probe, the dashboard is separate and makes none):

  essential   exits of open positions and the few commodity/currency symbols -- allowed until the HARD limit
  entry       equity entry scans (49 symbols)                                   -- PACED: at most ENTRY_PACE calls in any minute, and under the SOFT 30-min limit
  sampling    spread sampling                                                   -- allowed until the SOFT limit minus a reserve

Pacing matters: a plain 30-minute cap made equity scan flat out for ~15 minutes and then starve completely until the window cleared (seen live 2026-09-25: 49 of 49
scans deferred, usage crawling at 6 calls/min). Capping the last MINUTE instead spreads the same budget evenly: ~50 calls/min x 30 = 1,500 < 2,000, so equity is
scanned about once every 65-80 seconds all session long. Its 5-minute bars change slowly and the backtest itself enters at bar close. Limits are env-tunable.
"""
from __future__ import annotations

import os

from services.broker import upstox_broker

LIMIT_30MIN, LIMIT_MIN = 2000, 500


def _f(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def used(seconds: float = 1800.0) -> int:
    return upstox_broker.api_calls_in_window(seconds)


def allow(kind: str, used30: int | None = None, used60: int | None = None) -> bool:
    """May a call of this class be made now? kind: 'essential' | 'entry' | 'sampling'."""
    u30 = used(1800.0) if used30 is None else used30
    u60 = used(60.0) if used60 is None else used60
    soft30, hard30 = _f("UPSTOX_API_SOFT_LIMIT_30MIN", 1600), _f("UPSTOX_API_HARD_LIMIT_30MIN", 1900)
    soft60, hard60 = _f("UPSTOX_API_SOFT_LIMIT_MIN", 400), _f("UPSTOX_API_HARD_LIMIT_MIN", 470)
    if kind == "essential":
        return u30 < hard30 and u60 < hard60
    if kind == "sampling":
        return u30 < soft30 - 200 and u60 < soft60 - 60
    pace = _f("UPSTOX_API_ENTRY_PACE_MIN", 50)                          # equity entry scans: evenly paced, not burst-then-starve
    return u30 < soft30 and u60 < min(soft60, pace)


def snapshot() -> dict:
    return {"used_30min": used(1800.0), "limit_30min": LIMIT_30MIN, "used_1min": used(60.0), "limit_1min": LIMIT_MIN}
