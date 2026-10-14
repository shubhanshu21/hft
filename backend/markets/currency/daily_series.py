"""Continuous daily series of each NSE currency future, back-adjusted at rolls, for daily-bar backtests.  Run:  python3 -m markets.currency.daily_series

Source: var/archive/currency/futures_daily.csv.gz (every NSE currency future, daily, 2012 on; engine/research_data.py "currency-daily", nightly).
Each day takes the contract with the most open interest that has at least 3 days to expiry; at each roll the older history is scaled by the ratio of
the new contract's settle to the old one's on that day, so the series has no roll gaps. Bad prints more than 3% from the settle are clipped.
Writes var/archive/currency/<PAIR>_daily_adj.csv (timestamp, open, high, low, close, volume), the file core/backtester.py reads for currency.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT

PAIRS = ("USDINR", "EURINR", "GBPINR")
SOURCE = ARCHIVE_ROOT / "currency" / "futures_daily.csv.gz"


def build(pair: str, raw: pd.DataFrame) -> pd.DataFrame:
    d = raw[raw.symbol == pair]
    rows = []
    for day, g in d.groupby("date"):
        live = g[(g.expiry - day).dt.days >= 3]
        if len(live):
            rows.append(live.sort_values(["oi", "volume"], ascending=False).iloc[0])
    f = pd.DataFrame(rows).reset_index(drop=True)
    adj = np.ones(len(f))
    for i in range(len(f) - 1, 0, -1):
        if f.expiry.iat[i] != f.expiry.iat[i - 1]:
            prev = d[(d.date == f.date.iat[i]) & (d.expiry == f.expiry.iat[i - 1])]
            adj[:i] *= (f.settle.iat[i] / prev.settle.iat[0]) if len(prev) else 1.0
    f["close"] = f["settle"] * adj
    for k in ("open", "high", "low"):
        f[k] = np.where(f[k] > 0, f[k] * adj, f["close"])
    f["high"] = np.maximum.reduce([f["high"], f["open"], f["close"]])
    f["low"] = np.minimum.reduce([f["low"], f["open"], f["close"]])
    f["low"] = np.where(f["low"] < f["close"] * 0.97, f["close"], f["low"])
    f["high"] = np.where(f["high"] > f["close"] * 1.03, f["close"], f["high"])
    return pd.DataFrame({"timestamp": f["date"].dt.strftime("%Y-%m-%dT00:00:00+05:30"), "open": f["open"].round(4), "high": f["high"].round(4),
                         "low": f["low"].round(4), "close": f["close"].round(4), "volume": f["volume"]})


def main() -> int:
    raw = pd.read_csv(SOURCE, parse_dates=["date", "expiry"])
    raw = raw[(raw.close > 0) & (raw.settle > 0)]
    for pair in PAIRS:
        out = build(pair, raw)
        out.to_csv(ARCHIVE_ROOT / "currency" / f"{pair}_daily_adj.csv", index=False)
        print(f"{pair}: {len(out)} days, {out.timestamp.iloc[0][:10]} -> {out.timestamp.iloc[-1][:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
