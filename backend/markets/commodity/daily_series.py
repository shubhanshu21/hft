"""Daily series for the traded MCX contracts, for daily-bar strategies.  Run:  python3 -m markets.commodity.daily_series

Real MCX prices are on disk only from May 2026 (our own recording, var/archive/commodity/<SYM>_1day.csv). Before that, the world price converted to
rupees stands in (var/archive/global/XAUUSD / LIGHTCMDUSD 5-minute x the nearest USDINR future), scaled so it meets the real MCX price where both exist
(the scale absorbs import duty and the Indian premium). Writes var/archive/commodity/<SYM>_daily_adj.csv (timestamp, open, high, low, close, volume),
the file core/backtester.py and the live gold_crude strategy read.
  GOLDPETAL: rupees per gram = MCX GOLDTEN (per 10 g) / 10;  CRUDEOILM: rupees per barrel.
"""
from __future__ import annotations

import pandas as pd

from core.paths import ARCHIVE_ROOT

SOURCES = {"GOLDPETAL": ("GOLDTEN", 10.0, "XAUUSD", 10 / 31.1035 / 10), "CRUDEOILM": ("CRUDEOILM", 1.0, "LIGHTCMDUSD", 1.0)}   # real file, real divisor, world, world factor


def _daily_from_5min(path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["date"] = pd.to_datetime(d["timestamp"].astype(str).str[:10])
    g = d.groupby("date")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(), "volume": 0})
    return out[out.index.dayofweek < 5]


def build(symbol: str) -> pd.DataFrame:
    real_name, div, world, k = SOURCES[symbol]
    real = pd.read_csv(ARCHIVE_ROOT / "commodity" / f"{real_name}_1day.csv")
    real["date"] = pd.to_datetime(real["timestamp"].str[:10])
    real = real.set_index("date").sort_index()[["open", "high", "low", "close", "volume"]]
    for c in ("open", "high", "low", "close"):
        real[c] = real[c] / div
    raw = pd.read_csv(ARCHIVE_ROOT / "currency" / "futures_daily.csv.gz", parse_dates=["date", "expiry"])
    usd = raw[(raw.symbol == "USDINR") & (raw.settle > 0)].sort_values(["date", "expiry"]).groupby("date").first()["settle"]
    w = _daily_from_5min(ARCHIVE_ROOT / "global" / f"{world}_5minute.csv")
    fx = usd.reindex(w.index).ffill().bfill()
    for c in ("open", "high", "low", "close"):
        w[c] = w[c] * fx * k
    overlap = real["close"].reindex(w.index).dropna()
    scale = float((overlap / w["close"].reindex(overlap.index)).median()) if len(overlap) else 1.0
    for c in ("open", "high", "low", "close"):
        w[c] = w[c] * scale
    out = pd.concat([w[w.index < real.index[0]], real]).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return pd.DataFrame({"timestamp": out.index.strftime("%Y-%m-%dT00:00:00+05:30"), "open": out["open"].round(2), "high": out["high"].round(2),
                         "low": out["low"].round(2), "close": out["close"].round(2), "volume": out["volume"].fillna(0).astype(int)})


def main() -> int:
    for sym in SOURCES:
        df = build(sym)
        df.to_csv(ARCHIVE_ROOT / "commodity" / f"{sym}_daily_adj.csv", index=False)
        print(f"{sym}: {len(df)} days, {df.timestamp.iloc[0][:10]} -> {df.timestamp.iloc[-1][:10]} (real MCX from the first recorded day)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
