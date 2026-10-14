"""What trend_ema_cross is waiting for, from the same nightly daily archive and the same rules the paper trader uses (read-only).

Served by engine/backtest_api.py as GET /api/equity-status for the Paper trading page.
"""
from __future__ import annotations

import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.equity.universe import NIFTY50_SYMBOLS


def _daily(sym: str) -> pd.DataFrame:
    d = pd.read_csv(ARCHIVE_ROOT / "equity" / f"{sym}_daily.csv")
    d.index = pd.to_datetime(d["timestamp"].str[:10])
    return d[["high", "low", "close"]].astype(float)


def equity_status(history_days: int = 30) -> dict:
    import markets.equity.strategies.tf_daily.trend_ema_cross.strategy as T
    closes = {s: _daily(s)["close"] for s in NIFTY50_SYMBOLS}
    c = pd.DataFrame(closes)
    breadth = (c > c.ewm(span=50, adjust=False).mean()).sum(axis=1) / c.notna().sum(axis=1) * 100
    as_of = breadth.index[-1]
    stocks = []
    for sym in T.BEST_SYMBOLS:
        d = _daily(sym).tail(T.WINDOW)
        px = d["close"]
        fast, slow, long_ = (px.ewm(span=n, adjust=False).mean() for n in (T.FAST, T.SLOW, T.LONG))
        above_long = bool(px.iloc[-1] > long_.iloc[-1])
        gap = float(fast.iloc[-1] / slow.iloc[-1] - 1) * 100            # > 0: the 20-day average is already above the 50-day
        crossed_today = bool(fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1])
        up = fast > slow
        last_cross = up & ~up.shift(fill_value=True)
        last_cross_day = str(last_cross[last_cross].index[-1].date()) if last_cross.any() else None
        if crossed_today and above_long:
            state, order = "signal today: buys tomorrow if the market is healthy", 0
        elif not above_long:
            state, order = f"below its 200-day average ({float(px.iloc[-1] / long_.iloc[-1] - 1) * 100:+.1f}%): not eligible", 3
        elif gap > 0:
            state, order = "uptrend already running: waits for the next fresh crossover", 2
        else:
            state, order = f"20-day average {abs(gap):.1f}% below the 50-day: needs to cross up", 1
        stocks.append({"symbol": sym, "close": round(float(px.iloc[-1]), 2), "above_200": above_long, "gap_pct": round(gap, 2),
                       "last_cross": last_cross_day, "state": state, "order": order})
    for sym in T.GLOBAL_ETFS:                                            # foreign-market ETFs: no market-health rule, a premium rule instead
        d = _daily(sym).tail(T.WINDOW); px = d["close"]
        fast, slow, long_ = (px.ewm(span=n, adjust=False).mean() for n in (T.FAST, T.SLOW, T.LONG))
        above_long, gap = bool(px.iloc[-1] > long_.iloc[-1]), float(fast.iloc[-1] / slow.iloc[-1] - 1) * 100
        prem = T.premium(sym, str(d.index[-1].date()))
        up = fast > slow; lc = up & ~up.shift(fill_value=True)
        if prem is None or prem > T.PREMIUM_BUY_MAX:
            state, order = f"ETF {prem:+.1f}% above its real value (limit {T.PREMIUM_BUY_MAX:.0f}%): not buyable" if prem is not None else "ETF premium unknown: not buyable", 3
        elif not above_long:
            state, order = "ETF below its 200-day average: not eligible", 3
        elif gap > 0:
            state, order = f"ETF uptrend already running (premium {prem:+.1f}%)", 2
        else:
            state, order = f"ETF 20-day average {abs(gap):.1f}% below the 50-day (premium {prem:+.1f}%)", 1
        stocks.append({"symbol": sym, "close": round(float(px.iloc[-1]), 2), "above_200": above_long, "gap_pct": round(gap, 2),
                       "last_cross": str(lc[lc].index[-1].date()) if lc.any() else None, "state": state, "order": order})
    stocks.sort(key=lambda s: (s["order"], -s["gap_pct"] if s["order"] == 1 else 0))
    return {"strategy": "trend_ema_cross", "as_of": str(as_of.date()), "breadth": round(float(breadth.iloc[-1]), 1), "need": T.MIN_BREADTH_PCT,
            "breadth_history": [{"date": str(t.date()), "value": round(float(v), 1)} for t, v in breadth.tail(history_days).items()],
            "stocks": stocks}
