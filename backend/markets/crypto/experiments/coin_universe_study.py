"""Does adding more coins to the live BTC/ETH/SOL blend help?  (docs/CRYPTO_ALL_WEATHER.md, "More coins")

    python3 -m markets.crypto.experiments.coin_universe_study

Needs the 1h + funding archive of the extra coins: services.data.binance.topup / topup_funding for BNB XRP ADA DOGE LINK AVAX. Live blend logic, 1.0x, equal weights, alt cost 0.15%/side.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from markets.crypto import router, backtest as b
from markets.crypto.experiments.momentum_study import load
BASE=("BTCUSDT","ETHUSDT","SOLUSDT"); EXTRA=("BNBUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","LINKUSDT","AVAXUSDT")
ALL=BASE+EXTRA
data={s:load(s,"1h") for s in ALL}
for s in EXTRA: b.COST_SIDE.setdefault(s,0.0015)
# per-coin returns from the LIVE blend logic (router anchored on BTC), 1.0x
tg=router.blend_series(data,0.5)
rets={s:b.coin_returns(s,data[s],tg[s])[0] for s in ALL}
bh={s:data[s]["close"].pct_change().fillna(0.0) for s in ALL}
start=max(d.index[0] for d in data.values())+pd.Timedelta(days=200)
R=pd.concat(rets,axis=1).loc[start:]; BH=pd.concat(bh,axis=1).loc[start:]
def st(r):
    eq=(1+r).cumprod(); y=len(r)/b.BPY
    return dict(cagr=100*(eq.iloc[-1]**(1/y)-1), sh=r.mean()/r.std()*np.sqrt(b.BPY), dd=100*((eq.cummax()-eq)/eq.cummax()).max())
def row(lab,cols):
    r=R[list(cols)].mean(axis=1); a=st(r); tr=st(r.loc[:"2023-12-31"]); te=st(r.loc["2024-01-01":]); l90=r.iloc[-90*24:]
    print(f"{lab:26s}| all {a['cagr']:+6.1f}%/yr sharpe {a['sh']:.2f} DD {a['dd']:4.0f}% | train(<=2023) {tr['cagr']:+6.1f}% sh {tr['sh']:.2f} | test(2024+) {te['cagr']:+6.1f}% sh {te['sh']:.2f} DD {te['dd']:3.0f}% | 2025+ {st(r.loc['2025-01-01':])['cagr']:+6.1f}%")
print("window",start.date(),"->",R.index[-1].date(),"; blend 50/50 at 1.0x, equal weights, real costs+funding\n")
row("BTC ETH SOL (live)",BASE)
for e in EXTRA: row(f"live + {e[:-4]}",BASE+(e,))
row("live + BNB XRP LINK",BASE+("BNBUSDT","XRPUSDT","LINKUSDT"))
row("all 9 coins",ALL)
print("\nEach extra coin alone (blend):")
for e in EXTRA: row(e[:-4]+" alone",(e,))
print("\nBuy&hold equal-weight: live 3 vs all 9:")
for lab,c in (("live 3",BASE),("all 9",ALL)):
    r=BH[list(c)].mean(axis=1); a=st(r); print(f"  {lab}: {a['cagr']:+.1f}%/yr sharpe {a['sh']:.2f} DD {a['dd']:.0f}%")
print("\nCorrelation of daily blend returns (live 3 + extras):"); d=R.resample('1D').sum().corr().round(2); print(d.to_string())
