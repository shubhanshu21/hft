"""Free multi-year 1-minute history for the GLOBAL commodity futures/CFD prices that MCX contracts follow, from Dukascopy's public datafeed.

Why: Upstox serves only ~4 months of intraday history for an MCX contract (monthly expiries), far too little to test whether a strategy works in different market
phases. Dukascopy publishes one LZMA-compressed file of 1-minute candles per instrument per UTC day (https://datafeed.dukascopy.com/datafeed/<SYM>/<YYYY>/<MM-1>/<DD>/BID_candles_min_1.bi5,
24-byte big-endian records: seconds since 00:00 UTC, open, close, low, high as integers, volume as float32). Research data only -- a PROXY for the MCX contract (global price,
no MCX premium/spread, no rupee conversion here), never used for trading.

    python3 -m services.data.dukascopy XAUUSD XAGUSD LIGHTCMDUSD GASCMDUSD --from 2024-01-01

Per-day files are cached under var/cache/dukascopy/<SYM>/ so an interrupted run resumes; the assembled 5-minute series (timestamps in IST) is written to var/archive/global/<SYM>_5minute.csv.
Be polite: the server answers 429/503 when hit hard, so requests are spaced and retried with back-off.
"""
from __future__ import annotations

import argparse
import lzma
import struct
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from core.paths import ARCHIVE_ROOT, CACHE_DIR

URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m:02d}/{d:02d}/BID_candles_min_1.bi5"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
# Integer price scale per instrument, checked against known prices (XAUUSD 2915.148 on 2025-03-12, Brent 69.917, natgas 4.3363, copper 4.7916).
SCALE = {"XAUUSD": 1e3, "XAGUSD": 1e3, "LIGHTCMDUSD": 1e3, "BRENTCMDUSD": 1e3, "GASCMDUSD": 1e4, "COPPERCMDUSD": 1e4}
OUT_DIR = ARCHIVE_ROOT / "global"
DAY_DIR = CACHE_DIR / "dukascopy"
IST = timezone(timedelta(hours=5, minutes=30))
COLS = ["timestamp", "open", "high", "low", "close", "volume"]


def decode(payload: bytes, day: date, scale: float) -> pd.DataFrame:
    """One day's file -> 1-minute candles (UTC timestamps)."""
    raw = lzma.decompress(payload) if payload else b""
    rows = []
    base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    for off in range(0, len(raw) - 23, 24):
        sec, o, c, lo, hi, vol = struct.unpack(">IIIIIf", raw[off:off + 24])
        rows.append((base + timedelta(seconds=sec), o / scale, hi / scale, lo / scale, c / scale, vol))
    return pd.DataFrame(rows, columns=COLS)


def fetch_day(sym: str, day: date, session: requests.Session, tries: int = 6) -> bytes | None:
    """Payload for one day; b'' when the market was closed (404 / empty); None only if every retry failed (not cached, so a rerun tries again)."""
    url = URL.format(sym=sym, y=day.year, m=day.month - 1, d=day.day)
    for attempt in range(tries):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.content
            if r.status_code == 404:
                return b""
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def download(sym: str, start: date, end: date, pause: float = 0.4, log=print) -> Path:
    cache = DAY_DIR / sym
    cache.mkdir(parents=True, exist_ok=True)
    scale = SCALE[sym]
    session = requests.Session()
    day, failed = start, []
    while day <= end:
        if day.weekday() < 5 and not (cache / f"{day}.csv").exists():
            payload = fetch_day(sym, day, session)
            if payload is None:
                failed.append(day)
            else:
                decode(payload, day, scale).to_csv(cache / f"{day}.csv", index=False)
            time.sleep(pause)
            if day.day == 1:
                log(f"{sym}: reached {day}")
        day += timedelta(days=1)
    if failed:
        log(f"{sym}: {len(failed)} day(s) failed after retries (rerun to retry): {failed[:5]}")
    return assemble(sym)


def assemble(sym: str) -> Path:
    """Concatenate the cached days into a 5-minute series (IST timestamps) at var/archive/global/<SYM>_5minute.csv."""
    frames = [pd.read_csv(p, parse_dates=["timestamp"]) for p in sorted((DAY_DIR / sym).glob("*.csv")) if p.stat().st_size > 30]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{sym}_5minute.csv"
    if not frames:
        return out
    m = pd.concat(frames).drop_duplicates("timestamp").set_index("timestamp").sort_index()
    m5 = m.resample("5min", label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["close"])
    m5.index = m5.index.tz_convert(IST)
    m5.index.name = "timestamp"
    m5.reset_index().to_csv(out, index=False, date_format="%Y-%m-%dT%H:%M:%S%z")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+", choices=sorted(SCALE))
    ap.add_argument("--from", dest="start", default="2024-01-01")
    ap.add_argument("--to", dest="end", default=None)
    args = ap.parse_args(argv)
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    for sym in args.symbols:
        path = download(sym, date.fromisoformat(args.start), end)
        print(f"{sym}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
