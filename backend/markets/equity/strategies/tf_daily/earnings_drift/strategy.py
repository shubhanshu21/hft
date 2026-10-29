"""equity / earnings_drift -- buy stocks whose quarterly results got a strong positive reaction, hold 40 trading days (post-earnings drift).

Rules (research 2026-10-16, unchanged since):
  * events: every "Financial Results" board meeting on NSE (var/archive/nse/board_meetings_all.csv.gz, all equities since 2022,
    refreshed nightly by markets/equity/results_dates.py) of a stock that was among the day's 200 most-traded NSE stocks;
  * reaction = the stock's return from the close before the meeting day to the close of the day after it, minus the average stock's;
  * buy when the reaction is above +4% and the two days' traded value is at least 2x its 50-day average, at the open two days after the
    meeting (the reaction is known by then), strongest reaction first, only while >= 50% of the 200 stocks are above their 50-day average;
  * up to 5 stocks, 1/5 of the strategy's money each; sell at the open after 40 trading days. NO stop: stops cut the drift short
    (Rs1L -> Rs0.86-0.93L with a 2.5-ATR stop, against Rs1.95L without).
Re-test 2026-10-29 (same rules, data to 2026-10-19): Rs1L -> Rs1.95L since Oct 2022 (+18%/yr, worst fall -16%, 65 trades, 75% won),
2022-24 +20%/yr, 2025-26 +16%/yr, every year positive (2026 +7%). Not proven: four years, few trades.

Paper (2026-10-29, user: "add this strategy and use same equity 1 lakh account"): it shares the RESULTS_GAP account with the intraday
results_gap strategy (engine/earnings_drift.py). Its money is SLEEVE_CAPITAL plus its own closed trades; a buy is also capped by the
account's free cash. results_gap sizes its 3x intraday trades from the cash these holdings do not lock. Simulated together on Rs1L
(Oct 2022 -> Oct 2026): results_gap alone Rs5.75L (worst fall -26%), with earnings_drift Rs6.19L (-23%).
The orders are decided before the open (08:50) and fill at the official open price, as an order placed in NSE's 09:00-09:08 pre-open
auction does -- the price the backtest uses.
"""
from __future__ import annotations

import glob
import os
from datetime import date, timedelta

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT

NAME = "earnings_drift"
EVENTS_FILE = ARCHIVE_ROOT / "nse" / "board_meetings_all.csv.gz"
DATA_DIR = ARCHIVE_ROOT / "equity_all"            # every NSE equity, daily (markets/equity/all_daily.py, nightly)
MIN_REACTION, MIN_VOL_SURGE = 0.04, 2.0
K, HOLD_DAYS = 5, 40
MIN_BREADTH_PCT = 50.0
SLEEVE_CAPITAL = 100_000.0
UNIVERSE, LIQ_DAYS, MIN_HISTORY, MIN_PRICE = 200, 126, 125, 20.0      # the day's 200 most-traded stocks (126-day median traded value)
SPLIT_LOW, SPLIT_HIGH = 0.6, 1.7   # an overnight open/prev-close outside this is a split / bonus / demerger, not a market move
SLIP_BP = 5.0


def load(symbols=None, since="2022-01-01") -> dict[str, pd.DataFrame]:
    """Daily open/high/low/close/traded value per stock (columns = symbols), history scaled for splits, bonuses and demergers."""
    o, h, l, c, v = {}, {}, {}, {}, {}
    files = [DATA_DIR / f"{s}_daily.csv.gz" for s in symbols] if symbols else sorted(glob.glob(str(DATA_DIR / "*_daily.csv.gz")))
    for f in files:
        if not os.path.exists(f):
            continue
        x = pd.read_csv(f)
        if len(x) < 30:
            continue
        x["d"] = pd.to_datetime(x["timestamp"].str[:10])
        x = x.drop_duplicates("d", keep="last").set_index("d").sort_index()
        r = x["open"] / x["close"].shift()
        adj = pd.Series(1.0, index=x.index)
        for i in np.where((r < SPLIT_LOW) | (r > SPLIT_HIGH))[0]:
            adj.iloc[:i] *= r.iloc[i]
        s = os.path.basename(str(f))[: -len("_daily.csv.gz")]
        o[s], h[s], l[s], c[s] = x["open"] * adj, x["high"] * adj, x["low"] * adj, x["close"] * adj
        v[s] = (x["close"] * x["volume"]).astype(float)
    mk = lambda d: pd.DataFrame(d).sort_index().loc[since:].astype(float)
    return {"O": mk(o), "H": mk(h), "L": mk(l), "C": mk(c), "VAL": mk(v)}


def data_range() -> dict:
    """First and last day of the daily archive (read from one always-liquid stock), for the Backtest page."""
    try:
        d = pd.read_csv(DATA_DIR / "RELIANCE_daily.csv.gz", usecols=["timestamp"])["timestamp"].str[:10]
        return {"from": str(d.iloc[0]), "to": str(d.iloc[-1])}
    except (OSError, ValueError, KeyError, IndexError):
        return {"from": None, "to": None}


_CACHE: dict = {}


def cached_indicators() -> dict:
    """indicators(load()) once per archive version (the website runs many backtests; a load reads ~4,300 files)."""
    newest = max((f.stat().st_mtime for f in DATA_DIR.glob("*_daily.csv.gz")), default=0.0)
    if _CACHE.get("mtime") != newest:
        _CACHE.update(mtime=newest, ind=indicators(load()))
    return _CACHE["ind"]


def nifty_daily() -> pd.Series:
    """NIFTY 50 daily closes from the nightly 1-minute index archive (engine/research_data.py) -- the benchmark line."""
    x = pd.read_csv(ARCHIVE_ROOT / "index" / "NIFTY50_1minute.csv.gz", usecols=["timestamp", "close"])
    x["d"] = pd.to_datetime(x["timestamp"].str[:10])
    return x.groupby("d")["close"].last()


def indicators(p: dict) -> dict:
    """The day's 200 most-traded stocks and market health (% of them above their 50-day EMA), as date x stock frames."""
    C, VAL = p["C"], p["VAL"]
    e50 = C.ewm(span=50, adjust=False, min_periods=50).mean()
    liq = VAL.rolling(LIQ_DAYS, min_periods=100).median()
    hist = C.notna().rolling(LIQ_DAYS + 4, min_periods=1).sum()
    univ = liq.where((hist >= MIN_HISTORY) & (C >= MIN_PRICE)).rank(axis=1, ascending=False) <= UNIVERSE
    breadth = ((C > e50) & univ).sum(axis=1) / univ.sum(axis=1).replace(0, np.nan) * 100
    return dict(univ=univ, breadth=breadth, **p)


def results_events() -> pd.DataFrame:
    """(symbol, meeting day) for every financial-results board meeting."""
    bm = pd.read_csv(EVENTS_FILE, usecols=["bm_symbol", "bm_date", "bm_purpose"])
    bm = bm[bm["bm_purpose"].str.contains("financial result", case=False, na=False)].copy()
    bm["d"] = pd.to_datetime(bm["bm_date"], format="%d-%b-%Y", errors="coerce")
    return bm.dropna(subset=["d"]).drop_duplicates(["bm_symbol", "d"]).rename(columns={"bm_symbol": "sym"})[["sym", "d"]]


def signals(ind: dict, events: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per qualifying event: sym, meeting day, signal day (day+1: the reaction is complete at its close), reaction, volume surge.
    The buy is at the open of the trading day after `signal_day`."""
    events = results_events() if events is None else events
    C, VAL, univ = ind["C"], ind["VAL"], ind["univ"]
    idx = C.index
    mkt = (1 + C.pct_change(fill_method=None).where(univ).mean(axis=1).fillna(0)).cumprod()
    avgv = VAL.rolling(50, min_periods=30).mean().shift(1)
    rows = []
    for r in events.itertuples():
        if r.sym not in C.columns:
            continue
        p0 = idx.searchsorted(r.d)                                     # day 0: the meeting day, or the next trading day
        if p0 < 1 or p0 + 1 >= len(idx):
            continue
        j = C.columns.get_loc(r.sym)
        if not bool(univ.iat[p0 - 1, j]):
            continue
        pre, c1 = C.iat[p0 - 1, j], C.iat[p0 + 1, j]
        if not (pre > 0 and c1 > 0):
            continue
        reaction = c1 / pre - 1 - (mkt.iat[p0 + 1] / mkt.iat[p0 - 1] - 1)
        surge = (VAL.iat[p0, j] + VAL.iat[p0 + 1, j]) / 2 / avgv.iat[p0, j] if avgv.iat[p0, j] > 0 else np.nan
        if reaction > MIN_REACTION and surge >= MIN_VOL_SURGE:
            rows.append(dict(sym=r.sym, meeting=idx[p0], signal_day=idx[p0 + 1], reaction=reaction, surge=surge))
    return pd.DataFrame(rows, columns=["sym", "meeting", "signal_day", "reaction", "surge"])


def candidates(ind: dict, sig: pd.DataFrame, day) -> list[str]:
    """Stocks to buy at the open after the completed day `day`, strongest reaction first; [] in an unhealthy market."""
    if float(ind["breadth"].loc[day]) < MIN_BREADTH_PCT:
        return []
    return sig[sig["signal_day"] == day].sort_values("reaction", ascending=False)["sym"].tolist()


def _fee(ev: float, xv: float, slip_bp: float = SLIP_BP) -> tuple[float, float]:
    """Backtest charges for one round trip: (Upstox delivery fees, slippage at slip_bp a side)."""
    br = lambda v: min(v * 0.001, 30.0)
    fees = (br(ev) + br(xv)) * 1.18 + (ev + xv) * 0.001 + ev * 0.00015 + (ev + xv) * 0.0000335 * 1.18 + 20 * 1.18
    return fees, (ev + xv) * slip_bp / 1e4


def delivery_costs(entry: float, exit_price: float, qty: int) -> dict:
    """_fee as the itemised dict the trades table stores (DP charge Rs20 + GST counted with brokerage)."""
    ev, xv = entry * qty, exit_price * qty
    brokerage = min(ev * 0.001, 30.0) + min(xv * 0.001, 30.0) + 20.0
    stt, stamp, exch = (ev + xv) * 0.001, ev * 0.00015, (ev + xv) * 0.0000335
    gst, slip = 0.18 * (brokerage + exch), (ev + xv) * SLIP_BP / 1e4
    total = brokerage + stt + stamp + exch + gst + slip
    return {"brokerage": round(brokerage, 2), "stt": round(stt, 2), "stamp_duty": round(stamp, 2), "exchange_txn": round(exch, 2),
            "sebi": 0.0, "gst": round(gst, 2), "slippage": round(slip, 2), "total": round(total, 2)}


def trading_days_held(entry_day: date, today: date, holidays: set[date]) -> int:
    """Trading days after the buy day up to and including today (the backtest sells at the open when this reaches HOLD_DAYS)."""
    return int(np.busday_count(entry_day + timedelta(days=1), today + timedelta(days=1), holidays=sorted(holidays)))


def backtest(start="2022-10-03", end=None, capital=100_000.0, ind: dict | None = None, slip_bp: float = SLIP_BP, record: bool = False,
             progress=None) -> dict:
    """The research account: Rs1L, up to K stocks, buy at the open after the signal day, sell at the open after HOLD_DAYS.
    record=True also returns the website's replay (days, equity, events, open positions, price bars of the traded stocks)."""
    ind = ind or indicators(load())
    sig = signals(ind)
    O, C = ind["O"], ind["C"]
    idx = C.index; col = {s: j for j, s in enumerate(C.columns)}
    days = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end or idx[-1]))]
    cash, pos, trades, curve = capital, {}, [], []
    rep = {"days": [], "equity": [], "events": [], "pos": []}; traded = set()
    for k, d in enumerate(days):
        if progress is not None and k % max(1, len(days) // 100) == 0:
            progress(k, len(days))
        i = idx.get_loc(d); evs = []
        for s in list(pos):                                            # time exit at the open
            p = pos[s]
            if i - p["i0"] >= HOLD_DAYS:
                xp = O.iat[i, col[s]]
                xp = xp if xp == xp else p["last"]
                fee, slip = _fee(p["q"] * p["e"], p["q"] * xp, slip_bp)
                cash += p["q"] * xp - fee - slip; net = p["q"] * (xp - p["e"]) - fee - slip
                trades.append(dict(sym=s, entry_day=p["d0"], exit_day=d, entry=p["e"], exit=xp, qty=p["q"], net=net, fees=fee, slip=slip, gross=p["q"] * (xp - p["e"])))
                evs.append({"t": "sell", "side": "long", "sym": s, "q": p["q"], "px": round(xp, 2), "pnl": round(net, 2), "fees": round(fee, 2), "slip": round(slip, 2),
                            "days": (d - p["d0"]).days, "why": f"Held {HOLD_DAYS} trading days", "entry": round(p["e"], 2)})
                pos.pop(s)
        if i > 0 and len(pos) < K:
            for s in candidates(ind, sig, idx[i - 1]):
                if len(pos) >= K or s in pos:
                    continue
                e = O.iat[i, col[s]]
                if not e > 0:
                    continue
                equity = cash + sum(p["q"] * p["last"] for p in pos.values())
                q = int(min(equity / K, cash) / e)
                if q < 1:
                    continue
                cash -= q * e; pos[s] = dict(q=q, e=e, i0=i, d0=d, last=e, k=len(rep["days"])); traded.add(s)
                r = sig[(sig.sym == s) & (sig.signal_day == idx[i - 1])].iloc[0]
                evs.append({"t": "buy", "side": "long", "sym": s, "q": q, "px": round(e, 2), "entry": round(e, 2),
                            "note": f"results {r.meeting:%d %b}: {r.reaction:+.1%} vs market on {r.surge:.1f}x volume"})
        for s, p in pos.items():
            c = C.iat[i, col[s]]
            if c == c:
                p["last"] = c
        value = cash + sum(p["q"] * p["last"] for p in pos.values()); curve.append((d, value))
        if record:
            rep["days"].append(d.strftime("%Y-%m-%d")); rep["equity"].append(round(value)); rep["events"].append(evs)
            rep["pos"].append([{"sym": s, "q": p["q"], "e": round(p["e"], 2), "px": round(p["last"], 2), "start": p["k"]} for s, p in pos.items()])
    if not len(curve):
        raise ValueError("No trading days in the chosen dates (daily data starts January 2022; signals need ~6 months of history)")
    curve = pd.Series(dict(curve))
    yearly = curve.groupby(curve.index.year).last(); yearly = yearly / yearly.shift(1).fillna(capital) - 1
    yrs = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
    out = dict(curve=curve, trades=pd.DataFrame(trades), open=pos, yearly=yearly, final=float(curve.iloc[-1]),
               cagr=(curve.iloc[-1] / capital) ** (1 / yrs) - 1, max_dd=float((curve / curve.cummax() - 1).min()))
    if record:
        sel = (idx >= days[0]) & (idx <= days[-1])
        bars = {s: [[round(float(x), 2) if x == x else None for x in r] for r in zip(ind["O"].loc[sel, s], ind["H"].loc[sel, s], ind["L"].loc[sel, s], C.loc[sel, s])]
                for s in sorted(traded)}
        rep["bars"] = {s: [r if r[3] is not None else None for r in v] for s, v in bars.items()}
        out["replay"] = rep
    return out


def web_backtest(capital: float, start: str, end: str, slippage_bp: float = SLIP_BP, progress=None) -> dict:
    """The website's replay shape (engine/backtest_api.py, the Backtest page). Daily data: var/archive/equity_all (every NSE stock)."""
    from core.backtester import metrics
    r = backtest(start, end, capital, ind=cached_indicators(), slip_bp=slippage_bp, record=True, progress=progress)
    rep = r["replay"]
    if len(rep["days"]) < 2:
        raise ValueError("No trading days in the chosen dates (daily data starts January 2022; signals need ~6 months of history)")
    t = r["trades"]
    closed = [{"net": x.net, "fees": x.fees, "slip": x.slip, "gross": x.gross, "days": (x.exit_day - x.entry_day).days} for x in t.itertuples()] if len(t) else []
    n = nifty_daily().reindex(pd.to_datetime(rep["days"])).ffill().bfill()
    rep["benchmark"] = [round(capital * v / n.iloc[0]) for v in n]
    won = int((t.net > 0).sum()) if len(t) else 0
    rep["metrics"] = metrics(rep["equity"], capital, closed, len(rep["days"]), len(r["open"])) | {
        "note": f"Buys a stock at the open two days after quarterly results that beat the market by more than {MIN_REACTION:.0%} on "
                f"{MIN_VOL_SURGE:.0f}x volume (one of the day's {UNIVERSE} most-traded NSE stocks), up to {K} at a time, {1 / K:.0%} of the money "
                f"each; sells at the open after {HOLD_DAYS} trading days, no stop. Buys only while half of those {UNIVERSE} stocks are above "
                f"their 50-day average. Upstox delivery fees + {slippage_bp:g} bp slippage a side. {len(t)} trades, {won} won. "
                f"Daily data to {rep['days'][-1]}. Benchmark = NIFTY 50."}
    rep.update(trades=[{"symbol": x.sym, "day": str(x.entry_day.date()), "exit_day": str(x.exit_day.date()), "entry": round(x.entry, 2),
                        "exit": round(x.exit, 2), "qty": int(x.qty), "pnl": round(x.net), "won": x.net > 0} for x in t.itertuples()] if len(t) else [],
               overlays={}, unit="stock", skipped_symbols=[])
    return rep
