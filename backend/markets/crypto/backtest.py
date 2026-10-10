"""Backtest of the LIVE crypto router (markets/crypto/strategies/tf_1hour/router.py + the trader's rebalance band + its costs and funding).  Run:  python3 -m markets.crypto.backtest

Same signals as engine/crypto_paper.py: BTC regime picks long spot (bull / sideways) or short perpetuals (bear); the position of each coin moves only when the target differs by >= 10% of its share (or the target is zero);
spot costs 0.12% (SOL 0.15%) and perp costs 0.09% per side on the leg that changes; shorts earn / pay the perpetual funding every 8 hours. Equal thirds, rebalanced each bar (the live trader rebalances inside its band).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.crypto.strategies.tf_1hour import router
from markets.crypto.experiments.momentum_study import SYMBOLS, load
from markets.crypto.strategies.tf_1hour.momentum import COST_SIDE, ensemble_position

PERP_SIDE = 0.0009
BAND = 0.10
TRAIN_END, TEST_START = "2023-12-31", "2024-01-01"
BPY = 365 * 24


def banded(target: np.ndarray, band: float = BAND) -> np.ndarray:
    cur, out = 0.0, np.zeros(len(target))
    for i, t in enumerate(target):
        if (t == 0.0 and cur != 0.0) or abs(t - cur) >= band:
            cur = t
        out[i] = cur
    return out


def funding(sym: str, idx) -> pd.Series:
    f = pd.read_csv(ARCHIVE_ROOT / "crypto" / f"{sym}_funding.csv", parse_dates=["timestamp"]).set_index("timestamp")["rate"]
    return f.reindex(idx).fillna(0.0)


def coin_returns(sym: str, df: pd.DataFrame, target: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Hourly return on the coin's share, and the position (signed, fraction of the share) actually held over each bar."""
    pos = pd.Series(banded(target.to_numpy()), index=df.index).shift(1).fillna(0.0)
    prev = pos.shift(1).fillna(0.0)
    spot = (pos.clip(lower=0) - prev.clip(lower=0)).abs() * COST_SIDE[sym]
    perp = ((-pos).clip(lower=0) - (-prev).clip(lower=0)).abs() * PERP_SIDE
    r = pos * df["close"].pct_change().fillna(0.0) - spot - perp + (-pos.clip(upper=0)) * funding(sym, df.index)
    return r, pos


def stats(r: pd.Series) -> dict:
    eq = (1 + r).cumprod()
    yrs = len(r) / BPY
    return {"cagr": (eq.iloc[-1] ** (1 / yrs) - 1) * 100, "sharpe": r.mean() / r.std() * np.sqrt(BPY) if r.std() > 0 else 0.0, "dd": ((eq.cummax() - eq) / eq.cummax()).max() * 100, "total": (eq.iloc[-1] - 1) * 100}


def run(router_weight: float = 1.0) -> dict:
    data = {s: load(s, "1h") for s in SYMBOLS}
    targets = router.target_series(data) if router_weight >= 1.0 else router.blend_series(data, router_weight)
    rets, poss = {}, {}
    for s in SYMBOLS:
        rets[s], poss[s] = coin_returns(s, data[s], targets[s])
    trend = {s: coin_returns(s, data[s], pd.Series(ensemble_position(data[s], 24), index=data[s].index))[0] for s in SYMBOLS}     # long/flat trend only (the earlier deployment)
    start = max(d.index[0] for d in data.values()) + pd.Timedelta(days=200)
    port = pd.concat(rets.values(), axis=1).mean(axis=1).loc[start:]
    tport = pd.concat(trend.values(), axis=1).mean(axis=1).loc[start:]
    bh = pd.concat([data[s]["close"].pct_change().fillna(0.0) for s in SYMBOLS], axis=1).mean(axis=1).loc[start:]
    reg = router.regimes(data["BTCUSDT"]).loc[start:]
    return {"port": port, "trend": tport, "bh": bh, "rets": {s: r.loc[start:] for s, r in rets.items()}, "pos": {s: p.loc[start:] for s, p in poss.items()}, "reg": reg, "start": start}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--router-weight", type=float, default=0.5, help="share of the capital on the regime router (the rest on the trend ensemble); 1.0 = the router alone, 0.0 = trend alone")
    a = ap.parse_args(argv)
    R = run(a.router_weight)
    port, tport, bh, reg = R["port"], R["trend"], R["bh"], R["reg"]
    label = "BLEND" if 0 < a.router_weight < 1 else ("ROUTER" if a.router_weight >= 1 else "TREND")
    print(f"CRYPTO {label} ({a.router_weight:.0%} router / {1 - a.router_weight:.0%} trend) -- backtest {port.index[0]:%Y-%m-%d} to {port.index[-1]:%Y-%m-%d} (BTC, ETH, SOL equal thirds; costs + funding included)\n")
    print(f"{'':28s} | {'TRAIN 2021-03..2023':>30s} | {'TEST 2024..now':>30s} | {'WHOLE PERIOD':>30s}")
    print(f"{'':28s} | {'CAGR   Sharpe  maxDD  total':>30s} | {'CAGR   Sharpe  maxDD  total':>30s} | {'CAGR   Sharpe  maxDD  total':>30s}")
    for name, r in ((f"{label} (live logic)", port), ("trend only (long/flat)", tport), ("buy & hold", bh)):
        cells = []
        for w in (r.loc[:TRAIN_END], r.loc[TEST_START:], r):
            s = stats(w)
            cells.append(f"{s['cagr']:+5.0f}% {s['sharpe']:+6.2f} {s['dd']:5.0f}% {s['total']:+7.0f}%")
        print(f"{name:28s} | {cells[0]:>30s} | {cells[1]:>30s} | {cells[2]:>30s}")
    print("\nBY CALENDAR YEAR (10,000 USDT compounding from the first day)")
    print(f"   {'year':6s} | {label.lower() + ' start':>12s} {'end':>9s} {'gain':>8s} {'return':>8s} {'worst drop':>10s} | {'trend only':>10s} | {'buy&hold':>9s}")
    v = 10000.0
    for y in sorted(set(port.index.year)):
        g = port[port.index.year == y]
        eq = (1 + g).cumprod()
        dd = ((eq.cummax() - eq) / eq.cummax()).max() * 100
        t = ((1 + tport[tport.index.year == y]).prod() - 1) * 100
        b = ((1 + bh[bh.index.year == y]).prod() - 1) * 100
        print(f"   {y:<6d} | {v:12,.0f} {v * eq.iloc[-1]:9,.0f} {v * (eq.iloc[-1] - 1):+8,.0f} {100 * (eq.iloc[-1] - 1):+7.1f}% {dd:9.0f}% | {t:+9.1f}% | {b:+8.1f}%")
        v *= eq.iloc[-1]
    print(f"   10,000 USDT -> {v:,.0f} USDT ({(v / 10000 - 1) * 100:+,.0f}%)   |   500 USDT -> {v / 20:,.0f} USDT")
    print("\nBY QUARTER, held-out test period (return %)")
    print("   " + "  ".join(f"{str(q)}:{((1 + g).prod() - 1) * 100:+.1f}" for q, g in port.loc[TEST_START:].groupby(port.loc[TEST_START:].index.to_period('Q'))))
    print("   " + "  ".join(f"{'bh ' + str(q)[2:]}:{((1 + g).prod() - 1) * 100:+.0f}" for q, g in bh.loc[TEST_START:].groupby(bh.loc[TEST_START:].index.to_period('Q'))))
    print("\nBY REGIME (BTC label): share of time, annualised return / Sharpe of the strategy vs buy & hold")
    for k in ("bull", "bear", "sideways"):
        m = reg == k
        x, b = port[m], bh[m]
        print(f"   {k:8s} {m.mean() * 100:4.0f}% of the time | strategy {x.mean() * BPY * 100:+6.0f}% {x.mean() / x.std() * np.sqrt(BPY):+5.2f} | buy&hold {b.mean() * BPY * 100:+6.0f}% {b.mean() / b.std() * np.sqrt(BPY):+5.2f}")
    print("\nPER COIN (whole period, the coin's share alone) and activity")
    for s in SYMBOLS:
        r, p = R["rets"][s], R["pos"][s]
        st = stats(r)
        chg = (p.diff().abs() > 1e-9).sum() / (len(p) / BPY)
        print(f"   {s:8s} CAGR {st['cagr']:+5.0f}%  Sharpe {st['sharpe']:+5.2f}  maxDD {st['dd']:4.0f}% | long {(p > 0.05).mean() * 100:3.0f}% short {(p < -0.05).mean() * 100:3.0f}% flat {(p.abs() <= 0.05).mean() * 100:3.0f}% of the time | {chg:4.0f} position changes a year")
    last = port.loc[port.index[-1] - pd.Timedelta(days=365):]
    print(f"\nLAST 12 MONTHS: strategy {((1 + last).prod() - 1) * 100:+.1f}%  |  trend only {((1 + tport.loc[last.index]).prod() - 1) * 100:+.1f}%  |  buy & hold {((1 + bh.loc[last.index]).prod() - 1) * 100:+.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
