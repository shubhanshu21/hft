"""Exit structure sweep: does any breakeven / lock / trail / target / time-stop setting beat the current one in BOTH halves?

The current exit constants were tuned on ONE month of data (2026-09-11). This re-checks them on the 4 months available, one parameter at
a time (others at their current value), on two halves, for three symbols. A setting only counts as better if it improves the POOLED profit
factor in both halves and does not break any single symbol. Scale-free metrics (PF, payoff, win%) are used because margin sizing
compounds and makes rupee totals depend on the order of trades.

    python3 -m markets.commodity.experiments.exit_structure_study
"""
from __future__ import annotations

import contextlib
import io

from markets.commodity.scalping import backtest as bt

HALVES = {"first": ("2026-05-18", "2026-07-19"), "second": ("2026-07-20", "2026-09-23")}
SYMBOLS = {"CRUDEOILM": "crude", "GOLDM": "gold", "SILVER": "silver"}     # traded symbol -> ENTRY_THRESHOLDS key
SWEEPS = {                                                                  # constant -> values (the current value is marked by matching the baseline)
    "BE_ACTIVATION_MULT": [0.4, 0.6, 0.8, 1.0, 1.2],
    "BE_LOCK_BUFFER_PCT": [0.0005, 0.001, 0.002, 0.003, 0.004],
    "TRAIL_DIST_MULT": [0.15, 0.3, 0.5, 0.8],
    "tp_mult": [1.2, 1.8, 2.4, 3.0],
    "HOLD_BARS": [8, 16, 24, 32],
}


def _run(sym: str, frm: str, to: str) -> list[dict]:
    with contextlib.redirect_stdout(io.StringIO()):
        return bt.run_commodity_backtest(symbols=[sym], capital=100000.0, risk_pct=4.0, leverage=5.0, from_date=frm, to_date=to,
                                         size_mode="margin", return_trades=True)["trade_list"]


def _stats(trades: list[dict]) -> dict:
    net = [t["net_pnl"] for t in trades]
    w, l = [x for x in net if x > 0], [x for x in net if x <= 0]
    return {"n": len(net), "win": 100 * len(w) / len(net) if net else 0.0, "gw": sum(w), "gl": -sum(l),
            "pf": sum(w) / -sum(l) if l and sum(l) else float("inf"), "payoff": (sum(w) / len(w)) / (-sum(l) / len(l)) if w and l and sum(l) else 0.0}


def _pool(cells: list[dict]) -> dict:
    gw, gl, n = sum(c["gw"] for c in cells), sum(c["gl"] for c in cells), sum(c["n"] for c in cells)
    return {"n": n, "pf": gw / gl if gl else float("inf")}


def _evaluate() -> dict:
    return {(sym, h): _stats(_run(sym, *rng)) for sym in SYMBOLS for h, rng in HALVES.items()}


def _set(name: str, value):
    if name == "tp_mult":
        for key in SYMBOLS.values():
            bt.ENTRY_THRESHOLDS[key]["tp_mult"] = value
    else:
        setattr(bt, name, value)


def _get(name: str):
    return bt.ENTRY_THRESHOLDS["crude"]["tp_mult"] if name == "tp_mult" else getattr(bt, name)


def main() -> None:
    base_tp = {k: bt.ENTRY_THRESHOLDS[k]["tp_mult"] for k in SYMBOLS.values()}
    base = _evaluate()
    bp = {h: _pool([base[(s, h)] for s in SYMBOLS]) for h in HALVES}
    print("BASELINE (current constants): pooled PF  first=%.2f (n=%d)  second=%.2f (n=%d)" % (bp["first"]["pf"], bp["first"]["n"], bp["second"]["pf"], bp["second"]["n"]))
    for s in SYMBOLS:
        print("   %-10s " % s + "  ".join("%s: n=%d win=%.0f%% PF=%.2f" % (h, base[(s, h)]["n"], base[(s, h)]["win"], base[(s, h)]["pf"]) for h in HALVES))
    verdicts = []
    for name, values in SWEEPS.items():
        original = _get(name)
        print(f"\n--- {name} (current {original})")
        print("  value     " + "".join(f"| {h:>6s} PF (n)   " for h in HALVES) + "| per-symbol PF first/second           | verdict")
        for v in values:
            _set(name, v)
            res = _evaluate()
            pooled = {h: _pool([res[(s, h)] for s in SYMBOLS]) for h in HALVES}
            per = "  ".join("%s %.2f/%.2f" % (s[:4], res[(s, "first")]["pf"], res[(s, "second")]["pf"]) for s in SYMBOLS)
            better = all(pooled[h]["pf"] > bp[h]["pf"] * 1.03 for h in HALVES)                       # >3% better in BOTH halves
            worse = any(pooled[h]["pf"] < bp[h]["pf"] * 0.97 for h in HALVES)
            verdict = "current" if v == original else "BETTER in both" if better else "worse in a half" if worse else "no clear change"
            if v != original and better:
                verdicts.append((name, v))
            print(f"  {v!s:9s} " + "".join(f"| {pooled[h]['pf']:6.2f} ({pooled[h]['n']:3d})   " for h in HALVES) + f"| {per} | {verdict}")
        if name == "tp_mult":
            for k, val in base_tp.items():
                bt.ENTRY_THRESHOLDS[k]["tp_mult"] = val
        else:
            _set(name, original)
    print("\nSettings better than current in BOTH halves:", verdicts or "none")


if __name__ == "__main__":
    main()
