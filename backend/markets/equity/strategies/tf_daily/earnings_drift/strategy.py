"""equity / earnings_drift -- buy stocks whose quarterly results got a strong positive reaction, hold 40 trading days (post-earnings drift).

Research 2026-10-16 (user "ok do what u want", then "ok do it" to running it in the shared equity swing account):
  * events: every "Financial Results" board meeting on NSE (var/archive/nse/board_meetings_all.csv.gz, all equities since 2022,
    refreshed nightly by markets/equity/results_dates.py) of a stock that was among the day's 200 most-traded NSE stocks;
  * reaction = the stock's return from the close before the meeting day to the close of the day after it, minus the average stock's;
  * buy when the reaction is above +4% and the two days' traded value is at least 2x its 50-day average, at the open two days after the
    meeting (the reaction is known by then), strongest reaction first, only while >= 50% of the 200 stocks are above their 50-day average;
  * up to 5 stocks, 1/5 of the strategy's money each; sell at the open after 40 trading days. NO stop: stops cut the drift short
    (Rs1L -> Rs0.86-0.93L with a 2.5-ATR stop, against Rs1.95L without).
Event study (3,369 events): reaction > +4% -> +1.1..+2.3% excess return over 40 days in both 2022-24 and 2025-26, every year positive;
weak reactions keep lagging. Account test, Rs1L, delivery fees: Oct 2022 -> Oct 2026 Rs1.95L (+18%/yr), worst fall -16%,
2022-24 +20%/yr, 2025-26 +16%/yr; years +8 / +19 / +17 / +21 / +7%. About 16 trades a year. Not proven: four years, few trades.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT

EVENTS_FILE = ARCHIVE_ROOT / "nse" / "board_meetings_all.csv.gz"
MIN_REACTION, MIN_VOL_SURGE = 0.04, 2.0
K, HOLD_DAYS = 5, 40
MIN_BREADTH_PCT = 50.0


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


def backtest(start="2022-10-03", end=None, capital=100_000.0, ind: dict | None = None, slip_bp: float = 5.0, record: bool = False) -> dict:
    from markets.equity.strategies.tf_daily.breakout_pair.strategy import _fee, indicators, load
    ind = ind or indicators(load())
    sig = signals(ind)
    O, C = ind["O"], ind["C"]
    idx = C.index; col = {s: j for j, s in enumerate(C.columns)}
    days = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end or idx[-1]))]
    cash, pos, trades, curve = capital, {}, [], []
    rep = {"days": [], "equity": [], "events": [], "pos": []}; traded = set()
    for d in days:
        i = idx.get_loc(d); evs = []
        for s in list(pos):                                            # time exit at the open
            p = pos[s]
            if i - p["i0"] >= HOLD_DAYS:
                xp = O.iat[i, col[s]]
                xp = xp if xp == xp else p["last"]
                fee, slip = _fee(p["q"] * p["e"], p["q"] * xp, slip_bp)
                cash += p["q"] * xp - fee - slip; net = p["q"] * (xp - p["e"]) - fee - slip
                trades.append(dict(sym=s, entry_day=p["d0"], exit_day=d, entry=p["e"], exit=xp, qty=p["q"], net=net, fees=fee, slip=slip, gross=p["q"] * (xp - p["e"])))
                evs.append({"t": "sell", "sym": s, "q": p["q"], "px": round(xp, 2), "pnl": round(net, 2), "fees": round(fee, 2), "slip": round(slip, 2),
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
                evs.append({"t": "buy", "sym": s, "q": q, "px": round(e, 2), "note": f"results {r.meeting:%d %b}: +{r.reaction:.1%} vs market on {r.surge:.1f}x volume"})
        for s, p in pos.items():
            c = C.iat[i, col[s]]
            if c == c:
                p["last"] = c
        value = cash + sum(p["q"] * p["last"] for p in pos.values()); curve.append((d, value))
        if record:
            rep["days"].append(d.strftime("%Y-%m-%d")); rep["equity"].append(round(value)); rep["events"].append(evs)
            rep["pos"].append([{"sym": s, "q": p["q"], "e": round(p["e"], 2), "px": round(p["last"], 2), "start": p["k"]} for s, p in pos.items()])
    curve = pd.Series(dict(curve))
    yearly = curve.groupby(curve.index.year).last(); yearly = yearly / yearly.shift(1).fillna(capital) - 1
    yrs = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
    out = dict(curve=curve, trades=pd.DataFrame(trades), open=pos, yearly=yearly, final=float(curve.iloc[-1]),
               cagr=(curve.iloc[-1] / capital) ** (1 / yrs) - 1, max_dd=float((curve / curve.cummax() - 1).min()))
    if record:
        sel = (idx >= days[0]) & (idx <= days[-1])
        rep["bars"] = {s: [[round(float(x), 2) if x == x else None for x in r] for r in zip(ind["O"].loc[sel, s], ind["H"].loc[sel, s], ind["L"].loc[sel, s], C.loc[sel, s])]
                       for s in sorted(traded)}
        rep["bars"] = {s: [r if r[3] is not None else None for r in v] for s, v in rep["bars"].items()}
        out["replay"] = rep
    return out
