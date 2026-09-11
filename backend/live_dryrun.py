#!/usr/bin/env python3
"""
live_dryrun.py — Paper-trading dry run using LIVE Upstox 5-minute candles & SQLite DB.

Runs during market hours (09:15-15:30 IST) and fires the EXACT SAME
strategy logic as backtest_scalper.py, but:
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
import math
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from broker.upstox_broker import UpstoxBroker, token_invalid_event
import pandas_ta_classic as ta
from database import TradingDB
from strategy.costs import (
    TOTAL_ROUND_TRIP_COST_PCT, brokerage_rupees,
    STT_PCT_SELL_SIDE, STAMP_DUTY_PCT_BUY_SIDE,
    EXCHANGE_TXN_PCT_PER_SIDE, SEBI_PCT_PER_SIDE, GST_RATE,
    slippage_pct_per_leg,
)
from strategy.commodity_costs import (
    compute_mcx_commodity_costs, size_commodity_lots, COMMODITY_SPECS, get_contract_multiplier
)
from strategy.commodity_features import (
    compute_commodity_features, COMMODITY_FEATURE_COLUMNS
)
from strategy.features import WARMUP_CANDLES, compute_intraday_bar_features
from strategy.risk import DEFAULT_RISK_PCT, DEFAULT_LEVERAGE
from strategy.screener import DEFAULT_TOP_N
from backtest.engine import (
    HOLD_MINUTES, STOP_VOL_MULT, MIN_STOP_TO_COST_RATIO, MIN_AVG_RANGE_PCT,
    UP_THRESHOLD, DOWN_THRESHOLD, TAKE_PROFIT_MULT, BE_ACTIVATION_MULT,
    TRAIL_DIST_MULT, SESSION_START_MINUTES, SESSION_CUTOFF_MINUTES,
)
from broker.instruments import build_nifty50_map, build_mcx_commodity_map, ensure_master, get_instrument_key
from utils.logger import get_logger, setup_logger
from utils import telegram

log = get_logger("live_dryrun")

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_SCAN_INTERVAL_SECONDS = 60  # 1-minute scan for fast SL/TP trailing & new bar detection

try:
    SYMBOL_MAP: dict[str, str] = {**build_nifty50_map(), **build_mcx_commodity_map()}
except Exception as _e:
    log.warning("Could not load Upstox master; falling back to hardcoded keys: %s", _e)
    SYMBOL_MAP = {
        **build_mcx_commodity_map(),
        "RELIANCE":   "NSE_EQ|INE002A01018", "HDFCBANK":  "NSE_EQ|INE040A01034",
        "ICICIBANK":  "NSE_EQ|INE090A01021", "SBIN":      "NSE_EQ|INE062A01020",
        "INFY":       "NSE_EQ|INE009A01021", "TCS":       "NSE_EQ|INE467B01029",
        "AXISBANK":   "NSE_EQ|INE238A01034", "KOTAKBANK": "NSE_EQ|INE237A01036",
        "LT":         "NSE_EQ|INE018A01030", "BAJFINANCE": "NSE_EQ|INE296A01032",
        "BAJAJFINSV": "NSE_EQ|INE918I01026", "WIPRO":     "NSE_EQ|INE075A01022",
        "HCLTECH":    "NSE_EQ|INE860A01027", "SUNPHARMA": "NSE_EQ|INE044A01036",
        "MARUTI":     "NSE_EQ|INE585B01010", "TATASTEEL": "NSE_EQ|INE081A01020",
        "NTPC":       "NSE_EQ|INE733E01010", "POWERGRID": "NSE_EQ|INE752H01013",
        "ONGC":       "NSE_EQ|INE213A01029", "COALINDIA": "NSE_EQ|INE522F01014",
        "ADANIENT":   "NSE_EQ|INE423A01024", "ADANIPORTS":"NSE_EQ|INE742F01042",
        "HINDALCO":   "NSE_EQ|INE038A01020", "VEDL":      "NSE_EQ|INE205A01025",
        "EICHERMOT":  "NSE_EQ|INE066A01021", "CANBK":     "NSE_EQ|INE476A01022",
        "TITAN":      "NSE_EQ|INE280A01028", "ITC":       "NSE_EQ|INE154A01025",
        "BANKBARODA": "NSE_EQ|INE028A01039", "BEL":       "NSE_EQ|INE263A01024",
        "HAL":        "NSE_EQ|INE066F01020", "INDIGO":    "NSE_EQ|INE646L01027",
        "TATAPOWER":  "NSE_EQ|INE245A01021", "PNB":       "NSE_EQ|INE160A01022",
        "M&M":        "NSE_EQ|INE101A01026",
    }

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


def _to_bars(candles: list[dict]) -> list[dict]:
    out = []
    for c in candles:
        ts = pd.Timestamp(c["timestamp"])
        out.append({
            "open": float(c["open"]), "high": float(c["high"]),
            "low":  float(c["low"]),  "close": float(c["close"]),
            "volume": float(c.get("volume", 0)),
            "_dt": ts,
            "minutes_since_open": (ts.hour * 60 + ts.minute) - (9 * 60 + 15),
        })
    return out


def _size(capital: float, entry: float, stop_dist: float,
          risk_pct: float, leverage: float) -> int:
    if capital <= 0 or entry <= 0 or stop_dist <= 0:
        return 0
    qty_risk   = math.floor(capital * risk_pct / 100 / stop_dist)
    qty_margin = math.floor(capital * leverage / entry)
    return max(0, min(qty_risk, qty_margin))


def compute_itemized_costs(direction: str, entry: float, exit_p: float, qty: int) -> dict:
    """Computes exact itemized costs matching Indian statutory & broker rules."""
    d = 1 if direction == "long" else -1
    ev = qty * entry
    xv = qty * exit_p
    gross = qty * (exit_p - entry) * d

    brok = brokerage_rupees(ev) + brokerage_rupees(xv)
    stt = (xv if d == 1 else ev) * (STT_PCT_SELL_SIDE / 100)
    stamp = (ev if d == 1 else xv) * (STAMP_DUTY_PCT_BUY_SIDE / 100)
    exch = (ev + xv) * (EXCHANGE_TXN_PCT_PER_SIDE / 100)
    sebi = (ev + xv) * (SEBI_PCT_PER_SIDE / 100)
    gst_charges = exch * GST_RATE
    slip = (ev + xv) * (slippage_pct_per_leg(entry) / 100)
    total_costs = brok + stt + stamp + exch + sebi + gst_charges + slip

    return {
        "gross": gross,
        "brokerage": brok,
        "stt": stt,
        "stamp_duty": stamp,
        "exchange_txn": exch,
        "sebi": sebi,
        "gst": gst_charges,
        "slippage": slip,
        "total": total_costs,
        "net": gross - total_costs
    }


# ---------------------------------------------------------------------------
# Core Paper Trading Runner
# ---------------------------------------------------------------------------

class DryRunner:
    def __init__(self, broker: UpstoxBroker, db: TradingDB, symbols: list[str],
                 capital: float, risk_pct: float, leverage: float,
                 account_id: str = "DRYRUN_ACCOUNT",
                 direction_filter: str = "both",
                 is_commodity: bool = False):
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
        self.use_ml_filter = os.environ.get("DRYRUN_USE_ML_FILTER", "true").lower() in ("1", "true", "yes")
        self.is_commodity     = is_commodity

        # Load commodity ML models if in commodity mode
        self.commodity_models = {}
        if self.is_commodity:
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

        # Daily-loss kill switch: halts new entries (existing positions are
        # still managed/exited normally) once today's realized loss hits
        # MAX_DAILY_LOSS_PCT of the capital this process started the day with.
        self.max_daily_loss_pct = float(os.environ.get("MAX_DAILY_LOSS_PCT", "5.0"))
        self.trading_day = self.today
        self.day_start_capital = self.capital
        self.kill_switch_active = False

        # Manual Telegram kill switch -- separate from the automatic daily-loss
        # one above. Loaded from disk so a "stop" sent before a restart is
        # still honored after it.
        self.trading_enabled = self._load_trading_enabled()
        logs_dir = Path(__file__).parent / "logs"
        logs_dir.mkdir(exist_ok=True)
        self.log_path = logs_dir / f"dryrun_{self.today}.csv"

    # ---- scan ---------------------------------------------------------------
    def scan(self) -> list[dict]:
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")
        if today_str != self.trading_day:
            self.trading_day = today_str
            self.day_start_capital = self.capital
            self.kill_switch_active = False

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
            if sym in self.positions:
                self._maybe_exit(sym, now)
                continue
            if self.kill_switch_active or not self.trading_enabled:
                continue
            candles = _fetch_candles(self.broker, sym, self.today)
            if not candles or len(candles) < 25:
                continue

            if self.is_commodity:
                # -----------------------------------------------------------------
                # MCX Commodity Scalper Logic (LightGBM + Microstructure Momentum)
                # -----------------------------------------------------------------
                raw_df = pd.DataFrame(candles)
                feat_df = compute_commodity_features(raw_df, symbol=sym)
                if feat_df is None or len(feat_df) < 25:
                    continue

                t = len(feat_df) - 1   # latest complete 5m bar
                row = feat_df.iloc[t]
                mins = int(row.get("minutes_since_open", 0))

                # US/Evening overlap session only (18:30-22:00 IST) -- matches
                # backtest_commodity.py's us_session_only=True default, which is
                # what the validated backtest results were produced with.
                if mins < 570 or mins > 780:
                    continue

                # Model probability
                model = self.commodity_models.get(sym)
                if model:
                    try:
                        X_feat = feat_df[COMMODITY_FEATURE_COLUMNS].iloc[[t]]
                        p_up = float(model.predict_proba(X_feat)[0, 1])
                    except Exception:
                        p_up = 0.50
                else:
                    p_up = 0.50

                adx = float(row.get("adx", 25.0))
                dmp = float(row.get("dmp", 25.0))
                dmn = float(row.get("dmn", 25.0))
                vol_s = float(row.get("vol_surge_ratio", 1.0))
                vwap_d = float(row.get("vwap_dist_pct", 0.0))
                ema_s = float(row.get("ema_slope_pct", 0.0))
                orb_h_dist = float(row.get("orb_high_dist_pct", 0.0))
                orb_l_dist = float(row.get("orb_low_dist_pct", 0.0))
                rsi = float(row.get("intraday_rsi", 50.0))
                entry = float(row["close"])
                atr = float(row.get("atr", 0.005 * entry))

                # Asset-calibrated parameter profiles (matches backtest_commodity.py)
                is_natgas = "NATGAS" in sym.upper() or "NATURALGAS" in sym.upper()
                min_ml_l = 0.55 if is_natgas else 0.54
                max_ml_s = 0.43 if is_natgas else 0.44
                min_adx = 22.0 if is_natgas else 15.0  # natgas was 19.0 -- matches backtest_commodity.py's ENTRY_THRESHOLDS (backtested 2026-09-11: raising natgas' adx/vol bar cut its trade count 23->11 and flipped it from net-loss to net-profit after costs)
                min_vol = 1.70 if is_natgas else 1.10  # natgas was 1.40 -- same 2026-09-11 backtest
                min_orb = 0.08 if is_natgas else 0.05
                min_vwap = 0.08 if is_natgas else 0.05
                min_stop_pct = 0.0050 if is_natgas else 0.0035

                sdist = max(1.4 * atr, min_stop_pct * entry)
                if sdist <= 0 or entry <= 0:
                    continue

                direction = None
                ml_long_ok = (not self.use_ml_filter) or (p_up >= min_ml_l)
                ml_short_ok = (not self.use_ml_filter) or (p_up <= max_ml_s)
                # Calibrated 70%+ Win Rate Rules (asset-calibrated, matches backtest):
                if ml_long_ok and adx >= min_adx and dmp > dmn and ema_s > 0.010 and orb_h_dist >= min_orb and vwap_d >= min_vwap and vol_s >= min_vol:
                    direction = "long"
                elif self.direction_filter != "long" and ml_short_ok and adx >= min_adx and dmn > dmp and ema_s < -0.010 and orb_l_dist <= -min_orb and vwap_d <= -min_vwap and vol_s >= min_vol:
                    direction = "short"

                if not direction:
                    continue

                if self.direction_filter != "both" and direction != self.direction_filter:
                    continue

                lots = size_commodity_lots(self.capital, entry, sdist, self.risk_pct, sym, self.leverage)
                if lots == 0:
                    continue

                multiplier = get_contract_multiplier(sym)
                qty = lots
                trade_val = lots * (COMMODITY_SPECS.get(sym, {}).get("lot_size", 1)) * entry

                d  = 1 if direction == "long" else -1
                sl = round(entry - sdist * d, 2)
                tp = round(entry + 1.80 * sdist * d, 2)  # was 1.20 -- matches backtest_commodity.py's TAKE_PROFIT_MULT (backtested 2026-09-11: wider target cut brokerage's share of net PnL by diluting the flat per-trade fee over a bigger win)
                be = round(entry + 0.60 * sdist * d, 2)  # was 0.50 -- matches backtest_commodity.py's BE_ACTIVATION_MULT (backtested 2026-09-11 improvement)

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
                    "entry_idx": t,
                    "current_stop": sl, "best_price": entry, "armed_be": False,
                }
            else:
                # -----------------------------------------------------------------
                # NSE Equity Scalper Logic
                # -----------------------------------------------------------------
                df = compute_intraday_bar_features(candles, interval_minutes=5)
                if df is None or len(df) < WARMUP_CANDLES + 2:
                    continue
                df["ema_fast"] = ta.ema(df["close"], length=9)
                df["ema_slope_pct"] = df["ema_fast"].pct_change() * 100

                t = len(df) - 2          # last complete bar
                if t < WARMUP_CANDLES:
                    continue
                row = df.iloc[t]
                mins = int(row.get("minutes_since_open", 0))
                if mins < SESSION_START_MINUTES or mins > SESSION_CUTOFF_MINUTES:
                    continue

                vwap_d = float(row.get("vwap_dist_pct", 0.0))
                ema_s  = float(row.get("ema_slope_pct", 0.0))
                rsi    = float(row.get("intraday_rsi", 50.0))
                mom    = float(row.get("mom_pct", 0.0))
                vwap_s = float(row.get("vwap_slope", 0.0))
                body   = float(row.get("body_ratio", 0.5))

                p = 0.50
                p += 0.08 * (1 if mom > 0 else -1)
                p += 0.06 * (1 if vwap_d > 0 else -1)
                p += 0.05 * (1 if ema_s  > 0 else -1)
                p += 0.05 * (1 if vwap_s > 0 else -1)
                p += 0.04 * (body - 0.5) * 2
                p = max(0.0, min(1.0, p))

                entry = float(row["close"])
                avg_r = float(row.get("avg_range_pct", 0.5)) / 100 * entry
                hold  = max(1, HOLD_MINUTES // 5)
                sdist = STOP_VOL_MULT * avg_r * (hold ** 0.5)
                if avg_r / entry * 100 < MIN_AVG_RANGE_PCT:
                    continue
                if sdist <= 0 or entry <= 0:
                    continue
                if sdist / entry * 100 < MIN_STOP_TO_COST_RATIO * TOTAL_ROUND_TRIP_COST_PCT:
                    continue

                direction = None
                if p >= UP_THRESHOLD and ema_s >= 0.0 and vwap_d >= 0.0 and 45 <= rsi <= 72:
                    direction = "long"
                elif p <= DOWN_THRESHOLD and ema_s <= -0.01 and vwap_d <= -0.01 and 25 <= rsi <= 48:
                    direction = "short"
                if not direction:
                    continue

                if self.direction_filter != "both" and direction != self.direction_filter:
                    continue

                qty = _size(self.capital, entry, sdist, self.risk_pct, self.leverage)
                if qty == 0:
                    continue

                d    = 1 if direction == "long" else -1
                sl   = round(entry - sdist * d, 2)
                tp   = round(entry + TAKE_PROFIT_MULT * sdist * d, 2)
                be   = round(entry + BE_ACTIVATION_MULT * sdist * d, 2)
                tval = round(qty * entry, 2)

                ts_tag = now.strftime('%Y%m%d_%H%M%S')
                pos_id = f"POS_{ts_tag}_{sym}"
                entry_order_id = f"ORD_E_{ts_tag}_{sym}"

                order_side = "BUY" if direction == "long" else "SELL"
                self.db.place_order(
                    order_id=entry_order_id,
                    symbol=sym,
                    direction=order_side,
                    intent="ENTRY",
                    order_type="MARKET",
                    qty=qty,
                    requested_price=entry,
                    fill_price=entry,
                    status="FILLED",
                    tag="DRY_RUN_ENTRY",
                    account_id=self.account_id
                )

                self.db.open_position(
                    position_id=pos_id,
                    symbol=sym,
                    direction=direction,
                    qty=qty,
                    entry_price=entry,
                    current_stop=sl,
                    target_price=tp,
                    breakeven_price=be,
                    account_id=self.account_id
                )

                sig = {
                    "position_id": pos_id,
                    "entry_order_id": entry_order_id,
                    "time": now.strftime("%H:%M:%S"), "symbol": sym,
                    "direction": direction, "entry_price": round(entry, 2),
                    "sl": sl, "tp": tp, "be": be,
                    "qty": qty, "trade_value": tval, "stop_dist": round(sdist, 4),
                    "p_up": round(p, 3), "rsi": round(rsi, 1),
                    "vwap_dist_pct": round(vwap_d, 4), "ema_slope_pct": round(ema_s, 4),
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

        # Upstox RMS auto-squareoff: 15:25 IST for Non-CAS & F&O, 22:50 IST for MCX Commodities.
        # Set algorithmic square-off 5 mins prior (15:20 IST for NSE, 22:45 IST for MCX) to avoid RMS broker penalty charges.
        close_h = 22 if self.is_commodity else 15
        close_m = 45 if self.is_commodity else 20
        market_close = datetime.now(IST).replace(hour=close_h, minute=close_m, second=0, microsecond=0)

        exit_p = None; reason = None
        if (fav >= pos["tp"] if d == 1 else fav <= pos["tp"]):
            exit_p = pos["tp"];           reason = "take_profit"
        elif (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
            exit_p = pos["current_stop"]; reason = "be_stop" if pos["armed_be"] else "initial_stop"
        elif self.is_commodity and (now - pos["entry_time"]).total_seconds() >= 80 * 60:
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
                lock_buffer = 0.0020 if self.is_commodity else 0.0025  # was 0.0008 -- matches backtest_commodity.py's BE_LOCK_BUFFER_PCT (backtested 2026-09-10 improvement)
                pos["current_stop"] = pos["entry_price"] + lock_buffer * pos["entry_price"] * d
            if pos["armed_be"]:
                pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
                trail_mult = 0.30 if self.is_commodity else TRAIL_DIST_MULT
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
        if self.is_commodity:
            cost_info = compute_mcx_commodity_costs(sym, pos["direction"], pos["entry_price"], exit_p, pos.get("lots", pos.get("qty", 1)))
        else:
            cost_info = compute_itemized_costs(pos["direction"], pos["entry_price"], exit_p, pos["qty"])

        net_pnl = cost_info["net"]
        gross_pnl = cost_info["gross"]
        self.capital += net_pnl

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
    telegram.alert_entry(sig)


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
    ap.add_argument("--top-n",     type=int,   default=DEFAULT_TOP_N,    help="Daily screener size")
    ap.add_argument("--symbols",   nargs="+",  default=None,        help="Override screener with fixed symbol list")
    ap.add_argument("--db",        default=None,           help="Path to SQLite DB (default: data/paper_trading.db)")
    ap.add_argument("--account",   default="DRYRUN_ACCOUNT", help="Account identifier")
    ap.add_argument("--commodity", action="store_true",    help="Switch to MCX Commodity scalping mode (09:00-23:30 IST)")
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

    if args.commodity:
        # Energy Contracts (Crude Oil Mini & Natural Gas Mini)
        default_commodities = ["CRUDEOILM", "NATGASMINI"]
        symbols = args.symbols or default_commodities
        market_mode = "MCX COMMODITIES (ENERGY: CRUDE & NATGAS)"
    else:
        symbols = args.symbols or list(SYMBOL_MAP.keys())[:args.top_n]
        market_mode = "5-MIN NSE SCALPER"

    direction_mode = "long" if args.long_only else args.direction
    runner = DryRunner(broker, db, symbols, args.capital, args.risk_pct, args.leverage,
                       account_id=args.account, direction_filter=direction_mode,
                       is_commodity=args.commodity)


    print(f"\n{BOLD}{CY}{'='*75}{R}")
    print(f"{BOLD}{WH}  LIVE PAPER-TRADING DRY RUN (24/7 DAEMON)  --  {market_mode}{R}")
    print(f"  {GY}Capital:{R} {WH}₹{runner.capital:,.0f}{R}  "
          f"{GY}Risk:{R} {WH}{args.risk_pct}%{R}  "
          f"{GY}Leverage:{R} {MG}{args.leverage}x{R}  "
          f"{GY}Direction:{R} {CY}{direction_mode.upper()}{R}  "
          f"{GY}Interval:{R} {YL}{args.interval}s{R}"
          + (f"  {GY}Hours:{R} {WH}09:00–23:30 IST{R}" if args.commodity else f"  {GY}Hours:{R} {WH}09:15–15:30 IST{R}"))
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
    import signal
    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # 24/7 outer loop: market-hours gating lives entirely in this code (not in
    # the process supervisor) -- the process itself waits out nights/weekends
    # and rolls into the next trading day rather than exiting, so systemd (or
    # any supervisor) just needs to keep one long-running process alive.
    stopped_by_user = False
    while not stopped_by_user:
        now = datetime.now(IST)
        if args.commodity:
            mopen  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
            mclose = now.replace(hour=23, minute=30, second=0, microsecond=0)
        else:
            mopen  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
            mclose = now.replace(hour=15, minute=30, second=0, microsecond=0)

        if now > mclose:
            # Today's session is already over -- roll to the next trading day.
            next_day = now + timedelta(days=1)
            while next_day.weekday() == 6:  # Sunday -- Indian markets are shut; NOT a full holiday calendar (Sat/holidays aren't handled -- see runner.scan()'s own session gate as the real safety net)
                next_day += timedelta(days=1)
            mopen  = mopen.replace(year=next_day.year, month=next_day.month, day=next_day.day)
            mclose = mclose.replace(year=next_day.year, month=next_day.month, day=next_day.day)

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

        total_pnl = sum(t.get("net_pnl", 0) for t in runner.trades)
        telegram.send(
            f"🏁 <b>SESSION COMPLETE</b> ({runner.today}) — {market_mode}\n"
            f"Trades today: {len(runner.trades)}  Total PnL: ₹{total_pnl:+,.2f}\n"
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
        except Exception:
            pass
        sys.exit(0)
