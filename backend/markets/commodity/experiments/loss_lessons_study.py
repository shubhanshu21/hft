"""Do the lessons from the losing trades actually fix anything?  (docs/COMMODITY_LOSS_LESSONS.md)

    python3 -m markets.commodity.experiments.loss_lessons_study

Lessons tested (each is an opt-in parameter of markets/commodity/scalping/backtest.py, off by default = live behaviour):
  1. Most losers reverse at once (64-79% never reach 0.3R)       -> CONFIRM: wait 1 or 2 bars and enter only if the breakout held (no bar back through half a stop; last close still beyond the signal close).
  2. Costs decide viability (crude: gross Rs148 < fees Rs169)     -> COST GATE: skip a signal whose round-trip costs exceed 0.10 / 0.15 / 0.20 of the risked amount.
Judged on (a) the real MCX archive in two halves (first 2026-05-18..07-31, second 08-01..09-24) and (b) the 2.7-year global-price proxy, each calendar year restarting at Rs100,000.
A variant counts as an improvement only if it beats the baseline net in BOTH real halves AND in at least 2 of the 3 proxy years. Live settings: full session, 10% risk, leverage = min(10x, Upstox real).
"""
from __future__ import annotations

import contextlib
import io

import numpy as np

from markets.commodity.experiments import alt_strategy_study as alt
from markets.commodity.experiments import global_proxy_study as gp
from markets.commodity.scalping import backtest as bt

SYMBOLS = ["CRUDEOILM", "SILVERMIC", "GOLDTEN", "NATGASMINI"]
VARIANTS = {"baseline": {}, "confirm 1 bar": {"confirm_bars": 1}, "confirm 2 bars": {"confirm_bars": 2},
            "cost gate 0.10": {"max_cost_r": 0.10}, "cost gate 0.15": {"max_cost_r": 0.15}, "cost gate 0.20": {"max_cost_r": 0.20}}
HALVES = {"first": ("2026-05-18", "2026-07-31"), "second": ("2026-08-01", "2026-09-24")}
YEARS = {"2024": ("2024-01-01", "2024-12-31"), "2025": ("2025-01-01", "2025-12-31"), "2026": ("2026-01-01", "2026-09-24")}


def net(sym: str, lev: float, window: tuple[str, str], **kw) -> tuple[int, int]:
    with contextlib.redirect_stdout(io.StringIO()):
        r = bt.run_commodity_backtest(symbols=[sym], capital=alt.CAPITAL, risk_pct=alt.RISK_PCT, leverage=lev, from_date=window[0], to_date=window[1],
                                      return_trades=True, size_mode="margin", us_session_only=False, **kw)
    tl = r.get("trade_list") or []
    return len(tl), round(sum(t["net_pnl"] for t in tl))


def main() -> int:
    rates = alt.real_rates()
    real_dir = bt.ARCHIVE_DIR
    results = {}
    for sym in SYMBOLS:
        lev = min(alt.CONFIGURED_LEVERAGE, rates[sym]["leverage"])
        bt.ARCHIVE_DIR = real_dir
        real = {v: {h: net(sym, lev, w, **kw) for h, w in HALVES.items()} for v, kw in VARIANTS.items()}
        bt.ARCHIVE_DIR = gp.SCRATCH                           # proxy years (scratch copies written by global_proxy_study)
        gp.to_mcx_like(sym).to_csv(gp.SCRATCH / f"{gp.MAP[sym][1]}_5minute.csv", index=False)
        prox = {v: {y: net(sym, lev, w, **kw) for y, w in YEARS.items()} for v, kw in VARIANTS.items()}
        results[sym] = (real, prox)
        print(f"\n=== {sym}  (leverage {lev:.1f}x)")
        print(f"   {'variant':16s} | {'REAL first half':>18s} {'REAL second half':>18s} | {'PROXY 2024':>16s} {'2025':>16s} {'2026':>16s} | verdict")
        b_real, b_prox = real["baseline"], prox["baseline"]
        for v in VARIANTS:
            rr, pp = real[v], prox[v]
            beats_real = all(rr[h][1] > b_real[h][1] for h in HALVES)
            beats_prox = sum(pp[y][1] > b_prox[y][1] for y in YEARS)
            verdict = "-" if v == "baseline" else ("IMPROVES" if beats_real and beats_prox >= 2 else f"no (real {sum(rr[h][1] > b_real[h][1] for h in HALVES)}/2, proxy {beats_prox}/3)")
            cells = [f"n={rr[h][0]:3d} {rr[h][1]:+8,}" for h in HALVES] + [f"n={pp[y][0]:3d} {pp[y][1]:+8,}" for y in YEARS]
            print(f"   {v:16s} | {cells[0]:>18s} {cells[1]:>18s} | {cells[2]:>16s} {cells[3]:>16s} {cells[4]:>16s} | {verdict}")
    bt.ARCHIVE_DIR = real_dir
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
