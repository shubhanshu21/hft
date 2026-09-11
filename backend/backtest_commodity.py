#!/usr/bin/env python3
"""
backend/backtest_commodity.py — 5-Minute MCX Commodity Scalping Backtest Engine

Features:
  - Multi-year Walk-Forward Replay over 5-Minute Bars (2022–2026).
  - Trades CRUDEOIL, NATURALGAS, GOLD, SILVER, COPPER (and Mini/Micro contracts).
  - Session-aware: Evaluates setups primarily in the US/Evening Session (18:00–22:30 IST).
  - Exact Indian MCX Statutory Fees (0.01% CTT, ₹20 Upstox brokerage cap, Stamp Duty, GST, slippage).
  - Asymmetric +1.8R Take Profit, Trailing Stops, and Breakeven Arming.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from dotenv import load_dotenv

# Add backend root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

from strategy.commodity_costs import (
    COMMODITY_SPECS, compute_mcx_commodity_costs, size_commodity_lots, get_contract_multiplier
)
from strategy.commodity_features import compute_commodity_features

ARCHIVE_DIR = Path(__file__).resolve().parent / "archive_commodities"

HOLD_BARS = 16
TAKE_PROFIT_MULT = 1.20
STOP_VOL_MULT = 1.4
BE_ACTIVATION_MULT = 0.50  # was 0.35 -- backtested 2026-09-10: delaying arming to 50% of the R-multiple toward TP (vs 29% of actual TP distance before) improved both net return and max drawdown over Jan-Sep 2026, see git history for the comparison
TRAIL_DIST_MULT = 0.30
BE_LOCK_BUFFER_PCT = 0.0020  # was 0.0008 -- same backtest: locking a bigger guaranteed profit on arm outperformed the tighter buffer

# Asset-calibrated entry-signal thresholds. NATGAS runs stricter (noisier
# order book, thinner liquidity outside inventory windows) than crude by
# default -- see README's Crude vs NatGas microstructure comparison.
ENTRY_THRESHOLDS = {
    "crude":  {"min_ml_l": 0.54, "max_ml_s": 0.44, "min_adx": 15.0, "min_vol": 1.10, "min_orb": 0.05, "min_vwap": 0.05, "min_stop_pct": 0.0035},
    "natgas": {"min_ml_l": 0.55, "max_ml_s": 0.43, "min_adx": 19.0, "min_vol": 1.40, "min_orb": 0.08, "min_vwap": 0.08, "min_stop_pct": 0.0050},
}
# min_adx was 20.0/24.0 -- backtested 2026-09-10: lowering by 5 (validated on
# both Jan-Sep 7 and Jan-Sep 10 ranges, at both 2x and 4x leverage) improved
# win rate, net profit (+33-58%), and max drawdown simultaneously. Every
# other lever tried (tighter ML/ADX/volume, TP/stop-distance, hold-bars) was
# flat-to-worse -- see conversation history / git log for the full sweep.


def run_commodity_backtest(
    symbols: list[str] | None = None,
    capital: float = 200000.0,
    risk_pct: float = 2.5,
    leverage: float = 5.0,
    long_only: bool = False,
    us_session_only: bool = True,
    from_date: str | None = None,
    to_date: str | None = None,
    size_mode: str = "margin",
    no_ml_filter: bool = False,  # diagnostic only: drop the p_up condition, keep every other rule-based filter -- see conversation history for why/when
) -> dict:
    target_symbols = symbols or ["CRUDEOILM", "NATGASMINI"]

    all_trades: list[dict] = []
    current_capital = capital
    equity_curve = [capital]

    period_str = f"{from_date or '2022-01-01'} to {to_date or '2026-03-01'}"

    print(f"\n{'='*75}")
    print(f"  MCX COMMODITY 5-MINUTE SCALPER WALK-FORWARD BACKTEST")
    print(f"  Capital: ₹{capital:,.0f} | Risk: {risk_pct}% | Leverage: {leverage}x | Long-Only: {long_only}")
    print(f"  Period: {period_str}")
    print(f"  Session: {'US/Evening Overlap (18:30-22:00 IST)' if us_session_only else 'Full Session (09:00-23:30)'}")
    print(f"  ML Filter: {'DISABLED (rule-based only)' if no_ml_filter else 'enabled'}")
    print(f"  Symbols ({len(target_symbols)}): {', '.join(target_symbols)}")
    print(f"{'='*75}\n")

    aliases = {
        "CRUDEOILM": "CRUDEOIL",
        "NATGASMINI": "NATURALGAS",
        "GOLDM": "GOLD",
        "SILVERMIC": "SILVER",
        "SILVERM": "SILVER",
        "COPPER": "COPPER",
    }

    # Lot point value multiplier map for MCX
    point_vals = {
        "CRUDEOILM": 10.0,
        "NATGASMINI": 25.0 / 0.10,
        "GOLDM": 10.0,
        "SILVERMIC": 1.0,
        "SILVERM": 5.0,
        "COPPER": 2500.0,
        "CRUDEOIL": 100.0,
        "NATURALGAS": 125.0 / 0.10,
        "GOLD": 100.0,
        "SILVER": 30.0,
    }

    for sym in target_symbols:
        base_sym = aliases.get(sym.upper(), sym.upper())
        csv_path = ARCHIVE_DIR / f"{base_sym}_5minute.csv"
        if not csv_path.exists():
            csv_path = ARCHIVE_DIR / f"{sym.upper()}_5minute.csv"
        if not csv_path.exists():
            continue

        raw_df = pd.read_csv(csv_path)
        feat_df = compute_commodity_features(raw_df, symbol=base_sym)

        in_pos = False
        pos = {}

        n = len(feat_df)
        closes = feat_df["close"].values
        highs = feat_df["high"].values
        lows = feat_df["low"].values
        p_vols = feat_df["parkinson_vol"].values
        rsis = feat_df["intraday_rsi"].values
        vwap_ds = feat_df["vwap_dist_pct"].values
        ema_slopes = feat_df["ema_slope_pct"].values
        mins_open = feat_df["minutes_since_open"].values
        timestamps = feat_df["timestamp"].values
        adxs = feat_df["adx"].values if "adx" in feat_df.columns else np.full(n, 25.0)
        dmps = feat_df["dmp"].values if "dmp" in feat_df.columns else np.full(n, 25.0)
        dmns = feat_df["dmn"].values if "dmn" in feat_df.columns else np.full(n, 25.0)
        vol_surges = feat_df["vol_surge_ratio"].values if "vol_surge_ratio" in feat_df.columns else np.full(n, 1.0)
        atrs = feat_df["atr"].values if "atr" in feat_df.columns else (feat_df["avg_range_pct"].values / 100.0) * closes

        # Load LightGBM model if available
        model_path = Path(__file__).resolve().parent / "cache" / "commodity_models" / f"lgb_{sym.lower()}.pkl"
        if not model_path.exists():
            model_path = Path(__file__).resolve().parent / "cache" / "commodity_models" / f"lgb_{base_sym.lower()}.pkl"

        model = None
        if model_path.exists():
            import pickle
            try:
                with open(model_path, "rb") as f:
                    model = pickle.load(f)
            except Exception:
                model = None

        from strategy.commodity_features import COMMODITY_FEATURE_COLUMNS
        X_feats = feat_df[COMMODITY_FEATURE_COLUMNS] if model else None
        if model and X_feats is not None:
            p_ups = model.predict_proba(X_feats)[:, 1]
        else:
            p_ups = np.full(n, 0.50)

        for i in range(25, n):
            c_price = closes[i]
            c_high  = highs[i]
            c_low   = lows[i]
            c_time  = timestamps[i]
            c_day   = str(c_time)[:10]
            m_open  = mins_open[i]

            # Date filter (if specific range requested)
            if from_date and c_day < from_date:
                continue
            if to_date and c_day > to_date:
                continue

            # 1. Manage Active Position
            if in_pos:
                d = 1 if pos["direction"] == "long" else -1
                fav = c_high if d == 1 else c_low
                adv = c_low  if d == 1 else c_high

                exit_p = None; reason = None
                if (fav >= pos["tp"] if d == 1 else fav <= pos["tp"]):
                    exit_p = pos["tp"]; reason = "take_profit"
                elif (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
                    exit_p = pos["current_stop"]; reason = "be_stop" if pos["armed_be"] else "initial_stop"
                elif (i - pos["entry_idx"]) >= HOLD_BARS:
                    exit_p = c_price; reason = "timeout_exit"
                elif m_open >= 825:  # 22:45 IST square-off (ahead of Upstox 22:50 RMS cut-off)
                    exit_p = c_price; reason = "eod_squareoff"

                if exit_p is not None:
                    # Itemized MCX Cost calculation
                    cost_info = compute_mcx_commodity_costs(sym, pos["direction"], pos["entry_price"], exit_p, pos["lots"])
                    net_pnl = cost_info["net"]
                    current_capital += net_pnl
                    equity_curve.append(current_capital)

                    all_trades.append({
                        "symbol": sym,
                        "direction": pos["direction"],
                        "entry_time": pos["entry_time"],
                        "exit_time": c_time,
                        "entry_price": pos["entry_price"],
                        "exit_price": exit_p,
                        "lots": pos["lots"],
                        "gross_pnl": cost_info["gross"],
                        "total_fees": cost_info["total"],
                        "net_pnl": net_pnl,
                        "reason": reason,
                        **cost_info,
                    })


                    in_pos = False
                    pos = {}
                    continue
                else:
                    # Trail Stop & Breakeven Arming
                    if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
                        pos["armed_be"] = True
                        pos["current_stop"] = pos["entry_price"] + BE_LOCK_BUFFER_PCT * pos["entry_price"] * d
                    if pos["armed_be"]:
                        pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
                        trail = pos["best_price"] - TRAIL_DIST_MULT * pos["stop_dist"] * d
                        pos["current_stop"] = (max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail))

                continue

            # 2. Check Trading Session
            # US/Evening session runs 18:30 (570 mins) to 22:00 (780 mins)
            if us_session_only and (m_open < 570 or m_open > 780):
                continue
            elif not us_session_only and (m_open < 60 or m_open > 810):
                continue

            # 3. High-Conviction Trend & Machine Learning Filter (70%+ Win Rate Target)
            p_up = p_ups[i]
            adx = adxs[i]
            dmp = dmps[i]
            dmn = dmns[i]
            vol_s = vol_surges[i]
            vwap_d = vwap_ds[i]
            ema_s = ema_slopes[i]
            orb_h_dist = feat_df["orb_high_dist_pct"].values[i]
            orb_l_dist = feat_df["orb_low_dist_pct"].values[i]
            atr = atrs[i]

            # Asset-calibrated parameter profiles (see ENTRY_THRESHOLDS module dict)
            is_natgas = "NATGAS" in sym.upper() or "NATURALGAS" in sym.upper()
            _et = ENTRY_THRESHOLDS["natgas"] if is_natgas else ENTRY_THRESHOLDS["crude"]
            min_ml_l = _et["min_ml_l"]
            max_ml_s = _et["max_ml_s"]
            min_adx = _et["min_adx"]
            min_vol = _et["min_vol"]
            min_orb = _et["min_orb"]
            min_vwap = _et["min_vwap"]
            min_stop_pct = _et["min_stop_pct"]

            sdist = max(STOP_VOL_MULT * atr, min_stop_pct * c_price)
            if sdist <= 0 or c_price <= 0:
                continue

            direction = None
            ml_long_ok = no_ml_filter or (p_up >= min_ml_l)
            ml_short_ok = no_ml_filter or (p_up <= max_ml_s)
            # High-conviction Trend Expansion Setup
            if ml_long_ok and adx >= min_adx and dmp > dmn and ema_s > 0.010 and orb_h_dist >= min_orb and vwap_d >= min_vwap and vol_s >= min_vol:
                direction = "long"
            elif not long_only and ml_short_ok and adx >= min_adx and dmn > dmp and ema_s < -0.010 and orb_l_dist <= -min_orb and vwap_d <= -min_vwap and vol_s >= min_vol:
                direction = "short"



            if not direction:
                continue

            # Dynamic risk & leverage-based lot sizing
            lots = size_commodity_lots(
                capital=current_capital,
                entry_price=c_price,
                stop_distance=sdist,
                risk_pct=risk_pct,
                symbol=sym.upper(),
                leverage=leverage,
                size_mode=size_mode,
            )
            if lots < 1:
                continue

            d = 1 if direction == "long" else -1
            sl = round(c_price - sdist * d, 2)
            tp = round(c_price + TAKE_PROFIT_MULT * sdist * d, 2)
            be = round(c_price + BE_ACTIVATION_MULT * sdist * d, 2)

            in_pos = True
            pos = {
                "direction": direction,
                "entry_price": c_price,
                "entry_idx": i,
                "entry_time": c_time,
                "lots": lots,
                "sl": sl,
                "tp": tp,
                "be": be,
                "current_stop": sl,
                "best_price": c_price,
                "armed_be": False,
                "stop_dist": sdist,
            }



    # Summary Metrics
    total_trades = len(all_trades)
    if total_trades == 0:
        print("No trades executed.")
        return {}

    wins = [t for t in all_trades if t["net_pnl"] > 0]
    losses = [t for t in all_trades if t["net_pnl"] <= 0]
    win_rate = len(wins) / total_trades * 100

    gross_sum = sum(t["gross_pnl"] for t in all_trades)
    fees_sum = sum(t["total_fees"] for t in all_trades)
    net_pnl_sum = sum(t["net_pnl"] for t in all_trades)
    pct_return = (net_pnl_sum / capital) * 100

    gross_wins = sum(t["gross_pnl"] for t in wins)
    gross_losses = abs(sum(t["gross_pnl"] for t in losses))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 99.0

    eq = np.array(equity_curve)
    peaks = np.maximum.accumulate(eq)
    drawdowns = (peaks - eq) / peaks * 100
    max_dd = np.max(drawdowns) if len(drawdowns) > 0 else 0.0

    # ANSI Terminal Colors
    C_RESET   = "\033[0m"
    C_BOLD    = "\033[1m"
    C_RED     = "\033[91m"
    C_GREEN   = "\033[92m"
    C_YELLOW  = "\033[93m"
    C_BLUE    = "\033[94m"
    C_MAGENTA = "\033[95m"
    C_CYAN    = "\033[96m"
    C_WHITE   = "\033[97m"
    C_GRAY    = "\033[90m"

    def _col_wr(wr: float) -> str:
        color = C_GREEN if wr >= 55.0 else (C_YELLOW if wr >= 45.0 else C_RED)
        return f"{color}{wr:5.1f}%{C_RESET}"

    def _col_ret(val: float) -> str:
        color = C_GREEN if val > 0 else (C_RED if val < 0 else C_WHITE)
        return f"{color}₹{val:+12,.2f}{C_RESET}"

    def _col_pf(pf: float) -> str:
        color = C_GREEN if pf >= 1.20 else (C_YELLOW if pf >= 1.0 else C_RED)
        return f"{color}{pf:5.2f}{C_RESET}"

    brok_sum  = sum(t.get("brokerage", 0.0) for t in all_trades)
    ctt_sum   = sum(t.get("ctt", 0.0) for t in all_trades)
    stamp_sum = sum(t.get("stamp_duty", 0.0) for t in all_trades)
    exch_sum  = sum(t.get("exchange_txn", 0.0) for t in all_trades)
    sebi_sum  = sum(t.get("sebi", 0.0) for t in all_trades)
    gst_sum   = sum(t.get("gst", 0.0) for t in all_trades)
    slip_sum  = sum(t.get("slippage", 0.0) for t in all_trades)

    cap_color = C_GREEN if net_pnl_sum > 0 else C_RED

    # 1. Header Banner
    print(f"\n{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}  ⚡ 5-MINUTE MCX COMMODITY SCALPER — WALK-FORWARD BACKTEST RESULTS{C_RESET}")
    print(f"  {C_GRAY}📅 Period:{C_RESET} {C_YELLOW}{period_str}{C_RESET} | {C_GRAY}💰 Capital:{C_RESET} {C_WHITE}₹{capital:,.0f}{C_RESET} | {C_GRAY}⚙️ Leverage:{C_RESET} {C_MAGENTA}{leverage:.1f}x (MCX MIS){C_RESET}")
    print(f"{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")

    # 2. Portfolio Executive Summary Card
    print(f"\n{C_BOLD}{C_WHITE}┌── 📊 PORTFOLIO EXECUTIVE SUMMARY ──────────────────────────────────────────────────┐{C_RESET}")
    print(f"│  {C_GRAY}Starting Capital:{C_RESET}  {C_WHITE}₹{capital:,.2f}{C_RESET}          {C_GRAY}Final Capital:{C_RESET}    {cap_color}₹{current_capital:,.2f} ({pct_return:+.2f}%){C_RESET}")
    print(f"│  {C_GRAY}Executed Trades:{C_RESET}   {C_WHITE}{total_trades:,}{C_RESET} ({len(wins)}W / {len(losses)}L)   {C_GRAY}Overall Win Rate:{C_RESET} {_col_wr(win_rate)}")
    print(f"│  {C_GRAY}Profit Factor:{C_RESET}     {_col_pf(profit_factor)}                 {C_GRAY}Max Drawdown:{C_RESET}     {C_RED}-{max_dd:.2f}%{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}└───{'─'*79}┘{C_RESET}")

    # 3. Itemized Brokerage, CTT & Friction Table
    gross_color = C_GREEN if gross_sum > 0 else C_RED
    net_color = C_GREEN if net_pnl_sum > 0 else C_RED

    print(f"\n{C_BOLD}{C_YELLOW}┌── 💰 ITEMIZED MCX STATUTORY COSTS & TAXES (₹ INR) {'─'*31}┐{C_RESET}")
    print(f"│  {C_GRAY}Gross Trading PnL:{C_RESET}               {gross_color}₹{gross_sum:+12,.2f}{C_RESET}                                    │")
    print(f"│  {C_GRAY}Upstox Flat Brokerage (₹20/ord):{C_RESET}{C_RED}-₹{brok_sum:12,.2f}{C_RESET}  {C_GRAY}(Includes 18% GST on brokerage){C_RESET}  │")
    print(f"│  {C_GRAY}Commodity Trans. Tax (CTT):{C_RESET}      {C_RED}-₹{ctt_sum:12,.2f}{C_RESET}  {C_GRAY}(0.010% on Sell side){C_RESET}            │")
    print(f"│  {C_GRAY}Stamp Duty (MCX Statutory):{C_RESET}      {C_RED}-₹{stamp_sum:12,.2f}{C_RESET}  {C_GRAY}(0.002% on Buy side){C_RESET}             │")
    print(f"│  {C_GRAY}MCX Exchange Turnover Fee:{C_RESET}       {C_RED}-₹{exch_sum:12,.2f}{C_RESET}  {C_GRAY}(0.0021% turnover fee){C_RESET}           │")
    print(f"│  {C_GRAY}SEBI Commodity Regulatory Fee:{C_RESET}   {C_RED}-₹{sebi_sum:12,.2f}{C_RESET}  {C_GRAY}(₹10 per Crore turnover){C_RESET}         │")
    print(f"│  {C_GRAY}18% GST on Regulatory Charges:{C_RESET}   {C_RED}-₹{gst_sum:12,.2f}{C_RESET}  {C_GRAY}(GST on Exch + SEBI){C_RESET}             │")
    print(f"│  {C_GRAY}Estimated Bid-Ask Slippage:{C_RESET}      {C_RED}-₹{slip_sum:12,.2f}{C_RESET}  {C_GRAY}(½-tick per leg){C_RESET}                 │")
    print(f"{C_GRAY}├──────────────────────────────────────────────────────────────────────────────────┤{C_RESET}")
    print(f"│  {C_BOLD}{C_WHITE}TOTAL MCX FRICTION & TAXES:{C_RESET}      {C_RED}{C_BOLD}-₹{fees_sum:12,.2f}{C_RESET}                                    │")
    print(f"│  {C_BOLD}{C_WHITE}NET REALIZED PROFIT IN ₹:{C_RESET}        {net_color}{C_BOLD}₹{net_pnl_sum:+12,.2f}{C_RESET}  {C_GRAY}(After all statutory taxes){C_RESET} │")
    print(f"{C_BOLD}{C_YELLOW}└──────────────────────────────────────────────────────────────────────────────────┘{C_RESET}")

    # 4. Per-Commodity Performance Breakdown Table
    print(f"\n{C_BOLD}{C_CYAN}┌── 📈 PER-COMMODITY ALPHA BREAKDOWN {'─'*48}┐{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}│ {'Symbol':<12s} │ {'Trades':>6s} │ {'Win Rate':>8s} │ {'Gross PnL':>14s} │ {'Total Fees':>12s} │ {'Net Realized':>14s} │{C_RESET}")
    print(f"{C_GRAY}├──────────────┼────────┼──────────┼────────────────┼──────────────┼────────────────┤{C_RESET}")

    for sym in target_symbols:
        s_trades = [t for t in all_trades if t["symbol"] == sym]
        if not s_trades:
            continue
        s_wins = [t for t in s_trades if t["net_pnl"] > 0]
        s_wr = len(s_wins) / len(s_trades) * 100
        s_gross = sum(t["gross_pnl"] for t in s_trades)
        s_fees = sum(t["total_fees"] for t in s_trades)
        s_net = sum(t["net_pnl"] for t in s_trades)
        s_net_str = _col_ret(s_net)
        s_gross_str = f"{C_GREEN if s_gross>0 else C_RED}₹{s_gross:+12,.2f}{C_RESET}"

        print(f"│ {C_BOLD}{C_CYAN}{sym:<12s}{C_RESET} │ {len(s_trades):6d} │ {_col_wr(s_wr):>8s} │ {s_gross_str:>14s} │ {C_RED}-₹{s_fees:10,.2f}{C_RESET} │ {s_net_str:>14s} │")

    print(f"{C_BOLD}{C_CYAN}└──────────────┴────────┴──────────┴────────────────┴──────────────┴────────────────┘{C_RESET}\n")

    return {
        "trades": total_trades, "win_rate": win_rate, "net_pnl": net_pnl_sum,
        "profit_factor": profit_factor, "max_dd": max_dd, "fees": fees_sum
    }



def main():
    parser = argparse.ArgumentParser(description="Backtest 5-min MCX Commodity Scalping Engine (09:00 - 23:30 IST)")
    parser.add_argument("--capital", type=float, default=200000.0, help="Initial capital in Rs")
    parser.add_argument("--risk-pct", type=float, default=2.5, help="Risk %% per trade")
    parser.add_argument("--leverage", type=float, default=5.0, help="MIS leverage multiplier")
    parser.add_argument("--symbols", nargs="+", default=None, help="Commodity symbols to test")
    parser.add_argument("--long-only", action="store_true", help="Execute Long trades only")
    parser.add_argument("--all-day", action="store_true", help="Trade full Indian MCX day (09:00-23:30 IST) instead of evening only")
    parser.add_argument("--screener", action="store_true", help="Use automated dynamic 'Commodities in Play' daily selector")
    parser.add_argument("--from", "--from-date", "--start-date", dest="from_date", default=None, help="Start date (YYYY-MM-DD), e.g. 2024-01-01")
    parser.add_argument("--to", "--to-date", "--end-date", dest="to_date", default=None, help="End date (YYYY-MM-DD), e.g. 2024-12-31")
    parser.add_argument("--year", type=int, default=None, help="Backtest a specific year (e.g. 2024, 2025)")
    parser.add_argument("--size-mode", choices=["risk", "margin"], default="risk", help="Position sizing mode: 'risk' (pure risk budget sizing) or 'margin' (capped by broker margin)")
    parser.add_argument("--no-ml-filter", dest="no_ml_filter", action="store_true",
                         default=os.environ.get("BACKTEST_USE_ML_FILTER", "true").lower() not in ("1", "true", "yes"),
                         help="Drop the ML p_up condition, keep every other rule-based filter (or set BACKTEST_USE_ML_FILTER=false in .env)")
    parser.add_argument("--use-ml-filter", dest="no_ml_filter", action="store_false",
                         help="Force the ML p_up condition back on, overriding BACKTEST_USE_ML_FILTER=false in .env")
    args = parser.parse_args()

    from_d = args.from_date
    to_d = args.to_date
    if args.year:
        from_d = f"{args.year}-01-01"
        to_d = f"{args.year}-12-31"

    if args.screener:
        from scratch.test_screener_backtest import run_screener_portfolio_backtest
        run_screener_portfolio_backtest(capital=args.capital, risk_pct=args.risk_pct)
        return

    run_commodity_backtest(
        symbols=args.symbols,
        capital=args.capital,
        risk_pct=args.risk_pct,
        leverage=args.leverage,
        long_only=args.long_only,
        us_session_only=not args.all_day,
        from_date=from_d,
        to_date=to_d,
        size_mode=args.size_mode,
        no_ml_filter=args.no_ml_filter,
    )



if __name__ == "__main__":
    main()
