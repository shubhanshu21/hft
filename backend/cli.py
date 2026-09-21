#!/usr/bin/env python3
"""
cli.py — Master Unified CLI for Multi-Asset Quantitative Trading Framework.

Supports:
  - Futures (MCX Commodity LightGBM Scalper)
  - NSE Currency Derivatives (USDINR/EURINR/GBPINR/JPYINR)

Equity intraday scalping was removed 2026-09-18: even with the full NIFTY50
universe (50 symbols, real 2022-2026 Upstox data) and disciplined
train/test-split validation, no threshold/meta-labeling/symbol-selection
combination survived out-of-sample -- every apparent edge was overfitting
to the selection window. See git history for the full sweep.

Usage Examples:
    # 1. Backtest MCX Commodities
    python3 cli.py backtest --asset futures --symbols CRUDEOILM NATGASMINI --from 2026-01-01 --to 2026-09-07

    # 2. Backtest NSE Currency Derivatives (standalone script, not wired into this CLI)
    python3 backtest_currency.py --symbols USDINR EURINR GBPINR

    # 3. Live Paper-Trading Dryrun (MCX Commodities + NSE Currency)
    python3 cli.py dryrun --capital 100000 --risk-pct 5.0 --interval 30

    # 4. View SQLite Trade Dashboard & PnL Report
    python3 cli.py report

    # 5. Reset Paper-Trading SQLite Database
    python3 cli.py reset-db --capital 100000
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

from database import TradingDB


def cmd_backtest(args):
    from backtest_commodity import run_commodity_backtest
    run_commodity_backtest(
        symbols=args.symbols,
        capital=args.capital,
        risk_pct=args.risk_pct,
        leverage=args.leverage,
        from_date=args.from_date,
        to_date=args.to_date,
        us_session_only=not args.full_session,
        no_ml_filter=args.no_ml_filter,
    )


def cmd_dryrun(args):
    # Delegate to live_dryrun runner
    import subprocess
    cmd = [
        str(_venv_py if _venv_py.exists() else "python3"),
        str(Path(__file__).parent / "live_dryrun.py"),
        "--capital", str(args.capital),
        "--risk-pct", str(args.risk_pct),
        "--leverage", str(args.leverage),
        "--interval", str(args.interval),
    ]
    if args.symbols:
        cmd.extend(["--symbols"] + args.symbols)
    if args.direction:
        cmd.extend(["--direction", args.direction])

    subprocess.run(cmd)


def cmd_report(args):
    db = TradingDB(args.db)
    db.print_dashboard(args.account)


def cmd_arm_live_trading(args):
    from safety_gate import arm, KILL_SWITCH_ENGAGED
    ok = arm(args.component, args.confirm)
    if ok and KILL_SWITCH_ENGAGED:
        print("\033[93mNote: safety_gate.KILL_SWITCH_ENGAGED is still True in source -- "
              "live trading remains blocked until that constant is hand-edited to False "
              "and redeployed. That is intentional (see safety_gate.py).\033[0m")


def cmd_disarm_live_trading(args):
    from safety_gate import disarm
    disarm(args.component)


def cmd_reset_db(args):
    db = TradingDB(args.db)
    if db.db_path.exists():
        db.db_path.unlink()
    db._init_db()
    db.init_account(args.account, capital=args.capital, leverage=args.leverage, risk_pct=args.risk_pct)
    print(f"\033[92mPaper trading DB reset at {db.db_path} with initial capital ₹{args.capital:,.2f}\033[0m")


def main():
    parser = argparse.ArgumentParser(description="Multi-Asset Quantitative Trading Framework Master CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Backtest Subcommand -- defaults come from backend/.env (BACKTEST_* /
    # TRADING_* vars) so `python3 cli.py backtest` needs no flags at all;
    # passing a flag still overrides the .env value for that one run.
    p_bt = subparsers.add_parser("backtest", help="Run walk-forward backtest on historical MCX commodity data")
    p_bt.add_argument("--symbols", nargs="+", default=_env("BACKTEST_SYMBOLS", "").split() or None,
                       help="Symbols to backtest")
    p_bt.add_argument("--capital", type=float, default=float(_env("TRADING_CAPITAL", "100000.0")),
                       help="Initial capital in INR")
    p_bt.add_argument("--risk-pct", type=float, default=float(_env("BACKTEST_RISK_PCT", "5.0")),
                       help="Risk %% per trade")
    p_bt.add_argument("--leverage", type=float,
                       default=float(_env("BACKTEST_LEVERAGE", "") or _env("INTRADAY_LEVERAGE", "4.0")),
                       help="Margin leverage")
    p_bt.add_argument("--from", "--from-date", dest="from_date", default=_env("BACKTEST_FROM", "") or None,
                       help="Start date YYYY-MM-DD")
    p_bt.add_argument("--to", "--to-date", dest="to_date", default=_env("BACKTEST_TO", "") or None,
                       help="End date YYYY-MM-DD")
    p_bt.add_argument("--full-session", action="store_true",
                       default=_env("BACKTEST_FULL_SESSION", "false").lower() in ("1", "true", "yes"),
                       help="Include full session bars (or set BACKTEST_FULL_SESSION=true in .env)")
    p_bt.add_argument("--no-ml-filter", dest="no_ml_filter", action="store_true",
                       default=_env("BACKTEST_USE_ML_FILTER", "true").lower() not in ("1", "true", "yes"),
                       help="Commodity only -- drop the ML p_up condition, keep every other rule-based filter (or set BACKTEST_USE_ML_FILTER=false in .env)")
    p_bt.add_argument("--use-ml-filter", dest="no_ml_filter", action="store_false",
                       help="Force the ML p_up condition back on, overriding BACKTEST_USE_ML_FILTER=false in .env")
    p_bt.set_defaults(func=cmd_backtest)

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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
