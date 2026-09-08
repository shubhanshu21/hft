"""
strategy/commodity_features.py — Feature Engineering Tailored for 5-Min MCX Commodities

Features engineered specifically for commodity trading dynamics:
  1. Session Regime: Asian (09:00-14:00), European (14:00-18:30), US Core (18:30-22:30), Late (22:30-23:30).
  2. US Session Breakout & Open Proximity (relative to 18:30/19:00 IST NYMEX open).
  3. High-Impact Inventory Events: Wednesday Crude Oil (20:00 IST) & Thursday NatGas (20:00 IST).
  4. Directional Volume Flow, Parkinson Volatility, Fast RSI(8), Multi-bar EMA & VWAP Slopes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

COMMODITY_FEATURE_COLUMNS = [
    "mom_fast", "mom_med", "mom_slow", "intraday_rsi", "vwap_dist_pct", "vwap_slope",
    "parkinson_vol", "avg_range_pct", "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    "close_loc_in_bar", "bar_direction", "bb_bandwidth", "htf_trend_slope", "is_near_vwap",
    "vol_surge_ratio", "session_phase", "minutes_since_open",
    "minutes_since_us_open", "is_us_session", "is_inventory_window", "day_of_week",
    "orb_high_dist_pct", "orb_low_dist_pct", "orb_breakout_up", "orb_breakout_dn",
    "ema_slope_pct", "is_ema_bullish", "adx", "dmp", "dmn", "atr_pct"
]



def compute_commodity_features(df: pd.DataFrame, symbol: str = "CRUDEOIL") -> pd.DataFrame:
    """
    Computes causality-preserving 5-minute rolling features for MCX commodities.
    `df` contains [timestamp, open, high, low, close, volume].
    """
    df = df.copy()
    if "timestamp" in df.columns:
        df["_dt"] = pd.to_datetime(df["timestamp"])
    else:
        df["_dt"] = pd.to_datetime(df["date"])

    df["hour"] = df["_dt"].dt.hour
    df["minute"] = df["_dt"].dt.minute
    df["day_of_week"] = df["_dt"].dt.dayofweek
    df["day"] = df["_dt"].dt.date
    df["minutes_since_open"] = (df["hour"] * 60 + df["minute"]) - (9 * 60) # Minutes since 09:00 open

    # 1. Session Phase Categorization
    # 0: Asian (09:00-14:00), 1: Europe (14:00-18:30), 2: US Core (18:30-22:30), 3: Late (22:30-23:30)
    mins = df["hour"] * 60 + df["minute"]
    us_open_min = 18 * 60 + 30  # 18:30 IST

    session_phase = np.where(
        mins < 14 * 60, 0,
        np.where(
            mins < us_open_min, 1,
            np.where(mins <= 22 * 60 + 30, 2, 3)
        )
    )
    df["session_phase"] = session_phase
    df["is_us_session"] = np.where((mins >= us_open_min) & (mins <= 22 * 60 + 30), 1.0, 0.0)
    df["minutes_since_us_open"] = np.maximum(0, mins - us_open_min)

    # 2. Weekly High-Impact Inventory Window (Wednesday Crude / Thursday NatGas at 20:00-20:30 IST)
    is_crude_eia = (df["day_of_week"] == 2) & (mins >= 19 * 60 + 45) & (mins <= 20 * 60 + 45) & (symbol.startswith("CRUDE"))
    is_natgas_eia = (df["day_of_week"] == 3) & (mins >= 19 * 60 + 45) & (mins <= 20 * 60 + 45) & (symbol.startswith("NAT"))
    df["is_inventory_window"] = np.where(is_crude_eia | is_natgas_eia, 1.0, 0.0)

    # 3. Momentum over multiple lookbacks (4 bars = 20m, 12 bars = 60m, 24 bars = 2h)
    df["mom_fast"] = df["close"].pct_change(4).fillna(0.0) * 100
    df["mom_med"]  = df["close"].pct_change(12).fillna(0.0) * 100
    df["mom_slow"] = df["close"].pct_change(24).fillna(0.0) * 100

    # 4. Volatility, Range, & Parkinson Estimator
    hl_range = np.maximum(df["high"] - df["low"], 1e-4)
    df["range_pct"] = hl_range / df["close"] * 100
    df["avg_range_pct"] = df["range_pct"].rolling(8).mean().fillna(0.3)
    log_hl = np.log(np.maximum(df["high"] / np.maximum(df["low"], 1e-6), 1.0))
    df["parkinson_vol"] = (np.sqrt((log_hl ** 2).rolling(8).mean() / (4 * np.log(2))) * 100).fillna(0.3)

    # 5. Fast Intraday RSI (8 periods)
    df["intraday_rsi"] = ta.rsi(df["close"], length=8).fillna(50.0)

    # 6. Trend Strength (ADX, +DI, -DI, ATR)
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx_df is not None:
        df["adx"] = adx_df.iloc[:, 0].fillna(25.0)
        df["dmp"] = adx_df.iloc[:, 1].fillna(25.0)
        df["dmn"] = adx_df.iloc[:, 2].fillna(25.0)
    else:
        df["adx"] = 25.0; df["dmp"] = 25.0; df["dmn"] = 25.0

    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14).fillna(hl_range)
    df["atr_pct"] = (df["atr"] / df["close"] * 100).fillna(0.4)

    # 7. Price Action & Candle Microstructure
    df["body_ratio"] = np.abs(df["close"] - df["open"]) / hl_range
    df["upper_wick_ratio"] = (df["high"] - np.maximum(df["open"], df["close"])) / hl_range
    df["lower_wick_ratio"] = (np.minimum(df["open"], df["close"]) - df["low"]) / hl_range
    df["close_loc_in_bar"] = (df["close"] - df["low"]) / hl_range
    df["bar_direction"] = (df["close"] - df["open"]) / hl_range

    # 8. Volume Flow & Surge Ratio
    vol_avg = df["volume"].rolling(8).mean()
    df["vol_surge_ratio"] = np.where(vol_avg > 0, df["volume"] / vol_avg, 1.0)

    # 9. Daily Reset VWAP & Elasticity
    df["vol_close"] = df["volume"] * df["close"]
    cum_vol = df.groupby("day")["volume"].cumsum()
    cum_pv = df.groupby("day")["vol_close"].cumsum()
    df["vwap"] = np.where(cum_vol > 0, cum_pv / cum_vol, df["close"])
    df["vwap_dist_pct"] = np.where(df["vwap"] > 0, (df["close"] - df["vwap"]) / df["vwap"] * 100, 0.0)
    df["vwap_slope"] = df["vwap"].pct_change(3).fillna(0.0) * 100
    df["is_near_vwap"] = np.where(np.abs(df["vwap_dist_pct"]) <= 0.45, 1.0, 0.0)

    # 10. Fast & Slow EMAs + Higher Timeframe (1-hour) Trend
    df["ema_fast"] = ta.ema(df["close"], length=9).fillna(df["close"])
    df["ema_slow"] = ta.ema(df["close"], length=21).fillna(df["close"])
    df["ema_slope_pct"] = df["ema_fast"].pct_change().fillna(0.0) * 100
    df["is_ema_bullish"] = np.where(df["ema_fast"] > df["ema_slow"], 1.0, 0.0)
    df["htf_trend_slope"] = ta.ema(df["close"], length=45).pct_change(3).fillna(0.0) * 100

    # 11. Bollinger Bandwidth (Volatility Expansion)
    bb = ta.bbands(df["close"], length=20, std=2.0)
    if bb is not None:
        bbu = bb.iloc[:, 0]
        bbm = bb.iloc[:, 1]
        bbl = bb.iloc[:, 2]
        bw = np.where(bbm > 0, (bbu - bbl) / bbm * 100, 1.0)
        df["bb_bandwidth"] = np.nan_to_num(bw, nan=1.0)
    else:
        df["bb_bandwidth"] = 1.0

    # 12. Daily Opening Range Breakout (ORB: First 60 mins -> minutes 0 to 60)
    is_orb_bar = df["minutes_since_open"] <= 60
    orb_high_cum = df["high"].where(is_orb_bar).groupby(df["day"]).cummax().ffill()
    orb_low_cum  = df["low"].where(is_orb_bar).groupby(df["day"]).cummin().ffill()

    df["orb_high_dist_pct"] = np.where(orb_high_cum > 0, (df["close"] - orb_high_cum) / orb_high_cum * 100, 0.0)
    df["orb_low_dist_pct"]  = np.where(orb_low_cum > 0, (df["close"] - orb_low_cum) / orb_low_cum * 100, 0.0)
    df["orb_breakout_up"] = np.where((df["close"] > orb_high_cum) & (~is_orb_bar), 1.0, 0.0)
    df["orb_breakout_dn"] = np.where((df["close"] < orb_low_cum) & (~is_orb_bar), 1.0, 0.0)

    return df.fillna(0.0)

