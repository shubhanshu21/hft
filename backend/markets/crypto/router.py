"""Regime router for BTC / ETH / SOL: the same logic the research validated (markets/crypto/experiments/all_weather_study.py, docs/CRYPTO_ALL_WEATHER.md).

Regime (from BTC, at every hourly bar, past data only):  BULL = close above the 200-day EMA and the 50-day EMA above the 200-day EMA;  BEAR = both below;  SIDEWAYS = everything else.
Target for each coin (fraction of that coin's equal share; positive = long spot, negative = short perpetual):
    BTC regime BULL or SIDEWAYS -> +the slow-momentum ensemble size (EMA 20/50d + Donchian 20/10d + 90-day momentum, volatility-targeted, never above 1)
    BTC regime BEAR             -> -(1 - fraction of those three trend signals that are on) x volatility scale, only for a coin whose OWN regime is bear (else flat)
Long positions are spot (no leverage); shorts are perpetual futures (funding received / paid, 0.09% per side in the research).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from markets.crypto.momentum import PARTS, ensemble_position, signal, vol_scale

BPD = 24


def regimes(df: pd.DataFrame) -> pd.Series:
    c = df["close"]
    e50, e200 = c.ewm(span=50 * BPD, adjust=False).mean(), c.ewm(span=200 * BPD, adjust=False).mean()
    out = pd.Series("sideways", index=df.index)
    out[(c > e200) & (e50 > e200)] = "bull"
    out[(c < e200) & (e50 < e200)] = "bear"
    out.iloc[: 200 * BPD] = "warmup"
    return out


def short_target(df: pd.DataFrame) -> np.ndarray:
    """Negative size in a coin's OWN bear regime, larger the fewer trend signals are on; zero otherwise."""
    sigs = np.mean([signal(df, k, p, BPD) for k, p in PARTS], axis=0)
    return -(1.0 - sigs) * vol_scale(df, BPD) * (regimes(df) == "bear").to_numpy()


def target_series(frames: dict[str, pd.DataFrame], anchor: str = "BTCUSDT") -> dict[str, pd.Series]:
    """Signed target size at every bar of each coin's own index (the routing uses the anchor coin's regime, forward-filled onto the coin's bars)."""
    reg = regimes(frames[anchor])
    out = {}
    for s, df in frames.items():
        long_t = pd.Series(ensemble_position(df, BPD), index=df.index)
        short_t = pd.Series(short_target(df), index=df.index)
        bear = (reg.reindex(df.index, method="ffill") == "bear")
        out[s] = long_t.where(~bear, short_t)
    return out


def blend_series(frames: dict[str, pd.DataFrame], router_weight: float = 0.5, anchor: str = "BTCUSDT") -> dict[str, pd.Series]:
    """`router_weight` of the capital follows the regime router, the rest the plain long/flat trend ensemble: the net signed target per coin is the weighted sum. The trend half keeps earning through a
    regime-detection miss (2023: the router was short into the rebound, +22% with the blend vs -1%); the router half earns in bear markets (2022: -3% with the blend vs -24% for trend alone)."""
    routed = target_series(frames, anchor)
    return {s: router_weight * routed[s] + (1.0 - router_weight) * pd.Series(ensemble_position(df, BPD), index=df.index) for s, df in frames.items()}


def current_targets(frames: dict[str, pd.DataFrame], anchor: str = "BTCUSDT", router_weight: float = 1.0) -> tuple[dict[str, float], str]:
    ts = target_series(frames, anchor) if router_weight >= 1.0 else blend_series(frames, router_weight, anchor)
    return {s: float(t.iloc[-1]) for s, t in ts.items()}, str(regimes(frames[anchor]).iloc[-1])
