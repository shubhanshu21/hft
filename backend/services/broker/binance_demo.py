"""Binance DEMO trading client (spot + USDT-M futures): real API calls against Binance's own demo exchange, never the live one.

    Spot demo REST     https://demo-api.binance.com/api        (docs: developers.binance.com/docs/binance-spot-api-docs/demo-mode/general-info)
    Futures demo REST  https://demo-fapi.binance.com           (docs: developers.binance.com/docs/derivatives/usds-margined-futures/general-info)
Demo keys are created by the account owner at demo.binance.com -> API Key Management; they are separate from production keys and cannot touch real money.

SAFETY: every request goes through `_request`, which refuses any host that is not one of the two demo hosts above. There is no code path in this module to api.binance.com / fapi.binance.com,
so a mistaken URL or a production key pasted by accident cannot place a real order. Signed requests use HMAC-SHA256 over the query string with the X-MBX-APIKEY header.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import time
from urllib.parse import urlencode, urlparse

import requests

log = logging.getLogger("binance_demo")
SPOT_BASE = "https://demo-api.binance.com"
FUTURES_BASE = "https://demo-fapi.binance.com"
ALLOWED_HOSTS = frozenset({"demo-api.binance.com", "demo-fapi.binance.com"})


class BinanceError(RuntimeError):
    def __init__(self, status: int, code, msg: str):
        super().__init__(f"Binance demo HTTP {status} code {code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


def floor_step(qty: float, step: float) -> float:
    """Round a quantity DOWN to the exchange's step (never up: an order must not exceed what the ledger holds)."""
    if step <= 0:
        return qty
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.floor(qty / step + 1e-9) * step, decimals)


class BinanceDemo:
    def __init__(self, api_key: str, api_secret: str, spot_base: str = SPOT_BASE, futures_base: str = FUTURES_BASE, session: requests.Session | None = None, timeout: float = 20.0):
        if not api_key or not api_secret:
            raise ValueError("Binance demo API key and secret are required")
        self.key, self.secret = api_key, api_secret.encode()
        self.spot_base, self.futures_base = spot_base.rstrip("/"), futures_base.rstrip("/")
        self.session, self.timeout = session or requests.Session(), timeout
        self._filters: dict[tuple[str, str], dict] = {}
        for base in (self.spot_base, self.futures_base):
            self._check_host(base)

    def sign(self, query: str) -> str:
        return hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()

    # -- transport -----------------------------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _check_host(url: str) -> None:
        host = urlparse(url).hostname or ""
        if host not in ALLOWED_HOSTS:
            raise ValueError(f"refusing {host!r}: this client only talks to the Binance DEMO hosts {sorted(ALLOWED_HOSTS)}")

    def _request(self, base: str, method: str, path: str, params: dict | None = None, signed: bool = False, retries: int = 3):
        url = base + path
        self._check_host(url)
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(retries):
            q = dict(params)
            headers = {"X-MBX-APIKEY": self.key}
            if signed:
                q["timestamp"] = int(time.time() * 1000)
                q["recvWindow"] = 10000
                query = urlencode(q)
                q_final = query + "&signature=" + self.sign(query)
            else:
                q_final = urlencode(q)
            resp = self.session.request(method, url + ("?" + q_final if q_final else ""), headers=headers, timeout=self.timeout)
            if resp.status_code >= 500 and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {"msg": resp.text[:200]}
            if resp.status_code != 200:
                raise BinanceError(resp.status_code, body.get("code") if isinstance(body, dict) else None, body.get("msg") if isinstance(body, dict) else str(body))
            return body
        raise BinanceError(0, None, "no response")

    # -- exchange rules --------------------------------------------------------------------------------------------------------------------------------------
    def filters(self, venue: str, symbol: str) -> dict:
        """{'step', 'min_qty', 'min_notional'} from the demo exchange's own filters (cached)."""
        if (venue, symbol) not in self._filters:
            base, path = (self.spot_base, "/api/v3/exchangeInfo") if venue == "spot" else (self.futures_base, "/fapi/v1/exchangeInfo")
            info = self._request(base, "GET", path, {"symbol": symbol} if venue == "spot" else None)
            sym = next(s for s in info["symbols"] if s["symbol"] == symbol)
            f = {x["filterType"]: x for x in sym["filters"]}
            lot = f.get("MARKET_LOT_SIZE") if float((f.get("MARKET_LOT_SIZE") or {}).get("stepSize", 0) or 0) > 0 else f["LOT_SIZE"]
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._filters[(venue, symbol)] = {"step": float(lot["stepSize"]), "min_qty": float(lot["minQty"]), "min_notional": float(notional.get("minNotional") or notional.get("notional") or 0.0)}
        return self._filters[(venue, symbol)]

    def price(self, venue: str, symbol: str) -> float:
        base, path = (self.spot_base, "/api/v3/ticker/price") if venue == "spot" else (self.futures_base, "/fapi/v1/ticker/price")
        return float(self._request(base, "GET", path, {"symbol": symbol})["price"])

    # -- account -----------------------------------------------------------------------------------------------------------------------------------------------
    def spot_balance(self, asset: str) -> float:
        acct = self._request(self.spot_base, "GET", "/api/v3/account", signed=True)
        return next((float(b["free"]) + float(b["locked"]) for b in acct["balances"] if b["asset"] == asset), 0.0)

    def futures_position(self, symbol: str) -> dict:
        rows = self._request(self.futures_base, "GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        r = rows[0] if rows else {}
        return {"amt": float(r.get("positionAmt", 0.0)), "entry": float(r.get("entryPrice", 0.0)), "liquidation": float(r.get("liquidationPrice", 0.0) or 0.0), "leverage": r.get("leverage")}

    def futures_wallet(self) -> dict:
        a = self._request(self.futures_base, "GET", "/fapi/v2/account", signed=True)
        return {"wallet": float(a.get("totalWalletBalance", 0.0)), "available": float(a.get("availableBalance", 0.0)), "unrealized": float(a.get("totalUnrealizedProfit", 0.0))}

    def futures_income(self, since_ms: int, symbol: str | None = None) -> float:
        """Funding fees received (+) / paid (-) since `since_ms`, in USDT."""
        rows = self._request(self.futures_base, "GET", "/fapi/v1/income", {"incomeType": "FUNDING_FEE", "symbol": symbol, "startTime": since_ms, "limit": 1000}, signed=True)
        return sum(float(r["income"]) for r in rows)

    def set_isolated(self, symbol: str, leverage: int) -> None:
        try:
            self._request(self.futures_base, "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except BinanceError as exc:
            if exc.code != -4046:                                            # -4046 = "No need to change margin type": already isolated
                raise
        self._request(self.futures_base, "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}, signed=True)

    # -- orders (market) ---------------------------------------------------------------------------------------------------------------------------------------------
    def spot_market(self, symbol: str, side: str, qty: float) -> dict:
        r = self._request(self.spot_base, "POST", "/api/v3/order", {"symbol": symbol, "side": side, "type": "MARKET", "quantity": f"{qty:.8f}".rstrip("0").rstrip("."), "newOrderRespType": "FULL"}, signed=True)
        fills = r.get("fills") or []
        executed = float(r["executedQty"])
        quote = float(r["cummulativeQuoteQty"])
        base_asset = symbol[:-4]
        fee = 0.0
        for f in fills:
            c, asset = float(f["commission"]), f["commissionAsset"]
            fee += c if asset == "USDT" else c * float(f["price"]) if asset == base_asset else 0.0
        return {"qty": executed, "avg_price": quote / executed if executed else 0.0, "notional": quote, "fee": fee, "order_id": r["orderId"], "status": r["status"]}

    def futures_market(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> dict:
        r = self._request(self.futures_base, "POST", "/fapi/v1/order", {"symbol": symbol, "side": side, "type": "MARKET", "quantity": f"{qty:.8f}".rstrip("0").rstrip("."),
                                                                          "reduceOnly": "true" if reduce_only else None, "newOrderRespType": "RESULT"}, signed=True)
        executed = float(r.get("executedQty", 0.0))
        avg = float(r.get("avgPrice", 0.0) or 0.0)
        if not avg and executed:
            avg = float(r.get("cumQuote", 0.0)) / executed
        fee = 0.0
        # The demo answers a market order before the fill details are final (avgPrice 0), so the fills themselves -- price, quantity and commission -- are read from the trade list.
        for attempt in range(4):
            try:
                trades = self._request(self.futures_base, "GET", "/fapi/v1/userTrades", {"symbol": symbol, "orderId": r["orderId"]}, signed=True)
            except BinanceError as exc:
                log.warning("could not read the futures fills for order %s: %s", r.get("orderId"), exc)
                break
            if trades:
                q = sum(float(t["qty"]) for t in trades)
                if q:
                    executed, avg = q, sum(float(t["price"]) * float(t["qty"]) for t in trades) / q
                fee = sum(float(t["commission"]) for t in trades if t.get("commissionAsset") == "USDT")
                break
            time.sleep(0.5)
        return {"qty": executed, "avg_price": avg, "notional": executed * avg, "fee": fee, "order_id": r["orderId"], "status": r.get("status")}
