"""Does restricting direction (long-only) or the crude daily-regime gate help? Two halves, real thresholds, honest fills.

    python3 -m markets.commodity.experiments.direction_regime_study
"""
from __future__ import annotations

import contextlib
import io

from markets.commodity.scalping.backtest import run_commodity_backtest

HALVES = {"first": ("2026-05-18", "2026-07-19"), "second": ("2026-07-20", "2026-09-23")}
SYMBOLS = ["CRUDEOILM", "GOLDM", "SILVER"]
VARIANTS = {"both directions (current)": {}, "long only": {"long_only": True}, "crude regime gate on": {"use_crude_regime_filter": True}}


def _stats(trades):
    net = [t["net_pnl"] for t in trades]
    w, l = sum(x for x in net if x > 0), -sum(x for x in net if x <= 0)
    longs = sum(1 for t in trades if t["direction"] == "long")
    return f"n={len(net):3d} (long {longs:3d}) win={100 * sum(x > 0 for x in net) / max(len(net), 1):3.0f}% PF={(w / l if l else float('inf')):5.2f} net={sum(net):+9.0f}"


def main() -> None:
    for sym in SYMBOLS:
        print(f"\n== {sym}")
        for name, kw in VARIANTS.items():
            if "regime" in name and sym != "CRUDEOILM":
                continue                                   # the gate exists for crude only
            cells = []
            for h, (a, b) in HALVES.items():
                with contextlib.redirect_stdout(io.StringIO()):
                    r = run_commodity_backtest(symbols=[sym], capital=100000.0, risk_pct=4.0, leverage=5.0, from_date=a, to_date=b,
                                               size_mode="margin", return_trades=True, **kw)
                cells.append(f"{h}: {_stats(r['trade_list'])}")
            print(f"  {name:26s} " + " | ".join(cells))


if __name__ == "__main__":
    main()
