"""Reusable exit managers for Strategy.manage().

These are the two exit mechanics the system has always used, lifted verbatim out of the
runners so every strategy (and both runners) share ONE implementation instead of two
hand-copied ones. A new strategy usually just calls one of these from manage(); write a new
one here only if it needs a genuinely different exit shape.

Both mutate the position dict (stop / best price / armed flag) exactly as the runners did and
return an ExitDecision when the trade should close, else None. They are pure with respect to
the broker and the DB.

Live strategies call the *_bars versions (fixed_tp_breakeven_trail_bars, activation_trail_bars), which take the whole candle
list and move the stop only once a 5-minute bar is COMPLETE -- see _manage_bars for why. The single-bar functions below them are
the per-bar step, kept for callers that already hand them one completed bar at a time.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from core.strategy import ExitDecision

BE_LOCK_BUFFER_PCT = 0.0020       # stop moves to entry +0.20% once the breakeven level is reached
FIXED_TP_TRAIL_MULT = 0.30        # commodity / currency trail distance, in stop-distances


def lock_stop(entry: float, trigger: float, d: int) -> float:
    """The stop a breakeven / trail arm moves to: entry +/- BE_LOCK_BUFFER_PCT, but NEVER beyond the trigger level.
    The arm fires only after price reached `trigger`, so a stop at or inside it is a price that really traded. A fixed 0.20% lock beyond
    the trigger is a stop ABOVE the market: the paper fill then happens at a price that never traded. That is common for currency, where
    the trigger is ~0.04% from entry (USDINR 2026-09-23: entry 95.715, best price after entry 95.7525, "exit" 95.906 for +Rs957).
    Commodity is unaffected (its 0.35% minimum stop puts the trigger at >= 0.21%, above the 0.20% lock)."""
    return entry + min(BE_LOCK_BUFFER_PCT * entry, abs(trigger - entry)) * d


def stop_fill(stop: float, bar_open, d: int) -> float:
    """The price a stop really fills at on a bar that reaches it.

    A stop resting on the right side of the bar's open fills at the stop. A bar that OPENS already through it -- the stop was just moved
    past the market (a tight trail raised from a bar's high on a bar that then closed lower) or price gapped -- fills at the open: a real
    stop order cannot do better than the market. Filling those at the stop price inflated every backtest and the paper account
    (2026-10-07: 41% of equity stop exits opened through the stop, by ~22 bp on average; docs/FILL_MODEL_AUDIT.md)."""
    if bar_open is None:
        return stop
    o = float(bar_open)
    if o != o:
        return stop
    return min(stop, o) if d == 1 else max(stop, o)


def bar_exit(stop: float, tp: float | None, bar_open, fav: float, adv: float, d: int) -> tuple[float, str] | None:
    """(fill price, "stop" | "tp") when this bar reaches the stop or the take-profit, else None.

    When one bar reaches both, the order inside it is unknown: the stop is taken first unless the bar opened at or beyond the target.
    (The backtests used to assume the target always came first.)"""
    stop_hit = adv <= stop if d == 1 else adv >= stop
    tp_hit = tp is not None and (fav >= tp if d == 1 else fav <= tp)
    if tp_hit and bar_open is not None and (float(bar_open) >= tp if d == 1 else float(bar_open) <= tp):
        return tp, "tp"
    if stop_hit:
        return stop_fill(stop, bar_open, d), "stop"
    if tp_hit:
        return tp, "tp"
    return None


def intrabar_exit(stop: float, tp: float | None, minutes, d: int) -> tuple[float, str] | None:
    """bar_exit settled minute by minute inside one bar. `minutes` = rows of (open, high, low) in time order (core.minute_bars). A minute that
    opens through the stop fills at that open -- price jumped through it inside the bar -- and within one minute the stop still comes before
    the target. None if no minute reaches either level."""
    for o, h, l in minutes:
        hit = bar_exit(stop, tp, o, h if d == 1 else l, l if d == 1 else h, d)
        if hit is not None:
            return hit
    return None


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


def open_after_entry(pos: dict, latest: dict) -> float | None:
    """The first price of `latest` the position could fill at: the bar's open, or on the entry bar the latest price (the only post-entry
    price known there, see _side). None when the candle has no open. A custom Strategy.manage() fills its stop with
    stop_fill(stop, open_after_entry(pos, candle), d)."""
    entry_bar = pos.get("entry_bar_ts")
    if entry_bar is not None:
        bar, ebar = _naive_pair(_ts(latest["timestamp"]), _ts(entry_bar))
        if bar == ebar:
            return float(latest["close"])
    return float(latest["open"]) if latest.get("open") is not None else None


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
        hit = bar_exit(pos["current_stop"], pos["tp"], open_after_entry(pos, latest), fav, adv, d)
        if hit is not None:
            return ExitDecision(hit[0], "take_profit" if hit[1] == "tp" else ("be_stop" if pos["armed_be"] else "initial_stop"))
    if max_hold_s is not None and (now - pos["entry_time"]).total_seconds() >= max_hold_s:
        return ExitDecision(close, "timeout_exit")
    if deadline is not None and now >= deadline:
        return ExitDecision(close, "eod_squareoff")
    if fav is None:
        return None                   # nothing post-entry to trail on yet

    if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
        pos["armed_be"] = True
        pos["current_stop"] = lock_stop(pos["entry_price"], pos["be"], d)
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
        return ExitDecision(stop_fill(pos["current_stop"], open_after_entry(pos, latest), d), "trail_stop" if pos["armed_trail"] else "initial_stop")
    if max_hold_s is not None and (now - pos["entry_time"]).total_seconds() >= max_hold_s:
        return ExitDecision(close, "timeout_exit")
    if deadline is not None and now >= deadline:
        return ExitDecision(close, "eod_squareoff")
    if fav is None:
        return None                   # nothing post-entry to trail on yet

    if not pos["armed_trail"] and (fav >= pos["activation_price"] if d == 1 else fav <= pos["activation_price"]):
        pos["armed_trail"] = True
        pos["current_stop"] = lock_stop(pos["entry_price"], pos["activation_price"], d)
    if pos["armed_trail"]:
        pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
        trail_dist = pos["trail_mult"] * max(atr, pos["stop_dist"] * 0.1)
        trail = pos["best_price"] - trail_dist * d
        pos["current_stop"] = max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail)
    return None


# ---------------------------------------------------------------------------------------------------------------------------------------
# Bar-sequence exit managers: what live strategies call. The stop is CHECKED on every scan against the bar-so-far, but it MOVES (breakeven
# arm, trail) only from a COMPLETED bar -- exactly the order the backtests use (bar i's adverse extreme is tested against the stop as it
# stood before bar i; bar i's favourable extreme then moves the stop for bar i+1 onward).
#
# Why (found 2026-10-05, measured on 1-minute data): the single-bar functions above were called with the still-FORMING 5-minute bar on
# every 30-second scan. A scan that saw the forming bar's high raised the stop; the NEXT scan of the same bar then tested that bar's LOW
# -- often made minutes BEFORE the high -- against the raised stop and closed the trade at a price that never traded after the stop moved.
# It cut winners short: live paper trades exited 1.5-6 minutes after entry with small gains while losses ran to the full stop. Replaying the
# backtests' own entries at 1-minute resolution, same entries, only the exit mechanics varied: commodity+currency (537 trades, 2026-05-18
# ..10-01) backtest-style exits +Rs63,012, live's same-bar mechanics -Rs57,282, these bar-sequence mechanics +Rs52,689; equity (208 trades,
# 2026-05-12..10-01) -Rs12,750 / -Rs72,875 / -Rs12,750. Moving the stop every minute, even with correct ordering, recovered only part of it
# (commodity+currency +Rs13,262): the validated edge depends on the trail being advanced once per 5-minute bar.
# ---------------------------------------------------------------------------------------------------------------------------------------
BAR_SECONDS = 300


def bar_complete_lag_s() -> float:
    """Seconds after a bar's nominal end before its candle counts as complete: Upstox publishes a minute's bar ~15-20 s after it ends
    (measured 2026-10-05), so the 5-minute candle is missing its last minute until then. A newer candle in the list settles it sooner."""
    return float(os.environ.get("LIVE_BAR_COMPLETE_LAG_S", "45"))


def _naive_pair(a: datetime, b: datetime) -> tuple[datetime, datetime]:
    if (a.tzinfo is None) != (b.tzinfo is None):
        return a.replace(tzinfo=None), b.replace(tzinfo=None)
    return a, b


def _is_complete(candles: list[dict], i: int, now: datetime, interval_s: int, lag_s: float) -> bool:
    if i < len(candles) - 1:
        return True                                              # a newer candle exists: this one is finished
    end, n = _naive_pair(_ts(candles[i]["timestamp"]) + timedelta(seconds=interval_s + lag_s), now)
    return n >= end


def _stop_hit(pos: dict, fav: float, adv: float, bar_open: float | None, d: int, check_tp: bool, armed_key: str, moved_reason: str) -> ExitDecision | None:
    hit = bar_exit(pos["current_stop"], pos["tp"] if check_tp else None, bar_open, fav, adv, d)
    if hit is None:
        return None
    if hit[1] == "tp":
        return ExitDecision(hit[0], "take_profit")
    return ExitDecision(hit[0], moved_reason if pos.get(armed_key) else "initial_stop")


def _raise(pos: dict, level: float, d: int) -> None:
    pos["current_stop"] = max(pos["current_stop"], level) if d == 1 else min(pos["current_stop"], level)


def _apply_fixed(pos: dict, fav: float, d: int) -> None:
    if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
        pos["armed_be"] = True
        _raise(pos, lock_stop(pos["entry_price"], pos["be"], d), d)
    if pos["armed_be"]:
        pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
        _raise(pos, pos["best_price"] - FIXED_TP_TRAIL_MULT * pos["stop_dist"] * d, d)


def _apply_activation(pos: dict, fav: float, atr: float, d: int) -> None:
    if not pos["armed_trail"] and (fav >= pos["activation_price"] if d == 1 else fav <= pos["activation_price"]):
        pos["armed_trail"] = True
        _raise(pos, lock_stop(pos["entry_price"], pos["activation_price"], d), d)
    if pos["armed_trail"]:
        pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
        _raise(pos, pos["best_price"] - pos["trail_mult"] * max(atr, pos["stop_dist"] * 0.1) * d, d)


def _manage_bars(pos: dict, candles: list[dict], now: datetime, *, kind: str, atr_of, close_at, max_hold_s,
                 interval_s: int, lag_s: float | None) -> ExitDecision | None:
    if not candles:
        return None
    lag_s = bar_complete_lag_s() if lag_s is None else lag_s
    d = 1 if pos["direction"] == "long" else -1
    armed_key, moved_reason, check_tp = ("armed_be", "be_stop", True) if kind == "fixed" else ("armed_trail", "trail_stop", False)
    if pos.get("entry_bar_ts") is None:                          # a row saved before entry bars were recorded: treat the bar we first see as the entry bar
        pos["entry_bar_ts"] = str(candles[-1]["timestamp"])
    ebar = _ts(pos["entry_bar_ts"])
    if pos.get("trail_bar_ts") is None:
        if pos.get(armed_key):
            # A position from before this field existed that has already moved its stop: re-testing old bars against today's stop would close it
            # on prices from before the move -- start from the newest completed bar instead.
            done = [str(c["timestamp"]) for i, c in enumerate(candles) if _is_complete(candles, i, now, interval_s, lag_s)]
            pos["trail_bar_ts"] = done[-1] if done else pos["entry_bar_ts"]
        else:
            pos["trail_bar_ts"] = pos["entry_bar_ts"]
    applied = _ts(pos["trail_bar_ts"])
    last = len(candles) - 1
    for i, c in enumerate(candles):
        ts = _ts(c["timestamp"])
        ts_cmp, e_cmp = _naive_pair(ts, ebar)
        if ts_cmp < e_cmp:
            continue
        if ts_cmp == e_cmp:
            if i == last:                                        # still inside the entry bar: only its latest price is known to be post-entry (see _side)
                px = float(c["close"])
                dec = _stop_hit(pos, px, px, px, d, check_tp, armed_key, moved_reason)
                if dec is not None:
                    return dec
            continue
        ts_cmp, a_cmp = _naive_pair(ts, applied)
        if ts_cmp <= a_cmp:
            continue                                             # already tested and applied on an earlier scan
        fav = float(c["high"]) if d == 1 else float(c["low"])
        adv = float(c["low"]) if d == 1 else float(c["high"])
        dec = _stop_hit(pos, fav, adv, float(c["open"]) if c.get("open") is not None else None, d, check_tp, armed_key, moved_reason)
        if dec is not None:
            return dec
        if not _is_complete(candles, i, now, interval_s, lag_s):
            break                                                # the forming bar: tested against the stop as it stood when the bar began, moves nothing
        if kind == "fixed":
            _apply_fixed(pos, fav, d)
        else:
            _apply_activation(pos, fav, atr_of(c), d)
        pos["trail_bar_ts"] = str(c["timestamp"])
        applied = ts
    close = float(candles[-1]["close"])
    if max_hold_s is not None and (now - pos["entry_time"]).total_seconds() >= max_hold_s:
        return ExitDecision(close, "timeout_exit")
    deadline = _deadline(now, close_at)
    if deadline is not None and now >= deadline:
        return ExitDecision(close, "eod_squareoff")
    return None


def fixed_tp_breakeven_trail_bars(pos: dict, candles: list[dict], now: datetime, *, close_at: tuple[int, int] | None,
                                  max_hold_s: float | None, interval_s: int = BAR_SECONDS, lag_s: float | None = None) -> ExitDecision | None:
    """fixed_tp_breakeven_trail over the candle list: checks every scan, moves the stop only on completed bars (see the block comment above).
    Persist pos["trail_bar_ts"] with the position so a restart continues from the last bar applied."""
    return _manage_bars(pos, candles, now, kind="fixed", atr_of=None, close_at=close_at, max_hold_s=max_hold_s, interval_s=interval_s, lag_s=lag_s)


def activation_trail_bars(pos: dict, candles: list[dict], atr_of, now: datetime, *, close_at: tuple[int, int] | None,
                          max_hold_s: float | None, interval_s: int = BAR_SECONDS, lag_s: float | None = None) -> ExitDecision | None:
    """activation_trail over the candle list. `atr_of(candle)` returns that COMPLETED bar's ATR -- the trail distance for the next bar
    comes from the bar that moved it, as in the equity backtest (markets/equity/strategies/tf_5min/scalping/backtest.py: cur_atr = atrs[i])."""
    return _manage_bars(pos, candles, now, kind="activation", atr_of=atr_of, close_at=close_at, max_hold_s=max_hold_s,
                        interval_s=interval_s, lag_s=lag_s)
