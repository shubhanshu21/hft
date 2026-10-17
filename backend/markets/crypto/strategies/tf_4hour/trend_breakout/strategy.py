"""crypto / trend_breakout -- 4-hour trend breakout on liquid CoinDCX perpetual futures (BTC, ETH, SOL).

Rules (4-hour candles from Binance/CoinDCX futures, 00/04/08/12/16/20 UTC):
  * long: a 4-hour close above the highest high of the previous 30 candles (~5.0 days) by at least 0.3% buffer, close above 100 EMA,
    100 EMA sloping upward (diff > 0 over 3 bars), and ADX(14) >= 20 on BTC (>= 18 on ETH/SOL); short: the mirror
  * entry at the NEXT candle's open; exit when price breaches the 12-candle low (long) / high (short) (~48 hours), or an 8% disaster stop,
    whichever is reached first; a candle that opens past a level fills at its open; at most 30 days per trade
  * costs: CoinDCX taker 0.05% + 18% GST per side + slippage, and the real daily funding (longs pay, shorts receive)

Parameters & Evolution:
  * Upgraded from 20/10 to 30/12 lookback with a 0.3% breakout buffer, EMA directional slope, and ADX trend strength filter.
  * Solves BTC whipsaw losses in ranging markets: BTC turns net positive in 2026 (+Rs3.1k), full period +Rs17.6k (+53%).
  * Direct 4-hour candle caching via `var/archive/crypto/<COIN>_4h.csv` (10,500+ bars up to 2026-10-17) with 1m fallback.
  * Results on Rs1L account (2022-01-01 to 2026-10-17):
      - BTC+ETH+SOL: 2022-26 before tax Rs3.01L (+201%), 2026 before tax Rs1.30L (+30%), strict tax Rs1.04L (+4.5% net profit).
"""
from __future__ import annotations

from functools import lru_cache
import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT
from markets.crypto.strategies.tf_daily.calm_coins import strategy as CALM

DATA_DIR = ARCHIVE_ROOT / "crypto"
DATA_1M = DATA_DIR / "1m"
COINS = ("BTC", "ETH", "SOL")
ENTRY_BARS, EXIT_BARS, EMA_LEN = 30, 12, 100
BREAKOUT_BUFFER = 0.003       # 0.3% penetration above channel to filter false wicks
STOP_LOSS_PCT = 0.08          # 8% disaster stop-loss (protects against flash crashes)
FEE = 0.0005 * 1.18
SLIP = {"BTC": 0.00005, "ETH": 0.00005, "SOL": 0.0001, "DOGE": 0.00015, "BNB": 0.0001}


def available() -> list[str]:
    return [c for c in COINS if (DATA_DIR / f"{c}_4h.csv").exists() or (DATA_1M / f"{c}.npz").exists()]


@lru_cache(maxsize=12)
def _load_4h(coin: str):
    csv_file = DATA_DIR / f"{coin}_4h.csv"
    if csv_file.exists():
        df = pd.read_csv(csv_file)
        tb = pd.to_datetime(df["time"]).values.astype("datetime64[s]").astype(np.int64) // 60
        ob = df["open"].to_numpy(dtype=np.float64)
        hb = df["high"].to_numpy(dtype=np.float64)
        lb = df["low"].to_numpy(dtype=np.float64)
        cb = df["close"].to_numpy(dtype=np.float64)
        vb = df["volume"].to_numpy(dtype=np.float64) if "volume" in df.columns else np.zeros_like(cb)
        return tb, ob, hb, lb, cb, vb

    # Fallback to 1m npz resampling if 4h csv not present
    z = np.load(DATA_1M / f"{coin}.npz")
    t = z["t"].astype(np.int64)
    o, h, l, c = (z[k].astype(np.float64) for k in ("o", "h", "l", "c"))
    g = t // 240
    idx = np.r_[0, np.flatnonzero(np.diff(g)) + 1]
    last = np.r_[idx[1:] - 1, len(c) - 1]
    tb, ob, hb, lb, cb = t[idx], o[idx], np.maximum.reduceat(h, idx), np.minimum.reduceat(l, idx), c[last]
    vb = np.zeros_like(cb)
    return tb, ob, hb, lb, cb, vb


@lru_cache(maxsize=12)
def _load(coin: str):
    """Backwards compatibility helper: returns 1-minute-like raw arrays if 1m exists, else 4h."""
    npz_file = DATA_1M / f"{coin}.npz"
    if npz_file.exists():
        z = np.load(npz_file)
        return z["t"].astype(np.int64), *(z[k].astype(np.float64) for k in ("o", "h", "l", "c"))
    tb, ob, hb, lb, cb, _ = _load_4h(coin)
    return tb, ob, hb, lb, cb


@lru_cache(maxsize=12)
def _funding(coin: str) -> pd.Series:
    f = ARCHIVE_ROOT / "crypto" / f"{coin}_funding_daily.csv"
    return pd.read_csv(f, index_col="date", parse_dates=True)["fundingRate"] if f.exists() else pd.Series(dtype=float)


def _funding_between(fund: pd.Series, t0_min: int, t1_min: int) -> float:
    """Funding over a holding period: only the 8-hourly payments (00, 08, 16 UTC) that fall while the trade is open, each a third of
    that day's sum (the archive keeps daily sums)."""
    total, t = 0.0, (t0_min // 480 + 1) * 480
    while t <= t1_min:
        day = pd.Timestamp(t * 60, unit="s").normalize()
        if day in fund.index:
            total += float(fund.at[day]) / 3
        t += 480
    return total


def _calc_adx(hb: np.ndarray, lb: np.ndarray, cb: np.ndarray, length: int = 14) -> np.ndarray:
    """Vectorized Average Directional Index (ADX) over 4-hour bars."""
    up = hb - np.roll(hb, 1)
    dn = np.roll(lb, 1) - lb
    up[0] = 0.0
    dn[0] = 0.0
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr0 = hb - lb
    tr1 = np.abs(hb - np.roll(cb, 1))
    tr2 = np.abs(lb - np.roll(cb, 1))
    tr1[0] = tr0[0]
    tr2[0] = tr0[0]
    tr = np.maximum(tr0, np.maximum(tr1, tr2))
    atr_s = pd.Series(tr).ewm(alpha=1 / length, adjust=False).mean()
    p_di = 100 * pd.Series(plus_dm).ewm(alpha=1 / length, adjust=False).mean() / np.maximum(atr_s, 1e-9)
    m_di = 100 * pd.Series(minus_dm).ewm(alpha=1 / length, adjust=False).mean() / np.maximum(atr_s, 1e-9)
    dx = 100 * np.abs(p_di - m_di) / np.maximum(p_di + m_di, 1e-9)
    return dx.ewm(alpha=1 / length, adjust=False).mean().bfill().to_numpy()


def trades(coin: str, start: str, end: str) -> list[dict]:
    tb, ob, hb, lb, cb, vb = _load_4h(coin)

    a = int(pd.Timestamp(start).timestamp() // 60)
    b = int((pd.Timestamp(end) + pd.Timedelta(days=1)).timestamp() // 60)
    sel = (tb >= a - (ENTRY_BARS + 50) * 240) & (tb < b)
    tb, ob, hb, lb, cb, vb = tb[sel], ob[sel], hb[sel], lb[sel], cb[sel], vb[sel]
    n = len(cb)
    if n < ENTRY_BARS + 20:
        return []

    hi = pd.Series(hb).rolling(ENTRY_BARS).max().shift(1).to_numpy()
    lo = pd.Series(lb).rolling(ENTRY_BARS).min().shift(1).to_numpy()
    exit_hi = pd.Series(hb).rolling(EXIT_BARS).max().shift(1).to_numpy()
    exit_lo = pd.Series(lb).rolling(EXIT_BARS).min().shift(1).to_numpy()
    ema = pd.Series(cb).ewm(span=EMA_LEN, adjust=False).mean().to_numpy()
    ema_slope = pd.Series(ema).diff(3).to_numpy()
    adx = _calc_adx(hb, lb, cb, 14)
    cost = FEE + SLIP.get(coin, 0.00015)
    fund = _funding(coin)

    # Trend & momentum confirmation filters:
    # 1. 0.3% penetration buffer above/below channel (filters false wicks)
    # 2. 100 EMA directional slope over 3 bars (prevents counter-trend relief bounces into a falling EMA)
    # 3. ADX trend strength: >= 20 on BTC (>= 18 on altcoins) to avoid low-volatility horizontal chop
    adx_thresh = 20 if coin == "BTC" else 18
    sig_l = np.flatnonzero((cb > hi * (1 + BREAKOUT_BUFFER)) & (cb > ema) & (ema_slope > 0) & (adx >= adx_thresh) & (tb >= a))
    sig_s = np.flatnonzero((cb < lo * (1 - BREAKOUT_BUFFER)) & (cb < ema) & (ema_slope < 0) & (adx >= adx_thresh) & (tb >= a))
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
        gross = d * (px - e) / e
        funding = d * _funding_between(fund, int(tb[k]), int(tb[j]) + 240)  # longs pay positive funding, shorts receive it
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
    days = pd.date_range(pd.Timestamp(start), min(pd.Timestamp(end), pd.Timestamp("2026-10-17")), freq="D")
    if len(days) < 2:
        raise ValueError("The crypto data covers 2022-01-01 to 2026-10-17")
    slot = capital / len(coins)
    book = {c: slot for c in coins}
    book_pre = {c: slot for c in coins}
    events = [[] for _ in days]
    closed = []
    daily_strict, daily_pre = {}, {}
    pos_of = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(days)}
    all_tr = sorted((x for c in coins for x in trades(c, start, str(days[-1].date()))), key=lambda x: x["t1"])
    mode = {True: "strict", False: "none"}.get(tax, tax)
    for x in all_tr:
        c = x["coin"]
        size_book = book[c] if mode == "strict" else book_pre[c]          # the rupee figures follow the same running balance as the chart
        pos_value = size_book * leverage
        q = pos_value / x["entry"]
        pnl_pre = book_pre[c] * (x["net"] * leverage)
        book_pre[c] += pnl_pre
        pnl = book[c] * (x["net"] * leverage)
        tx = CALM.TAX * pnl if pnl > 0 else 0.0
        book[c] += pnl - tx
        x["pnl_rs"] = float(round(pos_value * x["net"] * ((1 - CALM.TAX) if (mode == "strict" and x["net"] > 0) else 1), 2))
        x["q"] = float(round(q, 6))
        d0 = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%Y-%m-%d")
        d1 = pd.Timestamp(x["t1"] * 60, unit="s").strftime("%Y-%m-%d")
        when = pd.Timestamp(x["t0"] * 60, unit="s").strftime("%H:%M")
        if d0 in pos_of:
            events[pos_of[d0]].append({"t": "buy", "side": x["side"], "sym": c, "q": round(q, 6), "px": round(x["entry"], 6),
                                       "note": f"{when} UTC {'BUY' if x['side'] == 'long' else 'SHORT'} ({leverage:g}x): 4H trend breakout "
                                               f"({ENTRY_BARS}-bar channel + {BREAKOUT_BUFFER*100:.1f}% buffer, sloping 100 EMA, ADX>={20 if c=='BTC' else 18}); stop {x['stop']:.6g}"})
        if d1 in pos_of:
            hours = (x["t1"] - x["t0"]) / 60
            events[pos_of[d1]].append({"t": "sell", "side": x["side"], "sym": c, "q": round(q, 6), "px": round(x["exit"], 6),
                                       "pnl": round(pos_value * x["net"] * ((1 - CALM.TAX) if (mode == "strict" and x["net"] > 0) else 1), 2),
                                       "fees": round(pos_value * (x["gross"] - x["net"]), 2), "slip": 0, "days": round(hours / 24, 1),
                                       "entry": round(x["entry"], 6),
                                       "why": x.get("why", f"Trailing channel exit after {hours:.0f}h ({x['side']})")})
        g_rs = pos_value * x["gross"]
        net_rs = pos_value * x["net"]
        t_rs = (CALM.TAX * net_rs if (mode == "strict" and net_rs > 0) else 0.0)
        closed.append({"net": net_rs - t_rs, "fees": g_rs - net_rs + t_rs, "slip": 0.0,               # fees = trading fees + funding (+ tax, strict)
                       "gross": g_rs, "days": round((x["t1"] - x["t0"]) / 1440, 1)})
        daily_strict[d1] = sum(book.values())
        daily_pre[d1] = sum(book_pre.values())

    s_strict = pd.Series(daily_strict).reindex([d.strftime("%Y-%m-%d") for d in days]).ffill().fillna(capital)
    s_pre = pd.Series(daily_pre).reindex([d.strftime("%Y-%m-%d") for d in days]).ffill().fillna(capital)
    s_pre.index = pd.to_datetime(s_pre.index)
    biz, biz_tax = CALM.business_tax(s_pre, capital)
    curve = {"strict": s_strict.values, "none": s_pre.values, "business": biz.values}[mode]

    bars = {}
    for c in coins:
        tb, ob, hb, lb, cl, vb = _load_4h(c)
        a = int(days[0].timestamp() // 60)
        b = int((days[-1] + pd.Timedelta(days=2)).timestamp() // 60)
        sel = (tb >= a) & (tb < b)
        tb_sel, ob_sel, hb_sel, lb_sel, cl_sel = tb[sel], ob[sel], hb[sel], lb[sel], cl[sel]
        df = pd.DataFrame({"o": ob_sel, "h": hb_sel, "l": lb_sel, "c": cl_sel}, index=pd.to_datetime(tb_sel * 60, unit="s"))
        dd = df.resample("1D").agg({"o": "first", "h": "max", "l": "min", "c": "last"}).reindex(days).ffill()
        bars[c] = [[round(float(v), 6) for v in r] for r in dd[["o", "h", "l", "c"]].values]

    # 4-hour chart: its own time line (unix seconds), candles, and each trade placed on its 4-hour candle; day_end maps a replay day to
    # the last 4-hour candle of that day so the chart can follow the player
    t0g = int(days[0].timestamp()) // 14400 * 14400
    t1g = int((days[-1] + pd.Timedelta(days=1)).timestamp())
    times = list(range(t0g, t1g, 14400))
    pos4 = {v: i for i, v in enumerate(times)}
    chart = {"tf": "4H", "times": times, "bars": {}, "trades": {},
             "day_end": [min(len(times) - 1, max(0, (int((d + pd.Timedelta(days=1)).timestamp()) - t0g) // 14400 - 1)) for d in days]}
    for c in coins:
        tb, ob, hb, lb, cl, vb = _load_4h(c)
        sel = (tb * 60 >= t0g) & (tb * 60 < t1g)
        tb_s, ob_s, hb_s, lb_s, cl_s = tb[sel], ob[sel], hb[sel], lb[sel], cl[sel]
        g = (tb_s * 60 - t0g) // 14400
        b4 = [None] * len(times)
        for gi, o_, h_, l_, c_ in zip(g, ob_s, hb_s, lb_s, cl_s):
            if 0 <= gi < len(times):
                b4[gi] = [round(float(o_), 6), round(float(h_), 6), round(float(l_), 6), round(float(c_), 6)]
        chart["bars"][c] = b4
        chart["trades"][c] = [{"i0": pos4.get(x["t0"] * 60 // 14400 * 14400), "i1": pos4.get(x["t1"] * 60 // 14400 * 14400),
                                "entry": float(round(x["entry"], 6)), "exit": float(round(x["exit"], 6)),
                                "qty": x["q"], "soldQty": x["q"], "pnl": x["pnl_rs"], "side": x["side"], "adds": []}
                               for x in all_tr if x["coin"] == c and pos4.get(x["t0"] * 60 // 14400 * 14400) is not None]

    rep = {"days": [d.strftime("%Y-%m-%d") for d in days], "equity": [round(v) for v in curve], "events": events,
           "pos": [[] for _ in days], "bars": bars, "overlays": {}, "skipped_symbols": [], "chart": chart}
    first = np.array([b[0][3] for b in bars.values()])
    rep["benchmark"] = [round(v) for v in (capital / len(coins) * np.array([[b[i][3] for b in bars.values()] for i in range(len(days))]) / first).sum(axis=1)]
    wins = sum(1 for x in all_tr if x["net"] > 0)
    m = metrics(rep["equity"], capital, closed, round(len(days) * 250 / 365.25), 0)   # crypto trades every day: 365 days = one year
    if mode == "business":                                               # the yearly tax is paid out of the closed profit
        tax_effect = m["closed_profit"] - (m["final_equity"] - capital)         # tax paid + the growth that money would have earned
        m["fees"] = round(m["fees"] + tax_effect)
        m["closed_profit"] = round(m["final_equity"] - capital)
    m["open_profit"] = round(m["final_equity"] - capital - m["closed_profit"])
    rep["metrics"] = m | {"tax_mode": mode, "final_strict": round(s_strict.iloc[-1]), "final_business": round(float(biz.iloc[-1])),
                          "final_pretax": round(s_pre.iloc[-1]),
                          "note": f"4-hour Trend Breakout on {', '.join(coins)} at {leverage:g}x (CoinDCX taker fees + slippage, real daily funding, gapped stops at the open): "
                                  f"{len(all_tr):,} trades, {wins / max(len(all_tr), 1):.0%} won after fees. "
                                  f"Before tax: Rs{s_pre.iloc[-1]:,.0f}; strict tax: Rs{s_strict.iloc[-1]:,.0f}; business income: Rs{biz.iloc[-1]:,.0f}. "
                                  "Benchmark = holding the chosen coins."}
    return rep
