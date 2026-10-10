"""Research data the strategies lack (added 2026-10-09). Resumable downloads into var/archive, compressed (disk is tight).

    python3 -m engine.research_data equity-1m [--symbols RELIANCE TCS] [--from 2022-01-03]
    python3 -m engine.research_data index                 # Nifty 50, Nifty Bank, India VIX -- 1-minute
    python3 -m engine.research_data index-futures         # NIFTY / BANKNIFTY front-month futures, 1-minute with open interest
    python3 -m engine.research_data participant-oi        # NSE daily open interest and volume by participant (FII / DII / Pro / Client)
    python3 -m engine.research_data nifty-options-oi      # Nifty weekly options within +-3% of the index: 5-minute OHLC + open interest
    python3 -m engine.research_data fii-dii-cash          # FII / DII cash-market buy-sell (NSE serves only the latest day: daily)
    python3 -m engine.research_data bulk-block            # bulk and block deals (latest day only: daily)
    python3 -m engine.research_data delivery              # delivery quantity and % per NIFTY50 stock, NSE bhavcopy, 2020 onward
    python3 -m engine.research_data events                # board meetings / results dates for NIFTY50, 2022 onward + next 60 days
    python3 -m engine.research_data global-1m             # US futures, dollar index, crude, metals, US 10y, USD/INR, VIX (Yahoo keeps 7 days: daily)
    python3 -m engine.research_data all                   # everything above; already-held data is skipped
    python3 -m engine.research_data all --max-minutes 40  # the nightly job: stops in time, saves progress, finishes on later nights

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
_deadline: float | None = None                     # --max-minutes: stop starting new work after this (progress is saved per unit)


def out_of_time(what: str = "") -> bool:
    if _deadline is not None and time.time() >= _deadline:
        log(f"time budget reached{' before ' + what if what else ''} -- the rest continues on the next run")
        return True
    return False


def concat(frames) -> pd.DataFrame:
    """pd.concat without the empty pieces (pandas warns about them, and they add nothing)."""
    frames = [f for f in frames if f is not None and not f.empty]
    return pd.concat(frames) if frames else pd.DataFrame(columns=COLS)


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
        have = concat([have, pd.read_csv(old_plain).reindex(columns=COLS)])
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
    out = concat(frames)
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
        if out_of_time(s):
            return
        key = get_instrument_key(s)
        if not key:
            log(f"{s}: no instrument key")
            continue
        minute_series(b, key, ARCHIVE_ROOT / "equity" / f"{s}_1minute.csv.gz", date.fromisoformat(start), end, s)


def job_index(start="2022-01-03") -> None:
    b = broker()
    for name, key in INDEXES.items():
        if out_of_time(name):
            return
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
            if e >= date.today().isoformat() or out_of_time(f"{name} futures {e}"):
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
        out = concat(frames)
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
            if out_of_time(f"participant {kind} {d}"):
                break
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
        if not out.exists() and out_of_time(f"options {e}"):
            break
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
            concat(frames).to_csv(out, index=False, compression="gzip")
            log(f"options {e}: {len(pick)} contracts around {s0:.0f}")
        prev = e


# ---------------------------------------------------------------------------------------------------------------- NSE flows, events, global
NSE_API_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                   "Accept": "application/json,text/html,*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"}
GLOBAL_1M = {"ES=F": "SP500_FUT", "YM=F": "DOW_FUT", "NQ=F": "NASDAQ_FUT", "DX-Y.NYB": "DXY", "CL=F": "WTI", "BZ=F": "BRENT", "GC=F": "GOLD",
             "SI=F": "SILVER", "HG=F": "COPPER", "^TNX": "US10Y", "INR=X": "USDINR_SPOT", "^VIX": "US_VIX"}


def append_unique(path: Path, df: pd.DataFrame, keys: list[str]) -> int:
    """Append rows to a small plain CSV, unique by `keys`; returns how many rows were new."""
    if df.empty:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame(columns=df.columns)
    out = concat([old, df.astype(str)]).drop_duplicates(keys, keep="last")
    out.to_csv(path, index=False)
    return len(out) - len(old)


def nse_get(url: str):
    import requests
    time.sleep(0.5)
    return requests.get(url, headers=NSE_API_HEADERS, timeout=25)


def job_fii_dii_cash() -> None:
    """FII/FPI and DII cash-market buy / sell / net (Rs crore), NSE provisional figures. NSE serves only the latest day: collect daily."""
    r = nse_get("https://www.nseindia.com/api/fiidiiTradeReact")
    df = pd.DataFrame(r.json())
    n = append_unique(ARCHIVE_ROOT / "nse" / "fii_dii_cash.csv", df, ["date", "category"])
    log(f"fii/dii cash: +{n} rows ({', '.join(sorted(df['date'].unique())) if not df.empty else 'none'})")


def parse_deals_csv(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return df[df["Symbol"].notna() & (df["Symbol"].str.strip() != "")] if "Symbol" in df.columns else df.iloc[0:0]


def job_bulk_block() -> None:
    """Bulk and block deals (large named trades). NSE's archive keeps only the latest day; its history pages refuse servers: collect daily."""
    for kind in ("bulk", "block"):
        r = nse_get(f"https://archives.nseindia.com/content/equities/{kind}.csv")
        df = parse_deals_csv(r.text) if r.status_code == 200 else pd.DataFrame()
        n = append_unique(ARCHIVE_ROOT / "nse" / f"{kind}_deals.csv", df, list(df.columns)) if not df.empty else 0
        log(f"{kind} deals: +{n} rows")


def parse_bhavcopy(text: str, symbols: set[str]) -> pd.DataFrame:
    """NSE sec_bhavdata_full: keep EQ-series rows of `symbols`; column names and values come padded with spaces."""
    df = pd.read_csv(io.StringIO(text), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df.apply(lambda c: c.str.strip())
    return df[(df["SERIES"] == "EQ") & df["SYMBOL"].isin(symbols)]


def job_delivery(start="2020-01-01") -> None:
    """Daily delivery quantity and % per NIFTY50 stock (real accumulation vs intraday churn), from NSE's full bhavcopy."""
    import requests
    from markets.equity.universe import NIFTY50_SYMBOLS
    syms = set(NIFTY50_SYMBOLS)
    root = ARCHIVE_ROOT / "nse" / "delivery"
    root.mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    d, end, got = date.fromisoformat(start), date.today(), 0
    while d <= end:
        if out_of_time(f"delivery {d}"):
            break
        out = root / f"{d}.csv"
        if d.weekday() < 5 and not out.exists() and not (root / f"{d}.none").exists():
            try:
                r = s.get(f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv", headers=NSE_API_HEADERS, timeout=30)
                if r.status_code == 200 and "SYMBOL" in r.text[:200]:
                    parse_bhavcopy(r.text, syms).to_csv(out, index=False)
                    got += 1
                elif r.status_code == 404 and d < end - timedelta(days=3):
                    (root / f"{d}.none").write_text("")
            except Exception:
                pass
            time.sleep(0.4)
        d += timedelta(days=1)
    log(f"delivery: +{got} days ({len(list(root.glob('*.csv')))} held)")


def job_events(start="2022-01-01") -> None:
    """Board meetings (results dates) for NIFTY50 stocks, month by month from `start`, plus NSE's forward event calendar (60 days)."""
    from markets.equity.universe import NIFTY50_SYMBOLS
    syms = set(NIFTY50_SYMBOLS)
    root = ARCHIVE_ROOT / "nse"
    done_file = root / "board_meetings_months.txt"
    done = set(done_file.read_text().split()) if done_file.exists() else set()
    m, today = date.fromisoformat(start).replace(day=1), date.today()
    frames = []
    while m <= today:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        key = f"{m:%Y-%m}"
        if key not in done or nxt > today - timedelta(days=31):               # past months once; the latest two again
            if out_of_time(f"board meetings {key}"):
                break
            r = nse_get(f"https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date={m:%d-%m-%Y}"
                        f"&to_date={(nxt - timedelta(days=1)):%d-%m-%Y}")
            if r.status_code == 200:
                df = pd.DataFrame(r.json())
                if not df.empty and "bm_symbol" in df.columns:
                    frames.append(df[df["bm_symbol"].isin(syms)])
                done.add(key)
        m = nxt
    if frames:
        n = append_unique(root / "board_meetings.csv", concat(frames), ["bm_symbol", "bm_date", "bm_purpose"])
        log(f"board meetings: +{n} rows")
    done_file.write_text("\n".join(sorted(done)))
    r = nse_get(f"https://www.nseindia.com/api/event-calendar?index=equities&from_date={today:%d-%m-%Y}"
                f"&to_date={(today + timedelta(days=60)):%d-%m-%Y}")
    if r.status_code == 200:
        df = pd.DataFrame(r.json())
        if not df.empty and "symbol" in df.columns:
            n = append_unique(root / "event_calendar.csv", df[df["symbol"].isin(syms)], ["symbol", "date", "purpose"])
            log(f"event calendar: +{n} rows")


def yahoo_1m(ticker: str) -> pd.DataFrame:
    """The last 7 days of 1-minute bars from Yahoo (all it serves at this resolution), IST timestamps, in COLS order."""
    import requests
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}", params={"interval": "1m", "range": "7d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    ts = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata").strftime("%Y-%m-%dT%H:%M:%S%z")
    df = pd.DataFrame({"timestamp": ts, "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"], "volume": q["volume"], "oi": None})
    df["timestamp"] = df["timestamp"].str.replace(r"(\d\d)(\d\d)$", r"\1:\2", regex=True)
    return df.dropna(subset=["close"])


def job_global_1m() -> None:
    """US index futures, dollar index, crude, metals, US 10-year, USD/INR spot and US VIX at 1 minute: Yahoo keeps only 7 days, so collect daily."""
    for ticker, name in GLOBAL_1M.items():
        if out_of_time(name):
            return
        try:
            time.sleep(0.5)
            new = yahoo_1m(ticker)
        except Exception as e:
            log(f"{name}: {str(e)[:80]}")
            continue
        path = ARCHIVE_ROOT / "global_1m" / f"{name}_1minute.csv.gz"
        out = concat([read_gz(path), new])
        write_gz(path, out)
        log(f"{name}: {len(new)} new-window bars, {len(out.drop_duplicates('timestamp')):,} held")


JOBS = {"equity-1m": job_equity_1m, "index": job_index, "index-futures": job_index_futures,
        "participant-oi": job_participant_oi, "nifty-options-oi": job_nifty_options_oi, "fii-dii-cash": job_fii_dii_cash,
        "bulk-block": job_bulk_block, "delivery": job_delivery, "events": job_events, "global-1m": job_global_1m}


def main(argv=None) -> int:
    from dotenv import load_dotenv
    load_dotenv(BACKEND_ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=list(JOBS) + ["all"])
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--max-minutes", type=float, default=None, help="stop starting new work after this long and exit 0 (the nightly job's limit)")
    a = ap.parse_args(argv)
    global _deadline
    _deadline = time.time() + a.max_minutes * 60 if a.max_minutes else None
    order = (["fii-dii-cash", "bulk-block", "global-1m", "index", "participant-oi", "events", "delivery", "index-futures", "equity-1m",
              "nifty-options-oi"] if a.job == "all" else [a.job])     # the collect-daily-or-lose-it sources first
    failed = []
    for name in order:
        if out_of_time(name):
            break
        try:
            if name == "equity-1m":
                job_equity_1m(a.symbols, a.start or "2022-01-03")
            elif name in ("index", "participant-oi", "delivery", "events") and a.start:
                JOBS[name](a.start)
            else:
                JOBS[name]()
        except Exception as ex:                                       # one source failing must not stop the others
            log(f"{name} FAILED: {ex}")
            failed.append(name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
