#!/usr/bin/env python3
"""
backtest_currency_ema_crossover.py — Backtest EMA 9/15 Crossover Strategy on Currency Derivatives.

Supports:
  - Symbols: USDINR, EURINR, GBPINR, JPYINR
  - Timeframes: 5m, 15m, 1m
  - Statutory NSE Currency Derivatives Cost Model (0% STT/CTT, ₹20/order max brokerage, exact exchange fees)
"""
from __future__ import annotations

import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from strategy.currency_costs import compute_ncd_currency_costs, size_currency_lots, CURRENCY_SPECS


def run_single_currency_backtest(
    csv_path: str | Path,
    symbol: str = "USDINR",
    timeframe: str = "5m",
    capital: float = 100000.0,
    risk_pct: float = 3.0,
    leverage: float = 4.0,
    rr_ratio: float = 2.5,
    use_volume_filter: bool = True,
    use_opposite_exit: bool = True,
    use_adx_filter: bool = False,
) -> dict:
    df = pd.read_csv(csv_path)
    if "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(df["timestamp"])
    else:
        df["datetime"] = pd.to_datetime(df["date"])

    df = df.sort_values("datetime").reset_index(drop=True)

    # Technical Indicators
    df["ema_fast"] = ta.ema(df["close"], length=9)
    df["ema_slow"] = ta.ema(df["close"], length=15)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(10).mean()

    # Candle metrics
    hl_range = np.maximum(df["high"] - df["low"], 1e-4)
    df["body_ratio"] = np.abs(df["close"] - df["open"]) / hl_range
    df["is_green"] = df["close"] > df["open"]
    df["is_red"] = df["close"] < df["open"]

    # Crossovers
    df["ema_diff"] = df["ema_fast"] - df["ema_slow"]
    df["bull_cross"] = (df["ema_diff"] > 0) & (df["ema_diff"].shift(1) <= 0)
    df["bear_cross"] = (df["ema_diff"] < 0) & (df["ema_diff"].shift(1) >= 0)

    # Filters
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    df["adx"] = adx_df.iloc[:, 0].fillna(20.0) if adx_df is not None else 20.0
    adx_ok = (df["adx"] >= 18.0) if use_adx_filter else True
    vol_ok = (df["volume"] >= 1.05 * df["vol_ma"]) if use_volume_filter else True

    # Signals
    df["long_signal"] = (
        df["bull_cross"]
        & (df["close"] > df["ema_fast"])
        & df["is_green"]
        & (df["body_ratio"] >= 0.3)
        & vol_ok
        & adx_ok
    )
    df["short_signal"] = (
        df["bear_cross"]
        & (df["close"] < df["ema_fast"])
        & df["is_red"]
        & (df["body_ratio"] >= 0.3)
        & vol_ok
        & adx_ok
    )

    current_capital = capital
    peak_capital = capital
    max_drawdown_pct = 0.0
    trades = []
    position = None

    lot_multiplier = CURRENCY_SPECS.get(symbol.upper(), {}).get("lot_size", 1000)

    for i in range(20, len(df)):
        row = df.iloc[i]
        price = row["close"]
        high = row["high"]
        low = row["low"]
        atr = row["atr"] if not np.isnan(row["atr"]) else price * 0.001

        # Check Position Exits
        if position is not None:
            direction = position["direction"]
            entry_price = position["entry_price"]
            sl = position["sl"]
            tp = position["tp"]
            lots = position["lots"]
            exit_price = None
            exit_reason = None

            if direction == "long":
                if low <= sl:
                    exit_price = sl
                    exit_reason = "STOP_LOSS"
                elif high >= tp:
                    exit_price = tp
                    exit_reason = "TAKE_PROFIT"
                elif use_opposite_exit and row["bear_cross"]:
                    exit_price = price
                    exit_reason = "OPPOSITE_CROSS"
            elif direction == "short":
                if high >= sl:
                    exit_price = sl
                    exit_reason = "STOP_LOSS"
                elif low <= tp:
                    exit_price = tp
                    exit_reason = "TAKE_PROFIT"
                elif use_opposite_exit and row["bull_cross"]:
                    exit_price = price
                    exit_reason = "OPPOSITE_CROSS"

            if exit_price is not None:
                gross_pnl = (exit_price - entry_price) * lots * lot_multiplier if direction == "long" else (entry_price - exit_price) * lots * lot_multiplier
                costs = compute_ncd_currency_costs(
                    symbol=symbol,
                    direction=direction,
                    entry=entry_price,
                    exit_p=exit_price,
                    lots=lots,
                )
                friction = costs.get("total", costs.get("total_friction", 0.0))
                net_pnl = gross_pnl - friction
                current_capital += net_pnl
                peak_capital = max(peak_capital, current_capital)
                dd = (peak_capital - current_capital) / peak_capital * 100.0
                max_drawdown_pct = max(max_drawdown_pct, dd)

                trades.append({
                    "symbol": symbol,
                    "entry_time": position["entry_time"],
                    "exit_time": row["datetime"],
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "lots": lots,
                    "gross_pnl": gross_pnl,
                    "friction": friction,
                    "net_pnl": net_pnl,
                    "exit_reason": exit_reason,
                    "capital": current_capital,
                })
                position = None

        # Check Position Entries
        if position is None:
            min_stop_dist = price * 0.0008  # ~8 paise for USDINR
            sl_dist = max(1.5 * atr, min_stop_dist)

            if row["long_signal"]:
                sl = price - sl_dist
                tp = price + (sl_dist * rr_ratio)
                lots = size_currency_lots(
                    capital=current_capital,
                    entry_price=price,
                    stop_distance=sl_dist,
                    risk_pct=risk_pct,
                    symbol=symbol,
                    leverage=leverage,
                )
                if lots > 0:
                    position = {
                        "direction": "long",
                        "entry_price": price,
                        "entry_time": row["datetime"],
                        "sl": sl,
                        "tp": tp,
                        "lots": min(lots, 20),
                    }
            elif row["short_signal"]:
                sl = price + sl_dist
                tp = price - (sl_dist * rr_ratio)
                lots = size_currency_lots(
                    capital=current_capital,
                    entry_price=price,
                    stop_distance=sl_dist,
                    risk_pct=risk_pct,
                    symbol=symbol,
                    leverage=leverage,
                )
                if lots > 0:
                    position = {
                        "direction": "short",
                        "entry_price": price,
                        "entry_time": row["datetime"],
                        "sl": sl,
                        "tp": tp,
                        "lots": min(lots, 20),
                    }

    total_trades = len(trades)
    win_trades = [t for t in trades if t["net_pnl"] > 0]
    loss_trades = [t for t in trades if t["net_pnl"] <= 0]
    win_rate = (len(win_trades) / total_trades * 100.0) if total_trades > 0 else 0.0
    total_net_pnl = current_capital - capital
    total_friction = sum(t["friction"] for t in trades)

    gross_wins = sum(t["gross_pnl"] for t in win_trades)
    gross_losses = abs(sum(t["gross_pnl"] for t in loss_trades))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (1.0 if total_trades == 0 else float("inf"))

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "initial_capital": capital,
        "final_capital": current_capital,
        "total_net_pnl": total_net_pnl,
        "total_net_pnl_pct": (total_net_pnl / capital) * 100.0,
        "total_friction": total_friction,
        "total_trades": total_trades,
        "win_trades": len(win_trades),
        "loss_trades": len(loss_trades),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "trades": trades,
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest EMA 9/15 Crossover Strategy on Currency Pairs")
    parser.add_argument("--symbols", nargs="+", default=["USDINR", "EURINR", "GBPINR", "JPYINR"], help="Currency symbols")
    parser.add_argument("--tf", choices=["5m", "15m", "1m"], default="15m", help="Timeframe (default: 15m)")
    parser.add_argument("--capital", type=float, default=100000.0, help="Initial Capital")
    parser.add_argument("--risk-pct", type=float, default=3.0, help="Risk % per trade")
    parser.add_argument("--leverage", type=float, default=4.0, help="Margin Leverage")
    parser.add_argument("--rr", type=float, default=2.5, help="Risk:Reward Target Ratio (default: 2.5)")
    parser.add_argument("--no-volume-filter", action="store_true", help="Disable volume surge confirmation")
    parser.add_argument("--adx-filter", action="store_true", help="Enable ADX trend filter")
    args = parser.parse_args()

    print("=" * 80)
    print(f"🌍 BACKTESTING EMA 9/15 CROSSOVER STRATEGY ACROSS NSE CURRENCY DERIVATIVES")
    print(f"   Timeframe: {args.tf} | RR Target: 1:{args.rr} | Capital: ₹{args.capital:,.2f} | Leverage: {args.leverage}x")
    print("=" * 80)

    total_net = 0.0
    total_trades_all = 0
    total_wins_all = 0
    total_friction_all = 0

    for sym in args.symbols:
        csv_file = Path(__file__).parent / "archive_currency" / f"{sym.upper()}_{'15minute' if args.tf == '15m' else ('5minute' if args.tf == '5m' else '1minute')}.csv"
        if not csv_file.exists():
            print(f"⚠️ Data file not found for {sym}: {csv_file}")
            continue

        res = run_single_currency_backtest(
            csv_path=csv_file,
            symbol=sym,
            timeframe=args.tf,
            capital=args.capital,
            risk_pct=args.risk_pct,
            leverage=args.leverage,
            rr_ratio=args.rr,
            use_volume_filter=not args.no_volume_filter,
            use_adx_filter=args.adx_filter,
        )

        total_net += res["total_net_pnl"]
        total_trades_all += res["total_trades"]
        total_wins_all += res["win_trades"]
        total_friction_all += res["total_friction"]

        pnl_col = "+" if res["total_net_pnl"] >= 0 else ""
        print(f"  📌 {sym:7s} | Net P&L: {pnl_col}₹{res['total_net_pnl']:+9,.2f} ({res['total_net_pnl_pct']:+6.2f}%) | "
              f"Win Rate: {res['win_rate']:5.1f}% | PF: {res['profit_factor']:4.2f} | "
              f"Trades: {res['total_trades']:3d} (W:{res['win_trades']:2d}/L:{res['loss_trades']:2d}) | "
              f"Max DD: {res['max_drawdown_pct']:5.2f}% | Friction: ₹{res['total_friction']:6.2f}")

    print("-" * 80)
    avg_win_rate = (total_wins_all / total_trades_all * 100.0) if total_trades_all > 0 else 0.0
    pnl_sign = "+" if total_net >= 0 else ""
    print(f"  🏁 PORTFOLIO TOTAL: {pnl_sign}₹{total_net:+,.2f} | Total Trades: {total_trades_all} | "
          f"Avg Win Rate: {avg_win_rate:.1f}% | Total Friction: ₹{total_friction_all:,.2f}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
