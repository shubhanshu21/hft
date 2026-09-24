"""Restarts the paper-trading daemon when it is alive but no longer scanning, and says so on Telegram.

    python3 -m engine.watchdog          # run by hft-watchdog.timer every 2 minutes

Only acts on a service that systemd reports ACTIVE. If it is stopped (deliberately, by `systemctl stop`) or failed, that is not a
hang: Restart=always or the operator owns that case, and a watchdog that restarts a service someone just stopped is a bug.
A restart is limited to one per RESTART_COOLDOWN_S, so a daemon that hangs on startup cannot be restarted in a loop.
Set WATCHDOG_AUTORESTART=false to alert only.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

from core.paths import DB_DIR
from engine import heartbeat

SERVICE = "hft-dryrun.service"
STATE_PATH = DB_DIR / "watchdog_state.json"
RESTART_COOLDOWN_S = 900


def _is_active(run=subprocess.run) -> bool:
    r = run(["systemctl", "--user", "is-active", SERVICE], capture_output=True, text=True)
    return r.stdout.strip() == "active"


def _restart(run=subprocess.run) -> bool:
    return run(["systemctl", "--user", "restart", SERVICE], capture_output=True, text=True).returncode == 0


def check(now: datetime | None = None, *, hb: dict | None = None, run=subprocess.run, notify=None, state_path=STATE_PATH,
          autorestart: bool | None = None) -> str:
    """One watchdog pass. Returns 'ok', 'not-active', 'stale-alerted' or 'stale-restarted' (for logging and tests)."""
    now = now or datetime.now(heartbeat.IST)
    hb = heartbeat.read() if hb is None else hb
    if notify is None:
        from services.utils import telegram
        notify = telegram.send
    if autorestart is None:
        autorestart = os.environ.get("WATCHDOG_AUTORESTART", "true").lower() != "false"

    if hb is None:
        return "no-heartbeat"          # daemon not yet running the heartbeat code (first deploy): never restart on the absence of a signal
    reason = heartbeat.stale_reason(now, hb)
    if reason is None:
        return "ok"
    if not _is_active(run):
        return "not-active"

    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    last = state.get("last_restart")
    recently = bool(last) and (now - datetime.fromisoformat(last)).total_seconds() < RESTART_COOLDOWN_S
    if not autorestart or recently:
        notify(f"⚠️ <b>WATCHDOG</b> — daemon looks hung: {reason}. "
               f"{'Auto-restart is off.' if not autorestart else 'Restarted less than 15 min ago; not restarting again.'}")
        return "stale-alerted"
    ok = _restart(run)
    state_path.write_text(json.dumps({"last_restart": now.isoformat(), "reason": reason}))
    notify(f"🔁 <b>WATCHDOG RESTART</b> — {reason}. Restart {'issued' if ok else 'FAILED'}; open positions are restored from the DB.")
    return "stale-restarted"


if __name__ == "__main__":
    print(check())
