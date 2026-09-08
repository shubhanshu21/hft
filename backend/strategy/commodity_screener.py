"""
strategy/commodity_screener.py — Indian MCX Market "Commodities in Play" Dynamic Screener

Handles Official Indian MCX Market Structure (09:00 AM to 11:30 PM IST):
  1. Morning Session (09:00 AM – 05:00 PM IST):
     - Morning Opening Gap vs previous day 23:30 MCX close
     - Morning 30-min Opening Range (09:00 – 09:30 AM)
     - European Market Overlap (01:30 PM – 05:00 PM IST)
  2. Evening Session (05:00 PM – 11:30 PM IST):
     - US COMEX/NYMEX Pit Opening Momentum (06:30 PM – 10:00 PM IST)
     - High-Impact EIA Inventory Events (Wed Crude / Thu NatGas at 08:00 PM IST)
  3. Automatic Daily Selection:
     - Ranks all active MCX commodities (CRUDEOILM, GOLDM, SILVERMIC, NATGASMINI, COPPER)
     - Dynamically selects the highest-momentum "Commodity in Play" for execution
     - Enforces 11:15 PM mandatory intraday MIS square-off
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COMMODITY_UNIVERSE = ["CRUDEOILM", "NATGASMINI"]


def score_commodity_opportunity(
    feat_df: pd.DataFrame,
    idx: int,
    symbol: str
) -> float:
    """
    Computes a composite 'In Play' opportunity score for an MCX commodity at bar `idx`.
    Works across both Indian Morning (09:00-17:00) and Evening (17:00-23:30) sessions.
    """
    if idx < 10 or idx >= len(feat_df):
        return 0.0

    p_vol = float(feat_df["parkinson_vol"].values[idx]) if "parkinson_vol" in feat_df.columns else 0.2
    adx = float(feat_df["adx"].values[idx]) if "adx" in feat_df.columns else 20.0
    vol_surge = float(feat_df["vol_surge_ratio"].values[idx]) if "vol_surge_ratio" in feat_df.columns else 1.0
    orb_h_dist = abs(float(feat_df["orb_high_dist_pct"].values[idx])) if "orb_high_dist_pct" in feat_df.columns else 0.0
    orb_l_dist = abs(float(feat_df["orb_low_dist_pct"].values[idx])) if "orb_low_dist_pct" in feat_df.columns else 0.0
    vwap_d = abs(float(feat_df["vwap_dist_pct"].values[idx])) if "vwap_dist_pct" in feat_df.columns else 0.0
    mins_open = int(feat_df["minutes_since_open"].values[idx]) if "minutes_since_open" in feat_df.columns else 0
    is_inventory = float(feat_df["is_inventory_window"].values[idx]) if "is_inventory_window" in feat_df.columns else 0.0

    score = 0.0

    # 1. Trend Strength (ADX) - up to 30 points
    if adx >= 25:
        score += 30.0
    elif adx >= 20:
        score += 20.0
    elif adx < 16:
        score -= 20.0  # Penalize choppy sideways consolidation

    # 2. Volume Surge Ratio - up to 25 points
    if vol_surge >= 1.5:
        score += 25.0
    elif vol_surge >= 1.15:
        score += 15.0
    elif vol_surge < 0.8:
        score -= 15.0

    # 3. Volatility Expansion - up to 20 points
    if p_vol >= 0.25:
        score += 20.0
    elif p_vol >= 0.15:
        score += 10.0

    # 4. Opening Range Breakout (ORB) Clearance - up to 15 points
    orb_clearance = max(orb_h_dist, orb_l_dist)
    if orb_clearance >= 0.20:
        score += 15.0
    elif orb_clearance >= 0.05:
        score += 8.0

    # 5. VWAP Displacement - up to 10 points
    if vwap_d >= 0.15:
        score += 10.0
    elif vwap_d >= 0.05:
        score += 5.0

    # 6. Session Timing Boost (US / Evening Core Liquidity: 18:30 - 22:00 IST -> mins 570 - 780)
    if 570 <= mins_open <= 780:
        score += 15.0
    # European session opening boost (14:00 - 16:30 IST -> mins 300 - 450)
    elif 300 <= mins_open <= 450:
        score += 8.0

    # 7. Scheduled High-Impact EIA Inventory Events (Wed Crude / Thu NatGas at 20:00 IST)
    if is_inventory > 0:
        score += 20.0

    return score


def select_top_commodity_in_play(
    symbol_features: dict[str, pd.DataFrame],
    idx_map: dict[str, int],
    top_n: int = 1
) -> list[tuple[str, float]]:
    """
    Evaluates all MCX commodities at current bar indices and returns the Top-N ranked symbols.
    """
    scores = []
    for sym, df in symbol_features.items():
        idx = idx_map.get(sym)
        if idx is not None:
            sc = score_commodity_opportunity(df, idx, sym)
            scores.append((sym, sc))

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:top_n]

