"""Backtest of the EURINR / GBPINR fair-value gap strategy on the 5-minute archives, the way the live strategy sees them: raw contract
switches (no back-adjustment; signal.py's roll guard handles them), the same gap code. Entry at the next bar's open plus half the
spread actually paid in trades (Roll estimate), exit at the close of the bar 30 minutes later minus half the spread, protective stop
filled with core/exits.stop_fill. Costs: brokerage, exchange, SEBI, stamp duty, GST (the strategy's own costs()).

    python3 -m markets.currency.parity.backtest [--lots 5] [--split 2026-09-17]

Inputs: var/archive/currency/<PAIR>_5minute.csv, var/archive/global_5m/<EURUSD|GBPUSD>_5minute.csv (Yahoo 5-minute, inputs only)."""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from core.exits import stop_fill
from core.paths import ARCHIVE_ROOT
from markets.currency.parity.signal import HOLD_MIN, STOP_BP, THRESHOLD_BP, gap_bp
from markets.currency.parity.strategy import STRATEGY

START = {"EURINR": "2026-08-21", "GBPINR": "2026-08-21"}
FX_FILE = {"EURINR": "EURUSD", "GBPINR": "GBPUSD"}
HALF_SPREAD = {"EURINR": 0.0430 / 2, "GBPINR": 0.0401 / 2}      # Roll estimate of the spread paid in trades, 2026-08-26..10-09
HOLD_BARS = HOLD_MIN // 5


def load(path) -> pd.DataFrame:
    d = pd.read_csv(path)
    d["ts"] = pd.to_datetime(d.timestamp, utc=True).dt.tz_convert("Asia/Kolkata")
    return d.drop_duplicates("ts").set_index("ts").sort_index()


def clean_opens(b: pd.DataFrame) -> pd.DataFrame:
    """Stray opening prints in the thin crosses (EURINR 'opened' at 112.00 against a 110.25 close on 2026-09-21): an open further from the
    previous close than 3x the usual bar range (and > 10 bp) is replaced by that close."""
    b = b.copy()
    day = b.index.normalize()
    rng = (np.log(b.high / b.low) * 1e4).rolling(200, min_periods=20).median().bfill().clip(lower=1.0)
    prev = b.close.shift(1).where(pd.Series(day, index=b.index) == pd.Series(day, index=b.index).shift(1))
    ref = prev.fillna(b.close)
    bad = (np.log(b.open / ref) * 1e4).abs() > np.maximum(3 * rng, 10)
    b.loc[bad, "open"] = ref[bad]
    return b


def run(pair: str, lots: int) -> pd.DataFrame:
    b = clean_opens(load(ARCHIVE_ROOT / "currency" / f"{pair}_5minute.csv"))
    b = b[b.index >= pd.Timestamp(START[pair], tz="Asia/Kolkata")]
    usd = load(ARCHIVE_ROOT / "currency" / "USDINR_5minute.csv").close
    fx = load(ARCHIVE_ROOT / "global_5m" / f"{FX_FILE[pair]}_5minute.csv").close
    g = gap_bp(b.close, usd, fx)
    last_entry = STRATEGY._last_entry_min()
    idx, o, h, l, c = b.index, b.open.to_numpy(), b.high.to_numpy(), b.low.to_numpy(), b.close.to_numpy()
    day = idx.normalize()
    mult = STRATEGY.lot_size(pair)
    out, i = [], 0
    while i < len(idx) - HOLD_BARS - 1:
        end_min = (idx[i] + pd.Timedelta(minutes=5)).hour * 60 + (idx[i] + pd.Timedelta(minutes=5)).minute   # decision time = bar close
        gi = g.iloc[i]
        if not (9 * 60 + 20 <= end_min <= last_entry) or gi != gi or abs(gi) <= THRESHOLD_BP[pair] or day[i + HOLD_BARS + 1] != day[i]:
            i += 1
            continue
        d = -1 if gi > 0 else 1
        entry = o[i + 1] + d * HALF_SPREAD[pair]
        stop = entry * (1 - d * STOP_BP / 1e4)
        exit_px, reason = c[i + HOLD_BARS] - d * HALF_SPREAD[pair], "time_exit"
        for k in range(i + 1, i + HOLD_BARS + 1):
            if (l[k] <= stop) if d == 1 else (h[k] >= stop):
                exit_px, reason = stop_fill(stop, o[k] if k > i + 1 else entry, d) - d * HALF_SPREAD[pair], "initial_stop"
                break
        cost = STRATEGY.costs(pair, "long" if d == 1 else "short", entry, exit_px, lots)
        gross = (exit_px - entry) * d * lots * mult
        out.append({"pair": pair, "entry_ts": idx[i + 1], "dir": "long" if d == 1 else "short", "gap_bp": round(gi, 1), "entry": entry,
                    "exit": exit_px, "reason": reason, "gross_rs": gross, "fees_rs": cost["total"], "net_rs": gross - cost["total"],
                    "net_bp": (gross - cost["total"]) / (lots * mult * entry) * 1e4})
        i += HOLD_BARS + 1
    return pd.DataFrame(out)


def summary(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    n = t.net_bp
    return {"trades": len(t), "win%": round(100 * (t.net_rs > 0).mean(), 1), "net bp/trade": round(n.mean(), 2),
            "t": round(n.mean() / (n.std() / np.sqrt(len(n))), 2) if len(n) > 2 else np.nan, "net Rs": round(t.net_rs.sum()),
            "fees Rs": round(t.fees_rs.sum()), "stops": int((t.reason == "initial_stop").sum()), "days": t.entry_ts.dt.date.nunique()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lots", type=int, default=5)
    ap.add_argument("--split", default="2026-09-17", help="thresholds were chosen on days before this date; later days are the test")
    a = ap.parse_args(argv)
    T = pd.concat([run(p, a.lots) for p in THRESHOLD_BP])
    split = pd.Timestamp(a.split, tz="Asia/Kolkata")
    rows = []
    for pair in list(THRESHOLD_BP) + ["BOTH"]:
        x = T if pair == "BOTH" else T[T.pair == pair]
        for part, m in (("chosen on (before split)", x.entry_ts < split), ("TEST (after split)", x.entry_ts >= split), ("all", x.entry_ts == x.entry_ts)):
            rows.append({"pair": pair, "period": part, **summary(x[m])})
    pd.set_option("display.width", 200)
    print(f"EURINR / GBPINR fair-value gap, {a.lots} lots, thresholds {THRESHOLD_BP} bp, hold {HOLD_MIN} min, stop {STOP_BP} bp")
    print(pd.DataFrame(rows).to_string(index=False))
    wk = T.groupby(T.entry_ts.dt.to_period("W")).net_rs.agg(["size", "sum"]).round(0)
    print("\nby week (trades, net Rs):\n", wk.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
