"""What do leverage and looser volatility sizing do to the crypto blend?  (docs/CRYPTO_ALL_WEATHER.md)

    python3 -m markets.crypto.experiments.leverage_study

Live blend = half router / half trend, sized to 40% annual volatility and never above 1x of each coin's share (no leverage). Here the SAME signals are re-run with (a) a higher volatility target and (b) leverage L
(every position multiplied by L; above 1x the notional is a perpetual future: 0.09% cost per side on all traded notional, and the part of a long above 1x pays the perpetual funding; shorts receive it).
Risk shown: max drawdown, worst calendar year, worst single day, and days the portfolio fell by more than the liquidation distance of an isolated-margin position (about 1/L of price).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import markets.crypto.backtest as bt
import markets.crypto.strategies.tf_1hour.momentum as mo
from markets.crypto.strategies.tf_1hour import router

BPY = bt.BPY


def run(target_vol: float, lev: float, data) -> pd.Series:
    old = mo.TARGET_VOL
    mo.TARGET_VOL = target_vol
    try:
        targets = router.blend_series(data, 0.5)
    finally:
        mo.TARGET_VOL = old
    rets = []
    for s in bt.SYMBOLS:
        df = data[s]
        pos = pd.Series(bt.banded(targets[s].to_numpy() * lev, band=0.10 * lev), index=df.index).shift(1).fillna(0.0)
        prev = pos.shift(1).fillna(0.0)
        r = df["close"].pct_change().fillna(0.0)
        fund = bt.funding(s, df.index)
        if lev > 1.0:
            cost = (pos - prev).abs() * bt.PERP_SIDE
            fcost = (pos.clip(lower=1.0) - 1.0) * fund - pos.clip(upper=0.0) * fund * -1.0        # longs above 1x pay funding; shorts receive it
            fcost = (pos.clip(lower=1.0) - 1.0) * fund + pos.clip(upper=0.0) * fund
        else:
            spot = (pos.clip(lower=0) - prev.clip(lower=0)).abs() * bt.COST_SIDE[s]
            perp = ((-pos).clip(lower=0) - (-prev).clip(lower=0)).abs() * bt.PERP_SIDE
            cost = spot + perp
            fcost = pos.clip(upper=0.0) * fund
        rets.append(pos * r - cost - fcost)
    start = max(d.index[0] for d in data.values()) + pd.Timedelta(days=200)
    return pd.concat(rets, axis=1).mean(axis=1).loc[start:]


def summarize(r: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    daily = (1 + r).groupby(r.index.date).prod() - 1
    yrs = [((1 + r[r.index.year == y]).prod() - 1) * 100 for y in sorted(set(r.index.year))]
    s25 = r.loc["2025-01-01":]
    return {"cagr": (eq.iloc[-1] ** (BPY / len(r)) - 1) * 100, "sharpe": r.mean() / r.std() * np.sqrt(BPY), "dd": ((eq.cummax() - eq) / eq.cummax()).max() * 100,
            "worst_year": min(yrs), "worst_day": daily.min() * 100, "since25": ((1 + s25).prod() - 1) * 100, "test": stats_test(r), "eq_min": eq.min()}


def stats_test(r: pd.Series) -> tuple[float, float]:
    t = r.loc[bt.TEST_START:]
    eq = (1 + t).cumprod()
    return (eq.iloc[-1] ** (BPY / len(t)) - 1) * 100, t.mean() / t.std() * np.sqrt(BPY)


def main() -> int:
    data = {s: bt.load(s, "1h") for s in bt.SYMBOLS}
    print(f"{'vol target':>10s} {'leverage':>8s} | {'CAGR':>6s} {'Sharpe':>6s} {'maxDD':>6s} {'worst yr':>8s} {'worst day':>9s} | {'TEST CAGR/Sharpe':>17s} | {'since 2025':>10s} {'$500 ->':>8s} | verdict")
    for tv in (0.40, 0.60, 0.80, 1.50):
        for lev in (1.0, 1.5, 2.0, 3.0):
            r = run(tv, lev, data)
            s = summarize(r)
            danger = "WIPED OUT (fell to %.0f%% of start)" % (s["eq_min"] * 100) if s["eq_min"] < 0.15 else ("very high risk" if s["dd"] > 70 else "")
            print(f"{tv * 100:9.0f}% {lev:7.1f}x | {s['cagr']:+5.0f}% {s['sharpe']:6.2f} {s['dd']:5.0f}% {s['worst_year']:+7.0f}% {s['worst_day']:+8.1f}% | {s['test'][0]:+6.0f}% / {s['test'][1]:4.2f}   | {s['since25']:+9.0f}% {500 * (1 + s['since25'] / 100):8,.0f} | {danger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
