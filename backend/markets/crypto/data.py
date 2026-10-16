"""Crypto daily data top-up for crypto/calm_btc (public APIs, no keys, no orders).

    python3 -m markets.crypto.data          # top up var/archive/crypto/<COIN>_1d.csv and <COIN>_funding_daily.csv

Candles: CoinDCX public candles for the B-<COIN>_USDT pairs (daily, 00:00 UTC). Funding: Binance USDT-M perpetual funding history summed per
UTC day (CoinDCX's futures track the same market; their own history has no public endpoint). Only COMPLETED UTC days are kept: the day still
forming is dropped, so a decision never reads a half candle. Re-fetches the last 30 days each run (a few KB), so a missed night heals itself.
"""
from __future__ import annotations

import sys
import time

import pandas as pd
import requests

from markets.crypto.strategies.tf_daily.calm_btc.strategy import COINS, DATA_DIR

DAY_MS = 86_400_000
LOOKBACK_DAYS = 30


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


def funding(coin: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    start = int(time.time() * 1000) - days * DAY_MS
    r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": f"{coin}USDT", "startTime": start, "limit": 1000}, timeout=30)
    r.raise_for_status()
    df = pd.DataFrame(r.json())
    df["date"] = pd.to_datetime(df["fundingTime"], unit="ms").dt.strftime("%Y-%m-%d")
    return df.assign(fundingRate=df["fundingRate"].astype(float)).groupby("date", as_index=False)["fundingRate"].sum()


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
        time.sleep(0.3)
    return {"added": added, "errors": errors}


if __name__ == "__main__":
    out = topup()
    print("new days:", out["added"])
    for e in out["errors"]:
        print("ERROR", e)
    sys.exit(1 if out["errors"] else 0)
