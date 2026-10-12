#!/usr/bin/env python3
"""
markets/equity/experiments/fibonacci_retracement.py — Multi-Timeframe Fibonacci Retracement Backtest.

Initial Capital: ₹1,00,000
Timeframes evaluated: 1minute, 3minute, 5minute, 30minute, daily, weekly
Statutory NSE Equity Costs:
  - Intraday (MIS for 1m, 3m, 5m, 30m): compute_nse_equity_costs
  - Delivery (CNC for daily, weekly): compute_nse_equity_delivery_costs
High-performance NumPy vectorized loop for instant execution across millions of candles.
"""
from __future__ import annotations

import os
import glob
import math
import argparse
from pathlib import Path
from dataclasses import dataclass
import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from markets.equity.costs import (
    compute_nse_equity_costs,
    compute_nse_equity_delivery_costs,
    size_equity_shares,
)

ARCHIVE_DIR = Path("/var/www/html/hft/backend/var/archive/equity")


@dataclass
class Trade:
    symbol: str
    timeframe: str
    direction: str
    entry_time: object
    exit_time: object
    entry_price: float
    exit_price: float
    qty: int
    sl: float
    tp: float
    gross_pnl: float
    friction: float
    net_pnl: float
    exit_reason: str


def find_swing_points_fast(highs: np.ndarray, lows: np.ndarray, lookback: int = 15) -> tuple[np.ndarray, np.ndarray]:
    """
    Identifies swing highs and swing lows using rolling sliding window.
    """
    n = len(highs)
    swing_high = np.zeros(n, dtype=bool)
    swing_low = np.zeros(n, dtype=bool)
    
    for i in range(lookback, n - lookback):
        # Local max
        window_h = highs[i - lookback : i + lookback + 1]
        if highs[i] == np.max(window_h):
            swing_high[i] = True
        # Local min
        window_l = lows[i - lookback : i + lookback + 1]
        if lows[i] == np.min(window_l):
            swing_low[i] = True
            
    return swing_high, swing_low


def run_fibonacci_backtest(
    csv_path: Path,
    symbol: str,
    timeframe: str,
    initial_capital: float = 100000.0,
    risk_pct: float = 2.0,
    lookback: int = 15,
    target_type: str = "swing_target",
    rr_ratio: float = 2.0,
) -> tuple[dict, list[Trade]]:
    """
    Executes Fibonacci retracement strategy on a single symbol/timeframe using fast NumPy arrays.
    """
    if not csv_path.exists():
        return {}, []

    df = pd.read_csv(csv_path)
    if df.empty or len(df) < lookback * 3:
        return {}, []

    df["datetime"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("datetime").reset_index(drop=True)

    # Technical indicators
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["ema50"] = ta.ema(df["close"], length=50)

    is_intraday = timeframe in ["1minute", "3minute", "5minute", "30minute"]
    
    # Pre-extract NumPy arrays for maximum speed
    opens = df["open"].to_numpy(dtype=np.float64)
    highs = df["high"].to_numpy(dtype=np.float64)
    lows = df["low"].to_numpy(dtype=np.float64)
    closes = df["close"].to_numpy(dtype=np.float64)
    atrs = df["atr"].fillna(df["close"] * 0.01).to_numpy(dtype=np.float64)
    ema50s = df["ema50"].fillna(df["close"]).to_numpy(dtype=np.float64)
    dtimes = df["datetime"].to_numpy()

    if is_intraday:
        hours = df["datetime"].dt.hour.to_numpy()
        minutes = df["datetime"].dt.minute.to_numpy()
    else:
        hours = None
        minutes = None

    swing_highs, swing_lows = find_swing_points_fast(highs, lows, lookback=lookback)

    capital = initial_capital
    peak_capital = capital
    max_drawdown = 0.0
    trades: list[Trade] = []

    active_position = None
    last_swing_low_val = None
    last_swing_low_idx = None
    last_swing_high_val = None
    last_swing_high_idx = None

    start_bar = max(lookback * 2, 50)
    n_bars = len(df)

    for i in range(start_bar, n_bars):
        curr_time = dtimes[i]
        close_p = closes[i]
        high_p = highs[i]
        low_p = lows[i]
        open_p = opens[i]
        atr_val = atrs[i]

        check_idx = i - lookback
        if swing_lows[check_idx]:
            last_swing_low_val = lows[check_idx]
            last_swing_low_idx = check_idx
        if swing_highs[check_idx]:
            last_swing_high_val = highs[check_idx]
            last_swing_high_idx = check_idx

        # -------------------------------------------------------------
        # 1. Check Active Position Exit
        # -------------------------------------------------------------
        if active_position is not None:
            direction = active_position["direction"]
            entry_p = active_position["entry_price"]
            sl = active_position["sl"]
            tp = active_position["tp"]
            qty = active_position["qty"]
            exit_price = None
            exit_reason = None

            # Intraday 15:15 IST Square-off rule
            if is_intraday and (hours[i] == 15 and minutes[i] >= 15):
                exit_price = close_p
                exit_reason = "INTRADAY_SQUAREOFF"
            elif direction == "long":
                if low_p <= sl:
                    exit_price = sl
                    exit_reason = "STOP_LOSS"
                elif high_p >= tp:
                    exit_price = tp
                    exit_reason = "TAKE_PROFIT"
            elif direction == "short":
                if high_p >= sl:
                    exit_price = sl
                    exit_reason = "STOP_LOSS"
                elif low_p <= tp:
                    exit_price = tp
                    exit_reason = "TAKE_PROFIT"

            if exit_price is not None:
                if is_intraday:
                    cost_info = compute_nse_equity_costs(
                        direction=direction,
                        entry=entry_p,
                        exit_p=exit_price,
                        qty=qty,
                        tick_size=0.05,
                        symbol=symbol,
                    )
                else:
                    cost_info = compute_nse_equity_delivery_costs(
                        direction=direction,
                        entry=entry_p,
                        exit_p=exit_price,
                        qty=qty,
                        tick_size=0.05,
                        symbol=symbol,
                    )

                gross_pnl = cost_info["gross"]
                friction = cost_info["total"]
                net_pnl = cost_info["net"]

                capital += net_pnl
                peak_capital = max(peak_capital, capital)
                dd = (peak_capital - capital) / peak_capital * 100.0 if peak_capital > 0 else 0.0
                max_drawdown = max(max_drawdown, dd)

                t = Trade(
                    symbol=symbol,
                    timeframe=timeframe,
                    direction=direction,
                    entry_time=active_position["entry_time"],
                    exit_time=curr_time,
                    entry_price=entry_p,
                    exit_price=exit_price,
                    qty=qty,
                    sl=sl,
                    tp=tp,
                    gross_pnl=gross_pnl,
                    friction=friction,
                    net_pnl=net_pnl,
                    exit_reason=exit_reason,
                )
                trades.append(t)
                active_position = None

        # -------------------------------------------------------------
        # 2. Check Fibonacci Entry Conditions
        # -------------------------------------------------------------
        if active_position is None and capital > 10000:
            if is_intraday and ((hours[i] > 14) or (hours[i] == 14 and minutes[i] >= 45)):
                continue

            ema50 = ema50s[i]

            # BULLISH RETRACEMENT (LONG SETUP)
            if (
                last_swing_low_val is not None
                and last_swing_high_val is not None
                and last_swing_high_idx > last_swing_low_idx
            ):
                swing_range = last_swing_high_val - last_swing_low_val
                if swing_range >= 1.5 * atr_val and close_p >= ema50:
                    fib_50 = last_swing_high_val - 0.500 * swing_range
                    fib_618 = last_swing_high_val - 0.618 * swing_range
                    fib_786 = last_swing_high_val - 0.786 * swing_range

                    in_golden_zone = (low_p <= fib_50) and (low_p >= fib_786)
                    candle_range = max(high_p - low_p, 1e-4)
                    bullish_rejection = (
                        (close_p > open_p)
                        and (close_p > fib_618)
                        and ((close_p - low_p) / candle_range >= 0.5)
                    )

                    if in_golden_zone and bullish_rejection:
                        entry_price = close_p
                        sl = last_swing_low_val - (0.2 * atr_val)
                        stop_dist = entry_price - sl

                        if stop_dist > 0:
                            if target_type == "swing_target":
                                tp = last_swing_high_val
                            elif target_type == "extension":
                                tp = last_swing_high_val + 0.272 * swing_range
                            else:
                                tp = entry_price + (stop_dist * rr_ratio)

                            leverage = 5.0 if is_intraday else 1.0
                            qty = size_equity_shares(
                                capital=capital,
                                entry_price=entry_price,
                                stop_distance=stop_dist,
                                risk_pct=risk_pct,
                                leverage=leverage,
                            )

                            if qty > 0 and (qty * entry_price <= capital * leverage):
                                active_position = {
                                    "direction": "long",
                                    "entry_time": curr_time,
                                    "entry_price": entry_price,
                                    "sl": sl,
                                    "tp": tp,
                                    "qty": qty,
                                }
                                continue

            # BEARISH RETRACEMENT (SHORT SETUP - INTRADAY ONLY)
            if is_intraday and (
                last_swing_high_val is not None
                and last_swing_low_val is not None
                and last_swing_low_idx > last_swing_high_idx
            ):
                swing_range = last_swing_high_val - last_swing_low_val
                if swing_range >= 1.5 * atr_val and close_p <= ema50:
                    fib_50 = last_swing_low_val + 0.500 * swing_range
                    fib_618 = last_swing_low_val + 0.618 * swing_range
                    fib_786 = last_swing_low_val + 0.786 * swing_range

                    in_golden_zone = (high_p >= fib_50) and (high_p <= fib_786)
                    candle_range = max(high_p - low_p, 1e-4)
                    bearish_rejection = (
                        (close_p < open_p)
                        and (close_p < fib_618)
                        and ((high_p - close_p) / candle_range >= 0.5)
                    )

                    if in_golden_zone and bearish_rejection:
                        entry_price = close_p
                        sl = last_swing_high_val + (0.2 * atr_val)
                        stop_dist = sl - entry_price

                        if stop_dist > 0:
                            if target_type == "swing_target":
                                tp = last_swing_low_val
                            elif target_type == "extension":
                                tp = last_swing_low_val - 0.272 * swing_range
                            else:
                                tp = entry_price - (stop_dist * rr_ratio)

                            qty = size_equity_shares(
                                capital=capital,
                                entry_price=entry_price,
                                stop_distance=stop_dist,
                                risk_pct=risk_pct,
                                leverage=5.0,
                            )

                            if qty > 0 and (qty * entry_price <= capital * 5.0):
                                active_position = {
                                    "direction": "short",
                                    "entry_time": curr_time,
                                    "entry_price": entry_price,
                                    "sl": sl,
                                    "tp": tp,
                                    "qty": qty,
                                }

    # Summary metrics
    n_trades = len(trades)
    if n_trades == 0:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "gross_pnl": 0.0,
            "total_friction": 0.0,
            "net_pnl": 0.0,
            "ending_capital": capital,
            "roi_pct": 0.0,
            "max_drawdown_pct": 0.0,
        }, trades

    wins = [t for t in trades if t.net_pnl > 0]
    win_rate = len(wins) / n_trades * 100.0

    gross_profit = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    gross_loss = abs(sum(t.gross_pnl for t in trades if t.gross_pnl < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999.0

    total_gross = sum(t.gross_pnl for t in trades)
    total_friction = sum(t.friction for t in trades)
    total_net = sum(t.net_pnl for t in trades)
    roi_pct = (capital - initial_capital) / initial_capital * 100.0

    res = {
        "symbol": symbol,
        "timeframe": timeframe,
        "trades": n_trades,
        "win_rate": round(win_rate, 2),
        "profit_factor": profit_factor,
        "gross_pnl": round(total_gross, 2),
        "total_friction": round(total_friction, 2),
        "net_pnl": round(total_net, 2),
        "ending_capital": round(capital, 2),
        "roi_pct": round(roi_pct, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "avg_trade_pnl": round(total_net / n_trades, 2),
    }
    return res, trades


def get_symbol_file(symbol: str, timeframe: str) -> Path | None:
    gz_path = ARCHIVE_DIR / f"{symbol}_{timeframe}.csv.gz"
    if gz_path.exists():
        return gz_path
    csv_path = ARCHIVE_DIR / f"{symbol}_{timeframe}.csv"
    if csv_path.exists():
        return csv_path
    return None


def run_multi_timeframe_study(
    symbols: list[str],
    timeframes: list[str],
    initial_capital: float = 100000.0,
    risk_pct: float = 2.0,
    target_type: str = "swing_target",
) -> pd.DataFrame:
    records = []
    for tf in timeframes:
        tf_trades_total = 0
        tf_gross = 0.0
        tf_friction = 0.0
        tf_net = 0.0
        tf_wins = 0
        all_tf_trades = []

        print(f"Running {tf:10s} across {len(symbols)} symbols...", flush=True)

        for sym in symbols:
            fpath = get_symbol_file(sym, tf)
            if not fpath:
                continue

            stats, trades = run_fibonacci_backtest(
                csv_path=fpath,
                symbol=sym,
                timeframe=tf,
                initial_capital=initial_capital,
                risk_pct=risk_pct,
                target_type=target_type,
            )

            if stats and stats["trades"] > 0:
                tf_trades_total += stats["trades"]
                tf_gross += stats["gross_pnl"]
                tf_friction += stats["total_friction"]
                tf_net += stats["net_pnl"]
                tf_wins += sum(1 for t in trades if t.net_pnl > 0)
                all_tf_trades.extend(trades)

        win_rate = (tf_wins / tf_trades_total * 100.0) if tf_trades_total > 0 else 0.0
        gross_profit = sum(t.gross_pnl for t in all_tf_trades if t.gross_pnl > 0)
        gross_loss = abs(sum(t.gross_pnl for t in all_tf_trades if t.gross_pnl < 0))
        pf = (gross_profit / gross_loss) if gross_loss > 0 else 0.0
        friction_drag_pct = (tf_friction / gross_profit * 100.0) if gross_profit > 0 else 0.0

        records.append({
            "Timeframe": tf,
            "Total Trades": tf_trades_total,
            "Win Rate (%)": round(win_rate, 2),
            "Profit Factor": round(pf, 2),
            "Gross P&L (₹)": round(tf_gross, 2),
            "Total Friction (₹)": round(tf_friction, 2),
            "Net P&L (₹)": round(tf_net, 2),
            "Avg Net/Trade (₹)": round(tf_net / tf_trades_total, 2) if tf_trades_total > 0 else 0.0,
            "Fee Drag (%)": round(friction_drag_pct, 1),
        })

    summary_df = pd.DataFrame(records)
    return summary_df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-Timeframe Fibonacci Retracement Backtest")
    parser.add_argument("--capital", type=float, default=100000.0, help="Initial capital in INR")
    parser.add_argument("--risk", type=float, default=2.0, help="Risk percent per trade")
    parser.add_argument("--target", type=str, default="swing_target", choices=["swing_target", "extension", "rr2"])
    parser.add_argument("--symbols", type=str, default="top10", help="comma-separated or 'all' or 'top10'")
    args = parser.parse_args()

    top10_symbols = [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
        "SBIN", "BHARTIARTL", "LT", "TATAMOTORS", "AXISBANK"
    ]

    if args.symbols == "top10":
        target_symbols = top10_symbols
    elif args.symbols == "all":
        target_symbols = sorted(list(set(f.split("_")[0] for f in os.listdir(ARCHIVE_DIR) if "_" in f)))
    else:
        target_symbols = [s.strip().upper() for s in args.symbols.split(",")]

    timeframes_to_test = ["1minute", "3minute", "5minute", "30minute", "daily", "weekly"]

    print(f"Starting Multi-Timeframe Fibonacci Retracement Backtest...")
    print(f"Capital: ₹{args.capital:,.2f} | Risk per Trade: {args.risk}% | Target: {args.target}")
    print(f"Symbols ({len(target_symbols)}): {target_symbols[:10]}...")

    df_results = run_multi_timeframe_study(
        symbols=target_symbols,
        timeframes=timeframes_to_test,
        initial_capital=args.capital,
        risk_pct=args.risk,
        target_type=args.target,
    )

    print("\n" + "=" * 80)
    print("                    MULTI-TIMEFRAME FIBONACCI STUDY RESULTS")
    print("=" * 80)
    print(df_results.to_string(index=False))
    print("=" * 80)
