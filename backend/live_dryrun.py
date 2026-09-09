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
import math
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from broker.upstox_broker import UpstoxBroker
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
from utils.logger import get_logger
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
        logs_dir = Path(__file__).parent / "logs"
        logs_dir.mkdir(exist_ok=True)
        self.log_path = logs_dir / f"dryrun_{self.today}.csv"

    # ---- scan ---------------------------------------------------------------
    def scan(self) -> list[dict]:
        now = datetime.now(IST)
        signals = []
        for sym in self.symbols:
            if sym in self.positions:
                self._maybe_exit(sym, now)
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

                # Allow full MCX hours (09:00 - 22:45 IST) or prime evening session (18:30 - 22:00 IST)
                if mins < 30 or mins > 825:
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

                sdist = max(1.4 * atr, 0.0035 * entry)
                if sdist <= 0 or entry <= 0:
                    continue

                direction = None
                # Calibrated 70%+ Win Rate Rules:
                # Long: ML Prob >= 0.54, ADX >= 20, +DI > -DI, EMA slope > 0.01%, ORB breakout >= 0.05%, VWAP >= 0.05%, Vol Surge >= 1.10x
                if p_up >= 0.54 and adx >= 20 and dmp > dmn and ema_s > 0.010 and orb_h_dist >= 0.05 and vwap_d >= 0.05 and vol_s >= 1.10:
                    direction = "long"
                # Short: ML Prob <= 0.44, ADX >= 20, -DI > +DI, EMA slope < -0.01%, ORB breakdown <= -0.05%, VWAP <= -0.05%, Vol Surge >= 1.10x
                elif self.direction_filter != "long" and p_up <= 0.44 and adx >= 20 and dmn > dmp and ema_s < -0.010 and orb_l_dist <= -0.05 and vwap_d <= -0.05 and vol_s >= 1.10:
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
                tp = round(entry + 1.20 * sdist * d, 2)
                be = round(entry + 0.35 * sdist * d, 2)

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
        else:
            # Trail stop & Breakeven Lock
            armed_before = pos["armed_be"]
            stop_before = pos["current_stop"]
            if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
                pos["armed_be"] = True
                lock_buffer = 0.0008 if self.is_commodity else 0.0025
                pos["current_stop"] = pos["entry_price"] + lock_buffer * pos["entry_price"] * d
            if pos["armed_be"]:
                pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
                trail_mult = 0.40 if self.is_commodity else TRAIL_DIST_MULT
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
        return

    token = args.token or _load_token()
    if not token:
        print(f"{RED}ERROR: No ACCESS_TOKEN. Set in backend/.env or pass --token.{R}")
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


    now = datetime.now(IST)
    if args.commodity:
        mopen  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
        mclose = now.replace(hour=23, minute=30, second=0, microsecond=0)
    else:
        mopen  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
        mclose = now.replace(hour=15, minute=30, second=0, microsecond=0)

    print(f"\n{BOLD}{CY}{'='*75}{R}")
    print(f"{BOLD}{WH}  LIVE PAPER-TRADING DRY RUN  --  {market_mode}{R}")
    print(f"  {GY}Capital:{R} {WH}₹{runner.capital:,.0f}{R}  "
          f"{GY}Risk:{R} {WH}{args.risk_pct}%{R}  "
          f"{GY}Leverage:{R} {MG}{args.leverage}x{R}  "
          f"{GY}Direction:{R} {CY}{direction_mode.upper()}{R}  "
          f"{GY}Interval:{R} {YL}{args.interval}s{R}  "
          f"{GY}Hours:{R} {WH}{mopen.strftime('%H:%M')}–{mclose.strftime('%H:%M')} IST{R}")
    print(f"  {GY}Database:{R} {WH}{db.db_path}{R}")
    print(f"  {GY}Symbols ({len(symbols)}):{R} {WH}{', '.join(symbols[:8])}{'...' if len(symbols)>8 else ''}{R}")
    print(f"  {GY}Log:{R} {WH}{runner.log_path}{R}")
    print(f"{BOLD}{CY}{'='*75}{R}\n")

    telegram.send(
        f"🚀 <b>DRY RUN STARTED</b> — {market_mode}\n"
        f"Capital: ₹{runner.capital:,.0f}  Risk: {args.risk_pct}%  Leverage: {args.leverage}x  "
        f"Direction: {direction_mode.upper()}\n"
        f"Symbols: {', '.join(symbols)}"
    )

    if now < mopen:
        wait = int((mopen - now).total_seconds())
        print(f"  {YL}Market opens in {wait//60}m {wait%60}s — waiting...{R}")
        time.sleep(wait)

    scan_n = 0
    while datetime.now(IST) <= mclose:
        scan_n += 1
        now = datetime.now(IST)
        print(f"\n{GY}-- Scan #{scan_n} @ {now.strftime('%H:%M:%S')} IST  "
              f"| Paper Balance: {WH}₹{runner.capital:,.2f}{GY} --{R}", flush=True)
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
            break
        except Exception as exc:
            log.error("Scan error: %s", exc, exc_info=True)
            telegram.alert_error(f"Scan #{scan_n} ({market_mode})", exc)

        nxt = datetime.now(IST) + timedelta(seconds=args.interval)
        if nxt > mclose:
            break
        secs = max(1, (nxt - datetime.now(IST)).total_seconds())
        print(f"  {GY}Next scan in {secs:.0f}s...{R}", flush=True)
        time.sleep(secs)

    # EOD summary
    print(f"\n{BOLD}{CY}{'='*75}{R}")
    print(f"{BOLD}{WH}  DRY RUN COMPLETED — DATABASE SUMMARY{R}")
    db.print_dashboard(args.account)
    runner._save()

    total_pnl = sum(t.get("net_pnl", 0) for t in runner.trades)
    telegram.send(
        f"🏁 <b>DRY RUN COMPLETED</b> — {market_mode}\n"
        f"Trades: {len(runner.trades)}  Total PnL: ₹{total_pnl:+,.2f}\n"
        f"Final Balance: ₹{runner.capital:,.2f}"
    )


if __name__ == "__main__":
    main()
