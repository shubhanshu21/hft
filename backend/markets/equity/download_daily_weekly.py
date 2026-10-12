#!/usr/bin/env python3
"""
markets/equity/download_daily_weekly.py
Download daily and weekly candles for all NIFTY50 symbols using the
Upstox v2 API (HistoryApi), which accepts interval strings like "day" and "week".

v3 API (used by the main broker) only supports minutes/hours.
v2 API supports: 1minute, 30minute, day, week, month.

Usage:
    python3 -m markets.equity.download_daily_weekly
    python3 -m markets.equity.download_daily_weekly --symbols RELIANCE TCS
    python3 -m markets.equity.download_daily_weekly --topup
    python3 -m markets.equity.download_daily_weekly --tf day week
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
import upstox_client

from engine.config import UpstoxConfig
from markets.equity.universe import NIFTY50_SYMBOLS
from services.auth.upstox_auto_login import ensure_fresh_upstox_token
from services.broker.instruments import ensure_master, get_instrument_key
from services.utils.logger import get_logger, setup_logger

ARCHIVE_DIR = ARCHIVE_ROOT / "equity"
log = get_logger("equity_daily_weekly")

# (interval_str, file_suffix, max_lookback_days, chunk_days)
# v2 limits per Upstox docs: day=1yr, week=10yr, month=10yr
INTERVALS = {
    "day":   ("day",   "daily",   1500, 365),   # use 365-day chunks (well under 1yr cap)
    "week":  ("week",  "weekly",  3650, 730),   # 10yr history; 2yr chunks
}

# Upstox v2 API version header
_API_VER = "2.0"

_api_client: upstox_client.ApiClient | None = None
_history_api: upstox_client.HistoryApi | None = None


def _get_api() -> upstox_client.HistoryApi:
    global _api_client, _history_api
    if _history_api is None:
        token = ensure_fresh_upstox_token() or UpstoxConfig.ACCESS_TOKEN
        if not token:
            raise RuntimeError("No valid Upstox access token. Run: python3 -m services.auth.upstox_auth")
        cfg = upstox_client.Configuration()
        cfg.access_token = token
        _api_client = upstox_client.ApiClient(cfg)
        _history_api = upstox_client.HistoryApi(_api_client)
    return _history_api


def _fetch_chunk(instrument_key: str, interval_str: str,
                 from_date: str, to_date: str) -> list[dict]:
    """Single chunk via v2 HistoryApi.get_historical_candle_data1()."""
    api = _get_api()
    try:
        resp = api.get_historical_candle_data1(
            instrument_key=instrument_key,
            interval=interval_str,
            to_date=to_date,
            from_date=from_date,
            api_version=_API_VER,
        )
        if resp.status != "success" or resp.data is None:
            return []
        return [
            {"timestamp": row[0], "open": row[1], "high": row[2],
             "low": row[3], "close": row[4], "volume": row[5]}
            for row in resp.data.candles
        ]
    except Exception as exc:
        log.warning("_fetch_chunk %s %s %s..%s: %s", instrument_key, interval_str, from_date, to_date, exc)
        return []


def _fetch_all_history(instrument_key: str, interval_str: str,
                       max_lookback_days: int, chunk_days: int) -> list[dict]:
    """Fetch all available history backwards in chunks, same logic as fetch_real_history_backward."""
    today = date.today()
    all_rows: dict[str, dict] = {}
    chunk_end = today
    empty_in_a_row = 0

    while (today - chunk_end).days < max_lookback_days:
        chunk_start = max(chunk_end - timedelta(days=chunk_days),
                          today - timedelta(days=max_lookback_days))
        rows = _fetch_chunk(instrument_key, interval_str,
                            chunk_start.isoformat(), chunk_end.isoformat())
        if rows:
            for r in rows:
                all_rows[r["timestamp"]] = r
            empty_in_a_row = 0
        else:
            empty_in_a_row += 1
            if empty_in_a_row >= 2:
                break

        chunk_end = chunk_start - timedelta(days=1)
        time.sleep(0.2)  # gentle rate limiting

    return sorted(all_rows.values(), key=lambda r: r["timestamp"])


def download_symbol(symbol: str, interval_str: str, suffix: str,
                    max_lookback_days: int, chunk_days: int) -> "pd.DataFrame | None":
    ikey = get_instrument_key(symbol)
    if not ikey:
        log.warning("%s: no instrument_key – skipped.", symbol)
        return None

    log.info("%s  %s  fetching...", symbol, suffix)
    candles = _fetch_all_history(ikey, interval_str, max_lookback_days, chunk_days)
    if not candles:
        log.warning("%s  %s: no candles.", symbol, suffix)
        return None

    df = pd.DataFrame(candles).sort_values("timestamp").reset_index(drop=True)
    log.info("%s  %s: %d candles  %s -> %s", symbol, suffix, len(df),
             df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
    return df


def topup_symbol(symbol: str, interval_str: str, suffix: str,
                 max_lookback_days: int, chunk_days: int) -> int:
    out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
    if not out_path.exists():
        df = download_symbol(symbol, interval_str, suffix, max_lookback_days, chunk_days)
        if df is None or df.empty:
            return 0
        df.to_csv(out_path, index=False)
        return len(df)

    existing = pd.read_csv(out_path)
    last_date = pd.Timestamp(existing["timestamp"].max()).date()
    from_date = (last_date + timedelta(days=1)).isoformat()
    to_date = date.today().isoformat()
    if from_date > to_date:
        log.info("%s  %s: already up to date.", symbol, suffix)
        return 0

    ikey = get_instrument_key(symbol)
    if not ikey:
        return 0
    gap_rows = _fetch_chunk(ikey, interval_str, from_date, to_date)
    if not gap_rows:
        return 0

    new_df = pd.DataFrame(gap_rows)
    combined = (pd.concat([existing, new_df], ignore_index=True)
                .drop_duplicates(subset="timestamp")
                .sort_values("timestamp")
                .reset_index(drop=True))
    added = len(combined) - len(existing)
    combined.to_csv(out_path, index=False)
    log.info("%s  %s: +%d rows.", symbol, suffix, added)
    return added


def run(symbols: list, topup: bool, timeframes: list) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ensure_master()

    tfs = {k: v for k, v in INTERVALS.items() if k in timeframes}
    mode = "TOP-UP" if topup else "FULL DOWNLOAD"
    print(f"\n{'='*72}")
    print(f"  {mode}  -  {len(symbols)} symbols  x  {list(tfs)}  (v2 API: day/week)")
    print(f"{'='*72}\n")

    totals = {k: 0 for k in tfs}

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {symbol}")
        for tf_name, (interval_str, suffix, max_days, chunk_days) in tfs.items():
            out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
            try:
                if topup:
                    n = topup_symbol(symbol, interval_str, suffix, max_days, chunk_days)
                    print(f"      {suffix:<10}  +{n:>6} rows")
                    totals[tf_name] += n
                else:
                    df = download_symbol(symbol, interval_str, suffix, max_days, chunk_days)
                    if df is not None and not df.empty:
                        df.to_csv(out_path, index=False)
                        print(f"      {suffix:<10}  {len(df):>7} candles -> {out_path.name}")
                        totals[tf_name] += len(df)
                    else:
                        print(f"      {suffix:<10}  NO DATA")
            except Exception as exc:
                log.error("%s  %s: %s", symbol, suffix, exc)
                print(f"      {suffix:<10}  ERROR: {exc}")
            time.sleep(0.3)

    print(f"\n{'='*72}")
    print("  Summary:")
    for tf_name, count in totals.items():
        suffix = INTERVALS[tf_name][1]
        print(f"    {suffix:<10}  {count:>8} total rows  ({len(symbols)} symbols)")
    print(f"{'='*72}\n")


if __name__ == "__main__":
    setup_logger("", log_file=str(LOG_DIR / "equity_daily_weekly.log"))

    parser = argparse.ArgumentParser(
        description="Download daily/weekly equity candles from Upstox (v2 API)"
    )
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--topup", action="store_true")
    parser.add_argument("--tf", nargs="+", default=list(INTERVALS.keys()),
                        choices=list(INTERVALS.keys()))
    args = parser.parse_args()

    syms = args.symbols or NIFTY50_SYMBOLS
    run(syms, args.topup, args.tf)
