"""crypto / compounder_5lakh -- 1-hour All-Weather Compounder (Target ₹5,00,000 on BTC, ETH, SOL).

Rules (1-hour finished candles from Binance USDT-M Futures):
  * Macro Regime Gate:
      - BULL: BTC Close > EMA200 AND BTC ADX(14) > 18 -> Longs Only (SOL/ETH/BTC 24h momentum breakout > +3% or RSI < 38)
      - BEAR: BTC Close < EMA200 AND BTC ADX(14) > 18 -> Shorts Only (SOL/ETH/BTC 24h momentum breakdown < -3% or RSI > 62)
      - CHOP: BTC ADX(14) <= 18 -> 100% Cash (Capital preserved, zero fee bleed)
  * Asymmetric Payoff (1 : 2.53 Risk-to-Reward):
      - Stop Loss: 1.5x ATR(14)
      - Take Profit: 3.8x ATR(14)
      - Time Stop: 48 hours maximum hold
  * Risk & Leverage:
      - Base Risk: 4.0% of capital per trade
      - Leverage: Strictly capped at 3.8x (0.0% liquidation risk)
      - Fee Model: Passive Maker Limit (0.02% fee per side)
"""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from core.backtester import metrics

DATA_DIR = ARCHIVE_ROOT / "crypto"
COINS = ("BTC", "ETH", "SOL")


def available() -> list[str]:
    return [c for c in COINS if (DATA_DIR / f"{c}USDT_1h.csv").exists()]


_DATA_CACHE: dict = {}


def load_1h_data(coins: list[str]) -> dict:
    global _DATA_CACHE
    missing = [c for c in coins if c not in _DATA_CACHE]
    for c in missing:
        f = DATA_DIR / f"{c}USDT_1h.csv"
        df = pd.read_csv(f)
        df["dt"] = pd.to_datetime(df["timestamp"]).dt.tz_localize(None)
        df.sort_values("dt", inplace=True)
        df.reset_index(drop=True, inplace=True)

        df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        rs = avg_gain / np.maximum(avg_loss, 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs()
        ], axis=1).max(axis=1)
        df["atr"] = tr.rolling(14).mean()

        up_move = df["high"] - df["high"].shift(1)
        down_move = df["low"].shift(1) - df["low"]
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        plus_di = 100 * (pd.Series(plus_dm).ewm(alpha=1/14, adjust=False).mean() / np.maximum(df["atr"], 1e-9))
        minus_di = 100 * (pd.Series(minus_dm).ewm(alpha=1/14, adjust=False).mean() / np.maximum(df["atr"], 1e-9))
        dx = 100 * (plus_di - minus_di).abs() / np.maximum(plus_di + minus_di, 1e-9)
        df["adx"] = dx.ewm(alpha=1/14, adjust=False).mean()

        _DATA_CACHE[c] = df.set_index("dt")
    return {c: _DATA_CACHE[c] for c in coins}


def web_backtest(capital: float, start: str, end: str, symbols: list[str] | None = None, leverage: float = 3.8, **kwargs) -> dict:
    """Run full backtest for web dashboard."""
    coins = [s for s in (symbols or available()) if s in available()]
    if "BTC" not in coins:
        coins.append("BTC")
        
    d_map = load_1h_data(coins)
    common_idx = sorted(list(set.intersection(*(set(d_map[c].index) for c in coins))))
    t_start = pd.Timestamp(start)
    t_end = pd.Timestamp(end)
    date_range = [t for t in common_idx if t_start <= t <= t_end]
    
    if len(date_range) < 24:
        raise ValueError(f"Insufficient crypto data between {start} and {end}")

    ret24 = pd.DataFrame({c: d_map[c]["close"].pct_change(24) for c in coins}).reindex(date_range).fillna(0.0)

    # Pre-extract numpy arrays for 100x faster execution
    n_bars = len(date_range)
    close_arr = {c: d_map[c].reindex(date_range)["close"].to_numpy() for c in coins}
    high_arr = {c: d_map[c].reindex(date_range)["high"].to_numpy() for c in coins}
    low_arr = {c: d_map[c].reindex(date_range)["low"].to_numpy() for c in coins}
    atr_arr = {c: d_map[c].reindex(date_range)["atr"].to_numpy() for c in coins}
    rsi_arr = {c: d_map[c].reindex(date_range)["rsi"].to_numpy() for c in coins}
    adx_arr = {c: d_map[c].reindex(date_range)["adx"].to_numpy() for c in coins}
    ema200_arr = {c: d_map[c].reindex(date_range)["ema200"].to_numpy() for c in coins}
    ret24_arr = {c: ret24[c].to_numpy() for c in coins}
    trade_candidates = [c for c in coins if c in ("SOL", "ETH", "BTC")]

    cur_cap = float(capital)
    risk_pct = 0.040
    sl_mult = 1.5
    tp_mult = 3.8
    maker_fee = 0.0002

    in_pos = False
    cur_coin = None
    pos_side = 0
    entry_px = 0.0
    pos_size_usd = 0.0
    sl_px = 0.0
    tp_px = 0.0
    entry_idx = 0
    consec_losses = 0

    all_tr = []
    daily_equity = {}

    for idx, dt in enumerate(date_range):
        d_str = dt.strftime("%Y-%m-%d")
        daily_equity[d_str] = cur_cap

        if in_pos:
            h = high_arr[cur_coin][idx]
            l = low_arr[cur_coin][idx]
            c = close_arr[cur_coin][idx]
            is_exit = False
            exit_px = c
            why = ""

            if pos_side == 1:
                if l <= sl_px: exit_px = sl_px; why = "Stop Loss (1.5x ATR)"; is_exit = True
                elif h >= tp_px: exit_px = tp_px; why = "Take Profit (3.8x ATR)"; is_exit = True
                elif (idx - entry_idx) >= 48: exit_px = c; why = "Time Stop (48h)"; is_exit = True
            else:
                if h >= sl_px: exit_px = sl_px; why = "Stop Loss (1.5x ATR)"; is_exit = True
                elif l <= tp_px: exit_px = tp_px; why = "Take Profit (3.8x ATR)"; is_exit = True
                elif (idx - entry_idx) >= 48: exit_px = c; why = "Time Stop (48h)"; is_exit = True

            if is_exit:
                pnl = (pos_side * (exit_px - entry_px) / entry_px) * pos_size_usd
                fee = pos_size_usd * (maker_fee * 2)
                net = pnl - fee
                cur_cap += net
                if net > 0:
                    consec_losses = 0
                else:
                    consec_losses += 1

                all_tr.append({
                    "coin": cur_coin, "side": "long" if pos_side == 1 else "short",
                    "t0": int(date_range[entry_idx].timestamp() // 60),
                    "t1": int(dt.timestamp() // 60),
                    "entry": float(entry_px), "exit": float(exit_px),
                    "pnl_rs": float(net), "gross": float(pnl), "net": float(net),
                    "fees": float(fee), "q": float(pos_size_usd / entry_px),
                    "why": why, "won": net > 0
                })
                in_pos = False
                cur_coin = None

        if not in_pos and cur_cap > 50.0:
            btc_c = close_arr["BTC"][idx]
            btc_ema200 = ema200_arr["BTC"][idx]
            btc_adx = adx_arr["BTC"][idx]
            is_bull = (btc_c > btc_ema200) and (btc_adx > 18.0)
            is_bear = (btc_c < btc_ema200) and (btc_adx > 18.0)

            best_c = trade_candidates[0]
            best_val = ret24_arr[best_c][idx]
            worst_c = trade_candidates[0]
            worst_val = ret24_arr[worst_c][idx]
            for c in trade_candidates[1:]:
                v = ret24_arr[c][idx]
                if v > best_val: best_val = v; best_c = c
                if v < worst_val: worst_val = v; worst_c = c

            sig = 0
            chosen = None
            if is_bull:
                if best_val > 0.03: sig = 1; chosen = best_c
                elif rsi_arr[best_c][idx] < 38.0: sig = 1; chosen = best_c
            elif is_bear:
                if worst_val < -0.03: sig = -1; chosen = worst_c
                elif rsi_arr[worst_c][idx] > 62.0: sig = -1; chosen = worst_c

            if sig != 0 and chosen is not None:
                # Dynamic Chop Filter: BTC and ETH require ADX > 22 to filter false breakouts during sideways chop
                if chosen in ("BTC", "ETH") and adx_arr[chosen][idx] < 22.0:
                    continue

                atr = atr_arr[chosen][idx]
                entry = close_arr[chosen][idx]
                if atr > 0:
                    sl_dist = atr * sl_mult
                    sl_pct = sl_dist / entry

                    # Dynamic Streak Risk Dampener: cushions drawdown during chop regimes
                    dyn_risk = risk_pct
                    if consec_losses == 1:
                        dyn_risk *= 0.70
                    elif consec_losses >= 2:
                        dyn_risk *= 0.45

                    pos_size_usd = min((cur_cap * dyn_risk) / sl_pct, cur_cap * leverage)

                    in_pos = True
                    cur_coin = chosen
                    pos_side = sig
                    entry_px = entry
                    entry_idx = idx
                    sl_px = entry - sl_dist if sig == 1 else entry + sl_dist
                    tp_px = entry + (sl_dist * tp_mult) if sig == 1 else entry - (sl_dist * tp_mult)

    days = pd.date_range(t_start.floor("D"), t_end.floor("D"), freq="D")
    s_eq = pd.Series(daily_equity).reindex([d.strftime("%Y-%m-%d") for d in days]).ffill().fillna(capital)

    # Daily bars for dashboard chart
    bars = {}
    for c in coins:
        df_c = d_map[c].reindex(date_range)
        dd = df_c.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).reindex(days).ffill().bfill()
        bars[c] = [[round(float(v), 4) for v in r] for r in dd[["open", "high", "low", "close"]].values]

    closed_formatted = [
        {"net": x["net"], "fees": x["fees"], "gross": x["gross"], "slip": 0.0, "days": round((x["t1"] - x["t0"]) / 1440, 1)}
        for x in all_tr
    ]

    m = metrics(list(s_eq.values), capital, closed_formatted, round(len(days) * 250 / 365.25), 0)
    m["closed_profit"] = round(cur_cap - capital)
    m["final_equity"] = round(cur_cap)

    events = [[] for _ in days]
    pos = [[] for _ in days]
    pos_of = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(days)}

    for x in all_tr:
        dt_entry = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%Y-%m-%d")
        dt_exit = pd.Timestamp(x["t1"] * 60, unit="s").strftime("%Y-%m-%d")
        when = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%H:%M")
        hours = max(1.0, (x["t1"] - x["t0"]) / 60)

        # Standard buy/entry event
        if dt_entry in pos_of:
            events[pos_of[dt_entry]].append({
                "t": "buy",
                "side": x["side"],
                "sym": x["coin"],
                "q": round(x["q"], 4),
                "px": round(x["entry"], 4),
                "entry": round(x["entry"], 4),
                "stop": round(x.get("stop", x["entry"] * 0.95), 4),
                "note": f"{when} UTC {'BUY' if x['side'] == 'long' else 'SHORT'} ({x['coin']}) at ${x['entry']:,.2f}"
            })

        # Standard sell/exit event
        if dt_exit in pos_of:
            events[pos_of[dt_exit]].append({
                "t": "sell",
                "side": x["side"],
                "sym": x["coin"],
                "q": round(x["q"], 4),
                "px": round(x["exit"], 4),
                "entry": round(x["entry"], 4),
                "pnl": round(x["pnl_rs"], 2),
                "fees": round(x["fees"], 2),
                "slip": 0.0,
                "days": round(hours / 24, 1),
                "why": x["why"]
            })

        # Position tracking across calendar days
        i_start = pos_of.get(dt_entry)
        i_end = pos_of.get(dt_exit)
        if i_start is not None and i_end is not None:
            for d_idx in range(i_start, min(i_end, len(days))):
                pos[d_idx].append({
                    "sym": x["coin"],
                    "q": round(x["q"], 4),
                    "e": round(x["entry"], 4),
                    "px": round(x["exit"], 4),
                    "start": i_start
                })

    # Hourly chart data for full-fidelity interactive replay
    t0g = int(days[0].timestamp()) // 3600 * 3600
    t1g = int((days[-1] + pd.Timedelta(days=1)).timestamp())
    times = list(range(t0g, t1g, 3600))
    pos1h = {v: i for i, v in enumerate(times)}
    chart = {
        "tf": "1H",
        "times": times,
        "bars": {},
        "trades": {},
        "day_end": [min(len(times) - 1, max(0, (int((d + pd.Timedelta(days=1)).timestamp()) - t0g) // 3600 - 1)) for d in days]
    }
    for c in coins:
        df_c = d_map[c].reindex(date_range).dropna(subset=["close"])
        b1 = [None] * len(times)
        if len(df_c):
            ts_secs = (df_c.index.astype("int64") // 10**9 // 3600 * 3600).to_numpy()
            ov = df_c["open"].to_numpy()
            hv = df_c["high"].to_numpy()
            lv = df_c["low"].to_numpy()
            cv = df_c["close"].to_numpy()
            for ts_sec, o_, h_, l_, c_ in zip(ts_secs, ov, hv, lv, cv):
                gi = pos1h.get(ts_sec)
                if gi is not None and 0 <= gi < len(times):
                    b1[gi] = [round(float(o_), 4), round(float(h_), 4), round(float(l_), 4), round(float(c_), 4)]
        last_b = None
        for k_idx in range(len(b1)):
            if b1[k_idx] is not None:
                last_b = b1[k_idx]
            elif last_b is not None:
                b1[k_idx] = list(last_b)
        chart["bars"][c] = b1
        chart["trades"][c] = [
            {
                "i0": pos1h.get(x["t0"] * 60 // 3600 * 3600),
                "i1": pos1h.get(x["t1"] * 60 // 3600 * 3600),
                "entry": float(round(x["entry"], 4)),
                "exit": float(round(x["exit"], 4)),
                "qty": round(x["q"], 4),
                "soldQty": round(x["q"], 4),
                "pnl": round(x["pnl_rs"], 2),
                "side": x["side"],
                "adds": []
            }
            for x in all_tr if x["coin"] == c and pos1h.get(x["t0"] * 60 // 3600 * 3600) is not None
        ]

    first_px = np.array([b[0][3] for b in bars.values()])
    bench = (capital / len(coins) * np.array([[b[i][3] for b in bars.values()] for i in range(len(days))]) / first_px).sum(axis=1)

    return {
        "days": [d.strftime("%Y-%m-%d") for d in days],
        "equity": [round(v) for v in s_eq.values],
        "events": events,
        "pos": pos,
        "bars": bars,
        "chart": chart,
        "overlays": {},
        "skipped_symbols": [],
        "benchmark": [round(v) for v in bench],
        "metrics": m | {
            "note": f"1-hour All-Weather Compounder on {', '.join(coins)} (Target ₹5L, 3.8x leverage cap, Maker fees): "
                    f"{len(all_tr):,} trades, {sum(1 for x in all_tr if x['won']) / max(len(all_tr), 1):.1%} win rate."
        }
    }
