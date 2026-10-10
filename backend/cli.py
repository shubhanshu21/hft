#!/usr/bin/env python3
"""
cli.py — Master CLI for the paper-trading framework (MCX commodity, NSE currency; crypto runs on its own engine).

Strategies live in markets/<market>/strategies/<timeframe>/<name>/ (markets/README.md lists them). The 5-minute scalpers
(commodity, currency, equity) were deleted on 2026-10-10 after losing money once costs and real fills were counted -- see git history.

Usage Examples:
    # 1. Backtest a strategy (each has its own backtest next to it)
    python3 -m markets.currency.strategies.tf_5min.parity.backtest

    # 2. Live paper-trading daemon (systemd: hft-dryrun.service)
    python3 cli.py dryrun --interval 30

    # 3. Report / reset the paper-trading database
    python3 cli.py report
    python3 cli.py reset-db --capital 100000

    # 4. New strategy from a template
    python3 cli.py new-strategy --market commodity --timeframe 15min --name my_idea
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Auto-bootstrap virtual environment if running outside .venv
_venv_py = Path(__file__).parent / ".venv" / "bin" / "python3"
if _venv_py.exists() and sys.executable != str(_venv_py):
    os.execv(str(_venv_py), [str(_venv_py)] + sys.argv)

import argparse
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


sys.path.insert(0, str(Path(__file__).parent))

from engine.database import TradingDB


def cmd_dryrun(args):
    # Delegate to live_dryrun runner
    import subprocess
    cmd = [
        str(_venv_py if _venv_py.exists() else "python3"),
        "-m", "engine.live_dryrun",
        "--capital", str(args.capital),
        "--risk-pct", str(args.risk_pct),
        "--leverage", str(args.leverage),
        "--interval", str(args.interval),
    ]
    if args.symbols:
        cmd.extend(["--symbols"] + args.symbols)
    if args.direction:
        cmd.extend(["--direction", args.direction])

    subprocess.run(cmd, cwd=Path(__file__).parent)


def cmd_report(args):
    db = TradingDB(args.db)
    db.print_dashboard(args.account)


def cmd_new_strategy(args):
    from core import scaffold
    try:
        path = scaffold.create(args.market, args.name, timeframe=args.timeframe)
    except (ValueError, FileExistsError) as exc:
        sys.exit(f"error: {exc}")
    print(f"Created {path.relative_to(Path(__file__).parent)}\n"
          f"Fill in entry() / manage() / costs(), then switch it on with {args.market.upper()}_STRATEGIES=scalping,{args.name} in .env.\n"
          f"Guide: docs/ADDING_A_STRATEGY.md")


def cmd_arm_live_trading(args):
    from engine.safety_gate import arm, KILL_SWITCH_ENGAGED
    ok = arm(args.component, args.confirm)
    if ok and KILL_SWITCH_ENGAGED:
        print("\033[93mNote: safety_gate.KILL_SWITCH_ENGAGED is still True in source -- "
              "live trading remains blocked until that constant is hand-edited to False "
              "and redeployed. That is intentional (see safety_gate.py).\033[0m")


def cmd_disarm_live_trading(args):
    from engine.safety_gate import disarm
    disarm(args.component)


def cmd_reset_db(args):
    db = TradingDB(args.db)
    if db.db_path.exists():
        from engine.backup_db import backup_once
        saved = backup_once(db.db_path, kind="pre_reset")        # a reset is irreversible; keep the old history restorable
        if saved:
            print(f"Previous DB saved to {saved} (restore with: python3 cli.py restore-db {saved})")
        db.db_path.unlink()
    db._init_db()
    db.init_account(args.account, capital=args.capital, leverage=args.leverage, risk_pct=args.risk_pct)
    print(f"\033[92mPaper trading DB reset at {db.db_path} with initial capital ₹{args.capital:,.2f}\033[0m")


def cmd_status(args):
    from engine.status import main as status_main
    status_main(args.account)


def cmd_restore_db(args):
    from engine.backup_db import main as backup_main
    raise SystemExit(backup_main(["--restore", args.source] + (["--force"] if args.force else [])))


def main():
    parser = argparse.ArgumentParser(description="Multi-Asset Quantitative Trading Framework Master CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Dryrun Subcommand -- defaults come from backend/.env (DRYRUN_* / TRADING_*
    # vars) so `python3 cli.py dryrun` needs no flags at all; passing a flag
    # still overrides the .env value for that one run.
    p_dr = subparsers.add_parser("dryrun", help="Run live paper-trading dryrun (MCX commodities + NSE currency)")
    p_dr.add_argument("--symbols", nargs="+", default=_env("DRYRUN_SYMBOLS", "").split() or None,
                       help="Symbols to watch")
    p_dr.add_argument("--capital", type=float, default=float(_env("TRADING_CAPITAL", "100000.0")),
                       help="Paper capital in INR")
    p_dr.add_argument("--risk-pct", type=float, default=float(_env("DRYRUN_RISK_PCT", "5.0")),
                       help="Risk %% per trade")
    p_dr.add_argument("--leverage", type=float,
                       default=float(_env("DRYRUN_LEVERAGE", "") or _env("INTRADAY_LEVERAGE", "4.0")),
                       help="Margin leverage")
    p_dr.add_argument("--interval", type=int, default=int(_env("DRYRUN_INTERVAL", "30")),
                       help="Scan interval seconds")
    p_dr.add_argument("--direction", choices=["both", "long", "short"],
                       default=_env("TRADING_DIRECTION", "both"))
    p_dr.set_defaults(func=cmd_dryrun)

    # Report Subcommand
    p_rep = subparsers.add_parser("report", help="View SQLite PnL & trade performance dashboard")
    p_rep.add_argument("--db", default=None, help="SQLite DB path")
    p_rep.add_argument("--account", default="DRYRUN_ACCOUNT", help="Account ID")
    p_rep.set_defaults(func=cmd_report)

    # Reset DB Subcommand
    p_rst = subparsers.add_parser("reset-db", help="Reset paper trading database")
    p_rst.add_argument("--db", default=None, help="SQLite DB path")
    p_rst.add_argument("--account", default="DRYRUN_ACCOUNT", help="Account ID")
    p_rst.add_argument("--capital", type=float, default=float(_env("TRADING_CAPITAL", "100000.0")), help="Initial capital in INR")
    p_rst.add_argument("--risk-pct", type=float, default=float(_env("DRYRUN_RISK_PCT", "5.0")), help="Risk %% per trade")
    p_rst.add_argument("--leverage", type=float, default=float(_env("INTRADAY_LEVERAGE", "4.0")), help="Margin leverage")
    p_rst.set_defaults(func=cmd_reset_db)

    p_st = subparsers.add_parser("status", help="One-screen health check: daemon, heartbeat, P&L, open positions, token, last backup")
    p_st.add_argument("--account", default="DRYRUN_ACCOUNT", help="Account ID")
    p_st.set_defaults(func=cmd_status)

    p_rs = subparsers.add_parser("restore-db", help="Restore the paper-trading DB from a backup (daemon must be stopped)")
    p_rs.add_argument("source", nargs="?", default="latest", help="backup file, or 'latest' (default); see: python3 -m engine.backup_db --list")
    p_rs.add_argument("--force", action="store_true", help="restore even if the daemon is running")
    p_rs.set_defaults(func=cmd_restore_db)

    # Layered live-trading kill switch (see safety_gate.py) -- arming here is
    # only one of three independent gates; KILL_SWITCH_ENGAGED in
    # safety_gate.py must also be hand-edited to False, and ALLOW_LIVE_TRADING
    # must be set in .env. This command exists to be the deliberate,
    # hard-to-fat-finger human-confirmation step, not a switch on its own.
    p_arm = subparsers.add_parser("arm-live-trading", help="Arm one of the three live-trading gates (see safety_gate.py)")
    p_arm.add_argument("--component", required=True, choices=["upstox", "binance", "ALL"])
    p_arm.add_argument("--confirm", required=True,
                        help='Must exactly equal "I UNDERSTAND THIS PLACES REAL ORDERS WITH REAL MONEY"')
    p_arm.set_defaults(func=cmd_arm_live_trading)

    p_disarm = subparsers.add_parser("disarm-live-trading", help="Remove the armed-state gate (always safe)")
    p_disarm.add_argument("--component", default=None, choices=["upstox", "binance", "ALL", None])
    p_disarm.set_defaults(func=cmd_disarm_live_trading)

    p_new = subparsers.add_parser("new-strategy", help="Create a new strategy file from a template (see docs/ADDING_A_STRATEGY.md)")
    p_new.add_argument("--market", required=True, choices=["commodity", "currency", "equity"])
    p_new.add_argument("--name", required=True, help="strategy name, e.g. swing (lowercase, digits, underscores)")
    p_new.add_argument("--timeframe", default="5min", choices=["5min", "15min", "1hour", "daily"],
                       help="the bars its signal uses; decides the folder markets/<market>/strategies/tf_<timeframe>/ (default 5min)")
    p_new.set_defaults(func=cmd_new_strategy)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
