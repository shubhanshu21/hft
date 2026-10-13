#!/usr/bin/env python3
"""
markets/equity/experiments/edge_discovery_2026.py
Edge Discovery Engine — Beyond Fibonacci Retracement.

Tests fundamentally different strategies vs the broken Fibonacci edge on 2026 data:
  1. Fibonacci (baseline - for comparison)
  2. EMA Breakout Retest (pullback to rising EMA after breakout)
  3. 52-Week High Momentum Breakout (weekly closing above 52wk high)
  4. Inside Bar Breakout (tight consolidation -> directional burst)
  5. Weekly Trend + Daily Pullback (multi-timeframe: weekly uptrend, daily dip)
  6. Volume Surge Breakout (2.5x+ average volume breakout bar)
  7. Higher-Low Trend Continuation (HH-HL sequence, buy HL, target new HH)
  8. RSI 60 Momentum Crossover

All backtests:
- True single-account event-driven simulation (100k shared cash pool).
- Full delivery costs.
- 2026 Jan 1 - Oct 10 as TEST window.
"""
import os, sys
import pandas as pd
import numpy as np
import pandas_ta_classic as ta
from scipy.signal import argrelextrema

sys.path.insert(0, '/var/www/html/hft/backend')
from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs

DATA_DIR = "/var/www/html/hft/backend/var/archive/equity"
CAPITAL  = 100_000.0
START    = "2026-01-01"
END      = "2026-10-10"

def _load_tf(sym, tf):
    f = f"{DATA_DIR}/{sym}_{tf}.csv"
    if not os.path.exists(f): return None
    df = pd.read_csv(f)
    if df.empty or len(df) < 60: return None
    df["datetime"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["date_str"] = df["datetime"].dt.strftime("%Y-%m-%d")
    return df

def load_daily_universe():
    result = {}
    for sym in NIFTY50_SYMBOLS:
        df = _load_tf(sym, "daily")
        if df is None or len(df) < 120: continue
        df["atr"]     = ta.atr(df["high"], df["low"], df["close"], length=14)
        df["ema20"]   = ta.ema(df["close"], length=20)
        df["ema50"]   = ta.ema(df["close"], length=50)
        df["ema200"]  = ta.ema(df["close"], length=200)
        df["rsi14"]   = ta.rsi(df["close"], length=14)
        df["vol_sma20"] = ta.sma(df["volume"], length=20)
        df["mom20"]   = df["close"].pct_change(20)
        df["mom60"]   = df["close"].pct_change(60)
        result[sym] = df
    return result

def load_weekly_universe():
    result = {}
    for sym in NIFTY50_SYMBOLS:
        df = _load_tf(sym, "weekly")
        if df is None or len(df) < 30: continue
        df["ema10w"] = ta.ema(df["close"], length=10)
        df["ema20w"] = ta.ema(df["close"], length=20)
        df["high52"] = df["high"].rolling(52).max()
        result[sym] = df
    return result

print("Loading data ...")
DAILY  = load_daily_universe()
WEEKLY = load_weekly_universe()
print(f"  Daily: {len(DAILY)} symbols | Weekly: {len(WEEKLY)} symbols")

def build_recs(data_dict):
    return {sym: df.set_index("date_str").to_dict("index") for sym, df in data_dict.items()}

def get_all_dates(recs, start=START, end=END):
    dates = set()
    for sym_recs in recs.values():
        for d in sym_recs:
            if start <= d <= end: dates.add(d)
    return sorted(dates)

DAILY_RECS = build_recs(DAILY)
ALL_DATES  = get_all_dates(DAILY_RECS)

def simulate_portfolio(signals_fn, daily_recs, all_dates,
                       initial_capital=CAPITAL, max_positions=3,
                       risk_pct=4.0, max_alloc_pct=0.45, trail_be=True):
    cash = initial_capital
    open_pos = {}
    closed_trades = []
    for d in all_dates:
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            r = daily_recs.get(sym, {}).get(d)
            if not r: continue
            c_high, c_low = r["high"], r["low"]
            exit_p = exit_r = None
            if c_low <= pos["sl"]:
                exit_p = pos["sl"]
                exit_r = "be" if pos["sl"] >= pos["entry"] else "stop"
            elif c_high >= pos["tp"]:
                exit_p = pos["tp"]; exit_r = "target"
            if exit_p is not None:
                cost = compute_nse_equity_delivery_costs("long", pos["entry"], exit_p, pos["qty"], symbol=sym)
                cash += pos["qty"] * exit_p - cost["total"]
                closed_trades.append({"sym": sym, "entry_date": pos["entry_date"], "exit_date": d,
                    "entry": pos["entry"], "exit": exit_p, "qty": pos["qty"],
                    "net": cost["net"], "fees": cost["total"], "result": exit_r,
                    "hold_days": (pd.Timestamp(d) - pd.Timestamp(pos["entry_date"])).days})
                del open_pos[sym]
            else:
                if trail_be:
                    risk_unit = pos["entry"] - pos["orig_sl"]
                    if risk_unit > 0 and c_high >= pos["entry"] + risk_unit:
                        pos["sl"] = max(pos["sl"], pos["entry"])

        if len(open_pos) < max_positions:
            cands = signals_fn(d, open_pos, daily_recs)
            port_eq = cash + sum(p["qty"] * (daily_recs.get(s, {}).get(d, {}).get("close", p["entry"])) for s, p in open_pos.items())
            for c in cands:
                if len(open_pos) >= max_positions: break
                if c["sym"] in open_pos: continue
                stop_dist = c["entry"] - c["sl"]
                if stop_dist <= 0: continue
                qty = min(int(port_eq * (risk_pct/100.0) / stop_dist),
                          int((port_eq * max_alloc_pct) / c["entry"]),
                          int(cash / c["entry"]))
                if qty <= 0 or qty * c["entry"] > cash: continue
                cash -= qty * c["entry"]
                open_pos[c["sym"]] = {"entry_date": d, "entry": c["entry"], "sl": c["sl"],
                                      "orig_sl": c["sl"], "tp": c["tp"], "qty": qty}

    last_d = all_dates[-1]
    for sym, pos in open_pos.items():
        r = daily_recs.get(sym, {}).get(last_d)
        lp = r["close"] if r else pos["entry"]
        cost = compute_nse_equity_delivery_costs("long", pos["entry"], lp, pos["qty"], symbol=sym)
        closed_trades.append({"sym": sym, "entry_date": pos["entry_date"], "exit_date": last_d+"*",
            "entry": pos["entry"], "exit": lp, "qty": pos["qty"],
            "net": cost["net"], "fees": cost["total"], "result": "open",
            "hold_days": (pd.Timestamp(last_d) - pd.Timestamp(pos["entry_date"])).days})
        cash += pos["qty"] * lp - cost["total"]

    wins = [t for t in closed_trades if t["result"] in ("target","be") or t["net"] > 0]
    n    = len(closed_trades)
    wr   = round(len(wins)/n*100,1) if n else 0
    net  = round(sum(t["net"] for t in closed_trades), 0)
    fees = round(sum(t["fees"] for t in closed_trades), 0)
    avg_hd = round(np.mean([t["hold_days"] for t in closed_trades]),1) if closed_trades else 0
    gross_wins  = sum(t["net"] for t in closed_trades if t["net"] > 0)
    gross_losses = abs(sum(t["net"] for t in closed_trades if t["net"] < 0))
    pf = round(gross_wins / max(gross_losses, 1), 2)
    return {"trades": n, "wins": len(wins), "wr": wr, "net": net, "fees": fees,
            "equity": round(cash, 0), "roi_pct": round((cash-initial_capital)/initial_capital*100,2),
            "pf": pf, "avg_hold_days": avg_hd, "detail": closed_trades}

# === STRATEGY 1: FIBONACCI GOLDEN POCKET (BASELINE) ===
_fib_swing_cache = {}
def _build_fib_swing_cache():
    for sym, df in DAILY.items():
        highs   = df["high"].to_numpy()
        lows    = df["low"].to_numpy()
        peaks   = argrelextrema(highs, np.greater_equal, order=15)[0]
        troughs = argrelextrema(lows,  np.less_equal,   order=15)[0]
        dates   = df["date_str"].tolist()
        p_map = {}; curr_p = None; pi = 0
        t_map = {}; curr_t = None; ti = 0
        for i, d in enumerate(dates):
            while pi < len(peaks)   and peaks[pi]   <= i-15: curr_p = peaks[pi];   pi += 1
            while ti < len(troughs) and troughs[ti] <= i-15: curr_t = troughs[ti]; ti += 1
            p_map[d] = curr_p; t_map[d] = curr_t
        _fib_swing_cache[sym] = (p_map, t_map)
_build_fib_swing_cache()

def fib_signals(date, open_positions, daily_recs):
    cands = []
    for sym, recs in daily_recs.items():
        r = recs.get(date)
        if not r or sym in open_positions: continue
        ema50 = r.get("ema50"); c = r["close"]; o = r["open"]
        if not ema50 or pd.isna(ema50) or c < ema50: continue
        p_map, t_map = _fib_swing_cache.get(sym, ({}, {}))
        pi = p_map.get(date); ti = t_map.get(date)
        if pi is None or ti is None or pi <= ti: continue
        df_s = DAILY[sym]
        h_val = float(df_s.at[pi, "high"]); l_val = float(df_s.at[ti, "low"])
        sw = h_val - l_val
        if sw / l_val < 0.05: continue
        fib50 = h_val - 0.50*sw; fib618 = h_val - 0.618*sw; fib786 = h_val - 0.786*sw
        if r["low"] > fib50 or r["low"] < fib786: continue
        bar_r = max(r["high"]-r["low"], 1e-4)
        if not ((c > o) and (c >= fib618) and ((c-r["low"])/bar_r >= 0.40)): continue
        atr = r["atr"] if pd.notna(r.get("atr")) else 0.015*c
        sl = round(l_val - 0.2*atr, 2); tp = round(h_val, 2)
        if tp > c and (tp-c)/(c-sl) >= 1.0:
            cands.append({"sym": sym, "entry": c, "sl": sl, "tp": tp})
    return sorted(cands, key=lambda x: (x["tp"]-x["entry"])/(x["entry"]-x["sl"]), reverse=True)

# === STRATEGY 2: EMA50 BREAKOUT RETEST ===
def ema_retest_signals(date, open_positions, daily_recs):
    cands = []
    for sym, recs in daily_recs.items():
        r = recs.get(date)
        if not r or sym in open_positions: continue
        df_s  = DAILY[sym]
        i_arr = df_s[df_s["date_str"] == date].index
        if len(i_arr) == 0: continue
        i = i_arr[0]
        if i < 5: continue
        ema50 = r.get("ema50"); c = r["close"]; o = r["open"]
        lo = r["low"]; hi = r["high"]
        if pd.isna(ema50) or ema50 <= 0: continue
        if not (lo <= ema50 * 1.005 and c > ema50): continue
        prev_rows = df_s.iloc[max(0, i-15):i]
        if len(prev_rows) < 3: continue
        if not (prev_rows["close"] > prev_rows["ema50"]).all(): continue
        bar_r = max(hi-lo, 1e-4)
        if not (c > o and (c-lo)/bar_r >= 0.40): continue
        atr = r.get("atr") or 0.015*c
        sl = round(ema50 - 1.0*atr, 2)
        if c <= sl: continue
        recent_high = df_s.iloc[max(0, i-20):i]["high"].max()
        tp = round(recent_high, 2)
        stop_dist = c - sl
        if tp <= c or stop_dist <= 0 or (tp-c)/stop_dist < 1.2: continue
        cands.append({"sym": sym, "entry": c, "sl": sl, "tp": tp})
    return sorted(cands, key=lambda x: (x["tp"]-x["entry"])/(x["entry"]-x["sl"]), reverse=True)

# === STRATEGY 3: 52-WEEK HIGH MOMENTUM BREAKOUT ===
_weekly_recs = build_recs(WEEKLY)
def weekly_52wk_signals(date, open_positions, daily_recs):
    cands = []
    for sym, df_w in WEEKLY.items():
        if sym in open_positions: continue
        rows = df_w[df_w["date_str"] <= date]
        if len(rows) < 53: continue
        last_w = rows.iloc[-1]; prev_w = rows.iloc[-2]
        high52 = prev_w.get("high52") if "high52" in prev_w.index else None
        if high52 is None or pd.isna(high52): continue
        if last_w["close"] <= high52: continue
        r = daily_recs.get(sym, {}).get(date)
        if not r: continue
        c = r["close"]; atr = r.get("atr") or 0.015*c
        sl = round(float(prev_w["low"]) - 0.2*atr, 2)
        stop_dist = c - sl
        if stop_dist <= 0: continue
        weekly_range = float(last_w["high"]) - float(last_w["low"])
        tp = round(c + max(2.0*weekly_range, 2.0*stop_dist), 2)
        if tp <= c or (tp-c)/stop_dist < 1.5: continue
        cands.append({"sym": sym, "entry": c, "sl": sl, "tp": tp})
    return cands

# === STRATEGY 4: INSIDE BAR BREAKOUT ===
def inside_bar_signals(date, open_positions, daily_recs):
    cands = []
    for sym, recs in daily_recs.items():
        r = recs.get(date)
        if not r or sym in open_positions: continue
        df_s  = DAILY[sym]
        i_arr = df_s[df_s["date_str"] == date].index
        if len(i_arr) == 0: continue
        i = i_arr[0]
        if i < 3: continue
        c = r["close"]; o = r["open"]; hi = r["high"]; lo = r["low"]
        prev = df_s.iloc[i-1]
        p_hi = prev["high"]; p_lo = prev["low"]; p_cl = prev["close"]; p_op = prev["open"]
        ema50 = r.get("ema50"); prev_ema = prev.get("ema50")
        if pd.isna(ema50) or pd.isna(prev_ema): continue
        if p_cl < prev_ema: continue
        mother_body  = p_cl - p_op
        mother_range = p_hi - p_lo
        if mother_range < 1e-4 or mother_body/mother_range < 0.50: continue
        if hi >= p_hi or lo <= p_lo: continue
        if c < (p_lo + 0.5*mother_range): continue
        atr = r.get("atr") or 0.015*c
        entry_p = round(p_hi + 0.05*atr, 2)
        sl = round(p_lo - 0.3*atr, 2)
        stop_dist = entry_p - sl
        if stop_dist <= 0: continue
        tp = round(entry_p + 2.0*mother_range, 2)
        if (tp-entry_p)/stop_dist < 1.5: continue
        cands.append({"sym": sym, "entry": entry_p, "sl": sl, "tp": tp})
    return sorted(cands, key=lambda x: (x["tp"]-x["entry"])/(x["entry"]-x["sl"]), reverse=True)

# === STRATEGY 5: WEEKLY TREND + DAILY RSI PULLBACK ===
def weekly_trend_daily_pullback_signals(date, open_positions, daily_recs):
    cands = []
    for sym, recs in daily_recs.items():
        r = recs.get(date)
        if not r or sym in open_positions: continue
        df_w = WEEKLY.get(sym)
        if df_w is None: continue
        w_rows = df_w[df_w["date_str"] <= date]
        if len(w_rows) < 12: continue
        last_w = w_rows.iloc[-1]
        ema10w = last_w.get("ema10w")
        if pd.isna(ema10w) or last_w["close"] < ema10w: continue
        rsi = r.get("rsi14"); ema50 = r.get("ema50")
        c = r["close"]; o = r["open"]; hi = r["high"]; lo = r["low"]
        if pd.isna(rsi) or pd.isna(ema50): continue
        if c < ema50: continue
        if rsi > 50 or rsi < 20: continue
        bar_r = max(hi-lo, 1e-4)
        if not (c > o and (c-lo)/bar_r >= 0.40): continue
        atr = r.get("atr") or 0.015*c
        sl = round(lo - 0.5*atr, 2)
        stop_dist = c - sl
        if stop_dist <= 0: continue
        df_s = DAILY[sym]
        i_arr = df_s[df_s["date_str"] == date].index
        if len(i_arr) == 0: continue
        i = i_arr[0]
        recent_high = df_s.iloc[max(0, i-20):i]["high"].max()
        tp = round(recent_high, 2)
        if tp <= c or (tp-c)/stop_dist < 1.5: continue
        cands.append({"sym": sym, "entry": c, "sl": sl, "tp": tp})
    return sorted(cands, key=lambda x: (x["tp"]-x["entry"])/(x["entry"]-x["sl"]), reverse=True)

# === STRATEGY 6: VOLUME SURGE BREAKOUT ===
def volume_surge_signals(date, open_positions, daily_recs):
    cands = []
    for sym, recs in daily_recs.items():
        r = recs.get(date)
        if not r or sym in open_positions: continue
        df_s  = DAILY[sym]
        i_arr = df_s[df_s["date_str"] == date].index
        if len(i_arr) == 0: continue
        i = i_arr[0]
        if i < 25: continue
        c = r["close"]; lo = r["low"]; hi = r["high"]
        ema50 = r.get("ema50"); vol = r.get("volume", 0); vol_sma = r.get("vol_sma20", 0)
        if pd.isna(ema50) or c < ema50: continue
        if pd.isna(vol_sma) or vol_sma <= 0: continue
        if vol < 2.5*vol_sma: continue
        recent_highs = df_s.iloc[max(0, i-20):i]["high"]
        if len(recent_highs) < 10: continue
        if c < recent_highs.max(): continue
        bar_r = max(hi-lo, 1e-4)
        if (c-lo)/bar_r < 0.60: continue
        atr = r.get("atr") or 0.015*c
        sl = round(lo - 0.3*atr, 2)
        stop_dist = c - sl
        if stop_dist <= 0: continue
        tp = round(c + 2.5*atr, 2)
        if (tp-c)/stop_dist < 1.5: continue
        cands.append({"sym": sym, "entry": c, "sl": sl, "tp": tp})
    return cands

# === STRATEGY 7: HIGHER-LOW TREND CONTINUATION ===
def higher_low_signals(date, open_positions, daily_recs):
    cands = []
    for sym, recs in daily_recs.items():
        r = recs.get(date)
        if not r or sym in open_positions: continue
        df_s  = DAILY[sym]
        i_arr = df_s[df_s["date_str"] == date].index
        if len(i_arr) == 0: continue
        i = i_arr[0]
        if i < 40: continue
        window = df_s.iloc[max(0, i-60):i+1]
        highs  = window["high"].to_numpy()
        lows   = window["low"].to_numpy()
        peaks   = argrelextrema(highs, np.greater_equal, order=5)[0]
        troughs = argrelextrema(lows,  np.less_equal,   order=5)[0]
        if len(peaks) < 3 or len(troughs) < 2: continue
        ph = highs[peaks[-3:]]
        if not (ph[0] < ph[1] < ph[2]): continue
        pt = lows[troughs[-2:]]
        if pt[1] <= pt[0]: continue
        hl_price = lows[troughs[-1]]
        atr = r.get("atr") or 0.015*r["close"]
        c = r["close"]; lo = r["low"]
        if lo > hl_price + 0.5*atr: continue
        if c < r["open"]: continue
        ema50 = r.get("ema50")
        if pd.isna(ema50) or c < ema50: continue
        sl = round(hl_price - 0.5*atr, 2)
        stop_dist = c - sl
        if stop_dist <= 0: continue
        last_hh = highs[peaks[-1]]
        avg_swing = highs[peaks[-1]] - lows[troughs[-2]]
        tp = round(last_hh + 0.5*avg_swing, 2)
        if tp <= c or (tp-c)/stop_dist < 1.2: continue
        cands.append({"sym": sym, "entry": c, "sl": sl, "tp": tp})
    return cands

# === STRATEGY 8: RSI 60 MOMENTUM CROSSOVER ===
def rsi_momentum_signals(date, open_positions, daily_recs):
    cands = []
    for sym, recs in daily_recs.items():
        r = recs.get(date)
        if not r or sym in open_positions: continue
        df_s  = DAILY[sym]
        i_arr = df_s[df_s["date_str"] == date].index
        if len(i_arr) == 0: continue
        i = i_arr[0]
        if i < 20: continue
        c = r["close"]; o = r["open"]; hi = r["high"]; lo = r["low"]
        rsi = r.get("rsi14"); ema20 = r.get("ema20"); ema50 = r.get("ema50")
        if pd.isna(rsi) or pd.isna(ema20) or pd.isna(ema50): continue
        if c < ema20 or c < ema50: continue
        prev = df_s.iloc[i-1]
        prev_rsi = prev.get("rsi14")
        if pd.isna(prev_rsi): continue
        if not (prev_rsi < 60 and rsi >= 60): continue
        bar_r = max(hi-lo, 1e-4)
        if not (c > o and (c-lo)/bar_r >= 0.40): continue
        atr = r.get("atr") or 0.015*c
        sl = round(ema20 - 0.5*atr, 2)
        stop_dist = c - sl
        if stop_dist <= 0: continue
        tp = round(c + 2.5*atr, 2)
        if (tp-c)/stop_dist < 1.5: continue
        cands.append({"sym": sym, "entry": c, "sl": sl, "tp": tp})
    return cands

# === RUN ALL STRATEGIES ===
STRATEGIES = [
    ("1. Fibonacci Golden Pocket (Baseline)",       fib_signals),
    ("2. EMA50 Breakout Retest",                    ema_retest_signals),
    ("3. 52-Week High Momentum Breakout (Weekly)",  weekly_52wk_signals),
    ("4. Inside Bar Breakout (Mother Bar)",         inside_bar_signals),
    ("5. Weekly Trend + Daily RSI Pullback",        weekly_trend_daily_pullback_signals),
    ("6. Volume Surge Breakout (2.5x Vol)",         volume_surge_signals),
    ("7. Higher-Low Trend Continuation (HH-HL)",   higher_low_signals),
    ("8. RSI 60 Momentum Crossover",               rsi_momentum_signals),
]

if __name__ == "__main__":
    print(f"\n{'='*105}")
    print(f"  EDGE DISCOVERY ENGINE — 2026 YTD ({START} to {END})  |  Rs.{CAPITAL:,.0f} Single Account")
    print(f"{'='*105}")
    print(f"  Universe: {len(DAILY)} NIFTY50 stocks | Max 3 concurrent positions | 4% risk/trade | Trail BE at 1R")
    print(f"{'='*105}\n")

    rows = []
    for name, fn in STRATEGIES:
        print(f"  Running: {name} ...")
        res = simulate_portfolio(fn, DAILY_RECS, ALL_DATES)
        rows.append({
            "Strategy": name,
            "Trades": res["trades"],
            "WR%": f"{res['wr']}%",
            "PF": f"{res['pf']:.2f}",
            "Net P&L": f"Rs.{res['net']:+,.0f}",
            "Fees": f"Rs.{res['fees']:,.0f}",
            "ROI%": f"{res['roi_pct']:+.2f}%",
            "AvgHold": f"{res['avg_hold_days']}d",
            "FinalEq": f"Rs.{res['equity']:,.0f}",
        })

    print(f"\n{'='*105}")
    print("  RESULTS SUMMARY")
    print(f"{'='*105}")
    df_out = pd.DataFrame(rows)
    print(df_out.to_string(index=False))
    print(f"\n{'='*105}")
    print("  PF = Profit Factor (>1.3 viable edge) | WR = Win Rate | Min 15 trades for any statistical meaning")
    print(f"{'='*105}\n")
