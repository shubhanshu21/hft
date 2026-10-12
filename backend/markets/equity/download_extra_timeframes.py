#!/usr/bin/env python3
"""
markets/equity/download_extra_timeframes.py
Download additional timeframes (30min, daily, weekly) for all NIFTY50 symbols.

Upstox v3 supported intervals:
  minutes / 1   -> 1min  (already have as 1minute.csv.gz)
  minutes / 3   -> 3min  (already have as 3minute.csv)
  minutes / 5   -> 5min  (already have as 5minute.csv)
  minutes / 30  -> 30min (NEW)
  day / 1       -> daily (NEW)
  week / 1      -> weekly (NEW)

Usage:
    python3 -m markets.equity.download_extra_timeframes
    python3 -m markets.equity.download_extra_timeframes --symbols RELIANCE TCS
    python3 -m markets.equity.download_extra_timeframes --topup
    python3 -m markets.equity.download_extra_timeframes --tf 30min daily weekly
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from core.paths import ARCHIVE_ROOT, BACKEND_ROOT, LOG_DIR

sys.path.insert(0, str(BACKEND_ROOT))

import pandas as pd

from engine.config import UpstoxConfig
from markets.equity.universe import NIFTY50_SYMBOLS
from services.auth.upstox_auto_login import ensure_fresh_upstox_token
from services.broker.instruments import ensure_master, get_instrument_key
from services.broker.upstox_broker import UpstoxBroker
from services.data.candles import fetch_real_history_backward
from services.utils.logger import get_logger, setup_logger

ARCHIVE_DIR = ARCHIVE_ROOT / "equity"
log = get_logger("equity_extra_tf")

# (unit, interval, file_suffix, max_lookback_days, chunk_days)
EXTRA_INTERVALS = {
    "30min":  ("minutes", 30, "30minute",  365,  20),
    "daily":  ("day",      1, "daily",    1500, 180),
    "weekly": ("week",     1, "weekly",   3650, 365),
}

_broker: UpstoxBroker | None = None


def _get_broker() -> UpstoxBroker:
    global _broker
    if _broker is None:
        token = ensure_fresh_upstox_token() or UpstoxConfig.ACCESS_TOKEN
        if not token:
            raise RuntimeError(
                "No valid Upstox access token. Run: python3 -m services.auth.upstox_auth"
            )
        _broker = UpstoxBroker(access_token=token, dry_run=True)
    return _broker


def download_symbol_tf(symbol: str, unit: str, interval: int,
                       suffix: str, max_lookback_days: int,
                       chunk_days: int) -> "pd.DataFrame | None":
    broker = _get_broker()
    ikey = get_instrument_key(symbol)
    if not ikey:
        log.warning("%s: could not resolve NSE_EQ instrument_key – skipped.", symbol)
        return None

    log.info("%s  %s  fetching (up to %d days back, %d-day chunks)…",
             symbol, suffix, max_lookback_days, chunk_days)
    candles = fetch_real_history_backward(
        broker, ikey, unit, interval,
        max_lookback_days=max_lookback_days,
        chunk_days=chunk_days,
    )
    if not candles:
        log.warning("%s  %s: no candles returned.", symbol, suffix)
        return None

    df = pd.DataFrame(candles).sort_values("timestamp").reset_index(drop=True)
    log.info("%s  %s: %d candles  %s -> %s",
             symbol, suffix, len(df),
             df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
    return df


def topup_symbol_tf(symbol: str, unit: str, interval: int,
                    suffix: str, max_lookback_days: int,
                    chunk_days: int) -> int:
    out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"

    if not out_path.exists():
        df = download_symbol_tf(symbol, unit, interval, suffix, max_lookback_days, chunk_days)
        if df is None or df.empty:
            return 0
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)
        return len(df)

    existing = pd.read_csv(out_path)
    last_date = pd.Timestamp(existing["timestamp"].max()).date()
    from_date = (last_date + timedelta(days=1)).isoformat()
    to_date = date.today().isoformat()
    if from_date > to_date:
        log.info("%s  %s: already up to date.", symbol, suffix)
        return 0

    gap_days = (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1
    gap_chunk = min(chunk_days, gap_days)

    broker = _get_broker()
    ikey = get_instrument_key(symbol)
    if not ikey:
        return 0
    new_candles = fetch_real_history_backward(
        broker, ikey, unit, interval,
        max_lookback_days=gap_days,
        chunk_days=gap_chunk,
    )
    if not new_candles:
        return 0

    new_df = pd.DataFrame(new_candles)
    combined = (pd.concat([existing, new_df], ignore_index=True)
                .drop_duplicates(subset="timestamp")
                .sort_values("timestamp")
                .reset_index(drop=True))
    added = len(combined) - len(existing)
    combined.to_csv(out_path, index=False)
    log.info("%s  %s: +%d new rows (total %d).", symbol, suffix, added, len(combined))
    return added


def run(symbols: list, topup: bool, timeframes: list) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ensure_master()

    tfs = {k: v for k, v in EXTRA_INTERVALS.items() if k in timeframes}
    if not tfs:
        print(f"No matching timeframes. Valid: {list(EXTRA_INTERVALS)}")
        return

    mode = "TOP-UP" if topup else "FULL DOWNLOAD"
    print(f"\n{'='*72}")
    print(f"  {mode}  -  {len(symbols)} symbols  x  {list(tfs)}  (extra timeframes)")
    print(f"{'='*72}\n")

    totals = {k: 0 for k in tfs}

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {symbol}")
        for tf_name, (unit, interval, suffix, max_days, chunk_days) in tfs.items():
            out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
            try:
                if topup:
                    n = topup_symbol_tf(symbol, unit, interval, suffix, max_days, chunk_days)
                    print(f"      {suffix:<12}  +{n:>6} rows")
                    totals[tf_name] += n
                else:
                    df = download_symbol_tf(symbol, unit, interval, suffix, max_days, chunk_days)
                    if df is not None and not df.empty:
                        df.to_csv(out_path, index=False)
                        print(f"      {suffix:<12}  {len(df):>7} candles  ->  {out_path.name}")
                        totals[tf_name] += len(df)
                    else:
                        print(f"      {suffix:<12}  NO DATA")
            except Exception as exc:
                log.error("%s  %s: %s", symbol, suffix, exc)
                print(f"      {suffix:<12}  ERROR: {exc}")
            time.sleep(0.4)

    print(f"\n{'='*72}")
    print("  Summary:")
    for tf_name, count in totals.items():
        suffix = EXTRA_INTERVALS[tf_name][2]
        print(f"    {suffix:<12}  {count:>8} total rows  ({len(symbols)} symbols)")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    setup_logger("", log_file=str(LOG_DIR / "equity_extra_tf.log"))

    parser = argparse.ArgumentParser(
        description="Download 30min / daily / weekly equity candles from Upstox"
    )
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Subset of symbols (default: all NIFTY50)")
    parser.add_argument("--topup", action="store_true",
                        help="Incremental top-up instead of full download")
    parser.add_argument("--tf", nargs="+",
                        default=list(EXTRA_INTERVALS.keys()),
                        choices=list(EXTRA_INTERVALS.keys()),
                        help="Timeframes to download (default: all three)")
    args = parser.parse_args()

    syms = args.symbols or NIFTY50_SYMBOLS
    run(syms, args.topup, args.tf)
