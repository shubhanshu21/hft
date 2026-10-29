#!/usr/bin/env python3
"""
markets/commodity/futures_eod.py -- daily MCX futures table, every expiry, for the trend_carry strategy and its backtests.

    python3 -m markets.commodity.futures_eod            # top up (run nightly by engine/nightly.py, step "mcx futures eod")

Output: var/archive/commodity/mcx_futures_daily.csv with one row per (date, symbol, expiry): open, high, low, close, volume, oi, source.
  * history 2021-12 -> 2026-10: the MCX month-end bhavcopies the user downloaded in their own browser on 2026-10-15
    (var/archive/commodity/mcx_download/*.json; mcxindia.com blocks this server, so no script here fetches from it), source "bhavcopy";
  * from then on: Upstox daily candles of every ACTIVE futures contract of the families below (all expiries, so the near/next price gap
    -- the carry -- is known every day), source "upstox". Upstox only serves live contracts, so a missed night is caught up only while the
    contract is still trading; the nightly job keeps the gap small.
Families: the signal contracts (the liquid "big" symbols in the bhavcopy) and the mini contracts the strategy trades.
"""
from __future__ import annotations

import gzip
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from core.paths import ARCHIVE_ROOT

log = logging.getLogger(__name__)

SIGNAL_SYMBOLS = ("GOLDM", "SILVERM", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC", "ALUMINIUM", "LEAD")
TRADE_SYMBOLS = ("GOLDTEN", "SILVERMIC", "CRUDEOILM", "NATGASMINI", "ZINCMINI", "ALUMINI", "LEADMINI")
OUT = ARCHIVE_ROOT / "commodity" / "mcx_futures_daily.csv"
DOWNLOADS = ARCHIVE_ROOT / "commodity" / "mcx_download"
COLS = ["date", "symbol", "expiry", "open", "high", "low", "close", "volume", "oi", "source"]


def from_bhavcopies(folder: Path = DOWNLOADS) -> pd.DataFrame:
    """FUTCOM rows of every downloaded bhavcopy JSON ({date: {status, body}} as saved by the browser snippet)."""
    rows = []
    for f in sorted(folder.glob("*.json")):
        for day, v in json.loads(f.read_text()).items():
            if isinstance(v, list):                                  # compact daily files (mcx_daily_<year>.json, 2026-10-16):
                d = pd.to_datetime(day, format="%d/%m/%Y")           # {"dd/mm/yyyy": [[symbol, expiry, open, high, low, close, volume, oi], ...]}
                rows += [{"date": d, "symbol": r[0], "expiry": pd.to_datetime(r[1], format="%d%b%Y"), "open": r[2], "high": r[3], "low": r[4],
                          "close": r[5], "volume": r[6], "oi": r[7], "source": "bhavcopy"} for r in v]
                continue
            if v.get("status") != 200:
                continue
            for r in json.loads(v["body"]).get("Data") or []:
                if str(r.get("InstrumentName", "")).strip() != "FUTCOM":
                    continue
                rows.append({"date": pd.to_datetime(r["Date"], format="%m/%d/%Y"), "symbol": r["Symbol"].strip(),
                             "expiry": pd.to_datetime(r["ExpiryDate"].strip(), format="%d%b%Y"), "open": r["Open"], "high": r["High"],
                             "low": r["Low"], "close": r["Close"], "volume": r["Volume"], "oi": r["OpenInterest"], "source": "bhavcopy"})
    return pd.DataFrame(rows, columns=COLS)


def load() -> pd.DataFrame:
    if not OUT.exists():
        return pd.DataFrame(columns=COLS)
    return pd.read_csv(OUT, parse_dates=["date", "expiry"])


def active_contracts(master: Path | None = None) -> pd.DataFrame:
    """Live futures of every family from the Upstox MCX master (downloaded daily by services/broker/instruments.ensure_master)."""
    from services.broker.instruments import ensure_master
    ensure_master()
    path = master or (Path(__file__).resolve().parents[2] / "var" / "cache" / "upstox_instruments_mcx.csv.gz")
    with gzip.open(path, "rt") as fh:
        m = pd.read_csv(fh)
    m["expiry"] = pd.to_datetime(m["expiry"], errors="coerce")
    fams = "|".join(SIGNAL_SYMBOLS + TRADE_SYMBOLS)
    m = m[m["tradingsymbol"].astype(str).str.fullmatch(rf"({fams})\d{{2}}[A-Z]{{3}}FUT") & (m["expiry"] >= pd.Timestamp(date.today()))]
    m["symbol"] = m["tradingsymbol"].str.extract(rf"^({fams})\d", expand=False)
    return m[["symbol", "expiry", "instrument_key", "tradingsymbol"]].reset_index(drop=True)


def rows_frame(rows: list[dict]) -> pd.DataFrame:
    """New Upstox rows as a frame with a float `oi` column (Upstox gives none): an all-None object column makes pd.concat raise pandas'
    FutureWarning about all-NA entries, a behaviour change in a coming pandas (2026-10-29 review)."""
    return pd.DataFrame(rows, columns=COLS).astype({"oi": "float64"})


def topup(broker, today: date | None = None) -> int:
    """Append every active contract's daily candles newer than what is archived. Returns rows added."""
    today = today or date.today()
    df = load()
    if df.empty:
        df = from_bhavcopies()
    up = df[df["source"] == "upstox"] if len(df) else df
    last = up.groupby(["symbol", "expiry"])["date"].max().to_dict() if len(up) else {}
    new = []
    for c in active_contracts().itertuples():
        # a contract seen before continues after its last Upstox day; a new one is backfilled as far as Upstox has it (bhavcopy rows win on overlap)
        start = last[(c.symbol, c.expiry)] + timedelta(days=1) if (c.symbol, c.expiry) in last else pd.Timestamp(today - timedelta(days=400))
        if start.date() > today:
            continue
        candles = broker.get_historical_candles(c.instrument_key, "days", 1, today.isoformat(), start.date().isoformat()) or []
        for k in candles:
            d = pd.Timestamp(str(k["timestamp"])[:10])
            if d >= start:
                new.append({"date": d, "symbol": c.symbol, "expiry": c.expiry, "open": k["open"], "high": k["high"], "low": k["low"],
                            "close": k["close"], "volume": k["volume"], "oi": None, "source": "upstox"})
    if new:
        df = pd.concat([df.astype({"oi": "float64"}), rows_frame(new)], ignore_index=True)
    df = df.drop_duplicates(["date", "symbol", "expiry"], keep="first").sort_values(["date", "symbol", "expiry"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False, date_format="%Y-%m-%d")
    log.info("mcx futures eod: +%d rows, %d total, newest %s", len(new), len(df), df["date"].max().date() if len(df) else None)
    return len(new)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from markets.commodity.data import _get_broker
    n = topup(_get_broker())
    print(f"mcx futures eod: {n} new rows -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
