"""NSE equity intraday scalping -- the existing validated scalper behind the Strategy contract.

No fixed take-profit: an ADX-scaled activation level arms a trailing stop that is recomputed from
the current bar's ATR every scan (core/exits.activation_trail).
"""
from __future__ import annotations

from core import sessions

import pandas as pd

from core.exits import activation_trail
from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.equity.costs import compute_nse_equity_costs
from markets.equity.features import compute_equity_features
from markets.equity.scalping.entry_signal import MAX_CONCURRENT_EQUITY_POSITIONS, compute_equity_entry_signal

HOLD_SECONDS = 80 * 60          # timeout exit
DEFAULT_TRAIL_MULT = 0.30       # only used to rebuild a position restored from an older DB row


class EquityScalping(Strategy):
    name = "scalping"
    market = "equity"
    intraday = True
    product = "I"
    timeframe = ("minutes", 5)
    default_enabled = True
    id_prefix = "EQ"
    max_positions = MAX_CONCURRENT_EQUITY_POSITIONS
    sector_cap = True
    @property
    def close_at(self) -> tuple[int, int]:
        return sessions.squareoff_clock(self.market)      # the day's real close (from Upstox) minus the exit policy: core/sessions.py
    price_decimals = 4

    def blocked(self, flags: dict) -> bool:
        return flags.get("equity_regime_ok") is False      # NIFTY50 index regime gate (opt-in)

    def entry(self, ctx: EntryContext) -> Signal | None:
        r = compute_equity_entry_signal(
            ctx.symbol, ctx.candles, ctx.instrument_key, ctx.capital, ctx.risk_pct, ctx.leverage,
            direction_filter=ctx.direction_filter,
        )
        if not r:
            return None
        act = r["activation_price"]
        return Signal(
            symbol=ctx.symbol, direction=r["direction"], entry_price=r["entry_price"], stop_loss=r["sl"],
            qty=r["qty"], stop_dist=r["stop_dist"], instrument_key=r["instrument_key"], lot_size=1,
            setup_type=r.get("setup_type", "trend_breakout"),
            exit_state={"armed_trail": False, "activation_price": act, "trail_mult": r["trail_mult"]},
            price_levels=("activation_price",),
            target_price=act, breakeven_price=act, alert_levels={"tp": act},
            diagnostics={k: r[k] for k in ("rsi", "adx", "vol_surge", "vwap_dist_pct", "ema_slope_pct")},
        )

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        if not ctx.candles or len(ctx.candles) < 25:
            return None
        feat = compute_equity_features(pd.DataFrame(ctx.candles))
        atr = float(feat["atr"].iloc[-1]) if len(feat) else pos["stop_dist"]
        return activation_trail(pos, ctx.candles[-1], atr, ctx.now, close_at=self.close_at, max_hold_s=HOLD_SECONDS)

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_costs(direction, entry, exit_price, qty)

    def restore(self, row: dict) -> dict:
        # The DB stores the activation price in target_price and the armed flag in armed_be. trail_mult
        # was never persisted for older rows, so fall back to the default (this used to raise KeyError).
        return {"tp": row["target_price"], "be": row["breakeven_price"], "activation_price": row["target_price"],
                "armed_trail": bool(row["armed_be"]), "trail_mult": DEFAULT_TRAIL_MULT}


STRATEGY = EquityScalping()
