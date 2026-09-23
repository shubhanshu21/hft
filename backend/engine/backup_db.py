#!/usr/bin/env python3
"""
backup_db.py — daily backup of var/db/paper_trading.db.

Added 2026-09-18: the entire paper-trading history (every trade, every
position, account balances) lives in one SQLite file with no backup
mechanism anywhere in this project -- a disk problem, a bad migration, or
an accidental `cli.py reset-db` would silently lose all of it with no way
back. Runs via sqlite3's own backup API (Connection.backup()), not a raw
file copy -- that takes a consistent snapshot even if the live dryrun
daemon is mid-write, where a plain `cp` could copy a torn/corrupt page.

Keeps the last KEEP_BACKUPS daily snapshots, pruning older ones so this
doesn't grow unbounded.

Usage:
    python3 -m engine.backup_db
"""
from __future__ import annotations

from core.paths import DB_DIR, LOG_DIR
import sqlite3
from datetime import date
from pathlib import Path

from services.utils.logger import get_logger, setup_logger

DB_PATH = DB_DIR / "paper_trading.db"
BACKUP_DIR = DB_DIR / "backups"
KEEP_BACKUPS = 14

log = get_logger("backup_db")


def backup_once() -> Path | None:
    if not DB_PATH.exists():
        log.warning("No DB found at %s -- nothing to back up.", DB_PATH)
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"paper_trading_{date.today().isoformat()}.db"

    src_conn = sqlite3.connect(str(DB_PATH))
    dest_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()

    log.info("Backed up %s -> %s (%.1f KB)", DB_PATH, dest, dest.stat().st_size / 1024)

    existing = sorted(BACKUP_DIR.glob("paper_trading_*.db"))
    for old in existing[:-KEEP_BACKUPS]:
        old.unlink()
        log.info("Pruned old backup: %s", old)

    return dest


if __name__ == "__main__":
    setup_logger("", log_file=str(LOG_DIR / "backup_db.log"))
    result = backup_once()
    if result:
        print(f"Backed up to {result}")
