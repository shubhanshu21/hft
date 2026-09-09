"""
features/technical.py — Parametric Technical Indicators Engine (EMA, VWAP, RSI, ADX, ATR, Bands).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta


def compute_technical_indicators(
    df: pd.DataFrame,
    ema_fast_length: int = 9,
    ema_slow_length: int = 21,
    rsi_length: int = 14,
    adx_length: int = 14,
    atr_length: int = 14,
    bb_length: int = 20,
    bb_std: float = 2.0,
) -> pd.DataFrame:
    """
    Computes standard parametric technical indicators on OHLCV bar data.
    """
    out = df.copy()
    if "timestamp" not in out.columns and "date" in out.columns:
        out["timestamp"] = out["date"]

    # EMA
    out["ema_fast"] = ta.ema(out["close"], length=ema_fast_length)
    out["ema_slow"] = ta.ema(out["close"], length=ema_slow_length)
    out["ema_slope_pct"] = out["ema_fast"].pct_change() * 100.0

    # RSI
    out["rsi"] = ta.rsi(out["close"], length=rsi_length)
    out["intraday_rsi"] = out["rsi"].fillna(50.0)

    # ADX & DMI
    try:
        adx_df = ta.adx(out["high"], out["low"], out["close"], length=adx_length)
        if adx_df is not None:
            out["adx"] = adx_df[f"ADX_{adx_length}"].fillna(25.0)
            out["dmp"] = adx_df[f"DMP_{adx_length}"].fillna(25.0)
            out["dmn"] = adx_df[f"DMN_{adx_length}"].fillna(25.0)
        else:
            out["adx"], out["dmp"], out["dmn"] = 25.0, 25.0, 25.0
    except Exception:
        out["adx"], out["dmp"], out["dmn"] = 25.0, 25.0, 25.0

    # ATR
    try:
        atr_s = ta.atr(out["high"], out["low"], out["close"], length=atr_length)
        out["atr"] = atr_s.bfill().ffill() if atr_s is not None else (out["high"] - out["low"]).rolling(atr_length).mean()
    except Exception:
        out["atr"] = (out["high"] - out["low"]).rolling(atr_length).mean()

    # Bollinger Bands
    try:
        bb = ta.bbands(out["close"], length=bb_length, std=bb_std)
        if bb is not None:
            out["bb_upper"] = bb[f"BBU_{bb_length}_{bb_std}"]
            out["bb_lower"] = bb[f"BBL_{bb_length}_{bb_std}"]
            out["bb_mid"] = bb[f"BBM_{bb_length}_{bb_std}"]
            out["bb_bandwidth"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_mid"] * 100.0
        else:
            out["bb_bandwidth"] = 1.0
    except Exception:
        out["bb_bandwidth"] = 1.0

    # Intraday VWAP
    if "timestamp" in out.columns:
        dt_s = pd.to_datetime(out["timestamp"])
        out["_date"] = dt_s.dt.date
        if out["volume"].sum() > 0:
            out["cum_vol"] = out.groupby("_date")["volume"].cumsum()
            out["cum_pv"] = out.groupby("_date").apply(lambda g: (g["close"] * g["volume"]).cumsum()).reset_index(level=0, drop=True)
            out["vwap"] = (out["cum_pv"] / out["cum_vol"]).fillna(out["close"])
            out.drop(columns=["cum_vol", "cum_pv"], inplace=True, errors="ignore")
        else:
            # No real traded volume (e.g. index series like NIFTY 50 /
            # NIFTY BANK) — a volume-weighted VWAP degenerates to close.
            # Fall back to the cumulative intraday average price instead.
            day_pos = out.groupby("_date").cumcount() + 1
            out["vwap"] = out.groupby("_date")["close"].cumsum() / day_pos
        out["vwap_dist_pct"] = (out["close"] - out["vwap"]) / out["vwap"] * 100.0
        out.drop(columns=["_date"], inplace=True, errors="ignore")
    else:
        out["vwap"] = out["close"]
        out["vwap_dist_pct"] = 0.0

    return out
