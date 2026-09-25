"""Leading signals for commodity moves: what the literature suggests, tested on our data.  (docs/COMMODITY_LEADING_SIGNALS.md)

    python3 -m markets.commodity.experiments.leading_signal_study

Candidates (each from published evidence, see the doc):
  T1  GLOBAL -> MCX LEAD-LAG      MCX prices are the global (NYMEX/COMEX) price in rupees. If global moves reach MCX with a delay, a global 1-minute move predicts MCX's next minutes.
                                  Real MCX 1-minute archive (2026-05..09) vs Dukascopy 1-minute global prices, aligned on the clock.
  T2  INTRADAY MOMENTUM           Gao-Han-Li-Zhou (2018) and Wen et al. on crude: the first half-hour return of the NYMEX day predicts the LAST half-hour's. Windows in New York time on 2.7 years of global 1-minute data.
  T3  EIA WEDNESDAYS              crude: on EIA-report days (Wed 10:30 ET) the third half-hour predicts the last half-hour.
  T4  COMPRESSION -> EXPANSION    Crabel / Bollinger: volatility expansion follows a squeeze; direction is not called. Tested as (a) does range expand after compression, (b) does compression before a TREND entry
                                  improve that trade.
A signal must clear round-trip costs (~5-6 bps of notional on the commodities we trade: crude Rs169 on ~Rs267k, silver Rs363 on ~Rs711k) to be worth anything; every table shows mean signed bps next to that hurdle.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT, CACHE_DIR

GLOBAL_CACHE = CACHE_DIR / "dukascopy"
MCX_ARCHIVE = ARCHIVE_ROOT / "commodity"
PAIRS = {"CRUDEOILM": ("LIGHTCMDUSD", "CRUDEOIL"), "SILVERMIC": ("XAGUSD", "SILVER"), "GOLDTEN": ("XAUUSD", "GOLD"), "NATGASMINI": ("GASCMDUSD", "NATURALGAS")}
HURDLE_BPS = 6.0


def load_global_1m(sym: str) -> pd.Series:
    frames = [pd.read_csv(p, parse_dates=["timestamp"]) for p in sorted((GLOBAL_CACHE / sym).glob("*.csv")) if p.stat().st_size > 30]
    df = pd.concat(frames).drop_duplicates("timestamp").set_index("timestamp").sort_index()
    return df["close"].astype(float)


def load_mcx_1m(alias: str) -> pd.Series:
    df = pd.read_csv(MCX_ARCHIVE / f"{alias}_1minute.csv")
    ts = pd.to_datetime(df["timestamp"], utc=True)
    return pd.Series(df["close"].to_numpy(dtype=float), index=ts).sort_index()


def tstat(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else 0.0


# -- T1 ------------------------------------------------------------------------------------------------------------------------------------------------
def lead_lag(mcx_name: str) -> list[str]:
    g_sym, alias = PAIRS[mcx_name]
    g, m = load_global_1m(g_sym), load_mcx_1m(alias)
    idx = g.index.intersection(m.index)
    g, m = np.log(g.loc[idx]), np.log(m.loc[idx])
    rg, rm = g.diff().to_numpy() * 1e4, m.diff().to_numpy() * 1e4                        # bps per minute
    same_min = (idx[1:] - idx[:-1]) == pd.Timedelta(minutes=1)
    out = [f"  {mcx_name}: {len(idx):,} aligned 1-minute bars ({idx[0]:%Y-%m-%d}..{idx[-1]:%Y-%m-%d})"]
    ok = np.r_[False, same_min]
    for k in (0, 1, 2, 3, 5):
        a, b = rg[: len(rg) - k], rm[k:]
        valid = ok[: len(a)] & ok[k:][: len(a)] & ~np.isnan(a) & ~np.isnan(b)
        c = np.corrcoef(a[valid], b[valid])[0, 1] if valid.sum() > 10 else float("nan")
        out.append(f"     corr(global return at t, MCX return at t+{k}) = {c:+.3f}   (n={valid.sum():,})")
    # tradable version: after a big global 1-minute move, MCX's NEXT 5 minutes in the same direction?
    fwd = pd.Series(m.to_numpy()).diff(5).shift(-5).to_numpy() * 1e4                       # MCX 5-minute forward return from the close of minute t
    for th in (5, 10, 20):
        sel = ok & (np.abs(rg) >= th) & ~np.isnan(fwd)
        if sel.sum() < 20:
            out.append(f"     |global 1-min move| >= {th} bps: only {sel.sum()} events")
            continue
        signed = np.sign(rg[sel]) * fwd[sel]
        out.append(f"     |global 1-min move| >= {th:2d} bps: {sel.sum():5,d} events; MCX next-5-min return in the same direction: mean {signed.mean():+5.2f} bps (hurdle {HURDLE_BPS:.0f}), hit rate {100 * (signed > 0).mean():4.1f}%, t={tstat(signed):+.1f}")
    return out


# -- T2 / T3 -------------------------------------------------------------------------------------------------------------------------------------------
def window_return(g: pd.Series, day: pd.Timestamp, start: str, end: str) -> float:
    try:
        a = g.loc[pd.Timestamp(f"{day.date()} {start}", tz="America/New_York").tz_convert("UTC"):].iloc[0]
        b = g.loc[pd.Timestamp(f"{day.date()} {end}", tz="America/New_York").tz_convert("UTC"):].iloc[0]
        return float(np.log(b / a) * 1e4)
    except (IndexError, KeyError):
        return float("nan")


def intraday_momentum(mcx_name: str, wed_only: bool = False) -> list[str]:
    g_sym, _ = PAIRS[mcx_name]
    g = load_global_1m(g_sym)
    ny = g.index.tz_convert("America/New_York")
    days = pd.DatetimeIndex(sorted({d for d in ny.normalize() if d.weekday() < 5 and (d.weekday() == 2 or not wed_only)})).tz_localize(None)
    first_start, first_end = ("10:00", "10:30") if wed_only else ("09:00", "09:30")
    rows = []
    for d in days:
        rows.append((window_return(g, d, first_start, first_end), window_return(g, d, "13:30", "14:00"), window_return(g, d, "12:00", "13:15")))
    a = np.array(rows)
    out = []
    label = "EIA-day third half-hour (10:00-10:30 ET)" if wed_only else "first half-hour (09:00-09:30 ET)"
    for j, tgt in ((1, "last half-hour 13:30-14:00 ET (= 23:00-23:30 IST in summer)"), (2, "12:00-13:15 ET (= 21:30-22:45 IST, tradable before our 22:45 exit)")):
        ok = ~np.isnan(a[:, 0]) & ~np.isnan(a[:, j])
        x, y = a[ok, 0], a[ok, j]
        signed = np.sign(x) * y
        out.append(f"  {mcx_name:10s} {label} -> {tgt}: n={ok.sum():3d} corr {np.corrcoef(x, y)[0, 1]:+.3f}, mean signed {signed.mean():+5.2f} bps, hit {100 * (signed > 0).mean():4.1f}%, t={tstat(signed):+.1f}")
    return out


# -- T4 ------------------------------------------------------------------------------------------------------------------------------------------------
def compression() -> list[str]:
    out = []
    for name, (g_sym, alias) in PAIRS.items():
        m1 = load_global_1m(g_sym)
        c = m1.resample("15min").last().dropna()
        rng = (m1.resample("15min").max() - m1.resample("15min").min()).reindex(c.index) / c * 1e4
        bw = c.rolling(20).std() / c.rolling(20).mean() * 1e4                                 # Bollinger bandwidth (bps), 20 x 15-min bars
        pct = bw.rolling(500).apply(lambda x: (x[:-1] < x[-1]).mean(), raw=True)               # trailing percentile rank
        fut = rng.rolling(4).mean().shift(-4)                                                  # mean 15-min range over the NEXT hour
        base = fut.mean()
        for lo, hi, lab in ((0, 0.10, "squeeze (bottom 10% width)"), (0.10, 0.90, "normal"), (0.90, 1.01, "wide (top 10%)")):
            sel = (pct >= lo) & (pct < hi) & fut.notna()
            out.append(f"  {name:10s} {lab:28s}: n={sel.sum():6,d}  next-hour avg 15-min range {fut[sel].mean():5.1f} bps  ({fut[sel].mean() / base:4.2f}x the all-bars average)")
        # direction: break above/below the squeeze's range within the hour -- is the first break followed?
        ret_next = (c.shift(-4) / c - 1) * 1e4
        sq = (pct < 0.10) & ret_next.notna()
        mom = (c / c.shift(4) - 1) * 1e4
        signed = (np.sign(mom[sq]) * ret_next[sq]).dropna()
        out.append(f"  {name:10s} after a squeeze, does the PRIOR hour's direction continue for the next hour? n={len(signed):5,d} mean {signed.mean():+5.2f} bps, hit {100 * (signed > 0).mean():4.1f}%, t={tstat(signed.to_numpy()):+.1f}")
    return out


def main() -> int:
    print("T1  GLOBAL -> MCX LEAD-LAG (real MCX 1-minute vs Dukascopy 1-minute, aligned on the clock)")
    for s in PAIRS:
        try:
            print("\n".join(lead_lag(s)))
        except Exception as exc:                                    # one symbol failing must not hide the others
            print(f"  {s}: {type(exc).__name__}: {exc}")
    print("\nT2  FIRST HALF-HOUR -> LATER (NYMEX/COMEX regular hours, New York time, 2024-01..2026-09 proxy)")
    for s in PAIRS:
        print("\n".join(intraday_momentum(s)))
    print("\nT3  EIA WEDNESDAYS (crude only: third half-hour 10:00-10:30 ET, report at 10:30)")
    print("\n".join(intraday_momentum("CRUDEOILM", wed_only=True)))
    print("\nT4  COMPRESSION -> EXPANSION (15-minute bars, 20-bar Bollinger bandwidth, percentile over the trailing 500 bars)")
    print("\n".join(compression()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
