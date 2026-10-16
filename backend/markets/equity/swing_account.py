"""Backtest of the whole equity swing account (engine/equity_swing.py): breakout_pair, momentum_portfolio and earnings_drift, one third of
the money each, added together. Each part runs on its own third (the live account also skips a stock another part already holds; that
rare overlap is not modelled here). Used by the New backtest page as "equity_swing"."""
from __future__ import annotations

import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.equity.strategies.tf_daily.breakout_pair import strategy as BP
from markets.equity.strategies.tf_daily.earnings_drift import strategy as ED
from markets.equity.strategies.tf_quarterly.momentum_portfolio import strategy as MP

PARTS = (("breakout", BP), ("momentum", MP), ("earnings", ED))


def web_backtest(capital: float, start: str, end: str, slippage_bp: float = 5.0) -> dict:
    from core.backtester import metrics
    ind = BP.indicators(BP.load())
    runs = {name: mod.backtest(start, end, capital / 3, ind=ind, slip_bp=slippage_bp, record=True) for name, mod in PARTS}
    reps = {k: r["replay"] for k, r in runs.items()}
    days = reps["breakout"]["days"]
    if len(days) < 2:
        raise ValueError("No trading days in the chosen dates (signals need ~6 months of history: from mid-2022)")
    rep = {"days": days, "equity": [sum(reps[k]["equity"][i] for k in reps) for i in range(len(days))],
           "events": [[{**e, "note": f"{k}: {e.get('note', '')}".strip(": ")} if e["t"] == "buy" else {**e, "why": f"{k}: {e['why']}"}
                       for k in reps for e in reps[k]["events"][i]] for i in range(len(days))],
           "pos": [[p for k in reps for p in reps[k]["pos"][i]] for i in range(len(days))],
           "bars": {s: v for k in reps for s, v in reps[k]["bars"].items()}}
    closed = []
    for r in runs.values():
        t = r["trades"]
        closed += [{"net": x.net, "fees": x.fees, "slip": x.slip, "gross": x.gross, "days": (x.exit_day - x.entry_day).days} for x in t.itertuples()] if len(t) else []
    nb = pd.read_csv(ARCHIVE_ROOT / "equity" / "NIFTYBEES_daily.csv")
    nb.index = pd.to_datetime(nb["timestamp"].str[:10])
    n = nb["close"].reindex(pd.to_datetime(days)).ffill().bfill()
    rep["benchmark"] = [round(capital * v / n.iloc[0]) for v in n]
    part = ", ".join(f"{k} {r['final'] - capital / 3:+,.0f}" for k, r in runs.items())
    rep["metrics"] = metrics(rep["equity"], capital, closed, len(days), sum(len(r["open"]) for r in runs.values())) | {
        "note": f"One third of the money each: breakout pair, momentum (15 stocks, quarterly), earnings drift. Profit by part (Rs): {part}. "
                "Stocks: the 200 most-traded NSE stocks of each day. Benchmark = NIFTY 50 (NIFTYBEES)."}
    rep["skipped_symbols"] = []
    return rep
