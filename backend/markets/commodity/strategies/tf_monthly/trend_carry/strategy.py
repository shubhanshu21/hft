"""commodity / trend_carry -- monthly MCX trend + carry: hold the 3 commodities that have risen most AND whose next contract is cheapest.

Research: Fuertes, Miffre & Rallis (2010), "Tactical allocation in commodity futures markets: combining momentum and term structure signals"
(momentum alone ~9%/yr, the combination best). Tested 2026-10-15 on real MCX month-end bhavcopies (the user downloaded them in a browser,
Dec 2021 -> Oct 2026, every expiry) -- see markets/commodity/futures_eod.py for the data.

Rules (fixed before the test; 8 liquid commodities; decided at each month end, held one month):
  * trend = return over the last 12 months of holding the nearest contract that is still alive at the next month end, rolled monthly;
  * carry = log(near / next contract price) per year (positive = the next month is cheaper = backwardation = buyers earn the roll);
  * score = rank(trend) + rank(carry); buy the top 3, buy-only (selling short tested worse);
  * size each holding by 1 / its 12-month volatility (the research standard; 2026-10-15 test: +6-11%/yr at the same worst fall);
  * trade the MINI contracts (GOLDTEN, SILVERMIC, CRUDEOILM, NATGASMINI, ZINCMINI, ALUMINI, LEADMINI; COPPER has no mini), whole lots only;
    a holding whose one lot is larger than its share stays in cash.
Result on real MCX prices, Rs10 lakh at 1x, vol sizing: Rs10L -> Rs20.4L (2023-26), worst fall -12.9%. NOT proven: ~46 months, a few big
months decide each year (2023 -16%), and 2 or 4 holdings instead of 3 did much worse. Long/short and trend-only or carry-only versions were weaker.
Account protection tested: the trend filter and a drawdown brake both cut returns without cutting the worst fall -> not used. Exposure is the
real protection: at 1x the worst month was ~-13%; at 2-3x it was -25% to -44%; at full Upstox leverage (~8x) June 2026 alone lost ~85%.

This is not a core.strategy.Strategy (it ranks 8 commodities together and picks expiries), so it has its own monthly paper engine
(engine/mcx_trend_carry.py) and its own backtest (backtest() below, also used by the web backtest page).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from markets.commodity.costs import compute_mcx_costs, lot_units

SIGNALS = ("GOLDM", "SILVERM", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC", "ALUMINIUM", "LEAD")
TRADE = {"GOLDM": "GOLDTEN", "SILVERM": "SILVERMIC", "CRUDEOIL": "CRUDEOILM", "NATURALGAS": "NATGASMINI", "COPPER": "COPPER",
         "ZINC": "ZINCMINI", "ALUMINIUM": "ALUMINI", "LEAD": "LEADMINI"}
TOP_N, LOOKBACK, VOL_WINDOW = 3, 12, 12
MARGIN_PCT = {"GOLDM": 0.093, "SILVERM": 0.126, "CRUDEOIL": 0.35, "NATURALGAS": 0.30, "COPPER": 0.10, "ZINC": 0.092, "ALUMINIUM": 0.093, "LEAD": 0.10}
MAX_MARGIN_USE = 0.8           # never block more than 80% of the account as margin
EXPOSURE_MIN = 0.25
EXPOSURE_MAX = round(1 / min(MARGIN_PCT.values()), 1)      # the most Upstox lets any of them be geared (gold ~9.3% margin -> ~10.8x)
SIGNAL_OF = {v: k for k, v in TRADE.items()}                 # mini contract -> the signal contract it follows


def top_n(n_symbols: int) -> int:
    """3 holdings out of 8; with fewer commodities chosen, at most half of them (so there is still a ranking), at least 1."""
    return max(1, min(TOP_N, n_symbols // 2))


def month_ends(table: pd.DataFrame, before: pd.Timestamp | None = None) -> list[pd.Timestamp]:
    """Last trading day of each COMPLETED month: the bhavcopy date when one exists (it has every contract), else the newest day."""
    t = table[table["symbol"].isin(SIGNALS)]
    if before is not None:
        t = t[t["date"] < before.replace(day=1)]
    out = []
    for _, g in t.groupby(t["date"].dt.to_period("M")):
        b = g[g["source"] == "bhavcopy"] if "source" in g else g
        out.append((b if len(b) else g)["date"].max())
    return sorted(out)


def _rows(table, sym, day):
    x = table[(table["symbol"] == sym) & (table["date"] == day) & (table["close"] > 0)]
    return x.sort_values("expiry")


def panel(table: pd.DataFrame, ends: list[pd.Timestamp], signals=SIGNALS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """R: month-by-month return of holding each commodity's contract alive at the next month end; C: carry at each month end."""
    R, C = {}, {}
    for s in signals:
        r, c = {}, {}
        for i, t in enumerate(ends):
            cur = _rows(table, s, t)
            two = cur[(cur["expiry"] > t + pd.Timedelta(days=3)) & (cur["volume"] > 0)].head(2)
            if len(two) == 2:
                days = max((two["expiry"].iloc[1] - two["expiry"].iloc[0]).days, 1)
                c[t] = np.log(two["close"].iloc[0] / two["close"].iloc[1]) * 365 / days
            if i + 1 < len(ends):
                nxt = ends[i + 1]
                alive = cur[cur["expiry"] > nxt]
                if len(alive):
                    k = alive.iloc[0]
                    later = _rows(table, s, nxt)
                    later = later[later["expiry"] == k["expiry"]]
                    if len(later):
                        r[nxt] = later["close"].iloc[0] / k["close"] - 1
        R[s], C[s] = pd.Series(r, dtype=float), pd.Series(c, dtype=float)
    return pd.DataFrame(R).sort_index(), pd.DataFrame(C).sort_index()


def targets(R: pd.DataFrame, C: pd.DataFrame, t: pd.Timestamp) -> dict[str, float]:
    """{signal symbol: weight} decided at month end t (weights sum to 1; empty when there is not yet 12 months of history)."""
    mom = (1 + R).rolling(LOOKBACK).apply(np.prod, raw=True) - 1
    if t not in mom.index or t not in C.index:
        return {}
    score = (mom.loc[t].rank() + C.loc[t].rank()).dropna()
    n = top_n(len(R.columns))
    if len(score) < 2 * n:
        return {}
    picks = list(score.nlargest(n).index)
    vol = R.rolling(VOL_WINDOW, min_periods=6).std().loc[t]
    inv = {s: 1 / vol[s] for s in picks if vol.get(s, 0) > 0}
    tot = sum(inv.values())
    return {s: v / tot for s, v in inv.items()} if tot else {s: 1 / len(picks) for s in picks}


def lots_for(capital: float, exposure: float, weight: float, price: float, signal: str) -> int:
    """Whole lots of the mini contract: the notional share (capital x exposure x weight), never more than the margin share allows."""
    lot_val = price * lot_units(TRADE[signal])
    by_notional = capital * exposure * weight // lot_val
    by_margin = capital * MAX_MARGIN_USE * weight // (lot_val * MARGIN_PCT[signal])
    return int(max(0, min(by_notional, by_margin)))


def backtest(table: pd.DataFrame, capital: float = 500_000, start: str = "2023-01-01", end: str = "2100-01-01", exposure: float = 1.5,
             symbols=None) -> dict:
    """Month-end backtest in the web backtester's replay shape: one row per month end (days, equity, events, pos, bars, benchmark, metrics).
    At each month end the last month's holdings are sold at that day's close of the SAME contract and the new ones bought at the close of the
    contract alive past the next month end. Prices are the signal contract's month-end close (minis track it per unit); fees and half-spread
    from markets/commodity/costs.py on the mini contract."""
    from core.backtester import metrics
    signals = tuple(SIGNAL_OF.get(x, x) for x in symbols) if symbols else SIGNALS     # the mini contracts chosen on the page -> their signal contracts
    ends = month_ends(table)
    R, C = panel(table, ends, signals)
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    ends = [t for t in ends if t1 >= t >= t0 - pd.offsets.MonthEnd(1)]
    cap, book, closed, skipped = capital, {}, [], []
    out = {"days": [], "equity": [], "events": [], "pos": [], "bars": {}}
    bench, bench_v = [], capital
    prev_px: dict[str, float] = {}
    for k, t in enumerate(ends):
        evs = []
        for s, h in list(book.items()):                                  # 1. sell last month's holdings at this month end
            now = _rows(table, s, t)
            now = now[now["expiry"] == h["expiry"]]
            px = float(now["close"].iloc[0]) if len(now) else h["entry"]
            c = compute_mcx_costs(TRADE[s], "long", h["entry"], px, h["lots"])
            cap += c["net"]
            fees = c["total"] - c["slippage"]
            closed.append({"net": c["net"], "fees": fees, "slip": c["slippage"], "gross": c["gross"], "days": (t - h["t"]).days})
            evs.append({"t": "sell", "sym": TRADE[s], "q": h["lots"] * lot_units(TRADE[s]), "px": round(px, 2), "pnl": round(c["net"], 2),
                        "fees": round(fees, 2), "slip": c["slippage"], "days": (t - h["t"]).days, "why": "Monthly rebalance", "entry": round(h["entry"], 2)})
            del book[s]
        if k > 0 and t in R.index:
            bench_v *= 1 + float(R.loc[t].mean())
        nxt = ends[k + 1] if k + 1 < len(ends) else None
        if nxt is not None:                                              # 2. buy this month's picks
            for s, wt in targets(R, C, t).items():
                alive = _rows(table, s, t)
                alive = alive[alive["expiry"] > nxt]
                if alive.empty:
                    continue
                ct = alive.iloc[0]
                lots = lots_for(cap, exposure, wt, float(ct["close"]), s)
                if lots < 1:
                    skipped.append(TRADE[s])                             # one lot is bigger than this holding's share of the money
                    continue
                book[s] = {"expiry": ct["expiry"], "entry": float(ct["close"]), "lots": lots, "k": k, "t": t}
                evs.append({"t": "buy", "sym": TRADE[s], "q": lots * lot_units(TRADE[s]), "px": round(float(ct["close"]), 2), "note": f"weight {wt:.0%}"})
        for s in signals:                                                # monthly candle per mini symbol: last month end's close -> this one
            front = _rows(table, s, t)
            front = front[front["expiry"] > t + pd.Timedelta(days=3)]
            c_ = float(front["close"].iloc[0]) if len(front) else None
            o_ = prev_px.get(s, c_)
            out["bars"].setdefault(TRADE[s], []).append([o_, max(o_, c_), min(o_, c_), c_] if c_ is not None else None)
            if c_ is not None:
                prev_px[s] = c_
        out["days"].append(t.strftime("%Y-%m-%d")); out["equity"].append(round(cap)); out["events"].append(evs)
        out["pos"].append([{"sym": TRADE[s], "q": h["lots"] * lot_units(TRADE[s]), "e": round(h["entry"], 2), "px": round(h["entry"], 2), "start": h["k"]}
                           for s, h in book.items()])
        bench.append(round(bench_v))
    if len(out["days"]) < 2 or not closed:
        raise ValueError("No complete month in the chosen dates (trend_carry needs 12 months of history before its first trade, i.e. from 2023)")
    out["bars"] = {k_: v for k_, v in out["bars"].items() if any(v)}
    out["benchmark"] = bench
    out["metrics"] = metrics(out["equity"], capital, closed, len(out["days"]) * 21, len(book)) | {
        "note": f"Monthly strategy: one point per month end; buys and sells happen at month-end closes; holds the best {top_n(len(signals))} of the "
                f"{len(signals)} chosen commodities. Benchmark = the chosen commodities held equally."
                + (f" {len(skipped)} planned holdings were skipped because one lot was bigger than their share of the money"
                   f" ({', '.join(sorted(set(skipped)))}): more starting money or exposure trades them." if skipped else "")}
    out["skipped_symbols"] = []
    return out
