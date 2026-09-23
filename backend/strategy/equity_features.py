"""
strategy/equity_features.py -- Feature engineering for 5-min NSE equity intraday (MIS).

Rebuilt 2026-09-19 (see strategy/equity_universe.py's docstring for the
rebuild context). Reuses the same core technical-indicator shape as
strategy/commodity_features.py (momentum, RSI, ADX/DMI/ATR, VWAP, EMA slope,
ORB, volume surge, candle microstructure) since those are generic
price-action constructs, not MCX-specific -- but drops commodity_features.py's
MCX-only constructs entirely (session_phase Asian/Europe/US-core buckets,
US-open proximity, Wed/Thu EIA inventory windows) since NSE cash equity has
none of those: one continuous 09:15-15:30 IST session, no overnight/global
session handoff.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

EQUITY_SESSION_OPEN_MINUTES = 9 * 60 + 15   # 09:15 IST
EQUITY_SESSION_CLOSE_MINUTES = 15 * 60 + 30  # 15:30 IST

EQUITY_FEATURE_COLUMNS = [
    "mom_fast", "mom_med", "mom_slow", "intraday_rsi", "vwap_dist_pct", "vwap_slope",
    "parkinson_vol", "avg_range_pct", "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    "close_loc_in_bar", "bar_direction", "bb_bandwidth", "htf_trend_slope", "is_near_vwap",
    "vol_surge_ratio", "minutes_since_open", "day_of_week",
    "orb_high_dist_pct", "orb_low_dist_pct", "orb_breakout_up", "orb_breakout_dn",
    "ema_slope_pct", "is_ema_bullish", "adx", "dmp", "dmn", "atr_pct",
]


def compute_equity_features(df: pd.DataFrame) -> pd.DataFrame:
    """`df` contains [timestamp, open, high, low, close, volume]. Every
    rolling/cumulative feature is computed causally (no lookahead) and reset
    per calendar day (no blending across the overnight gap)."""
    df = df.copy()
    df["_dt"] = pd.to_datetime(df["timestamp"] if "timestamp" in df.columns else df["date"])
    df["hour"] = df["_dt"].dt.hour
    df["minute"] = df["_dt"].dt.minute
    df["day_of_week"] = df["_dt"].dt.dayofweek
    df["day"] = df["_dt"].dt.date
    df["minutes_since_open"] = (df["hour"] * 60 + df["minute"]) - EQUITY_SESSION_OPEN_MINUTES

    df["mom_fast"] = df["close"].pct_change(4).fillna(0.0) * 100
    df["mom_med"] = df["close"].pct_change(12).fillna(0.0) * 100
    df["mom_slow"] = df["close"].pct_change(24).fillna(0.0) * 100

    hl_range = np.maximum(df["high"] - df["low"], 1e-4)
    df["range_pct"] = hl_range / df["close"] * 100
    df["avg_range_pct"] = df["range_pct"].rolling(8).mean().fillna(0.3)
    log_hl = np.log(np.maximum(df["high"] / np.maximum(df["low"], 1e-6), 1.0))
    df["parkinson_vol"] = (np.sqrt((log_hl ** 2).rolling(8).mean() / (4 * np.log(2))) * 100).fillna(0.3)

    rsi = ta.rsi(df["close"], length=8)
    df["intraday_rsi"] = rsi.fillna(50.0) if rsi is not None else 50.0

    adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx_df is not None:
        df["adx"] = adx_df.iloc[:, 0].fillna(25.0)
        df["dmp"] = adx_df.iloc[:, 1].fillna(25.0)
        df["dmn"] = adx_df.iloc[:, 2].fillna(25.0)
    else:
        df["adx"] = 25.0; df["dmp"] = 25.0; df["dmn"] = 25.0

    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["atr"] = atr.fillna(hl_range) if atr is not None else hl_range
    df["atr_pct"] = (df["atr"] / df["close"] * 100).fillna(0.4)

    df["body_ratio"] = np.abs(df["close"] - df["open"]) / hl_range
    df["upper_wick_ratio"] = (df["high"] - np.maximum(df["open"], df["close"])) / hl_range
    df["lower_wick_ratio"] = (np.minimum(df["open"], df["close"]) - df["low"]) / hl_range
    df["close_loc_in_bar"] = (df["close"] - df["low"]) / hl_range
    df["bar_direction"] = (df["close"] - df["open"]) / hl_range

    vol_avg = df["volume"].rolling(8).mean()
    df["vol_surge_ratio"] = np.where(vol_avg > 0, df["volume"] / vol_avg, 1.0)

    df["vol_close"] = df["volume"] * df["close"]
    cum_vol = df.groupby("day")["volume"].cumsum()
    cum_pv = df.groupby("day")["vol_close"].cumsum()
    df["vwap"] = np.where(cum_vol > 0, cum_pv / cum_vol, df["close"])
    df["vwap_dist_pct"] = np.where(df["vwap"] > 0, (df["close"] - df["vwap"]) / df["vwap"] * 100, 0.0)
    df["vwap_slope"] = df["vwap"].pct_change(3).fillna(0.0) * 100
    df["is_near_vwap"] = np.where(np.abs(df["vwap_dist_pct"]) <= 0.45, 1.0, 0.0)

    ema9 = ta.ema(df["close"], length=9)
    ema21 = ta.ema(df["close"], length=21)
    ema45 = ta.ema(df["close"], length=45)
    df["ema_fast"] = ema9.fillna(df["close"]) if ema9 is not None else df["close"]
    df["ema_slow"] = ema21.fillna(df["close"]) if ema21 is not None else df["close"]
    df["ema_slope_pct"] = df["ema_fast"].pct_change().fillna(0.0) * 100
    df["is_ema_bullish"] = np.where(df["ema_fast"] > df["ema_slow"], 1.0, 0.0)
    df["htf_trend_slope"] = ema45.pct_change(3).fillna(0.0) * 100 if ema45 is not None else 0.0

    bb = ta.bbands(df["close"], length=20, std=2.0)
    if bb is not None:
        bbu, bbm, bbl = bb.iloc[:, 0], bb.iloc[:, 1], bb.iloc[:, 2]
        bw = np.where(bbm > 0, (bbu - bbl) / bbm * 100, 1.0)
        df["bb_bandwidth"] = np.nan_to_num(bw, nan=1.0)
    else:
        df["bb_bandwidth"] = 1.0

    # ORB: first 30 mins of the equity session (09:15-09:45), shorter than
    # commodity's 60-min window since the equity session itself is only
    # 6h15m total vs commodity's 13h30m -- proportionally similar warm-up.
    is_orb_bar = df["minutes_since_open"] <= 30
    orb_high_cum = df["high"].where(is_orb_bar).groupby(df["day"]).cummax().ffill()
    orb_low_cum = df["low"].where(is_orb_bar).groupby(df["day"]).cummin().ffill()
    df["orb_high_dist_pct"] = np.where(orb_high_cum > 0, (df["close"] - orb_high_cum) / orb_high_cum * 100, 0.0)
    df["orb_low_dist_pct"] = np.where(orb_low_cum > 0, (df["close"] - orb_low_cum) / orb_low_cum * 100, 0.0)
    df["orb_breakout_up"] = np.where((df["close"] > orb_high_cum) & (~is_orb_bar), 1.0, 0.0)
    df["orb_breakout_dn"] = np.where((df["close"] < orb_low_cum) & (~is_orb_bar), 1.0, 0.0)

    return df.fillna(0.0)


def compute_garman_klass_vol(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """
    Computes Garman-Klass intraday volatility estimator incorporating Open, High, Low, and Close prices:
    GK = 0.5 * ln(H/L)^2 - (2*ln(2) - 1) * ln(C/O)^2
    """
    log_hl = np.log(np.maximum(df["high"] / np.maximum(df["low"], 1e-6), 1.0))
    log_co = np.log(np.maximum(df["close"] / np.maximum(df["open"], 1e-6), 1e-6))
    gk = 0.5 * (log_hl ** 2) - (2 * np.log(2) - 1) * (log_co ** 2)
    rolling_gk = np.sqrt(np.maximum(gk.rolling(window).mean(), 0.0)) * 100
    return rolling_gk.fillna(0.3)


def compute_order_book_imbalance(bids: list[dict] | list[tuple], asks: list[dict] | list[tuple]) -> float:
    """
    Computes Level 2 Order Book Imbalance (OBI) from top bid and ask queues:
    OBI = (Total Bid Qty - Total Ask Qty) / (Total Bid Qty + Total Ask Qty)
    Returns value in [-1.0, +1.0] (positive = buy pressure, negative = sell pressure).
    """
    bid_qty = 0.0
    ask_qty = 0.0

    for b in bids:
        if isinstance(b, dict):
            bid_qty += float(b.get("quantity", b.get("qty", 0)))
        elif isinstance(b, (list, tuple)) and len(b) >= 2:
            bid_qty += float(b[1])

    for a in asks:
        if isinstance(a, dict):
            ask_qty += float(a.get("quantity", a.get("qty", 0)))
        elif isinstance(a, (list, tuple)) and len(a) >= 2:
            ask_qty += float(a[1])

    total = bid_qty + ask_qty
    if total <= 0:
        return 0.0
    return float((bid_qty - ask_qty) / total)

