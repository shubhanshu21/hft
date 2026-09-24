"""NSE currency scalping -- the same trend-breakout scalper as commodity (entry_signal handles both
by symbol), with the currency session hours (read from Upstox, core/sessions.py) and the NCD cost model."""
from __future__ import annotations

from markets.commodity.scalping.strategy import McxScalping
from markets.currency.costs import CURRENCY_SPECS, compute_ncd_currency_costs


class NcdScalping(McxScalping):
    market = "currency"

    def lot_size(self, sym: str) -> int:
        from markets.currency.costs import get_contract_multiplier
        return get_contract_multiplier(sym)

    def costs(self, symbol: str, direction: str, entry: float, exit_price: float, qty: int) -> dict:
        return compute_ncd_currency_costs(symbol, direction, entry, exit_price, qty)


STRATEGY = NcdScalping()
