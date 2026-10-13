#!/usr/bin/env python3
"""
fast_edge_discovery.py
Optimized edge discovery — ALL lookups O(1) via pre-built dicts.
Tests 8 rule-based strategies + 1 ML (LightGBM) on 2026 NIFTY50.
Full Upstox delivery costs. Single 100k account. Trail BE at 1R.
"""
import os, sys, warnings
import pandas as pd
import numpy as np
import pandas_ta_classic as ta
from scipy.signal import argrelextrema
warnings.filterwarnings("ignore")

sys.path.insert(0, '/var/www/html/hft/backend')
from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs

DATA_DIR = "/var/www/html/hft/backend/var/archive/equity"
CAPITAL  = 100_000.0
START    = "2026-01-01"
END      = "2026-10-10"

# ─── Load + pre-process all data ───────────────────────────────
print("Loading data...")
DAILY  = {}   # sym -> df
WEEKLY = {}

for sym in NIFTY50_SYMBOLS:
    for tf, store in [("daily", DAILY), ("weekly", WEEKLY)]:
        f = f"{DATA_DIR}/{sym}_{tf}.csv"
        if not os.path.exists(f): continue
        df = pd.read_csv(f)
        if df.empty or len(df) < (120 if tf=="daily" else 55): continue
        df["datetime"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("datetime").reset_index(drop=True)
        df["date_str"] = df["datetime"].dt.strftime("%Y-%m-%d")
        if tf == "daily":
            df["atr"]      = ta.atr(df["high"], df["low"], df["close"], length=14)
            df["ema20"]    = ta.ema(df["close"], length=20)
            df["ema50"]    = ta.ema(df["close"], length=50)
            df["ema200"]   = ta.ema(df["close"], length=200)
            df["rsi14"]    = ta.rsi(df["close"], length=14)
            df["vol_sma20"]= ta.sma(df["volume"], length=20)
            df["mom20"]    = df["close"].pct_change(20)
            df["mom60"]    = df["close"].pct_change(60)
            df["hv20"]     = df["close"].pct_change().rolling(20).std() * np.sqrt(252)
        else:
            df["ema10w"]   = ta.ema(df["close"], length=10)
            df["high52"]   = df["high"].rolling(52).max()
        store[sym] = df

print(f"  Daily: {len(DAILY)} syms | Weekly: {len(WEEKLY)} syms")

# ─── Build O(1) index maps ──────────────────────────────────────
# For daily: per-symbol dict of date_str -> row dict (with all computed columns)
# Also: date_str -> integer row index for slicing
DRECS  = {}   # sym -> {date_str: row_dict}
DIDX   = {}   # sym -> {date_str: int_index}
WRECS  = {}   # sym -> {date_str: row_dict}

for sym, df in DAILY.items():
    rec = df.set_index("date_str").to_dict("index")
    DRECS[sym] = rec
    DIDX[sym]  = {d: i for i, d in enumerate(df["date_str"])}

for sym, df in WEEKLY.items():
    WRECS[sym] = df.set_index("date_str").to_dict("index")

# All trading dates in window
ALL_DATES = sorted(set(
    d for sym in DRECS for d in DRECS[sym] if START <= d <= END
))
print(f"  Trading dates in window: {len(ALL_DATES)}")

# ─── Pre-build swing point maps ────────────────────────────────
# For each sym, for each date, what is the last confirmed peak/trough index?
SWING = {}   # sym -> {date_str: (peak_idx, trough_idx)}  (None if not available)

for sym, df in DAILY.items():
    highs   = df["high"].to_numpy()
    lows    = df["low"].to_numpy()
    dates   = df["date_str"].tolist()
    peaks   = argrelextrema(highs, np.greater_equal, order=15)[0]
    troughs = argrelextrema(lows,  np.less_equal,   order=15)[0]
    p_map   = {}; curr_p = None; pi = 0
    t_map   = {}; curr_t = None; ti = 0
    for i, d in enumerate(dates):
        while pi < len(peaks)   and peaks[pi]   <= i - 15: curr_p = peaks[pi];   pi += 1
        while ti < len(troughs) and troughs[ti] <= i - 15: curr_t = troughs[ti]; ti += 1
        p_map[d] = curr_p; t_map[d] = curr_t
    SWING[sym] = (p_map, t_map)

# Fine-grained swing (order=5) for HH-HL strategy
SWING5 = {}
for sym, df in DAILY.items():
    highs   = df["high"].to_numpy()
    lows    = df["low"].to_numpy()
    dates   = df["date_str"].tolist()
    peaks   = argrelextrema(highs, np.greater_equal, order=5)[0]
    troughs = argrelextrema(lows,  np.less_equal,   order=5)[0]
    # For each date, store list of confirmed peaks/troughs up to that point
    p_list = []; t_list = []
    pi = 0; ti = 0
    confirmed_p = []; confirmed_t = []
    date_peaks   = {}; date_troughs = {}
    for i, d in enumerate(dates):
        while pi < len(peaks)   and peaks[pi]   <= i - 5: confirmed_p.append(peaks[pi]);   pi += 1
        while ti < len(troughs) and troughs[ti] <= i - 5: confirmed_t.append(troughs[ti]); ti += 1
        date_peaks[d]   = list(confirmed_p)
        date_troughs[d] = list(confirmed_t)
    SWING5[sym] = (date_peaks, date_troughs, highs, lows)

# Weekly last-bar map: date -> last weekly row on or before date
WLAST = {}
for sym, df in WEEKLY.items():
    wdates = df["date_str"].tolist()
    # for each daily date, find the last weekly bar <= that date
    WLAST[sym] = {}

def get_last_weekly(sym, date):
    if sym not in WRECS: return None, None
    df_w = WEEKLY[sym]
    rows = df_w[df_w["date_str"] <= date]
    if len(rows) < 53: return None, None
    return rows.iloc[-1], rows.iloc[-2]

# ─── Portfolio simulator ────────────────────────────────────────
def simulate(sig_fn, max_pos=3, risk_pct=4.0, max_alloc=0.45, trail_be=True):
    cash = CAPITAL
    open_pos = {}
    trades   = []

    for d in ALL_DATES:
        # 1. Exits first
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            r = DRECS.get(sym, {}).get(d)
            if not r: continue
            hi, lo = r["high"], r["low"]
            exit_p = exit_r = None
            if lo <= pos["sl"]:
                exit_p = pos["sl"]
                exit_r = "be" if pos["sl"] >= pos["entry"] else "stop"
            elif hi >= pos["tp"]:
                exit_p = pos["tp"]; exit_r = "target"
            if exit_p is not None:
                cost = compute_nse_equity_delivery_costs("long", pos["entry"], exit_p, pos["qty"], symbol=sym)
                cash += pos["qty"] * exit_p - cost["total"]
                trades.append({"sym": sym, "entry_date": pos["entry_date"], "exit_date": d,
                    "entry": pos["entry"], "exit": exit_p, "net": cost["net"],
                    "fees": cost["total"], "result": exit_r,
                    "hold": (pd.Timestamp(d) - pd.Timestamp(pos["entry_date"])).days})
                del open_pos[sym]
            elif trail_be:
                ru = pos["entry"] - pos["orig_sl"]
                if ru > 0 and hi >= pos["entry"] + ru:
                    pos["sl"] = max(pos["sl"], pos["entry"])

        # 2. New signals
        if len(open_pos) < max_pos:
            port_eq = cash + sum(
                p["qty"] * (DRECS.get(s,{}).get(d,{}).get("close", p["entry"]))
                for s, p in open_pos.items()
            )
            cands = sig_fn(d, open_pos)
            for c in cands:
                if len(open_pos) >= max_pos: break
                if c["sym"] in open_pos: continue
                sd = c["entry"] - c["sl"]
                if sd <= 0: continue
                qty = min(
                    int(port_eq * risk_pct / 100.0 / sd),
                    int(port_eq * max_alloc / c["entry"]),
                    int(cash / c["entry"])
                )
                if qty <= 0 or qty * c["entry"] > cash: continue
                cash -= qty * c["entry"]
                open_pos[c["sym"]] = {"entry_date": d, "entry": c["entry"],
                    "sl": c["sl"], "orig_sl": c["sl"], "tp": c["tp"], "qty": qty}

    # Mark open positions at last price
    ld = ALL_DATES[-1]
    for sym, pos in open_pos.items():
        r = DRECS.get(sym,{}).get(ld)
        lp = r["close"] if r else pos["entry"]
        cost = compute_nse_equity_delivery_costs("long", pos["entry"], lp, pos["qty"], symbol=sym)
        trades.append({"sym": sym, "entry_date": pos["entry_date"], "exit_date": ld+"*",
            "entry": pos["entry"], "exit": lp, "net": cost["net"],
            "fees": cost["total"], "result": "open",
            "hold": (pd.Timestamp(ld) - pd.Timestamp(pos["entry_date"])).days})
        cash += pos["qty"] * lp - cost["total"]

    n   = len(trades)
    wins= [t for t in trades if t["net"] > 0]
    wr  = round(len(wins)/n*100,1) if n else 0
    net = round(sum(t["net"] for t in trades), 0)
    fees= round(sum(t["fees"] for t in trades), 0)
    gw  = sum(t["net"] for t in trades if t["net"] > 0)
    gl  = abs(sum(t["net"] for t in trades if t["net"] < 0))
    pf  = round(gw/max(gl,1),2)
    ahd = round(np.mean([t["hold"] for t in trades]),1) if trades else 0
    return {"n":n,"wins":len(wins),"wr":wr,"net":net,"fees":fees,
            "eq":round(cash,0),"roi":round((cash-CAPITAL)/CAPITAL*100,2),"pf":pf,"ahd":ahd,"trades":trades}

# ═══════════════════════════════════════════════════════════════
# SIGNAL FUNCTIONS (all O(1) per bar per symbol)
# ═══════════════════════════════════════════════════════════════

# 1. FIBONACCI GOLDEN POCKET (baseline)
def sig_fib(date, open_pos):
    cands = []
    for sym in DRECS:
        if sym in open_pos: continue
        r = DRECS[sym].get(date)
        if not r: continue
        ema50 = r.get("ema50"); c = r["close"]; o = r["open"]
        if not ema50 or pd.isna(ema50) or c < ema50: continue
        p_map, t_map = SWING[sym]
        pi = p_map.get(date); ti = t_map.get(date)
        if pi is None or ti is None or pi <= ti: continue
        df_s = DAILY[sym]
        h = float(df_s.at[pi,"high"]); l = float(df_s.at[ti,"low"])
        sw = h - l
        if sw/l < 0.05: continue
        fib50=h-0.50*sw; fib618=h-0.618*sw; fib786=h-0.786*sw
        if r["low"]>fib50 or r["low"]<fib786: continue
        br=max(r["high"]-r["low"],1e-4)
        if not(c>o and c>=fib618 and (c-r["low"])/br>=0.40): continue
        atr=r.get("atr") or 0.015*c
        sl=round(l-0.2*atr,2); tp=round(h,2)
        if tp>c and (tp-c)/(c-sl)>=1.0:
            cands.append({"sym":sym,"entry":c,"sl":sl,"tp":tp,"rr":(tp-c)/(c-sl)})
    return sorted(cands,key=lambda x:x["rr"],reverse=True)

# 2. EMA50 RETEST (price pulls back to touch rising EMA, bounces bullish)
def sig_ema_retest(date, open_pos):
    cands = []
    for sym in DRECS:
        if sym in open_pos: continue
        r = DRECS[sym].get(date)
        if not r: continue
        ema50=r.get("ema50"); c=r["close"]; o=r["open"]
        lo=r["low"]; hi=r["high"]
        if pd.isna(ema50) or ema50<=0: continue
        if not(lo<=ema50*1.005 and c>ema50): continue
        i=DIDX[sym].get(date)
        if i is None or i<15: continue
        df_s=DAILY[sym]
        prev=df_s.iloc[max(0,i-15):i]
        if not(prev["close"]>prev["ema50"]).all(): continue
        br=max(hi-lo,1e-4)
        if not(c>o and (c-lo)/br>=0.40): continue
        atr=r.get("atr") or 0.015*c
        sl=round(ema50-1.0*atr,2)
        if c<=sl: continue
        recent_hi=df_s.iloc[max(0,i-20):i]["high"].max()
        tp=round(recent_hi,2)
        sd=c-sl
        if tp<=c or sd<=0 or (tp-c)/sd<1.2: continue
        cands.append({"sym":sym,"entry":c,"sl":sl,"tp":tp,"rr":(tp-c)/sd})
    return sorted(cands,key=lambda x:x["rr"],reverse=True)

# 3. 52-WEEK HIGH BREAKOUT (weekly close > prior 52w high)
def sig_52wk(date, open_pos):
    cands = []
    for sym in DRECS:
        if sym in open_pos: continue
        r=DRECS[sym].get(date)
        if not r: continue
        last_w,prev_w=get_last_weekly(sym,date)
        if last_w is None: continue
        high52=prev_w.get("high52") if hasattr(prev_w,"get") else prev_w["high52"]
        if pd.isna(high52): continue
        lw_close=last_w["close"] if hasattr(last_w,"get") else last_w["close"]
        if lw_close<=high52: continue
        c=r["close"]; atr=r.get("atr") or 0.015*c
        prev_lo=float(prev_w["low"])
        sl=round(prev_lo-0.2*atr,2)
        sd=c-sl
        if sd<=0: continue
        wrange=float(last_w["high"])-float(last_w["low"])
        tp=round(c+max(2.0*wrange,2.0*sd),2)
        if tp<=c or (tp-c)/sd<1.5: continue
        cands.append({"sym":sym,"entry":c,"sl":sl,"tp":tp})
    return cands

# 4. INSIDE BAR BREAKOUT (mother bar breakout setup)
def sig_inside_bar(date, open_pos):
    cands = []
    for sym in DRECS:
        if sym in open_pos: continue
        r=DRECS[sym].get(date)
        if not r: continue
        i=DIDX[sym].get(date)
        if i is None or i<3: continue
        df_s=DAILY[sym]
        prev=df_s.iloc[i-1]
        c=r["close"]; o=r["open"]; hi=r["high"]; lo=r["low"]
        p_hi=prev["high"]; p_lo=prev["low"]; p_cl=prev["close"]; p_op=prev["open"]
        ema50=r.get("ema50"); pe50=prev.get("ema50")
        if pd.isna(ema50) or pd.isna(pe50): continue
        if p_cl<pe50: continue
        mbody=p_cl-p_op; mrange=p_hi-p_lo
        if mrange<1e-4 or mbody/mrange<0.50: continue
        if hi>=p_hi or lo<=p_lo: continue  # must be inside
        if c<(p_lo+0.5*mrange): continue   # bullish bias
        atr=r.get("atr") or 0.015*c
        ep=round(p_hi+0.05*atr,2); sl=round(p_lo-0.3*atr,2)
        sd=ep-sl
        if sd<=0: continue
        tp=round(ep+2.0*mrange,2)
        if (tp-ep)/sd<1.5: continue
        cands.append({"sym":sym,"entry":ep,"sl":sl,"tp":tp})
    return sorted(cands,key=lambda x:(x["tp"]-x["entry"])/(x["entry"]-x["sl"]),reverse=True)

# 5. WEEKLY TREND + DAILY RSI PULLBACK
def sig_wtf_pullback(date, open_pos):
    cands = []
    for sym in DRECS:
        if sym in open_pos: continue
        r=DRECS[sym].get(date)
        if not r: continue
        last_w,_=get_last_weekly(sym,date)
        if last_w is None: continue
        ema10w=float(last_w.get("ema10w",0) or 0)
        if ema10w<=0 or float(last_w["close"])<ema10w: continue
        rsi=r.get("rsi14"); ema50=r.get("ema50")
        c=r["close"]; o=r["open"]; hi=r["high"]; lo=r["low"]
        if pd.isna(rsi) or pd.isna(ema50) or c<ema50: continue
        if rsi>50 or rsi<20: continue
        br=max(hi-lo,1e-4)
        if not(c>o and (c-lo)/br>=0.40): continue
        atr=r.get("atr") or 0.015*c
        sl=round(lo-0.5*atr,2); sd=c-sl
        if sd<=0: continue
        i=DIDX[sym].get(date)
        if i is None: continue
        df_s=DAILY[sym]
        tp=round(df_s.iloc[max(0,i-20):i]["high"].max(),2)
        if tp<=c or (tp-c)/sd<1.5: continue
        cands.append({"sym":sym,"entry":c,"sl":sl,"tp":tp,"rr":(tp-c)/sd})
    return sorted(cands,key=lambda x:x["rr"],reverse=True)

# 6. VOLUME SURGE BREAKOUT (2.5x avg vol, at 20d high, strong close)
def sig_vol_surge(date, open_pos):
    cands = []
    for sym in DRECS:
        if sym in open_pos: continue
        r=DRECS[sym].get(date)
        if not r: continue
        ema50=r.get("ema50"); c=r["close"]; lo=r["low"]; hi=r["high"]
        vol=r.get("volume",0) or 0; vs=r.get("vol_sma20",0) or 0
        if pd.isna(ema50) or c<ema50: continue
        if pd.isna(vs) or vs<=0 or vol<2.5*vs: continue
        i=DIDX[sym].get(date)
        if i is None or i<25: continue
        df_s=DAILY[sym]
        rh=df_s.iloc[max(0,i-20):i]["high"]
        if len(rh)<10 or c<rh.max(): continue
        br=max(hi-lo,1e-4)
        if (c-lo)/br<0.60: continue
        atr=r.get("atr") or 0.015*c
        sl=round(lo-0.3*atr,2); sd=c-sl
        if sd<=0: continue
        tp=round(c+2.5*atr,2)
        if (tp-c)/sd<1.5: continue
        cands.append({"sym":sym,"entry":c,"sl":sl,"tp":tp})
    return cands

# 7. HIGHER-LOW CONTINUATION (HH-HL structure, buy the HL)
def sig_hl_cont(date, open_pos):
    cands = []
    for sym in DRECS:
        if sym in open_pos: continue
        r=DRECS[sym].get(date)
        if not r: continue
        i=DIDX[sym].get(date)
        if i is None or i<40: continue
        date_p,date_t,highs,lows=SWING5[sym]
        dp=date_p.get(date,[]);  dt=date_t.get(date,[])
        if len(dp)<3 or len(dt)<2: continue
        ph=highs[dp[-3:]]
        if not(ph[0]<ph[1]<ph[2]): continue
        pt=lows[dt[-2:]]
        if pt[1]<=pt[0]: continue
        hl_p=lows[dt[-1]]
        atr=r.get("atr") or 0.015*r["close"]
        c=r["close"]; lo=r["low"]
        if lo>hl_p+0.5*atr or c<r["open"]: continue
        ema50=r.get("ema50")
        if pd.isna(ema50) or c<ema50: continue
        sl=round(hl_p-0.5*atr,2); sd=c-sl
        if sd<=0: continue
        lhh=highs[dp[-1]]
        avg_sw=highs[dp[-1]]-lows[dt[-2]]
        tp=round(lhh+0.5*avg_sw,2)
        if tp<=c or (tp-c)/sd<1.2: continue
        cands.append({"sym":sym,"entry":c,"sl":sl,"tp":tp})
    return cands

# 8. RSI-60 MOMENTUM CROSSOVER
def sig_rsi60(date, open_pos):
    cands = []
    for sym in DRECS:
        if sym in open_pos: continue
        r=DRECS[sym].get(date)
        if not r: continue
        i=DIDX[sym].get(date)
        if i is None or i<20: continue
        df_s=DAILY[sym]
        c=r["close"]; o=r["open"]; hi=r["high"]; lo=r["low"]
        rsi=r.get("rsi14"); ema20=r.get("ema20"); ema50=r.get("ema50")
        if pd.isna(rsi) or pd.isna(ema20) or pd.isna(ema50): continue
        if c<ema20 or c<ema50: continue
        pr=df_s.iloc[i-1].get("rsi14")
        if pd.isna(pr) or not(pr<60<=rsi): continue
        br=max(hi-lo,1e-4)
        if not(c>o and (c-lo)/br>=0.40): continue
        atr=r.get("atr") or 0.015*c
        sl=round(ema20-0.5*atr,2); sd=c-sl
        if sd<=0: continue
        tp=round(c+2.5*atr,2)
        if (tp-c)/sd<1.5: continue
        cands.append({"sym":sym,"entry":c,"sl":sl,"tp":tp})
    return cands

# ═══════════════════════════════════════════════════════════════
# STRATEGY 9: LightGBM ML EDGE
# Train on 2022-2025 data. Features: technical indicators at entry.
# Label: did price reach +2R within 20 bars WITHOUT hitting -1R first?
# Use only predictions with proba > 0.55 as entry filter.
# ═══════════════════════════════════════════════════════════════
def build_ml_features(sym, i, df_s, r):
    """Build feature vector for bar i."""
    atr   = r.get("atr") or 0.015*r["close"]
    c     = r["close"]; hi=r["high"]; lo=r["low"]; o=r["open"]
    ema20 = r.get("ema20") or c; ema50=r.get("ema50") or c
    ema200= r.get("ema200") or c; rsi=r.get("rsi14") or 50
    mom20 = r.get("mom20") or 0; mom60=r.get("mom60") or 0
    vol   = r.get("volume",0) or 0; vs=r.get("vol_sma20",1) or 1
    hv    = r.get("hv20") or 0.20

    br = max(hi-lo,1e-4)
    body_pct    = abs(c-o)/br
    upper_wick  = (hi-max(c,o))/br
    lower_wick  = (min(c,o)-lo)/br
    close_pos   = (c-lo)/br          # close position in bar range
    is_green    = float(c>o)
    c_vs_ema20  = (c/ema20)-1
    c_vs_ema50  = (c/ema50)-1
    c_vs_ema200 = (c/ema200)-1
    ema20_slope = (df_s.iloc[i]["ema20"]-df_s.iloc[max(0,i-5)]["ema20"])/(ema20*5+1e-9)
    vol_ratio   = vol/(vs+1e-9)
    atr_pct     = atr/c
    # Swing position
    p_map,t_map = SWING[sym]
    d = r.get("date_str") or df_s.at[i,"date_str"]
    pi=p_map.get(d); ti=t_map.get(d)
    swing_pct=0; fib_pos=0
    if pi is not None and ti is not None and pi>ti:
        h=float(df_s.at[pi,"high"]); l=float(df_s.at[ti,"low"])
        sw=h-l
        if sw>0:
            swing_pct=sw/l
            fib_pos=(h-c)/sw  # 0=at high, 1=at low (0.5-0.7=golden zone)

    return [rsi, mom20, mom60, c_vs_ema20, c_vs_ema50, c_vs_ema200,
            ema20_slope, vol_ratio, atr_pct, hv, body_pct, upper_wick,
            lower_wick, close_pos, is_green, swing_pct, fib_pos]

FEATURE_NAMES = ["rsi","mom20","mom60","c_vs_ema20","c_vs_ema50","c_vs_ema200",
                 "ema20_slope","vol_ratio","atr_pct","hv20","body_pct",
                 "upper_wick","lower_wick","close_pos","is_green","swing_pct","fib_pos"]

def build_ml_dataset(train_end="2025-12-31"):
    """Build (X, y) for bars where EMA50 uptrend holds. Label = +2R in 20 bars before -1R."""
    rows=[]
    for sym, df in DAILY.items():
        # add date_str column to row dicts for feature builder
        df_with_ds = df.copy()
        rec = DRECS[sym]
        for i in range(50, len(df)-20):
            d = df.at[i,"date_str"]
            if d>train_end: break
            r = rec.get(d)
            if not r: continue
            ema50=r.get("ema50"); c=r["close"]
            if pd.isna(ema50) or c<ema50: continue  # only uptrend bars
            feats=build_ml_features(sym,i,df,r)
            if any(pd.isna(f) for f in feats): continue
            # Label: does price reach +2R in next 20 bars before hitting -1R?
            atr=r.get("atr") or 0.015*c
            tp_target=c+2.0*atr; sl_target=c-1.0*atr
            hit_tp=False; hit_sl=False
            for j in range(i+1, min(i+21, len(df))):
                fh=df.at[j,"high"]; fl=df.at[j,"low"]
                if fl<=sl_target: hit_sl=True; break
                if fh>=tp_target: hit_tp=True; break
            if hit_tp or hit_sl:
                rows.append(feats+[int(hit_tp)])
    return rows

print("Building ML training dataset (2022-2025)...")
rows=build_ml_dataset(train_end="2025-12-31")
print(f"  Training samples: {len(rows)}")

import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

arr=np.array(rows,dtype=np.float32)
X=arr[:,:-1]; y=arr[:,-1]
print(f"  Label balance: {y.mean():.1%} positive ({int(y.sum())} wins / {len(y)} total)")

X_tr,X_val,y_tr,y_val=train_test_split(X,y,test_size=0.2,random_state=42,stratify=y)

# Use native lgb.train (avoids sklearn wrapper issues in lgb 4.5.0)
dtrain=lgb.Dataset(X_tr,label=y_tr,feature_name=FEATURE_NAMES,free_raw_data=False)
dval  =lgb.Dataset(X_val,label=y_val,reference=dtrain,free_raw_data=False)
params=dict(objective="binary",metric="auc",learning_rate=0.05,max_depth=4,
            num_leaves=15,min_child_samples=30,subsample=0.8,colsample_bytree=0.8,
            verbose=-1,seed=42)
callbacks=[lgb.early_stopping(30,verbose=False),lgb.log_evaluation(period=-1)]
booster=lgb.train(params,dtrain,num_boost_round=300,valid_sets=[dval],callbacks=callbacks)

def _predict_proba(X_arr):
    return booster.predict(X_arr)  # returns proba of class 1

val_preds=_predict_proba(X_val)
val_auc=roc_auc_score(y_val,val_preds)
print(f"  LightGBM val AUC: {val_auc:.4f}")

# Feature importance
fi=pd.Series(booster.feature_importance(importance_type="gain"),
             index=FEATURE_NAMES).sort_values(ascending=False)
print(f"  Top features: {', '.join(fi.head(5).index.tolist())}")

# ML signal function: uses same EMA50 filter, then gates on proba > 0.55
ML_THRESH = 0.55
def sig_ml(date, open_pos):
    cands=[]
    for sym in DRECS:
        if sym in open_pos: continue
        r=DRECS[sym].get(date)
        if not r: continue
        ema50=r.get("ema50"); c=r["close"]
        if pd.isna(ema50) or c<ema50: continue
        i=DIDX[sym].get(date)
        if i is None or i<50: continue
        feats=build_ml_features(sym,i,DAILY[sym],r)
        if any(pd.isna(f) for f in feats): continue
        proba=float(_predict_proba(np.array([feats],dtype=np.float32))[0])
        if proba<ML_THRESH: continue
        # Entry: use ATR-based stop + 2R target
        atr=r.get("atr") or 0.015*c
        ema20=r.get("ema20") or c
        sl=round(min(ema20, c-1.0*atr)-0.2*atr, 2)
        sd=c-sl
        if sd<=0: continue
        tp=round(c+2.0*atr,2)
        if (tp-c)/sd<1.5: continue
        cands.append({"sym":sym,"entry":c,"sl":sl,"tp":tp,"proba":proba})
    return sorted(cands,key=lambda x:x["proba"],reverse=True)

# ─── Run everything ─────────────────────────────────────────────
STRATEGIES=[
    ("1. Fibonacci GP (Baseline)",     sig_fib),
    ("2. EMA50 Retest",                sig_ema_retest),
    ("3. 52-Wk High Breakout",         sig_52wk),
    ("4. Inside Bar Breakout",         sig_inside_bar),
    ("5. Weekly+Daily RSI Pullback",   sig_wtf_pullback),
    ("6. Volume Surge 2.5x",           sig_vol_surge),
    ("7. Higher-Low Continuation",     sig_hl_cont),
    ("8. RSI-60 Momentum Cross",       sig_rsi60),
    (f"9. LightGBM ML (AUC={val_auc:.4f})", sig_ml),
]

print(f"\n{'='*110}")
print(f"  EDGE DISCOVERY 2026 ({START}→{END}) | Rs.{CAPITAL:,.0f} Account | 4%R/trade | Max 3 pos | Trail BE@1R")
print(f"{'='*110}\n")

rows_out=[]
for name,fn in STRATEGIES:
    sys.stdout.write(f"  [{name}]..."); sys.stdout.flush()
    res=simulate(fn)
    print(f" {res['n']} trades, WR={res['wr']}%, PF={res['pf']}, ROI={res['roi']:+.2f}%")
    rows_out.append({"Strategy":name,"N":res['n'],"WR%":f"{res['wr']}%",
        "PF":f"{res['pf']:.2f}","Net":f"Rs.{res['net']:+,.0f}",
        "Fees":f"Rs.{res['fees']:,.0f}","ROI":f"{res['roi']:+.2f}%",
        "AvgHold":f"{res['ahd']}d","FinalEq":f"Rs.{res['eq']:,.0f}",
        "_roi_raw":res['roi'],"_res":res})

print(f"\n{'='*110}")
print("  FULL RESULTS")
print(f"{'='*110}")
df_out=pd.DataFrame([{k:v for k,v in r.items() if not k.startswith("_")} for r in rows_out])
print(df_out.to_string(index=False))

best=max(rows_out,key=lambda x:x["_roi_raw"])
print(f"\n  >>> BEST EDGE: {best['Strategy']} | ROI={best['ROI']} | WR={best['WR%']} | PF={best['PF']}")

# Print trade detail for best strategy
bres=best["_res"]
if bres["n"]>=5:
    print(f"\n  --- Trade Detail for Best Strategy ({bres['n']} trades) ---")
    for t in bres["trades"][-20:]:
        print(f"    {t['sym']:12s} {t['entry_date']} -> {t['exit_date']:12s} "
              f"entry={t['entry']:.1f} exit={t['exit']:.1f} "
              f"net=Rs.{t['net']:+,.0f} [{t['result']}]")

print(f"\n{'='*110}")
print("  NOTES: PF>1.3 = viable. Need >=15 trades for significance.")
print(f"{'='*110}\n")
