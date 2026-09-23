#!/usr/bin/env python3
"""
backend/backtest_equity.py -- 5-Minute NSE Equity Intraday (MIS) Scalping Backtest Engine

Rebuilt 2026-09-19 (see markets/equity/universe.py's docstring for the full
rebuild context -- the original equity scalper was removed 2026-09-18 after
failing to show a real OOS edge on the full NIFTY50, and its own universe
selection was independently found to be circular/survivorship-biased).

Mirrors markets/commodity/scalping/backtest.py/backtest_currency.py's proven walk-forward
structure (same entry-rule shape, same stop/target/breakeven/trailing
mechanics, same statutory-cost-aware simulation loop), pointed at the fixed
NIFTY50 universe instead of MCX commodities or currency pairs.

Deliberate difference from every other asset in this codebase: ONE shared
ENTRY_THRESHOLDS set applies to the ENTIRE universe, not a per-symbol
calibration. Two reasons:
  1. The original removed version tuned/curated per-stock, which is exactly
     what made its universe selection circular (see markets/equity/universe.py).
     A single shared rule set applied uniformly, then evaluated in aggregate
     across 49 stocks, cannot cherry-pick winners after the fact the same way.
  2. It gives a much larger, statistically sturdier sample (thousands of
     trades across the whole universe) than any one MCX/currency symbol ever
     had, which is exactly the kind of sample size this project's own
     discipline (>=15 trades, genuine train/test split) wants.

SECOND deliberate difference, and an important one: trades across the 49
symbols are simulated in TRUE CHRONOLOGICAL order, not symbol-by-symbol.
Every other backtest in this codebase (markets/commodity/scalping/backtest.py,
markets/currency/scalping/backtest.py) processes at most a handful of symbols and runs each
one to completion before moving to the next -- fine at that scale, but for a
49-stock universe that would mean fully simulating RELIANCE's entire
2022-2026 history, THEN TCS's, THEN HDFCBANK's, etc., which makes the
"compounding capital" curve almost entirely an artifact of alphabetical
symbol order rather than real calendar time (a bad losing streak in
whichever symbol happens to be processed first could wipe the account,
regardless of how every other symbol performed on those same real dates).
Caught directly: an initial symbol-sequential version of this file wiped the
account to a suspiciously exact -100.01% -- discarded once this chronological
rewrite was recognized as a real methodological requirement for a
multi-symbol universe test, not an optional nicety. See run_equity_backtest's
docstring for how entries/exits are now interleaved.

No trained ML model exists yet -- rule-based only (p_up held at a neutral
0.50), same honest-first-baseline posture markets/currency/scalping/backtest.py took before
any threshold tuning happened.

Usage:
    python3 -m markets.equity.scalping.backtest --symbols RELIANCE TCS
    python3 -m markets.equity.scalping.backtest                          # full NIFTY50 (49 names) universe
"""
from __future__ import annotations

from core.paths import BACKEND_ROOT
import argparse
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(BACKEND_ROOT))
load_dotenv(dotenv_path=BACKEND_ROOT / ".env")

from markets.equity.costs import compute_nse_equity_costs, size_equity_shares
from markets.equity.features import compute_equity_features
from markets.equity.universe import NIFTY50_SYMBOLS

ARCHIVE_DIR = BACKEND_ROOT / "archive_equity"

HOLD_BARS = 16
TAKE_PROFIT_MULT = 1.80  # kept only as the ENTRY_THRESHOLDS default fallback key -- no longer used as a fixed TP price (see below)
STOP_VOL_MULT = 1.4
BE_ACTIVATION_MULT = 0.60
TRAIL_DIST_MULT = 0.30
BE_LOCK_BUFFER_PCT = 0.0020

# Dynamic exit redesign, 2026-09-19, per explicit user request: TP/SL should
# trail and scale dynamically rather than sit at fixed multiples decided once
# at entry. Two changes from the fixed-TP version above:
#   1. NO fixed take-profit price. Once a trade proves itself (moves in favor
#      by the activation threshold below), it's handed to a trailing exit for
#      the rest of its life -- a strong trend can run well past the old fixed
#      2R cap instead of getting capped there; a trade that reverses right
#      after arming still locks in a real, if smaller, win.
#   2. The activation threshold AND the trailing distance are no longer one
#      constant for every trade -- both are scaled by the ENTRY bar's ADX
#      (how strong the trend looked at the moment of entry), and the
#      trailing distance itself is recomputed from the CURRENT bar's ATR
#      every single bar (not frozen at the entry-time ATR), so it tightens
#      or widens as the stock's own volatility actually changes during the
#      trade, not just once at entry.
# ADX=25 is the "neutral" reference point (scale=1.0, matches the old fixed
# constants exactly); ranges are clipped to keep behavior sane at ADX
# extremes rather than letting a huge ADX spike make the trail absurdly wide.
_ADX_SCALE_REF = 25.0
_ADX_SCALE_MIN = 0.7   # weak-momentum entry -> tighter management (less room to run, arm sooner)
_ADX_SCALE_MAX = 1.8   # strong-trend entry -> more room to run (wider trail, arms later)


def _dynamic_exit_scale(entry_adx: float) -> float:
    return float(np.clip(entry_adx / _ADX_SCALE_REF, _ADX_SCALE_MIN, _ADX_SCALE_MAX))

# ONE shared threshold set across the whole universe -- see module docstring
# for why this is deliberately not per-symbol. Originally seeded from crude's
# commodity default as an unvalidated starting point; validated 2026-09-19
# via a 144-combo sweep + 3-fold OOS check, then min_ema_slope loosened
# 0.22->0.18 on 2026-09-21 after re-validating on the same 3 folds (more
# trades, equal-or-better drawdown on every fold -- see
# markets/equity/scalping/entry_signal.py's ENTRY_THRESHOLDS comment for the full
# story, which is the copy live_dryrun.py actually imports; this one is kept
# in sync by hand so `python3 -m markets.equity.scalping.backtest` with no threshold override
# reflects the same config that's actually live).
ENTRY_THRESHOLDS = {
    "min_adx": 22.0, "min_vol": 1.5, "min_orb": 0.10, "min_vwap": 0.10,
    "min_stop_pct": 0.005, "min_ema_slope": 0.18, "tp_mult": 1.80, "stop_mult": 1.4,
}

_ENTRY_GATE_MIN = 15    # skip first 15 min (09:15-09:30) -- opening volatility, thin ORB sample
_SQUAREOFF_MIN = 360    # 15:15 IST square-off (minutes_since_open from 09:15), ahead of 15:30 close
_MAX_CONCURRENT_POSITIONS = 3  # portfolio-concentration cap -- a real account wouldn't fire all 49 names' signals at once uncapped


def _simulate_symbol_candidates(sym: str, from_date: str | None, to_date: str | None, long_only: bool, et: dict) -> list[dict]:
    """Phase 1: walks one symbol's bars and produces fully-formed candidate
    trades (entry/exit price & time already resolved via TP/SL/timeout/EOD),
    completely independent of capital or position sizing -- share quantity
    doesn't affect WHEN or AT WHAT PRICE a trade would have exited, only how
    much money it made. Sizing happens later, in chronological order across
    ALL symbols together (see run_equity_backtest)."""
    csv_path = ARCHIVE_DIR / f"{sym.upper()}_5minute.csv"
    if not csv_path.exists():
        return []
    raw_df = pd.read_csv(csv_path)
    if len(raw_df) < 50:
        return []
    feat_df = compute_equity_features(raw_df)

    n = len(feat_df)
    closes = feat_df["close"].values
    highs = feat_df["high"].values
    lows = feat_df["low"].values
    vwap_ds = feat_df["vwap_dist_pct"].values
    ema_slopes = feat_df["ema_slope_pct"].values
    mins_open = feat_df["minutes_since_open"].values
    timestamps = feat_df["timestamp"].values
    adxs = feat_df["adx"].values
    dmps = feat_df["dmp"].values
    dmns = feat_df["dmn"].values
    vol_surges = feat_df["vol_surge_ratio"].values
    atrs = feat_df["atr"].values
    orb_h = feat_df["orb_high_dist_pct"].values
    orb_l = feat_df["orb_low_dist_pct"].values

    min_adx, min_vol, min_orb, min_vwap = et["min_adx"], et["min_vol"], et["min_orb"], et["min_vwap"]
    min_stop_pct, min_ema_slope = et["min_stop_pct"], et["min_ema_slope"]
    tp_mult = et.get("tp_mult", TAKE_PROFIT_MULT)
    stop_mult = et.get("stop_mult", STOP_VOL_MULT)

    candidates = []
    in_pos = False
    pos = {}

    for i in range(25, n):
        c_price, c_high, c_low = closes[i], highs[i], lows[i]
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
            cur_atr = atrs[i]  # current-bar ATR, not the entry-time one -- trailing distance reacts to volatility as it evolves

            exit_p = None; reason = None
            if (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
                exit_p = pos["current_stop"]; reason = "trail_stop" if pos["armed_trail"] else "initial_stop"
            elif (i - pos["entry_idx"]) >= HOLD_BARS:
                exit_p = c_price; reason = "timeout_exit"
            elif m_open >= _SQUAREOFF_MIN:
                exit_p = c_price; reason = "eod_squareoff"

            if exit_p is not None:
                candidates.append({
                    "symbol": sym, "direction": pos["direction"],
                    "entry_time": pos["entry_time"], "exit_time": c_time,
                    "entry_price": pos["entry_price"], "exit_price": exit_p,
                    "stop_dist": pos["stop_dist"], "reason": reason,
                })
                in_pos = False
                pos = {}
                continue
            else:
                # Arm the trailing exit once price has proven the trade by moving
                # in favor by the (ADX-scaled) activation threshold -- no fixed
                # take-profit ceiling above this; the trail manages the rest.
                if not pos["armed_trail"] and (fav >= pos["activation_price"] if d == 1 else fav <= pos["activation_price"]):
                    pos["armed_trail"] = True
                    pos["current_stop"] = pos["entry_price"] + BE_LOCK_BUFFER_PCT * pos["entry_price"] * d
                if pos["armed_trail"]:
                    pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
                    trail_dist = pos["trail_mult"] * max(cur_atr, pos["stop_dist"] * 0.1)  # floor so a momentary ATR collapse can't zero out the trail
                    trail = pos["best_price"] - trail_dist * d
                    pos["current_stop"] = (max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail))
            continue

        if m_open < _ENTRY_GATE_MIN or m_open > _SQUAREOFF_MIN:
            continue

        adx, dmp, dmn = adxs[i], dmps[i], dmns[i]
        vol_s, vwap_d, ema_s = vol_surges[i], vwap_ds[i], ema_slopes[i]
        orb_h_dist, orb_l_dist, atr = orb_h[i], orb_l[i], atrs[i]

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

        d = 1 if direction == "long" else -1
        sl = round(c_price - sdist * d, 4)

        # Dynamic risk:reward shape from THIS entry's own ADX -- a stronger
        # trend at entry both needs a smaller proof-of-move before trusting it
        # (arms sooner) AND earns a wider trail once armed (more room to run).
        # See _dynamic_exit_scale's docstring/comment block above.
        exit_scale = _dynamic_exit_scale(adx)
        activation_mult = BE_ACTIVATION_MULT / exit_scale
        trail_mult = TRAIL_DIST_MULT * exit_scale
        activation_price = round(c_price + activation_mult * sdist * d, 4)

        in_pos = True
        pos = {
            "direction": direction, "entry_price": c_price, "entry_idx": i, "entry_time": c_time,
            "sl": sl, "activation_price": activation_price, "current_stop": sl,
            "best_price": c_price, "armed_trail": False, "stop_dist": sdist, "trail_mult": trail_mult,
        }

    return candidates


def run_equity_backtest(
    symbols: list[str] | None = None,
    capital: float = 100000.0,
    risk_pct: float = 5.0,
    leverage: float = 5.0,
    long_only: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
    thresholds: dict | None = None,
    return_trades: bool = False,
) -> dict:
    """Phase 1 generates each symbol's candidate trades independently (see
    _simulate_symbol_candidates). Phase 2 below merges ALL candidates across
    the whole universe by entry_time and walks them in true chronological
    order, maintaining one shared capital pool and up to
    _MAX_CONCURRENT_POSITIONS open positions at once (across different
    symbols) -- a real portfolio holds multiple names simultaneously, it
    doesn't finish trading one stock for four years before touching the
    next. A new candidate is skipped (not deferred) if it would exceed the
    concurrent-position cap or there isn't enough free capital for even one
    share -- both realistic portfolio constraints, not simulation artifacts."""
    target_symbols = symbols or NIFTY50_SYMBOLS
    _et = thresholds or ENTRY_THRESHOLDS

    print(f"\n{'='*75}")
    print(f"  NSE EQUITY 5-MINUTE INTRADAY (MIS) SCALPER WALK-FORWARD BACKTEST")
    print(f"  Capital: Rs{capital:,.0f} | Risk: {risk_pct}% | Leverage: {leverage}x | Long-Only: {long_only}")
    print(f"  Period: {from_date or 'full archive'} to {to_date or 'full archive'}")
    print(f"  Session: NSE equity cash (09:15-15:30 IST, square-off 15:15)")
    print(f"  ML Filter: DISABLED (no trained model exists for equity yet -- rule-based only)")
    print(f"  Entry thresholds: ONE shared set across the whole universe (not per-symbol -- see module docstring)")
    print(f"  Simulation: chronologically interleaved across symbols, max {_MAX_CONCURRENT_POSITIONS} concurrent positions")
    print(f"  Symbols ({len(target_symbols)}): {', '.join(target_symbols)}")
    print(f"{'='*75}\n")

    all_candidates: list[dict] = []
    for sym in target_symbols:
        all_candidates.extend(_simulate_symbol_candidates(sym, from_date, to_date, long_only, _et))

    if not all_candidates:
        print("No trades executed.")
        return {"trades": 0}

    all_candidates.sort(key=lambda t: t["entry_time"])

    current_capital = capital
    committed_margin = 0.0
    open_positions: list[dict] = []  # each: candidate dict + qty + margin
    all_trades: list[dict] = []
    equity_curve = [capital]

    def _close(p: dict) -> None:
        nonlocal current_capital, committed_margin
        cost_info = compute_nse_equity_costs(p["direction"], p["entry_price"], p["exit_price"], p["qty"])
        net_pnl = cost_info["net"]
        current_capital += net_pnl
        committed_margin -= p["margin"]
        equity_curve.append(current_capital)
        all_trades.append({
            "symbol": p["symbol"], "direction": p["direction"],
            "entry_time": p["entry_time"], "exit_time": p["exit_time"],
            "entry_price": p["entry_price"], "exit_price": p["exit_price"],
            "qty": p["qty"], "gross_pnl": cost_info["gross"],
            "total_fees": cost_info["total"], "net_pnl": net_pnl, "reason": p["reason"],
            **cost_info,
        })

    for cand in all_candidates:
        # Close out any open positions whose exit_time has passed as of this candidate's entry -- keeps capital/margin current before sizing the next entry.
        still_open = []
        for p in open_positions:
            if p["exit_time"] <= cand["entry_time"]:
                _close(p)
            else:
                still_open.append(p)
        open_positions = still_open

        if len(open_positions) >= _MAX_CONCURRENT_POSITIONS:
            continue

        available = current_capital - committed_margin
        if available <= 0:
            continue

        qty = size_equity_shares(capital=available, entry_price=cand["entry_price"], stop_distance=cand["stop_dist"], risk_pct=risk_pct, leverage=leverage)
        if qty < 1:
            continue
        margin = (cand["entry_price"] * qty) / max(leverage, 1.0)
        if margin > available:
            continue

        open_positions.append({**cand, "qty": qty, "margin": margin})
        committed_margin += margin

    # Close anything still open at the very end (backtest boundary, not a real EOD -- negligible at 4 years of data).
    for p in sorted(open_positions, key=lambda p: p["exit_time"]):
        _close(p)

    total_trades = len(all_trades)
    if total_trades == 0:
        print("No trades executed.")
        return {"trades": 0}

    wins = [t for t in all_trades if t["net_pnl"] > 0]
    losses = [t for t in all_trades if t["net_pnl"] <= 0]
    win_rate = len(wins) / total_trades * 100

    gross_sum = sum(t["gross_pnl"] for t in all_trades)
    net_pnl_sum = sum(t["net_pnl"] for t in all_trades)
    pct_return = (net_pnl_sum / capital) * 100

    gross_wins = sum(t["gross_pnl"] for t in wins)
    gross_losses = abs(sum(t["gross_pnl"] for t in losses))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 99.0

    eq = np.array(equity_curve)
    peaks = np.maximum.accumulate(eq)
    drawdowns = (peaks - eq) / peaks * 100
    max_dd = np.max(drawdowns) if len(drawdowns) > 0 else 0.0

    print(f"Executed Trades: {total_trades:,} ({len(wins)}W / {len(losses)}L)   Overall Win Rate: {win_rate:.1f}%")
    print(f"Profit Factor: {profit_factor:.3f}                 Max Drawdown: -{max_dd:.2f}%")
    print(f"Gross PnL: Rs{gross_sum:,.2f}   Net PnL: Rs{net_pnl_sum:,.2f} ({pct_return:+.2f}%)")

    result = {
        "trades": total_trades, "win_rate": win_rate, "profit_factor": profit_factor,
        "net_pnl": net_pnl_sum, "max_dd": max_dd, "gross_pnl": gross_sum,
    }
    if return_trades:
        result["trade_list"] = all_trades
    return result


def main():
    parser = argparse.ArgumentParser(description="NSE Equity Intraday (MIS) 5-min scalper backtest")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--risk-pct", type=float, default=5.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--long-only", action="store_true")
    parser.add_argument("--from", "--from-date", dest="from_date", default=None)
    parser.add_argument("--to", "--to-date", dest="to_date", default=None)
    args = parser.parse_args()
    run_equity_backtest(
        symbols=args.symbols, capital=args.capital, risk_pct=args.risk_pct, leverage=args.leverage,
        long_only=args.long_only, from_date=args.from_date, to_date=args.to_date,
    )


if __name__ == "__main__":
    main()
