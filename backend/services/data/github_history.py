"""
data/github_history.py — extra pre-2022 DAILY bars to extend the
daily-indicator (ATR/RSI/ADX/EMA) warm-up window past Upstox's own
~4.5-year limit (minute data only goes back to Jan 2022 there).

Source: ShabbirHasan1/NSE-Data (github.com/ShabbirHasan1/NSE-Data, GPLv3),
1-minute OHLCV for a subset of NIFTY 50 stocks, 2017-01-02 to 2021-01-01,
collected via the Alice Blue API. Spot-checked against Yahoo Finance's
own daily closes for INDUSINDBK around Dec 2020 — same trend, dates, and
price level (sub-2% vendor differences, consistent with different
official-close conventions, not corrupted data).

Daily bars only — the backtest is single-timeframe (native 5-minute
Upstox candles), and this source is 1-minute, so per instruction it is
never resampled into a synthetic 5-minute intraday series. That means no
feature/label rows are ever built for these pre-2022 days (no real
intraday 5m data exists for them); they only extend how far back the
daily indicators can warm up before Upstox's own native range starts.

Only symbols with a CSV present under var/cache/github_nse_1min/ get this
extra history; everything else is Upstox-only (2022+). There's a real
~1-year gap between this source's end (Jan 2021) and Upstox's start
(Jan 2022) — indicators simply warm up again after the gap.
"""
from __future__ import annotations

from core.paths import CACHE_DIR
from pathlib import Path

import pandas as pd

_DATA_DIR = CACHE_DIR / "github_nse_1min"

# strategy/universe.py symbol -> this dataset's filename stem (differs for
# symbols with special characters, e.g. Upstox's "M&M" vs this repo's "M_M")
_SYMBOL_ALIASES = {"M&M": "M_M"}


def available_symbols() -> set[str]:
    if not _DATA_DIR.exists():
        return set()
    stems = {f.stem for f in _DATA_DIR.glob("*.csv")}
    reverse_alias = {v: k for k, v in _SYMBOL_ALIASES.items()}
    return {reverse_alias.get(s, s) for s in stems}


def load_daily_candles(symbol: str) -> list[dict]:
    """Daily OHLCV built by resampling the same 1-minute source, oldest-first."""
    stem = _SYMBOL_ALIASES.get(symbol, symbol)
    path = _DATA_DIR / f"{stem}.csv"
    if not path.exists():
        return []

    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
    df = df.dropna(subset=["open", "high", "low", "close", "volume"]).set_index("timestamp").sort_index()

    daily = df.resample("1D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna(subset=["open"])

    return [
        {"timestamp": ts.date().isoformat(), "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"], "volume": row["volume"]}
        for ts, row in daily.iterrows()
    ]
