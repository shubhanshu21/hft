"""Opt-in 'do not chase' entry shared by the backtests (research only; live trading is unchanged).  See docs/FIVE_MINUTE_SIGNAL_SCREEN.md.

The 5-minute screen found that the most stable pattern after a strong bar is a small reversal over the next 15 minutes -- exactly where the trend rule buys. `step` turns a signal into a RESTING LIMIT a fraction of a stop distance
better than the signal price, valid for `bars` bars: it fills only if a later bar trades to the limit (through it by `through` stop-distances, a conservative queue assumption) without also trading through the stop; otherwise the signal is dropped.
"""
from __future__ import annotations


def step(pending: dict | None, i: int, direction: str | None, sdist: float, c_price: float, c_low: float, c_high: float,
         frac: float, bars: int, through: float = 0.0) -> tuple[dict | None, str | None, float, float]:
    """Returns (pending, direction, sdist, price). `direction` is None when there is no entry on this bar; otherwise the entry is `direction` at `price` with stop distance `sdist`."""
    if pending is not None and i - pending["idx"] > bars:
        pending = None                                              # never filled in time
    if pending is not None:
        d, lim, psd = pending["direction"], pending["limit"], pending["sdist"]
        long = d == "long"
        touched = (c_low <= lim - through * psd) if long else (c_high >= lim + through * psd)
        blown = (c_low <= lim - psd) if long else (c_high >= lim + psd)
        if touched and not blown:
            return None, d, psd, lim
        return (None if blown else pending), None, sdist, c_price
    if direction:
        sign = 1 if direction == "long" else -1
        return {"direction": direction, "idx": i, "sdist": sdist, "limit": round(c_price - sign * frac * sdist, 4)}, None, sdist, c_price
    return None, None, sdist, c_price


# ---------------------------------------------------------------------------------------------------------------------------------------------------
# Live (paper) use: a signal becomes a pending limit instead of an immediate entry.
import os
from dataclasses import replace
from datetime import datetime


def config_for(symbol: str, market: str) -> tuple[float, int, float] | None:
    """(frac, bars, through) from the environment, or None when off. `<SYMBOL>_PULLBACK_*` beats `<MARKET>_PULLBACK_*`; default frac 0 = off (the shipped behaviour)."""
    def get(name: str, default: str) -> str:
        return os.environ.get(f"{symbol.upper()}_PULLBACK_{name}") or os.environ.get(f"{market.upper()}_PULLBACK_{name}") or default
    try:
        frac = float(get("FRAC", "0"))
        return (frac, int(float(get("BARS", "3"))), float(get("THROUGH", "0"))) if frac > 0 else None
    except ValueError:
        return None


def _when(value) -> datetime:
    t = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return t


def shift_signal(signal, fill_price: float, decimals: int = 4):
    """The same Signal re-anchored to a fill at `fill_price`: entry, stop, target / breakeven, alert levels and the strategy's own price levels all move by the difference (the pattern live_trading uses for real fills)."""
    delta = fill_price - signal.entry_price
    exit_state = dict(signal.exit_state)
    for key in signal.price_levels:
        exit_state[key] = round(exit_state[key] + delta, decimals)
    return replace(signal, entry_price=round(fill_price, decimals), stop_loss=round(signal.stop_loss + delta, decimals),
                   target_price=round(signal.target_price + delta, decimals), breakeven_price=round(signal.breakeven_price + delta, decimals),
                   alert_levels={k: round(v + delta, decimals) for k, v in signal.alert_levels.items()}, exit_state=exit_state)


class PendingBook:
    """Resting limit entries waiting for a pullback, one per symbol, held in memory (a restart drops them: the signal is simply not taken)."""

    def __init__(self) -> None:
        self._p: dict[str, dict] = {}

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._p

    def clear(self, symbol: str) -> None:
        self._p.pop(symbol, None)

    def register(self, symbol: str, signal, signal_bar_ts, cfg: tuple[float, int, float]) -> dict:
        frac, bars, through = cfg
        sign = 1 if signal.direction == "long" else -1
        limit = round(signal.entry_price - sign * frac * signal.stop_dist, 4)
        self._p[symbol] = {"signal": signal, "limit": limit, "ts": _when(signal_bar_ts), "bars": bars, "through": through}
        return self._p[symbol]

    def check(self, symbol: str, candles: list[dict], now: datetime):
        """('none'|'wait'|'filled'|'dropped', shifted Signal | None). Looks at the bars AFTER the signal bar (the forming bar included) exactly like the backtest:
        filled when a bar trades to the limit (through it by `through` stop-distances) without also trading through the stop; dropped when a bar trades through
        the stop, the day changes, or `bars` bars passed without a fill ('none' = expired, the caller may look for a fresh signal)."""
        p = self._p.get(symbol)
        if p is None:
            return "none", None
        sig, lim, sd, through = p["signal"], p["limit"], p["signal"].stop_dist, p["through"]
        if not candles or _when(candles[-1]["timestamp"]).date() != p["ts"].date():      # a new day (or no data): the pending limit is stale
            self.clear(symbol)
            return "none", None
        rows = [c for c in candles if _when(c["timestamp"]) > p["ts"]]
        long = sig.direction == "long"
        for c in rows[: p["bars"]]:
            touched = (c["low"] <= lim - through * sd) if long else (c["high"] >= lim + through * sd)
            blown = (c["low"] <= lim - sd) if long else (c["high"] >= lim + sd)
            if blown:
                self.clear(symbol)
                return "dropped", None
            if touched:
                self.clear(symbol)
                return "filled", shift_signal(sig, lim)
        if len(rows) > p["bars"]:
            self.clear(symbol)
            return "none", None                                       # expired unfilled
        return "wait", None
