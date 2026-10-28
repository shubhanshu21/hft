"""equity / results_gap -- buy a stock that opens 7%+ above the market the morning after its quarterly results and keeps rising in the
first 15 minutes; sell the same day at 15:20. Intraday (MIS), long only, paper trading.

Found 2026-10-24 (scratchpad fast/events.py, gapbt3.py) on 5-minute data for the 414 stocks in var/archive/equity, Sep 2022 -> Oct 2026,
with NSE board-meeting results dates:
  * results on the meeting day D or the next trading day; gap = 09:15 open / previous close - 1, minus the median stock's gap that morning;
  * gap vs market >= 7% AND price at 09:30 above the 09:15 open -> buy at 09:30, sell at the 15:20 close;
  * real Upstox intraday costs + 10 bp slippage a side; capital split equally across the same morning's signals; 3x notional (MIS allows 5x).
Result (frozen-at-09:30 / too-thin trades removed): Rs1L -> Rs11.6L at 3x, every year positive (+5/+41/+111/+198/+24% 2022-26),
worst fall -22%; 2x Rs5.4L (-15%), 1x Rs2.4L (-8%); gaps of 8/9/10% also positive in both 2022-24 and 2025-26; 47 trades, 66% won.
Caveats: few trades (~15-20 a year); a handful of big days carry much of the profit (without the best 5: 3.9x); shorting gap-downs,
smaller gaps and stops all failed. The user chose 3x (2026-10-24, "add 3x"), 5x for a day (2026-10-29), then 3x again.
2026-10-29 FAIRNESS FIX: the 414 are today's liquid names (hindsight); the rule lost -1.0%/trade on 130 results-day gap-ups outside them.
It now trades every stock liquid AT THE TIME (>= Rs10 cr median turnover over the 20 days before; liquid_days.py / liquid_list): fair
backtest 3x Rs1L -> Rs1.91L 2022-24 (19 trades), Rs2.27L 2025-26 (40), Rs4.40L Sep 2022 -> Oct 2026 (59, 36 won), worst fall -25%;
5x 2.47L / 2.99L with -43%. Edge +0.8-1.4%/trade, not yet statistically proven (t ~1.3). The engine caps each stock at Upstox's MIS leverage.
"""
from __future__ import annotations

import glob
import os
from datetime import date, timedelta

import pandas as pd

from core.paths import ARCHIVE_ROOT

GAP_MIN = 0.07                 # gap vs the market's median gap
LEVERAGE = float(os.environ.get("RESULTS_GAP_LEVERAGE", 3.0))
SLIP = 0.0010                  # 10 bp a side (the backtest's conservative setting)
MIN_PRICE = 20.0
CALENDAR = ARCHIVE_ROOT / "nse" / "results_calendar.csv"
UNIVERSE_DIR = ARCHIVE_ROOT / "equity"
BHAV_DIR = ARCHIVE_ROOT / "nse" / "bhav_eq"     # NSE's daily full bhavcopy, EQ series only (symbol, close, turnover), one file per day
LIQUID_MIN = 10e7              # Rs10 crore median daily turnover ...
LIQUID_DAYS = 20               # ... over the 20 trading days before the day (point in time: no hindsight)


def market_panel() -> list[str]:
    """The 414 stocks with 5-minute history. The market's gap each morning is their median (as in every backtest); until 2026-10-29
    they were also the only stocks traded."""
    return sorted(os.path.basename(p).replace("_5minute.csv.gz", "") for p in glob.glob(str(UNIVERSE_DIR / "*_5minute.csv.gz")))


def parse_bhav(text: str) -> pd.DataFrame:
    """NSE sec_bhavdata_full -> EQ rows: SYMBOL, CLOSE, TURNOVER (rupees). Anything that is not a bhavcopy gives an empty frame."""
    import io
    try:
        df = pd.read_csv(io.StringIO(text), skipinitialspace=True)
        df.columns = [c.strip() for c in df.columns]
        df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
        return pd.DataFrame({"SYMBOL": df["SYMBOL"].astype(str).str.strip(), "CLOSE": pd.to_numeric(df["CLOSE_PRICE"], errors="coerce"),
                             "TURNOVER": pd.to_numeric(df["TURNOVER_LACS"], errors="coerce") * 1e5}).dropna().reset_index(drop=True)
    except (ValueError, KeyError, pd.errors.ParserError):
        return pd.DataFrame(columns=["SYMBOL", "CLOSE", "TURNOVER"])


def refresh_bhavcopy(today: date | None = None, get=None, days: int = 35, root=None) -> int:
    """Download the bhavcopies of the `days` calendar days before `today` that are not held yet (weekdays; a holiday 404s and is
    remembered as <day>.none once it is a few days old). Returns the number of new files."""
    import requests
    from engine.research_data import NSE_API_HEADERS
    root = root or BHAV_DIR
    root.mkdir(parents=True, exist_ok=True)
    get = get or (lambda url: requests.get(url, headers=NSE_API_HEADERS, timeout=30))
    today = today or date.today()
    new = 0
    for k in range(days, 0, -1):
        d = today - timedelta(days=k)
        if d.weekday() >= 5 or (root / f"{d}.csv.gz").exists() or (root / f"{d}.none").exists():
            continue
        try:
            r = get(f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv")
        except Exception:
            continue
        df = parse_bhav(r.text) if r.status_code == 200 else pd.DataFrame()
        if len(df):
            df.to_csv(root / f"{d}.csv.gz", index=False)
            new += 1
        elif r.status_code == 404 and (today - d).days > 4:
            (root / f"{d}.none").write_text("")
    return new


def liquid_list(asof: date, root=None) -> list[str]:
    """EQ stocks whose median daily turnover over the LIQUID_DAYS trading days before `asof` is >= LIQUID_MIN (a day a stock did not
    trade counts as zero) and whose last close is >= MIN_PRICE. Empty when fewer than LIQUID_DAYS days of files are held."""
    root = root or BHAV_DIR
    files = sorted(p for p in root.glob("*.csv.gz") if p.name[:10] < asof.isoformat())[-LIQUID_DAYS:]
    if len(files) < LIQUID_DAYS:
        return []
    frames = [pd.read_csv(p).assign(day=p.name[:10]) for p in files]
    x = pd.concat(frames)
    turn = x.pivot_table(index="SYMBOL", columns="day", values="TURNOVER", aggfunc="sum").fillna(0.0)
    last = x.sort_values("day").groupby("SYMBOL")["CLOSE"].last()
    ok = turn.median(axis=1) >= LIQUID_MIN
    return sorted(s for s in turn.index[ok] if last.get(s, 0) >= MIN_PRICE)


def universe(asof: date | None = None) -> list[str]:
    """Stocks results_gap may buy (2026-10-29): every liquid NSE stock at the time (see liquid_list). The fixed 414-stock list was
    hindsight -- the same rule lost money on results-day gap-ups outside it -- so it is only the fallback when the bhavcopies are missing."""
    return liquid_list(asof or date.today()) or market_panel()


def costs(entry: float, exit_: float, qty: int) -> dict:
    """Upstox intraday equity charges for one round trip (Upstox Plus: Rs30 an order, see user_upstox_plus memory) + slippage."""
    buy, sell = entry * qty, exit_ * qty
    brokerage = min(30.0, 0.0005 * buy) + min(30.0, 0.0005 * sell)
    stt = 0.00025 * sell
    exch = 0.0000297 * (buy + sell)
    sebi = 0.000001 * (buy + sell)
    stamp = 0.00003 * buy
    gst = 0.18 * (brokerage + exch + sebi)
    slip = SLIP * (buy + sell)
    total = brokerage + stt + exch + sebi + stamp + gst + slip
    return {"brokerage": round(brokerage, 2), "stt": round(stt, 2), "exchange_txn": round(exch, 2), "sebi": round(sebi, 2),
            "stamp_duty": round(stamp, 2), "gst": round(gst, 2), "slippage": round(slip, 2), "total": round(total, 2)}


def refresh_calendar(today: date | None = None, get=None) -> int:
    """Download NSE's results calendar (all equities) for the last 7 and next 7 days into results_calendar.csv. Returns rows saved."""
    if get is None:
        from engine.research_data import nse_get as get
    today = today or date.today()
    a, b = today - timedelta(days=7), today + timedelta(days=7)
    r = get(f"https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}")
    df = pd.DataFrame(r.json() if r.status_code == 200 else [])
    if df.empty or "bm_symbol" not in df.columns:
        return 0
    df = df[df["bm_purpose"].str.contains("Financial Result", case=False, na=False)][["bm_symbol", "bm_date", "bm_purpose"]]
    CALENDAR.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CALENDAR, index=False)
    return len(df)


def candidates(today: date, prev_trading_day: date, calendar: pd.DataFrame | None = None) -> list[str]:
    """Stocks in today's universe whose results meeting fell on the previous trading day or any day since (incl. today)."""
    if calendar is None:
        calendar = pd.read_csv(CALENDAR) if CALENDAR.exists() else pd.DataFrame(columns=["bm_symbol", "bm_date"])
    d = pd.to_datetime(calendar["bm_date"], format="%d-%b-%Y", errors="coerce").dt.date
    hit = calendar[(d >= prev_trading_day) & (d <= today)]["bm_symbol"]
    uni = set(universe(today))
    return sorted({s for s in hit if s in uni})


def signals(quotes: dict, cands: list[str], panel: set[str] | None = None) -> tuple[list[dict], float | None]:
    """quotes: {symbol: {"ltp", "prev_close", "today_open", "today_high", "today_low"}}; the market gap is the median over `panel`
    (market_panel(), as in the backtests) or over every quote when no panel is given. Returns (buys, market_gap): a candidate buys when its gap vs the market >= GAP_MIN, the price now (09:30) is above the
    09:15 open, and it is not frozen (high == low, e.g. locked at a price band)."""
    gaps = [q["today_open"] / q["prev_close"] - 1 for s, q in quotes.items()
            if (panel is None or s in panel) and q and q.get("today_open") and q.get("prev_close") and abs(q["today_open"] / q["prev_close"] - 1) < 0.25]
    if len(gaps) < 20:
        return [], None
    mkt = float(pd.Series(gaps).median())
    out = []
    for s in cands:
        q = quotes.get(s)
        if not q or not q.get("today_open") or not q.get("prev_close") or not q.get("ltp") or q["ltp"] < MIN_PRICE:
            continue
        xgap = q["today_open"] / q["prev_close"] - 1 - mkt
        frozen = q.get("today_high") is not None and q.get("today_low") is not None and q["today_high"] == q["today_low"]
        if xgap >= GAP_MIN and q["ltp"] > q["today_open"] and not frozen:
            out.append({"symbol": s, "gap_vs_mkt": round(xgap, 4), "open": q["today_open"], "ltp": q["ltp"], "prev_close": q["prev_close"]})
    return out, mkt


# ---------------------------------------------------------------------------------------------------------------------------------------
# Website backtest (2026-10-24): the same rules on the 5-minute archive, summarised once per stock-day into a cache so a run takes seconds.
# ---------------------------------------------------------------------------------------------------------------------------------------
MIN_0930_VALUE = 50e5          # Rs50 lakh traded in the 09:30 5-minute bar, else too thin to fill the order (the research dropped SHAILY)


def _cache_path():
    from core.paths import CACHE_DIR
    return CACHE_DIR / "results_gap_days.pkl"


def _summarise(path: str) -> pd.DataFrame | None:
    sym = os.path.basename(path).replace("_5minute.csv.gz", "")
    try:
        d = pd.read_csv(path, usecols=["timestamp", "open", "high", "low", "close", "volume"])
    except Exception:
        return None
    d["day"] = d["timestamp"].str[:10]; d["hm"] = d["timestamp"].str[11:16]
    rows = []
    for day, g in d.groupby("day", sort=True):
        g = g.set_index("hm")
        if not {"09:15", "09:25", "09:30", "15:20"} <= set(g.index):
            continue
        b = g.loc["09:30"]
        rows.append((sym, day, float(g.at["09:15", "open"]), float(g.at["09:25", "close"]), float(b["open"]), float(b["high"]), float(b["low"]),
                     float(b["open"] * b["volume"]), float(g.at["15:20", "close"]), float(g["close"].iloc[-1]), float(g["high"].max()), float(g["low"].min())))
    x = pd.DataFrame(rows, columns=["sym", "day", "o915", "c925", "o930", "h930", "l930", "v930", "c1520", "close", "hi", "lo"])
    x["prev_close"] = x["close"].shift(1)
    return x


def day_summary(rebuild: bool = False) -> pd.DataFrame:
    """One row per stock-day (cached in var/cache; rebuilt when the 5-minute archive is newer than the cache)."""
    cache = _cache_path()
    files = sorted(glob.glob(str(UNIVERSE_DIR / "*_5minute.csv.gz")))
    newest = max((os.path.getmtime(f) for f in files), default=0)
    if not rebuild and cache.exists() and cache.stat().st_mtime >= newest:
        return pd.read_pickle(cache)
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=4) as ex:
        parts = [p for p in ex.map(_summarise, files, chunksize=4) if p is not None and len(p)]
    df = pd.concat(parts, ignore_index=True)
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(cache)
    return df


def _results_days() -> set[tuple[str, str]]:
    """(symbol, day) pairs where results were out: the meeting day and the next trading day."""
    frames = []
    for p in (ARCHIVE_ROOT / "nse" / "board_meetings_all.csv.gz", CALENDAR):
        if p.exists():
            frames.append(pd.read_csv(p, usecols=["bm_symbol", "bm_date", "bm_purpose"]))
    if not frames:
        return set()
    bm = pd.concat(frames)
    bm = bm[bm["bm_purpose"].str.contains("Financial Result", case=False, na=False)]
    return {(s, d.strftime("%Y-%m-%d")) for s, d in zip(bm["bm_symbol"], pd.to_datetime(bm["bm_date"], format="%d-%b-%Y", errors="coerce")) if not pd.isna(d)}


def backtest(capital: float = 100_000, start: str = "2022-09-01", end: str = "2100-01-01", leverage: float = LEVERAGE, progress=None,
             days_df: pd.DataFrame | None = None, results: set | None = None, universe: str = "liquid",
             liquid_rows: pd.DataFrame | None = None) -> dict:
    """The website's replay shape: {days, equity, events, pos, bars, metrics, trades}. Capital split across each morning's signals,
    notional = leverage x capital, compounding.
    universe="liquid" (default, 2026-10-29): signals from every stock that was liquid at the time (liquid_days.py: >= LIQUID_MIN median
    turnover over the 20 days before), as the live engine trades now; "list": only the 414 with 5-minute history (hindsight -- kept to
    compare). Liquid mode needs the saved table; without it (or in unit tests with a hand-made days_df) it falls back to the list."""
    from core.backtester import metrics
    x = (days_df if days_df is not None else day_summary()).copy()
    x = x[x["prev_close"].notna() & (x["o915"] > 0)]
    x["gap"] = x["o915"] / x["prev_close"] - 1
    x = x[x["gap"].abs() < 0.25]
    x["xgap"] = x["gap"] - x.groupby("day")["gap"].transform("median")
    all_days = sorted(x["day"].unique())
    res = results if results is not None else _results_days()
    nxt = {d: all_days[i + 1] for i, d in enumerate(all_days[:-1])}
    hit = set(res) | {(s, nxt[d]) for s, d in res if d in nxt}
    days = [d for d in all_days if start <= d <= end]
    if not days:
        raise ValueError("No trading days with 5-minute data in the chosen dates (the archive starts 2022-09-01)")
    sig = x[x["day"].isin(set(days)) & (x["xgap"] >= GAP_MIN) & (x["c925"] > x["o915"]) & (x["h930"] > x["l930"])
            & (x["v930"] >= MIN_0930_VALUE) & (x["o930"] >= MIN_PRICE)]
    sig = sig[[(s, d) in hit for s, d in zip(sig["sym"], sig["day"])]]
    if universe == "liquid" and liquid_rows is None and days_df is None:
        from markets.equity.strategies.tf_intraday.results_gap import liquid_days
        liquid_rows = liquid_days.load()
    liquid = universe == "liquid" and liquid_rows is not None
    if liquid:                                    # results-day rows were selected when the table was built; same rule, plus liquidity
        L = liquid_rows
        sig = L[L["day"].isin(set(days)) & (L["xgap"] >= GAP_MIN) & (L["c925"] > L["o915"]) & (L["h930"] > L["l930"])
                & (L["v930"] >= MIN_0930_VALUE) & (L["o930"] >= MIN_PRICE) & (L["turn20"].fillna(0) >= LIQUID_MIN)]
    by_day = {d: g for d, g in sig.groupby("day")}
    cap, equity, events, closed, trades = float(capital), [], [], [], []
    for k, d in enumerate(days):
        if progress is not None and k % max(1, len(days) // 100) == 0:
            progress(k, len(days))
        ev = []
        g = by_day.get(d)
        if g is not None:
            per = cap * leverage / len(g)
            pnl = 0.0
            for _, r in g.iterrows():
                qty = int(per // r["o930"])
                if qty < 1:
                    continue
                c = costs(r["o930"], r["c1520"], qty)
                gross = (r["c1520"] - r["o930"]) * qty
                net = gross - c["total"]
                pnl += net
                ev.append({"t": "buy", "side": "long", "sym": r["sym"], "q": qty, "px": round(r["o930"], 2), "entry": round(r["o930"], 2),
                           "note": f"BUY {r['sym']} at 09:30: opened {r['xgap']:+.1%} vs the market after results, still rising"})
                ev.append({"t": "sell", "side": "long", "sym": r["sym"], "q": qty, "px": round(r["c1520"], 2), "entry": round(r["o930"], 2),
                           "pnl": round(net), "fees": round(c["total"] - c["slippage"]), "slip": round(c["slippage"]), "hours": 5.8, "why": "end_of_day"})
                closed.append({"net": net, "fees": c["total"] - c["slippage"], "gross": gross, "slip": c["slippage"], "days": 0})
                trades.append({"symbol": r["sym"], "day": d, "entry": r["o930"], "exit": r["c1520"], "qty": qty, "gap_vs_mkt": round(r["xgap"], 4),
                               "pnl": round(net), "won": net > 0})
            cap += pnl
        events.append(ev)
        equity.append(round(cap))
    syms = sorted({t["symbol"] for t in trades})
    bars = {}
    for s in syms:
        row = x[x["sym"] == s].set_index("day")
        bars[s] = [[float(row.at[d, "o915"]), float(row.at[d, "hi"]), float(row.at[d, "lo"]), float(row.at[d, "close"])] if d in row.index else None
                   for d in days]
    m = metrics(equity, capital, closed, len(days), 0)
    won = sum(t["won"] for t in trades)
    who = (f"every NSE stock liquid at the time (>= Rs{LIQUID_MIN / 1e7:.0f} cr median daily turnover over the 20 days before)" if liquid
           else f"the fixed list of {x['sym'].nunique()} stocks with 5-minute history (hindsight: today's liquid names -- expect less live)")
    m["note"] = (f"Results-day gap-up, {leverage:g}x the account per morning (split across that morning's buys), 09:30 -> 15:20, real Upstox "
                 f"intraday costs + 10 bp slippage a side. {len(trades)} trades, {won} won. Stocks: {who}. Days {all_days[0]} -> {all_days[-1]}.")
    return {"metrics": m, "equity": equity, "days": days, "events": events, "pos": [[] for _ in days], "bars": bars, "trades": trades,
            "overlays": {}, "unit": "stock"}
