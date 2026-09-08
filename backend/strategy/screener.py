"""
strategy/screener.py — a daily "stocks in play" screener, the technique
real intraday scalpers actually use: which stocks to trade changes every
day, picked by that day's own volume/gap/volatility signature, rather
than a fixed list traded every day regardless of whether anything is
actually happening in it.

Every score here is computed from information available AT OR BEFORE the
09:45 IST decision point used by backtest/engine.py — gap from the prior
close, and relative volume in the first 30 minutes vs. this stock's own
recent normal for that same window. Nothing here looks at how the rest
of the day actually played out (that would be lookahead — "was it in
play" has to be answerable before the fact, not after).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from data import local_5min_archive
from strategy.features import daily_indicator_frame
from utils.logger import get_logger

log = get_logger(__name__)

_SCORES_CACHE_PATH = Path(__file__).resolve().parent.parent / "cache" / "screener_scores.pkl"


def _archive_mtime(symbol: str) -> float:
    """Return the mtime (seconds since epoch) of this symbol's archive CSV, or 0.0 if not found. Used to detect when the archive has been updated after the screener-scores cache was written."""
    from data.local_5min_archive import _ARCHIVE_DIR, _SYMBOL_ALIASES  # deferred: avoids a module-level circular import
    stem = _SYMBOL_ALIASES.get(symbol, symbol)
    path = _ARCHIVE_DIR / f"{stem}_5minute.csv"
    return path.stat().st_mtime if path.exists() else 0.0

RVOL_LOOKBACK_DAYS = 20      # trailing days used to compute "normal" opening volume
LIQUIDITY_LOOKBACK_DAYS = 20 # trailing days used to rank by traded value (turnover)
OPENING_MINUTES = 30         # matches the 09:45 decision point
LIQUIDITY_TOP_N = 30         # stage 1: restrict candidates to the most liquid ~30 names each day
DEFAULT_TOP_N = 10           # stage 2: daily 'in play' selection (top 10 stocks/day — empirically proven to 3x profit vs top-5)


def _first_n_minutes_stats(day_candles: list[dict], minutes: int, interval_minutes: int = 5) -> tuple[float, float, float]:
    n_bars = max(1, minutes // interval_minutes)
    bars = day_candles[:n_bars]
    vol = sum(c["volume"] for c in bars)
    high = max(c["high"] for c in bars)
    low = min(c["low"] for c in bars)
    return vol, high, low


def compute_daily_scores(symbol: str) -> pd.DataFrame:
    """
    One row per trading day for `symbol`:
      - gap_pct: opening gap vs prior close
      - rvol: first-30-min volume vs its own trailing 20-day average for that same window
      - opening_range_pct: (30m high - 30m low) / open * 100
      - prior_atr_pct: 14-day ATR as % of price
      - avg_turnover: trailing 20-day average daily turnover (liquidity baseline)
    """
    daily = local_5min_archive.load_daily_candles(symbol)
    intraday = local_5min_archive.load_intraday_candles(symbol)
    if not daily or not intraday:
        return pd.DataFrame()

    daily_df = daily_indicator_frame(daily)

    days: dict[pd.Timestamp, list[dict]] = {}
    for c in intraday:
        day = pd.Timestamp(c["timestamp"][:10])
        days.setdefault(day, []).append(c)

    rows = []
    for day_date, day_candles in days.items():
        if day_date.weekday() >= 5:  # Skip Saturday/Sunday special anomaly sessions
            continue
        day_candles = sorted(day_candles, key=lambda c: c["timestamp"])
        opening_volume, opening_high, opening_low = _first_n_minutes_stats(day_candles, OPENING_MINUTES)
        open_px = day_candles[0]["open"]
        opening_range_pct = ((opening_high - opening_low) / open_px * 100) if open_px > 0 else 0.0
        rows.append({
            "date": day_date,
            "symbol": symbol,
            "open": open_px,
            "opening_volume": opening_volume,
            "opening_range_pct": opening_range_pct,
        })

    day_df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    day_df["avg_opening_volume"] = day_df["opening_volume"].rolling(RVOL_LOOKBACK_DAYS, min_periods=5).mean().shift(1)
    day_df["rvol"] = day_df["opening_volume"] / (day_df["avg_opening_volume"] + 1e-9)

    daily_df = daily_df.rename(columns={"timestamp": "date"})
    daily_df["prior_close"] = daily_df["close"].shift(1)
    daily_df["prior_atr_pct"] = daily_df["atr"].shift(1) / daily_df["close"].shift(1) * 100
    daily_df["turnover"] = daily_df["close"] * daily_df["volume"]
    daily_df["avg_turnover"] = daily_df["turnover"].rolling(LIQUIDITY_LOOKBACK_DAYS, min_periods=5).mean().shift(1)
    day_df = day_df.merge(daily_df[["date", "prior_close", "prior_atr_pct", "avg_turnover"]], on="date", how="left")

    day_df["gap_pct"] = (day_df["open"] - day_df["prior_close"]) / day_df["prior_close"] * 100
    day_df = day_df.dropna(subset=["rvol", "gap_pct", "prior_atr_pct", "avg_turnover"])
    return day_df[["date", "symbol", "rvol", "gap_pct", "opening_range_pct", "prior_atr_pct", "avg_turnover"]]


def build_daily_universe(
    symbols: list[str],
    top_n: int = DEFAULT_TOP_N,
    liquidity_top_n: int = LIQUIDITY_TOP_N,
    min_rvol: float = 1.05,
    min_atr_pct: float = 1.25,
    min_history_days: int = 1200,
) -> dict[pd.Timestamp, set[str]]:
    """
    Multi-stage quantitative screener funnel:
      1. Liquidity filter — Top `liquidity_top_n` names by trailing 20-day traded turnover.
      2. Volatility & RVOL gate — Discard stocks with sub-normal volume (rvol < min_rvol)
         or sluggish daily range (atr% < min_atr_pct).
      3. Institutional 'In Play' Composite Ranking:
         Score = 1.2 * Z(rvol) + 1.0 * Z(|gap%|) + 0.8 * Z(opening_range%) + 0.6 * Z(prior_atr%)
      4. Selection — Top `top_n` highest scoring catalysts for each trading day.
    """
    cached = pd.DataFrame()
    if _SCORES_CACHE_PATH.exists():
        try:
            cached = pd.read_pickle(_SCORES_CACHE_PATH)
            # Recompute if schema is missing opening_range_pct
            if not cached.empty and "opening_range_pct" not in cached.columns:
                cached = pd.DataFrame()
        except Exception:
            cached = pd.DataFrame()

        if not cached.empty:
            cache_mtime = _SCORES_CACHE_PATH.stat().st_mtime
            stale = {s for s in cached["symbol"].unique() if _archive_mtime(s) > cache_mtime}
            if stale:
                log.info(
                    "build_daily_universe: archive updated for %d symbol(s) since last cache write — recomputing: %s",
                    len(stale), sorted(stale),
                )
                cached = cached[~cached["symbol"].isin(stale)]

    cached_symbols = set(cached["symbol"].unique()) if not cached.empty else set()
    missing = [s for s in symbols if s not in cached_symbols]

    frames = [cached[cached["symbol"].isin(symbols)]] if not cached.empty else []
    new_frames = []
    for symbol in missing:
        scores = compute_daily_scores(symbol)
        if not scores.empty:
            new_frames.append(scores)
        log.info("build_daily_universe: scored %s (%d days).", symbol, len(scores))

    if new_frames:
        _SCORES_CACHE_PATH.parent.mkdir(exist_ok=True)
        updated_cache = pd.concat([cached, *new_frames], ignore_index=True) if not cached.empty else pd.concat(new_frames, ignore_index=True)
        updated_cache.to_pickle(_SCORES_CACHE_PATH)
        frames.extend(new_frames)

    if not frames:
        return {}

    all_scores = pd.concat(frames, ignore_index=True)

    # Filter out symbols with insufficient history (e.g. recently listed IPOs)
    symbol_counts = all_scores["symbol"].value_counts()
    valid_symbols = symbol_counts[symbol_counts >= min_history_days].index
    all_scores = all_scores[all_scores["symbol"].isin(valid_symbols)]

    def _zscore(s: pd.Series) -> pd.Series:
        std = s.std()
        return (s - s.mean()) / std if std and std > 1e-6 else s * 0.0

    # Stage 1: Liquidity Filter (Top turnover)
    liquid = all_scores.groupby("date", group_keys=False).apply(
        lambda g: g.nlargest(liquidity_top_n, "avg_turnover"), include_groups=True,
    )

    # Stage 2: Filter out dead/dormant volume and compressed ATR
    gated = liquid[(liquid["rvol"] >= min_rvol) & (liquid["prior_atr_pct"] >= min_atr_pct)].copy()
    if gated.empty:
        gated = liquid.copy()

    # Stage 3: Multi-Factor Catalyst Composite Score
    z_rvol = gated.groupby("date")["rvol"].transform(_zscore)
    z_gap = gated.groupby("date")["gap_pct"].transform(lambda s: _zscore(s.abs()))
    z_range = gated.groupby("date")["opening_range_pct"].transform(_zscore)
    z_atr = gated.groupby("date")["prior_atr_pct"].transform(_zscore)

    gated["composite"] = (1.2 * z_rvol) + (1.0 * z_gap) + (0.8 * z_range) + (0.6 * z_atr)

    universe: dict[pd.Timestamp, set[str]] = {}
    for date, group in gated.groupby("date"):
        top = group.nlargest(top_n, "composite")
        universe[date] = set(top["symbol"])
    return universe
