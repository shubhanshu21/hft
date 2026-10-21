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
            raise RuntimeError(f"Delta demo API error {r.status_code} on {method} {path}: {data.get('error')}")
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
