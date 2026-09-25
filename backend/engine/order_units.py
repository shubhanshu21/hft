"""What quantity Upstox expects in an ORDER, per market.

Upstox's Place Order V3 documentation: "For commodity - number of LOTS is accepted. For other Futures & Options and equities - number of UNITS."
Until 2026-09-25 the live path sent lots x lot_size for every market, i.e. a 5-lot crude order as quantity 50 = 50 lots, ten times too large
(live trading has always been blocked by safety_gate, which is the only reason nothing was placed).

  equity      shares                                   (the bot's qty already is shares)
  commodity   number of lots                           (documented)
  currency    lots x lot size (units), by default      (V3 rule for non-commodity F&O; consistent with Upstox's brokerage calculator, where qty 1000 = one
                                                        USDINR lot). An Upstox staff answer from the v2 era says currency "takes lot size as inputs", so this is
                                                        the ONE unverified case: LIVE_CURRENCY_QTY_MODE=lots switches it, and it must be confirmed with a
                                                        1-lot order before any live trading is armed.

The margin and brokerage calculators use different units again (margin: lots; brokerage: units) -- see engine/margin_rates.py and engine/cost_drift.py.

Also UNVERIFIED: services/utils/position_reconciliation.py compares the bot's lots with `get_positions().quantity`; whether Upstox reports MCX / NCD
positions in lots or units cannot be known without a real position. Confirm both with a single 1-lot order before arming live trading.
"""
from __future__ import annotations

import os


def broker_quantity(market: str, lots: int, lot_size_multiplier: int) -> int:
    """The integer `quantity` to send to Upstox for `lots` lots (shares, for equity) of a contract whose cost-model multiplier is `lot_size_multiplier`."""
    if lots < 1:
        return 0
    if market == "commodity":
        return int(lots)
    if market == "currency":
        mode = os.environ.get("LIVE_CURRENCY_QTY_MODE", "units").strip().lower()
        return int(lots) if mode == "lots" else int(lots * lot_size_multiplier)
    return int(lots)                                   # equity: shares
