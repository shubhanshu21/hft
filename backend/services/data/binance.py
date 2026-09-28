"""Binance public market data (spot klines), for crypto research and paper trading.  No API key: the /api/v3/klines endpoint is public.

    python3 -m services.data.binance [--symbols BTCUSDT ETHUSDT SOLUSDT] [--intervals 15m 1h]

Each call returns up to 1,000 candles; history is pulled forward from the first available candle (or from the last archived one, so re-running only tops up) into
var/archive/crypto/<SYMBOL>_<interval>.csv with UTC ISO timestamps and columns timestamp, open, high, low, close, volume, trades. Research / paper trading only.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from core.paths import ARCHIVE_ROOT

URL = "https://api.binance.com/api/v3/klines"
ARCHIVE_DIR = ARCHIVE_ROOT / "crypto"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
COLS = ["timestamp", "open", "high", "low", "close", "volume", "trades"]


def _rows(payload: list) -> pd.DataFrame:
    df = pd.DataFrame([(pd.Timestamp(r[0], unit="ms", tz="UTC"), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), int(r[8])) for r in payload], columns=COLS)
    return df


def fetch(symbol: str, interval: str, start_ms: int, end_ms: int | None = None, session: requests.Session | None = None) -> pd.DataFrame:
    """All closed candles from start_ms (inclusive) to now, paged 1,000 at a time."""
    s = session or requests.Session()
    out, cur = [], start_ms
    end_ms = end_ms or int(time.time() * 1000)
    while cur < end_ms:
        for attempt in range(5):
            r = s.get(URL, params={"symbol": symbol, "interval": interval, "startTime": cur, "limit": 1000}, timeout=30)
            if r.status_code == 200:
                break
            time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"Binance {symbol} {interval}: HTTP {r.status_code} {r.text[:120]}")
        payload = r.json()
        if not payload:
            break
        out.append(_rows(payload))
        cur = payload[-1][0] + INTERVAL_MS[interval]
        if len(payload) < 1000:
            break
        time.sleep(0.15)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=COLS)


def archive_path(symbol: str, interval: str) -> Path:
    return ARCHIVE_DIR / f"{symbol}_{interval}.csv"


def topup(symbol: str, interval: str, first_ms: int = int(datetime(2020, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)) -> int:
    """Append candles newer than the archive (or everything since `first_ms` if there is none); the still-forming last candle is dropped. Returns rows added."""
    path = archive_path(symbol, interval)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(path, parse_dates=["timestamp"]) if path.exists() else pd.DataFrame(columns=COLS)
    start = int(old["timestamp"].iloc[-1].timestamp() * 1000) + INTERVAL_MS[interval] if len(old) else first_ms
    new = fetch(symbol, interval, start)
    if len(new):
        new = new[new["timestamp"] + pd.Timedelta(milliseconds=INTERVAL_MS[interval]) <= pd.Timestamp.now(tz="UTC")]     # closed candles only
    merged = pd.concat([old, new], ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
    merged.to_csv(path, index=False, date_format="%Y-%m-%dT%H:%M:%S%z")
    return len(merged) - len(old)


FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


def funding_path(symbol: str) -> Path:
    return ARCHIVE_DIR / f"{symbol}_funding.csv"


def topup_funding(symbol: str) -> int:
    """USDT-M perpetual FUNDING RATES (paid every 8 hours at 00:00 / 08:00 / 16:00 UTC; positive = longs pay shorts), appended to var/archive/crypto/<SYMBOL>_funding.csv. Returns rows added."""
    path = funding_path(symbol)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(path, parse_dates=["timestamp"]) if path.exists() else pd.DataFrame(columns=["timestamp", "rate"])
    cur = int(old["timestamp"].iloc[-1].timestamp() * 1000) + 1 if len(old) else int(datetime(2019, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    rows = []
    while True:
        for attempt in range(5):
            r = requests.get(FUNDING_URL, params={"symbol": symbol, "startTime": cur, "limit": 1000}, timeout=30)
            if r.status_code == 200:
                break
            time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"Binance funding {symbol}: HTTP {r.status_code} {r.text[:120]}")
        payload = r.json()
        if not payload:
            break
        rows += [(pd.Timestamp(x["fundingTime"], unit="ms", tz="UTC").round("h"), float(x["fundingRate"])) for x in payload]
        cur = payload[-1]["fundingTime"] + 1
        if len(payload) < 1000:
            break
        time.sleep(0.15)
    new = pd.DataFrame(rows, columns=["timestamp", "rate"])
    merged = pd.concat([old, new], ignore_index=True).drop_duplicates("timestamp").sort_values("timestamp")
    merged.to_csv(path, index=False, date_format="%Y-%m-%dT%H:%M:%S%z")
    return len(merged) - len(old)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=list(SYMBOLS))
    ap.add_argument("--intervals", nargs="+", default=["15m", "1h"], choices=sorted(INTERVAL_MS))
    ap.add_argument("--funding", action="store_true", help="also fetch perpetual funding-rate history")
    a = ap.parse_args(argv)
    if a.funding:
        for s in a.symbols:
            print(f"{s} funding: +{topup_funding(s):,} rows -> {funding_path(s)}")
        return 0
    for s in a.symbols:
        for i in a.intervals:
            n = topup(s, i)
            print(f"{s} {i}: +{n:,} rows -> {archive_path(s, i)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
