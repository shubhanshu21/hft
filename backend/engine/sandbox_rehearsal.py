"""Rehearse every paper order against Upstox's SANDBOX, so the paper bot proves its real order requests are valid.

When UPSTOX_SANDBOX_TOKEN is set (and SANDBOX_REHEARSAL is not off), each paper entry and exit also sends the equivalent order -- the quantity the live path would send
(engine/order_units.py), product I, order type MARKET -- to the sandbox in a background thread and records Upstox's verdict in var/logs/sandbox_orders.jsonl. It cannot
change paper trading: it never blocks, never raises and never touches fills, P&L or positions (the sandbox does not create any -- see services/broker/sandbox_client.py).
What it gives: proof the payload (instrument key, quantity, product, order type) is accepted, and an early warning if Upstox starts rejecting a segment.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from core.paths import LOG_DIR
from engine.order_units import broker_quantity
from services.broker import sandbox_client

log = logging.getLogger("sandbox_rehearsal")
IST = timezone(timedelta(hours=5, minutes=30))
PATH = LOG_DIR / "sandbox_orders.jsonl"
_pool: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sandbox")       # one at a time: the sandbox order group is limited to 10 requests/s
    return _pool


def _record(row: dict, path=None) -> None:
    try:
        with open(path or PATH, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def submit(kind: str, symbol: str, market: str, instrument_key: str | None, side: str, lots: int, multiplier: int, position_id: str = "",
           order_type: str = "MARKET", place=None, path=None, wait: bool = False):
    """Rehearse one order. Returns the Future (or the result dict if wait=True), or None when rehearsal is off / nothing to send."""
    if not sandbox_client.enabled() or not instrument_key:
        return None
    quantity = broker_quantity(market, lots, multiplier)
    if quantity < 1:
        return None
    send = place or sandbox_client.place

    def work() -> dict:
        try:
            r = send(instrument_key, quantity, side, "I", order_type, 0.0, tag=f"REH_{kind}"[:16])
        except Exception as exc:                               # sandbox_client never raises; belt and braces
            r = {"ok": False, "order_id": None, "http_status": None, "error_code": type(exc).__name__, "message": str(exc)[:200], "latency_ms": 0}
        _record({"ts": datetime.now(IST).isoformat(timespec="seconds"), "kind": kind, "symbol": symbol, "market": market, "side": side, "lots": lots,
                 "quantity": quantity, "order_type": order_type, "position_id": position_id, "ok": r["ok"], "order_id": r.get("order_id"),
                 "http_status": r.get("http_status"), "error_code": r.get("error_code"), "message": r.get("message"), "latency_ms": r.get("latency_ms")}, path)
        if not r["ok"]:
            log.warning("sandbox rejected %s %s %s x%s: [%s] %s", kind, side, symbol, quantity, r.get("error_code"), r.get("message"))
        return r

    if wait:
        return work()
    fut: Future = _executor().submit(work)
    return fut


def read(limit: int = 500, path=None) -> list[dict]:
    try:
        lines = (path or PATH).read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("ok")]
    bad = [r for r in rows if not r.get("ok")]
    reasons: dict[str, int] = {}
    for r in bad:
        key = f"{r.get('error_code') or 'error'}: {(r.get('message') or '')[:80]}"
        reasons[key] = reasons.get(key, 0) + 1
    return {"total": len(rows), "accepted": len(ok), "rejected": len(bad),
            "by_reason": [{"reason": k, "n": v} for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])],
            "recent": list(reversed(rows))[:15]}
