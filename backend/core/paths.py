"""Single source of truth for every filesystem location.

Code lives in the package dirs; everything that changes at runtime (market
data, caches, logs, the SQLite DB) lives under var/ and is gitignored.
"""
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]

VAR_DIR = BACKEND_ROOT / "var"
ARCHIVE_ROOT = VAR_DIR / "archive"   # per-market: ARCHIVE_ROOT / "commodity" | "currency" | "equity" | "index_futures"
CACHE_DIR = VAR_DIR / "cache"
LOG_DIR = VAR_DIR / "logs"
DB_DIR = VAR_DIR / "db"              # paper_trading.db, backups/, lock + control files
