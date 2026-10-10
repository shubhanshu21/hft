"""core.strategy.MarketData on a runner's broker connection: other symbols' 5-minute candles and the live best bid/ask."""
from __future__ import annotations

from typing import Callable

from core.strategy import MarketData


class BrokerMarketData(MarketData):
    def __init__(self, candles_fn: Callable[[str], list[dict]], broker, symbol_map: dict[str, str]):
        self._candles_fn, self._broker, self._map = candles_fn, broker, symbol_map

    def candles(self, symbol: str) -> list[dict]:
        try:
            return self._candles_fn(symbol) or []
        except Exception:
            return []

    def quote(self, symbol: str) -> dict | None:
        key = self._map.get(symbol)
        if not key or not hasattr(self._broker, "get_market_depth"):
            return None
        d = self._broker.get_market_depth(key)
        if not d:
            return None
        bids = [(float(x["price"]), int(x.get("qty") or 0)) for x in d.get("buy", []) if x.get("price")]
        asks = [(float(x["price"]), int(x.get("qty") or 0)) for x in d.get("sell", []) if x.get("price")]
        if not bids or not asks:
            return None
        return {"bid": bids[0][0], "ask": asks[0][0], "bid_qty": bids[0][1], "ask_qty": asks[0][1], "bids": bids, "asks": asks,
                "ltp": d.get("last_price")}
