"""MCX commodity scalping -- the existing trend-breakout scalper behind the Strategy contract.

Entry logic is unchanged (entry_signal.compute_entry_signal); exits are the shared fixed
take-profit + breakeven-lock + trail manager from core/exits.py.
"""
from __future__ import annotations

from core.exits import fixed_tp_breakeven_trail
from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.commodity.costs import COMMODITY_SPECS, compute_mcx_commodity_costs
from markets.commodity.scalping.entry_signal import compute_entry_signal

HOLD_SECONDS = 80 * 60          # timeout exit


def signal_from_dict(sym: str, r: dict, lot_size: int) -> Signal:
    """Wrap compute_entry_signal()'s dict as a Signal (shared with the currency scalper)."""
    return Signal(
        symbol=sym, direction=r["direction"], entry_price=r["entry_price"], stop_loss=r["sl"],
        qty=r["lots"], stop_dist=r["stop_dist"], instrument_key=r["instrument_key"], lot_size=lot_size,
        setup_type=r.get("setup_type", "trend_breakout"),
        exit_state={"tp": r["tp"], "be": r["be"], "armed_be": False}, price_levels=("tp", "be"),
        target_price=r["tp"], breakeven_price=r["be"], alert_levels={"tp": r["tp"], "be": r["be"]},
        diagnostics={k: r[k] for k in ("rsi", "adx", "vol_surge", "vwap_dist_pct", "ema_slope_pct")},
    )


class McxScalping(Strategy):
    name = "scalping"
    market = "commodity"
    intraday = True
    product = "I"
    timeframe = ("minutes", 5)
    default_enabled = True
    id_prefix = "MCX"
    close_at = (22, 45)         # square off 5 min before Upstox's 22:50 MCX RMS auto-squareoff

    def lot_size(self, sym: str) -> int:
        return COMMODITY_SPECS.get(sym, {}).get("lot_size", 1)

    def entry(self, ctx: EntryContext) -> Signal | None:
        r = compute_entry_signal(
            ctx.symbol, ctx.candles, ctx.instrument_key, ctx.full_session, ctx.direction_filter,
            ctx.capital, ctx.risk_pct, ctx.leverage, regime_ok=ctx.regime_ok,
        )
        return signal_from_dict(ctx.symbol, r, self.lot_size(ctx.symbol)) if r else None

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        if not ctx.candles:
            return None
        return fixed_tp_breakeven_trail(pos, ctx.candles[-1], ctx.now, close_at=self.close_at, max_hold_s=HOLD_SECONDS)

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_mcx_commodity_costs(symbol, direction, entry, exit_price, qty)

    def restore(self, row: dict) -> dict:
        return {"tp": row["target_price"], "be": row["breakeven_price"], "armed_be": bool(row["armed_be"])}


STRATEGY = McxScalping()
