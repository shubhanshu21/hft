#!/usr/bin/env python3
"""
backend/backtest_index_futures.py — 5-Minute NSE Index Futures (NIFTY/BANKNIFTY) Scalping Backtest Engine

Mirrors markets/currency/scalping/backtest.py's proven structure, pointed at NIFTY/BANKNIFTY
index futures instead of NCD_FO currency futures.

Differences from the currency path, both real and deliberate:
  - Session: NSE F&O trades 09:15-15:30 IST (equity market hours), NOT
    MCX/NCD's shared 09:00 open -- `compute_commodity_features` is called
    with `session_open_minutes=9*60+15` so `minutes_since_open` (and every
    session-window gate downstream) is anchored correctly. Reusing the
    09:00 anchor here would silently misalign every session gate by 15
    minutes, the exact class of mistake flagged mid-project ("forex market
    time and commodity and equity are different").
  - Cost model: markets/index_futures/costs.py -- STT 0.05% on sell-side
    turnover (hiked from 0.02% in Union Budget 2026, effective 2026-04-01),
    a real cost neither MCX (0.01% CTT) nor currency (STT-exempt) carries
    at this rate. See that module's docstring for the full schedule.
  - No trained ML model exists yet -- entries are rule-based only (p_up
    held at a neutral 0.50), same convention as currency at this stage.
  - ENTRY_THRESHOLDS starts as ONE unvalidated set (borrowed from
    currency's EURINR calibration as a reasonable starting point) applied
    to both symbols -- NOT yet per-symbol swept the way crude/gold/USDINR
    each earned their own dedicated sweep. Treat this run as a first,
    honest baseline, not a tuned result.

Usage:
    python3 -m markets.index_futures.backtest --symbols NIFTY
    python3 -m markets.index_futures.backtest --symbols NIFTY BANKNIFTY
"""
from __future__ import annotations

from core.paths import ARCHIVE_ROOT, BACKEND_ROOT
import argparse
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(BACKEND_ROOT))
load_dotenv(dotenv_path=BACKEND_ROOT / ".env")

from markets.index_futures.costs import compute_index_futures_costs, size_index_futures_lots
from markets.commodity.features import compute_commodity_features

ARCHIVE_DIR = ARCHIVE_ROOT / "index_futures"

SESSION_OPEN_MINUTES = 9 * 60 + 15  # NSE F&O opens 09:15 IST -- NOT MCX/NCD's shared 09:00

HOLD_BARS = 16
TAKE_PROFIT_MULT = 1.80
STOP_VOL_MULT = 1.4
BE_ACTIVATION_MULT = 0.60
TRAIL_DIST_MULT = 0.30
BE_LOCK_BUFFER_PCT = 0.0020

# Unvalidated baseline (borrowed from currency's EURINR calibration) -- see
# module docstring. Not yet per-symbol swept.
ENTRY_THRESHOLDS = {
    "NIFTY":     {"min_adx": 10.0, "min_vol": 1.1, "min_vwap": 0.06, "min_stop_pct": 0.0006, "min_ema_slope": 0.008, "tp_mult": 1.80, "stop_mult": 1.4},
    "BANKNIFTY": {"min_adx": 10.0, "min_vol": 1.1, "min_vwap": 0.06, "min_stop_pct": 0.0006, "min_ema_slope": 0.008, "tp_mult": 1.80, "stop_mult": 1.4},
}
_MIN_ORB = 0.05


def run_index_futures_backtest(
    symbols: list[str] | None = None,
    capital: float = 100000.0,
    risk_pct: float = 10.0,
    leverage: float = 7.0,
    long_only: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
    size_mode: str = "margin",
    return_trades: bool = False,
) -> dict:
    target_symbols = symbols or ["NIFTY"]

    all_trades: list[dict] = []
    current_capital = capital
    equity_curve = [capital]

    _archive_lo, _archive_hi = None, None
    for _sym in target_symbols:
        _p = ARCHIVE_DIR / f"{_sym.upper()}_5minute.csv"
        if _p.exists():
            _ts = pd.read_csv(_p, usecols=lambda c: c in ("timestamp", "date"))
            _ts_col = "timestamp" if "timestamp" in _ts.columns else "date"
            _ts[_ts_col] = pd.to_datetime(_ts[_ts_col])
            _lo, _hi = _ts[_ts_col].min(), _ts[_ts_col].max()
            _archive_lo = _lo if _archive_lo is None else min(_archive_lo, _lo)
            _archive_hi = _hi if _archive_hi is None else max(_archive_hi, _hi)
    _actual_start = from_date or (str(_archive_lo.date()) if _archive_lo is not None else "?")
    _actual_end = to_date or (str(_archive_hi.date()) if _archive_hi is not None else "?")
    period_str = f"{_actual_start} to {_actual_end}"
    if _archive_lo is not None and from_date is None and to_date is None:
        _n_days = (_archive_hi.date() - _archive_lo.date()).days + 1
        period_str += f"  ({_n_days} calendar days -- real NSE_FO archives are capped to the front-month contract's own listing life, same constraint as MCX/NCD_FO)"

    print(f"\n{'='*75}")
    print(f"  NSE INDEX FUTURES 5-MINUTE SCALPER WALK-FORWARD BACKTEST")
    print(f"  Capital: ₹{capital:,.0f} | Risk: {risk_pct}% | Leverage: {leverage}x | Long-Only: {long_only}")
    print(f"  Period: {period_str}")
    print(f"  Session: NSE F&O session (09:15-15:30 IST) -- equity market hours, NOT MCX/currency's 09:00 open")
    print(f"  ML Filter: DISABLED (no trained model exists for index futures yet -- rule-based only)")
    print(f"  Entry thresholds: UNVALIDATED baseline (see ENTRY_THRESHOLDS comment) -- not yet per-symbol swept")
    print(f"  Symbols ({len(target_symbols)}): {', '.join(target_symbols)}")
    print(f"{'='*75}\n")

    for sym in target_symbols:
        csv_path = ARCHIVE_DIR / f"{sym.upper()}_5minute.csv"
        if not csv_path.exists():
            print(f"  {sym}: no archive found, skipping.")
            continue

        raw_df = pd.read_csv(csv_path)
        feat_df = compute_commodity_features(raw_df, symbol=sym.upper(), session_open_minutes=SESSION_OPEN_MINUTES)

        in_pos = False
        pos = {}

        n = len(feat_df)
        closes = feat_df["close"].values
        highs = feat_df["high"].values
        lows = feat_df["low"].values
        vwap_ds = feat_df["vwap_dist_pct"].values
        ema_slopes = feat_df["ema_slope_pct"].values
        mins_open = feat_df["minutes_since_open"].values
        timestamps = feat_df["timestamp"].values
        adxs = feat_df["adx"].values if "adx" in feat_df.columns else np.full(n, 25.0)
        dmps = feat_df["dmp"].values if "dmp" in feat_df.columns else np.full(n, 25.0)
        dmns = feat_df["dmn"].values if "dmn" in feat_df.columns else np.full(n, 25.0)
        vol_surges = feat_df["vol_surge_ratio"].values if "vol_surge_ratio" in feat_df.columns else np.full(n, 1.0)
        atrs = feat_df["atr"].values if "atr" in feat_df.columns else (feat_df["avg_range_pct"].values / 100.0) * closes
        orb_h = feat_df["orb_high_dist_pct"].values
        orb_l = feat_df["orb_low_dist_pct"].values

        _et = ENTRY_THRESHOLDS.get(sym.upper(), ENTRY_THRESHOLDS["NIFTY"])

        for i in range(25, n):
            c_price = closes[i]
            c_high = highs[i]
            c_low = lows[i]
            c_time = timestamps[i]
            c_day = str(c_time)[:10]
            m_open = mins_open[i]

            if from_date and c_day < from_date:
                continue
            if to_date and c_day > to_date:
                continue

            if in_pos:
                d = 1 if pos["direction"] == "long" else -1
                fav = c_high if d == 1 else c_low
                adv = c_low if d == 1 else c_high

                exit_p = None; reason = None
                if (fav >= pos["tp"] if d == 1 else fav <= pos["tp"]):
                    exit_p = pos["tp"]; reason = "take_profit"
                elif (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
                    exit_p = pos["current_stop"]; reason = "be_stop" if pos["armed_be"] else "initial_stop"
                elif (i - pos["entry_idx"]) >= HOLD_BARS:
                    exit_p = c_price; reason = "timeout_exit"
                elif m_open >= 370:  # 15:25 IST square-off, ahead of NSE F&O's 15:30 close
                    exit_p = c_price; reason = "eod_squareoff"

                if exit_p is not None:
                    cost_info = compute_index_futures_costs(sym, pos["direction"], pos["entry_price"], exit_p, pos["lots"])
                    net_pnl = cost_info["net"]
                    current_capital += net_pnl
                    equity_curve.append(current_capital)

                    all_trades.append({
                        "symbol": sym, "direction": pos["direction"],
                        "entry_time": pos["entry_time"], "exit_time": c_time,
                        "entry_price": pos["entry_price"], "exit_price": exit_p,
                        "lots": pos["lots"], "gross_pnl": cost_info["gross"],
                        "total_fees": cost_info["total"], "net_pnl": net_pnl, "reason": reason,
                        **cost_info,
                    })
                    in_pos = False
                    pos = {}
                    continue
                else:
                    if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
                        pos["armed_be"] = True
                        pos["current_stop"] = pos["entry_price"] + BE_LOCK_BUFFER_PCT * pos["entry_price"] * d
                    if pos["armed_be"]:
                        pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
                        trail = pos["best_price"] - TRAIL_DIST_MULT * pos["stop_dist"] * d
                        pos["current_stop"] = (max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail))
                continue

            # NSE F&O session only: 09:15-15:05 IST for entries (15-min open buffer, no entries in the last 25 min)
            if m_open < 15 or m_open > 350:
                continue

            adx = adxs[i]
            dmp = dmps[i]
            dmn = dmns[i]
            vol_s = vol_surges[i]
            vwap_d = vwap_ds[i]
            ema_s = ema_slopes[i]
            orb_h_dist = orb_h[i]
            orb_l_dist = orb_l[i]
            atr = atrs[i]

            min_adx = _et["min_adx"]
            min_vol = _et["min_vol"]
            min_orb = _MIN_ORB
            min_vwap = _et["min_vwap"]
            min_stop_pct = _et["min_stop_pct"]
            min_ema_slope = _et["min_ema_slope"]
            tp_mult = _et.get("tp_mult", TAKE_PROFIT_MULT)
            stop_mult = _et.get("stop_mult", STOP_VOL_MULT)

            sdist = max(stop_mult * atr, min_stop_pct * c_price)
            if sdist <= 0 or c_price <= 0:
                continue

            direction = None
            if adx >= min_adx and dmp > dmn and ema_s > min_ema_slope and orb_h_dist >= min_orb and vwap_d >= min_vwap and vol_s >= min_vol:
                direction = "long"
            elif not long_only and adx >= min_adx and dmn > dmp and ema_s < -min_ema_slope and orb_l_dist <= -min_orb and vwap_d <= -min_vwap and vol_s >= min_vol:
                direction = "short"

            if not direction:
                continue

            lots = size_index_futures_lots(
                capital=current_capital, entry_price=c_price, stop_distance=sdist,
                risk_pct=risk_pct, symbol=sym.upper(), leverage=leverage, size_mode=size_mode,
            )
            if lots < 1:
                continue

            d = 1 if direction == "long" else -1
            sl = round(c_price - sdist * d, 4)
            tp = round(c_price + tp_mult * sdist * d, 4)
            be = round(c_price + BE_ACTIVATION_MULT * sdist * d, 4)

            in_pos = True
            pos = {
                "direction": direction, "entry_price": c_price, "entry_idx": i, "entry_time": c_time,
                "lots": lots, "sl": sl, "tp": tp, "be": be, "current_stop": sl,
                "best_price": c_price, "armed_be": False, "stop_dist": sdist,
            }

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

    C_RESET, C_BOLD = "\033[0m", "\033[1m"
    C_RED, C_GREEN, C_YELLOW = "\033[91m", "\033[92m", "\033[93m"
    C_CYAN, C_WHITE, C_GRAY = "\033[96m", "\033[97m", "\033[90m"

    def _col_wr(wr): return f"{(C_GREEN if wr>=55 else C_YELLOW if wr>=45 else C_RED)}{wr:5.1f}%{C_RESET}"
    def _col_ret(v): return f"{(C_GREEN if v>0 else C_RED if v<0 else C_WHITE)}₹{v:+12,.2f}{C_RESET}"
    def _col_pf(pf): return f"{(C_GREEN if pf>=1.2 else C_YELLOW if pf>=1.0 else C_RED)}{pf:5.2f}{C_RESET}"

    brok_sum  = sum(t.get("brokerage", 0.0) for t in all_trades)
    stt_sum   = sum(t.get("stt", 0.0) for t in all_trades)
    stamp_sum = sum(t.get("stamp_duty", 0.0) for t in all_trades)
    exch_sum  = sum(t.get("exchange_txn", 0.0) for t in all_trades)
    sebi_sum  = sum(t.get("sebi", 0.0) for t in all_trades)
    gst_sum   = sum(t.get("gst", 0.0) for t in all_trades)
    slip_sum  = sum(t.get("slippage", 0.0) for t in all_trades)
    cap_color = C_GREEN if net_pnl_sum > 0 else C_RED

    print(f"\n{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}  ⚡ 5-MINUTE NSE INDEX FUTURES SCALPER — WALK-FORWARD BACKTEST RESULTS{C_RESET}")
    print(f"  {C_GRAY}📅 Period:{C_RESET} {C_YELLOW}{period_str}{C_RESET} | {C_GRAY}💰 Capital:{C_RESET} {C_WHITE}₹{capital:,.0f}{C_RESET} | {C_GRAY}⚙️ Leverage:{C_RESET} {C_WHITE}{leverage:.1f}x{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")

    print(f"\n{C_BOLD}{C_WHITE}┌── 📊 PORTFOLIO EXECUTIVE SUMMARY ──────────────────────────────────────────────────┐{C_RESET}")
    print(f"│  {C_GRAY}Starting Capital:{C_RESET}  {C_WHITE}₹{capital:,.2f}{C_RESET}          {C_GRAY}Final Capital:{C_RESET}    {cap_color}₹{current_capital:,.2f} ({pct_return:+.2f}%){C_RESET}")
    print(f"│  {C_GRAY}Executed Trades:{C_RESET}   {C_WHITE}{total_trades:,}{C_RESET} ({len(wins)}W / {len(losses)}L)   {C_GRAY}Overall Win Rate:{C_RESET} {_col_wr(win_rate)}")
    print(f"│  {C_GRAY}Profit Factor:{C_RESET}     {_col_pf(profit_factor)}                 {C_GRAY}Max Drawdown:{C_RESET}     {C_RED}-{max_dd:.2f}%{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}└───{'─'*79}┘{C_RESET}")

    gross_color = C_GREEN if gross_sum > 0 else C_RED
    net_color = C_GREEN if net_pnl_sum > 0 else C_RED
    print(f"\n{C_BOLD}{C_YELLOW}┌── 💰 ITEMIZED NSE F&O STATUTORY COSTS & TAXES (₹ INR) {'─'*27}┐{C_RESET}")
    print(f"│  {C_GRAY}Gross Trading PnL:{C_RESET}               {gross_color}₹{gross_sum:+12,.2f}{C_RESET}                                    │")
    print(f"│  {C_GRAY}Upstox Flat Brokerage (₹20/ord):{C_RESET}{C_RED}-₹{brok_sum:12,.2f}{C_RESET}  {C_GRAY}(Includes 18% GST on brokerage){C_RESET}  │")
    print(f"│  {C_GRAY}STT (Futures, Sell side):{C_RESET}        {C_RED}-₹{stt_sum:12,.2f}{C_RESET}  {C_GRAY}(0.05%, hiked 2026-04-01){C_RESET}        │")
    print(f"│  {C_GRAY}Stamp Duty (Buy side):{C_RESET}           {C_RED}-₹{stamp_sum:12,.2f}{C_RESET}  {C_GRAY}(0.002%){C_RESET}                          │")
    print(f"│  {C_GRAY}NSE F&O Exchange Fee:{C_RESET}            {C_RED}-₹{exch_sum:12,.2f}{C_RESET}  {C_GRAY}(0.00183% turnover fee){C_RESET}          │")
    print(f"│  {C_GRAY}SEBI Regulatory Fee:{C_RESET}             {C_RED}-₹{sebi_sum:12,.2f}{C_RESET}  {C_GRAY}(₹10 per Crore turnover){C_RESET}         │")
    print(f"│  {C_GRAY}18% GST on Regulatory Charges:{C_RESET}   {C_RED}-₹{gst_sum:12,.2f}{C_RESET}  {C_GRAY}(GST on Exch + SEBI){C_RESET}             │")
    print(f"│  {C_GRAY}Estimated Bid-Ask Slippage:{C_RESET}      {C_RED}-₹{slip_sum:12,.2f}{C_RESET}  {C_GRAY}(½-tick per leg){C_RESET}                 │")
    print(f"{C_GRAY}├──────────────────────────────────────────────────────────────────────────────────┤{C_RESET}")
    print(f"│  {C_BOLD}{C_WHITE}TOTAL F&O FRICTION & TAXES:{C_RESET}      {C_RED}{C_BOLD}-₹{fees_sum:12,.2f}{C_RESET}                                    │")
    print(f"│  {C_BOLD}{C_WHITE}NET REALIZED PROFIT IN ₹:{C_RESET}        {net_color}{C_BOLD}₹{net_pnl_sum:+12,.2f}{C_RESET}  {C_GRAY}(After all statutory taxes){C_RESET} │")
    print(f"{C_BOLD}{C_YELLOW}└──────────────────────────────────────────────────────────────────────────────────┘{C_RESET}")

    print(f"\n{C_BOLD}{C_CYAN}┌── 📈 PER-SYMBOL ALPHA BREAKDOWN {'─'*51}┐{C_RESET}")
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
        s_gross_str = f"{C_GREEN if s_gross>0 else C_RED}₹{s_gross:+12,.2f}{C_RESET}"
        print(f"│ {C_BOLD}{C_CYAN}{sym:<12s}{C_RESET} │ {len(s_trades):6d} │ {_col_wr(s_wr):>8s} │ {s_gross_str:>14s} │ {C_RED}-₹{s_fees:10,.2f}{C_RESET} │ {_col_ret(s_net):>14s} │")
    print(f"{C_BOLD}{C_CYAN}└──────────────┴────────┴──────────┴────────────────┴──────────────┴────────────────┘{C_RESET}\n")

    result = {"trades": total_trades, "win_rate": win_rate, "net_pnl": net_pnl_sum,
              "profit_factor": profit_factor, "max_dd": max_dd, "fees": fees_sum}
    if return_trades:
        result["trade_list"] = all_trades
    return result


def main():
    parser = argparse.ArgumentParser(description="Backtest 5-min NSE Index Futures (NIFTY/BANKNIFTY) Scalping Engine")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--risk-pct", type=float, default=10.0)
    parser.add_argument("--leverage", type=float, default=7.0)
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--long-only", action="store_true")
    parser.add_argument("--from", "--from-date", dest="from_date", default=None)
    parser.add_argument("--to", "--to-date", dest="to_date", default=None)
    parser.add_argument("--size-mode", choices=["risk", "margin"], default="margin")
    args = parser.parse_args()
    run_index_futures_backtest(
        symbols=args.symbols, capital=args.capital, risk_pct=args.risk_pct, leverage=args.leverage,
        long_only=args.long_only, from_date=args.from_date, to_date=args.to_date, size_mode=args.size_mode,
    )


if __name__ == "__main__":
    main()
