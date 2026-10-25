"""commodity / trend_carry -- MCX trend + carry: holds the best 3 of 8 commodities, re-checked every 2 weeks (10 trading days).

It picks the commodities that have risen most over the last year AND whose next contract is cheapest.

Research: Fuertes, Miffre & Rallis (2010), "Tactical allocation in commodity futures markets: combining momentum and term structure signals"
(momentum alone ~9%/yr, the combination best). Data: real MCX daily prices of every expiry, Dec 2021 -> now (markets/commodity/futures_eod.py).

Rules (8 liquid commodities):
  * trend = return over the last 252 trading days (~12 months) of holding the nearest contract, switched to the next one 3 days before expiry;
  * carry = log(near / next contract price) per year (positive = the next contract is cheaper = backwardation = buyers earn the roll);
  * score = rank(trend) + rank(carry), ties to the stronger trend, then the name; hold the top 3, buy-only (selling short tested worse);
  * size each holding by 1 / its 252-day volatility, whole lots of the MINI contracts rounded to the nearest lot (lots_for);
  * re-check every 10 trading days: a holding still in the top 3 with the same lots is KEPT (no needless fees); others are sold / bought.

2026-10-25 (the user: "check different timeframes i need profits" -> "do what is best"): the same rules were run day by day on real MCX
prices at Rs2.5L, 1.5x exposure (scratchpad mcx_freq.py), dev 2023-24 / test 2025-26 / worst fall measured daily:
  monthly (the old rule)  Rs2.78L / Rs5.18L, worst -40%       every 2 weeks  Rs3.18L / Rs5.39L, worst -34%  <- chosen: better in BOTH
  weekly                  Rs2.84L / Rs5.35L, worst -43%       daily          Rs2.70L / Rs4.50L, worst -50% (fees x3)
6- and 3-month trends looked better in 2025-26 only and worse in 2023-24 (a one-phase fit), so the 12-month trend stays. Faster than
monthly does NOT create big profits: most of every version's gain is the 2025-26 commodity boom; 2023-24 was weak for all.
Exposure is the real risk control: at 1x the worst month was ~-13%; at 2-3x -25% to -44%; at full Upstox leverage (~8x) June 2026 lost ~85%.

This is not a core.strategy.Strategy (it ranks 8 commodities together and picks expiries), so it has its own paper engine
(engine/mcx_trend_carry.py) and its own backtest (backtest() below, also used by the web backtest page).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from markets.commodity.costs import LOT_SIZE_TEXT, compute_mcx_costs, lot_units

SIGNALS = ("GOLDM", "SILVERM", "CRUDEOIL", "NATURALGAS", "COPPER", "ZINC", "ALUMINIUM", "LEAD")
TRADE = {"GOLDM": "GOLDTEN", "SILVERM": "SILVERMIC", "CRUDEOIL": "CRUDEOILM", "NATURALGAS": "NATGASMINI", "COPPER": "COPPER",
         "ZINC": "ZINCMINI", "ALUMINIUM": "ALUMINI", "LEAD": "LEADMINI"}
TOP_N = 3
LOOKBACK = 252                 # trading days of trend (~12 months)
VOL_WINDOW = 252               # trading days of volatility for the 1/vol weights
REBALANCE_DAYS = 10            # re-check every 10 trading days (~2 weeks)
ROLL_DAYS = 3                  # switch to the next contract this many days before expiry
MARGIN_PCT = {"GOLDM": 0.093, "SILVERM": 0.126, "CRUDEOIL": 0.35, "NATURALGAS": 0.30, "COPPER": 0.10, "ZINC": 0.092, "ALUMINIUM": 0.093, "LEAD": 0.10}
MAX_MARGIN_USE = 0.8           # never block more than 80% of the account as margin
EXPOSURE_MIN = 0.25
EXPOSURE_MAX = round(1 / min(MARGIN_PCT.values()), 1)      # the most Upstox lets any of them be geared (gold ~9.3% margin -> ~10.8x)
SIGNAL_OF = {v: k for k, v in TRADE.items()}                 # mini contract -> the signal contract it follows


def top_n(n_symbols: int) -> int:
    """3 holdings out of 8; with fewer commodities chosen, at most half of them (so there is still a ranking), at least 1."""
    return max(1, min(TOP_N, n_symbols // 2))


def _by_day(table: pd.DataFrame, signals=SIGNALS) -> dict:
    t = table[table["symbol"].isin(signals) & (table["close"] > 0)]
    return {(s, d): g.sort_values("expiry") for (s, d), g in t.groupby(["symbol", "date"])}


def front(by: dict, s: str, d, k: int = 0):
    """The k-th contract of commodity s alive more than ROLL_DAYS after day d (k=0: the one to hold)."""
    g = by.get((s, d))
    if g is None:
        return None
    g = g[g["expiry"] > d + pd.Timedelta(days=ROLL_DAYS)]
    return g.iloc[k] if len(g) > k else None


def daily_panel(table: pd.DataFrame, signals=SIGNALS, by: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """R: each commodity's daily return of holding the same nearest contract (a roll day counts the old contract's move); C: carry each day."""
    by = by if by is not None else _by_day(table, signals)
    days = sorted(table.loc[table["symbol"].isin(signals), "date"].unique())
    R, C = {}, {}
    for s in signals:
        r, c, prev = {}, {}, None
        for d in days:
            f = front(by, s, d)
            if f is None:
                prev = None
                continue
            if prev is not None:
                g = by.get((s, d))
                same = g[g["expiry"] == prev[0]] if g is not None else []
                if len(same):
                    r[d] = float(same["close"].iloc[0]) / prev[1] - 1
            g = by.get((s, d))
            g = g[(g["expiry"] > d + pd.Timedelta(days=ROLL_DAYS)) & (g["volume"] > 0)]
            if len(g) >= 2:
                span = max((g["expiry"].iloc[1] - g["expiry"].iloc[0]).days, 1)
                c[d] = np.log(g["close"].iloc[0] / g["close"].iloc[1]) * 365 / span
            prev = (f["expiry"], float(f["close"]))
        R[s], C[s] = pd.Series(r, dtype=float), pd.Series(c, dtype=float)
    return pd.DataFrame(R).reindex(days), pd.DataFrame(C).reindex(days).ffill(limit=5)


def targets(R: pd.DataFrame, C: pd.DataFrame, t: pd.Timestamp) -> dict[str, float]:
    """{signal symbol: weight} decided on day t (weights sum to 1; empty without ~a year of history).

    2026-10-23: skewness and basis-momentum were tested as extra ranks and rejected on a fair re-test. Ties go to the stronger trend,
    then the name, so the result never depends on list order (before that fix the website and the engine could disagree)."""
    if t not in R.index or t not in C.index:
        return {}
    mom = ((1 + R.fillna(0)).rolling(LOOKBACK, min_periods=int(LOOKBACK * 0.8)).apply(np.prod, raw=True) - 1).loc[t]
    score = (mom.rank() + C.loc[t].rank()).dropna()
    n = top_n(len(R.columns))
    if len(score) < 2 * n:
        return {}
    order = pd.DataFrame({"score": score, "mom": mom.reindex(score.index), "name": score.index})
    picks = list(order.sort_values(["score", "mom", "name"], ascending=[False, False, True]).index[:n])
    vol = R.rolling(VOL_WINDOW, min_periods=120).std().loc[t]
    inv = {s: 1 / vol[s] for s in picks if vol.get(s, 0) > 0}
    tot = sum(inv.values())
    return {s: v / tot for s, v in inv.items()} if tot else {s: 1 / len(picks) for s in picks}


def rebalance_due(last: str | None, days: list, today: pd.Timestamp) -> bool:
    """True when REBALANCE_DAYS trading days (days in the price table, up to today) have passed since the last re-check `last`."""
    if not last:
        return True
    last = pd.Timestamp(last)
    return sum(1 for d in days if last < pd.Timestamp(d) <= today) >= REBALANCE_DAYS


MAX_LOT_X = 2.0                # a rounded-up holding may be at most 2x the account in contract value


def lots_for(capital: float, exposure: float, weight: float, price: float, signal: str) -> int:
    """Whole lots of the mini contract: the notional share (capital x exposure x weight) ROUNDED to the nearest lot (was: rounded down).

    2026-10-22: rounding down left a Rs2.5L account (the user's real size) in cash whenever a pick's share was under one lot. Rounding to
    the nearest lot (a share of >= half a lot buys one), capped at MAX_LOT_X x the account per holding and at the margin limit. Never fewer
    lots than the old round-down rule."""
    lot_val = price * lot_units(TRADE[signal])
    by_notional = capital * exposure * weight // lot_val
    by_margin = capital * MAX_MARGIN_USE * weight // (lot_val * MARGIN_PCT[signal])
    floor_lots = int(max(0, min(by_notional, by_margin)))
    lots = int(capital * exposure * weight / lot_val + 0.5)
    while lots > 0 and (lots * lot_val > MAX_LOT_X * capital or lots * lot_val * MARGIN_PCT[signal] > MAX_MARGIN_USE * capital):
        lots -= 1
    return max(lots, floor_lots)


def backtest(table: pd.DataFrame, capital: float = 250_000, start: str = "2023-01-01", end: str = "2100-01-01", exposure: float = 1.5,
             symbols=None, panel: tuple | None = None) -> dict:
    """Day-by-day backtest in the web backtester's replay shape (days, equity, events, pos, bars, benchmark, metrics).

    Every trading day: roll a holding 3 days before expiry (same lots, next contract), mark the account to the close. Every REBALANCE_DAYS
    trading days: rank, then keep / resize / sell / buy at that day's close. Costs (markets/commodity/costs.py, incl. a half-spread) are
    charged on every lot closed. Prices are the signal contract's close (minis track it per unit)."""
    from core.backtester import metrics
    signals = tuple(SIGNAL_OF.get(x, x) for x in symbols) if symbols else SIGNALS     # the mini contracts chosen on the page -> their signals
    by = _by_day(table, signals)
    R, C = panel if panel is not None else daily_panel(table, signals, by)
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    days = [d for d in R.index if t0 <= d <= t1]
    if len(days) < 2:
        raise ValueError("No trading days in the chosen dates (MCX daily data starts December 2021; the first trade needs ~a year of history)")

    def px(s, d, exp):
        g = by.get((s, d))
        g = g[g["expiry"] == exp] if g is not None else None
        return float(g["close"].iloc[0]) if g is not None and len(g) else None

    cash = float(capital)
    pos: dict[str, dict] = {}                  # signal -> {expiry, lots, entry, k}
    closed, skipped = [], []
    out = {"days": [], "equity": [], "events": [], "pos": [], "bars": {TRADE[s]: [] for s in signals}}
    bench, bench_v, last_reb = [], float(capital), None

    def close(s, d, k, p, why, evs):
        nonlocal cash
        h = pos[s]
        c = compute_mcx_costs(TRADE[s], "long", h["entry"], p, k)
        cash += c["net"]
        held = (d - h["t"]).days
        closed.append({"net": c["net"], "fees": c["total"] - c["slippage"], "slip": c["slippage"], "gross": c["gross"], "days": held})
        evs.append({"t": "sell", "sym": TRADE[s], "q": k * lot_units(TRADE[s]), "px": round(p, 2), "pnl": round(c["net"], 2),
                    "fees": round(c["total"] - c["slippage"], 2), "slip": c["slippage"], "days": held, "why": why, "entry": round(h["entry"], 2)})
        h["lots"] -= k
        if h["lots"] <= 0:
            del pos[s]

    def buy(s, d, k_idx, lots, p, exp, note, evs):
        if s in pos:
            h = pos[s]
            h["entry"] = (h["entry"] * h["lots"] + p * lots) / (h["lots"] + lots)
            h["lots"] += lots
        else:
            pos[s] = {"expiry": exp, "lots": lots, "entry": p, "k": k_idx, "t": d}
        evs.append({"t": "buy", "sym": TRADE[s], "q": lots * lot_units(TRADE[s]), "px": round(p, 2), "note": note})

    for k, d in enumerate(days):
        evs = []
        for s in list(pos):                                          # roll before expiry, same lots into the next contract
            h = pos[s]
            if h["expiry"] <= d + pd.Timedelta(days=ROLL_DAYS):
                lots = h["lots"]
                close(s, d, lots, px(s, d, h["expiry"]) or h["entry"], "Roll before expiry", evs)
                f = front(by, s, d)
                if f is not None:
                    buy(s, d, k, lots, float(f["close"]), f["expiry"], "next contract (roll)", evs)
        val = cash + sum(((px(s, d, h["expiry"]) or h["entry"]) - h["entry"]) * h["lots"] * lot_units(TRADE[s]) for s, h in pos.items())
        if rebalance_due(last_reb, days, d):
            w = targets(R, C, d)
            if w:
                last_reb = d
                for s in list(pos):
                    if s not in w:
                        close(s, d, pos[s]["lots"], px(s, d, pos[s]["expiry"]) or pos[s]["entry"], "Rebalance: dropped from the top 3", evs)
                for s, wt in w.items():
                    f = front(by, s, d)
                    if f is None:
                        continue
                    p = float(f["close"])
                    want = lots_for(val, exposure, wt, p, s)
                    if want < 1:
                        skipped.append(TRADE[s])
                    have = pos[s]["lots"] if s in pos else 0
                    if s in pos and pos[s]["expiry"] != f["expiry"]:
                        close(s, d, have, px(s, d, pos[s]["expiry"]) or pos[s]["entry"], "Rebalance: next contract", evs)
                        have = 0
                    if want < have:
                        close(s, d, have - want, p, "Rebalance: smaller share", evs)
                    elif want > have:
                        buy(s, d, k, want - have, p, f["expiry"], f"weight {wt:.0%}", evs)
        if k > 0:
            bench_v *= 1 + float(R.loc[d, list(signals)].fillna(0).mean())
        for s in signals:
            f = front(by, s, d)
            out["bars"][TRADE[s]].append([float(f["open"]), float(f["high"]), float(f["low"]), float(f["close"])] if f is not None else None)
        out["days"].append(d.strftime("%Y-%m-%d")); out["equity"].append(round(val)); out["events"].append(evs)
        out["pos"].append([{"sym": TRADE[s], "q": h["lots"] * lot_units(TRADE[s]), "e": round(h["entry"], 2),
                            "px": round(px(s, d, h["expiry"]) or h["entry"], 2), "start": h["k"]} for s, h in pos.items()])
        bench.append(round(bench_v))
    if not closed and not pos:
        raise ValueError("No trade in the chosen dates (trend_carry needs about a year of history before its first trade, i.e. from 2023)")
    out["bars"] = {k_: v for k_, v in out["bars"].items() if any(v)}
    out["benchmark"] = bench
    open_now = sum(((px(s, pd.Timestamp(days[-1]), h["expiry"]) or h["entry"]) - h["entry"]) * h["lots"] * lot_units(TRADE[s]) for s, h in pos.items())
    out["metrics"] = metrics(out["equity"], capital, closed, len(days), len(pos)) | {
        "note": f"Re-checked every {REBALANCE_DAYS} trading days (~2 weeks); holds the best {top_n(len(signals))} of the {len(signals)} chosen commodities, "
                f"keeping a holding that stays in the top {top_n(len(signals))}. Benchmark = the chosen commodities held equally."
                + (f" Some planned holdings were skipped because one lot was too big for their share of the money ({', '.join(sorted(set(skipped)))})."
                   if skipped else "")}
    out["metrics"]["open_profit"] = round(open_now)
    out["skipped_symbols"] = []
    # quantities in events / holdings are price units (barrels, kg, ...); the page turns them into lots with this
    out["lots"] = {TRADE[s]: {"q_per_lot": lot_units(TRADE[s]), "per_lot": LOT_SIZE_TEXT[TRADE[s]][0], "unit": LOT_SIZE_TEXT[TRADE[s]][1]} for s in signals}
    return out
