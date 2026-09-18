"""
utils/position_reconciliation.py — detect drift between what this app's own
DB/in-memory state THINKS is open and what the real broker actually shows.

Built 2026-09-18 for the scenario: a real position opened by live_trading.py
gets manually closed (or partially closed) directly in the Upstox app/site,
completely outside this code's control. Before this existed, live_trading.py
had no way to notice -- it would keep tracking that symbol as open, and
eventually, once its simulated TP/SL/timeout condition fired, try to place a
REAL CLOSING ORDER against a position that no longer exists. For an
intraday (MIS) product that doesn't just fail -- it OPENS A BRAND NEW
POSITION in the opposite direction, untracked, with no stop-loss or
take-profit recorded for it anywhere. That's a genuinely dangerous failure
mode, not a cosmetic one.

broker.get_broker_positions() (broker/upstox_broker.py) is the real source
of truth this reconciles against -- Upstox's own book, not anything this
app remembers placing.
"""
from __future__ import annotations

from broker.upstox_broker import UpstoxBroker


def find_discrepancies(
    broker: UpstoxBroker,
    tracked_positions: dict[str, dict],
    symbol_map: dict[str, str] | None = None,
) -> dict[str, dict]:
    """
    Compares `tracked_positions` (this app's own {symbol: {"direction",
    "qty", "instrument_key", ...}} view of what's open) against the REAL
    broker-side net quantity per instrument.

    Uses each position's own `instrument_key` (the exact contract it was
    actually opened on) when present, rather than `symbol_map` -- a fresh
    symbol->instrument lookup can point at a DIFFERENT (rolled-over)
    contract than the one really held, which would incorrectly compare
    against an unrelated instrument's broker-side quantity. `symbol_map` is
    kept only as a fallback for callers/positions that don't carry their own
    instrument_key (e.g. a position dict built before this field existed).

    Returns {symbol: {"kind": ..., "tracked_qty": int, "broker_qty": int}}
    for every symbol where they disagree. `kind` is one of:
      - "externally_closed"  -- this app thinks it's open, broker shows flat.
        Most likely cause: someone closed it manually outside this process.
      - "externally_reduced" -- broker shows a smaller position in the same
        direction than tracked (a partial manual close).
      - "externally_reversed" -- broker shows the OPPOSITE direction or a
        LARGER quantity than tracked -- something placed additional real
        orders on this instrument outside this process entirely.
      - "unknown"  -- get_broker_positions() itself failed (network/auth).
        Caller must treat this as "couldn't check right now", not "confirmed
        no discrepancy" -- see get_broker_positions' own docstring.

    Does not touch tracked_positions or place any orders -- read-only
    comparison. The caller decides what to do with each discrepancy.
    """
    broker_positions = broker.get_broker_positions()
    if broker_positions is None:
        return {sym: {"kind": "unknown", "tracked_qty": None, "broker_qty": None} for sym in tracked_positions}

    discrepancies: dict[str, dict] = {}
    for sym, pos in tracked_positions.items():
        ikey = pos.get("instrument_key") or (symbol_map or {}).get(sym)
        if not ikey:
            continue
        tracked_qty_signed = pos["qty"] * (1 if pos["direction"] == "long" else -1)
        broker_qty_signed = broker_positions.get(ikey, 0)

        if broker_qty_signed == tracked_qty_signed:
            continue  # matches -- no discrepancy

        if broker_qty_signed == 0:
            kind = "externally_closed"
        elif (broker_qty_signed > 0) == (tracked_qty_signed > 0) and abs(broker_qty_signed) < abs(tracked_qty_signed):
            kind = "externally_reduced"
        else:
            kind = "externally_reversed"

        discrepancies[sym] = {
            "kind": kind,
            "tracked_qty": tracked_qty_signed,
            "broker_qty": broker_qty_signed,
        }
    return discrepancies
