"""Telegram alert when a paper engine quietly stops (2026-10-24). Run by hft-engine-monitor.timer every 15 minutes.

    python3 -m engine.engine_monitor            # one check; prints what it found
    python3 -m engine.engine_monitor --dry-run  # same, but never sends Telegram or saves alert state

Checks (each problem is alerted ONCE when it appears, repeated every REMIND_H hours while it lasts, and a "fixed" message is sent when it
clears -- state in var/db/engine_monitor_state.json):
  * crypto  -- hft-crypto-dip-reversal.timer active; the engine saved its state within CRYPTO_STALE_MIN (it saves on every 5-minute run);
               its last run exited successfully.
  * MCX     -- hft-mcx-trend-carry.timer active; the service finished a run after the latest scheduled slot (Mon-Fri 09:10 and 23:40 IST,
               MCX_GRACE_MIN allowed) and that run succeeded.
  * Upstox  -- in the half hour before each MCX run, the login token is refreshed if needed and must then be valid (MCX prices come
               from Upstox), so there is time to log in by hand.
The old engine/watchdog.py watches hft-dryrun.service, which was removed on 2026-10-21; this replaces it for the two engines that run now."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from core.paths import DB_DIR

IST = timezone(timedelta(hours=5, minutes=30))
STATE_PATH = DB_DIR / "engine_monitor_state.json"
CRYPTO_STATE = DB_DIR / "crypto_dip_reversal_state.json"
CRYPTO_STALE_MIN = 15
MCX_SLOTS = (dtime(9, 10), dtime(23, 40))          # hft-mcx-trend-carry.timer, IST, Mon-Fri
MCX_GRACE_MIN = 20
TOKEN_WINDOW_MIN = 30                               # check the Upstox login this long before each MCX slot
REMIND_H = 6


def _systemctl(*args: str) -> str:
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
    return r.stdout.strip()


def unit_active(unit: str, sh=_systemctl) -> bool:
    return sh("is-active", unit) == "active"


def last_run(unit: str, sh=_systemctl) -> tuple[datetime | None, str]:
    """(when the service's last run finished, its result 'success'/'exit-code'/...); (None, '') if it never ran."""
    out = dict(line.split("=", 1) for line in sh("show", unit, "-p", "Result", "-p", "ExecMainExitTimestamp", "--timestamp=unix").splitlines() if "=" in line)
    ts = out.get("ExecMainExitTimestamp", "").lstrip("@")
    when = datetime.fromtimestamp(int(ts), IST) if ts.isdigit() and int(ts) > 0 else None
    return when, out.get("Result", "")


def last_mcx_slot(now: datetime) -> datetime:
    """The most recent scheduled MCX run time at or before now (weekdays only)."""
    d = now.date()
    for _ in range(8):
        if d.weekday() < 5:
            for t in sorted(MCX_SLOTS, reverse=True):
                slot = datetime.combine(d, t, IST)
                if slot <= now:
                    return slot
        d -= timedelta(days=1)
    raise RuntimeError("no MCX slot in the last week")


def next_mcx_slot(now: datetime) -> datetime:
    d = now.date()
    for _ in range(8):
        if d.weekday() < 5:
            for t in sorted(MCX_SLOTS):
                slot = datetime.combine(d, t, IST)
                if slot > now:
                    return slot
        d += timedelta(days=1)
    raise RuntimeError("no MCX slot in the next week")


def problems(now: datetime, sh=_systemctl, crypto_state: Path = CRYPTO_STATE, token_ok=None) -> dict[str, str]:
    """{problem key: plain-words message} for everything wrong right now; {} when all is fine."""
    out: dict[str, str] = {}
    # crypto
    if not unit_active("hft-crypto-dip-reversal.timer", sh):
        out["crypto_timer"] = "Crypto engine timer is OFF -- no trades are being checked."
    try:
        saved = datetime.fromisoformat(json.loads(crypto_state.read_text())["updated_at"])
        age = (now - saved).total_seconds() / 60
        if age > CRYPTO_STALE_MIN:
            out["crypto_stale"] = f"Crypto engine has not run for {age:.0f} minutes (last {saved:%d %b %H:%M} IST)."
    except (OSError, ValueError, KeyError):
        out["crypto_stale"] = "Crypto engine state file is missing or unreadable."
    when, result = last_run("hft-crypto-dip-reversal.service", sh)
    if result and result != "success":
        out["crypto_failed"] = f"Crypto engine's last run FAILED ({result}). Check: journalctl --user -u hft-crypto-dip-reversal.service -n 50"
    # MCX
    if not unit_active("hft-mcx-trend-carry.timer", sh):
        out["mcx_timer"] = "MCX engine timer is OFF -- the monthly rebalance will not happen."
    slot = last_mcx_slot(now)
    if now - slot > timedelta(minutes=MCX_GRACE_MIN):
        when, result = last_run("hft-mcx-trend-carry.service", sh)
        if when is None or when < slot:
            out["mcx_missed"] = f"MCX engine missed its {slot:%a %d %b %H:%M} IST run."
        elif result and result != "success":
            out["mcx_failed"] = f"MCX engine's {when:%a %H:%M} IST run FAILED ({result}). Check: journalctl --user -u hft-mcx-trend-carry.service -n 50"
    # Upstox login, shortly before an MCX run
    if next_mcx_slot(now) - now <= timedelta(minutes=TOKEN_WINDOW_MIN):
        ok = token_ok() if token_ok else _upstox_token_ok()
        if not ok:
            out["upstox_login"] = ("Upstox login is EXPIRED and the automatic refresh failed -- MCX cannot get prices. "
                                   "Log in: cd backend && python3 -m services.auth.upstox_auth")
    return out


def _upstox_token_ok() -> bool:
    try:
        from services.auth.upstox_auto_login import ensure_fresh_upstox_token
        return bool(ensure_fresh_upstox_token())
    except Exception:
        return False


def check(now: datetime | None = None, *, sh=_systemctl, crypto_state: Path = CRYPTO_STATE, token_ok=None, notify=None,
          state_path: Path = STATE_PATH, dry_run: bool = False) -> dict:
    """One pass: find problems, send new / reminder / fixed messages. Returns {"problems": {...}, "sent": [...]}."""
    now = now or datetime.now(IST)
    found = problems(now, sh=sh, crypto_state=crypto_state, token_ok=token_ok)
    try:
        seen = json.loads(state_path.read_text())
    except (OSError, ValueError):
        seen = {}
    if notify is None:
        from services.utils import telegram
        notify = telegram.send
    sent = []
    for key, msg in found.items():
        last = seen.get(key)
        if last is None or now - datetime.fromisoformat(last) >= timedelta(hours=REMIND_H):
            sent.append(f"⚠️ <b>ENGINE CHECK</b> — {msg}" + ("" if last is None else " (still not fixed)"))
            seen[key] = now.isoformat()
    for key in [k for k in seen if k not in found]:
        sent.append(f"✅ <b>ENGINE CHECK</b> — fixed: {key.replace('_', ' ')}.")
        del seen[key]
    if not dry_run:
        for m in sent:
            notify(m)
        state_path.write_text(json.dumps(seen, indent=1))
    return {"problems": found, "sent": sent}


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env: Telegram + Upstox settings)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only; no Telegram, no state change")
    a = ap.parse_args(argv)
    out = check(dry_run=a.dry_run)
    print("all engines OK" if not out["problems"] else "\n".join(out["problems"].values()))
    for m in out["sent"]:
        print("would send:" if a.dry_run else "sent:", m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
