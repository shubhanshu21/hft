"""What quantity Upstox expects in an ORDER, per market.

Upstox's Place Order V3 documentation: "For commodity - number of LOTS is accepted. For other Futures & Options and equities - number of UNITS."
(MCX commodity, the one LOTS market, was removed 2026-10-12.)

  equity      shares                                   (the bot's qty already is shares)
  currency    lots x lot size (units), by default      (V3 rule for non-commodity F&O; consistent with Upstox's brokerage calculator, where qty 1000 = one
                                                        USDINR lot). An Upstox staff answer from the v2 era says currency "takes lot size as inputs", so this is
                                                        the ONE unverified case: LIVE_CURRENCY_QTY_MODE=lots switches it, and it must be confirmed with a
                                                        1-lot order before any live trading is armed.

The margin and brokerage calculators use different units again (margin: lots; brokerage: units) -- see engine/margin_rates.py.

Sources, dates and the pre-live checklist (including that a static IP is required): docs/UPSTOX_LIVE_READINESS.md.

Also UNVERIFIED: services/utils/position_reconciliation.py compares the bot's lots with `get_positions().quantity`; whether Upstox reports NCD
positions in lots or units cannot be known without a real position. Confirm both with a single 1-lot order before arming live trading.
"""
from __future__ import annotations

import os


def broker_quantity(market: str, lots: int, lot_size_multiplier: int) -> int:
    """The integer `quantity` to send to Upstox for `lots` lots (shares, for equity) of a contract whose cost-model multiplier is `lot_size_multiplier`."""
    if lots < 1:
        return 0
    if market == "currency":
        mode = os.environ.get("LIVE_CURRENCY_QTY_MODE", "units").strip().lower()
        return int(lots) if mode == "lots" else int(lots * lot_size_multiplier)
    return int(lots)                                   # equity: shares
