"""
strategy/features.py — rolling intraday scalp features for the LightGBM
model. No "opening range" anchor — a decision can be evaluated at ANY
bar during the session (after a short per-day warm-up), using only
information available up to and including that bar.

Indicators (RSI/ATR/ADX/EMA) come from pandas_ta_classic, standing in
for TA-Lib (no system C library dependency).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

# --- Per-bar (intraday) feature windows, all computed PER DAY (never
# blending across the overnight gap into the previous session) ---
MOMENTUM_LOOKBACK = 4        # bars (~20 min), fast momentum
MOMENTUM_LOOKBACK_LONG = 12  # bars (~60 min), trend momentum
RANGE_LOOKBACK = 8          # bars, for recent local volatility
VOLUME_LOOKBACK = 8         # bars, for volume surge ratio
INTRADAY_RSI_LENGTH = 8      # fast intraday RSI
WARMUP_CANDLES = 6          # skip first 30 min (09:15-09:45) to allow morning price discovery

# --- Daily-bar (prior-day context) indicator lengths, unchanged from before ---
ATR_LENGTH = 14
RSI_LENGTH = 14
ADX_LENGTH = 14
EMA_FAST = 9
EMA_SLOW = 21
VOLUME_SMA_LENGTH = 20

DEFAULT_INDEX_SYMBOL = "NIFTY 50"  # for relative-strength: is this stock outperforming/underperforming the market right now?

# Sector-appropriate benchmark where one clearly applies — a bank stock's
# relative strength against its own sector (Bank Nifty) is more
# economically meaningful than against the broad market, which mixes in
# IT/energy/FMCG names that move on entirely different drivers.
SECTOR_INDEX_MAP = {
    "HDFCBANK": "NIFTY BANK", "ICICIBANK": "NIFTY BANK", "AXISBANK": "NIFTY BANK", "SBIN": "NIFTY BANK",
    "KOTAKBANK": "NIFTY BANK", "BANKBARODA": "NIFTY BANK", "PNB": "NIFTY BANK", "CANBK": "NIFTY BANK",
    "INDUSINDBK": "NIFTY BANK", "UNIONBANK": "NIFTY BANK", "IDFCFIRSTB": "NIFTY BANK", "AUBANK": "NIFTY BANK",
}


def index_symbol_for(symbol: str) -> str:
    return SECTOR_INDEX_MAP.get(symbol, DEFAULT_INDEX_SYMBOL)


FEATURE_COLUMNS = [
    "mom_pct", "mom_pct_long", "rel_strength_vs_nifty", "avg_range_pct", "intraday_rsi", "vol_ratio",
    "minutes_since_open", "prior_atr_pct", "prior_rsi", "prior_adx", "prior_ema_slope_pct", "day_of_week",
    "vwap_dist_pct", "parkinson_vol", "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    "volume_flow_ratio", "vwap_slope", "orb_high_dist_pct", "orb_low_dist_pct",
]

# Lazily-built, process-wide cache: an index's own per-day bar features
# never depend on which stock is being processed, so compute each
# (index, day) once and reuse across every stock mapped to it, rather
# than once per (symbol, day) — a large reduction in redundant work
# across a universe that size. Keyed by (index_symbol, day_date).
_index_days_maps: dict[str, dict[pd.Timestamp, list[dict]]] = {}
_index_bar_cache: dict[tuple[str, pd.Timestamp], pd.DataFrame | None] = {}


def daily_indicator_frame(daily_candles: list[dict]) -> pd.DataFrame:
    """
    Build a DataFrame of prior-day indicators (ATR/RSI/ADX/EMA) from
    closed daily candles, oldest-first. Each row's indicators are
    computed using only that row and everything before it (pandas_ta's
    rolling/EWM functions are causal by construction), so row i's values
    are safe to use as "as of the close of day i" features — i.e. known
    context for trading on day i+1.
    """
    df = pd.DataFrame(daily_candles)
    df["timestamp"] = pd.to_datetime(df["timestamp"].astype(str).str.replace(r"\+\d{2}:\d{2}$", "", regex=True), format="ISO8601")
    df = df.sort_values("timestamp").reset_index(drop=True)

    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_LENGTH)
    df["rsi"] = ta.rsi(df["close"], length=RSI_LENGTH)
    adx_df = ta.adx(df["high"], df["low"], df["close"], length=ADX_LENGTH)
    df["adx"] = adx_df[f"ADX_{ADX_LENGTH}"] if adx_df is not None else float("nan")
    df["ema_fast"] = ta.ema(df["close"], length=EMA_FAST)
    df["ema_slow"] = ta.ema(df["close"], length=EMA_SLOW)
    df["ema_slope_pct"] = df["ema_fast"].pct_change() * 100
    return df


def compute_intraday_bar_features(day_candles: list[dict], interval_minutes: int) -> pd.DataFrame:
    """
    Per-bar rolling features for ONE trading day's intraday candles
    (oldest-first, dicts with open/high/low/close/volume). Every column
    is causal — row t only ever uses bars up to and including t — and
    all rolling windows are computed within this single day's bars only,
    so nothing blends across the overnight session gap.
    """
    df = pd.DataFrame(day_candles)
    hl_range = np.maximum(df["high"] - df["low"], 1e-4)
    df["range_pct"] = hl_range / df["close"] * 100
    df["avg_range_pct"] = df["range_pct"].rolling(RANGE_LOOKBACK).mean()
    df["mom_pct"] = df["close"].pct_change(MOMENTUM_LOOKBACK) * 100
    df["mom_pct_long"] = df["close"].pct_change(MOMENTUM_LOOKBACK_LONG) * 100
    df["intraday_rsi"] = ta.rsi(df["close"], length=INTRADAY_RSI_LENGTH)
    vol_avg = df["volume"].rolling(VOLUME_LOOKBACK).mean()
    vol_ratio = np.where(vol_avg > 0, df["volume"] / vol_avg, 1.0)
    df["vol_ratio"] = vol_ratio
    df["minutes_since_open"] = np.arange(len(df)) * interval_minutes

    # Candle price action / microstructure
    df["body_ratio"] = np.abs(df["close"] - df["open"]) / hl_range
    df["upper_wick_ratio"] = (df["high"] - np.maximum(df["open"], df["close"])) / hl_range
    df["lower_wick_ratio"] = (np.minimum(df["open"], df["close"]) - df["low"]) / hl_range

    # Volume Flow Directional Ratio
    flow_dir = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl_range
    df["volume_flow_ratio"] = flow_dir * vol_ratio

    # Intraday cumulative VWAP, distance, and slope
    cum_vol = df["volume"].cumsum()
    cum_pv = (df["close"] * df["volume"]).cumsum()
    df["vwap"] = np.where(cum_vol > 0, cum_pv / cum_vol, df["close"])
    df["vwap_dist_pct"] = np.where(df["vwap"] > 0, (df["close"] - df["vwap"]) / df["vwap"] * 100, 0.0)
    df["vwap_slope"] = df["vwap"].pct_change(3).fillna(0.0) * 100

    # 15-Minute Opening Range (ORB) Breakout Distance
    orb_high = df["high"].iloc[:3].max() if len(df) >= 3 else df["high"].max()
    orb_low = df["low"].iloc[:3].min() if len(df) >= 3 else df["low"].min()
    df["orb_high_dist_pct"] = (df["close"] - orb_high) / np.maximum(orb_high, 1e-4) * 100
    df["orb_low_dist_pct"] = (df["close"] - orb_low) / np.maximum(orb_low, 1e-4) * 100

    # Parkinson High-Low Volatility estimator over local lookback
    log_hl = np.log(np.maximum(df["high"] / np.maximum(df["low"], 1e-6), 1.0))
    df["parkinson_vol"] = np.sqrt((log_hl ** 2).rolling(RANGE_LOOKBACK).mean() / (4 * np.log(2))) * 100

    return df


def _get_index_day_bars(index_symbol: str, day_date: pd.Timestamp, interval_minutes: int) -> pd.DataFrame | None:
    """One benchmark index's own per-bar features for one day, cached process-wide (see module docstring note above)."""
    if index_symbol not in _index_days_maps:
        from data import local_5min_archive  # deferred import: avoids a module-level circular import with data/
        intraday = local_5min_archive.load_intraday_candles(index_symbol)
        days: dict[pd.Timestamp, list[dict]] = {}
        for c in intraday:
            d = pd.Timestamp(c["timestamp"][:10])
            days.setdefault(d, []).append(c)
        _index_days_maps[index_symbol] = days

    cache_key = (index_symbol, day_date)
    if cache_key not in _index_bar_cache:
        day_candles = _index_days_maps[index_symbol].get(day_date)
        if day_candles:
            day_candles = sorted(day_candles, key=lambda c: c["timestamp"])
            _index_bar_cache[cache_key] = compute_intraday_bar_features(day_candles, interval_minutes)
        else:
            _index_bar_cache[cache_key] = None
    return _index_bar_cache[cache_key]


def build_bar_features(
    bar_row: pd.Series,
    prior_daily_row: pd.Series,
    day_date: pd.Timestamp,
    bar_index: int,
    interval_minutes: int,
    index_symbol: str = DEFAULT_INDEX_SYMBOL,
) -> dict | None:
    """One feature row for a single intraday decision point, or None if inputs are still warming up (including the benchmark index's own features — a symbol can't be scored on a day the index itself has no data for, e.g. a market-wide special session). `index_symbol` should come from index_symbol_for(symbol) — sector benchmark where one applies, else the broad market."""
    needed = (
        "mom_pct", "mom_pct_long", "avg_range_pct", "intraday_rsi", "vol_ratio", "vwap_dist_pct",
        "parkinson_vol", "body_ratio", "volume_flow_ratio", "vwap_slope",
    )
    for k in needed:
        val = bar_row.get(k)
        if val is None or pd.isna(val):
            return None

    needed_prior = ("atr", "rsi", "adx", "ema_slope_pct")
    for k in needed_prior:
        val = prior_daily_row.get(k)
        if val is None or pd.isna(val):
            return None

    index_bars = _get_index_day_bars(index_symbol, day_date, interval_minutes)
    if index_bars is None or bar_index >= len(index_bars):
        return None
    index_row = index_bars.iloc[bar_index]
    if pd.isna(index_row["mom_pct"]):
        return None
    rel_strength_vs_nifty = bar_row["mom_pct"] - index_row["mom_pct"]

    return {
        "date": day_date,
        "mom_pct": bar_row["mom_pct"],
        "mom_pct_long": bar_row["mom_pct_long"],
        "rel_strength_vs_nifty": rel_strength_vs_nifty,
        "avg_range_pct": bar_row["avg_range_pct"],
        "intraday_rsi": bar_row["intraday_rsi"],
        "vol_ratio": bar_row["vol_ratio"],
        "minutes_since_open": bar_row["minutes_since_open"],
        "prior_atr_pct": prior_daily_row["atr"] / prior_daily_row["close"] * 100,
        "prior_rsi": prior_daily_row["rsi"],
        "prior_adx": prior_daily_row["adx"],
        "prior_ema_slope_pct": prior_daily_row["ema_slope_pct"],
        "day_of_week": day_date.dayofweek,
        "vwap_dist_pct": bar_row["vwap_dist_pct"],
        "parkinson_vol": bar_row["parkinson_vol"],
        "body_ratio": bar_row["body_ratio"],
        "upper_wick_ratio": bar_row["upper_wick_ratio"],
        "lower_wick_ratio": bar_row["lower_wick_ratio"],
        "volume_flow_ratio": bar_row["volume_flow_ratio"],
        "vwap_slope": bar_row["vwap_slope"],
        "orb_high_dist_pct": bar_row["orb_high_dist_pct"],
        "orb_low_dist_pct": bar_row["orb_low_dist_pct"],
    }
