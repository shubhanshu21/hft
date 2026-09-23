"""
broker/instruments.py — Upstox NSE instruments master download & symbol key resolver.

Downloads the official Upstox NSE.csv.gz instrument master once per day,
caches it locally, and provides fast symbol -> instrument_key lookup.

Usage:
    from services.broker.instruments import resolve_symbols, get_instrument_key

    key = get_instrument_key("RELIANCE")        # "NSE_EQ|INE002A01018"
    sym_map = resolve_symbols(["RELIANCE", "HDFCBANK", "TCS"])
"""
from __future__ import annotations

from core.paths import CACHE_DIR
import csv
import re
import gzip
import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"
_CACHE_DIR  = CACHE_DIR
_CACHE_FILE = _CACHE_DIR / "upstox_instruments_nse.csv.gz"
_META_FILE  = _CACHE_DIR / "upstox_instruments_meta.json"

_MCX_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.csv.gz"
_MCX_CACHE_FILE = _CACHE_DIR / "upstox_instruments_mcx.csv.gz"
_MCX_META_FILE  = _CACHE_DIR / "upstox_instruments_mcx_meta.json"

# In-memory cache: {trading_symbol: instrument_key} for NSE_EQ EQUITY rows
_SYMBOL_KEY_MAP: dict[str, str] = {}
# In-memory cache: {commodity_base_symbol: instrument_key} for nearest active MCX futures
_MCX_KEY_MAP: dict[str, str] = {}
_CURRENCY_KEY_MAP: dict[str, str] = {}
_INDEX_FUT_KEY_MAP: dict[str, str] = {}


def _cache_is_fresh(meta_file: Path, cache_file: Path) -> bool:
    if not cache_file.exists() or not meta_file.exists():
        return False
    try:
        meta = json.loads(meta_file.read_text())
        return meta.get("date") == date.today().isoformat()
    except Exception:
        return False


def _download_master() -> None:
    import requests
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Downloading Upstox NSE instruments master from %s ...", _MASTER_URL)
    t0 = time.monotonic()
    try:
        r = requests.get(_MASTER_URL, timeout=30)
        r.raise_for_status()
        _CACHE_FILE.write_bytes(r.content)
        _META_FILE.write_text(json.dumps({"date": date.today().isoformat(), "size": len(r.content)}))
        log.info("Downloaded NSE instruments master (%.1f KB) in %.1fs",
                 len(r.content) / 1024, time.monotonic() - t0)
    except Exception as exc:
        log.error("Failed to download Upstox instruments master: %s", exc)
        raise


def _download_mcx_master() -> None:
    import requests
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Downloading Upstox MCX instruments master from %s ...", _MCX_MASTER_URL)
    t0 = time.monotonic()
    try:
        r = requests.get(_MCX_MASTER_URL, timeout=30)
        r.raise_for_status()
        _MCX_CACHE_FILE.write_bytes(r.content)
        _MCX_META_FILE.write_text(json.dumps({"date": date.today().isoformat(), "size": len(r.content)}))
        log.info("Downloaded MCX instruments master (%.1f KB) in %.1fs",
                 len(r.content) / 1024, time.monotonic() - t0)
    except Exception as exc:
        log.error("Failed to download Upstox MCX instruments master: %s", exc)
        raise


def _load_master() -> None:
    global _SYMBOL_KEY_MAP
    if not _CACHE_FILE.exists():
        raise FileNotFoundError(f"Instrument master cache not found: {_CACHE_FILE}")
    with gzip.open(_CACHE_FILE, "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        _SYMBOL_KEY_MAP = {
            row["tradingsymbol"]: row["instrument_key"]
            for row in reader
            if row.get("exchange") == "NSE_EQ"
            and row.get("instrument_type") == "EQUITY"
        }
    log.info("Loaded %d NSE EQ equity instruments into memory.", len(_SYMBOL_KEY_MAP))


def _load_currency_master() -> None:
    """NSE currency derivatives (NCD_FO/FUTCUR) live in the SAME NSE.csv.gz master as
    equities -- not a separate file like MCX. Added 2026-09-18 for USDINR/EURINR/
    GBPINR/JPYINR futures, same nearest-expiry-per-base-symbol pattern as
    _load_mcx_master()."""
    global _CURRENCY_KEY_MAP
    if not _CACHE_FILE.exists():
        return
    today_str = date.today().isoformat()
    # NCD_FO lists both WEEKLY expiries (tradingsymbol e.g. "USDINR26918FUT" --
    # year+month+day, digits only) and the standard MONTHLY contract (e.g.
    # "USDINR26SEPFUT" -- year+3-letter-month). Weekly ones can be days from
    # expiry with almost no listing history; only the monthly pattern gives a
    # contract comparable in life span to MCX's, which is what we actually
    # want to trade/backtest. Regex mirrors resolve_commodity_key()'s pattern.
    monthly_pattern = re.compile(r"^([A-Z]+)\d{2}[A-Z]{3}FUT$")
    candidates: dict[str, list[dict]] = {}
    with gzip.open(_CACHE_FILE, "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("exchange") != "NCD_FO" or row.get("instrument_type") != "FUTCUR":
                continue
            exp = row.get("expiry", "")
            if exp < today_str:
                continue
            tsym = row.get("tradingsymbol", "")
            m = monthly_pattern.match(tsym)
            if not m:
                continue
            base = m.group(1)
            if base in ("USDINR", "EURINR", "GBPINR", "JPYINR"):
                candidates.setdefault(base, []).append(row)

    _CURRENCY_KEY_MAP = {}
    for base, rows in candidates.items():
        rows.sort(key=lambda r: r.get("expiry", ""))
        _CURRENCY_KEY_MAP[base] = rows[0]["instrument_key"]
        log.info("Resolved currency %s -> %s (%s, Expiry: %s)", base, rows[0]["instrument_key"],
                  rows[0]["tradingsymbol"], rows[0].get("expiry"))


def _load_index_futures_master() -> None:
    """NSE index futures (NSE_FO/FUTIDX) live in the SAME NSE.csv.gz master as
    equities/currency. Nearest-expiry-per-base-symbol pattern, same as
    _load_currency_master()/_load_mcx_master(). Restricted to NIFTY/BANKNIFTY
    only -- the two genuinely liquid index futures; FINNIFTY/MIDCPNIFTY/
    NIFTYNXT50/NIFTYFPI also exist in the master but weren't validated."""
    global _INDEX_FUT_KEY_MAP
    if not _CACHE_FILE.exists():
        return
    today_str = date.today().isoformat()
    candidates: dict[str, list[dict]] = {}
    with gzip.open(_CACHE_FILE, "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("exchange") != "NSE_FO" or row.get("instrument_type") != "FUTIDX":
                continue
            if row.get("name") not in ("NIFTY", "BANKNIFTY"):
                continue
            exp = row.get("expiry", "")
            if exp < today_str:
                continue
            candidates.setdefault(row["name"], []).append(row)

    _INDEX_FUT_KEY_MAP = {}
    for base, rows in candidates.items():
        rows.sort(key=lambda r: r.get("expiry", ""))
        _INDEX_FUT_KEY_MAP[base] = rows[0]["instrument_key"]
        log.info("Resolved index future %s -> %s (%s, Expiry: %s)", base, rows[0]["instrument_key"],
                  rows[0]["tradingsymbol"], rows[0].get("expiry"))


def _load_mcx_master() -> None:
    global _MCX_KEY_MAP
    if not _MCX_CACHE_FILE.exists():
        return
    today_str = date.today().isoformat()
    candidates: dict[str, list[dict]] = {}
    with gzip.open(_MCX_CACHE_FILE, "rt", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            itype = row.get("instrument_type", "")
            if itype in ("FUTCOM", "FUTIDX", "FUT"):
                exp = row.get("expiry", "")
                if exp >= today_str:
                    tsym = row.get("tradingsymbol", "")
                    # Bug found live 2026-09-23: MCX added a "SILVER100" contract
                    # series (tradingsymbol "SILVER10026SEPFUT", lot_size 100, an
                    # entirely different/smaller-denomination product from the
                    # real 30kg SILVER contract) that also matches
                    # tsym.startswith("SILVER"). Because it expires sooner than
                    # the real SILVER contract, nearest-expiry selection below
                    # picked it instead -- silently trading a contract with a
                    # ~100x different price scale and a real lot_size (100) that
                    # doesn't match markets/commodity/costs.py's COMMODITY_SPECS
                    # assumption (30), corrupting both price and every
                    # size/margin/P&L calculation for "SILVER". Must be excluded
                    # explicitly, the same way SILVERMIC/SILVERM are disambiguated
                    # below -- it is never a valid match for base "SILVER".
                    if tsym.startswith("SILVER100"):
                        continue
                    # Match base symbols -- extended 2026-09-17 to survey other MCX
                    # commodities beyond crude/natgas (Gold Mini, Silver Mini/Micro,
                    # Copper already have cost-model support in markets/commodity/costs.py's
                    # get_contract_multiplier fallbacks; this was the actual blocker,
                    # not data availability -- Upstox's MCX master already lists these.
                    # Order matters: startswith() means a more specific prefix (SILVERMIC,
                    # SILVERM) must be checked before the shorter prefix it also matches
                    # (SILVER) would otherwise steal the match. ALUMINIUM/ALUMINI, LEAD/
                    # LEADMINI, ZINC/ZINCMINI added 2026-09-18 (base metals survey) -- same
                    # ordering rule: LEADMINI/ZINCMINI/ALUMINIUM before the shorter prefix
                    # each also starts with (LEAD/ZINC/ALUMINI respectively).
                    for base in ["CRUDEOILM", "NATGASMINI", "CRUDEOIL", "NATURALGAS",
                                 "GOLDM", "GOLD", "SILVERMIC", "SILVERM", "SILVER", "COPPER",
                                 "ALUMINIUM", "ALUMINI", "LEADMINI", "LEAD", "ZINCMINI", "ZINC", "NICKEL"]:
                        if tsym.startswith(base):
                            if base not in candidates:
                                candidates[base] = []
                            candidates[base].append(row)
                            break
    
    # Select nearest expiry for each commodity base
    _MCX_KEY_MAP = {}
    for base, rows in candidates.items():
        rows.sort(key=lambda r: r.get("expiry", ""))
        _MCX_KEY_MAP[base] = rows[0]["instrument_key"]
        log.info("Resolved MCX %s -> %s (%s, Expiry: %s)", base, rows[0]["instrument_key"], rows[0]["tradingsymbol"], rows[0].get("expiry"))


def ensure_master(force: bool = False) -> None:
    """Ensure the instruments masters (NSE & MCX) are fresh and loaded in memory.

    Bug fixed 2026-09-22: this used to gate every _load_*_master() call on
    "if the in-memory map is still empty" -- which meant that once a map was
    populated ONCE, it was NEVER re-parsed again for the rest of that
    process's life, even on a day where the underlying cache file WAS
    freshly re-downloaded (e.g. after a monthly MCX contract rollover).
    live_dryrun.py's day-rollover loop calls this daily specifically to stay
    "rollover-safe" (see its own _build_symbol_map() docstring), but that
    safety net was silently defeated by this short-circuit the whole time --
    a long-running process would keep resolving whatever contract was
    current when it first started, forever. Caught directly: a MCX
    CRUDEOILM candle fetch started failing with Upstox's "Invalid
    Instrument key" (UDAPI100011) on every single scan after ~11 hours of
    uptime, tracked back to build_mcx_commodity_map() falling through to its
    hardcoded stale-key fallback (also fixed below) because _MCX_KEY_MAP
    never got a chance to re-populate from the freshly-downloaded file.
    Now each master's in-memory map is force-reloaded whenever its file was
    ACTUALLY just (re-)downloaded, not only when the map happens to be
    empty -- restoring the rollover-safety this function's callers already
    assume it provides."""
    nse_downloaded = force or not _cache_is_fresh(_META_FILE, _CACHE_FILE)
    if nse_downloaded:
        _download_master()
    if nse_downloaded or not _SYMBOL_KEY_MAP:
        _load_master()

    mcx_downloaded = force or not _cache_is_fresh(_MCX_META_FILE, _MCX_CACHE_FILE)
    if mcx_downloaded:
        try:
            _download_mcx_master()
        except Exception as e:
            log.warning("Could not download MCX master: %s", e)
            mcx_downloaded = False  # the on-disk file wasn't actually refreshed -- don't force a reparse of it below
    if (mcx_downloaded or not _MCX_KEY_MAP) and _MCX_CACHE_FILE.exists():
        _load_mcx_master()
    if (nse_downloaded or not _CURRENCY_KEY_MAP) and _CACHE_FILE.exists():
        _load_currency_master()
    if (nse_downloaded or not _INDEX_FUT_KEY_MAP) and _CACHE_FILE.exists():
        _load_index_futures_master()


def get_instrument_key(symbol: str, auto_refresh: bool = True) -> Optional[str]:
    """Return the Upstox instrument_key for a symbol (NSE EQ or MCX commodity)."""
    if not _SYMBOL_KEY_MAP or auto_refresh:
        ensure_master()
    sym_u = symbol.upper()
    if sym_u in _MCX_KEY_MAP:
        return _MCX_KEY_MAP[sym_u]
    if sym_u in _CURRENCY_KEY_MAP:
        return _CURRENCY_KEY_MAP[sym_u]
    if sym_u in _INDEX_FUT_KEY_MAP:
        return _INDEX_FUT_KEY_MAP[sym_u]
    return _SYMBOL_KEY_MAP.get(sym_u)


def resolve_symbols(symbols: list[str]) -> dict[str, str]:
    """Resolve a list of NSE trading symbols to {symbol: instrument_key}. Skips unknown symbols."""
    ensure_master()
    result = {}
    for sym in symbols:
        key = (_SYMBOL_KEY_MAP.get(sym.upper()) or _MCX_KEY_MAP.get(sym.upper())
               or _CURRENCY_KEY_MAP.get(sym.upper()) or _INDEX_FUT_KEY_MAP.get(sym.upper()))
        if key:
            result[sym] = key
        else:
            log.warning("Symbol not found in master: %s", sym)
    return result


def build_currency_map() -> dict[str, str]:
    """Return {base_symbol: instrument_key} for active NSE currency futures (nearest expiry)."""
    ensure_master()
    if _CURRENCY_KEY_MAP:
        return _CURRENCY_KEY_MAP
    return {}


def build_index_futures_map() -> dict[str, str]:
    """Return {base_symbol: instrument_key} for active NSE index futures (NIFTY/BANKNIFTY, nearest expiry)."""
    ensure_master()
    if _INDEX_FUT_KEY_MAP:
        return _INDEX_FUT_KEY_MAP
    return {}


def build_mcx_commodity_map() -> dict[str, str]:
    """Return {trading_symbol: instrument_key} for active MCX Mini & Standard commodity futures."""
    ensure_master()
    if _MCX_KEY_MAP:
        return _MCX_KEY_MAP
    # Bug fixed 2026-09-22: this used to fall back to a hardcoded dict of
    # instrument keys "if offline" -- but those keys are for whichever MCX
    # contract happened to be current when this fallback was written, and
    # MCX contracts are monthly-expiry. Once that month rolled over, the
    # fallback silently pointed at an EXPIRED, delisted contract -- caught
    # directly when a live process fell through to this path (see
    # ensure_master()'s docstring for how) and every candle fetch for
    # CRUDEOILM started failing with Upstox's "Invalid Instrument key"
    # (UDAPI100011). A stale-but-plausible-looking key is worse than no key
    # at all here -- it fails loudly at the API instead of silently trading
    # (or in live_trading.py's case, ATTEMPTING TO PLACE A REAL ORDER
    # against) the wrong contract. Log clearly and return empty instead, same
    # safe-degradation pattern build_currency_map()/build_index_futures_map()
    # already use.
    log.error("MCX instrument master unavailable (no cached data and download failed) -- "
              "returning no MCX symbols rather than stale hardcoded contract keys.")
    return {}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    force = "--force" in sys.argv
    ensure_master(force=force)

    queries = [s for s in sys.argv[1:] if not s.startswith("--")] or [
        "RELIANCE", "HDFCBANK", "TCS", "KOTAKBANK", "BAJFINANCE",
        "TATASTEEL", "CANBK", "POWERGRID", "M&M", "BHARTIARTL",
    ]

    print(f"\n{'Symbol':15s} {'Instrument Key':30s}")
    print("-" * 50)
    for sym in queries:
        key = get_instrument_key(sym, auto_refresh=False)
        print(f"{sym:15s} {key or '*** NOT FOUND ***'}")
    print(f"\nTotal NSE EQ equities in master: {len(_SYMBOL_KEY_MAP)}")
