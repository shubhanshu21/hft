"""The strategy contract -- everything a new trading strategy needs to know.

To add a strategy you write ONE file, markets/<market>/<name>/strategy.py, that
defines a Strategy subclass and exposes an instance called STRATEGY:

    class EquitySwing(Strategy):
        name, market = "swing", "equity"
        intraday, product = False, "D"          # held overnight, delivery product
        timeframe = ("days", 1)

        def entry(self, ctx):  ...              # -> Signal | None
        def manage(self, pos, ctx): ...         # -> ExitDecision | None

    STRATEGY = EquitySwing()

then switch it on with  EQUITY_STRATEGIES=scalping,swing  in .env. Nothing else
changes: the paper-trading runner (engine/live_dryrun.py) and the real-order
runner (engine/live_trading.py) both discover it through core/registry.py and
apply the SAME gates to it (kill switches, cooldowns, heat and margin caps,
sector cap, drawdown-scaled sizing). A strategy only makes decisions; the runners
own execution, so a strategy never touches the broker or the database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Signal:
    """An entry decision. Everything the runner needs to size-check, record and alert."""
    symbol: str
    direction: str                      # "long" | "short"
    entry_price: float
    stop_loss: float
    qty: int                            # shares (equity) or lots (commodity / currency)
    stop_dist: float                    # |entry - stop| in price units
    instrument_key: str | None = None
    lot_size: int = 1                   # units per qty; notional = qty * lot_size * entry_price
    setup_type: str = "trend_breakout"
    # State carried on the open position and persisted to the DB, whatever manage() needs on
    # later scans (breakeven level, activation price, trail multiple, target, ...).
    exit_state: dict[str, Any] = field(default_factory=dict)
    # Levels shown in alerts as "tp" / "be" and stored as the DB's target/breakeven columns.
    target_price: float = 0.0
    breakeven_price: float = 0.0
    alert_levels: dict[str, float] = field(default_factory=dict)
    # Names of exit_state entries that are PRICES. A real fill rarely equals the assumed entry price, so
    # the live runner shifts the stop, target/breakeven and each of these by the fill slippage.
    price_levels: tuple[str, ...] = ()
    # Diagnostics stored with the trade: rsi, adx, vwap_dist_pct, ema_slope_pct, vol_surge.
    diagnostics: dict[str, float] = field(default_factory=dict)


@dataclass
class ExitDecision:
    price: float
    reason: str                         # "take_profit" | "initial_stop" | "trail_stop" | "eod_squareoff" | ...


@dataclass
class EntryContext:
    symbol: str
    candles: list[dict]                 # chronological OHLCV in the strategy's own timeframe
    now: datetime
    instrument_key: str | None
    capital: float
    risk_pct: float                     # already scaled by the drawdown rule
    leverage: float                     # 1.0 when the strategy declares uses_leverage = False
    direction_filter: str               # "both" | "long" | "short"
    full_session: bool                  # MCX full session vs evening-only window
    regime_ok: bool | None = True       # this symbol's regime gate (None = unknown)
    equity_regime_ok: bool | None = True


@dataclass
class ExitContext:
    symbol: str
    candles: list[dict]
    now: datetime


class Strategy:
    """Base class. Override the declarations and implement entry() / manage()."""

    # ---- declare ---------------------------------------------------------------
    name: str = ""                      # unique within a market, e.g. "scalping", "swing"
    market: str = ""                    # "commodity" | "currency" | "equity"
    intraday: bool = True               # False = held overnight: no EOD square-off, no timeout
    product: str = "I"                  # Upstox product for real orders: "I" intraday, "D" delivery
    uses_leverage: bool = True          # False (delivery) = sized and margin-checked at 1x
    allow_short: bool = True            # False (delivery) = the runner never opens a short
    timeframe: tuple[str, int] = ("minutes", 5)   # (unit, interval) of the candles entry()/manage() read
    lookback_days: int = 0              # >0 = fetch this many days of history (daily strategies); 0 = today's intraday candles
    default_enabled: bool = False       # True = runs without being named in <MARKET>_STRATEGIES
    max_positions: int | None = None    # cap on this strategy's concurrent open positions
    sector_cap: bool = False            # apply the per-sector position cap (equity)
    id_prefix: str = "STRAT"            # used in order / position ids
    price_decimals: int = 2             # rounding of re-anchored price levels (tick precision)

    # ---- implement -------------------------------------------------------------
    def entry(self, ctx: EntryContext) -> Signal | None:
        raise NotImplementedError

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        """Called every scan for an open position of this strategy. Return an ExitDecision to
        close it. You may move the stop by mutating pos["current_stop"] / pos["best_price"] /
        the armed flag; the runner persists any change. Return None to hold."""
        raise NotImplementedError

    # ---- optional overrides ----------------------------------------------------
    def blocked(self, flags: dict) -> bool:
        """True = don't even fetch candles for a new entry right now (a cheap market-wide gate,
        e.g. an index regime filter). flags carries the runner's regime state."""
        return False

    def in_session(self, now: datetime) -> bool:
        """True while this strategy's market is open -- use it in due() / entry(); the runner does not
        gate entries on trading hours itself (each strategy owns its own entry window)."""
        from core.sessions import is_open
        return is_open(self.market, now)

    def due(self, now: datetime) -> bool:
        """False = skip entry evaluation on this scan (e.g. a daily strategy that only looks
        once, near the close). The default evaluates on every scan."""
        return True

    def lot_size(self, symbol: str) -> int:
        """Units per qty for this symbol (1 for shares; the contract multiplier for lot-sized markets)."""
        return 1

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        """Itemised round-trip costs; must return the same dict shape as the market's cost
        functions (gross, total, net, brokerage, ...). Override for delivery vs intraday."""
        raise NotImplementedError

    def restore(self, row: dict) -> dict:
        """Rebuild this strategy's exit state for a position loaded from the DB after a restart.
        `row` is the positions-table row; row["state"] holds whatever entry() saved in exit_state.
        Return keys to merge into the in-memory position."""
        import json
        state = row.get("state")
        return json.loads(state) if state else {}
