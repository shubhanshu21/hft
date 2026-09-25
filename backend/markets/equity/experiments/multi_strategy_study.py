"""NSE equity: does a LIBRARY of strategies picked by market phase beat the one live rule?  (4 years of real 5-min data, 49 NIFTY names.)

    python3 -m markets.equity.experiments.multi_strategy_study [--symbols A B ...] [--json out.json]

Same question as markets/commodity/experiments/regime_switch_study.py, but equity has ~4 years of 5-min history (2022-08 .. now), so the phase tests have real power.
Library: TREND = the live rule (markets/equity/scalping/backtest.py, incl. its ADX-scaled trailing exit); MR-A / MR-B = VWAP + RSI mean reversion (two strengths);
ORB / ORB-FADE = the first-30-minute range broken / faded once per stock per day. Each strategy is run through the SAME portfolio rules as live -- one Rs100,000 pool,
at most 3 concurrent positions across the universe, real Upstox MIS 5x, real cost model (STT, exchange, stamp, min(0.06%, Rs30) brokerage), risk 4% -- but on a FIXED
Rs100,000 (no compounding) so a strategy's daily P&L does not depend on how it did before, which is what lets the pickers be compared fairly.
Pickers (strictly walk-forward): performance switch (best trailing-L-day P&L, must be > 0) and regime map (trend / range from the prior 10 sessions' efficiency ratio of
the equal-weighted universe, strategy per regime learned on the first half, applied to the second). Results: docs/EQUITY_CURRENCY_MULTI_STRATEGY.md.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

from core import sessions
from core.paths import ARCHIVE_ROOT
from markets.commodity.experiments.regime_switch_study import max_drawdown, performance_switch, regime_map
from markets.equity.costs import compute_nse_equity_costs, size_equity_shares
from markets.equity.features import compute_equity_features
from markets.equity.scalping import backtest as eqbt
from markets.equity.universe import NIFTY50_SYMBOLS

ARCHIVE = ARCHIVE_ROOT / "equity"
CAPITAL, RISK_PCT, LEVERAGE, MAX_CONCURRENT = 100000.0, 4.0, 5.0, 3
ENTRY_FROM, ENTRY_TO = 15, sessions.EQUITY_LAST_ENTRY_SINCE_OPEN          # minutes since 09:15
SQUAREOFF = sessions.EQUITY_SQUAREOFF_SINCE_OPEN
MIN_STOP_PCT = 0.005
LOOKBACKS = (10, 20, 40)
ER_WINDOW = 10


def load(sym: str) -> pd.DataFrame:
    return compute_equity_features(pd.read_csv(ARCHIVE / f"{sym}_5minute.csv"))


# -- signals (+1 long, -1 short) on each bar's close ------------------------------------------------------------------------------------------------
def sig_meanrev(f: pd.DataFrame, x: float, rsi_lvl: float) -> np.ndarray:
    v, r = f["vwap_dist_pct"].to_numpy(), f["intraday_rsi"].to_numpy()
    return np.where((v <= -x) & (r <= rsi_lvl), 1, np.where((v >= x) & (r >= 100 - rsi_lvl), -1, 0))


def sig_orb(f: pd.DataFrame, fade: bool, range_bars: int = 6) -> np.ndarray:
    sig = np.zeros(len(f), dtype=int)
    days = f["timestamp"].astype(str).str[:10].to_numpy()
    hi, lo, close, mins = f["high"].to_numpy(), f["low"].to_numpy(), f["close"].to_numpy(), f["minutes_since_open"].to_numpy()
    start = 0
    for end in list(np.flatnonzero(days[1:] != days[:-1]) + 1) + [len(f)]:
        if end - start > range_bars + 1:
            h, l = hi[start:start + range_bars].max(), lo[start:start + range_bars].min()
            for i in range(start + range_bars, end):
                if not 30 <= mins[i] <= 225:
                    continue
                if close[i] > h:
                    sig[i] = -1 if fade else 1
                    break
                if close[i] < l:
                    sig[i] = 1 if fade else -1
                    break
        start = end
    return sig


def candidates(sym: str, f: pd.DataFrame, sig: np.ndarray, stop_mult: float, tp_mult: float, hold: int) -> list[dict]:
    """Independent single-symbol trades: enter at the signal bar's close; stop (checked first), take-profit, time limit or the forced exit."""
    close, hi, lo, atr = f["close"].to_numpy(), f["high"].to_numpy(), f["low"].to_numpy(), f["atr"].to_numpy()
    mins, ts = f["minutes_since_open"].to_numpy(), f["timestamp"].astype(str).to_numpy()
    out, busy_until = [], -1
    for i in np.flatnonzero(sig):
        if i <= busy_until or not ENTRY_FROM <= mins[i] <= ENTRY_TO or np.isnan(atr[i]):
            continue
        d, entry = int(sig[i]), float(close[i])
        sdist = max(stop_mult * float(atr[i]), MIN_STOP_PCT * entry)
        stop, tp = entry - d * sdist, entry + d * tp_mult * sdist
        exit_p, j = None, i + 1
        while j < len(close):
            if (d == 1 and lo[j] <= stop) or (d == -1 and hi[j] >= stop):
                exit_p = stop
            elif (d == 1 and hi[j] >= tp) or (d == -1 and lo[j] <= tp):
                exit_p = tp
            elif j - i >= hold or mins[j] >= SQUAREOFF or ts[j][:10] != ts[i][:10]:
                exit_p = float(close[j])
            if exit_p is not None:
                break
            j += 1
        if exit_p is None:
            break
        out.append({"symbol": sym, "direction": "long" if d == 1 else "short", "entry_time": ts[i], "exit_time": ts[j], "entry_price": entry,
                    "exit_price": exit_p, "stop_dist": sdist})
        busy_until = j
    return out


def pool(cands: list[dict]) -> list[dict]:
    """The live portfolio rules on a fixed Rs100,000: chronological, at most MAX_CONCURRENT open, sized to the free margin, real costs."""
    cands = sorted(cands, key=lambda c: str(c["entry_time"]))
    open_pos, trades, committed = [], [], 0.0
    for c in cands:
        keep = []
        for p in open_pos:
            if str(p["exit_time"]) <= str(c["entry_time"]):
                committed -= p["margin"]
            else:
                keep.append(p)
        open_pos = keep
        if len(open_pos) >= MAX_CONCURRENT or any(p["symbol"] == c["symbol"] for p in open_pos):
            continue
        avail = CAPITAL - committed
        qty = size_equity_shares(capital=avail, entry_price=c["entry_price"], stop_distance=c["stop_dist"], risk_pct=RISK_PCT, leverage=LEVERAGE)
        margin = c["entry_price"] * qty / LEVERAGE
        if qty < 1 or margin > avail:
            continue
        cost = compute_nse_equity_costs(c["direction"], c["entry_price"], c["exit_price"], qty)
        open_pos.append({**c, "qty": qty, "margin": margin})
        committed += margin
        trades.append({"day": str(c["entry_time"])[:10], "net_pnl": cost["net"], "gross": cost["gross"], "symbol": c["symbol"]})
    return trades


def daily(trades: list[dict], days: list[str]) -> np.ndarray:
    d: dict[str, float] = defaultdict(float)
    for t in trades:
        d[t["day"]] += t["net_pnl"]
    return np.array([d.get(x, 0.0) for x in days])


def trade_stats(trades: list[dict]) -> dict:
    net = np.array([t["net_pnl"] for t in trades]) if trades else np.array([0.0])
    w, l = net[net > 0].sum(), -net[net <= 0].sum()
    return {"n": len(trades), "net": round(float(net.sum())), "pf": round(float(w / l), 2) if l else 0.0, "win": round(100 * float((net > 0).mean()), 1)}


def composite_er(frames: dict[str, pd.DataFrame], days: list[str]) -> np.ndarray:
    """Efficiency ratio of the equal-weighted universe's daily closes over the previous ER_WINDOW sessions (known before day t opens)."""
    rets = []
    for f in frames.values():
        c = f.assign(day=f["timestamp"].astype(str).str[:10]).groupby("day")["close"].last().reindex(days)
        rets.append(np.log(c).diff())
    idx = np.nancumsum(np.nanmean(np.vstack([r.to_numpy() for r in rets]), axis=0))
    er = np.full(len(days), np.nan)
    for t in range(ER_WINDOW + 1, len(days)):
        seg = idx[t - 1 - ER_WINDOW:t]
        path = np.abs(np.diff(seg)).sum()
        er[t] = abs(seg[-1] - seg[0]) / path if path else 0.0
    return er


def run(symbols: list[str]) -> dict:
    frames = {s: load(s) for s in symbols}
    days = sorted({str(t)[:10] for f in frames.values() for t in f["timestamp"]})
    lib_cands: dict[str, list[dict]] = {"TREND": [], "MR-A": [], "MR-B": [], "ORB": [], "ORB-FADE": []}
    for s, f in frames.items():
        lib_cands["TREND"] += [{**c, "entry_time": str(c["entry_time"]), "exit_time": str(c["exit_time"])}
                               for c in eqbt._simulate_symbol_candidates(s, None, None, False, eqbt.ENTRY_THRESHOLDS)]
        lib_cands["MR-A"] += candidates(s, f, sig_meanrev(f, 0.6, 30), 1.4, 1.0, 24)
        lib_cands["MR-B"] += candidates(s, f, sig_meanrev(f, 1.0, 25), 1.4, 1.0, 24)
        lib_cands["ORB"] += candidates(s, f, sig_orb(f, False), 1.4, 1.8, 40)
        lib_cands["ORB-FADE"] += candidates(s, f, sig_orb(f, True), 1.4, 1.0, 40)
    trades = {k: pool(v) for k, v in lib_cands.items()}
    series = {k: daily(v, days) for k, v in trades.items()}
    years = sorted({d[:4] for d in days})
    out = {"symbols": len(symbols), "days": len(days), "first": days[0], "last": days[-1],
           "standalone": {k: {**trade_stats(v), "dd": round(max_drawdown(series[k]), 1)} for k, v in trades.items()},
           "by_year": {y: {k: round(float(series[k][[i for i, d in enumerate(days) if d[:4] == y]].sum())) for k in series} for y in years}}
    out["switch"] = {}
    for L in LOOKBACKS:
        pnl, picks = performance_switch(series, L)
        seg = {k: round(float(v[L:].sum())) for k, v in series.items()}
        out["switch"][L] = {"net": round(float(pnl.sum())), "dd": round(max_drawdown(pnl), 1), "days": len(pnl), "flat_days": picks.count("flat"),
                            "picks": {k: picks.count(k) for k in sorted(set(picks))}, "same_days_standalone": seg}
    er = composite_er(frames, days)
    rm = regime_map(series, er)
    test_idx = rm.pop("test_idx")
    pnl = rm.pop("test_pnl")
    out["regime"] = {**rm, "net": round(float(pnl.sum())), "dd": round(max_drawdown(pnl), 1), "same_days_standalone": {k: round(float(v[test_idx].sum())) for k, v in series.items()}}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=list(NIFTY50_SYMBOLS))
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    r = run(a.symbols)
    print(f"NSE equity multi-strategy, {r['symbols']} names, {r['first']}..{r['last']} ({r['days']} days), Rs{CAPITAL:,.0f} fixed, risk {RISK_PCT}%, {LEVERAGE}x, max {MAX_CONCURRENT} open  |  {datetime.now():%Y-%m-%d %H:%M}\n")
    print("Each strategy alone:")
    for k, s in r["standalone"].items():
        print(f"   {k:9s} trades {s['n']:6d}  net {s['net']:+10,}  PF {s['pf']:5.2f}  win {s['win']:5.1f}%  maxDD {s['dd']:5.1f}%")
    print("\nNet by year:")
    for y, row in r["by_year"].items():
        print(f"   {y}  " + "  ".join(f"{k} {v:+,}" for k, v in row.items()))
    for L, s in r["switch"].items():
        print(f"\nPERFORMANCE SWITCH (best of last {L} days): net {s['net']:+,}  maxDD {s['dd']}%  {s['days']} days, flat {s['flat_days']}  picks {s['picks']}")
        print("   same days, each alone: " + "  ".join(f"{k} {v:+,}" for k, v in s["same_days_standalone"].items()))
    g = r["regime"]
    print(f"\nREGIME MAP (ER split {g['threshold']}, learned on first half -> {g['chosen']}): TEST net {g['net']:+,}  maxDD {g['dd']}%  ({g['test_days']} days)")
    print("   TEST days, each alone: " + "  ".join(f"{k} {v:+,}" for k, v in g["same_days_standalone"].items()))
    print(f"   by regime TRAIN {g['train_table']}\n   by regime TEST  {g['test_table']}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(r, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
