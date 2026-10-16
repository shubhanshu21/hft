"""crypto / calm_btc -- hold BTC futures (long, 1x) only while the crypto market is CALM; otherwise stay in cash. CoinDCX USDT futures.

Research 2026-10-16 (user: "now lets focus on crypto I have coindcx account" ... "do proper research build our own staretgy", "find sweet spot"):
  * a feature map over 12 coins (2022-01 -> 2026-10, daily) found one timing effect that held in BOTH 2022-24 and 2025-26: after a calm
    stretch the market does better over the next 1-4 weeks, after a wild one worse (crashes come in high-volatility periods);
  * calm = the 20-day standard deviation of the AVERAGE coin's daily log return (12 coins below) is under CALM_THRESHOLD = 2.43%/day,
    the low third of 2022-24 (learned on 2022-24 only, never re-fitted);
  * re-decided every week on the last completed daily candle (00:00 UTC): calm -> hold BTC long, wild -> cash. The money is split into 7
    parts, part k deciding on weekday k: a single weekday's result is partly luck (Rs1.19L-2.22L by weekday), the 7 parts together are steady;
  * costs: CoinDCX futures taker 0.05% + 18% GST per side, 0.05% slippage, real funding (longs mostly pay ~6%/yr on BTC).
Results, Rs1L, March 2022 -> Oct 2026, after fees, funding and India's 30% tax on every winning trade (losses not set off):
  7 parts at 1x: Rs1,57,427 (+12%/yr 2022-24, worst fall -36%; +9%/yr 2025-26, worst -26%; 2026 to 9 Oct +14%) vs holding BTC
  Rs1,37,391 (worst -68%). Checking every day instead was worse (Rs1.02-1.30L: more flips, more tax).
  Leverage sweet spot (1x..3x, chosen on 2022-24 by return per unit of worst fall): 1x after tax AND before tax. 1.5x adds ~Rs4k after tax
  and deepens the worst fall to -59%; 3x -84%; 10x wiped out. Before tax (if futures losses can be set off -- ask a CA) 1x: Rs2,78,534.
Selection (which coin) effects were real but too weak to survive the tax; long/short coin picking lost in every version.
Paper: engine/crypto_calm.py (account CRYPTO_CALM). Data: markets/crypto/data.py (nightly).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT

DATA_DIR = ARCHIVE_ROOT / "crypto"
COINS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "TRX")
TRADE = "BTC"
CALM_THRESHOLD = 0.0243
VOL_DAYS = 20
FEE_SIDE = 0.0005 * 1.18 + 0.0005            # taker 0.05% + GST, + 0.05% slippage
TAX = 0.30


def load() -> dict[str, pd.DataFrame]:
    px = {c: pd.read_csv(DATA_DIR / f"{c}_1d.csv", index_col="date", parse_dates=True) for c in COINS if (DATA_DIR / f"{c}_1d.csv").exists()}
    out = {k: pd.DataFrame({c: px[c][k] for c in px}).sort_index() for k in ("open", "high", "low", "close")}
    f = DATA_DIR / f"{TRADE}_funding_daily.csv"
    out["funding"] = (pd.read_csv(f, index_col="date", parse_dates=True)["fundingRate"].reindex(out["close"].index).fillna(0)
                      if f.exists() else pd.Series(0.0, index=out["close"].index))
    return out


def market_vol(close: pd.DataFrame) -> pd.Series:
    """20-day standard deviation of the average coin's daily log return."""
    return np.log(close).diff().mean(axis=1).rolling(VOL_DAYS).std()


def calm_on(d: dict, day) -> bool:
    """The weekly decision from the completed candle `day`."""
    return bool(market_vol(d["close"]).loc[:day].iloc[-1] < CALM_THRESHOLD)


def backtest(start="2022-03-01", end=None, capital=100_000.0, leverage=1.0, tax=True, d: dict | None = None, record=False) -> dict:
    d = d or load()
    C, O, F = d["close"][TRADE], d["open"][TRADE], d["funding"]
    mv = market_vol(d["close"])
    idx = C.index; days = idx[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end or idx[-1]))]
    week = idx.to_period("W")
    E, units, mark, entry, efee = capital, 0.0, 0.0, 0.0, 0.0
    on = False; curve, trades = [], []
    rep = {"days": [], "equity": [], "events": [], "pos": []}
    for i, t in enumerate(days):
        j = idx.get_loc(t); evs = []
        if j > 0 and week[j] != week[j - 1]:                                  # Monday: decide from Sunday's completed candle
            y = idx[j - 1]; on = bool(mv.at[y] < CALM_THRESHOLD)
            p = O.at[t]
            if units and not on:                                              # sell to cash at the open
                E += units * (p - mark); fee = abs(units) * p * FEE_SIDE; E -= fee
                gain = units * (p - entry) - fee - efee; tx = TAX * gain if (tax and gain > 0) else 0.0; E -= tx
                trades.append(dict(entry_day=t0, exit_day=t, entry=entry, exit=p, qty=units, net=gain - tx, fees=fee + efee, tax=tx, gross=units * (p - entry)))
                evs.append({"t": "sell", "sym": TRADE, "q": round(units, 6), "px": round(p, 2), "pnl": round(gain - tx, 2), "fees": round(fee + efee, 2), "slip": 0,
                            "days": (t - t0).days, "why": f"Market turned wild (20-day volatility {mv.at[y]:.2%} >= {CALM_THRESHOLD:.2%})", "entry": round(entry, 2)})
                units = 0.0
            elif not units and on:                                            # buy at the open
                q = leverage * E / p; efee = abs(q) * p * FEE_SIDE; E -= efee
                units, mark, entry, t0 = q, p, p, t
                evs.append({"t": "buy", "sym": TRADE, "q": round(q, 6), "px": round(p, 2), "note": f"market calm (20-day volatility {mv.at[y]:.2%} < {CALM_THRESHOLD:.2%})"})
        if units:                                                              # mark to the close + funding
            E += units * (C.at[t] - mark); mark = C.at[t]; E -= units * C.at[t] * F.at[t]
        curve.append((t, E))
        if record:
            rep["days"].append(t.strftime("%Y-%m-%d")); rep["equity"].append(round(E)); rep["events"].append(evs)
            rep["pos"].append([{"sym": TRADE, "q": round(units, 6), "e": round(entry, 2), "px": round(mark, 2), "start": rep["days"].index(t0.strftime("%Y-%m-%d"))}] if units else [])
    e = pd.Series(dict(curve)); yrs = max((e.index[-1] - e.index[0]).days / 365.25, 1 / 365.25)
    yr = e.groupby(e.index.year).last(); yr = yr / yr.shift(1).fillna(capital) - 1
    out = dict(curve=e, trades=pd.DataFrame(trades), final=float(e.iloc[-1]), cagr=(max(e.iloc[-1], 1) / capital) ** (1 / yrs) - 1,
               max_dd=float((e / e.cummax() - 1).min()), yearly=yr, open=units)
    if record:
        sel = (idx >= days[0]) & (idx <= days[-1])
        rep["bars"] = {TRADE: [[round(float(v), 2) for v in r] for r in zip(d["open"][TRADE][sel], d["high"][TRADE][sel], d["low"][TRADE][sel], C[sel])]}
        rep["overlays"] = {TRADE: {"lines": [], "markers": [], "osc": {"name": "market volatility % (calm below 2.43)", "values": [None if v != v else round(float(v) * 100, 3) for v in mv[sel]],
                                                                       "levels": [round(CALM_THRESHOLD * 100, 2)]}}}
        out["replay"] = rep
    return out


PARTS = 7                                     # the money is split into 7 parts; part k decides on weekday k (Monday = 0)


def _shift(d: dict, k: int) -> dict:
    """The data with dates moved k days earlier, so a 'Monday' decision in backtest() falls on weekday k of the real calendar."""
    out = {}
    for key, v in d.items():
        v = v.copy(); v.index = v.index - pd.Timedelta(days=k); out[key] = v
    return out


def backtest_staggered(start="2022-03-01", end=None, capital=100_000.0, leverage=1.0, tax=True, d: dict | None = None, record=False) -> dict:
    """7 parts of capital/7, part k re-deciding calm/wild every week on weekday k (removes the luck of one weekday: single-day results ranged
    Rs1.19L-2.22L, the 7 parts together Rs1.57L, worst fall -36%)."""
    d = d or load()
    end = pd.Timestamp(end or d["close"].index[-1])
    runs = []
    for k in range(PARTS):
        r = backtest(pd.Timestamp(start) - pd.Timedelta(days=k), end - pd.Timedelta(days=k), capital / PARTS, leverage=leverage, tax=tax, d=_shift(d, k), record=record)
        r["curve"].index = r["curve"].index + pd.Timedelta(days=k)
        if len(r["trades"]):
            r["trades"]["entry_day"] += pd.Timedelta(days=k); r["trades"]["exit_day"] += pd.Timedelta(days=k); r["trades"]["part"] = k
        runs.append(r)
    curve = pd.concat([r["curve"] for r in runs], axis=1).dropna().sum(axis=1)
    yrs = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
    yr = curve.groupby(curve.index.year).last(); yr = yr / yr.shift(1).fillna(capital) - 1
    out = dict(curve=curve, trades=pd.concat([r["trades"] for r in runs], ignore_index=True), final=float(curve.iloc[-1]),
               cagr=(max(curve.iloc[-1], 1) / capital) ** (1 / yrs) - 1, max_dd=float((curve / curve.cummax() - 1).min()), yearly=yr,
               open=sum(r["open"] for r in runs))
    if record:
        days = [x.strftime("%Y-%m-%d") for x in curve.index]; pos_of = {x: i for i, x in enumerate(days)}
        rep = {"days": days, "equity": [round(v) for v in curve.values], "events": [[] for _ in days], "pos": [[] for _ in days]}
        held = [0.0] * len(days); cost = [0.0] * len(days)
        for k, r in enumerate(runs):
            rp = r["replay"]
            for i, dd in enumerate(rp["days"]):
                real = (pd.Timestamp(dd) + pd.Timedelta(days=k)).strftime("%Y-%m-%d")
                if real not in pos_of: continue
                j = pos_of[real]
                rep["events"][j] += [{**e, "note" if e["t"] == "buy" else "why": f"part {k + 1}/7 ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][k]}): " + (e.get("note") or e.get("why", ""))} for e in rp["events"][i]]
                for p in rp["pos"][i]: held[j] += p["q"]; cost[j] += p["q"] * p["e"]
        C = d["close"][TRADE]
        for j, dd in enumerate(days):
            if held[j] > 1e-12:
                rep["pos"][j] = [{"sym": TRADE, "q": round(held[j], 6), "e": round(cost[j] / held[j], 2), "px": round(float(C.get(pd.Timestamp(dd), np.nan)), 2), "start": j}]
        sel = (d["close"].index >= curve.index[0]) & (d["close"].index <= curve.index[-1])
        mv = market_vol(d["close"])
        rep["bars"] = {TRADE: [[round(float(v), 2) for v in r] for r in zip(d["open"][TRADE][sel], d["high"][TRADE][sel], d["low"][TRADE][sel], C[sel])]}
        rep["overlays"] = {TRADE: {"lines": [], "markers": [], "osc": {"name": "market volatility % (calm below 2.43)",
                                                                       "values": [None if v != v else round(float(v) * 100, 3) for v in mv[sel]], "levels": [round(CALM_THRESHOLD * 100, 2)]}}}
        out["replay"] = rep
    return out


def web_backtest(capital: float, start: str, end: str, leverage: float = 1.0, tax: bool = True) -> dict:
    from core.backtester import metrics
    r = backtest_staggered(start, end, capital, leverage=leverage, tax=tax, record=True)
    rep = r["replay"]
    if len(rep["days"]) < 2:
        raise ValueError("No days in the chosen dates (crypto data starts 2022-01; the volatility needs 20 days)")
    t = r["trades"]
    closed = [{"net": x.net, "fees": x.fees, "slip": 0.0, "gross": x.gross, "days": (x.exit_day - x.entry_day).days} for x in t.itertuples()] if len(t) else []
    c = load()["close"][TRADE].reindex(pd.to_datetime(rep["days"])).ffill()
    rep["benchmark"] = [round(capital * v / c.iloc[0]) for v in c]
    m = metrics(rep["equity"], capital, closed, len(rep["days"]), 1 if r["open"] else 0)
    tax_paid = float(t["tax"].sum()) if len(t) else 0.0
    rep["metrics"] = m | {"note": f"BTC futures (CoinDCX, USDT) long at {leverage:g}x only while the 12-coin market's 20-day volatility is below "
                                  f"{CALM_THRESHOLD:.2%}/day; the money is split into 7 parts, each re-deciding on its own weekday; otherwise cash. "
                                  "Fees, slippage and real funding included"
                                  + (f"; India's 30% tax on each winning trade, losses not set off (tax paid Rs{tax_paid:,.0f})." if tax else "; NO income tax (the before-tax result).")
                                  + " Benchmark = holding BTC."}
    rep["skipped_symbols"] = []
    return rep
