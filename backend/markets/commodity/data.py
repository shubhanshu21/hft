#!/usr/bin/env python3
"""
backend/real_commodity_data.py — REAL MCX Commodity Historical Data (via Upstox).

Replaces markets/commodity/synthetic_data.py's synthetic random-walk generator for
CRUDEOILM / NATGASMINI (the only two symbols the live strategy actually
trades) with genuine historical candles fetched from Upstox's own History
API -- the same broker this bot already authenticates and live-trades
through, so no new credentials/account are needed.

IMPORTANT CONSTRAINT: MCX commodity futures are monthly-expiry contracts,
not continuously-listed instruments like equities -- real history for the
CURRENT active contract only reaches back to that contract's own listing
date, not years. This same cap applies to every interval below (1min/5min/
15min/1day all come from the same underlying contract) -- daily bars don't
get more history, just fewer, coarser rows.

CORRECTED 2026-09-18: the ~1-month figure quoted here until now (and the
binary-search-for-earliest-date approach this module used to use) was
itself partly an artifact of a separate bug, not the true contract-listing
constraint -- a single wide from=/to= Upstox history call silently returns
only a small recent slice instead of erroring, the same class of bug found
independently for currency/index-futures/Binance funding-rate data. Fixed
by routing through data.candles.fetch_real_history_backward's chunked
backward probe (see that function's docstring) -- verified directly on
CRUDEOILM: a single 2026-08-01..2026-09-18 call returned 92 candles: the
same range chunked returned 7,560. The real contract-listing constraint
still exists and is still discovered (not assumed), it just goes back
meaningfully further than the old single-call approach ever revealed.

This module intentionally does NOT try to stitch together older expired
contracts' instrument_keys into a longer synthetic-feeling history -- that
data exists on Upstox in principle but each expired contract needs its own
instrument_key resolved separately, which is out of scope here. Real
history accumulates naturally, one real trading day at a time, via the
daily top-up job (hft-daily-data-topup.timer).

Usage:
    python3 -m markets.commodity.data                 # full initial backfill, both symbols, all intervals
    python3 -m markets.commodity.data --topup           # incremental: only fetch days newer than what's archived
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
from services.broker.instruments import build_mcx_commodity_map
from services.broker.upstox_broker import UpstoxBroker
from engine.config import UpstoxConfig
from services.utils.logger import get_logger, setup_logger

ARCHIVE_DIR = ARCHIVE_ROOT / "commodity"

# {archive filename prefix: MCX trading symbol used to resolve the instrument_key}
SYMBOLS = {
    "CRUDEOIL": "CRUDEOILM",     # archive under the base name (matches backtest/live's COMMODITY_ALIASES lookup)
    "NATURALGAS": "NATGASMINI",
    # Added 2026-09-17 to survey other MCX commodities beyond crude/natgas, per user
    # request -- cost-model support (contract multipliers) already existed in
    # markets/commodity/costs.py's get_contract_multiplier fallbacks; the only real
    # blocker was broker/instruments.py's hardcoded base-symbol allowlist, now fixed.
    "GOLD": "GOLDM",
    "SILVER": "SILVERMIC",
    "COPPER": "COPPER",
    # Added 2026-09-18: base metals survey (copper already covered above came
    # back weak/20% robust -- checking the rest of the MCX base-metal group
    # with the same discipline). Mini contracts used where they exist
    # (lower margin, matches the CRUDEOILM/GOLDM/SILVERMIC convention above);
    # NICKEL has no mini variant on MCX.
    "ALUMINIUM": "ALUMINI",
    "LEAD": "LEADMINI",
    "ZINC": "ZINCMINI",
    "NICKEL": "NICKEL",
}

# (Upstox unit, Upstox interval, archive filename suffix)
INTERVALS = [
    ("minutes", 1, "1minute"),
    ("minutes", 5, "5minute"),
    ("minutes", 15, "15minute"),
    ("days", 1, "1day"),
]

_MAX_PROBE_DAYS = 120  # generous upper bound; the real cutoff (contract listing date) is discovered, not assumed
log = get_logger("real_commodity_data")


def _get_broker() -> UpstoxBroker:
    token = ensure_fresh_upstox_token() or UpstoxConfig.ACCESS_TOKEN
    if not token:
        raise RuntimeError("No valid Upstox access token available -- run `python3 -m auth.upstox_auth` first.")
    return UpstoxBroker(access_token=token, dry_run=True)


def download_real_commodity_history(symbol: str, unit: str, interval: int, mcx_symbol: str) -> pd.DataFrame | None:
    """Fetches ALL real history currently available on Upstox for one symbol/interval (full backfill, not incremental).
    Uses data.candles.fetch_real_history_backward -- see that function's docstring for why a single
    wide from=/to= call (the previous approach here, via _find_earliest_available + one call) was
    silently under-archiving this exact dataset for as long as this module has existed."""
    from services.data.candles import fetch_real_history_backward
    broker = _get_broker()
    instrument_key = build_mcx_commodity_map().get(mcx_symbol)
    if not instrument_key:
        log.warning("%s: could not resolve MCX instrument_key.", mcx_symbol)
        return None

    candles = fetch_real_history_backward(broker, instrument_key, unit, interval, max_lookback_days=_MAX_PROBE_DAYS)
    if not candles:
        log.warning("%s: no real history available at all (contract may be unlisted/expired).", mcx_symbol)
        return None

    df = pd.DataFrame(candles).sort_values("timestamp").reset_index(drop=True)
    log.info("%s (%s/%d): fetched %d real candles (chunked), %s to %s.",
              mcx_symbol, unit, interval, len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
    return df


def topup_real_commodity_history(symbol: str, unit: str, interval: int, mcx_symbol: str, suffix: str) -> int:
    """Incremental: fetches only candles newer than what's already archived, appends de-duplicated. Returns rows added."""
    out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
    if not out_path.exists():
        log.info("%s: no existing archive, doing a full backfill instead of a top-up.", symbol)
        df = download_real_commodity_history(symbol, unit, interval, mcx_symbol)
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

    from services.data.candles import fetch_real_history_backward
    gap_days = (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1
    candles = fetch_real_history_backward(broker, instrument_key, unit, interval, max_lookback_days=gap_days)
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
        for unit, interval, suffix in INTERVALS:
            print(f"  {mcx_symbol} @ {suffix}...")
            if topup:
                added = topup_real_commodity_history(symbol, unit, interval, mcx_symbol, suffix)
                print(f"    +{added} new rows")
            else:
                df = download_real_commodity_history(symbol, unit, interval, mcx_symbol)
                if df is None or df.empty:
                    print(f"    No data available.")
                    continue
                out_path = ARCHIVE_DIR / f"{symbol}_{suffix}.csv"
                df.to_csv(out_path, index=False)
                print(f"    Saved {len(df):,} candles -> {out_path.name} "
                      f"({df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]})")

    # Mini-contract aliases (backtest/train code looks up the base symbol's file via COMMODITY_ALIASES)
    for mini_sym, base_sym in {"CRUDEOILM": "CRUDEOIL", "NATGASMINI": "NATURALGAS"}.items():
        for _, _, suffix in INTERVALS:
            base_file = ARCHIVE_DIR / f"{base_sym}_{suffix}.csv"
            mini_file = ARCHIVE_DIR / f"{mini_sym}_{suffix}.csv"
            if base_file.exists():
                import shutil
                shutil.copyfile(base_file, mini_file)

    print(f"\n{'='*75}\n  Done.\n{'='*75}\n")


if __name__ == "__main__":
    setup_logger("", log_file=str(LOG_DIR / "real_commodity_data.log"))
    parser = argparse.ArgumentParser(description="Real MCX commodity data (CRUDEOILM/NATGASMINI/GOLDM/SILVERMIC/COPPER) via Upstox")
    parser.add_argument("--topup", action="store_true", help="Incremental top-up instead of a full backfill")
    args = parser.parse_args()
    build_all_real_commodity_archives(topup=args.topup)
