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
RG_SLOTS = (dtime(9, 30), dtime(9, 40), dtime(15, 20))   # hft-results-gap.timer trading runs, IST, Mon-Fri (09:40 = buy retry)
TOKEN_WINDOW_MIN = 30                               # check the Upstox login this long before each MCX slot
REMIND_H = 6


def _systemctl(*args: str) -> str:
    r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
    return r.stdout.strip()


def _journal(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout


def failed_runs(unit: str, since: datetime, run=None) -> int:
    """How many starts of `unit` FAILED since `since`, from the journal (0 if the journal cannot be read).

    `systemctl show` only reports the LAST exit, so a failed 09:30 run became invisible as soon as the 15:20 run succeeded
    (2026-10-26). The journal keeps every invocation, so both are counted here."""
    cmd = ["journalctl", "--user", "-u", unit, "--since", f"@{int(since.timestamp())}", "--no-pager", "-o", "cat"]
    try:
        text = (run or _journal)(cmd)
    except Exception:
        return 0                          # no journal (or no permission): never turn that into a false alarm
    # systemd writes "<unit>: Failed with result 'exit-code'." -- the unit name comes first, so match anywhere in the line
    return sum(1 for ln in str(text).splitlines() if "Failed with result" in ln and "success" not in ln)


def _today_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), dtime(0, 0), IST)


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


def last_slot(now: datetime, slots) -> datetime:
    """The most recent weekday run time in `slots` at or before now."""
    d = now.date()
    for _ in range(8):
        if d.weekday() < 5:
            for t in sorted(slots, reverse=True):
                s = datetime.combine(d, t, IST)
                if s <= now:
                    return s
        d -= timedelta(days=1)
    raise RuntimeError("no slot in the last week")


def next_slot(now: datetime, slots) -> datetime:
    d = now.date()
    for _ in range(8):
        if d.weekday() < 5:
            for t in sorted(slots):
                s = datetime.combine(d, t, IST)
                if s > now:
                    return s
        d += timedelta(days=1)
    raise RuntimeError("no slot in the next week")


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


def problems(now: datetime, sh=_systemctl, crypto_state: Path = CRYPTO_STATE, token_ok=None, journal=None) -> dict[str, str]:
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
        out["mcx_timer"] = "MCX engine timer is OFF -- the 2-weekly rebalance will not happen."
    slot = last_mcx_slot(now)
    if now - slot > timedelta(minutes=MCX_GRACE_MIN):
        when, result = last_run("hft-mcx-trend-carry.service", sh)
        if when is None or when < slot:
            out["mcx_missed"] = f"MCX engine missed its {slot:%a %d %b %H:%M} IST run."
        elif result and result != "success":
            out["mcx_failed"] = f"MCX engine's {when:%a %H:%M} IST run FAILED ({result}). Check: journalctl --user -u hft-mcx-trend-carry.service -n 50"
    n = failed_runs("hft-mcx-trend-carry.service", _today_start(now), run=journal)      # a failure earlier today is not masked by a
    if n and "mcx_failed" not in out:                                                   # later successful run
        out["mcx_failed"] = f"MCX engine had {n} failed run(s) today. Check: journalctl --user -u hft-mcx-trend-carry.service -n 50"
    # results_gap (equity intraday): its 09:30 buy and 15:20 sell runs
    if not unit_active("hft-results-gap.timer", sh):
        out["rg_timer"] = "Results-gap engine timer is OFF -- no results-day trades will be taken."
    slot = last_slot(now, RG_SLOTS)
    if now - slot > timedelta(minutes=MCX_GRACE_MIN):
        when, result = last_run("hft-results-gap.service", sh)
        if when is not None and when < slot:                       # never run yet (just installed) is not a miss
            out["rg_missed"] = f"Results-gap engine missed its {slot:%a %d %b %H:%M} IST run."
        elif result and result != "success":
            out["rg_failed"] = f"Results-gap engine's {when:%a %H:%M} IST run FAILED ({result}). Check: journalctl --user -u hft-results-gap.service -n 50"
    n = failed_runs("hft-results-gap.service", _today_start(now), run=journal)
    if n and "rg_failed" not in out:
        out["rg_failed"] = (f"Results-gap engine had {n} failed run(s) today -- the last run succeeding does not mean the morning one did. "
                            "Check: journalctl --user -u hft-results-gap.service -n 30")
    # results_gap got no usable prices at its 09:30 check today (2026-10-26: Upstox's OHLC quote had no previous close). The engine
    # retries at 09:40 before it gives the day up, so alert only once that window has closed.
    try:
        rg = json.loads((crypto_state.parent / "results_gap_state.json").read_text())
        chk = rg.get("last_check") or {}
        attempted_today = rg.get("entry_date") == str(now.date()) or rg.get("entered_on") == str(now.date())
        if attempted_today and chk.get("market_gap") is None and now.time() >= dtime(9, 45):
            tries = int(rg.get("entry_attempts") or 1)
            out["rg_no_prices"] = (f"Results-gap got NO usable prices today ({tries} attempt(s) since 09:30) -- it could not check any "
                                   "stock. Check: journalctl --user -u hft-results-gap.service -n 30")
    except (OSError, ValueError, TypeError):
        pass
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
          state_path: Path = STATE_PATH, dry_run: bool = False, journal=None) -> dict:
    """One pass: find problems, send new / reminder / fixed messages. Returns {"problems": {...}, "sent": [...]}."""
    now = now or datetime.now(IST)
    found = problems(now, sh=sh, crypto_state=crypto_state, token_ok=token_ok, journal=journal)
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


def engine_status(account: str, now: datetime | None = None, sh=_systemctl, crypto_state: Path = CRYPTO_STATE) -> dict:
    """For the paper page (2026-10-24): is this account's engine alive? {"ok": bool, "last": iso|None, "next": iso|None, "text": plain words}."""
    now = now or datetime.now(IST)
    if account == "CRYPTO_DIP_REVERSAL":
        on = unit_active("hft-crypto-dip-reversal.timer", sh)
        try:
            last = datetime.fromisoformat(json.loads(crypto_state.read_text())["updated_at"])
        except (OSError, ValueError, KeyError):
            last = None
        nxt = now.replace(second=5, microsecond=0) + timedelta(minutes=5 - now.minute % 5) if on else None
        fresh = last is not None and (now - last).total_seconds() <= CRYPTO_STALE_MIN * 60
        ok = on and fresh
        if not on:
            text = "Engine is OFF -- no coins are being checked"
        elif last is None:
            text = "Engine has not run yet"
        else:
            mins = int((now - last).total_seconds() // 60)
            text = f"{'Running' if fresh else 'NOT RUNNING'} · last check {('just now' if mins < 1 else f'{mins} min ago')} · next at {nxt:%H:%M} IST"
        return {"ok": ok, "last": last.isoformat() if last else None, "next": nxt.isoformat() if nxt else None, "text": text}
    if account == "MCX_TREND_CARRY":
        on = unit_active("hft-mcx-trend-carry.timer", sh)
        when, result = last_run("hft-mcx-trend-carry.service", sh)
        slot = last_mcx_slot(now)
        missed = now - slot > timedelta(minutes=MCX_GRACE_MIN) and (when is None or when < slot)
        ok = on and not missed and result in ("", "success")
        nxt = next_mcx_slot(now) if on else None
        if not on:
            text = "Engine is OFF -- the 2-weekly rebalance will not happen"
        elif not ok:
            text = f"Last run {'was missed' if missed else 'FAILED'} · next at {nxt:%a %H:%M} IST"
        else:
            text = f"Running · last check {when:%a %H:%M} IST · next at {nxt:%a %H:%M} IST" if when else f"Running · next at {nxt:%a %H:%M} IST"
        return {"ok": ok, "last": when.isoformat() if when else None, "next": nxt.isoformat() if nxt else None, "text": text}
    if account == "RESULTS_GAP":
        on = unit_active("hft-results-gap.timer", sh)
        when, result = last_run("hft-results-gap.service", sh)
        slot = last_slot(now, RG_SLOTS)
        missed = now - slot > timedelta(minutes=MCX_GRACE_MIN) and when is not None and when < slot
        ok = on and not missed and result in ("", "success")
        nxt = next_slot(now, RG_SLOTS) if on else None
        if not on:
            text = "Engine is OFF -- no results-day trades will be taken"
        elif when is None:
            text = f"Waiting for the first run · next at {nxt:%a %H:%M} IST"
        elif not ok:
            text = f"Last run {'was missed' if missed else 'FAILED'} · next at {nxt:%a %H:%M} IST"
        else:
            text = (f"Running · last check {when:%a %H:%M} IST · next at {nxt:%a %H:%M} IST" if when else f"Running · next at {nxt:%a %H:%M} IST")
        return {"ok": ok, "last": when.isoformat() if when else None, "next": nxt.isoformat() if nxt else None, "text": text}
    return {"ok": None, "last": None, "next": None, "text": ""}
