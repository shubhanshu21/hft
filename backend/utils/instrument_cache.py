"""
utils/instrument_cache.py — Daily Upstox Instrument Master Cache.

Upstox publishes a fresh instrument master (CSV) that maps stock symbols
to their internal instrument tokens. This must be downloaded once per
trading day (before market opens) because:

  - Tokens can change (corporate actions, new listings)
  - New option strikes are added; old ones expire
  - Using stale data risks placing orders on wrong instruments

Cache strategy:
  - Cache files live in `cache/` with date-stamped names.
  - As soon as TODAY's file is confirmed on disk (freshly downloaded or
    already present), every OTHER date-stamped instrument-master file is
    deleted immediately — this cache is only ever read for "today's"
    file (_today_cache_path), so a prior day's copy serves no purpose
    once today's exists and would just accumulate on disk forever
    otherwise (each one is ~12MB).
  - If today's file already exists, it is reused (fast, no network call).
  - If the file is missing or stale, a fresh copy is downloaded.

Source: https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz

Usage:
    from utils.instrument_cache import InstrumentCache
    cache = InstrumentCache()
    df = cache.get_or_refresh()  # Returns a pandas DataFrame
"""

import itertools
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from utils.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Where cache files are stored (relative to project root)
CACHE_DIR = Path("cache")

# Network request timeout (seconds)
REQUEST_TIMEOUT = 30

MASTER_URLS = {
    "NSE": "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz",
    "MCX": "https://assets.upstox.com/market-quote/instruments/exchange/MCX.csv.gz",
    "BSE": "https://assets.upstox.com/market-quote/instruments/exchange/BSE.csv.gz",
}

# Column names to normalise for easy downstream use
UPSTOX_COLS = {
    "tradingsymbol": "symbol",
    "instrument_key": "instrument_key",
    "instrument_type": "instrument_type",
    "exchange": "exchange",
    "name": "name",
    "lot_size": "lot_size",
    "tick_size": "tick_size",
}


class InstrumentCache:
    """Manages daily download and local caching of Upstox's instrument master."""

    def __init__(self, cache_dir: Path = CACHE_DIR, **kwargs) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._df: pd.DataFrame | None = None  # In-memory cache

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------


    def get_or_refresh(self, force: bool = False) -> pd.DataFrame:
        """
        Return today's instrument master as a DataFrame.

        Uses a local cache file if it exists for today's date;
        otherwise downloads a fresh copy from Upstox.

        Args:
            force: If True, always re-download even if cache exists.

        Returns:
            DataFrame with normalised column names.

        Raises:
            RuntimeError: If the download fails and no cache exists.
        """
        # Return in-memory cached copy if already loaded today
        if self._df is not None and not force:
            log.debug("Using in-memory instrument master (%s rows).", len(self._df))
            return self._df

        cache_file = self._today_cache_path()

        if not force and cache_file.exists():
            log.info(
                "Loading cached instrument master: %s (%s)",
                cache_file.name,
                self._human_size(cache_file.stat().st_size),
            )
            self._df = self._load_csv(cache_file)
        else:
            self._df = self._download_and_cache(cache_file)

        # Housekeeping: remove stale cache files
        self._cleanup_old_cache()

        log.info(
            "Instrument master ready — rows=%d | date=%s",
            len(self._df), date.today(),
        )
        return self._df

    def instrument_exists(self, instrument_key: str) -> bool:
        """Is `instrument_key` still listed in today's instrument master? False for a contract that has expired/been delisted since it was last traded — the caller's signal to stop retrying an order against it."""
        df = self.get_or_refresh()
        return bool((df["instrument_key"] == instrument_key).any())

    def resolve_equity_key(self, symbol: str) -> str | None:
        """
        Find the Upstox instrument_key for an NSE equity, e.g.
        'NSE_EQ|INE002A01018' for 'RELIANCE'.

        Args:
            symbol: NSE trading symbol (e.g., 'RELIANCE', 'TCS').

        Returns:
            Instrument key string, or None if not found.
        """
        symbol = symbol.upper().strip()
        df = self.get_or_refresh()

        try:
            mask = (
                (df["symbol"].str.upper() == symbol) &
                (df["exchange"].str.startswith("NSE_EQ"))
            )
            matches = df[mask]

            if matches.empty:
                # Try with instrument_type filter
                mask2 = (
                    (df["symbol"].str.upper() == symbol) &
                    (df["instrument_type"].str.upper().isin(["EQUITY", "EQ", "ES"]))
                )
                matches = df[mask2]

            if matches.empty:
                # Not an error by itself — resolve_key() calls this as a
                # first probe before falling back to the INDEX segment, so
                # a miss here is a normal outcome for index symbols. Callers
                # needing a hard equity-only lookup already raise/log based
                # on their own None-return handling.
                log.debug("No equity entry found for symbol '%s' in instrument master.", symbol)
                return None

            key = str(matches.iloc[0]["instrument_key"])
            log.info("Upstox instrument_key for %s = %s", symbol, key)
            return key

        except KeyError as exc:
            log.error("Column missing in Upstox master: %s. Available: %s", exc, list(df.columns))
            return None

    def resolve_key(self, symbol: str) -> str | None:
        """
        Resolve `symbol` as an equity, an index, OR an MCX commodity —
        tries the equity segment first (resolve_equity_key), then the
        INDEX segment, then falls back to the nearest MCX commodity
        future (see resolve_commodity_key — MCX has no separate "spot"
        instrument at all, only futures/options-on-futures, so the
        nearest future IS the underlying's tradable instrument_key).
        Lets callers pass a plain ticker ('RELIANCE', 'NIFTY', or 'GOLD')
        without needing to know in advance which segment it's in.

        Index rows carry a matching `tradingsymbol` (e.g. 'NIFTY' →
        'NSE_INDEX|Nifty 50', 'BANKNIFTY' → 'NSE_INDEX|Nifty Bank').

        Returns:
            Instrument key string, or None if not found in any segment.
        """
        key = self.resolve_equity_key(symbol)
        if key:
            return key
        key = self._resolve_index(symbol.upper().strip())
        if key:
            return key
        commodity = self.resolve_commodity_key(symbol)
        return commodity[0] if commodity else None

    def resolve_commodity_key(self, symbol: str) -> tuple | None:
        """
        Return (instrument_key, expiry) of the nearest-dated MCX future
        for `symbol` (e.g. 'GOLD', 'CRUDEOIL', 'SILVERM'), or None if it
        has no listed MCX contract. MCX commodities have no cash-spot
        instrument in Upstox's model at all — every price (for ATM-strike
        calc, Black-76 F, live LTP) comes from this nearest future, same
        role NSE's resolve_nearest_future_key() plays for Greeks there
        (the difference: for MCX this IS the underlying's own tradable
        key, not just a Greeks input — MCX options are ALREADY
        futures-struck, so Black-76 needs no separate spot-vs-futures
        distinction for commodities).
        """
        import re
        df = self.get_or_refresh()
        symbol = symbol.upper().strip()
        fut = df[(df["exchange"] == "MCX_FO") & (df["instrument_type"] == "FUTCOM")]
        pattern = re.compile(rf"^{re.escape(symbol)}\d{{2}}[A-Z]{{3}}FUT$")
        matches = fut[fut["symbol"].astype(str).str.match(pattern)]
        if matches.empty:
            return None
        matches = matches.sort_values("expiry")
        row = matches.iloc[0]
        return str(row["instrument_key"]), str(row["expiry"])

    def resolve_lot_size(self, symbol: str) -> int | None:
        """
        Resolve the REAL, CURRENT F&O lot size for `symbol` from today's
        instrument master — more authoritative than any hardcoded table
        (NSE revises lot sizes periodically; this is refreshed daily).
        Returns None if not resolvable — callers (e.g.
        TenPercentOTMStrangle.__init__()) raise rather than guess.

        Note: the equity spot row's own lot_size is always 1 (irrelevant —
        that's shares-per-share for the cash-market segment). The real F&O
        multiplier lives on the option/future contract rows instead, whose
        `symbol` column is the full contract tradingsymbol (e.g.
        'RELIANCE26AUG1170PE'), not the plain ticker — matched here via a
        `^{SYMBOL}\\d{{2}}[A-Z]{{3}}` prefix pattern (symbol immediately
        followed by a 2-digit year + 3-letter month) to avoid false
        prefix collisions (e.g. 'TATASTEEL' matching 'TATASTEELBSL').

        Returns:
            Lot size as int, or None if not resolvable.
        """
        symbol = symbol.upper().strip()
        df = self.get_or_refresh()
        try:
            import re
            pattern = re.compile(rf"^{re.escape(symbol)}\d{{2}}[A-Z]{{3}}")
            fo = df[df["exchange"].isin(("NSE_FO", "MCX_FO", "BSE_FO"))]
            matches = fo[fo["symbol"].astype(str).str.match(pattern)]
            if matches.empty:
                log.warning("No F&O contracts found for '%s' to resolve lot size from.", symbol)
                return None
            lot_sizes = matches["lot_size"].astype(float).unique()
            if len(lot_sizes) > 1:
                log.warning(
                    "Multiple distinct lot sizes found for '%s' F&O contracts (%s) — "
                    "using the first. This can happen mid-transition when NSE revises a lot size.",
                    symbol, lot_sizes,
                )
            lot_size = int(lot_sizes[0])
            log.info("Upstox real lot size for %s = %d (from today's instrument master)", symbol, lot_size)
            return lot_size
        except (KeyError, ValueError, IndexError) as exc:
            log.warning("Could not resolve lot size for '%s' from instrument master: %s", symbol, exc)
            return None

    def resolve_strike_step(self, symbol: str) -> float | None:
        """
        Resolve the REAL, CURRENT strike-price interval for `symbol` from
        today's instrument master's actual listed option strikes (nearest
        expiry) — same reasoning as resolve_lot_size(): NSE strike
        intervals aren't fixed forever (a stock split, or NSE's own
        periodic re-tightening, can change them), and a stale static
        number risks computing a target strike that isn't actually
        listed — the caller then silently falls back to a different,
        unintended strike rather than the real ±10% OTM one. Verified
        live: several hardcoded values in config.py had already drifted
        (e.g. RELIANCE configured as 20, really 10; TCS configured as 50,
        really 20) the moment they were checked against real listed
        strikes.

        Real NSE strike intervals can be fractional for lower-priced
        stocks (e.g. 2.5) — this returns a float, not an int.

        Returns:
            Strike interval as float, or None if not resolvable (caller
            should raise rather than guess).
        """
        symbol = symbol.upper().strip()
        df = self.get_or_refresh()
        try:
            import re
            pattern = re.compile(rf"^{re.escape(symbol)}\d{{2}}[A-Z]{{3}}")
            fo = df[df["exchange"].isin(("NSE_FO", "MCX_FO", "BSE_FO"))]
            matches = fo[fo["symbol"].astype(str).str.match(pattern)]
            matches = matches[matches["option_type"].isin(["CE", "PE"])]
            if matches.empty:
                log.warning("No option contracts found for '%s' to resolve strike step from.", symbol)
                return None
            nearest_expiry = sorted(matches["expiry"].unique())[0]
            strikes = sorted(matches[matches["expiry"] == nearest_expiry]["strike"].astype(float).unique())
            if len(strikes) < 2:
                log.warning(
                    "Only %d strike(s) listed for '%s' nearest expiry (%s) — can't determine an interval.",
                    len(strikes), symbol, nearest_expiry,
                )
                return None
            gaps = sorted({round(b - a, 4) for a, b in itertools.pairwise(strikes)})
            step = gaps[0]
            log.info(
                "Real strike step for %s = %s (from today's instrument master, nearest expiry %s)",
                symbol, step, nearest_expiry,
            )
            return step
        except (KeyError, ValueError, IndexError) as exc:
            log.warning("Could not resolve strike step for '%s' from instrument master: %s", symbol, exc)
            return None

    def list_tradable_symbols(self) -> dict:
        """
        Every underlying that actually has listed F&O contracts today,
        split into stocks/indices/commodities — sourced from the real
        downloaded instrument master (NSE_FO + MCX_FO futures rows), not
        any hardcoded convenience list. Used by the control-panel API so a
        user can pick from what's genuinely tradable right now, not just
        the ~11 stocks config.py happens to mention for --all bulk-
        download purposes.

        Futures tradingsymbols follow `{SYMBOL}{2-digit-year}{3-letter
        -month}FUT` (e.g. 'RELIANCE26AUGFUT', 'GOLD27FEBFUT') — the
        underlying ticker is recovered by stripping that trailing
        date+FUT suffix, then deduplicated across the (usually 3) listed
        expiries. Same pattern for both exchanges.
        """
        import re
        df = self.get_or_refresh()
        fut = df[df["exchange"] == "NSE_FO"]
        mcx_fut = df[(df["exchange"] == "MCX_FO") & (df["instrument_type"] == "FUTCOM")]
        pattern = re.compile(r"^([A-Z0-9\-&]+?)\d{2}[A-Z]{3}FUT$")

        def _extract(symbols) -> list:
            out = set()
            for s in symbols:
                m = pattern.match(str(s))
                if m:
                    out.add(m.group(1))
            return sorted(out)

        return {
            "stocks": _extract(fut[fut["instrument_type"] == "FUTSTK"]["symbol"]),
            "indices": _extract(fut[fut["instrument_type"] == "FUTIDX"]["symbol"]),
            "commodities": _extract(mcx_fut["symbol"]),
        }

    def resolve_nearest_future_key(self, symbol: str) -> tuple | None:
        """
        Return (instrument_key, expiry) of the nearest-dated future for
        `symbol` (NSE_FO/BSE_FO/MCX_FO — same exchange set resolve_lot_size/
        resolve_strike_step already search), or None if it has no listed
        futures contract. Was NSE_FO-only until this fix, which silently
        broke Black-76 Greeks for every SENSEX leg (SENSEX futures list
        under BSE_FO, not NSE_FO) — same class of gap as the earlier
        NSE_FO-only bug in resolve_lot_size/resolve_strike_step.

        Used to get the FUTURES price for Black-76 Greeks (utils/black76.py)
        — options are priced against the futures leg, not raw spot, and
        substituting spot here would silently make every Greek slightly
        wrong (the whole reason Black-76 exists instead of plain
        Black-Scholes — see that module's docstring).
        """
        import re
        df = self.get_or_refresh()
        symbol = symbol.upper().strip()
        fut = df[(df["exchange"].isin(("NSE_FO", "BSE_FO", "MCX_FO"))) & (df["instrument_type"].isin(["FUTSTK", "FUTIDX"]))]
        pattern = re.compile(rf"^{re.escape(symbol)}\d{{2}}[A-Z]{{3}}FUT$")
        matches = fut[fut["symbol"].astype(str).str.match(pattern)]
        if matches.empty:
            log.warning("No listed futures contract found for '%s' — can't resolve a Black-76 futures price.", symbol)
            return None
        matches = matches.sort_values("expiry")
        row = matches.iloc[0]
        return str(row["instrument_key"]), str(row["expiry"])

    def _resolve_index(self, symbol: str) -> str | None:
        """Find the Upstox instrument_key for an index by its tradingsymbol (e.g. 'NIFTY')."""
        df = self.get_or_refresh()
        try:
            mask = (
                (df["symbol"].str.upper() == symbol) &
                (df["instrument_type"].str.upper() == "INDEX")
            )
            matches = df[mask]
            if matches.empty:
                log.error("No index entry found for '%s' in instrument master.", symbol)
                return None
            key = str(matches.iloc[0]["instrument_key"])
            log.info("Upstox index instrument_key for %s = %s", symbol, key)
            return key
        except KeyError as exc:
            log.error("Column missing in Upstox master: %s. Available: %s", exc, list(df.columns))
            return None

    # ------------------------------------------------------------------
    # Download + cache
    # ------------------------------------------------------------------

    def _download_and_cache(self, cache_file: Path) -> pd.DataFrame:
        """Download and concatenate instrument master CSVs from NSE, MCX, and BSE daily."""
        dfs = []
        start = time.monotonic()

        for name, url in MASTER_URLS.items():
            log.info("Downloading %s instrument master from %s ...", name, url)
            try:
                response = requests.get(url, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                import io
                df_part = pd.read_csv(io.BytesIO(response.content), compression="gzip", low_memory=False)
                dfs.append(df_part)
                log.info("Downloaded %s: %d rows", name, len(df_part))
            except Exception as exc:
                log.error("Failed to download/parse %s master: %s", name, exc)
                # If NSE fails, we must raise as it's critical. MCX/BSE are optional.
                if name == "NSE":
                    log.critical(
                        "Critical instrument master (NSE) download failed: %s. Strategy entries/exits, "
                        "strike resolution, and lot-size lookups will fail until this succeeds.", exc,
                    )
                    raise RuntimeError(f"Critical instrument master {name} download failed: {exc}") from exc

        if not dfs:
            log.critical("No instrument master files were successfully downloaded — trading is blind until this is fixed.")
            raise RuntimeError("No instrument master files were successfully downloaded.")

        df_combined = pd.concat(dfs, ignore_index=True)
        # Save as a clean, uncompressed CSV to the cache file
        df_combined.to_csv(cache_file, index=False)

        elapsed = time.monotonic() - start
        size = self._human_size(cache_file.stat().st_size)

        log.info(
            "Combined and saved instruments cache: size=%s | time=%.1fs → %s",
            size, elapsed, cache_file.name,
        )

        return self._load_csv(cache_file)

    def sync_to_db(self, engine) -> None:
        """
        Download today's NSE, MCX, BSE instruments, concatenate them,
        truncate the MySQL `instruments` table, and bulk-load all of them.
        """
        log.info("Syncing instrument master to MySQL database...")
        df = self.get_or_refresh(force=True)


        # Select and map only the columns present in our DB table:
        db_df = df.copy()

        # Rename columns to match MySQL table schema
        col_map = {k: v for k, v in UPSTOX_COLS.items() if k in db_df.columns}
        db_df = db_df.rename(columns=col_map)

        # Clean data for MySQL
        import numpy as np

        # Convert empty strings to actual numeric NaN before replacing with None/NULL
        numeric_cols = ["last_price", "strike", "tick_size", "lot_size"]
        for col in numeric_cols:
            if col in db_df.columns:
                db_df[col] = pd.to_numeric(db_df[col].replace("", np.nan), errors="coerce")

        db_df = db_df.replace({np.nan: None})

        # Ensure all columns exist in the DataFrame
        allowed_cols = [
            "instrument_key", "exchange_token", "symbol", "name",
            "last_price", "expiry", "strike", "tick_size", "lot_size",
            "instrument_type", "option_type", "exchange"
        ]
        for col in allowed_cols:
            if col not in db_df.columns:
                db_df[col] = None

        db_df = db_df[allowed_cols]


        # Truncate and write to database using pandas to_sql in a single transaction
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE instruments"))
            db_df.to_sql(
                name="instruments",
                con=conn,
                if_exists="append",
                index=False,
                chunksize=10000
            )
        log.info("Successfully synced %d instruments to database.", len(db_df))


    def _load_csv(self, path: Path) -> pd.DataFrame:
        """Load CSV from disk and normalise column names."""
        try:
            df = pd.read_csv(path, low_memory=False)
        except Exception as exc:
            raise RuntimeError(f"Failed to parse instrument master CSV '{path}': {exc}") from exc

        col_map = {k: v for k, v in UPSTOX_COLS.items() if k in df.columns}
        df = df.rename(columns=col_map)

        # Fill NaN with empty string for safe string operations
        df = df.fillna("")
        return df

    # ------------------------------------------------------------------
    # Cache file management
    # ------------------------------------------------------------------

    def _today_cache_path(self) -> Path:
        """Return the expected path for today's cache file."""
        today = date.today().isoformat()  # e.g., '2025-07-31'
        return self.cache_dir / f"upstox_instruments_{today}.csv"

    def _cleanup_old_cache(self) -> None:
        """Delete every instrument-master cache file except today's — see module docstring's Cache strategy section for why keeping any older ones serves no purpose."""
        today_path = self._today_cache_path()
        for f in self.cache_dir.glob("upstox_instruments_*.csv"):
            if f != today_path:
                f.unlink()
                log.info("Deleted previous day's cache file: %s", f.name)

    @staticmethod
    def _human_size(size_bytes: int) -> str:
        """Convert bytes to human-readable string."""
        for unit in ("B", "KB", "MB"):
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes //= 1024
        return f"{size_bytes:.1f} GB"
