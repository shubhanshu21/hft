"""Equity trend rule: how much of its edge can risk size and the concurrent-position cap turn into profit, and at what drawdown?  (4 years, 49 names, compounding)

    python3 -m markets.equity.experiments.scaling_study

Candidates are the LIVE rule's trades (markets/equity/scalping/backtest.py, one shared threshold set, ADX-scaled trailing exit), generated once; each variant re-runs the SAME portfolio
phase as the real backtest -- chronological, one shared pool that COMPOUNDS, at most `cap` open positions, sized to the free margin (real Upstox MIS 5x), real costs -- with a different risk % / cap.
Reported: net %, max drawdown, and the split 2022-08..2024-08 (first) vs 2024-09..2026-09 (second) so a variant has to hold in both, not just overall.
"""
from __future__ import annotations

import numpy as np

from markets.equity.costs import compute_nse_equity_costs, size_equity_shares
from markets.equity.scalping import backtest as eqbt
from markets.equity.universe import NIFTY50_SYMBOLS

CAPITAL, LEVERAGE = 100000.0, 5.0
SPLIT = "2024-09-01"


def pool(cands: list[dict], risk_pct: float, cap: int, slot_frac: float = 1.0) -> list[dict]:
    """slot_frac: max share of the pool's CURRENT equity one position may commit as margin (1.0 = the live behaviour: the first trade may take everything)."""
    cands = sorted(cands, key=lambda c: str(c["entry_time"]))
    equity, committed, open_pos, out = CAPITAL, 0.0, [], []

    def close(p):
        nonlocal equity, committed
        net = compute_nse_equity_costs(p["direction"], p["entry_price"], p["exit_price"], p["qty"])["net"]
        equity += net
        committed -= p["margin"]
        out.append({"day": str(p["exit_time"])[:10], "net": net, "equity": equity})

    for c in cands:
        still = []
        for p in open_pos:
            if str(p["exit_time"]) <= str(c["entry_time"]):
                close(p)
            else:
                still.append(p)
        open_pos = still
        if len(open_pos) >= cap:
            continue
        avail = min(equity - committed, equity * slot_frac)
        if avail <= 0:
            continue
        qty = size_equity_shares(capital=avail, entry_price=c["entry_price"], stop_distance=c["stop_dist"], risk_pct=risk_pct, leverage=LEVERAGE)
        margin = c["entry_price"] * qty / LEVERAGE
        if qty < 1 or margin > avail:
            continue
        open_pos.append({**c, "qty": qty, "margin": margin})
        committed += margin
    for p in sorted(open_pos, key=lambda p: str(p["exit_time"])):
        close(p)
    return out


def summarize(trades: list[dict]) -> dict:
    eq = np.array([CAPITAL] + [t["equity"] for t in trades])
    peak = np.maximum.accumulate(eq)
    first = [t for t in trades if t["day"] < SPLIT]
    second = [t for t in trades if t["day"] >= SPLIT]
    e1 = first[-1]["equity"] if first else CAPITAL
    e2 = second[-1]["equity"] if second else e1
    net = np.array([t["net"] for t in trades])
    years = (len(set(t["day"][:7] for t in trades)) or 1) / 12
    return {"n": len(trades), "final": eq[-1], "ret": (eq[-1] / CAPITAL - 1) * 100, "dd": float(((peak - eq) / peak).max() * 100),
            "first": (e1 / CAPITAL - 1) * 100, "second": (e2 / e1 - 1) * 100, "pf": float(net[net > 0].sum() / max(-net[net <= 0].sum(), 1)), "cagr": ((eq[-1] / CAPITAL) ** (1 / years) - 1) * 100}


def slot_table(cands) -> None:
    print("\nPer-position margin cap (risk 4%, cap on concurrent positions = 5): does the first trade hog the pool?")
    print(f"{'max margin / position':>22s} | {'trades':>6s} {'return':>9s} {'CAGR':>6s} {'maxDD':>6s} {'PF':>5s} | {'1st 2y':>8s} {'2nd 2y':>8s}")
    for frac in (1.0, 0.5, 0.34, 0.25, 0.2):
        s = summarize(pool(cands, 4, 5, frac))
        print(f"{frac * 100:20.0f}% | {s['n']:6d} {s['ret']:+8.0f}% {s['cagr']:+5.0f}% {s['dd']:5.1f}% {s['pf']:5.2f} | {s['first']:+7.0f}% {s['second']:+7.0f}%{'   <- live' if frac == 1.0 else ''}")


def main() -> int:
    cands = []
    for s in NIFTY50_SYMBOLS:
        cands += [{**c, "entry_time": str(c["entry_time"]), "exit_time": str(c["exit_time"])} for c in eqbt._simulate_symbol_candidates(s, None, None, False, eqbt.ENTRY_THRESHOLDS)]
    print(f"{len(cands):,} candidate trades from the live rule, 49 names, 2022-08..2026-09, Rs{CAPITAL:,.0f} compounding, real 5x MIS\n")
    print(f"{'risk%':>5s} {'cap':>3s} | {'trades':>6s} {'final Rs':>11s} {'return':>8s} {'CAGR':>6s} {'maxDD':>6s} {'PF':>5s} | {'1st 2y':>8s} {'2nd 2y':>8s}")
    import sys
    for risk in ((2, 4, 6, 8) if "--full" in sys.argv else ()):
        for cap in (2, 3, 4, 5):
            s = summarize(pool(cands, risk, cap))
            tag = "   <- live" if (risk, cap) == (4, 3) else ""
            print(f"{risk:5d} {cap:3d} | {s['n']:6d} {s['final']:11,.0f} {s['ret']:+7.0f}% {s['cagr']:+5.0f}% {s['dd']:5.1f}% {s['pf']:5.2f} | {s['first']:+7.0f}% {s['second']:+7.0f}%{tag}")
    slot_table(cands)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
