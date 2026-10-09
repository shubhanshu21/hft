"""1-minute candles grouped by the 5-minute bar they belong to, so a backtest on 5-minute bars can settle WHERE inside a bar a stop or a
target was reached and at what price a stop really filled (core/exits.intrabar_exit, docs/FILL_MODEL_AUDIT.md).

A 5-minute bar only shows that the stop was crossed; the 1-minute path shows whether price jumped through it (USDINR 2026-07-03: a short's
95.81 stop, the next minute opened at 95.99) and which of stop and target came first when one bar reaches both."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


class MinuteBars:
    """`get(bar_start)` -> rows of (open, high, low) per minute in time order, or None when the 1-minute archive does not cover that bar."""

    def __init__(self, path: Path | None, bar_minutes: int = 5):
        self._bars: dict[int, np.ndarray] = {}
        if path is not None and not Path(path).exists() and Path(str(path) + ".gz").exists():
            path = Path(str(path) + ".gz")                         # engine/research_data.py stores long 1-minute histories compressed
        if path is None or not Path(path).exists():
            return
        df = pd.read_csv(path, usecols=["timestamp", "open", "high", "low"])
        df["ts"] = pd.to_datetime(df["timestamp"])
        df = df.drop_duplicates("ts").sort_values("ts")
        keys = pd.DatetimeIndex(df["ts"].dt.floor(f"{bar_minutes}min")).asi8      # UTC nanoseconds, the same as pd.Timestamp(...).value
        vals = df[["open", "high", "low"]].to_numpy(float)
        starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
        for s, e in zip(starts, np.r_[starts[1:], len(keys)]):
            self._bars[int(keys[s])] = vals[s:e]

    def __bool__(self) -> bool:
        return bool(self._bars)

    def get(self, bar_start) -> np.ndarray | None:
        return self._bars.get(pd.Timestamp(bar_start).value)
