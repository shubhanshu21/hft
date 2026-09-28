"""Slow-momentum ensemble for BTC / ETH / SOL (spot, long or flat) -- the logic shared by the research studies and the paper trader.

Validated in markets/crypto/experiments/{momentum_study,ensemble_study}.py (docs/CRYPTO_MOMENTUM.md): three independent long/flat signals on HOURLY bars, averaged, then scaled to a target volatility:
    EMA 20/50 days crossover  +  Donchian 20-day breakout / 10-day exit  +  time-series momentum over 90 days   ->  0, 1/3, 2/3 or 1 of the size
    size = min(1, 40% / annual volatility of the previous 30 days)   (spot, so never more than 1x)
Decisions use CLOSED bars only and take effect at the close (the backtests hold from the next bar).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BARS_PER_DAY = {"1h": 24, "15m": 96}
COST_SIDE = {"BTCUSDT": 0.0012, "ETHUSDT": 0.0012, "SOLUSDT": 0.0015}          # taker fee 0.10% + slippage, charged on every unit of position change
TARGET_VOL = 0.40
PARTS = (("ema", (20, 50)), ("donchian", 20), ("tsmom", 90))
WARMUP_DAYS = 250                                                              # history the indicators need to have converged (EMA 50 days = 1,200 bars, 90-day momentum = 2,160 bars)


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def signal(df: pd.DataFrame, kind: str, param, bpd: int) -> np.ndarray:
    """+1 / 0 (long / flat) decided at each bar's close."""
    c, h, l = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    n = len(c)
    if kind == "tsmom":
        L = int(param * bpd)
        s = np.zeros(n)
        s[L:] = (c[L:] > c[:-L]).astype(float)
        return s
    if kind == "ema":
        fast, slow = param
        return (ema(c, fast * bpd) > ema(c, slow * bpd)).astype(float)
    if kind == "donchian":
        nb, xb = int(param * bpd), int(param / 2 * bpd)
        hi = pd.Series(h).shift(1).rolling(nb).max().to_numpy()
        lo = pd.Series(l).shift(1).rolling(xb).min().to_numpy()
        s, on = np.zeros(n), 0.0
        for i in range(n):
            if on == 0.0 and c[i] > hi[i]:
                on = 1.0
            elif on == 1.0 and c[i] < lo[i]:
                on = 0.0
            s[i] = on
        return s
    raise ValueError(kind)


def vol_scale(df: pd.DataFrame, bpd: int) -> np.ndarray:
    r = np.log(df["close"]).diff()
    vol = r.rolling(30 * bpd).std() * np.sqrt(365 * bpd)
    return np.minimum(1.0, TARGET_VOL / vol).fillna(0.0).to_numpy()


def components(df: pd.DataFrame, bpd: int = 24) -> dict[str, float]:
    """The last bar's three signals and the volatility scale (for logging and the dashboard)."""
    out = {f"{k}{'' if isinstance(p, tuple) else p}" if k != "ema" else "ema20/50": float(signal(df, k, p, bpd)[-1]) for k, p in PARTS}
    out["vol_scale"] = float(vol_scale(df, bpd)[-1])
    return out


def ensemble_position(df: pd.DataFrame, bpd: int = 24) -> np.ndarray:
    """Target size in [0, 1] at every bar's close."""
    return np.mean([signal(df, k, p, bpd) for k, p in PARTS], axis=0) * vol_scale(df, bpd)
