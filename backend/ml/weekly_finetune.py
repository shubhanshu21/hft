#!/usr/bin/env python3
"""
ml/weekly_finetune.py — Weekly job: top up commodity archives, then
FINE-TUNE the existing LightGBM models on just the new data since
their last update (continued boosting via init_model -- see
ml.train_commodity.finetune_commodity_model), and Telegram-alert the
outcome. This is deliberately not a from-scratch retrain every week: the
existing model is carried forward and refined, not replaced.

Scheduling assumption: runs on a day/time MCX is closed (see
hft-weekly-finetune.timer -- Sunday) so there are no open positions to worry
about, and it's safe to restart the live dryrun daemon afterwards so it picks
up the newly-updated models (a running DryRunner only loads models once at
startup -- overwriting the .pkl files alone doesn't affect an already-running
process).

Usage:
    python3 -m ml.weekly_finetune               # update archives + fine-tune + restart the dryrun service
    python3 -m ml.weekly_finetune --no-restart    # skip restarting the systemd service
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import telegram
from utils.logger import get_logger, setup_logger

setup_logger("", log_file=str(Path(__file__).resolve().parent.parent / "logs" / "weekly_finetune.log"))
log = get_logger("weekly_finetune")

MODEL_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "commodity_models"
SYSTEMD_SERVICE = "hft-dryrun.service"


def _model_mtimes() -> dict[str, float]:
    if not MODEL_CACHE_DIR.exists():
        return {}
    return {p.name: p.stat().st_mtime for p in MODEL_CACHE_DIR.glob("*.pkl")}


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly commodity archive top-up + LightGBM fine-tuning")
    ap.add_argument("--no-restart", action="store_true",
                     help="Skip restarting the systemd dryrun service after updating models")
    args = ap.parse_args()

    started = time.monotonic()
    before = _model_mtimes()

    try:
        from real_commodity_data import build_all_real_commodity_archives
        from ml.train_commodity import finetune_all_commodities

        log.info("Weekly fine-tuning: updating commodity archives with REAL Upstox data...")
        build_all_real_commodity_archives(topup=True)

        log.info("Weekly fine-tuning: updating LightGBM models on new data...")
        finetune_all_commodities()

    except Exception as exc:
        log.error("Weekly fine-tuning failed: %s", exc, exc_info=True)
        telegram.alert_error("Weekly ML fine-tuning", exc)
        return 1

    after = _model_mtimes()
    changed = sorted(k for k in after if before.get(k) != after[k])
    elapsed_min = (time.monotonic() - started) / 60.0

    msg = (
        f"🧠 <b>WEEKLY ML FINE-TUNING COMPLETE</b>\n"
        f"Duration: {elapsed_min:.1f} min\n"
        f"Models updated: {', '.join(changed) if changed else '(none had enough new data)'}"
    )

    restarted = False
    if not args.no_restart and changed:
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", SYSTEMD_SERVICE],
                check=True, capture_output=True, text=True, timeout=60,
            )
            restarted = True
            msg += f"\n♻️ Restarted {SYSTEMD_SERVICE} to load the updated models."
        except Exception as exc:
            log.warning("Could not restart %s: %s", SYSTEMD_SERVICE, exc)
            msg += (f"\n⚠️ Could not restart {SYSTEMD_SERVICE} automatically ({exc}) -- "
                     f"restart it manually so the updated models take effect.")

    log.info("Weekly fine-tuning done in %.1f min. Changed: %s. Restarted: %s",
              elapsed_min, changed, restarted)
    telegram.send(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
