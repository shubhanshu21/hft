"""Latest price of each OPEN position, written by the daemon and read by the dashboard.

The dashboard never talks to Upstox (a second client could disturb the single active token), yet an open position's unrealised P&L needs a current
price. The daemon already has one every scan (the newest candle it manages the position with), so it drops it in var/db/live_prices.json; the dashboard
only reads that file. Written atomically; never raises (a full disk must not stop trading); entries for symbols no longer open are removed on `prune`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from core.paths import DB_DIR

IST = timezone(timedelta(hours=5, minutes=30))
PATH = DB_DIR / "live_prices.json"


def read(path=None) -> dict:
    try:
        return json.loads((path or PATH).read_text())
    except (OSError, ValueError):
        return {}


def update(symbol: str, price: float, now: datetime | None = None, path=None) -> None:
    p = path or PATH
    data = read(p)
    data[symbol] = {"price": float(price), "ts": (now or datetime.now(IST)).isoformat(timespec="seconds")}
    _write(data, p)


def prune(open_symbols, path=None) -> None:
    p = path or PATH
    data = read(p)
    kept = {k: v for k, v in data.items() if k in set(open_symbols)}
    if kept != data:
        _write(kept, p)


def _write(data: dict, p) -> None:
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, p)
    except OSError:
        pass
