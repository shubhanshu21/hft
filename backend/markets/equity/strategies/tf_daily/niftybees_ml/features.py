"""Daily features for niftybees_ml, built only from the nightly archives (each value on day t uses data up to and including day t).

Sources (all topped up by engine/nightly.py):
  equity/NIFTYBEES_daily.csv                        the ETF itself
  equity/<NIFTY50>_daily.csv                        breadth: share above the 50/200-day EMA, at 20-day highs, advance/decline
  nse/participant_oi/<day>.csv                      FII long share of index futures (NSE participant-wise open interest)
  index/NIFTY50|NIFTYBANK|INDIAVIX_1minute.csv.gz   opening gap, last-hour move, day range, Bank Nifty vs Nifty, India VIX
"""
from __future__ import annotations

import glob
import os
from functools import lru_cache

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.equity.universe import NIFTY50_SYMBOLS

ETF = "NIFTYBEES"


def _rsi(s: pd.Series, n: int) -> pd.Series:
    ch = s.diff()
    up = ch.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-ch.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def etf_daily() -> pd.DataFrame:
    d = pd.read_csv(ARCHIVE_ROOT / "equity" / f"{ETF}_daily.csv")
    d.index = pd.to_datetime(d["timestamp"].str[:10])
    return d[["open", "high", "low", "close"]].astype(float)


def _fii_long_share() -> pd.Series:
    rows = []
    for f in sorted(glob.glob(str(ARCHIVE_ROOT / "nse" / "participant_oi" / "*.csv"))):
        try:
            t = pd.read_csv(f, skiprows=1)
            t.columns = [c.strip() for c in t.columns]
            r = t[t["Client Type"].str.strip() == "FII"].iloc[0]
            long_, short = float(r["Future Index Long"]), float(r["Future Index Short"])
            rows.append((pd.Timestamp(os.path.basename(f)[:10]), long_ / (long_ + short)))
        except Exception:
            continue                                                    # holiday marker or a malformed file
    return pd.Series(dict(rows)).sort_index()


def _index_daily(name: str) -> pd.DataFrame:
    m = pd.read_csv(ARCHIVE_ROOT / "index" / f"{name}_1minute.csv.gz", usecols=["timestamp", "open", "high", "low", "close"])
    m["d"] = pd.to_datetime(m["timestamp"].str[:10])
    hm = m["timestamp"].str[11:16]
    g = m.groupby("d")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()})
    out["c1430"] = m[hm <= "14:30"].groupby("d")["close"].last()
    return out


def _stamp() -> tuple:
    """Changes whenever a source file changes, so the cached frame is rebuilt after the nightly top-up."""
    files = [ARCHIVE_ROOT / "equity" / f"{ETF}_daily.csv", ARCHIVE_ROOT / "index" / "NIFTY50_1minute.csv.gz",
             ARCHIVE_ROOT / "index" / "INDIAVIX_1minute.csv.gz", ARCHIVE_ROOT / "equity" / "RELIANCE_daily.csv"]
    oi = sorted(glob.glob(str(ARCHIVE_ROOT / "nse" / "participant_oi" / "*.csv")))
    return tuple(os.path.getmtime(f) if os.path.exists(f) else 0 for f in files) + (oi[-1] if oi else "",)


@lru_cache(maxsize=2)
def _all_features(stamp: tuple) -> pd.DataFrame:
    d = etf_daily()
    c = d["close"]
    closes, highs = {}, {}
    for s in NIFTY50_SYMBOLS:
        x = pd.read_csv(ARCHIVE_ROOT / "equity" / f"{s}_daily.csv")
        x.index = pd.to_datetime(x["timestamp"].str[:10])
        closes[s], highs[s] = x["close"], x["high"]
    cl, hi = pd.DataFrame(closes), pd.DataFrame(highs)
    n_ok = cl.notna().sum(axis=1)
    br50 = (cl > cl.ewm(span=50, adjust=False).mean()).sum(axis=1) / n_ok * 100
    br200 = (cl > cl.ewm(span=200, adjust=False).mean()).sum(axis=1) / n_ok * 100
    hi20 = (cl >= hi.rolling(20).max()).sum(axis=1) / n_ok * 100
    adv = ((cl.diff() > 0).sum(axis=1) - (cl.diff() < 0).sum(axis=1)) / n_ok
    nifty, bank = _index_daily("NIFTY50"), _index_daily("NIFTYBANK")
    vix = _index_daily("INDIAVIX")["close"]

    def on_days(s: pd.Series) -> pd.Series:
        return s.reindex(d.index).ffill()

    f = pd.DataFrame(index=d.index)
    for n in (5, 20, 60):
        f[f"ret{n}"] = c.pct_change(n)
    f["rsi14"] = _rsi(c, 14)
    for n in (20, 50, 200):
        f[f"dist{n}"] = c / c.ewm(span=n, adjust=False).mean() - 1
    f["fall_hi"] = 1 - c / d["high"].rolling(252, min_periods=120).max()
    f["br50"], f["br200"], f["hi20"], f["adv5"] = on_days(br50), on_days(br200), on_days(hi20), on_days(adv.rolling(5).mean())
    f["br50_chg10"] = f["br50"].diff(10)
    f["fii_long"] = on_days(_fii_long_share())
    f["fii_chg5"] = f["fii_long"].diff(5)
    f["fii_vs20"] = f["fii_long"] - f["fii_long"].rolling(20).mean()
    ni = nifty.reindex(d.index)
    f["gap"] = ni["open"] / ni["close"].shift() - 1
    f["last_hour"] = ni["close"] / ni["c1430"] - 1
    f["range"] = (ni["high"] - ni["low"]) / ni["close"]
    f["gap5"], f["last_hour5"] = f["gap"].rolling(5).sum(), f["last_hour"].rolling(5).sum()
    f["bank_vs_nifty20"] = bank["close"].reindex(d.index).pct_change(20, fill_method=None) - ni["close"].pct_change(20, fill_method=None)
    f["vix"] = on_days(vix)
    f["vix_chg5"] = f["vix"].pct_change(5)
    f["vix_rel"] = f["vix"] / f["vix"].rolling(20).mean()
    return f.dropna()


def features(upto: str | None = None) -> pd.DataFrame:
    """Feature rows for days up to and including `upto` (YYYY-MM-DD); all of them when None."""
    f = _all_features(_stamp())
    return f if upto is None else f[f.index <= pd.Timestamp(upto)]
