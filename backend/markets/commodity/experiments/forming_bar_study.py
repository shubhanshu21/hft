"""Does evaluating entries on the still-forming 5-minute bar change the strategy's results?

The backtests enter at a 5-minute bar's CLOSE. The live scanner runs every ~30 s on the candle list that includes the
current, partial bar, so it can enter mid-bar on features computed from an incomplete bar. This replays recent history
minute by minute from the 1-minute archive and runs the real compute_entry_signal exactly as the scanner would, then
compares two entry policies with an identical exit simulation:

    LIVE      take the first signal at any minute while flat
    BACKTEST  take a signal only on the minute a 5-minute bar completes

    python3 -m markets.commodity.experiments.forming_bar_study [--days 30] [--json out.json]

Exit simulation (same for both): the signal's own stop and take-profit, judged on post-entry 1-minute prices only, an
80-minute timeout and the session square-off; no breakeven/trail (it is the entry policy being compared).
"""
from __future__ import annotations

import argparse
import json
from multiprocessing import Pool

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT

# traded symbol -> (archive market dir, archive file stem, square-off HH:MM)
SYMBOLS = {"CRUDEOILM": ("commodity", "CRUDEOILM", (22, 45)), "GOLDM": ("commodity", "GOLD", (22, 45)),
           "SILVER": ("commodity", "SILVER", (22, 45)), "USDINR": ("currency", "USDINR", (16, 50))}
HOLD_MIN = 80


def _signals_for_day(sym: str, day_df: pd.DataFrame) -> list[dict | None]:
    """Per 1-minute bar: the signal the live scanner would see right after that minute closed (or None)."""
    from markets.commodity.scalping.entry_signal import compute_entry_signal
    ts = day_df["ts"].dt.floor("5min")
    o, h, l, c, v = (day_df[k].values for k in ("open", "high", "low", "close", "volume"))
    buckets = ts.values
    done: list[dict] = []
    out: list[dict | None] = []
    start = 0
    for k in range(len(day_df)):
        if k > 0 and buckets[k] != buckets[k - 1]:
            done.append(_agg(day_df, start, k - 1, buckets[k - 1]))
            start = k
        forming = _agg(day_df, start, k, buckets[k])
        candles = done + [forming]
        sig = None
        if len(candles) >= 25:
            sig = compute_entry_signal(sym, candles, "K|1", True, "both", 100_000.0, 4.0, 5.0, regime_ok=True)
        out.append(sig)
    return out


def _agg(df: pd.DataFrame, a: int, b: int, bucket) -> dict:
    seg = df.iloc[a:b + 1]
    return {"timestamp": pd.Timestamp(bucket).isoformat(), "open": float(seg["open"].iloc[0]), "high": float(seg["high"].max()),
            "low": float(seg["low"].min()), "close": float(seg["close"].iloc[-1]), "volume": float(seg["volume"].sum())}


def _simulate(day_df: pd.DataFrame, sigs: list, only_bar_close: bool, squareoff: tuple[int, int]) -> list[float]:
    """R-multiples of the trades one entry policy would take that day."""
    h, l, c = day_df["high"].values, day_df["low"].values, day_df["close"].values
    mins = (day_df["ts"].dt.hour * 60 + day_df["ts"].dt.minute).values
    end_min = squareoff[0] * 60 + squareoff[1]
    r_list, k, n = [], 0, len(day_df)
    while k < n:
        s = sigs[k]
        bar_closes = (mins[k] % 5) == 4
        if s is None or (only_bar_close and not bar_closes):
            k += 1
            continue
        d = 1 if s["direction"] == "long" else -1
        entry, sl, tp, sd = s["entry_price"], s["sl"], s["tp"], s["stop_dist"]
        exit_px, j = c[min(k + HOLD_MIN, n - 1)], k + 1
        for j in range(k + 1, min(k + 1 + HOLD_MIN, n)):
            if (l[j] <= sl) if d == 1 else (h[j] >= sl):
                exit_px = sl
                break
            if (h[j] >= tp) if d == 1 else (l[j] <= tp):
                exit_px = tp
                break
            if mins[j] >= end_min:
                exit_px = c[j]
                break
        else:
            j = min(k + HOLD_MIN, n - 1)
        r_list.append((exit_px - entry) * d / sd if sd else 0.0)
        k = j + 1
    return r_list


PERMISSIVE = {"min_adx": 8.0, "min_vol": 0.6, "min_ema_slope": 0.002, "min_orb": 0.0, "min_vwap": 0.0}


def _loosen_thresholds() -> None:
    """Loosen entry thresholds (identically for both policies) so a 30-day window has enough events to compare."""
    from markets.commodity.scalping import backtest as cbt
    from markets.currency.scalping import backtest as ubt
    for table in (cbt.ENTRY_THRESHOLDS, ubt.ENTRY_THRESHOLDS):
        for row in table.values():
            row.update({k: v for k, v in PERMISSIVE.items() if k in row})


def study_symbol(args: tuple[str, int, bool]) -> dict:
    sym, days, permissive = args
    if permissive:
        _loosen_thresholds()
    market, stem, sq = SYMBOLS[sym]
    df = pd.read_csv(ARCHIVE_ROOT / market / f"{stem}_1minute.csv")
    df["ts"] = pd.to_datetime(df["timestamp"])
    df["day"] = df["ts"].dt.strftime("%Y-%m-%d")
    day_list = sorted(df["day"].unique())[-days:]
    live_r, back_r, stats = [], [], {"days": len(day_list), "midbar_first_fires": 0, "held_at_close": 0, "bars_with_any_signal": 0}
    for day in day_list:
        d = df[df["day"] == day].sort_values("ts").reset_index(drop=True)
        if len(d) < 200:
            continue
        sigs = _signals_for_day(sym, d)
        live_r += _simulate(d, sigs, False, sq)
        back_r += _simulate(d, sigs, True, sq)
        # repaint: per 5-minute bucket, did a mid-bar signal survive to the bar's close?
        buckets = d["ts"].dt.floor("5min")
        for b, idx in d.groupby(buckets).groups.items():
            idx = list(idx)
            fired = [i for i in idx if sigs[i] is not None]
            if fired:
                stats["bars_with_any_signal"] += 1
                if any((d.loc[i, "ts"].minute % 5) != 4 for i in fired):
                    stats["midbar_first_fires"] += 1
                    if sigs[idx[-1]] is not None:
                        stats["held_at_close"] += 1
    return {"symbol": sym, "live_R": live_r, "backtest_R": back_r, **stats}


def _summ(r: list[float]) -> str:
    if not r:
        return "n=0"
    a = np.array(r)
    w, ls = a[a > 0].sum(), -a[a <= 0].sum()
    return f"n={len(a):3d} win={100 * (a > 0).mean():3.0f}% avgR={a.mean():+.3f} totalR={a.sum():+7.1f} PF={(w / ls if ls > 0 else float('inf')):.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", default=None)
    ap.add_argument("--permissive", action="store_true", help="loosen entry thresholds for both policies (more events)")
    args = ap.parse_args()
    with Pool(4) as pool:
        results = pool.map(study_symbol, [(s, args.days, args.permissive) for s in SYMBOLS])
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh)
    print(f"\nLast {args.days} trading days, {'LOOSENED' if args.permissive else 'real'} thresholds, exits = signal SL/TP + 80-min timeout (no breakeven/trail)\n")
    for r in results:
        print(f"{r['symbol']:10s} LIVE     (any minute)      {_summ(r['live_R'])}")
        print(f"{'':10s} BACKTEST (bar close only) {_summ(r['backtest_R'])}")
        mb = r["midbar_first_fires"]
        print(f"{'':10s} bars where a signal fired mid-bar: {mb} -> still a signal at the bar's close: {r['held_at_close']} "
              f"({100 * r['held_at_close'] / mb:.0f}%)\n" if mb else f"{'':10s} no mid-bar signals\n")


if __name__ == "__main__":
    main()
