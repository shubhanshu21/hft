"""Equity: what does being flat before Upstox's auto square-off cost? Upstox reports NSE equity MIS auto square-off between 15:00 (2021
announcement) and 15:15 (later sources); the bot's forced exit is 15:15 with entries allowed until then. Variants move both the last entry and
the forced exit earlier (minutes after the 09:15 open: 360 = 15:15, 345 = 15:00, 340 = 14:55, 330 = 14:45).

    python3 -m markets.equity.experiments.squareoff_study
"""
from __future__ import annotations

import contextlib
import io

from markets.equity.scalping import backtest as bt

SYMBOLS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "TATASTEEL", "ITC", "LT", "AXISBANK", "BHARTIARTL", "MARUTI"]
VARIANTS = [(360, "15:15 (current)"), (345, "15:00"), (340, "14:55"), (330, "14:45")]
HALVES = {"2022-08..2024-06": ("2022-08-01", "2024-06-30"), "2024-07..2026-09": ("2024-07-01", "2026-09-23")}


def _stats(trades):
    net = [t["net_pnl"] for t in trades]
    w, l = sum(v for v in net if v > 0), -sum(v for v in net if v <= 0)
    return len(net), (100 * sum(v > 0 for v in net) / len(net) if net else 0), (w / l if l else float("inf")), sum(net)


def main() -> None:
    keep = (bt._SQUAREOFF_MIN, bt._LAST_ENTRY_MIN)
    try:
        for mins, label in VARIANTS:
            bt._SQUAREOFF_MIN = bt._LAST_ENTRY_MIN = mins
            cells = []
            for half, (a, b) in HALVES.items():
                trades = []
                for s in SYMBOLS:
                    with contextlib.redirect_stdout(io.StringIO()):
                        r = bt.run_equity_backtest(symbols=[s], capital=100000.0, risk_pct=4.0, leverage=5.0, from_date=a, to_date=b, return_trades=True)
                    trades += r.get("trade_list") or []
                n, win, pf, net = _stats(trades)
                cells.append(f"{half}: n={n:4d} win={win:3.0f}% PF={pf:5.2f}")
            print(f"  last exit {label:16s} " + " | ".join(cells))
    finally:
        bt._SQUAREOFF_MIN, bt._LAST_ENTRY_MIN = keep


if __name__ == "__main__":
    main()
