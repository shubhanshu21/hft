"""Crypto data top-up (public APIs, no keys, no orders).

    python3 -m markets.crypto.data          # top up <COIN>_1d.csv, <COIN>_5m.csv and <COIN>_funding_daily.csv

Daily candles: CoinDCX public candles for the B-<COIN>_USDT pairs (00:00 UTC) -- historical for crypto/calm_coins (removed, kept for any
future daily use). 5-minute candles: Binance USDT-M futures klines, for crypto/dip_reversal's live paper trading and backtest history (the
2022 -> 2026-10-16 depth was seeded once from an earlier 1-minute archive; this keeps it current). Funding: Binance USDT-M perpetual funding
history summed per UTC day (Delta Exchange India's futures track the same market and funding interval -- 8-hourly, verified 2026-10-21 -- and have no simple historical-range public endpoint). Only COMPLETED candles/days
are kept, so a decision never reads one still forming. Re-fetches a short recent window each run, so a missed night heals itself.
Open interest (2026-11-21): Binance USD-M "metrics" daily files (5-min open interest, long/short and taker ratios) from data.binance.vision
-> metrics/<COIN>_metrics.csv.gz, for dip_reversal's open-interest filter in the backtest (the live engine reads Binance in real time).
"""
from __future__ import annotations

import io
import sys
import time
import zipfile

import pandas as pd
import requests

from core.paths import ARCHIVE_ROOT
DATA_DIR = ARCHIVE_ROOT / "crypto"
COINS = ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "TRX", "ZEC")   # ZEC 2026-11-20

METRICS_DIR = DATA_DIR / "metrics"
METRICS_URL = "https://data.binance.vision/data/futures/um/daily/metrics/{c}USDT/{c}USDT-metrics-{d}.zip"
DAY_MS = 86_400_000
LOOKBACK_DAYS = 30
MIN_MS = 60_000


def _today_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()


def _merge(path, new: pd.DataFrame) -> int:
    old = pd.read_csv(path, dtype={"date": str}) if path.exists() else pd.DataFrame(columns=new.columns)
    out = pd.concat([old, new]).drop_duplicates("date", keep="last").sort_values("date")
    out = out[out["date"] < _today_utc().strftime("%Y-%m-%d")]              # completed days only
    out.to_csv(path, index=False)
    return len(out) - len(old)


def candles(coin: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    end = int(time.time() * 1000); start = end - days * DAY_MS
    r = requests.get("https://public.coindcx.com/market_data/candles",
                     params={"pair": f"B-{coin}_USDT", "interval": "1d", "startTime": start, "endTime": end, "limit": 1000}, timeout=30)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    df["date"] = pd.to_datetime(df["time"], unit="ms").dt.strftime("%Y-%m-%d")
    return df[["date", "open", "high", "low", "close", "volume"]]


def candles_5m(coin: str, days: int = 5) -> pd.DataFrame:
    """Finished 5-minute Binance USDT-M futures candles, last `days` days (default short -- this just heals recent gaps; the deep
    2022-> history lives in <COIN>_5m.csv already and is never re-fetched in bulk here)."""
    end = int(time.time() * 1000) - MIN_MS; start = end - days * DAY_MS
    out = []
    while start < end:
        r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                         params={"symbol": f"{coin}USDT", "interval": "5m", "startTime": start, "limit": 1000}, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        out.extend(rows)
        start = rows[-1][0] + 5 * MIN_MS
        time.sleep(0.2)
    if not out:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close"])
    df = pd.DataFrame(out, columns=["t", "open", "high", "low", "close", "v", "close_t"] + [f"x{i}" for i in range(5)])
    df = df.astype({"t": "int64", "close_t": "int64", "open": "float64", "high": "float64", "low": "float64", "close": "float64"})
    df = df[df["close_t"] < end][["t", "open", "high", "low", "close"]]             # completed candles only
    df["date"] = pd.to_datetime(df["t"], unit="ms").dt.strftime("%Y-%m-%dT%H:%M:%SZ")   # opening time (close_t ends in .999: labels were 1 s early, 2026-11-05)
    return df[["date", "open", "high", "low", "close"]]


def _merge_5m(path, new: pd.DataFrame) -> int:
    old = pd.read_csv(path, dtype={"date": str}) if path.exists() else pd.DataFrame(columns=new.columns)
    out = pd.concat([old, new]).drop_duplicates("date", keep="last").sort_values("date")
    out.to_csv(path, index=False)
    return len(out) - len(old)


def funding(coin: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    start = int(time.time() * 1000) - days * DAY_MS
    r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": f"{coin}USDT", "startTime": start, "limit": 1000}, timeout=30)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    df["date"] = pd.to_datetime(df["fundingTime"], unit="ms").dt.strftime("%Y-%m-%d")
    return df.assign(fundingRate=df["fundingRate"].astype(float)).groupby("date", as_index=False)["fundingRate"].sum()


def _fetch_zip(url: str) -> bytes | None:
    r = requests.get(url, timeout=30, headers={"User-Agent": "research-client"})
    if r.status_code == 404:                                   # that day is not published yet (it appears a day later)
        return None
    r.raise_for_status()
    return r.content


def topup_metrics(coins=None, today: pd.Timestamp | None = None, fetch=_fetch_zip, max_days: int = 40) -> dict:
    """Append each missing UTC day (after the file's last row, up to yesterday) of Binance's 5-minute metrics. Zips are read in memory
    as data, never extracted. Returns rows added per coin."""
    if coins is None:
        from markets.crypto.strategies.tf_5min.dip_reversal.strategy import COINS as coins
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    today = (today or _today_utc()).normalize()
    out = {}
    for c in coins:
        path = METRICS_DIR / f"{c}_metrics.csv.gz"
        have = pd.read_csv(path) if path.exists() else pd.DataFrame()
        last = pd.Timestamp(have["create_time"].max()).normalize() if len(have) else today - pd.Timedelta(days=max_days + 1)
        frames = []
        for d in pd.date_range(last + pd.Timedelta(days=1), today - pd.Timedelta(days=1), freq="D")[-max_days:]:
            raw = fetch(METRICS_URL.format(c=c, d=d.strftime("%Y-%m-%d")))
            if raw is None:
                continue
            z = zipfile.ZipFile(io.BytesIO(raw))
            frames.append(pd.read_csv(z.open(z.namelist()[0])).drop(columns=["symbol"], errors="ignore"))
        if frames:
            new = pd.concat([have, *frames], ignore_index=True).drop_duplicates("create_time").sort_values("create_time")
            new.to_csv(path, index=False, compression="gzip")
            out[c] = len(new) - len(have)
        else:
            out[c] = 0
    return out


def topup() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    added, errors = {}, []
    for c in COINS:
        try:
            added[c] = _merge(DATA_DIR / f"{c}_1d.csv", candles(c))
        except Exception as e:
            errors.append(f"{c} candles: {e}")
        try:
            _merge(DATA_DIR / f"{c}_funding_daily.csv", funding(c))
        except Exception as e:
            errors.append(f"{c} funding: {e}")
        try:
            added[f"{c}_5m"] = _merge_5m(DATA_DIR / f"{c}_5m.csv", candles_5m(c))
        except Exception as e:
            errors.append(f"{c} 5m candles: {e}")
        time.sleep(0.3)
    try:
        added["open_interest"] = topup_metrics()
    except Exception as e:
        errors.append(f"open-interest metrics: {e}")
    return {"added": added, "errors": errors}


if __name__ == "__main__":
    out = topup()
    print("new days:", out["added"])
    for e in out["errors"]:
        print("ERROR", e)
    sys.exit(1 if out["errors"] else 0)
