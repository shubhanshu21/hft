#!/usr/bin/env python3
"""
markets/equity/data.py -- REAL NSE Equity Historical Data (via Upstox).

Rebuilt 2026-09-19 alongside the rest of the equity-scalper rebuild (see
markets/equity/universe.py's docstring for why the original was removed and
what this rebuild changes). Mirrors markets/commodity/data.py's shape, but
simpler: NSE cash equities are continuously-listed (no monthly-expiry
contract cap), so a single fetch_real_history_backward call per symbol
retrieves everything Upstox has, no mini/base-symbol aliasing needed.

Usage:
    python3 -m markets.equity.data                 # full initial backfill, all NIFTY50 symbols
    python3 -m markets.equity.data --topup          # incremental: only fetch days newer than archived
    python3 -m markets.equity.data --symbols RELIANCE TCS   # subset
"""
from __future__ import annotations

from core.paths import ARCHIVE_ROOT, BACKEND_ROOT, LOG_DIR
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(BACKEND_ROOT))

import pandas as pd

from services.auth.upstox_auto_login import ensure_fresh_upstox_token
from services.broker.instruments import ensure_master, get_instrument_key
from services.broker.upstox_broker import UpstoxBroker
from engine.config import UpstoxConfig
from markets.equity.universe import NIFTY50_SYMBOLS
from services.utils.logger import get_logger, setup_logger

ARCHIVE_DIR = ARCHIVE_ROOT / "equity"
# (unit, interval, file suffix) -- one CSV per symbol per timeframe: <SYMBOL>_<suffix>.csv
INTERVALS = [
    ("minutes", 5, "5minute"),
    ("minutes", 3, "3minute"),
]
_MAX_LOOKBACK_DAYS = 1500  # generous upper bound; real cutoff is each stock's own listing date, discovered not assumed
log = get_logger("real_equity_data")


def _get_broker() -> UpstoxBroker:
    token = ensure_fresh_upstox_token() or UpstoxConfig.ACCESS_TOKEN
    if not token:
        raise RuntimeError("No valid Upstox access token available -- run `python3 -m auth.upstox_auth` first.")
    return UpstoxBroker(access_token=token, dry_run=True)


def download_real_equity_history(symbol: str, unit: str = "minutes", interval: int = 5) -> pd.DataFrame | None:
    from services.data.candles import fetch_real_history_backward
    broker = _get_broker()
    ikey = get_instrument_key(symbol)
    if not ikey:
        log.warning("%s: could not resolve NSE_EQ instrument_key.", symbol)
        return None
    candles = fetch_real_history_backward(broker, ikey, unit, interval, max_lookback_days=_MAX_LOOKBACK_DAYS)
    if not candles:
        log.warning("%s: no real history available.", symbol)
        return None
    df = pd.DataFrame(candles).sort_values("timestamp").reset_index(drop=True)
    log.info("%s %s%d: fetched %d real candles, %s to %s.", symbol, unit[:3], interval, len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
    return df


def topup_real_equity_history(symbol: str, unit: str, interval: int, suffix: str) -> int:
    out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
    if not out_path.exists():
        df = download_real_equity_history(symbol, unit, interval)
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
        log.info("%s %s: already up to date.", symbol, suffix)
        return 0

    from services.data.candles import fetch_real_history_backward
    broker = _get_broker()
    ikey = get_instrument_key(symbol)
    if not ikey:
        return 0
    gap_days = (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1
    candles = fetch_real_history_backward(broker, ikey, unit, interval, max_lookback_days=gap_days)
    if not candles:
        return 0
    new_df = pd.DataFrame(candles)
    combined = pd.concat([existing, new_df], ignore_index=True).drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    added = len(combined) - len(existing)
    combined.to_csv(out_path, index=False)
    log.info("%s %s: appended %d new rows (now %d total).", symbol, suffix, added, len(combined))
    return added


def build_all_equity_archives(symbols: list[str] | None = None, topup: bool = False) -> None:
    symbols = symbols or NIFTY50_SYMBOLS
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ensure_master()
    print(f"\n{'='*75}\n  {'TOPPING UP' if topup else 'BUILDING'} REAL NSE EQUITY ARCHIVES ({len(symbols)} symbols)\n{'='*75}\n")
    for symbol in symbols:
        print(f"  {symbol}...")
        for unit, interval, suffix in INTERVALS:
            if topup:
                added = topup_real_equity_history(symbol, unit, interval, suffix)
                print(f"    {suffix}: +{added} new rows")
            else:
                df = download_real_equity_history(symbol, unit, interval)
                if df is None or df.empty:
                    print(f"    {suffix}: No data available.")
                    continue
                out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
                df.to_csv(out_path, index=False)
                print(f"    {suffix}: saved {len(df):,} candles -> {out_path.name} ({df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]})")
    print(f"\n{'='*75}\n  Done.\n{'='*75}\n")


if __name__ == "__main__":
    setup_logger("", log_file=str(LOG_DIR / "real_equity_data.log"))
    parser = argparse.ArgumentParser(description="Real NSE equity data (NIFTY50) via Upstox")
    parser.add_argument("--topup", action="store_true", help="Incremental top-up instead of a full backfill")
    parser.add_argument("--symbols", nargs="+", default=None, help="Subset of symbols instead of the full NIFTY50 list")
    args = parser.parse_args()
    build_all_equity_archives(symbols=args.symbols, topup=args.topup)
