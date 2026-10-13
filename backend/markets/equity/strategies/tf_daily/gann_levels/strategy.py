"""equity / gann_levels -- buy a bullish reversal candle sitting on a Gann square-of-nine level, target the next level up. DAILY-bar adaptation.

The pasted Gann idea was an intraday scalp on 5-minute bars; the web backtester runs daily bars only, so this is the same idea on daily candles (long only, delivery):
  * Gann levels: for k = 1, 2, 3 ... the level is k*k + (1 if k is even else 0) (the odd-adjusted squares: 2705, 2809, 2917, ...);
  * signal: today is a bullish reversal (hammer, bullish engulfing or morning star) and its LOW is within 1% of the nearest Gann level (support);
  * entry at the next open (the engine fills it), stop 0.2% under the pattern low, target the next Gann level above the entry;
  * the risk must be 0.5% - 6% of the price and the reward at least 1R.
Judged by the same fees, slippage and gap fills as every strategy here. Not switched on for trading (default_enabled stays False).
"""
from __future__ import annotations

import math

import numpy as np

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.equity.costs import compute_nse_equity_delivery_costs

TOL_PCT = 1.0
MIN_RISK, MAX_RISK = 0.005, 0.06


def level(k: int) -> float:
    return float(k * k + (1 if k % 2 == 0 else 0))


def nearest_level(x: float) -> float:
    k = int(math.sqrt(x))
    return min((level(j) for j in range(max(k - 1, 1), k + 3)), key=lambda v: abs(v - x))


def next_level_above(price: float) -> float:
    k = int(math.sqrt(price))
    while level(k) <= price * 1.002:
        k += 1
    return level(k)


def bullish_pattern(c: list[dict]) -> tuple[bool, float]:
    """(is a bullish reversal on the last bar, the pattern's low)."""
    o, h, l, cl = (np.array([x[k] for x in c[-3:]], float) for k in ("open", "high", "low", "close"))
    body, rng = abs(cl[2] - o[2]), max(h[2] - l[2], 1e-9)
    upper, lower = h[2] - max(o[2], cl[2]), min(o[2], cl[2]) - l[2]
    hammer = body > 0 and lower >= 2 * body and upper <= max(0.5 * body, 0.1 * rng)
    engulf = cl[1] < o[1] and cl[2] > o[2] and cl[2] >= o[1] and o[2] <= cl[1]
    big = max(h[0] - l[0], 1e-9)
    morning = (cl[0] < o[0] and abs(cl[0] - o[0]) >= 0.6 * big and abs(cl[1] - o[1]) <= 0.3 * max(h[1] - l[1], 1e-9)
               and cl[2] > o[2] and cl[2] > (o[0] + cl[0]) / 2)
    low = float(min(l)) if morning else float(min(l[1:])) if engulf else float(l[2])
    return bool(hammer or engulf or morning), low


class GannLevelsEquity(Strategy):
    name = "gann_levels"
    market = "equity"
    intraday = False
    product = "D"
    uses_leverage = False
    allow_short = False
    timeframe = ("days", 1)
    lookback_days = 60
    id_prefix = "GAN"

    def entry(self, ctx: EntryContext) -> Signal | None:
        if len(ctx.candles) < 5:
            return None
        ok, low = bullish_pattern(ctx.candles)
        if not ok or low <= 1:
            return None
        lv = nearest_level(low)
        if abs(low - lv) / lv > TOL_PCT / 100:
            return None
        entry = float(ctx.candles[-1]["close"])                       # the engine fills at the next open
        stop = low * (1 - 0.002)
        risk = entry - stop
        target = next_level_above(entry)
        if risk <= 0 or not (MIN_RISK <= risk / entry <= MAX_RISK) or target - entry < risk:
            return None
        qty = int(min(ctx.capital * ctx.risk_pct / 100 / risk, ctx.capital / entry))
        if qty < 1:
            return None
        return Signal(symbol=ctx.symbol, direction="long", entry_price=entry, stop_loss=stop, qty=qty, stop_dist=risk,
                      setup_type="gann support", target_price=target)

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        return None

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(direction, entry, exit_price, qty, symbol=symbol)


STRATEGY = GannLevelsEquity()
