"""Real Upstox margin, so sizing never assumes leverage or a lot Upstox would refuse.

Upstox's margin calculator (POST /charges/margin, `broker.get_required_margin`) returns the SPAN + exposure margin it would actually block
for an order. Measured 2026-09-24 (MIS, per ONE lot / one share): CRUDEOILM Rs27.6k on Rs91k notional = 3.3x; GOLDM Rs140k on Rs1.5M =
10.7x; SILVER Rs901k on Rs7.0M = 7.7x; USDINR Rs2.3k on Rs96k = 42x; NIFTY50 stocks exactly 20% = 5x. A leverage configured above these is
imaginary, and a symbol whose ONE lot needs more margin than the account holds cannot trade at all.

Brokers change margins and lot sizes at any time (span revisions, exchange circulars), so nothing here is assumed to stay true:
  * `refresh` re-fetches every rate at start and every MARGIN_REFRESH_MIN (default 60) minutes, plus each trading-day rollover, and reads the
    CURRENT lot size from the live instrument master by exact instrument key (not from a hardcoded table);
  * `confirm_order` asks Upstox for the margin of the EXACT order (quantity and side) right before an entry opens, and shrinks or skips it
    if the account cannot carry it -- so a change made since the last refresh is caught on the very next order;
  * rates older than MARGIN_MAX_AGE_HOURS (default 6) are not trusted: without a fresh answer the entry is skipped (MARGIN_VERIFY=off disables
    this for tests only).
`cap_leverage` limits the configured leverage to the real one. Rates are kept in var/cache/margin_rates.json so a restart still has the last
real values; with no data at all the configured value is used and a warning is logged, never a silent guess.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime

from core.paths import CACHE_DIR

log = logging.getLogger("margin_rates")
RATES_PATH = CACHE_DIR / "margin_rates.json"
_rates: dict[str, dict] | None = None
last_alerted: tuple = ()          # symbols already reported as unaffordable, so the alert fires once per change


def _load(path=None) -> dict[str, dict]:
    global _rates
    if _rates is None or path is not None:
        try:
            _rates = json.loads((path or RATES_PATH).read_text())
        except (OSError, ValueError):
            _rates = {}
    return _rates


def _save(rates: dict, path=None) -> None:
    try:
        p = path or RATES_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rates, indent=1))
    except OSError:
        log.warning("could not write %s", path or RATES_PATH)


def real_leverage(symbol: str) -> float | None:
    """Notional / margin for one unit, from the last refresh; None if never fetched."""
    r = _load().get(symbol.upper())
    return float(r["leverage"]) if r and r.get("leverage") else None


def margin_per_unit(symbol: str) -> float | None:
    r = _load().get(symbol.upper())
    return float(r["margin"]) if r and r.get("margin") else None


def master_lot_size(broker, instrument_key: str) -> int | None:
    """The lot size Upstox lists TODAY for this exact instrument_key (nearest-expiry contract), or None if it cannot be read."""
    try:
        df = broker._cache.get_or_refresh()
        row = df[df["instrument_key"] == instrument_key]
        return int(float(row["lot_size"].iloc[0])) if len(row) else None
    except Exception as exc:
        log.warning("could not read the lot size for %s from the instrument master: %s", instrument_key, exc)
        return None


def _age_hours(rate: dict, now: datetime | None = None) -> float:
    try:
        return ((now or datetime.now()) - datetime.fromisoformat(rate["asof"])).total_seconds() / 3600
    except (KeyError, ValueError):
        return float("inf")


def verify_mode() -> str:
    return os.environ.get("MARGIN_VERIFY", "strict").strip().lower()


def confirm_order(broker, symbol: str, instrument_key: str | None, qty: int, side: str, free_capital: float, now: datetime | None = None) -> dict:
    """Ask Upstox what THIS order (qty units, `side` BUY/SELL, MIS) would block, and fit it to `free_capital`.

    Returns {"qty": int (0 = skip), "margin": float|None (for the qty returned), "source": "upstox"|"cached"|"unverified"|"off", "note": str}.
    Order of trust: a fresh answer from Upstox's margin calculator; else the last refreshed per-unit rate if it is younger than
    MARGIN_MAX_AGE_HOURS; else the entry is skipped (strict mode) because the margin cannot be verified."""
    if verify_mode() == "off":
        return {"qty": qty, "margin": None, "source": "off", "note": ""}
    margin = None
    if instrument_key and qty > 0:
        try:
            margin = broker.get_required_margin(instrument_key, qty, side, product="I")
        except Exception as exc:
            log.warning("margin check failed for %s: %s", symbol, exc)
    source = "upstox"
    if not margin:
        rate = _load().get(symbol.upper())
        max_age = float(os.environ.get("MARGIN_MAX_AGE_HOURS", "6"))
        if rate and rate.get("margin") and _age_hours(rate, now) <= max_age:
            margin, source = float(rate["margin"]) * qty, "cached"
        else:
            return {"qty": 0, "margin": None, "source": "unverified", "note": "Upstox margin could not be verified and no fresh rate is cached"}
    if margin <= free_capital:
        return {"qty": qty, "margin": float(margin), "source": source, "note": ""}
    fit = math.floor(qty * free_capital / margin)          # margin scales linearly with quantity (measured: 1:2:5 lots = 1:2:5 margin)
    if fit < 1:
        return {"qty": 0, "margin": None, "source": source,
                "note": f"one unit needs about Rs{margin / qty:,.0f} of margin but only Rs{free_capital:,.0f} is free"}
    return {"qty": fit, "margin": float(margin) * fit / qty, "source": source,
            "note": f"Upstox margin Rs{margin:,.0f} for {qty} exceeds the Rs{free_capital:,.0f} available: cut to {fit}"}


def cap_leverage(symbol: str, configured: float) -> float:
    """The leverage sizing may use: the configured value, never above what Upstox really gives for this symbol."""
    real = real_leverage(symbol)
    return min(float(configured), real) if real else float(configured)


def refresh(broker, keys: dict[str, str], notional_per_unit, path=None, sleep_s: float = 0.15,
            equity_syms: frozenset = frozenset(), mismatches: list | None = None) -> dict[str, dict]:
    """Fetch the real MIS margin for ONE unit (1 lot for MCX/NCD futures, 1 share for equity) of every symbol.
    `mismatches` (optional list) receives (symbol, previous master lot size, new master lot size) for every contract the broker revised.
    `keys`: symbol -> instrument_key; `notional_per_unit(symbol, price)`: contract value of one unit. Failures keep the previous value."""
    rates = dict(_load(path))
    now = datetime.now().isoformat(timespec="seconds")
    mismatches = mismatches if mismatches is not None else []
    for sym, key in keys.items():
        try:
            margin = broker.get_required_margin(key, 1, "BUY", product="I")
            price = broker.get_ltp(key)
            if price is None:                                        # market closed: the last daily close is a fine price for a ratio
                daily = broker.get_historical_candles(key, "days", 1, datetime.now().date().isoformat()) or []
                price = float(daily[-1]["close"]) if daily else None
            if not margin or not price:
                continue
            notional = float(notional_per_unit(sym, float(price)))
            lev = notional / float(margin)
            if not 1.0 <= lev <= 200.0:                                        # a ratio outside anything real means a units problem: distrust it, keep the old rate
                log.error("margin ratio for %s is implausible (%.2fx from notional %.0f / margin %.0f); ignoring this reading", sym, lev, notional, margin)
                continue
            # The master's lot_size is NOT in the cost model's units (GOLDM: master 100 g vs a 10x price multiplier; USDINR: 1 vs 1000), so it is only ever
            # compared with what the MASTER said last time: a change there means the broker revised the contract. The notional is left alone.
            live_lot = master_lot_size(broker, key) if sym.upper() not in equity_syms else None
            prev_lot = (rates.get(sym.upper()) or {}).get("lot_size")
            if live_lot and prev_lot and live_lot != prev_lot:
                mismatches.append((sym, prev_lot, live_lot))
            rates[sym.upper()] = {"margin": float(margin), "price": float(price), "notional": notional,
                                  "leverage": round(lev, 3), "asof": now, "lot_size": live_lot or prev_lot}
        except Exception as exc:                                     # one symbol failing must not lose the others
            log.warning("margin refresh failed for %s: %s", sym, exc)
        time.sleep(sleep_s)
    _save(rates, path)
    global _rates
    _rates = rates
    return rates


def unaffordable(capital: float, symbols) -> list[tuple[str, float]]:
    """(symbol, margin for one unit) for every symbol whose ONE unit needs more margin than `capital` -- they cannot trade at all."""
    out = []
    for s in symbols:
        m = margin_per_unit(s)
        if m and m > capital:
            out.append((s, m))
    return out
