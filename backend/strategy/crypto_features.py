"""
strategy/crypto_features.py — Feature Engineering for 5-Min Binance Perpetual Futures

Mirrors strategy/commodity_features.py's technical-indicator core (momentum,
Parkinson volatility, fast RSI, ADX/DMI/ATR trend strength, candle
microstructure, volume surge, VWAP, EMA trend, Bollinger bandwidth, Opening
Range Breakout), but drops everything tied to an exchange open/close or a
weekly commodity inventory report -- Binance perpetuals trade 24/7, so there
is no MCX-style session-phase or Wed/Thu-EIA-inventory-window analog. VWAP
resets and the Opening Range Breakout instead use a UTC-midnight day
boundary (the closest 24/7 equivalent of a "trading day").
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

CRYPTO_FEATURE_COLUMNS = [
    "mom_fast", "mom_med", "mom_slow", "intraday_rsi", "vwap_dist_pct", "vwap_slope",
    "parkinson_vol", "avg_range_pct", "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    "close_loc_in_bar", "bar_direction", "bb_bandwidth", "htf_trend_slope", "is_near_vwap",
    "vol_surge_ratio", "hour_of_day", "day_of_week", "minutes_since_day_open",
    "orb_high_dist_pct", "orb_low_dist_pct", "orb_breakout_up", "orb_breakout_dn",
    "ema_slope_pct", "is_ema_bullish", "adx", "dmp", "dmn", "atr_pct",
    # Crypto-specific additions -- no MCX/Upstox equivalent exists for these:
    "taker_buy_ratio", "volume_delta_pct", "bb_squeeze_pctl", "is_bb_squeeze",
    "rsi_extreme_long", "rsi_extreme_short", "ema50_dist_pct", "is_above_ema50",
    "funding_rate_recent", "funding_rate_change",
]


def compute_crypto_features(df: pd.DataFrame, symbol: str = "BTCUSDT", funding_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Computes causality-preserving 5-minute rolling features for Binance
    USDT-M perpetual futures. `df` contains [timestamp, open, high, low, close, volume],
    optionally `taker_buy_base` (order-flow proxy). `funding_df`, if given, has
    [timestamp, funding_rate] (from archive_crypto/{symbol}_funding.csv).
    """
    df = df.copy()
    if "timestamp" in df.columns:
        df["_dt"] = pd.to_datetime(df["timestamp"])
    else:
        df["_dt"] = pd.to_datetime(df["date"])

    df["hour_of_day"] = df["_dt"].dt.hour
    df["minute"] = df["_dt"].dt.minute
    df["day_of_week"] = df["_dt"].dt.dayofweek
    df["day"] = df["_dt"].dt.date
    df["minutes_since_day_open"] = df["hour_of_day"] * 60 + df["minute"]  # minutes since UTC midnight

    # 1. Momentum over multiple lookbacks (4 bars = 20m, 12 bars = 60m, 24 bars = 2h)
    df["mom_fast"] = df["close"].pct_change(4).fillna(0.0) * 100
    df["mom_med"] = df["close"].pct_change(12).fillna(0.0) * 100
    df["mom_slow"] = df["close"].pct_change(24).fillna(0.0) * 100

    # 2. Volatility, Range, & Parkinson Estimator
    hl_range = np.maximum(df["high"] - df["low"], 1e-4)
    df["range_pct"] = hl_range / df["close"] * 100
    df["avg_range_pct"] = df["range_pct"].rolling(8).mean().fillna(0.3)
    log_hl = np.log(np.maximum(df["high"] / np.maximum(df["low"], 1e-6), 1.0))
    df["parkinson_vol"] = (np.sqrt((log_hl ** 2).rolling(8).mean() / (4 * np.log(2))) * 100).fillna(0.3)

    # 3. Fast Intraday RSI (8 periods)
    rsi = ta.rsi(df["close"], length=8)
    df["intraday_rsi"] = rsi.fillna(50.0) if rsi is not None else 50.0

    # 4. Trend Strength (ADX, +DI, -DI, ATR)
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

    # 5. Price Action & Candle Microstructure
    df["body_ratio"] = np.abs(df["close"] - df["open"]) / hl_range
    df["upper_wick_ratio"] = (df["high"] - np.maximum(df["open"], df["close"])) / hl_range
    df["lower_wick_ratio"] = (np.minimum(df["open"], df["close"]) - df["low"]) / hl_range
    df["close_loc_in_bar"] = (df["close"] - df["low"]) / hl_range
    df["bar_direction"] = (df["close"] - df["open"]) / hl_range

    # 6. Volume Flow & Surge Ratio
    vol_avg = df["volume"].rolling(8).mean()
    df["vol_surge_ratio"] = np.where(vol_avg > 0, df["volume"] / vol_avg, 1.0)

    # 7. UTC-Day Reset VWAP & Elasticity
    df["vol_close"] = df["volume"] * df["close"]
    cum_vol = df.groupby("day")["volume"].cumsum()
    cum_pv = df.groupby("day")["vol_close"].cumsum()
    df["vwap"] = np.where(cum_vol > 0, cum_pv / cum_vol, df["close"])
    df["vwap_dist_pct"] = np.where(df["vwap"] > 0, (df["close"] - df["vwap"]) / df["vwap"] * 100, 0.0)
    df["vwap_slope"] = df["vwap"].pct_change(3).fillna(0.0) * 100
    df["is_near_vwap"] = np.where(np.abs(df["vwap_dist_pct"]) <= 0.45, 1.0, 0.0)

    # 8. Fast & Slow EMAs + Higher Timeframe (1-hour) Trend
    ema9 = ta.ema(df["close"], length=9)
    ema21 = ta.ema(df["close"], length=21)
    ema45 = ta.ema(df["close"], length=45)
    df["ema_fast"] = ema9.fillna(df["close"]) if ema9 is not None else df["close"]
    df["ema_slow"] = ema21.fillna(df["close"]) if ema21 is not None else df["close"]
    df["ema_slope_pct"] = df["ema_fast"].pct_change().fillna(0.0) * 100
    df["is_ema_bullish"] = np.where(df["ema_fast"] > df["ema_slow"], 1.0, 0.0)
    df["htf_trend_slope"] = ema45.pct_change(3).fillna(0.0) * 100 if ema45 is not None else 0.0

    # 9. Bollinger Bandwidth (Volatility Expansion)
    bb = ta.bbands(df["close"], length=20, std=2.0)
    if bb is not None:
        bbu = bb.iloc[:, 0]
        bbm = bb.iloc[:, 1]
        bbl = bb.iloc[:, 2]
        bw = np.where(bbm > 0, (bbu - bbl) / bbm * 100, 1.0)
        df["bb_bandwidth"] = np.nan_to_num(bw, nan=1.0)
    else:
        df["bb_bandwidth"] = 1.0

    # 10. UTC-Day Opening Range Breakout (first 60 mins after UTC midnight)
    is_orb_bar = df["minutes_since_day_open"] <= 60
    orb_high_cum = df["high"].where(is_orb_bar).groupby(df["day"]).cummax().ffill()
    orb_low_cum = df["low"].where(is_orb_bar).groupby(df["day"]).cummin().ffill()

    df["orb_high_dist_pct"] = np.where(orb_high_cum > 0, (df["close"] - orb_high_cum) / orb_high_cum * 100, 0.0)
    df["orb_low_dist_pct"] = np.where(orb_low_cum > 0, (df["close"] - orb_low_cum) / orb_low_cum * 100, 0.0)
    df["orb_breakout_up"] = np.where((df["close"] > orb_high_cum) & (~is_orb_bar), 1.0, 0.0)
    df["orb_breakout_dn"] = np.where((df["close"] < orb_low_cum) & (~is_orb_bar), 1.0, 0.0)

    # 11. Taker Buy/Sell Order-Flow Proxy (from Binance's per-bar taker_buy_base --
    # no MCX/Upstox candle carries this; approximates aggressive buy vs sell volume
    # within the bar without needing a separate tick/order-book feed).
    if "taker_buy_base" in df.columns:
        df["taker_buy_ratio"] = np.where(df["volume"] > 0, df["taker_buy_base"] / df["volume"], 0.5)
        taker_sell = df["volume"] - df["taker_buy_base"]
        df["volume_delta_pct"] = np.where(df["volume"] > 0, (df["taker_buy_base"] - taker_sell) / df["volume"] * 100, 0.0)
    else:
        df["taker_buy_ratio"] = 0.5
        df["volume_delta_pct"] = 0.0

    # 12. Bollinger Bandwidth Squeeze (percentile rank of bandwidth over a rolling
    # 100-bar window -- research-backed "squeeze precedes a sharp move" pattern;
    # bottom-20th-percentile bandwidth flags a coiled/low-volatility regime).
    df["bb_squeeze_pctl"] = df["bb_bandwidth"].rolling(100, min_periods=20).rank(pct=True).fillna(0.5)
    df["is_bb_squeeze"] = np.where(df["bb_squeeze_pctl"] <= 0.20, 1.0, 0.0)

    # 13. RSI Extreme Zones (20/80 -- crypto's fatter tails call for wider extremes
    # than the classic 30/70 used on lower-volatility assets).
    df["rsi_extreme_long"] = np.where(df["intraday_rsi"] <= 20.0, 1.0, 0.0)
    df["rsi_extreme_short"] = np.where(df["intraday_rsi"] >= 80.0, 1.0, 0.0)

    # 14. 50-period EMA Trend Filter (a common crypto scalping reference beyond the
    # 9/21/45 EMAs already computed above).
    ema50 = ta.ema(df["close"], length=50)
    ema50_series = ema50.fillna(df["close"]) if ema50 is not None else df["close"]
    df["ema50_dist_pct"] = (df["close"] - ema50_series) / ema50_series * 100
    df["is_above_ema50"] = np.where(df["close"] > ema50_series, 1.0, 0.0)

    # 15. Funding Rate Context (unique to perpetual futures -- an elevated positive
    # funding rate means longs are crowded/paying shorts, historically a mild
    # mean-reversion signal; MCX futures have no funding-rate analog at all).
    if funding_df is not None and not funding_df.empty:
        fdf = funding_df.sort_values("timestamp")
        merged = pd.merge_asof(df[["_dt"]], fdf, left_on="_dt", right_on="timestamp", direction="backward")
        df["funding_rate_recent"] = merged["funding_rate"].fillna(0.0).values
        df["funding_rate_change"] = pd.Series(df["funding_rate_recent"]).diff().fillna(0.0).values
    else:
        df["funding_rate_recent"] = 0.0
        df["funding_rate_change"] = 0.0

    return df.fillna(0.0)
