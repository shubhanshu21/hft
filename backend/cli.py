#!/usr/bin/env python3
"""
cli.py — Master Unified CLI for Multi-Asset Quantitative Trading Framework.

Supports:
  - Equities (NSE Intraday Scalper)
  - Futures (MCX Commodity LightGBM Scalper)
  - Options (Directional Buyer & Credit Spreads with Greeks)

Usage Examples:
    # 1. Backtest MCX Commodities
    python3 cli.py backtest --asset futures --symbols CRUDEOILM NATGASMINI --from 2026-01-01 --to 2026-09-07

    # 2. Backtest NSE Equities
    python3 cli.py backtest --asset equity --symbols RELIANCE HDFCBANK TCS

    # 3. Live Paper-Trading Dryrun (Commodities or Equities)
    python3 cli.py dryrun --asset futures --capital 100000 --risk-pct 5.0 --interval 30

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
from datetime import datetime
import pandas as pd
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


sys.path.insert(0, str(Path(__file__).parent))

from framework import (
    AssetClass, FrameworkConfig, GLOBAL_CONFIG, TradingDB
)
from strategies import (
    NSEIntradayScalper, MCXCommodityScalper, DirectionalOptionBuyer
)
from engine import MultiAssetBacktester, MultiAssetLiveRunner
from broker.upstox_broker import UpstoxBroker
from broker.instruments import ensure_master, get_instrument_key
from live_dryrun import _load_token


def cmd_backtest(args):
    asset_str = args.asset.lower()
    symbols = args.symbols

    if asset_str in ("futures", "commodity", "commodities"):
        from backtest_commodity import run_commodity_backtest
        res = run_commodity_backtest(
            symbols=symbols,
            capital=args.capital,
            risk_pct=args.risk_pct,
            leverage=args.leverage,
            from_date=args.from_date,
            to_date=args.to_date,
            us_session_only=not args.full_session,
        )
    elif asset_str in ("equity", "equities", "cash"):
        from backtest_scalper import run_scalper_backtest
        res = run_scalper_backtest(
            symbols=symbols,
            capital=args.capital,
            risk_pct=args.risk_pct,
            leverage=args.leverage,
            top_n=args.top_n or 15,
            from_date=args.from_date,
            to_date=args.to_date,
        )
    elif asset_str in ("options", "option"):
        from data.local_5min_archive import _load, available_symbols
        target_symbols = symbols or ["NIFTY 50", "NIFTY BANK"]
        data_by_symbol = {}
        for sym in target_symbols:
            df = _load(sym)
            if df is not None and not df.empty:
                data_by_symbol[sym] = df

        if not data_by_symbol:
            print(f"\033[91mNo candle data found for options symbols: {target_symbols}\033[0m")
            return

        strat = DirectionalOptionBuyer(symbols=list(data_by_symbol.keys()))
        engine = MultiAssetBacktester(
            strategy=strat,
            initial_capital=args.capital,
            from_date=args.from_date,
            to_date=args.to_date,
        )
        res = engine.run(data_by_symbol)

        print(f"\n\033[1m\033[96m{'='*85}\033[0m")
        print(f"\033[1m\033[97m  ⚡ DIRECTIONAL OPTIONS BUYING — WALK-FORWARD BACKTEST RESULTS\033[0m")
        print(f"  \033[90mPeriod:\033[0m {args.from_date or 'All'} to {args.to_date or 'All'} | \033[90mCapital:\033[0m ₹{args.capital:,.0f} | \033[90mSymbols:\033[0m {', '.join(data_by_symbol.keys())}")
        print(f"\033[1m\033[96m{'='*85}\033[0m")
        pnl_color = "\033[92m" if res["total_net_pnl"] > 0 else "\033[91m"
        print(f"  \033[90mTotal Executed Trades:\033[0m {res['total_trades']} ({res['wins']}W / {res['losses']}L)")
        print(f"  \033[90mOverall Win Rate:\033[0m      {res['win_rate_pct']:.1f}%")
        print(f"  \033[90mProfit Factor:\033[0m         {res['profit_factor']:.2f}")
        print(f"  \033[90mMax Drawdown:\033[0m          {res['max_drawdown_pct']:.2f}%")
        print(f"  \033[90mNet Realized PnL:\033[0m      {pnl_color}₹{res['total_net_pnl']:+,.2f} ({res['roi_pct']:+.2f}%)\033[0m")
        print(f"\033[1m\033[96m{'='*85}\033[0m\n")
    else:
        print(f"Asset class '{args.asset}' backtest running via unified engine...")


def cmd_dryrun(args):
    asset_str = args.asset.lower()
    is_comm = asset_str in ("futures", "commodity", "commodities")

    if asset_str in ("options", "option"):
        # live_dryrun.py / engine.MultiAssetLiveRunner have no options signal
        # path wired up -- DirectionalOptionBuyer only runs inside the
        # backtest engine today. Silently falling through to the equity
        # scalper here would trade options-shaped symbols with equity logic,
        # so refuse instead of doing the wrong thing quietly.
        print(f"\033[91mERROR: 'dryrun --asset options' isn't implemented yet -- "
              f"there is no live/paper options strategy wired up (only "
              f"'backtest --asset options' exists). Use --asset commodity or "
              f"--asset equity for dryrun.\033[0m")
        sys.exit(1)

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
    if is_comm:
        cmd.append("--commodity")
    elif args.top_n:
        cmd.extend(["--top-n", str(args.top_n)])
    if args.symbols:
        cmd.extend(["--symbols"] + args.symbols)
    if args.direction:
        cmd.extend(["--direction", args.direction])

    subprocess.run(cmd)


def cmd_report(args):
    db = TradingDB(args.db)
    db.print_dashboard(args.account)


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
    p_bt = subparsers.add_parser("backtest", help="Run walk-forward backtest on historical data")
    p_bt.add_argument("--asset", choices=["futures", "equity", "options", "commodity"],
                       default=_env("BACKTEST_ASSET", "futures"))
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
    p_bt.add_argument("--top-n", type=int, default=int(_env("BACKTEST_EQUITY_TOP_N", "15")),
                       help="Equity screener top N")
    p_bt.add_argument("--full-session", action="store_true",
                       default=_env("BACKTEST_FULL_SESSION", "false").lower() in ("1", "true", "yes"),
                       help="Include full session bars (or set BACKTEST_FULL_SESSION=true in .env)")
    p_bt.set_defaults(func=cmd_backtest)

    # Dryrun Subcommand -- defaults come from backend/.env (DRYRUN_* / TRADING_*
    # vars) so `python3 cli.py dryrun` needs no flags at all; passing a flag
    # still overrides the .env value for that one run.
    p_dr = subparsers.add_parser("dryrun", help="Run live paper-trading dryrun")
    p_dr.add_argument("--asset", choices=["futures", "equity", "options", "commodity"],
                       default=_env("DRYRUN_ASSET", "futures"))
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
    p_dr.add_argument("--top-n", type=int, default=int(_env("DRYRUN_EQUITY_TOP_N", "15")),
                       help="Equity screener top N (--asset equity only, ignored for commodity)")
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
