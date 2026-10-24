"""Reset ONE paper-trading account back to its starting money (the paper page's "Reset account" button, 2026-10-24).

A reset deletes that account's trade history, so it is deliberately hard to do by accident:
  * the caller must pass confirm == the account id (the page makes the user type it);
  * the whole database is backed up first (var/db/backups/pre_reset_<account>_<time>.db) -- a reset can always be undone with
    `python3 -m engine.backup_db --restore <that file>`;
  * only rows of that account are touched (trades, positions, orders, daily snapshots); the other account is left alone;
  * it holds the same per-account lock the paper engine holds while it runs (account_lock), so a reset never lands in the middle of
    an engine run that would then write the old state back.

After a reset: crypto starts again from its starting money at the next 5-minute check; MCX buys the current month's picks at its next
run (the engine treats "nothing open that it opened" as a reset, see engine/mcx_trend_carry.py)."""
from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.paths import DB_DIR

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = DB_DIR / "paper_trading.db"
# account id -> the engine's own state file (kept next to the DB)
ACCOUNTS = {"CRYPTO_DIP_REVERSAL": "crypto_dip_reversal_state.json", "MCX_TREND_CARRY": "mcx_trend_carry_state.json",
            "RESULTS_GAP": "results_gap_state.json"}
TABLES = ("trades", "positions", "orders", "portfolio_snapshots")


class ResetError(ValueError):
    """The reset was refused (unknown account, wrong confirmation, or busy); nothing was changed."""


@contextmanager
def account_lock(account_id: str, timeout_s: float = 30.0, lock_dir: Path | None = None):
    """Exclusive lock for one paper account: the engine holds it for a whole run, a reset holds it while it rewrites the account."""
    d = lock_dir or DB_DIR
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f".{account_id}.run.lock", "w") as fh:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ResetError(f"{account_id} is busy (its engine is running); try again in a minute")
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _fresh_state(account_id: str, capital: float, now: str) -> dict:
    if account_id == "CRYPTO_DIP_REVERSAL":                     # same keys as engine/crypto_dip_reversal.load_state()'s default
        return {"trading_capital": capital, "open_positions": {}, "completed_trades_count": 0, "winning_trades_count": 0,
                "total_fees_paid": 0.0, "created_at": now, "updated_at": now, "reset_at": now}
    return {"reset_at": now}                                    # MCX: no rebalanced_for / opened -> it rebalances at the next run


def reset_account(account_id: str, confirm: str, db_path: Path | None = None, state_dir: Path | None = None,
                  backup_dir: Path | None = None) -> dict:
    """Back up the DB, then wipe one account's history and set it back to its starting money. Returns what was done."""
    if account_id not in ACCOUNTS:
        raise ResetError(f"Unknown paper account: {account_id}")
    if (confirm or "").strip() != account_id:
        raise ResetError(f"To reset, type the account name exactly: {account_id}")
    db_path = Path(db_path or DB_PATH)
    state_dir = Path(state_dir or db_path.parent)
    if not db_path.exists():
        raise ResetError("There is no paper-trading database yet")
    state_file = state_dir / ACCOUNTS[account_id]
    with account_lock(account_id, lock_dir=state_dir):
        try:
            st = json.loads(state_file.read_text()) if state_file.exists() else {}
        except ValueError:
            st = {}
        real = [c for c, p in (st.get("open_positions") or {}).items() if isinstance(p, dict) and p.get("real")]
        if real:                                                # a real Delta demo order would be orphaned on the exchange
            raise ResetError(f"Close the real Delta demo positions first: {', '.join(real)}")
        from engine.backup_db import BACKUP_DIR, backup_once
        saved = backup_once(db_path, backup_dir or BACKUP_DIR, kind=f"pre_reset_{account_id.lower()}")
        if not saved:
            raise ResetError("Could not back up the database, so nothing was reset")
        now = datetime.now(IST).isoformat()
        con = sqlite3.connect(db_path, timeout=30)
        try:
            with con:
                row = con.execute("SELECT initial_capital FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
                if row is None:
                    raise ResetError(f"{account_id} has no account row yet; nothing to reset")
                capital = float(row[0])
                deleted = {t: con.execute(f"DELETE FROM {t} WHERE account_id = ?", (account_id,)).rowcount for t in TABLES}
                con.execute("UPDATE accounts SET current_capital = initial_capital, created_at = ?, updated_at = ? WHERE account_id = ?",
                            (now, now, account_id))
        finally:
            con.close()
        state_file.write_text(json.dumps(_fresh_state(account_id, capital, now), indent=2))
    return {"account": account_id, "capital": capital, "deleted": deleted, "backup": str(saved), "reset_at": now}
