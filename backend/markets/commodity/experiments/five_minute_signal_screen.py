"""What, visible at a 5-minute bar's close, predicts the next 15 / 30 / 60 minutes?  A systematic screen with stability checks.  (docs/FIVE_MINUTE_SIGNAL_SCREEN.md)

    python3 -m markets.commodity.experiments.five_minute_signal_screen [--equity]

We trade 5-minute bars, so a "leading signal" has to be computable from bars up to the current close. Every feature the live pipeline computes (markets/*/features.py: momentum, RSI, ADX, VWAP distance, wick and body ratios,
volume surge, Bollinger bandwidth, opening-range distances, higher-timeframe slope ...) plus a few more (returns over 1-24 bars, distance to the previous day's high / low, position in the last 24-bar range, volume trend)
is ranked against the forward return over the next 3 / 6 / 12 bars (same day, entries 10:00-22:30 IST for MCX, 09:30-14:45 for equity).
Stability, not one big number: the rank correlation (IC) is computed PER MONTH; we report its mean, the t-stat across months, the share of months with the same sign, and the mean by calendar year. A feature counts only if
|t| >= 3 (about 130 feature-horizon pairs are tried per symbol, so a plain 2 is not enough), the sign is the same in every year, and the top-vs-bottom decile spread beats the ~6 bps round-trip cost hurdle.
Commodity data: 2.7 years of Dukascopy global prices as MCX-like 5-minute bars (proxy) and, separately, the real MCX archive (4 months).
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.commodity.experiments import global_proxy_study as gp
from markets.commodity.features import compute_commodity_features
from markets.equity.features import compute_equity_features

HORIZONS = (3, 6, 12)
HURDLE_BPS = 6.0
SKIP = {"timestamp", "open", "high", "low", "close", "volume", "_dt", "vwap", "ema_fast", "ema_slow", "vol_close", "atr", "hour", "minute", "day", "day_of_week", "session_phase", "minutes_since_open",
        "minutes_since_us_open", "is_us_session", "is_inventory_window"}


def build(raw: pd.DataFrame, kind: str) -> pd.DataFrame:
    f = compute_commodity_features(raw, symbol="X") if kind == "commodity" else compute_equity_features(raw)
    f = f.reset_index(drop=True)
    c = f["close"].astype(float)
    day = f["timestamp"].astype(str).str[:10]
    for k in (1, 3, 6, 12, 24):
        f[f"ret_{k}"] = (np.log(c / c.shift(k)) * 1e4).where(day == day.shift(k))
    dh, dl = f.groupby(day)["high"].transform("max"), f.groupby(day)["low"].transform("min")
    prev_h, prev_l = dh.groupby(day).first().shift(1), dl.groupby(day).first().shift(1)
    f["dist_prev_day_high"] = (c / day.map(prev_h) - 1) * 1e4
    f["dist_prev_day_low"] = (c / day.map(prev_l) - 1) * 1e4
    hi24, lo24 = f["high"].rolling(24).max(), f["low"].rolling(24).min()
    f["pos_in_range_24"] = (c - lo24) / (hi24 - lo24).replace(0, np.nan)
    f["vol_trend"] = f["volume"].rolling(6).mean() / f["volume"].rolling(48).mean().replace(0, np.nan)
    for h in HORIZONS:
        f[f"fwd_{h}"] = (np.log(c.shift(-h) / c) * 1e4).where(day == day.shift(-h))
    m = f["minutes_since_open"]
    lo, hi = (60, 810) if kind == "commodity" else (15, 330)
    f = f[(m >= lo) & (m <= hi)].copy()
    f["month"] = f["timestamp"].astype(str).str[:7]
    f["year"] = f["timestamp"].astype(str).str[:4]
    return f


def features_of(f: pd.DataFrame) -> list[str]:
    return [c for c in f.columns if c not in SKIP and not c.startswith("fwd_") and c not in ("month", "year") and pd.api.types.is_numeric_dtype(f[c])]


def rank_ic(x: pd.Series, y: pd.Series) -> float:
    ok = x.notna() & y.notna()
    if ok.sum() < 300 or x[ok].nunique() < 5:
        return float("nan")
    return float(x[ok].rank().corr(y[ok].rank()))


def screen(f: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feat in features_of(f):
        for h in HORIZONS:
            t = f"fwd_{h}"
            by_month = f.groupby("month").apply(lambda g: rank_ic(g[feat], g[t]), include_groups=False).dropna()
            if len(by_month) < 3:
                continue
            by_year = f.groupby("year").apply(lambda g: rank_ic(g[feat], g[t]), include_groups=False).dropna()
            ok = f[feat].notna() & f[t].notna()
            q = pd.qcut(f.loc[ok, feat].rank(method="first"), 10, labels=False)
            dec = f.loc[ok].groupby(q)[t].mean()
            mean_ic = by_month.mean()
            rows.append({"feature": feat, "h": h, "ic": mean_ic, "t": mean_ic / (by_month.std(ddof=1) / np.sqrt(len(by_month))) if by_month.std() > 0 else 0.0,
                         "pos_months": (np.sign(by_month) == np.sign(mean_ic)).mean(), "years": " ".join(f"{y[2:]}:{v:+.3f}" for y, v in by_year.items()),
                         "year_sign_ok": bool((np.sign(by_year) == np.sign(mean_ic)).all()), "spread": float(dec.iloc[-1] - dec.iloc[0]), "n_months": len(by_month)})
    return pd.DataFrame(rows)


def report(name: str, r: pd.DataFrame, top: int = 8) -> list[str]:
    out = [f"\n=== {name}: top {top} features by |t| (IC = mean monthly rank correlation with the forward return)"]
    r = r.reindex(r["t"].abs().sort_values(ascending=False).index).head(top)
    out.append(f"   {'feature':22s} {'h':>3s} {'IC':>7s} {'t':>6s} {'same-sign months':>16s} {'top-bottom decile fwd (bps)':>28s} | by year")
    for _, x in r.iterrows():
        flag = "  <== passes" if abs(x.t) >= 3 and x.year_sign_ok and abs(x.spread) > HURDLE_BPS else ""
        out.append(f"   {x.feature:22s} {int(x.h) * 5:3d}m {x.ic:+7.3f} {x.t:+6.1f} {100 * x.pos_months:14.0f}% {x.spread:+27.1f}  | {x.years}{flag}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", action="store_true")
    args = ap.parse_args(argv)
    allres = {}
    if not args.equity:
        for sym in ("CRUDEOILM", "SILVERMIC", "GOLDTEN", "NATGASMINI"):
            proxy = build(gp.to_mcx_like(sym), "commodity")
            r = screen(proxy)
            allres[sym] = r
            print("\n".join(report(f"{sym} PROXY 2024-01..2026-09 ({len(proxy):,} bars, {r.n_months.max()} months)", r)))
            real = build(pd.read_csv(ARCHIVE_ROOT / "commodity" / f"{gp.MAP[sym][1]}_5minute.csv"), "commodity")
            print("\n".join(report(f"{sym} REAL MCX 2026-05..09 ({len(real):,} bars)", screen(real), 5)))
    else:
        from markets.equity.universe import NIFTY50_SYMBOLS
        frames = []
        for s in NIFTY50_SYMBOLS:
            frames.append(build(pd.read_csv(ARCHIVE_ROOT / "equity" / f"{s}_5minute.csv"), "equity"))
        pooled = pd.concat(frames, ignore_index=True)
        r = screen(pooled)
        print("\n".join(report(f"EQUITY 49 names pooled 2022-08..2026-09 ({len(pooled):,} bars, {r.n_months.max()} months)", r, 12)))
        allres["EQUITY"] = r
    # features that pass in at least 3 of the 4 commodity symbols with the same sign
    if not args.equity:
        passing = {k: v[(v.t.abs() >= 3) & v.year_sign_ok & (v.spread.abs() > HURDLE_BPS)] for k, v in allres.items()}
        print("\nPassing the screen (|t|>=3, same sign every year, spread > 6 bps) on the proxy:")
        for k, v in passing.items():
            print(f"   {k}: " + (", ".join(f"{a.feature}@{int(a.h) * 5}m({a.ic:+.3f})" for a in v.itertuples()) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
