"""
core/base_engine.py — Unified Base Execution Engine for Quant Trading Framework.

Encapsulates:
  - Multi-market session timeframes (MCX Commodities, NSE Currency, NSE Equity, Index Futures)
  - Opening Range Breakout (ORB) and session phase gating
  - Asymmetric Long/Short entry thresholds and mean-reversion triggers
  - Dynamic ADX-scaled Trailing Stops, Breakeven Locks, Timeouts, and EOD Square-offs
  - Drawdown-scaled risk sizing, Portfolio Heat Caps, and Market-specific Cooldowns
  - Sector concentration and cross-asset correlation gating via SectorCorrelationGate
  - Real-time WebSocket feed integration with graceful REST fallback
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from broker.feed_streamer import UpstoxFeedStreamer
from broker.instruments import build_mcx_commodity_map, build_currency_map, get_instrument_key
from broker.upstox_broker import UpstoxBroker
from config import UpstoxConfig
from database import TradingDB
from markets.commodity.costs import compute_mcx_commodity_costs, COMMODITY_SPECS
from markets.currency.costs import compute_ncd_currency_costs, CURRENCY_SPECS
from markets.commodity.scalping.entry_signal import compute_entry_signal, is_currency as _is_currency
from markets.equity.costs import compute_nse_equity_costs
from markets.equity.scalping.entry_signal import (
    compute_equity_entry_signal, is_equity as _is_equity,
    MAX_CONCURRENT_EQUITY_POSITIONS, BE_LOCK_BUFFER_PCT as EQUITY_BE_LOCK_BUFFER_PCT,
)
from markets.equity.features import compute_equity_features
from markets.equity.universe import NIFTY50_SYMBOLS
from core.sector_correlation import SectorCorrelationGate
from utils.logger import get_logger
from utils.market_holidays import get_trading_holidays

log = get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Overrides per symbol
SYMBOL_RISK_PCT_OVERRIDE = {"SILVER": 5.0, "CRUDEOILM": 3.0}
SYMBOL_LEVERAGE_OVERRIDE = {"GBPINR": 3.5}


def get_market_segment(sym: str) -> str:
    """Classify a trading symbol into its asset class: 'equity', 'currency', or 'commodity'."""
    if _is_equity(sym):
        return "equity"
    elif _is_currency(sym):
        return "currency"
    return "commodity"


def build_unified_symbol_map() -> Dict[str, str]:
    """Resolves current nearest-expiry instrument keys for all active symbols."""
    equity_map = {}
    for sym in NIFTY50_SYMBOLS:
        key = get_instrument_key(sym)
        if key:
            equity_map[sym] = key
    try:
        return {**build_mcx_commodity_map(), **build_currency_map(), **equity_map}
    except Exception as e:
        log.warning("Symbol map build partial failure: %s", e)
        return equity_map


class BaseTradingEngine:
    """
    Abstract Trading Engine containing shared market session management,
    signal evaluation, exit state machines, risk management, and telemetry.
    """

    def __init__(
        self,
        broker: UpstoxBroker,
        db: TradingDB,
        account_id: str = "DRYRUN_ACCOUNT",
        capital: float = 100000.0,
        risk_pct: float = 5.0,
        leverage: float = 4.0,
        direction: str = "both",
        is_dry_run: bool = True,
        use_crude_regime_filter: bool = True,
        enable_streaming: bool = True,
    ) -> None:
        self.broker = broker
        self.db = db
        self.account_id = account_id
        self.capital = capital
        self.risk_pct = risk_pct
        self.leverage = leverage
        self.direction = direction.lower()
        self.is_dry_run = is_dry_run
        self.use_crude_regime_filter = use_crude_regime_filter

        # Sector & Risk Management
        self.sector_gate = SectorCorrelationGate(max_per_sector=1)
        self.market_cooldowns: Dict[str, datetime] = {}
        self.symbol_cooldowns: Dict[str, datetime] = {}

        # Symbol Mapping
        self.symbol_map = build_unified_symbol_map()

        # WebSocket Streaming
        self.feed_streamer: Optional[UpstoxFeedStreamer] = None
        if enable_streaming and hasattr(broker, "_configuration") and broker._configuration.access_token:
            try:
                active_keys = list(self.symbol_map.values())
                self.feed_streamer = UpstoxFeedStreamer(
                    access_token=broker._configuration.access_token,
                    initial_keys=active_keys,
                    mode="full",
                )
                self.feed_streamer.start()
                log.info("🚀 WebSocket Market Data Feeder initialized with %d symbols", len(active_keys))
            except Exception as e:
                log.warning("⚠️ WebSocket streamer initialization failed, falling back to REST: %s", e)
                self.feed_streamer = None

    def stop(self) -> None:
        """Gracefully stop background streamer and connections."""
        if self.feed_streamer:
            self.feed_streamer.stop()

    # -------------------------------------------------------------------------
    # Market Session Time Gates
    # -------------------------------------------------------------------------
    @staticmethod
    def in_commodity_session(now: datetime) -> bool:
        """MCX session: 09:00 - 23:30 IST."""
        t = now.time()
        return (t.hour >= 9) and ((t.hour < 23) or (t.hour == 23 and t.minute <= 30))

    @staticmethod
    def in_currency_session(now: datetime) -> bool:
        """NSE currency session: 09:00 - 17:00 IST (entries gated 09:15 - 16:40)."""
        t = now.time()
        return (t.hour > 9 or (t.hour == 9 and t.minute >= 15)) and (t.hour < 16 or (t.hour == 16 and t.minute <= 40))

    @staticmethod
    def in_equity_session(now: datetime) -> bool:
        """NSE equity session: 09:15 - 15:30 IST (entries gated 09:30 - 15:15)."""
        t = now.time()
        return (t.hour > 9 or (t.hour == 9 and t.minute >= 30)) and (t.hour < 15 or (t.hour == 15 and t.minute <= 15))

    @staticmethod
    def is_equity_squareoff_time(now: datetime) -> bool:
        t = now.time()
        return (t.hour == 15 and t.minute >= 15) or t.hour > 15

    @staticmethod
    def is_currency_squareoff_time(now: datetime) -> bool:
        t = now.time()
        return (t.hour == 16 and t.minute >= 50) or t.hour > 16

    @staticmethod
    def is_mcx_squareoff_time(now: datetime) -> bool:
        t = now.time()
        return (t.hour == 23 and t.minute >= 15) or (t.hour >= 23 and t.minute >= 25)

    # -------------------------------------------------------------------------
    # Risk Management & Sizing
    # -------------------------------------------------------------------------
    def get_drawdown_scaled_risk_pct(self, base_risk_pct: float) -> float:
        """Scales risk down dynamically during peak drawdowns."""
        acc = self.db.get_account(self.account_id)
        if not acc:
            return base_risk_pct
        initial = float(acc["initial_capital"])
        current = float(acc["current_capital"])
        if initial <= 0:
            return base_risk_pct
        dd_pct = max(0.0, (initial - current) / initial * 100.0)
        if dd_pct >= 15.0:
            return base_risk_pct * 0.4
        elif dd_pct >= 10.0:
            return base_risk_pct * 0.6
        elif dd_pct >= 5.0:
            return base_risk_pct * 0.8
        return base_risk_pct

    def get_portfolio_heat_pct(self, open_positions: List[Dict]) -> float:
        """Computes aggregate risk heat across all currently open positions."""
        total_risk = 0.0
        for pos in open_positions:
            sym = pos.get("symbol", "")
            risk = SYMBOL_RISK_PCT_OVERRIDE.get(sym, self.risk_pct)
            total_risk += risk
        return total_risk

    def is_market_in_cooldown(self, market: str, now: datetime) -> bool:
        cd_end = self.market_cooldowns.get(market)
        if cd_end and now < cd_end:
            return True
        return False

    def trigger_market_cooldown(self, market: str, now: datetime, duration_minutes: int = 60) -> None:
        self.market_cooldowns[market] = now + timedelta(minutes=duration_minutes)
        log.warning("⏸️ %s Market in Cooldown until %s", market.upper(), self.market_cooldowns[market].strftime("%H:%M:%S"))

    # -------------------------------------------------------------------------
    # Real-Time LTP Fetcher (Streamer with REST Fallback)
    # -------------------------------------------------------------------------
    def get_latest_ltp(self, symbol: str, instrument_key: Optional[str] = None) -> Optional[float]:
        """Fetches LTP from streaming WebSocket cache if fresh, falling back to REST."""
        key = instrument_key or self.symbol_map.get(symbol)
        if key and self.feed_streamer:
            ltp = self.feed_streamer.get_ltp(key, max_age_sec=20.0)
            if ltp and ltp > 0:
                return ltp

        # REST fallback
        try:
            if key:
                quote = self.broker.get_ltp(key)
                if quote and quote.get("last_price"):
                    return float(quote["last_price"])
        except Exception as e:
            log.debug("REST LTP fallback failed for %s: %s", symbol, e)
        return None
