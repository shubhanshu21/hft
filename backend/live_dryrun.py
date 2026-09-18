#!/usr/bin/env python3
"""
live_dryrun.py — Paper-trading dry run using LIVE Upstox 5-minute candles & SQLite DB.

Runs 24/7 (MCX 09:00-23:30 IST, NSE currency 09:00-17:00 IST -- both handled
internally) and fires the EXACT SAME strategy logic as backtest_commodity.py
/ backtest_currency.py, but:
  - Places ZERO real orders in live broker (dry_run=True on Upstox broker)
  - Places & tracks VIRTUAL ORDERS in SQLite database (orders, positions, trades, snapshots)
  - Itemizes every fee (Brokerage, STT, Stamp Duty, Exchange Txn, SEBI, GST, Slippage)
  - Updates account capital and portfolio equity curves in real time
  - Saves both SQLite records and daily CSV logs

Usage:
    # Run paper trading during market hours
    python3 live_dryrun.py

    # View current DB PnL, Capital, Open Positions & Order History
    python3 live_dryrun.py --report

    # Reset paper trading DB with custom capital
    python3 live_dryrun.py --reset-db --capital 200000

    # Custom settings
    python3 live_dryrun.py --capital 100000 --risk-pct 5.0 --leverage 4.0
"""
from __future__ import annotations

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
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from broker.upstox_broker import UpstoxBroker, token_invalid_event
from database import TradingDB
from strategy.commodity_costs import compute_mcx_commodity_costs, COMMODITY_SPECS, get_contract_multiplier
from strategy.currency_costs import (
    compute_ncd_currency_costs, CURRENCY_SPECS,
    get_contract_multiplier as get_currency_contract_multiplier,
)
from strategy.entry_signal import compute_entry_signal, is_currency as _is_currency

# Per-symbol risk-per-trade override (falls back to --risk-pct/DRYRUN_RISK_PCT
# for anything not listed). SILVER re-added 2026-09-18 at half the account
# default: its recalibrated thresholds passed a real train/test OOS split
# (unlike crude's current thresholds, which didn't -- see
# backtest_commodity.py's ENTRY_THRESHOLDS["crude"] comment) but only on a
# single split with a meaningfully higher test-window drawdown (35.1%) than
# gold's (~7%) -- sized down until it has real live experience behind it,
# not treated as equally trusted as gold/crude yet.
_SYMBOL_RISK_PCT_OVERRIDE = {"SILVER": 5.0}

from broker.instruments import build_mcx_commodity_map, build_currency_map, ensure_master, get_instrument_key
from utils.logger import get_logger, setup_logger
from utils import telegram
from utils.market_holidays import get_trading_holidays

log = get_logger("live_dryrun")

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_SCAN_INTERVAL_SECONDS = 60  # 1-minute scan for fast SL/TP trailing & new bar detection
DEFAULT_RISK_PCT = 5.0
DEFAULT_LEVERAGE = 4.0

def _build_symbol_map() -> dict[str, str]:
    """Resolves the current nearest-expiry instrument_key for every MCX
    commodity + NSE currency base symbol. MCX/currency contracts are
    monthly-expiry (see real_commodity_data.py/real_currency_data.py
    docstrings) -- calling this again after a contract has rolled picks up
    the new one automatically, since build_mcx_commodity_map()/
    build_currency_map() always select whichever expiry is nearest >=
    today at call time. Must be re-called periodically (see main()'s
    day-rollover loop) -- a value cached once at process startup would
    silently keep resolving an expired instrument_key for as long as the
    process runs without restarting."""
    try:
        return {**build_mcx_commodity_map(), **build_currency_map()}
    except Exception as _e:
        log.warning("Could not load Upstox master; falling back to hardcoded keys: %s", _e)
        return {
            **build_mcx_commodity_map(),
            **build_currency_map(),
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
    lock_dir = Path(__file__).parent / "data"
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
        from config import UpstoxConfig
        if UpstoxConfig.ACCESS_TOKEN:
            return UpstoxConfig.ACCESS_TOKEN
    except Exception:
        pass

    # 2. Check cache/upstox_token.json
    cache_path = Path(__file__).parent / "cache" / "upstox_token.json"
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
    env = Path(__file__).parent / ".env"
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
        from auth.upstox_auto_login import ensure_fresh_upstox_token
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


# ---------------------------------------------------------------------------
# Core Paper Trading Runner
# ---------------------------------------------------------------------------

class DryRunner:
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
        # Diagnostic finding 2026-09-10: on both the real ~1-month archive AND
        # a freshly-regenerated copy of the original synthetic dataset (using
        # the correctly-sized, non-overfit model), dropping the p_up
        # condition and keeping every other rule-based filter outperformed
        # keeping it -- consistently, by ~4-5pts of win rate on both. The
        # model doesn't have enough real data yet to be a net-positive
        # filter. Toggle via DRYRUN_USE_ML_FILTER in .env once that changes
        # (e.g. after the real archive has grown substantially) -- see
        # conversation/git history for the full comparison.
        # Re-verified 2026-09-18 after retraining all commodity models fresh
        # (fixing an unrelated 28-vs-33-feature schema incompatibility that
        # was making every predict_proba() call silently fall back to 0.50
        # anyway) -- precision-at-threshold is still poor: crude 28.1%@0.55
        # (best of the lot), gold 1.9%@0.55 (worse than random), silver 3.1%.
        # Same conclusion holds; still not enough real data for ML to add
        # value over the rule-based filter alone.
        self.use_ml_filter = os.environ.get("DRYRUN_USE_ML_FILTER", "true").lower() in ("1", "true", "yes")

        # Load commodity ML models
        self.commodity_models = {}
        from pathlib import Path
        import pickle
        mod_dir = Path(__file__).parent / "cache" / "commodity_models"
        for s in self.symbols:
            p = mod_dir / f"lgb_{s.lower()}.pkl"
            if p.exists():
                try:
                    with open(p, "rb") as f:
                        self.commodity_models[s] = pickle.load(f)
                except Exception:
                    pass

        # Sync account in SQLite
        self.db.init_account(account_id=self.account_id, capital=self.capital,
                             leverage=self.leverage, risk_pct=self.risk_pct)
        acct = self.db.get_account(self.account_id)
        if acct:
            self.capital = acct["current_capital"]


        # Restore open positions from DB if any
        self.positions: dict[str, dict] = {}
        for p in self.db.get_open_positions(self.account_id):
            self.positions[p["symbol"]] = {
                "position_id": p["position_id"],
                "symbol": p["symbol"],
                "direction": p["direction"],
                "entry_price": p["entry_price"],
                "qty": p["qty"],
                "sl": p["current_stop"],
                "tp": p["target_price"],
                "be": p["breakeven_price"],
                "current_stop": p["current_stop"],
                "best_price": p["best_price"],
                "armed_be": bool(p["armed_be"]),
                "entry_time": datetime.fromisoformat(p["entry_time"]) if "T" in p["entry_time"] else datetime.now(IST),
                "stop_dist": abs(p["entry_price"] - p["current_stop"]),
                "trade_value": p["qty"] * p["entry_price"],
                "p_up": 0.5, "rsi": 50.0, "vwap_dist_pct": 0.0, "ema_slope_pct": 0.0,
            }

        self.trades: list[dict] = []
        self.today = date.today().isoformat()

        # Full-day vs evening-only trading window -- see backtest_commodity.py's
        # us_session_only for the matching backtest flag/comparison. Switched
        # to full-session by default 2026-09-17 per user decision: real
        # backtest on the full 2022-2026 archive showed full session more
        # than doubles net PnL (+Rs68,097 vs +Rs30,248) at the cost of a
        # lower win rate (65.8% vs 70.6%) and higher max DD (8.51% vs 6.51%)
        # from 3x more (noisier, more expensive) daytime trades. Toggle via
        # DRYRUN_FULL_SESSION in .env -- keep BACKTEST_FULL_SESSION in sync
        # so backtest and live dryrun match, same convention as
        # DRYRUN_USE_ML_FILTER/BACKTEST_USE_ML_FILTER above.
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
        # hitting a bad regime, see backtest_commodity.py's ENTRY_THRESHOLDS
        # comment) doesn't halt entries account-wide for symbols that are fine.
        # The existing account-wide switch above still exists as the final
        # backstop for a bad day across the whole book.
        self.symbol_daily_pnl: dict[str, float] = {s: 0.0 for s in self.symbols}
        self.symbol_kill_switch: dict[str, bool] = {s: False for s in self.symbols}

        # Real bid-ask spread sampling (added 2026-09-18): every cost model in
        # this project assumes a flat half-tick-per-leg slippage guess, never
        # measured against a real order book. A one-off check found real
        # spreads running 2x-29x wider than that assumption across every live
        # symbol at that moment (CRUDEOILM 4x, GOLDM 25x, USDINR 3x, EURINR 2x,
        # GBPINR 29x) -- consistent enough across symbols to be a real signal,
        # not a fluke, but one snapshot isn't a distribution. This logs a real
        # sample (throttled to once per SPREAD_SAMPLE_INTERVAL_MIN per symbol,
        # not every scan, to stay well under API rate limits) to
        # logs/spread_samples.csv so a genuine empirical slippage model can
        # eventually replace the flat guess -- accumulates automatically as
        # this daemon runs, no separate job needed.
        self.spread_sample_interval_min = float(os.environ.get("SPREAD_SAMPLE_INTERVAL_MIN", "5"))
        self._last_spread_sample: dict[str, datetime] = {}
        self.spread_log_path = Path(__file__).parent / "logs" / "spread_samples.csv"

        # Manual Telegram kill switch -- separate from the automatic daily-loss
        # one above. Loaded from disk so a "stop" sent before a restart is
        # still honored after it.
        self.trading_enabled = self._load_trading_enabled()
        logs_dir = Path(__file__).parent / "logs"
        logs_dir.mkdir(exist_ok=True)
        self.log_path = logs_dir / f"dryrun_{self.today}.csv"

    # ---- real spread sampling (see __init__'s comment) -----------------------
    def _maybe_sample_spread(self, sym: str, now: datetime) -> None:
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

            # Entry decision delegated to strategy.entry_signal.compute_entry_signal
            # -- the single shared function live_trading.py's LiveTrader also calls,
            # eliminating what used to be near-identical logic hand-duplicated in
            # both files (the exact "keep two files in sync by hand" drift risk
            # ENTRY_THRESHOLDS' own extraction eliminated one layer up, on 2026-09-18).
            candles = _fetch_candles(self.broker, sym, self.today)
            sym_risk_pct = _SYMBOL_RISK_PCT_OVERRIDE.get(sym.upper(), self.risk_pct)
            sig_result = compute_entry_signal(
                sym, candles, SYMBOL_MAP.get(sym), self.commodity_models, self.use_ml_filter,
                self.full_session, self.direction_filter, self.capital, sym_risk_pct, self.leverage,
            )
            if not sig_result:
                continue

            is_curr = _is_currency(sym)
            direction = sig_result["direction"]
            entry = sig_result["entry_price"]
            sl, tp, be = sig_result["sl"], sig_result["tp"], sig_result["be"]
            lots = sig_result["lots"]
            sdist = sig_result["stop_dist"]
            p_up, rsi = sig_result["p_up"], sig_result["rsi"]
            adx, vol_s = sig_result["adx"], sig_result["vol_surge"]
            vwap_d, ema_s = sig_result["vwap_dist_pct"], sig_result["ema_slope_pct"]

            multiplier = get_currency_contract_multiplier(sym) if is_curr else get_contract_multiplier(sym)
            qty = lots
            lot_size = CURRENCY_SPECS.get(sym.upper(), {}).get("lot_size", 1000) if is_curr \
                else COMMODITY_SPECS.get(sym, {}).get("lot_size", 1)
            trade_val = lots * lot_size * entry

            ts_tag = now.strftime('%Y%m%d_%H%M%S')
            pos_id = f"POS_MCX_{ts_tag}_{sym}"
            entry_order_id = f"ORD_E_MCX_{ts_tag}_{sym}"

            # 1. Virtual Order & Position in DB
            order_side = "BUY" if direction == "long" else "SELL"
            self.db.place_order(
                order_id=entry_order_id,
                symbol=sym,
                direction=order_side,
                intent="ENTRY",
                order_type="MARKET",
                qty=lots,
                requested_price=entry,
                fill_price=entry,
                status="FILLED",
                tag="DRY_RUN_MCX_ENTRY",
                account_id=self.account_id
            )

            self.db.open_position(
                position_id=pos_id,
                symbol=sym,
                direction=direction,
                qty=lots,
                entry_price=entry,
                current_stop=sl,
                target_price=tp,
                breakeven_price=be,
                account_id=self.account_id
            )

            sig = {
                "position_id": pos_id,
                "entry_order_id": entry_order_id,
                "time": now.strftime("%H:%M:%S"),
                "symbol": sym,
                "direction": direction,
                "entry_price": round(entry, 2),
                "sl": sl, "tp": tp, "be": be,
                "qty": lots, "lots": lots,
                "trade_value": round(trade_val, 2),
                "stop_dist": round(sdist, 4),
                "p_up": round(p_up, 3), "rsi": round(rsi, 1),
                "vwap_dist_pct": round(vwap_d, 4),
                "ema_slope_pct": round(ema_s, 4),
                "adx": round(adx, 1),
                "vol_surge": round(vol_s, 2),
            }
            signals.append(sig)
            self.positions[sym] = {
                **sig, "entry_time": now,
                "current_stop": sl, "best_price": entry, "armed_be": False,
            }
        return signals

    # ---- exit check ---------------------------------------------------------
    def _maybe_exit(self, sym: str, now: datetime):
        pos = self.positions[sym]
        candles = _fetch_candles(self.broker, sym, self.today)
        if not candles:
            return
        latest = candles[-1]
        high, low = float(latest["high"]), float(latest["low"])
        d = 1 if pos["direction"] == "long" else -1
        fav = high if d == 1 else low
        adv = low  if d == 1 else high

        # NSE currency derivatives close at 17:00 IST -- square off at 16:50
        # (matches backtest_currency.py's m_open>=470 cutoff exactly), well
        # ahead of MCX's own close. Upstox RMS auto-squareoff for MCX
        # Commodities is 22:50 IST; square off 5 mins prior (22:45) to avoid
        # RMS broker penalty charges. These two were previously conflated
        # under a single is_commodity flag (both MCX and currency share this
        # DryRunner instance) -- a currency position would incorrectly sit
        # open until 22:45 waiting on stale/absent candles from a market
        # that already closed at 17:00. Fixed 2026-09-18, gated per-symbol.
        if _is_currency(sym):
            market_close = datetime.now(IST).replace(hour=16, minute=50, second=0, microsecond=0)
        else:
            market_close = datetime.now(IST).replace(hour=22, minute=45, second=0, microsecond=0)

        exit_p = None; reason = None
        if (fav >= pos["tp"] if d == 1 else fav <= pos["tp"]):
            exit_p = pos["tp"];           reason = "take_profit"
        elif (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
            exit_p = pos["current_stop"]; reason = "be_stop" if pos["armed_be"] else "initial_stop"
        elif (now - pos["entry_time"]).total_seconds() >= 80 * 60:
            exit_p = float(latest["close"]); reason = "timeout_exit"
        elif now >= market_close:
            exit_p = float(latest["close"]); reason = "eod_squareoff"

        if exit_p is not None:
            self._close_position(sym, pos, exit_p, reason, now)
        else:
            # Trail stop & Breakeven Lock
            armed_before = pos["armed_be"]
            stop_before = pos["current_stop"]
            if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
                pos["armed_be"] = True
                lock_buffer = 0.0020  # matches backtest_commodity.py's BE_LOCK_BUFFER_PCT (backtested 2026-09-10 improvement)
                pos["current_stop"] = pos["entry_price"] + lock_buffer * pos["entry_price"] * d
            if pos["armed_be"]:
                pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
                trail_mult = 0.30
                trail = pos["best_price"] - trail_mult * pos["stop_dist"] * d
                pos["current_stop"] = (max(pos["current_stop"], trail) if d == 1
                                       else min(pos["current_stop"], trail))

            if pos["current_stop"] != stop_before or pos["armed_be"] != armed_before:
                self.db.update_position_stop(
                    position_id=pos["position_id"],
                    current_stop=pos["current_stop"],
                    best_price=pos["best_price"],
                    armed_be=pos["armed_be"]
                )

    def _close_position(self, sym: str, pos: dict, exit_p: float, reason: str, now: datetime):
        """Shared exit path for SL/TP/timeout/EOD exits AND the manual Telegram
        kill switch (force_exit_all) -- one place that writes the DB order/
        position/trade/snapshot records, updates capital, and alerts."""
        # 1. Calculate Exact Itemized Costs
        if _is_currency(sym):
            cost_info = compute_ncd_currency_costs(sym, pos["direction"], pos["entry_price"], exit_p, pos.get("lots", pos.get("qty", 1)))
        else:
            cost_info = compute_mcx_commodity_costs(sym, pos["direction"], pos["entry_price"], exit_p, pos.get("lots", pos.get("qty", 1)))

        net_pnl = cost_info["net"]
        gross_pnl = cost_info["gross"]
        self.capital += net_pnl

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
            p_up=pos.get("p_up"),
            rsi=pos.get("rsi"),
            vwap_dist_pct=pos.get("vwap_dist_pct"),
            ema_slope_pct=pos.get("ema_slope_pct"),
            account_id=self.account_id
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
        return Path(__file__).parent / "data" / f".{self.account_id}_control.json"

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
            candles = _fetch_candles(self.broker, sym, self.today)
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
        with open(self.log_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(self.trades[0].keys()))
            w.writeheader(); w.writerows(self.trades)


# ---------------------------------------------------------------------------
# CLI & Terminal Output
# ---------------------------------------------------------------------------

def _print_sig(sig: dict, cap: float):
    col = GR if sig["direction"] == "long" else RED
    arrow = "LONG  " if sig["direction"] == "long" else "SHORT "
    print(f"\n  {BOLD}VIRTUAL ORDER PLACED  {WH}{sig['symbol']:12s}{R} {col}{arrow}{R} "
          f"@ {YL}₹{sig['entry_price']:.2f}{R}")
    print(f"     Order ID: {CY}{sig.get('entry_order_id', 'N/A')}{R} | Position ID: {CY}{sig.get('position_id', 'N/A')}{R}")
    print(f"     SL: {RED}₹{sig['sl']:.2f}{R}  TP: {GR}₹{sig['tp']:.2f}{R}  "
          f"BE: ₹{sig['be']:.2f}")
    print(f"     Qty: {sig['qty']}  Val: ₹{sig['trade_value']:,.0f}  "
          f"Score: {sig['p_up']:.3f}  RSI: {sig['rsi']:.1f}")
    print(f"     VWAP: {sig['vwap_dist_pct']:+.3f}%  EMA: {sig['ema_slope_pct']:+.4f}%  "
          f"Cap now: ₹{cap:,.2f}")
    print(f"     {GY}[VIRTUAL EXECUTION -> RECORDED IN SQLITE DB - NO REAL BROKER ORDER]{R}")
    telegram.alert_entry(sig, cap)


def main():
    # Wires up the rotating file handler on the ROOT logger (name="") so
    # every module's get_logger(__name__) call -- live_dryrun's own, plus
    # broker/upstox_broker.py, auth/upstox_auto_login.py, etc. -- actually
    # reaches a persistent log file via propagation, not just stdout/stderr
    # (which is all journalctl captures; nothing was ever written to
    # logs/ before this, since get_logger() alone never attaches handlers).
    setup_logger("", log_file=str(Path(__file__).parent / "logs" / "live_dryrun.log"))

    ap = argparse.ArgumentParser(description="Live paper-trading dry run with SQLite virtual order & PnL tracking")
    ap.add_argument("--token",     default=None,           help="Upstox access token")
    ap.add_argument("--capital",   type=float, default=100_000, help="Paper capital Rs (default 1,00,000)")
    ap.add_argument("--risk-pct",  type=float, default=DEFAULT_RISK_PCT, help="Risk %% per trade")
    ap.add_argument("--leverage",  type=float, default=DEFAULT_LEVERAGE, help="MIS leverage (default 4.0)")
    ap.add_argument("--symbols",   nargs="+",  default=None,        help="MCX commodity / NSE currency symbols to trade")
    ap.add_argument("--db",        default=None,           help="Path to SQLite DB (default: data/paper_trading.db)")
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
        from utils.chart import generate_equity_curve
        chart_path = generate_equity_curve(
            db.get_snapshots(args.account), args.account,
            Path(__file__).parent / "logs" / f"equity_{args.account}.png",
        )
        if chart_path:
            print(f"{GY}Equity curve chart saved: {WH}{chart_path}{R}")
        return

    _lock_fh = _acquire_process_lock(args.account)  # held for process lifetime; see _acquire_process_lock

    token = args.token or _load_token()

    # Startup token validation: a cached/env token being *present* doesn't
    # mean it's still *valid* (Upstox tokens expire daily) -- this system
    # trades real money on a schedule, so silently starting with a stale
    # token and only discovering it hours later via the in-loop check is not
    # acceptable. Validate now and force a refresh if needed, before the
    # daemon ever claims to be running.
    from auth.upstox_auto_login import _token_is_valid, ensure_fresh_upstox_token
    from config import UpstoxConfig as _UC
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
    market_mode = "MCX COMMODITIES / NSE CURRENCY"

    direction_mode = "long" if args.long_only else args.direction
    runner = DryRunner(broker, db, symbols, args.capital, args.risk_pct, args.leverage,
                       account_id=args.account, direction_filter=direction_mode)


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

    from config import UpstoxConfig
    token_check_interval_sec = int(os.environ.get("TOKEN_CHECK_INTERVAL_MIN", "15")) * 60
    last_token_check = time.monotonic()

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
        mopen  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
        mclose = now.replace(hour=23, minute=30, second=0, microsecond=0)

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

            # Refresh instrument_key resolution once per trading-day rollover --
            # not just at process startup -- so a monthly contract expiry is
            # picked up within a day instead of depending on the weekly
            # finetune job's incidental restart to notice it (found 2026-09-18).
            global SYMBOL_MAP
            fresh_map = _build_symbol_map()
            changed = {k: v for k, v in fresh_map.items() if SYMBOL_MAP.get(k) != v}
            if changed:
                log.info("Instrument key(s) rolled over: %s", changed)
                telegram.send(f"🔄 <b>CONTRACT ROLLOVER</b> — {len(changed)} instrument key(s) updated: "
                              f"{', '.join(changed.keys())}")
            SYMBOL_MAP.clear()
            SYMBOL_MAP.update(fresh_map)

        runner.today = mopen.strftime("%Y-%m-%d")
        runner.log_path = Path(__file__).parent / "logs" / f"dryrun_{runner.today}.csv"

        now = datetime.now(IST)
        if now < mopen:
            wait = int((mopen - now).total_seconds())
            print(f"  {YL}Market opens {mopen.strftime('%Y-%m-%d %H:%M')} IST "
                  f"(in {wait//3600}h {(wait%3600)//60}m) — sleeping...{R}", flush=True)
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
                    cmd = u["text"].strip().lower()
                    if cmd in ("/stop", "stop", "stop trading"):
                        closed = runner.stop_trading(now)
                        print(f"  {BOLD}{RED}!! TRADING STOPPED via Telegram -- {closed} position(s) force-closed !!{R}", flush=True)
                        telegram.send(f"🛑 <b>TRADING STOPPED</b> (manual) — {closed} open position(s) force-closed.\n"
                                      f"New entries halted until you send /start.")
                    elif cmd in ("/start", "start", "start trading"):
                        runner.start_trading()
                        print(f"  {BOLD}{GR}TRADING STARTED via Telegram{R}", flush=True)
                        telegram.send("🟢 <b>TRADING STARTED</b> (manual) — resuming normal entries.")
            except Exception as exc:
                log.error("Telegram command poll raised: %s", exc, exc_info=True)

            # Token refresh: proactively every TOKEN_CHECK_INTERVAL_MIN, or
            # immediately if the broker's 401 circuit breaker trips. A 24/7
            # process would otherwise never notice its daily token expired.
            if token_invalid_event.is_set() or (time.monotonic() - last_token_check) >= token_check_interval_sec:
                last_token_check = time.monotonic()
                token_invalid_event.clear()
                if UpstoxConfig.auto_login_configured():
                    try:
                        from auth.upstox_auto_login import ensure_fresh_upstox_token
                        old_token = UpstoxConfig.ACCESS_TOKEN
                        new_token = ensure_fresh_upstox_token(on_token_refreshed=broker.set_access_token)
                        if new_token is None:
                            log.error("Token refresh failed — trading may be blind until fixed.")
                            telegram.send("🔴 <b>TOKEN REFRESH FAILED</b> — auto-login attempt failed. "
                                          "Run `python3 -m auth.upstox_auth` manually or check credentials.")
                        elif new_token != old_token:
                            # An actual refresh happened (not just a no-op
                            # echo of an already-valid token) -- worth a
                            # visibility alert since this system trades real
                            # money and a silent credential rotation is
                            # exactly the kind of thing to have a record of.
                            log.info("Token refreshed via scheduled check.")
                            telegram.send("🔑 <b>TOKEN REFRESHED</b> (scheduled check) — dry run continuing normally.")
                    except Exception as exc:
                        log.error("Token refresh raised: %s", exc, exc_info=True)
                        telegram.alert_error("Token refresh", exc)
                elif not UpstoxConfig.ACCESS_TOKEN:
                    telegram.send("🔴 <b>TOKEN INVALID</b> — auto-login not configured. "
                                   "Run `python3 -m auth.upstox_auth` manually.")

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

        from utils.chart import generate_equity_curve
        chart_path = generate_equity_curve(
            db.get_snapshots(args.account), args.account,
            Path(__file__).parent / "logs" / f"equity_{args.account}.png",
        )
        if chart_path:
            telegram.send_photo(chart_path, caption=f"📈 Equity Curve — {args.account} ({runner.today})")

        runner.trades = []  # reset for the next trading day's CSV/summary

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
        try:
            telegram.send("🔴 <b>DRYRUN DAEMON STOPPED</b> (shutdown signal)")
        except BaseException:
            # BaseException, not Exception: a second SIGTERM landing mid-send
            # raises KeyboardInterrupt again, which `except Exception` does
            # NOT catch -- that's what produced the traceback on restart.
            # _handle_sigterm is now one-shot so this is belt-and-suspenders.
            pass
        sys.exit(0)
