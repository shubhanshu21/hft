#!/usr/bin/env python3
"""
backend/backtest_currency.py — 5-Minute NSE Currency Derivatives Scalping Backtest Engine

Mirrors markets/commodity/strategies/scalping/backtest.py's proven structure exactly (same entry rules,
same stop/target/trailing mechanics, same statutory-cost-aware simulation
loop), pointed at USDINR/EURINR/GBPINR/JPYINR instead of MCX commodities.

Differences from the commodity path, both real and deliberate:
  - Cost model: markets/currency/costs.py (NO STT/CTT on currency
    derivatives -- a real, verified cost advantage; different stamp duty
    and exchange-fee rates -- see that module's docstring).
  - Session: NSE currency derivatives trade 09:00-17:00 IST (no MCX-style
    evening/US-overlap session -- there's no analogous "second session" for
    a currency pair the way crude/natgas track NYMEX's evening hours).
  - Entries are purely rule-based (no ML model).
  - ENTRY_THRESHOLDS starts as ONE unvalidated set (borrowed from gold's
    calibration, since gold was the most robustly successful commodity
    threshold set found) applied to all four pairs -- explicitly NOT yet
    per-pair calibrated the way crude/natgas/gold each earned their own
    dedicated sweep. Treat this run as a first, honest baseline, not a
    tuned result.

Usage:
    python3 -m markets.currency.strategies.scalping.backtest --symbols USDINR
    python3 -m markets.currency.strategies.scalping.backtest --symbols USDINR EURINR GBPINR JPYINR
"""
from __future__ import annotations

from core import sessions

from core import entry_pullback
from core.exits import bar_exit, intrabar_exit, lock_stop
from core.minute_bars import MinuteBars
from core.paths import ARCHIVE_ROOT, BACKEND_ROOT
from core.regime import regime_ok as _regime_ok_intraday
import argparse
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(BACKEND_ROOT))
load_dotenv(dotenv_path=BACKEND_ROOT / ".env")

from markets.currency.costs import compute_ncd_currency_costs, size_currency_lots
from markets.commodity.features import compute_commodity_features

ARCHIVE_DIR = ARCHIVE_ROOT / "currency"

HOLD_BARS = 16
TAKE_PROFIT_MULT = 1.80
STOP_VOL_MULT = 1.4
BE_ACTIVATION_MULT = 0.60
TRAIL_DIST_MULT = 0.30
BE_LOCK_BUFFER_PCT = 0.0020

# Per-pair calibrated thresholds, swept 2026-09-18 (375 combos each: ema_slope
# x adx x vol_surge x vwap_dist, requiring >=15 trades to be credible --
# same discipline as gold/crude's own dedicated sweeps):
#   USDINR: 258/258 credible combos profitable (100%) -- the most robust
#           result found in this entire project, commodities included.
#   GBPINR: 192/192 credible combos profitable (100%).
#   EURINR: 201/359 credible combos profitable (56%) -- still a real,
#           majority-robust edge, just less universal than USDINR/GBPINR.
#   JPYINR: 0/375 combos reached 15 trades at all -- not a negative
#           finding, just not enough real trading days yet (youngest
#           contract, ~24 days of archive). Left on the unvalidated
#           baseline; revisit once more real data accumulates.
# All four show the SAME distinctive shape: win rate well under 50%
# (39-49%), but strong profit factors (2.2-3.8x) and small drawdowns
# (<4.3%) -- profit from asymmetric win/loss size, not win rate. Genuinely
# different payoff shape than crude/gold's 65-70%-win-rate/tight-R:R style.
# tp_mult/stop_mult added 2026-09-18: a follow-up 40-combo-per-pair sweep of the
# take-profit/stop-distance multipliers (previously the shared TAKE_PROFIT_MULT=1.80/
# STOP_VOL_MULT=1.4 constants below, borrowed unvalidated from commodities) found a
# clean win for all three live pairs -- better win rate, net PnL, AND profit factor
# simultaneously, not a tradeoff:
#   USDINR: win 48.9%->51.2%, net +Rs13,854->+Rs17,240, PF 3.13->3.65 (tp=2.6, stop=2.0)
#   EURINR: net +Rs8,416->+Rs9,458, PF 2.23->2.34 (tp=3.0, stop=1.0)
#   GBPINR: win 41.2%->47.1%, net +Rs3,848->+Rs6,694, PF 2.87->4.36 (tp=2.2, stop=1.0)
# 40/40 credible combos were profitable for every pair in that sweep -- currency's
# low-win-rate/high-profit-factor payoff shape rewards a wider target much more than
# commodities' tighter-R:R style did.
#
# Genuine train/test split attempted 2026-09-19 (train 2026-08-21..2026-09-07,
# test 2026-09-08..2026-09-17 -- the full archive is only ~28 real days, so this
# is a much smaller split than gold/silver/crude's ~4-month one). EURINR held up
# cleanly on both sides of a real split: train 25 trades/PF 1.84/net +3.41%,
# test 18 trades/PF 3.35/net +6.05% -- both independently clear the 15-trade
# credibility bar. GBPINR could NOT be genuinely validated this way -- its full
# archive is only 17 trades total, so splitting it leaves 10 (train) and 7
# (test) trades, both below the credibility bar on their own. Both halves were
# directionally profitable, but with samples this small that's not evidence,
# just an inconclusive split -- treated the same as SILVER's under-proven
# single-fold case, sized down rather than pulled since the full-period number is at
# least real. The sizing fix is a leverage override, not risk_pct, though -- see
# live_dryrun.py's _SYMBOL_LEVERAGE_OVERRIDE comment for why (a risk_pct override was
# tried first and confirmed to be a complete no-op for this symbol).
# USDINR re-tuned 2026-09-19 per an explicit user request to optimize for win
# rate/fewer losses over raw total profit: min_vol 1.0->1.3, min_ema_slope
# 0.004->0.003, tp_mult 2.6->3.5, stop_mult 2.0->1.0. Validated on a genuine
# train/test split (train 2026-06-02..2026-08-10, test 2026-08-11..2026-09-17):
# TRAIN win 47.0%->55.2% (PF 4.63->6.89, net +Rs27,553->+Rs34,827), TEST win
# 52.0%->54.2% (PF 4.02->4.64, DD 1.44%->1.33%) -- improved on almost every
# metric on BOTH windows, with only a negligible ~Rs300 dip in TEST net. About
# as close to a clean win as this project's sweeps have found.
ENTRY_THRESHOLDS = {
    "USDINR": {"min_adx": 15.0, "min_vol": 1.3, "min_vwap": 0.04, "min_stop_pct": 0.0006, "min_ema_slope": 0.003, "tp_mult": 3.5, "stop_mult": 1.0},
    "EURINR": {"min_adx": 10.0, "min_vol": 1.1, "min_vwap": 0.06, "min_stop_pct": 0.0006, "min_ema_slope": 0.008, "tp_mult": 3.0, "stop_mult": 1.0},
    "GBPINR": {"min_adx": 10.0, "min_vol": 1.0, "min_vwap": 0.04, "min_stop_pct": 0.0006, "min_ema_slope": 0.008, "tp_mult": 2.2, "stop_mult": 1.0},
    "JPYINR": {"min_adx": 12.0, "min_vol": 1.30, "min_vwap": 0.05, "min_stop_pct": 0.0006, "min_ema_slope": 0.008, "tp_mult": 1.80, "stop_mult": 1.4},  # unvalidated, see above -- left on the old shared default
}
_MIN_ORB = 0.05  # min_orb showed little discriminating power in the sweep (unlike ema_slope/adx) -- kept at gold's value, not over-fit as a 5th dimension


def run_currency_backtest(
    symbols: list[str] | None = None,
    capital: float = 200000.0,
    risk_pct: float = 2.5,
    leverage: float = 5.0,
    long_only: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
    size_mode: str = "margin",
    return_trades: bool = False,
    exit_mode: str = "fixed",        # opt-in research options (docs/STATIC_VS_DYNAMIC.md), off = live behaviour: "dynamic" = ADX-scaled activation / current-ATR trail / no TP; "tp_scaled" = TP x ADX scale
    hold_mode: str = "fixed",        # "adx" = the 16-bar limit x the ADX scale
    stop_floor_mode: str = "fixed",  # "dynamic" = stop >= 1.0 x median ATR of the previous 5 days instead of the fixed % floor
    adaptive_window: int = 0,        # thresholds (ADX, volume surge, |EMA slope|) = rolling quantile over the previous N bars at the quantile the fixed number represents
    pullback_frac: float = 0.0,      # opt-in research option, see core/entry_pullback.py (0 = live behaviour)
    pullback_bars: int = 3,
    pullback_through: float = 0.0,
    daily_regime_window: int = 0,    # opt-in research (2026-10-03, USDINR leverage review): core.regime.regime_ok on daily closes, same mechanism as
    # CRUDEOILM's live gate (markets/commodity/strategies/scalping/backtest.py) -- but NOT assumed to have the same sign: USDINR's own train/test split showed the
    # OPPOSITE relationship (profitable window had NEGATIVE daily autocorrelation, losing window near zero/positive), so `daily_regime_min_autocorr` and
    # `daily_regime_invert` are exposed separately rather than hard-coding crude's convention. 0 = off (live behaviour).
    daily_regime_min_autocorr: float = 0.0,
    daily_regime_invert: bool = False,  # True: block when autocorr >= min_autocorr instead of when it's below (see the sign note above)
    live_short_rules: bool = False,  # research (2026-10-05): reproduce entry_signal.py's live short rules (ADX +3, slope x1.2, ORB/VWAP x1.1, volume x1.25, TP x0.8 min 1.4, breakeven 0.4R) -- never in the backtests before
    intraday_chop_window: int = 0,   # same idea as markets/commodity/strategies/scalping/backtest.py's param of the same name, on 5-min closes instead of daily
    intraday_chop_min_autocorr: float = 0.0,
    intraday_chop_invert: bool = False,
    minute_fills: bool = True,       # settle stop / target fills inside a 5-minute bar from the 1-minute archive where it exists (docs/FILL_MODEL_AUDIT.md); False = 5-minute bars only
) -> dict:
    target_symbols = symbols or ["USDINR"]

    all_trades: list[dict] = []
    current_capital = capital
    equity_curve = [capital]

    _daily_regime_by_sym_date: dict = {}
    if daily_regime_window:
        from core.regime import regime_ok as _daily_regime_ok
        for _s in target_symbols:
            _s_path = ARCHIVE_DIR / f"{_s.upper()}_5minute.csv"
            if not _s_path.exists():
                continue
            _s_raw = pd.read_csv(_s_path)
            _s_raw["_dt"] = pd.to_datetime(_s_raw["timestamp"])
            _daily = _s_raw.set_index("_dt")["close"].resample("1D").last().dropna()
            _closes, _dates = list(_daily.values), list(_daily.index.date)
            for _i, _d in enumerate(_dates):
                _r = _daily_regime_ok(_closes[:_i], window=daily_regime_window, min_autocorr=daily_regime_min_autocorr)
                _daily_regime_by_sym_date[(_s.upper(), _d)] = (not _r if (daily_regime_invert and _r is not None) else _r)

    _archive_lo, _archive_hi = None, None
    for _sym in target_symbols:
        _p = ARCHIVE_DIR / f"{_sym.upper()}_5minute.csv"
        if _p.exists():
            _ts = pd.read_csv(_p, usecols=lambda c: c in ("timestamp", "date"))
            _ts_col = "timestamp" if "timestamp" in _ts.columns else "date"
            _ts[_ts_col] = pd.to_datetime(_ts[_ts_col])
            _lo, _hi = _ts[_ts_col].min(), _ts[_ts_col].max()
            _archive_lo = _lo if _archive_lo is None else min(_archive_lo, _lo)
            _archive_hi = _hi if _archive_hi is None else max(_archive_hi, _hi)
    _actual_start = from_date or (str(_archive_lo.date()) if _archive_lo is not None else "?")
    _actual_end = to_date or (str(_archive_hi.date()) if _archive_hi is not None else "?")
    period_str = f"{_actual_start} to {_actual_end}"
    if _archive_lo is not None and from_date is None and to_date is None:
        _n_days = (_archive_hi.date() - _archive_lo.date()).days + 1
        period_str += f"  ({_n_days} calendar days -- real NCD_FO archives are capped to ~1 month/contract, same constraint as MCX)"

    print(f"\n{'='*75}")
    print(f"  NSE CURRENCY 5-MINUTE SCALPER WALK-FORWARD BACKTEST")
    print(f"  Capital: ₹{capital:,.0f} | Risk: {risk_pct}% | Leverage: {leverage}x | Long-Only: {long_only}")
    print(f"  Period: {period_str}")
    print(f"  Session: Full NSE currency session (09:00-17:00 IST)")
    print(f"  Entry thresholds: per-pair calibrated (USDINR/GBPINR 100% of credible sweep combos profitable, EURINR 56%, JPYINR unvalidated -- see ENTRY_THRESHOLDS comment)")
    print(f"  Symbols ({len(target_symbols)}): {', '.join(target_symbols)}")
    print(f"{'='*75}\n")

    def _record_trade(sym: str, pos: dict, exit_p: float, reason: str, exit_time, stopped_on_fill: bool = False) -> None:
        nonlocal current_capital
        cost_info = compute_ncd_currency_costs(sym, pos["direction"], pos["entry_price"], exit_p, pos["lots"])
        net_pnl = cost_info["net"]
        current_capital += net_pnl
        equity_curve.append(current_capital)

        all_trades.append({
            "symbol": sym, "direction": pos["direction"],
            "entry_time": pos["entry_time"], "exit_time": exit_time,
            "entry_price": pos["entry_price"], "exit_price": exit_p,
            "lots": pos["lots"], "gross_pnl": cost_info["gross"],
            "total_fees": cost_info["total"], "net_pnl": net_pnl, "reason": reason,
            # exit parameters as set at entry -- lets a finer-grained replay (engine/live_replay.py) re-run the exit on 1-minute data
            "sl": pos["sl"], "tp": pos["tp"], "be": pos["be"], "stop_dist": pos["stop_dist"],
            "trail_mult": pos["trail_mult"], "hold_bars": pos.get("hold", HOLD_BARS), "stopped_on_fill": stopped_on_fill,
            **cost_info,
        })

    for sym in target_symbols:
        csv_path = ARCHIVE_DIR / f"{sym.upper()}_5minute.csv"
        if not csv_path.exists():
            print(f"  {sym}: no archive found, skipping.")
            continue

        raw_df = pd.read_csv(csv_path)
        feat_df = compute_commodity_features(raw_df, symbol=sym.upper())
        minute_bars = MinuteBars(csv_path.with_name(csv_path.name.replace("_5minute", "_1minute")) if minute_fills else None)

        in_pos = False
        pos = {}
        pending_pb = None

        n = len(feat_df)
        opens = feat_df["open"].values
        closes = feat_df["close"].values
        highs = feat_df["high"].values
        lows = feat_df["low"].values
        rsis = feat_df["intraday_rsi"].values
        vwap_ds = feat_df["vwap_dist_pct"].values
        ema_slopes = feat_df["ema_slope_pct"].values
        mins_open = feat_df["minutes_since_open"].values
        timestamps = feat_df["timestamp"].values
        adxs = feat_df["adx"].values if "adx" in feat_df.columns else np.full(n, 25.0)
        dmps = feat_df["dmp"].values if "dmp" in feat_df.columns else np.full(n, 25.0)
        dmns = feat_df["dmn"].values if "dmn" in feat_df.columns else np.full(n, 25.0)
        vol_surges = feat_df["vol_surge_ratio"].values if "vol_surge_ratio" in feat_df.columns else np.full(n, 1.0)
        atrs = feat_df["atr"].values if "atr" in feat_df.columns else (feat_df["avg_range_pct"].values / 100.0) * closes
        atr_med5d = pd.Series(atrs).rolling(480, min_periods=100).median().shift(1).to_numpy() if stop_floor_mode == "dynamic" else None
        _adaptive_ready = False

        _et = ENTRY_THRESHOLDS.get(sym.upper(), ENTRY_THRESHOLDS["USDINR"])

        for i in range(25, n):
            c_price = closes[i]
            c_high  = highs[i]
            c_low   = lows[i]
            c_time  = timestamps[i]
            c_day   = str(c_time)[:10]
            m_open  = mins_open[i]

            if from_date and c_day < from_date:
                continue
            if to_date and c_day > to_date:
                continue

            if in_pos:
                d = 1 if pos["direction"] == "long" else -1
                fav = c_high if d == 1 else c_low
                adv = c_low  if d == 1 else c_high

                exit_p = None; reason = None
                hit = bar_exit(pos["current_stop"], pos["tp"], opens[i], fav, adv, d)      # stop first when one bar reaches both; the open when it opened through the stop
                if hit is not None and minute_bars:
                    mins = minute_bars.get(c_time)
                    if mins is not None:
                        hit = intrabar_exit(pos["current_stop"], pos["tp"], mins, d) or hit     # which came first, and a jump through the stop inside the bar
                if hit is not None:
                    exit_p = hit[0]; reason = "take_profit" if hit[1] == "tp" else ("be_stop" if pos["armed_be"] else "initial_stop")
                elif (i - pos["entry_idx"]) >= pos.get("hold", HOLD_BARS):
                    exit_p = c_price; reason = "timeout_exit"
                elif m_open >= sessions.CURRENCY_SQUAREOFF_MIN:  # square-off, ahead of Upstox's 16:30 auto square-off and the 17:00 close
                    exit_p = c_price; reason = "eod_squareoff"

                if exit_p is not None:
                    _record_trade(sym, pos, exit_p, reason, c_time)
                    in_pos = False
                    pos = {}
                    continue
                else:
                    if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
                        pos["armed_be"] = True
                        pos["current_stop"] = lock_stop(pos["entry_price"], pos["be"], d)      # never above what price reached: see core/exits.lock_stop
                    if pos["armed_be"]:
                        pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
                        if exit_mode == "dynamic":
                            trail = pos["best_price"] - pos["trail_mult"] * max(atrs[i], pos["stop_dist"] * 0.1) * d
                        else:
                            trail = pos["best_price"] - TRAIL_DIST_MULT * pos["stop_dist"] * d
                        pos["current_stop"] = (max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail))
                continue

            # Full session only -- no MCX-style evening-window gate for currency
            if m_open < 15 or m_open > sessions.CURRENCY_LAST_ENTRY_MIN:
                continue

            adx = adxs[i]
            dmp = dmps[i]
            dmn = dmns[i]
            vol_s = vol_surges[i]
            vwap_d = vwap_ds[i]
            ema_s = ema_slopes[i]
            orb_h_dist = feat_df["orb_high_dist_pct"].values[i]
            orb_l_dist = feat_df["orb_low_dist_pct"].values[i]
            atr = atrs[i]

            min_adx = _et["min_adx"]
            min_vol = _et["min_vol"]
            min_orb = _MIN_ORB
            min_vwap = _et["min_vwap"]
            min_stop_pct = _et["min_stop_pct"]
            min_ema_slope = _et["min_ema_slope"]
            tp_mult = _et.get("tp_mult", TAKE_PROFIT_MULT)
            stop_mult = _et.get("stop_mult", STOP_VOL_MULT)

            _floor = min_stop_pct * c_price
            if stop_floor_mode == "dynamic" and atr_med5d is not None and not np.isnan(atr_med5d[i]):
                _floor = 1.0 * atr_med5d[i]
            sdist = max(stop_mult * atr, _floor)
            if adaptive_window and not _adaptive_ready:
                _adaptive_ready = True
                _sess = (mins_open >= 15) & (mins_open <= sessions.CURRENCY_LAST_ENTRY_MIN)
                def _rolling_thr(values, static_thr, absolute=False):
                    v = np.abs(values) if absolute else values
                    q = float(np.mean(v[_sess] < static_thr))
                    return pd.Series(v).rolling(adaptive_window, min_periods=adaptive_window // 2).quantile(q).shift(1).to_numpy()
                _adx_thr, _vol_thr, _slope_thr = _rolling_thr(adxs, min_adx), _rolling_thr(vol_surges, min_vol), _rolling_thr(ema_slopes, min_ema_slope, absolute=True)
            _madx, _mvol, _mslope = min_adx, min_vol, min_ema_slope
            if adaptive_window and not (np.isnan(_adx_thr[i]) or np.isnan(_vol_thr[i]) or np.isnan(_slope_thr[i])):
                _madx, _mvol, _mslope = _adx_thr[i], _vol_thr[i], _slope_thr[i]
            if sdist <= 0 or c_price <= 0:
                continue

            direction = None
            if adx >= _madx and dmp > dmn and ema_s > _mslope and orb_h_dist >= min_orb and vwap_d >= min_vwap and vol_s >= _mvol:
                direction = "long"
            elif not long_only and not live_short_rules and adx >= _madx and dmn > dmp and ema_s < -_mslope and orb_l_dist <= -min_orb and vwap_d <= -min_vwap and vol_s >= _mvol:
                direction = "short"
            elif not long_only and live_short_rules and adx >= _madx + 3.0 and dmn > dmp and ema_s < -_mslope * 1.2 and orb_l_dist <= -min_orb * 1.1 and vwap_d <= -min_vwap * 1.1 and vol_s >= _mvol * 1.25:
                direction = "short"

            if direction and daily_regime_window:
                _regime = _daily_regime_by_sym_date.get((sym.upper(), pd.Timestamp(c_time).date()))
                if _regime is False:
                    direction = None

            if direction and intraday_chop_window:
                _lo = max(0, i - intraday_chop_window - 5)
                _chop_ok = _regime_ok_intraday(list(closes[_lo:i + 1]), window=intraday_chop_window, min_autocorr=intraday_chop_min_autocorr)
                if intraday_chop_invert and _chop_ok is not None:
                    _chop_ok = not _chop_ok
                if _chop_ok is False:
                    direction = None

            stopped_on_fill = False
            if pullback_frac:
                pending_pb, direction, sdist, c_price, stopped_on_fill = entry_pullback.step(pending_pb, i, direction, sdist, c_price, c_low, c_high, pullback_frac, pullback_bars, pullback_through)

            if not direction:
                continue

            lots = size_currency_lots(
                capital=current_capital, entry_price=c_price, stop_distance=sdist,
                risk_pct=risk_pct, symbol=sym.upper(), leverage=leverage, size_mode=size_mode,
            )
            if lots < 1:
                continue

            d = 1 if direction == "long" else -1
            sl = round(c_price - sdist * d, 4)
            _scale = float(np.clip(adx / 25.0, 0.7, 1.8))
            if exit_mode == "dynamic":
                tp = round(c_price + 1e9 * d, 4)
                be = round(c_price + (BE_ACTIVATION_MULT / _scale) * sdist * d, 4)
            elif exit_mode == "tp_scaled":
                tp = round(c_price + tp_mult * _scale * sdist * d, 4)
                be = round(c_price + BE_ACTIVATION_MULT * sdist * d, 4)
            else:
                tp = round(c_price + tp_mult * sdist * d, 4)
                be = round(c_price + BE_ACTIVATION_MULT * sdist * d, 4)
                if live_short_rules and d == -1:                             # entry_signal.py's live short exit: TP x0.8 (min 1.4R), breakeven at 0.4R
                    tp = round(c_price - max(1.4, tp_mult * 0.80) * sdist, 4)
                    be = round(c_price - 0.40 * sdist, 4)

            in_pos = True
            pos = {
                "direction": direction, "entry_price": c_price, "entry_idx": i, "entry_time": c_time,
                "lots": lots, "sl": sl, "tp": tp, "be": be, "current_stop": sl,
                "best_price": c_price, "armed_be": False, "stop_dist": sdist,
                "trail_mult": TRAIL_DIST_MULT * (_scale if exit_mode == "dynamic" else 1.0), "hold": int(round(HOLD_BARS * _scale)) if hold_mode == "adx" else HOLD_BARS,
            }
            if stopped_on_fill:                                  # the pullback limit filled and the stop was hit inside the same bar
                _record_trade(sym, pos, sl, "initial_stop", c_time, stopped_on_fill=True)
                in_pos = False
                pos = {}

    total_trades = len(all_trades)
    if total_trades == 0:
        print("No trades executed.")
        return {}

    wins = [t for t in all_trades if t["net_pnl"] > 0]
    losses = [t for t in all_trades if t["net_pnl"] <= 0]
    win_rate = len(wins) / total_trades * 100

    gross_sum = sum(t["gross_pnl"] for t in all_trades)
    fees_sum = sum(t["total_fees"] for t in all_trades)
    net_pnl_sum = sum(t["net_pnl"] for t in all_trades)
    pct_return = (net_pnl_sum / capital) * 100

    gross_wins = sum(t["gross_pnl"] for t in wins)
    gross_losses = abs(sum(t["gross_pnl"] for t in losses))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 99.0

    eq = np.array(equity_curve)
    peaks = np.maximum.accumulate(eq)
    drawdowns = (peaks - eq) / peaks * 100
    max_dd = np.max(drawdowns) if len(drawdowns) > 0 else 0.0

    C_RESET, C_BOLD = "\033[0m", "\033[1m"
    C_RED, C_GREEN, C_YELLOW = "\033[91m", "\033[92m", "\033[93m"
    C_CYAN, C_WHITE, C_GRAY = "\033[96m", "\033[97m", "\033[90m"

    def _col_wr(wr): return f"{(C_GREEN if wr>=55 else C_YELLOW if wr>=45 else C_RED)}{wr:5.1f}%{C_RESET}"
    def _col_ret(v): return f"{(C_GREEN if v>0 else C_RED if v<0 else C_WHITE)}₹{v:+12,.2f}{C_RESET}"
    def _col_pf(pf): return f"{(C_GREEN if pf>=1.2 else C_YELLOW if pf>=1.0 else C_RED)}{pf:5.2f}{C_RESET}"

    brok_sum  = sum(t.get("brokerage", 0.0) for t in all_trades)
    stamp_sum = sum(t.get("stamp_duty", 0.0) for t in all_trades)
    exch_sum  = sum(t.get("exchange_txn", 0.0) for t in all_trades)
    sebi_sum  = sum(t.get("sebi", 0.0) for t in all_trades)
    gst_sum   = sum(t.get("gst", 0.0) for t in all_trades)
    slip_sum  = sum(t.get("slippage", 0.0) for t in all_trades)
    cap_color = C_GREEN if net_pnl_sum > 0 else C_RED

    print(f"\n{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}  ⚡ 5-MINUTE NSE CURRENCY SCALPER — WALK-FORWARD BACKTEST RESULTS{C_RESET}")
    print(f"  {C_GRAY}📅 Period:{C_RESET} {C_YELLOW}{period_str}{C_RESET} | {C_GRAY}💰 Capital:{C_RESET} {C_WHITE}₹{capital:,.0f}{C_RESET} | {C_GRAY}⚙️ Leverage:{C_RESET} {C_WHITE}{leverage:.1f}x{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}{'='*85}{C_RESET}")

    print(f"\n{C_BOLD}{C_WHITE}┌── 📊 PORTFOLIO EXECUTIVE SUMMARY ──────────────────────────────────────────────────┐{C_RESET}")
    print(f"│  {C_GRAY}Starting Capital:{C_RESET}  {C_WHITE}₹{capital:,.2f}{C_RESET}          {C_GRAY}Final Capital:{C_RESET}    {cap_color}₹{current_capital:,.2f} ({pct_return:+.2f}%){C_RESET}")
    print(f"│  {C_GRAY}Executed Trades:{C_RESET}   {C_WHITE}{total_trades:,}{C_RESET} ({len(wins)}W / {len(losses)}L)   {C_GRAY}Overall Win Rate:{C_RESET} {_col_wr(win_rate)}")
    print(f"│  {C_GRAY}Profit Factor:{C_RESET}     {_col_pf(profit_factor)}                 {C_GRAY}Max Drawdown:{C_RESET}     {C_RED}-{max_dd:.2f}%{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}└───{'─'*79}┘{C_RESET}")

    gross_color = C_GREEN if gross_sum > 0 else C_RED
    net_color = C_GREEN if net_pnl_sum > 0 else C_RED
    print(f"\n{C_BOLD}{C_YELLOW}┌── 💰 ITEMIZED NCD STATUTORY COSTS & TAXES (₹ INR) {'─'*31}┐{C_RESET}")
    print(f"│  {C_GRAY}Gross Trading PnL:{C_RESET}               {gross_color}₹{gross_sum:+12,.2f}{C_RESET}                                    │")
    print(f"│  {C_GRAY}Upstox Flat Brokerage (₹20/ord):{C_RESET}{C_RED}-₹{brok_sum:12,.2f}{C_RESET}  {C_GRAY}(Includes 18% GST on brokerage){C_RESET}  │")
    print(f"│  {C_GRAY}STT / CTT:{C_RESET}                       {C_WHITE}₹{0.0:12,.2f}{C_RESET}  {C_GRAY}(currency derivatives are exempt){C_RESET}    │")
    print(f"│  {C_GRAY}Stamp Duty (Currency, Buy side):{C_RESET} {C_RED}-₹{stamp_sum:12,.2f}{C_RESET}  {C_GRAY}(0.0001% = Rs10/crore){C_RESET}            │")
    print(f"│  {C_GRAY}NSE Currency Exchange Fee:{C_RESET}       {C_RED}-₹{exch_sum:12,.2f}{C_RESET}  {C_GRAY}(0.0009% turnover fee){C_RESET}           │")
    print(f"│  {C_GRAY}SEBI Regulatory Fee:{C_RESET}             {C_RED}-₹{sebi_sum:12,.2f}{C_RESET}  {C_GRAY}(₹10 per Crore turnover){C_RESET}         │")
    print(f"│  {C_GRAY}18% GST on Regulatory Charges:{C_RESET}   {C_RED}-₹{gst_sum:12,.2f}{C_RESET}  {C_GRAY}(GST on Exch + SEBI){C_RESET}             │")
    print(f"│  {C_GRAY}Estimated Bid-Ask Slippage:{C_RESET}      {C_RED}-₹{slip_sum:12,.2f}{C_RESET}  {C_GRAY}(½-tick per leg){C_RESET}                 │")
    print(f"{C_GRAY}├──────────────────────────────────────────────────────────────────────────────────┤{C_RESET}")
    print(f"│  {C_BOLD}{C_WHITE}TOTAL NCD FRICTION & TAXES:{C_RESET}      {C_RED}{C_BOLD}-₹{fees_sum:12,.2f}{C_RESET}                                    │")
    print(f"│  {C_BOLD}{C_WHITE}NET REALIZED PROFIT IN ₹:{C_RESET}        {net_color}{C_BOLD}₹{net_pnl_sum:+12,.2f}{C_RESET}  {C_GRAY}(After all statutory taxes){C_RESET} │")
    print(f"{C_BOLD}{C_YELLOW}└──────────────────────────────────────────────────────────────────────────────────┘{C_RESET}")

    print(f"\n{C_BOLD}{C_CYAN}┌── 📈 PER-PAIR ALPHA BREAKDOWN {'─'*53}┐{C_RESET}")
    print(f"{C_BOLD}{C_WHITE}│ {'Symbol':<12s} │ {'Trades':>6s} │ {'Win Rate':>8s} │ {'Gross PnL':>14s} │ {'Total Fees':>12s} │ {'Net Realized':>14s} │{C_RESET}")
    print(f"{C_GRAY}├──────────────┼────────┼──────────┼────────────────┼──────────────┼────────────────┤{C_RESET}")
    for sym in target_symbols:
        s_trades = [t for t in all_trades if t["symbol"] == sym]
        if not s_trades:
            continue
        s_wins = [t for t in s_trades if t["net_pnl"] > 0]
        s_wr = len(s_wins) / len(s_trades) * 100
        s_gross = sum(t["gross_pnl"] for t in s_trades)
        s_fees = sum(t["total_fees"] for t in s_trades)
        s_net = sum(t["net_pnl"] for t in s_trades)
        s_gross_str = f"{C_GREEN if s_gross>0 else C_RED}₹{s_gross:+12,.2f}{C_RESET}"
        print(f"│ {C_BOLD}{C_CYAN}{sym:<12s}{C_RESET} │ {len(s_trades):6d} │ {_col_wr(s_wr):>8s} │ {s_gross_str:>14s} │ {C_RED}-₹{s_fees:10,.2f}{C_RESET} │ {_col_ret(s_net):>14s} │")
    print(f"{C_BOLD}{C_CYAN}└──────────────┴────────┴──────────┴────────────────┴──────────────┴────────────────┘{C_RESET}\n")

    result = {"trades": total_trades, "win_rate": win_rate, "net_pnl": net_pnl_sum,
              "profit_factor": profit_factor, "max_dd": max_dd, "fees": fees_sum}
    if return_trades:
        result["trade_list"] = all_trades
    return result


def main():
    parser = argparse.ArgumentParser(description="Backtest 5-min NSE Currency Derivatives Scalping Engine")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--risk-pct", type=float, default=10.0)
    parser.add_argument("--leverage", type=float, default=7.0)
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--long-only", action="store_true")
    parser.add_argument("--from", "--from-date", dest="from_date", default=None)
    parser.add_argument("--to", "--to-date", dest="to_date", default=None)
    parser.add_argument("--size-mode", choices=["risk", "margin"], default="margin")
    args = parser.parse_args()
    run_currency_backtest(
        symbols=args.symbols, capital=args.capital, risk_pct=args.risk_pct, leverage=args.leverage,
        long_only=args.long_only, from_date=args.from_date, to_date=args.to_date, size_mode=args.size_mode,
    )


if __name__ == "__main__":
    main()
