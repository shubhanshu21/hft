"""Chart overlays for a web backtest result: the strategy's own lines (so the chart shows WHY each trade happened), an oscillator band and
event markers, computed from FULL history (a 200-day average needs 200 days before the test starts) and aligned to the result's days.

    overlays[symbol] = {"lines": [{"name", "values"}], "osc": {"name", "values", "levels"} | None, "markers": [{"i", "text"}]}

  trend_ema_cross     20 / 50 / 200-day EMAs (its entry: the 20 crossing above the 50, price above the 200)
  breakout_pair       55-day high before each day (the d55 entry level) and the 50 / 200-day EMAs (its trend filters)
  rsi_range           20-day average (its exit) + RSI(14) band with 30 / 70 (its entry)
  earnings_drift      "R" on each quarterly results day (its entry is two days later)
Other strategies get no overlays. Missing data simply leaves a symbol out.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT


def _r(s: pd.Series) -> list:
    return [None if not np.isfinite(v) else round(float(v), 4) for v in s.values]


def _closes_equity(sym: str) -> pd.DataFrame | None:
    for p in (ARCHIVE_ROOT / "equity" / f"{sym}_daily.csv", ARCHIVE_ROOT / "equity_all" / f"{sym}_daily.csv.gz"):
        if p.exists():
            x = pd.read_csv(p)
            x.index = pd.to_datetime(x["timestamp"].astype(str).str[:10])
            return x[~x.index.duplicated(keep="last")].sort_index()
    return None


def _ema(c: pd.Series, n: int) -> pd.Series:
    return c.ewm(span=n, adjust=False, min_periods=n).mean()


def overlays(strategy: str, market: str, result: dict) -> dict:
    days = pd.to_datetime(result.get("days_all") or result.get("days") or [])
    syms = list((result.get("bars") or {}).keys())
    if not len(days) or not syms:
        return {}
    out = {}
    if strategy == "breakout_pair":
        from markets.equity.strategies.tf_daily.breakout_pair import strategy as BP
        p = BP.load(syms)
        for s in syms:
            if s not in p["C"].columns:
                continue
            c, h = p["C"][s].dropna(), p["H"][s]
            hi55 = h.shift(1).rolling(55).max()
            out[s] = {"lines": [{"name": "55-day high", "values": _r(hi55.reindex(days))}, {"name": "EMA 50", "values": _r(_ema(c, 50).reindex(days))},
                                {"name": "EMA 200", "values": _r(_ema(c, 200).reindex(days))}], "osc": None, "markers": []}
    elif strategy == "trend_ema_cross":
        for s in syms:
            x = _closes_equity(s)
            if x is None:
                continue
            c = x["close"].astype(float)
            out[s] = {"lines": [{"name": f"EMA {n}", "values": _r(_ema(c, n).reindex(days))} for n in (20, 50, 200)], "osc": None, "markers": []}
    elif strategy == "rsi_range":
        for s in syms:
            p = ARCHIVE_ROOT / "currency" / f"{s}_daily_adj.csv"
            if not p.exists():
                continue
            x = pd.read_csv(p); x.index = pd.to_datetime(x["timestamp"].astype(str).str[:10]); c = x["close"].astype(float)
            c = c[~c.index.duplicated(keep="last")]
            d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
            rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
            out[s] = {"lines": [{"name": "20-day average", "values": _r(c.rolling(20).mean().reindex(days))}],
                      "osc": {"name": "RSI 14", "values": _r(rsi.reindex(days)), "levels": [30, 70]}, "markers": []}
    elif strategy == "earnings_drift":
        from markets.equity.strategies.tf_daily.earnings_drift import strategy as ED
        ev = ED.results_events()
        for s in syms:
            mk = []
            for d in ev.loc[ev["sym"] == s, "d"]:
                i = days.searchsorted(d)                                   # the meeting day, or the next trading day
                if i < len(days) and (days[i] - d).days <= 4:
                    mk.append({"i": int(i), "text": "R"})
            out[s] = {"lines": [], "osc": None, "markers": mk}
    return out
