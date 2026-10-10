"""Every static number in the commodity rule, next to a dynamic version of it.  (docs/STATIC_VS_DYNAMIC.md)

    python3 -m markets.commodity.experiments.static_vs_dynamic_study [--only commodity|equity|currency]

Static numbers found in the live rule and the dynamic alternative tested (each an opt-in parameter, off by default):
  entry levels ADX / volume surge / EMA slope   -> rolling quantile of the same feature over the last 3 / 10 trading days     (adaptive_window)
  exit shape (TP 1.8R, break-even 0.6R, trail 0.3R) -> the equity scalper's ADX-scaled exit: activation 0.6R/scale, trail 0.3R*scale of the CURRENT ATR, no fixed TP   (exit_mode="dynamic")
  take-profit distance 1.8R                     -> 1.8R x ADX scale                                                            (exit_mode="tp_scaled")
  time limit 16 bars (80 min)                   -> 16 bars x ADX scale                                                         (hold_mode="adx")
  minimum stop 0.35% of price                   -> 1.0 x the median ATR of the previous 5 days                                 (stop_floor_mode="dynamic")
  all of the above together                     -> "everything dynamic"
Judged as before: net Rs on the real MCX halves and on the 2.7-year proxy by calendar year; "improves" = beats the static rule in BOTH real halves and >= 2 of 3 proxy years.
"""
from __future__ import annotations

import argparse
import contextlib
import io

from markets.commodity.experiments import alt_strategy_study as alt
from markets.commodity.experiments import global_proxy_study as gp
from markets.commodity.strategies.scalping import backtest as bt

DAY = 174
VARIANTS = {
    "static (live)": {},
    "entry levels 3d": {"adaptive_window": 3 * DAY},
    "entry levels 10d": {"adaptive_window": 10 * DAY},
    "dynamic exit": {"exit_mode": "dynamic"},
    "TP x ADX scale": {"exit_mode": "tp_scaled"},
    "time limit x ADX": {"hold_mode": "adx"},
    "stop floor dynamic": {"stop_floor_mode": "dynamic"},
    "EVERYTHING dynamic": {"adaptive_window": 5 * DAY, "exit_mode": "dynamic", "hold_mode": "adx", "stop_floor_mode": "dynamic"},
}
HALVES = {"first": ("2026-05-18", "2026-07-31"), "second": ("2026-08-01", "2026-09-24")}
YEARS = {"2024": ("2024-01-01", "2024-12-31"), "2025": ("2025-01-01", "2025-12-31"), "2026": ("2026-01-01", "2026-09-24")}


def net(sym, lev, w, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        r = bt.run_commodity_backtest(symbols=[sym], capital=alt.CAPITAL, risk_pct=alt.RISK_PCT, leverage=lev, from_date=w[0], to_date=w[1], return_trades=True, size_mode="margin", us_session_only=False, **kw)
    tl = r.get("trade_list") or []
    return len(tl), round(sum(t["net_pnl"] for t in tl))


def commodity() -> None:
    rates, real = alt.real_rates(), bt.ARCHIVE_DIR
    for sym in ("SILVERMIC", "CRUDEOILM", "GOLDTEN", "NATGASMINI"):
        lev = min(alt.CONFIGURED_LEVERAGE, rates[sym]["leverage"])
        bt.ARCHIVE_DIR = real
        R = {v: {h: net(sym, lev, w, **kw) for h, w in HALVES.items()} for v, kw in VARIANTS.items()}
        bt.ARCHIVE_DIR = gp.SCRATCH
        gp.to_mcx_like(sym).to_csv(gp.SCRATCH / f"{gp.MAP[sym][1]}_5minute.csv", index=False)
        P = {v: {y: net(sym, lev, w, **kw) for y, w in YEARS.items()} for v, kw in VARIANTS.items()}
        print(f"\n=== {sym}   net Rs (trades)")
        print(f"   {'variant':20s} | {'REAL 1st':>15s} {'REAL 2nd':>15s} | {'PROXY 2024':>15s} {'2025':>15s} {'2026':>15s} | verdict")
        base = "static (live)"
        for v in VARIANTS:
            rr, pp = R[v], P[v]
            wins = sum(rr[h][1] > R[base][h][1] for h in HALVES)
            yrs = sum(pp[y][1] > P[base][y][1] for y in YEARS)
            verdict = "-" if v == base else ("IMPROVES" if wins == 2 and yrs >= 2 else f"no (real {wins}/2, proxy {yrs}/3)")
            cells = [f"{rr[h][1]:+8,} ({rr[h][0]:3d})" for h in HALVES] + [f"{pp[y][1]:+8,} ({pp[y][0]:3d})" for y in YEARS]
            print(f"   {v:20s} | {cells[0]:>15s} {cells[1]:>15s} | {cells[2]:>15s} {cells[3]:>15s} {cells[4]:>15s} | {verdict}", flush=True)
    bt.ARCHIVE_DIR = real


def equity() -> None:
    from markets.equity.experiments import multi_strategy_study as ms
    from markets.equity.strategies.scalping import backtest as eqbt
    from markets.equity.universe import NIFTY50_SYMBOLS
    EQ = {"static (live)": {}, "entry levels 3d": {"adaptive_window": 225}, "entry levels 10d": {"adaptive_window": 750}, "time limit x ADX": {"hold_mode": "adx"},
          "stop floor dynamic": {"stop_floor_mode": "dynamic"}, "EVERYTHING dynamic": {"adaptive_window": 375, "hold_mode": "adx", "stop_floor_mode": "dynamic"}}
    rows = {}
    for v, kw in EQ.items():
        cands = []
        for sym in NIFTY50_SYMBOLS:
            cands += [{**c, "entry_time": str(c["entry_time"]), "exit_time": str(c["exit_time"])} for c in eqbt._simulate_symbol_candidates(sym, None, None, False, eqbt.ENTRY_THRESHOLDS, **kw)]
        tr = ms.pool(cands)
        by = {}
        for t in tr:
            by[t["day"][:4]] = by.get(t["day"][:4], 0.0) + t["net_pnl"]
        rows[v] = (by, sum(t["net_pnl"] for t in tr if t["day"] < "2024-09-01"), sum(t["net_pnl"] for t in tr if t["day"] >= "2024-09-01"), ms.trade_stats(tr))
        print(f"  ...{v}", flush=True)
    print("\n=== EQUITY 49 names, live portfolio rules, fixed Rs100k   net Rs by year")
    print(f"   {'variant':20s} | " + " ".join(f"{y:>9s}" for y in ("2022", "2023", "2024", "2025", "2026")) + f" | {'1st half':>9s} {'2nd half':>9s} | {'total':>9s} {'trades':>6s} {'PF':>5s} | verdict")
    b_by, b1, b2, _ = rows["static (live)"]
    for v, (by, f1, f2, st) in rows.items():
        yrs = sum(by.get(y, 0) > b_by.get(y, 0) for y in ("2022", "2023", "2024", "2025", "2026"))
        halves = int(f1 > b1) + int(f2 > b2)
        verdict = "-" if v == "static (live)" else ("IMPROVES" if yrs >= 4 and halves == 2 else f"no (years {yrs}/5, halves {halves}/2)")
        print(f"   {v:20s} | " + " ".join(f"{by.get(y, 0):+9,.0f}" for y in ("2022", "2023", "2024", "2025", "2026")) + f" | {f1:+9,.0f} {f2:+9,.0f} | {st['net']:+9,d} {st['n']:6d} {st['pf']:5.2f} | {verdict}", flush=True)


def currency() -> None:
    from markets.currency.strategies.scalping import backtest as cbt
    CV = {"static (live)": {}, "entry levels 3d": {"adaptive_window": 288}, "entry levels 10d": {"adaptive_window": 960}, "dynamic exit": {"exit_mode": "dynamic"}, "TP x ADX scale": {"exit_mode": "tp_scaled"},
          "time limit x ADX": {"hold_mode": "adx"}, "stop floor dynamic": {"stop_floor_mode": "dynamic"},
          "EVERYTHING dynamic": {"adaptive_window": 480, "exit_mode": "dynamic", "hold_mode": "adx", "stop_floor_mode": "dynamic"}}
    print("\n=== USDINR at the live 10x, real Upstox data (only 4 months exist)   net Rs (trades)")
    print(f"   {'variant':20s} | {'1st 06-02..07-31':>18s} {'2nd 08-01..09-24':>18s} | verdict")
    base = None
    for v, kw in CV.items():
        cells = []
        for a, b in (("2026-06-02", "2026-07-31"), ("2026-08-01", "2026-09-24")):
            with contextlib.redirect_stdout(io.StringIO()):
                r = cbt.run_currency_backtest(symbols=["USDINR"], capital=100000, risk_pct=10, leverage=10.0, from_date=a, to_date=b, return_trades=True, size_mode="margin", **kw)
            tl = r.get("trade_list") or []
            cells.append((len(tl), round(sum(t["net_pnl"] for t in tl))))
        base = base or cells
        wins = sum(c[1] > b0[1] for c, b0 in zip(cells, base))
        print(f"   {v:20s} | {cells[0][1]:+10,} ({cells[0][0]:3d}) {cells[1][1]:+10,} ({cells[1][0]:3d}) | {'-' if v == 'static (live)' else ('IMPROVES' if wins == 2 else f'no ({wins}/2)')}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["commodity", "equity", "currency"])
    a = ap.parse_args()
    if a.only in (None, "commodity"):
        commodity()
    if a.only in (None, "currency"):
        currency()
    if a.only in (None, "equity"):
        equity()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
