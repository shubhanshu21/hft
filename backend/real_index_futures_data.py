#!/usr/bin/env python3
"""
backend/real_index_futures_data.py — REAL NSE Index Futures Historical Data (via Upstox).

Mirrors real_currency_data.py exactly, for NIFTY/BANKNIFTY index futures
(NSE_FO/FUTIDX) instead of NCD_FO currency futures. Same monthly-expiry
constraint applies, verified empirically 2026-09-18 the same way: a single
wide-range request silently truncates (only 70 candles for a
2026-01-01..2026-09-18 request vs. 924+1617+1725 candles when the same
range was split into monthly chunks) -- the same class of bug already
found for currency and Binance funding-rate history. NIFTY26SEPFUT (the
front-month contract at time of writing) has real data from ~early July
2026 onward (no June data at all -- it wasn't listed yet, consistent with
NSE's ~3-months-before-expiry listing convention), so real depth here is
similar in order of magnitude to currency's own 24-60 day archives, not
MCX's ~1 month.

Usage:
    python3 -m real_index_futures_data                 # full initial backfill, both symbols, all intervals
    python3 -m real_index_futures_data --topup           # incremental: only fetch days newer than what's archived
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from auth.upstox_auto_login import ensure_fresh_upstox_token
from broker.instruments import build_index_futures_map
from broker.upstox_broker import UpstoxBroker
from config import UpstoxConfig
from utils.logger import get_logger, setup_logger

ARCHIVE_DIR = Path(__file__).resolve().parent / "archive_index_futures"

SYMBOLS = ["NIFTY", "BANKNIFTY"]

INTERVALS = [
    ("minutes", 1, "1minute"),
    ("minutes", 5, "5minute"),
    ("minutes", 15, "15minute"),
    ("days", 1, "1day"),
]

log = get_logger("real_index_futures_data")


def _get_broker() -> UpstoxBroker:
    token = ensure_fresh_upstox_token() or UpstoxConfig.ACCESS_TOKEN
    if not token:
        raise RuntimeError("No valid Upstox access token available -- run `python3 -m auth.upstox_auth` first.")
    return UpstoxBroker(access_token=token, dry_run=True)


_CHUNK_DAYS = 20  # see module docstring -- a wide single-call request silently truncates


def download_real_index_futures_history(symbol: str, unit: str, interval: int, instrument_key: str,
                                          max_lookback_days: int = 100) -> pd.DataFrame | None:
    """Chunked fetch, oldest-to-newest, de-duplicated by timestamp."""
    broker = _get_broker()
    today = date.today()
    all_rows: dict[str, dict] = {}
    chunk_end = today
    empty_chunks_in_a_row = 0
    while (today - chunk_end).days < max_lookback_days:
        chunk_start = chunk_end - timedelta(days=_CHUNK_DAYS)
        candles = broker.get_historical_candles(instrument_key, unit, interval,
                                                   to_date=chunk_end.isoformat(), from_date=chunk_start.isoformat())
        if candles:
            for c in candles:
                all_rows[c["timestamp"]] = c
            empty_chunks_in_a_row = 0
        else:
            empty_chunks_in_a_row += 1
            if empty_chunks_in_a_row >= 2:  # two consecutive empty chunks = past the real listing date
                break
        chunk_end = chunk_start - timedelta(days=1)

    if not all_rows:
        log.warning("%s: no real history available at all.", symbol)
        return None
    df = pd.DataFrame(sorted(all_rows.values(), key=lambda c: c["timestamp"]))
    log.info("%s (%s/%d): fetched %d real candles (chunked), %s to %s.",
              symbol, unit, interval, len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
    return df


def topup_real_index_futures_history(symbol: str, unit: str, interval: int, instrument_key: str, suffix: str) -> int:
    out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
    if not out_path.exists():
        df = download_real_index_futures_history(symbol, unit, interval, instrument_key)
        if df is None or df.empty:
            return 0
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)
        return len(df)

    existing = pd.read_csv(out_path)
    last_ts = existing["timestamp"].max()
    from_date = (pd.Timestamp(last_ts).date() + timedelta(days=1)).isoformat()
    to_date = date.today().isoformat()
    if from_date > to_date:
        return 0

    broker = _get_broker()
    candles = broker.get_historical_candles(instrument_key, unit, interval, to_date, from_date)
    if not candles:
        return 0
    new_df = pd.DataFrame(candles)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    added = len(combined) - len(existing)
    combined.to_csv(out_path, index=False)
    return added


def build_all_real_index_futures_archives(topup: bool = False) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*75}\n  {'TOPPING UP' if topup else 'BUILDING'} REAL NSE INDEX FUTURES ARCHIVES (via Upstox)\n{'='*75}\n")

    idx_map = build_index_futures_map()
    for symbol in SYMBOLS:
        instrument_key = idx_map.get(symbol)
        if not instrument_key:
            print(f"  {symbol}: could not resolve instrument key, skipping.")
            continue
        for unit, interval, suffix in INTERVALS:
            print(f"  {symbol} @ {suffix}...")
            if topup:
                added = topup_real_index_futures_history(symbol, unit, interval, instrument_key, suffix)
                print(f"    +{added} new rows")
            else:
                df = download_real_index_futures_history(symbol, unit, interval, instrument_key)
                if df is None or df.empty:
                    print(f"    No data available.")
                    continue
                out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
                df.to_csv(out_path, index=False)
                print(f"    Saved {len(df):,} candles -> {out_path.name} "
                      f"({df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]})")

    print(f"\n{'='*75}\n  Done.\n{'='*75}\n")


if __name__ == "__main__":
    setup_logger("", log_file=str(Path(__file__).resolve().parent / "logs" / "real_index_futures_data.log"))
    parser = argparse.ArgumentParser(description="Real NSE index futures data (NIFTY/BANKNIFTY) via Upstox")
    parser.add_argument("--topup", action="store_true", help="Incremental top-up instead of a full backfill")
    args = parser.parse_args()
    build_all_real_index_futures_archives(topup=args.topup)
