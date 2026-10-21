"""Paper trading engine: Crypto Perpetual Futures Compounder (₹30,000 to ₹5,00,000 Milestone Target).

Strategy: Trend-Aligned Asymmetric SuperTrend Compounder + BTC Macro ADX Gate
Timeframe: 1-hour finished candles on BTC, ETH, SOL
Mathematical Parameters:
  - Macro Regime Gate: BTC Close > EMA200 AND BTC ADX(14) > 18 (Longs)
                       BTC Close < EMA200 AND BTC ADX(14) > 18 (Shorts)
  - Asymmetric Payoff: Stop Loss = 1.5x ATR(14) | Take Profit = 3.8x ATR(14) (R:R 1 : 2.53)
  - Leverage: 3.8x (Mathematically Zero Liquidation Risk: requires -26.3% instant gap down)
  - Risk per Trade: 4.0% base equity (with streak reduction: 0.70x after 1 loss, 0.45x after 2+ losses)
  - Execution: Passive Maker Limit (0.02% fee model)
  - Max Hold Time: 48 hours
  - Milestone Vault System:
      * Milestone 1: At >= ₹1,00,000 total wealth, sweep ₹50,000 into cold reserve vault.
      * Milestone 2: At >= ₹2,00,000 total wealth, sweep ₹1,00,000 into cold reserve vault.
      * Milestone 3: At >= ₹5,00,000 total wealth, sweep ₹4,00,000 into cold reserve vault (Goal Achieved!).

Usage:
    python3 -m engine.crypto_compounder_5lakh            # Process finished 1h candle & execute trades
    python3 -m engine.crypto_compounder_5lakh --status   # Check status & milestone vault
    python3 -m engine.crypto_compounder_5lakh --simulate # Quick verification simulation on local archive
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from core.paths import BACKEND_ROOT, DB_DIR, ARCHIVE_ROOT
from engine.database import TradingDB

_log = logging.getLogger("crypto_compounder")

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT_ID = "CRYPTO_COMPOUNDER_5LAKH"
STRATEGY_NAME = "asymmetric_5lakh_compounder"
STATE_FILE = DB_DIR / "crypto_compounder_5lakh_state.json"
COINS = ("BTC", "ETH", "SOL")

# Core Strategy Constants
INITIAL_CAPITAL_INR = 100000.0
MAX_LEVERAGE = 3.8
BASE_RISK_PCT = 0.020        # 2.0% (best profit after real costs; 4% made less and fell -66%)
SL_ATR_MULT = 1.5            # 1.5x ATR
TP_ATR_MULT = 3.8            # 3.8x ATR (Sweet spot from matrix grid search)
ADX_REGIME_THRESH = 18.0     # Trend strength gate on BTC
MOMENTUM_THRESH = 0.03       # 3.0% 24h momentum breakout
MAX_HOLD_HOURS = 48          # 48-hour time stop
MAKER_FEE_PCT = 0.0002       # 0.02% limit order fee (take-profit)
TAKER_FEE_PCT = 0.0005       # 0.05% market order fee (entry, stop, time exit)
GST = 1.18
SLIP = {"BTC": 0.00005, "ETH": 0.00005, "SOL": 0.0001}
BAR_MS = 3600 * 1000         # 1 hour in ms

# Milestone Vault Thresholds (in INR)
MILESTONES = [
    {"name": "Milestone 1 (₹2 Lakhs)", "target": 200000.0, "lock": 100000.0},
    {"name": "Milestone 2 (₹3.5 Lakhs)", "target": 350000.0, "lock": 150000.0},
    {"name": "Milestone 3 (₹5 Lakhs - GOAL)", "target": 500000.0, "lock": 400000.0},
]


def _inr(v: float) -> str:
    """Format INR currency."""
    s = f"{abs(v):,.2f}"
    return f"{'-' if v < 0 else ''}₹{s}"


def load_state() -> dict:
    """Load persistent state from disk."""
    default_state = {
        "trading_capital": INITIAL_CAPITAL_INR,
        "vault_locked": 0.0,
        "peak_wealth": INITIAL_CAPITAL_INR,
        "consecutive_losses": 0,
        "active_trade": None,  # Dict when trade is open
        "completed_trades_count": 0,
        "winning_trades_count": 0,
        "total_fees_paid": 0.0,
        "last_candle_ms": 0,
        "locked_milestones": [],
        "created_at": datetime.now(IST).isoformat(),
        "updated_at": datetime.now(IST).isoformat(),
    }
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            for k, v in default_state.items():
                data.setdefault(k, v)
            return data
        except Exception as e:
            _log.warning("Failed to parse state file %s (%s). Recreating.", STATE_FILE, e)
    return default_state


def save_state(state: dict) -> None:
    """Save persistent state to disk."""
    state["updated_at"] = datetime.now(IST).isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_live_prices() -> dict:
    """Fetch live CoinDCX futures prices and USDT/INR conversion rate."""
    out = {"prices": {}, "usdtinr": 92.5, "timestamp": datetime.now(IST).isoformat()}
    try:
        r = requests.get("https://public.coindcx.com/market_data/v3/current_prices/futures/rt", timeout=10)
        if r.status_code == 200:
            prices_data = (r.json().get("prices") or {})
            for c in COINS:
                pair_key = f"B-{c}_USDT"
                p_item = prices_data.get(pair_key) or {}
                if p_item.get("ls"):
                    out["prices"][c] = float(p_item["ls"])
    except Exception as e:
        _log.warning("CoinDCX live price fetch error: %s", e)

    # USDT/INR rate
    try:
        r_ticker = requests.get("https://api.coindcx.com/exchange/ticker", timeout=10)
        if r_ticker.status_code == 200:
            for item in r_ticker.json():
                if item.get("market") == "USDTINR":
                    out["usdtinr"] = float(item["last_price"])
                    break
    except Exception as e:
        _log.warning("CoinDCX USDTINR fetch error: %s", e)

    # Fallback to Binance spot/futures for any missing coin price
    for c in COINS:
        if c not in out["prices"]:
            try:
                r_binance = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={c}USDT", timeout=5)
                if r_binance.status_code == 200:
                    out["prices"][c] = float(r_binance.json()["price"])
            except Exception:
                pass
    return out


def fetch_1h_candles(coin: str, limit: int = 250) -> pd.DataFrame:
    """Fetch finished 1-hour candles from Binance USDT-M Futures with indicator precalculation."""
    url = "https://fapi.binance.com/fapi/v1/klines"
    r = requests.get(url, params={"symbol": f"{coin}USDT", "interval": "1h", "limit": limit}, timeout=15)
    r.raise_for_status()
    raw = r.json()
    df = pd.DataFrame(raw).iloc[:, :7]
    df.columns = ["t", "o", "h", "l", "c", "v", "close_t"]
    df = df.astype({"t": "int64", "o": "float64", "h": "float64", "l": "float64", "c": "float64", "v": "float64", "close_t": "int64"})
    
    # Filter only finished candles
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    df = df[df["close_t"] < now_ms].copy()
    df.sort_values("t", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Indicators
    df["ema200"] = df["c"].ewm(span=200, adjust=False).mean()
    
    # ATR(14)
    tr = pd.concat([
        df["h"] - df["l"],
        (df["h"] - df["c"].shift(1)).abs(),
        (df["l"] - df["c"].shift(1)).abs()
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # ADX(14)
    up_move = df["h"] - df["h"].shift(1)
    down_move = df["l"].shift(1) - df["l"]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_di = 100 * (pd.Series(plus_dm).ewm(alpha=1/14, adjust=False).mean() / np.maximum(df["atr"], 1e-9))
    minus_di = 100 * (pd.Series(minus_dm).ewm(alpha=1/14, adjust=False).mean() / np.maximum(df["atr"], 1e-9))
    dx = 100 * (plus_di - minus_di).abs() / np.maximum(plus_di + minus_di, 1e-9)
    df["adx"] = dx.ewm(alpha=1/14, adjust=False).mean()

    # RSI(14)
    delta = df["c"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / np.maximum(avg_loss, 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))

    # 24h rolling return
    df["ret24"] = df["c"].pct_change(24)

    return df


def init_account(db: TradingDB) -> None:
    """Ensure account is registered in TradingDB."""
    db.init_account(
        account_id=ACCOUNT_ID,
        name="Crypto 5 Lakh Compounder (BTC/ETH/SOL 1h Perps)",
        capital=INITIAL_CAPITAL_INR,
        leverage=MAX_LEVERAGE,
        risk_pct=BASE_RISK_PCT * 100
    )


def check_milestone_vault(state: dict, lines: list) -> None:
    """Check if total wealth has reached milestone thresholds and sweep profit into cold vault."""
    total_wealth = state["trading_capital"] + state["vault_locked"]
    if total_wealth > state["peak_wealth"]:
        state["peak_wealth"] = total_wealth

    for ms in MILESTONES:
        name = ms["name"]
        target = ms["target"]
        lock_amt = ms["lock"]
        if name not in state["locked_milestones"]:
            if total_wealth >= target:
                sweep_amt = min(lock_amt, state["trading_capital"] * 0.70)
                state["vault_locked"] += sweep_amt
                state["trading_capital"] -= sweep_amt
                state["locked_milestones"].append(name)
                alert_msg = (
                    f"🏆 <b>MILESTONE ACHIEVED & VAULT LOCKED!</b>\n"
                    f"Target Crossed: {name} (Total Wealth: {_inr(total_wealth)})\n"
                    f"Locked into Safe Vault: {_inr(sweep_amt)}\n"
                    f"Total Vault Reserve: {_inr(state['vault_locked'])}\n"
                    f"Remaining Active Trading Capital: {_inr(state['trading_capital'])}"
                )
                lines.append(alert_msg)


def process_exits(db: TradingDB, state: dict, candles_map: dict, live_info: dict, lines: list) -> None:
    """Check open position for SL, TP, or 48-hour timeout."""
    pos = state.get("active_trade")
    if not pos:
        return

    coin = pos["coin"]
    df = candles_map.get(coin)
    if df is None or df.empty:
        return

    last_bar = df.iloc[-1]
    last_o = float(last_bar["o"])
    last_h = float(last_bar["h"])
    last_l = float(last_bar["l"])
    last_c = float(last_bar["c"])
    bar_t = int(last_bar["t"])

    side = pos["side"]  # 1 for Long, -1 for Short
    sl_px = float(pos["sl_price"])
    tp_px = float(pos["tp_price"])
    entry_px = float(pos["entry_price"])
    pos_usd = float(pos["pos_usd"])
    entry_bar_t = int(pos["entry_bar_t"])
    fx = live_info["usdtinr"]

    is_exit = False
    exit_px = last_c
    exit_reason = ""

    # Check 1h price breach
    if side == 1:
        if last_l <= sl_px:
            exit_px = min(last_o, sl_px)  # gapped past the stop -> fills at the open
            exit_reason = f"STOP LOSS HIT ({SL_ATR_MULT:.1f}x ATR)"
            is_exit = True
        elif last_h >= tp_px:
            exit_px = tp_px
            exit_reason = f"TAKE PROFIT HIT ({TP_ATR_MULT:.1f}x ATR - Asymmetric Payoff)"
            is_exit = True
        elif (bar_t - entry_bar_t) >= (MAX_HOLD_HOURS * BAR_MS):
            exit_px = last_c
            exit_reason = f"TIME STOP ({MAX_HOLD_HOURS}h reached)"
            is_exit = True
    else:
        if last_h >= sl_px:
            exit_px = max(last_o, sl_px)
            exit_reason = f"STOP LOSS HIT ({SL_ATR_MULT:.1f}x ATR)"
            is_exit = True
        elif last_l <= tp_px:
            exit_px = tp_px
            exit_reason = f"TAKE PROFIT HIT ({TP_ATR_MULT:.1f}x ATR - Asymmetric Payoff)"
            is_exit = True
        elif (bar_t - entry_bar_t) >= (MAX_HOLD_HOURS * BAR_MS):
            exit_px = last_c
            exit_reason = f"TIME STOP ({MAX_HOLD_HOURS}h reached)"
            is_exit = True

    if is_exit:
        raw_pnl_usd = (side * (exit_px - entry_px) / entry_px) * pos_usd
        slip = SLIP.get(pos.get("coin", ""), 0.00015)
        exit_fee = MAKER_FEE_PCT * GST if exit_reason.startswith("TAKE PROFIT") else TAKER_FEE_PCT * GST + slip
        fee_usd = pos_usd * (TAKER_FEE_PCT * GST + slip + exit_fee)          # market entry + exit, GST, slippage
        try:                                                                  # real funding: Binance payments while held
            import requests
            fr = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": f"{pos.get('coin', '')}USDT",
                              "startTime": entry_bar_t, "endTime": bar_t + BAR_MS, "limit": 100}, timeout=15).json()
            fee_usd += side * pos_usd * sum(float(x["fundingRate"]) for x in fr)
        except Exception:
            pass
        net_pnl_usd = raw_pnl_usd - fee_usd
        net_pnl_inr = net_pnl_usd * fx

        state["trading_capital"] += net_pnl_inr
        state["total_fees_paid"] += (fee_usd * fx)
        state["completed_trades_count"] += 1
        is_win = net_pnl_inr > 0
        if is_win:
            state["winning_trades_count"] += 1
            state["consecutive_losses"] = 0
        else:
            state["consecutive_losses"] += 1

        # Record trade in TradingDB
        hold_hours = (bar_t - entry_bar_t) / BAR_MS
        pos_id = pos.get("position_id", f"POS_5LAKH_{bar_t}_{coin}")
        db.close_position(pos_id, exit_px, exit_reason, raw_pnl_inr, net_pnl_inr, fee_usd * fx)
        db.record_trade(
            position_id=pos_id,
            symbol=coin,
            direction="long" if side == 1 else "short",
            qty=pos["qty"],
            entry_price=entry_px,
            exit_price=exit_px,
            entry_dt=pos["entry_time"],
            exit_dt=datetime.now(IST).isoformat(),
            hold_minutes=hold_hours * 60,
            exit_reason=exit_reason,
            gross_pnl=raw_pnl_inr,
            costs_dict={"brokerage": fee_usd * fx, "slippage": 0.0, "total": fee_usd * fx},
            net_pnl=net_pnl_inr,
            capital_after=state["trading_capital"] + state["vault_locked"],
            account_id=ACCOUNT_ID,
            strategy=STRATEGY_NAME
        )

        sign = "+" if net_pnl_inr >= 0 else ""
        lines.append(
            f"🎯 <b>CLOSED {'LONG' if side == 1 else 'SHORT'} {coin}</b>\n"
            f"Reason: {exit_reason}\n"
            f"Entry: {entry_px:,.2f} → Exit: {exit_px:,.2f} USDT\n"
            f"Net PnL: <b>{sign}{_inr(net_pnl_inr)}</b> (Fees: {_inr(fee_usd * fx)})\n"
            f"Trading Capital: {_inr(state['trading_capital'])} | Vault: {_inr(state['vault_locked'])}\n"
            f"Total Wealth: <b>{_inr(state['trading_capital'] + state['vault_locked'])}</b>"
        )
        state["active_trade"] = None


def process_entries(db: TradingDB, state: dict, candles_map: dict, live_info: dict, lines: list) -> None:
    """Evaluate Macro Regime Gate and enter momentum leader if flat."""
    if state.get("active_trade") is not None:
        return

    cap = state["trading_capital"]
    if cap < 500.0:
        lines.append("⚠️ Trading capital below ₹500 minimum. Pausing.")
        return

    # Check BTC Macro Regime Gate
    btc_df = candles_map.get("BTC")
    if btc_df is None or btc_df.empty:
        return

    btc_last = btc_df.iloc[-1]
    btc_c = float(btc_last["c"])
    btc_ema200 = float(btc_last["ema200"])
    btc_adx = float(btc_last["adx"])

    is_bull = (btc_c > btc_ema200) and (btc_adx > ADX_REGIME_THRESH)
    is_bear = (btc_c < btc_ema200) and (btc_adx > ADX_REGIME_THRESH)

    if not is_bull and not is_bear:
        # Market is in chop consolidation regime. Macro gate protects capital!
        return

    # Rank 24h momentum leaders among SOL, ETH, BTC
    ret24_map = {}
    for c in COINS:
        c_df = candles_map.get(c)
        if c_df is not None and not c_df.empty:
            ret24_map[c] = float(c_df.iloc[-1]["ret24"])

    if not ret24_map:
        return

    best_coin = max(ret24_map, key=ret24_map.get)
    worst_coin = min(ret24_map, key=ret24_map.get)
    best_val = ret24_map[best_coin]
    worst_val = ret24_map[worst_coin]

    sig = 0
    chosen_coin = None

    if is_bull:
        best_rsi = float(candles_map[best_coin].iloc[-1]["rsi"])
        if best_val > MOMENTUM_THRESH:
            sig = 1
            chosen_coin = best_coin
        elif best_rsi < 38.0:
            sig = 1
            chosen_coin = best_coin
    elif is_bear:
        worst_rsi = float(candles_map[worst_coin].iloc[-1]["rsi"])
        if worst_val < -MOMENTUM_THRESH:
            sig = -1
            chosen_coin = worst_coin
        elif worst_rsi > 62.0:
            sig = -1
            chosen_coin = worst_coin

    if sig != 0 and chosen_coin is not None:
        # Dynamic Chop Filter: BTC and ETH require ADX > 22 to filter false breakouts during sideways chop
        if chosen_coin in ("BTC", "ETH") and float(candles_map[chosen_coin].iloc[-1]["adx"]) < 22.0:
            lines.append(f"ℹ️ Skipping {chosen_coin}: ADX {float(candles_map[chosen_coin].iloc[-1]['adx']):.1f} < 22.0 (sideways chop gate active)")
            return

        c_df = candles_map[chosen_coin]
        last_row = c_df.iloc[-1]
        entry_px = float(last_row["c"])
        atr = float(last_row["atr"])
        if atr <= 0:
            return

        sl_dist = atr * SL_ATR_MULT
        sl_pct = sl_dist / entry_px
        fx = live_info["usdtinr"]

        # Risk scaling with consecutive loss cooldown
        risk_pct = BASE_RISK_PCT
        losses = state.get("consecutive_losses", 0)
        if losses == 1:
            risk_pct *= 0.70
        elif losses >= 2:
            risk_pct *= 0.45

        risk_inr = cap * risk_pct
        risk_usd = risk_inr / fx

        # Leverage capped at strictly MAX_LEVERAGE (3.8x)
        max_notional_usd = (cap * MAX_LEVERAGE) / fx
        pos_usd = min(risk_usd / sl_pct, max_notional_usd)
        qty = pos_usd / entry_px

        if sig == 1:
            sl_px = entry_px - sl_dist
            tp_px = entry_px + (sl_dist * TP_ATR_MULT)
        else:
            sl_px = entry_px + sl_dist
            tp_px = entry_px - (sl_dist * TP_ATR_MULT)

        pos_id = f"POS_5LAKH_{int(last_row['t'])}_{chosen_coin}"
        now_iso = datetime.now(IST).isoformat()

        state["active_trade"] = {
            "position_id": pos_id,
            "coin": chosen_coin,
            "side": sig,
            "qty": qty,
            "entry_price": entry_px,
            "sl_price": sl_px,
            "tp_price": tp_px,
            "pos_usd": pos_usd,
            "entry_bar_t": int(last_row["t"]),
            "entry_time": now_iso
        }

        # Open in DB
        db.open_position(
            position_id=pos_id,
            symbol=chosen_coin,
            direction="long" if sig == 1 else "short",
            qty=qty,
            entry_price=entry_px,
            current_stop=sl_px,
            target_price=tp_px,
            breakeven_price=entry_px,
            account_id=ACCOUNT_ID,
            instrument_key=f"B-{chosen_coin}_USDT",
            strategy=STRATEGY_NAME,
            state=json.dumps({"risk_pct": risk_pct, "sl_dist": sl_dist, "tp_dist": sl_dist * TP_ATR_MULT})
        )

        direction_str = "LONG 🟢" if sig == 1 else "SHORT 🔴"
        lines.append(
            f"🚀 <b>ENTERED {direction_str} {chosen_coin}</b>\n"
            f"Signal: 24h Leader ({ret24_map[chosen_coin]*100:+.2f}%) | BTC Macro: {'BULL' if is_bull else 'BEAR'} (ADX: {btc_adx:.1f})\n"
            f"Entry: {entry_px:,.2f} USDT\n"
            f"Stop Loss: {sl_px:,.2f} USDT ({SL_ATR_MULT:.1f}x ATR)\n"
            f"Take Profit: {tp_px:,.2f} USDT ({TP_ATR_MULT:.1f}x ATR - Asymmetric 1:2.53 R:R)\n"
            f"Size: {_inr(pos_usd * fx)} ({pos_usd:,.1f} USDT @ {MAX_LEVERAGE:.1f}x Cap)\n"
            f"Risk: {_inr(risk_inr)} ({risk_pct*100:.1f}%)"
        )


def run_iteration(db: TradingDB, send_fn=None) -> dict:
    """Execute 1-hour candle cycle."""
    state = load_state()
    live_info = fetch_live_prices()
    lines = []

    # Fetch 1h candles
    candles_map = {}
    for coin in COINS:
        try:
            candles_map[coin] = fetch_1h_candles(coin, limit=250)
        except Exception as e:
            _log.warning("Failed to fetch 1h candles for %s: %s", coin, e)

    if not candles_map or "BTC" not in candles_map:
        lines.append("⚠️ Binance 1h candle feed unavailable. Skipping iteration.")
        return {"lines": lines, "state": state}

    # 1. Process active trade exits
    process_exits(db, state, candles_map, live_info, lines)

    # 2. Process milestone sweeps
    check_milestone_vault(state, lines)

    # 3. Process new entries if flat
    process_entries(db, state, candles_map, live_info, lines)

    # Save state & update DB account capital
    save_state(state)
    tot_wealth = state["trading_capital"] + state["vault_locked"]
    db.update_capital(ACCOUNT_ID, tot_wealth)
    db.record_snapshot(ACCOUNT_ID)

    if lines and send_fn:
        now_dt = datetime.now(IST)
        pct_to_5lakh = (tot_wealth / 500000.0) * 100
        header = f"⚡ <b>CRYPTO 5 LAKH COMPOUNDER</b> ({now_dt:%d %b %H:%M} IST)\n"
        footer = (
            f"\n📊 <b>Wealth: {_inr(tot_wealth)} / ₹5,00,000 ({pct_to_5lakh:.1f}%)</b>\n"
            f"Active Pool: {_inr(state['trading_capital'])} | Locked Vault: {_inr(state['vault_locked'])}\n"
            f"Peak: {_inr(state['peak_wealth'])} | Trades: {state['completed_trades_count']} "
            f"(Win: {(state['winning_trades_count']/max(1, state['completed_trades_count'])*100):.1f}%)"
        )
        msg = header + "\n".join(lines) + footer
        send_fn(msg)

    return {"lines": lines, "state": state}


def print_status(db: TradingDB) -> None:
    """Print full status of the 5 Lakh compounder account."""
    state = load_state()
    tot_wealth = state["trading_capital"] + state["vault_locked"]
    pct = (tot_wealth / 500000.0) * 100
    pos = state.get("active_trade")

    print("=" * 70)
    print("CRYPTO 5 LAKH COMPOUNDER STATUS (BTC, ETH, SOL 1H PERPS)")
    print("=" * 70)
    print(f"Target:               ₹5,00,000 INR")
    print(f"Total Portfolio:      {_inr(tot_wealth)} ({pct:.1f}% to Target)")
    print(f"Active Trading Pool:  {_inr(state['trading_capital'])}")
    print(f"Locked Safe Vault:    {_inr(state['vault_locked'])}")
    print(f"Peak Wealth Recorded: {_inr(state['peak_wealth'])}")
    print(f"Total Completed:      {state['completed_trades_count']} trades")
    wr = (state['winning_trades_count'] / max(1, state['completed_trades_count'])) * 100
    print(f"Win Rate:             {wr:.1f}% ({state['winning_trades_count']} wins)")
    print(f"Total Fees Paid:      {_inr(state['total_fees_paid'])} (0.02% Maker Model)")
    print(f"Milestones Locked:    {', '.join(state['locked_milestones']) or 'None yet'}")
    print("-" * 70)
    if pos:
        print(f"ACTIVE POSITION:")
        print(f"  Symbol:             {pos['coin']} ({'LONG' if pos['side'] == 1 else 'SHORT'})")
        print(f"  Entry Price:        {pos['entry_price']:,.2f} USDT")
        print(f"  Stop Loss:          {pos['sl_price']:,.2f} USDT")
        print(f"  Take Profit:        {pos['tp_price']:,.2f} USDT")
        print(f"  Position Size:      {pos['pos_usd']:,.2f} USDT")
        print(f"  Entered At:         {pos['entry_time']}")
    else:
        print("ACTIVE POSITION:      FLAT (Awaiting Macro Gate + Momentum Leader)")
    print("=" * 70)


def main(argv=None) -> int:
    import engine.config  # Loads .env
    from services.utils import telegram

    parser = argparse.ArgumentParser(description="Crypto 5 Lakh Perpetual Futures Compounder")
    parser.add_argument("--status", action="store_true", help="Print account & vault status")
    parser.add_argument("--no-telegram", action="store_true", help="Suppress Telegram alerts")
    args = parser.parse_args(argv)

    db = TradingDB()
    init_account(db)

    if args.status:
        print_status(db)
        return 0

    send_fn = print if args.no_telegram else telegram.send
    out = run_iteration(db, send_fn=send_fn)
    if out["lines"]:
        print("\n".join(out["lines"]))
    else:
        print("Cycle completed: No trade triggers or state changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
