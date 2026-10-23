"""commodity / trend_carry -- monthly MCX trend + carry + skewness: hold the 3 commodities that have risen most, whose next contract is
cheapest, and that jump up least suddenly.

Research: Fuertes, Miffre & Rallis (2010), "Tactical allocation in commodity futures markets: combining momentum and term structure signals"
(momentum alone ~9%/yr, the combination best). Tested 2026-10-15 on real MCX month-end bhavcopies (the user downloaded them in a browser,
Dec 2021 -> Oct 2026, every expiry) -- see markets/commodity/futures_eod.py for the data.

Rules (fixed before the test; 8 liquid commodities; decided at each month end, held one month):
  * trend = return over the last 12 months of holding the nearest contract that is still alive at the next month end, rolled monthly;
  * carry = log(near / next contract price) per year (positive = the next month is cheaper = backwardation = buyers earn the roll);
  * score = rank(trend) + rank(carry) + rank(-skewness of daily returns over the last year) (skewness added 2026-10-23, see targets());
    buy the top 3, buy-only (selling short tested worse);
  * size each holding by 1 / its 12-month volatility (the research standard; 2026-10-15 test: +6-11%/yr at the same worst fall);
  * trade the MINI contracts (GOLDTEN, SILVERMIC, CRUDEOILM, NATGASMINI, ZINCMINI, ALUMINI, LEADMINI; COPPER has no mini), whole lots only;
    a holding whose one lot is larger than its share stays in cash.
Result on real MCX prices, Rs10 lakh at 1x, vol sizing: Rs10L -> Rs20.4L (2023-26), worst fall -12.9%. NOT proven: ~46 months, a few big
months decide each year (2023 -16%), and 2 or 4 holdings instead of 3 did much worse. Long/short and trend-only or carry-only versions were weaker.
Account protection tested: the trend filter and a drawdown brake both cut returns without cutting the worst fall -> not used. Exposure is the
real protection: at 1x the worst month was ~-13%; at 2-3x it was -25% to -44%; at full Upstox leverage (~8x) June 2026 alone lost ~85%.

This is not a core.strategy.Strategy (it ranks 8 commodities together and picks expiries), so it has its own monthly paper engine
(engine/mcx_trend_carry.py) and its own backtest (backtest() below, also used by the web backtest page).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from markets.commodity.costs import LOT_SIZE_TEXT, compute_mcx_costs, lot_units

SIGNALS = ("GOLDM", "SILVERM", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC", "ALUMINIUM", "LEAD")
TRADE = {"GOLDM": "GOLDTEN", "SILVERM": "SILVERMIC", "CRUDEOIL": "CRUDEOILM", "NATURALGAS": "NATGASMINI", "COPPER": "COPPER",
         "ZINC": "ZINCMINI", "ALUMINIUM": "ALUMINI", "LEAD": "LEADMINI"}
TOP_N, LOOKBACK, VOL_WINDOW = 3, 12, 12
SKEW_DAYS = 365                # 2026-10-23: skewness of daily returns over the last year joins the score (user's choice, see targets())
MARGIN_PCT = {"GOLDM": 0.093, "SILVERM": 0.126, "CRUDEOIL": 0.35, "NATURALGAS": 0.30, "COPPER": 0.10, "ZINC": 0.092, "ALUMINIUM": 0.093, "LEAD": 0.10}
MAX_MARGIN_USE = 0.8           # never block more than 80% of the account as margin
EXPOSURE_MIN = 0.25
EXPOSURE_MAX = round(1 / min(MARGIN_PCT.values()), 1)      # the most Upstox lets any of them be geared (gold ~9.3% margin -> ~10.8x)
SIGNAL_OF = {v: k for k, v in TRADE.items()}                 # mini contract -> the signal contract it follows


def top_n(n_symbols: int) -> int:
    """3 holdings out of 8; with fewer commodities chosen, at most half of them (so there is still a ranking), at least 1."""
    return max(1, min(TOP_N, n_symbols // 2))


def month_ends(table: pd.DataFrame, before: pd.Timestamp | None = None) -> list[pd.Timestamp]:
    """Last trading day of each month (only COMPLETED months when `before` is given). The bhavcopy date is preferred when it IS the month's
    end (within 3 days of the newest day that month: it has every contract, including ones Upstox no longer serves); a mid-month bhavcopy
    (the 2026-10-13 download) must not stand in for the month end -- that bug would have traded on 13-October prices on 2 November."""
    t = table[table["symbol"].isin(SIGNALS)]
    if before is not None:
        t = t[t["date"] < before.replace(day=1)]
    out = []
    for _, g in t.groupby(t["date"].dt.to_period("M")):
        last = g["date"].max()
        b = g[g["source"] == "bhavcopy"]["date"] if "source" in g else g["date"]
        out.append(b.max() if len(b) and (last - b.max()).days <= 3 else last)
    return sorted(out)


def _rows(table, sym, day):
    x = table[(table["symbol"] == sym) & (table["date"] == day) & (table["close"] > 0)]
    return x.sort_values("expiry")


def panel(table: pd.DataFrame, ends: list[pd.Timestamp], signals=SIGNALS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """R: month-by-month return of holding each commodity's contract alive at the next month end; C: carry at each month end."""
    R, C = {}, {}
    for s in signals:
        r, c = {}, {}
        for i, t in enumerate(ends):
            cur = _rows(table, s, t)
            two = cur[(cur["expiry"] > t + pd.Timedelta(days=3)) & (cur["volume"] > 0)].head(2)
            if len(two) == 2:
                days = max((two["expiry"].iloc[1] - two["expiry"].iloc[0]).days, 1)
                c[t] = np.log(two["close"].iloc[0] / two["close"].iloc[1]) * 365 / days
            if i + 1 < len(ends):
                nxt = ends[i + 1]
                alive = cur[cur["expiry"] > nxt]
                if len(alive):
                    k = alive.iloc[0]
                    later = _rows(table, s, nxt)
                    later = later[later["expiry"] == k["expiry"]]
                    if len(later):
                        r[nxt] = later["close"].iloc[0] / k["close"] - 1
        R[s], C[s] = pd.Series(r, dtype=float), pd.Series(c, dtype=float)
    return pd.DataFrame(R).sort_index(), pd.DataFrame(C).sort_index()


def skew_panel(table: pd.DataFrame, ends: list[pd.Timestamp], signals=SIGNALS) -> pd.DataFrame:
    """K: at each month end, the skewness of each commodity's daily returns over the last SKEW_DAYS (nearest contract, day-to-day returns
    only within the same contract, so a roll is never counted as a move). Low (negative) skew = fewer sudden up-jumps = scores higher."""
    K = {}
    for s in signals:
        d = table[(table["symbol"] == s) & (table["close"] > 0)].sort_values(["date", "expiry"])
        front = d.groupby("date").first()
        ret = front["close"].pct_change().where(front["expiry"].eq(front["expiry"].shift()))
        K[s] = pd.Series({t: ret[(ret.index <= t) & (ret.index > t - pd.Timedelta(days=SKEW_DAYS))].skew() for t in ends}, dtype=float)
    return pd.DataFrame(K).sort_index()


def targets(R: pd.DataFrame, C: pd.DataFrame, t: pd.Timestamp, K: pd.DataFrame) -> dict[str, float]:
    """{signal symbol: weight} decided at month end t (weights sum to 1; empty when there is not yet 12 months of history).

    score = rank(12m trend) + rank(carry) + rank(-skewness)  [skewness: Fernandez-Perez, Frijns, Fuertes & Miffre (2018), "The skewness of
    commodity futures returns" -- low-skew commodities earn more]. Added 2026-10-23 at the user's request. Rs2.5L, real costs, nearest-lot
    rounding: 2023-24 +15% -> +9% (WORSE), 2025-26 +190% -> +217%, 2023-26 +184% -> +222%, worst fall -26% -> -6%. It did not beat
    trend+carry in both windows (the project's usual bar); the user chose it for the higher total and the much smaller worst fall and
    asked to REMOVE the old trend+carry-only scoring (no fallback). Skew window: 270 days similar (+219%, -11%), 180 days much weaker
    (+111%, -30%) -- the result holds near one year, not everywhere.
    Basis-momentum (Boons & Prado 2019) was tested in the same round and rejected (2025-26 -16% alone, +106% combined)."""
    mom = (1 + R).rolling(LOOKBACK).apply(np.prod, raw=True) - 1
    if t not in mom.index or t not in C.index:
        return {}
    if t not in K.index:
        return {}
    score = (mom.loc[t].rank() + C.loc[t].rank() + (-K.loc[t]).rank()).dropna()
    n = top_n(len(R.columns))
    if len(score) < 2 * n:
        return {}
    picks = list(score.nlargest(n).index)
    vol = R.rolling(VOL_WINDOW, min_periods=6).std().loc[t]
    inv = {s: 1 / vol[s] for s in picks if vol.get(s, 0) > 0}
    tot = sum(inv.values())
    return {s: v / tot for s, v in inv.items()} if tot else {s: 1 / len(picks) for s in picks}


MAX_LOT_X = 2.0                # a rounded-up holding may be at most 2x the account in contract value


def lots_for(capital: float, exposure: float, weight: float, price: float, signal: str) -> int:
    """Whole lots of the mini contract: the notional share (capital x exposure x weight) ROUNDED to the nearest lot (was: rounded down).

    2026-10-22: rounding down left a Rs2.5L account (the user's real size) in cash whenever a pick's share was under one lot -- e.g.
    Oct 2026: zinc's share Rs2.33L vs one ZINCMINI lot Rs4.15L -> 0 lots, account idle. Rounding to the nearest lot (a share of >= half a
    lot buys one), capped at MAX_LOT_X x the account per holding and at the margin limit, tested at Rs2.5L: 2023 +3% / 2024 +10% (same as
    before), 2025 +23% -> +70%, 2026 +26% -> +51%; 2023-26 +94% -> +184%; worst fall -15% -> -26% (2026 -21%). Profit spread across zinc,
    crude, silver and gold, not one trade. Never fewer lots than the old round-down rule."""
    lot_val = price * lot_units(TRADE[signal])
    by_notional = capital * exposure * weight // lot_val
    by_margin = capital * MAX_MARGIN_USE * weight // (lot_val * MARGIN_PCT[signal])
    floor_lots = int(max(0, min(by_notional, by_margin)))
    lots = int(capital * exposure * weight / lot_val + 0.5)
    while lots > 0 and (lots * lot_val > MAX_LOT_X * capital or lots * lot_val * MARGIN_PCT[signal] > MAX_MARGIN_USE * capital):
        lots -= 1
    return max(lots, floor_lots)


def backtest(table: pd.DataFrame, capital: float = 250_000, start: str = "2023-01-01", end: str = "2100-01-01", exposure: float = 1.5,
             symbols=None) -> dict:
    """Month-end backtest in the web backtester's replay shape: one row per month end (days, equity, events, pos, bars, benchmark, metrics).
    At each month end the last month's holdings are sold at that day's close of the SAME contract and the new ones bought at the close of the
    contract alive past the next month end. Prices are the signal contract's month-end close (minis track it per unit); fees and half-spread
    from markets/commodity/costs.py on the mini contract."""
    from core.backtester import metrics
    signals = tuple(SIGNAL_OF.get(x, x) for x in symbols) if symbols else SIGNALS     # the mini contracts chosen on the page -> their signal contracts
    ends = month_ends(table)
    R, C = panel(table, ends, signals)
    K = skew_panel(table, ends, signals)
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    ends = [t for t in ends if t1 >= t >= t0 - pd.offsets.MonthEnd(1)]
    cap, book, closed, skipped = capital, {}, [], []
    out = {"days": [], "equity": [], "events": [], "pos": [], "bars": {}}
    bench, bench_v = [], capital
    prev_px: dict[str, float] = {}
    for k, t in enumerate(ends):
        evs = []
        for s, h in list(book.items()):                                  # 1. sell last month's holdings at this month end
            now = _rows(table, s, t)
            now = now[now["expiry"] == h["expiry"]]
            px = float(now["close"].iloc[0]) if len(now) else h["entry"]
            c = compute_mcx_costs(TRADE[s], "long", h["entry"], px, h["lots"])
            cap += c["net"]
            fees = c["total"] - c["slippage"]
            closed.append({"net": c["net"], "fees": fees, "slip": c["slippage"], "gross": c["gross"], "days": (t - h["t"]).days})
            evs.append({"t": "sell", "sym": TRADE[s], "q": h["lots"] * lot_units(TRADE[s]), "px": round(px, 2), "pnl": round(c["net"], 2),
                        "fees": round(fees, 2), "slip": c["slippage"], "days": (t - h["t"]).days, "why": "Monthly rebalance", "entry": round(h["entry"], 2)})
            del book[s]
        if k > 0 and t in R.index:
            bench_v *= 1 + float(R.loc[t].mean())
        nxt = ends[k + 1] if k + 1 < len(ends) else None
        if nxt is not None:                                              # 2. buy this month's picks
            for s, wt in targets(R, C, t, K).items():
                alive = _rows(table, s, t)
                alive = alive[alive["expiry"] > nxt]
                if alive.empty:
                    continue
                ct = alive.iloc[0]
                lots = lots_for(cap, exposure, wt, float(ct["close"]), s)
                if lots < 1:
                    skipped.append(TRADE[s])                             # one lot is bigger than this holding's share of the money
                    continue
                book[s] = {"expiry": ct["expiry"], "entry": float(ct["close"]), "lots": lots, "k": k, "t": t}
                evs.append({"t": "buy", "sym": TRADE[s], "q": lots * lot_units(TRADE[s]), "px": round(float(ct["close"]), 2), "note": f"weight {wt:.0%}"})
        for s in signals:                                                # monthly candle per mini symbol: last month end's close -> this one
            front = _rows(table, s, t)
            front = front[front["expiry"] > t + pd.Timedelta(days=3)]
            c_ = float(front["close"].iloc[0]) if len(front) else None
            o_ = prev_px.get(s, c_)
            out["bars"].setdefault(TRADE[s], []).append([o_, max(o_, c_), min(o_, c_), c_] if c_ is not None else None)
            if c_ is not None:
                prev_px[s] = c_
        out["days"].append(t.strftime("%Y-%m-%d")); out["equity"].append(round(cap)); out["events"].append(evs)
        out["pos"].append([{"sym": TRADE[s], "q": h["lots"] * lot_units(TRADE[s]), "e": round(h["entry"], 2), "px": round(h["entry"], 2), "start": h["k"]}
                           for s, h in book.items()])
        bench.append(round(bench_v))
    if len(out["days"]) < 2 or not closed:
        raise ValueError("No complete month in the chosen dates (trend_carry needs 12 months of history before its first trade, i.e. from 2023)")
    out["bars"] = {k_: v for k_, v in out["bars"].items() if any(v)}
    out["benchmark"] = bench
    out["metrics"] = metrics(out["equity"], capital, closed, len(out["days"]) * 21, len(book)) | {
        "note": f"Monthly strategy: one point per month end; buys and sells happen at month-end closes; holds the best {top_n(len(signals))} of the "
                f"{len(signals)} chosen commodities. Benchmark = the chosen commodities held equally."
                + (f" {len(skipped)} planned holdings were skipped because one lot was bigger than their share of the money"
                   f" ({', '.join(sorted(set(skipped)))}): more starting money or exposure trades them." if skipped else "")}
    out["skipped_symbols"] = []
    # quantities in events / holdings are price units (barrels, kg, ...); the page turns them into lots with this
    out["lots"] = {TRADE[s]: {"q_per_lot": lot_units(TRADE[s]), "per_lot": LOT_SIZE_TEXT[TRADE[s]][0], "unit": LOT_SIZE_TEXT[TRADE[s]][1]} for s in signals}
    return out
