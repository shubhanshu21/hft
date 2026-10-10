"""Order-book recorder: Upstox full-mode feed updates (5-level depth, last trade, volume, OI; IV and Greeks for options) to disk.

    python3 -m engine.depth_recorder            # systemd: hft-depth-recorder.service

Why: order-book imbalance is the one short-horizon predictor with real academic support (Cont, Kukanov & Stoikov), and Upstox keeps no depth
history (docs/COMMODITY_LEADING_SIGNALS.md); trade-by-trade updates give order flow (volume at the bid vs at the ask); the live option chain
gives intraday positioning (open interest, implied volatility, Greeks) that the expired-contract history does not contain.

Records only; places no orders and does not touch the trading daemon or its DB. One WebSocket connection (Upstox MarketDataStreamerV3, mode
"full"), connected on weekdays during market hours, re-resolved each day (front contracts, option strikes), reconnected if the feed goes silent.
Output: var/archive/depth/<SYMBOL>/<YYYY-MM-DD>.csv (gzipped once the day is over).
  * every update is kept for contracts and stocks (DEPTH_EVERY_S, default 0 = no sampling);
  * NIFTYOPT = the Nifty option chain: current weekly expiry, ATM +-DEPTH_OPTION_STRIKES strikes (default 10), calls and puts, one row per
    option every DEPTH_OPTION_EVERY_S seconds (default 15) -- positioning changes slowly, and 42 options updating many times a second
    would cost ~100 MB a day.
Symbols: DEPTH_SYMBOLS in .env (default below; NIFTY / BANKNIFTY = the front-month index futures, stocks = NSE cash).
"""
from __future__ import annotations

import csv
import gzip
import os
import shutil
import signal
import threading
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.paths import ARCHIVE_ROOT
from services.utils.logger import get_logger

log = get_logger("depth_recorder")
IST = ZoneInfo("Asia/Kolkata")
DEPTH_DIR = ARCHIVE_ROOT / "depth"
DEFAULT_SYMBOLS = "USDINR SILVERMIC GOLDTEN CRUDEOILM NIFTY BANKNIFTY RELIANCE HDFCBANK ICICIBANK INFY TCS NIFTYOPT"
OPTION_GROUPS = {"NIFTYOPT": ("NIFTY", "NSE_INDEX|Nifty 50", 50)}      # pseudo-symbol -> (option underlying name, spot key, strike step)
LEVELS = 5
SESSION = ((8, 58), (23, 35))          # MCX 09:00-23:30 covers NSE currency 09:00-17:00; a few minutes either side
SILENT_RECONNECT_S = 180               # no update for this long inside the session -> reconnect
FLUSH_EVERY_S = 5

COLUMNS = (["recv_ms", "instrument", "ltt_ms", "ltp", "ltq", "atp", "vtt", "tbq", "tsq", "oi", "iv", "delta", "gamma", "theta", "vega"]
           + [f"{side}{k}_{lvl}" for lvl in range(1, LEVELS + 1) for side in ("bid", "ask") for k in ("p", "q")])


def in_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return SESSION[0] <= hm < SESSION[1]


def parse_feed(feed: dict, recv_ms: int, instrument: str | None = None) -> dict | None:
    """One instrument's entry from a 'feeds' message -> a flat row, or None if it carries no full-mode market data."""
    ff = feed.get("fullFeed") or feed.get("ff") or {}
    m = ff.get("marketFF")
    if not m:
        return None
    ltpc = m.get("ltpc", {})
    g = m.get("optionGreeks") or {}
    row = {"recv_ms": recv_ms, "instrument": instrument, "ltt_ms": ltpc.get("ltt"), "ltp": ltpc.get("ltp"), "ltq": ltpc.get("ltq"),
           "atp": m.get("atp"), "vtt": m.get("vtt"), "tbq": m.get("tbq"), "tsq": m.get("tsq"), "oi": m.get("oi"), "iv": m.get("iv"),
           "delta": g.get("delta"), "gamma": g.get("gamma"), "theta": g.get("theta"), "vega": g.get("vega")}
    quotes = (m.get("marketLevel") or {}).get("bidAskQuote") or []
    for lvl in range(1, LEVELS + 1):
        q = quotes[lvl - 1] if lvl <= len(quotes) else {}
        row[f"bidp_{lvl}"], row[f"bidq_{lvl}"] = q.get("bidP"), q.get("bidQ")
        row[f"askp_{lvl}"], row[f"askq_{lvl}"] = q.get("askP"), q.get("askQ")
    return row


class DayFiles:
    """Appends rows to <root>/<SYMBOL>/<day>.csv; compresses a file once its day is over. A file begun with a different column layout
    (an older recorder version earlier the same day) is never appended to: the rows go to <day>-2.csv instead."""

    HEADER = ",".join(COLUMNS)

    def __init__(self, root: Path = DEPTH_DIR):
        self.root = root
        self._open: dict[str, tuple[str, Path, object, csv.DictWriter]] = {}

    def _path(self, symbol: str, day: str) -> Path:
        path, n = self.root / symbol / f"{day}.csv", 1
        while path.exists():
            with open(path) as fh:
                if fh.readline().strip() == self.HEADER:
                    break
            n += 1
            path = self.root / symbol / f"{day}-{n}.csv"
        return path

    def write(self, symbol: str, day: str, rows: list[dict]) -> None:
        cur = self._open.get(symbol)
        if cur is None or cur[0] != day:
            if cur is not None:
                self._close(symbol)
            path = self._path(symbol, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            fh = open(path, "a", newline="")
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            self._open[symbol] = (day, path, fh, w)
            cur = self._open[symbol]
        cur[3].writerows(rows)
        cur[2].flush()

    def _close(self, symbol: str) -> None:
        day, path, fh, _ = self._open.pop(symbol)
        fh.close()
        compress_day(path)

    def close_all(self) -> None:
        for s in list(self._open):
            day, path, fh, _ = self._open.pop(s)
            fh.close()


def compress_day(path: Path) -> None:
    if path.exists():
        with open(path, "rb") as src, gzip.open(str(path) + ".gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()


def compress_finished_days(root: Path = DEPTH_DIR, today: str | None = None) -> None:
    """Gzip any plain CSV from an earlier day (left behind by a crash or restart)."""
    today = today or datetime.now(IST).strftime("%Y-%m-%d")
    for p in root.glob("*/*.csv"):
        if p.stem[:10] < today:
            compress_day(p)


def option_chain(rows: list[dict], name: str, spot: float, step: float, n: int, today: str) -> dict[str, str]:
    """{instrument_key: tradingsymbol} for the nearest expiry on/after `today`: the ATM strike +-n steps, calls and puts.
    `rows` are instrument-master rows (exchange, name, instrument_type, option_type, expiry, strike, instrument_key, tradingsymbol)."""
    opts = [r for r in rows if r.get("exchange") == "NSE_FO" and r.get("name") == name and r.get("instrument_type") == "OPTIDX"
            and r.get("expiry", "") >= today]
    if not opts or not spot:
        return {}
    expiry = min(r["expiry"] for r in opts)
    atm = round(spot / step) * step
    wanted = {atm + k * step for k in range(-n, n + 1)}
    return {r["instrument_key"]: r["tradingsymbol"] for r in opts
            if r["expiry"] == expiry and float(r["strike"]) in wanted and r.get("option_type") in ("CE", "PE")}


def resolve_keys(symbols: list[str], n_strikes: int = 10) -> dict[str, tuple[str, str | None]]:
    """{instrument_key: (symbol, instrument name or None)}: today's front contracts for MCX, currency and index futures (NIFTY, BANKNIFTY),
    NSE cash for stocks, and the option chain for an OPTION_GROUPS pseudo-symbol."""
    from services.broker.instruments import build_currency_map, build_index_futures_map, build_mcx_commodity_map, get_instrument_key
    m = {**build_mcx_commodity_map(), **build_currency_map(), **build_index_futures_map()}
    out: dict[str, tuple[str, str | None]] = {}
    for s in symbols:
        if s in OPTION_GROUPS:
            for key, tsym in resolve_option_group(s, n_strikes).items():
                out[key] = (s, tsym)
            continue
        key = m.get(s) or get_instrument_key(s)
        if key:
            out[key] = (s, None)
    return out


def resolve_option_group(group: str, n_strikes: int) -> dict[str, str]:
    from services.auth.upstox_auto_login import ensure_fresh_upstox_token
    from services.broker.instruments import _CACHE_FILE, ensure_master
    from services.broker.upstox_broker import UpstoxBroker
    name, spot_key, step = OPTION_GROUPS[group]
    ensure_master()
    try:
        spot = UpstoxBroker(access_token=ensure_fresh_upstox_token() or "", dry_run=True).get_ltp(spot_key)
        with gzip.open(_CACHE_FILE, "rt") as fh:
            rows = list(csv.DictReader(fh))
    except Exception as e:
        log.warning("%s: option chain not resolved (%s)", group, e)
        return {}
    chain = option_chain(rows, name, float(spot or 0), step, n_strikes, date.today().isoformat())
    log.info("%s: %d options around spot %.1f", group, len(chain), float(spot or 0))
    return chain


class Recorder:
    def __init__(self, symbols: list[str], every_s: float = 0.0, option_every_s: float = 15.0, n_strikes: int = 10):
        self.symbols = symbols
        self.every_ms = every_s * 1000
        self.option_every_ms = option_every_s * 1000
        self.n_strikes = n_strikes
        self.files = DayFiles()
        self.buf: dict[str, list[dict]] = {}
        self.lock = threading.Lock()
        self.last_msg = 0.0
        self.counts: dict[str, int] = {}
        self.last_kept: dict[str, int] = {}          # instrument key -> recv_ms of the last row kept (for the sampling intervals)
        self.streamer = None
        self.keys: dict[str, tuple[str, str | None]] = {}
        self.day = ""
        self.stop = threading.Event()

    def on_message(self, msg: dict) -> None:
        feeds = msg.get("feeds") if isinstance(msg, dict) else None
        if not feeds:
            return
        recv_ms = int(time.time() * 1000)
        with self.lock:
            self.last_msg = time.time()
            for key, feed in feeds.items():
                sym, inst = self.keys.get(key, (None, None))
                if sym is None:
                    continue
                gap = self.option_every_ms if sym in OPTION_GROUPS else self.every_ms
                if gap and recv_ms - self.last_kept.get(key, -10**15) < gap:
                    continue
                row = parse_feed(feed, recv_ms, inst)
                if row:
                    self.buf.setdefault(sym, []).append(row)
                    self.counts[sym] = self.counts.get(sym, 0) + 1
                    self.last_kept[key] = recv_ms

    def flush(self) -> None:
        with self.lock:
            buf, self.buf = self.buf, {}
        day = datetime.now(IST).strftime("%Y-%m-%d")
        for sym, rows in buf.items():
            if rows:
                self.files.write(sym, day, rows)

    def connect(self) -> None:
        import upstox_client
        from upstox_client.feeder.market_data_streamer_v3 import MarketDataStreamerV3
        from services.auth.upstox_auto_login import ensure_fresh_upstox_token
        self.disconnect()
        self.keys = resolve_keys(self.symbols, self.n_strikes)
        if not self.keys:
            log.warning("no instrument keys resolved for %s", self.symbols)
            return
        cfg = upstox_client.Configuration()
        cfg.access_token = ensure_fresh_upstox_token() or ""
        s = MarketDataStreamerV3(upstox_client.ApiClient(cfg), list(self.keys), "full")
        s.on("message", self.on_message)
        s.on("error", lambda e: log.warning("depth feed error: %s", e))
        s.on("close", lambda *a: log.info("depth feed closed"))
        s.auto_reconnect(True, interval=5, retry_count=20)
        threading.Thread(target=s.connect, name="depth-feed", daemon=True).start()
        self.streamer = s
        self.last_msg = time.time()
        groups: dict[str, int] = {}
        for sym, _ in self.keys.values():
            groups[sym] = groups.get(sym, 0) + 1
        log.info("depth feed connecting: %d instruments %s", len(self.keys), groups)

    def disconnect(self) -> None:
        if self.streamer is not None:
            try:
                self.streamer.disconnect()
            except Exception:
                pass
            self.streamer = None

    def run(self) -> None:
        DEPTH_DIR.mkdir(parents=True, exist_ok=True)
        compress_finished_days()
        last_report = time.time()
        while not self.stop.is_set():
            now = datetime.now(IST)
            today = now.strftime("%Y-%m-%d")
            if in_session(now):
                if self.streamer is None or today != self.day:
                    self.day = today
                    self.connect()
                elif time.time() - self.last_msg > SILENT_RECONNECT_S:
                    log.warning("depth feed silent for %.0fs -- reconnecting", time.time() - self.last_msg)
                    self.connect()
            elif self.streamer is not None:
                self.flush()
                self.disconnect()
                self.files.close_all()
                compress_finished_days(today="9999-12-31")          # the session is over: every file on disk is complete
                log.info("session over -- feed disconnected; rows today: %s", self.counts)
                self.counts = {}
            self.flush()
            if time.time() - last_report > 600 and self.streamer is not None:
                log.info("depth rows so far today: %s", self.counts)
                last_report = time.time()
            self.stop.wait(FLUSH_EVERY_S)
        self.flush()
        self.disconnect()
        self.files.close_all()


def main() -> int:
    from dotenv import load_dotenv
    from core.paths import BACKEND_ROOT, LOG_DIR
    from services.utils.logger import setup_logger
    load_dotenv(BACKEND_ROOT / ".env")
    setup_logger("", log_file=str(LOG_DIR / "depth_recorder.log"))
    symbols = (os.environ.get("DEPTH_SYMBOLS") or DEFAULT_SYMBOLS).split()
    rec = Recorder(symbols, every_s=float(os.environ.get("DEPTH_EVERY_S", "0")),
                   option_every_s=float(os.environ.get("DEPTH_OPTION_EVERY_S", "15")),
                   n_strikes=int(os.environ.get("DEPTH_OPTION_STRIKES", "10")))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: rec.stop.set())
    log.info("depth recorder starting for %s -> %s", symbols, DEPTH_DIR)
    rec.run()
    log.info("depth recorder stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
