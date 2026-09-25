"""Per-symbol calibration of the commodity entry rules, with a genuine held-out test window, under what Upstox really allows.

    python3 -m markets.commodity.experiments.symbol_calibration_study [--refresh] [--json out.json]

Question: the commodity strategy has one rule set and per-family threshold dicts; the base metals (ALUMINI/LEADMINI/ZINCMINI/NICKEL) were never calibrated
(they carry crude's values). For every contract that ONE LOT fits in Rs100,000 of margin (Upstox's margin calculator, measured -- COPPER Rs327k, SILVERM
Rs150k, GOLDM Rs139k do not fit) this sweeps entry thresholds on a TRAIN window and judges the pick on a TEST window never used in the selection.

Discipline (CLAUDE.md): >= 15 trades in the window being judged, costs = the real cost model, leverage = min(configured, Upstox's real leverage for that
contract), size 0 when one lot does not fit, each symbol on its own Rs100,000 at the live 10% risk. Full session 09:00-23:30 as live (the backtest default is the evening window only). Exits stay at 1.8 / 1.4 (only entry thresholds move, to
keep the search small enough that a winner is not just the luckiest of hundreds).
A symbol is ADOPTED only if its chosen thresholds, on TEST: n >= 15, net > 0, PF >= 1.2, AND better net than the thresholds it has today.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import itertools
import json
from datetime import datetime

from core.paths import CACHE_DIR
from engine import margin_rates
from markets.commodity.scalping import backtest as bt

CAPITAL, RISK_PCT, CONFIGURED_LEVERAGE = 100000.0, 10.0, 10.0
TRAIN = ("2026-05-18", "2026-07-31")
TEST = ("2026-08-01", "2026-09-24")
MIN_TRADES, MIN_PF = 15, 1.2

# contract -> key of bt.ENTRY_THRESHOLDS it uses
CANDIDATES = {
    "CRUDEOILM": "crude", "NATGASMINI": "natgas", "SILVERMIC": "silver", "GOLDTEN": "gold",
    "ALUMINI": "alumini", "LEADMINI": "leadmini", "ZINCMINI": "zincmini", "NICKEL": "nickel",
}
GRID = {"min_adx": [14.0, 18.0, 22.0, 26.0], "min_vol": [1.1, 1.3, 1.7, 2.0], "min_ema_slope": [0.03, 0.05, 0.08]}
RATES_PATH = CACHE_DIR / "margin_candidates.json"


def refresh_rates() -> None:
    """Measure Upstox's real MIS margin for one lot of every candidate (own cache file; the daemon's is untouched)."""
    from dotenv import load_dotenv
    from core.paths import BACKEND_ROOT
    from markets.commodity.costs import get_contract_multiplier
    from markets.commodity.data import _get_broker
    from services.broker.instruments import build_mcx_commodity_map
    load_dotenv(BACKEND_ROOT / ".env")
    keys = {s: k for s, k in build_mcx_commodity_map().items() if s in CANDIDATES}
    margin_rates.refresh(_get_broker(), keys, lambda s, price: price * get_contract_multiplier(s), path=RATES_PATH)


def real_rates() -> dict:
    return json.loads(RATES_PATH.read_text()) if RATES_PATH.exists() else {}


def stats(trades: list[dict]) -> dict:
    net = [t["net_pnl"] for t in trades]
    win, loss = sum(x for x in net if x > 0), -sum(x for x in net if x <= 0)
    peak = eq = CAPITAL
    dd = 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)
    return {"n": len(net), "win": round(100 * sum(x > 0 for x in net) / len(net), 1) if net else 0.0, "net": round(sum(net)),
            "pf": round(win / loss, 2) if loss else (float("inf") if win else 0.0), "dd": round(dd, 1)}


def run(sym: str, thresholds: dict, leverage: float, window: tuple[str, str]) -> dict:
    key = CANDIDATES[sym]
    saved = dict(bt.ENTRY_THRESHOLDS[key])
    bt.ENTRY_THRESHOLDS[key] = {**saved, **thresholds}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            r = bt.run_commodity_backtest(symbols=[sym], capital=CAPITAL, risk_pct=RISK_PCT, leverage=leverage, from_date=window[0], to_date=window[1],
                                          return_trades=True, size_mode="margin", us_session_only=False)   # live trades the FULL session (DRYRUN_FULL_SESSION=true)
    finally:
        bt.ENTRY_THRESHOLDS[key] = saved
    return stats(r.get("trade_list") or [])


def credible(s: dict) -> bool:
    return s["n"] >= MIN_TRADES


def study(sym: str, rates: dict) -> dict:
    rate = rates.get(sym)
    out = {"symbol": sym, "key": CANDIDATES[sym]}
    if not rate:
        return {**out, "verdict": "NO DATA", "why": "no Upstox margin reading for this contract"}
    out["margin_per_lot"], out["real_leverage"] = round(rate["margin"]), rate["leverage"]
    if rate["margin"] > CAPITAL:
        return {**out, "verdict": "UNAFFORDABLE", "why": f"one lot needs Rs{rate['margin']:,.0f} > Rs{CAPITAL:,.0f}"}
    lev = min(CONFIGURED_LEVERAGE, rate["leverage"])
    out["leverage_used"] = round(lev, 2)
    current = {k: bt.ENTRY_THRESHOLDS[CANDIDATES[sym]][k] for k in GRID}
    out["current"] = current
    out["current_train"], out["current_test"] = run(sym, {}, lev, TRAIN), run(sym, {}, lev, TEST)

    rows = []
    for combo in itertools.product(*GRID.values()):
        th = dict(zip(GRID, combo))
        rows.append({"th": th, "train": run(sym, th, lev, TRAIN), "test": run(sym, th, lev, TEST)})
    out["grid_size"] = len(rows)
    ok_train = [r for r in rows if credible(r["train"]) and r["train"]["net"] > 0 and r["train"]["pf"] >= MIN_PF]
    out["train_profitable"] = len(ok_train)
    out["train_profitable_also_test_profitable"] = sum(1 for r in ok_train if r["test"]["net"] > 0)
    if not ok_train:
        return {**out, "verdict": "NO EDGE (or too little data)",
                "why": f"no combination of {len(rows)} reached >= {MIN_TRADES} trades with net > 0 and PF >= {MIN_PF} on TRAIN"}
    best = max(ok_train, key=lambda r: r["train"]["net"])
    out["chosen"] = best
    t, base = best["test"], out["current_test"]
    if not credible(t):
        return {**out, "verdict": "UNVALIDATED", "why": f"chosen thresholds give only {t['n']} trades on TEST (< {MIN_TRADES})"}
    passes = t["net"] > 0 and t["pf"] >= MIN_PF and t["net"] > base["net"]
    return {**out, "verdict": "ADOPT" if passes else "REJECT",
            "why": (f"TEST n={t['n']} net {t['net']:+,} PF {t['pf']} vs current thresholds net {base['net']:+,} PF {base['pf']}")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-measure Upstox's real margin for every candidate first")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    if args.refresh or not RATES_PATH.exists():
        refresh_rates()
    rates = real_rates()
    results = []
    print(f"Train {TRAIN[0]}..{TRAIN[1]}  |  Test {TEST[0]}..{TEST[1]}  |  Rs{CAPITAL:,.0f}, risk {RISK_PCT}%, real Upstox margin  |  {datetime.now():%Y-%m-%d %H:%M}\n")
    for sym in CANDIDATES:
        r = study(sym, rates)
        results.append(r)
        print(f"{sym:11s} margin/lot Rs{r.get('margin_per_lot', 0):>7,}  real {r.get('real_leverage', 0):5.1f}x  -> {r['verdict']}: {r['why']}")
        if "current_test" in r:
            c, ct = r["current_train"], r["current_test"]
            print(f"   current {r['current']}: TRAIN n={c['n']} net {c['net']:+,} PF {c['pf']} | TEST n={ct['n']} net {ct['net']:+,} PF {ct['pf']} DD {ct['dd']}%")
        if "chosen" in r:
            ch = r["chosen"]
            print(f"   chosen  {ch['th']}: TRAIN n={ch['train']['n']} net {ch['train']['net']:+,} PF {ch['train']['pf']} | "
                  f"TEST n={ch['test']['n']} net {ch['test']['net']:+,} PF {ch['test']['pf']} DD {ch['test']['dd']}%")
            print(f"   robustness: {r['train_profitable']}/{r['grid_size']} combos profitable on TRAIN, "
                  f"{r['train_profitable_also_test_profitable']} of those also profitable on TEST")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"train": TRAIN, "test": TEST, "results": results}, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
