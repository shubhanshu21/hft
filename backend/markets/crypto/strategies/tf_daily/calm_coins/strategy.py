"""crypto / calm_coins -- hold 5 coins' futures (BTC ETH SOL DOGE TRX, long, 1x, equal money) only while the crypto market is CALM;
otherwise stay in cash. CoinDCX USDT futures.

Research 2026-10-16/17 (user: "now lets focus on crypto I have coindcx account" ... "do proper research build our own staretgy", "find sweet spot",
"see if other coins earning then its fine if they are not earning more that also fine"):
  * a feature map over 12 coins (2022-01 -> 2026-10, daily) found one timing effect that held in BOTH 2022-24 and 2025-26: after a calm
    stretch the market does better over the next 1-4 weeks, after a wild one worse (crashes come in high-volatility periods);
  * calm = the 20-day standard deviation of the AVERAGE coin's daily log return (12 coins below) is under CALM_THRESHOLD = 2.43%/day,
    the low third of 2022-24 (learned on 2022-24 only, never re-fitted);
  * re-decided every week on the last completed daily candle (00:00 UTC): calm -> hold, wild -> cash. Each coin's money is split into 7
    parts, part k deciding on weekday k: a single weekday's result is partly luck (BTC: Rs1.19L-2.22L by weekday), 7 parts are steady;
  * coins: the calm rule run on each of the 12; TRADED = those that made money on 2022-24 (BTC +12%/yr, ETH +6, SOL +29, DOGE +31, TRX +11);
    all five also made money on the judging years 2025-26 (+8, +18, +2, +7, +10%/yr). XRP BNB ADA AVAX LINK DOT LTC lost on 2022-24;
  * costs: CoinDCX futures taker 0.05% + 18% GST per side, 0.05% slippage, real funding (longs mostly pay ~4-8%/yr).
Results, Rs1L, March 2022 -> Oct 2026, after fees, funding and India's 31.2% tax (30% + 4% cess) on every winning trade (losses not set off), 1x:
  5 coins, Rs3L+ (every order fits CoinDCX's minimums): 1.80x (+18%/yr 2022-24, +8.5%/yr 2025-26), worst fall -33%, at 31.2% tax (30% + 4% cess;
  1.87x at a flat 30%). BTC alone 1.56x at 30%, worst -36%.
  5 coins, Rs1L: Rs1.73L (+16%/yr, +7%/yr), worst -30% -- a BTC part (Rs2,857) is below the smallest BTC order (0.001 BTC ~Rs8,100), so
  BTC's fifth stays in cash; Rs2L 1.76x. Order sizes follow LOT (rounded down to CoinDCX's step) in the backtest and the paper account.
  Checking every day instead was worse (more flips, more tax). Leverage sweet spot (BTC, 1x..3x, by return per unit of worst fall): 1x.
Rejected (2026-10-16/17): 91 walk-forward AI/ML versions, 624 per-coin rules (each coin's best 2022-24 rule: 2 of 12 positive in 2025-26),
long/short and coin picking (tax), funding collection (~3%/yr before tax, ~0 after).
Paper: engine/crypto_calm.py (account CRYPTO_CALM, hft-crypto-calm.timer 05:45 IST daily). Data: markets/crypto/data.py (topped up by that run).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.paths import ARCHIVE_ROOT

DATA_DIR = ARCHIVE_ROOT / "crypto"
COINS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "TRX")
TRADED = ("BTC", "ETH", "SOL", "DOGE", "TRX")       # equal money each
CALM_THRESHOLD = 0.0243
VOL_DAYS = 20
FEE_SIDE = 0.0005 * 1.18 + 0.0005            # taker 0.05% + GST, + 0.05% slippage
TAX = 0.312                                   # 30% (Section 115BBH) + 4% health & education cess; surcharge above Rs50L income not included
# CoinDCX futures order rules (instrument API, 2026-10-17): quantity step = smallest quantity, and the smallest order value in USDT.
LOT = {"BTC": (0.001, 60.0), "ETH": (0.001, 24.0), "SOL": (0.01, 6.0), "DOGE": (1.0, 6.0), "TRX": (1.0, 6.0)}
USDTINR = 90.0                                 # rupees per USDT for the backtest's order-size check (CoinDCX ~85-100 over 2022-26)


def order_qty(coin: str, rupees: float, price_usdt: float, usdtinr: float = USDTINR) -> float:
    """Coins one order can buy with `rupees`: rounded DOWN to CoinDCX's quantity step; 0 if below its smallest quantity or order value."""
    step, min_usdt = LOT.get(coin, (0.0, 0.0))
    q = rupees / usdtinr / price_usdt
    if step:
        q = int(q / step + 1e-9) * step
    return round(q, 8) if q > 0 and q >= step and q * price_usdt >= min_usdt else 0.0


def load() -> dict[str, pd.DataFrame]:
    px = {c: pd.read_csv(DATA_DIR / f"{c}_1d.csv", index_col="date", parse_dates=True) for c in COINS if (DATA_DIR / f"{c}_1d.csv").exists()}
    out = {k: pd.DataFrame({c: px[c][k] for c in px}).sort_index() for k in ("open", "high", "low", "close")}
    fund = {c: pd.read_csv(DATA_DIR / f"{c}_funding_daily.csv", index_col="date", parse_dates=True)["fundingRate"]
            for c in COINS if (DATA_DIR / f"{c}_funding_daily.csv").exists()}
    out["funding"] = pd.DataFrame(fund).reindex(out["close"].index).reindex(columns=out["close"].columns).fillna(0)
    return out


def market_vol(close: pd.DataFrame) -> pd.Series:
    """20-day standard deviation of the average coin's daily log return."""
    return np.log(close).diff().mean(axis=1).rolling(VOL_DAYS).std()


def calm_on(d: dict, day) -> bool:
    """The weekly decision from the completed candle `day`."""
    return bool(market_vol(d["close"]).loc[:day].iloc[-1] < CALM_THRESHOLD)


def backtest(start="2022-03-01", end=None, capital=100_000.0, leverage=1.0, tax=True, d: dict | None = None, record=False, coin="BTC", vol_k=None) -> dict:
    """One coin, one part deciding on Mondays."""
    d = d or load()
    TRADE = coin
    C, O, F = d["close"][TRADE], d["open"][TRADE], d["funding"][TRADE]
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
                trades.append(dict(entry_day=t0, exit_day=t, entry=entry, exit=p, qty=units / USDTINR, net=gain - tx, fees=fee + efee, tax=tx, gross=units * (p - entry)))
                evs.append({"t": "sell", "sym": TRADE, "q": round(units / USDTINR, 6), "px": round(p, 2), "pnl": round(gain - tx, 2), "fees": round(fee + efee, 2), "slip": 0,
                            "days": (t - t0).days, "why": f"Market turned wild (20-day volatility {mv.at[y]:.2%} >= {CALM_THRESHOLD:.2%})", "entry": round(entry, 2)})
                units = 0.0
            elif not units and on:                                            # buy at the open
                lev = size_for(mv.at[y], vol_k) if vol_k else leverage   # smart sizing: more when very calm (up to VOL_MAX)
                q = order_qty(TRADE, lev * E, p) * USDTINR            # real order size (rupee units: price is read as rupees per USDT-coin)
                if q > 0:
                    efee = abs(q) * p * FEE_SIDE; E -= efee
                    units, mark, entry, t0 = q, p, p, t
                if q > 0: evs.append({"t": "buy", "sym": TRADE, "q": round(q / USDTINR, 6), "px": round(p, 2), "note": f"market calm (20-day volatility {mv.at[y]:.2%} < {CALM_THRESHOLD:.2%})"})
        if units:                                                              # mark to the close + funding
            E += units * (C.at[t] - mark); mark = C.at[t]; E -= units * C.at[t] * F.at[t]
        curve.append((t, E))
        if record:
            rep["days"].append(t.strftime("%Y-%m-%d")); rep["equity"].append(round(E)); rep["events"].append(evs)
            rep["pos"].append([{"sym": TRADE, "q": round(units / USDTINR, 6), "e": round(entry, 2), "px": round(mark, 2), "start": rep["days"].index(t0.strftime("%Y-%m-%d"))}] if units else [])
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


VOL_K, VOL_MAX = 0.8, 2.0                      # smart sizing (2026-10-17): size = 0.8 x 2.43% / market volatility, at most 2x the part's money


def size_for(vol: float, k: float = VOL_K) -> float:
    """Position size as a multiple of the part's money when buying: bigger when the market is very calm, never above VOL_MAX."""
    return float(min(VOL_MAX, k * CALM_THRESHOLD / vol)) if vol and vol == vol else 1.0


PARTS = 7                                     # the money is split into 7 parts; part k decides on weekday k (Monday = 0)


def _shift(d: dict, k: int) -> dict:
    """The data with dates moved k days earlier, so a 'Monday' decision in backtest() falls on weekday k of the real calendar."""
    out = {}
    for key, v in d.items():
        v = v.copy(); v.index = v.index - pd.Timedelta(days=k); out[key] = v
    return out


def backtest_staggered(start="2022-03-01", end=None, capital=100_000.0, leverage=1.0, tax=True, d: dict | None = None, record=False, coin="BTC", vol_k=None) -> dict:
    """7 parts of capital/7, part k re-deciding calm/wild every week on weekday k (removes the luck of one weekday: single-day results ranged
    Rs1.19L-2.22L, the 7 parts together Rs1.57L, worst fall -36%)."""
    d = d or load()
    TRADE = coin
    end = pd.Timestamp(end or d["close"].index[-1])
    runs = []
    for k in range(PARTS):
        r = backtest(pd.Timestamp(start) - pd.Timedelta(days=k), end - pd.Timedelta(days=k), capital / PARTS, leverage=leverage, tax=tax, d=_shift(d, k), record=record, coin=coin, vol_k=vol_k)
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
                rep["events"][j] += [{**e, "note" if e["t"] == "buy" else "why": f"{TRADE} part {k + 1}/7 ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][k]}): " + (e.get("note") or e.get("why", ""))} for e in rp["events"][i]]
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


def backtest_basket(start="2022-03-01", end=None, capital=100_000.0, leverage=1.0, tax=True, d: dict | None = None, record=False, coins=TRADED, vol_k=None) -> dict:
    """Equal money in each coin, each run as 7 weekday parts."""
    d = d or load()
    runs = {c: backtest_staggered(start, end, capital / len(coins), leverage=leverage, tax=tax, d=d, record=record, coin=c, vol_k=vol_k) for c in coins}
    curve = pd.concat([r["curve"] for r in runs.values()], axis=1).dropna().sum(axis=1)
    yrs = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
    yr = curve.groupby(curve.index.year).last(); yr = yr / yr.shift(1).fillna(capital) - 1
    trades = pd.concat([r["trades"].assign(coin=c) for c, r in runs.items() if len(r["trades"])], ignore_index=True) if any(len(r["trades"]) for r in runs.values()) else pd.DataFrame()
    out = dict(curve=curve, trades=trades, final=float(curve.iloc[-1]), cagr=(max(curve.iloc[-1], 1) / capital) ** (1 / yrs) - 1,
               max_dd=float((curve / curve.cummax() - 1).min()), yearly=yr, open=sum(1 for r in runs.values() if r["open"]))
    if record:
        days = [x.strftime("%Y-%m-%d") for x in curve.index]
        rep = {"days": days, "equity": [round(v) for v in curve.values], "events": [[] for _ in days], "pos": [[] for _ in days], "bars": {}, "overlays": {}}
        for c, r in runs.items():
            rp = r["replay"]; at = {x: i for i, x in enumerate(rp["days"])}
            for j, dd in enumerate(days):
                if dd in at:
                    rep["events"][j] += rp["events"][at[dd]]
                    rep["pos"][j] += [p | {"start": j if p["start"] is None else p["start"]} for p in rp["pos"][at[dd]]]
            first = at.get(days[0], 0)
            rep["bars"][c] = rp["bars"][c][first: first + len(days)]
            rep["overlays"][c] = rp["overlays"][c] | {"osc": rp["overlays"][c]["osc"] | {"values": rp["overlays"][c]["osc"]["values"][first: first + len(days)]}}
        out["replay"] = rep
    return out


def business_tax(curve: pd.Series, capital: float, rate: float = TAX, carry_years: int = 4) -> tuple[pd.Series, float]:
    """The other tax reading: futures profit as speculative BUSINESS income -- tax on each financial year's NET profit (April-March), a
    year's loss carried forward up to 4 years against later profits. `curve` is the BEFORE-tax account value; the result is the account
    after paying that tax at each 31 March (and on the last day, as if the account were closed then), plus the total tax."""
    r = curve / curve.shift(1) - 1; r.iloc[0] = curve.iloc[0] / capital - 1
    fy = [d.year + (1 if d.month >= 4 else 0) for d in curve.index]          # FY 2026-27 -> 2027
    A, start, out, carry, paid = capital, capital, [], [], 0.0
    for k, (d, g) in enumerate(r.items()):
        A *= 1 + g
        if k == len(r) - 1 or fy[k + 1] != fy[k]:
            profit = A - start
            if profit > 0:
                use = 0.0
                for c in carry:
                    take = min(c[1], profit - use); c[1] -= take; use += take
                tax = rate * (profit - use); A -= tax; paid += tax
            elif profit < 0:
                carry.append([fy[k], -profit])
            carry = [c for c in carry if c[1] > 0 and fy[k] - c[0] < carry_years]
            start = A
        out.append(A)
    return pd.Series(out, index=curve.index), paid


def web_backtest(capital: float, start: str, end: str, leverage: float = 1.0, tax="strict") -> dict:
    """tax: 'strict' (31.2% on every winning trade, losses never set off), 'business' (31.2% on each year's net profit, losses carried 4
    years), 'none' (before tax). The chart follows the chosen rule; the note always shows all three results."""
    from core.backtester import metrics
    mode = {True: "strict", False: "none"}.get(tax, tax)
    r = backtest_basket(start, end, capital, leverage=leverage, tax=(mode == "strict"), record=True)
    rep = r["replay"]
    if len(rep["days"]) < 2:
        raise ValueError("No days in the chosen dates (crypto data starts 2022-01; the volatility needs 20 days)")
    pre = r if mode != "strict" else backtest_basket(start, end, capital, leverage=leverage, tax=False)
    strict = r if mode == "strict" else backtest_basket(start, end, capital, leverage=leverage, tax=True)
    biz, biz_tax = business_tax(pre["curve"], capital)
    if mode == "business":
        rep["equity"] = [round(v) for v in biz.reindex(pd.to_datetime(rep["days"])).ffill().values]
    t = r["trades"]
    closed = [{"net": x.net, "fees": x.fees, "slip": 0.0, "gross": x.gross, "days": (x.exit_day - x.entry_day).days} for x in t.itertuples()] if len(t) else []
    c = load()["close"][list(TRADED)].reindex(pd.to_datetime(rep["days"])).ffill()
    rep["benchmark"] = [round(v) for v in (capital / len(TRADED) * c / c.iloc[0]).sum(axis=1)]
    m = metrics(rep["equity"], capital, closed, round(len(rep["days"]) * 250 / 365.25), r["open"])   # crypto: 365 days a year
    st_tax = float(strict["trades"]["tax"].sum()) if len(strict["trades"]) else 0.0
    shown = {"strict": "the STRICT crypto rule", "business": "the BUSINESS-income rule", "none": "NO tax"}[mode]
    rep["metrics"] = m | {"tax_mode": mode, "final_strict": round(strict["final"]), "final_business": round(float(biz.iloc[-1])), "final_pretax": round(pre["final"]),
                          "note": f"Futures (CoinDCX, USDT) on {', '.join(TRADED)}, equal money each, long at {leverage:g}x only while the 12-coin market's "
                                  f"20-day volatility is below {CALM_THRESHOLD:.2%}/day; each coin's money in 7 weekday parts; otherwise cash. Fees, slippage "
                                  f"and real funding included. Chart and figures use {shown}. All three: before tax Rs{pre['final']:,.0f}; "
                                  f"STRICT crypto rule (31.2% of every winning trade, losses never set off -- the safe reading) Rs{strict['final']:,.0f} "
                                  f"(tax Rs{st_tax:,.0f}); BUSINESS income (31.2% of each April-March year's net profit, losses carried 4 years -- what many CAs "
                                  f"use for futures, not confirmed by CBDT) Rs{biz.iloc[-1]:,.0f} (tax Rs{biz_tax:,.0f}). Benchmark = holding the 5 coins."}
    rep["skipped_symbols"] = []
    return rep
