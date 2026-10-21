"""services/broker/delta_broker.py -- Delta Exchange India demo/testnet REST client (paper trading only).

Hardcoded to the testnet host (cdn-ind.testnet.deltaex.org) -- there is deliberately no "live" mode or live host anywhere in this
file, the same spirit as UpstoxBroker always being constructed dry_run=True: a bug here can place a wrong-sized demo order, never a
real one. Keys come from DELTA_DEMO_API_KEY / DELTA_DEMO_API_SECRET in .env (never a live-account key -- see CLAUDE.md).

Covers only the 6 coins Delta's demo environment actually lists as perpetual futures (2026-10-21: BTC, ETH, SOL, XRP, DOGE, ADA --
16 products total on testnet vs 237 on the real exchange); engine/crypto_dip_reversal.py falls back to its own simulation for any
coin not in PRODUCTS.

Signing (docs.delta.exchange): signature = HMAC-SHA256(secret, method + timestamp(seconds) + path + query + body), hex digest, sent
as the `signature` header alongside `api-key` and `timestamp`. A request is only valid within 5 seconds of its timestamp.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

import requests

_log = logging.getLogger("delta_broker")


class DeltaAPIError(RuntimeError):
    """Carries the parsed {"code": ..., "context": {...}} Delta returns, so callers can react to e.g. insufficient_margin instead of
    just parsing the message string."""

    def __init__(self, status: int, method: str, path: str, error: dict):
        self.status, self.error = status, error or {}
        super().__init__(f"Delta demo API error {status} on {method} {path}: {error}")

HOST = "https://cdn-ind.testnet.deltaex.org"      # testnet ONLY -- no live host constant exists in this file, by design

PRODUCTS = {       # symbol -> (product_id, contract_value in base units per contract); from /v2/products on testnet, 2026-10-21
    "BTC": (84, 0.001), "ETH": (1699, 0.01), "SOL": (92572, 1), "XRP": (93723, 1), "DOGE": (101555, 100), "ADA": (101760, 1),
}


class DeltaDemoBroker:
    """One instance per process; reads its key/secret from the environment at construction time."""

    def __init__(self):
        self.key = os.environ.get("DELTA_DEMO_API_KEY", "")
        self.secret = os.environ.get("DELTA_DEMO_API_SECRET", "")
        if not self.key or not self.secret:
            raise RuntimeError("DELTA_DEMO_API_KEY / DELTA_DEMO_API_SECRET not set in .env")

    def _sign(self, method: str, path: str, query: str, body: str, ts: str) -> str:
        msg = method + ts + path + query + body
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, query: str = "", body: dict | None = None) -> dict:
        ts = str(int(time.time()))
        body_str = "" if body is None else __import__("json").dumps(body, separators=(",", ":"))
        sig = self._sign(method, path, query, body_str, ts)
        headers = {"api-key": self.key, "signature": sig, "timestamp": ts, "User-Agent": "hft-dip-reversal/1.0", "Content-Type": "application/json"}
        r = requests.request(method, f"{HOST}{path}{query}", headers=headers, data=body_str or None, timeout=15)
        data = r.json()
        if not data.get("success"):
            raise DeltaAPIError(r.status_code, method, path, data.get("error"))
        return data.get("result")

    def balances(self) -> dict:
        """{asset_symbol: balance} for every nonzero balance."""
        rows = self._request("GET", "/v2/wallet/balances") or []
        return {b["asset_symbol"]: float(b["balance"]) for b in rows if float(b.get("balance", 0)) != 0}

    def positions(self) -> dict:
        """{coin: {"size": contracts (signed, +long/-short), "entry_price": float}} for every open demo position."""
        rows = self._request("GET", "/v2/positions/margined") or []
        out = {}
        for p in rows:
            size = float(p.get("size", 0))
            if size == 0:
                continue
            sym = (p.get("product") or {}).get("underlying_asset", {}).get("symbol", "")
            if sym:
                out[sym] = {"size": size, "entry_price": float(p.get("entry_price") or 0)}
        return out

    def place_market_order(self, coin: str, side: str, contracts: int) -> dict:
        """side: 'buy' or 'sell'. Returns the order's result dict (id, state, average_fill_price once filled)."""
        if coin not in PRODUCTS:
            raise ValueError(f"{coin} is not on Delta's testnet product list")
        product_id, _ = PRODUCTS[coin]
        body = {"product_id": product_id, "size": int(contracts), "side": side, "order_type": "market_order"}
        return self._request("POST", "/v2/orders", body=body)

    def place_market_order_fit_margin(self, coin: str, side: str, contracts: int) -> dict:
        """Like place_market_order, but if the account doesn't have enough margin for the full size (the demo account's seeded
        funds are small relative to a RISK_PCT-sized position -- 2026-10-21), scale down to what it can actually afford using the
        available_balance/required_additional_balance Delta's own error tells us, and place that instead. Raises if even 1
        contract doesn't fit."""
        try:
            return self.place_market_order(coin, side, contracts)
        except DeltaAPIError as e:
            if e.error.get("code") != "insufficient_margin" or contracts <= 1:
                raise
            ctx = e.error.get("context") or {}
            avail = float(ctx.get("available_balance", 0))
            extra = float(ctx.get("required_additional_balance", 0))
            total_needed = avail + extra
            if total_needed <= 0:
                raise
            scaled = max(1, int(contracts * avail / total_needed * 0.95))   # 5% headroom for price movement between the two calls
            scaled = min(scaled, contracts - 1) if scaled >= contracts else scaled
            if scaled < 1:
                raise
            _log.warning("%s: full size %d contracts needs more margin than available -- placing %d instead", coin, contracts, scaled)
            return self.place_market_order(coin, side, scaled)

    def place_limit_order(self, coin: str, side: str, contracts: int, limit_price: float, reduce_only: bool = False) -> dict:
        """A resting GTC limit order (the take-profit: a maker fill if it's reached, not a market/taker exit)."""
        if coin not in PRODUCTS:
            raise ValueError(f"{coin} is not on Delta's testnet product list")
        product_id, _ = PRODUCTS[coin]
        body = {"product_id": product_id, "size": int(contracts), "side": side, "order_type": "limit_order",
                "limit_price": str(round(limit_price, 8)), "time_in_force": "gtc", "reduce_only": reduce_only}
        return self._request("POST", "/v2/orders", body=body)

    def get_order(self, order_id: int) -> dict:
        return self._request("GET", f"/v2/orders/{order_id}")

    def cancel_order(self, coin: str, order_id: int) -> dict:
        product_id, _ = PRODUCTS[coin]
        return self._request("DELETE", "/v2/orders", body={"id": order_id, "product_id": product_id})

    def close_position(self, coin: str) -> dict | None:
        """Flatten an open position at market, whichever side it's on. No-op (returns None) if already flat."""
        pos = self.positions().get(coin)
        if not pos or pos["size"] == 0:
            return None
        side = "sell" if pos["size"] > 0 else "buy"
        return self.place_market_order(coin, side, abs(int(pos["size"])))

    def ticker(self, coin: str) -> dict:
        r = requests.get(f"{HOST}/v2/tickers/{coin}USD", timeout=10)
        data = r.json()
        return data.get("result") or {}
