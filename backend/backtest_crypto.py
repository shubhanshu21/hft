#!/usr/bin/env python3
"""
backend/backtest_crypto.py — 5-Minute Binance Perpetual Futures Scalping Backtest Engine

Mirrors backtest_commodity.py's hand-rolled walk-forward loop (ML-filtered
trend-expansion entries, breakeven-arm + trailing stop management, itemized
fee accounting) for BTCUSDT/ETHUSDT USDT-M perpetual futures. The main
structural difference from the MCX version: crypto trades 24/7, so there is
no session-window gate and no end-of-day square-off -- a position is only
closed by its own TP/stop/timeout rules.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pickle
import sys

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

from strategy.crypto_costs import (
    compute_binance_futures_costs, size_crypto_position,
    BINANCE_MAKER_FEE_PCT, BINANCE_TAKER_FEE_PCT,
)
from strategy.crypto_features import compute_crypto_features, CRYPTO_FEATURE_COLUMNS

ARCHIVE_DIR = Path(__file__).resolve().parent / "archive_crypto"

HOLD_BARS = 16
TAKE_PROFIT_MULT = 1.80
STOP_VOL_MULT = 1.4
BE_ACTIVATION_MULT = 0.60
TRAIL_DIST_MULT = 0.30
BE_LOCK_BUFFER_PCT = 0.0020

# Asset-calibrated entry-signal thresholds -- seeded from the MCX
# crude/natgas defaults as a starting point; retune once real Binance
# archive data has been walk-forward tested.
ENTRY_THRESHOLDS = {
    "btc": {"min_ml_l": 0.54, "max_ml_s": 0.44, "min_adx": 15.0, "min_vol": 1.10, "min_orb": 0.05, "min_vwap": 0.05, "min_stop_pct": 0.0035},
    "eth": {"min_ml_l": 0.54, "max_ml_s": 0.44, "min_adx": 17.0, "min_vol": 1.20, "min_orb": 0.05, "min_vwap": 0.05, "min_stop_pct": 0.0040},
}


def run_crypto_backtest(
    symbols: list[str] | None = None,
    capital: float = 100000.0,
    risk_pct: float = 2.5,
    leverage: float = 5.0,
    long_only: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
    size_mode: str = "margin",
    no_ml_filter: bool = False,
    entry_fee_mode: str = "taker",
    exit_fee_mode: str = "taker",
) -> dict:
    target_symbols = symbols or ["BTCUSDT", "ETHUSDT"]

    all_trades: list[dict] = []
    current_capital = capital
    equity_curve = [capital]

    period_str = f"{from_date or 'earliest archived'} to {to_date or 'latest archived'}"

    print(f"\n{'='*75}")
    print(f"  BINANCE PERPETUAL FUTURES 5-MINUTE SCALPER WALK-FORWARD BACKTEST")
    print(f"  Capital: ${capital:,.0f} | Risk: {risk_pct}% | Leverage: {leverage}x | Long-Only: {long_only}")
    print(f"  Period: {period_str}")
    print(f"  Session: 24/7 (no session gate, no EOD square-off)")
    print(f"  ML Filter: {'DISABLED (rule-based only)' if no_ml_filter else 'enabled'}")
    print(f"  Fees: entry={entry_fee_mode} exit={exit_fee_mode} ({BINANCE_MAKER_FEE_PCT if entry_fee_mode=='maker' else BINANCE_TAKER_FEE_PCT}% / {BINANCE_MAKER_FEE_PCT if exit_fee_mode=='maker' else BINANCE_TAKER_FEE_PCT}%)")
    print(f"  Symbols ({len(target_symbols)}): {', '.join(target_symbols)}")
    print(f"{'='*75}\n")

    for sym in target_symbols:
        sym = sym.upper()
        csv_path = ARCHIVE_DIR / f"{sym}_5minute.csv"
        if not csv_path.exists():
            continue

        raw_df = pd.read_csv(csv_path)

        funding_path = ARCHIVE_DIR / f"{sym}_funding.csv"
        funding_df = None
        if funding_path.exists():
            funding_df = pd.read_csv(funding_path)
            funding_df["timestamp"] = pd.to_datetime(funding_df["timestamp"])

        feat_df = compute_crypto_features(raw_df, symbol=sym, funding_df=funding_df)

        in_pos = False
        pos = {}

        n = len(feat_df)
        closes = feat_df["close"].values
        highs = feat_df["high"].values
        lows = feat_df["low"].values
        vwap_ds = feat_df["vwap_dist_pct"].values
        ema_slopes = feat_df["ema_slope_pct"].values
        timestamps = pd.to_datetime(feat_df["timestamp"]).values
        adxs = feat_df["adx"].values if "adx" in feat_df.columns else np.full(n, 25.0)
        dmps = feat_df["dmp"].values if "dmp" in feat_df.columns else np.full(n, 25.0)
        dmns = feat_df["dmn"].values if "dmn" in feat_df.columns else np.full(n, 25.0)
        vol_surges = feat_df["vol_surge_ratio"].values if "vol_surge_ratio" in feat_df.columns else np.full(n, 1.0)
        atrs = feat_df["atr"].values if "atr" in feat_df.columns else (feat_df["avg_range_pct"].values / 100.0) * closes
        orb_h_dists = feat_df["orb_high_dist_pct"].values
        orb_l_dists = feat_df["orb_low_dist_pct"].values

        model_path = Path(__file__).resolve().parent / "cache" / "crypto_models" / f"lgb_{sym.lower()}.pkl"
        model = None
        if model_path.exists():
            try:
                with open(model_path, "rb") as f:
                    model = pickle.load(f)
            except Exception:
                model = None

        X_feats = feat_df[CRYPTO_FEATURE_COLUMNS] if model else None
        if model and X_feats is not None:
            p_ups = model.predict_proba(X_feats)[:, 1]
        else:
            p_ups = np.full(n, 0.50)

        et_key = "eth" if "ETH" in sym else "btc"
        _et = ENTRY_THRESHOLDS[et_key]
        min_ml_l = _et["min_ml_l"]
        max_ml_s = _et["max_ml_s"]
        min_adx = _et["min_adx"]
        min_vol = _et["min_vol"]
        min_orb = _et["min_orb"]
        min_vwap = _et["min_vwap"]
        min_stop_pct = _et["min_stop_pct"]

        for i in range(25, n):
            c_price = closes[i]
            c_high = highs[i]
            c_low = lows[i]
            c_time = pd.Timestamp(timestamps[i])
            c_day = str(c_time)[:10]

            if from_date and c_day < from_date:
                continue
            if to_date and c_day > to_date:
                continue

            if in_pos:
                d = 1 if pos["direction"] == "long" else -1
                fav = c_high if d == 1 else c_low
                adv = c_low if d == 1 else c_high

                exit_p = None
                reason = None
                if (fav >= pos["tp"] if d == 1 else fav <= pos["tp"]):
                    exit_p = pos["tp"]; reason = "take_profit"
                elif (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
                    exit_p = pos["current_stop"]; reason = "be_stop" if pos["armed_be"] else "initial_stop"
                elif (i - pos["entry_idx"]) >= HOLD_BARS:
                    exit_p = c_price; reason = "timeout_exit"

                if exit_p is not None:
                    cost_info = compute_binance_futures_costs(
                        sym, pos["direction"], pos["entry_price"], exit_p, pos["qty"],
                        entry_time=pos["entry_time"], exit_time=c_time, funding_df=funding_df,
                        entry_fee_mode=entry_fee_mode, exit_fee_mode=exit_fee_mode,
                    )
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
                        "qty": pos["qty"],
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
                    if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
                        pos["armed_be"] = True
                        pos["current_stop"] = pos["entry_price"] + BE_LOCK_BUFFER_PCT * pos["entry_price"] * d
                    if pos["armed_be"]:
                        pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
                        trail = pos["best_price"] - TRAIL_DIST_MULT * pos["stop_dist"] * d
                        pos["current_stop"] = (max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail))

                continue

            # No session gate -- crypto trades 24/7, every bar is eligible.
            p_up = p_ups[i]
            adx = adxs[i]
            dmp = dmps[i]
            dmn = dmns[i]
            vol_s = vol_surges[i]
            vwap_d = vwap_ds[i]
            ema_s = ema_slopes[i]
            orb_h_dist = orb_h_dists[i]
            orb_l_dist = orb_l_dists[i]
            atr = atrs[i]

            sdist = max(STOP_VOL_MULT * atr, min_stop_pct * c_price)
            if sdist <= 0 or c_price <= 0:
                continue

            direction = None
            ml_long_ok = no_ml_filter or (p_up >= min_ml_l)
            ml_short_ok = no_ml_filter or (p_up <= max_ml_s)
            if ml_long_ok and adx >= min_adx and dmp > dmn and ema_s > 0.010 and orb_h_dist >= min_orb and vwap_d >= min_vwap and vol_s >= min_vol:
                direction = "long"
            elif not long_only and ml_short_ok and adx >= min_adx and dmn > dmp and ema_s < -0.010 and orb_l_dist <= -min_orb and vwap_d <= -min_vwap and vol_s >= min_vol:
                direction = "short"

            if not direction:
                continue

            qty = size_crypto_position(
                capital=current_capital,
                entry_price=c_price,
                stop_distance=sdist,
                risk_pct=risk_pct,
                symbol=sym,
                leverage=leverage,
                size_mode=size_mode,
            )
            if qty <= 0:
                continue

            d = 1 if direction == "long" else -1
            sl = c_price - sdist * d
            tp = c_price + TAKE_PROFIT_MULT * sdist * d
            be = c_price + BE_ACTIVATION_MULT * sdist * d

            in_pos = True
            pos = {
                "direction": direction,
                "entry_price": c_price,
                "entry_idx": i,
                "entry_time": c_time,
                "qty": qty,
                "sl": sl,
                "tp": tp,
                "be": be,
                "current_stop": sl,
                "best_price": c_price,
                "armed_be": False,
                "stop_dist": sdist,
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

    C_RESET = "\033[0m"; C_BOLD = "\033[1m"; C_RED = "\033[91m"; C_GREEN = "\033[92m"
    C_YELLOW = "\033[93m"; C_MAGENTA = "\033[95m"; C_CYAN = "\033[96m"; C_WHITE = "\033[97m"; C_GRAY = "\033[90m"

    def _col_wr(wr: float) -> str:
        color = C_GREEN if wr >= 55.0 else (C_YELLOW if wr >= 45.0 else C_RED)
        return f"{color}{wr:5.1f}%{C_RESET}"

    def _col_ret(val: float) -> str:
        color = C_GREEN if val > 0 else (C_RED if val < 0 else C_WHITE)
        return f"{color}${val:+12,.2f}{C_RESET}"

    def _col_pf(pf: float) -> str:
        color = C_GREEN if pf >= 1.20 else (C_YELLOW if pf >= 1.0 else C_RED)
        return f"{color}{pf:5.2f}{C_RESET}"

    fee_sum = sum(t.get("taker_fee_entry", 0.0) + t.get("taker_fee_exit", 0.0) for t in all_trades)
    funding_sum = sum(t.get("funding_cost", 0.0) for t in all_trades)

    cap_color = C_GREEN if net_pnl_sum > 0 else C_RED

    print(f"\n{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}  BINANCE PERPETUAL FUTURES SCALPER — WALK-FORWARD BACKTEST RESULTS{C_RESET}")
    print(f"  {C_GRAY}Period:{C_RESET} {C_YELLOW}{period_str}{C_RESET} | {C_GRAY}Capital:{C_RESET} {C_WHITE}${capital:,.0f}{C_RESET} | {C_GRAY}Leverage:{C_RESET} {C_MAGENTA}{leverage:.1f}x{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")

    print(f"\n{C_BOLD}{C_WHITE}┌── PORTFOLIO EXECUTIVE SUMMARY ─────────────────────────────────────────────────────┐{C_RESET}")
    print(f"│  {C_GRAY}Starting Capital:{C_RESET}  {C_WHITE}${capital:,.2f}{C_RESET}          {C_GRAY}Final Capital:{C_RESET}    {cap_color}${current_capital:,.2f} ({pct_return:+.2f}%){C_RESET}")
    print(f"│  {C_GRAY}Executed Trades:{C_RESET}   {C_WHITE}{total_trades:,}{C_RESET} ({len(wins)}W / {len(losses)}L)   {C_GRAY}Overall Win Rate:{C_RESET} {_col_wr(win_rate)}")
    print(f"│  {C_GRAY}Profit Factor:{C_RESET}     {_col_pf(profit_factor)}                 {C_GRAY}Max Drawdown:{C_RESET}     {C_RED}-{max_dd:.2f}%{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}└───{'─'*79}┘{C_RESET}")

    gross_color = C_GREEN if gross_sum > 0 else C_RED
    net_color = C_GREEN if net_pnl_sum > 0 else C_RED

    print(f"\n{C_BOLD}{C_YELLOW}┌── ITEMIZED BINANCE FEES & FUNDING (USDT) {'─'*40}┐{C_RESET}")
    print(f"│  {C_GRAY}Gross Trading PnL:{C_RESET}               {gross_color}${gross_sum:+12,.2f}{C_RESET}                                    │")
    print(f"│  {C_GRAY}Taker Fees (0.05% x2 legs):{C_RESET}      {C_RED}-${fee_sum:12,.2f}{C_RESET}  {C_GRAY}(entry + exit, market orders){C_RESET}      │")
    print(f"│  {C_GRAY}Funding Cost/Rebate:{C_RESET}             {C_RED if funding_sum >= 0 else C_GREEN}{'-' if funding_sum >= 0 else '+'}${abs(funding_sum):12,.2f}{C_RESET}  {C_GRAY}(8h settlement while held){C_RESET}          │")
    print(f"{C_GRAY}├──────────────────────────────────────────────────────────────────────────────────┤{C_RESET}")
    print(f"│  {C_BOLD}{C_WHITE}TOTAL FEES & FUNDING:{C_RESET}            {C_RED}{C_BOLD}-${fees_sum:12,.2f}{C_RESET}                                    │")
    print(f"│  {C_BOLD}{C_WHITE}NET REALIZED PROFIT:{C_RESET}             {net_color}{C_BOLD}${net_pnl_sum:+12,.2f}{C_RESET}  {C_GRAY}(after fees + funding){C_RESET}          │")
    print(f"{C_BOLD}{C_YELLOW}└──────────────────────────────────────────────────────────────────────────────────┘{C_RESET}")

    print(f"\n{C_BOLD}{C_CYAN}┌── PER-SYMBOL ALPHA BREAKDOWN {'─'*54}┐{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}│ {'Symbol':<12s} │ {'Trades':>6s} │ {'Win Rate':>8s} │ {'Gross PnL':>14s} │ {'Total Fees':>12s} │ {'Net Realized':>14s} │{C_RESET}")
    print(f"{C_GRAY}├──────────────┼────────┼──────────┼────────────────┼──────────────┼────────────────┤{C_RESET}")

    for sym in target_symbols:
        s_trades = [t for t in all_trades if t["symbol"] == sym.upper()]
        if not s_trades:
            continue
        s_wins = [t for t in s_trades if t["net_pnl"] > 0]
        s_wr = len(s_wins) / len(s_trades) * 100
        s_gross = sum(t["gross_pnl"] for t in s_trades)
        s_fees = sum(t["total_fees"] for t in s_trades)
        s_net = sum(t["net_pnl"] for t in s_trades)
        s_net_str = _col_ret(s_net)
        s_gross_str = f"{C_GREEN if s_gross>0 else C_RED}${s_gross:+12,.2f}{C_RESET}"

        print(f"│ {C_BOLD}{C_CYAN}{sym:<12s}{C_RESET} │ {len(s_trades):6d} │ {_col_wr(s_wr):>8s} │ {s_gross_str:>14s} │ {C_RED}-${s_fees:10,.2f}{C_RESET} │ {s_net_str:>14s} │")

    print(f"{C_BOLD}{C_CYAN}└──────────────┴────────┴──────────┴────────────────┴──────────────┴────────────────┘{C_RESET}\n")

    return {
        "trades": total_trades, "win_rate": win_rate, "net_pnl": net_pnl_sum,
        "profit_factor": profit_factor, "max_dd": max_dd, "fees": fees_sum,
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest 5-min Binance USDT-M Perpetual Futures Scalping Engine (24/7)")
    parser.add_argument("--capital", type=float, default=100000.0, help="Initial capital in USDT")
    parser.add_argument("--risk-pct", type=float, default=2.5, help="Risk %% per trade")
    parser.add_argument("--leverage", type=float, default=5.0, help="Margin leverage multiplier")
    parser.add_argument("--symbols", nargs="+", default=None, help="Binance perpetual symbols to test (e.g. BTCUSDT ETHUSDT)")
    parser.add_argument("--long-only", action="store_true", help="Execute Long trades only")
    parser.add_argument("--from", "--from-date", "--start-date", dest="from_date", default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", "--to-date", "--end-date", dest="to_date", default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--year", type=int, default=None, help="Backtest a specific year (e.g. 2024, 2025)")
    parser.add_argument("--size-mode", choices=["risk", "margin"], default="risk", help="Position sizing mode: 'risk' (pure risk budget sizing) or 'margin' (capped by leverage)")
    parser.add_argument("--no-ml-filter", dest="no_ml_filter", action="store_true",
                         default=os.environ.get("BACKTEST_USE_ML_FILTER", "true").lower() not in ("1", "true", "yes"),
                         help="Drop the ML p_up condition, keep every other rule-based filter")
    parser.add_argument("--use-ml-filter", dest="no_ml_filter", action="store_false",
                         help="Force the ML p_up condition back on")
    parser.add_argument("--entry-fee-mode", choices=["taker", "maker"], default="taker",
                         help="Fee assumption for entries: 'taker' (market order, 0.05%%) or 'maker' (resting limit order, 0.02%%)")
    parser.add_argument("--exit-fee-mode", choices=["taker", "maker"], default="taker",
                         help="Fee assumption for exits: 'taker' (market order, 0.05%%) or 'maker' (resting limit order, 0.02%%)")
    args = parser.parse_args()

    from_d = args.from_date
    to_d = args.to_date
    if args.year:
        from_d = f"{args.year}-01-01"
        to_d = f"{args.year}-12-31"

    run_crypto_backtest(
        symbols=args.symbols,
        capital=args.capital,
        risk_pct=args.risk_pct,
        leverage=args.leverage,
        long_only=args.long_only,
        from_date=from_d,
        to_date=to_d,
        size_mode=args.size_mode,
        no_ml_filter=args.no_ml_filter,
        entry_fee_mode=args.entry_fee_mode,
        exit_fee_mode=args.exit_fee_mode,
    )


if __name__ == "__main__":
    main()
