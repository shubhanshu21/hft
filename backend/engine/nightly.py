"""Nightly maintenance job: data top-ups, DB backup, trade audit, archive-freshness check -- one step failing never skips the rest.

    python3 -m engine.nightly

Replaces five separate ExecStart= lines in hft-daily-data-topup.service. systemd stops a oneshot service at the first failing
ExecStart, so one bad top-up used to silently skip every step after it, including the DB backup. Here every step runs, failures are
collected and sent to Telegram, and the exit status is non-zero if any step failed. The trade audit exiting 1 means "flagged trades"
(already alerted by the audit itself), not a job failure.

Upstox does not always have the previous day's candles final at 00:30 IST, so the timer also runs this at 06:30; the top-up is
incremental and idempotent, and the audit re-checks the last few days, so a late day is picked up on the second run.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.paths import ARCHIVE_ROOT

PY = sys.executable
STEP_TIMEOUT_S = 45 * 60
STEPS = [                            # (name, argv, exit codes that count as success)
    ("commodity data", [PY, "-m", "markets.commodity.data", "--topup"], {0}),
    ("currency data", [PY, "-m", "markets.currency.data", "--topup"], {0}),
    ("equity data", [PY, "-m", "markets.equity.data", "--topup"], {0}),
    ("db backup", [PY, "-m", "engine.backup_db"], {0}),
    ("db backup verify", [PY, "-m", "engine.backup_db", "--verify"], {0}),      # open the newest backup: a backup nobody has opened is a hope
    ("trade audit", [PY, "-m", "engine.trade_audit"], {0, 1}),          # 1 = flagged trades: alerted by the audit itself
]
# representative archive files whose newest candle date must not lag (market, file)
FRESHNESS = [("commodity", "CRUDEOILM_1minute.csv"), ("commodity", "GOLD_1minute.csv"), ("commodity", "SILVER_1minute.csv"),
             ("currency", "USDINR_1minute.csv"), ("equity", "RELIANCE_5minute.csv"), ("equity", "TATASTEEL_5minute.csv")]
IST = ZoneInfo("Asia/Kolkata")


def run_steps(steps=STEPS, run=subprocess.run, log=print) -> list[str]:
    """Run every step regardless of earlier failures. Returns the names of the failed steps."""
    failed = []
    for name, argv, ok_codes in steps:
        t0 = time.monotonic()
        try:
            rc = run(argv, timeout=STEP_TIMEOUT_S).returncode
        except subprocess.TimeoutExpired:
            rc = -9
        except Exception as exc:                       # a missing interpreter etc. must not skip the remaining steps
            log(f"[{name}] could not run: {exc}")
            rc = -1
        log(f"[{name}] exit {rc} in {time.monotonic() - t0:.0f}s")
        if rc not in ok_codes:
            failed.append(name)
    return failed


def last_trading_day(today: date, holidays: set) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5 or d in holidays:
        d -= timedelta(days=1)
    return d


def newest_candle_date(path) -> date | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 512))
            last = fh.read().decode(errors="ignore").strip().splitlines()[-1]
        return date.fromisoformat(last.split(",")[0][:10])
    except (OSError, ValueError, IndexError):
        return None


def stale_archives(today: date, holidays: set, files=FRESHNESS, root=ARCHIVE_ROOT) -> list[str]:
    """Archives more than ONE trading day behind. One day is tolerated (Upstox finalises a day late); two is a real problem."""
    expected = last_trading_day(today, holidays)
    limit = last_trading_day(expected, holidays)                     # the trading day before `expected`
    out = []
    for market, fname in files:
        newest = newest_candle_date(root / market / fname)
        if newest is None:
            out.append(f"{market}/{fname}: unreadable or missing")
        elif newest < limit:
            out.append(f"{market}/{fname}: newest candle {newest}, expected at least {limit}")
    return out


def main() -> int:
    failed = run_steps()
    try:
        from services.utils.market_holidays import get_trading_holidays
        holidays = set(get_trading_holidays())
    except Exception:
        holidays = set()
    stale = stale_archives(datetime.now(IST).date(), holidays)
    for line in stale:
        print(f"[freshness] STALE {line}")
    if failed or stale:
        try:
            from services.utils import telegram
            parts = []
            if failed:
                parts.append("Failed steps: " + ", ".join(failed))
            if stale:
                parts.append("Stale archives:\n  " + "\n  ".join(stale))
            telegram.send("🛠 <b>NIGHTLY JOB</b> needs attention\n" + "\n".join(parts))
        except Exception as exc:
            print(f"could not send Telegram alert: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
