"""crypto / trend_breakout -- 4-hour trend breakout on liquid CoinDCX perpetual futures (BTC, ETH, SOL).

Rules (4-hour candles built from Binance 1-minute futures data, 00/04/08/12/16/20 UTC):
  * long: a 4-hour close above the highest high of the previous 20 candles (~3.3 days) AND above the 100 EMA; short: the mirror
  * entry at the NEXT candle's open; exit when price breaches the 10-candle low (long) / high (short), or an 8% disaster stop,
    whichever is reached first; a candle that opens past a level fills at its open; at most 30 days per trade
  * costs: CoinDCX taker 0.05% + 18% GST per side + slippage, and the real daily funding (longs pay, shorts receive)
Settings history: 30/15 at first, then tuned to 20/10/100 and the SOL+ETH default on 2026 results (so 2026 is NOT a fair test).
Results, Rs1L, BTC+ETH+SOL, 1x (checked 2026-10-17 with funding and gap fills):
  2022-24: before tax Rs1.98L; 2025-26: Rs1.12L; 2022-26: Rs2.01L before tax, Rs1.65L as business income, Rs0.30L under the strict
  crypto rule (31.2% of every winning trade, ~180 trades a year); worst fall -32%. A real trend edge on the 3 biggest coins (doing the
  opposite loses 85% / 48%), but neighbouring settings work on only 15 of 27 versions for BTC/ETH/SOL and 1 of 27 across 12 coins,
  and 2025 was negative. Weaker than calm_coins (Rs3.4L before tax, Rs1.7L strict). Research / backtest page only.
"""
from __future__ import annotations

from functools import lru_cache
import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.crypto.strategies.tf_daily.calm_coins import strategy as CALM

DATA = ARCHIVE_ROOT / "crypto" / "1m"
COINS = ("BTC", "ETH", "SOL")
ENTRY_BARS, EXIT_BARS, EMA_LEN = 20, 10, 100
STOP_LOSS_PCT = 0.08        # 8% disaster stop-loss (protects against flash crashes without choking trend breathing room)
FEE = 0.0005 * 1.18
SLIP = {"BTC": 0.00005, "ETH": 0.00005, "SOL": 0.0001, "DOGE": 0.00015}


def available() -> list[str]:
    return [c for c in COINS if (DATA / f"{c}.npz").exists()]


@lru_cache(maxsize=12)
def _load(coin: str):
    z = np.load(DATA / f"{coin}.npz")
    return z["t"].astype(np.int64), *(z[k].astype(np.float64) for k in ("o", "h", "l", "c"))


@lru_cache(maxsize=12)
def _funding(coin: str) -> pd.Series:
    f = ARCHIVE_ROOT / "crypto" / f"{coin}_funding_daily.csv"
    return pd.read_csv(f, index_col="date", parse_dates=True)["fundingRate"] if f.exists() else pd.Series(dtype=float)


def trades(coin: str, start: str, end: str) -> list[dict]:
    t, o, h, l, c = _load(coin)
    # Resample to 4-hour bars (240 minutes)
    g = t // 240
    idx = np.r_[0, np.flatnonzero(np.diff(g)) + 1]
    last = np.r_[idx[1:] - 1, len(c) - 1]
    tb, ob, hb, lb, cb = t[idx], o[idx], np.maximum.reduceat(h, idx), np.minimum.reduceat(l, idx), c[last]

    a = int(pd.Timestamp(start).timestamp() // 60)
    b = int((pd.Timestamp(end) + pd.Timedelta(days=1)).timestamp() // 60)
    sel = (tb >= a - (ENTRY_BARS + 50) * 240) & (tb < b)
    tb, ob, hb, lb, cb = tb[sel], ob[sel], hb[sel], lb[sel], cb[sel]
    n = len(cb)
    if n < ENTRY_BARS + 20:
        return []

    hi = pd.Series(hb).rolling(ENTRY_BARS).max().shift(1).to_numpy()
    lo = pd.Series(lb).rolling(ENTRY_BARS).min().shift(1).to_numpy()
    exit_hi = pd.Series(hb).rolling(EXIT_BARS).max().shift(1).to_numpy()
    exit_lo = pd.Series(lb).rolling(EXIT_BARS).min().shift(1).to_numpy()
    ema = pd.Series(cb).ewm(span=EMA_LEN, adjust=False).mean().to_numpy()
    cost = FEE + SLIP.get(coin, 0.00015)
    fund = _funding(coin)

    sig_l = np.flatnonzero((cb > hi) & (cb > ema) & (tb >= a))
    sig_s = np.flatnonzero((cb < lo) & (cb < ema) & (tb >= a))
    cands = sorted([(i, 1) for i in sig_l] + [(i, -1) for i in sig_s])
    busy = -1
    out = []
    for i, d in cands:
        if i + 1 >= n or i <= busy:
            continue
        k = i + 1
        e = ob[k]
        px = None
        stop_px = e * (1 - d * STOP_LOSS_PCT)
        why = f"Trailing channel exit after {EXIT_BARS} bars ({'long' if d == 1 else 'short'})"
        for j in range(k, min(n, k + 180)):  # hold up to 30 days
            # the level price reaches first: the channel exit when it sits before the disaster stop, else the stop;
            # a candle that OPENS past a level fills at its open (gap), not at the level
            if d == 1:
                if lb[j] <= exit_lo[j] and exit_lo[j] > stop_px:
                    px = min(ob[j], exit_lo[j]) if j > k else exit_lo[j]
                    break
                if lb[j] <= stop_px:
                    px = min(ob[j], stop_px) if j > k else stop_px
                    why = f"Disaster stop-loss ({STOP_LOSS_PCT * 100:.0f}%) hit (long)"
                    break
                if lb[j] <= exit_lo[j]:
                    px = min(ob[j], exit_lo[j]) if j > k else exit_lo[j]
                    break
            else:
                if hb[j] >= exit_hi[j] and exit_hi[j] < stop_px:
                    px = max(ob[j], exit_hi[j]) if j > k else exit_hi[j]
                    break
                if hb[j] >= stop_px:
                    px = max(ob[j], stop_px) if j > k else stop_px
                    why = f"Disaster stop-loss ({STOP_LOSS_PCT * 100:.0f}%) hit (short)"
                    break
                if hb[j] >= exit_hi[j]:
                    px = max(ob[j], exit_hi[j]) if j > k else exit_hi[j]
                    break
        if px is None:
            j = min(n - 1, k + 179)
            px = cb[j]
        if px is None or j >= n:
            j = min(n - 1, k + 179)
        gross = d * (px - e) / e
        d0, d1 = pd.Timestamp(tb[k] * 60, unit="s").normalize(), pd.Timestamp(tb[j] * 60, unit="s").normalize()
        funding = d * float(fund.loc[d0:d1].sum())                    # longs pay positive funding, shorts receive it (daily sums)
        net = gross - 2 * cost - funding
        out.append({"coin": coin, "side": "long" if d == 1 else "short", "t0": int(tb[k]), "t1": int(tb[j]),
                    "entry": e, "exit": px, "stop": stop_px,
                    "target": e * (1 + 0.15 * d), "gross": gross, "net": net, "fees": 2 * cost, "funding": funding, "won": net > 0,
                    "why": why})
        busy = j
    return out


def web_backtest(capital: float, start: str, end: str, symbols: list[str] | None = None, leverage: float = 1.0, tax="strict") -> dict:
    """Equal money per chosen coin; each trade uses that coin's share multiplied by leverage. Daily replay: chart shows daily candles, trade
    list shows 4-hour trend trades. tax: 'strict' (31.2% per win), 'business' (31.2% annual net), 'none'."""
    from core.backtester import metrics
    coins = [s for s in (symbols or available()) if s in available()]
    if not coins:
        raise ValueError("No data for the chosen coins")
    days = pd.date_range(pd.Timestamp(start), min(pd.Timestamp(end), pd.Timestamp("2026-10-16")), freq="D")
    if len(days) < 2:
        raise ValueError("The crypto data covers 2022-01-01 to 2026-10-16")
    slot = capital / len(coins)
    book = {c: slot for c in coins}
    book_pre = {c: slot for c in coins}
    events = [[] for _ in days]
    closed = []
    daily_strict, daily_pre = {}, {}
    pos_of = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(days)}
    all_tr = sorted((x for c in coins for x in trades(c, start, str(days[-1].date()))), key=lambda x: x["t1"])
    for x in all_tr:
        c = x["coin"]
        pos_value = book[c] * leverage
        q = pos_value / x["entry"]
        pnl_pre = book_pre[c] * (x["net"] * leverage)
        book_pre[c] += pnl_pre
        pnl = book[c] * (x["net"] * leverage)
        tx = CALM.TAX * pnl if pnl > 0 else 0.0
        book[c] += pnl - tx
        x["pnl_rs"] = float(round(pnl - tx if tax == "strict" else pnl, 2)); x["q"] = float(round(q, 6))
        d0 = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%Y-%m-%d")
        d1 = pd.Timestamp(x["t1"] * 60, unit="s").strftime("%Y-%m-%d")
        when = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%H:%M")
        if d0 in pos_of:
            events[pos_of[d0]].append({"t": "buy", "side": x["side"], "sym": c, "q": round(q, 6), "px": round(x["entry"], 6),
                                       "note": f"{when} UTC {'BUY' if x['side'] == 'long' else 'SHORT'} ({leverage:g}x): 4H trend breakout "
                                               f"(20-bar high/low above 100 EMA); stop {x['stop']:.6g}"})
        if d1 in pos_of:
            hours = (x["t1"] - x["t0"]) / 60
            events[pos_of[d1]].append({"t": "sell", "side": x["side"], "sym": c, "q": round(q, 6), "px": round(x["exit"], 6),
                                       "pnl": round(pnl - tx if tax == "strict" else pnl, 2),
                                       "fees": round(pos_value * x["fees"], 2), "slip": 0, "days": round(hours / 24, 1),
                                       "entry": round(x["entry"], 6),
                                       "why": x.get("why", f"Trailing channel exit after {hours:.0f}h ({x['side']})")})
        closed.append({"net": pnl - tx if tax == "strict" else pnl, "fees": pos_value * x["fees"], "slip": 0.0,
                       "gross": pos_value * x["gross"], "days": round((x["t1"] - x["t0"]) / 1440, 1)})
        daily_strict[d1] = sum(book.values())
        daily_pre[d1] = sum(book_pre.values())

    s_strict = pd.Series(daily_strict).reindex([d.strftime("%Y-%m-%d") for d in days]).ffill().fillna(capital)
    s_pre = pd.Series(daily_pre).reindex([d.strftime("%Y-%m-%d") for d in days]).ffill().fillna(capital)
    s_pre.index = pd.to_datetime(s_pre.index)
    biz, biz_tax = CALM.business_tax(s_pre, capital)
    mode = {True: "strict", False: "none"}.get(tax, tax)
    curve = {"strict": s_strict.values, "none": s_pre.values, "business": biz.values}[mode]

    bars = {}
    for c in coins:
        t, o, h, l, cl = _load(c)
        a = int(days[0].timestamp() // 60)
        b = int((days[-1] + pd.Timedelta(days=2)).timestamp() // 60)
        sel = (t >= a) & (t < b)
        t, o, h, l, cl = t[sel], o[sel], h[sel], l[sel], cl[sel]
        df = pd.DataFrame({"o": o, "h": h, "l": l, "c": cl}, index=pd.to_datetime(t * 60, unit="s"))
        dd = df.resample("1D").agg({"o": "first", "h": "max", "l": "min", "c": "last"}).reindex(days).ffill()
        bars[c] = [[round(float(v), 6) for v in r] for r in dd[["o", "h", "l", "c"]].values]

    # 4-hour chart: its own time line (unix seconds), candles, and each trade placed on its 4-hour candle; day_end maps a replay day to
    # the last 4-hour candle of that day so the chart can follow the player
    t0g = int(days[0].timestamp()) // 14400 * 14400; t1g = int((days[-1] + pd.Timedelta(days=1)).timestamp())
    times = list(range(t0g, t1g, 14400)); pos4 = {v: i for i, v in enumerate(times)}
    chart = {"tf": "4H", "times": times, "bars": {}, "trades": {}, "day_end": [min(len(times) - 1, max(0, (int((d + pd.Timedelta(days=1)).timestamp()) - t0g) // 14400 - 1)) for d in days]}
    for c in coins:
        t, o, h, l, cl = _load(c)
        sel = (t * 60 >= t0g) & (t * 60 < t1g); t, o, h, l, cl = t[sel], o[sel], h[sel], l[sel], cl[sel]
        g = (t * 60 - t0g) // 14400; idx = np.r_[0, np.flatnonzero(np.diff(g)) + 1]; last = np.r_[idx[1:] - 1, len(cl) - 1]
        b4 = [None] * len(times)
        for gi, a_, z_ in zip(g[idx], idx, last):
            if 0 <= gi < len(times): b4[gi] = [round(float(o[a_]), 6), round(float(h[a_:z_ + 1].max()), 6), round(float(l[a_:z_ + 1].min()), 6), round(float(cl[z_]), 6)]
        chart["bars"][c] = b4
        chart["trades"][c] = [{"i0": pos4.get(x["t0"] * 60 // 14400 * 14400), "i1": pos4.get(x["t1"] * 60 // 14400 * 14400), "entry": float(round(x["entry"], 6)),
                               "exit": float(round(x["exit"], 6)), "qty": x["q"], "soldQty": x["q"], "pnl": x["pnl_rs"], "side": x["side"], "adds": []}
                              for x in all_tr if x["coin"] == c and pos4.get(x["t0"] * 60 // 14400 * 14400) is not None]
    rep = {"days": [d.strftime("%Y-%m-%d") for d in days], "equity": [round(v) for v in curve], "events": events,
           "pos": [[] for _ in days], "bars": bars, "overlays": {}, "skipped_symbols": [], "chart": chart}
    first = np.array([b[0][3] for b in bars.values()])
    rep["benchmark"] = [round(v) for v in (capital / len(coins) * np.array([[b[i][3] for b in bars.values()] for i in range(len(days))]) / first).sum(axis=1)]
    wins = sum(1 for x in all_tr if x["net"] > 0)
    m = metrics(rep["equity"], capital, closed, len(days), 0)
    rep["metrics"] = m | {"tax_mode": mode, "final_strict": round(s_strict.iloc[-1]), "final_business": round(float(biz.iloc[-1])),
                          "final_pretax": round(s_pre.iloc[-1]),
                          "note": f"4-hour Trend Breakout on {', '.join(coins)} at {leverage:g}x (CoinDCX taker fees + slippage, real daily funding, gapped stops at the open): "
                                  f"{len(all_tr):,} trades, {wins / max(len(all_tr), 1):.0%} won after fees. "
                                  f"Before tax: Rs{s_pre.iloc[-1]:,.0f}; strict tax: Rs{s_strict.iloc[-1]:,.0f}; business income: Rs{biz.iloc[-1]:,.0f}. "
                                  "Benchmark = holding the chosen coins."}
    return rep
