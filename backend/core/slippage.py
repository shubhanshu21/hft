"""
core/slippage.py — Adaptive per-symbol slippage estimator.

Replaces the flat ½-tick-per-leg assumption used by every cost model in this
project once enough real spread data has accumulated in logs/spread_samples.csv
(written by live_dryrun.py's DryRunner._maybe_sample_spread every
SPREAD_SAMPLE_INTERVAL_MIN per symbol).

A one-off snapshot taken 2026-09-18 found real bid-ask spreads running 2x–29x
wider than the ½-tick guess (CRUDEOILM 4x, GOLDM 25x, USDINR 3x, EURINR 2x,
GBPINR 29x). This module builds the empirical model from that accumulating CSV
so cost models can automatically switch from the flat guess to the real median
spread once a symbol has ≥ MIN_SAMPLES observations — no config change needed.

Falls back transparently to the original ½-tick estimate if:
  - The CSV doesn't exist yet (just started, or was cleared).
  - Fewer than MIN_SAMPLES rows exist for a symbol (not yet reliable).
  - Any read/parse error (fail open, never block a trade because of this).

Cache is refreshed every CACHE_TTL_SECONDS to pick up newly-logged rows without
requiring a restart.
"""
from __future__ import annotations

from core.paths import BACKEND_ROOT
import csv
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Minimum number of spread samples required before switching from the flat
# ½-tick guess to the empirical median. 20 samples at 5-minute intervals per
# symbol = ~100 minutes of real observations, a reasonable minimum.
MIN_SAMPLES = 20

# How long (seconds) to cache the loaded stats before re-reading the CSV.
# 3600 = once per hour, matching the cadence at which new samples accumulate.
CACHE_TTL_SECONDS = 3600

# Module-level cache: maps symbol -> median_spread (INR), plus a timestamp.
_cache: dict[str, float] = {}
_cache_loaded_at: float = 0.0
_csv_path: Path | None = None


def set_spread_log_path(path: Path) -> None:
    """Called once by the live runner to wire in the real log path.
    Falls back to the default relative path if never called."""
    global _csv_path
    _csv_path = path


def _resolve_csv() -> Path:
    if _csv_path is not None:
        return _csv_path
    # Fallback: assume this file lives inside backend/strategy/, so go up one
    # level to backend/ then into logs/.
    return BACKEND_ROOT / "logs" / "spread_samples.csv"


def _load_spread_stats() -> dict[str, float]:
    """Reads spread_samples.csv and returns {symbol: median_spread} for
    symbols with >= MIN_SAMPLES rows. Returns {} on any error."""
    csv_file = _resolve_csv()
    if not csv_file.exists():
        return {}
    try:
        rows: dict[str, list[float]] = {}
        with open(csv_file, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym = row.get("symbol", "").strip().upper()
                try:
                    spread = float(row["spread"])
                except (KeyError, ValueError):
                    continue
                rows.setdefault(sym, []).append(spread)

        result: dict[str, float] = {}
        for sym, spreads in rows.items():
            if len(spreads) >= MIN_SAMPLES:
                spreads_sorted = sorted(spreads)
                mid = len(spreads_sorted) // 2
                # Median: average of two middle elements for even-length lists
                if len(spreads_sorted) % 2 == 0:
                    median = (spreads_sorted[mid - 1] + spreads_sorted[mid]) / 2
                else:
                    median = spreads_sorted[mid]
                result[sym] = median
                log.debug(
                    "Adaptive slippage: %s median spread=%.5f from %d samples",
                    sym, median, len(spreads_sorted)
                )
            else:
                log.debug(
                    "Adaptive slippage: %s only %d samples (need %d) — using flat fallback",
                    sym, len(spreads), MIN_SAMPLES
                )
        return result
    except Exception as exc:
        log.warning("Could not load spread_samples.csv for adaptive slippage: %s", exc)
        return {}


def _refresh_cache_if_needed() -> None:
    global _cache, _cache_loaded_at
    if time.monotonic() - _cache_loaded_at > CACHE_TTL_SECONDS:
        _cache = _load_spread_stats()
        _cache_loaded_at = time.monotonic()
        if _cache:
            log.info(
                "Adaptive slippage cache refreshed: %d symbol(s) with empirical spreads: %s",
                len(_cache),
                {s: f"{v:.5f}" for s, v in _cache.items()},
            )


def adaptive_slippage_per_leg(sym: str, fallback_half_tick: float) -> float:
    """Returns the best available slippage estimate for ONE order leg (entry or
    exit) for `sym`.

    If a reliable empirical median spread is available (>= MIN_SAMPLES real
    observations), returns half that spread (since the spread is the full
    round-trip cost, and each leg pays half).

    Otherwise returns `fallback_half_tick`, which should be the caller's
    existing ½-tick estimate — so the behaviour is identical to before this
    module existed when data is sparse.

    Called by compute_mcx_commodity_costs, compute_ncd_currency_costs, and
    compute_nse_equity_costs to replace their hardcoded slip calculations.
    """
    _refresh_cache_if_needed()
    sym_key = sym.upper().split("|")[-1].split("2")[0]  # strip exchange prefix/expiry
    empirical = _cache.get(sym_key)
    if empirical is not None:
        return empirical / 2.0  # half-spread per leg
    return fallback_half_tick
