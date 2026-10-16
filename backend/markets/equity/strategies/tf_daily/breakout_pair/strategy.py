"""equity / breakout_pair -- two swing breakout systems on the most-traded NSE stocks, half the money each, long only, delivery.

Research 2026-10-16 (user: "find out some other strategy which make us more money", then "do what is best"). ~75 versions of 9 swing
designs (20/55-day breakouts, RSI(2) dips, pullback to the 20 EMA, gap-up on volume, 52-week highs, NR7, the trend_ema_cross entry) on a
FAIR stock list: each day the 200 stocks with the highest 126-day median traded value among ALL NSE equities on that day (no hindsight
list). Rs1 lakh, trend_ema_cross's money rules, next-open fills, gap-through stops, Upstox delivery fees + 5 bp slippage a side.
Rules chosen on 2022-10..2024-12, judged on 2025-01..2026-10:

  sleeve  entry (decided on a completed day)                                        2022-24   2025-26
  nr7     narrowest high-low range of the last 7 days, close > EMA50 > EMA200 ->     +37%/yr   +18%/yr   all 16 neighbouring settings
          buy-stop at that day's high on the next day (fills at max(open, high))                          positive in both periods
  d55     close above the highest high of the previous 55 days, close > EMA200       +12%/yr   +33%/yr
          -> buy at the next open
  both, half the money each (5 + 3 stocks): Rs1L -> Rs2.28L (Oct 2022 -> Oct 2026), +23%/yr, worst fall -16%,
          2023 +48% | 2024 +4% | 2025 +12% | 2026 +28%  (NIFTY: Rs1.41L, 2026 -12%)
  for comparison, trend_ema_cross's rules on the same fair list: Rs1.41L, worst fall -39%, 2025 -11%, 2026 -17%
  (its paper results lean on its 20 BEST_SYMBOLS, which were picked on 2023-25).

Common rules (trend_ema_cross's): buy only when >= 50% of the 200 stocks close above their 50-day EMA (market health); risk 2% of the
sleeve's value to a 2.5-ATR(14) stop, at most 1/K of the sleeve in one stock; trailing stop 3.25 ATR under the highest high since the buy,
only moves up; a trade still below its buy price after 20 trading days is sold at the next open. Candidates are taken strongest first by
126-day return. Not proven: four years, about half the profit came from the 5 best trades, 2023 (PSU rally) was the biggest year.

Not a core.strategy.Strategy (it ranks ~200 stocks together every day), so it has its own paper engine (engine/equity_breakout.py) and
its own backtest (backtest() below). Data: var/archive/equity_all/<SYM>_daily.csv.gz, refreshed nightly by markets/equity/all_daily.py.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT

DATA_DIR = ARCHIVE_ROOT / "equity_all"
UNIVERSE = 200                  # top N by 126-day median traded value
LIQ_DAYS, MIN_HISTORY, MIN_PRICE = 126, 125, 20.0
MIN_BREADTH_PCT = 50.0
RISK_PCT, STOP_ATR, TRAIL_ATR, DEAD_DAYS = 2.0, 2.5, 3.25, 20
SLEEVES = {"nr7": dict(K=5, share=0.5, name="nr7_breakout"), "d55": dict(K=3, share=0.5, name="d55_breakout")}
SPLIT_LOW, SPLIT_HIGH = 0.6, 1.7   # an overnight open/prev-close outside this is a split / bonus / demerger, not a market move


def load(symbols=None, since="2022-01-01") -> dict[str, pd.DataFrame]:
    """Daily open/high/low/close/traded value per stock (columns = symbols), history scaled for splits, bonuses and demergers."""
    o, h, l, c, v = {}, {}, {}, {}, {}
    files = [DATA_DIR / f"{s}_daily.csv.gz" for s in symbols] if symbols else sorted(glob.glob(str(DATA_DIR / "*_daily.csv.gz")))
    for f in files:
        if not os.path.exists(f):
            continue
        x = pd.read_csv(f)
        if len(x) < 30:
            continue
        x["d"] = pd.to_datetime(x["timestamp"].str[:10])
        x = x.drop_duplicates("d", keep="last").set_index("d").sort_index()
        r = x["open"] / x["close"].shift()
        adj = pd.Series(1.0, index=x.index)
        for i in np.where((r < SPLIT_LOW) | (r > SPLIT_HIGH))[0]:
            adj.iloc[:i] *= r.iloc[i]
        s = os.path.basename(str(f))[: -len("_daily.csv.gz")]
        o[s], h[s], l[s], c[s] = x["open"] * adj, x["high"] * adj, x["low"] * adj, x["close"] * adj
        v[s] = (x["close"] * x["volume"]).astype(float)
    mk = lambda d: pd.DataFrame(d).sort_index().loc[since:].astype(float)
    return {"O": mk(o), "H": mk(h), "L": mk(l), "C": mk(c), "VAL": mk(v)}


def indicators(p: dict) -> dict:
    """Everything the rules need, as date x stock frames (row t uses data up to and including day t)."""
    O, H, L, C, VAL = p["O"], p["H"], p["L"], p["C"], p["VAL"]
    ema = lambda n: C.ewm(span=n, adjust=False, min_periods=n).mean()
    e50, e200 = ema(50), ema(200)
    tr = pd.concat([H - L, (H - C.shift()).abs(), (L - C.shift()).abs()]).groupby(level=0).max().reindex(C.index)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    liq = VAL.rolling(LIQ_DAYS, min_periods=100).median()
    hist = C.notna().rolling(LIQ_DAYS + 4, min_periods=1).sum()
    rank = liq.where((hist >= MIN_HISTORY) & (C >= MIN_PRICE)).rank(axis=1, ascending=False)
    univ = rank <= UNIVERSE
    breadth = ((C > e50) & univ).sum(axis=1) / univ.sum(axis=1).replace(0, np.nan) * 100
    rng = H - L
    nr7 = univ & (rng <= rng.rolling(7).min()) & (C > e50) & (e50 > e200)
    d55 = univ & (C > H.shift(1).rolling(55).max()) & (C > e200)
    return dict(e50=e50, e200=e200, atr=atr, univ=univ, breadth=breadth, nr7=nr7, d55=d55, mom126=C / C.shift(126) - 1, **p)


def candidates(ind: dict, day) -> dict:
    """Signals from the completed day `day`: {'healthy': bool, 'breadth': %, 'nr7': [(sym, trigger)], 'd55': [sym]}, strongest first."""
    b = float(ind["breadth"].loc[day])
    order = lambda mask: ind["mom126"].loc[day, mask.loc[day][mask.loc[day]].index].sort_values(ascending=False).index.tolist()
    return {"healthy": b >= MIN_BREADTH_PCT, "breadth": b, "d55": order(ind["d55"]),
            "nr7": [(s, float(ind["H"].at[day, s])) for s in order(ind["nr7"])]}


def size(equity: float, cash: float, K: int, entry: float, stop: float) -> int:
    """Shares: 2% of the sleeve at risk to the stop, at most 1/K of the sleeve, never more than the sleeve's cash."""
    if not entry > stop:
        return 0
    return int(np.floor(min(equity * RISK_PCT / 100 / (entry - stop), min(equity / K, cash) / entry)))


def costs(entry: float, exit_price: float, qty: int, symbol: str = "") -> dict:
    from markets.equity.costs import compute_nse_equity_delivery_costs
    return compute_nse_equity_delivery_costs("long", entry, exit_price, qty, symbol=symbol)


def _fee(ev: float, xv: float) -> float:
    """Backtest fee: Upstox delivery charges + 5 bp slippage a side (as in the research)."""
    br = lambda v: min(v * 0.001, 30.0)
    return (br(ev) + br(xv)) * 1.18 + (ev + xv) * 0.001 + ev * 0.00015 + (ev + xv) * 0.0000335 * 1.18 + 20 * 1.18 + (ev + xv) * 0.0005


def backtest_sleeve(ind: dict, sleeve: str, start, end, cap: float) -> dict:
    """One sleeve on its own capital: the same rules the paper engine applies, on daily bars."""
    K = SLEEVES[sleeve]["K"]
    O, H, L, C, A = (ind[k].values for k in ("O", "H", "L", "C", "atr"))
    idx = ind["C"].index; col = {s: j for j, s in enumerate(ind["C"].columns)}
    sig = ind[sleeve]; days = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))]
    cash, pos, trades, curve, pend = cap, {}, [], [], set()

    def sell(s, xp, d, why):
        nonlocal cash
        p = pos.pop(s); cash += p["q"] * xp - _fee(p["q"] * p["e"], p["q"] * xp)
        trades.append(dict(sym=s, entry_day=p["d0"], exit_day=d, entry=p["e"], exit=xp, qty=p["q"], reason=why, sleeve=sleeve))

    for di, d in enumerate(days):
        i = idx.get_loc(d)
        for s in list(pend):
            if s in pos:
                xp = O[i, col[s]]
                sell(s, xp if xp == xp else pos[s]["last"], d, "dead_money")
        pend = set()
        if di > 0 and len(pos) < K:
            y = idx[i - 1]; cand = candidates(ind, y)
            if cand["healthy"]:
                for s, *trig in ([(x,) for x in cand["d55"]] if sleeve == "d55" else cand["nr7"]):
                    if len(pos) >= K: break
                    if s in pos: continue
                    j = col[s]; op, hi = O[i, j], H[i, j]
                    if not op == op: continue
                    if trig:
                        if not hi >= trig[0]: continue
                        e = max(op, trig[0])
                    else:
                        e = op
                    a = A[i - 1, j]
                    if not a > 0: continue
                    stop = e - STOP_ATR * a
                    equity = cash + sum(p["q"] * p["last"] for p in pos.values())
                    q = size(equity, cash, K, e, stop)
                    if q < 1: continue
                    cash -= q * e; pos[s] = dict(q=q, e=e, stop=stop, hi=e, d0=d, n=0, last=e)
        for s in list(pos):
            p = pos[s]; j = col[s]; op, lo, hi, cl = O[i, j], L[i, j], H[i, j], C[i, j]
            if not cl == cl: continue
            if op <= p["stop"]: sell(s, op, d, "stop"); continue
            if lo <= p["stop"]: sell(s, p["stop"], d, "stop"); continue
            p["n"] += 1; p["hi"] = max(p["hi"], hi); p["last"] = cl
            p["stop"] = max(p["stop"], p["hi"] - TRAIL_ATR * A[i, j])
            if p["n"] >= DEAD_DAYS and cl < p["e"]: pend.add(s)
        curve.append((d, cash + sum(p["q"] * p["last"] for p in pos.values())))
    return dict(curve=pd.Series(dict(curve)), trades=pd.DataFrame(trades), open=pos)


def backtest(start="2022-10-03", end=None, capital=100_000.0, ind: dict | None = None) -> dict:
    """Both sleeves, half the capital each. Returns the combined equity curve, the trades and yearly returns."""
    ind = ind or indicators(load())
    end = end or ind["C"].index[-1]
    parts = {s: backtest_sleeve(ind, s, start, end, capital * cfg["share"]) for s, cfg in SLEEVES.items()}
    curve = sum(p["curve"] for p in parts.values())
    yearly = curve.groupby(curve.index.year).last()
    yearly = yearly / yearly.shift(1).fillna(capital) - 1
    yrs = (curve.index[-1] - curve.index[0]).days / 365.25
    return dict(curve=curve, trades=pd.concat([p["trades"] for p in parts.values()], ignore_index=True), yearly=yearly,
                final=float(curve.iloc[-1]), cagr=(curve.iloc[-1] / capital) ** (1 / yrs) - 1, max_dd=float((curve / curve.cummax() - 1).min()))
