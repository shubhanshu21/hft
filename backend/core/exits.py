"""Reusable exit managers for Strategy.manage().

These are the two exit mechanics the system has always used, lifted verbatim out of the
runners so every strategy (and both runners) share ONE implementation instead of two
hand-copied ones. A new strategy usually just calls one of these from manage(); write a new
one here only if it needs a genuinely different exit shape.

Both mutate the position dict (stop / best price / armed flag) exactly as the runners did and
return an ExitDecision when the trade should close, else None. They are pure with respect to
the broker and the DB.
"""
from __future__ import annotations

from datetime import datetime

from core.strategy import ExitDecision

BE_LOCK_BUFFER_PCT = 0.0020       # stop moves to entry +0.20% once the breakeven level is reached
FIXED_TP_TRAIL_MULT = 0.30        # commodity / currency trail distance, in stop-distances


def _ts(value) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _side(pos: dict, latest: dict) -> tuple[int, float | None, float | None]:
    """Direction and this bar's most favourable / most adverse price, counting ONLY price action that happened
    after the entry. The entry is decided on a bar (pos["entry_bar_ts"]); that bar's high/low include prices from
    BEFORE the entry, and a breakout signal fires on exactly the wide bars where the low is already past the stop.
    Reading the full range there stopped positions out ~35 s after entry on prices that came before it (2026-09-23:
    5 of 17 trades, 84% of the day's loss, none of the stops actually reached afterwards). The backtest never does
    this -- it enters at a bar's close and looks at exits from the NEXT bar -- so:
      * on the entry bar only the latest price (close) is post-entry;
      * a bar older than the entry bar carries no post-entry information at all (fav/adv are None);
      * any later bar is used in full.
    Positions without an entry_bar_ts (rows saved before this fix) keep the old behaviour."""
    d = 1 if pos["direction"] == "long" else -1
    high, low, close = float(latest["high"]), float(latest["low"]), float(latest["close"])
    entry_bar = pos.get("entry_bar_ts")
    if entry_bar is not None:
        bar, ebar = _ts(latest["timestamp"]), _ts(entry_bar)
        if bar < ebar:
            return d, None, None
        if bar == ebar:
            high = low = close
    fav = high if d == 1 else low     # most favourable price of the bar
    adv = low if d == 1 else high     # most adverse price of the bar
    return d, fav, adv


def post_entry_range(pos: dict, latest: dict) -> tuple[float | None, float | None]:
    """(most favourable, most adverse) price of `latest` counting only what happened after the entry, or
    (None, None) if the bar carries nothing post-entry. Use this in a custom Strategy.manage() instead of the
    bar's raw high/low -- see _side for why."""
    _, fav, adv = _side(pos, latest)
    return fav, adv


def _deadline(now: datetime, close_at: tuple[int, int] | None) -> datetime | None:
    return None if close_at is None else now.replace(hour=close_at[0], minute=close_at[1], second=0, microsecond=0)


def fixed_tp_breakeven_trail(pos: dict, latest: dict, now: datetime, *,
                             close_at: tuple[int, int] | None,
                             max_hold_s: float | None) -> ExitDecision | None:
    """Fixed take-profit, initial stop, then a breakeven lock + trailing stop (MCX / NSE currency).

    Needs pos["tp"], pos["be"], pos["armed_be"], pos["current_stop"], pos["best_price"], pos["stop_dist"].
    close_at = (hour, minute) of the forced end-of-day exit, or None for a strategy held overnight.
    max_hold_s = timeout in seconds, or None for no timeout.
    """
    d, fav, adv = _side(pos, latest)
    deadline = _deadline(now, close_at)
    close = float(latest["close"])

    if fav is not None:
        if (fav >= pos["tp"] if d == 1 else fav <= pos["tp"]):
            return ExitDecision(pos["tp"], "take_profit")
        if (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
            return ExitDecision(pos["current_stop"], "be_stop" if pos["armed_be"] else "initial_stop")
    if max_hold_s is not None and (now - pos["entry_time"]).total_seconds() >= max_hold_s:
        return ExitDecision(close, "timeout_exit")
    if deadline is not None and now >= deadline:
        return ExitDecision(close, "eod_squareoff")
    if fav is None:
        return None                   # nothing post-entry to trail on yet

    if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
        pos["armed_be"] = True
        pos["current_stop"] = pos["entry_price"] + BE_LOCK_BUFFER_PCT * pos["entry_price"] * d
    if pos["armed_be"]:
        pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
        trail = pos["best_price"] - FIXED_TP_TRAIL_MULT * pos["stop_dist"] * d
        pos["current_stop"] = max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail)
    return None


def activation_trail(pos: dict, latest: dict, atr: float, now: datetime, *,
                     close_at: tuple[int, int] | None,
                     max_hold_s: float | None) -> ExitDecision | None:
    """No fixed target: an initial stop, then once price has moved `activation_price` in favour a
    trailing stop that is recomputed from the CURRENT bar's ATR every scan (NSE equity).

    Needs pos["activation_price"], pos["trail_mult"], pos["armed_trail"], pos["current_stop"],
    pos["best_price"], pos["stop_dist"]. `atr` is the latest bar's ATR, computed by the caller.
    """
    d, fav, adv = _side(pos, latest)
    deadline = _deadline(now, close_at)
    close = float(latest["close"])

    if fav is not None and (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
        return ExitDecision(pos["current_stop"], "trail_stop" if pos["armed_trail"] else "initial_stop")
    if max_hold_s is not None and (now - pos["entry_time"]).total_seconds() >= max_hold_s:
        return ExitDecision(close, "timeout_exit")
    if deadline is not None and now >= deadline:
        return ExitDecision(close, "eod_squareoff")
    if fav is None:
        return None                   # nothing post-entry to trail on yet

    if not pos["armed_trail"] and (fav >= pos["activation_price"] if d == 1 else fav <= pos["activation_price"]):
        pos["armed_trail"] = True
        pos["current_stop"] = pos["entry_price"] + BE_LOCK_BUFFER_PCT * pos["entry_price"] * d
    if pos["armed_trail"]:
        pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
        trail_dist = pos["trail_mult"] * max(atr, pos["stop_dist"] * 0.1)
        trail = pos["best_price"] - trail_dist * d
        pos["current_stop"] = max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail)
    return None
