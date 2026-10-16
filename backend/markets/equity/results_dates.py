"""NSE board meetings (quarterly results dates) for ALL equities -> var/archive/nse/board_meetings_all.csv.gz (equity/earnings_drift).

    python3 -m markets.equity.results_dates             # nightly: this month and last month (2 NSE calls), merged into the file
    python3 -m markets.equity.results_dates --from 2022-01-01   # backfill month by month (first built 2026-10-16: 83,864 rows)
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

import pandas as pd

from markets.equity.strategies.tf_daily.earnings_drift.strategy import EVENTS_FILE

KEYS = ["bm_symbol", "bm_date", "bm_purpose"]


def month_starts(start: date, end: date):
    m = start.replace(day=1)
    while m <= end:
        yield m
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)


def run(start: date, log=print) -> int:
    from engine.research_data import nse_get
    frames, failed = [], []
    for m in month_starts(start, date.today()):
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        r = nse_get(f"https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date={m:%d-%m-%Y}&to_date={(nxt - timedelta(days=1)):%d-%m-%Y}")
        if r.status_code == 200 and r.text.lstrip().startswith("["):
            frames.append(pd.DataFrame(r.json()).astype(str))
        else:
            failed.append(f"{m:%Y-%m} ({r.status_code})")
    have = pd.read_csv(EVENTS_FILE, dtype=str) if EVENTS_FILE.exists() else pd.DataFrame(columns=KEYS)
    new = pd.concat(frames, ignore_index=True) if frames else have.head(0)
    out = pd.concat([have, new], ignore_index=True).drop_duplicates(KEYS, keep="last")
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(EVENTS_FILE, index=False, compression="gzip")
    log(f"results dates: {len(out) - len(have):+d} rows (now {len(out):,}); failed months: {failed or 'none'}")
    return 1 if failed and not frames else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default=None, help="first month to fetch (default: last month)")
    a = ap.parse_args(argv)
    start = date.fromisoformat(a.start) if a.start else (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
    return run(start)


if __name__ == "__main__":
    raise SystemExit(main())
