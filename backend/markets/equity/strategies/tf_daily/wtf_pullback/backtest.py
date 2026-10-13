#!/usr/bin/env python3
"""
markets/equity/strategies/tf_daily/wtf_pullback/backtest.py
CLI backtester for the Weekly Trend + Daily RSI Pullback ("WTF Pullback") strategy.
Supports full Upstox statutory costs, custom capital, year selection, and universe mode.

Usage:
  .venv/bin/python3 markets/equity/strategies/tf_daily/wtf_pullback/backtest.py --year 2026 --capital 100000
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

warnings.filterwarnings("ignore")

# Ensure backend root is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "../../../../.."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from markets.equity.costs import compute_nse_equity_delivery_costs, size_equity_shares
from markets.equity.strategies.tf_daily.wtf_pullback.strategy import (
    TOP_MOMENTUM_UNIVERSE,
    BROAD_UNIVERSE,
)

DATA_DIR = os.path.join(BACKEND_DIR, "var/archive/equity")


def load_candles(symbols: list[str]) -> tuple[dict, dict]:
    daily, weekly = {}, {}
    for sym in symbols:
        for tf, store in [("daily", daily), ("weekly", weekly)]:
            fpath = os.path.join(DATA_DIR, f"{sym}_{tf}.csv")
            if not os.path.exists(fpath):
                continue
            df = pd.read_csv(fpath)
            if df.empty or len(df) < (60 if tf == "daily" else 20):
                continue
            df["datetime"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("datetime").reset_index(drop=True)
            df["date_str"] = df["datetime"].dt.strftime("%Y-%m-%d")
            if tf == "daily":
                df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
                df["ema50"] = ta.ema(df["close"], length=50)
                df["rsi14"] = ta.rsi(df["close"], length=14)
            store[sym] = df
    return daily, weekly


def run_backtest(
    year: int,
    initial_capital: float = 100000.0,
    risk_pct: float = 6.0,
    max_pos: int = 3,
    universe_mode: str = "top_momentum",
    compound: bool = True,
):
    symbols = list(TOP_MOMENTUM_UNIVERSE if universe_mode == "top_momentum" else BROAD_UNIVERSE)
    daily, weekly = load_candles(symbols)

    start_date = f"{year}-01-01"
    end_date = f"{year}-12-31"

    # Pre-index daily records for fast O(1) access
    drecs = {sym: df.set_index("date_str").to_dict("index") for sym, df in daily.items()}
    all_dates = sorted(
        list(set(d for rec in drecs.values() for d in rec if start_date <= d <= end_date))
    )

    if not all_dates:
        print(f"No market data available for year {year} in {DATA_DIR}")
        return

    cash = float(initial_capital)
    open_pos = {}
    trades = []
    equity_curve = []

    def get_weekly_bullish(sym: str, d: str) -> bool:
        df_w = weekly.get(sym)
        if df_w is None:
            return False
        sub = df_w[df_w["date_str"] <= d]
        if len(sub) < 12:
            return False
        ema10 = ta.ema(sub["close"], length=10)
        if ema10 is None or len(ema10) == 0:
            return False
        last_ema = ema10.iloc[-1]
        return not pd.isna(last_ema) and sub["close"].iloc[-1] > last_ema

    for d in all_dates:
        # 1. Manage Exits
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            r = drecs.get(sym, {}).get(d)
            if not r:
                continue
            hi, lo = r["high"], r["low"]
            ep, er = None, None

            if lo <= pos["sl"]:
                ep = pos["sl"]
                er = "breakeven" if pos["sl"] >= pos["entry"] else "stop_loss"
            elif hi >= pos["tp"]:
                ep = pos["tp"]
                er = "take_profit"

            if ep is not None:
                cost = compute_nse_equity_delivery_costs("long", pos["entry"], ep, pos["qty"], symbol=sym)
                cash += pos["qty"] * ep - cost["total"]
                trades.append({
                    "sym": sym, "entry_date": pos["entry_date"], "exit_date": d,
                    "entry": pos["entry"], "exit": ep, "qty": pos["qty"],
                    "net": cost["net"], "fees": cost["total"], "reason": er,
                    "hold_days": (pd.Timestamp(d) - pd.Timestamp(pos["entry_date"])).days
                })
                del open_pos[sym]
            else:
                # Breakeven trail at +1.5R (gives trades room to breathe, prevents premature scratching)
                risk_unit = pos["entry"] - pos["orig_sl"]
                if risk_unit > 0 and hi >= pos["entry"] + 1.5 * risk_unit:
                    pos["sl"] = max(pos["sl"], round(pos["entry"] * 1.006, 2))

        # Current portfolio equity
        port_eq = cash + sum(
            p["qty"] * (drecs.get(s, {}).get(d, {}).get("close", p["entry"]))
            for s, p in open_pos.items()
        )
        equity_curve.append({"date": d, "equity": port_eq})

        # 2. Check Entries
        if len(open_pos) < max_pos:
            cands = []
            for sym in symbols:
                if sym in open_pos or sym not in drecs:
                    continue
                r = drecs[sym].get(d)
                if not r:
                    continue
                c, hi, lo, op = r["close"], r["high"], r["low"], r["open"]
                ema50, rsi, atr = r.get("ema50"), r.get("rsi14"), r.get("atr")

                if pd.isna(ema50) or c < ema50:
                    continue
                if pd.isna(rsi) or rsi < 35.0 or rsi > 48.0:
                    continue
                bar_range = max(hi - lo, 1e-4)
                if not (c > op and (c - lo) / bar_range >= 0.40):
                    continue
                if not get_weekly_bullish(sym, d):
                    continue

                atr_val = atr if (atr and not pd.isna(atr)) else 0.015 * c
                # Wider stop (1.0 ATR) gives trades breathing room from daily noise
                sl = round(lo - 1.0 * atr_val, 2)
                sd = c - sl
                if sd <= 0:
                    continue

                # Realistic, attainable 2.0R target rather than distant 20-day high
                tp = round(c + 2.0 * sd, 2)
                rr = 2.0

                cands.append({"sym": sym, "entry": c, "sl": sl, "tp": tp, "rr": rr, "sd": sd})

            cands.sort(key=lambda x: x["rr"], reverse=True)
            for cand in cands:
                if len(open_pos) >= max_pos:
                    break
                base_cap = port_eq if compound else initial_capital
                qty = size_equity_shares(
                    capital=base_cap,
                    entry_price=cand["entry"],
                    stop_distance=cand["sd"],
                    risk_pct=risk_pct,
                    leverage=1.0,
                )
                max_slot_alloc = (port_eq / max_pos) if compound else (initial_capital / max_pos)
                max_qty_by_slot = int(min(max_slot_alloc, cash) / cand["entry"])
                qty = min(qty, max_qty_by_slot)

                if qty <= 0 or qty * cand["entry"] > cash:
                    continue

                cash -= qty * cand["entry"]
                open_pos[cand["sym"]] = {
                    "entry_date": d, "entry": cand["entry"], "sl": cand["sl"],
                    "orig_sl": cand["sl"], "tp": cand["tp"], "qty": qty
                }

    # Mark-to-market any remaining positions at end of year
    ld = all_dates[-1]
    for sym, pos in list(open_pos.items()):
        r = drecs.get(sym, {}).get(ld)
        lp = r["close"] if r else pos["entry"]
        cost = compute_nse_equity_delivery_costs("long", pos["entry"], lp, pos["qty"], symbol=sym)
        cash += pos["qty"] * lp - cost["total"]
        trades.append({
            "sym": sym, "entry_date": pos["entry_date"], "exit_date": ld + "*",
            "entry": pos["entry"], "exit": lp, "qty": pos["qty"],
            "net": cost["net"], "fees": cost["total"], "reason": "open_m2m",
            "hold_days": (pd.Timestamp(ld) - pd.Timestamp(pos["entry_date"])).days
        })

    # Summary
    n_trades = len(trades)
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    total_net = cash - initial_capital
    roi = (total_net / initial_capital) * 100.0
    win_rate = (len(wins) / n_trades * 100.0) if n_trades else 0.0
    gross_win = sum(t["net"] for t in wins)
    gross_loss = abs(sum(t["net"] for t in losses))
    pf = (gross_win / max(gross_loss, 1.0)) if gross_loss > 0 else 99.0
    total_fees = sum(t["fees"] for t in trades)
    avg_hold = np.mean([t["hold_days"] for t in trades]) if trades else 0.0

    print("=" * 75)
    print(f"  WTF PULLBACK BACKTEST RESULTS — YEAR {year}")
    print(f"  Universe: {universe_mode.upper()} ({len(symbols)} symbols) | Risk: {risk_pct}% per trade")
    print("=" * 75)
    print(f"  Starting Capital:      Rs. {initial_capital:>12,.2f}")
    print(f"  Final Capital:         Rs. {cash:>12,.2f}")
    print(f"  Net Profit Earned:     Rs. {total_net:>+12,.2f}  ({roi:+.2f}%)")
    print(f"  Total Trades:          {n_trades:>12}")
    print(f"  Wins / Losses:         {len(wins)} / {len(losses)}  (Win Rate: {win_rate:.1f}%)")
    print(f"  Profit Factor (PF):    {pf:>12.2f}")
    print(f"  Statutory Costs Deducted: Rs. {total_fees:>9,.2f}")
    print(f"  Avg Holding Period:    {avg_hold:>12.1f} days")
    print("=" * 75)
    return trades


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WTF Pullback Strategy Backtester")
    parser.add_argument("--year", type=int, default=2026, help="Backtest year (e.g. 2026)")
    parser.add_argument("--capital", type=float, default=100000.0, help="Initial capital in Rs.")
    parser.add_argument("--risk-pct", type=float, default=6.0, help="Risk percent per trade (default: 6.0)")
    parser.add_argument("--max-pos", type=int, default=3, help="Max concurrent open positions (default: 3)")
    parser.add_argument("--universe", type=str, default="top_momentum", choices=["top_momentum", "broad"],
                        help="Universe mode: 'top_momentum' (Top 20 leaders) or 'broad' (Nifty50)")
    parser.add_argument("--no-compound", action="store_true", help="Disable compounding position sizing")
    args = parser.parse_args()

    run_backtest(
        year=args.year,
        initial_capital=args.capital,
        risk_pct=args.risk_pct,
        max_pos=args.max_pos,
        universe_mode=args.universe,
        compound=not args.no_compound,
    )
