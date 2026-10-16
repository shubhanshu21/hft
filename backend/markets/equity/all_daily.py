"""Daily candles for every NSE equity -> var/archive/equity_all/<SYM>_daily.csv.gz (equity/breakout_pair's stock list and signals).

    python3 -m markets.equity.all_daily                    # nightly: the liquid pool first, then the rest, oldest first, within --max-minutes
    python3 -m markets.equity.all_daily --max-minutes 0    # no time limit (a full refresh is ~4,250 calls, ~90 min)

breakout_pair picks from the 200 most-traded stocks of each day, so the ~400 most-traded (by 126-day median traded value) plus anything
the paper account holds are refreshed every night; the remaining stocks are refreshed oldest-first in what time is left, so a stock that
becomes liquid joins the pool within a few nights. Stocks: NSE_EQ equities with an INE ISIN and lot size 1 (no SME, bonds or ETFs).
First built 2026-10-16 with a full download from 2022-01-01 (49 MB). Upstox calls are paced (UPSTOX_PACE_S, default 1.2 s).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import re
import time
from datetime import date, timedelta

import pandas as pd

from core.paths import CACHE_DIR
from markets.equity.strategies.tf_daily.breakout_pair.strategy import DATA_DIR, LIQ_DAYS

POOL = 400
START = "2022-01-01"


def instruments() -> dict[str, str]:
    """tradingsymbol -> instrument_key for plain NSE equities (the same filter as the 2026-10-16 download)."""
    from services.broker.instruments import ensure_master
    ensure_master()
    out = {}
    with gzip.open(CACHE_DIR / "upstox_instruments_nse.csv.gz", "rt") as fh:
        for r in csv.DictReader(fh):
            sym = r["tradingsymbol"]
            if (r["exchange"] == "NSE_EQ" and r["instrument_type"] == "EQUITY" and r["instrument_key"].split("|")[1].startswith("INE")
                    and r["lot_size"] == "1" and re.fullmatch(r"[A-Z0-9&]+(-[A-Z]+)?", sym)
                    and not re.search(r"-(N|SM|BE|BZ|RR|GB|IV|NC|ST|X)\w*$", sym)):
                out[sym] = r["instrument_key"]
    return out


def _read(path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except (OSError, ValueError):
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])


def liquid_pool(n: int = POOL) -> list[str]:
    """The n stocks with the highest 126-day median traded value in the archive."""
    vals = {}
    for f in DATA_DIR.glob("*_daily.csv.gz"):
        x = _read(f).tail(LIQ_DAYS)
        if len(x) >= 20:
            vals[f.name[: -len("_daily.csv.gz")]] = float((x["close"] * x["volume"]).median())
    return [s for s, _ in sorted(vals.items(), key=lambda kv: -kv[1])[:n]]


def topup_one(broker, sym: str, key: str, today: date) -> int:
    from engine.research_data import candles_to_df, pace
    path = DATA_DIR / f"{sym}_daily.csv.gz"
    have = _read(path)
    start = START if have.empty else str(pd.to_datetime(have["timestamp"].iloc[-1][:10]).date() - timedelta(days=3))
    pace()
    new = candles_to_df(broker.get_historical_candles(key, unit="days", interval=1, to_date=today.isoformat(), from_date=start))
    out = pd.concat([have, new]).drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    out.to_csv(path, index=False, compression="gzip")
    return len(out) - len(have)


def run(max_minutes: float = 40.0, extra: list[str] | None = None, log=print) -> int:
    from engine.research_data import broker
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    keys = instruments()
    pool = [s for s in dict.fromkeys(list(extra or []) + liquid_pool()) if s in keys]
    stale = lambda s: (DATA_DIR / f"{s}_daily.csv.gz").stat().st_mtime if (DATA_DIR / f"{s}_daily.csv.gz").exists() else 0.0
    rest = sorted((s for s in keys if s not in set(pool)), key=stale)
    b, today, t0, done, added, failed = broker(), date.today(), time.monotonic(), 0, 0, 0
    for i, s in enumerate(pool + rest):
        if i >= len(pool) and max_minutes and time.monotonic() - t0 > max_minutes * 60:
            break
        try:
            added += topup_one(b, s, keys[s], today); done += 1
        except Exception as exc:                      # one stock failing must not stop the rest
            failed += 1
            log(f"{s}: {exc!s:.120}")
    log(f"all_daily: {done} stocks refreshed ({len(pool)} in the liquid pool), {added} new rows, {failed} failed, {time.monotonic() - t0:.0f}s")
    return 1 if failed > max(10, done // 10) else 0


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-minutes", type=float, default=40.0, help="stop starting new non-pool stocks after this (0 = no limit)")
    a = ap.parse_args(argv)
    held = []
    try:
        from engine.database import TradingDB
        held = [p["symbol"] for p in TradingDB().get_open_positions("EQUITY_BREAKOUT")]
    except Exception:
        pass
    return run(a.max_minutes, extra=held)


if __name__ == "__main__":
    raise SystemExit(main())
