"""
markets/commodity/scalping/entry_signal.py — shared "should we enter, and at what levels"
decision logic for MCX commodities + NSE currency, used by BOTH
live_dryrun.py's DryRunner (paper, simulated fills) and live_trading.py's
LiveTrader (real orders).

Extracted 2026-09-18 from what used to be duplicated, nearly-identical code
in both files -- exactly the "keep these two in sync by hand" drift risk
that ENTRY_THRESHOLDS' own extraction (markets/commodity/scalping/backtest.py/
markets/currency/scalping/backtest.py -> live_dryrun.py, same day) was built to eliminate,
just reintroduced one file later when live_trading.py was written as its
own self-contained module. This is deliberately a pure function: no DB
writes, no broker order placement, no mutation of caller state -- it only
decides whether a tradeable setup exists right now and what its entry/SL/
TP/BE/lots would be. The two callers stay responsible for everything about
HOW that decision gets acted on (simulated fill vs real order).
"""
from __future__ import annotations

from core import sessions

import logging
import os

import pandas as pd

log = logging.getLogger(__name__)

from markets.commodity.costs import size_commodity_lots
from markets.currency.costs import size_currency_lots
from markets.commodity.features import compute_commodity_features, COMMODITY_FEATURE_COLUMNS
from markets.commodity.scalping.backtest import ENTRY_THRESHOLDS as COMMODITY_ENTRY_THRESHOLDS
from markets.currency.scalping.backtest import ENTRY_THRESHOLDS as CURRENCY_ENTRY_THRESHOLDS, _MIN_ORB as CURRENCY_MIN_ORB
from core.regime import regime_ok as _intraday_chop_ok

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
    logging/Telegram: rsi, adx, vol_surge, vwap_dist_pct,
    ema_slope_pct) if a qualifying setup exists on the latest closed 5-min
    bar in `candles`, else None. `candles` must already be chronological
    (oldest-first) real 5-min OHLCV for `sym`, at least 25 bars.

    `regime_ok`: see core/regime.py -- currently only meaningful for
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
        # matching markets/currency/scalping/backtest.py exactly.
        if mins < 15 or mins > sessions.last_entry_since_open("currency"):          # from Upstox's session hours (core/sessions.py)
            return None
    else:
        # Full session (10:00-22:30 IST) vs US/Evening-overlap-only
        # (18:30-22:00 IST) -- matches markets/commodity/scalping/backtest.py's
        # us_session_only flag exactly.
        if full_session:
            if mins < 60 or mins > sessions.last_entry_since_open("commodity"):
                return None
        else:
            if mins < 570 or mins > 780:
                return None

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
        min_orb, min_stop_pct = CURRENCY_MIN_ORB, _et["min_stop_pct"]
    else:
        _key = "natgas" if is_natgas else "gold" if is_gold else "silver" if is_silver else "crude"
        _et = COMMODITY_ENTRY_THRESHOLDS[_key]
        min_orb, min_stop_pct = _et["min_orb"], _et["min_stop_pct"]
    min_adx, min_vol, min_vwap = _et["min_adx"], _et["min_vol"], _et["min_vwap"]
    min_ema_slope = _et["min_ema_slope"]
    tp_mult, stop_mult = _et["tp_mult"], _et["stop_mult"]

    sdist = max(stop_mult * atr, min_stop_pct * entry)
    if sdist <= 0 or entry <= 0:
        return None

    direction = None
    setup_type = "trend_breakout"
    # ENABLE_MEAN_REVERSION in .env gates Setup 2. Read once here, before the
    # if/elif chain, so the elif branch is syntactically valid Python.
    _enable_mr = os.environ.get("ENABLE_MEAN_REVERSION", "true").lower() in ("1", "true", "yes")

    # Setup 1 (LONG): Trend Expansion Breakout (steady accumulation)
    if adx >= min_adx and dmp > dmn and ema_s > min_ema_slope and orb_h_dist >= min_orb and vwap_d >= min_vwap and vol_s >= min_vol:
        direction = "long"
        setup_type = "trend_breakout"

    # Setup 1 (SHORT): the mirror of the long -- the same thresholds the backtests validated (markets/commodity/scalping/backtest.py,
    # markets/currency/scalping/backtest.py). Until 2026-10-05 live shorts used stricter, never-backtested rules (ADX +3, slope x1.2, ORB/VWAP
    # x1.1, volume x1.25, take-profit x0.8, breakeven at 0.4R). Put into the backtests as `live_short_rules` and run on the real archive, they
    # took fewer trades and made less: TEST (2026-08-01..10-01) +Rs31,084 vs +Rs43,531 symmetric across SILVERMIC/CRUDEOILM/GOLDTEN/USDINR,
    # TRAIN (05-18..07-31) +Rs18,205 vs +Rs20,491.
    elif direction_filter != "long" and adx >= min_adx and dmn > dmp and ema_s < -min_ema_slope and orb_l_dist <= -min_orb and vwap_d <= -min_vwap and vol_s >= min_vol:
        direction = "short"
        setup_type = "trend_breakout"

    # Setup 2: Statistical VWAP Mean-Reversion Extremes (MCX Commodities only, not currencies).
    # Deliberately NOT blocked by regime_ok: mean-reversion BENEFITS from choppy/mean-reverting
    # regimes (exactly the conditions the regime gate blocks trend breakouts for). Controlled
    # independently by ENABLE_MEAN_REVERSION in .env.
    elif not is_curr and _enable_mr:
        if vwap_d <= -1.2 and rsi <= 25.0:
            direction = "long"
            setup_type = "mean_reversion"
            tp_mult = 1.5
        elif direction_filter != "long" and vwap_d >= 1.5 and rsi >= 78.0:
            direction = "short"
            setup_type = "mean_reversion"
            tp_mult = 1.4

    if not direction:
        return None
    if direction_filter != "both" and direction != direction_filter:
        return None
    if regime_ok is False and setup_type == "trend_breakout":
        return None

    # Intraday chop gate -- SILVERMIC only (2026-10-01, loss review: 4 of 5 recent
    # SILVERMIC trades hit initial_stop, alternating long AND short within the same
    # two days -- a whipsaw pattern, not a directional miss). Same mechanism as the
    # daily regime_ok gate above (core/regime.py's rolling-autocorrelation test) but
    # computed on 5-MIN BAR closes over a short trailing window instead of daily
    # closes over 15 days -- the daily gate has one data point per day and cannot see
    # a choppy stretch WITHIN a single session at all. Validated on the real archive
    # (markets/commodity/scalping/backtest.py's intraday_chop_window param) with a
    # proper train/test split: window=20 bars (~100 min) improved PF on BOTH windows
    # independently (TRAIN 1.19->1.46, TEST 1.60->1.72) at roughly flat full-period net
    # profit. NOT extended to CRUDEOILM/GOLDTEN -- tested there too, and no window
    # held up on both train AND test (noise, not a real signal, consistent with crude/
    # gold's already-known weak or absent edge -- see markets/commodity/scalping/backtest.py's
    # ENTRY_THRESHOLDS comments). Like regime_ok, only gates trend_breakout: mean-
    # reversion benefits from exactly the chop this is built to detect. Env-tunable;
    # 0 = off (not SILVERMIC, or not configured).
    if setup_type == "trend_breakout" and is_silver:
        _chop_window = int(os.environ.get("SILVERMIC_INTRADAY_CHOP_WINDOW", "0"))
        if _chop_window > 0:
            _chop_closes = [float(c["close"]) for c in candles[-(_chop_window + 5):]]
            if _intraday_chop_ok(_chop_closes, window=_chop_window, min_autocorr=0.0) is False:
                # The one deliberate exception to this module's "pure function, no side effects" rule (see the
                # module docstring) -- otherwise this filter's effect is invisible, a silently absent trade
                # indistinguishable from "no setup existed at all". A qualifying setup DID exist here (direction
                # and setup_type are already resolved above); only the chop gate is turning it away.
                log.info("SILVERMIC: %s trend_breakout signal at %.2f blocked by the intraday chop gate "
                         "(window=%d bars, trailing autocorrelation < 0 -- choppy, not trending).", direction, entry, _chop_window)
                return None

    # Intraday chop gate -- USDINR only (2026-10-03, leverage review: USDINR's own train/test split showed the
    # exact same whipsaw pattern as SILVERMIC -- the recent/TEST window was a net loser (PF 0.54) at every
    # leverage level tried, unrelated to leverage or exit mode (fixed vs dynamic exit gave the same result).
    # Unlike SILVERMIC, a single window wasn't a lucky pick: window=45 through 96 bars ALL turned TEST
    # profitable (PF 1.02-4.12) while TRAIN stayed strongly profitable (PF 3.31-5.59) -- a stable plateau, not
    # one cherry-picked point. window=65 (middle of that range) was chosen specifically because it's also the
    # smallest window whose TEST trade count (15) clears the project's >=15-trade credibility bar; narrower
    # windows had even better TEST PF (up to 4.12 at window=55) but only 10-12 TEST trades, too thin to trust
    # alone. See markets/currency/scalping/backtest.py's intraday_chop_window param for the sweep.
    if setup_type == "trend_breakout" and is_curr and sym.upper() == "USDINR":
        _chop_window = int(os.environ.get("USDINR_INTRADAY_CHOP_WINDOW", "0"))
        if _chop_window > 0:
            _chop_closes = [float(c["close"]) for c in candles[-(_chop_window + 5):]]
            if _intraday_chop_ok(_chop_closes, window=_chop_window, min_autocorr=0.0) is False:
                log.info("USDINR: %s trend_breakout signal at %.4f blocked by the intraday chop gate "
                         "(window=%d bars, trailing autocorrelation < 0 -- choppy, not trending).", direction, entry, _chop_window)
                return None

    if is_curr:
        lots = size_currency_lots(capital, entry, sdist, risk_pct, sym, leverage)
    else:
        lots = size_commodity_lots(capital, entry, sdist, risk_pct, sym, leverage)
    if lots <= 0:
        return None

    d = 1 if direction == "long" else -1
    nd = 4 if is_curr else 2                                    # currency ticks are 0.0025: 2 decimals moved USDINR levels by up to ~8% of a stop distance
    sl = round(entry - sdist * d, nd)
    tp = round(entry + tp_mult * sdist * d, nd)
    be = round(entry + 0.60 * sdist * d, nd)                    # breakeven arm at 0.6R both ways, as in the backtests (BE_ACTIVATION_MULT)

    return {
        "symbol": sym, "direction": direction, "entry_price": entry,
        "sl": sl, "tp": tp, "be": be, "lots": lots, "stop_dist": sdist,
        "instrument_key": instrument_key,
        "setup_type": setup_type,
        "rsi": rsi, "adx": adx, "vol_surge": vol_s,
        "vwap_dist_pct": vwap_d, "ema_slope_pct": ema_s,
    }
