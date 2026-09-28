"""NSE currency scalping -- the same trend-breakout scalper as commodity (entry_signal handles both
by symbol), with the currency session hours (read from Upstox, core/sessions.py) and the NCD cost model.

Exit shape (opt-in, paper trading): CURRENCY_EXIT_MODE / <SYMBOL>_EXIT_MODE = "dynamic" swaps the fixed take-profit + break-even + fixed trail for the equity scalper's
ADX-scaled exit -- break-even/activation at 0.6R / scale, trail 0.3R x scale of the CURRENT bar's ATR, no fixed target, same 80-minute limit and square-off (scale = ADX / 25 clipped to 0.7-1.8).
Tested 2026-09-26 (docs/STATIC_VS_DYNAMIC.md): USDINR at 10x, real data, first / second half +10,558 / -3,271 fixed vs +17,573 / -2,064 dynamic. Default "fixed" = the original behaviour."""
from __future__ import annotations

import os
from dataclasses import replace

import numpy as np
import pandas as pd

from core.exits import activation_trail
from core.strategy import EntryContext, ExitContext, ExitDecision, Signal
from markets.commodity.features import compute_commodity_features
from markets.commodity.scalping.backtest import BE_ACTIVATION_MULT, TRAIL_DIST_MULT
from markets.commodity.scalping.strategy import HOLD_SECONDS, McxScalping
from markets.currency.costs import CURRENCY_SPECS, compute_ncd_currency_costs


def exit_mode(symbol: str) -> str:
    return (os.environ.get(f"{symbol.upper()}_EXIT_MODE") or os.environ.get("CURRENCY_EXIT_MODE") or "fixed").strip().lower()


def to_dynamic(signal: Signal) -> Signal:
    """The same entry with the ADX-scaled exit state: no fixed target, activation at 0.6R/scale, trail multiple 0.3R x scale."""
    scale = float(np.clip(float(signal.diagnostics.get("adx", 25.0)) / 25.0, 0.7, 1.8))
    d = 1 if signal.direction == "long" else -1
    act = round(signal.entry_price + (BE_ACTIVATION_MULT / scale) * signal.stop_dist * d, 4)
    return replace(signal, exit_state={"armed_trail": False, "activation_price": act, "trail_mult": TRAIL_DIST_MULT * scale}, price_levels=("activation_price",),
                   target_price=act, breakeven_price=act, alert_levels={"tp": act})


class NcdScalping(McxScalping):
    market = "currency"

    def lot_size(self, sym: str) -> int:
        from markets.currency.costs import get_contract_multiplier
        return get_contract_multiplier(sym)

    def entry(self, ctx: EntryContext) -> Signal | None:
        signal = super().entry(ctx)
        if signal is not None and exit_mode(ctx.symbol) == "dynamic":
            return to_dynamic(signal)
        return signal

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        if "activation_price" not in pos:                       # a position opened with the fixed exit keeps it, whatever the setting is now
            return super().manage(pos, ctx)
        if not ctx.candles:
            return None
        try:
            atr = float(compute_commodity_features(pd.DataFrame(ctx.candles), symbol=ctx.symbol)["atr"].iloc[-1])
        except Exception:                                       # too few bars for the indicator: fall back to the entry's own stop distance
            atr = float(pos["stop_dist"])
        if not np.isfinite(atr) or atr <= 0:
            atr = float(pos["stop_dist"])
        return activation_trail(pos, ctx.candles[-1], atr, ctx.now, close_at=self.close_at, max_hold_s=HOLD_SECONDS)

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_ncd_currency_costs(symbol, direction, entry, exit_price, qty)

    def restore(self, row: dict) -> dict:
        import json
        state = json.loads(row["state"]) if row.get("state") else {}
        if "activation_price" in state:                         # opened with the dynamic exit
            return {"activation_price": state["activation_price"], "trail_mult": state.get("trail_mult", TRAIL_DIST_MULT), "armed_trail": bool(row["armed_be"])}
        return super().restore(row)


STRATEGY = NcdScalping()
