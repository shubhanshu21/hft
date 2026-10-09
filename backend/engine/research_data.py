"""Research data the strategies lack (added 2026-10-09). Resumable downloads into var/archive, compressed (disk is tight).

    python3 -m engine.research_data equity-1m [--symbols RELIANCE TCS] [--from 2022-01-03]
    python3 -m engine.research_data index                 # Nifty 50, Nifty Bank, India VIX -- 1-minute
    python3 -m engine.research_data index-futures         # NIFTY / BANKNIFTY front-month futures, 1-minute with open interest
    python3 -m engine.research_data participant-oi        # NSE daily open interest and volume by participant (FII / DII / Pro / Client)
    python3 -m engine.research_data nifty-options-oi      # Nifty weekly options within +-3% of the index: 5-minute OHLC + open interest
    python3 -m engine.research_data all                   # everything above (the nightly job runs this; already-held data is skipped)

Why each exists, what it fills: docs/RESEARCH_DATA.md. Upstox calls are paced (UPSTOX_PACE_S, default 1.2 s) to stay well inside the
2,000-per-30-minutes limit the trading daemon shares. Nothing here trades.
"""
from __future__ import annotations

import argparse
import gzip
import io
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from core.paths import ARCHIVE_ROOT, BACKEND_ROOT

COLS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
PACE_S = float(os.environ.get("UPSTOX_PACE_S", "1.2"))
CHUNK_DAYS = 21                                     # 1-minute requests return at most 7,500 candles (~20 trading days)
INDEXES = {"NIFTY50": "NSE_INDEX|Nifty 50", "NIFTYBANK": "NSE_INDEX|Nifty Bank", "INDIAVIX": "NSE_INDEX|India VIX"}
OPTIONS_BAND = 0.03
NSE_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": "https://www.nseindia.com/"}
_last_call = 0.0


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def pace() -> None:
    global _last_call
    wait = PACE_S - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


# ---------------------------------------------------------------------------------------------------------------- storage
def read_gz(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=COLS)
    return pd.read_csv(path)


def write_gz(path: Path, df: pd.DataFrame) -> None:
    """Atomic write of a gzipped CSV, rows unique by timestamp (+contract when present) and sorted."""
    keys = ["timestamp", "contract"] if "contract" in df.columns else ["timestamp"]
    df = df.drop_duplicates(keys, keep="last").sort_values(keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt") as fh:
        df.to_csv(fh, index=False)
    tmp.replace(path)


def candles_to_df(candles) -> pd.DataFrame:
    """Upstox candles (dicts from UpstoxBroker, or [ts, o, h, l, c, v, oi] lists from the SDK) -> DataFrame in COLS order."""
    rows = []
    for c in candles or []:
        if isinstance(c, dict):
            rows.append([c.get("timestamp"), c.get("open"), c.get("high"), c.get("low"), c.get("close"), c.get("volume"), c.get("oi")])
        else:
            rows.append(list(c) + [None] * (7 - len(c)))
    return pd.DataFrame(rows, columns=COLS)


def chunks(start: date, end: date, days: int = CHUNK_DAYS):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


# ---------------------------------------------------------------------------------------------------------------- Upstox clients
def broker():
    from services.auth.upstox_auto_login import ensure_fresh_upstox_token
    from services.broker.upstox_broker import UpstoxBroker
    return UpstoxBroker(access_token=ensure_fresh_upstox_token() or "", dry_run=True)


def expired_api():
    import upstox_client
    from services.auth.upstox_auto_login import ensure_fresh_upstox_token
    cfg = upstox_client.Configuration()
    cfg.access_token = ensure_fresh_upstox_token() or ""
    return upstox_client.ExpiredInstrumentApi(upstox_client.ApiClient(cfg))


def minute_series(b, key: str, path: Path, start: date, end: date, label: str) -> None:
    """Fill `path` (gz) with 1-minute candles for [start, end]: only the date ranges before the first / after the last held candle."""
    have = read_gz(path)
    old_plain = path.with_name(path.name[:-3]) if path.name.endswith(".gz") else None
    if old_plain is not None and old_plain.exists():                 # fold an older uncompressed archive in, then drop it below
        have = pd.concat([have, pd.read_csv(old_plain).reindex(columns=COLS)])
    todo = []
    if have.empty:
        todo = list(chunks(start, end))
    else:
        first = pd.to_datetime(have["timestamp"].min()).date()
        last = pd.to_datetime(have["timestamp"].max()).date()
        if start < first:
            todo += list(chunks(start, first - timedelta(days=1)))
        if last < end:
            todo += list(chunks(last, end))                           # re-fetch the last held day: it may have been partial
    frames = [have]
    for a, z in todo:
        pace()
        frames.append(candles_to_df(b.get_historical_candles(key, unit="minutes", interval=1, to_date=z.isoformat(), from_date=a.isoformat())))
    out = pd.concat(frames)
    if out.empty:
        log(f"{label}: no data")
        return
    write_gz(path, out)
    if old_plain is not None and old_plain.exists():
        old_plain.unlink()
    log(f"{label}: {len(out.drop_duplicates('timestamp')):,} candles {out['timestamp'].min()[:10]}..{out['timestamp'].max()[:10]} ({len(todo)} requests)")


# ---------------------------------------------------------------------------------------------------------------- jobs
def job_equity_1m(symbols=None, start="2022-01-03") -> None:
    from markets.equity.universe import NIFTY50_SYMBOLS
    from services.broker.instruments import get_instrument_key
    b = broker()
    end = date.today()
    for s in symbols or NIFTY50_SYMBOLS:
        key = get_instrument_key(s)
        if not key:
            log(f"{s}: no instrument key")
            continue
        minute_series(b, key, ARCHIVE_ROOT / "equity" / f"{s}_1minute.csv.gz", date.fromisoformat(start), end, s)


def job_index(start="2022-01-03") -> None:
    b = broker()
    for name, key in INDEXES.items():
        minute_series(b, key, ARCHIVE_ROOT / "index" / f"{name}_1minute.csv.gz", date.fromisoformat(start), date.today(), name)


def job_index_futures() -> None:
    """Front-month futures stitched from expired contracts (each contract's last month: when it is the front month), plus the live one."""
    api = expired_api()
    b = broker()
    from services.broker.instruments import build_index_futures_map
    live = build_index_futures_map()
    for name, underlying in (("NIFTY", "NSE_INDEX|Nifty 50"), ("BANKNIFTY", "NSE_INDEX|Nifty Bank")):
        path = ARCHIVE_ROOT / "index_futures" / f"{name}_FUT_1minute_oi.csv.gz"
        have = read_gz(path)
        done = set(have["contract"].unique()) if "contract" in have.columns else set()
        pace()
        expiries = sorted(api.get_expiries(underlying).data or [])
        frames, prev = [have], None
        for e in expiries:
            if e >= date.today().isoformat():
                break
            pace()
            futs = api.get_expired_future_contracts(underlying, e).data or []
            if not futs:
                continue                                              # a weekly (options-only) expiry
            f = futs[0]
            if f.trading_symbol in done:
                prev = e
                continue
            frm = (date.fromisoformat(prev) + timedelta(days=1)).isoformat() if prev else (date.fromisoformat(e) - timedelta(days=31)).isoformat()
            pace()
            r = api.get_expired_historical_candle_data(f.instrument_key, "1minute", e, frm)
            df = candles_to_df(r.data.candles if r.data else [])
            df["contract"] = f.trading_symbol
            frames.append(df)
            prev = e
            log(f"{name} {f.trading_symbol}: {len(df)} candles {frm}..{e}")
        key = live.get(name)
        if key and prev:
            pace()
            c = b.get_historical_candles(key, unit="minutes", interval=1, to_date=date.today().isoformat(),
                                         from_date=(date.fromisoformat(prev) + timedelta(days=1)).isoformat())
            df = candles_to_df(c)
            df["contract"] = "LIVE:" + key
            frames.append(df)
        out = pd.concat(frames)
        if not out.empty:
            write_gz(path, out)
            log(f"{name} futures: {len(out):,} candles in {path.name}")


def job_participant_oi(start="2020-01-01") -> None:
    """NSE daily files: participant-wise open interest and trading volume (Client / DII / FII / Pro) in equity derivatives."""
    import requests
    s = requests.Session()
    for kind in ("oi", "vol"):
        root = ARCHIVE_ROOT / "nse" / f"participant_{kind}"
        root.mkdir(parents=True, exist_ok=True)
        d, end, got = date.fromisoformat(start), date.today(), 0
        while d <= end:
            out = root / f"{d}.csv"
            if d.weekday() < 5 and not out.exists() and not (root / f"{d}.none").exists():
                url = f"https://archives.nseindia.com/content/nsccl/fao_participant_{kind}_{d:%d%m%Y}.csv"
                try:
                    r = s.get(url, headers=NSE_HEADERS, timeout=20)
                    if r.status_code == 200 and "Client" in r.text:
                        out.write_text(r.text)
                        got += 1
                    elif r.status_code == 404 and d < end - timedelta(days=3):
                        (root / f"{d}.none").write_text("")           # a holiday: remember, do not ask again
                except requests.RequestException:
                    pass
                time.sleep(0.4)
            d += timedelta(days=1)
        log(f"participant {kind}: +{got} days ({len(list(root.glob('*.csv')))} held)")


def job_nifty_options_oi() -> None:
    """Per weekly expiry: every strike within +-3% of the Nifty close on the day the expiry became the front week, CE and PE,
    1-minute candles (each expired contract's last month) -> 5-minute OHLC + volume + open interest, one gz per expiry."""
    api = expired_api()
    spot = read_gz(ARCHIVE_ROOT / "index" / "NIFTY50_1minute.csv.gz")
    if spot.empty:
        log("nifty-options-oi needs the index job first (NIFTY50_1minute)")
        return
    spot["day"] = spot["timestamp"].str[:10]
    day_close = spot.groupby("day")["close"].last()
    root = ARCHIVE_ROOT / "options" / "NIFTY"
    pace()
    expiries = sorted(api.get_expiries("NSE_INDEX|Nifty 50").data or [])
    prev = None
    for e in expiries:
        if e >= date.today().isoformat():
            break
        out = root / f"{e}.csv.gz"
        if out.exists():
            prev = e
            continue
        ref_day = prev or (date.fromisoformat(e) - timedelta(days=7)).isoformat()
        ref = day_close[day_close.index <= ref_day]
        if ref.empty:
            prev = e
            continue
        s0 = float(ref.iloc[-1])
        pace()
        contracts = api.get_expired_option_contracts("NSE_INDEX|Nifty 50", e).data or []
        pick = [c for c in contracts if abs(c.strike_price / s0 - 1) <= OPTIONS_BAND]
        frm = (date.fromisoformat(ref_day) - timedelta(days=1)).isoformat()
        frames = []
        for c in pick:
            pace()
            try:
                r = api.get_expired_historical_candle_data(c.instrument_key, "1minute", e, frm)
            except Exception as ex:
                log(f"{e} {c.trading_symbol}: {str(ex)[:80]}")
                continue
            df = candles_to_df(r.data.candles if r.data else [])
            if df.empty:
                continue
            df["ts"] = pd.to_datetime(df["timestamp"])
            g = df.set_index("ts").sort_index().resample("5min", label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"}).dropna(subset=["close"])
            g["strike"], g["type"] = c.strike_price, c.instrument_type
            g.index = g.index.strftime("%Y-%m-%dT%H:%M:%S%z")
            frames.append(g.reset_index().rename(columns={"ts": "timestamp"}))
        if frames:
            out.parent.mkdir(parents=True, exist_ok=True)
            pd.concat(frames).to_csv(out, index=False, compression="gzip")
            log(f"options {e}: {len(pick)} contracts around {s0:.0f}")
        prev = e


JOBS = {"equity-1m": job_equity_1m, "index": job_index, "index-futures": job_index_futures,
        "participant-oi": job_participant_oi, "nifty-options-oi": job_nifty_options_oi}


def main(argv=None) -> int:
    from dotenv import load_dotenv
    load_dotenv(BACKEND_ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=list(JOBS) + ["all"])
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--from", dest="start")
    a = ap.parse_args(argv)
    order = ["index", "participant-oi", "index-futures", "equity-1m", "nifty-options-oi"] if a.job == "all" else [a.job]
    failed = []
    for name in order:
        try:
            if name == "equity-1m":
                job_equity_1m(a.symbols, a.start or "2022-01-03")
            elif name in ("index", "participant-oi") and a.start:
                JOBS[name](a.start)
            else:
                JOBS[name]()
        except Exception as ex:                                       # one source failing must not stop the others
            log(f"{name} FAILED: {ex}")
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
