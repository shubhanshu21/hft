"""How much of the gross edge do costs eat, and does a wider minimum stop (bigger targets vs fixed per-order costs) help?

    python3 -m markets.commodity.experiments.cost_share_study
"""
from __future__ import annotations

import contextlib
import io

from markets.commodity.scalping import backtest as bt

HALVES = {"first": ("2026-05-18", "2026-07-19"), "second": ("2026-07-20", "2026-09-23")}
SYMBOLS = {"CRUDEOILM": "crude", "GOLDM": "gold", "SILVER": "silver"}
MIN_STOP = [0.0035, 0.005, 0.0065, 0.008]                # current is 0.0035


def _cell(sym, a, b):
    with contextlib.redirect_stdout(io.StringIO()):
        t = bt.run_commodity_backtest(symbols=[sym], capital=100000.0, risk_pct=4.0, leverage=5.0, from_date=a, to_date=b,
                                      size_mode="margin", return_trades=True)["trade_list"]
    gross, fees = sum(x["gross_pnl"] for x in t), sum(x["total_fees"] for x in t)
    net = [x["net_pnl"] for x in t]
    w, l = sum(v for v in net if v > 0), -sum(v for v in net if v <= 0)
    return {"n": len(t), "gross": gross, "fees": fees, "net": gross - fees, "pf": w / l if l else float("inf")}


def main() -> None:
    print("COST SHARE at current settings (4 months):")
    for sym in SYMBOLS:
        c = [_cell(sym, *r) for r in HALVES.values()]
        g, f = sum(x["gross"] for x in c), sum(x["fees"] for x in c)
        print(f"  {sym:10s} trades={sum(x['n'] for x in c):3d}  gross Rs{g:+10.0f}  fees Rs{f:9.0f}  fees = {100 * f / g if g > 0 else float('nan'):5.0f}% of gross  net Rs{g - f:+10.0f}")
    print("\nWIDER MINIMUM STOP (min_stop_pct), per half:")
    base = {k: bt.ENTRY_THRESHOLDS[k]["min_stop_pct"] for k in SYMBOLS.values()}
    for v in MIN_STOP:
        for k in SYMBOLS.values():
            bt.ENTRY_THRESHOLDS[k]["min_stop_pct"] = v
        for sym, k in SYMBOLS.items():
            cells = [_cell(sym, *r) for r in HALVES.values()]
            print(f"  min_stop={v:.4f} {sym:10s} " + " | ".join(f"{h}: n={c['n']:3d} PF={c['pf']:5.2f} fees={c['fees']:7.0f} net={c['net']:+9.0f}" for h, c in zip(HALVES, cells)))
    for k, v in base.items():
        bt.ENTRY_THRESHOLDS[k]["min_stop_pct"] = v


if __name__ == "__main__":
    main()
