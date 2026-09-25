"""The commodity multi-strategy / regime study on ~2.7 YEARS of intraday data, using the global prices MCX contracts follow.

    python3 -m services.data.dukascopy XAUUSD XAGUSD LIGHTCMDUSD GASCMDUSD --from 2024-01-01     # one-off download (resumable)
    python3 -m markets.commodity.experiments.global_proxy_study [--symbols GOLDTEN SILVERMIC CRUDEOILM NATGASMINI]

Upstox gives ~4 months of MCX intraday history, which cannot say whether strategies work in different market phases. Dukascopy's free 1-minute history (services/data/dukascopy.py) goes back
years. Here each series is turned into an MCX-LIKE 5-minute series -- price in rupees at the day's USDINR (Yahoo), in the MCX unit (gold per 10 g, silver per kg, crude per barrel, natgas per
mmBtu), restricted to the MCX session -- and fed through the SAME strategy library, costs, real-margin sizing and pickers as regime_switch_study.py. It is a PROXY: no MCX import-duty premium,
no MCX spread / liquidity, continuous global price. Treat a result as "does this idea survive 2.7 years of the underlying's behaviour", not as an MCX backtest.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

import pandas as pd

from core.paths import ARCHIVE_ROOT, CACHE_DIR
from markets.commodity.experiments import alt_strategy_study as alt
from markets.commodity.experiments import regime_switch_study as rss
from markets.commodity.scalping import backtest as bt
from services.data.yahoo import fetch_daily

GLOBAL_DIR = ARCHIVE_ROOT / "global"
SCRATCH = CACHE_DIR / "global_proxy"
OZ_G = 31.1035
# MCX contract -> (Dukascopy instrument, archive base name the backtest resolves, USD price -> MCX price multiplier before USDINR)
MAP = {
    "GOLDTEN": ("XAUUSD", "GOLD", 10.0 / OZ_G),            # INR per 10 g
    "SILVERMIC": ("XAGUSD", "SILVER", 1000.0 / OZ_G),      # INR per kg
    "CRUDEOILM": ("LIGHTCMDUSD", "CRUDEOIL", 1.0),         # INR per barrel (WTI)
    "NATGASMINI": ("GASCMDUSD", "NATURALGAS", 1.0),        # INR per mmBtu
}
START, END = "2024-01-01", "2026-09-24"


def to_mcx_like(mcx: str) -> pd.DataFrame:
    src, _, mult = MAP[mcx]
    df = pd.read_csv(GLOBAL_DIR / f"{src}_5minute.csv")
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata")
    fx = fetch_daily("INR=X")["close"]
    rate = fx.reindex(pd.to_datetime(ts.dt.date)).ffill().bfill().to_numpy()
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] * rate * mult
    hm = ts.dt.hour * 60 + ts.dt.minute
    keep = (ts.dt.weekday < 5) & (hm >= 9 * 60) & (hm < 23 * 60 + 30)                       # MCX session, weekdays
    df["timestamp"] = ts.dt.strftime("%Y-%m-%dT%H:%M:%S%z").str.replace(r"(\d\d)(\d\d)$", r"\1:\2", regex=True)
    return df[keep].reset_index(drop=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=list(MAP))
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for s in a.symbols:
        to_mcx_like(s).to_csv(SCRATCH / f"{MAP[s][1]}_5minute.csv", index=False)
    bt.ARCHIVE_DIR = SCRATCH                          # the live rule's backtest and the alt studies read this scratch copy, not the real MCX archive
    alt.ARCHIVE = SCRATCH
    rss.FULL = (START, END)
    rates = alt.real_rates()
    print(f"Global-price proxy, {START}..{END}, MCX session, Rs{alt.CAPITAL:,.0f} risk {alt.RISK_PCT}% real Upstox margin  |  {datetime.now():%Y-%m-%d %H:%M}\n")
    out = []
    for s in a.symbols:
        r = rss.study(s, rates)
        out.append(r)
        print(f"=== {s}  ({r['days']} days, leverage {r['leverage_used']}x)")
        print("  each alone:  " + "  ".join(f"{k} {v:+,}" for k, v in r["standalone_full"].items()))
        for y, row in r["by_year"].items():
            print(f"     {y}: " + "  ".join(f"{k} {v:+,}" for k, v in row.items()))
        for L, sw in r["switch"].items():
            print(f"  SWITCH L={L}: net {sw['net']:+,} ({sw['days']} days, flat {sw['flat_days']}) | same days each alone: " + "  ".join(f"{k} {v:+,}" for k, v in sw["same_days_standalone"].items()))
        g = r["regime"]
        print(f"  REGIME MAP {g['chosen']}: TEST net {g['net']:+,} ({g['test_days']} days) | each alone: " + "  ".join(f"{k} {v:+,}" for k, v in g["same_days_standalone"].items()))
        print(f"     by regime TRAIN {g['train_table']}\n     by regime TEST  {g['test_table']}\n")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(out, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
