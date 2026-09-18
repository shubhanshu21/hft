#!/usr/bin/env python3
"""
backend/binance_data.py — Real Binance USDT-M Perpetual Futures Historical Data.

Fetches genuine historical 5-minute candles for BTCUSDT/ETHUSDT perpetual
futures from Binance's public Futures market-data API (no API key needed --
/fapi/v1/klines and /fapi/v1/fundingRate are both public endpoints), mirroring
real_commodity_data.py's archive-and-topup pattern for the MCX pipeline.

Unlike MCX commodity futures (monthly-expiry contracts capped at ~1 month of
real history per contract), Binance perpetuals are continuously listed --
requesting startTime=0 simply gets clamped to the symbol's actual listing
date by Binance, so no binary-search-for-earliest-date is needed here.

Usage:
    python3 -m binance_data                 # full initial backfill, both symbols
    python3 -m binance_data --topup           # incremental: only fetch candles newer than what's archived
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import requests

from utils.logger import get_logger, setup_logger

ARCHIVE_DIR = Path(__file__).resolve().parent / "archive_crypto"

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "5m"          # Binance kline interval string
SUFFIX = "5minute"        # keep naming consistent with archive_commodities/*_5minute.csv

KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

_KLINES_LIMIT = 1500     # Binance's max rows per /fapi/v1/klines call
_FUNDING_LIMIT = 1000    # Binance's max rows per /fapi/v1/fundingRate call
_REQUEST_TIMEOUT = 15
_RATE_LIMIT_SLEEP = 0.3  # be polite to the public endpoint between pages

log = get_logger("binance_data")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _fetch_klines(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Paginates /fapi/v1/klines from start_ms to end_ms, 1500 candles per call."""
    rows: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        resp = requests.get(
            KLINES_URL,
            params={
                "symbol": symbol,
                "interval": INTERVAL,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": _KLINES_LIMIT,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        last_close_time = batch[-1][6]
        next_cursor = last_close_time + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < _KLINES_LIMIT:
            break
        time.sleep(_RATE_LIMIT_SLEEP)

    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "taker_buy_base"])

    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_localize(None)
    for col in ("open", "high", "low", "close", "volume", "taker_buy_base"):
        df[col] = df[col].astype(float)
    # taker_buy_base kept as a real order-flow proxy: taker_buy_base vs (volume -
    # taker_buy_base) approximates aggressive buy vs sell volume within the bar --
    # not available from MCX/Upstox candles, genuinely crypto-specific signal.
    df = df[["timestamp", "open", "high", "low", "close", "volume", "taker_buy_base"]]
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


_FUNDING_MAX_WINDOW_MS = 190 * 24 * 3600 * 1000  # Binance caps startTime..endTime at 200 days for this endpoint;
                                                    # a wider span is silently reinterpreted as "most recent records
                                                    # ending at endTime" instead of erroring, which would silently
                                                    # skip all older history -- so we chunk requests ourselves.


def _fetch_funding_rates(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Paginates /fapi/v1/fundingRate (funding events occur roughly every 8 hours), chunked into <=190-day windows."""
    rows: list[dict] = []
    win_start = start_ms
    while win_start < end_ms:
        win_end = min(win_start + _FUNDING_MAX_WINDOW_MS, end_ms)
        cursor = win_start
        while cursor < win_end:
            resp = requests.get(
                FUNDING_URL,
                params={"symbol": symbol, "startTime": cursor, "endTime": win_end, "limit": _FUNDING_LIMIT},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            rows.extend(batch)
            next_cursor = batch[-1]["fundingTime"] + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < _FUNDING_LIMIT:
                break
            time.sleep(_RATE_LIMIT_SLEEP)
        win_start = win_end
        time.sleep(_RATE_LIMIT_SLEEP)

    if not rows:
        return pd.DataFrame(columns=["timestamp", "funding_rate"])

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True).dt.tz_localize(None)
    df["funding_rate"] = df["fundingRate"].astype(float)
    df = df[["timestamp", "funding_rate"]].drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def download_full_history(symbol: str) -> pd.DataFrame:
    """Fetches ALL real 5-minute history Binance has for a perpetual future (from listing date to now)."""
    df = _fetch_klines(symbol, start_ms=0, end_ms=_now_ms())
    if not df.empty:
        log.info("%s: fetched %d real 5-min candles, %s to %s.", symbol, len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
    return df


def topup_history(symbol: str) -> int:
    """Incremental: fetches only candles newer than what's already archived. Returns rows added."""
    out_path = ARCHIVE_DIR / f"{symbol}_{SUFFIX}.csv"
    if not out_path.exists():
        log.info("%s: no existing archive, doing a full backfill instead of a top-up.", symbol)
        df = download_full_history(symbol)
        if df.empty:
            return 0
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)
        return len(df)

    existing = pd.read_csv(out_path)
    last_ts = pd.Timestamp(existing["timestamp"].max())
    start_ms = int(last_ts.timestamp() * 1000) + 1
    end_ms = _now_ms()
    if start_ms >= end_ms:
        log.info("%s: already up to date (archive ends %s).", symbol, last_ts)
        return 0

    new_df = _fetch_klines(symbol, start_ms=start_ms, end_ms=end_ms)
    if new_df.empty:
        log.info("%s: no new candles since %s.", symbol, last_ts)
        return 0

    combined = pd.concat([existing.assign(timestamp=pd.to_datetime(existing["timestamp"])), new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    added = len(combined) - len(existing)
    combined.to_csv(out_path, index=False)
    log.info("%s: archive was current through %s -- appended %d new rows (now %d total).", symbol, last_ts, added, len(combined))
    return added


def _topup_funding(symbol: str) -> int:
    out_path = ARCHIVE_DIR / f"{symbol}_funding.csv"
    if not out_path.exists():
        df = _fetch_funding_rates(symbol, start_ms=0, end_ms=_now_ms())
        if df.empty:
            return 0
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)
        return len(df)

    existing = pd.read_csv(out_path)
    last_ts = pd.Timestamp(existing["timestamp"].max())
    start_ms = int(last_ts.timestamp() * 1000) + 1
    end_ms = _now_ms()
    if start_ms >= end_ms:
        return 0

    new_df = _fetch_funding_rates(symbol, start_ms=start_ms, end_ms=end_ms)
    if new_df.empty:
        return 0

    combined = pd.concat([existing.assign(timestamp=pd.to_datetime(existing["timestamp"])), new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    added = len(combined) - len(existing)
    combined.to_csv(out_path, index=False)
    return added


def build_all_crypto_archives(topup: bool = False) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*75}")
    print(f"  {'TOPPING UP' if topup else 'BUILDING'} REAL BINANCE CRYPTO ARCHIVES (BTCUSDT/ETHUSDT Perp Futures)")
    print(f"{'='*75}\n")

    for symbol in SYMBOLS:
        print(f"  {symbol} @ {INTERVAL}...")
        if topup:
            added = topup_history(symbol)
            print(f"    +{added} new candles")
        else:
            df = download_full_history(symbol)
            if df.empty:
                print(f"    No data available.")
                continue
            out_path = ARCHIVE_DIR / f"{symbol}_{SUFFIX}.csv"
            df.to_csv(out_path, index=False)
            print(f"    Saved {len(df):,} candles -> {out_path.name} ({df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]})")

        print(f"  {symbol} funding rate history...")
        funding_added = _topup_funding(symbol)
        print(f"    +{funding_added} new funding events")

    print(f"\n{'='*75}\n  Done.\n{'='*75}\n")


if __name__ == "__main__":
    setup_logger("", log_file=str(Path(__file__).resolve().parent / "logs" / "binance_data.log"))
    parser = argparse.ArgumentParser(description="Real Binance crypto data (BTCUSDT/ETHUSDT perpetual futures)")
    parser.add_argument("--topup", action="store_true", help="Incremental top-up instead of a full backfill")
    args = parser.parse_args()
    build_all_crypto_archives(topup=args.topup)
