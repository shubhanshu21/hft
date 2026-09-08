"""
utils/logger.py — Production-grade rotating logger for the trading bot.

Configures a logger that writes to both the console (stdout) and a
daily-rotating log file, using ISO-8601 timestamps in IST — this trades
against Indian markets, so log times should read in the exchange's own
timezone regardless of what timezone the server itself is set to (this
project's boxes run UTC). This is designed for unattended execution via
cron jobs on a Linux server.

Security note: Sensitive data (access tokens, API secrets) must NEVER be
passed to any logging call. Log only operational metadata.
"""

import logging
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


class _ISTFormatter(logging.Formatter):
    """logging.Formatter's default formatTime() uses the server's local timezone (time.localtime) — this overrides it to always render in IST, independent of the server's own timezone."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_IST)
        return dt.strftime(datefmt or "%Y-%m-%dT%H:%M:%S%z")


def setup_logger(name: str, level: str = "INFO", log_file: str = "") -> logging.Logger:
    """
    Create and configure a named logger with console + optional file output.

    Args:
        name:     Logger name (use __name__ in each module).
        level:    Log level string: DEBUG | INFO | WARNING | ERROR | CRITICAL.
        log_file: Absolute or relative path to the log file.
                  If empty, logging goes to console only.

    Returns:
        A configured logging.Logger instance.
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers when the module is re-imported.
    if logger.handlers:
        return logger

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    # Format: ISO timestamp (IST) + level + module:line → message
    fmt = _ISTFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    # --- Console handler (always enabled) ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # --- Rotating file handler (enabled when log_file is specified) ---
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Rotate at midnight, keep 30 days of history
        file_handler = TimedRotatingFileHandler(
            filename=str(log_path),
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    # Prevent log records from propagating to the root logger (avoids duplicates)
    logger.propagate = False

    return logger


# ---------------------------------------------------------------------------
# Module-level convenience: get a pre-configured logger for any module.
# Usage:  from utils.logger import get_logger
#         log = get_logger(__name__)
# ---------------------------------------------------------------------------
def get_logger(name: str) -> logging.Logger:
    """Return the existing logger for `name`, or the root app logger."""
    return logging.getLogger(name)
