"""
backtest/engine.py — no-lookahead replay of the LightGBM direction-scalper
over real historical NIFTY50/BankNifty stock data, from the local archive
(data/local_5min_archive.py, backend/archive/*.csv — real 5-minute bars,
2015-2026). No Upstox API calls are needed for the backtest.

Genuine intraday SCALPING — a decision can be evaluated at any bar
across the trading day (after the per-day warm-up,
strategy/features.WARMUP_CANDLES, through SESSION_CUTOFF_MINUTES, which
leaves just enough room before the close for a trade to be managed). An
earlier version of this project restricted decisions to the first two
hours only, on the theory that the open carries most of the real
directional tendency — that's true as far as it goes, but a system that
only fires a few times a month isn't a scalper regardless of whether
each individual signal is slightly better. The four safeguards below are
what make trading the WHOLE day survivable — an even earlier all-day
attempt, before these existed, lost ~99.94% of capital; this is the same
idea with the machinery that was missing back then:
  - The LABEL only asks a short, fixed-window question (HOLD_MINUTES —
    "which side moves by the stop distance first") — that's what the
    LightGBM model is trained to predict, and it stays a genuinely
    short-horizon question.
  - The TRADE ITSELF, once taken, is managed with a trailing stop
    instead of a fixed timeout/target: it never exits just because time
    ran out. An initial stop protects the first 1R of risk; once price
    moves in favor by TRAIL_ACTIVATION_MULT×stop, a trailing stop
    activates and follows the best price seen by TRAIL_DIST_MULT×stop
    behind it. The trade only ends when price actually reverses against
    it (or the session forces a square-off at close) — it can keep
    running past HOLD_MINUTES if it keeps moving favorably. This fixes a
    real problem found in an earlier version: with a fixed timeout,
    winners were routinely cut short before reaching their 2R target
    while losers reliably ran to the full stop, inverting the intended
    1:2 payoff shape.
  - The stop distance itself is sized off the stock's own recent local
    volatility (its trailing average bar range over the last
    strategy.features.RANGE_LOOKBACK bars, scaled up for the hold
    window), not its full-day ATR — a day's ATR is the wrong scale for a
    short hold.
  - Within one symbol, trades never overlap: once a trade is taken, the
    next decision point isn't considered until that trade has exited.

5-minute only — the archive's native bars, no other timeframe.

Pipeline per symbol:
  1. Load full daily + intraday history from the local archive.
  2. Build per-day intraday rolling features (strategy/features.py), plus
     prior-day daily-indicator context, for every eligible bar.
  3. Label each bar: over the next HOLD_CANDLES bars, does price move up
     by the local-volatility-sized stop distance before down, or the
     reverse?
  4. Pool every symbol's rows together and run walk-forward LightGBM
     (ml/train.py) to get an out-of-sample P(up) per decision point.
  5. Simulate the trade that prediction implies (local-vol-sized stop,
     trailing once in profit, exits only on reversal or session close),
     applying real transaction costs, into a trade log — enforcing one
     trade at a time per symbol.

Only rows with a genuine out-of-sample prediction are ever traded/scored
— the first calendar year of a symbol's history (no prior year to train
on) is excluded from the backtest entirely, not treated as "no signal".
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pandas as pd

from data import local_5min_archive
from ml.train import walk_forward_predict
from strategy import costs
from strategy.features import (
    FEATURE_COLUMNS, WARMUP_CANDLES, build_bar_features, compute_intraday_bar_features, daily_indicator_frame,
    index_symbol_for,
)
from utils.logger import get_logger

log = get_logger(__name__)

HOLD_MINUTES = 45  # 9 bars of 5-min — gives the label sufficient window to resolve real intraday trends
STOP_VOL_MULT = 1.2  # Stop distance = 1.2x expected local volatility (tighter risk)
UP_THRESHOLD = 0.52  # High conviction threshold for longs
DOWN_THRESHOLD = 0.44 # Strict breakdown threshold for shorts (prevents counter-trend shorting)
META_THRESHOLD = 0.50   # meta-labeling model cutoff

TAKE_PROFIT_MULT = 1.8  # Asymmetric 1.8R reward target (ensures wins dominate transaction friction)
BE_ACTIVATION_MULT = 0.8 # Arm Breakeven stop only after +0.8R move (avoids early breakeven whipsaws)
TRAIL_DIST_MULT = 0.5   # Trailing stop follows 0.5R behind the peak price once armed
MIN_STOP_TO_COST_RATIO = 4.0 # Ensure minimum stop distance is at least 4x total round-trip friction
MIN_AVG_RANGE_PCT = 0.25 # Minimum trailing 5-min bar range % to avoid dead choppy stocks

INTERVAL_MINUTES = 5
HOLD_CANDLES = max(1, round(HOLD_MINUTES / INTERVAL_MINUTES))

# Active high-conviction scalping session window: 09:45 to 13:15 IST
SESSION_START_MINUTES = 30
SESSION_CUTOFF_MINUTES = 240

_DATASET_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "symbol_datasets"


def _group_by_day(candles: list[dict]) -> dict[pd.Timestamp, list[dict]]:
    days: dict[pd.Timestamp, list[dict]] = {}
    for c in candles:
        ts = datetime.fromisoformat(c["timestamp"])
        day = pd.Timestamp(ts.date())
        days.setdefault(day, []).append({**c, "_dt": ts})
    return days


def build_symbol_dataset(symbol: str, eligible_dates: set[pd.Timestamp] | None = None) -> pd.DataFrame:
    """
    Cached wrapper around `_build_symbol_dataset` — this is the
    expensive per-bar feature/label loop, and it's pure given (symbol,
    eligible_dates, the current feature/label code).
    """
    cache_key = _dataset_cache_key(symbol, eligible_dates)
    cache_path = _DATASET_CACHE_DIR / f"{cache_key}.pkl"
    if cache_path.exists():
        return pd.read_pickle(cache_path)

    # Automatically erase any older/stale cache versions for this symbol
    if _DATASET_CACHE_DIR.exists():
        for old_cache in _DATASET_CACHE_DIR.glob(f"{symbol}_*.pkl"):
            try:
                old_cache.unlink()
            except OSError:
                pass

    result = _build_symbol_dataset(symbol, eligible_dates)
    _DATASET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    result.to_pickle(cache_path)
    return result


def _dataset_cache_key(symbol: str, eligible_dates: set[pd.Timestamp] | None) -> str:
    dates_part = "all" if eligible_dates is None else hashlib.md5(
        ",".join(sorted(d.isoformat() for d in eligible_dates)).encode()
    ).hexdigest()[:12]
    return f"{symbol}_{_code_version_hash()}_{dates_part}"


_code_hash_cache: str | None = None


def _code_version_hash() -> str:
    global _code_hash_cache
    if _code_hash_cache is None:
        _code_hash_cache = "hf_scalper_v5_alpha"
    return _code_hash_cache


def _build_symbol_dataset(symbol: str, eligible_dates: set[pd.Timestamp] | None = None) -> pd.DataFrame:
    hold_candles = HOLD_CANDLES
    index_symbol = index_symbol_for(symbol)
    daily = local_5min_archive.load_daily_candles(symbol)
    intraday = local_5min_archive.load_intraday_candles(symbol)
    if not daily or not intraday:
        log.warning("build_symbol_dataset: no archive history for %s.", symbol)
        return pd.DataFrame()

    daily_df = daily_indicator_frame(daily)
    days_map = _group_by_day(intraday)
    daily_index_by_date = {row["timestamp"].normalize(): i for i, row in daily_df.iterrows()}

    rows = []
    for day_date, day_candles in days_map.items():
        if day_date.weekday() >= 5:  # Skip Saturday/Sunday special anomaly sessions
            continue
        if eligible_dates is not None and day_date not in eligible_dates:
            continue
        i = daily_index_by_date.get(day_date)
        if i is None or i == 0:
            continue
        prior_row = daily_df.iloc[i - 1]
        if any(pd.isna(prior_row.get(k)) for k in ("atr", "rsi", "adx", "ema_slope_pct")):
            continue

        day_candles = sorted(day_candles, key=lambda c: c["_dt"])
        bar_df = compute_intraday_bar_features(day_candles, INTERVAL_MINUTES)

        last_usable = len(bar_df) - hold_candles - 1
        cutoff_bar = SESSION_CUTOFF_MINUTES // INTERVAL_MINUTES
        last_usable = min(last_usable, cutoff_bar)
        for t in range(WARMUP_CANDLES, last_usable + 1):
            feat = build_bar_features(bar_df.iloc[t], prior_row, day_date, bar_index=t, interval_minutes=INTERVAL_MINUTES, index_symbol=index_symbol)
            if feat is None:
                continue

            entry_price = day_candles[t]["close"]
            entry_dt = day_candles[t]["_dt"]
            vol_basis_pct = bar_df.iloc[t]["avg_range_pct"] * (hold_candles ** 0.5)
            stop_dist = STOP_VOL_MULT * vol_basis_pct / 100 * entry_price
            if stop_dist <= 0:
                continue
            stop_dist_pct = stop_dist / entry_price * 100
            if stop_dist_pct < MIN_STOP_TO_COST_RATIO * costs.TOTAL_ROUND_TRIP_COST_PCT:
                continue

            rest_of_day = day_candles[t + 1:]
            label = _label_outcome(entry_price, stop_dist, rest_of_day[:hold_candles])
            if label is None:
                continue

            feat.update({
                "symbol": symbol, "label": label, "entry_price": entry_price, "entry_dt": entry_dt,
                "stop_dist": stop_dist, "_window": rest_of_day,
            })
            rows.append(feat)

    return pd.DataFrame(rows)


def _label_outcome(entry_price: float, stop_dist: float, window: list[dict], target_mult: float = 1.0) -> int | None:
    """Symmetric 1R label — UP wins if price moves +1R before -1R, DOWN wins if price drops -1R first.
    Using equal targets for both sides eliminates the prior 1.5R/1.0R asymmetry that caused p_up mean ≈ 0.18."""
    up_level = entry_price + target_mult * stop_dist
    down_level = entry_price - target_mult * stop_dist
    for c in window:
        if c["high"] >= up_level and c["low"] <= down_level:
            return None
        if c["high"] >= up_level:
            return 1
        if c["low"] <= down_level:
            return 0
    return None



def _simulate_trade(
    direction: int, entry_price: float, entry_dt, stop_dist: float, window: list[dict],
    take_profit_mult: float = TAKE_PROFIT_MULT, be_activation_mult: float = BE_ACTIVATION_MULT,
) -> dict:
    """
    High win-rate scalper execution:
      1. Sized take-profit target (+1.0R) captures clean trend impulse.
      2. When price reaches +0.45R, stop is immediately locked to Breakeven (+all fees covered).
      3. Trails behind peak to protect gains.
    """
    initial_stop = entry_price - stop_dist if direction == 1 else entry_price + stop_dist
    current_stop = initial_stop
    tp_level = entry_price + take_profit_mult * stop_dist * direction
    be_level = entry_price + be_activation_mult * stop_dist * direction

    armed_be = False
    best_price = entry_price

    exit_price, exit_reason, exit_dt = entry_price, "eod_exit", entry_dt
    for c in window:
        favorable_price = c["high"] if direction == 1 else c["low"]
        adverse_price = c["low"] if direction == 1 else c["high"]

        # Check Take-Profit
        hit_tp = favorable_price >= tp_level if direction == 1 else favorable_price <= tp_level
        if hit_tp:
            exit_price, exit_reason, exit_dt = tp_level, "take_profit", c["_dt"]
            break

        # Check Stop
        hit_stop = adverse_price <= current_stop if direction == 1 else adverse_price >= current_stop
        if hit_stop:
            exit_price, exit_reason, exit_dt = current_stop, ("be_stop" if armed_be else "initial_stop"), c["_dt"]
            break

        # Check Break-Even trigger (+fees covered)
        if not armed_be and (favorable_price >= be_level if direction == 1 else favorable_price <= be_level):
            armed_be = True
            be_stop_price = entry_price + 0.0025 * entry_price * direction
            current_stop = max(current_stop, be_stop_price) if direction == 1 else min(current_stop, be_stop_price)

        if armed_be:
            best_price = max(best_price, favorable_price) if direction == 1 else min(best_price, favorable_price)
            trailing_level = best_price - TRAIL_DIST_MULT * stop_dist * direction
            current_stop = max(current_stop, trailing_level) if direction == 1 else min(current_stop, trailing_level)
    else:
        exit_price, exit_reason, exit_dt = (window[-1]["close"] if window else entry_price), "eod_exit", (window[-1]["_dt"] if window else entry_dt)

    gross_pct = (exit_price - entry_price) / entry_price * 100 * direction
    
    # Direction-specific fee accounting
    entry_tax = costs.BUY_SIDE_PCT if direction == 1 else costs.SELL_SIDE_PCT
    exit_tax = costs.SELL_SIDE_PCT if direction == 1 else costs.BUY_SIDE_PCT
    actual_slippage_rt = 2 * costs.slippage_pct_per_leg(entry_price)
    net_pct = gross_pct - (entry_tax + exit_tax) - actual_slippage_rt
    
    r_multiple = (net_pct / 100 * entry_price) / stop_dist if stop_dist else 0.0

    return {
        "direction": "long" if direction == 1 else "short",
        "entry_price": entry_price, "entry_dt": entry_dt, "exit_price": exit_price, "exit_dt": exit_dt,
        "stop": initial_stop, "exit_stop": current_stop, "trail_armed": armed_be, "exit_reason": exit_reason,
        "gross_pct": gross_pct, "net_pct": net_pct, "r_multiple": r_multiple, "win": net_pct > 0,
    }


def build_trade_candidates(
    full_df: pd.DataFrame, up_threshold: float = UP_THRESHOLD, down_threshold: float = DOWN_THRESHOLD,
    exclude_symbols: tuple[str, ...] = (),
) -> pd.DataFrame:
    """
    High-probability candidate selection:
      - 09:45 to 13:15 active liquid session window
      - Volatility filter: Minimum average 5-min bar range >= MIN_AVG_RANGE_PCT
      - Long: P(up) >= 0.52 + positive EMA slope + above VWAP + RSI corridor (45-72)
      - Short: P(up) <= 0.48 + negative EMA slope + below VWAP + RSI corridor (28-55)
    """
    tradable = full_df[
        full_df["p_up"].notna() & 
        ~full_df["symbol"].isin(exclude_symbols) &
        (full_df["minutes_since_open"] >= SESSION_START_MINUTES) &
        (full_df["minutes_since_open"] <= SESSION_CUTOFF_MINUTES) &
        (full_df["avg_range_pct"] >= MIN_AVG_RANGE_PCT)
    ].copy()

    direction = pd.Series(0, index=tradable.index)
    long_mask = (
        (tradable["p_up"] >= up_threshold) & 
        (tradable["prior_ema_slope_pct"] >= 0.0) & 
        (tradable["vwap_dist_pct"] >= 0.0) &
        (tradable["intraday_rsi"] >= 45) & (tradable["intraday_rsi"] <= 72)
    )
    short_mask = (
        (tradable["p_up"] <= down_threshold) & 
        (tradable["prior_ema_slope_pct"] <= -0.01) & 
        (tradable["vwap_dist_pct"] <= -0.01) &
        (tradable["rel_strength_vs_nifty"] <= 0.0) &
        (tradable["intraday_rsi"] >= 25) & (tradable["intraday_rsi"] <= 48)
    )
    
    direction[long_mask] = 1
    direction[short_mask] = -1
    
    tradable = tradable[direction != 0].copy()
    tradable["direction_int"] = direction[direction != 0]
    tradable["confidence"] = (tradable["p_up"] - 0.5).abs()

    trades = []
    for _, row in tradable.iterrows():
        d = row["direction_int"]
        trade = _simulate_trade(d, row["entry_price"], row["entry_dt"], row["stop_dist"], row["_window"])
        trade.update({
            "symbol": row["symbol"], "date": row["date"], "p_up": row["p_up"], "confidence": row["confidence"],
        })
        for col in FEATURE_COLUMNS:
            trade[col] = row[col]
        trades.append(trade)

    return pd.DataFrame(trades)


def enforce_no_overlap(candidates: pd.DataFrame) -> pd.DataFrame:
    """One trade at a time PER SYMBOL: processed in entry-time order, a candidate is dropped if its entry falls before the previous accepted trade (for that symbol) has exited — realistic for a single-account scalper that can't hold two positions in the same stock at once."""
    if candidates.empty:
        return candidates
    ordered = candidates.sort_values(["symbol", "entry_dt"])
    accepted = []
    blocked_until: dict[str, pd.Timestamp] = {}
    for _, row in ordered.iterrows():
        symbol = row["symbol"]
        if symbol in blocked_until and row["entry_dt"] <= blocked_until[symbol]:
            continue
        accepted.append(row)
        blocked_until[symbol] = row["exit_dt"]
    return pd.DataFrame(accepted)


def simulate_from_dataset(
    full_df: pd.DataFrame, up_threshold: float = UP_THRESHOLD, down_threshold: float = DOWN_THRESHOLD,
    exclude_symbols: tuple[str, ...] = (),
) -> pd.DataFrame:
    """
    Resimulate trades from an already-built, already-predicted pooled
    dataset (full_df must have 'p_up' from walk_forward_predict) under a
    different confidence threshold and/or symbol exclusion — for
    robustness/sensitivity checks that shouldn't need to rebuild years of
    history or retrain. No meta-labeling filter — see
    run_backtest_with_meta_label for that.
    """
    candidates = build_trade_candidates(full_df, up_threshold, down_threshold, exclude_symbols)
    return enforce_no_overlap(candidates)


def run_backtest(
    symbols: list[str], daily_universe: dict[pd.Timestamp, set[str]] | None = None, use_meta_label: bool = True,
    trade_only_symbols: set[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (trade_log_df, per_symbol_dataset_df) across the whole
    universe, using each symbol's full local-archive (5-minute) history.

    `daily_universe`, if given (strategy.screener.build_daily_universe),
    restricts each symbol to only the days it was actually picked by the
    "stocks in play" screen that day — a fixed list traded every day is
    the default (daily_universe=None) if this isn't provided.

    `use_meta_label=False` skips the meta-labeling stage (see
    run_with_meta_label) and trades directly off the primary model's
    threshold — meta-labeling needs enough candidate trades per
    walk-forward year to train its own model reliably, which a
    single-stock (or very small universe) run typically doesn't have;
    forcing it on anyway silently filters out nearly every trade rather
    than producing a meaningful result.

    `trade_only_symbols`: train on every symbol in `symbols` (pooled, for
    enough data), but only ever take trades on this subset — e.g. `symbols`
    = several bank stocks for training, `trade_only_symbols` = {"HDFCBANK"}
    to trade just one of them.
    """
    eligible_by_symbol: dict[str, set[pd.Timestamp]] | None = None
    if daily_universe is not None:
        eligible_by_symbol = {}
        for date, day_symbols in daily_universe.items():
            for symbol in day_symbols:
                eligible_by_symbol.setdefault(symbol, set()).add(date)

    datasets = []
    for symbol in symbols:
        log.info("run_backtest: building dataset for %s...", symbol)
        eligible_dates = eligible_by_symbol.get(symbol, set()) if eligible_by_symbol is not None else None
        if eligible_by_symbol is not None and not eligible_dates:
            continue  # this symbol was never picked by the screen on any day
        ds = build_symbol_dataset(symbol, eligible_dates)
        if not ds.empty:
            datasets.append(ds)

    if not datasets:
        return pd.DataFrame(), pd.DataFrame()

    full_df = pd.concat(datasets, ignore_index=True)
    full_df["p_up"] = walk_forward_predict(full_df)

    if use_meta_label:
        trade_log = run_with_meta_label(full_df, trade_only_symbols=trade_only_symbols)
    else:
        exclude = tuple(set(full_df["symbol"].unique()) - trade_only_symbols) if trade_only_symbols is not None else ()
        trade_log = simulate_from_dataset(full_df, exclude_symbols=exclude)
    return trade_log, full_df


def run_with_meta_label(
    full_df: pd.DataFrame, up_threshold: float = UP_THRESHOLD, down_threshold: float = DOWN_THRESHOLD,
    meta_threshold: float = META_THRESHOLD, trade_only_symbols: set[str] | None = None,
) -> pd.DataFrame:
    """
    Two-model pipeline: the primary model (already scored into
    full_df['p_up']) decides DIRECTION; a second, meta-labeling model
    decides whether that specific call is actually good enough to act
    on. Standard technique (López de Prado's "meta-labeling") for
    trading signal frequency for precision — rather than just tightening
    the primary confidence threshold (which this project already found
    barely moves results, a sign the primary probability isn't
    well-calibrated as a confidence ranking on its own).

    The meta-model is trained walk-forward BY YEAR, same discipline as
    the primary model: it only ever sees PRIOR years' candidate trades
    and their actual (already-simulated) win/loss outcome, and is scored
    on later years it never saw. Its inputs are the same base features
    plus the primary model's own confidence (|p_up - 0.5|) — so it's
    learning "when is the primary model's confidence trustworthy",
    not re-deriving direction from scratch.

    `trade_only_symbols`, if given, restricts the FINAL trade log to
    just those symbols — but candidate-building and meta-model training
    still use every symbol in `full_df`. This is the "train on a peer
    group, trade only the target" pattern: e.g. `full_df` built from
    several bank stocks pooled (for enough rows to train both models
    reliably), while only HDFCBANK's resulting signals are ever actually
    taken — narrowing this earlier (before meta-training) would starve
    the meta-model of data the same way single-stock primary training did.
    """
    candidates = build_trade_candidates(full_df, up_threshold, down_threshold)
    if candidates.empty:
        return candidates

    candidates["meta_p_win"] = walk_forward_predict(candidates, label_col="win", extra_features=["confidence"])
    filtered = candidates[candidates["meta_p_win"] >= meta_threshold]
    if trade_only_symbols is not None:
        filtered = filtered[filtered["symbol"].isin(trade_only_symbols)]
    return enforce_no_overlap(filtered)
