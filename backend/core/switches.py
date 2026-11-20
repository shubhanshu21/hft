"""On/off switch per equity strategy (2026-11-19, user: "while live we should be able to switch on/off any strategy, keep only which is
profitable"). In backend/.env, STRATEGY_<NAME>=off stops that strategy's NEW buys from its next run (no restart: every run reads .env);
what it already holds is still sold by its own rules, so switching off never strands a position. Anything other than off/0/false/no = on."""
import os

NAMES = ("RESULTS_GAP", "EARNINGS_DRIFT", "BREAKOUT", "MOMENTUM")


def on(name: str) -> bool:
    return os.environ.get(f"STRATEGY_{name}", "on").strip().lower() not in ("off", "0", "false", "no")


def status() -> dict[str, bool]:
    return {n: on(n) for n in NAMES}
