"""Swing-strategy research: documented rules x market, train/test, net of costs.

    python3 -m markets.swing_research [--json out.json]

Discipline (same as the rest of this project): parameters are a small fixed grid taken from the literature; the
best set per rule is chosen on the TRAIN period only, and the untouched TEST period is what counts. A rule is only
credible with >=15 trades in the period judged. Data: see services/data/yahoo.py (long public history; a PROXY for
MCX / NSE-currency contracts) and the real Upstox 5-minute archive resampled to daily for a second equity check.
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from core.swing_engine import Rule, run_symbol, summarize, with_indicators
from markets.equity.universe import NIFTY50_SYMBOLS
from services.data.yahoo import fetch_daily

# ---- rules to test: (rule name, parameter sets, stop multiple, max hold) --------------------------------------
GRID = {
    "donchian":       ([{"n": 20, "exit_n": 10}, {"n": 55, "exit_n": 20}], 3.0, None),
    "ema_trend":      ([{"fast": 20, "slow": 50}, {"fast": 50, "slow": 200}], 3.0, None),
    "tsmom":          ([{"lookback": 126}, {"lookback": 252}], 3.0, None),
    "rsi2_pullback":  ([{"thr": 5}, {"thr": 10}], 4.0, 10),
    "trend_pullback": ([{"thr": 35}, {"thr": 45}], 3.0, 20),
}

# ---- markets ---------------------------------------------------------------------------------------------------
CURRENCY = {"INR=X": "USDINR", "EURINR=X": "EURINR", "GBPINR=X": "GBPINR", "JPYINR=X": "JPYINR"}


def _adjusted(df: pd.DataFrame) -> pd.DataFrame:
    f = df["adjclose"] / df["close"]
    out = pd.DataFrame({"open": df["open"] * f, "high": df["high"] * f, "low": df["low"] * f, "close": df["adjclose"]})
    return out[(out > 0).all(axis=1)]


def load_equity_yahoo() -> dict[str, pd.DataFrame]:
    return {s: _adjusted(fetch_daily(s + ".NS", start="2005-01-01")) for s in NIFTY50_SYMBOLS}


def load_equity_upstox() -> dict[str, pd.DataFrame]:
    out = {}
    for s in NIFTY50_SYMBOLS:
        d = pd.read_csv(ARCHIVE_ROOT / "equity" / f"{s}_5minute.csv")
        d["ts"] = pd.to_datetime(d["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata")
        d["day"] = d["ts"].dt.tz_localize(None).dt.normalize()
        g = d.groupby("day").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
        gap = g["close"].pct_change().abs()
        if (gap > 0.4).any():          # an unadjusted split/bonus would look like a crash -- skip that name
            continue
        out[s] = g
    return out


def load_commodity_proxy() -> dict[str, pd.DataFrame]:
    from markets.commodity.swing.proxy import MCX_PROXY as PROXY, inr_frame
    return {name: inr_frame(name) for name in PROXY}


def load_currency() -> dict[str, pd.DataFrame]:
    return {name: fetch_daily(t)[["open", "high", "low", "close"]].dropna() for t, name in CURRENCY.items()}


def round_trip_costs() -> dict[str, float]:
    """Round-trip cost as a fraction of notional, from this project's own cost models (plus explicit slippage)."""
    from markets.commodity.costs import compute_mcx_commodity_costs, COMMODITY_SPECS
    from markets.currency.costs import compute_ncd_currency_costs, CURRENCY_SPECS
    from markets.equity.costs import compute_nse_equity_delivery_costs
    px, q = 500.0, 200                                            # Rs100k position
    eq = compute_nse_equity_delivery_costs("long", px, px, q)["total"] / (px * q) + 0.0010     # + 5 bps/side slippage
    com = []
    for sym, price, lots in (("GOLDM", 150000.0, 2), ("CRUDEOILM", 8600.0, 5)):
        r = compute_mcx_commodity_costs(sym, "long", price, price, lots)
        com.append(r["total"] / (lots * COMMODITY_SPECS[sym]["lot_size"] * price))
    cur = compute_ncd_currency_costs("USDINR", "long", 95.0, 95.0, 4)["total"] / (4 * CURRENCY_SPECS["USDINR"]["lot_size"] * 95.0)
    return {"equity": eq, "commodity": float(np.mean(com)) + 0.0004, "currency": cur + 0.0002}   # + extra overnight slippage


MARKETS = {                       # name -> (loader, allow_short, train_end, test_start, description)
    "equity (Yahoo NSE, 2005-26)":   ("equity_yahoo", False, "2018-12-31", "2019-01-01"),
    "equity (real Upstox, 2022-26)": ("equity_upstox", False, "2024-06-30", "2024-07-01"),
    "commodity (USD futures x INR)": ("commodity", True, "2018-12-31", "2019-01-01"),
    "currency (INR pairs)":          ("currency", True, "2018-12-31", "2019-01-01"),
}


def _buy_and_hold(frames: dict[str, pd.DataFrame], a: str, b: str | None) -> float:
    rets = []
    for df in frames.values():
        w = df.loc[a:b] if b else df.loc[a:]
        if len(w) > 20:
            rets.append(w["close"].iloc[-1] / w["close"].iloc[0] - 1)
    return float(np.mean(rets) * 100) if rets else float("nan")


def evaluate(market: str, frames: dict[str, pd.DataFrame], allow_short: bool, train_end: str, test_start: str, cost_rt: float) -> dict:
    inds = {s: with_indicators(f) for s, f in frames.items() if len(f) > 260}
    first = min(f.index[0] for f in inds.values())
    out = {"market": market, "symbols": len(inds), "cost_rt_pct": cost_rt * 100, "rules": {}}
    for rname, (grid, stop_mult, max_hold) in GRID.items():
        rows = []
        for params in grid:
            rule = Rule(rname, params, stop_mult, max_hold)
            train, test = [], []
            for s, ind in inds.items():
                train += run_symbol(s, ind, rule, allow_short, cost_rt, end=train_end)
                test += run_symbol(s, ind, rule, allow_short, cost_rt, start=test_start)
            rows.append({"params": params, "train": summarize(train), "test": summarize(test)})
        eligible = [r for r in rows if r["train"]["n"] >= 30]
        chosen = max(eligible, key=lambda r: r["train"]["t"]) if eligible else None
        out["rules"][rname] = {"grid": rows, "chosen": chosen["params"] if chosen else None}
    out["buy_hold_train_pct"] = _buy_and_hold(frames, str(first.date()), train_end)
    out["buy_hold_test_pct"] = _buy_and_hold(frames, test_start, None)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    costs = round_trip_costs()
    print("round-trip cost assumptions:", {k: f"{v * 100:.3f}%" for k, v in costs.items()})
    loaders = {"equity_yahoo": load_equity_yahoo, "equity_upstox": load_equity_upstox,
               "commodity": load_commodity_proxy, "currency": load_currency}
    results = []
    for market, (key, allow_short, train_end, test_start) in MARKETS.items():
        cost = costs["equity" if key.startswith("equity") else key]
        results.append(evaluate(market, loaders[key](), allow_short, train_end, test_start, cost))
        print("done:", market)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=1, default=str)
    _print(results)


def _fmt(s: dict) -> str:
    return "  n=0" if not s.get("n") else f"n={s['n']:5d} win={s['win']:4.0f}% avg={s['avg_pct']:+6.2f}% PF={s['pf']:5.2f} t={s['t']:+5.1f} hold={s['hold']:3.0f}d"


def _print(results: list[dict]) -> None:
    for m in results:
        print(f"\n{'=' * 118}\n{m['market']}  |  {m['symbols']} symbols  |  round-trip cost {m['cost_rt_pct']:.3f}%  |  "
              f"buy&hold avg: TRAIN {m['buy_hold_train_pct']:+.0f}%  TEST {m['buy_hold_test_pct']:+.0f}%\n{'=' * 118}")
        for rname, r in m["rules"].items():
            for row in r["grid"]:
                mark = " <- chosen on TRAIN" if row["params"] == r["chosen"] else ""
                print(f"  {rname:15s} {str(row['params']):34s} TRAIN {_fmt(row['train'])}\n{'':52s} TEST  {_fmt(row['test'])}{mark}")


if __name__ == "__main__":
    main()
