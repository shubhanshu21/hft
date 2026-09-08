"""
update_archive.py — CLI: top up backend/archive/*.csv (the native
5-minute local archive, see data/local_5min_archive.py) with whatever
recent data Upstox has that the archive doesn't yet — the archive was a
point-in-time download (through 2026-04-09) and goes stale as new
trading days happen.

    python3 -m update_archive                          # curated universe
    python3 -m update_archive --symbols HDFCBANK SBIN   # just these

For each symbol already present in the archive, fetches native 5-minute
candles from the day after the archive's last date through today via
Upstox, and appends only the genuinely new rows (de-duplicated by
timestamp) to that symbol's CSV. Symbols with no existing archive file
are skipped — this tops up existing archive coverage, it doesn't create
new archive entries from scratch (Upstox alone only reaches back to
Jan 2022, far short of the archive's 2015 start).
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from auth.upstox_auto_login import ensure_fresh_upstox_token
from broker.upstox_broker import UpstoxBroker
from config import UpstoxConfig
from data import local_5min_archive
from data.candles import fetch_intraday_candles_range
from strategy.universe import CURATED_SYMBOLS
from utils.logger import get_logger, setup_logger


def main() -> None:
    parser = argparse.ArgumentParser(description="Top up the local 5-minute archive with recent Upstox data.")
    parser.add_argument("--symbols", nargs="+", default=CURATED_SYMBOLS)
    parser.add_argument(
        "--from", dest="from_date", default=None,
        help="YYYY-MM-DD override for the start of the fetch window. Normally the script resumes from the "
             "day after the archive's last date; use this to force a re-fetch of an already-archived range "
             "(e.g. to repair a corrupt segment). Must be <= --to.",
    )
    parser.add_argument("--to", dest="to_date", default=date.today().isoformat(), help="YYYY-MM-DD, default today")
    parser.add_argument("--commodity", action="store_true", help="Update MCX Commodity archives (GOLDM, SILVERMIC, CRUDEOILM, etc.)")
    args = parser.parse_args()

    setup_logger("", log_file="logs/update_archive.log")
    log = get_logger(__name__)

    if args.commodity:
        log.info("Updating MCX Commodity archives via download_commodity_data...")
        from download_commodity_data import build_all_commodity_archives
        build_all_commodity_archives()
        log.info("Commodity archives successfully updated.")
        return

    UpstoxConfig.validate()
    ensure_fresh_upstox_token()
    broker = UpstoxBroker(UpstoxConfig.ACCESS_TOKEN, dry_run=True)
    broker.refresh_instrument_master()

    for symbol in args.symbols:
        last = local_5min_archive.last_date(symbol)
        if last is None:
            log.info("%s: not in the local archive — skipping (top-up only, not initial creation).", symbol)
            continue

        if args.from_date is not None:
            from_date = args.from_date
            log.info("%s: using --from override %s (archive ends %s).", symbol, from_date, last)
        else:
            from_date = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
        if from_date > args.to_date:
            log.info("%s: already up to date (archive ends %s).", symbol, last)
            continue

        try:
            instrument_key = broker.resolve_instrument_key(symbol)
        except Exception as exc:
            log.warning("%s: could not resolve instrument key: %s", symbol, exc)
            continue

        new_candles = fetch_intraday_candles_range(broker, instrument_key, interval_minutes=5, from_date=from_date, to_date=args.to_date)
        added = local_5min_archive.append_candles(symbol, new_candles)
        log.info("%s: archive was current through %s — fetched %d candles (%s to %s), appended %d new rows.",
                  symbol, last, len(new_candles), from_date, args.to_date, added)


if __name__ == "__main__":
    main()

