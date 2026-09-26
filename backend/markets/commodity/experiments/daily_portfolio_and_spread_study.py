"""Slower ideas that suit a small account: a diversified DAILY trend-following portfolio and the gold-silver ratio.  (docs/COMMODITY_DAILY_AND_SPREAD.md)

    python3 -m markets.commodity.experiments.daily_portfolio_and_spread_study

Data: Yahoo daily global futures 2003-2026 (services/data/yahoo.py) as a proxy for the MCX contracts (no MCX premium / roll adjustment); train = 2003-2018, test = 2019-2026, as in docs/SWING_RESEARCH.md.

Part 1  DIVERSIFIED TREND FOLLOWING. Time-series momentum (sign of the past 63/126/252-day return, averaged) on gold, silver, crude, natural gas (the contracts one lot of which fits Rs100,000 of margin;
        copper needs Rs327k), each sized to 10% annual volatility (60-day realised vol, leverage capped at 3x) and combined equally. Signal at close, position from the NEXT day, cost 5 bps of the position change.
        The point: does diversification across the four make a stable, positive Sharpe where each one alone does not?
Part 2  GOLD-SILVER RATIO. Mean-reversion of log(gold/silver) around its rolling mean: enter when |z| > entry, exit at |z| < 0.5 or after 60 days; both legs equal notional, round-trip cost 15 bps of one leg's
        notional (both legs; real MCX cost measured at 5-8 bps per leg). Every (lookback, entry) cell is shown; nothing is picked after the fact.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from services.data.yahoo import fetch_daily

TRAIN_END, TEST_START = "2018-12-31", "2019-01-01"
TICKERS = {"GOLD": "GC=F", "SILVER": "SI=F", "CRUDE": "CL=F", "NATGAS": "NG=F"}
COST_BPS = 5.0
TARGET_VOL = 0.10


def load() -> dict[str, pd.Series]:
    return {k: fetch_daily(t)["close"].astype(float).dropna() for k, t in TICKERS.items()}


def tsm_returns(px: pd.Series) -> pd.Series:
    r = np.log(px).diff()
    sig = sum(np.sign(px / px.shift(n) - 1) for n in (63, 126, 252)) / 3.0
    vol = r.rolling(60).std() * np.sqrt(252)
    size = (TARGET_VOL / vol).clip(upper=3.0)
    pos = (sig * size).shift(1)                                      # decided at the close, held from the next day
    cost = pos.diff().abs() * COST_BPS / 1e4
    return (pos * r - cost).dropna()


def stats(r: pd.Series) -> dict:
    if len(r) < 30:
        return {"ann": float("nan"), "vol": float("nan"), "sharpe": float("nan"), "dd": float("nan")}
    eq = (1 + r).cumprod()
    return {"ann": r.mean() * 252 * 100, "vol": r.std() * np.sqrt(252) * 100, "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0.0, "dd": ((eq.cummax() - eq) / eq.cummax()).max() * 100}


def fmt(s: dict) -> str:
    return f"ann {s['ann']:+6.1f}%  vol {s['vol']:5.1f}%  Sharpe {s['sharpe']:+5.2f}  maxDD {s['dd']:5.1f}%"


def part1() -> None:
    px = load()
    rets = {k: tsm_returns(v) for k, v in px.items()}
    port = pd.concat(rets.values(), axis=1, join="inner").mean(axis=1)
    windows = {"ALL 2003-26": (None, None), "TRAIN 2003-18": (None, TRAIN_END), "TEST 2019-26": (TEST_START, None), "last 3y": ("2023-10-01", None)}
    print("PART 1  time-series momentum, vol-targeted 10% per instrument, equal-weight portfolio (unlevered percentages of capital)")
    for label, (a, b) in windows.items():
        print(f"  {label}")
        for k, r in list(rets.items()) + [("PORTFOLIO (4)", port)]:
            rr = r.loc[a:b] if a or b else r
            print(f"     {k:14s} {fmt(stats(rr))}")
    print("  portfolio by calendar year (annual return %, Sharpe):")
    yr = port.groupby(port.index.year).agg(lambda x: x.sum() * 100)
    sh = port.groupby(port.index.year).agg(lambda x: x.mean() / x.std() * np.sqrt(252) if x.std() > 0 else 0)
    print("     " + "  ".join(f"{y}:{yr[y]:+.0f}%({sh[y]:+.1f})" for y in yr.index if y >= 2010))
    pos_years = (yr[yr.index >= 2010] > 0).sum()
    print(f"     positive years since 2010: {pos_years} of {len(yr[yr.index >= 2010])}")


def part2() -> None:
    g, s = fetch_daily("GC=F")["close"].astype(float), fetch_daily("SI=F")["close"].astype(float)
    ratio = np.log(g / s).dropna()
    print("\nPART 2  gold-silver ratio mean reversion (per-trade return net of 15 bps; PF and t are per-trade)")
    print(f"   {'lookback':>8s} {'entry z':>7s} | {'TRAIN 2003-18: trades  avg%   PF     t':>44s} | {'TEST 2019-26: trades  avg%   PF     t':>42s}")
    for n in (60, 120, 250):
        z = (ratio - ratio.rolling(n).mean()) / ratio.rolling(n).std()
        for ez in (1.5, 2.0, 2.5):
            trades = []
            i, idx = n, ratio.index
            while i < len(ratio) - 1:
                zi = z.iloc[i]
                if abs(zi) >= ez:
                    d = -np.sign(zi)                                    # ratio too high -> short gold / long silver (the spread falls)
                    entry = ratio.iloc[i + 1]                           # executed the next day
                    j = i + 1
                    while j < len(ratio) - 1 and abs(z.iloc[j]) > 0.5 and j - i < 60:
                        j += 1
                    trades.append((idx[i + 1], d * (ratio.iloc[j] - entry) - 0.0015))
                    i = j + 1
                else:
                    i += 1
            df = pd.DataFrame(trades, columns=["d", "r"]).set_index("d")
            row = []
            for lo, hi in ((None, TRAIN_END), (TEST_START, None)):
                x = df.loc[lo:hi, "r"] if (lo or hi) else df["r"]
                if len(x) < 5:
                    row.append("      too few trades"); continue
                pf = x[x > 0].sum() / max(-x[x <= 0].sum(), 1e-9)
                t = x.mean() / (x.std(ddof=1) / np.sqrt(len(x))) if x.std() > 0 else 0
                row.append(f"{len(x):6d} {x.mean() * 100:+6.2f}% {pf:5.2f} {t:+5.1f}")
            print(f"   {n:8d} {ez:7.1f} | {row[0]:>44s} | {row[1]:>42s}")


if __name__ == "__main__":
    part1()
    part2()
