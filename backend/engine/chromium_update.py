"""Monthly Chromium update, tested (2026-11-19).

    python3 -m engine.chromium_update        # what hft-chromium-update.timer runs, first Saturday of the month 10:00 IST

The Upstox auto-login drives the snap Chromium. On 2026-11-19 a snap auto-refresh (05:08 IST) shipped a chromedriver needing GLIBC 2.42,
the login died and the morning engines had no prices. Since then Chromium is HELD (`snap refresh --hold chromium`: no automatic updates).
This job updates it on purpose, on a market-closed day: unhold -> refresh -> a real forced Upstox login -> if the login fails,
`snap revert` to the version that worked and log in again -> hold again. Telegram hears the result either way.
"""
from __future__ import annotations

import subprocess
import sys


def _sh(*args: str) -> tuple[int, str]:
    r = subprocess.run(["sudo", "-n", "snap", *args], capture_output=True, text=True, timeout=900)
    return r.returncode, (r.stdout + r.stderr).strip()


def _login() -> bool:
    try:
        from services.auth.upstox_auto_login import ensure_fresh_upstox_token
        return bool(ensure_fresh_upstox_token(force=True))
    except Exception as exc:
        print(f"login failed: {exc!s:.200}")
        return False


def _version(sh) -> str:
    _, out = sh("list", "chromium")
    rows = out.splitlines()
    return rows[-1].split()[1] if len(rows) > 1 else "?"


def run(sh=_sh, login=_login, send=None, log=print) -> int:
    """0 = Chromium is current (or unchanged) and the login works; 1 = something needs a person."""
    if send is None:
        from services.utils import telegram
        send = telegram.send
    from services.utils import telegram as T
    before = _version(sh)
    sh("refresh", "--unhold", "chromium")
    code, out = sh("refresh", "chromium")
    after = _version(sh)
    log(f"chromium {before} -> {after} (refresh exit {code}: {out[:200]})")
    try:
        if login():
            if after != before:
                send(T.message("🔧", "Chromium updated", [f"Chromium {before} → {after}. Upstox login tested after the update: works."]))
            return 0
        if after == before:
            send(T.problem("Upstox login", f"The login fails on Chromium {after} (no update was installed, so this is not the update).",
                           "cd backend && python3 -m services.auth.upstox_auth"))
            return 1
        sh("revert", "chromium")
        back = _version(sh)
        ok = login()
        log(f"login failed on {after}; reverted to {back}; login {'works' if ok else 'STILL FAILS'}")
        send(T.problem("Upstox login", f"Chromium {after} broke the Upstox login, so it was put back to {back}. "
                                       f"Login on {back}: {'works' if ok else 'STILL FAILS -- log in by hand'}.",
                       "journalctl --user -u hft-chromium-update.service -n 40"))
        return 0 if ok else 1
    finally:
        sh("refresh", "--hold", "chromium")                        # never left on automatic updates


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    return run()


if __name__ == "__main__":
    sys.exit(main())
