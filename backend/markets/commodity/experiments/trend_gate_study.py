"""Does trading the live TREND rule ONLY when the underlying is in a strong multi-day trend fix its losing years?  (global-price proxy, 2024-01..2026-09)

    python3 -m markets.commodity.experiments.trend_gate_study

Round 3 (docs/COMMODITY_ALT_STRATEGIES.md) found TREND lost on silver/crude in 2024 and 2025 and earned in 2026's strong trend. Hypothesis: a gate that keeps TREND flat unless the previous
`window` sessions were trending (efficiency ratio |net move| / total path of daily closes above a threshold) removes the losing years. Six pre-set gates (window 10/20 x threshold 0.25/0.35/0.45),
NO selection among them: all are shown, and a gate only counts as robust if it beats ungated TREND in EVERY calendar year, not just in total. The gate for day t uses closes before t only.
"""
from __future__ import annotations

import numpy as np

from markets.commodity.experiments import alt_strategy_study as alt
from markets.commodity.experiments import global_proxy_study as gp
from markets.commodity.experiments import regime_switch_study as rss
from markets.commodity.scalping import backtest as bt

WINDOWS, THRESHOLDS = (10, 20), (0.25, 0.35, 0.45)


def main() -> int:
    for s in gp.MAP:
        gp.to_mcx_like(s).to_csv(gp.SCRATCH / f"{gp.MAP[s][1]}_5minute.csv", index=False)
    bt.ARCHIVE_DIR = gp.SCRATCH
    alt.ARCHIVE = gp.SCRATCH
    rss.FULL = (gp.START, gp.END)
    rates = alt.real_rates()
    for sym in gp.MAP:
        lev = min(alt.CONFIGURED_LEVERAGE, rates[sym]["leverage"])
        lib, days, b5 = rss.strategy_library(sym, lev)
        trend = np.array([lib["TREND"].get(d, 0.0) for d in days])
        years = sorted({d[:4] for d in days})
        yi = {y: [i for i, d in enumerate(days) if d[:4] == y] for y in years}
        print(f"\n=== {sym}   ungated TREND: " + "  ".join(f"{y} {trend[yi[y]].sum():+,.0f}" for y in years) + f"   total {trend.sum():+,.0f}")
        for w in WINDOWS:
            er = rss.efficiency_ratios(b5, days, w)
            for th in THRESHOLDS:
                on = np.nan_to_num(er, nan=0.0) > th
                g = np.where(on, trend, 0.0)
                per = {y: g[yi[y]].sum() for y in years}
                better = all(per[y] >= trend[yi[y]].sum() for y in years)
                print(f"   gate ER{w}>{th:.2f}: on {on.mean() * 100:4.0f}% of days | " + "  ".join(f"{y} {per[y]:+,.0f}" for y in years) + f" | total {g.sum():+,.0f}" + ("   <- beats ungated in EVERY year" if better else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
