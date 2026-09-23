"""
data/candles.py — daily + N-minute OHLCV candles for a symbol, backed by
UpstoxBroker.get_historical_candles().

Upstox's historical-candle API only ever returns CLOSED bars — the
currently-forming 5-minute (or daily) candle isn't in it yet, since it
hasn't closed. RealtimeCandleFeed below fills that gap via REST polling
(UpstoxBroker.get_market_depth() — LTP + today's cumulative volume).
merge_realtime_candles() (bottom of this file) combines its live bar(s)
with a REST historical snapshot from fetch_daily_candles/fetch_intraday_candles.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

from services.broker.upstox_broker import UpstoxBroker
from services.utils.logger import get_logger

log = get_logger(__name__)

# Upstox's v3 history API docs claim a ~1-quarter cap for sub-hour
# intervals (5m/15m), but empirically (tested directly against the live
# API) the real cap is much smaller and inconsistent near month
# boundaries — some 30/31-day spans succeed, others reject with
# "Invalid date range" (UDAPI1148) for reasons not documented anywhere.
# 28 days stayed reliably under every failure seen during testing.
_MAX_CHUNK_DAYS = 28

# A second, DIFFERENT and more dangerous failure mode than the one above,
# found independently three times (currency, Binance funding-rate history
# earlier, NSE index futures, and — most consequentially — MCX commodities'
# own original downloader, found 2026-09-18 while building this shared
# utility): a single get_historical_candles() call spanning too wide a
# range doesn't error and doesn't return a clipped-but-honest result — it
# silently returns only a small recent slice, as if the rest of the range
# genuinely had no data. Verified directly: a CRUDEOILM request for
# 2026-08-01..2026-09-18 returned 92 candles; the same range fetched in
# 20-day chunks and unioned returned 7,560 — the wide call was quietly
# discarding ~98.8% of real data, not merely erroring where the docs say
# it should. This is why markets/commodity/data.py's original single-wide-call
# downloader had been silently under-archiving the entire live-trading
# dataset since it was written, undetected until this was built. NEVER
# trust one wide from=/to= call for real backfills — always chunk and
# union, even within _MAX_CHUNK_DAYS above.
_SAFE_BACKFILL_CHUNK_DAYS = 20


_CHUNK_FETCH_TIMEOUT_SEC = 30.0  # generous for a 20-day/5-min chunk; a real response is normally 2-3s (measured)
_CHUNK_TIMEOUT_RETRIES = 2  # extra attempts after the first, before giving up on a chunk as an unknown gap


def fetch_real_history_backward(
    broker: UpstoxBroker, instrument_key: str, unit: str, interval: int,
    max_lookback_days: int = 120, chunk_days: int = _SAFE_BACKFILL_CHUNK_DAYS,
) -> list[dict]:
    """
    Discovers and fetches ALL real history Upstox currently has for one
    instrument, working backward from today in small chunks (see
    _SAFE_BACKFILL_CHUNK_DAYS above for why small chunks are mandatory, not
    just a performance nicety) until two consecutive CONFIRMED-empty chunks
    show the real listing/history start has been passed. Closed candles
    only, oldest-first, de-duplicated by timestamp.

    Use this for every "how far back does real history go" backfill —
    monthly-expiry contracts (MCX/NCD_FO/NSE_FO) don't know their own start
    date in advance, so this discovers it rather than assuming one.

    Each chunk call runs under a hard timeout (see _CHUNK_FETCH_TIMEOUT_SEC)
    -- added 2026-09-19 after a real hang was found rebuilding the equity
    downloader: one specific NSE_EQ instrument's chunk call blocked
    indefinitely, apparently a stuck connection inside the Upstox SDK's own
    HTTP client, which exposes no timeout parameter to set here.

    A timed-out chunk is retried up to _CHUNK_TIMEOUT_RETRIES times, then
    skipped as an unknown GAP -- it does NOT count toward the two-strikes
    "history exhausted" rule, unlike a chunk that genuinely, successfully
    returned zero candles. Conflating the two was a real bug caught while
    building this: an earlier version counted a timeout the same as a
    confirmed-empty chunk, so two consecutive stuck/timed-out requests on
    the MOST RECENT chunks (fetched first, since this walks backward from
    today) made the whole function conclude "no history exists" and stop
    immediately -- discarding years of real older history it never even
    attempted to fetch. Reproduced directly: COALINDIA and INDUSINDBK both
    have 4+ years of real data (every other NIFTY50 stock does), but came
    back completely empty this way before the retry/gap distinction below
    was added. A skipped gap leaves a real hole in that date range (logged),
    not a silent fabrication -- it does not retry indefinitely, and does not
    stop the backward walk from continuing further.

    Uses a FRESH single-worker executor per attempt, shut down without
    waiting on a timeout -- if the underlying call has genuinely wedged
    forever, that one worker thread leaks (reclaimed when the stuck call
    eventually errors/returns, or at process exit) rather than a shared
    pool's own cleanup blocking on it, which would reintroduce a hang on
    every later chunk after the stuck one.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

    today = date.today()
    all_rows: dict[str, dict] = {}
    chunk_end = today
    empty_chunks_in_a_row = 0
    while (today - chunk_end).days < max_lookback_days:
        chunk_start = chunk_end - timedelta(days=chunk_days)

        candles = None
        confirmed_empty = False
        for attempt in range(_CHUNK_TIMEOUT_RETRIES + 1):
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(
                broker.get_historical_candles, instrument_key, unit, interval,
                to_date=chunk_end.isoformat(), from_date=chunk_start.isoformat(),
            )
            try:
                candles = future.result(timeout=_CHUNK_FETCH_TIMEOUT_SEC)
                pool.shutdown(wait=False)
                confirmed_empty = True  # the call actually completed -- None/[] here is a real answer, not a failure
                break
            except FutureTimeoutError:
                pool.shutdown(wait=False)
                log.warning(
                    "fetch_real_history_backward: chunk %s..%s for %s timed out after %.0fs (attempt %d/%d).",
                    chunk_start, chunk_end, instrument_key, _CHUNK_FETCH_TIMEOUT_SEC, attempt + 1, _CHUNK_TIMEOUT_RETRIES + 1,
                )

        if candles:
            for c in candles:
                all_rows[c["timestamp"]] = c
            empty_chunks_in_a_row = 0
        elif confirmed_empty:
            empty_chunks_in_a_row += 1
            if empty_chunks_in_a_row >= 2:
                break
        else:
            log.warning(
                "fetch_real_history_backward: giving up on chunk %s..%s for %s after %d timeouts -- "
                "leaving a real gap in this date range, NOT treated as end-of-history.",
                chunk_start, chunk_end, instrument_key, _CHUNK_TIMEOUT_RETRIES + 1,
            )
        chunk_end = chunk_start - timedelta(days=1)

    return sorted(all_rows.values(), key=lambda c: c["timestamp"])


def fetch_intraday_candles_range(
    broker: UpstoxBroker, instrument_key: str, interval_minutes: int, from_date: str, to_date: str,
) -> list[dict]:
    """
    Closed N-minute candles across an arbitrary [from_date, to_date] range
    (both 'YYYY-MM-DD'), oldest-first — for backtesting, where
    fetch_intraday_candles()'s few-day lookback isn't enough. Chunks the
    request into ~quarter-sized windows (Upstox's own per-request cap for
    sub-hour intervals) and stitches the results together, de-duplicating
    by timestamp in case adjacent chunks overlap at their boundary date.
    """
    start = datetime.fromisoformat(from_date).date()
    end = datetime.fromisoformat(to_date).date()
    if start > end:
        return []

    all_candles: dict[str, dict] = {}
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=_MAX_CHUNK_DAYS), end)
        for c in _fetch_chunk_with_retry(broker, instrument_key, interval_minutes, chunk_start, chunk_end):
            all_candles[c["timestamp"]] = c
        chunk_start = chunk_end + timedelta(days=1)

    return sorted(all_candles.values(), key=lambda c: c["timestamp"])


def _fetch_chunk_with_retry(broker: UpstoxBroker, instrument_key: str, interval_minutes: int, chunk_start, chunk_end) -> list[dict]:
    """
    A single chunked request, sized safely under the documented cap, can
    still be rejected by Upstox for undocumented reasons observed near
    month boundaries (see _MAX_CHUNK_DAYS comment). Rather than silently
    dropping that period's data, bisect and retry on failure down to a
    single day before giving up on that day.
    """
    candles = broker.get_historical_candles(
        instrument_key, unit="minutes", interval=interval_minutes,
        to_date=chunk_end.isoformat(), from_date=chunk_start.isoformat(),
    )
    if candles is not None:
        return candles
    if chunk_start >= chunk_end:
        log.warning("fetch_intraday_candles_range: giving up on %s for %s — even a single day was rejected.", chunk_start, instrument_key)
        return []

    mid = chunk_start + (chunk_end - chunk_start) // 2
    return (
        _fetch_chunk_with_retry(broker, instrument_key, interval_minutes, chunk_start, mid)
        + _fetch_chunk_with_retry(broker, instrument_key, interval_minutes, mid + timedelta(days=1), chunk_end)
    )


def fetch_daily_candles(broker: UpstoxBroker, instrument_key: str, lookback_days: int = 200) -> list[dict]:
    """Closed daily candles, oldest-first, each {"timestamp","open","high","low","close","volume"}."""
    to_date = date.today().isoformat()
    candles = broker.get_historical_candles(instrument_key, unit="days", interval=1, to_date=to_date) or []
    candles = sorted(candles, key=lambda c: c["timestamp"])
    return candles[-lookback_days:]


def fetch_daily_candles_range(broker: UpstoxBroker, instrument_key: str, from_date: str, to_date: str) -> list[dict]:
    """Closed daily candles across an arbitrary [from_date, to_date] range, oldest-first. Daily bars have no quarter-sized request cap, so this is a single call (unlike fetch_intraday_candles_range)."""
    candles = broker.get_historical_candles(instrument_key, unit="days", interval=1, to_date=to_date, from_date=from_date) or []
    return sorted(candles, key=lambda c: c["timestamp"])


def fetch_intraday_candles(
    broker: UpstoxBroker, instrument_key: str, interval_minutes: int = 5, lookback_days: int = 5,
) -> list[dict]:
    """Closed N-minute candles, oldest-first. Does NOT include the still-forming current bar — see RealtimeCandleFeed for that."""
    to_date = date.today().isoformat()
    candles = broker.get_historical_candles(instrument_key, unit="minutes", interval=interval_minutes, to_date=to_date) or []
    candles = sorted(candles, key=lambda c: c["timestamp"])
    cutoff = datetime.now() - timedelta(days=lookback_days)
    return [c for c in candles if datetime.fromisoformat(c["timestamp"].split("+")[0]) >= cutoff]


class RealtimeCandleFeed:
    """
    Keeps a closed-candle history plus one continuously-updated live bar
    for `instrument_key` at `interval_minutes` granularity, refreshed by
    polling `poll_once()` (called automatically by start()'s background
    thread, or by hand if you'd rather drive the loop yourself).
    """

    def __init__(
        self, broker: UpstoxBroker, instrument_key: str, interval_minutes: int = 5, poll_interval_sec: float = 5.0,
    ) -> None:
        self.broker = broker
        self.instrument_key = instrument_key
        self.interval_minutes = interval_minutes
        self.poll_interval_sec = poll_interval_sec

        self._closed_candles: list[dict] = fetch_intraday_candles(broker, instrument_key, interval_minutes)
        self._live_candle: dict | None = None
        self._window_start: datetime | None = None
        self._volume_baseline: float = 0.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _bar_window_start(self, ts: datetime) -> datetime:
        floored_minute = (ts.minute // self.interval_minutes) * self.interval_minutes
        return ts.replace(minute=floored_minute, second=0, microsecond=0)

    def poll_once(self) -> dict | None:
        """Fetch one fresh LTP/volume snapshot and update the live bar. Returns the live candle dict, or None if the fetch failed and no live bar exists yet."""
        depth = self.broker.get_market_depth(self.instrument_key)
        if depth is None:
            log.warning("RealtimeCandleFeed: market depth fetch failed for %s — keeping previous live bar.", self.instrument_key)
            return self._live_candle

        now = datetime.now()
        window_start = self._bar_window_start(now)
        ltp = depth["last_price"]
        day_volume = depth["volume"] or 0

        with self._lock:
            if self._window_start is None or window_start > self._window_start:
                if self._live_candle is not None:
                    self._closed_candles.append(self._live_candle)
                self._window_start = window_start
                self._volume_baseline = day_volume
                self._live_candle = {
                    "timestamp": window_start.isoformat(),
                    "open": ltp, "high": ltp, "low": ltp, "close": ltp,
                    "volume": 0,
                }
            else:
                c = self._live_candle
                c["high"] = max(c["high"], ltp)
                c["low"] = min(c["low"], ltp)
                c["close"] = ltp
                c["volume"] = max(0, day_volume - self._volume_baseline)

            return dict(self._live_candle)

    def get_candles(self) -> list[dict]:
        """Closed candles + the live (still-forming) bar appended, oldest-first."""
        with self._lock:
            candles = list(self._closed_candles)
            if self._live_candle is not None:
                candles.append(dict(self._live_candle))
            return candles

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                except Exception:
                    log.exception("RealtimeCandleFeed: poll failed for %s.", self.instrument_key)
                self._stop_event.wait(self.poll_interval_sec)

        self._thread = threading.Thread(target=_loop, daemon=True, name=f"candle-feed-{self.instrument_key}")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval_sec * 2)
            self._thread = None


def merge_realtime_candles(rest_candles: list[dict], live_candles: list[dict]) -> list[dict]:
    """
    Append only the `live_candles` (from RealtimeCandleFeed or
    UpstoxWebsocketFeed) that are newer than the last REST-fetched closed
    bar, oldest-first. Compares actual datetimes rather than raw
    timestamp strings — a websocket-derived timestamp is UTC
    ('...+00:00') while Upstox's REST history API returns IST-offset
    ('...+05:30'); those sort differently as strings even though the
    datetimes they represent compare correctly once parsed.
    """
    if not rest_candles:
        return list(live_candles)
    if not live_candles:
        return list(rest_candles)

    last_rest_ts = datetime.fromisoformat(rest_candles[-1]["timestamp"])
    extra = [c for c in live_candles if datetime.fromisoformat(c["timestamp"]) > last_rest_ts]
    return rest_candles + extra
