#!/usr/bin/env python3
"""
backend/database.py — SQLite Database for Paper Trading, Virtual Orders, & Performance Auditing.

Tracks:
  - Virtual Order Lifecycle (SUBMITTED -> FILLED -> EXITED) without touching live broker capital
  - Active & Historic Positions (Entry, Trailing Stop, TP, Breakeven)
  - Detailed Realized Trades with granular Fee/Tax Breakdown (Brokerage, STT, Stamp Duty, Exch, SEBI, GST, Slippage)
  - Portfolio Equity & Daily Performance Snapshots
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_DB_PATH = Path(__file__).parent / "data" / "paper_trading.db"


class TradingDB:
    def __init__(self, db_path: Optional[Path | str] = None):
        if db_path is None:
            self.db_path = DEFAULT_DB_PATH
        else:
            self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                initial_capital REAL NOT NULL,
                current_capital REAL NOT NULL,
                leverage REAL NOT NULL DEFAULT 4.0,
                risk_pct REAL NOT NULL DEFAULT 5.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                account_id TEXT DEFAULT 'DRYRUN_ACCOUNT',
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,       -- BUY / SELL
                intent TEXT NOT NULL,          -- ENTRY / TAKE_PROFIT / STOP_LOSS / EOD_EXIT
                order_type TEXT NOT NULL,      -- MARKET / LIMIT / SL-M
                quantity INTEGER NOT NULL,
                requested_price REAL NOT NULL,
                fill_price REAL,
                status TEXT NOT NULL,          -- PLACED / FILLED / REJECTED / CANCELLED
                is_dry_run INTEGER NOT NULL DEFAULT 1,
                rejection_reason TEXT,
                tag TEXT,
                created_at TEXT NOT NULL,
                filled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                account_id TEXT DEFAULT 'DRYRUN_ACCOUNT',
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,       -- long / short
                qty INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                current_stop REAL NOT NULL,
                target_price REAL NOT NULL,
                breakeven_price REAL NOT NULL,
                best_price REAL NOT NULL,
                armed_be INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN', -- OPEN / CLOSED
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                exit_price REAL,
                exit_reason TEXT,
                gross_pnl REAL DEFAULT 0.0,
                net_pnl REAL DEFAULT 0.0,
                total_fees REAL DEFAULT 0.0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id TEXT,
                account_id TEXT DEFAULT 'DRYRUN_ACCOUNT',
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                entry_dt TEXT NOT NULL,
                exit_dt TEXT NOT NULL,
                hold_minutes REAL,
                exit_reason TEXT NOT NULL,
                gross_pnl REAL NOT NULL,
                brokerage REAL NOT NULL,
                stt REAL NOT NULL,
                stamp_duty REAL NOT NULL,
                exchange_txn_fee REAL NOT NULL,
                sebi_charges REAL NOT NULL,
                gst REAL NOT NULL,
                slippage REAL NOT NULL,
                total_friction REAL NOT NULL,
                net_pnl REAL NOT NULL,
                capital_after REAL NOT NULL,
                p_up REAL,
                rsi REAL,
                vwap_dist_pct REAL,
                ema_slope_pct REAL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT DEFAULT 'DRYRUN_ACCOUNT',
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                starting_capital REAL NOT NULL,
                current_capital REAL NOT NULL,
                gross_pnl REAL NOT NULL,
                total_fees REAL NOT NULL,
                net_pnl REAL NOT NULL,
                open_positions INTEGER NOT NULL,
                total_trades INTEGER NOT NULL,
                win_trades INTEGER NOT NULL,
                loss_trades INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                max_drawdown_pct REAL NOT NULL DEFAULT 0.0
            );

            CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(exit_dt);
            """)

    # -------------------------------------------------------------------------
    # Account Management
    # -------------------------------------------------------------------------
    def init_account(self, account_id: str = "DRYRUN_ACCOUNT",
                     name: str = "Live Dry Run Scalper",
                     capital: float = 100000.0,
                     leverage: float = 4.0,
                     risk_pct: float = 5.0,
                     force_capital: bool = False) -> None:
        now_str = datetime.now(IST).isoformat()
        with self._get_conn() as conn:
            row = conn.execute("SELECT initial_capital, current_capital FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
            trade_count = conn.execute("SELECT COUNT(*) FROM trades WHERE account_id = ?", (account_id,)).fetchone()[0]
            pos_count = conn.execute("SELECT COUNT(*) FROM positions WHERE account_id = ? AND status = 'OPEN'", (account_id,)).fetchone()[0]

            if row is None:
                conn.execute("""
                INSERT INTO accounts (account_id, name, initial_capital, current_capital, leverage, risk_pct, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (account_id, name, capital, capital, leverage, risk_pct, now_str, now_str))
            else:
                if force_capital or (trade_count == 0 and pos_count == 0):
                    conn.execute("""
                    UPDATE accounts SET initial_capital = ?, current_capital = ?, leverage = ?, risk_pct = ?, updated_at = ?
                    WHERE account_id = ?
                    """, (capital, capital, leverage, risk_pct, now_str, account_id))
                else:
                    conn.execute("""
                    UPDATE accounts SET leverage = ?, risk_pct = ?, updated_at = ?
                    WHERE account_id = ?
                    """, (leverage, risk_pct, now_str, account_id))

    def get_account(self, account_id: str = "DRYRUN_ACCOUNT") -> Optional[dict]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
            return dict(row) if row else None

    def update_capital(self, account_id: str, new_capital: float) -> None:
        now_str = datetime.now(IST).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
            UPDATE accounts SET current_capital = ?, updated_at = ? WHERE account_id = ?
            """, (new_capital, now_str, account_id))

    # -------------------------------------------------------------------------
    # Order Placement & Virtual Execution
    # -------------------------------------------------------------------------
    def place_order(self, order_id: str, symbol: str, direction: str,
                    intent: str, order_type: str, qty: int,
                    requested_price: float, fill_price: Optional[float] = None,
                    status: str = "FILLED", tag: str = "DRY_RUN",
                    account_id: str = "DRYRUN_ACCOUNT") -> dict:
        now_str = datetime.now(IST).isoformat()
        filled_at = now_str if status == "FILLED" else None
        fill_p = fill_price if fill_price is not None else requested_price

        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO orders (
                order_id, client_order_id, account_id, symbol, direction,
                intent, order_type, quantity, requested_price, fill_price,
                status, is_dry_run, rejection_reason, tag, created_at, filled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                fill_price = excluded.fill_price,
                status = excluded.status,
                filled_at = excluded.filled_at
            """, (
                order_id, f"CLI_{order_id}", account_id, symbol, direction.upper(),
                intent.upper(), order_type.upper(), qty, requested_price, fill_p,
                status.upper(), tag, now_str, filled_at
            ))

        return {
            "order_id": order_id, "symbol": symbol, "direction": direction,
            "qty": qty, "fill_price": fill_p, "status": status,
            "intent": intent, "created_at": now_str
        }

    # -------------------------------------------------------------------------
    # Position Tracking
    # -------------------------------------------------------------------------
    def open_position(self, position_id: str, symbol: str, direction: str,
                      qty: int, entry_price: float, current_stop: float,
                      target_price: float, breakeven_price: float,
                      account_id: str = "DRYRUN_ACCOUNT") -> None:
        now_str = datetime.now(IST).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO positions (
                position_id, account_id, symbol, direction, qty, entry_price,
                current_stop, target_price, breakeven_price, best_price, armed_be,
                status, entry_time, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'OPEN', ?, ?)
            ON CONFLICT(position_id) DO UPDATE SET
                current_stop = excluded.current_stop,
                updated_at = excluded.updated_at
            """, (
                position_id, account_id, symbol, direction.lower(), qty,
                entry_price, current_stop, target_price, breakeven_price,
                entry_price, now_str, now_str
            ))

    def update_position_stop(self, position_id: str, current_stop: float,
                             best_price: float, armed_be: bool) -> None:
        now_str = datetime.now(IST).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
            UPDATE positions SET
                current_stop = ?,
                best_price = ?,
                armed_be = ?,
                updated_at = ?
            WHERE position_id = ? AND status = 'OPEN'
            """, (current_stop, best_price, 1 if armed_be else 0, now_str, position_id))

    def get_open_positions(self, account_id: str = "DRYRUN_ACCOUNT") -> List[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE account_id = ? AND status = 'OPEN' ORDER BY entry_time DESC",
                (account_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def close_position(self, position_id: str, exit_price: float,
                       exit_reason: str, gross_pnl: float,
                       net_pnl: float, total_fees: float) -> None:
        now_str = datetime.now(IST).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
            UPDATE positions SET
                status = 'CLOSED',
                exit_time = ?,
                exit_price = ?,
                exit_reason = ?,
                gross_pnl = ?,
                net_pnl = ?,
                total_fees = ?,
                updated_at = ?
            WHERE position_id = ?
            """, (now_str, exit_price, exit_reason, gross_pnl, net_pnl, total_fees, now_str, position_id))

    # -------------------------------------------------------------------------
    # Trade Logging & Fee Breakdowns
    # -------------------------------------------------------------------------
    def record_trade(self, position_id: str, symbol: str, direction: str,
                     qty: int, entry_price: float, exit_price: float,
                     entry_dt: str, exit_dt: str, hold_minutes: float,
                     exit_reason: str, gross_pnl: float, costs_dict: dict,
                     net_pnl: float, capital_after: float,
                     p_up: Optional[float] = None, rsi: Optional[float] = None,
                     vwap_dist_pct: Optional[float] = None,
                     ema_slope_pct: Optional[float] = None,
                     account_id: str = "DRYRUN_ACCOUNT") -> int:
        now_str = datetime.now(IST).isoformat()
        with self._get_conn() as conn:
            cur = conn.execute("""
            INSERT INTO trades (
                position_id, account_id, symbol, direction, qty,
                entry_price, exit_price, entry_dt, exit_dt, hold_minutes,
                exit_reason, gross_pnl, brokerage, stt, stamp_duty,
                exchange_txn_fee, sebi_charges, gst, slippage, total_friction,
                net_pnl, capital_after, p_up, rsi, vwap_dist_pct,
                ema_slope_pct, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position_id, account_id, symbol, direction.lower(), qty,
                entry_price, exit_price, entry_dt, exit_dt, hold_minutes,
                exit_reason, gross_pnl,
                costs_dict.get("brokerage", 0.0),
                costs_dict.get("stt", 0.0),
                costs_dict.get("stamp_duty", 0.0),
                costs_dict.get("exchange_txn", 0.0),
                costs_dict.get("sebi", 0.0),
                costs_dict.get("gst", 0.0),
                costs_dict.get("slippage", 0.0),
                costs_dict.get("total", 0.0),
                net_pnl, capital_after,
                p_up, rsi, vwap_dist_pct, ema_slope_pct, now_str
            ))
            trade_id = cur.lastrowid

            # Also update account capital
            conn.execute("""
            UPDATE accounts SET current_capital = ?, updated_at = ? WHERE account_id = ?
            """, (capital_after, now_str, account_id))

            return trade_id

    # -------------------------------------------------------------------------
    # Snapshots & Dashboard Reporting
    # -------------------------------------------------------------------------
    def record_snapshot(self, account_id: str = "DRYRUN_ACCOUNT") -> None:
        now = datetime.now(IST)
        now_str = now.isoformat()
        today_str = now.strftime("%Y-%m-%d")

        acct = self.get_account(account_id)
        if not acct:
            return

        with self._get_conn() as conn:
            trades = conn.execute("SELECT * FROM trades WHERE account_id = ?", (account_id,)).fetchall()
            open_pos = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE account_id = ? AND status = 'OPEN'", (account_id,)).fetchone()["cnt"]

            total_trades = len(trades)
            gross_pnl = sum(t["gross_pnl"] for t in trades)
            total_fees = sum(t["total_friction"] for t in trades)
            net_pnl = sum(t["net_pnl"] for t in trades)

            wins = [t for t in trades if t["net_pnl"] > 0]
            win_count = len(wins)
            loss_count = total_trades - win_count
            win_rate = (win_count / total_trades * 100.0) if total_trades > 0 else 0.0

            conn.execute("""
            INSERT INTO portfolio_snapshots (
                account_id, timestamp, date, starting_capital, current_capital,
                gross_pnl, total_fees, net_pnl, open_positions, total_trades,
                win_trades, loss_trades, win_rate, max_drawdown_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0)
            """, (
                account_id, now_str, today_str, acct["initial_capital"],
                acct["current_capital"], gross_pnl, total_fees, net_pnl,
                open_pos, total_trades, win_count, loss_count, win_rate
            ))

    def get_snapshots(self, account_id: str = "DRYRUN_ACCOUNT", limit: int = 2000) -> List[dict]:
        """Portfolio equity curve points, oldest first -- one row per trade close (see record_snapshot)."""
        with self._get_conn() as conn:
            rows = conn.execute("""
            SELECT * FROM portfolio_snapshots WHERE account_id = ? ORDER BY id ASC LIMIT ?
            """, (account_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_trades(self, limit: int = 50, account_id: str = "DRYRUN_ACCOUNT") -> List[dict]:
        with self._get_conn() as conn:
            rows = conn.execute("""
            SELECT * FROM trades WHERE account_id = ? ORDER BY trade_id DESC LIMIT ?
            """, (account_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_orders(self, limit: int = 50, account_id: str = "DRYRUN_ACCOUNT") -> List[dict]:
        with self._get_conn() as conn:
            rows = conn.execute("""
            SELECT * FROM orders WHERE account_id = ? ORDER BY created_at DESC LIMIT ?
            """, (account_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def print_dashboard(self, account_id: str = "DRYRUN_ACCOUNT") -> None:
        acct = self.get_account(account_id)
        if not acct:
            self.init_account(account_id=account_id)
            acct = self.get_account(account_id)

        with self._get_conn() as conn:
            trades = conn.execute("SELECT * FROM trades WHERE account_id = ? ORDER BY trade_id ASC", (account_id,)).fetchall()
            positions = conn.execute("SELECT * FROM positions WHERE account_id = ? AND status = 'OPEN'", (account_id,)).fetchall()
            orders = conn.execute("SELECT * FROM orders WHERE account_id = ? ORDER BY created_at DESC LIMIT 10", (account_id,)).fetchall()

        # Terminal formatting codes
        R = "\033[0m"; BOLD = "\033[1m"
        GR = "\033[92m"; RED = "\033[91m"; YL = "\033[93m"
        CY = "\033[96m"; WH = "\033[97m"; MG = "\033[95m"; GY = "\033[90m"

        init_cap = acct["initial_capital"]
        curr_cap = acct["current_capital"]
        total_pnl = curr_cap - init_cap
        pct_return = (total_pnl / init_cap * 100) if init_cap > 0 else 0.0

        total_trades = len(trades)
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        wr = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0

        gross_sum = sum(t["gross_pnl"] for t in trades)
        fees_sum = sum(t["total_friction"] for t in trades)
        brok_sum = sum(t["brokerage"] for t in trades)
        stt_sum = sum(t["stt"] for t in trades)
        taxes_sum = sum(t["stamp_duty"] + t["exchange_txn_fee"] + t["sebi_charges"] + t["gst"] for t in trades)
        slip_sum = sum(t["slippage"] for t in trades)

        pnl_col = GR if total_pnl >= 0 else RED

        print(f"\n{BOLD}{CY}{'='*80}{R}")
        print(f"{BOLD}{WH}  PAPER TRADING & VIRTUAL EXECUTION DATABASE DASHBOARD{R}")
        print(f"  {GY}DB Location:{R} {self.db_path}")
        print(f"{BOLD}{CY}{'='*80}{R}")

        print(f"\n{BOLD}{WH} ACCOUNT OVERVIEW:{R}")
        print(f"  Initial Capital:     {WH}₹{init_cap:,.2f}{R}")
        print(f"  Current Balance:     {BOLD}{WH}₹{curr_cap:,.2f}{R}")
        print(f"  Net Realized Profit: {pnl_col}₹{total_pnl:+,.2f} ({pct_return:+.2f}%){R}")
        print(f"  Leverage Setting:    {MG}{acct['leverage']}x{R}  |  Risk Per Trade: {MG}{acct['risk_pct']}%{R}")

        print(f"\n{BOLD}{WH} PERFORMANCE & STATS:{R}")
        print(f"  Total Trades:        {WH}{total_trades}{R}  (Wins: {GR}{len(wins)}{R}, Losses: {RED}{len(losses)}{R})")
        print(f"  Win Rate:            {BOLD}{GR if wr >= 50 else YL}{wr:.1f}%{R}")
        print(f"  Gross Trading PnL:   {GR if gross_sum >= 0 else RED}₹{gross_sum:+,.2f}{R}")

        print(f"\n{BOLD}{WH} DETAILED FRICTION & STATUTORY AUDIT:{R}")
        print(f"  Total Brokerage:     {RED}₹{brok_sum:,.2f}{R} (Upstox ₹20/order max)")
        print(f"  Securities STT:      {RED}₹{stt_sum:,.2f}{R} (0.025% sell-side)")
        print(f"  Statutory Taxes/GST: {RED}₹{taxes_sum:,.2f}{R} (Stamp duty, SEBI, Exch, GST)")
        print(f"  Estimated Slippage:  {RED}₹{slip_sum:,.2f}{R}")
        print(f"  --------------------------------------------------")
        print(f"  Total All Friction:  {BOLD}{RED}₹{fees_sum:,.2f}{R}")

        if positions:
            print(f"\n{BOLD}{WH} OPEN ACTIVE POSITIONS ({len(positions)}):{R}")
            for p in positions:
                col = GR if p["direction"] == "long" else RED
                print(f"  * {col}{p['direction'].upper():5s}{R} {WH}{p['symbol']:10s}{R} "
                      f"Qty: {p['qty']} @ ₹{p['entry_price']:.2f} | "
                      f"SL: ₹{p['current_stop']:.2f} | TP: ₹{p['target_price']:.2f} | "
                      f"Armed BE: {'YES' if p['armed_be'] else 'NO'}")
        else:
            print(f"\n{GY}  No open active positions.{R}")

        if trades:
            print(f"\n{BOLD}{WH} LAST 5 REALIZED TRADES:{R}")
            for t in trades[-5:]:
                col = GR if t["net_pnl"] >= 0 else RED
                print(f"  [{str(t['exit_dt'])[:19]}] {t['symbol']:10s} {t['direction'].upper():5s} "
                      f"Qty:{t['qty']:3d} | In:₹{t['entry_price']:.2f} Out:₹{t['exit_price']:.2f} | "
                      f"Exit: {t['exit_reason']:12s} | Net: {col}₹{t['net_pnl']:+8.2f}{R} | Fee: ₹{t['total_friction']:.2f}")

        if orders:
            print(f"\n{BOLD}{WH} LAST 5 VIRTUAL ORDERS:{R}")
            for o in orders[:5]:
                st_col = GR if o["status"] == "FILLED" else YL
                print(f"  {o['order_id']} | {o['created_at'][:19]} | {o['direction']:4s} {o['symbol']:10s} | "
                      f"{o['quantity']} @ ₹{o['fill_price']:.2f} | {st_col}{o['status']}{R} | {o['intent']}")

        print(f"{BOLD}{CY}{'='*80}{R}\n")


# -----------------------------------------------------------------------------
# CLI Entrypoint for inspecting Database
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Query Paper Trading & Virtual Execution Database")
    parser.add_argument("--db", default=None, help="Path to SQLite database file")
    parser.add_argument("--account", default="DRYRUN_ACCOUNT", help="Account ID")
    parser.add_argument("--reset", action="store_true", help="Reset and clear database")
    parser.add_argument("--capital", type=float, default=100000.0, help="Initial capital if resetting")
    parser.add_argument("--sql", default=None, help="Execute custom SQL query against SQLite DB")
    args = parser.parse_args()

    db = TradingDB(args.db)

    if args.sql:
        with db._get_conn() as conn:
            cur = conn.execute(args.sql)
            rows = cur.fetchall()
            if not rows:
                print("Query returned 0 rows.")
            else:
                col_names = [d[0] for d in cur.description] if cur.description else []
                print(" | ".join(col_names))
                print("-" * 60)
                for r in rows:
                    print(" | ".join(str(val) for val in r))
        return

    if args.reset:
        if db.db_path.exists():
            db.db_path.unlink()
        db._init_db()
        db.init_account(args.account, capital=args.capital, force_capital=True)
        print(f"Reset database at {db.db_path} with initial capital ₹{args.capital:,.2f}")

    db.print_dashboard(args.account)


if __name__ == "__main__":
    main()

