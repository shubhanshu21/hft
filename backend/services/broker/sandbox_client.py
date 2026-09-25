"""Upstox SANDBOX order client (https://api-sandbox.upstox.com), used only to rehearse and validate order requests.

What the sandbox is (Upstox docs + staff, 2025-09-30): "purely a simulation layer" for the place / modify / cancel order APIs. It validates a request the way the live
API does and returns a mock order id; it never matches or fills, so there are no positions, trades or P&L (it is NOT a paper-trading engine). It needs its own token
(Developer Apps -> sandbox app -> Generate, valid 30 days) that cannot trade live. Because it is a different host with its own limits, its calls are deliberately NOT
counted against the live API budget (engine/api_budget.py).

Everything returns a plain dict and never raises: a sandbox problem must never touch paper trading.
"""
from __future__ import annotations

import json
import os
import time

import upstox_client
from upstox_client.rest import ApiException

CONNECT_S, READ_S = 5.0, 15.0


class _TimeoutClient(upstox_client.ApiClient):
    """Timeouts only -- no metering (this is not the live API host)."""

    def call_api(self, *args, **kwargs):
        if kwargs.get("_request_timeout") is None:
            kwargs["_request_timeout"] = (CONNECT_S, READ_S)
        return super().call_api(*args, **kwargs)


def token() -> str:
    return os.environ.get("UPSTOX_SANDBOX_TOKEN", "").strip()


def enabled() -> bool:
    """True only when a sandbox token is configured AND SANDBOX_REHEARSAL is not switched off."""
    return bool(token()) and os.environ.get("SANDBOX_REHEARSAL", "true").strip().lower() not in ("0", "false", "no", "off")


def _api(tok: str | None = None) -> upstox_client.OrderApiV3:
    cfg = upstox_client.Configuration(sandbox=True)
    cfg.access_token = tok or token()
    return upstox_client.OrderApiV3(_TimeoutClient(cfg))


def _error_from(exc: ApiException) -> tuple[str | None, str]:
    """(errorCode, message) from an Upstox error body, else (None, reason)."""
    try:
        body = json.loads(exc.body or b"{}")
        first = (body.get("errors") or [{}])[0]
        return first.get("errorCode"), first.get("message") or exc.reason or ""
    except (ValueError, TypeError, AttributeError):
        return None, str(getattr(exc, "reason", "") or exc)


def place(instrument_key: str, quantity: int, transaction_type: str, product: str = "I", order_type: str = "MARKET", price: float = 0.0,
          tag: str = "", trigger_price: float = 0.0, api=None) -> dict:
    """Send one order to the SANDBOX. Returns {ok, order_id, http_status, error_code, message, latency_ms, request}."""
    request = {"instrument_key": instrument_key, "quantity": int(quantity), "transaction_type": transaction_type, "product": product,
               "order_type": order_type, "price": price}
    t0 = time.monotonic()
    try:
        body = upstox_client.PlaceOrderV3Request(
            quantity=int(quantity), product=product, validity="DAY", price=price, tag=(tag or "")[:16], instrument_token=instrument_key,
            order_type=order_type, transaction_type=transaction_type, disclosed_quantity=0, trigger_price=trigger_price, is_amo=False, slice=True)
        resp = (api or _api()).place_order(body)
        ids = getattr(getattr(resp, "data", None), "order_ids", None) or []
        return {"ok": True, "order_id": ids[0] if ids else None, "http_status": 200, "error_code": None, "message": "accepted",
                "latency_ms": round((time.monotonic() - t0) * 1000), "request": request}
    except ApiException as exc:
        code, msg = _error_from(exc)
        return {"ok": False, "order_id": None, "http_status": exc.status, "error_code": code, "message": msg,
                "latency_ms": round((time.monotonic() - t0) * 1000), "request": request}
    except Exception as exc:                                    # network, timeout, bad token format ...
        return {"ok": False, "order_id": None, "http_status": None, "error_code": type(exc).__name__, "message": str(exc)[:200] or "no message",
                "latency_ms": round((time.monotonic() - t0) * 1000), "request": request}
