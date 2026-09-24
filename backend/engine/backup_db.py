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


def backup_once(db_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR, kind: str = "daily", now: datetime | None = None) -> Path | None:
    """kind: 'daily' -> paper_trading_<date>.db, 'hourly' -> hourly_<date>_<HHMM>.db, or any other tag (e.g. 'pre_reset') -> <tag>_<date>_<HHMMSS>.db (never pruned)."""
    if not db_path.exists():
        log.warning("No DB found at %s -- nothing to back up.", db_path)
        return None
    now = now or datetime.now()
    backup_dir.mkdir(parents=True, exist_ok=True)
    if kind == "daily":
        dest = backup_dir / f"paper_trading_{now.date().isoformat()}.db"
    elif kind == "hourly":
        dest = backup_dir / f"hourly_{now.date().isoformat()}_{now.strftime('%H%M')}.db"
    else:
        dest = backup_dir / f"{kind}_{now.strftime('%Y%m%d_%H%M%S')}.db"
    _snapshot(db_path, dest)
    log.info("Backed up %s -> %s (%.1f KB)", db_path, dest, dest.stat().st_size / 1024)
    if kind == "daily":
        _prune(backup_dir, "paper_trading_*.db", KEEP_DAILY)
    elif kind == "hourly":
        _prune(backup_dir, "hourly_*.db", KEEP_HOURLY)
    return dest


def list_backups(backup_dir: Path = BACKUP_DIR) -> list[Path]:
    """Every snapshot, newest first (by modification time)."""
    files = [p for p in backup_dir.glob("*.db")] if backup_dir.exists() else []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def verify_backup(path: Path) -> dict:
    """Open a snapshot read-only, run SQLite's integrity check and count rows. Raises ValueError if it is unusable."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"{path.name}: integrity_check said {result!r}")
        counts = {}
        for t in TABLES:
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[t] = None
        if counts.get("accounts") is None:
            raise ValueError(f"{path.name}: no accounts table -- not a paper-trading database")
        return counts
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"{path.name}: not a usable SQLite database ({exc})") from exc
    finally:
        con.close()


def restore(src: Path, db_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR) -> Path:
    """Replace the live DB with `src`. The source is verified first; the current DB is saved as pre_restore_* so this is reversible."""
    verify_backup(src)                                          # refuse to overwrite a good DB with a bad backup
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--hourly", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--list", action="store_true")
    g.add_argument("--restore", metavar="FILE|latest")
    ap.add_argument("--force", action="store_true", help="restore even if the daemon is running (not recommended)")
    args = ap.parse_args(argv)
    setup_logger("", log_file=str(LOG_DIR / "backup_db.log"))

    if args.list:
        for p in list_backups():
            print(f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}  {p.stat().st_size / 1024:8.1f} KB  {p.name}")
        return 0
    if args.verify:
        backups = [p for p in list_backups() if not p.name.startswith(("pre_restore", "pre_reset"))]
        if not backups:
            print("VERIFY FAILED: no backups exist")
            return 1
        try:
            counts = verify_backup(backups[0])
        except ValueError as exc:
            print(f"VERIFY FAILED: {exc}")
            try:
                from services.utils import telegram
                telegram.send(f"🔴 <b>DB BACKUP UNUSABLE</b> — {exc}")
            except Exception:
                pass
            return 1
        print(f"verified {backups[0].name}: {counts}")
        return 0
    if args.restore:
        if _daemon_running() and not args.force:
            print("hft-dryrun.service is running. Stop it first (systemctl --user stop hft-dryrun) or pass --force.")
            return 1
        src = list_backups()[0] if args.restore == "latest" and list_backups() else Path(args.restore)
        try:
            saved = restore(src)
        except (ValueError, OSError) as exc:
            print(f"RESTORE REFUSED: {exc}")
            return 1
        print(f"Restored {src.name}. Previous DB saved as {saved.name if saved else 'n/a'}. Start the daemon again.")
        return 0
    result = backup_once(kind="hourly" if args.hourly else "daily")
    if result:
        print(f"Backed up to {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
