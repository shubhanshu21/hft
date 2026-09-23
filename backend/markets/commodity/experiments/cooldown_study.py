"""Does blocking re-entry on the same side after an initial stop improve results? Train/test halves, real thresholds.

    python3 -m markets.commodity.experiments.cooldown_study
"""
from __future__ import annotations

import contextlib
import io

from markets.commodity.scalping.backtest import run_commodity_backtest

HALVES = {"first (May18-Jul19)": ("2026-05-18", "2026-07-19"), "second (Jul20-Sep23)": ("2026-07-20", "2026-09-23")}
SYMBOLS = ["CRUDEOILM", "GOLDM", "SILVER"]
COOLDOWNS = [0, 3, 6, 12]          # 5-minute bars


def _run(sym: str, frm: str, to: str, cool: int) -> dict:
    with contextlib.redirect_stdout(io.StringIO()):
        return run_commodity_backtest(symbols=[sym], capital=100000.0, risk_pct=4.0, leverage=5.0, from_date=frm, to_date=to,
                                      size_mode="margin", stop_cooldown_bars=cool, return_trades=True)


def main() -> None:
    for half, (frm, to) in HALVES.items():
        print(f"\n== {half}")
        for sym in SYMBOLS:
            for cool in COOLDOWNS:
                res = _run(sym, frm, to, cool)
                trades = res.get("trade_list", [])
                net = [t["net_pnl"] for t in trades]
                w, l = sum(x for x in net if x > 0), -sum(x for x in net if x <= 0)
                print(f"  {sym:10s} cooldown={cool * 5:3d}min  n={len(net):3d}  win={100 * sum(x > 0 for x in net) / max(len(net), 1):3.0f}%  "
                      f"net=Rs{sum(net):+9.0f}  PF={(w / l if l else float('inf')):.2f}")


if __name__ == "__main__":
    main()
