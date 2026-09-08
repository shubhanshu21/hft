"""
features/options_features.py — Options Specific Features (PCR, IV Rank, Max Pain, OI Surges).
"""
from __future__ import annotations

import pandas as pd
import numpy as np


def compute_iv_rank_percentile(current_iv: float, historical_ivs: list[float] | pd.Series) -> tuple[float, float]:
    """
    Computes IV Rank and IV Percentile given historical IV values.
    Returns (iv_rank, iv_percentile) in range [0, 100].
    """
    s = pd.Series(historical_ivs).dropna()
    if len(s) == 0:
        return 50.0, 50.0

    min_iv = s.min()
    max_iv = s.max()

    iv_rank = ((current_iv - min_iv) / (max_iv - min_iv) * 100.0) if max_iv > min_iv else 50.0
    iv_percentile = (s < current_iv).mean() * 100.0

    return max(0.0, min(100.0, float(iv_rank))), max(0.0, min(100.0, float(iv_percentile)))


def compute_option_chain_features(
    option_chain_df: pd.DataFrame,
) -> dict[str, float]:
    """
    Computes summary market-level metrics from a full option chain snapshot DataFrame.
    Expected columns: ["strike", "option_type", "oi", "volume", "iv", "ltp"]
    """
    if option_chain_df.empty:
        return {"pcr_oi": 1.0, "pcr_volume": 1.0, "avg_iv": 20.0, "call_oi": 0.0, "put_oi": 0.0}

    calls = option_chain_df[option_chain_df["option_type"].isin(["CE", "CALL"])]
    puts = option_chain_df[option_chain_df["option_type"].isin(["PE", "PUT"])]

    call_oi = calls["oi"].sum()
    put_oi = puts["oi"].sum()
    call_vol = calls["volume"].sum()
    put_vol = puts["volume"].sum()

    pcr_oi = (put_oi / call_oi) if call_oi > 0 else 1.0
    pcr_vol = (put_vol / call_vol) if call_vol > 0 else 1.0
    avg_iv = option_chain_df["iv"].mean() if "iv" in option_chain_df.columns else 20.0

    return {
        "pcr_oi": round(float(pcr_oi), 3),
        "pcr_volume": round(float(pcr_vol), 3),
        "call_oi": float(call_oi),
        "put_oi": float(put_oi),
        "avg_iv": round(float(avg_iv), 2),
    }
