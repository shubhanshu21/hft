"""Fixed entry thresholds vs thresholds that FOLLOW the market.  (docs/DYNAMIC_THRESHOLDS.md)

    python3 -m markets.commodity.experiments.dynamic_thresholds_study

The live rule uses fixed numbers (ADX >= 22, volume surge >= 1.3, |EMA slope| >= 0.05 ...), tuned on one market phase. `adaptive_window` (opt-in in markets/commodity/strategies/scalping/backtest.py) replaces each of those three
by the rolling quantile of the SAME feature over the previous N bars, at the quantile the fixed number represents over the whole sample: identical selectivity, but the level rises in busy markets and falls in quiet ones.
Windows: 1, 3, 5, 10 trading days (174 5-minute bars per MCX day). Judged like the other studies: net Rs on the real MCX halves and on the 2.7-year proxy by calendar year (each year restarts at Rs100k).
"""
from __future__ import annotations

import contextlib
import io

from markets.commodity.experiments import alt_strategy_study as alt
from markets.commodity.experiments import global_proxy_study as gp
from markets.commodity.strategies.scalping import backtest as bt

DAY = 174
VARIANTS = {"fixed (live)": {}, "dynamic 1 day": {"adaptive_window": DAY}, "dynamic 3 days": {"adaptive_window": 3 * DAY}, "dynamic 5 days": {"adaptive_window": 5 * DAY}, "dynamic 10 days": {"adaptive_window": 10 * DAY}}
HALVES = {"first": ("2026-05-18", "2026-07-31"), "second": ("2026-08-01", "2026-09-24")}
YEARS = {"2024": ("2024-01-01", "2024-12-31"), "2025": ("2025-01-01", "2025-12-31"), "2026": ("2026-01-01", "2026-09-24")}


def net(sym, lev, w, **kw):
    with contextlib.redirect_stdout(io.StringIO()):
        r = bt.run_commodity_backtest(symbols=[sym], capital=alt.CAPITAL, risk_pct=alt.RISK_PCT, leverage=lev, from_date=w[0], to_date=w[1], return_trades=True, size_mode="margin", us_session_only=False, **kw)
    tl = r.get("trade_list") or []
    return len(tl), round(sum(t["net_pnl"] for t in tl))


def main() -> int:
    rates, real = alt.real_rates(), bt.ARCHIVE_DIR
    for sym in ("SILVERMIC", "CRUDEOILM", "GOLDTEN", "NATGASMINI"):
        lev = min(alt.CONFIGURED_LEVERAGE, rates[sym]["leverage"])
        bt.ARCHIVE_DIR = real
        R = {v: {h: net(sym, lev, w, **kw) for h, w in HALVES.items()} for v, kw in VARIANTS.items()}
        bt.ARCHIVE_DIR = gp.SCRATCH
        gp.to_mcx_like(sym).to_csv(gp.SCRATCH / f"{gp.MAP[sym][1]}_5minute.csv", index=False)
        P = {v: {y: net(sym, lev, w, **kw) for y, w in YEARS.items()} for v, kw in VARIANTS.items()}
        print(f"\n=== {sym}   net Rs (trades)")
        print(f"   {'variant':16s} | {'REAL 1st':>15s} {'REAL 2nd':>15s} | {'PROXY 2024':>15s} {'2025':>15s} {'2026':>15s} | verdict")
        for v in VARIANTS:
            rr, pp = R[v], P[v]
            wins = sum(rr[h][1] > R["fixed (live)"][h][1] for h in HALVES)
            yrs = sum(pp[y][1] > P["fixed (live)"][y][1] for y in YEARS)
            verdict = "-" if v == "fixed (live)" else ("IMPROVES" if wins == 2 and yrs >= 2 else f"no (real {wins}/2, proxy {yrs}/3)")
            cells = [f"{rr[h][1]:+8,} ({rr[h][0]:3d})" for h in HALVES] + [f"{pp[y][1]:+8,} ({pp[y][0]:3d})" for y in YEARS]
            print(f"   {v:16s} | {cells[0]:>15s} {cells[1]:>15s} | {cells[2]:>15s} {cells[3]:>15s} {cells[4]:>15s} | {verdict}")
    bt.ARCHIVE_DIR = real
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
