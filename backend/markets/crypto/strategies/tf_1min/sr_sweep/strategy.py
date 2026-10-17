"""crypto / sr_sweep -- 1-minute support/resistance sweep-and-reclaim scalper (backtest page only; NOT paper traded).

Built 2026-10-17 from the user's "upgraded strategy" plan for the video '1 Minute Scalping Strategy' (shoaib bro crypto), version D,
the one with the highest win rate (about 68%):
  1. support = lowest low of the previous 60 one-minute candles, resistance = highest high (current candle excluded)
  2. 2-3% filter: trade only while (resistance - support) / support is between 2% and 3%
  3. sideways market: the 5-minute ADX(14) of the last FINISHED 5-minute candle is below 25
  4. sweep and reclaim (long): a candle's low dips below support and it closes back at or above support
  5. confirmation: the next candle closes above the reclaim candle's high -> buy at the open of the candle after it
  6. stop: just below the dip (0.01%); target: 0.5 x the risk (TARGET_R); shorts are the mirror at resistance
  7. exits checked minute by minute (stop first when both are hit in one minute; a stop gapped past fills at the open)
Costs: CoinDCX futures 0.059% (fee + GST) + slippage (0.005% BTC/ETH, 0.015% others) per side; funding ignored (trades last minutes).
Research result (12 coins, 2022-01 -> 2026-10): 68% / 67% wins (2022-24 / 2025-26) but about 0 before fees and -0.14% per trade after
fees -- the edge (+0.01-0.03% per trade before fees at 1R) is about ten times smaller than the fees. It loses money; it is on the
backtest page so it can be inspected, not because it works. Data: var/archive/crypto/1m/<COIN>.npz (Binance USDT-M 1-minute, to
2026-10-16; no nightly top-up).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.crypto.strategies.tf_daily.calm_coins import strategy as CALM

DATA = ARCHIVE_ROOT / "crypto" / "1m"
COINS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "TRX")
LOOKBACK, GAP_MIN, GAP_MAX, ADX_MAX, TARGET_R = 60, 0.02, 0.03, 25.0, 0.5
FEE = 0.0005 * 1.18
SLIP = {"BTC": 0.00005, "ETH": 0.00005}


def available() -> list[str]:
    return [c for c in COINS if (DATA / f"{c}.npz").exists()]


def _load(coin: str):
    z = np.load(DATA / f"{coin}.npz")
    return z["t"].astype(np.int64), *(z[k].astype(np.float64) for k in ("o", "h", "l", "c"))


def _adx5(t, h, l, c, n=14):
    g = t // 5; idx = np.r_[0, np.flatnonzero(np.diff(g)) + 1]; last = np.r_[idx[1:] - 1, len(c) - 1]
    H, L, C = np.maximum.reduceat(h, idx), np.minimum.reduceat(l, idx), c[last]
    up, dn = np.r_[0, np.diff(H)], np.r_[0, -np.diff(L)]
    pdm = np.where((up > dn) & (up > 0), up, 0.0); ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = np.r_[C[0], C[:-1]]; tr = np.maximum(H - L, np.maximum(abs(H - pc), abs(L - pc)))
    ew = lambda x: pd.Series(x).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    atr = ew(tr); pdi, ndi = 100 * ew(pdm) / atr, 100 * ew(ndm) / atr
    adx = ew(100 * abs(pdi - ndi) / np.maximum(pdi + ndi, 1e-12))
    out = np.full(len(c), np.nan); out[last] = adx                     # known once the 5-minute candle has finished
    return pd.Series(out).ffill().to_numpy()


def trades(coin: str, start: str, end: str, target_r: float = TARGET_R) -> list[dict]:
    t, o, h, l, c = _load(coin)
    a, b = int(pd.Timestamp(start).timestamp() // 60) - 2 * LOOKBACK, int((pd.Timestamp(end) + pd.Timedelta(days=1)).timestamp() // 60)
    sel = (t >= a) & (t < b)
    t, o, h, l, c = t[sel], o[sel], h[sel], l[sel], c[sel]
    if len(c) < 3 * LOOKBACK:
        return []
    lo = pd.Series(l).rolling(LOOKBACK).min().shift(1).to_numpy(); hi = pd.Series(h).rolling(LOOKBACK).max().shift(1).to_numpy()
    gap = (hi - lo) / lo; adx = _adx5(t, h, l, c)
    ok = (gap >= GAP_MIN) & (gap <= GAP_MAX) & (adx < ADX_MAX) & (t >= int(pd.Timestamp(start).timestamp() // 60))
    cost = FEE + SLIP.get(coin, 0.00015)
    n = len(c); out = []; busy = -1
    sig_l = np.flatnonzero(ok & (l < lo) & (c >= lo)); sig_s = np.flatnonzero(ok & (h > hi) & (c <= hi))
    cands = sorted([(i, 1) for i in sig_l] + [(i, -1) for i in sig_s])
    for i, d in cands:
        if i + 2 >= n or i + 1 <= busy:
            continue
        if not ((d == 1 and c[i + 1] > h[i]) or (d == -1 and c[i + 1] < l[i])):
            continue
        k = i + 2; e = o[k]
        stop = l[i] * (1 - 0.0001) if d == 1 else h[i] * (1 + 0.0001)
        risk = (e - stop) * d
        if risk <= 0:
            continue
        tgt = e + d * target_r * risk; px = None
        for j in range(k, n):
            if d == 1:
                if l[j] <= stop: px = min(o[j], stop) if j > k else stop; break
                if h[j] >= tgt: px = tgt; break
            else:
                if h[j] >= stop: px = max(o[j], stop) if j > k else stop; break
                if l[j] <= tgt: px = tgt; break
        if px is None:
            break
        gross = d * (px - e) / e
        out.append({"coin": coin, "side": "long" if d == 1 else "short", "t0": int(t[k]), "t1": int(t[j]), "entry": e, "exit": px, "stop": stop,
                    "target": tgt, "gross": gross, "net": gross - 2 * cost, "fees": 2 * cost, "won": px == tgt})
        busy = j
    return out


def web_backtest(capital: float, start: str, end: str, symbols: list[str] | None = None, tax="strict") -> dict:
    """Equal money per chosen coin; each trade uses that coin's whole share (1x). Daily replay: the chart shows daily candles, the trade
    list every 1-minute trade. tax: 'strict' (31.2% of each winning trade), 'business' (31.2% of each April-March year's net), 'none'."""
    from core.backtester import metrics
    coins = [s for s in (symbols or available()) if s in available()]
    if not coins:
        raise ValueError("No 1-minute data for the chosen coins")
    days = pd.date_range(pd.Timestamp(start), min(pd.Timestamp(end), pd.Timestamp("2026-10-16")), freq="D")
    if len(days) < 2:
        raise ValueError("The 1-minute crypto data covers 2022-01-01 to 2026-10-16")
    slot = capital / len(coins); book = {c: slot for c in coins}; book_pre = {c: slot for c in coins}
    events = [[] for _ in days]; closed = []; daily_strict, daily_pre = {}, {}
    pos_of = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(days)}
    all_tr = sorted((x for c in coins for x in trades(c, start, str(days[-1].date()))), key=lambda x: x["t1"])
    for x in all_tr:
        c = x["coin"]; q = book[c] / x["entry"]
        pnl_pre = book_pre[c] * x["net"]; book_pre[c] += pnl_pre
        pnl = book[c] * x["net"]; tx = CALM.TAX * pnl if pnl > 0 else 0.0; book[c] += pnl - tx
        d0 = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%Y-%m-%d"); d1 = pd.Timestamp(x["t1"] * 60, unit="s").strftime("%Y-%m-%d")
        when = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%H:%M")
        if d0 in pos_of:
            events[pos_of[d0]].append({"t": "buy", "sym": c, "q": round(q, 6), "px": round(x["entry"], 6),
                                       "note": f"{when} UTC {'BUY' if x['side'] == 'long' else 'SHORT'}: sweep of {'support' if x['side'] == 'long' else 'resistance'} "
                                               f"reclaimed and confirmed; stop {x['stop']:.6g}, target {x['target']:.6g} ({TARGET_R:g}R)"})
        if d1 in pos_of:
            mins = x["t1"] - x["t0"]
            events[pos_of[d1]].append({"t": "sell", "sym": c, "q": round(q, 6), "px": round(x["exit"], 6), "pnl": round(pnl - tx if tax == "strict" else pnl, 2),
                                       "fees": round(book[c] * x["fees"], 2), "slip": 0, "days": 0, "entry": round(x["entry"], 6),
                                       "why": f"{'target' if x['won'] else 'stop'} after {mins} min ({x['side']})"})
        closed.append({"net": pnl - tx if tax == "strict" else pnl, "fees": book[c] * x["fees"], "slip": 0.0, "gross": book[c] * x["gross"], "days": 0})
        daily_strict[d1] = sum(book.values()); daily_pre[d1] = sum(book_pre.values())
    s_strict = pd.Series(daily_strict).reindex([d.strftime("%Y-%m-%d") for d in days]).ffill().fillna(capital)
    s_pre = pd.Series(daily_pre).reindex([d.strftime("%Y-%m-%d") for d in days]).ffill().fillna(capital)
    s_pre.index = pd.to_datetime(s_pre.index); biz, biz_tax = CALM.business_tax(s_pre, capital)
    mode = {True: "strict", False: "none"}.get(tax, tax)
    curve = {"strict": s_strict.values, "none": s_pre.values, "business": biz.values}[mode]
    bars = {}
    for c in coins:
        t, o, h, l, cl = _load(c)
        df = pd.DataFrame({"o": o, "h": h, "l": l, "c": cl}, index=pd.to_datetime(t * 60, unit="s")).loc[days[0]: days[-1] + pd.Timedelta(days=1)]
        dd = df.resample("1D").agg({"o": "first", "h": "max", "l": "min", "c": "last"}).reindex(days).ffill()
        bars[c] = [[round(float(v), 6) for v in r] for r in dd[["o", "h", "l", "c"]].values]
    rep = {"days": [d.strftime("%Y-%m-%d") for d in days], "equity": [round(v) for v in curve], "events": events, "pos": [[] for _ in days], "bars": bars,
           "overlays": {}, "skipped_symbols": []}
    first = np.array([b[0][3] for b in bars.values()]); rep["benchmark"] = [round(v) for v in (capital / len(coins) * np.array([[b[i][3] for b in bars.values()] for i in range(len(days))]) / first).sum(axis=1)]
    wins = sum(1 for x in all_tr if x["net"] > 0)
    m = metrics(rep["equity"], capital, closed, len(days), 0)
    rep["metrics"] = m | {"tax_mode": mode, "final_strict": round(s_strict.iloc[-1]), "final_business": round(float(biz.iloc[-1])), "final_pretax": round(s_pre.iloc[-1]),
                          "note": f"1-minute S/R sweep-and-reclaim scalper on {', '.join(coins)} (CoinDCX futures costs, Binance 1-minute prices to 2026-10-16): "
                                  f"{len(all_tr):,} trades, {wins / max(len(all_tr), 1):.0%} won after fees "
                                  f"({sum(1 for x in all_tr if x['won']) / max(len(all_tr), 1):.0%} reached the target). Target {TARGET_R:g}R, stop below the sweep, "
                                  f"2-3% S/R gap, 5-min ADX < {ADX_MAX:g}, confirmation candle. Fees ~0.13% per trade are about as big as the average move, "
                                  f"so it loses money: before tax Rs{s_pre.iloc[-1]:,.0f}; strict tax Rs{s_strict.iloc[-1]:,.0f}; business income Rs{biz.iloc[-1]:,.0f}. "
                                  "Research only; not paper traded. Benchmark = holding the chosen coins."}
    return rep
