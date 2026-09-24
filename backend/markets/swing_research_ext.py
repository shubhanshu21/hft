"""Wider swing-strategy search with the safeguards that make a wide search meaningful.

    python3 -m markets.swing_research_ext [--json out.json] [--markets equity_upstox equity_yahoo commodity currency]

A wide search is only worth anything if luck is controlled, so:
  * THE RULE LIST IS FIXED BELOW, before any result was seen. Every variant tried is counted and reported.
  * TRAIN / TEST split by trade entry date; nothing is tuned on the test period.
  * Significance is measured on MONTHS (trades on the same days across 49 stocks are correlated), not on raw trades.
  * MULTIPLE-TESTING bar: with N variants tried, a single result needs |t| >= Bonferroni z(0.05/N) (about 3.2 for N~40) to
    count; t >= 2 is only "watch".
  * RANDOM-ENTRY CONTROLS run through the identical exits/stops/costs. If random entries also reach t~2-3, then t~2-3 from a
    rule is what luck looks like here.
  * A rule must be positive in the majority of test years, not carried by one.
Costs, no-lookahead execution and stops are the shared engine's (core/swing_engine.py).
"""
from __future__ import annotations

import argparse
import json
from multiprocessing import Pool
from statistics import NormalDist

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from core.swing_engine import Rule, run_symbol, summarize, with_indicators
from markets.equity.universe import NIFTY50_SYMBOLS
from markets.swing_research import CURRENCY, _adjusted, round_trip_costs
from services.data.yahoo import fetch_daily

# name -> (parameter sets, stop_mult, max_hold, needs_volume, equity_only). Registered up front; see module docstring.
GRID = {
    "supertrend":    ([{"period": 10, "mult": 3}, {"period": 14, "mult": 2}], 3.0, None, False, False),
    "ichimoku":      ([{}], 3.0, None, False, False),
    "macd_cross":    ([{}], 3.0, None, False, False),
    "keltner_break": ([{}], 3.0, None, False, False),
    "rs_rank":       ([{"lookback": 126, "top": 0.8, "exit": 0.5}, {"lookback": 252, "top": 0.8, "exit": 0.5}], 3.0, None, False, True),
    "bb_squeeze":    ([{"q": 0.2}], 3.0, None, False, False),
    "vol_breakout":  ([{"vol": 1.5}], 3.0, None, True, False),
    "pivot_break":   ([{"min_age": 5}, {"min_age": 20}], 3.0, None, False, False),
    "flag":          ([{"pole": 5.0, "tight": 3.5}], 2.5, 15, False, False),
    "double_bottom": ([{}], 3.0, 30, False, False),
    "fib_pullback":  ([{"lo": 0.382, "hi": 0.618}], 3.0, 30, False, False),
    "ibs":           ([{"thr": 0.2}, {"thr": 0.1}], 3.0, 5, False, False),
    "bb_meanrev":    ([{"sd": 2.0}, {"sd": 2.5}], 4.0, 10, False, False),
    "engulfing":     ([{"ctx": False}, {"ctx": True}], 2.0, 5, False, False),
    "hammer":        ([{"ctx": False}, {"ctx": True}], 2.0, 5, False, False),
    "morning_star":  ([{"ctx": False}, {"ctx": True}], 2.0, 5, False, False),
    # the rules from the first study, re-run through the same harness so every number here is comparable
    "donchian":      ([{"n": 20, "exit_n": 10}, {"n": 55, "exit_n": 20}], 3.0, None, False, False),
    "ema_trend":     ([{"fast": 20, "slow": 50}, {"fast": 50, "slow": 200}], 3.0, None, False, False),
    "tsmom":         ([{"lookback": 126}, {"lookback": 252}], 3.0, None, False, False),
    "rsi2_pullback": ([{"thr": 5}, {"thr": 10}], 4.0, 10, False, False),
    "trend_pullback": ([{"thr": 35}, {"thr": 45}], 3.0, 20, False, False),
}
N_RANDOM = 24                       # random-entry control rules per market (identical stop / time-stop / costs)
RANDOM = {"p": 0.02}
RANDOM_HOLD, RANDOM_STOP = 10, 3.0

MARKETS = {                          # key -> (title, allow_short, train_end, has_volume)
    "equity_upstox": ("equity (real Upstox daily, 2022-26)", False, "2024-06-30", True),
    "equity_yahoo": ("equity (Yahoo NSE, 2005-26)", False, "2018-12-31", True),
    "commodity": ("commodity (USD futures x INR proxy)", True, "2018-12-31", False),
    "currency": ("currency (INR pairs)", True, "2018-12-31", False),
}


# ---- data ------------------------------------------------------------------------------------------------------
def _load(key: str) -> dict[str, pd.DataFrame]:
    if key == "equity_yahoo":
        out = {}
        for s in NIFTY50_SYMBOLS:
            raw = fetch_daily(s + ".NS", start="2005-01-01")
            df = _adjusted(raw)
            df["volume"] = raw["volume"].reindex(df.index)
            out[s] = df
        return out
    if key == "equity_upstox":
        out = {}
        for s in NIFTY50_SYMBOLS:
            d = pd.read_csv(ARCHIVE_ROOT / "equity" / f"{s}_5minute.csv")
            d["day"] = pd.to_datetime(d["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).dt.normalize()
            g = d.groupby("day").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
            if (g["close"].pct_change().abs() > 0.4).any():        # unadjusted split/bonus would look like a crash
                continue
            out[s] = g
        return out
    if key == "commodity":
        from markets.commodity.swing.proxy import MCX_PROXY, inr_frame
        return {name: inr_frame(name) for name in MCX_PROXY}
    return {name: fetch_daily(t)[["open", "high", "low", "close"]].dropna() for t, name in CURRENCY.items()}


def _prepare(frames: dict[str, pd.DataFrame], equity: bool) -> dict[str, pd.DataFrame]:
    """Indicators + (equity) point-in-time cross-sectional relative-strength ranks + random-control columns."""
    inds = {s: with_indicators(f, extended=True) for s, f in frames.items() if len(f) > 260}
    if equity and len(inds) >= 20:
        for lb in (126, 252):
            panel = pd.DataFrame({s: d["close"] / d["close"].shift(lb) - 1 for s, d in inds.items()})
            rank = panel.rank(axis=1, pct=True).where(panel.notna().sum(axis=1) >= 20)
            for s, d in inds.items():
                d[f"rs_rank{lb}"] = rank[s].reindex(d.index)
    for si, (s, d) in enumerate(inds.items()):
        rng = np.random.default_rng(1000 + si)
        for k in range(N_RANDOM):
            d[f"rnd{k}"] = rng.random(len(d))
    return inds


# ---- evaluation ------------------------------------------------------------------------------------------------
_CTX: dict = {}


def _run_variant(job: tuple) -> dict:
    name, params, stop_mult, max_hold = job
    inds, allow_short, cost, train_end = _CTX["inds"], _CTX["allow_short"], _CTX["cost"], _CTX["train_end"]
    rule = Rule(name, params, stop_mult, max_hold)
    trades = []
    for s, ind in inds.items():
        trades += run_symbol(s, ind, rule, allow_short, cost)
    cut = pd.Timestamp(train_end)
    train, test = [t for t in trades if t.entry_date <= cut], [t for t in trades if t.entry_date > cut]
    years = {}
    for t in test:
        years.setdefault(t.entry_date.year, []).append(t.net_ret)
    yr_pos = sum(1 for v in years.values() if np.mean(v) > 0)
    return {"rule": name, "params": params, "train": summarize(train), "test": summarize(test),
            "test_years_positive": yr_pos, "test_years": len(years)}


def _z_bonferroni(n_trials: int) -> float:
    return NormalDist().inv_cdf(1 - 0.05 / (2 * n_trials))


def _verdict(row: dict, z_bar: float) -> str:
    tr, te = row["train"], row["test"]
    if te.get("n", 0) < 30 or tr.get("n", 0) < 30:
        return "too few trades"
    same_sign = tr["avg_pct"] > 0 and te["avg_pct"] > 0
    majority = row["test_years"] == 0 or row["test_years_positive"] / row["test_years"] > 0.5
    if same_sign and majority and te["t_cl"] >= z_bar and te["pf"] >= 1.3:
        return "PASS"
    if same_sign and majority and te["t_cl"] >= 2.0:
        return "watch"
    return "no"


def evaluate(key: str, cost_map: dict[str, float]) -> dict:
    title, allow_short, train_end, has_vol = MARKETS[key]
    equity = key.startswith("equity")
    frames = _load(key)
    inds = _prepare(frames, equity)
    _CTX.update(inds=inds, allow_short=allow_short, cost=cost_map["equity" if equity else key], train_end=train_end)
    jobs = []
    for name, (grid, stop, hold, needs_vol, equity_only) in GRID.items():
        if (needs_vol and not has_vol) or (equity_only and not equity):
            continue
        jobs += [(name, p, stop, hold) for p in grid]
    ctrl = [("random", {"p": RANDOM["p"], "seed": k}, RANDOM_STOP, RANDOM_HOLD) for k in range(N_RANDOM)]
    with Pool(4) as pool:
        rows = pool.map(_run_variant, jobs + ctrl, chunksize=1)
    real, random_rows = rows[:len(jobs)], rows[len(jobs):]
    z_bar = _z_bonferroni(len(jobs))
    for r in real:
        r["verdict"] = _verdict(r, z_bar)
    rt = [r["test"]["t_cl"] for r in random_rows if r["test"].get("n", 0) >= 30]
    return {"market": title, "symbols": len(inds), "cost_rt_pct": _CTX["cost"] * 100, "train_end": train_end, "variants": len(jobs),
            "z_bar": z_bar, "rows": real,
            "random": {"n": len(rt), "max_test_t": max(rt) if rt else None, "share_t_ge_2": float(np.mean([t >= 2 for t in rt])) if rt else None,
                       "mean_test_avg_pct": float(np.mean([r["test"]["avg_pct"] for r in random_rows if r["test"].get("n")])),
                       "max_train_t": max((r["train"]["t_cl"] for r in random_rows if r["train"].get("n", 0) >= 30), default=None)}}


def _fmt(s: dict) -> str:
    return "n=    0" if not s.get("n") else f"n={s['n']:5d} avg={s['avg_pct']:+6.2f}% PF={s['pf']:5.2f} t_cl={s['t_cl']:+5.1f}"


def _print(res: dict) -> None:
    print(f"\n{'=' * 128}\n{res['market']} | {res['symbols']} symbols | round-trip cost {res['cost_rt_pct']:.3f}% | train <= {res['train_end']} | "
          f"{res['variants']} variants tried -> Bonferroni bar t >= {res['z_bar']:.2f}\n{'=' * 128}")
    print(f"{'rule':15s} {'params':32s} {'TRAIN':44s} {'TEST':44s} yrs+  verdict")
    for r in sorted(res["rows"], key=lambda r: -(r["test"].get("t_cl", -99) if r["test"].get("n") else -99)):
        print(f"{r['rule']:15s} {str(r['params']):32s} {_fmt(r['train']):44s} {_fmt(r['test']):44s} {r['test_years_positive']}/{r['test_years']}  {r['verdict']}")
    rd = res["random"]
    print(f"  RANDOM-ENTRY CONTROLS ({rd['n']} rules, identical exits & costs): best test t_cl = {rd['max_test_t']:+.1f}, "
          f"{100 * rd['share_t_ge_2']:.0f}% reach t>=2, mean test avg/trade {rd['mean_test_avg_pct']:+.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--markets", nargs="*", default=list(MARKETS))
    args = ap.parse_args()
    costs = round_trip_costs()
    out = []
    for key in args.markets:
        res = evaluate(key, costs)
        out.append(res)
        _print(res)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
