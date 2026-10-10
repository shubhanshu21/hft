"""Can one crypto book earn in bull, bear AND sideways markets?  (docs/CRYPTO_ALL_WEATHER.md)

    python3 -m markets.crypto.experiments.all_weather_study

Regimes are labelled from PAST data only, per coin and at every hourly bar:
    BULL     close above the 200-day EMA and the 50-day EMA above the 200-day EMA
    BEAR     close below the 200-day EMA and the 50-day EMA below the 200-day EMA
    SIDEWAYS everything else (transitions and ranges)
Sleeves (hourly returns, three coins equal weight, costs charged, TRAIN 2020-09..2023-12 / TEST 2024-01..now):
    TREND      the spot long/flat ensemble (markets/crypto/strategies/tf_1hour/momentum.py) with the live 10% rebalance band
    SHORT      perp short, only in a BEAR regime, sized (1 - trend signals on) x volatility scale; earns/pays funding; 0.09% per side (fee 0.05% + slippage)
    CARRY      long spot + short perpetual of the same notional: earns the perpetual FUNDING RATE every 8 hours, direction-neutral; capital = 1.25x the notional (25% margin for the short leg);
               0.4% round trip once, "gated" = only while the trailing 30-day funding is above 3% a year
    REVERT     mean reversion inside a SIDEWAYS regime: long when the price is >= 1.5 std below its 7-day mean, out at -0.2; short (perp) mirrored; up to 0.5 of the volatility-targeted size
Books: the sleeves alone, and a REGIME ROUTER = CARRY (half the capital) + the directional sleeve for the current regime (bull -> TREND, bear -> SHORT, sideways -> REVERT or nothing) on the other half.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.crypto.experiments.momentum_study import SYMBOLS, load
from markets.crypto.strategies.tf_1hour.momentum import COST_SIDE, TARGET_VOL, ensemble_position, signal, vol_scale
from engine.crypto_paper import plan_trades  # noqa: F401  (keeps the live rebalance rule next to the research)

BPD = 24
TRAIN_END, TEST_START = "2023-12-31", "2024-01-01"
PERP_SIDE = 0.0009
CARRY_CAPITAL_FACTOR = 1.25
CARRY_ROUND_TRIP = 0.004


def banded(target: np.ndarray, band: float = 0.10) -> np.ndarray:
    cur, out = 0.0, np.zeros(len(target))
    for i, t in enumerate(target):
        if (t == 0.0 and cur != 0.0) or abs(t - cur) >= band:
            cur = t
        out[i] = cur
    return out


def funding_series(sym: str, idx: pd.DatetimeIndex) -> pd.Series:
    f = pd.read_csv(ARCHIVE_ROOT / "crypto" / f"{sym}_funding.csv", parse_dates=["timestamp"]).set_index("timestamp")["rate"]
    return f.reindex(idx).fillna(0.0)


from markets.crypto.strategies.tf_1hour.router import regimes  # noqa: E402  (shared with the live router)


def sleeve_returns(sym: str, df: pd.DataFrame) -> dict[str, pd.Series]:
    idx, c = df.index, df["close"]
    r = c.pct_change().fillna(0.0)
    fund = funding_series(sym, idx)
    reg = regimes(df)
    vs = pd.Series(vol_scale(df, BPD), index=idx)
    sigs = np.mean([signal(df, k, p, BPD) for k, p in (("ema", (20, 50)), ("donchian", 20), ("tsmom", 90))], axis=0)
    cost = COST_SIDE[sym]
    out = {}
    # TREND: spot long/flat ensemble, live rebalance band
    tpos = pd.Series(banded(ensemble_position(df, BPD)), index=idx).shift(1).fillna(0.0)
    out["TREND"] = tpos * r - tpos.diff().abs().fillna(0.0) * cost
    # SHORT: perp short in a bear regime, only where the trend signals are off; earns funding when the rate is positive
    spos_raw = -(1.0 - pd.Series(sigs, index=idx)) * vs * (reg == "bear")
    spos = pd.Series(banded(spos_raw.to_numpy()), index=idx).shift(1).fillna(0.0)
    out["SHORT"] = spos * r - spos.diff().abs().fillna(0.0) * PERP_SIDE + (-spos) * fund
    # CARRY: long spot + short perp (delta neutral): +funding; gated variant only while the trailing 30-day funding is attractive
    ann = fund.rolling(30 * BPD).sum() * (365 / 30)
    on_gate = (ann > 0.03).shift(1).fillna(False).astype(float)
    out["CARRY_always"] = fund / CARRY_CAPITAL_FACTOR
    out["CARRY_gated"] = on_gate * fund / CARRY_CAPITAL_FACTOR - on_gate.diff().abs().fillna(0.0) / 2 * CARRY_ROUND_TRIP / CARRY_CAPITAL_FACTOR
    out["CARRY_always"].iloc[0] -= CARRY_ROUND_TRIP / CARRY_CAPITAL_FACTOR
    # REVERT: mean reversion inside a sideways regime (spot long, perp short)
    m, sd = c.rolling(7 * BPD).mean(), c.rolling(7 * BPD).std()
    z = ((c - m) / sd).fillna(0.0)
    side = (reg == "sideways").to_numpy()
    pos, cur = np.zeros(len(c)), 0.0
    zv = z.to_numpy()
    for i in range(len(c)):
        if not side[i]:
            cur = 0.0
        elif cur == 0.0 and zv[i] <= -1.5:
            cur = 0.5
        elif cur == 0.0 and zv[i] >= 1.5:
            cur = -0.5
        elif (cur > 0 and zv[i] >= -0.2) or (cur < 0 and zv[i] <= 0.2):
            cur = 0.0
        pos[i] = cur
    rpos = pd.Series(pos, index=idx) * vs
    rpos = rpos.shift(1).fillna(0.0)
    out["REVERT"] = rpos * r - rpos.diff().abs().fillna(0.0) * np.where(rpos.abs() > 0, PERP_SIDE, cost) + (-rpos.clip(upper=0)) * fund
    out["regime"] = reg
    return out


def stats(r: pd.Series) -> dict:
    if len(r) < 24 * 20 or r.std() == 0:
        return {"cagr": float("nan"), "sharpe": float("nan"), "dd": float("nan")}
    eq = (1 + r).cumprod()
    return {"cagr": (eq.iloc[-1] ** (365 * 24 / len(r)) - 1) * 100, "sharpe": r.mean() / r.std() * np.sqrt(365 * 24), "dd": ((eq.cummax() - eq) / eq.cummax()).max() * 100}


def fmt(s: dict) -> str:
    return f"{s['cagr']:+6.0f}% {s['sharpe']:+5.2f} {s['dd']:4.0f}%"


def main() -> int:
    data = {s: load(s, "1h") for s in SYMBOLS}
    per = {s: sleeve_returns(s, data[s]) for s in SYMBOLS}
    start = max(d.index[0] for d in data.values()) + pd.Timedelta(days=200)
    names = ["TREND", "SHORT", "CARRY_always", "CARRY_gated", "REVERT"]
    port = {n: pd.concat([per[s][n] for s in SYMBOLS], axis=1).mean(axis=1).loc[start:] for n in names}
    btc_reg = per["BTCUSDT"]["regime"].loc[start:]
    bh = pd.concat([data[s]["close"].pct_change().fillna(0.0) for s in SYMBOLS], axis=1).mean(axis=1).loc[start:]

    # regime router: carry (half) + the directional sleeve of the regime (half); sideways -> REVERT or nothing
    def router(sideways_sleeve: str | None):
        directional = pd.Series(0.0, index=btc_reg.index)
        directional[btc_reg == "bull"] = port["TREND"][btc_reg == "bull"]
        directional[btc_reg == "bear"] = port["SHORT"][btc_reg == "bear"]
        if sideways_sleeve:
            directional[btc_reg == "sideways"] = port[sideways_sleeve][btc_reg == "sideways"]
        return 0.5 * port["CARRY_gated"] + 0.5 * directional
    books = {"buy & hold (equal weight)": bh, **{n: port[n] for n in names},
             "trend + carry (50/50, always on)": 0.5 * port["TREND"] + 0.5 * port["CARRY_gated"],
             "ROUTER: carry + bull TREND / bear SHORT / sideways cash": router(None),
             "ROUTER: carry + bull TREND / bear SHORT / sideways REVERT": router("REVERT")}
    share = btc_reg.value_counts(normalize=True) * 100
    print(f"Regime share of time (BTC), {start:%Y-%m-%d}..now: " + ", ".join(f"{k} {v:.0f}%" for k, v in share.items()) + "\n")
    print(f"{'book':60s} | {'TRAIN  CAGR Sharpe maxDD':>26s} | {'TEST  CAGR Sharpe maxDD':>26s}")
    for n, r in books.items():
        print(f"{n:60s} | {fmt(stats(r.loc[:TRAIN_END])):>26s} | {fmt(stats(r.loc[TEST_START:])):>26s}")
    print("\nBY REGIME (BTC regime label), whole period: annualised return / Sharpe while the regime is on (return is the hourly average x 8,760)")
    print(f"{'book':60s} | " + " | ".join(f"{k:>14s}" for k in ("bull", "bear", "sideways")))
    for n, r in books.items():
        cells = []
        for k in ("bull", "bear", "sideways"):
            x = r[btc_reg == k]
            cells.append(f"{x.mean() * 8760 * 100:+6.0f}% {x.mean() / x.std() * np.sqrt(8760) if x.std() > 0 else 0:+5.2f}")
        print(f"{n:60s} | " + " | ".join(f"{c:>14s}" for c in cells))
    print("\nBY REGIME in the TEST period only:")
    print(f"{'book':60s} | " + " | ".join(f"{k:>14s}" for k in ("bull", "bear", "sideways")))
    for n, r in books.items():
        cells = []
        for k in ("bull", "bear", "sideways"):
            x = r.loc[TEST_START:][btc_reg.loc[TEST_START:] == k]
            cells.append(f"{x.mean() * 8760 * 100:+6.0f}% {x.mean() / x.std() * np.sqrt(8760) if x.std() > 0 else 0:+5.2f}" if len(x) > 500 else "     n/a")
        print(f"{n:60s} | " + " | ".join(f"{c:>14s}" for c in cells))
    print("\nBy calendar year (return %): router with REVERT vs trend alone vs buy&hold")
    a, b, c = books["ROUTER: carry + bull TREND / bear SHORT / sideways REVERT"], books["TREND"], books["buy & hold (equal weight)"]
    for y in sorted(set(a.index.year)):
        print(f"   {y}: router {((1 + a[a.index.year == y]).prod() - 1) * 100:+7.1f}%   trend {((1 + b[b.index.year == y]).prod() - 1) * 100:+7.1f}%   buy&hold {((1 + c[c.index.year == y]).prod() - 1) * 100:+8.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def er_regimes(df: pd.DataFrame, thr: float = 0.20) -> pd.Series:
    """TREND UP / TREND DOWN / SIDEWAYS from how efficiently the DAILY closes moved over the previous 30 days: |net move| / total path. Below `thr` = sideways (range-bound)."""
    d = df["close"].resample("1D").last().dropna()
    net = (d - d.shift(30)).abs()
    path = d.diff().abs().rolling(30).sum()
    er = (net / path).shift(1)                                  # known before the day opens
    up = (d - d.shift(30)) > 0
    lab = pd.Series("sideways", index=d.index)
    lab[(er >= thr) & up.shift(1).fillna(False)] = "trend up"
    lab[(er >= thr) & ~up.shift(1).fillna(False)] = "trend down"
    lab[er.isna()] = "warmup"
    return lab.reindex(df.index, method="ffill")


def er_study() -> None:
    data = {s: load(s, "1h") for s in SYMBOLS}
    per = {s: sleeve_returns(s, data[s]) for s in SYMBOLS}
    start = max(d.index[0] for d in data.values()) + pd.Timedelta(days=200)
    port = {n: pd.concat([per[s][n] for s in SYMBOLS], axis=1).mean(axis=1).loc[start:] for n in ("TREND", "SHORT", "CARRY_always", "CARRY_gated", "REVERT")}
    bh = pd.concat([data[s]["close"].pct_change().fillna(0.0) for s in SYMBOLS], axis=1).mean(axis=1).loc[start:]
    reg = er_regimes(data["BTCUSDT"]).loc[start:]
    print("\n\n######## Regimes by TREND EFFICIENCY (30-day |net move| / path of daily closes; below 0.20 = sideways), BTC label")
    print("share of time: " + ", ".join(f"{k} {v:.0f}%" for k, v in (reg.value_counts(normalize=True) * 100).items()))
    books = {"buy & hold": bh, **port}
    print(f"\n{'sleeve':16s} | " + " | ".join(f"{k:>16s}" for k in ("trend up", "trend down", "sideways")) + "   (annualised return / Sharpe while the regime is on)")
    for n, r in books.items():
        cells = []
        for k in ("trend up", "trend down", "sideways"):
            x = r[reg == k]
            cells.append(f"{x.mean() * 8760 * 100:+7.0f}% {x.mean() / x.std() * np.sqrt(8760) if x.std() > 0 else 0:+5.2f}")
        print(f"{n:16s} | " + " | ".join(f"{c:>16s}" for c in cells))
    print("\nTEST period only (2024-01..now):")
    for n, r in books.items():
        cells = []
        for k in ("trend up", "trend down", "sideways"):
            x = r.loc[TEST_START:][reg.loc[TEST_START:] == k]
            cells.append(f"{x.mean() * 8760 * 100:+7.0f}% {x.mean() / x.std() * np.sqrt(8760) if x.std() > 0 else 0:+5.2f}" if len(x) > 500 else "      n/a")
        print(f"{n:16s} | " + " | ".join(f"{c:>16s}" for c in cells))
    # router on the efficiency regimes: trending up -> TREND, trending down -> SHORT, sideways -> carry only
    dirn = pd.Series(0.0, index=reg.index)
    dirn[reg == "trend up"] = port["TREND"][reg == "trend up"]
    dirn[reg == "trend down"] = port["SHORT"][reg == "trend down"]
    dirn[reg == "sideways"] = 0.0
    router = 0.5 * port["CARRY_gated"] + 0.5 * dirn
    both = 0.5 * port["CARRY_gated"] + 0.5 * port["TREND"]
    print(f"\n{'book':52s} | {'TRAIN  CAGR Sharpe maxDD':>26s} | {'TEST  CAGR Sharpe maxDD':>26s}")
    for n, r in (("TREND alone", port["TREND"]), ("carry + TREND always", both), ("ROUTER (efficiency regimes): carry + up TREND / down SHORT", router)):
        print(f"{n:52s} | {fmt(stats(r.loc[:TRAIN_END])):>26s} | {fmt(stats(r.loc[TEST_START:])):>26s}")


if __name__ == "__main__":
    er_study()
