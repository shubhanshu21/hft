"""Long-term momentum on BTC, ETH and SOL from 15-minute and 1-hour Binance bars, with a held-out test period.  (docs/CRYPTO_MOMENTUM.md)

    python3 -m markets.crypto.experiments.momentum_study

CLAUDE.md: crypto scalping was removed earlier for lacking an out-of-sample edge; this tests a different idea -- slow trend following (lookbacks of days to weeks) executed on 15m / 1h bars -- with the same discipline:
variants fixed in advance from the literature (no tuning), TRAIN 2020-08..2023-12 (the 2021 bull run and the 2022 bear market), TEST 2024-01..now untouched, costs charged on every position change, benchmarked
against buy-and-hold. Long/flat models SPOT trading (no shorting, no leverage); long/short models perpetual futures (fees + slippage, funding NOT charged -- it would slightly hurt longs and help shorts).

Signals (lookbacks in DAYS, converted to bars):  TSMOM = sign of the past-L-day return; DONCHIAN n = long above the n-day high, out below the n/2-day low; EMA fast/slow crossover.
Sizing: plain (position 1) or VOL-TARGETED (scale to 40% annual volatility from the previous 30 days, never above 1x).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.crypto.strategies.momentum import BARS_PER_DAY, COST_SIDE, TARGET_VOL, ema, signal, vol_scale     # noqa: F401 (shared with the paper trader)

ARCHIVE = ARCHIVE_ROOT / "crypto"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
TRAIN_END, TEST_START = "2023-12-31", "2024-01-01"


def load(sym: str, interval: str) -> pd.DataFrame:
    return pd.read_csv(ARCHIVE / f"{sym}_{interval}.csv", parse_dates=["timestamp"]).set_index("timestamp")


def bar_returns(df: pd.DataFrame, sig: np.ndarray, bpd: int, cost_side: float, sized: bool, long_short: bool) -> pd.Series:
    pos = (2 * sig - 1) if long_short else sig
    if sized:
        pos = pos * vol_scale(df, bpd)
    pos = pd.Series(pos, index=df.index).shift(1).fillna(0.0)                     # decided at the close, held from the next bar
    r = df["close"].pct_change().fillna(0.0)
    return pos * r - pos.diff().abs().fillna(0.0) * cost_side


def stats(r: pd.Series, bpd: int) -> dict:
    if len(r) < bpd * 30:
        return {"cagr": float("nan"), "sharpe": float("nan"), "dd": float("nan"), "flips": 0}
    eq = (1 + r).cumprod()
    yrs = len(r) / (365 * bpd)
    return {"cagr": (eq.iloc[-1] ** (1 / yrs) - 1) * 100, "sharpe": r.mean() / r.std() * np.sqrt(365 * bpd) if r.std() > 0 else 0.0, "dd": ((eq.cummax() - eq) / eq.cummax()).max() * 100}


def variants():
    for L in (7, 14, 30, 60, 90):
        yield f"TSMOM {L}d", "tsmom", L
    for n in (20, 55):
        yield f"Donchian {n}d/{n // 2}d", "donchian", n
    for f, s in ((10, 30), (20, 50), (50, 200)):
        yield f"EMA {f}/{s}d", "ema", (f, s)


def window(r: pd.Series, which: str) -> pd.Series:
    return r.loc[:TRAIN_END] if which == "train" else r.loc[TEST_START:]


def main() -> int:
    for interval in ("1h", "15m"):
        bpd = BARS_PER_DAY[interval]
        data = {s: load(s, interval) for s in SYMBOLS}
        idx = data[SYMBOLS[0]].index
        for s in SYMBOLS[1:]:
            idx = idx.intersection(data[s].index)
        # equal-weight buy-and-hold of the three (all assets available from 2020-08-11)
        bh = pd.concat([data[s]["close"].pct_change().fillna(0.0) for s in SYMBOLS], axis=1).mean(axis=1)
        bh = bh.loc[idx[0]:]
        print(f"\n################ {interval} bars ({bpd}/day), portfolio = equal weight of BTC, ETH, SOL")
        print(f"   {'variant':22s} {'mode':11s} {'sizing':6s} | {'TRAIN 2020-08..2023: CAGR  Sharpe  maxDD':>44s} | {'TEST 2024..now: CAGR  Sharpe  maxDD':>38s} | flips/yr | verdict")
        b_tr, b_te = stats(window(bh, "train"), bpd), stats(window(bh, "test"), bpd)
        print(f"   {'BUY & HOLD (equal wt)':22s} {'':11s} {'':6s} | {b_tr['cagr']:+9.0f}% {b_tr['sharpe']:+7.2f} {b_tr['dd']:6.0f}% | {b_te['cagr']:+9.0f}% {b_te['sharpe']:+7.2f} {b_te['dd']:6.0f}% |")
        best = []
        for name, kind, param in variants():
            for ls in (False, True):
                for sized in (False, True):
                    rets = []
                    flips = 0
                    for s in SYMBOLS:
                        sig = signal(data[s], kind, param, bpd)
                        flips += int(np.abs(np.diff(sig)).sum())
                        rets.append(bar_returns(data[s], sig, bpd, COST_SIDE[s], sized, ls))
                    port = pd.concat(rets, axis=1).mean(axis=1).loc[idx[0]:]
                    tr, te = stats(window(port, "train"), bpd), stats(window(port, "test"), bpd)
                    fpy = flips / len(SYMBOLS) / (len(port) / (365 * bpd))
                    ok = te["sharpe"] > b_te["sharpe"] and te["cagr"] > 0 and tr["sharpe"] > 0 and te["dd"] < b_te["dd"]
                    print(f"   {name:22s} {'long/short' if ls else 'long/flat':11s} {'vol' if sized else 'plain':6s} | {tr['cagr']:+9.0f}% {tr['sharpe']:+7.2f} {tr['dd']:6.0f}% | {te['cagr']:+9.0f}% {te['sharpe']:+7.2f} {te['dd']:6.0f}% | {fpy:8.0f} | {'PASS' if ok else '-'}")
                    if ok:
                        best.append((te["sharpe"], name, ls, sized))
        print(f"   passing variants ({interval}): {len(best)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
