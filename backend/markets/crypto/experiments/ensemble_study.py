"""Robustness of the slow-momentum ENSEMBLE for BTC / ETH / SOL: per asset, per calendar year, and against buy-and-hold.  (docs/CRYPTO_MOMENTUM.md)

    python3 -m markets.crypto.experiments.ensemble_study

Ensemble = the average of three independent long/flat signals that each passed the held-out test in momentum_study.py -- EMA 20/50 days, Donchian 20/10 days, TSMOM 90 days -- so the position is 0, 1/3, 2/3 or 1 of the
asset's volatility-targeted size (40% annual vol from the previous 30 days, never above 1x: spot, no leverage). Hourly bars; charged 0.12% (BTC, ETH) / 0.15% (SOL) per unit of position change.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from markets.crypto.experiments.momentum_study import SYMBOLS, TEST_START, TRAIN_END, load, stats
from markets.crypto.momentum import COST_SIDE, ensemble_position as _ensemble

BPD = 24
def ensemble_position(df: pd.DataFrame):
    return _ensemble(df, BPD)


def returns(df: pd.DataFrame, pos: np.ndarray, cost: float) -> pd.Series:
    p = pd.Series(pos, index=df.index).shift(1).fillna(0.0)
    return p * df["close"].pct_change().fillna(0.0) - p.diff().abs().fillna(0.0) * cost


def yearly(r: pd.Series) -> dict:
    return {y: ((1 + g).prod() - 1) * 100 for y, g in r.groupby(r.index.year)}


def main() -> int:
    data = {s: load(s, "1h") for s in SYMBOLS}
    per_asset = {s: returns(data[s], ensemble_position(data[s]), COST_SIDE[s]) for s in SYMBOLS}
    bh = {s: data[s]["close"].pct_change().fillna(0.0) for s in SYMBOLS}
    port = pd.concat(per_asset.values(), axis=1).mean(axis=1)
    bh_port = pd.concat(bh.values(), axis=1).mean(axis=1)
    print("Ensemble (EMA 20/50 + Donchian 20/10 + TSMOM 90, vol-targeted, spot long/flat), hourly bars\n")
    print(f"{'':10s} | {'TRAIN 2020-08..2023: CAGR Sharpe maxDD':>40s} | {'TEST 2024..now: CAGR Sharpe maxDD':>36s} || buy&hold TRAIN / TEST (CAGR Sharpe maxDD)")
    rows = [(s, per_asset[s], bh[s]) for s in SYMBOLS] + [("PORTFOLIO", port, bh_port)]
    for name, r, b in rows:
        r = r.loc[r.index[0]:]
        tr, te, btr, bte = stats(r.loc[:TRAIN_END], BPD), stats(r.loc[TEST_START:], BPD), stats(b.loc[:TRAIN_END], BPD), stats(b.loc[TEST_START:], BPD)
        print(f"{name:10s} | {tr['cagr']:+8.0f}% {tr['sharpe']:+6.2f} {tr['dd']:6.0f}% | {te['cagr']:+8.0f}% {te['sharpe']:+6.2f} {te['dd']:6.0f}% || {btr['cagr']:+5.0f}% {btr['sharpe']:+5.2f} {btr['dd']:3.0f}%  /  {bte['cagr']:+5.0f}% {bte['sharpe']:+5.2f} {bte['dd']:3.0f}%")
    print("\nBy calendar year (return %): ensemble portfolio vs buy-and-hold portfolio")
    ye, yb = yearly(port), yearly(bh_port)
    for y in sorted(ye):
        print(f"   {y}: ensemble {ye[y]:+7.1f}%   buy&hold {yb[y]:+7.1f}%   {'ensemble better' if ye[y] > yb[y] else 'buy&hold better'}")
    ex = port.loc[TEST_START:]
    pos_now = {s: float(ensemble_position(data[s])[-1]) for s in SYMBOLS}
    print(f"\nPosition the ensemble would hold right now (fraction of each asset's equal share): " + ", ".join(f"{s[:3]} {v:.2f}" for s, v in pos_now.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
