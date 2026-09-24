"""USDINR: what does being flat BEFORE Upstox's 16:30 auto square-off cost? (currency entry gate / forced exit, minutes after the 09:00 open)

Upstox auto-squares-off currency MIS at 16:30 IST and charges for it. The bot's exit is 16:50 and entries run to 16:40, i.e. entries and exits
that a real account would have had done FOR it. Two halves, corrected Upstox costs, real per-symbol leverage cap.

    python3 -m markets.currency.experiments.squareoff_study
"""
from __future__ import annotations

import contextlib
import io

from core import sessions
from engine import margin_rates
from markets.currency.scalping.backtest import run_currency_backtest

HALVES = {"first": ("2026-05-18", "2026-07-19"), "second": ("2026-07-20", "2026-09-23"), "all": ("2026-05-18", "2026-09-23")}
# (last entry minute, forced exit minute): 460/470 = 16:40/16:50 (current), then variants inside 16:30
VARIANTS = [(460, 470, "16:40 / 16:50 (current)"), (430, 445, "16:10 / 16:25"), (400, 440, "15:40 / 16:20"), (360, 420, "15:00 / 16:00")]


def _stats(t):
    net = [x["net_pnl"] for x in t]
    w, l = sum(v for v in net if v > 0), -sum(v for v in net if v <= 0)
    eq = peak = 100000.0
    dd = 0.0
    for v in net:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)
    return len(net), (100 * sum(v > 0 for v in net) / len(net) if net else 0), (w / l if l else float("inf")), sum(net), dd


def main() -> None:
    keep = (sessions.CURRENCY_LAST_ENTRY_MIN, sessions.CURRENCY_SQUAREOFF_MIN)
    try:
        for lev_name, cfg in (("5x", 5.0), ("Upstox max 42x", 99.0)):
            lev = margin_rates.cap_leverage("USDINR", cfg)
            print(f"\n=== USDINR at {lev_name} (effective {lev:.1f}x)")
            for last, sq, label in VARIANTS:
                sessions.CURRENCY_LAST_ENTRY_MIN, sessions.CURRENCY_SQUAREOFF_MIN = last, sq
                cells = []
                for half, (a, b) in HALVES.items():
                    with contextlib.redirect_stdout(io.StringIO()):
                        r = run_currency_backtest(symbols=["USDINR"], capital=100000.0, risk_pct=10.0, leverage=lev, from_date=a, to_date=b,
                                                  size_mode="margin", return_trades=True)
                    n, win, pf, net, dd = _stats(r["trade_list"])
                    cells.append(f"{half}: n={n:3d} PF={pf:5.2f} net={net:+8.0f} dd={dd:4.1f}%")
                print(f"  entries to / exit {label:24s} " + " | ".join(cells))
    finally:
        sessions.CURRENCY_LAST_ENTRY_MIN, sessions.CURRENCY_SQUAREOFF_MIN = keep


if __name__ == "__main__":
    main()
