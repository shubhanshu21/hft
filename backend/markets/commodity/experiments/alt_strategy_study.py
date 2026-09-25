"""Different STRATEGY FAMILIES for the commodities the trend-breakout rule has no edge on (crude, natgas, base metals), validated train / test.

    python3 -m markets.commodity.experiments.alt_strategy_study [--symbols CRUDEOILM ...] [--json out.json]

Why: symbol_calibration_study.py showed that re-tuning the trend-breakout thresholds cannot rescue CRUDEOILM / NATGASMINI / ALUMINI / LEADMINI / ZINCMINI /
NICKEL (docs/COMMODITY_CALIBRATION.md). Two other families are tested here, each with a small grid:

  A. VWAP MEAN-REVERSION (5-min): fade a stretch away from the day's VWAP -- long when vwap_dist <= -X and RSI <= R, short when >= +X and RSI >= 100-R,
     only while ADX <= A (i.e. NOT trending), target/stop in ATR multiples, flat by the time limit or the 22:45 square-off.
  B. SLOWER-TIMEFRAME CHANNEL (15-min bars): Donchian break of the last N bars, traded WITH the break ("trend") or AGAINST it ("fade"), wider ATR stop.

Everything else is what the live account would face: full session (entries 10:00-22:30 IST, square-off 22:45 like the live daemon), the real cost model
(markets/commodity/costs), sizing from Upstox's real per-lot margin (min(10x, real leverage), size 0 when one lot does not fit, 10% risk on a fixed
Rs100,000 so every trade is independent of the ones before it). Each symbol's own history is split 60% TRAIN / 40% TEST by date; the pick is made on
TRAIN only (n >= 15, net > 0, PF >= 1.2) and judged on TEST: n >= 15, net > 0, PF >= 1.2, and at least 40% of the train-profitable combinations must also
be profitable on TEST (a lone lucky combination among ~100 tried is what this guards against). Results: docs/COMMODITY_ALT_STRATEGIES.md.
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.commodity.costs import compute_mcx_commodity_costs, size_commodity_lots
from markets.commodity.experiments.symbol_calibration_study import CAPITAL, CONFIGURED_LEVERAGE, MIN_PF, MIN_TRADES, real_rates, stats
from markets.commodity.features import compute_commodity_features

RISK_PCT = 10.0
ARCHIVE = ARCHIVE_ROOT / "commodity"
ALIAS = {"CRUDEOILM": "CRUDEOIL", "NATGASMINI": "NATURALGAS", "ALUMINI": "ALUMINIUM", "LEADMINI": "LEAD", "ZINCMINI": "ZINC", "NICKEL": "NICKEL"}
SYMBOLS = list(ALIAS)                                     # the six this study targets
ALIAS = {**ALIAS, "SILVERMIC": "SILVER", "GOLDTEN": "GOLD"}     # also resolvable for regime_switch_study.py (same proxies as the backtest)
TRAIN_FRACTION = 0.60
MIN_TEST_SURVIVAL = 0.40
MIN_STOP_PCT = 0.0035
MAX_ZERO_VOLUME_SHARE = 0.30                            # a contract whose 5-min bars are mostly zero-volume has no real price to trade at
ENTRY_FROM, ENTRY_TO, SQUAREOFF = 60, 810, 825          # minutes since the 09:00 open, as markets/commodity/scalping/backtest.py


@dataclass
class Bars:
    ts: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray
    mins: np.ndarray           # minutes since 09:00 at the bar's CLOSE (the moment a signal can be acted on)
    extra: dict


def _mins_at_close(ts: pd.Series, width: int) -> np.ndarray:
    t = pd.to_datetime(ts)
    return (t.dt.hour * 60 + t.dt.minute - 540 + width).to_numpy()


def bars_5min(symbol: str, raw: pd.DataFrame | None = None, feature_symbol: str | None = None) -> Bars:
    raw = pd.read_csv(ARCHIVE / f"{ALIAS[symbol]}_5minute.csv") if raw is None else raw
    f = compute_commodity_features(raw, symbol=feature_symbol or ALIAS[symbol])
    return Bars(f["timestamp"].to_numpy(), f["high"].to_numpy(), f["low"].to_numpy(), f["close"].to_numpy(), f["atr"].to_numpy(),
                _mins_at_close(f["timestamp"], 5),
                {"vwap": f["vwap_dist_pct"].to_numpy(), "rsi": f["intraday_rsi"].to_numpy(), "adx": f["adx"].to_numpy()})


def bars_15min(symbol: str, raw: pd.DataFrame | None = None) -> Bars:
    raw = pd.read_csv(ARCHIVE / f"{ALIAS[symbol]}_5minute.csv") if raw is None else raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"])
    r = raw.set_index("timestamp").resample("15min").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    prev = r["close"].shift(1)
    tr = pd.concat([r["high"] - r["low"], (r["high"] - prev).abs(), (r["low"] - prev).abs()], axis=1).max(axis=1)
    r["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    r = r.reset_index()
    return Bars(r["timestamp"].to_numpy(), r["high"].to_numpy(), r["low"].to_numpy(), r["close"].to_numpy(), r["atr"].to_numpy(),
                _mins_at_close(r["timestamp"], 15), {"high_s": r["high"], "low_s": r["low"]})


# -- signals: +1 long, -1 short, 0 none, computed on each bar's close -------------------------------------------------------------------------------
def signal_meanrev(b: Bars, x: float, rsi_lvl: float, adx_max: float) -> np.ndarray:
    v, rsi, adx = b.extra["vwap"], b.extra["rsi"], b.extra["adx"]
    calm = adx <= adx_max
    return np.where(calm & (v <= -x) & (rsi <= rsi_lvl), 1, np.where(calm & (v >= x) & (rsi >= 100 - rsi_lvl), -1, 0))


def signal_channel(b: Bars, n: int, fade: bool) -> np.ndarray:
    hi = b.extra["high_s"].shift(1).rolling(n).max().to_numpy()      # highest high of the PREVIOUS n bars
    lo = b.extra["low_s"].shift(1).rolling(n).min().to_numpy()
    up, dn = b.close > hi, b.close < lo
    s = np.where(up, 1, np.where(dn, -1, 0))
    return -s if fade else s


def simulate(symbol: str, b: Bars, sig: np.ndarray, stop_mult: float, tp_mult: float, hold_bars: int, leverage: float,
             cost_fn=None, size_fn=None, entry_from: int = ENTRY_FROM, entry_to: int = ENTRY_TO, squareoff: int = SQUAREOFF,
             min_stop_pct: float = MIN_STOP_PCT) -> list[dict]:
    """cost_fn(symbol, direction, entry, exit, lots) -> {"net","gross"} and size_fn(capital, entry_price, stop_distance, risk_pct, symbol, leverage, size_mode) -> lots
    default to the commodity ones; the currency study passes its own."""
    cost_fn = cost_fn or compute_mcx_commodity_costs
    size_fn = size_fn or size_commodity_lots
    trades, i, n = [], 30, len(b.close)
    while i < n:
        s = sig[i]
        if s == 0 or not (entry_from <= b.mins[i] <= entry_to) or np.isnan(b.atr[i]):
            i += 1
            continue
        entry = float(b.close[i])
        sdist = max(stop_mult * float(b.atr[i]), min_stop_pct * entry)
        lots = size_fn(capital=CAPITAL, entry_price=entry, stop_distance=sdist, risk_pct=RISK_PCT, symbol=symbol, leverage=leverage, size_mode="margin")
        if lots < 1:
            i += 1
            continue
        d = int(s)
        stop, tp = entry - d * sdist, entry + d * tp_mult * sdist
        exit_p, j = None, i + 1
        while j < n:
            hi, lo = b.high[j], b.low[j]
            if (d == 1 and lo <= stop) or (d == -1 and hi >= stop):          # stop first when a bar touches both (conservative, as the live exit order)
                exit_p = stop
            elif (d == 1 and hi >= tp) or (d == -1 and lo <= tp):
                exit_p = tp
            elif j - i >= hold_bars or b.mins[j] >= squareoff or str(b.ts[j])[:10] != str(b.ts[i])[:10]:
                exit_p = float(b.close[j])
            if exit_p is not None:
                break
            j += 1
        if exit_p is None:
            break
        c = cost_fn(symbol, "long" if d == 1 else "short", entry, exit_p, lots)
        trades.append({"entry": str(b.ts[i]), "net_pnl": c["net"], "gross": c["gross"], "direction": "long" if d == 1 else "short"})
        i = j + 1
    return trades


def split_date(b: Bars) -> str:
    days = sorted({str(t)[:10] for t in b.ts})
    return days[int(len(days) * TRAIN_FRACTION)]


def grid_meanrev():
    for x, r, a, tp in itertools.product([0.3, 0.5, 0.8, 1.2], [25, 30, 35], [20, 30, 99], [1.0, 1.8]):
        yield {"family": "meanrev", "vwap_x": x, "rsi": r, "adx_max": a, "tp": tp, "stop": 1.4, "hold": 24}


def grid_channel():
    for n, fade, k, m in itertools.product([12, 20, 32, 48], [False, True], [1.5, 2.5], [2.0, 3.0]):
        yield {"family": "channel", "n": n, "fade": fade, "tp": m, "stop": k, "hold": 16}


def evaluate(symbol: str, b5: Bars, b15: Bars, p: dict, leverage: float) -> list[dict]:
    if p["family"] == "meanrev":
        return simulate(symbol, b5, signal_meanrev(b5, p["vwap_x"], p["rsi"], p["adx_max"]), p["stop"], p["tp"], p["hold"], leverage)
    return simulate(symbol, b15, signal_channel(b15, p["n"], p["fade"]), p["stop"], p["tp"], p["hold"], leverage)


def liquidity(symbol: str) -> dict:
    """Share of 5-min bars with zero traded volume and the median volume per bar (lots). Measured 2026-09-25: crude 18% / 66, natgas 17% / 42,
    GOLDTEN 17% / 17, ALUMINI 39% / 2, ZINCMINI 36% / 4, LEADMINI 80% / 0, NICKEL 90% / 0."""
    v = pd.read_csv(ARCHIVE / f"{ALIAS.get(symbol, symbol)}_5minute.csv")["volume"]
    return {"zero_volume_share": round(float((v == 0).mean()), 3), "median_volume": float(v.median())}


def study_symbol(symbol: str, rates: dict) -> dict:
    liq = liquidity(symbol)
    if liq["zero_volume_share"] > MAX_ZERO_VOLUME_SHARE:
        return {"symbol": symbol, "liquidity": liq, "verdict": "ILLIQUID",
                "why": f"{liq['zero_volume_share']:.0%} of 5-min bars traded nothing (median volume {liq['median_volume']:.0f}): a backtest 'fill' here is not a price anyone would get"}
    rate = rates.get(symbol)
    if not rate:
        return {"symbol": symbol, "verdict": "NO DATA", "why": "no Upstox margin reading (run symbol_calibration_study --refresh)"}
    lev = min(CONFIGURED_LEVERAGE, rate["leverage"])
    b5, b15 = bars_5min(symbol), bars_15min(symbol)
    cut = split_date(b5)
    rows = []
    for p in itertools.chain(grid_meanrev(), grid_channel()):
        tr = evaluate(symbol, b5, b15, p, lev)
        rows.append({"p": p, "train": stats([t for t in tr if t["entry"][:10] < cut]), "test": stats([t for t in tr if t["entry"][:10] >= cut])})
    out = {"symbol": symbol, "split": cut, "leverage_used": round(lev, 2), "combos": len(rows), "liquidity": liq}
    for fam in ("meanrev", "channel"):
        fr = [r for r in rows if r["p"]["family"] == fam]
        ok = [r for r in fr if r["train"]["n"] >= MIN_TRADES and r["train"]["net"] > 0 and r["train"]["pf"] >= MIN_PF]
        surv = [r for r in ok if r["test"]["net"] > 0]
        info = {"combos": len(fr), "train_profitable": len(ok), "also_test_profitable": len(surv)}
        if ok:
            best = max(ok, key=lambda r: r["train"]["net"])
            t = best["test"]
            info["chosen"] = best
            share = len(surv) / len(ok)
            info["verdict"] = ("ADOPT-CANDIDATE" if (t["n"] >= MIN_TRADES and t["net"] > 0 and t["pf"] >= MIN_PF and share >= MIN_TEST_SURVIVAL)
                               else "REJECT" if t["n"] >= MIN_TRADES else "UNVALIDATED (test n < 15)")
        else:
            info["verdict"] = "NO EDGE (or too little data)"
        out[fam] = info
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)
    rates = real_rates()
    print(f"Alternative strategies, 60/40 date split per symbol, Rs{CAPITAL:,.0f} risk {RISK_PCT}% real Upstox margin, full session  |  {datetime.now():%Y-%m-%d %H:%M}\n")
    results = []
    for sym in args.symbols:
        r = study_symbol(sym, rates)
        results.append(r)
        if "meanrev" not in r:
            print(f"{sym}: {r['verdict']} {r['why']}")
            continue
        print(f"{sym}  (train < {r['split']} <= test, real leverage used {r['leverage_used']}x)")
        for fam in ("meanrev", "channel"):
            f = r[fam]
            line = f"   {fam:8s} {f['verdict']:28s} {f['train_profitable']}/{f['combos']} combos profitable on TRAIN, {f['also_test_profitable']} of those on TEST"
            if "chosen" in f:
                c = f["chosen"]
                line += (f"\n            best-on-train {c['p']}\n            TRAIN n={c['train']['n']} net {c['train']['net']:+,} PF {c['train']['pf']}"
                         f" | TEST n={c['test']['n']} net {c['test']['net']:+,} PF {c['test']['pf']} win {c['test']['win']}% DD {c['test']['dd']}%")
            print(line)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"results": results}, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
