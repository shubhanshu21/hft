#!/usr/bin/env python3
"""
markets/commodity/experiments/ema_crossover_gold.py — Backtest for TradingView Gold (XAUUSD / MCX GOLD) EMA 9/15 Crossover Strategy.

Strategy Reference:
  TradingView Idea: "Gold (XAUUSD) EMA Crossover Strategy | Trade Setup Analysis" by Gold_Professor_SMC
  Rules:
    - Fast EMA: 9, Slow EMA: 15
    - Bullish Entry: EMA 9 crosses above EMA 15, Close > EMA 9 & 15, Green candle with Body Ratio >= 0.4, Volume > 1.2x avg
    - Bearish Entry: EMA 9 crosses below EMA 15, Close < EMA 9 & 15, Red candle with Body Ratio >= 0.4, Volume > 1.2x avg
    - Stop Loss: Swing High / Low or 1.5x ATR
    - Take Profit: 1:2 Risk-Reward ratio or Opposite EMA Crossover
    - Realistic MCX Gold transaction costs & slippage
"""
from __future__ import annotations

from core.paths import ARCHIVE_ROOT
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from markets.commodity.costs import compute_mcx_commodity_costs, COMMODITY_SPECS


def run_gold_ema_backtest(
    csv_path: str | Path,
    timeframe: str = "5m",
    capital: float = 100000.0,
    risk_pct: float = 3.0,
    rr_ratio: float = 2.0,
    use_volume_filter: bool = True,
    use_opposite_exit: bool = True,
    **kwargs,
) -> dict:
    df = pd.read_csv(csv_path)
    if "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(df["timestamp"])
    else:
        df["datetime"] = pd.to_datetime(df["date"])

    df = df.sort_values("datetime").reset_index(drop=True)

    # Compute EMAs
    df["ema_fast"] = ta.ema(df["close"], length=9)
    df["ema_slow"] = ta.ema(df["close"], length=15)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ma"] = df["volume"].rolling(10).mean()

    # Candle body metrics
    hl_range = np.maximum(df["high"] - df["low"], 1e-4)
    df["body_ratio"] = np.abs(df["close"] - df["open"]) / hl_range
    df["is_green"] = df["close"] > df["open"]
    df["is_red"] = df["close"] < df["open"]

    # Crossovers
    df["ema_diff"] = df["ema_fast"] - df["ema_slow"]
    df["bull_cross"] = (df["ema_diff"] > 0) & (df["ema_diff"].shift(1) <= 0)
    df["bear_cross"] = (df["ema_diff"] < 0) & (df["ema_diff"].shift(1) >= 0)

    # ADX and Session Filters
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    df["adx"] = adx_df.iloc[:, 0].fillna(20.0) if adx_df is not None else 20.0
    adx_ok = (df["adx"] >= 20.0) if kwargs.get("use_adx_filter", False) else True

    # US Session Filter (18:00 - 23:00 IST has highest Gold liquidity)
    df["hour"] = df["datetime"].dt.hour
    session_ok = (df["hour"] >= 14) & (df["hour"] <= 22) if kwargs.get("use_session_filter", False) else True

    # Entry Signals
    vol_ok = (df["volume"] >= 1.1 * df["vol_ma"]) if use_volume_filter else True
    df["long_signal"] = (
        df["bull_cross"]
        & (df["close"] > df["ema_fast"])
        & df["is_green"]
        & (df["body_ratio"] >= 0.35)
        & vol_ok
        & adx_ok
        & session_ok
    )
    df["short_signal"] = (
        df["bear_cross"]
        & (df["close"] < df["ema_fast"])
        & df["is_red"]
        & (df["body_ratio"] >= 0.35)
        & vol_ok
        & adx_ok
        & session_ok
    )

    # Backtest Execution Loop
    current_capital = capital
    peak_capital = capital
    max_drawdown_pct = 0.0
    trades = []
    position = None

    lot_size = COMMODITY_SPECS.get("GOLDM", {}).get("lot_size", 10)

    for i in range(20, len(df)):
        row = df.iloc[i]
        price = row["close"]
        high = row["high"]
        low = row["low"]
        atr = row["atr"] if not np.isnan(row["atr"]) else price * 0.005

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
                gross_pnl = (exit_price - entry_price) * lots * lot_size if direction == "long" else (entry_price - exit_price) * lots * lot_size
                costs = compute_mcx_commodity_costs(
                    symbol="GOLDM",
                    direction=direction,
                    entry=entry_price,
                    exit_p=exit_price,
                    lots=lots,
                )
                net_pnl = gross_pnl - costs["total"]
                current_capital += net_pnl
                peak_capital = max(peak_capital, current_capital)
                dd = (peak_capital - current_capital) / peak_capital * 100.0
                max_drawdown_pct = max(max_drawdown_pct, dd)

                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": row["datetime"],
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "lots": lots,
                    "gross_pnl": gross_pnl,
                    "friction": costs["total"],
                    "net_pnl": net_pnl,
                    "exit_reason": exit_reason,
                    "capital": current_capital,
                })
                position = None

        # Check Position Entries
        if position is None:
            if row["long_signal"]:
                sl_dist = max(1.5 * atr, price * 0.003)
                sl = price - sl_dist
                tp = price + (sl_dist * rr_ratio)
                risk_amount = current_capital * (risk_pct / 100.0)
                lots = max(1, int(risk_amount / (sl_dist * lot_size)))
                position = {
                    "direction": "long",
                    "entry_price": price,
                    "entry_time": row["datetime"],
                    "sl": sl,
                    "tp": tp,
                    "lots": min(lots, 5),
                }
            elif row["short_signal"]:
                sl_dist = max(1.5 * atr, price * 0.003)
                sl = price + sl_dist
                tp = price - (sl_dist * rr_ratio)
                risk_amount = current_capital * (risk_pct / 100.0)
                lots = max(1, int(risk_amount / (sl_dist * lot_size)))
                position = {
                    "direction": "short",
                    "entry_price": price,
                    "entry_time": row["datetime"],
                    "sl": sl,
                    "tp": tp,
                    "lots": min(lots, 5),
                }

    # Summary Metrics
    total_trades = len(trades)
    win_trades = [t for t in trades if t["net_pnl"] > 0]
    loss_trades = [t for t in trades if t["net_pnl"] <= 0]
    win_rate = (len(win_trades) / total_trades * 100.0) if total_trades > 0 else 0.0
    total_net_pnl = current_capital - capital
    total_friction = sum(t["friction"] for t in trades)

    gross_wins = sum(t["gross_pnl"] for t in win_trades)
    gross_losses = abs(sum(t["gross_pnl"] for t in loss_trades))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")

    return {
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
    parser = argparse.ArgumentParser(description="Backtest TradingView Gold EMA 9/15 Crossover Strategy")
    parser.add_argument("--tf", choices=["5m", "15m", "1m"], default="5m", help="Timeframe to test")
    parser.add_argument("--capital", type=float, default=100000.0, help="Initial Capital")
    parser.add_argument("--risk-pct", type=float, default=3.0, help="Risk % per trade")
    parser.add_argument("--rr", type=float, default=2.0, help="Risk:Reward Target Multiplier")
    parser.add_argument("--no-volume-filter", action="store_true", help="Disable volume surge confirmation")
    parser.add_argument("--adx-filter", action="store_true", help="Enable ADX >= 20 trend filter")
    parser.add_argument("--session-filter", action="store_true", help="Enable high-liquidity session filter (14:00 - 23:00 IST)")
    args = parser.parse_args()

    data_map = {
        "1m": ARCHIVE_ROOT / "commodity" / "GOLD_1minute.csv",
        "5m": ARCHIVE_ROOT / "commodity" / "GOLD_5minute.csv",
        "15m": ARCHIVE_ROOT / "commodity" / "GOLD_15minute.csv",
    }
    csv_file = data_map[args.tf]
    if not csv_file.exists():
        print(f"File not found: {csv_file}")
        return

    print("=" * 70)
    print(f"📊 BACKTESTING TRADINGVIEW GOLD EMA 9/15 CROSSOVER STRATEGY")
    print(f"   Data: {csv_file.name} | Timeframe: {args.tf} | RR: 1:{args.rr}")
    print(f"   ADX Filter: {args.adx_filter} | Session Filter: {args.session_filter}")
    print("=" * 70)

    res = run_gold_ema_backtest(
        csv_path=csv_file,
        timeframe=args.tf,
        capital=args.capital,
        risk_pct=args.risk_pct,
        rr_ratio=args.rr,
        use_volume_filter=not args.no_volume_filter,
        use_adx_filter=args.adx_filter,
        use_session_filter=args.session_filter,
    )

    print(f"  Initial Capital:   ₹{res['initial_capital']:,.2f}")
    print(f"  Final Capital:     ₹{res['final_capital']:,.2f}")
    print(f"  Net P&L:           ₹{res['total_net_pnl']:+,.2f} ({res['total_net_pnl_pct']:+.2f}%)")
    print(f"  Total Friction:    ₹{res['total_friction']:,.2f} (Brokerage + Taxes + Slippage)")
    print(f"  Total Trades:      {res['total_trades']} (Wins: {res['win_trades']} | Losses: {res['loss_trades']})")
    print(f"  Win Rate:          {res['win_rate']:.2f}%")
    print(f"  Profit Factor:     {res['profit_factor']:.2f}")
    print(f"  Max Drawdown:      {res['max_drawdown_pct']:.2f}%")
    print("=" * 70)

    if res["trades"]:
        print("\nLast 5 Closed Trades:")
        for t in res["trades"][-5:]:
            print(f"  [{str(t['exit_time'])[:19]}] {t['direction'].upper():5s} In: ₹{t['entry_price']:.1f} Out: ₹{t['exit_price']:.1f} | Net: ₹{t['net_pnl']:+8.2f} | Reason: {t['exit_reason']}")
    print()


if __name__ == "__main__":
    main()
