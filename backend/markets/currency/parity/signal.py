"""EURINR / GBPINR fair-value gap -- the one currency edge that survived testing (docs/CURRENCY_RESEARCH.md, "Machine learning" and
"Round 2"). NSE's EURINR should equal EUR/USD x NSE USDINR (GBPINR likewise). The crosses trade ~1,000-2,000 contracts a day and lag the
liquid legs; when the gap opens, the cross catches up within ~30 minutes.

gap = log(cross) - log(EUR/USD) - log(USDINR), in bp, minus its median over the last 60 five-minute bars (the median absorbs the forward
premium and the spot-vs-futures basis). Fade it: cross rich -> short, cheap -> long, hold 30 minutes.

Thresholds chosen on 2026-08-21..09-16 only, then tested on 09-17..10-09 (16 days, never used to choose): EURINR |gap| > 10 bp, 47
trades, +3.5 bp a trade after costs at 5 lots (t = 3.1); GBPINR |gap| > 12 bp, 24 trades, +3.1 bp (t = 1.6). Shared by the live
strategy (strategy.py) and the backtest (backtest.py) so the two cannot drift."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

FX = {"EURINR": "EURUSD=X", "GBPINR": "GBPUSD=X"}       # Yahoo tickers of the dollar leg (inputs only, never traded)
THRESHOLD_BP = {"EURINR": 10.0, "GBPINR": 12.0}
HOLD_MIN = 30
STOP_BP = 25.0                                            # protective stop, ~3x the typical 30-minute move; rarely reached
MAX_SPREAD_BP = 15.0                                      # skip a signal when the book is wider than this (the gap is then mostly spread)
MEDIAN_BARS, MEDIAN_MIN_BARS = 60, 20
ROLL_BREAK_BP = 25.0
BAR = timedelta(minutes=5)
IST = "Asia/Kolkata"


def closes(candles: list[dict], now: datetime | None = None) -> pd.Series:
    """Close of each COMPLETED 5-minute bar, indexed by bar start (IST). A bar is complete once its 5 minutes have passed."""
    if not candles:
        return pd.Series(dtype=float)
    ts = pd.to_datetime([str(c["timestamp"]) for c in candles], utc=True).tz_convert(IST)
    s = pd.Series([float(c["close"]) for c in candles], index=ts).groupby(level=0).last().sort_index()
    if now is not None:
        s = s[s.index + BAR <= pd.Timestamp(now).tz_convert(IST)]
    return s


def gap_bp(cross: pd.Series, usdinr: pd.Series, fx: pd.Series) -> pd.Series:
    """The gap on each of the cross's bars. `fx` is a 5-minute close series indexed by bar start; it is matched to the cross's bar
    within 10 minutes, USDINR within two bars. NaN where an input is missing."""
    idx = cross.index
    u = usdinr.reindex(idx).ffill(limit=2)
    f = fx.sort_index().reindex(idx, method="ffill", tolerance=pd.Timedelta("10min"))
    dev = (np.log(cross) - np.log(f) - np.log(u)) * 1e4
    # A contract roll moves the basis by about a month of forward premium (30-70 bp) overnight, and USDINR and the cross need not roll on
    # the same day: an overnight jump > ROLL_BREAK_BP starts a new baseline instead of reading as a gap for the next few hours.
    day = pd.Series(idx.normalize(), index=idx)
    jump = (dev.ffill().diff().abs() > ROLL_BREAK_BP) & (day != day.shift(1))
    segment = jump.cumsum()
    med = dev.groupby(segment).transform(lambda x: x.rolling(MEDIAN_BARS, min_periods=MEDIAN_MIN_BARS).median())
    return dev - med


_fx_cache: dict[str, tuple[float, pd.Series]] = {}


def yahoo_fx_5m(ticker: str, max_age_s: float = 50.0) -> pd.Series:
    """The last few days of the dollar leg at 5 minutes (Yahoo 1-minute closes, last close in each 5-minute bar), IST bar starts.
    Cached for under a minute; an empty series on any failure (the strategy then does not trade)."""
    hit = _fx_cache.get(ticker)
    if hit and time.time() - hit[0] < max_age_s:
        return hit[1]
    try:
        import requests
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}", params={"interval": "1m", "range": "5d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        res = r.json()["chart"]["result"][0]
        s = pd.Series(res["indicators"]["quote"][0]["close"], index=pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(IST)).dropna()
        s = s.resample("5min", label="left", closed="left").last().dropna()
    except Exception:
        s = pd.Series(dtype=float)
    _fx_cache[ticker] = (time.time(), s)
    return s
