"""Record NSE pre-open quotes (09:00-09:16 IST) for every stock with a daily archive -- record only, never trades.

Why: on stocks that open far from the market (|gap vs average stock| >= 1%), the opening AUCTION price gives back ~20-28 bp by 09:25 (2022-26,
5-minute data), but entering one minute after the open leaves only ~4-6 bp, below costs. So the move can only be traded by an order placed in
the 09:00-09:08 pre-open auction, decided on the price the auction shows before it closes. No history of that price exists; this records it
so a forward test can check (a) how close the ~09:07 price is to the final open and (b) whether fading it still pays.

One batch quote call (up to 500 keys) every POLL_S seconds -> var/archive/preopen/<YYYY-MM-DD>.csv.gz
(polled_at, symbol, last_price, last_trade_time, volume, total_buy_qty, total_sell_qty, bid, bid_qty, ask, ask_qty, open, prev_close).

    python3 -m engine.preopen_recorder            # run by hft-preopen-recorder.timer at 08:59 IST Mon-Fri
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import requests

from core.paths import ARCHIVE_ROOT
from engine.config import _load_access_token
from services.broker.instruments import ensure_master, get_instrument_key
from services.utils.market_holidays import get_trading_holidays

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = ARCHIVE_ROOT / "preopen"
POLL_S = 10
START, END = (9, 0), (9, 16)


def symbols() -> list[str]:
    return sorted(p.name[: -len("_daily.csv")] for p in (ARCHIVE_ROOT / "equity").glob("*_daily.csv"))


def poll(keys: dict[str, str], token: str) -> list[dict]:
    r = requests.get("https://api.upstox.com/v2/market-quote/quotes", params={"instrument_key": ",".join(keys.values())},
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=10)
    r.raise_for_status()
    now = datetime.now(IST).isoformat(timespec="seconds")
    by_key = {v: k for k, v in keys.items()}
    rows = []
    for v in (r.json().get("data") or {}).values():
        depth = v.get("depth") or {}
        buy, sell = (depth.get("buy") or [{}])[0], (depth.get("sell") or [{}])[0]
        ohlc = v.get("ohlc") or {}
        rows.append({"polled_at": now, "symbol": by_key.get(v.get("instrument_token"), v.get("symbol")), "last_price": v.get("last_price"),
                     "last_trade_time": v.get("last_trade_time"), "volume": v.get("volume"), "total_buy_qty": v.get("total_buy_quantity"),
                     "total_sell_qty": v.get("total_sell_quantity"), "bid": buy.get("price"), "bid_qty": buy.get("quantity"),
                     "ask": sell.get("price"), "ask_qty": sell.get("quantity"), "open": ohlc.get("open"),
                     "prev_close": (v.get("last_price") or 0) - (v.get("net_change") or 0) if v.get("net_change") is not None else None})
    return rows


def main() -> None:
    today = datetime.now(IST)
    if today.weekday() >= 5 or today.date() in get_trading_holidays():
        print(f"{today.date()}: not an NSE trading day")
        return
    ensure_master()
    keys = {s: k for s in symbols() if (k := get_instrument_key(s))}
    token = _load_access_token()
    if not token:
        print("no Upstox token")
        return
    start = today.replace(hour=START[0], minute=START[1], second=0, microsecond=0)
    end = today.replace(hour=END[0], minute=END[1], second=0, microsecond=0)
    while datetime.now(IST) < start:
        time.sleep(1)
    rows = []
    while datetime.now(IST) < end:
        t0 = time.time()
        try:
            rows += poll(keys, token)
        except Exception as e:                       # keep recording; one failed poll is a 10 s hole, not a lost day
            print(f"{datetime.now(IST):%H:%M:%S} poll failed: {e!s:.200}")
        time.sleep(max(0.0, POLL_S - (time.time() - t0)))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{today.date()}.csv.gz"
    pd.DataFrame(rows).to_csv(path, index=False, compression="gzip")
    print(f"{len(keys)} symbols, {len(rows)} rows -> {path}")


if __name__ == "__main__":
    main()
