"""Several strategies per symbol, chosen by what the market is doing -- validated only on data the choice never saw.

    python3 -m markets.commodity.experiments.regime_switch_study [--symbols CRUDEOILM ...] [--json out.json]

Hypothesis (from the user, 2026-09-25): a symbol goes through phases; one rule cannot work in all of them, so each symbol needs a LIBRARY of strategies and a way to
pick. Two pickers are tested, both strictly walk-forward (the choice for day t uses only days before t):

  1. PERFORMANCE SWITCH  -- on each day run the one strategy with the best net P&L over the previous L trading days (only if that trailing P&L is positive, else stay flat).
  2. REGIME MAP          -- label each day trend / range from the prior 10 sessions' efficiency ratio (|net move| / total path of daily closes); on the first half of
                            the days learn which strategy earns most in each regime; apply that map to the second half.

Library (fixed parameters, no per-strategy tuning -- fewer knobs, less curve-fitting): TREND = the live trend-breakout rule (backtest.py, incl. its break-even lock and
trail); MR-A / MR-B = VWAP mean reversion (two strengths); CH-BRK / CH-FADE = 15-min 20-bar channel traded with / against the break; ORB / ORB-FADE = the first-30-minute
range broken (or faded) once per day. Costs, real Upstox margin, full session, 10% risk on Rs100,000 exactly as alt_strategy_study.py. Each strategy runs on its own
Rs100,000 (P&L simply adds), which is generous to the switcher when two strategies would hold positions at once, so the switcher only ever runs ONE strategy per day.

Reference lines ("in hindsight") are upper bounds nobody could have chosen in advance; only the two pickers are honest.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from collections import defaultdict
from datetime import datetime

import numpy as np

from markets.commodity.experiments.alt_strategy_study import (
    CAPITAL, CONFIGURED_LEVERAGE, RISK_PCT, Bars, bars_15min, bars_5min, liquidity, MAX_ZERO_VOLUME_SHARE, real_rates, signal_channel, signal_meanrev, simulate)
from markets.commodity.scalping import backtest as bt

SYMBOLS = ["CRUDEOILM", "NATGASMINI", "SILVERMIC", "GOLDTEN"]
FULL = ("2026-05-18", "2026-09-24")
LOOKBACKS = (10, 20)
ER_WINDOW = 10


def signal_orb(b: Bars, fade: bool, range_bars: int = 6) -> np.ndarray:
    """One signal per day: the first close beyond the first `range_bars` 5-min bars' range, entries 09:30-13:00 IST (mins since open 30..240)."""
    sig = np.zeros(len(b.close), dtype=int)
    days = np.array([str(t)[:10] for t in b.ts])
    for day in dict.fromkeys(days):
        idx = np.flatnonzero(days == day)
        if len(idx) <= range_bars + 1:
            continue
        head = idx[:range_bars]
        hi, lo = b.high[head].max(), b.low[head].min()
        for i in idx[range_bars:]:
            if not 30 <= b.mins[i] <= 240:
                continue
            if b.close[i] > hi:
                sig[i] = -1 if fade else 1
                break
            if b.close[i] < lo:
                sig[i] = 1 if fade else -1
                break
    return sig


def daily_pnl(trades: list[dict]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for t in trades:
        out[str(t["entry"])[:10]] += t["net_pnl"]
    return out


def strategy_library(symbol: str, lev: float) -> tuple[dict, list[str], Bars]:
    b5, b15 = bars_5min(symbol), bars_15min(symbol)
    lib = {}
    trend_trades = []
    y0, y1 = int(FULL[0][:4]), int(FULL[1][:4])
    for y in range(y0, y1 + 1):                # the backtest compounds capital: a bad year would starve every later year of lots, so each calendar year restarts at Rs100,000
        a, b = (FULL[0] if y == y0 else f"{y}-01-01"), (FULL[1] if y == y1 else f"{y}-12-31")
        with contextlib.redirect_stdout(io.StringIO()):
            r = bt.run_commodity_backtest(symbols=[symbol], capital=CAPITAL, risk_pct=RISK_PCT, leverage=lev, from_date=a, to_date=b,
                                          return_trades=True, size_mode="margin", us_session_only=False)
        trend_trades += r.get("trade_list") or []
    lib["TREND"] = daily_pnl([{"entry": str(t["entry_time"]), "net_pnl": t["net_pnl"]} for t in trend_trades])
    lib["MR-A"] = daily_pnl(simulate(symbol, b5, signal_meanrev(b5, 0.5, 30, 25), 1.4, 1.8, 24, lev))
    lib["MR-B"] = daily_pnl(simulate(symbol, b5, signal_meanrev(b5, 0.8, 25, 99), 1.4, 1.0, 24, lev))
    lib["CH-BRK"] = daily_pnl(simulate(symbol, b15, signal_channel(b15, 20, False), 1.5, 2.0, 16, lev))
    lib["CH-FADE"] = daily_pnl(simulate(symbol, b15, signal_channel(b15, 20, True), 1.5, 2.0, 16, lev))
    lib["ORB"] = daily_pnl(simulate(symbol, b5, signal_orb(b5, False), 1.4, 2.0, 400, lev))
    lib["ORB-FADE"] = daily_pnl(simulate(symbol, b5, signal_orb(b5, True), 1.4, 1.0, 400, lev))
    return {k: dict(v) for k, v in lib.items()}, sorted({str(t)[:10] for t in b5.ts}), b5


def _series(lib: dict, days: list[str]) -> dict[str, np.ndarray]:
    return {k: np.array([v.get(d, 0.0) for d in days]) for k, v in lib.items()}


def max_drawdown(daily: np.ndarray) -> float:
    eq = CAPITAL + np.cumsum(daily)
    peak = np.maximum.accumulate(np.concatenate([[CAPITAL], eq]))[1:]
    return float(((peak - eq) / peak * 100).max()) if len(eq) else 0.0


def performance_switch(series: dict[str, np.ndarray], lookback: int) -> tuple[np.ndarray, list[str]]:
    """Day t runs the strategy with the best trailing-`lookback`-day net P&L (must be > 0), using days < t only. Returns the daily P&L (days < lookback are skipped) and picks."""
    n = len(next(iter(series.values())))
    pnl, picks = np.zeros(n), []
    for t in range(lookback, n):
        trail = {k: v[t - lookback:t].sum() for k, v in series.items()}
        best = max(trail, key=trail.get)
        if trail[best] > 0:
            pnl[t] = series[best][t]
            picks.append(best)
        else:
            picks.append("flat")
    return pnl[lookback:], picks


def efficiency_ratios(b5: Bars, days: list[str], window: int = ER_WINDOW) -> np.ndarray:
    """Per day t: |close(t-1) - close(t-1-window)| / sum |daily change| over those days -- known before day t opens. NaN until enough history."""
    last = {}
    for t, c in zip(b5.ts, b5.close):
        last[str(t)[:10]] = float(c)
    closes = np.array([last[d] for d in days])
    er = np.full(len(days), np.nan)
    for t in range(window + 1, len(days)):
        seg = closes[t - 1 - window:t]                     # closes of days t-1-window .. t-1
        path = np.abs(np.diff(seg)).sum()
        er[t] = abs(seg[-1] - seg[0]) / path if path else 0.0
    return er


def regime_map(series: dict[str, np.ndarray], er: np.ndarray) -> dict:
    """Learn on the first half of the eligible days which strategy earns most in TREND (ER above the train median) and in RANGE; apply on the second half."""
    ok = np.flatnonzero(~np.isnan(er))
    half = ok[: len(ok) // 2]
    thr = float(np.median(er[half]))
    labels = {"trend": lambda i: er[i] > thr, "range": lambda i: er[i] <= thr}
    chosen, table = {}, {}
    for name, f in labels.items():
        idx = [i for i in half if f(i)]
        table[name] = {k: round(float(v[idx].sum())) for k, v in series.items()}
        best = max(table[name], key=table[name].get)
        chosen[name] = best if table[name][best] > 0 else "flat"
    test = ok[len(ok) // 2:]
    pnl = np.array([0.0 if chosen["trend" if er[i] > thr else "range"] == "flat" else series[chosen["trend" if er[i] > thr else "range"]][i] for i in test])
    return {"threshold": round(thr, 3), "chosen": chosen, "train_table": table, "test_days": len(test), "test_pnl": pnl, "test_idx": test,
            "test_table": {name: {k: round(float(v[[i for i in test if f(i)]].sum())) for k, v in series.items()} for name, f in labels.items()}}


def study(symbol: str, rates: dict) -> dict:
    liq = liquidity(symbol)
    if liq["zero_volume_share"] > MAX_ZERO_VOLUME_SHARE:
        return {"symbol": symbol, "verdict": "ILLIQUID"}
    lev = min(CONFIGURED_LEVERAGE, rates[symbol]["leverage"])
    lib, days, b5 = strategy_library(symbol, lev)
    series = _series(lib, days)
    out = {"symbol": symbol, "days": len(days), "leverage_used": round(lev, 2), "standalone_full": {k: round(float(v.sum())) for k, v in series.items()},
           "trades_days": {k: int(sum(1 for x in v.values() if x)) for k, v in lib.items()}}
    years = sorted({d[:4] for d in days})
    out["by_year"] = {y: {k: round(float(v[[i for i, d in enumerate(days) if d[:4] == y]].sum())) for k, v in series.items()} for y in years}
    out["switch"] = {}
    for L in LOOKBACKS:
        pnl, picks = performance_switch(series, L)
        seg = {k: round(float(v[L:].sum())) for k, v in series.items()}
        out["switch"][L] = {"net": round(float(pnl.sum())), "dd": round(max_drawdown(pnl), 1), "days": len(pnl), "flat_days": picks.count("flat"),
                            "same_days_standalone": seg, "best_in_hindsight": max(seg.values()), "picks": {k: picks.count(k) for k in sorted(set(picks))}}
    er = efficiency_ratios(b5, days)
    rm = regime_map(series, er)
    test_idx = rm.pop("test_idx")
    out["regime"] = {**{k: v for k, v in rm.items() if k != "test_pnl"}, "net": round(float(rm["test_pnl"].sum())), "dd": round(max_drawdown(rm["test_pnl"]), 1),
                     "same_days_standalone": {k: round(float(v[test_idx].sum())) for k, v in series.items()}}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    rates = real_rates()
    print(f"Multi-strategy per symbol, {FULL[0]}..{FULL[1]}, Rs{CAPITAL:,.0f} risk {RISK_PCT}% real Upstox margin, full session  |  {datetime.now():%Y-%m-%d %H:%M}\n")
    results = []
    for sym in args.symbols:
        r = study(sym, rates)
        results.append(r)
        if "switch" not in r:
            print(f"{sym}: {r['verdict']}\n")
            continue
        print(f"=== {sym}  ({r['days']} trading days, leverage {r['leverage_used']}x)")
        print("  each strategy alone, whole period:   " + "  ".join(f"{k} {v:+,}" for k, v in r["standalone_full"].items()))
        for y, row in r.get("by_year", {}).items():
            print(f"     {y}: " + "  ".join(f"{k} {v:+,}" for k, v in row.items()))
        for L, s in r["switch"].items():
            print(f"  PERFORMANCE SWITCH, best of last {L} days: net {s['net']:+,} (max DD {s['dd']}%, {s['days']} days, flat {s['flat_days']}) | picks {s['picks']}")
            print(f"       same days, each alone: " + "  ".join(f"{k} {v:+,}" for k, v in s["same_days_standalone"].items()) + f"   | best in hindsight {s['best_in_hindsight']:+,}")
        g = r["regime"]
        print(f"  REGIME MAP (efficiency-ratio split {g['threshold']}; learned on 1st half -> {g['chosen']}): TEST net {g['net']:+,} (max DD {g['dd']}%, {g['test_days']} days)")
        print(f"       TEST days, each alone: " + "  ".join(f"{k} {v:+,}" for k, v in g["same_days_standalone"].items()))
        print(f"       by regime on TRAIN {g['train_table']}\n       by regime on TEST  {g['test_table']}\n")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"results": results}, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
