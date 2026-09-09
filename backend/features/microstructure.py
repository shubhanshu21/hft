"""
features/microstructure.py — Quantitative Microstructure, Parkinson Volatility & Order Flow Features.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def compute_microstructure_features(df: pd.DataFrame, orb_bars: int = 6) -> pd.DataFrame:
    """
    Computes microstructure price location, volume surges, Parkinson volatility, and ORB levels.
    """
    out = df.copy()
    if "timestamp" not in out.columns and "date" in out.columns:
        out["timestamp"] = out["date"]

    # Bar internal price location [0.0 = low, 1.0 = high]
    bar_range = (out["high"] - out["low"]).replace(0, np.nan)
    out["close_loc_in_bar"] = ((out["close"] - out["low"]) / bar_range).fillna(0.5)

    # Bar Direction
    out["bar_direction"] = np.where(out["close"] > out["open"], 1.0, np.where(out["close"] < out["open"], -1.0, 0.0))

    # Parkinson Volatility (High-Low volatility estimator)
    ratio = np.log(out["high"].replace(0, np.nan) / out["low"].replace(0, np.nan)).fillna(0.0)
    out["parkinson_vol"] = np.sqrt((ratio ** 2) / (4.0 * np.log(2.0))) * 100.0

    # Volume Surge Ratio (relative to 20-bar rolling average)
    vol_roll = out["volume"].rolling(20, min_periods=1).mean()
    out["vol_surge_ratio"] = (out["volume"] / vol_roll.replace(0, np.nan)).fillna(1.0)

    # Intraday Opening Range Breakout (ORB) levels (First N bars of session)
    if "timestamp" in out.columns:
        dt_s = pd.to_datetime(out["timestamp"])
        out["_date"] = dt_s.dt.date

        def _calc_orb(group: pd.DataFrame) -> pd.DataFrame:
            n_orb = min(orb_bars, len(group))
            orb_h = group["high"].iloc[:n_orb].max() if n_orb > 0 else group["high"].iloc[0]
            orb_l = group["low"].iloc[:n_orb].min() if n_orb > 0 else group["low"].iloc[0]
            group["orb_high"] = orb_h
            group["orb_low"] = orb_l
            group["orb_high_dist_pct"] = (group["close"] - orb_h) / orb_h * 100.0
            group["orb_low_dist_pct"] = (group["close"] - orb_l) / orb_l * 100.0
            return group

        out = out.groupby("_date", group_keys=False).apply(_calc_orb)
        out.drop(columns=["_date"], inplace=True, errors="ignore")
    else:
        out["orb_high_dist_pct"] = 0.0
        out["orb_low_dist_pct"] = 0.0

    return out
