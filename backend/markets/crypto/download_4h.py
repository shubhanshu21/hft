"""Download / Top-Up 4-hour crypto candles from Binance USDT-M Futures.

Usage:
    python3 -m markets.crypto.download_4h
    python3 -m markets.crypto.download_4h --coins BTC ETH SOL
    python3 -m markets.crypto.download_4h --all

Saved to var/archive/crypto/<COIN>_4h.csv with columns:
    time,open,high,low,close,volume
"""
from __future__ import annotations

import argparse
import time

import pandas as pd
import requests

from core.paths import ARCHIVE_ROOT

DATA_DIR = ARCHIVE_ROOT / "crypto"
BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"

DEFAULT_COINS = ["BTC", "ETH", "SOL", "DOGE", "BNB"]
ALL_COINS = ["BTC", "ETH", "SOL", "DOGE", "BNB", "AVAX", "ADA", "XRP", "LINK", "DOT", "LTC", "TRX"]


def fetch_4h_candles(symbol: str, start_ms: int, end_ms: int | None = None) -> list:
    """Fetch 4-hour klines from Binance in chunks of up to 1500 bars."""
    all_rows = []
    cur_start = start_ms
    now_ms = int(time.time() * 1000) if end_ms is None else end_ms

    while cur_start < now_ms:
        params = {
            "symbol": f"{symbol}USDT",
            "interval": "4h",
            "startTime": cur_start,
            "endTime": now_ms,
            "limit": 1500,
        }
        try:
            r = requests.get(BINANCE_KLINES_URL, params=params, timeout=20)
            if r.status_code != 200:
                print(f"[{symbol}] HTTP {r.status_code}: {r.text[:100]}", flush=True)
                break
            data = r.json()
            if not data:
                break
            all_rows.extend(data)
            # data[-1][0] is candle open time in ms. Advance +4 hours (14,400,000 ms)
            cur_start = data[-1][0] + 4 * 3600 * 1000
            time.sleep(0.08)  # respectful API pacing
        except Exception as ex:
            print(f"[{symbol}] Network error: {ex}", flush=True)
            break

    return all_rows


def klines_to_df(data: list) -> pd.DataFrame:
    """Format raw Binance klines to DataFrame and drop currently forming candle."""
    if not data:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    rows = []
    for d in data:
        t_str = pd.to_datetime(d[0], unit="ms", utc=True).strftime("%Y-%m-%d %H:%M")
        rows.append({
            "time": t_str,
            "open": float(d[1]),
            "high": float(d[2]),
            "low": float(d[3]),
            "close": float(d[4]),
            "volume": float(d[5]),
        })
    df = pd.DataFrame(rows)

    now_ms = int(time.time() * 1000)
    if not df.empty:
        # Check if the last candle is still forming (starts < 4 hours ago)
        last_open_ms = int(pd.Timestamp(df.iloc[-1]["time"], tz="UTC").timestamp() * 1000)
        if now_ms < last_open_ms + 4 * 3600 * 1000:
            df = df.iloc[:-1]  # keep completed candles only

    return df


def download_or_topup(coin: str, start_date: str = "2022-01-01 00:00") -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_path = DATA_DIR / f"{coin}_4h.csv"
    existing_df = None
    start_ms = int(pd.Timestamp(start_date, tz="UTC").timestamp() * 1000)

    if file_path.exists():
        existing_df = pd.read_csv(file_path)
        if not existing_df.empty and "time" in existing_df.columns:
            last_ts = pd.Timestamp(existing_df.iloc[-1]["time"], tz="UTC")
            start_ms = int(last_ts.timestamp() * 1000)

    raw_data = fetch_4h_candles(coin, start_ms)
    new_df = klines_to_df(raw_data)

    if existing_df is not None and not existing_df.empty:
        merged = pd.concat([existing_df, new_df]).drop_duplicates(subset=["time"], keep="last").sort_values("time")
        added = len(merged) - len(existing_df)
    else:
        merged = new_df.drop_duplicates(subset=["time"], keep="last").sort_values("time")
        added = len(merged)

    if not merged.empty:
        merged.to_csv(file_path, index=False)
        print(f"[{coin}] +{added} candles (total {len(merged):,} rows, {merged.iloc[0]['time']} -> {merged.iloc[-1]['time']})", flush=True)
    return added


def main():
    parser = argparse.ArgumentParser(description="Download/Top-up 4-hour crypto candles from Binance")
    parser.add_argument("--coins", nargs="+", help="Coins to download (e.g. BTC ETH SOL)")
    parser.add_argument("--all", action="store_true", help="Download all 12 tracked crypto coins")
    parser.add_argument("--start", default="2022-01-01 00:00", help="Start date (default: 2022-01-01)")
    args = parser.parse_args()

    coins_to_fetch = ALL_COINS if args.all else (args.coins or DEFAULT_COINS)
    print(f"Fetching 4-hour data for: {', '.join(coins_to_fetch)}", flush=True)

    total_added = 0
    for coin in coins_to_fetch:
        added = download_or_topup(coin, start_date=args.start)
        total_added += added

    print(f"\nDone! Downloaded/updated {total_added:,} candles across {len(coins_to_fetch)} coins.", flush=True)


if __name__ == "__main__":
    main()
