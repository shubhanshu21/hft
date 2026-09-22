"""
strategy/entry_signal.py — shared "should we enter, and at what levels"
decision logic for MCX commodities + NSE currency, used by BOTH
live_dryrun.py's DryRunner (paper, simulated fills) and live_trading.py's
LiveTrader (real orders).

Extracted 2026-09-18 from what used to be duplicated, nearly-identical code
in both files -- exactly the "keep these two in sync by hand" drift risk
that ENTRY_THRESHOLDS' own extraction (backtest_commodity.py/
backtest_currency.py -> live_dryrun.py, same day) was built to eliminate,
just reintroduced one file later when live_trading.py was written as its
own self-contained module. This is deliberately a pure function: no DB
writes, no broker order placement, no mutation of caller state -- it only
decides whether a tradeable setup exists right now and what its entry/SL/
TP/BE/lots would be. The two callers stay responsible for everything about
HOW that decision gets acted on (simulated fill vs real order).
"""
from __future__ import annotations

import pandas as pd

from strategy.commodity_costs import size_commodity_lots
from strategy.currency_costs import size_currency_lots
from strategy.commodity_features import compute_commodity_features, COMMODITY_FEATURE_COLUMNS
from backtest_commodity import ENTRY_THRESHOLDS as COMMODITY_ENTRY_THRESHOLDS
from backtest_currency import ENTRY_THRESHOLDS as CURRENCY_ENTRY_THRESHOLDS, _MIN_ORB as CURRENCY_MIN_ORB

CURRENCY_SYMBOLS = {"USDINR", "EURINR", "GBPINR", "JPYINR"}


def is_currency(sym: str) -> bool:
    return sym.upper() in CURRENCY_SYMBOLS


def compute_entry_signal(
    sym: str,
    candles: list[dict],
    instrument_key: str,
    full_session: bool,
    direction_filter: str,
    capital: float,
    risk_pct: float,
    leverage: float,
    regime_ok: bool | None = True,
) -> dict | None:
    """
    Returns a signal dict (direction, entry_price, sl, tp, be, lots,
    stop_dist, instrument_key, plus diagnostic fields used only for
    logging/Telegram: p_up, rsi, adx, vol_surge, vwap_dist_pct,
    ema_slope_pct) if a qualifying setup exists on the latest closed 5-min
    bar in `candles`, else None. `candles` must already be chronological
    (oldest-first) real 5-min OHLCV for `sym`, at least 25 bars.

    `regime_ok`: see strategy/regime.py -- currently only meaningful for
    CRUDEOILM (the one symbol this was found and validated for). False
    blocks a new entry regardless of every other condition below; None
    (not enough daily history yet to compute the gate) or True lets the
    normal rule-based decision proceed unaffected. Callers that don't pass
    this at all get the default True, i.e. no behavior change -- this
    parameter is opt-in per caller, not a silent new restriction.
    """
    if not candles or len(candles) < 25:
        return None

    raw_df = pd.DataFrame(candles)
    feat_df = compute_commodity_features(raw_df, symbol=sym)
    if feat_df is None or len(feat_df) < 25:
        return None

    t = len(feat_df) - 1
    row = feat_df.iloc[t]
    mins = int(row.get("minutes_since_open", 0))
    is_curr = is_currency(sym)

    if is_curr:
        # NSE currency derivatives trade 09:00-17:00 IST -- no MCX-style
        # evening/US-overlap session. 15-min open buffer + 16:50 square-off,
        # matching backtest_currency.py exactly.
        if mins < 15 or mins > 460:
            return None
    else:
        # Full session (10:00-22:30 IST) vs US/Evening-overlap-only
        # (18:30-22:00 IST) -- matches backtest_commodity.py's
        # us_session_only flag exactly.
        if full_session:
            if mins < 60 or mins > 810:
                return None
        else:
            if mins < 570 or mins > 780:
                return None

    p_up = 0.50

    adx = float(row.get("adx", 25.0))
    dmp = float(row.get("dmp", 25.0))
    dmn = float(row.get("dmn", 25.0))
    vol_s = float(row.get("vol_surge_ratio", 1.0))
    vwap_d = float(row.get("vwap_dist_pct", 0.0))
    ema_s = float(row.get("ema_slope_pct", 0.0))
    orb_h_dist = float(row.get("orb_high_dist_pct", 0.0))
    orb_l_dist = float(row.get("orb_low_dist_pct", 0.0))
    rsi = float(row.get("intraday_rsi", 50.0))
    entry = float(row["close"])
    atr = float(row.get("atr", 0.005 * entry))

    is_natgas = "NATGAS" in sym.upper() or "NATURALGAS" in sym.upper()
    is_gold = "GOLD" in sym.upper()
    is_silver = "SILVER" in sym.upper()
    if is_curr:
        _et = CURRENCY_ENTRY_THRESHOLDS.get(sym.upper(), CURRENCY_ENTRY_THRESHOLDS["USDINR"])
        min_ml_l, max_ml_s = 0.54, 0.44
        min_orb, min_stop_pct = CURRENCY_MIN_ORB, _et["min_stop_pct"]
    else:
        _key = "natgas" if is_natgas else "gold" if is_gold else "silver" if is_silver else "crude"
        _et = COMMODITY_ENTRY_THRESHOLDS[_key]
        min_ml_l, max_ml_s = _et["min_ml_l"], _et["max_ml_s"]
        min_orb, min_stop_pct = _et["min_orb"], _et["min_stop_pct"]
    min_adx, min_vol, min_vwap = _et["min_adx"], _et["min_vol"], _et["min_vwap"]
    min_ema_slope = _et["min_ema_slope"]
    tp_mult, stop_mult = _et["tp_mult"], _et["stop_mult"]

    sdist = max(stop_mult * atr, min_stop_pct * entry)
    if sdist <= 0 or entry <= 0:
        return None

    direction = None
    if adx >= min_adx and dmp > dmn and ema_s > min_ema_slope and orb_h_dist >= min_orb and vwap_d >= min_vwap and vol_s >= min_vol:
        direction = "long"
    elif direction_filter != "long" and adx >= min_adx and dmn > dmp and ema_s < -min_ema_slope and orb_l_dist <= -min_orb and vwap_d <= -min_vwap and vol_s >= min_vol:
        direction = "short"
    if not direction:
        return None
    if direction_filter != "both" and direction != direction_filter:
        return None
    if regime_ok is False:
        return None

    if is_curr:
        lots = size_currency_lots(capital, entry, sdist, risk_pct, sym, leverage)
    else:
        lots = size_commodity_lots(capital, entry, sdist, risk_pct, sym, leverage)
    if lots <= 0:
        return None

    d = 1 if direction == "long" else -1
    sl = round(entry - sdist * d, 2)
    tp = round(entry + tp_mult * sdist * d, 2)
    be = round(entry + 0.60 * sdist * d, 2)

    return {
        "symbol": sym, "direction": direction, "entry_price": entry,
        "sl": sl, "tp": tp, "be": be, "lots": lots, "stop_dist": sdist,
        "instrument_key": instrument_key,
        "p_up": p_up, "rsi": rsi, "adx": adx, "vol_surge": vol_s,
        "vwap_dist_pct": vwap_d, "ema_slope_pct": ema_s,
    }
