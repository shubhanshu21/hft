"""
broker/feed_streamer.py — Upstox Market Data V3 WebSocket Feeder & Quote Cache.

Provides real-time tick streaming and Level 2 market depth via Upstox MarketDataStreamerV3
with automatic background reconnection, thread-safe quote caching, and graceful REST fallback.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set

import upstox_client
from upstox_client.feeder.market_data_streamer_v3 import MarketDataStreamerV3

from utils.logger import get_logger

log = get_logger(__name__)


class QuoteCache:
    """Thread-safe in-memory cache of streaming market quotes and order book depth."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._last_update_ts: Dict[str, float] = {}

    def update(self, instrument_key: str, data: Dict[str, Any]) -> None:
        with self._lock:
            if instrument_key not in self._quotes:
                self._quotes[instrument_key] = {}
            self._quotes[instrument_key].update(data)
            self._last_update_ts[instrument_key] = time.time()

    def get_quote(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._quotes.get(instrument_key, {}).copy() if instrument_key in self._quotes else None

    def get_ltp(self, instrument_key: str, max_age_sec: float = 60.0) -> Optional[float]:
        with self._lock:
            if instrument_key not in self._quotes:
                return None
            age = time.time() - self._last_update_ts.get(instrument_key, 0.0)
            if age > max_age_sec:
                return None
            q = self._quotes[instrument_key]
            # Try LTP from various fields parsed from protobuf/JSON
            ltp = q.get("ltp") or q.get("last_price") or q.get("close")
            return float(ltp) if ltp is not None else None

    def get_all_quotes(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: v.copy() for k, v in self._quotes.items()}


class UpstoxFeedStreamer:
    """
    Manages the Upstox MarketDataStreamerV3 WebSocket connection,
    subscribing to live instruments and caching quotes in memory.
    """

    def __init__(self, access_token: str, initial_keys: Optional[List[str]] = None, mode: str = "full") -> None:
        if not access_token:
            raise ValueError("access_token must not be empty.")

        self.access_token = access_token
        self.mode = mode
        self.subscribed_keys: Set[str] = set(initial_keys or [])
        self.cache = QuoteCache()
        self.is_connected = False
        self._streamer: Optional[MarketDataStreamerV3] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._init_client()

    def _init_client(self) -> None:
        try:
            config = upstox_client.Configuration()
            config.access_token = self.access_token
            api_client = upstox_client.ApiClient(config)

            self._streamer = MarketDataStreamerV3(
                api_client=api_client,
                instrumentKeys=list(self.subscribed_keys),
                mode=self.mode,
            )

            # Register event handlers
            self._streamer.on("open", self._on_open)
            self._streamer.on("message", self._on_message)
            self._streamer.on("error", self._on_error)
            self._streamer.on("close", self._on_close)
            self._streamer.auto_reconnect(True, interval=2, retry_count=10)
        except Exception as e:
            log.warning("Failed to initialize Upstox MarketDataStreamerV3: %s", e)
            self._streamer = None

    def _on_open(self, *args) -> None:
        self.is_connected = True
        log.info("🌐 Upstox WebSocket Market Feed Connected (Subscribed: %d keys)", len(self.subscribed_keys))

    def _on_close(self, *args) -> None:
        self.is_connected = False
        log.warning("⚠️ Upstox WebSocket Market Feed Closed. Reconnect pending...")

    def _on_error(self, error, *args) -> None:
        log.warning("Upstox WebSocket Market Feed Error: %s", error)

    def _on_message(self, message: Any) -> None:
        try:
            # Handle dictionary parsed messages from protobuf decoder
            if isinstance(message, dict):
                feeds = message.get("feeds", {})
                for key, feed_data in feeds.items():
                    quote_dict: Dict[str, Any] = {}
                    # Parse full mode or ltpc mode
                    if "ff" in feed_data:  # Full feed
                        ff = feed_data["ff"]
                        market_ff = ff.get("marketFF", {})
                        ltpc = market_ff.get("ltpc", {})
                        quote_dict["ltp"] = ltpc.get("ltp")
                        quote_dict["ltt"] = ltpc.get("ltt")
                        quote_dict["close"] = ltpc.get("cp")
                        quote_dict["open"] = market_ff.get("marketOHLC", {}).get("ohlc", [{}])[0].get("open")
                        quote_dict["high"] = market_ff.get("marketOHLC", {}).get("ohlc", [{}])[0].get("high")
                        quote_dict["low"] = market_ff.get("marketOHLC", {}).get("ohlc", [{}])[0].get("low")
                        quote_dict["volume"] = market_ff.get("vtt")
                        quote_dict["bids"] = market_ff.get("marketLevel", {}).get("bidAskQuote", [])
                    elif "ltpc" in feed_data:
                        ltpc = feed_data["ltpc"]
                        quote_dict["ltp"] = ltpc.get("ltp")
                        quote_dict["ltt"] = ltpc.get("ltt")
                        quote_dict["close"] = ltpc.get("cp")

                    if quote_dict:
                        self.cache.update(key, quote_dict)
        except Exception as e:
            log.debug("Error parsing WebSocket feed message: %s", e)

    def start(self) -> None:
        """Start streaming market data in a background daemon thread."""
        if not self._streamer:
            return

        def _runner():
            try:
                self._streamer.connect()
            except Exception as e:
                log.warning("WebSocket streamer loop terminated: %s", e)
                self.is_connected = False

        self._thread = threading.Thread(target=_runner, name="UpstoxFeedStreamerThread", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Disconnect and stop streaming."""
        self._stop_event.set()
        if self._streamer:
            try:
                self._streamer.disconnect()
            except Exception:
                pass
        self.is_connected = False

    def subscribe(self, keys: List[str]) -> None:
        """Add new instrument keys to subscription."""
        new_keys = [k for k in keys if k and k not in self.subscribed_keys]
        if not new_keys:
            return
        self.subscribed_keys.update(new_keys)
        if self._streamer and self.is_connected:
            try:
                self._streamer.subscribe(new_keys, self.mode)
            except Exception as e:
                log.warning("Failed to subscribe keys to WebSocket streamer: %s", e)

    def get_ltp(self, instrument_key: str, max_age_sec: float = 30.0) -> Optional[float]:
        """Returns cached LTP if available and fresh, else None."""
        return self.cache.get_ltp(instrument_key, max_age_sec=max_age_sec)

    def get_quote(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        """Returns full cached quote including depth if available."""
        return self.cache.get_quote(instrument_key)
