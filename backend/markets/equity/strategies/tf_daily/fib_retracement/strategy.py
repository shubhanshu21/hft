"""equity / fib_retracement -- buy the golden-pocket pullback of an up-move, target the old high.

Ported from the Fibonacci research (scratchpad fib_grid.py: L=10, big impulse, zone Z1, trend T0, exit X1), long side only, delivery:
  * swings: a swing high / low is the highest high / lowest low of 10 bars either side, known 10 bars after it happens;
  * impulse: latest swing low, then the later swing high, a rise of at least 8% and at least 2 ATR;
  * trend: close above the 50-day EMA, AND the price structure is rising: the latest swing high is above the swing high before it and the latest swing
    low is above the swing low before it (no buying a bounce inside lower highs; added 2026-10-13 after AXISBANK, where the slow averages still said "up");
  * setup: today's low is inside the 50% - 78.6% retracement of that impulse, today is a green candle that closes at or above the 61.8% level
    and in the upper half of its range; void if price already made a new high or broke the swing low since the swing high;
  * market check (added 2026-10-13 from the loss analysis): at least 60% of the 49 NIFTY stocks must be above their own 50-day EMA. In 2023-25 buying pullbacks
    when the market was healthy won 62-79% of the time; in 2026 the market was weak (46% above) and the same pullbacks lost 15 of 22. Skipped when the runner
    gives no other-symbol data;
  * one entry per swing high (the first day the setup is valid);
  * stop 0.2 ATR under the swing low, target = the swing high. No time stop: it stays until the stop or the target is hit.
The research found no buy-only delivery version profitable in 2026 after fees; this is here to be tested on the page, not switched on for trading
(default_enabled stays False).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategy import EntryContext, ExitContext, ExitDecision, Signal, Strategy
from markets.equity.costs import compute_nse_equity_delivery_costs
from markets.equity.universe import NIFTY50_SYMBOLS

L = 10                 # swing window, bars each side
MIN_IMPULSE_PCT = 0.08
MIN_IMPULSE_ATR = 2.0
MIN_BREADTH_PCT = 60.0


class FibRetracementEquity(Strategy):
    name = "fib_retracement"
    market = "equity"
    intraday = False
    product = "D"
    uses_leverage = False
    allow_short = False
    timeframe = ("days", 1)
    lookback_days = 250
    id_prefix = "FIB"

    def entry(self, ctx: EntryContext) -> Signal | None:
        if len(ctx.candles) < 80:
            return None
        df = pd.DataFrame(ctx.candles)
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        n = len(c)
        i = n - 1
        e50 = df["close"].ewm(span=50, adjust=False).mean().to_numpy()
        prev = df["close"].shift().to_numpy()
        tr = np.nanmax(np.vstack([h - l, np.abs(h - prev), np.abs(l - prev)]), axis=0)
        atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()

        win = 2 * L + 1
        is_high = h == pd.Series(h).rolling(win, center=True).max().to_numpy()
        is_low = l == pd.Series(l).rolling(win, center=True).min().to_numpy()
        known = np.arange(n) <= i - L                                    # a swing is only known L bars later
        highs, lows = np.where(is_high & known)[0], np.where(is_low & known)[0]
        if len(highs) < 2 or len(lows) < 2:
            return None
        if not (h[highs[-1]] > h[highs[-2]] and l[lows[-1]] > l[lows[-2]]):
            return None                                                  # lower high or lower low: a bounce in a downtrend, not a pullback in an uptrend
        hi_i, lo_i = int(highs[-1]), int(lows[-1])
        if lo_i >= hi_i:
            return None                                                  # the latest swing low must come before the latest swing high
        hi_v, lo_v = h[hi_i], l[lo_i]
        rng = hi_v - lo_v
        if not (rng / lo_v >= MIN_IMPULSE_PCT and rng >= MIN_IMPULSE_ATR * atr[i]):
            return None
        f50, f618, f786 = hi_v - 0.5 * rng, hi_v - 0.618 * rng, hi_v - 0.786 * rng

        def valid(j: int) -> bool:
            if not c[j] > e50[j]:
                return False
            if j - hi_i > 1 and (h[hi_i + 1:j].max() > hi_v or l[hi_i + 1:j].min() < lo_v):
                return False                                             # void: new high or swing low broken before this day
            if not (f786 <= l[j] <= f50):
                return False
            return bool(c[j] > o[j] and c[j] >= f618 and (c[j] - l[j]) / max(h[j] - l[j], 1e-9) >= 0.5)

        if not valid(i) or any(valid(j) for j in range(hi_i + L, i)):    # only the first valid day of this swing high
            return None
        if ctx.data is not None and self._breadth(ctx) < MIN_BREADTH_PCT:
            return None                                                  # most stocks are below their 50-day average: a weak market
        entry = float(c[i])
        stop = float(lo_v - 0.2 * atr[i])
        dist = entry - stop
        if dist <= 0 or hi_v <= entry:
            return None
        qty = int(min(ctx.capital * ctx.risk_pct / 100 / dist, ctx.capital / entry))
        if qty < 1:
            return None
        return Signal(symbol=ctx.symbol, direction="long", entry_price=entry, stop_loss=stop, qty=qty, stop_dist=dist,
                      setup_type="fib golden pocket", target_price=float(hi_v))

    @staticmethod
    def _breadth(ctx: EntryContext) -> float:
        """% of the 49 NIFTY stocks whose close is above their own 50-day EMA today (stocks without data today are left out)."""
        above = total = 0
        for sym in NIFTY50_SYMBOLS:
            cs = ctx.data.candles(sym)
            if len(cs) < 60:
                continue
            closes = pd.Series([x["close"] for x in cs])
            total += 1
            above += closes.iloc[-1] > closes.ewm(span=50, adjust=False).mean().iloc[-1]
        return 100.0 * above / total if total else 100.0

    def manage(self, pos: dict, ctx: ExitContext) -> ExitDecision | None:
        return None                                                      # stop and target only (the engine fills them)

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_nse_equity_delivery_costs(direction, entry, exit_price, qty, symbol=symbol)


STRATEGY = FibRetracementEquity()
