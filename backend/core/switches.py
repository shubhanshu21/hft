"""On/off switch per equity strategy (2026-11-19, user: "while live we should be able to switch on/off any strategy, keep only which is
profitable"). In backend/.env, STRATEGY_<NAME>=off stops that strategy's NEW buys from its next run (no restart: every run reads .env);
what it already holds is still sold by its own rules, so switching off never strands a position. Anything other than off/0/false/no/close = on; close = off + sell all at the next open (breakout, momentum)."""
import os

NAMES = ("RESULTS_GAP", "EARNINGS_DRIFT", "BREAKOUT", "MOMENTUM", "RG_MOMENTUM")   # RG_MOMENTUM (2026-11-22): momentum inside RESULTS_GAP;
# MOMENTUM stays the old EQUITY_BREAKOUT momentum half (set to close)


def _value(name: str) -> str:
    return os.environ.get(f"STRATEGY_{name}", "on").strip().lower()


def on(name: str) -> bool:
    return _value(name) not in ("off", "0", "false", "no", "close")


def closing(name: str) -> bool:
    """STRATEGY_<NAME>=close (2026-11-20, breakout + momentum only): off, AND sell everything it holds at the next open."""
    return _value(name) == "close"


ACCOUNT_SWITCHES = {"EQUITY_BREAKOUT": ("BREAKOUT", "MOMENTUM")}


def account_closed(account: str) -> bool:
    """2026-11-23: every strategy of the account set to close = the account is closed (history only: off the Desk and the left list,
    out of the nightly check and the monitor). Its rows stay; set a switch back to on to open it again."""
    names = ACCOUNT_SWITCHES.get(account)
    return bool(names) and all(closing(n) for n in names)


def status() -> dict[str, bool]:
    return {n: on(n) for n in NAMES}
