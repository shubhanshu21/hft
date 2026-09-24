"""Extended swing rules: momentum/trend indicators, volatility squeezes, candlestick patterns, and chart formations.

Used only by research (core.swing_engine.with_indicators(..., extended=True)); the live commodity swing strategy is not
touched. Everything is backward-looking. Swing pivots are confirmed K bars AFTER they occur (a pivot low at bar j is only
known once bars j+1..j+K have closed), so a pattern built from pivots never uses a bar it could not have seen.

Rules (every parameter set is listed in markets/swing_research_ext.py BEFORE any result is looked at):
  trend:      supertrend, ichimoku, macd_cross, keltner_break, rs_rank (cross-sectional relative strength)
  breakout:   bb_squeeze, vol_breakout, pivot_break, flag
  reversal:   double_bottom / double_top, fib_pullback
  pullback:   ibs, bb_meanrev
  candles:    engulfing, hammer (pin bar), morning_star (evening_star for shorts), optionally in a pullback context
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EXT_RULES = {"supertrend", "ichimoku", "macd_cross", "keltner_break", "rs_rank", "bb_squeeze", "vol_breakout",
             "pivot_break", "flag", "double_bottom", "fib_pullback", "ibs", "bb_meanrev", "engulfing", "hammer",
             "morning_star", "random"}
PIVOT_K = 3


def _supertrend(h, l, c, atr, mult) -> np.ndarray:
    hl2 = (h + l) / 2
    up, dn = hl2 + mult * atr, hl2 - mult * atr
    n = len(c)
    fu, fl, direction = up.copy(), dn.copy(), np.ones(n)
    valid = np.flatnonzero(~np.isnan(atr))
    if len(valid) == 0:
        return direction
    for i in range(valid[0] + 1, n):        # start at the first bar with an ATR, else a NaN band would freeze the whole series
        fu[i] = up[i] if (up[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = dn[i] if (dn[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if direction[i - 1] == -1 and c[i] > fu[i - 1]:
            direction[i] = 1
        elif direction[i - 1] == 1 and c[i] < fl[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    return direction


def _pivots(h, l, k=PIVOT_K):
    """Per bar i: the latest CONFIRMED pivot high/low (price and bar index), and the double-bottom / double-top state."""
    n = len(h)
    cols = {name: np.full(n, np.nan) for name in ("ph", "ph_i", "ph_age", "pl", "pl_i", "db_neck", "db_age", "db_low", "dt_neck", "dt_age", "dt_high")}
    ph = pl = ph_i = pl_i = np.nan
    pl_prev = pl_prev_i = ph_prev = ph_prev_i = np.nan
    db_neck = db_born = db_low = dt_neck = dt_born = dt_high = np.nan
    for i in range(2 * k, n):
        j = i - k                                   # candidate pivot bar, confirmed by the k bars after it
        if l[j] == l[j - k:i + 1].min():
            pl_prev, pl_prev_i, pl, pl_i = pl, pl_i, l[j], j
            if not np.isnan(pl_prev) and 10 <= pl_i - pl_prev_i <= 60 and abs(pl - pl_prev) / pl_prev < 0.03:
                db_neck, db_born, db_low = h[int(pl_prev_i):int(pl_i) + 1].max(), i, min(pl, pl_prev)
        if h[j] == h[j - k:i + 1].max():
            ph_prev, ph_prev_i, ph, ph_i = ph, ph_i, h[j], j
            if not np.isnan(ph_prev) and 10 <= ph_i - ph_prev_i <= 60 and abs(ph - ph_prev) / ph_prev < 0.03:
                dt_neck, dt_born, dt_high = l[int(ph_prev_i):int(ph_i) + 1].min(), i, max(ph, ph_prev)
        cols["ph"][i], cols["ph_i"][i], cols["ph_age"][i], cols["pl"][i], cols["pl_i"][i] = ph, ph_i, i - ph_i, pl, pl_i
        cols["db_neck"][i], cols["db_age"][i], cols["db_low"][i] = db_neck, i - db_born, db_low
        cols["dt_neck"][i], cols["dt_age"][i], cols["dt_high"][i] = dt_neck, i - dt_born, dt_high
    return cols


def extend_indicators(d: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c, atr = d["open"], d["high"], d["low"], d["close"], d["atr"]
    hv, lv, cv, av = h.values, l.values, c.values, atr.values
    for period, mult in ((10, 3.0), (14, 2.0)):
        a = (pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1).rolling(period).mean()).values
        d[f"st_{period}_{int(mult)}"] = _supertrend(hv, lv, cv, a, mult)
    # ichimoku (cloud = values computed 26 bars ago, so nothing is plotted "into the future" relative to the data used)
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    d["tenkan"], d["kijun"] = tenkan, kijun
    d["cloud_top"], d["cloud_bot"] = pd.concat([span_a, span_b], axis=1).max(axis=1), pd.concat([span_a, span_b], axis=1).min(axis=1)
    # macd
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    d["macd"], d["macds"] = macd, macd.ewm(span=9, adjust=False).mean()
    # bollinger / keltner
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    d["bb_mid"], d["bb_up"], d["bb_lo20"], d["bb_lo25"] = mid, mid + 2 * sd, mid - 2 * sd, mid - 2.5 * sd
    bw = (4 * sd) / mid
    d["bw_rank"] = bw.rolling(120).rank(pct=True)
    d["kc_up"], d["kc_dn"] = d["ema20"] + 2 * atr, d["ema20"] - 2 * atr
    d["vol_ratio"] = d["volume"] / d["volume"].rolling(20).mean() if "volume" in d else np.nan
    d["ibs"] = (c - l) / (h - l).replace(0, np.nan)
    # flag: strong pole, tight consolidation over the last 8 bars, breakout of that consolidation
    d["f_hi"], d["f_lo"] = h.rolling(8).max().shift(1), l.rolling(8).min().shift(1)
    d["pole_up"], d["pole_dn"] = (c.shift(9) - c.shift(29)) / atr, (c.shift(29) - c.shift(9)) / atr
    d["f_rng"] = (d["f_hi"] - d["f_lo"]) / atr
    # fibonacci retracement of the last 60-bar swing
    win = 60
    if len(d) > win:
        from numpy.lib.stride_tricks import sliding_window_view as swv
        hw, lw = swv(hv, win), swv(lv, win)
        ih, il = hw.argmax(axis=1), lw.argmin(axis=1)
        sh, sl = hw.max(axis=1), lw.min(axis=1)
        pad = np.full(win - 1, np.nan)
        d["sw_hi"], d["sw_lo"] = np.concatenate([pad, sh]), np.concatenate([pad, sl])
        d["sw_up"] = np.concatenate([pad, (ih > il).astype(float)])           # the low came BEFORE the high: an up-swing
        d["sw_dn"] = np.concatenate([pad, (il > ih).astype(float)])
        d["retr_up"] = (d["sw_hi"] - c) / (d["sw_hi"] - d["sw_lo"]).replace(0, np.nan)
        d["retr_dn"] = (c - d["sw_lo"]) / (d["sw_hi"] - d["sw_lo"]).replace(0, np.nan)
    else:
        for k in ("sw_hi", "sw_lo", "sw_up", "sw_dn", "retr_up", "retr_dn"):
            d[k] = np.nan
    # candlestick patterns (evaluated on the bar that just closed)
    body, rng = (c - o).abs(), (h - l).replace(0, np.nan)
    up_w, lo_w = h - np.maximum(o, c), np.minimum(o, c) - l
    po, pc = o.shift(), c.shift()
    d["bull_engulf"] = ((pc < po) & (c > o) & (o <= pc) & (c >= po)).astype(float)
    d["bear_engulf"] = ((pc > po) & (c < o) & (o >= pc) & (c <= po)).astype(float)
    d["hammer"] = ((lo_w >= 2 * body) & (up_w <= 0.35 * body.clip(lower=1e-9) + 0.1 * rng) & (body / rng < 0.4) & (c >= l + 0.6 * rng)).astype(float)
    d["shooting"] = ((up_w >= 2 * body) & (lo_w <= 0.35 * body.clip(lower=1e-9) + 0.1 * rng) & (body / rng < 0.4) & (c <= l + 0.4 * rng)).astype(float)
    o2, c2 = o.shift(2), c.shift(2)
    small1 = (body.shift() / rng.shift()) < 0.3
    d["morning_star"] = ((c2 < o2) & ((o2 - c2) / rng.shift(2) > 0.5) & small1 & (c > o) & (c > (o2 + c2) / 2)).astype(float)
    d["evening_star"] = ((c2 > o2) & ((c2 - o2) / rng.shift(2) > 0.5) & small1 & (c < o) & (c < (o2 + c2) / 2)).astype(float)
    for k, v in _pivots(hv, lv).items():
        d[k] = v
    return d


def entry(rule_name: str, p: dict, r, allow_short: bool) -> int:
    n = rule_name
    if n == "supertrend":
        s = r[f"st_{p['period']}_{p['mult']}"]
        return 1 if s == 1 else (-1 if allow_short and s == -1 else 0)
    if n == "ichimoku":
        if r["close"] > r["cloud_top"] and r["tenkan"] > r["kijun"]:
            return 1
        if allow_short and r["close"] < r["cloud_bot"] and r["tenkan"] < r["kijun"]:
            return -1
    elif n == "macd_cross":
        if r["macd"] > r["macds"] and r["macd"] > 0 and r["close"] > r["sma200"]:
            return 1
        if allow_short and r["macd"] < r["macds"] and r["macd"] < 0 and r["close"] < r["sma200"]:
            return -1
    elif n == "keltner_break":
        if r["close"] > r["kc_up"] and r["close"] > r["sma200"]:
            return 1
        if allow_short and r["close"] < r["kc_dn"] and r["close"] < r["sma200"]:
            return -1
    elif n == "rs_rank":
        rk = r[f"rs_rank{p['lookback']}"]
        if rk >= p["top"] and r["close"] > r["sma200"]:
            return 1
    elif n == "bb_squeeze":
        if r["bw_rank"] < p["q"] and r["close"] > r["bb_up"] and r["close"] > r["sma50"]:
            return 1
    elif n == "vol_breakout":
        if r["close"] > r["hh20"] and r["vol_ratio"] >= p["vol"]:
            return 1
    elif n == "pivot_break":
        if r["close"] > r["ph"] and r["ph_age"] >= p["min_age"] and r["close"] > r["sma50"]:
            return 1
    elif n == "flag":
        if r["pole_up"] > p["pole"] and r["f_rng"] < p["tight"] and r["close"] > r["f_hi"]:
            return 1
        if allow_short and r["pole_dn"] > p["pole"] and r["f_rng"] < p["tight"] and r["close"] < r["f_lo"]:
            return -1
    elif n == "double_bottom":
        if r["db_age"] <= 20 and r["close"] > r["db_neck"] and r["close"] > r["sma200"]:
            return 1
        if allow_short and r["dt_age"] <= 20 and r["close"] < r["dt_neck"]:
            return -1
    elif n == "fib_pullback":
        if r["sma50"] > r["sma200"] and r["sw_up"] == 1 and p["lo"] <= r["retr_up"] <= p["hi"] and r["close"] > r["open"]:
            return 1
        if allow_short and r["sma50"] < r["sma200"] and r["sw_dn"] == 1 and p["lo"] <= r["retr_dn"] <= p["hi"] and r["close"] < r["open"]:
            return -1
    elif n == "ibs":
        if r["ibs"] < p["thr"] and r["close"] > r["sma200"]:
            return 1
        if allow_short and r["ibs"] > 1 - p["thr"] and r["close"] < r["sma200"]:
            return -1
    elif n == "bb_meanrev":
        lo = r["bb_lo20"] if p["sd"] == 2.0 else r["bb_lo25"]
        if r["close"] < lo and r["close"] > r["sma200"]:
            return 1
    elif n in ("engulfing", "hammer", "morning_star"):
        bull, bear = {"engulfing": ("bull_engulf", "bear_engulf"), "hammer": ("hammer", "shooting"),
                      "morning_star": ("morning_star", "evening_star")}[n]
        if r[bull] == 1 and (not p["ctx"] or (r["sma50"] > r["sma200"] and r["rsi14"] < 50)):
            return 1
        if allow_short and r[bear] == 1 and (not p["ctx"] or (r["sma50"] < r["sma200"] and r["rsi14"] > 50)):
            return -1
    elif n == "random":
        if r[f"rnd{p['seed']}"] < p["p"]:
            return 1
    return 0


def exit_(rule_name: str, p: dict, r, direction: int) -> bool:
    n, c = rule_name, r["close"]
    if n == "supertrend":
        return r[f"st_{p['period']}_{p['mult']}"] != direction
    if n == "ichimoku":
        return c < r["kijun"] if direction > 0 else c > r["kijun"]
    if n == "macd_cross":
        return r["macd"] < r["macds"] if direction > 0 else r["macd"] > r["macds"]
    if n in ("keltner_break", "flag", "pivot_break"):
        return c < r["ema20"] if direction > 0 else c > r["ema20"]
    if n == "rs_rank":
        return r[f"rs_rank{p['lookback']}"] < p["exit"] or c < r["sma200"]
    if n == "bb_squeeze":
        return c < r["bb_mid"]
    if n == "vol_breakout":
        return c < r["ll10"]
    if n == "double_bottom":
        return (c < r["db_low"]) if direction > 0 else (c > r["dt_high"])
    if n == "fib_pullback":
        return (c >= r["sw_hi"] * 0.995) if direction > 0 else (c <= r["sw_lo"] * 1.005)
    if n == "ibs":
        return r["ibs"] > 0.7 if direction > 0 else r["ibs"] < 0.3
    if n == "bb_meanrev":
        return c > r["bb_mid"]
    return False            # candlestick patterns and the random control exit only on their time stop or ATR stop
