#!/usr/bin/env python3
"""
backup_db.py -- backups, verification and restore for var/db/paper_trading.db.

The entire paper-trading history (every trade, position, account balance) lives in one SQLite file. Backups use sqlite3's own backup
API (Connection.backup()), not a file copy, so the snapshot is consistent even while the daemon is mid-write.

    python3 -m engine.backup_db                    # daily snapshot  (keeps KEEP_DAILY)
    python3 -m engine.backup_db --hourly           # hourly snapshot (keeps KEEP_HOURLY), run by hft-db-backup.timer
    python3 -m engine.backup_db --verify           # open the newest backup, integrity-check it, count rows; exit 1 if unusable
    python3 -m engine.backup_db --restore latest|FILE [--force]   # replace the live DB (daemon must be stopped unless --force)
    python3 -m engine.backup_db --list

A backup that has never been opened is a hope, not a backup: --verify runs in the nightly job so a corrupt or empty backup is noticed
the next morning, not the day it is needed. A restore first saves the current DB as pre_restore_<time>.db, so it can itself be undone.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from core.paths import DB_DIR, LOG_DIR
from services.utils.logger import get_logger, setup_logger

DB_PATH = DB_DIR / "paper_trading.db"
BACKUP_DIR = DB_DIR / "backups"
KEEP_DAILY = 14
KEEP_HOURLY = 48
KEEP_BACKUPS = KEEP_DAILY                         # kept for older imports
TABLES = ("accounts", "positions", "trades", "orders")

# crypto_paper.db (engine/crypto_paper.py) was added 2026-09-28 and was never
# wired into this nightly job -- found 2026-10-01 while reviewing open issues:
# the Upstox paper account has 14 days of daily snapshots + 48 hours of hourly
# ones, crypto had none at all. Same backup API, same backup directory, a
# distinct filename prefix ("crypto_paper" vs the default "paper_trading") so
# pruning one never touches the other, and its own schema for verification
# (no "accounts" table -- see engine/crypto_paper.py's ledger).
CRYPTO_DB_PATH = DB_DIR / "crypto_paper.db"
CRYPTO_TABLES = ("state", "holdings", "trades", "equity")
# Registry main() iterates for the plain (no --db) daily/hourly/verify/list paths.
# (label, db_path, tables, required_table) -- required_table is what verify_backup()
# checks for to confirm a file is actually THIS database, not just any valid SQLite file.
DATABASES = {
    "main": ("paper_trading", DB_PATH, TABLES, "accounts"),
    "crypto": ("crypto_paper", CRYPTO_DB_PATH, CRYPTO_TABLES, "equity"),
    "breakout": ("crypto_breakout", DB_DIR / "crypto_breakout.db", ("state", "positions", "trades", "equity"), "equity"),   # engine/crypto_breakout.py
}

log = get_logger("backup_db")


def _snapshot(src: Path, dest: Path) -> None:
    src_conn, dest_conn = sqlite3.connect(str(src)), sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()


def _prune(backup_dir: Path, pattern: str, keep: int) -> None:
    for old in sorted(backup_dir.glob(pattern))[:-keep]:
        old.unlink()
        log.info("Pruned old backup: %s", old)


def backup_once(db_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR, kind: str = "daily", now: datetime | None = None, label: str | None = None) -> Path | None:
    """kind: 'daily' -> paper_trading_<date>.db, 'hourly' -> hourly_<date>_<HHMM>.db, or any other tag (e.g. 'pre_reset') -> <tag>_<date>_<HHMMSS>.db (never pruned).
    `label` (added for crypto_paper.db, see DATABASES): None keeps the exact filenames above (the original, still-tested default for the main paper-trading DB);
    given, daily/hourly are named "<label>_<date>.db" / "<label>_hourly_<date>_<HHMM>.db" instead, and pruned by their own non-overlapping patterns, so a second
    database's backups never collide with or get counted against the first's KEEP_DAILY/KEEP_HOURLY."""
    if not db_path.exists():
        log.warning("No DB found at %s -- nothing to back up.", db_path)
        return None
    now = now or datetime.now()
    backup_dir.mkdir(parents=True, exist_ok=True)
    if kind == "daily":
        dest = backup_dir / (f"{label}_{now.date().isoformat()}.db" if label else f"paper_trading_{now.date().isoformat()}.db")
    elif kind == "hourly":
        dest = backup_dir / (f"{label}_hourly_{now.date().isoformat()}_{now.strftime('%H%M')}.db" if label else f"hourly_{now.date().isoformat()}_{now.strftime('%H%M')}.db")
    else:
        dest = backup_dir / f"{kind}_{now.strftime('%Y%m%d_%H%M%S')}.db"
    _snapshot(db_path, dest)
    log.info("Backed up %s -> %s (%.1f KB)", db_path, dest, dest.stat().st_size / 1024)
    if kind == "daily":
        _prune(backup_dir, f"{label}_[0-9]*.db" if label else "paper_trading_*.db", KEEP_DAILY)
    elif kind == "hourly":
        _prune(backup_dir, f"{label}_hourly_*.db" if label else "hourly_*.db", KEEP_HOURLY)
    return dest


def list_backups(backup_dir: Path = BACKUP_DIR) -> list[Path]:
    """Every snapshot, newest first (by modification time)."""
    files = [p for p in backup_dir.glob("*.db")] if backup_dir.exists() else []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def verify_backup(path: Path, tables: tuple[str, ...] = TABLES, required: str = "accounts") -> dict:
    """Open a snapshot read-only, run SQLite's integrity check and count rows. Raises ValueError if it is unusable.
    `tables`/`required` default to the main paper-trading DB's schema; pass CRYPTO_TABLES/"equity" for crypto_paper.db backups (see DATABASES)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"{path.name}: integrity_check said {result!r}")
        counts = {}
        for t in tables:
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[t] = None
        if counts.get(required) is None:
            raise ValueError(f"{path.name}: no {required} table -- not a {tables} database")
        return counts
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"{path.name}: not a usable SQLite database ({exc})") from exc
    finally:
        con.close()


def restore(src: Path, db_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR, tables: tuple[str, ...] = TABLES, required: str = "accounts") -> Path:
    """Replace the live DB with `src`. The source is verified first; the current DB is saved as pre_restore_* so this is reversible."""
    verify_backup(src, tables, required)                        # refuse to overwrite a good DB with a bad backup
    saved = backup_once(db_path, backup_dir, kind="pre_restore") if db_path.exists() else None
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)      # a stale WAL from the old DB must not be replayed onto the restored one
    _snapshot(src, db_path)
    log.info("Restored %s -> %s (previous DB saved as %s)", src, db_path, saved)
    return saved or db_path


def _daemon_running() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "hft-dryrun.service"], capture_output=True, text=True)
        return r.stdout.strip() == "active"
    except OSError:
        return False


def _backups_for(name: str, backup_dir: Path = BACKUP_DIR) -> list[Path]:
    """This database's own backups only (by filename prefix), newest first -- see DATABASES/backup_once's `label`."""
    label, _, _, _ = DATABASES[name]
    prefix = "paper_trading_" if name == "main" else f"{label}_"
    return [p for p in list_backups(backup_dir) if p.name.startswith(prefix) and not p.name.startswith(("pre_restore", "pre_reset"))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--hourly", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--list", action="store_true")
    g.add_argument("--restore", metavar="FILE|latest")
    ap.add_argument("--db", choices=list(DATABASES), default="main", help="which database --restore targets (--verify with no --db checks every database; --list always shows everything)")
    ap.add_argument("--force", action="store_true", help="restore even if the daemon is running (not recommended)")
    args = ap.parse_args(argv)
    setup_logger("", log_file=str(LOG_DIR / "backup_db.log"))

    if args.list:
        for p in list_backups():
            print(f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}  {p.stat().st_size / 1024:8.1f} KB  {p.name}")
        return 0
    if args.verify:
        failed = False
        for name, (label, _, tables, required) in DATABASES.items():
            backups = _backups_for(name)
            if not backups:
                print(f"VERIFY FAILED ({name}): no backups exist")
                failed = True
                continue
            try:
                counts = verify_backup(backups[0], tables, required)
            except ValueError as exc:
                print(f"VERIFY FAILED ({name}): {exc}")
                failed = True
                try:
                    from services.utils import telegram
                    telegram.send(f"🔴 <b>DB BACKUP UNUSABLE</b> ({name}) — {exc}")
                except Exception:
                    pass
                continue
            print(f"verified {backups[0].name}: {counts}")
        return 1 if failed else 0
    if args.restore:
        if _daemon_running() and not args.force:
            print("hft-dryrun.service is running. Stop it first (systemctl --user stop hft-dryrun) or pass --force.")
            return 1
        _, db_path, tables, required = DATABASES[args.db]
        src = _backups_for(args.db)[0] if args.restore == "latest" and _backups_for(args.db) else Path(args.restore)
        try:
            saved = restore(src, db_path, BACKUP_DIR, tables, required)
        except (ValueError, OSError) as exc:
            print(f"RESTORE REFUSED: {exc}")
            return 1
        print(f"Restored {src.name}. Previous DB saved as {saved.name if saved else 'n/a'}. Start the daemon again.")
        return 0
    kind = "hourly" if args.hourly else "daily"
    for name, (label, db_path, _, _) in DATABASES.items():
        result = backup_once(db_path, BACKUP_DIR, kind, label=None if name == "main" else label)
        if result:
            print(f"Backed up to {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
