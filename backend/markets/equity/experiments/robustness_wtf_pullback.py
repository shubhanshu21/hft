#!/usr/bin/env python3
"""
robustness_wtf_pullback.py
Tests the Weekly+Daily RSI Pullback strategy across EVERY year from 2023-2026
to check if the 2026 edge is real or luck.

Also:
 - Runs parameter sensitivity (RSI threshold, min RR, max pos)
 - Runs ML-filtered version (LightGBM on top of the signal)
 - Prints full trade log for each year
"""
import os, sys, warnings
import pandas as pd
import numpy as np
import pandas_ta_classic as ta
from scipy.signal import argrelextrema
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score
warnings.filterwarnings("ignore")

sys.path.insert(0, '/var/www/html/hft/backend')
from markets.equity.universe import NIFTY50_SYMBOLS
from markets.equity.costs import compute_nse_equity_delivery_costs

DATA_DIR = "/var/www/html/hft/backend/var/archive/equity"
CAPITAL  = 100_000.0

# ─── Load data ─────────────────────────────────────────────────
print("Loading data...")
DAILY  = {}
WEEKLY = {}
for sym in NIFTY50_SYMBOLS:
    for tf, store in [("daily", DAILY), ("weekly", WEEKLY)]:
        f = f"{DATA_DIR}/{sym}_{tf}.csv"
        if not os.path.exists(f): continue
        df = pd.read_csv(f)
        if df.empty or len(df) < (150 if tf=="daily" else 55): continue
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

DRECS = {sym: df.set_index("date_str").to_dict("index") for sym, df in DAILY.items()}
DIDX  = {sym: {d: i for i, d in enumerate(df["date_str"])} for sym, df in DAILY.items()}

# ─── Swing maps ─────────────────────────────────────────────────
SWING = {}
for sym, df in DAILY.items():
    h=df["high"].to_numpy(); l=df["low"].to_numpy()
    dates=df["date_str"].tolist()
    peaks=argrelextrema(h,np.greater_equal,order=15)[0]
    troughs=argrelextrema(l,np.less_equal,order=15)[0]
    pm={}; cp=None; pi=0
    tm={}; ct=None; ti=0
    for i,d in enumerate(dates):
        while pi<len(peaks)   and peaks[pi]  <=i-15: cp=peaks[pi];   pi+=1
        while ti<len(troughs) and troughs[ti]<=i-15: ct=troughs[ti]; ti+=1
        pm[d]=cp; tm[d]=ct
    SWING[sym]=(pm,tm)

def get_last_weekly(sym, date):
    df_w=WEEKLY.get(sym)
    if df_w is None: return None,None
    rows=df_w[df_w["date_str"]<=date]
    if len(rows)<12: return None,None
    return rows.iloc[-1], rows.iloc[-2]

# ─── Core signal function ────────────────────────────────────────
def wtf_signal(date, sym, rsi_max=50, rsi_min=20, min_rr=1.5):
    r=DRECS.get(sym,{}).get(date)
    if not r: return None
    last_w,_=get_last_weekly(sym,date)
    if last_w is None: return None
    ema10w=float(last_w.get("ema10w",0) or 0)
    if ema10w<=0 or float(last_w["close"])<ema10w: return None
    rsi=r.get("rsi14"); ema50=r.get("ema50")
    c=r["close"]; o=r["open"]; hi=r["high"]; lo=r["low"]
    if pd.isna(rsi) or pd.isna(ema50) or c<ema50: return None
    if rsi>rsi_max or rsi<rsi_min: return None
    br=max(hi-lo,1e-4)
    if not(c>o and (c-lo)/br>=0.40): return None
    atr=r.get("atr") or 0.015*c
    sl=round(lo-0.5*atr,2); sd=c-sl
    if sd<=0: return None
    i=DIDX[sym].get(date)
    if i is None: return None
    df_s=DAILY[sym]
    tp=round(df_s.iloc[max(0,i-20):i]["high"].max(),2)
    if tp<=c or (tp-c)/sd<min_rr: return None
    return {"sym":sym,"entry":c,"sl":sl,"tp":tp,"rr":(tp-c)/sd}

# ─── Portfolio simulator ─────────────────────────────────────────
def simulate(start, end, max_pos=3, risk_pct=4.0, max_alloc=0.45,
             trail_be=True, rsi_max=50, rsi_min=20, min_rr=1.5,
             ml_filter=None, ml_thresh=0.55):
    all_dates=sorted(set(
        d for sym in DRECS for d in DRECS[sym] if start<=d<=end
    ))
    cash=CAPITAL; open_pos={}; trades=[]
    for d in all_dates:
        # Exits
        for sym in list(open_pos.keys()):
            pos=open_pos[sym]; r=DRECS.get(sym,{}).get(d)
            if not r: continue
            hi,lo=r["high"],r["low"]
            ep=er=None
            if lo<=pos["sl"]: ep=pos["sl"]; er="be" if pos["sl"]>=pos["entry"] else "stop"
            elif hi>=pos["tp"]: ep=pos["tp"]; er="target"
            if ep is not None:
                cost=compute_nse_equity_delivery_costs("long",pos["entry"],ep,pos["qty"],symbol=sym)
                cash+=pos["qty"]*ep-cost["total"]
                trades.append({"sym":sym,"entry_date":pos["entry_date"],"exit_date":d,
                    "entry":pos["entry"],"exit":ep,"net":cost["net"],"fees":cost["total"],
                    "result":er,"hold":(pd.Timestamp(d)-pd.Timestamp(pos["entry_date"])).days})
                del open_pos[sym]
            elif trail_be:
                ru=pos["entry"]-pos["orig_sl"]
                if ru>0 and hi>=pos["entry"]+ru: pos["sl"]=max(pos["sl"],pos["entry"])
        # Entries
        if len(open_pos)<max_pos:
            port_eq=cash+sum(p["qty"]*(DRECS.get(s,{}).get(d,{}).get("close",p["entry"])) for s,p in open_pos.items())
            cands=[]
            for sym in DRECS:
                if sym in open_pos: continue
                sig=wtf_signal(d,sym,rsi_max=rsi_max,rsi_min=rsi_min,min_rr=min_rr)
                if sig is None: continue
                # ML filter
                if ml_filter is not None:
                    i=DIDX[sym].get(d)
                    if i is None or i<50: continue
                    r=DRECS[sym].get(d)
                    feats=build_ml_features(sym,i,DAILY[sym],r)
                    if any(pd.isna(f) for f in feats): continue
                    proba=float(ml_filter.predict(np.array([feats],dtype=np.float32))[0])
                    if proba<ml_thresh: continue
                    sig["proba"]=proba
                cands.append(sig)
            cands.sort(key=lambda x:x.get("proba",x["rr"]),reverse=True)
            for c in cands:
                if len(open_pos)>=max_pos: break
                if c["sym"] in open_pos: continue
                sd=c["entry"]-c["sl"]
                if sd<=0: continue
                qty=min(int(port_eq*risk_pct/100.0/sd),
                        int(port_eq*max_alloc/c["entry"]),
                        int(cash/c["entry"]))
                if qty<=0 or qty*c["entry"]>cash: continue
                cash-=qty*c["entry"]
                open_pos[c["sym"]]={"entry_date":d,"entry":c["entry"],"sl":c["sl"],
                                    "orig_sl":c["sl"],"tp":c["tp"],"qty":qty}
    ld=all_dates[-1] if all_dates else end
    for sym,pos in open_pos.items():
        r=DRECS.get(sym,{}).get(ld)
        lp=r["close"] if r else pos["entry"]
        cost=compute_nse_equity_delivery_costs("long",pos["entry"],lp,pos["qty"],symbol=sym)
        trades.append({"sym":sym,"entry_date":pos["entry_date"],"exit_date":ld+"*",
            "entry":pos["entry"],"exit":lp,"net":cost["net"],"fees":cost["total"],
            "result":"open","hold":(pd.Timestamp(ld)-pd.Timestamp(pos["entry_date"])).days})
        cash+=pos["qty"]*lp-cost["total"]
    n=len(trades); wins=[t for t in trades if t["net"]>0]
    wr=round(len(wins)/n*100,1) if n else 0
    net=round(sum(t["net"] for t in trades),0)
    fees=round(sum(t["fees"] for t in trades),0)
    gw=sum(t["net"] for t in trades if t["net"]>0)
    gl=abs(sum(t["net"] for t in trades if t["net"]<0))
    pf=round(gw/max(gl,1),2)
    ahd=round(np.mean([t["hold"] for t in trades]),1) if trades else 0
    return {"n":n,"wins":len(wins),"wr":wr,"net":net,"fees":fees,
            "eq":round(cash,0),"roi":round((cash-CAPITAL)/CAPITAL*100,2),
            "pf":pf,"ahd":ahd,"trades":trades}

# ─── ML features (same as fast_edge_discovery) ───────────────────
FEATURE_NAMES = ["rsi","mom20","mom60","c_vs_ema20","c_vs_ema50","c_vs_ema200",
                 "ema20_slope","vol_ratio","atr_pct","hv20","body_pct",
                 "upper_wick","lower_wick","close_pos","is_green","swing_pct","fib_pos"]

def build_ml_features(sym, i, df_s, r):
    atr=r.get("atr") or 0.015*r["close"]
    c=r["close"]; hi=r["high"]; lo=r["low"]; o=r["open"]
    ema20=r.get("ema20") or c; ema50=r.get("ema50") or c
    ema200=r.get("ema200") or c; rsi=r.get("rsi14") or 50
    mom20=r.get("mom20") or 0; mom60=r.get("mom60") or 0
    vol=r.get("volume",0) or 0; vs=r.get("vol_sma20",1) or 1
    hv=r.get("hv20") or 0.20
    br=max(hi-lo,1e-4)
    body_pct=abs(c-o)/br; uw=(hi-max(c,o))/br; lw=(min(c,o)-lo)/br
    cp=(c-lo)/br; ig=float(c>o)
    c20=(c/ema20)-1; c50=(c/ema50)-1; c200=(c/ema200)-1
    s5=float((df_s.iloc[i]["ema20"]-df_s.iloc[max(0,i-5)]["ema20"])/(ema20*5+1e-9))
    vr=vol/(vs+1e-9); ap=atr/c
    pm,tm=SWING[sym]; d=df_s.at[i,"date_str"]
    pi=pm.get(d); ti2=tm.get(d)
    sp=0; fp=0
    if pi is not None and ti2 is not None and pi>ti2:
        h=float(df_s.at[pi,"high"]); l=float(df_s.at[ti2,"low"]); sw=h-l
        if sw>0: sp=sw/l; fp=(h-c)/sw
    return [rsi,mom20,mom60,c20,c50,c200,s5,vr,ap,hv,body_pct,uw,lw,cp,ig,sp,fp]

# ─── Train ML on 2022-2024 (keep 2025 as validation, 2026 as test) ─
def build_ml_dataset(train_end):
    rows=[]
    for sym,df in DAILY.items():
        rec=DRECS[sym]
        for i in range(50,len(df)-20):
            d=df.at[i,"date_str"]
            if d>train_end: break
            r=rec.get(d)
            if not r: continue
            ema50=r.get("ema50"); c=r["close"]
            if pd.isna(ema50) or c<ema50: continue
            # Only label bars that actually triggered WTF signal (reduces noise)
            sig=wtf_signal(d,sym)
            if sig is None: continue  # Only label WTF bars
            feats=build_ml_features(sym,i,df,r)
            if any(pd.isna(f) for f in feats): continue
            atr=r.get("atr") or 0.015*c
            tp_t=c+2.0*atr; sl_t=c-1.0*atr
            hit_tp=False; hit_sl=False
            for j in range(i+1,min(i+21,len(df))):
                fh=df.at[j,"high"]; fl=df.at[j,"low"]
                if fl<=sl_t: hit_sl=True; break
                if fh>=tp_t: hit_tp=True; break
            if hit_tp or hit_sl:
                rows.append(feats+[int(hit_tp)])
    return rows

print("\nBuilding ML dataset (WTF-filtered bars, train=2022-2024)...")
rows=build_ml_dataset(train_end="2024-12-31")
print(f"  Samples: {len(rows)}")
if len(rows) >= 50:
    arr=np.array(rows,dtype=np.float32); X=arr[:,:-1]; y=arr[:,-1]
    print(f"  Label balance: {y.mean():.1%} positive")
    X_tr,X_val,y_tr,y_val=train_test_split(X,y,test_size=0.2,random_state=42,stratify=y if y.mean()>0.05 else None)
    dtrain=lgb.Dataset(X_tr,label=y_tr,feature_name=FEATURE_NAMES,free_raw_data=False)
    dval  =lgb.Dataset(X_val,label=y_val,reference=dtrain,free_raw_data=False)
    params=dict(objective="binary",metric="auc",learning_rate=0.05,max_depth=4,
                num_leaves=15,min_child_samples=20,subsample=0.8,colsample_bytree=0.8,
                verbose=-1,seed=42)
    cb=[lgb.early_stopping(30,verbose=False),lgb.log_evaluation(period=-1)]
    booster=lgb.train(params,dtrain,num_boost_round=400,valid_sets=[dval],callbacks=cb)
    val_auc=roc_auc_score(y_val,booster.predict(X_val))
    print(f"  Val AUC: {val_auc:.4f}")
    fi=pd.Series(booster.feature_importance("gain"),index=FEATURE_NAMES).sort_values(ascending=False)
    print(f"  Top features: {', '.join(fi.head(5).index.tolist())}")
    ML_MODEL=booster
    HAS_ML=True
else:
    print("  Not enough WTF-signal bars to train ML — skipping ML filter")
    ML_MODEL=None; HAS_ML=False

# ══════════════════════════════════════════════════════════════════
# YEARLY ROBUSTNESS TEST — WTF PULLBACK STRATEGY
# ══════════════════════════════════════════════════════════════════
YEARS=[
    ("2023", "2023-01-01","2023-12-31"),
    ("2024", "2024-01-01","2024-12-31"),
    ("2025", "2025-01-01","2025-12-31"),
    ("2026", "2026-01-01","2026-10-10"),
]

print(f"\n{'='*105}")
print(f"  YEARLY ROBUSTNESS: Weekly Trend + Daily RSI Pullback  |  Rs.{CAPITAL:,.0f} account  |  4%R/trade")
print(f"{'='*105}")

all_rows=[]
for yr,s,e in YEARS:
    r=simulate(s,e)
    ml_r=None
    if HAS_ML and yr in ("2025","2026"):
        ml_r=simulate(s,e,ml_filter=ML_MODEL,ml_thresh=0.52)
    row={"Year":yr,"Trades":r["n"],"WR%":f"{r['wr']}%","PF":f"{r['pf']:.2f}",
         "Net":f"Rs.{r['net']:+,.0f}","ROI":f"{r['roi']:+.2f}%","AvgHold":f"{r['ahd']}d"}
    if ml_r:
        row["ML_Trades"]=ml_r["n"]
        row["ML_WR%"]=f"{ml_r['wr']}%"
        row["ML_PF"]=f"{ml_r['pf']:.2f}"
        row["ML_ROI"]=f"{ml_r['roi']:+.2f}%"
    all_rows.append(row)
    print(f"  {yr}: {r['n']} trades | WR={r['wr']}% | PF={r['pf']:.2f} | ROI={r['roi']:+.2f}% | AvgHold={r['ahd']}d", end="")
    if ml_r: print(f"  ||  ML-filtered: {ml_r['n']} trades | WR={ml_r['wr']}% | PF={ml_r['pf']:.2f} | ROI={ml_r['roi']:+.2f}%",end="")
    print()

print(f"\n{'='*105}")

# ── PARAMETER SENSITIVITY ON 2026 ────────────────────────────────
print(f"\n{'='*105}")
print(f"  PARAMETER SENSITIVITY (2026 only)")
print(f"{'='*105}")

grid=[
    ("Base (rsi_max=50, min_rr=1.5, max_pos=3)", dict(rsi_max=50,rsi_min=20,min_rr=1.5,max_pos=3)),
    ("Wider RSI (rsi_max=45)",                    dict(rsi_max=45,rsi_min=20,min_rr=1.5,max_pos=3)),
    ("Tighter RSI (rsi_max=40)",                  dict(rsi_max=40,rsi_min=15,min_rr=1.5,max_pos=3)),
    ("Higher min_rr=2.0",                         dict(rsi_max=50,rsi_min=20,min_rr=2.0,max_pos=3)),
    ("Lower min_rr=1.2",                          dict(rsi_max=50,rsi_min=20,min_rr=1.2,max_pos=3)),
    ("Max 2 positions",                           dict(rsi_max=50,rsi_min=20,min_rr=1.5,max_pos=2)),
    ("Max 5 positions",                           dict(rsi_max=50,rsi_min=20,min_rr=1.5,max_pos=5)),
    ("5% risk/trade",                             dict(rsi_max=50,rsi_min=20,min_rr=1.5,max_pos=3,risk_pct=5.0)),
]
sens_rows=[]
for name,params in grid:
    r=simulate("2026-01-01","2026-10-10",**params)
    sens_rows.append({"Setup":name,"Trades":r["n"],"WR%":f"{r['wr']}%","PF":f"{r['pf']:.2f}",
        "Net":f"Rs.{r['net']:+,.0f}","ROI":f"{r['roi']:+.2f}%"})
    print(f"  {name}: {r['n']} trades | WR={r['wr']}% | PF={r['pf']:.2f} | ROI={r['roi']:+.2f}%")

# ── STOCK BREAKDOWN for 2026 ─────────────────────────────────────
print(f"\n{'='*105}")
print(f"  STOCK BREAKDOWN (2026) — Which stocks drive the PnL?")
print(f"{'='*105}")
res_2026=simulate("2026-01-01","2026-10-10")
by_sym={}
for t in res_2026["trades"]:
    s=t["sym"]
    if s not in by_sym: by_sym[s]={"n":0,"net":0,"wins":0}
    by_sym[s]["n"]+=1; by_sym[s]["net"]+=t["net"]
    if t["net"]>0: by_sym[s]["wins"]+=1
sym_df=pd.DataFrame([{"Symbol":s,"Trades":v["n"],"Wins":v["wins"],
    "WR%":f"{v['wins']/v['n']*100:.0f}%","Net":f"Rs.{v['net']:+,.0f}"}
    for s,v in by_sym.items()]).sort_values("Net",ascending=False,key=lambda x:x.str.replace("Rs.","").str.replace(",","").str.replace("+","").astype(float))
print(sym_df.to_string(index=False))

print(f"\n{'='*105}")
print(f"  SUMMARY")
print(f"{'='*105}")
print(pd.DataFrame([{k:v for k,v in r.items()} for r in all_rows]).to_string(index=False))
print(f"\n  Interpretation:")
print(f"  - PF > 1.3 consistently across years = REAL edge, not luck")
print(f"  - WR ~30% + high RR (avg 2-3R) is a mathematically valid setup")
print(f"  - ML filter on WTF bars: higher threshold = fewer but better trades")
