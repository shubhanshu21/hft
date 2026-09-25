"""A record of entry signals that did NOT become trades, and why -- so "does crude block silver?" is measured, not guessed.

Margin is one pool (~Rs100k; one crude lot is Rs28k, one SILVERMIC lot Rs30k, both sized to fill it), so an open position leaves too little free margin
for another entry: sizing then yields zero lots and, until now, the signal simply vanished with no trace. `record()` appends one JSON line per skipped
signal to var/logs/blocked_entries.jsonl (deduplicated per symbol and bar); the dashboard reads it. `summarize()` turns it into counts per blocker.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core.paths import LOG_DIR

IST = timezone(timedelta(hours=5, minutes=30))
PATH = LOG_DIR / "blocked_entries.jsonl"
_seen: set[tuple] = set()


def record(symbol: str, market: str, reason: str, detail: str, holding: list[dict], bar_ts: str | None = None, direction: str | None = None,
           wanted_qty: int | None = None, got_qty: int | None = None, path=None, now: datetime | None = None) -> bool:
    """Append one line unless this (symbol, bar, reason) was already recorded. Never raises: a full disk must not stop trading. Returns True if written."""
    key = (symbol, bar_ts, reason)
    if key in _seen:
        return False
    _seen.add(key)
    if len(_seen) > 5000:
        _seen.clear()
    row = {"ts": (now or datetime.now(IST)).isoformat(timespec="seconds"), "symbol": symbol, "market": market, "reason": reason, "detail": detail,
           "direction": direction, "wanted_qty": wanted_qty, "got_qty": got_qty,
           "holding": [{"symbol": p["symbol"], "margin": round(float(p.get("margin_used") or 0.0))} for p in holding]}
    try:
        with open(path or PATH, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        return True
    except OSError:
        return False


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
    """{'total': n, 'by_symbol': [{symbol, n}], 'by_blocker': [{blocker, n}], 'recent': [...newest first]} -- 'blocker' = the position(s) holding the margin."""
    by_symbol: dict[str, int] = {}
    by_blocker: dict[str, int] = {}
    for r in rows:
        by_symbol[r["symbol"]] = by_symbol.get(r["symbol"], 0) + 1
        for h in r.get("holding") or [{"symbol": "(none)"}]:
            by_blocker[h["symbol"]] = by_blocker.get(h["symbol"], 0) + 1
    rank = lambda d, k: sorted(({k: a, "n": b} for a, b in d.items()), key=lambda x: -x["n"])
    return {"total": len(rows), "by_symbol": rank(by_symbol, "symbol"), "by_blocker": rank(by_blocker, "blocker"), "recent": list(reversed(rows))[:20]}
