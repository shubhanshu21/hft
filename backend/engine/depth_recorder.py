"""Order-book recorder: every Upstox full-mode feed update (5-level depth, last trade, volume) for the paper-traded contracts, to disk.

    python3 -m engine.depth_recorder            # systemd: hft-depth-recorder.service

Why: order-book imbalance is the one short-horizon predictor with real academic support (Cont, Kukanov & Stoikov), and Upstox keeps no depth
history (docs/COMMODITY_LEADING_SIGNALS.md). After a few weeks of recording, the imbalance can be tested against forward 1-5 minute returns.

Records only; places no orders and does not touch the trading daemon or its DB. One WebSocket connection (Upstox MarketDataStreamerV3, mode
"full"), connected on weekdays during market hours, re-mapped to the front contract each day, reconnected if the feed goes silent.
Output: var/archive/depth/<SYMBOL>/<YYYY-MM-DD>.csv (gzipped once the day is over), at most one row per instrument per second.
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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.paths import ARCHIVE_ROOT
from services.utils.logger import get_logger

log = get_logger("depth_recorder")
IST = ZoneInfo("Asia/Kolkata")
DEPTH_DIR = ARCHIVE_ROOT / "depth"
DEFAULT_SYMBOLS = "USDINR SILVERMIC GOLDTEN CRUDEOILM NIFTY BANKNIFTY RELIANCE HDFCBANK ICICIBANK INFY TCS"
LEVELS = 5
SESSION = ((8, 58), (23, 35))          # MCX 09:00-23:30 covers NSE currency 09:00-17:00; a few minutes either side
SILENT_RECONNECT_S = 180               # no update for this long inside the session -> reconnect
FLUSH_EVERY_S = 5

COLUMNS = (["recv_ms", "ltt_ms", "ltp", "ltq", "vtt", "tbq", "tsq", "oi"]
           + [f"{side}{k}_{lvl}" for lvl in range(1, LEVELS + 1) for side in ("bid", "ask") for k in ("p", "q")])


def in_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return SESSION[0] <= hm < SESSION[1]


def parse_feed(feed: dict, recv_ms: int) -> dict | None:
    """One instrument's entry from a 'feeds' message -> a flat row, or None if it carries no full-mode market data."""
    ff = feed.get("fullFeed") or feed.get("ff") or {}
    m = ff.get("marketFF")
    if not m:
        return None
    ltpc = m.get("ltpc", {})
    row = {"recv_ms": recv_ms, "ltt_ms": ltpc.get("ltt"), "ltp": ltpc.get("ltp"), "ltq": ltpc.get("ltq"),
           "vtt": m.get("vtt"), "tbq": m.get("tbq"), "tsq": m.get("tsq"), "oi": m.get("oi")}
    quotes = (m.get("marketLevel") or {}).get("bidAskQuote") or []
    for lvl in range(1, LEVELS + 1):
        q = quotes[lvl - 1] if lvl <= len(quotes) else {}
        row[f"bidp_{lvl}"], row[f"bidq_{lvl}"] = q.get("bidP"), q.get("bidQ")
        row[f"askp_{lvl}"], row[f"askq_{lvl}"] = q.get("askP"), q.get("askQ")
    return row


class DayFiles:
    """Appends rows to <root>/<SYMBOL>/<day>.csv; compresses a file once its day is over."""

    def __init__(self, root: Path = DEPTH_DIR):
        self.root = root
        self._open: dict[str, tuple[str, object, csv.DictWriter]] = {}

    def write(self, symbol: str, day: str, rows: list[dict]) -> None:
        cur = self._open.get(symbol)
        if cur is None or cur[0] != day:
            if cur is not None:
                self._close(symbol)
            path = self.root / symbol / f"{day}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            fh = open(path, "a", newline="")
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            if new:
                w.writeheader()
            self._open[symbol] = (day, fh, w)
            cur = self._open[symbol]
        cur[2].writerows(rows)
        cur[1].flush()

    def _close(self, symbol: str) -> None:
        day, fh, _ = self._open.pop(symbol)
        fh.close()
        compress_day(self.root / symbol / f"{day}.csv")

    def close_all(self) -> None:
        for s in list(self._open):
            day, fh, _ = self._open.pop(s)
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
        if p.stem < today:
            compress_day(p)


def resolve_keys(symbols: list[str]) -> dict[str, str]:
    """{instrument_key: symbol}: today's front contracts for MCX, currency and index futures (NIFTY, BANKNIFTY); NSE cash for stocks."""
    from services.broker.instruments import build_currency_map, build_index_futures_map, build_mcx_commodity_map, get_instrument_key
    m = {**build_mcx_commodity_map(), **build_currency_map(), **build_index_futures_map()}
    out = {}
    for s in symbols:
        key = m.get(s) or get_instrument_key(s)
        if key:
            out[key] = s
    return out


class Recorder:
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.files = DayFiles()
        self.buf: dict[str, list[dict]] = {}
        self.lock = threading.Lock()
        self.last_msg = 0.0
        self.counts: dict[str, int] = {}
        self.last_sec: dict[str, int] = {}           # at most one row per instrument per second (the latest): keeps a liquid stock's day ~1 MB
        self.streamer = None
        self.keys: dict[str, str] = {}
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
                sym = self.keys.get(key)
                row = parse_feed(feed, recv_ms) if sym else None
                if row:
                    rows = self.buf.setdefault(sym, [])
                    sec = recv_ms // 1000
                    if rows and self.last_sec.get(sym) == sec:
                        rows[-1] = row                           # same second: keep only the newest state
                    else:
                        rows.append(row)
                        self.counts[sym] = self.counts.get(sym, 0) + 1
                    self.last_sec[sym] = sec

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
        self.keys = resolve_keys(self.symbols)
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
        log.info("depth feed connecting: %s", {v: k for k, v in self.keys.items()})

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
                log.info("session over -- feed disconnected; updates today: %s", self.counts)
                self.counts = {}
            self.flush()
            if time.time() - last_report > 600 and self.streamer is not None:
                log.info("depth updates so far today: %s", self.counts)
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
    rec = Recorder(symbols)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *a: rec.stop.set())
    log.info("depth recorder starting for %s -> %s", symbols, DEPTH_DIR)
    rec.run()
    log.info("depth recorder stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
