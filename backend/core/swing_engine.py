"""Daily-bar swing-strategy simulator and rule definitions, shared by research and by the live strategies.

Nothing here can see the future: a signal is computed from data up to and including day i's close and is EXECUTED at
day i+1's open. Stops are checked against each later day's range (including the entry day), with gaps through the
stop filled at the open, not the stop. Every trade pays a round-trip cost.

The same `signal()` rules drive the backtests and the live Strategy classes, so what was validated is what trades.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

WARMUP = 210            # bars needed before the first signal (200-day averages)


@dataclass
class Trade:
    symbol: str
    direction: int            # +1 long, -1 short
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry: float
    exit: float
    net_ret: float            # after costs, fraction of entry price
    stop_pct: float           # initial stop distance as a fraction of entry price
    bars: int
    reason: str

    @property
    def r_multiple(self) -> float:
        return self.net_ret / self.stop_pct if self.stop_pct else 0.0


def with_indicators(df: pd.DataFrame, market_ok: pd.Series | None = None) -> pd.DataFrame:
    """Adds every indicator any rule uses. All are backward-looking (value at row i uses rows <= i).
    market_ok: optional boolean series (e.g. NIFTY50 above its 200-day average) for rules with params["regime"]."""
    d = df.copy()
    d["mkt_ok"] = 1.0 if market_ok is None else market_ok.astype(float).reindex(d.index).ffill().fillna(0.0)
    c, h, l = d["close"], d["high"], d["low"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    for n in (5, 50, 200):
        d[f"sma{n}"] = c.rolling(n).mean()
    for n in (20, 50, 100):
        d[f"ema{n}"] = c.ewm(span=n, adjust=False).mean()
    d["ema200"] = c.ewm(span=200, adjust=False).mean()

    def rsi(n):
        delta = c.diff()
        up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        return 100 - 100 / (1 + up / dn.replace(0, np.nan))
    d["rsi2"], d["rsi14"] = rsi(2), rsi(14)
    for n in (10, 20, 55):     # channels of the PREVIOUS n days (shifted, so today's bar is not inside its own breakout level)
        d[f"hh{n}"] = h.rolling(n).max().shift(1)
        d[f"ll{n}"] = l.rolling(n).min().shift(1)
    for n in (126, 252):
        d[f"ret{n}"] = c / c.shift(n) - 1
    return d


@dataclass(frozen=True)
class Rule:
    """One parameterised swing rule. `entry`/`exit` take the indicator row and the position direction."""
    name: str
    params: dict = field(default_factory=dict)
    stop_mult: float = 3.0          # initial stop = entry -/+ stop_mult * ATR (ATR at the signal bar)
    max_hold: int | None = None     # time stop in bars


def entry_signal(rule: Rule, r, allow_short: bool) -> int:
    """+1 / -1 / 0 from one indicator row (the bar that just closed)."""
    p, n = rule.params, rule.name
    if p.get("regime") and not r["mkt_ok"]:
        return 0                          # market filter: no new positions while the broad index is in a downtrend
    if n == "donchian":
        N = p["n"]
        if r["close"] > r[f"hh{N}"]:
            return 1
        if allow_short and r["close"] < r[f"ll{N}"]:
            return -1
    elif n == "ema_trend":
        f, s = r[f"ema{p['fast']}"], r[f"ema{p['slow']}"]
        if f > s and r["close"] > s:
            return 1
        if allow_short and f < s and r["close"] < s:
            return -1
    elif n == "rsi2_pullback":
        if r["close"] > r["sma200"] and r["rsi2"] < p["thr"]:
            return 1
        if allow_short and r["close"] < r["sma200"] and r["rsi2"] > 100 - p["thr"]:
            return -1
    elif n == "trend_pullback":
        if r["sma50"] > r["sma200"] and r["close"] > r["sma200"] and r["rsi14"] < p["thr"]:
            return 1
        if allow_short and r["sma50"] < r["sma200"] and r["close"] < r["sma200"] and r["rsi14"] > 100 - p["thr"]:
            return -1
    elif n == "tsmom":
        ret = r[f"ret{p['lookback']}"]
        if ret > 0:
            return 1
        if allow_short and ret < 0:
            return -1
    return 0


def exit_signal(rule: Rule, r, direction: int) -> bool:
    """True = close the position at the next open."""
    p, n = rule.params, rule.name
    if n == "donchian":
        M = p["exit_n"]
        return r["close"] < r[f"ll{M}"] if direction > 0 else r["close"] > r[f"hh{M}"]
    if n == "ema_trend":
        f, s = r[f"ema{p['fast']}"], r[f"ema{p['slow']}"]
        return f < s if direction > 0 else f > s
    if n == "rsi2_pullback":
        return r["close"] > r["sma5"] if direction > 0 else r["close"] < r["sma5"]
    if n == "trend_pullback":
        return (r["rsi14"] > 60 or r["close"] < r["sma50"]) if direction > 0 else (r["rsi14"] < 40 or r["close"] > r["sma50"])
    if n == "tsmom":
        ret = r[f"ret{p['lookback']}"]
        return ret <= 0 if direction > 0 else ret >= 0
    return False


def run_symbol(symbol: str, ind: pd.DataFrame, rule: Rule, allow_short: bool, cost_rt: float,
               start: str | None = None, end: str | None = None) -> list[Trade]:
    """Simulate `rule` on one symbol's indicator frame. `cost_rt` is the round-trip cost as a fraction of notional."""
    idx = ind.index
    o, h, l, c = ind["open"].values, ind["high"].values, ind["low"].values, ind["close"].values
    atr = ind["atr"].values
    rows = ind.to_dict("records")
    t0 = pd.Timestamp(start) if start else idx[0]
    t1 = pd.Timestamp(end) if end else idx[-1]
    trades: list[Trade] = []
    pos, entry_px, stop, entry_i, stop_pct = 0, 0.0, 0.0, 0, 0.0
    pend_entry, pend_exit, pend_atr = 0, False, 0.0
    n = len(ind)

    def close_trade(i, px, reason):
        nonlocal pos
        ret = (px / entry_px - 1) * pos - cost_rt
        trades.append(Trade(symbol, pos, idx[entry_i], idx[i], entry_px, px, ret, stop_pct, i - entry_i, reason))
        pos = 0

    for i in range(WARMUP, n):
        day = idx[i]
        # -- execute orders decided at yesterday's close, at today's open --
        if pos != 0 and pend_exit:
            close_trade(i, o[i], "signal_exit")
            pend_exit = False
        if pos == 0 and pend_entry != 0 and t0 <= day <= t1:
            pos, entry_px, entry_i = pend_entry, o[i], i
            stop = entry_px - pos * rule.stop_mult * pend_atr
            stop_pct = rule.stop_mult * pend_atr / entry_px
            pend_entry = 0
        pend_entry = 0 if pos != 0 else pend_entry
        # -- stop check on today's range (also on the entry day; a gap through the stop fills at the open) --
        if pos != 0:
            hit = (l[i] <= stop) if pos > 0 else (h[i] >= stop)
            if hit:
                gap = (o[i] <= stop) if pos > 0 else (o[i] >= stop)
                close_trade(i, o[i] if gap and i != entry_i else stop, "stop")
                pend_exit = False
        # -- decisions at today's close, executed tomorrow --
        if pos != 0:
            if exit_signal(rule, rows[i], pos) or (rule.max_hold and i - entry_i >= rule.max_hold):
                pend_exit = True
        elif t0 <= day <= t1:
            s = entry_signal(rule, rows[i], allow_short)
            if s and not np.isnan(atr[i]) and atr[i] > 0:
                pend_entry, pend_atr = s, atr[i]
    if pos != 0 and t0 <= idx[-1] <= t1:      # still open at the end of data: mark to the last close
        close_trade(n - 1, c[-1], "end_of_data")
    return trades


def summarize(trades: list[Trade]) -> dict:
    """Pooled per-trade statistics (unlevered, net of costs)."""
    if not trades:
        return {"n": 0}
    r = np.array([t.net_ret for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
    sd = r.std(ddof=1) if len(r) > 1 else float("nan")
    return {
        "n": len(r), "win": float((r > 0).mean() * 100), "avg_pct": float(r.mean() * 100), "pf": float(pf),
        "t": float(r.mean() / sd * np.sqrt(len(r))) if sd and sd > 0 else 0.0,
        "avg_R": float(np.mean([t.r_multiple for t in trades])), "hold": float(np.median([t.bars for t in trades])),
        "best_pct": float(r.max() * 100), "worst_pct": float(r.min() * 100),
    }
