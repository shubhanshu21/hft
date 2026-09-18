#!/usr/bin/env python3
"""
safety_gate.py — Layered real-money kill switch, shared by the Upstox and Binance paths.

Today every UpstoxBroker call site hardcodes dry_run=True in source, and the
Binance path only ever constructs the *Testnet* client classes (BASE_URL
baked in at the class level) -- so nothing here is currently exposed to real
money. That single hardcoded flag / hardcoded class choice is the *entire*
defense though: one careless edit to any call site, or a new call site added
later, silently flips real capital live with no independent check.

Modelled on the pattern in github.com/TristanODonnell/Autonomous-Trading-Platform
(reviewed 2026-09-17): stack multiple INDEPENDENT gates so no single mistake
-- a bad .env push, a stale flag, an automated restart re-reading old config
-- can enable real trading on its own. All three must agree:

  1. KILL_SWITCH_ENGAGED   -- a module-level constant, source-code only.
                              Flipping this requires an actual code change
                              and redeploy, not a config edit.
  2. ALLOW_LIVE_TRADING    -- an explicit .env flag, off by default.
  3. the armed state file  -- created only by `cli.py arm-live-trading`,
                              which requires typing an exact confirmation
                              phrase. This is the "human in the loop" gate,
                              made non-interactive-daemon-safe by being a
                              deliberate one-time CLI action instead of a
                              startup prompt (our daemons run headless under
                              systemd, so a runtime input() would just hang).

Fails safe: any gate that can't be satisfied silently forces paper/testnet
mode rather than raising, so a broken gate can only make the system SAFER,
never accidentally permissive.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("safety_gate")

# --- Gate 1: build-time kill switch --------------------------------------
# Must be hand-edited to False (and redeployed) before live trading can ever
# be considered. This is intentionally the highest-friction gate.
KILL_SWITCH_ENGAGED = True

CONFIRM_PHRASE = "I UNDERSTAND THIS PLACES REAL ORDERS WITH REAL MONEY"
ARMED_STATE_DIR = Path(__file__).resolve().parent / "data"
ARMED_STATE_FILE = ARMED_STATE_DIR / ".live_trading_armed"


def _env_flag(name: str) -> bool:
    import os
    return os.getenv(name, "false").strip().lower() in ("1", "true", "yes")


def is_armed(component: str) -> bool:
    """True only if the persistent armed-state file exists, contains the exact
    confirmation phrase, and names this component (or 'ALL')."""
    if not ARMED_STATE_FILE.exists():
        return False
    try:
        lines = ARMED_STATE_FILE.read_text().splitlines()
    except OSError:
        return False
    if not lines or lines[0].strip() != CONFIRM_PHRASE:
        return False
    armed_components = {ln.strip() for ln in lines[1:] if ln.strip()}
    return component in armed_components or "ALL" in armed_components


def live_trading_allowed(component: str) -> bool:
    """All three independent gates must agree. Used by broker constructors to
    decide whether a caller's request for real (non-paper) execution is
    actually honoured."""
    if KILL_SWITCH_ENGAGED:
        return False
    if not _env_flag("ALLOW_LIVE_TRADING"):
        return False
    if not is_armed(component):
        return False
    return True


def arm(component: str, confirm_phrase: str) -> bool:
    """Called only from `cli.py arm-live-trading`. Requires the exact phrase,
    on purpose -- this is the one deliberate, hard-to-fat-finger human step."""
    if confirm_phrase != CONFIRM_PHRASE:
        print(f"Confirmation phrase did not match exactly. Live trading NOT armed for {component}.")
        return False
    ARMED_STATE_DIR.mkdir(parents=True, exist_ok=True)
    existing = set()
    if ARMED_STATE_FILE.exists():
        lines = ARMED_STATE_FILE.read_text().splitlines()
        if lines and lines[0].strip() == CONFIRM_PHRASE:
            existing = {ln.strip() for ln in lines[1:] if ln.strip()}
    existing.add(component)
    ARMED_STATE_FILE.write_text(CONFIRM_PHRASE + "\n" + "\n".join(sorted(existing)) + "\n")
    log.warning("Live trading ARMED for component=%s (state file gate only -- "
                "KILL_SWITCH_ENGAGED and ALLOW_LIVE_TRADING must ALSO be satisfied).", component)
    print(f"Armed component '{component}'. Still blocked unless KILL_SWITCH_ENGAGED=False in "
          f"safety_gate.py AND ALLOW_LIVE_TRADING=true in .env.")
    return True


def disarm(component: str | None = None) -> None:
    """Removes the armed state entirely, or just one component. Always safe to call."""
    if not ARMED_STATE_FILE.exists():
        print("Nothing armed.")
        return
    if component is None:
        ARMED_STATE_FILE.unlink()
        print("Disarmed all components.")
        return
    lines = ARMED_STATE_FILE.read_text().splitlines()
    if not lines or lines[0].strip() != CONFIRM_PHRASE:
        ARMED_STATE_FILE.unlink()
        print("Malformed state file removed (fail safe).")
        return
    remaining = {ln.strip() for ln in lines[1:] if ln.strip() and ln.strip() != component}
    if remaining:
        ARMED_STATE_FILE.write_text(CONFIRM_PHRASE + "\n" + "\n".join(sorted(remaining)) + "\n")
    else:
        ARMED_STATE_FILE.unlink()
    print(f"Disarmed component '{component}'.")


def enforce_dry_run(component: str, requested_dry_run: bool) -> bool:
    """Broker constructors call this instead of trusting their dry_run argument
    directly. Returns the EFFECTIVE dry_run to use: True unless the caller
    explicitly asked for live (requested_dry_run=False) AND all three gates
    pass. A caller asking for dry_run=True is never overridden -- this only
    ever tightens, never loosens, what was requested."""
    if requested_dry_run:
        return True
    if live_trading_allowed(component):
        log.warning("safety_gate: LIVE trading permitted for component=%s (all gates passed).", component)
        return False
    log.warning("safety_gate: component=%s requested live trading but a gate failed -- "
                "forcing dry_run=True. (KILL_SWITCH_ENGAGED=%s, armed=%s)",
                component, KILL_SWITCH_ENGAGED, is_armed(component))
    return True
