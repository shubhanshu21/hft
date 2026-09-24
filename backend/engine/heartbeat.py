"""Liveness heartbeat for the paper-trading daemon.

systemd's Restart=always only helps when the process DIES. A hung network call leaves it alive and silent, with open positions
unmanaged. The daemon therefore writes var/db/heartbeat.json at the start of every scan and before every sleep, stating the time by
which the NEXT beat is due (`deadline`); engine/watchdog.py, run by a systemd timer, treats a passed deadline as a hang.
Recording the deadline (not just a timestamp) makes nights, weekends and exchange holidays -- when the daemon sleeps for hours -- look
exactly like a scan gap, with no market-calendar logic in the watchdog.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from core.paths import DB_DIR

IST = timezone(timedelta(hours=5, minutes=30))
HEARTBEAT_PATH = DB_DIR / "heartbeat.json"
GRACE_S = int(os.environ.get("HEARTBEAT_GRACE_SEC", "300"))      # slack after the promised next beat (a slow scan, a retry)


def beat(phase: str, next_beat_in_s: float = 0.0, now: datetime | None = None, path=None) -> None:
    """Record 'alive, doing `phase`, next beat due in `next_beat_in_s` seconds (+ grace)'. Never raises: a full disk must not stop trading."""
    now = now or datetime.now(IST)
    path = path or HEARTBEAT_PATH
    payload = {"ts": now.isoformat(), "phase": phase, "deadline": (now + timedelta(seconds=next_beat_in_s + GRACE_S)).isoformat(),
               "pid": os.getpid()}
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)                      # atomic: the watchdog never reads half a file
    except OSError:
        pass


def read(path=None) -> dict | None:
    try:
        return json.loads((path or HEARTBEAT_PATH).read_text())
    except (OSError, ValueError):
        return None


def stale_reason(now: datetime, hb: dict | None) -> str | None:
    """None when healthy (or deliberately stopped); otherwise a human-readable reason."""
    if hb is None:
        return "no heartbeat file"
    if hb.get("phase") == "stopped":
        return None
    deadline = datetime.fromisoformat(hb["deadline"])
    if now <= deadline:
        return None
    late = int((now - deadline).total_seconds())
    return f"no heartbeat for {late}s past its deadline (last phase: {hb.get('phase')} at {hb.get('ts', '?')[11:19]} IST)"
