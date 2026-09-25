"""The 'do not chase' pullback entry (core/entry_pullback.py) tested on EVERYTHING we trade.  (docs/FIVE_MINUTE_SIGNAL_SCREEN.md, round 2)

    python3 -m markets.commodity.experiments.pullback_everything_study [--only commodity|currency|equity]

Variants: baseline (live: enter at the signal) vs a resting limit `frac` of a stop distance better than the signal, valid 3 bars, with two fill assumptions -- "touch" (filled when price reaches the limit; optimistic) and
"through 0.1" (price must trade 0.1 stop-distances THROUGH it; a queue-priority haircut). frac in 0.15 / 0.25 / 0.35 / 0.5 / 0.75.
  Commodity  CRUDEOILM SILVERMIC GOLDTEN NATGASMINI: real MCX first / second half, and the 2.7-year global-price proxy by calendar year (capital restarts each year). Live risk 10%, leverage min(10, Upstox real), full session.
  Currency   USDINR at the live 10x: real Upstox data first / second half (only 4 months exist).
  Equity     49 NIFTY names, 2022-08..2026-09: portfolio phase as live (one Rs100k pool, max 3 open, real 5x MIS, real costs) on FIXED capital so calendar years are independent; per year, two halves, PF.
"Improves" = beats baseline net in BOTH real halves and >= 2 of 3 proxy years (commodity); both halves (currency); >= 4 of 5 calendar years and both halves (equity).
"""
from __future__ import annotations

import argparse
import contextlib
import io

import numpy as np

from markets.commodity.experiments import alt_strategy_study as alt
from markets.commodity.experiments import global_proxy_study as gp
from markets.commodity.scalping import backtest as bt

FRACS = (0.15, 0.25, 0.35, 0.5, 0.75)
VARIANTS = {"baseline": {}}
for th, lab in ((0.0, "touch"), (0.1, "through0.1")):
    for f in FRACS:
        VARIANTS[f"{f:.2f} {lab}"] = {"pullback_frac": f, "pullback_through": th}
HALVES = {"first": ("2026-05-18", "2026-07-31"), "second": ("2026-08-01", "2026-09-24")}
YEARS = {"2024": ("2024-01-01", "2024-12-31"), "2025": ("2025-01-01", "2025-12-31"), "2026": ("2026-01-01", "2026-09-24")}


def comm_net(sym, lev, w, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        r = bt.run_commodity_backtest(symbols=[sym], capital=alt.CAPITAL, risk_pct=alt.RISK_PCT, leverage=lev, from_date=w[0], to_date=w[1], return_trades=True, size_mode="margin", us_session_only=False, **kw)
    tl = r.get("trade_list") or []
    return len(tl), round(sum(t["net_pnl"] for t in tl))


def commodity() -> None:
    rates, real = alt.real_rates(), bt.ARCHIVE_DIR
    for sym in ("CRUDEOILM", "SILVERMIC", "GOLDTEN", "NATGASMINI"):
        lev = min(alt.CONFIGURED_LEVERAGE, rates[sym]["leverage"])
        bt.ARCHIVE_DIR = real
        R = {v: {h: comm_net(sym, lev, w, **kw) for h, w in HALVES.items()} for v, kw in VARIANTS.items()}
        bt.ARCHIVE_DIR = gp.SCRATCH
        gp.to_mcx_like(sym).to_csv(gp.SCRATCH / f"{gp.MAP[sym][1]}_5minute.csv", index=False)
        P = {v: {y: comm_net(sym, lev, w, **kw) for y, w in YEARS.items()} for v, kw in VARIANTS.items()}
        print(f"\n=== {sym} (leverage {lev:.1f}x)   net Rs (trades)")
        print(f"   {'variant':16s} | {'REAL 1st':>15s} {'REAL 2nd':>15s} | {'PROXY 2024':>15s} {'2025':>15s} {'2026':>15s} | verdict")
        for v in VARIANTS:
            rr, pp = R[v], P[v]
            wins = sum(rr[h][1] > R["baseline"][h][1] for h in HALVES)
            yr = sum(pp[y][1] > P["baseline"][y][1] for y in YEARS)
            verdict = "-" if v == "baseline" else ("IMPROVES" if wins == 2 and yr >= 2 else f"no (real {wins}/2, proxy {yr}/3)")
            cells = [f"{rr[h][1]:+8,} ({rr[h][0]:3d})" for h in HALVES] + [f"{pp[y][1]:+8,} ({pp[y][0]:3d})" for y in YEARS]
            print(f"   {v:16s} | {cells[0]:>15s} {cells[1]:>15s} | {cells[2]:>15s} {cells[3]:>15s} {cells[4]:>15s} | {verdict}")
    bt.ARCHIVE_DIR = real


def currency() -> None:
    from markets.currency.scalping import backtest as cbt
    print("\n=== USDINR at the live 10x, real Upstox data   net Rs (trades)")
    print(f"   {'variant':16s} | {'1st 06-02..07-31':>18s} {'2nd 08-01..09-24':>18s} | verdict")
    base = None
    for v, kw in VARIANTS.items():
        cells = []
        for a, b in (("2026-06-02", "2026-07-31"), ("2026-08-01", "2026-09-24")):
            with contextlib.redirect_stdout(io.StringIO()):
                r = cbt.run_currency_backtest(symbols=["USDINR"], capital=100000, risk_pct=10, leverage=10.0, from_date=a, to_date=b, return_trades=True, size_mode="margin", **kw)
            tl = r.get("trade_list") or []
            cells.append((len(tl), round(sum(t["net_pnl"] for t in tl))))
        base = base or cells
        wins = sum(c[1] > b0[1] for c, b0 in zip(cells, base))
        print(f"   {v:16s} | {cells[0][1]:+10,} ({cells[0][0]:3d}) {cells[1][1]:+10,} ({cells[1][0]:3d}) | {'-' if v == 'baseline' else ('IMPROVES' if wins == 2 else f'no ({wins}/2)')}")


def equity() -> None:
    from markets.equity.experiments import multi_strategy_study as ms
    from markets.equity.scalping import backtest as eqbt
    from markets.equity.universe import NIFTY50_SYMBOLS
    rows = {}
    for v, kw in VARIANTS.items():
        cands = []
        for s in NIFTY50_SYMBOLS:
            cands += [{**c, "entry_time": str(c["entry_time"]), "exit_time": str(c["exit_time"])} for c in eqbt._simulate_symbol_candidates(s, None, None, False, eqbt.ENTRY_THRESHOLDS, **kw)]
        tr = ms.pool(cands)
        by = {}
        for t in tr:
            by[t["day"][:4]] = by.get(t["day"][:4], 0.0) + t["net_pnl"]
        first = sum(t["net_pnl"] for t in tr if t["day"] < "2024-09-01")
        second = sum(t["net_pnl"] for t in tr if t["day"] >= "2024-09-01")
        rows[v] = (by, first, second, ms.trade_stats(tr))
        print(f"  ...{v}", flush=True)
    print("\n=== EQUITY 49 names, fixed Rs100k, max 3 open   net Rs by calendar year (trades, PF)")
    print(f"   {'variant':16s} | " + " ".join(f"{y:>9s}" for y in ("2022", "2023", "2024", "2025", "2026")) + f" | {'1st half':>9s} {'2nd half':>9s} | {'total':>9s} {'trades':>6s} {'PF':>5s} | verdict")
    b_by, b_first, b_second, b_stats = rows["baseline"]
    for v, (by, first, second, st) in rows.items():
        yrs = sum(by.get(y, 0) > b_by.get(y, 0) for y in ("2022", "2023", "2024", "2025", "2026"))
        halves = int(first > b_first) + int(second > b_second)
        verdict = "-" if v == "baseline" else ("IMPROVES" if yrs >= 4 and halves == 2 else f"no (years {yrs}/5, halves {halves}/2)")
        print(f"   {v:16s} | " + " ".join(f"{by.get(y, 0):+9,.0f}" for y in ("2022", "2023", "2024", "2025", "2026")) + f" | {first:+9,.0f} {second:+9,.0f} | {st['net']:+9,d} {st['n']:6d} {st['pf']:5.2f} | {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["commodity", "currency", "equity"])
    ap.add_argument("--through", nargs="+", type=float, help="fill haircuts (stop-distances price must trade THROUGH the limit); default 0 and 0.1")
    ap.add_argument("--fracs", nargs="+", type=float, help="pullback depths (stop-distances); default 0.15 0.25 0.35 0.5 0.75")
    a = ap.parse_args()
    if a.through or a.fracs:
        VARIANTS.clear()
        VARIANTS["baseline"] = {}
        for th in (a.through or (0.0, 0.1)):
            for f in (a.fracs or FRACS):
                VARIANTS[f"{f:.2f} thr{th:g}"] = {"pullback_frac": f, "pullback_through": th}
    for name, fn in (("commodity", commodity), ("currency", currency), ("equity", equity)):
        if a.only in (None, name):
            fn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
