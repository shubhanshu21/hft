"""equity / momentum_12_1 -- 12-month momentum, the 20 strongest of the day's 300 most-traded NSE stocks, refreshed monthly. Long only, delivery.

Research 2026-11-19 (user: "we need to get best profits in equity markets... properly research"; skills alpha-research, factor-investing,
survivorship-bias, overfitting-prevention; scratchpad eq_factors.py / eq_port.py / eq_robust.py / bo_mom_split.py). 12 price/volume/event
signals were measured on a point-in-time universe (each day the 300 stocks with the highest 250-day median traded value, price >= Rs20):
only the results reaction (= earnings_drift, already live) and 12-1 momentum kept their sign in 2023-24 AND 2025-26. Then 24 portfolios
with Upstox delivery costs, next-open fills, whole shares:

  top 20 by C[t-21] / C[t-252] - 1, rebalanced every ~21 trading days, no market filter, Rs1L, 2 Jan 2023 -> 18 Nov 2026:
  Rs1L -> Rs2.94L, worst fall -39% (15 Jul 2024 -> 3 Mar 2025), 2023 +98 | 2024 +20 | 2025 -7 | 2026 +32  (NIFTY 50: Rs1.39L)
  every neighbour (15-30 stocks, every 15-30 days) Rs2.58-3.04L and beat NIFTY in 2025-26; every start month 2023-01..2024-07 beat NIFTY;
  15 bp slippage a side: Rs2.84L. A market-breadth filter made 2025-26 worse every time; adding 52-week-high / results scores diluted it.
  Not proven: four years only (12-1 momentum needs a year of history, so from 2023), 2025 flat or negative in every version, and the
  archive holds today's listed stocks only (delisted names are missing -- small for the most-traded 300).

Paper: since 2026-11-19 (user's choice "inside Breakout, 50/50") half of the EQUITY_BREAKOUT account (engine/equity_breakout.py): on the
first trading session of each month it sells holdings that left the top 20 at the open and buys the new ones at the open, each 1/20 of
its half; no stop, no time limit (a holding stays while it is in the top 20). In the same backtest window the account (breakout 50% +
momentum 50%) made Rs1L -> Rs2.49L, worst fall -26% (breakout alone Rs2.17L / -18%). Live, a stock is held once per account: a top-20
name that a breakout sleeve already holds is skipped and the next one taken.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

NAME = "momentum_12_1"
N = 20                           # holdings
UNIVERSE, LIQ_DAYS, MIN_PRICE = 300, 250, 20.0
LOOKBACK, SKIP = 252, 21         # 12-month return, skipping the latest month (short-term reversal)
SLIP_BP = 5.0


def load():
    """The same split-adjusted daily panel the breakout engine loads (O/H/L/C/VAL, every NSE stock, nightly archive)."""
    from markets.equity.strategies.tf_daily.breakout_pair import strategy as BP
    return BP.load()


def scores(p: dict, universe: int = UNIVERSE) -> pd.DataFrame:
    """12-1 momentum per day and stock; NaN outside the day's `universe` most-traded stocks (250-day median value) or below Rs20."""
    C, VAL = p["C"], p["VAL"]
    liq = VAL.rolling(LIQ_DAYS, min_periods=200).median()
    univ = (liq.rank(axis=1, ascending=False) <= universe) & (C >= MIN_PRICE)
    return (C.shift(SKIP) / C.shift(LOOKBACK) - 1).where(univ)


def targets(row: pd.Series, n: int = N, exclude=()) -> list[str]:
    """The n highest scores of one day, strongest first, skipping `exclude` (stocks the account already holds in another sleeve)."""
    s = row.dropna()
    s = s[~s.index.isin(list(exclude))]
    return list(s.sort_values(ascending=False).index[:n])


def rebalance_due(last_month: str | None, today: date) -> bool:
    """Once per calendar month (the first session of the month), or right away if it never rebalanced."""
    return last_month != f"{today:%Y-%m}"


def backtest(capital: float, start: str, end: str, p: dict | None = None, n: int = N, universe: int = UNIVERSE,
             slip_bp: float = SLIP_BP) -> dict:
    """Day by day: decided on the last close of a month (or the day before `start`), traded at the next open; only holdings that left the
    top n are sold; each new buy gets 1/n of the money then, whole shares, never more than the cash. Returns days/equity/events/pos."""
    from markets.equity.strategies.tf_daily.earnings_drift.strategy import delivery_costs
    p = p or load()
    O, C = p["O"], p["C"]
    sc = scores(p, universe)
    idx = C.index
    i0 = max(int(idx.searchsorted(pd.Timestamp(start))), 1)
    i1 = int(idx.searchsorted(pd.Timestamp(end), side="right"))
    cash, pos, closed = float(capital), {}, []
    rep = {"days": [], "equity": [], "events": [], "pos": []}
    for i in range(i0, i1):
        d, evs = idx[i], []
        if i == i0 or idx[i - 1].month != d.month:                  # first session of the month: yesterday's close decides
            want = targets(sc.iloc[i - 1], n)
            o = O.iloc[i]
            for s in [s for s in pos if s not in want]:
                q, e, i_in = pos.pop(s)
                px = o[s] if o[s] > 0 else C.iloc[i - 1][s]
                c = delivery_costs(e, px, q, slip_bp)
                net = q * (px - e) - c["total"]
                cash += q * px - c["total"]
                closed.append({"net": net, "fees": c["total"] - c["slippage"], "slip": c["slippage"], "gross": q * (px - e), "days": (d - idx[i_in]).days})
                evs.append({"t": "sell", "sym": s, "q": q, "px": round(px, 2), "pnl": round(net, 2), "fees": round(c["total"] - c["slippage"], 2),
                            "slip": c["slippage"], "days": (d - idx[i_in]).days, "why": "Left the top 20 (monthly check)", "entry": round(e, 2)})
            value = cash + sum(q * (o[s] if o[s] > 0 else e) for s, (q, e, _) in pos.items())
            for s in [s for s in want if s not in pos]:
                px = o[s]
                if not px > 0:
                    continue
                q = int(min(value / n, cash) // px)
                if q >= 1:
                    cash -= q * px; pos[s] = (q, px, i)
                    evs.append({"t": "buy", "sym": s, "q": q, "px": round(px, 2), "note": "top 20 by 12-month momentum"})
        c_ = C.iloc[i]
        value = cash + sum(q * (c_[s] if c_[s] > 0 else e) for s, (q, e, _) in pos.items())
        rep["days"].append(d.strftime("%Y-%m-%d")); rep["equity"].append(round(value)); rep["events"].append(evs)
        rep["pos"].append([{"sym": s, "q": q, "e": round(e, 2), "px": round(float(c_[s]) if c_[s] > 0 else e, 2), "start": i_in - i0}
                           for s, (q, e, i_in) in pos.items()])
    rep["closed"], rep["open"] = closed, len(pos)
    return rep


def _nifty() -> pd.Series:
    from markets.equity.strategies.tf_daily.earnings_drift.strategy import nifty_daily
    return nifty_daily()


def web_backtest(capital: float, start: str, end: str, slippage_bp: float = SLIP_BP) -> dict:
    """The website's replay shape (engine/backtest_api.py)."""
    from core.backtester import metrics
    rep = backtest(capital, start, end, slip_bp=slippage_bp)
    if len(rep["days"]) < 2:
        raise ValueError("No trading days in the chosen dates (daily data starts January 2022; 12-month momentum needs a year, so from 2023)")
    nd = _nifty()
    days = pd.to_datetime(rep["days"])
    n = nd.reindex(nd.index.union(days)).ffill().reindex(days).bfill()
    rep["benchmark"] = [round(capital * v / n.iloc[0]) for v in n]
    rep["metrics"] = metrics(rep["equity"], capital, rep.pop("closed"), len(rep["days"]), rep.pop("open")) | {
        "note": "Each month the 20 NSE stocks with the best 12-month return (skipping the latest month) among the 300 most-traded; "
                "a holding stays while it is in the top 20. No stop, no market filter. Benchmark = NIFTY 50."}
    rep["skipped_symbols"] = []
    return rep
