"""Would leverage have been liquidated?  Isolated-margin simulation of the crypto blend at 1x / 1.5x / 2x / 3x, using hourly HIGH and LOW.  (docs/CRYPTO_ALL_WEATHER.md)

    python3 -m markets.crypto.experiments.liquidation_study

Each coin's third of the money is its own isolated margin. A position of L x share (long or short perpetual) is liquidated when price moves against the entry by (1/L - 0.5% maintenance margin): at 3x that is a 32.8% move,
at 2x 49.5%, at 1.5x 66%. Liquidation takes the WHOLE margin of that coin's share (plus a 0.5% liquidation fee is ignored: it would only make this worse); the coin then waits for its next signal. The entry price is the
size-weighted average of the fills since the position opened. Hourly extremes are used, so a wick that reverses inside the hour still counts. Costs and funding as in leverage_study.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import markets.crypto.backtest as bt
from markets.crypto import router

MM = 0.005


def simulate_coin(sym: str, df: pd.DataFrame, target: np.ndarray, lev: float) -> tuple[np.ndarray, list]:
    """Returns the per-bar return on the coin's share (margin) and the list of liquidation events."""
    n = len(df)
    close, high, low = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    fund = bt.funding(sym, df.index).to_numpy()
    tgt = bt.banded(target * lev, band=0.10 * lev)
    ret = np.zeros(n)
    pos, entry, dead_until_flat = 0.0, 0.0, False
    events = []
    cost_side = bt.PERP_SIDE if lev > 1.0 else bt.COST_SIDE[sym]
    for i in range(1, n):
        want = tgt[i - 1]                                                   # decided at the previous close, held over bar i
        if dead_until_flat:
            if want == 0.0 or np.sign(want) != np.sign(pos):
                dead_until_flat = False
            else:
                want = 0.0
        # transaction at the previous close
        change = want - pos
        if abs(change) > 1e-9:
            ret[i] -= abs(change) * cost_side
            if pos == 0.0 or np.sign(want) != np.sign(pos):
                entry = close[i - 1]
            elif abs(want) > abs(pos):
                entry = (entry * abs(pos) + close[i - 1] * abs(change)) / abs(want)
            pos = want
        if pos == 0.0:
            continue
        # liquidation check on this bar's extremes (isolated margin: the whole share is lost)
        dist = 1.0 / max(abs(pos), 1e-9) - MM                                # fraction of price move that exhausts the margin of |pos| x share
        if pos > 0 and low[i] <= entry * (1 - dist):
            loss = (entry * (1 - dist) / close[i - 1] - 1.0) * pos                # move from the last close to the liquidation price, on the position
            ret[i] += loss
            events.append((df.index[i], sym, "long", abs(pos)))
            pos, dead_until_flat = 0.0, True
            continue
        if pos < 0 and high[i] >= entry * (1 + dist):
            loss = (entry * (1 + dist) / close[i - 1] - 1.0) * pos
            ret[i] += loss
            events.append((df.index[i], sym, "short", abs(pos)))
            pos, dead_until_flat = 0.0, True
            continue
        r = close[i] / close[i - 1] - 1.0
        f = fund[i]
        fcost = (max(pos - 1.0, 0.0) * f + min(pos, 0.0) * f) if lev > 1.0 else min(pos, 0.0) * f
        ret[i] += pos * r - fcost
    return ret, events


def main() -> int:
    data = {s: bt.load(s, "1h") for s in bt.SYMBOLS}
    targets = router.blend_series(data, 0.5)
    start = max(d.index[0] for d in data.values()) + pd.Timedelta(days=200)
    print(f"{'leverage':>8s} | {'CAGR':>6s} {'maxDD':>6s} {'$500 -> ':>10s} {'since 2025':>10s} | liquidations (whole 5.6 yrs): count | wipe-outs of a coin's share")
    for lev in (1.0, 1.5, 2.0, 3.0):
        rets, events = [], []
        for s in bt.SYMBOLS:
            r, ev = simulate_coin(s, data[s], targets[s].to_numpy(), lev)
            rets.append(pd.Series(r, index=data[s].index))
            events += ev
        port = pd.concat(rets, axis=1).mean(axis=1).loc[start:]
        eq = (1 + port).cumprod()
        yrs = len(port) / bt.BPY
        s25 = port.loc["2025-01-01":]
        ev = [e for e in events if e[0] >= start]
        print(f"{lev:7.1f}x | {(eq.iloc[-1] ** (1 / yrs) - 1) * 100:+5.0f}% {((eq.cummax() - eq) / eq.cummax()).max() * 100:5.0f}% {500 * eq.iloc[-1]:10,.0f} {((1 + s25).prod() - 1) * 100:+9.0f}% | {len(ev):3d} events", end="")
        if ev:
            print("  e.g. " + ", ".join(f"{e[0]:%Y-%m-%d} {e[1][:3]} {e[2]}" for e in ev[:6]) + (" ..." if len(ev) > 6 else ""))
        else:
            print("")
    print("\nLargest hourly and daily moves in the data (why the distance matters):")
    for s in bt.SYMBOLS:
        d = data[s]["close"]
        day = d.resample("1D").last().pct_change().dropna()
        wick = (data[s]["low"] / data[s]["close"].shift(1) - 1).min()
        print(f"   {s}: worst day {day.min() * 100:+.0f}% ({day.idxmin():%Y-%m-%d}), worst hourly low vs previous close {wick * 100:+.0f}%, worst 7-day close-to-close {(d.pct_change(24 * 7).min()) * 100:+.0f}%, "
              f"days worse than -10%: {int((day < -0.10).sum())}, worse than -20%: {int((day < -0.20).sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
