"""NSE currency (USDINR): several strategies picked by phase, plus a slower-timeframe test on 2 years of proxy data.

    python3 -m markets.currency.experiments.multi_strategy_study [--json out.json]

Data: Upstox serves ~4 months of USDINR 5-min bars (2026-06-02 ..; monthly-expiry contracts). Yahoo's USDINR=X spot gives 2 YEARS of hourly bars (14k valid): a PROXY for the
NSE futures (spot trades 24h, NSE 09:00-17:00; only the NSE hours are used), good enough to ask whether a slower channel strategy has any edge over a longer window.

  A. 5-min library on the real Upstox data (7 strategies: TREND = live rule, MR-A/MR-B, CH-BRK/CH-FADE on 15-min, ORB/ORB-FADE) and the same walk-forward pickers as
     regime_switch_study.py. Only ~80 trading days, so this is low-power by construction -- it says what to expect, not a verdict.
  B. Hourly proxy, Dec-2023 .. now: 20-bar Donchian channel with / against the break, train (first 60% of days) / test (last 40%), same rules as alt_strategy_study.py.

Costs / sizing: markets/currency/costs (NCD: no STT/CTT, exchange + stamp + GST, Upstox brokerage min(0.06%, Rs30 cap)), real leverage min(5x configured, Upstox's real), 10% risk on a
fixed Rs100,000. Results: docs/EQUITY_CURRENCY_MULTI_STRATEGY.md.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests

from core import sessions
from core.paths import ARCHIVE_ROOT
from markets.commodity.experiments import alt_strategy_study as alt
from markets.commodity.experiments.regime_switch_study import efficiency_ratios, max_drawdown, performance_switch, regime_map, signal_orb, _series
from markets.currency.costs import compute_ncd_currency_costs, size_currency_lots
from markets.currency.scalping import backtest as cbt

SYMBOL = "USDINR"
ARCHIVE = ARCHIVE_ROOT / "currency"
LEVERAGE = 5.0                                                     # the configured cap (CURRENCY_LEVERAGE); Upstox's real value is 42x
ENTRY_FROM, ENTRY_TO = 15, sessions.CURRENCY_LAST_ENTRY_MIN         # minutes since the 09:00 open
SQUAREOFF = sessions.CURRENCY_SQUAREOFF_MIN
MIN_STOP_PCT = cbt.ENTRY_THRESHOLDS["USDINR"]["min_stop_pct"] if hasattr(cbt, "ENTRY_THRESHOLDS") else 0.0006
KW = dict(cost_fn=compute_ncd_currency_costs, size_fn=size_currency_lots, entry_from=ENTRY_FROM, entry_to=ENTRY_TO, squareoff=SQUAREOFF, min_stop_pct=MIN_STOP_PCT)


def sim(b, sig, stop, tp, hold):
    return alt.simulate(SYMBOL, b, sig, stop, tp, hold, LEVERAGE, **KW)


def yahoo_hourly() -> pd.DataFrame:
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/USDINR=X", params={"interval": "1h", "range": "730d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30).json()["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    df = pd.DataFrame({"timestamp": pd.to_datetime(r["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata"), "high": q["high"], "low": q["low"], "close": q["close"]}).dropna()
    hm = df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute
    return df[(hm >= 9 * 60) & (hm < 16 * 60 + 30)].reset_index(drop=True)          # NSE currency hours (bars that START inside the window)


def hourly_bars(df: pd.DataFrame) -> alt.Bars:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    hm = (df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute - 540 + 60).to_numpy()
    return alt.Bars(df["timestamp"].astype(str).to_numpy(), df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(), atr.to_numpy(), hm,
                    {"high_s": df["high"], "low_s": df["low"]})


def part_a() -> dict:
    raw = pd.read_csv(ARCHIVE / f"{SYMBOL}_5minute.csv")
    b5, b15 = alt.bars_5min(SYMBOL, raw=raw, feature_symbol=SYMBOL), alt.bars_15min(SYMBOL, raw=raw)
    days = sorted({str(t)[:10] for t in b5.ts})
    with contextlib.redirect_stdout(io.StringIO()):
        r = cbt.run_currency_backtest(symbols=[SYMBOL], capital=alt.CAPITAL, risk_pct=alt.RISK_PCT, leverage=LEVERAGE, return_trades=True, size_mode="margin")
    per = lambda tr: {d: sum(t["net_pnl"] for t in tr if str(t["entry"])[:10] == d) for d in {str(t["entry"])[:10] for t in tr}}
    lib = {"TREND": per([{"entry": str(t["entry_time"]), "net_pnl": t["net_pnl"]} for t in (r.get("trade_list") or [])])}
    lib["MR-A"] = per(sim(b5, alt.signal_meanrev(b5, 0.08, 30, 25), 1.4, 1.8, 24))
    lib["MR-B"] = per(sim(b5, alt.signal_meanrev(b5, 0.15, 25, 99), 1.4, 1.0, 24))
    lib["CH-BRK"] = per(sim(b15, alt.signal_channel(b15, 20, False), 1.5, 2.0, 16))
    lib["CH-FADE"] = per(sim(b15, alt.signal_channel(b15, 20, True), 1.5, 2.0, 16))
    lib["ORB"] = per(sim(b5, signal_orb(b5, False), 1.4, 2.0, 400))
    lib["ORB-FADE"] = per(sim(b5, signal_orb(b5, True), 1.4, 1.0, 400))
    series = _series(lib, days)
    out = {"days": len(days), "first": days[0], "last": days[-1], "standalone": {k: round(float(v.sum())) for k, v in series.items()},
           "trades_days": {k: int(sum(1 for x in v.values() if x)) for k, v in lib.items()}, "switch": {}}
    for L in (10, 20):
        pnl, picks = performance_switch(series, L)
        out["switch"][L] = {"net": round(float(pnl.sum())), "days": len(pnl), "flat": picks.count("flat"),
                            "same_days_standalone": {k: round(float(v[L:].sum())) for k, v in series.items()}}
    er = efficiency_ratios(b5, days)
    rm = regime_map(series, er)
    ti = rm.pop("test_idx")
    pnl = rm.pop("test_pnl")
    out["regime"] = {**rm, "net": round(float(pnl.sum())), "same_days_standalone": {k: round(float(v[ti].sum())) for k, v in series.items()}}
    return out


def part_b() -> dict:
    df = yahoo_hourly()
    b = hourly_bars(df)
    days = sorted({str(t)[:10] for t in b.ts})
    cut = days[int(len(days) * alt.TRAIN_FRACTION)]
    out = {"bars": len(df), "days": len(days), "first": days[0], "last": days[-1], "split": cut, "rows": []}
    for n in (12, 20, 32):
        for fade in (False, True):
            for stop, tp in ((1.5, 2.0), (2.5, 3.0)):
                tr = sim(b, alt.signal_channel(b, n, fade), stop, tp, 8)
                out["rows"].append({"n": n, "fade": fade, "stop": stop, "tp": tp,
                                    "train": alt.stats([t for t in tr if t["entry"][:10] < cut]), "test": alt.stats([t for t in tr if t["entry"][:10] >= cut])})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    print(f"USDINR multi-strategy  |  Rs{alt.CAPITAL:,.0f}, risk {alt.RISK_PCT}%, {LEVERAGE}x  |  {datetime.now():%Y-%m-%d %H:%M}\n")
    A = part_a()
    print(f"A. Real Upstox 5-min data {A['first']}..{A['last']} ({A['days']} days)")
    print("   each strategy alone: " + "  ".join(f"{k} {v:+,} ({A['trades_days'][k]}d)" for k, v in A["standalone"].items()))
    for L, s in A["switch"].items():
        print(f"   PERFORMANCE SWITCH L={L}: net {s['net']:+,} over {s['days']} days (flat {s['flat']}); same days each alone: " + "  ".join(f"{k} {v:+,}" for k, v in s["same_days_standalone"].items()))
    g = A["regime"]
    print(f"   REGIME MAP {g['chosen']} (ER split {g['threshold']}): TEST net {g['net']:+,} over {g['test_days']} days; each alone: " + "  ".join(f"{k} {v:+,}" for k, v in g["same_days_standalone"].items()))
    B = part_b()
    print(f"\nB. Hourly proxy (Yahoo USDINR=X, NSE hours) {B['first']}..{B['last']} ({B['days']} days, split {B['split']}): 20-bar-ish channel, with / against the break")
    for r in B["rows"]:
        tr, te = r["train"], r["test"]
        print(f"   N={r['n']:2d} {'FADE' if r['fade'] else 'BRK ':4s} stop {r['stop']} tp {r['tp']}: TRAIN n={tr['n']:4d} net {tr['net']:+9,} PF {tr['pf']:5.2f} | TEST n={te['n']:4d} net {te['net']:+9,} PF {te['pf']:5.2f}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"A": A, "B": B}, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
