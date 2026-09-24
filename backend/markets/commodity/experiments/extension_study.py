"""Does skipping entries that are already extended (far from VWAP / high RSI in the trade's direction) help? Halves, real thresholds.

Post-hoc screen on the backtest's own trade list: a filtered trade is simply removed. That ignores that skipping a trade would
let a later one open sooner, so it is a first look, not a validation.

    python3 -m markets.commodity.experiments.extension_study
"""
from __future__ import annotations

import contextlib
import io

from markets.commodity.scalping.backtest import run_commodity_backtest

HALVES = {"first": ("2026-05-18", "2026-07-19"), "second": ("2026-07-20", "2026-09-23")}
SYMBOLS = ["CRUDEOILM", "GOLDM", "SILVER"]
CAPS = [(None, None), (1.5, None), (1.0, None), (0.75, None), (None, 85), (None, 80), (1.0, 85)]   # (max directional VWAP dist %, max directional RSI)


def _trades(sym, frm, to):
    with contextlib.redirect_stdout(io.StringIO()):
        return run_commodity_backtest(symbols=[sym], capital=100000.0, risk_pct=4.0, leverage=5.0, from_date=frm, to_date=to,
                                      size_mode="margin", return_trades=True)["trade_list"]


def _keep(t, vcap, rcap) -> bool:
    d = 1 if t["direction"] == "long" else -1
    v, r = (t.get("vwap_dist") or 0.0) * d, t.get("rsi")
    r = 50.0 if r is None else (r if d == 1 else 100 - r)
    return (vcap is None or v <= vcap) and (rcap is None or r <= rcap)


def main() -> None:
    data = {(s, h): _trades(s, *rng) for s in SYMBOLS for h, rng in HALVES.items()}
    for sym in SYMBOLS:
        print(f"\n== {sym}")
        for vcap, rcap in CAPS:
            cells = []
            for h in HALVES:
                kept = [t for t in data[(sym, h)] if _keep(t, vcap, rcap)]
                net = [t["net_pnl"] for t in kept]
                w, l = sum(x for x in net if x > 0), -sum(x for x in net if x <= 0)
                cells.append(f"{h}: n={len(net):3d}/{len(data[(sym, h)]):3d} net={sum(net):+9.0f} PF={(w / l if l else float('inf')):5.2f}")
            print(f"  vwap<={str(vcap):5s} rsi<={str(rcap):5s} | " + " | ".join(cells))


if __name__ == "__main__":
    main()
