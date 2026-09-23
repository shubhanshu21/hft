"""Long daily price history from Yahoo Finance's public chart API, for swing-strategy research.

Upstox history is capped at roughly one contract's life for MCX / NSE-currency futures (1-4 months), far too short to
validate a strategy that holds for days. This gives 15+ years. It is research data only -- never used for trading --
and it is a PROXY: global futures in USD (GC=F, CL=F, ...) converted with USDINR stand in for MCX contracts, and
Yahoo's continuous futures are not roll-adjusted. Cached under var/cache/yahoo/ and refreshed once a day.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
import requests

from core.paths import CACHE_DIR

_DIR = CACHE_DIR / "yahoo"
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_daily(symbol: str, start: str = "2003-01-01", max_age_hours: float = 20.0) -> pd.DataFrame:
    """Daily OHLC(+adjusted close) for a Yahoo symbol, index = date. Raises on a failed download."""
    _DIR.mkdir(parents=True, exist_ok=True)
    path = _DIR / f"{symbol.replace('=', '_').replace('^', '_').replace('&', '_')}.csv"
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    resp = requests.get(_URL.format(symbol=symbol), params={"period1": p1, "period2": int(time.time()), "interval": "1d",
                                                             "events": "div,splits"}, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    q = result["indicators"]["quote"][0]
    adj = result["indicators"].get("adjclose", [{}])[0].get("adjclose")
    df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
                       "adjclose": adj if adj else q["close"], "volume": q["volume"]},
                      index=pd.to_datetime(result["timestamp"], unit="s").normalize())
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["open", "high", "low", "close"]).sort_index()
    df.index.name = "date"
    df.to_csv(path)
    return df
