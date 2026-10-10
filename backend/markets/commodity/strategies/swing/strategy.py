"""MCX commodity swing -- 12-month time-series momentum (Moskowitz, Ooi & Pedersen 2012), long AND short.

STATUS: PAPER-ONLY CANDIDATE, off by default. Backtested 2003-2026 on global futures x USDINR (docs/SWING_RESEARCH.md):
train (2003-2018) PF 1.17, held-out test (2019-2026) PF 1.85 over 229 trades, still PF 1.72 with costs 4x. But the
edge is statistically weak (t = 1.5 over 23 years), carried by gold and copper, and natural gas loses money -- treat
it as a hypothesis to gather paper-trading evidence on, not a validated strategy.

Rule (core/swing_engine.py, identical to what was backtested): hold long while the 252-day return is positive, short
while negative, flip/exit when it changes sign; initial stop 3 x ATR(14) from entry. Signals use COMPLETED daily bars
only and are acted on at the next session's open (here: the first half hour of the MCX session).

Contract roll: MCX contracts expire monthly and the runner re-maps a symbol to the next contract the day after expiry. A
position is therefore closed ROLL_BEFORE_EXPIRY_DAYS before its own contract expires (ahead of MCX's delivery / tender
period, at that contract's price) and no new one is opened on a contract that close to expiry; the next contract is entered
once it is the front month. Each roll pays a real round trip (docs/FILL_MODEL_AUDIT.md: a 2019-26 backtest of this account,
with these costs, a monthly roll and the heat cap, made +97k on GOLDTEN + SILVERMIC + CRUDEOILM but with a 60% drawdown and
nearly all of it from one 2024-26 gold trade; each contract alone lost or broke even in 2005-18).

Data: the 252-day signal cannot come from Upstox (the current contract only has 1-4 months of daily history), so the
direction and stop distance come from the proxy series in markets/commodity/strategies/swing/proxy.py; the trade itself is sized
and stopped on the real MCX price. If the proxy download fails, no new position is opened (an open one keeps its stop).
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from core.exits import open_after_entry, post_entry_range, stop_fill
from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from core.swing_engine import Rule, entry_signal, exit_signal, with_indicators
from markets.commodity.costs import COMMODITY_SPECS, compute_mcx_commodity_costs, size_commodity_lots
from markets.commodity.strategies.swing.proxy import has_proxy, inr_frame
from services.broker.instruments import mcx_front_expiry

RULE = Rule("tsmom", {"lookback": 252}, stop_mult=3.0)
ROLL_BEFORE_EXPIRY_DAYS = 7       # calendar days: close / do not open this close to the contract's expiry


def _days_to_expiry(expiry: str | None, now: datetime) -> int | None:
    if not expiry:
        return None
    try:
        return (datetime.fromisoformat(expiry).date() - now.date()).days
    except ValueError:
        return None


class CommoditySwing(Strategy):
    name = "swing"
    market = "commodity"
    intraday = False              # carried overnight: no square-off, no timeout
    product = "D"                 # carry-forward (NRML) futures position
    uses_leverage = True          # futures margin
    allow_short = True
    timeframe = ("minutes", 5)    # only the latest MCX price is needed from Upstox; the signal comes from the proxy
    default_enabled = False       # never trades until named in COMMODITY_STRATEGIES
    max_positions = 3
    id_prefix = "CSW"
    WINDOW = (9, 5, 10, 0)        # evaluate between 09:05 and 10:00 IST: yesterday's global close is in, the day is young

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], tuple[int, float, pd.Series] | None] = {}

    # ---- daily signal from the proxy series ----------------------------------------------------------------
    def _latest_row(self, sym: str, today: str):
        """Indicator row of the last COMPLETED daily bar, or None if the proxy is unavailable."""
        try:
            ind = with_indicators(inr_frame(sym))
        except Exception:
            return None
        ind = ind[ind.index < pd.Timestamp(today)]           # drop today's still-forming bar
        return ind.iloc[-1] if len(ind) > 260 else None

    def _signal(self, sym: str, now: datetime) -> tuple[int, float, pd.Series] | None:
        """(direction to enter or 0, stop distance as a fraction of price, indicator row of the last completed day),
        cached per symbol and day. None if the proxy series is unavailable."""
        key = (sym, now.strftime("%Y-%m-%d"))
        if key not in self._cache:
            row = self._latest_row(sym, key[1])
            if row is None:
                self._cache[key] = None
            else:
                stop_pct = RULE.stop_mult * float(row["atr"]) / float(row["close"])
                self._cache[key] = (entry_signal(RULE, row, allow_short=True), stop_pct, row)
        return self._cache[key]

    def due(self, now: datetime) -> bool:
        a, b, c, d = self.WINDOW
        return self.in_session(now) and (now.hour, now.minute) >= (a, b) and (now.hour, now.minute) < (c, d)

    # ---- decide ------------------------------------------------------------------------------------------------
    def entry(self, ctx: EntryContext) -> Signal | None:
        if not has_proxy(ctx.symbol) or not ctx.candles:
            return None
        expiry = mcx_front_expiry(ctx.symbol)
        left = _days_to_expiry(expiry, ctx.now)
        if left is not None and left <= ROLL_BEFORE_EXPIRY_DAYS:
            return None                                    # wait for the next contract to become the front month
        sig = self._signal(ctx.symbol, ctx.now)
        if sig is None or sig[0] == 0:
            return None
        direction, stop_pct, _row = sig
        if (direction < 0 and ctx.direction_filter == "long") or (direction > 0 and ctx.direction_filter == "short"):
            return None
        price = float(ctx.candles[-1]["close"])
        stop_dist = price * stop_pct
        lots = size_commodity_lots(ctx.capital, price, stop_dist, ctx.risk_pct, ctx.symbol, ctx.leverage)
        if lots <= 0:
            return None
        return Signal(
            symbol=ctx.symbol, direction="long" if direction > 0 else "short", entry_price=price,
            stop_loss=price - direction * stop_dist, qty=lots, stop_dist=stop_dist, instrument_key=ctx.instrument_key,
            lot_size=COMMODITY_SPECS.get(ctx.symbol, {}).get("lot_size", 1), setup_type="tsmom_252",
            exit_state={"dir": direction, "stop_pct": stop_pct, "contract_expiry": expiry},
            target_price=price, breakeven_price=price,      # the DB needs values; this strategy has no target
        )

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        if not ctx.candles:
            return None
        # 1. the protective stop, judged only on prices after the entry
        fav, adv = post_entry_range(pos, ctx.candles[-1])
        direction = 1 if pos["direction"] == "long" else -1
        if adv is not None and (adv <= pos["current_stop"] if direction > 0 else adv >= pos["current_stop"]):
            # a bar that opened through the stop (the overnight gap at 09:00) fills at that open, not at the stop
            return ExitDecision(stop_fill(pos["current_stop"], open_after_entry(pos, ctx.candles[-1]), direction), "initial_stop")
        # 2. roll: leave this contract before it expires, at its own price (the runner re-maps the symbol after expiry)
        left = _days_to_expiry(pos.get("contract_expiry"), ctx.now)
        if left is not None and left <= ROLL_BEFORE_EXPIRY_DAYS:
            return ExitDecision(float(ctx.candles[-1]["close"]), "contract_roll")
        # 3. the momentum sign flipped: leave at the current price (the backtest exits at the next open)
        if self.due(ctx.now):
            sig = self._signal(ctx.symbol, ctx.now)
            if sig is not None and exit_signal(RULE, sig[2], direction):
                return ExitDecision(float(ctx.candles[-1]["close"]), "signal_exit")
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_mcx_commodity_costs(symbol, direction, entry, exit_price, qty)

    def lot_size(self, symbol: str) -> int:
        return COMMODITY_SPECS.get(symbol, {}).get("lot_size", 1)


STRATEGY = CommoditySwing()
