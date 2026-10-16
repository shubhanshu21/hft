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
  both, half the money each (5 + 3 stocks), one account, a stock held at most once (as the paper engine trades):
          Rs1L -> Rs1.90L (Oct 2022 -> Oct 2026), +17%/yr, worst fall -18%, 2023 +39% | 2024 +4% | 2025 +10% | 2026 +18%;
          from 1 Jan 2026 alone: +13.4%, worst fall -8%  (NIFTY: Rs1.41L, 2026 -12%)
          (the research ran the sleeves as two separate accounts, which sometimes held the same stock twice: Rs2.28L -- not the real figure)
  for comparison, trend_ema_cross's rules on the same fair list: Rs1.41L, worst fall -39%, 2025 -11%, 2026 -17%
  (its paper results lean on its 20 BEST_SYMBOLS, which were picked on 2023-25).

Common rules (trend_ema_cross's): buy only when >= 50% of the 200 stocks close above their 50-day EMA (market health); risk 2% of the
sleeve's value to a 2.5-ATR(14) stop, at most 1/K of the sleeve in one stock; trailing stop 3.25 ATR under the highest high since the buy,
only moves up; a trade still below its buy price after 20 trading days is sold at the next open. Candidates are taken strongest first by
126-day return. Not proven: four years, about half the profit came from the 5 best trades, 2023 (PSU rally) was the biggest year.

Not a core.strategy.Strategy (it ranks ~200 stocks together every day), so it has its own paper engine (engine/equity_swing.py, the breakout third of the EQUITY_SWING account) and
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


def _fee(ev: float, xv: float, slip_bp: float = 5.0) -> tuple[float, float]:
    """Backtest charges for one round trip: (Upstox delivery fees, slippage at slip_bp a side)."""
    br = lambda v: min(v * 0.001, 30.0)
    fees = (br(ev) + br(xv)) * 1.18 + (ev + xv) * 0.001 + ev * 0.00015 + (ev + xv) * 0.0000335 * 1.18 + 20 * 1.18
    return fees, (ev + xv) * slip_bp / 1e4


def backtest(start="2022-10-03", end=None, capital=100_000.0, ind: dict | None = None, risk_pct: float = RISK_PCT, slip_bp: float = 5.0,
             record: bool = False, lev: float = 1.0, mtf_rate: float = 0.15, slots: dict | None = None) -> dict:
    """Both sleeves in one account, day by day, the way engine/equity_swing.py trades: each sleeve has its share of the money and its
    own slots; one stock is held at most once across the account. Per day: dead-money sales and gap-through stops at the open, d55 buys at
    the open, nr7 buy-stops (strongest first), then stops touched during the day; after the close, trailing stops and the dead-money count.
    record=True also returns the web backtester's replay (days, equity, events, pos, bars, benchmark).
    Research options (defaults = the paper account): lev = buying power as a multiple of the money (Upstox MTF-style borrowing; interest at
    mtf_rate a year on the borrowed amount, charged daily); slots = {sleeve: K} to change the number of holdings."""
    Ks = {k: (slots or {}).get(k, cfg["K"]) for k, cfg in SLEEVES.items()}
    interest = [0.0]
    ind = ind or indicators(load())
    O, H, L, C, A = (ind[k].values for k in ("O", "H", "L", "C", "atr"))
    idx = ind["C"].index; cols = list(ind["C"].columns); col = {s: j for j, s in enumerate(cols)}
    days = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end or idx[-1]))]
    cash = {k: capital * cfg["share"] for k, cfg in SLEEVES.items()}
    pos: dict[str, dict] = {}; trades, curve, pend = [], [], set()
    rep = {"days": [], "equity": [], "events": [], "pos": []}; traded = set()

    def sell(s, xp, d, why, evs):
        p = pos.pop(s); fee, slip = _fee(p["q"] * p["e"], p["q"] * xp, slip_bp)
        net = p["q"] * (xp - p["e"]) - fee - slip
        cash[p["sleeve"]] += p["q"] * xp - fee - slip
        trades.append(dict(sym=s, sleeve=p["sleeve"], entry_day=p["d0"], exit_day=d, entry=p["e"], exit=xp, qty=p["q"], reason=why,
                           net=net, fees=fee, slip=slip, gross=p["q"] * (xp - p["e"])))
        evs.append({"t": "sell", "sym": s, "q": p["q"], "px": round(xp, 2), "pnl": round(net, 2), "fees": round(fee, 2), "slip": round(slip, 2),
                    "days": (d - p["d0"]).days, "why": {"stop": "Stop / trailing stop", "dead_money": "Still losing after 20 days"}[why], "entry": round(p["e"], 2)})

    def sleeve_equity(k):
        return cash[k] + sum(p["q"] * p["last"] for p in pos.values() if p["sleeve"] == k)

    def buy(k, s, e, a, d, evs, why):
        K = Ks[k]
        if s in pos or sum(p["sleeve"] == k for p in pos.values()) >= K or not a > 0:
            return
        stop = e - STOP_ATR * a
        eq = sleeve_equity(k)
        q = int(np.floor(min(eq * risk_pct / 100 / (e - stop), min(eq * lev / K, cash[k] + (lev - 1) * eq) / e)))
        if q < 1:
            return
        cash[k] -= q * e; traded.add(s)
        pos[s] = dict(q=q, e=e, stop=stop, hi=e, d0=d, n=0, last=e, sleeve=k, i0=len(rep["days"]))
        evs.append({"t": "buy", "sym": s, "q": q, "px": round(e, 2), "note": f"{why}, stop {stop:,.2f}"})

    for di, d in enumerate(days):
        i = idx.get_loc(d); evs = []
        if di > 0:                                                      # interest on borrowed money (cash below zero), per calendar day
            for k in cash:
                if cash[k] < 0:
                    c = -cash[k] * mtf_rate * (d - days[di - 1]).days / 365; cash[k] -= c; interest[0] += c
        for s in list(pend):                                            # dead money: sell at the open
            if s in pos:
                xp = O[i, col[s]]
                sell(s, xp if xp == xp else pos[s]["last"], d, "dead_money", evs)
        pend = set()
        for s in list(pos):                                             # gap through the stop: sell at the open
            op = O[i, col[s]]
            if op == op and op <= pos[s]["stop"]:
                sell(s, op, d, "stop", evs)
        if di > 0:
            cand = candidates(ind, idx[i - 1])
            if cand["healthy"]:
                for s in cand["d55"]:                                   # d55: at the open, strongest first
                    op = O[i, col[s]]
                    if op == op:
                        buy("d55", s, op, A[i - 1, col[s]], d, evs, "55-day high")
                for s, trig in cand["nr7"]:                             # nr7: buy-stop at the quiet day's high
                    j = col[s]; op, hi = O[i, j], H[i, j]
                    if op == op and hi >= trig:
                        buy("nr7", s, max(op, trig), A[i - 1, j], d, evs, "quiet-day breakout")
        for s in list(pos):                                             # stops touched during the day; then the close
            p = pos[s]; j = col[s]; lo, hi, cl = L[i, j], H[i, j], C[i, j]
            if not cl == cl:
                continue
            if lo <= p["stop"]:
                sell(s, p["stop"], d, "stop", evs); continue
            p["n"] += 1; p["hi"] = max(p["hi"], hi); p["last"] = cl
            p["stop"] = max(p["stop"], p["hi"] - TRAIL_ATR * A[i, j])
            if p["n"] >= DEAD_DAYS and cl < p["e"]:
                pend.add(s)
        value = sum(cash.values()) + sum(p["q"] * p["last"] for p in pos.values())
        curve.append((d, value))
        if record:
            rep["days"].append(d.strftime("%Y-%m-%d")); rep["equity"].append(round(value)); rep["events"].append(evs)
            rep["pos"].append([{"sym": s, "q": p["q"], "e": round(p["e"], 2), "px": round(p["last"], 2), "start": p["i0"]} for s, p in pos.items()])
    curve = pd.Series(dict(curve))
    yearly = curve.groupby(curve.index.year).last()
    yearly = yearly / yearly.shift(1).fillna(capital) - 1
    yrs = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
    out = dict(curve=curve, trades=pd.DataFrame(trades), open=pos, interest=interest[0], yearly=yearly, final=float(curve.iloc[-1]),
               cagr=(curve.iloc[-1] / capital) ** (1 / yrs) - 1, max_dd=float((curve / curve.cummax() - 1).min()))
    if record:
        sel = (idx >= days[0]) & (idx <= days[-1])
        rep["bars"] = {s: [[round(float(x), 2) if x == x else None for x in row] for row in zip(ind["O"].loc[sel, s], ind["H"].loc[sel, s], ind["L"].loc[sel, s], ind["C"].loc[sel, s])]
                       for s in sorted(traded)}
        rep["bars"] = {s: [r if r[3] is not None else None for r in v] for s, v in rep["bars"].items()}
        out["replay"] = rep
    return out


def web_backtest(capital: float, start: str, end: str, risk_pct: float = RISK_PCT, slippage_bp: float = 5.0) -> dict:
    """The web backtester's replay shape (engine/backtest_api.py, the New backtest page)."""
    from core.backtester import metrics
    r = backtest(start, end, capital, risk_pct=risk_pct, slip_bp=slippage_bp, record=True)
    rep = r["replay"]
    if len(rep["days"]) < 2:
        raise ValueError("No trading days in the chosen dates (daily data starts January 2022; signals need ~200 days of history, so from late 2022)")
    t = r["trades"]
    closed = [{"net": x.net, "fees": x.fees, "slip": x.slip, "gross": x.gross, "days": (x.exit_day - x.entry_day).days} for x in t.itertuples()] if len(t) else []
    nb = pd.read_csv(ARCHIVE_ROOT / "equity" / "NIFTYBEES_daily.csv")
    nb.index = pd.to_datetime(nb["timestamp"].str[:10])
    n = nb["close"].reindex(pd.to_datetime(rep["days"])).ffill().bfill()
    rep["benchmark"] = [round(capital * v / n.iloc[0]) for v in n]
    rep["metrics"] = metrics(rep["equity"], capital, closed, len(rep["days"]), len(r["open"])) | {
        "note": "Picks from the 200 most-traded NSE stocks of each day (no fixed list). Half the money: quiet-day (NR7) breakouts, up to 5 stocks; "
                "half: 55-day-high breakouts, up to 3 stocks. Buys only while >= 50% of those 200 stocks are above their 50-day average. "
                "Benchmark = NIFTY 50 (NIFTYBEES)."}
    rep["skipped_symbols"] = []
    return rep
