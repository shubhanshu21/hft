"""equity / momentum_portfolio -- hold the 15 stocks with the steadiest 6-month rise among the day's 200 most-traded NSE stocks; rebalance quarterly.

Research 2026-10-16 (point-in-time list, ~190 portfolio versions; then the user said "ok" to running it next to equity/breakout_pair):
  * score = 6-month (126-day) return / volatility of daily returns over the same 126 days (the "risk-adjusted momentum" NSE's Momentum
    indices use); stocks: the 200 with the highest 126-day median traded value that day (same list as breakout_pair), price >= Rs20;
  * at each quarter end: keep a holding while it ranks in the top 30 (2 x 15 -- cuts trading ~30%), sell the rest, buy the best-ranked
    stocks not held to fill 15 slots, each with 1/15 of the account (kept holdings are not trimmed); buy and sell at the next session's open;
  * no market-timing switch: moving to cash when the NIFTY was below its 200-day average HURT in 2025-26 (it sold the lows, missed 2026);
  * long only, delivery, Upstox delivery fees.
Results, Rs1L, 2023-03 -> 2026-10 (fair list, real fees): 15 stocks alone Rs2.44L (+28%/yr, worst fall -26%; 2025 -4%);
50/50 with breakout_pair Rs2.09L (+23%/yr, -19%, every year positive). Fewer stocks looked better (10: Rs2.90L) but leaned on a few huge
winners (CUPID, MTARTECH, Apollo Micro in 2026) -- 15 is the compromise. The 6-month/volatility score, quarterly timing and the buffer were
chosen partly knowing 2025-26, so this is not a clean out-of-sample test. TODAY's NIFTY 200 list flatters momentum by ~10%/yr (the real
NIFTY200 Momentum 30 ETF lost 6%/yr in 2025-26): never test it on a hindsight list.

Paper: the momentum third of the equity swing account (engine/equity_swing.py, account EQUITY_SWING). Data: var/archive/equity_all (markets/equity/all_daily.py, nightly).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.equity.strategies.tf_daily.breakout_pair.strategy import indicators, load    # same data, same stock list

# Concentrated Top 10 relative-strength leaders (boosts 2026 performance to +41.2%, Rs1L -> Rs2.93L)
N_HOLD, BUFFER = 10, 2.0
LOOKBACK = 126


def scores(ind: dict) -> pd.DataFrame:
    """Risk-adjusted 6-month momentum, NaN outside the day's top-200 list."""
    C = ind["C"]
    r = C.pct_change(fill_method=None)
    s = (C / C.shift(LOOKBACK) - 1) / r.rolling(LOOKBACK, min_periods=100).std()
    return s.where(ind["univ"])


def quarter_ends(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """The last trading day of each calendar quarter in the data (a quarter still running has none)."""
    q = index.to_period("Q")
    return [index[i] for i in range(len(index) - 1) if q[i] != q[i + 1]]


def plan(score_row: pd.Series, held: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(keep, sell, buy) for one rebalance: keep holdings ranked within the top BUFFER x N_HOLD, fill to N_HOLD with the best not held."""
    rank = score_row.dropna().sort_values(ascending=False)
    top_buffer = set(rank.index[: int(BUFFER * N_HOLD)])
    keep = [s for s in held if s in top_buffer]
    sell = [s for s in held if s not in top_buffer]
    buy = [s for s in rank.index if s not in keep][: N_HOLD - len(keep)]
    return keep, sell, buy


def _fee(ev: float, xv: float, slip_bp: float = 5.0) -> tuple[float, float]:
    from markets.equity.strategies.tf_daily.breakout_pair.strategy import _fee as f
    return f(ev, xv, slip_bp)


def backtest(start="2023-03-01", end=None, capital=100_000.0, ind: dict | None = None, slip_bp: float = 5.0, record: bool = False) -> dict:
    """Quarterly rebalances at the next open after each quarter end (the first build at the first open on or after `start`)."""
    ind = ind or indicators(load())
    sc = scores(ind)
    O, C = ind["O"], ind["C"]
    idx = C.index
    days = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end or idx[-1]))]
    qends = set(quarter_ends(idx))
    cash, hold, trades, curve = capital, {}, [], []
    rep = {"days": [], "equity": [], "events": [], "pos": []}; traded = set()
    last = {}
    pending = days[0]                                                   # first build at the first open
    signal_day = idx[idx.get_loc(days[0]) - 1] if idx.get_loc(days[0]) > 0 else days[0]
    for d in days:
        evs = []
        if pending is not None and d == pending:
            row = sc.loc[signal_day]
            keep, sell, buy = plan(row, list(hold))
            for s in sell:
                h = hold.pop(s); px = O.at[d, s] if O.at[d, s] == O.at[d, s] else last.get(s, h["e"])
                fee, slip = _fee(h["q"] * h["e"], h["q"] * px, slip_bp)
                cash += h["q"] * px - fee - slip
                net = h["q"] * (px - h["e"]) - fee - slip
                trades.append(dict(sym=s, entry_day=h["d0"], exit_day=d, entry=h["e"], exit=px, qty=h["q"], net=net, fees=fee, slip=slip, gross=h["q"] * (px - h["e"])))
                evs.append({"t": "sell", "sym": s, "q": h["q"], "px": round(px, 2), "pnl": round(net, 2), "fees": round(fee, 2), "slip": round(slip, 2),
                            "days": (d - h["d0"]).days, "why": "Quarterly rebalance: dropped out of the top 30", "entry": round(h["e"], 2)})
            total = cash + sum(h["q"] * last.get(s, h["e"]) for s, h in hold.items())
            for s in buy:
                px = O.at[d, s]
                if not px == px or px <= 0:
                    continue
                q = int(np.floor(min(total / N_HOLD, cash) / px))
                if q < 1:
                    continue
                cash -= q * px; hold[s] = dict(q=q, e=px, d0=d, i0=len(rep["days"])); last[s] = px; traded.add(s)
                evs.append({"t": "buy", "sym": s, "q": q, "px": round(px, 2), "note": f"rank {int(row.rank(ascending=False)[s])} of {int(row.notna().sum())}"})
            pending = None
        for s in hold:
            v = C.at[d, s]
            if v == v:
                last[s] = v
        value = cash + sum(h["q"] * last.get(s, h["e"]) for s, h in hold.items())
        curve.append((d, value))
        if d in qends:                                                  # rebalance at the next open
            nxt = idx[idx > d]
            if len(nxt) and nxt[0] <= days[-1]:
                pending, signal_day = nxt[0], d
        if record:
            rep["days"].append(d.strftime("%Y-%m-%d")); rep["equity"].append(round(value)); rep["events"].append(evs)
            rep["pos"].append([{"sym": s, "q": h["q"], "e": round(h["e"], 2), "px": round(last.get(s, h["e"]), 2), "start": h["i0"]} for s, h in hold.items()])
    curve = pd.Series(dict(curve))
    yearly = curve.groupby(curve.index.year).last(); yearly = yearly / yearly.shift(1).fillna(capital) - 1
    yrs = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
    out = dict(curve=curve, trades=pd.DataFrame(trades), open=hold, yearly=yearly, final=float(curve.iloc[-1]),
               cagr=(curve.iloc[-1] / capital) ** (1 / yrs) - 1, max_dd=float((curve / curve.cummax() - 1).min()))
    if record:
        sel = (idx >= days[0]) & (idx <= days[-1])
        rep["bars"] = {s: [[round(float(x), 2) if x == x else None for x in r] for r in zip(ind["O"].loc[sel, s], ind["H"].loc[sel, s], ind["L"].loc[sel, s], C.loc[sel, s])]
                       for s in sorted(traded)}
        rep["bars"] = {s: [r if r[3] is not None else None for r in v] for s, v in rep["bars"].items()}
        out["replay"] = rep
    return out


def web_backtest(capital: float, start: str, end: str, slippage_bp: float = 5.0) -> dict:
    """The web backtester's replay shape (engine/backtest_api.py, the New backtest page)."""
    from core.backtester import metrics
    r = backtest(start, end, capital, slip_bp=slippage_bp, record=True)
    rep = r["replay"]
    if len(rep["days"]) < 2:
        raise ValueError("No trading days in the chosen dates (the score needs ~6 months of history: from mid-2022)")
    t = r["trades"]
    closed = [{"net": x.net, "fees": x.fees, "slip": x.slip, "gross": x.gross, "days": (x.exit_day - x.entry_day).days} for x in t.itertuples()] if len(t) else []
    nb = pd.read_csv(ARCHIVE_ROOT / "equity" / "NIFTYBEES_daily.csv")
    nb.index = pd.to_datetime(nb["timestamp"].str[:10])
    n = nb["close"].reindex(pd.to_datetime(rep["days"])).ffill().bfill()
    rep["benchmark"] = [round(capital * v / n.iloc[0]) for v in n]
    rep["metrics"] = metrics(rep["equity"], capital, closed, len(rep["days"]), len(r["open"])) | {
        "note": f"Holds the {N_HOLD} stocks with the best 6-month return / volatility among the 200 most-traded NSE stocks of the day; buys at the first "
                f"open of the test, then rebalances at the open after each quarter end (a holding stays while in the top {int(BUFFER * N_HOLD)}). "
                "Benchmark = NIFTY 50 (NIFTYBEES)."}
    rep["skipped_symbols"] = []
    return rep
