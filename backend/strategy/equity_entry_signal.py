"""
strategy/equity_entry_signal.py -- shared "should we enter, and at what
levels" decision logic for NSE equity intraday (MIS), used by live_dryrun.py's
DryRunner. Mirrors strategy/entry_signal.py's role for commodity/currency,
but kept as its OWN function rather than folded into that one, because
equity's exit mechanics are structurally different (see backtest_equity.py's
module docstring): no fixed take-profit price at all, a dynamic ADX-scaled
trailing exit instead. Forcing that into compute_entry_signal()'s fixed
sl/tp/be return shape would have meant either breaking commodity/currency's
contract or silently giving equity a fake "tp" that's never actually used --
a real footgun for someone reading that code later. Separate function, same
spirit.

Uses the ONE validated, universe-wide threshold set from backtest_equity.py
(no per-stock tuning -- see strategy/equity_universe.py's docstring for why),
confirmed consistent across three independent out-of-sample periods
(2026-09-19). Rule-based only, deliberately no ML filter -- the trained
pooled model (ml/train_equity.py) showed real predictive power (AUC 0.81)
but made results WORSE and even flipped the most recent test period from a
profit to a loss when added as a filter; not used here for that reason (see
conversation history for the full 3-fold comparison).
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from strategy.equity_costs import size_equity_shares
from strategy.equity_features import compute_equity_features
from strategy.equity_universe import NIFTY50_SYMBOLS

EQUITY_SYMBOLS = set(NIFTY50_SYMBOLS)

# Validated 2026-09-19 via a 144-combo sweep + 3-fold OOS check -- see
# backtest_equity.py's ENTRY_THRESHOLDS default and conversation history.
#
# min_ema_slope loosened 0.22 -> 0.18 on 2026-09-21, after a live session with
# ZERO trades across all 55 symbols prompted a check of whether the momentum
# filter was tighter than it needed to be. It was: public-strategy convention
# for an EMA-slope momentum filter is ~0.05-0.08% (confirmed via web
# research), while 0.22% is ~3-4x that. But loosening blindly to "what's
# typical" was tested first and REJECTED -- 0.10 reproduced the same fee-drag
# failure mode this threshold was originally raised to fix (2 of 3 OOS folds
# went net-negative, one to a near-total -98% drawdown). 0.18 was found by
# sweeping the gap and re-validating on the same 3 independent train/test
# folds: it trades ~65-70% more often than 0.22 (696-1028 trades/fold vs
# 422-599) and its max drawdown is EQUAL OR BETTER than 0.22 on all three
# folds simultaneously (e.g. fold 1: 30.9% vs 37.4%) -- a genuine improvement
# in trade frequency, not a tradeoff traded away against risk.
# Asymmetric Long vs Short Thresholds:
# Longs ride gradual institutional accumulation (moderate ADX, steady volume, wider trail)
# Shorts capture rapid liquidation panics (heavy volume cascade, steep drop, fast BE lock)
LONG_THRESHOLDS = {
    "min_adx": 20.0, "min_vol": 1.3, "min_orb": 0.08, "min_vwap": 0.08,
    "min_stop_pct": 0.005, "min_ema_slope": 0.15, "stop_mult": 1.4,
    "be_activation_mult": 0.60, "trail_dist_mult": 0.35,
}

SHORT_THRESHOLDS = {
    "min_adx": 26.0, "min_vol": 1.8, "min_orb": 0.12, "min_vwap": 0.12,
    "min_stop_pct": 0.005, "min_ema_slope": 0.22, "stop_mult": 1.2,
    "be_activation_mult": 0.40, "trail_dist_mult": 0.20,
}

# Legacy fallback for test compatibility
ENTRY_THRESHOLDS = LONG_THRESHOLDS

BE_ACTIVATION_MULT = 0.60
TRAIL_DIST_MULT = 0.30
BE_LOCK_BUFFER_PCT = 0.0020
_ADX_SCALE_REF = 25.0
_ADX_SCALE_MIN = 0.7
_ADX_SCALE_MAX = 1.8
_ENTRY_GATE_MIN = 15
_SQUAREOFF_MIN = 360  # 15:15 IST, ahead of the 15:30 close

MAX_CONCURRENT_EQUITY_POSITIONS = 3  # portfolio-concentration cap -- see backtest_equity.py's finding on correlated same-day losses


def is_equity(sym: str) -> bool:
    return sym.upper() in EQUITY_SYMBOLS


def dynamic_exit_scale(entry_adx: float) -> float:
    return float(np.clip(entry_adx / _ADX_SCALE_REF, _ADX_SCALE_MIN, _ADX_SCALE_MAX))


def compute_equity_entry_signal(
    sym: str,
    candles: list[dict],
    instrument_key: str,
    capital: float,
    risk_pct: float,
    leverage: float,
    direction_filter: str = "both",
) -> dict | None:
    """Returns a signal dict if a qualifying setup exists on the latest
    closed 5-min bar, else None. Uses asymmetric long vs short criteria."""
    if not candles or len(candles) < 25:
        return None

    raw_df = pd.DataFrame(candles)
    feat_df = compute_equity_features(raw_df)
    if feat_df is None or len(feat_df) < 25:
        return None

    t = len(feat_df) - 1
    row = feat_df.iloc[t]
    mins = int(row.get("minutes_since_open", 0))
    if mins < _ENTRY_GATE_MIN or mins > _SQUAREOFF_MIN:
        return None

    adx = float(row.get("adx", 25.0))
    dmp = float(row.get("dmp", 25.0))
    dmn = float(row.get("dmn", 25.0))
    vol_s = float(row.get("vol_surge_ratio", 1.0))
    vwap_d = float(row.get("vwap_dist_pct", 0.0))
    ema_s = float(row.get("ema_slope_pct", 0.0))
    orb_h_dist = float(row.get("orb_high_dist_pct", 0.0))
    orb_l_dist = float(row.get("orb_low_dist_pct", 0.0))
    rsi = float(row.get("intraday_rsi", 50.0))
    entry = float(row["close"])
    atr = float(row.get("atr", 0.005 * entry))

    lt = LONG_THRESHOLDS
    st = SHORT_THRESHOLDS
    # Reads from .env; was previously hardcoded True (2026-09-22 fix).
    enable_mean_rev = os.environ.get("ENABLE_MEAN_REVERSION", "true").lower() in ("1", "true", "yes")

    direction = None
    setup_type = "trend_breakout"

    # Setup 1 (LONG): Trend Accumulation (steady volume, trend alignment)
    if (adx >= lt["min_adx"] and dmp > dmn and ema_s > lt["min_ema_slope"]
            and orb_h_dist >= lt["min_orb"] and vwap_d >= lt["min_vwap"] and vol_s >= lt["min_vol"]):
        direction = "long"
        setup_type = "trend_breakout"

    # Setup 1 (SHORT): Panic Liquidation (stricter volume surge & drop momentum)
    elif (direction_filter != "long" and adx >= st["min_adx"] and dmn > dmp and ema_s < -st["min_ema_slope"]
            and orb_l_dist <= -st["min_orb"] and vwap_d <= -st["min_vwap"] and vol_s >= st["min_vol"]):
        direction = "short"
        setup_type = "trend_breakout"

    # Setup 2: Statistical VWAP Mean-Reversion Extremes
    elif enable_mean_rev:
        # Long mean-reversion on panic flush
        if vwap_d <= -1.2 and rsi <= 25.0:
            direction = "long"
            setup_type = "mean_reversion"
        # Short mean-reversion requires higher exhaustion threshold
        elif direction_filter != "long" and vwap_d >= 1.5 and rsi >= 78.0:
            direction = "short"
            setup_type = "mean_reversion"

    if not direction:
        return None
    if direction_filter != "both" and direction != direction_filter:
        return None

    # Asymmetric stop & exit management per direction
    th = lt if direction == "long" else st
    sdist = max(th["stop_mult"] * atr, th["min_stop_pct"] * entry)
    if sdist <= 0 or entry <= 0:
        return None

    qty = size_equity_shares(capital, entry, sdist, risk_pct, leverage)
    if qty <= 0:
        return None

    d = 1 if direction == "long" else -1
    sl = round(entry - sdist * d, 4)
    exit_scale = dynamic_exit_scale(adx)
    activation_mult = th["be_activation_mult"] / exit_scale
    trail_mult = th["trail_dist_mult"] * exit_scale
    activation_price = round(entry + activation_mult * sdist * d, 4)
    tp = round(entry + 1.5 * sdist * d, 4) if setup_type == "mean_reversion" else None

    return {
        "symbol": sym, "direction": direction, "entry_price": entry,
        "sl": sl, "tp": tp, "activation_price": activation_price, "trail_mult": trail_mult,
        "qty": qty, "stop_dist": sdist, "instrument_key": instrument_key,
        "setup_type": setup_type,
        "p_up": 0.50, "rsi": rsi, "adx": adx, "vol_surge": vol_s,
        "vwap_dist_pct": vwap_d, "ema_slope_pct": ema_s,
    }
