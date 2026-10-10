"""Active crypto strategy search (2026-10-06): 7 families, 316 configurations, fitted 2021-2024, judged 2025-2026.  (docs/CRYPTO_ACTIVE_STRATEGIES.md)

    python3 -m markets.crypto.experiments.active_strategy_lab donchian|volbo|tsmom|misc

Needs the 15-minute archive: services.data.binance.topup(sym, "15m") and topup_funding(sym). Library + sweep in one file; the chosen rule lives in markets/crypto/strategies/tf_15min/breakout.py.
"""
from __future__ import annotations
import numpy as np, pandas as pd
from core.paths import ARCHIVE_ROOT

SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
TRAIN = ("2021-01-01", "2024-12-31 23:59")
TEST = ("2025-01-01", "2030-01-01")


def load(sym, tf="15m"):
    d = pd.read_csv(ARCHIVE_ROOT / "crypto" / f"{sym}_{tf}.csv", parse_dates=["timestamp"]).drop_duplicates("timestamp").set_index("timestamp").sort_index()
    f = pd.read_csv(ARCHIVE_ROOT / "crypto" / f"{sym}_funding.csv", parse_dates=["timestamp"]).drop_duplicates("timestamp").set_index("timestamp")["rate"]
    d["funding"] = f.reindex(d.index).fillna(0.0)          # rate applied at the funding timestamp bar
    return d.loc["2020-12-01":]


def pnl(d: pd.DataFrame, pos: np.ndarray, cost: float) -> pd.Series:
    """Per-bar return on 1x notional. pos[t] decided at close t, held during t+1."""
    p = pd.Series(pos, index=d.index).fillna(0.0)
    held = p.shift(1).fillna(0.0)
    r = d["close"].pct_change().fillna(0.0)
    turn = p.diff().abs().fillna(p.abs())
    return held * r - turn.shift(1).fillna(0.0) * cost - held * d["funding"]


def stats(r: pd.Series, pos: pd.Series | None = None, bpy: float = 35040) -> dict:
    if len(r) == 0 or r.std() == 0:
        return {"cagr": 0, "sharpe": 0, "dd": 0, "tpm": 0}
    eq = (1 + r).cumprod()
    yrs = len(r) / bpy
    out = {"cagr": (eq.iloc[-1] ** (1 / yrs) - 1) * 100, "sharpe": r.mean() / r.std() * np.sqrt(bpy), "dd": ((eq.cummax() - eq) / eq.cummax()).max() * 100}
    if pos is not None:
        entries = ((pos != 0) & (pos.shift(1) != pos)).sum()
        out["tpm"] = entries / (yrs * 12)
    return out


def evaluate(name, fn, data, cost=0.0009, bpy=35040, **kw):
    """fn(df, **kw) -> position array. Equal-weight portfolio of the 3 coins."""
    rets, poss = [], []
    for s in SYMS:
        pos = fn(data[s], **kw)
        rets.append(pnl(data[s], pos, cost))
        poss.append(pd.Series(pos, index=data[s].index))
    port = pd.concat(rets, axis=1).fillna(0).mean(axis=1)
    pos_all = pd.concat(poss, axis=1).fillna(0)
    res = {}
    for lab, (a, b) in (("train", TRAIN), ("test", TEST)):
        pr = port.loc[a:b]
        tpm = np.mean([stats(port.loc[a:b], pos_all[c].loc[a:b], bpy)["tpm"] for c in pos_all.columns])
        st = stats(pr, None, bpy); st["tpm"] = tpm
        res[lab] = st
    return res


def fmt(name, res):
    t, s = res["train"], res["test"]
    return (f"{name:55s} TRAIN {t['cagr']:+7.1f}%/y Sh {t['sharpe']:5.2f} DD {t['dd']:4.0f}% | "
            f"TEST {s['cagr']:+7.1f}%/y Sh {s['sharpe']:5.2f} DD {s['dd']:4.0f}% | trades/coin/mo {s['tpm']:5.1f}")


# ---------------------------------------------------------------- strategy families
def ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def atr(d, n=14):
    h, l, c = d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy()
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()


def donchian_trail(d, n=96, k=3.0, long_only=False, trend_ema=0):
    """Enter on an n-bar channel breakout, exit on a k*ATR trailing stop (state machine)."""
    h, l, c = d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy()
    hh = pd.Series(h).rolling(n).max().shift(1).to_numpy()
    ll = pd.Series(l).rolling(n).min().shift(1).to_numpy()
    a = atr(d)
    tf = ema(c, trend_ema) if trend_ema else None
    pos = np.zeros(len(c)); cur = 0; stop = 0.0
    for i in range(n + 1, len(c)):
        if cur == 1:
            stop = max(stop, c[i] - k * a[i])
            if c[i] < stop: cur = 0
        elif cur == -1:
            stop = min(stop, c[i] + k * a[i])
            if c[i] > stop: cur = 0
        if cur == 0:
            up_ok = tf is None or c[i] > tf[i]
            dn_ok = tf is None or c[i] < tf[i]
            if c[i] > hh[i] and up_ok:
                cur, stop = 1, c[i] - k * a[i]
            elif c[i] < ll[i] and not long_only and dn_ok:
                cur, stop = -1, c[i] + k * a[i]
        pos[i] = cur
    return pos


def vol_breakout(d, k=0.5, trend_days=0, long_only=False):
    """Daily volatility breakout (UTC day): long once price > today's open + k * yesterday's range, flat at the day's end."""
    day = d.index.floor("1D")
    g = d.groupby(day)
    o = g["open"].transform("first")
    dh, dl = g["high"].max(), g["low"].min()
    rng = (dh - dl).shift(1).reindex(day).to_numpy()
    c = d["close"].to_numpy()
    up = o.to_numpy() + k * rng
    dn = o.to_numpy() - k * rng
    if trend_days:
        dc = g["close"].last()
        ma = dc.rolling(trend_days).mean().shift(1).reindex(day).to_numpy()
        prev_close = dc.shift(1).reindex(day).to_numpy()
        bull = prev_close > ma
    else:
        bull = np.ones(len(c), bool)
    pos = np.zeros(len(c)); cur = 0; cur_day = None
    days = day.to_numpy()
    last_bar_of_day = np.r_[days[1:] != days[:-1], True]
    for i in range(len(c)):
        if days[i] != cur_day:
            cur_day, cur = days[i], 0
        if cur == 0 and not np.isnan(up[i]):
            if c[i] > up[i] and bull[i]:
                cur = 1
            elif not long_only and c[i] < dn[i] and (not trend_days or not bull[i]):
                cur = -1
        pos[i] = 0 if last_bar_of_day[i] else cur
    return pos


def tsmom_intraday(d, look=16, hold=16, thr=0.0):
    """Sign of the past `look`-bar return, re-decided every `hold` bars, only if |return| > thr * rolling vol."""
    c = d["close"]
    r = np.log(c / c.shift(look))
    vol = np.log(c / c.shift(1)).rolling(96 * 5).std() * np.sqrt(look)
    sig = np.sign(r).where(r.abs() > thr * vol, 0.0).fillna(0.0).to_numpy()
    pos = np.zeros(len(c))
    for i in range(0, len(c), hold):
        pos[i:i + hold] = sig[i]
    return pos


def hour_of_day(d, hours=(), side=1):
    hrs = d.index.hour.to_numpy()
    # position decided at close t is held during t+1: hold during listed hours => set pos on the bar BEFORE
    nxt = np.r_[hrs[1:], hrs[0]]
    return np.where(np.isin(nxt, hours), side, 0.0)


def rsi(c, n=2):
    dlt = pd.Series(c).diff()
    up = dlt.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-dlt.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50).to_numpy()


def rsi_revert(d, lo=10, hi=60, trend=800, max_hold=32):
    c = d["close"].to_numpy(); rr = rsi(c, 2); tf = ema(c, trend)
    pos = np.zeros(len(c)); cur = 0; held = 0
    for i in range(trend, len(c)):
        if cur:
            held += 1
            if rr[i] > hi or held >= max_hold: cur = 0
        elif rr[i] < lo and c[i] > tf[i]:
            cur, held = 1, 0
        pos[i] = cur
    return pos


def squeeze_breakout(d, n=96, q=0.1, k=2.5):
    """Bollinger-width percentile squeeze, then trade the first close outside the band; ATR trailing exit."""
    c = d["close"]; ma = c.rolling(48).mean(); sd = c.rolling(48).std()
    width = (sd / ma)
    pct = width.rolling(n * 10).rank(pct=True)
    up, dn = (ma + 2 * sd).to_numpy(), (ma - 2 * sd).to_numpy()
    sq = (pct.shift(1) < q).to_numpy()
    cc = c.to_numpy(); a = atr(d)
    pos = np.zeros(len(cc)); cur = 0; stop = 0
    for i in range(1, len(cc)):
        if cur == 1:
            stop = max(stop, cc[i] - k * a[i]); cur = 0 if cc[i] < stop else 1
        elif cur == -1:
            stop = min(stop, cc[i] + k * a[i]); cur = 0 if cc[i] > stop else -1
        if cur == 0 and sq[i]:
            if cc[i] > up[i]: cur, stop = 1, cc[i] - k * a[i]
            elif cc[i] < dn[i]: cur, stop = -1, cc[i] + k * a[i]
        pos[i] = cur
    return pos


if __name__ == "__main__":
    import itertools, sys
    import markets.crypto.experiments.active_strategy_lab as _L
    fam = sys.argv[1]
    data = {s: _L.load(s) for s in _L.SYMS}
    def go(name, fn, **kw):
        for cost in (0.0009, 0.0005):
            res = _L.evaluate(name, fn, data, cost=cost, **kw)
            print(_L.fmt(f"{name} cost{cost*1e4:.0f}bp", res), flush=True)
    if fam == "donchian":
        for n, k, lo, te in itertools.product((48, 96, 192, 384), (2.0, 3.0, 5.0), (False, True), (0, 3840)):
            go(f"donchian n={n} k={k} long_only={lo} trend={te}", _L.donchian_trail, n=n, k=k, long_only=lo, trend_ema=te)
    elif fam == "volbo":
        for k, td, lo in itertools.product((0.3, 0.5, 0.7, 1.0), (0, 5, 20), (True, False)):
            go(f"vol_breakout k={k} trend_days={td} long_only={lo}", _L.vol_breakout, k=k, trend_days=td, long_only=lo)
    elif fam == "tsmom":
        for look, hold, thr in itertools.product((4, 16, 48, 96), (4, 16, 48, 96), (0.0, 1.0)):
            go(f"tsmom look={look} hold={hold} thr={thr}", _L.tsmom_intraday, look=look, hold=hold, thr=thr)
    elif fam == "misc":
        for lo, hi, tr in itertools.product((5, 10, 20), (50, 70), (400, 1600)):
            go(f"rsi2 revert lo={lo} hi={hi} trend={tr}", _L.rsi_revert, lo=lo, hi=hi, trend=tr)
        for n, q, k in itertools.product((96,), (0.05, 0.1, 0.2), (2.0, 3.5)):
            go(f"squeeze q={q} k={k}", _L.squeeze_breakout, n=n, q=q, k=k)
        # hour-of-day: pick hours on TRAIN only
        hr = {}
        for s in _L.SYMS:
            d = data[s].loc[_L.TRAIN[0]:_L.TRAIN[1]]
            r = d["close"].pct_change()
            hr[s] = r.groupby(d.index.hour).mean()
        avg = pd.concat(hr, axis=1).mean(axis=1)
        print("TRAIN mean 15m return by UTC hour (bps):", (avg * 1e4).round(2).to_dict(), flush=True)
        for top in (2, 4, 6):
            best = tuple(avg.sort_values(ascending=False).index[:top]); worst = tuple(avg.sort_values().index[:top])
            go(f"hour-of-day long best{top} {best}", _L.hour_of_day, hours=best, side=1)
            go(f"hour-of-day short worst{top} {worst}", _L.hour_of_day, hours=worst, side=-1)
