#!/usr/bin/env python3
"""
backend/real_commodity_data.py — REAL MCX Commodity Historical Data (via Upstox).

Replaces download_commodity_data.py's synthetic random-walk generator for
CRUDEOILM / NATGASMINI (the only two symbols the live strategy actually
trades) with genuine historical candles fetched from Upstox's own History
API -- the same broker this bot already authenticates and live-trades
through, so no new credentials/account are needed.

IMPORTANT CONSTRAINT (verified empirically 2026-09-10): MCX commodity
futures are monthly-expiry contracts, not continuously-listed instruments
like equities. Upstox's real history for the CURRENT active contract only
reaches back to that contract's own listing date -- for CRUDEOILM/
NATGASMINI right now that's 2026-08-10, i.e. ~1 month of genuine history,
not years. Requesting a from_date before the contract's listing date
returns zero candles (not a partial/clipped result), so this always
starts from the earliest date that actually returns data rather than
assuming a fixed lookback window.

This module intentionally does NOT try to stitch together older expired
contracts' instrument_keys into a longer synthetic-feeling history -- that
data exists on Upstox in principle but each expired contract needs its own
instrument_key resolved separately, which is out of scope here. Real
history accumulates naturally, one real trading day at a time, via the
daily top-up job (see update_archive.py --commodity).

Usage:
    python3 -m real_commodity_data                 # full initial backfill, both symbols, both intervals
    python3 -m real_commodity_data --topup           # incremental: only fetch days newer than what's archived
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from auth.upstox_auto_login import ensure_fresh_upstox_token
from broker.instruments import build_mcx_commodity_map
from broker.upstox_broker import UpstoxBroker
from config import UpstoxConfig
from utils.logger import get_logger, setup_logger

ARCHIVE_DIR = Path(__file__).resolve().parent / "archive_commodities"

# {archive filename prefix: MCX trading symbol used to resolve the instrument_key}
SYMBOLS = {
    "CRUDEOIL": "CRUDEOILM",     # archive under the base name (matches backtest/live's COMMODITY_ALIASES lookup)
    "NATURALGAS": "NATGASMINI",
}

INTERVALS = [(5, "5minute"), (1, "1minute")]

_MAX_PROBE_DAYS = 120  # generous upper bound; the real cutoff (contract listing date) is discovered, not assumed
log = get_logger("real_commodity_data")


def _get_broker() -> UpstoxBroker:
    token = ensure_fresh_upstox_token() or UpstoxConfig.ACCESS_TOKEN
    if not token:
        raise RuntimeError("No valid Upstox access token available -- run `python3 -m auth.upstox_auth` first.")
    return UpstoxBroker(access_token=token, dry_run=True)


def _find_earliest_available(broker: UpstoxBroker, instrument_key: str, interval_minutes: int) -> str | None:
    """
    Binary-searches for the earliest from_date that actually returns candles
    (Upstox returns zero candles, not a clipped result, for a from_date
    before the current contract's listing date -- see module docstring).
    Returns an ISO date string, or None if even a 1-day window returns nothing.
    """
    today = date.today()
    lo, hi = 1, _MAX_PROBE_DAYS  # days back
    earliest_working = None
    while lo <= hi:
        mid = (lo + hi) // 2
        frm = (today - timedelta(days=mid)).isoformat()
        to = today.isoformat()
        candles = broker.get_historical_candles(instrument_key, "minutes", interval_minutes, to, frm)
        if candles:
            earliest_working = mid
            lo = mid + 1  # try further back
        else:
            hi = mid - 1  # too far back, come closer
    if earliest_working is None:
        return None
    return (today - timedelta(days=earliest_working)).isoformat()


def download_real_commodity_history(symbol: str, interval_minutes: int, mcx_symbol: str) -> pd.DataFrame | None:
    """Fetches ALL real history currently available on Upstox for one symbol/interval (full backfill, not incremental)."""
    broker = _get_broker()
    instrument_key = build_mcx_commodity_map().get(mcx_symbol)
    if not instrument_key:
        log.warning("%s: could not resolve MCX instrument_key.", mcx_symbol)
        return None

    earliest = _find_earliest_available(broker, instrument_key, interval_minutes)
    if earliest is None:
        log.warning("%s: no real history available at all (contract may be unlisted/expired).", mcx_symbol)
        return None

    to_date = date.today().isoformat()
    candles = broker.get_historical_candles(instrument_key, "minutes", interval_minutes, to_date, earliest)
    if not candles:
        return None

    df = pd.DataFrame(candles).sort_values("timestamp").reset_index(drop=True)
    log.info("%s (%dmin): fetched %d real candles, %s to %s.",
              mcx_symbol, interval_minutes, len(df), earliest, to_date)
    return df


def topup_real_commodity_history(symbol: str, interval_minutes: int, mcx_symbol: str, suffix: str) -> int:
    """Incremental: fetches only candles newer than what's already archived, appends de-duplicated. Returns rows added."""
    out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
    if not out_path.exists():
        log.info("%s: no existing archive, doing a full backfill instead of a top-up.", symbol)
        df = download_real_commodity_history(symbol, interval_minutes, mcx_symbol)
        if df is None or df.empty:
            return 0
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)
        return len(df)

    existing = pd.read_csv(out_path)
    last_ts = existing["timestamp"].max()
    last_date = pd.Timestamp(last_ts).date()
    from_date = (last_date + timedelta(days=1)).isoformat()
    to_date = date.today().isoformat()
    if from_date > to_date:
        log.info("%s: already up to date (archive ends %s).", symbol, last_ts)
        return 0

    broker = _get_broker()
    instrument_key = build_mcx_commodity_map().get(mcx_symbol)
    if not instrument_key:
        log.warning("%s: could not resolve MCX instrument_key.", mcx_symbol)
        return 0

    candles = broker.get_historical_candles(instrument_key, "minutes", interval_minutes, to_date, from_date)
    if not candles:
        log.info("%s: no new candles (%s to %s).", symbol, from_date, to_date)
        return 0

    new_df = pd.DataFrame(candles)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    added = len(combined) - len(existing)
    combined.to_csv(out_path, index=False)
    log.info("%s: archive was current through %s -- appended %d new rows (now %d total).",
              symbol, last_ts, added, len(combined))
    return added


def build_all_real_commodity_archives(topup: bool = False) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*75}")
    print(f"  {'TOPPING UP' if topup else 'BUILDING'} REAL MCX COMMODITY ARCHIVES (via Upstox)")
    print(f"{'='*75}\n")

    for symbol, mcx_symbol in SYMBOLS.items():
        for interval_minutes, suffix in INTERVALS:
            print(f"  {mcx_symbol} @ {interval_minutes}min...")
            if topup:
                added = topup_real_commodity_history(symbol, interval_minutes, mcx_symbol, suffix)
                print(f"    +{added} new rows")
            else:
                df = download_real_commodity_history(symbol, interval_minutes, mcx_symbol)
                if df is None or df.empty:
                    print(f"    No data available.")
                    continue
                out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
                df.to_csv(out_path, index=False)
                print(f"    Saved {len(df):,} candles -> {out_path.name} "
                      f"({df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]})")

    # Mini-contract aliases (backtest/train code looks up the base symbol's file via COMMODITY_ALIASES)
    for mini_sym, base_sym in {"CRUDEOILM": "CRUDEOIL", "NATGASMINI": "NATURALGAS"}.items():
        for _, suffix in INTERVALS:
            base_file = ARCHIVE_DIR / f"{base_sym}_{suffix}.csv"
            mini_file = ARCHIVE_DIR / f"{mini_sym}_{suffix}.csv"
            if base_file.exists():
                import shutil
                shutil.copyfile(base_file, mini_file)

    print(f"\n{'='*75}\n  Done.\n{'='*75}\n")


if __name__ == "__main__":
    setup_logger("", log_file=str(Path(__file__).resolve().parent / "logs" / "real_commodity_data.log"))
    parser = argparse.ArgumentParser(description="Real MCX commodity data (CRUDEOILM/NATGASMINI) via Upstox")
    parser.add_argument("--topup", action="store_true", help="Incremental top-up instead of a full backfill")
    args = parser.parse_args()
    build_all_real_commodity_archives(topup=args.topup)
