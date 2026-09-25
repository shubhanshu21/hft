#!/usr/bin/env python3
"""
live_dryrun.py — Paper-trading dry run using LIVE Upstox 5-minute candles & SQLite DB.

Runs 24/7 (MCX 09:00-23:30 IST, NSE currency 09:00-17:00 IST -- both handled
internally) and fires the EXACT SAME strategy logic as markets/commodity/scalping/backtest.py
/ markets/currency/scalping/backtest.py, but:
  - Places ZERO real orders in live broker (dry_run=True on Upstox broker)
  - Places & tracks VIRTUAL ORDERS in SQLite database (orders, positions, trades, snapshots)
  - Itemizes every fee (Brokerage, STT, Stamp Duty, Exchange Txn, SEBI, GST, Slippage)
  - Updates account capital and portfolio equity curves in real time
  - Saves both SQLite records and daily CSV logs

Usage:
    # Run paper trading during market hours
    python3 -m engine.live_dryrun

    # View current DB PnL, Capital, Open Positions & Order History
    python3 -m engine.live_dryrun --report

    # Reset paper trading DB with custom capital
    python3 -m engine.live_dryrun --reset-db --capital 200000

    # Custom settings
    python3 -m engine.live_dryrun --capital 100000 --risk-pct 5.0 --leverage 4.0
"""
from __future__ import annotations

from core.paths import BACKEND_ROOT, CACHE_DIR, DB_DIR, LOG_DIR
import os
import sys
from pathlib import Path

# Auto-bootstrap virtual environment if running outside .venv
_venv_py = Path(__file__).parent / ".venv" / "bin" / "python3"
if _venv_py.exists() and sys.executable != str(_venv_py):
    os.execv(str(_venv_py), [str(_venv_py)] + sys.argv)

import argparse
import csv
import fcntl
import json
import time
from datetime import date, datetime, timedelta

from zoneinfo import ZoneInfo

sys.path.insert(0, str(BACKEND_ROOT))

import pandas as pd

from services.broker.feed_streamer import UpstoxFeedStreamer
from services.broker.upstox_broker import UpstoxBroker, token_invalid_event
from engine.database import TradingDB
from markets.commodity.costs import COMMODITY_SPECS
from markets.currency.costs import CURRENCY_SPECS
from markets.commodity.scalping.entry_signal import is_currency as _is_currency
from markets.equity.scalping.entry_signal import is_equity as _is_equity
from core import registry, sessions
from core.risk import RiskGates
from core.strategy import EntryContext, ExitContext
from markets.equity.universe import NIFTY50_SYMBOLS
from core.sector_correlation import SectorCorrelationGate

# Per-symbol risk-per-trade override (falls back to --risk-pct/DRYRUN_RISK_PCT
# for anything not listed). SILVER re-added 2026-09-18 at half the account
# default: its recalibrated thresholds passed a real train/test OOS split
# but only on a single split with a meaningfully higher test-window drawdown
# (35.1%) than gold's (~7%) -- sized down until it has real live experience
# behind it, not treated as equally trusted as gold/crude yet.
# CRUDEOILM cut to 3.0 on 2026-09-19: the full ~4-month real archive (not
# just the recent favorable window) showed the deployed thresholds are a net
# loser overall -- at the account-default 10% risk-per-trade with the regime
# filter OFF (both matching the config that was actually live), the backtest
# wipes the account out completely (-100.03%, max DD -100%). Turning
# use_crude_regime_filter on (see below) improves every metric at once (win
# rate 46.1%->56.5%, PF 0.79->1.17) but is still net -16.17% at 10% risk --
# the position sizing itself, not just the entry filter, was too aggressive
# for how much this symbol's edge swings by regime. risk-pct=3.0 with the
# filter on was the best risk/DD tradeoff found: net +17.20%, PF 1.31, max DD
# -37.96% (vs. +21.58%/-55.80% at 5%, +9.91%/-70.10% at 7%) -- see
# markets/commodity/scalping/backtest.py --use-crude-regime-filter sweep in conversation
# history for the full risk-pct grid. Still not equally trusted as gold.
# GBPINR intentionally NOT listed here (see _SYMBOL_LEVERAGE_OVERRIDE below
# instead) -- a genuine train/test split couldn't be run on it (its full
# ~28-day archive is only 17 trades, so splitting leaves 10/7, both below the
# 15-trade credibility bar), so it needed the same "under-proven, size it
# down" treatment as SILVER's single-fold case. But confirmed 2026-09-19 via
# the real backtest engine (not just a formula read) that a risk-pct override
# is a complete no-op for it: identical trades/net/DD at risk_pct=2 through
# 20 (size_currency_lots' margin-derived cap, not the risk-derived one, binds
# for every single one of its 17 trades at this account's capital/leverage --
# unlike CRUDEOILM/SILVER above, confirmed by the SAME method to move real
# lot counts here). A risk_pct entry was tried first and silently did
# nothing; caught only because CRUDEOILM's risk_pct fix was re-verified the
# same way and produced genuinely different backtest numbers, which GBPINR's
# didn't. Leverage is what actually binds GBPINR's size -- see below.
_SYMBOL_RISK_PCT_OVERRIDE = {"SILVER": 5.0, "CRUDEOILM": 3.0}

# Per-symbol leverage override (falls back to --leverage/DRYRUN_LEVERAGE for
# anything not listed). Added 2026-09-19 specifically because
# _SYMBOL_RISK_PCT_OVERRIDE has no effect on GBPINR (see comment above) --
# margin sizing there is driven by capital/entry_price/leverage, not
# risk_pct, so leverage is the only lever that actually shrinks its lot
# count. 3.5 (half the account default of 7.0) cuts its real backtested lot
# count from 6 to 3 at this account's capital -- confirmed via
# markets.currency.costs.size_currency_lots directly, not assumed.
_SYMBOL_LEVERAGE_OVERRIDE = {"GBPINR": 3.5}


def _get_market(sym: str) -> str:
    """Classify a trading symbol into its asset class: 'equity', 'currency', or 'commodity'."""
    if _is_equity(sym):
        return "equity"
    elif _is_currency(sym):
        return "currency"
    return "commodity"


def _is_market_session_open(sym: str, now: datetime) -> bool:
    return sessions.is_open(_get_market(sym), now)


from services.broker.instruments import build_mcx_commodity_map, build_currency_map, get_instrument_key
from services.utils.logger import get_logger, setup_logger
from services.utils import telegram
from services.utils.market_holidays import get_trading_holidays

log = get_logger("live_dryrun")

from engine import blocked_log, heartbeat, live_prices, margin_rates
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_SCAN_INTERVAL_SECONDS = 60  # 1-minute scan for fast SL/TP trailing & new bar detection
DEFAULT_RISK_PCT = 5.0
DEFAULT_LEVERAGE = 4.0

def _build_symbol_map() -> dict[str, str]:
    """Resolves the current nearest-expiry instrument_key for every MCX
    commodity + NSE currency base symbol. MCX/currency contracts are
    monthly-expiry (see markets/commodity/data.py/real_currency_data.py
    docstrings) -- calling this again after a contract has rolled picks up
    the new one automatically, since build_mcx_commodity_map()/
    build_currency_map() always select whichever expiry is nearest >=
    today at call time. Must be re-called periodically (see main()'s
    day-rollover loop) -- a value cached once at process startup would
    silently keep resolving an expired instrument_key for as long as the
    process runs without restarting."""
    equity_map = {}
    for sym in NIFTY50_SYMBOLS:
        key = get_instrument_key(sym)
        if key:
            equity_map[sym] = key
    try:
        return {**build_mcx_commodity_map(), **build_currency_map(), **equity_map}
    except Exception as _e:
        log.warning("Could not load Upstox master; falling back to hardcoded keys: %s", _e)
        return {
            **build_mcx_commodity_map(),
            **build_currency_map(),
            **equity_map,
        }


SYMBOL_MAP: dict[str, str] = _build_symbol_map()

# Terminal colours
R = "\033[0m"; BOLD = "\033[1m"
GR = "\033[92m"; RED = "\033[91m"; YL = "\033[93m"
CY = "\033[96m"; WH = "\033[97m"; MG = "\033[95m"; GY = "\033[90m"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _acquire_process_lock(account_id: str):
    """
    Prevents two dryrun/live processes from trading the same account at once
    (e.g. a stray nohup'd process someone forgot about, or a re-run before
    the old one exited) -- with real orders that could double-size or
    double-enter positions. Holds the lock (via an inherited open fd) for
    the life of the process; released automatically on exit/crash.
    """
    lock_dir = DB_DIR
    lock_dir.mkdir(exist_ok=True)
    lock_path = lock_dir / f".{account_id}.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"{RED}ERROR: Another dryrun process is already running for account "
              f"'{account_id}' (lock: {lock_path}). Refusing to start a second one.{R}")
        telegram.send(f"🔴 <b>STARTUP REFUSED</b> — another dryrun process is already running for "
                      f"account '{account_id}'. This attempt was refused to prevent double-trading.")
        sys.exit(1)
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh  # caller must keep this referenced so the fd (and lock) stays alive


def _load_token() -> str:
    # 1. Check if token already loaded in UpstoxConfig
    try:
        from engine.config import UpstoxConfig
        if UpstoxConfig.ACCESS_TOKEN:
            return UpstoxConfig.ACCESS_TOKEN
    except Exception:
        pass

    # 2. Check var/cache/upstox_token.json
    cache_path = CACHE_DIR / "upstox_token.json"
    if cache_path.exists():
        try:
            import json
            data = json.loads(cache_path.read_text())
            token = data.get("access_token", "").strip()
            if token:
                return token
        except Exception:
            pass

    # 3. Check .env file
    env = BACKEND_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("ACCESS_TOKEN=") and not line.startswith("#"):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if token:
                    return token

    # 4. Check OS environment variable
    if os.environ.get("ACCESS_TOKEN"):
        return os.environ.get("ACCESS_TOKEN", "").strip()

    # 5. Try auto-login if configured
    try:
        from services.auth.upstox_auto_login import ensure_fresh_upstox_token
        token = ensure_fresh_upstox_token()
        if token:
            return token
    except Exception as e:
        log.warning(f"Auto-login attempt failed: {e}")

    return ""


def _fetch_candles(broker: UpstoxBroker, symbol: str, today: str) -> list[dict]:
    ikey = SYMBOL_MAP.get(symbol)
    if not ikey:
        return []
    raw = broker.get_intraday_candles(ikey, unit="minutes", interval=5)
    if not raw:
        raw = broker.get_historical_candles(ikey, unit="minutes", interval=5, to_date=today)
    if not raw:
        return []
    # Upstox returns most-recent-first; reverse to chronological
    return list(reversed(raw))


def _fetch_intraday(broker: UpstoxBroker, symbol: str, unit: str, interval: int, today: str) -> list[dict]:
    """Today's candles for any intraday timeframe (the 5-minute case is _fetch_candles)."""
    ikey = SYMBOL_MAP.get(symbol)
    if not ikey:
        return []
    raw = broker.get_intraday_candles(ikey, unit=unit, interval=interval)
    if not raw:
        raw = broker.get_historical_candles(ikey, unit=unit, interval=interval, to_date=today)
    return list(reversed(raw)) if raw else []


def _fetch_history(broker: UpstoxBroker, symbol: str, unit: str, interval: int, lookback_days: int) -> list[dict]:
    """`lookback_days` of history for daily / multi-day strategies, chronological. The newest bar
    can be today's still-forming one; a strategy that needs completed bars should drop it."""
    ikey = SYMBOL_MAP.get(symbol)
    if not ikey:
        return []
    today = date.today()
    raw = broker.get_historical_candles(ikey, unit=unit, interval=interval, to_date=today.isoformat(),
                                        from_date=(today - timedelta(days=lookback_days)).isoformat())
    return sorted(raw, key=lambda c: c["timestamp"]) if raw else []


# ---------------------------------------------------------------------------
# Core Paper Trading Runner
# ---------------------------------------------------------------------------

class DryRunner(RiskGates):
    def __init__(self, broker: UpstoxBroker, db: TradingDB, symbols: list[str],
                 capital: float, risk_pct: float, leverage: float,
                 account_id: str = "DRYRUN_ACCOUNT",
                 direction_filter: str = "both"):
        self.broker           = broker
        self.db               = db
        self.symbols          = symbols
        self.capital          = capital
        self.risk_pct         = risk_pct
        self.leverage         = leverage
        self.account_id       = account_id
        self.direction_filter = direction_filter.lower()

        log.info("Rule-based signal engine initialized across %d symbols.", len(self.symbols))

        # Wire the spread log path into the adaptive slippage module so it reads
        # the same file this process writes.
        from core.slippage import set_spread_log_path
        set_spread_log_path(LOG_DIR / "spread_samples.csv")

        # Sync account in SQLite
        self.db.init_account(account_id=self.account_id, capital=self.capital,
                             leverage=self.leverage, risk_pct=self.risk_pct)
        acct = self.db.get_account(self.account_id)
        if acct:
            self.capital = acct["current_capital"]


        # Strategies switched on per market (COMMODITY_/CURRENCY_/EQUITY_STRATEGIES in .env).
        self.strategies = {m: registry.active(m) for m in registry.MARKETS}
        log.info("Strategies: %s", {m: [x.name for x in v] for m, v in self.strategies.items()})

        # Restore open positions from DB if any. Each is handed back to the strategy that opened it
        # (rows saved before strategies existed read as that market's "scalping").
        self.positions: dict[str, dict] = {}
        for p in self.db.get_open_positions(self.account_id):
            strat_name = p.get("strategy") or "scalping"
            pos = {
                "position_id": p["position_id"],
                "symbol": p["symbol"],
                "strategy": strat_name,
                "direction": p["direction"],
                "entry_price": p["entry_price"],
                "qty": p["qty"],
                "sl": p["current_stop"],
                "current_stop": p["current_stop"],
                "best_price": p["best_price"],
                "entry_time": datetime.fromisoformat(p["entry_time"]) if "T" in p["entry_time"] else datetime.now(IST),
                "stop_dist": abs(p["entry_price"] - p["current_stop"]),
                "trade_value": p["qty"] * p["entry_price"],
                "rsi": 50.0, "vwap_dist_pct": 0.0, "ema_slope_pct": 0.0,
            }
            strat = registry.get(_get_market(p["symbol"]), strat_name)
            pos.update(strat.restore(p))
            if p.get("state"):
                pos.update(json.loads(p["state"]))     # exact state the runner saved at entry wins over legacy fallbacks
            self.positions[p["symbol"]] = pos

        self.trades: list[dict] = []
        self.today = date.today().isoformat()

        # ---- Money management, added 2026-09-22 -----------------------------
        # Per-trade risk sizing and the daily-loss kill switch already existed,
        # but neither is what professional/prop-desk risk management actually
        # means -- researched real conventions (portfolio heat caps, drawdown-
        # scaled position sizing) rather than guessing, see the two methods
        # below for the sourced numbers. Both apply on top of (not instead of)
        # the existing per-trade sizing and kill switches.
        # Trailing high-water mark for drawdown-scaled sizing below. Derived
        # from portfolio_snapshots (MAX(current_capital) ever recorded) rather
        # than a new DB column -- must survive process restarts, or a restart
        # right after a loss would reset the peak to the now-lower capital
        # and silently erase the drawdown this whole feature exists to react
        # to. Falls back to current capital if no snapshot history exists yet.
        self.peak_capital = self.capital
        try:
            _historical_peak = self.db.get_peak_capital(self.account_id)
            if _historical_peak is not None:
                self.peak_capital = max(self.peak_capital, _historical_peak)
        except Exception as exc:
            log.warning("Could not load historical peak capital from snapshots (starting from current capital): %s", exc)
        # Portfolio heat cap: total open risk (sum of stop-distance-based Rs
        # risk across EVERY open position, commodity+currency+equity combined)
        # must never exceed this % of capital. Researched convention: swing
        # traders commonly cap at 4-8%, short-term scalpers with tight stops
        # up to ~10% (Alexander Elder's widely-cited 6% rule is the canonical
        # reference point). 8% chosen as the middle of that range -- this
        # system runs a selective, high-conviction cadence (not ultra-HFT), so
        # scalpers' 10% ceiling felt too loose but conservative swing-traders'
        # 4% felt tighter than the existing per-trade sizing already implies.
        self.max_portfolio_heat_pct = float(os.environ.get("MAX_PORTFOLIO_HEAT_PCT", "8.0"))

        # Shared margin-pool caps (global + per market) -- see core/risk.py.
        self._init_margin_limits()

        # Full-day vs evening-only trading window -- see markets/commodity/scalping/backtest.py's
        # us_session_only for the matching backtest flag/comparison. Switched
        # to full-session by default 2026-09-17 per user decision: real
        # backtest on the full 2022-2026 archive showed full session more
        # than doubles net PnL (+Rs68,097 vs +Rs30,248) at the cost of a
        # lower win rate (65.8% vs 70.6%) and higher max DD (8.51% vs 6.51%)
        # from 3x more (noisier, more expensive) daytime trades. Toggle via
        # DRYRUN_FULL_SESSION in .env -- keep BACKTEST_FULL_SESSION in sync
        # so backtest and live dryrun match.
        self.full_session = os.environ.get("DRYRUN_FULL_SESSION", "true").lower() in ("1", "true", "yes")

        # Daily-loss kill switch: halts new entries (existing positions are
        # still managed/exited normally) once today's realized loss hits
        # MAX_DAILY_LOSS_PCT of the capital this process started the day with.
        self.max_daily_loss_pct = float(os.environ.get("MAX_DAILY_LOSS_PCT", "5.0"))
        self.trading_day = self.today
        self.day_start_capital = self.capital
        self.kill_switch_active = False

        # Per-symbol daily-loss kill switch, same MAX_DAILY_LOSS_PCT threshold
        # but tracked per symbol against that symbol's own realized PnL today --
        # added 2026-09-18 so one symbol having a genuinely bad day (e.g. crude
        # hitting a bad regime, see markets/commodity/scalping/backtest.py's ENTRY_THRESHOLDS
        # comment) doesn't halt entries account-wide for symbols that are fine.
        # The existing account-wide switch above still exists as the final
        # backstop for a bad day across the whole book.
        self.symbol_daily_pnl: dict[str, float] = {s: 0.0 for s in self.symbols}
        self.symbol_kill_switch: dict[str, bool] = {s: False for s in self.symbols}

        # Market-specific daily loss limits & cooldown timers (added 2026-09-22):
        # Instead of shutting down all trading for the day across decoupled markets,
        # each market (commodity, currency, equity) tracks its own daily loss.
        # When a market crosses MAX_MARKET_DAILY_LOSS_PCT, that specific market
        # enters a MARKET_COOLDOWN_MINUTES pause, while other markets continue trading.
        self.max_market_daily_loss_pct = float(os.environ.get("MAX_MARKET_DAILY_LOSS_PCT", "3.0"))
        self.market_cooldown_minutes = int(os.environ.get("MARKET_COOLDOWN_MINUTES", "60"))
        self.market_daily_pnl: dict[str, float] = {"commodity": 0.0, "currency": 0.0, "equity": 0.0}
        self.market_cooldown_until: dict[str, datetime | None] = {"commodity": None, "currency": None, "equity": None}

        # Commodity regime gate — extended 2026-09-22 from CRUDEOILM-only to
        # all MCX momentum symbols (CRUDEOILM, GOLDM, SILVER, NATGASMINI).
        # Reads USE_COMMODITY_REGIME_FILTER; falls back to USE_CRUDE_REGIME_FILTER
        # for backward-compatibility with any existing .env that hasn't been updated.
        self.use_commodity_regime_filter = os.environ.get(
            "USE_COMMODITY_REGIME_FILTER",
            os.environ.get("USE_CRUDE_REGIME_FILTER", "false")
        ).lower() in ("1", "true", "yes")
        # Per-symbol dict: True = OK to enter, False = regime blocked, None = no data yet.
        # None is treated as fail-open (let trades through) -- a broker hiccup
        # should never silently block every symbol for the day.
        self.commodity_regime_ok: dict[str, bool | None] = {}
        if self.use_commodity_regime_filter:
            self._refresh_commodity_regimes()

        # NSE equity index-level regime gate (added 2026-09-22). Gates ALL new
        # equity entries using NIFTY50's own daily-return autocorrelation.
        # Off by default -- opt in via USE_EQUITY_REGIME_FILTER in .env.
        self.use_equity_regime_filter = os.environ.get("USE_EQUITY_REGIME_FILTER", "false").lower() in ("1", "true", "yes")
        self.equity_regime_ok: bool | None = True
        if self.use_equity_regime_filter:
            self._refresh_equity_regime()

        # Market Segment Trading Toggles (added 2026-09-22)
        self.segment_enabled: dict[str, bool] = {
            "commodity": os.environ.get("ENABLE_COMMODITY_TRADING", "true").lower() in ("1", "true", "yes"),
            "currency": os.environ.get("ENABLE_CURRENCY_TRADING", "true").lower() in ("1", "true", "yes"),
            "equity": os.environ.get("ENABLE_EQUITY_TRADING", os.environ.get("DRYRUN_INCLUDE_EQUITY", "true")).lower() in ("1", "true", "yes"),
        }

        # Sector & Correlation Risk Gate
        self.sector_gate = SectorCorrelationGate(
            max_per_sector=int(os.environ.get("MAX_POSITIONS_PER_SECTOR", "1"))
        )


        # Real bid-ask spread sampling (added 2026-09-18): every cost model in
        # this project assumes a flat half-tick-per-leg slippage guess, never
        # measured against a real order book. A one-off check found real
        # spreads running 2x-29x wider than that assumption across every live
        # symbol at that moment (CRUDEOILM 4x, GOLDM 25x, USDINR 3x, EURINR 2x,
        # GBPINR 29x) -- consistent enough across symbols to be a real signal,
        # not a fluke, but one snapshot isn't a distribution. This logs a real
        # sample (throttled to once per SPREAD_SAMPLE_INTERVAL_MIN per symbol,
        # not every scan, to stay well under API rate limits) to
        # var/logs/spread_samples.csv so a genuine empirical slippage model can
        # eventually replace the flat guess -- accumulates automatically as
        # this daemon runs, no separate job needed.
        self.spread_sample_interval_min = float(os.environ.get("SPREAD_SAMPLE_INTERVAL_MIN", "5"))
        self._last_spread_sample: dict[str, datetime] = {}
        self._stale_logged: dict[str, datetime] = {}      # newest candle time already reported as stale, per symbol
        self.spread_log_path = LOG_DIR / "spread_samples.csv"

        # Midday summary flag -- initialized here so scan() can read it without
        # the getattr workaround (which was masking the missing init).
        self._midday_summary_sent = False

        # Manual Telegram kill switch -- separate from the automatic daily-loss
        # one above. Loaded from disk so a "stop" sent before a restart is
        # still honored after it.
        self.trading_enabled = self._load_trading_enabled()
        logs_dir = LOG_DIR
        logs_dir.mkdir(exist_ok=True)
        self.log_path = logs_dir / f"dryrun_{self.today}.csv"

    def _get_segment_risk_and_leverage(self, sym: str) -> tuple[float, float]:
        market = _get_market(sym)
        if market == "commodity":
            r = float(os.environ.get("COMMODITY_RISK_PCT", self.risk_pct))
            l = float(os.environ.get("COMMODITY_LEVERAGE", self.leverage))
        elif market == "currency":
            r = float(os.environ.get("CURRENCY_RISK_PCT", self.risk_pct))
            l = float(os.environ.get("CURRENCY_LEVERAGE", self.leverage))
        else:
            r = float(os.environ.get("EQUITY_RISK_PCT", self.risk_pct))
            l = float(os.environ.get("EQUITY_LEVERAGE", self.leverage))

        # Per-symbol values: an env var <SYMBOL>_RISK_PCT / <SYMBOL>_LEVERAGE wins, else the coded override above, else the segment value.
        r = float(os.environ.get(f"{sym.upper()}_RISK_PCT", _SYMBOL_RISK_PCT_OVERRIDE.get(sym.upper(), r)))
        l = float(os.environ.get(f"{sym.upper()}_LEVERAGE", _SYMBOL_LEVERAGE_OVERRIDE.get(sym.upper(), l)))
        l = margin_rates.cap_leverage(sym, l)          # never more than Upstox really gives for this symbol (engine/margin_rates.py)
        return r, l

    # ---- Money management (see __init__'s comment) -----------------------
    def _portfolio_heat_pct(self) -> float:
        """Total open risk across every currently-open position (all asset
        classes combined), as a % of current capital -- if every stop hit at
        once, this is roughly how much of the account would be lost. Equity
        positions store share qty directly; commodity/currency store lot qty
        needing their own contract multiplier."""
        if self.capital <= 0:
            return 0.0
        total_risk_rupees = 0.0
        for sym, pos in self.positions.items():
            stop_dist = abs(pos["entry_price"] - pos.get("sl", pos["current_stop"]))
            if _is_equity(sym):
                multiplier = 1  # shares, no lot multiplier
            elif _is_currency(sym):
                multiplier = CURRENCY_SPECS.get(sym.upper(), {}).get("lot_size", 1000)
            else:
                multiplier = COMMODITY_SPECS.get(sym, {}).get("lot_size", 1)
            total_risk_rupees += stop_dist * pos["qty"] * multiplier
        return total_risk_rupees / self.capital * 100

    def _drawdown_risk_scale(self) -> float:
        """Anti-martingale position-size scaling: shrink new trades'
        risk-per-trade the further capital sits below its own trailing peak,
        restore automatically as capital recovers -- protects capital during
        a losing stretch without trying to predict/avoid any individual
        losing trade (already tried and found not to work for this system,
        see conversation history's cooldown/regime-filter tests). Researched,
        sourced tiered schedule (5%/10%/15% drawdown -> 10%/25%/50% size cut)
        rather than an invented one."""
        if self.peak_capital <= 0:
            return 1.0
        drawdown_pct = (self.peak_capital - self.capital) / self.peak_capital * 100
        if drawdown_pct >= 15.0:
            return 0.50
        if drawdown_pct >= 10.0:
            return 0.75
        if drawdown_pct >= 5.0:
            return 0.90
        return 1.0

    def _is_market_on_cooldown(self, market: str, now: datetime) -> bool:
        """Checks whether the given market ('commodity', 'currency', 'equity')
        is currently on a loss-triggered cooldown. Automatically clears expired cooldowns."""
        expiry = self.market_cooldown_until.get(market)
        if expiry is None:
            return False
        if now < expiry:
            return True
        # Cooldown expired!
        self.market_cooldown_until[market] = None
        log.info("%s cooldown expired. Resuming %s entry evaluation.", market.upper(), market)
        print(f"\n{BOLD}{GR}▶ {market.upper()} COOLDOWN EXPIRED -- {market} setups will now be evaluated.{R}\n")
        telegram.send(
            f"▶ <b>{market.upper()} COOLDOWN EXPIRED</b>\n"
            f"Cooldown has ended. New {market} setups will now be evaluated."
        )
        return False

    # ---- Commodity & Equity regime gates (see __init__'s comment) -------------
    def _refresh_commodity_regimes(self) -> None:
        """Fetches real daily candles through YESTERDAY for each MCX momentum
        symbol and recomputes self.commodity_regime_ok. Fails open per symbol
        (leaves previous value or None) if the fetch fails -- a broker hiccup
        should never silently block entries for the day.
        Extended 2026-09-22 from CRUDEOILM-only to cover GOLDM, SILVER,
        NATGASMINI, then REVERTED 2026-09-23 back to CRUDEOILM-only after
        train/test validation: the autocorrelation regime signal was derived
        from and only ever validated against CRUDEOILM's own two known
        regimes (see core/regime.py's docstring). Testing the extension
        found it clearly harmful for SILVER (TEST net Rs482k -> Rs68k, PF
        2.67 -> 1.71, trades 62 -> 17) and merely trade-count-reducing for
        GOLDM with no net benefit (TEST trades 39 -> 20, net roughly flat)
        -- i.e. it generalizes to neither. Only CRUDEOILM showed the actual
        regime-dependent failure mode this gate exists to catch.
        """
        from core.regime import regime_ok as _regime_ok
        mcx_symbols = [s for s in self.symbols if s.upper() == "CRUDEOILM"]
        yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
        for sym in mcx_symbols:
            ikey = SYMBOL_MAP.get(sym)
            if not ikey:
                continue
            try:
                candles = self.broker.get_historical_candles(ikey, unit="days", interval=1, to_date=yesterday)
            except Exception as exc:
                log.warning("Could not fetch daily candles for %s regime gate: %s", sym, exc)
                continue
            if not candles:
                continue
            closes = [float(c["close"]) for c in sorted(candles, key=lambda c: c["timestamp"])]
            result = _regime_ok(closes, window=15, min_autocorr=0.0)
            self.commodity_regime_ok[sym.upper()] = result
            if result is False:
                log.warning("%s regime gate: BLOCKED (daily-return autocorrelation < 0).", sym)
                telegram.send(
                    f"⚠️ <b>{sym} REGIME GATE: BLOCKED</b> — recent daily price action looks "
                    f"mean-reverting, not trending. New {sym} entries held back today (existing positions unaffected)."
                )
            elif result is True:
                log.info("%s regime gate: OK (autocorrelation >= 0).", sym)

    def _refresh_equity_regime(self) -> None:
        """Fetches NIFTY50 index daily candles through YESTERDAY and recomputes
        self.equity_regime_ok. Fails open (True) if the fetch fails.
        Added 2026-09-22 -- gates ALL new NSE equity entries."""
        from core.regime import regime_ok as _regime_ok
        nifty_key = "NSE_INDEX|Nifty 50"
        yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            candles = self.broker.get_historical_candles(nifty_key, unit="days", interval=1, to_date=yesterday)
        except Exception as exc:
            log.warning("Could not fetch NIFTY50 daily candles for equity regime gate: %s", exc)
            return
        if not candles:
            return
        closes = [float(c["close"]) for c in sorted(candles, key=lambda c: c["timestamp"])]
        self.equity_regime_ok = _regime_ok(closes, window=15, min_autocorr=0.0)
        if self.equity_regime_ok is False:
            log.warning("Equity regime gate: BLOCKED (NIFTY50 daily autocorrelation < 0).")
            telegram.send(
                "⚠️ <b>EQUITY REGIME GATE: BLOCKED</b> — NIFTY50 daily returns look "
                "mean-reverting. New NSE equity entries held back today (existing positions unaffected)."
            )
        elif self.equity_regime_ok is True:
            log.info("Equity regime gate: OK (NIFTY50 autocorrelation >= 0).")

    # ---- real spread sampling (see __init__'s comment) -----------------------
    def _maybe_sample_spread(self, sym: str, now: datetime) -> None:
        # Found 2026-09-23: with no session gate here, off-hours calls kept
        # hitting the broker's market-depth endpoint for closed markets
        # (currency after 17:00, equity after 15:30) and it returned the
        # same frozen last-known bid/ask over and over -- var/logs/spread_samples.csv
        # had dozens of byte-identical EURINR rows spanning 1.5+ hours
        # (17:04-18:44 IST). Those stale, non-executable "spreads" fed
        # directly into core/slippage.py's empirical median once
        # MIN_SAMPLES was crossed, inflating real intraday slippage cost
        # estimates for the affected symbols. Gate on the market actually
        # being open before sampling at all.
        if not _is_market_session_open(sym, now):
            return
        last = self._last_spread_sample.get(sym)
        if last is not None and (now - last).total_seconds() < self.spread_sample_interval_min * 60:
            return
        self._last_spread_sample[sym] = now
        ikey = SYMBOL_MAP.get(sym)
        if not ikey:
            return
        try:
            depth = self.broker.get_market_depth(ikey)
        except Exception:
            return
        if not depth:
            return
        bid = depth["buy"][0]["price"] if depth.get("buy") else 0.0
        ask = depth["sell"][0]["price"] if depth.get("sell") else 0.0
        if not bid or not ask or ask <= bid:
            return
        spread = ask - bid
        mid = (bid + ask) / 2
        spread_pct = spread / mid * 100 if mid else 0.0
        is_new = not self.spread_log_path.exists()
        with open(self.spread_log_path, "a", newline="") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["timestamp", "symbol", "bid", "ask", "spread", "mid", "spread_pct"])
            w.writerow([now.isoformat(), sym, bid, ask, round(spread, 4), round(mid, 4), round(spread_pct, 4)])

    def _send_midday_summary(self, now: datetime) -> None:
        self._midday_summary_sent = True
        open_syms = list(self.positions.keys())
        pnl = self.capital - self.day_start_capital
        pnl_pct = (pnl / self.day_start_capital * 100) if self.day_start_capital > 0 else 0.0
        mkt_pnl_lines = "\n".join([f"  • {m.capitalize()}: ₹{val:,.2f}" for m, val in self.market_daily_pnl.items()])
        msg = (
            f"📈 <b>MIDDAY P&L SUMMARY</b> ({now.strftime('%H:%M')} IST)\n"
            f"Capital: ₹{self.capital:,.2f} (Day P&L: ₹{pnl:+,.2f} / {pnl_pct:+.2f}%)\n"
            f"Market P&Ls:\n{mkt_pnl_lines}\n"
            f"Open Positions ({len(open_syms)}): {', '.join(open_syms) or 'none'}"
        )
        print(f"\n{BOLD}{GR}{msg}{R}\n", flush=True)
        telegram.send(msg)

    # ---- scan ---------------------------------------------------------------
    def scan(self) -> list[dict]:
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")
        if today_str != self.trading_day:
            self.trading_day = today_str
            self.day_start_capital = self.capital
            self.kill_switch_active = False
            self.symbol_daily_pnl = {s: 0.0 for s in self.symbols}
            self.symbol_kill_switch = {s: False for s in self.symbols}
            self.market_daily_pnl = {"commodity": 0.0, "currency": 0.0, "equity": 0.0}
            self.market_cooldown_until = {"commodity": None, "currency": None, "equity": None}
            if self.use_commodity_regime_filter:
                self._refresh_commodity_regimes()
            if self.use_equity_regime_filter:
                self._refresh_equity_regime()
            self._midday_summary_sent = False  # reset for new trading day (attr exists from __init__)

        if now.hour >= 12 and now.minute >= 30 and not self._midday_summary_sent:
            self._send_midday_summary(now)

        if self.day_start_capital > 0:
            daily_loss_pct = (self.day_start_capital - self.capital) / self.day_start_capital * 100
        else:
            daily_loss_pct = 0.0
        if not self.kill_switch_active and daily_loss_pct >= self.max_daily_loss_pct:
            self.kill_switch_active = True
            print(f"\n{BOLD}{RED}!! DAILY LOSS LIMIT HIT ({daily_loss_pct:.2f}% >= {self.max_daily_loss_pct}%) "
                  f"-- new entries halted for today. Open positions still managed. !!{R}\n")
            telegram.send(
                f"🛑 <b>DAILY LOSS LIMIT HIT</b> — {daily_loss_pct:.2f}% (limit {self.max_daily_loss_pct}%)\n"
                f"New entries halted for the rest of today. Existing open positions are still managed/exited normally."
            )

        signals = []
        for sym in self.symbols:
            self._maybe_sample_spread(sym, now)
            if sym in self.positions:
                self._maybe_exit(sym, now)
                continue
            if self.kill_switch_active or self.symbol_kill_switch.get(sym, False) or not self.trading_enabled:
                continue

            market = _get_market(sym)
            if not self.segment_enabled.get(market, True):
                continue
            if self._is_market_on_cooldown(market, now):
                continue

            for strat in self.strategies[market]:
                if self._try_enter(strat, sym, now, signals):
                    break                      # one position per symbol across all strategies
        return signals

    # ---- strategy-driven entry / exit -------------------------------------------
    # Strategies (core/strategy.py) only decide; every gate, sizing check, DB write and alert
    # below is shared, so a new strategy inherits all of it for free.
    def _gate_flags(self) -> dict:
        return {"equity_regime_ok": self.equity_regime_ok if self.use_equity_regime_filter else True}

    def _strategy_of(self, pos: dict, sym: str | None = None):
        return registry.get(_get_market(sym or pos["symbol"]), pos.get("strategy", "scalping"))

    def _candles(self, sym: str, strat) -> list[dict]:
        unit, interval = strat.timeframe
        if strat.lookback_days > 0:
            return _fetch_history(self.broker, sym, unit, interval, strat.lookback_days)
        if (unit, interval) == ("minutes", 5):
            return _fetch_candles(self.broker, sym, self.today)
        return _fetch_intraday(self.broker, sym, unit, interval, self.today)

    def _candles_fresh(self, sym: str, candles: list[dict], now: datetime, strat) -> bool:
        """Never trade on stale data: an intraday symbol whose newest candle is older than two bars + 3 min while its market is open is skipped
        (feed lag, an instrument key that stopped updating, a halted symbol). Daily-bar strategies are exempt."""
        unit, interval = strat.timeframe
        if unit != "minutes" or not candles or not _is_market_session_open(sym, now):        # after the close the newest candle is old by definition
            return True
        try:
            last = datetime.fromisoformat(str(candles[-1]["timestamp"]))
            if last.tzinfo is None:
                last = last.replace(tzinfo=IST)
            age_min = (now - last).total_seconds() / 60
        except (KeyError, ValueError, TypeError):
            return True
        limit = 2 * interval + 3
        if age_min > limit:
            if self._stale_logged.get(sym) != last:
                self._stale_logged[sym] = last
                log.warning("%s: newest %d-min candle is %.0f min old (limit %d) -- skipping entries until the feed catches up", sym, interval, age_min, limit)
            return False
        return True

    def _try_enter(self, strat, sym: str, now: datetime, signals: list[dict]) -> bool:
        """Evaluate `strat` for `sym`; open a (paper) position if it signals and every gate passes."""
        market = _get_market(sym)
        if strat.blocked(self._gate_flags()):
            return False
        if strat.max_positions is not None:
            open_n = sum(1 for p in self.positions.values()
                         if p.get("strategy", "scalping") == strat.name and _get_market(p["symbol"]) == market)
            if open_n >= strat.max_positions:
                return False
        if strat.sector_cap:
            can_enter_sec, reason_sec = self.sector_gate.can_enter(sym, list(self.positions.keys()))
            if not can_enter_sec:
                log.info("%s: %s entry skipped -- %s", sym, market, reason_sec)
                return False
        if not strat.due(now):
            return False

        base_risk_pct, sym_leverage = self._get_segment_risk_and_leverage(sym)
        override = os.environ.get(f"{market.upper()}_{strat.name.upper()}_RISK_PCT")
        if override:
            base_risk_pct = float(override)
        leverage = sym_leverage if strat.uses_leverage else 1.0
        regime_ok = self.commodity_regime_ok.get(sym.upper(), None) if self.use_commodity_regime_filter else True
        sizing_capital = self._sizing_capital(market)
        candles = self._candles(sym, strat)
        if not self._candles_fresh(sym, candles, now, strat):
            return False
        ctx = dict(symbol=sym, candles=candles, now=now, instrument_key=SYMBOL_MAP.get(sym),
                   risk_pct=base_risk_pct * self._drawdown_risk_scale(), leverage=leverage, direction_filter=self.direction_filter,
                   full_session=self.full_session, regime_ok=regime_ok, equity_regime_ok=self._gate_flags()["equity_regime_ok"])
        sig_result = strat.entry(EntryContext(capital=sizing_capital, **ctx)) if sizing_capital > 0 else None
        if not sig_result:
            # Margin is one pool. If part of it is held by open positions, a signal that could not be sized to the FREE margin leaves no trace
            # (zero lots -> no signal). Re-ask with the whole account: a signal there was blocked by margin, not absent (engine/blocked_log.py).
            if self.positions and sizing_capital < self.capital * 0.95:
                shadow = strat.entry(EntryContext(capital=self.capital, **ctx))
                if shadow:
                    blocked_log.record(sym, market, "margin_pool", f"free margin Rs{max(sizing_capital, 0):,.0f} of Rs{self.capital:,.0f}; a {shadow.direction} "
                                       f"signal for {shadow.qty} lot(s) could not be sized", list(self.positions.values()),
                                       bar_ts=str(candles[-1]["timestamp"]) if candles else None, direction=shadow.direction, wanted_qty=shadow.qty, got_qty=0)
                    log.info("%s: %s signal blocked by margin held by %s (free Rs%s of Rs%s).", sym, shadow.direction, ", ".join(self.positions),
                             f"{max(sizing_capital, 0):,.0f}", f"{self.capital:,.0f}")
            return False
        if sig_result.direction == "short" and not strat.allow_short:
            return False
        if candles:
            sig_result.exit_state["entry_bar_ts"] = str(candles[-1]["timestamp"])   # see core/exits._side

        rejection = self._entry_gate_rejection(sym, market, sig_result, leverage)
        if rejection:
            log.info("%s: %s entry skipped -- %s.", sym, market, rejection)
            return False

        # Ask Upstox what THIS exact order would block, right now (margins and lot sizes change at the broker's discretion).
        fit = margin_rates.confirm_order(self.broker, sym, SYMBOL_MAP.get(sym), sig_result.qty,
                                         "BUY" if sig_result.direction == "long" else "SELL", sizing_capital, now=None)
        if fit["qty"] < 1 or fit["note"]:
            blocked_log.record(sym, market, "margin_check" if fit["qty"] < 1 else "margin_cut", fit["note"] or "margin could not be verified with Upstox",
                               list(self.positions.values()), bar_ts=str(candles[-1]["timestamp"]) if candles else None, direction=sig_result.direction,
                               wanted_qty=sig_result.qty, got_qty=fit["qty"])
        if fit["qty"] < 1:
            log.info("%s: %s entry skipped -- %s.", sym, market, fit["note"] or "margin check")
            return False
        if fit["note"]:
            log.info("%s: %s", sym, fit["note"])
        sig_result.qty = fit["qty"]
        entry, sl, qty = sig_result.entry_price, sig_result.stop_loss, sig_result.qty
        trade_val = qty * sig_result.lot_size * entry

        ts_tag = now.strftime('%Y%m%d_%H%M%S')
        pos_id = f"POS_{strat.id_prefix}_{ts_tag}_{sym}"
        entry_order_id = f"ORD_E_{strat.id_prefix}_{ts_tag}_{sym}"
        direction = sig_result.direction

        # 1. Virtual order + position in the DB
        self.db.place_order(
            order_id=entry_order_id, symbol=sym, direction="BUY" if direction == "long" else "SELL",
            intent="ENTRY", order_type="MARKET", qty=qty, requested_price=entry, fill_price=entry,
            status="FILLED", tag=f"DRY_RUN_{strat.id_prefix}_ENTRY", account_id=self.account_id,
        )
        diag = sig_result.diagnostics
        sig = {
            "position_id": pos_id, "entry_order_id": entry_order_id, "strategy": strat.name,
            "time": now.strftime("%H:%M:%S"), "symbol": sym, "direction": direction,
            "entry_price": round(entry, 2), "sl": sl, **sig_result.alert_levels,
            "qty": qty, "lots": qty, "setup_type": sig_result.setup_type,
            "trade_value": round(trade_val, 2),
            # Balance/capital only ever changes on a CLOSE (see _close_position -- self.capital +=
            # net_pnl), never on entry, so the "Balance" in the entry alert is realized equity, not
            # "cash left after this trade's margin". Shows the margin this trade blocks so that
            # distinction is visible instead of implying it's already netted out of Balance.
            "margin_used": round(fit["margin"] if fit["margin"] else trade_val / leverage, 2), "leverage": leverage,
            "stop_dist": round(sig_result.stop_dist, 4),
            "rsi": round(diag.get("rsi", 0.0), 1), "vwap_dist_pct": round(diag.get("vwap_dist_pct", 0.0), 4),
            "ema_slope_pct": round(diag.get("ema_slope_pct", 0.0), 4),
            "adx": round(diag.get("adx", 0.0), 1), "vol_surge": round(diag.get("vol_surge", 0.0), 2),
        }
        # Persist the strategy's exit state (+ the sizing facts a restart would otherwise lose) so an
        # overnight or restarted position is managed exactly as if the process had never stopped.
        state = {**sig_result.exit_state, "margin_used": sig["margin_used"], "leverage": leverage,
                 "lot_size": sig_result.lot_size}
        self.db.open_position(
            position_id=pos_id, symbol=sym, direction=direction, qty=qty, entry_price=entry,
            current_stop=sl, target_price=sig_result.target_price, breakeven_price=sig_result.breakeven_price,
            account_id=self.account_id, instrument_key=SYMBOL_MAP.get(sym), entry_order_id=entry_order_id,
            strategy=strat.name, state=json.dumps(state),
        )
        signals.append(sig)
        self.positions[sym] = {**sig, "entry_time": now, "current_stop": sl, "best_price": entry, **sig_result.exit_state}
        return True

    def _maybe_exit(self, sym: str, now: datetime):
        pos = self.positions[sym]
        strat = self._strategy_of(pos, sym)
        candles = self._candles(sym, strat)
        if not candles:
            return
        live_prices.update(sym, candles[-1]["close"], now)               # for the dashboard's unrealised P&L (it never calls Upstox itself)
        armed_key = "armed_be" if "armed_be" in pos else "armed_trail"
        before = (pos["current_stop"], pos.get(armed_key))
        decision = strat.manage(pos, ExitContext(symbol=sym, candles=candles, now=now))
        if decision is not None:
            self._close_position(sym, pos, decision.price, decision.reason, now)
            return
        if (pos["current_stop"], pos.get(armed_key)) != before:
            self.db.update_position_stop(
                position_id=pos["position_id"], current_stop=pos["current_stop"],
                best_price=pos["best_price"], armed_be=bool(pos.get(armed_key, False)),
            )

    def _close_position(self, sym: str, pos: dict, exit_p: float, reason: str, now: datetime):
        live_prices.prune([s for s in self.positions if s != sym])
        """Shared exit path for SL/TP/timeout/EOD exits AND the manual Telegram
        kill switch (force_exit_all) -- one place that writes the DB order/
        position/trade/snapshot records, updates capital, and alerts."""
        # 1. Calculate Exact Itemized Costs (the strategy's own cost model: intraday vs delivery, per market)
        strat = self._strategy_of(pos, sym)
        cost_info = strat.costs(sym, pos["direction"], pos["entry_price"], exit_p, pos.get("lots", pos.get("qty", 1)))

        net_pnl = cost_info["net"]
        gross_pnl = cost_info["gross"]
        self.capital += net_pnl
        self.peak_capital = max(self.peak_capital, self.capital)

        # Market-specific daily loss limit & cooldown timer
        market = _get_market(sym)
        self.market_daily_pnl[market] = self.market_daily_pnl.get(market, 0.0) + net_pnl
        if self.day_start_capital > 0:
            mkt_loss_pct = -self.market_daily_pnl[market] / self.day_start_capital * 100
            if mkt_loss_pct >= self.max_market_daily_loss_pct:
                if self.market_cooldown_until.get(market) is None or self.market_cooldown_until[market] < now:
                    # Adaptive cooldown duration: scale by how far the loss exceeded the limit.
                    # 100-149% of limit -> base, 150-199% -> 1.5x, >=200% -> 2x.
                    overshoot = mkt_loss_pct / self.max_market_daily_loss_pct
                    if overshoot >= 2.0:
                        cooldown_mins = int(self.market_cooldown_minutes * 2)
                    elif overshoot >= 1.5:
                        cooldown_mins = int(self.market_cooldown_minutes * 1.5)
                    else:
                        cooldown_mins = self.market_cooldown_minutes
                    cooldown_expiry = now + timedelta(minutes=cooldown_mins)
                    self.market_cooldown_until[market] = cooldown_expiry
                    expiry_time_str = cooldown_expiry.strftime("%H:%M:%S")
                    print(f"\n{BOLD}{YL}⏸ {market.upper()} COOLDOWN ACTIVATED ({mkt_loss_pct:.2f}% loss >= "
                          f"{self.max_market_daily_loss_pct}%) -- {market} entries paused for {cooldown_mins}m "
                          f"until {expiry_time_str} IST. Other markets continue normally.{R}\n")
                    telegram.send(
                        f"⏸ <b>{market.upper()} COOLDOWN ACTIVATED</b> — {mkt_loss_pct:.2f}% loss "
                        f"(limit {self.max_market_daily_loss_pct}%)\n"
                        f"New {market} entries paused for <b>{cooldown_mins}m</b> until <b>{expiry_time_str} IST</b>.\n"
                        f"Other markets continue normally."
                    )

        # Per-symbol daily-loss kill switch (see __init__'s comment) --
        # tracked independently of the account-wide one above.
        self.symbol_daily_pnl[sym] = self.symbol_daily_pnl.get(sym, 0.0) + net_pnl
        if not self.symbol_kill_switch.get(sym, False) and self.day_start_capital > 0:
            sym_loss_pct = -self.symbol_daily_pnl[sym] / self.day_start_capital * 100
            if sym_loss_pct >= self.max_daily_loss_pct:
                self.symbol_kill_switch[sym] = True
                print(f"\n{BOLD}{RED}!! {sym} DAILY LOSS LIMIT HIT ({sym_loss_pct:.2f}% >= "
                      f"{self.max_daily_loss_pct}%) -- new {sym} entries halted for today, "
                      f"other symbols unaffected. !!{R}\n")
                telegram.send(
                    f"🛑 <b>{sym} DAILY LOSS LIMIT HIT</b> — {sym_loss_pct:.2f}% (limit {self.max_daily_loss_pct}%)\n"
                    f"New {sym} entries halted for the rest of today. Other symbols continue normally."
                )

        # 2. Record Virtual Exit Order in DB
        ts_tag = now.strftime('%Y%m%d_%H%M%S')
        exit_order_id = f"ORD_X_{ts_tag}_{sym}"
        exit_side = "SELL" if pos["direction"] == "long" else "BUY"
        self.db.place_order(
            order_id=exit_order_id,
            symbol=sym,
            direction=exit_side,
            intent=reason.upper(),
            order_type="MARKET",
            qty=pos.get("lots", pos.get("qty", 1)),
            requested_price=exit_p,
            fill_price=exit_p,
            status="FILLED",
            tag=f"DRY_RUN_EXIT_{reason.upper()}",
            account_id=self.account_id
        )

        # 3. Close Position in DB
        self.db.close_position(
            position_id=pos["position_id"],
            exit_price=exit_p,
            exit_reason=reason,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            total_fees=cost_info["total"]
        )

        # 4. Record Detailed Trade Record in DB
        hold_mins = (now - pos["entry_time"]).total_seconds() / 60.0
        self.db.record_trade(
            position_id=pos["position_id"],
            symbol=sym,
            direction=pos["direction"],
            qty=pos.get("lots", pos.get("qty", 1)),
            entry_price=pos["entry_price"],
            exit_price=exit_p,
            entry_dt=pos["entry_time"].isoformat(),
            exit_dt=now.isoformat(),
            hold_minutes=hold_mins,
            exit_reason=reason,
            gross_pnl=gross_pnl,
            costs_dict=cost_info,
            net_pnl=net_pnl,
            capital_after=self.capital,
            rsi=pos.get("rsi"),
            adx=pos.get("adx"),
            vwap_dist_pct=pos.get("vwap_dist_pct"),
            ema_slope_pct=pos.get("ema_slope_pct"),
            account_id=self.account_id,
            strategy=strat.name,
        )

        # 5. Record Portfolio Equity Snapshot in DB
        self.db.record_snapshot(self.account_id)

        col = GR if net_pnl >= 0 else RED
        print(f"  {BOLD}EXIT{R}  {WH}{sym:12s}{R} {pos['direction']:5s} "
              f"@ {YL}₹{exit_p:.2f}{R}  [{reason}]  "
              f"Net: {col}₹{net_pnl:+,.2f}{R} (Fees: ₹{cost_info['total']:.2f})  Cap: ₹{self.capital:,.2f}")
        telegram.alert_exit(sym, pos, exit_p, reason, net_pnl, self.capital)

        rec = {**pos, "exit_price": exit_p, "exit_reason": reason,
               "net_pnl": round(net_pnl, 2), "gross_pnl": round(gross_pnl, 2),
               "total_costs": round(cost_info["total"], 2),
               "exit_time": now.strftime("%H:%M:%S")}
        self.trades.append(rec)
        del self.positions[sym]
        self._save()

    # ---- Telegram kill switch -------------------------------------------
    # Persisted to disk (not just in-memory) so a "stop" command survives a
    # systemd restart: the daemon must come back up still halted, not quietly
    # resume trading just because the process happened to restart.
    def _control_path(self) -> Path:
        return DB_DIR / f".{self.account_id}_control.json"

    def _load_control(self) -> dict:
        try:
            return json.loads(self._control_path().read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def _save_control(self, **updates) -> None:
        data = self._load_control()
        data.update(updates)
        self._control_path().parent.mkdir(parents=True, exist_ok=True)
        self._control_path().write_text(json.dumps(data))

    def _load_trading_enabled(self) -> bool:
        return self._load_control().get("enabled", True)

    def get_telegram_offset(self):
        """Last processed Telegram update_id + 1, or None before the first poll --
        persisted so a restart doesn't replay (or miss) start/stop commands."""
        return self._load_control().get("telegram_offset")

    def save_telegram_offset(self, offset: int) -> None:
        self._save_control(telegram_offset=offset)

    def stop_trading(self, now: datetime) -> int:
        """Kill switch: halt new entries and force-exit every open position
        at the current market price. Returns the number of positions closed."""
        self.trading_enabled = False
        self._save_control(enabled=False)
        n = 0
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            candles = self._candles(sym, self._strategy_of(pos, sym))
            exit_p = float(candles[-1]["close"]) if candles else pos["entry_price"]
            self._close_position(sym, pos, exit_p, "manual_kill_switch", now)
            n += 1
        return n

    def start_trading(self) -> None:
        self.trading_enabled = True
        self._save_control(enabled=True)

    def _save(self):
        if not self.trades:
            return
        # fieldnames must cover every trade's keys, not just the first --
        # position dicts vary by asset type (equity/commodity/currency have
        # different fields, e.g. 'armed_be'/'be'), so a later trade can carry
        # keys the first one didn't, which crashes DictWriter otherwise.
        fieldnames = []
        seen = set()
        for t in self.trades:
            for k in t.keys():
                if k not in seen:
                    seen.add(k)
                    fieldnames.append(k)
        with open(self.log_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader(); w.writerows(self.trades)


# ---------------------------------------------------------------------------
# CLI & Terminal Output
# ---------------------------------------------------------------------------

def _print_sig(sig: dict, cap: float):
    # col and arrow must be derived locally here -- they are NOT in scope from
    # anywhere else in this module (col is defined only inside _close_position).
    # Bug fixed 2026-09-22: previously referenced undefined variables, causing
    # a NameError crash on every virtual entry signal.
    _is_long = sig["direction"] == "long"
    col = GR if _is_long else RED
    arrow = "🟢 LONG ↑" if _is_long else "🔴 SHORT ↓"
    stype = sig.get('setup_type', 'trend_breakout').replace('_', ' ').upper()
    print(f"\n  {BOLD}VIRTUAL ORDER PLACED  {WH}{sig['symbol']:12s}{R} {col}{arrow}{R} "
          f"@ {YL}₹{sig['entry_price']:.2f}{R} [{CY}{stype}{R}]")
    print(f"     Order ID: {CY}{sig.get('entry_order_id', 'N/A')}{R} | Position ID: {CY}{sig.get('position_id', 'N/A')}{R}")
    print(f"     Strategy: {CY}{stype}{R} | SL: {RED}₹{sig['sl']:.2f}{R}  TP: {GR}₹{sig.get('tp') or 0:.2f}{R}  "
          f"BE: ₹{sig.get('be', sig.get('activation_price', 0)):.2f}")
    print(f"     Qty: {sig['qty']}  Val: ₹{sig['trade_value']:,.0f}  "
          f"RSI: {sig['rsi']:.1f}")
    print(f"     VWAP: {sig['vwap_dist_pct']:+.3f}%  EMA: {sig['ema_slope_pct']:+.4f}%  "
          f"Cap now: ₹{cap:,.2f}")
    print(f"     {GY}[VIRTUAL EXECUTION -> RECORDED IN SQLITE DB - NO REAL BROKER ORDER]{R}")
    telegram.alert_entry(sig, cap)


_token_failure_alerted = False


def check_token(broker) -> str:
    """Make sure the Upstox token is valid, adopting one another process already saved before logging in again. Returns 'unconfigured', 'valid',
    'refreshed', 'failed' or 'error'. The operator is told once per failure streak and once when it recovers (not on every retry)."""
    global _token_failure_alerted
    from engine.config import UpstoxConfig
    if not UpstoxConfig.auto_login_configured():
        if not UpstoxConfig.ACCESS_TOKEN:
            telegram.send("🔴 <b>TOKEN INVALID</b> — auto-login not configured. Run `python3 -m auth.upstox_auth` manually.")
        return "unconfigured"
    try:
        from services.auth.upstox_auto_login import ensure_fresh_upstox_token
        old = UpstoxConfig.ACCESS_TOKEN
        new = ensure_fresh_upstox_token(on_token_refreshed=broker.set_access_token)
    except Exception as exc:
        log.error("Token refresh raised: %s", exc, exc_info=True)
        if not _token_failure_alerted:
            _token_failure_alerted = True
            telegram.alert_error("Token refresh", exc)
        return "error"
    if new is None:
        log.error("Token refresh failed — trading may be blind until fixed; retrying shortly.")
        if not _token_failure_alerted:
            _token_failure_alerted = True
            telegram.send("🔴 <b>TOKEN REFRESH FAILED</b> — auto-login attempt failed; retrying every minute. "
                          "Run `python3 -m auth.upstox_auth` manually if this persists.")
        return "failed"
    if _token_failure_alerted:
        _token_failure_alerted = False
        telegram.send("🟢 <b>TOKEN RECOVERED</b> — trading resumed.")
    if new != old:
        log.info("Token refreshed via scheduled check.")
        telegram.send("🔑 <b>TOKEN REFRESHED</b> (scheduled check) — dry run continuing normally.")
        return "refreshed"
    return "valid"


def refresh_market_hours(broker, day=None) -> None:
    """Read today's (or `day`'s) real exchange hours from Upstox and tell the operator if they differ from what was in use
    (a special or shortened session, MCX's daylight-saving shift). A failure keeps the last known hours."""
    try:
        for line in sessions.refresh(broker._api_client, day):
            log.warning("EXCHANGE HOURS CHANGED: %s", line)
            telegram.send(f"🕒 <b>EXCHANGE HOURS CHANGED</b> (from Upstox) — {line}")
    except Exception as exc:
        log.warning("Exchange-hours refresh failed: %s", exc)


def check_cost_drift(broker, runner) -> None:
    """At start: compare our cost model with Upstox's brokerage calculator for the symbols being traded (engine/cost_drift.py)."""
    try:
        from engine import cost_drift
        keys = {s: SYMBOL_MAP[s] for s in runner.symbols if SYMBOL_MAP.get(s)}
        sample = {}
        for market in ("commodity", "currency", "equity"):                       # one representative symbol per market keeps this to a few calls
            pick = next((s for s in keys if _get_market(s) == market), None)
            if pick:
                sample[pick] = keys[pick]
        rows = cost_drift.check(broker, sample, _get_market)
        log.info("Cost model vs Upstox calculator: %s", {r["symbol"]: r.get("ratio", r["status"]) for r in rows})
        bad = [r for r in rows if r["status"] in ("UNDERSTATES", "overstates")]
        if bad:
            telegram.send("💸 <b>COST MODEL DRIFT</b> — our charges differ from Upstox's calculator:\n" + cost_drift.format_rows(bad))
    except Exception as exc:
        log.warning("Cost-drift check failed: %s", exc)


def refresh_margin_rates(broker, runner) -> None:
    """Fetch Upstox's real margin for one lot / share of every traded symbol, then say plainly which ones this capital cannot afford.
    Called at start and at each trading-day rollover. A failure keeps the last known rates (engine/margin_rates.py)."""
    try:
        def notional(sym, price):
            market = _get_market(sym)
            if market == "equity":
                return price
            from markets.currency.costs import get_contract_multiplier as cur_mult
            from markets.commodity.costs import get_contract_multiplier as com_mult
            return price * (cur_mult(sym) if market == "currency" else com_mult(sym))
        keys = {s: SYMBOL_MAP[s] for s in runner.symbols if SYMBOL_MAP.get(s)}
        changed = []
        rates = margin_rates.refresh(broker, keys, notional,
                                     equity_syms=frozenset(s for s in keys if _is_equity(s)), mismatches=changed)
        if changed:
            msg = ", ".join(f"{s}: Upstox lot size was {a}, now {b}" for s, a, b in changed)
            log.error("LOT SIZE CHANGED by the broker: %s", msg)
            telegram.send(f"🔴 <b>LOT SIZE CHANGED</b> — {msg}. Costs / P&L for these still use the old size until the cost model is updated: "
                          f"markets/*/costs.py *_SPECS. Consider pausing them (/stop commodity, /stop currency).")
        log.info("Upstox margin rates: %s", {s: f"{r['leverage']:.1f}x (Rs{r['margin']:,.0f}/unit)" for s, r in rates.items() if s in keys and not _is_equity(s)})
        blocked = margin_rates.unaffordable(runner.capital, [s for s in runner.symbols])
        names = tuple(sorted(s for s, _ in blocked))
        if blocked:
            msg = ", ".join(f"{s} (1 lot needs Rs{m:,.0f})" for s, m in blocked)
            log.warning("Cannot trade with capital Rs%s: %s", f"{runner.capital:,.0f}", msg)
            if names != margin_rates.last_alerted:                     # tell the operator once per change, not every refresh
                telegram.send(f"ℹ️ <b>NOT TRADABLE at Rs{runner.capital:,.0f}</b> (Upstox margin for ONE lot exceeds capital): {msg}")
        elif margin_rates.last_alerted:
            telegram.send("✅ All configured symbols are affordable again at the current capital / Upstox margin.")
        margin_rates.last_alerted = names
    except Exception as exc:                            # margin info is a safety net; it must never stop the daemon
        log.warning("Margin refresh failed: %s", exc)


def handle_operator_command(runner, cmd: str, now: datetime, scan_n: int) -> str | None:
    """Apply one operator command from the Telegram chat. Returns the action taken ('stop', 'start', 'pause:<segment>',
    'resume:<segment>', 'status') or None for an unknown command. Separate from main() so it can be tested without a live loop."""
    if cmd in ("/stop", "stop", "stop trading"):
        closed = runner.stop_trading(now)
        print(f"  {BOLD}{RED}!! TRADING STOPPED via Telegram -- {closed} position(s) force-closed !!{R}", flush=True)
        telegram.send(f"🛑 <b>TRADING STOPPED</b> (manual) — {closed} open position(s) force-closed.\n"
                      f"New entries halted until you send /start.")
        return "stop"
    if cmd in ("/start", "start", "start trading"):
        runner.start_trading()
        print(f"  {BOLD}{GR}TRADING STARTED via Telegram{R}", flush=True)
        telegram.send("🟢 <b>TRADING STARTED</b> (manual) — resuming normal entries.")
        return "start"
    for seg in ("commodity", "currency", "equity"):
        if cmd in (f"/stop {seg}", f"/disable {seg}", f"stop {seg}"):
            runner.segment_enabled[seg] = False
            telegram.send(f"⏸ <b>{seg.upper()} TRADING PAUSED</b> (manual) — new {seg} entries halted.")
            return f"pause:{seg}"
        if cmd in (f"/start {seg}", f"/enable {seg}", f"start {seg}"):
            runner.segment_enabled[seg] = True
            telegram.send(f"▶ <b>{seg.upper()} TRADING RESUMED</b> (manual) — evaluating new {seg} entries.")
            return f"resume:{seg}"
    if cmd in ("/status", "status"):
        open_syms = list(runner.positions.keys())
        seg_status_str = ", ".join([f"{m.capitalize()}: {'🟢' if v else '🛑 PAUSED'}" for m, v in runner.segment_enabled.items()])
        regime_line = f"\nSegments: {seg_status_str}"
        if runner.use_commodity_regime_filter:
            regime_str = ", ".join([f"{s}: {'🟢' if v is not False else '🔴'}" for s, v in runner.commodity_regime_ok.items()])
            regime_line += f"\nCommodity regimes: {regime_str or 'N/A'}"
        if runner.use_equity_regime_filter:
            regime_line += f"\nEquity regime: {'🟢 OK' if runner.equity_regime_ok is not False else '🔴 BLOCKED'}"
        pos_lines = ""
        for sym, p in runner.positions.items():
            pos_lines += f"\n  • {sym} {p['direction'].upper()} @ ₹{p['entry_price']:.2f} | stop ₹{p['current_stop']:.2f}"
        telegram.send(
            f"📊 <b>DRYRUN STATUS</b>\n"
            f"Balance: ₹{runner.capital:,.2f}\n"
            f"Trading: {'🟢 ENABLED' if runner.trading_enabled else '🛑 HALTED'}\n"
            f"Scan #{scan_n} @ {now.strftime('%H:%M:%S')} IST\n"
            f"Open positions ({len(open_syms)}): {', '.join(open_syms) or 'none'}"
            f"{pos_lines}"
            f"{regime_line}"
        )
        return "status"
    return None


def main():
    # Wires up the rotating file handler on the ROOT logger (name="") so
    # every module's get_logger(__name__) call -- live_dryrun's own, plus
    # broker/upstox_broker.py, auth/upstox_auto_login.py, etc. -- actually
    # reaches a persistent log file via propagation, not just stdout/stderr
    # (which is all journalctl captures; nothing was ever written to
    # logs/ before this, since get_logger() alone never attaches handlers).
    setup_logger("", log_file=str(LOG_DIR / "live_dryrun.log"))

    ap = argparse.ArgumentParser(description="Live paper-trading dry run with SQLite virtual order & PnL tracking")
    ap.add_argument("--token",     default=None,           help="Upstox access token")
    ap.add_argument("--capital",   type=float, default=100_000, help="Paper capital Rs (default 1,00,000)")
    ap.add_argument("--risk-pct",  type=float, default=DEFAULT_RISK_PCT, help="Risk %% per trade")
    ap.add_argument("--leverage",  type=float, default=DEFAULT_LEVERAGE, help="MIS leverage (default 4.0)")
    ap.add_argument("--symbols",   nargs="+",  default=None,        help="MCX commodity / NSE currency symbols to trade")
    ap.add_argument("--db",        default=None,           help="Path to SQLite DB (default: var/db/paper_trading.db)")
    ap.add_argument("--account",   default="DRYRUN_ACCOUNT", help="Account identifier")
    ap.add_argument("--long-only", action="store_true",    help="Execute LONG orders only (skip all short setups)")
    ap.add_argument("--direction", choices=["both", "long", "short"], default="both", help="Trade direction filter: both, long, or short")
    ap.add_argument("--interval",  type=int,   default=DEFAULT_SCAN_INTERVAL_SECONDS, help="Scan interval in seconds (default 60s)")
    ap.add_argument("--report",    action="store_true",    help="Show current DB dashboard / performance summary and exit")
    ap.add_argument("--reset-db",  action="store_true",    help="Reset paper trading DB before running")
    args = ap.parse_args()

    db = TradingDB(args.db)

    if args.reset_db:
        if db.db_path.exists():
            db.db_path.unlink()
        db._init_db()
        db.init_account(args.account, capital=args.capital, leverage=args.leverage, risk_pct=args.risk_pct)
        print(f"{GR}Paper trading DB reset at {db.db_path} with initial capital ₹{args.capital:,.2f}{R}")

    if args.report:
        db.print_dashboard(args.account)
        from services.utils.chart import generate_equity_curve
        chart_path = generate_equity_curve(
            db.get_snapshots(args.account), args.account,
            LOG_DIR / f"equity_{args.account}.png",
        )
        if chart_path:
            print(f"{GY}Equity curve chart saved: {WH}{chart_path}{R}")
        return

    _lock_fh = _acquire_process_lock(args.account)  # noqa: F841 – held for process lifetime; fd must stay open to keep fcntl lock alive

    token = args.token or _load_token()

    # Startup token validation: a cached/env token being *present* doesn't
    # mean it's still *valid* (Upstox tokens expire daily) -- this system
    # trades real money on a schedule, so silently starting with a stale
    # token and only discovering it hours later via the in-loop check is not
    # acceptable. Validate now and force a refresh if needed, before the
    # daemon ever claims to be running.
    from services.auth.upstox_auto_login import _token_is_valid, ensure_fresh_upstox_token
    from engine.config import UpstoxConfig as _UC
    if not token or not _token_is_valid(token):
        print(f"{YL}Cached/provided token is missing or invalid -- attempting auto-login refresh...{R}")
        if _UC.auto_login_configured():
            try:
                token = ensure_fresh_upstox_token(force=True) or ""
            except Exception as exc:
                log.error("Startup auto-login raised: %s", exc, exc_info=True)
                telegram.alert_error("Startup auto-login", exc)
                token = ""
        if token:
            print(f"{GR}Auto-login refresh succeeded.{R}")
            telegram.send("🔑 <b>TOKEN REFRESHED</b> at startup via auto-login -- dry run proceeding.")
        else:
            print(f"{RED}ERROR: No valid ACCESS_TOKEN available (cache/.env stale and auto-login "
                  f"unavailable/failed). Set in backend/.env, pass --token, or run "
                  f"`python3 -m auth.upstox_auth` manually.{R}")
            telegram.send("🔴 <b>DRYRUN FAILED TO START</b> — no valid Upstox token and auto-login "
                          "unavailable/failed. Run `python3 -m auth.upstox_auth` manually.")
            sys.exit(1)

    broker = UpstoxBroker(access_token=token, dry_run=True)

    # Energy Contracts (Crude Oil Mini & Natural Gas Mini) default if --symbols not given
    default_symbols = ["CRUDEOILM", "NATGASMINI"]
    symbols = args.symbols or default_symbols
    # NSE equity intraday scalper wired in 2026-09-19 (see
    # markets/equity/scalping/entry_signal.py's module docstring) -- opt-in via
    # DRYRUN_INCLUDE_EQUITY=true rather than folded into DRYRUN_SYMBOLS,
    # since the full validated universe is all 49 NIFTY50 names (see
    # markets/equity/universe.py) and hand-typing that into a symbols list
    # would be unwieldy. Extends whatever commodity/currency symbols are
    # already configured rather than replacing them -- equity runs
    # alongside, not instead of.
    if os.environ.get("DRYRUN_INCLUDE_EQUITY", "false").lower() in ("1", "true", "yes"):
        symbols = list(symbols) + [s for s in NIFTY50_SYMBOLS if s not in symbols]
        market_mode = "MCX COMMODITIES / NSE CURRENCY / NSE EQUITY"
    else:
        market_mode = "MCX COMMODITIES / NSE CURRENCY"

    direction_mode = "long" if args.long_only else args.direction
    runner = DryRunner(broker, db, symbols, args.capital, args.risk_pct, args.leverage,
                       account_id=args.account, direction_filter=direction_mode)
    refresh_market_hours(broker)
    refresh_margin_rates(broker, runner)
    check_cost_drift(broker, runner)


    print(f"\n{BOLD}{CY}{'='*75}{R}")
    print(f"{BOLD}{WH}  LIVE PAPER-TRADING DRY RUN (24/7 DAEMON)  --  {market_mode}{R}")
    print(f"  {GY}Capital:{R} {WH}₹{runner.capital:,.0f}{R}  "
          f"{GY}Risk:{R} {WH}{args.risk_pct}%{R}  "
          f"{GY}Leverage:{R} {MG}{args.leverage}x{R}  "
          f"{GY}Direction:{R} {CY}{direction_mode.upper()}{R}  "
          f"{GY}Interval:{R} {YL}{args.interval}s{R}"
          f"  {GY}Hours:{R} {WH}MCX 09:00-23:30 / Currency 09:00-17:00 IST{R}")
    print(f"  {GY}Database:{R} {WH}{db.db_path}{R}")
    print(f"  {GY}Symbols ({len(symbols)}):{R} {WH}{', '.join(symbols[:8])}{'...' if len(symbols)>8 else ''}{R}")
    print(f"{BOLD}{CY}{'='*75}{R}\n")

    telegram.send(
        f"🟢 <b>DRYRUN DAEMON STARTED</b> — {market_mode}\n"
        f"Capital: ₹{runner.capital:,.0f}  Risk: {args.risk_pct}%  Leverage: {args.leverage}x  "
        f"Direction: {direction_mode.upper()}\n"
        f"Symbols: {', '.join(symbols)}\n"
        f"Trading is currently {'🟢 ENABLED' if runner.trading_enabled else '🛑 STOPPED (send /start to resume)'}.\n"
        f"Send <b>/stop</b> to halt new entries and force-close all open positions, or <b>/start</b> to resume."
    )

    from engine.config import UpstoxConfig
    token_check_interval_sec = int(os.environ.get("TOKEN_CHECK_INTERVAL_MIN", "15")) * 60
    last_token_check = time.monotonic()
    token_retry_sec = int(os.environ.get("TOKEN_RETRY_SEC", "60"))
    token_ok = True
    token_fail_streak = 0
    token_max_fast_retries = int(os.environ.get("TOKEN_MAX_FAST_RETRIES", "5"))
    margin_refresh_sec = int(os.environ.get("MARGIN_REFRESH_MIN", "60")) * 60
    last_margin_refresh = time.monotonic()          # the start-up refresh above just ran

    # Treat SIGTERM (systemctl stop / systemd restart) the same as Ctrl-C: a
    # clean, alerted shutdown -- instead of an unhandled-exception crash that
    # would exit non-zero and fight the stop command via Restart=on-failure.
    #
    # A signal-handler-raised KeyboardInterrupt is injected asynchronously,
    # between arbitrary bytecode instructions -- it can land literally
    # anywhere, including deep inside a Telegram network call during the
    # graceful shutdown path itself (EOD summary / the final "STOPPED"
    # message), and Python's traceback for it can even misattribute the
    # line (observed 2026-09-17: it pointed at a comment). Wrapping every
    # individual call site in try/except BaseException is fragile -- easy to
    # miss one, as the final unconditional telegram.send() in this function
    # was. The robust fix is to stop the signal from firing a second time at
    # all: once shutdown has begun, re-arm SIGTERM to SIG_IGN so nothing
    # further can be injected while cleanup runs. systemd's
    # TimeoutStopSec/SIGKILL remains the real backstop if cleanup ever hangs.
    import signal
    def _handle_sigterm(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # 24/7 outer loop: market-hours gating lives entirely in this code (not in
    # the process supervisor) -- the process itself waits out nights/weekends
    # and rolls into the next trading day rather than exiting, so systemd (or
    # any supervisor) just needs to keep one long-running process alive.
    stopped_by_user = False
    while not stopped_by_user:
        now = datetime.now(IST)
        # Widest window across both MCX (till 23:30) and currency (till 17:00)
        # -- per-symbol precision is handled inside scan()/_maybe_exit().
        _wins = [sessions.window(m) for m in ("commodity", "currency", "equity")]          # Upstox's hours (core/sessions.py)
        _lo, _hi = min(w[0] for w in _wins), max(w[1] for w in _wins)
        mopen  = now.replace(hour=_lo // 60, minute=_lo % 60, second=0, microsecond=0)
        mclose = now.replace(hour=_hi // 60, minute=_hi % 60, second=0, microsecond=0)

        if now > mclose:
            # Today's session is already over -- roll to the next trading day.
            # Skips weekends AND real public holidays. Holiday dates come from
            # Upstox's own public holiday API (utils/market_holidays.py) --
            # never a hand-maintained/hardcoded list, and never a hardcoded
            # year; it always reflects whatever "this year" currently is,
            # refetched automatically once a day (and across a year
            # boundary). Was previously Sunday-only, an admitted gap in an
            # earlier version of this comment -- found 2026-09-18.
            trading_holidays = get_trading_holidays()
            next_day = now + timedelta(days=1)
            while next_day.weekday() >= 5 or next_day.date() in trading_holidays:
                next_day += timedelta(days=1)
            mopen  = mopen.replace(year=next_day.year, month=next_day.month, day=next_day.day)
            mclose = mclose.replace(year=next_day.year, month=next_day.month, day=next_day.day)
            refresh_market_hours(broker, next_day.date())       # a special session or an hours change on that day is picked up before it opens

            # Refresh instrument_key resolution once per trading-day rollover --
            # not just at process startup -- so a monthly contract expiry is
            # picked up within a day instead of depending on the weekly
            # finetune job's incidental restart to notice it (found 2026-09-18).
            fresh_map = _build_symbol_map()
            changed = {k: v for k, v in fresh_map.items() if SYMBOL_MAP.get(k) != v}
            if changed:
                log.info("Instrument key(s) rolled over: %s", changed)
                telegram.send(f"🔄 <b>CONTRACT ROLLOVER</b> — {len(changed)} instrument key(s) updated: "
                              f"{', '.join(changed.keys())}")
            SYMBOL_MAP.clear()
            SYMBOL_MAP.update(fresh_map)
            refresh_margin_rates(broker, runner)

            # Pre-rollover contract expiry alert (MCX contracts expire ~20th of month)
            day_of_month = mopen.day
            if 18 <= day_of_month <= 20:
                mcx_syms = [s for s in runner.symbols if not _is_currency(s) and not _is_equity(s)]
                if mcx_syms:
                    telegram.send(
                        f"⚠️ <b>MCX CONTRACT EXPIRY WARNING</b> — Today is day {day_of_month} of month.\n"
                        f"MCX contracts for {', '.join(mcx_syms)} expire around 20th. Verify positions & instrument keys."
                    )

        runner.today = mopen.strftime("%Y-%m-%d")
        runner.log_path = LOG_DIR / f"dryrun_{runner.today}.csv"

        now = datetime.now(IST)
        if now < mopen:
            wait = int((mopen - now).total_seconds())
            print(f"  {YL}Market opens {mopen.strftime('%Y-%m-%d %H:%M')} IST "
                  f"(in {wait//3600}h {(wait%3600)//60}m) — sleeping...{R}", flush=True)
            heartbeat.beat("waiting_for_open", wait)          # a long sleep is fine: the deadline says when the next beat is due
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                print(f"\n{YL}Stopped by user (during overnight wait).{R}", flush=True)
                stopped_by_user = True
                continue

        scan_n = 0
        while datetime.now(IST) <= mclose:
            scan_n += 1
            now = datetime.now(IST)
            heartbeat.beat("scan", 0)
            print(f"\n{GY}-- Scan #{scan_n} @ {now.strftime('%Y-%m-%d %H:%M:%S')} IST  "
                  f"| Paper Balance: {WH}₹{runner.capital:,.2f}{GY} --{R}", flush=True)

            # Telegram start/stop kill switch: a quick non-blocking (timeout=0)
            # poll once per scan for any new command from the operator chat.
            # "stop" halts new entries AND force-closes every open position at
            # the current market price; "start" resumes normal entries. Both
            # states persist to disk (see runner.stop_trading/start_trading)
            # so they survive a systemd restart.
            try:
                updates = telegram.get_updates(offset=runner.get_telegram_offset(), timeout=0)
                for u in updates:
                    runner.save_telegram_offset(u["update_id"] + 1)
                    if not telegram.is_authorized_chat(u["chat_id"]):
                        continue
                    handle_operator_command(runner, u["text"].strip().lower(), now, scan_n)
            except Exception as exc:
                log.error("Telegram command poll raised: %s", exc, exc_info=True)

            # Token first (a scan or margin call on a dead token only produces a wall of 401s), then the hourly refresh of Upstox's hours / margins.
            if token_invalid_event.is_set() or (time.monotonic() - last_token_check) >= token_check_interval_sec:
                token_invalid_event.clear()
                status = check_token(broker)
                token_ok = status not in ("failed", "error")
                token_fail_streak = 0 if token_ok else token_fail_streak + 1
                # A failed login is retried in TOKEN_RETRY_SEC, not after a whole TOKEN_CHECK_INTERVAL_MIN of a blind session (2026-09-25: 15 minutes lost
                # at the open). Capped at TOKEN_MAX_FAST_RETRIES so a persistent failure cannot hammer Upstox's login and get the account rate-limited.
                fast = (not token_ok) and token_fail_streak <= token_max_fast_retries
                last_token_check = time.monotonic() - (token_check_interval_sec - token_retry_sec if fast else 0)

            # Margins and lot sizes are the broker's to change at any time: re-read them regularly, not just once a day.
            if (time.monotonic() - last_margin_refresh) >= margin_refresh_sec and token_ok:
                last_margin_refresh = time.monotonic()
                refresh_market_hours(broker)
                refresh_margin_rates(broker, runner)

            try:
                sigs = runner.scan()
                if sigs:
                    for s in sigs:
                        _print_sig(s, runner.capital)
                else:
                    open_s = list(runner.positions.keys())
                    if open_s:
                        print(f"  {GY}No new signals. Open Positions: {YL}{', '.join(open_s)}{R}", flush=True)
                    else:
                        print(f"  {GY}No signals. Watching {len(symbols)} symbols...{R}", flush=True)
            except KeyboardInterrupt:
                print(f"\n{YL}Stopped by user.{R}", flush=True)
                stopped_by_user = True
                break
            except Exception as exc:
                log.error("Scan error: %s", exc, exc_info=True)
                telegram.alert_error(f"Scan #{scan_n} ({market_mode})", exc)

            nxt = datetime.now(IST) + timedelta(seconds=args.interval)
            # Sleep to whichever comes first, rather than breaking out the
            # instant the next scan would overrun close: breaking early left
            # `now` a couple seconds before mclose, so the outer loop's
            # `now > mclose` day-rollover check below missed it and re-entered
            # this loop for one more scan -- duplicating the EOD Telegram
            # summary and equity-curve photo with identical (no-new-trades)
            # data. Sleeping past mclose lets the while-condition above exit
            # this loop naturally, with `now` guaranteed past close.
            sleep_until = min(nxt, mclose + timedelta(seconds=1))
            secs = max(1, (sleep_until - datetime.now(IST)).total_seconds())
            print(f"  {GY}Next scan in {secs:.0f}s...{R}", flush=True)
            heartbeat.beat("sleep", secs)
            time.sleep(secs)

        # EOD summary for the day just finished
        print(f"\n{BOLD}{CY}{'='*75}{R}")
        print(f"{BOLD}{WH}  SESSION COMPLETE ({runner.today}) — DATABASE SUMMARY{R}")
        db.print_dashboard(args.account)
        runner._save()

        # Sourced from the DB, not the in-memory runner.trades list -- that
        # list starts empty on every process start, so a restart mid-session
        # (e.g. to pick up a threshold change) used to make this report
        # "Trades today: 0 / Total PnL: Rs0" even with real trades already
        # closed earlier that day. The DB survives restarts; see
        # database.py's get_trades_for_date() docstring for the full story.
        todays_trades = db.get_trades_for_date(runner.today, args.account)
        total_pnl = sum(t.get("net_pnl", 0) for t in todays_trades)
        telegram.send(
            f"🏁 <b>SESSION COMPLETE</b> ({runner.today}) — {market_mode}\n"
            f"Trades today: {len(todays_trades)}  Total PnL: ₹{total_pnl:+,.2f}\n"
            f"Balance: ₹{runner.capital:,.2f}"
        )

        try:
            from engine.scorecard import format_scorecard
            card = format_scorecard(todays_trades, db.get_trades(limit=1_000_000, account_id=args.account))
            if card:
                telegram.send(card)
        except Exception as exc:                       # a reporting problem must never break the day rollover
            log.warning("Scorecard failed: %s", exc)

        from services.utils.chart import generate_equity_curve
        chart_path = generate_equity_curve(
            db.get_snapshots(args.account), args.account,
            LOG_DIR / f"equity_{args.account}.png",
        )
        if chart_path:
            telegram.send_photo(chart_path, caption=f"📈 Equity Curve — {args.account} ({runner.today})")

        runner.trades = []  # reset for the next trading day's CSV/summary

    heartbeat.beat("stopped")
    telegram.send(f"🔴 <b>DRYRUN DAEMON STOPPED</b> — {market_mode} (manual stop)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Top-level safety net: SIGTERM (systemctl stop) raises KeyboardInterrupt
        # from _handle_sigterm and it can land anywhere, including inside a
        # Telegram network call that only catches Exception (KeyboardInterrupt
        # is a BaseException, so it isn't swallowed there) -- without this,
        # that produces an ugly uncaught traceback instead of a clean exit.
        # The shutdown is still correct either way; this just makes it tidy.
        print(f"\n{YL}Shutting down.{R}", flush=True)
        heartbeat.beat("stopped")
        try:
            telegram.send("🔴 <b>DRYRUN DAEMON STOPPED</b> (shutdown signal)")
        except BaseException:
            # BaseException, not Exception: a second SIGTERM landing mid-send
            # raises KeyboardInterrupt again, which `except Exception` does
            # NOT catch -- that's what produced the traceback on restart.
            # _handle_sigterm is now one-shot so this is belt-and-suspenders.
            pass
        sys.exit(0)
