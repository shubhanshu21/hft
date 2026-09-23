"""NSE currency scalping -- the same trend-breakout scalper as commodity (entry_signal handles both
by symbol), with the currency session close (16:50 IST) and the NCD cost model."""
from __future__ import annotations

from markets.commodity.scalping.strategy import McxScalping
from markets.currency.costs import CURRENCY_SPECS, compute_ncd_currency_costs


class NcdScalping(McxScalping):
    market = "currency"
    close_at = (16, 50)         # NSE currency closes 17:00 IST

    def lot_size(self, sym: str) -> int:
        return CURRENCY_SPECS.get(sym.upper(), {}).get("lot_size", 1000)

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_ncd_currency_costs(symbol, direction, entry, exit_price, qty)


STRATEGY = NcdScalping()
