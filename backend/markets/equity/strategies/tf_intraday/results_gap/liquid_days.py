"""The fair backtest universe for results_gap (2026-10-29): every results-day gap-up in ANY liquid NSE stock, not just the 414 with
5-minute history. Those 414 are today's liquid names -- picking them is hindsight, and the same rule lost money on results-day gap-ups
outside them. One row per (stock, results day) with a gap vs the market >= 6.5%, the 09:15-09:30 and 15:20 prices the rule needs, and
turn20 = the stock's median daily turnover over the 20 trading days BEFORE that day (from var/archive/equity_all), so the backtest can
apply the same >= Rs10 cr liquidity test the live engine uses.

    rows(extra_bars)   build the table: the 414 from their 5-minute archive, other stocks from `extra_bars` -- {(symbol, day): 5-minute
                       DataFrame with timestamp/open/high/low/close/volume}, fetched from Upstox's history API (one request per day)
    load()             the saved table (var/archive/nse/results_gap_liquid_days.pkl), or None

Built once from the 2026-10-29 research (391 Upstox requests); the 414's rows come from day_summary(), so they keep up with the archive.
"""
from __future__ import annotations

import pandas as pd

from core.paths import ARCHIVE_ROOT

PATH = ARCHIVE_ROOT / "nse" / "results_gap_liquid_days_v2.pkl"      # v2 (2026-10-29): + v_pre / moved_pre (09:15-09:30 only, no look-ahead)
COLS = ["sym", "day", "xgap", "o915", "c925", "o930", "h930", "l930", "v930", "c1520", "turn20", "v_pre", "moved_pre"]


def turnover20(sym: str, day: str) -> float | None:
    """Median daily turnover (Rs) over the 20 trading days before `day`, from the all-NSE daily archive."""
    p = ARCHIVE_ROOT / "equity_all" / f"{sym}_daily.csv.gz"
    if not p.exists():
        return None
    d = pd.read_csv(p, usecols=["timestamp", "close", "volume"])
    d["day"] = d["timestamp"].str[:10]
    t = (d["close"] * d["volume"]).rolling(20).median().shift(1)
    hit = t[d["day"] == day]
    return float(hit.iloc[0]) if len(hit) and pd.notna(hit.iloc[0]) else None


def _summarise_extra(bars: pd.DataFrame) -> dict | None:
    b = bars.assign(hm=bars["timestamp"].str[11:16]).set_index("hm")
    if not {"09:15", "09:25", "09:30", "15:20"} <= set(b.index):
        return None
    r = b.loc["09:30"]
    pre = b.loc[[h for h in ("09:15", "09:20", "09:25") if h in b.index]]
    return {"v_pre": float((pre["close"] * pre["volume"]).sum()), "moved_pre": bool(pre["high"].max() > pre["low"].min()),
            "o915": float(b.at["09:15", "open"]), "c925": float(b.at["09:25", "close"]), "o930": float(r["open"]), "h930": float(r["high"]),
            "l930": float(r["low"]), "v930": float(r["open"] * r["volume"]), "c1520": float(b.at["15:20", "close"])}


def rows(extra_bars: dict, extra_gaps: dict, min_gap: float = 0.065) -> pd.DataFrame:
    """extra_gaps: {(symbol, day): gap vs the market (the 414's median, as in day_summary)} for the stocks outside the 414."""
    from markets.equity.strategies.tf_intraday.results_gap import strategy as S
    x = S.day_summary().copy()
    x = x[x["prev_close"].notna() & (x["o915"] > 0)]
    x["gap"] = x["o915"] / x["prev_close"] - 1
    x = x[x["gap"].abs() < 0.25]
    x["xgap"] = x["gap"] - x.groupby("day")["gap"].transform("median")
    days = sorted(x["day"].unique())
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    res = S._results_days()
    hit = set(res) | {(s, nxt[d]) for s, d in res if d in nxt}
    old = x[(x["xgap"] >= min_gap) & [(s, d) in hit for s, d in zip(x["sym"], x["day"])]].copy()
    old["turn20"] = [turnover20(s, d) for s, d in zip(old["sym"], old["day"])]
    new = []
    for (s, d), b in extra_bars.items():
        if b is None or len(b) == 0 or (s, d) not in extra_gaps or extra_gaps[(s, d)] < min_gap:
            continue
        r = _summarise_extra(b)
        if r:
            new.append({"sym": s, "day": d, "xgap": extra_gaps[(s, d)], **r, "turn20": turnover20(s, d)})
    out = pd.concat([old[COLS], pd.DataFrame(new, columns=COLS)], ignore_index=True)
    return out.drop_duplicates(["sym", "day"]).sort_values(["day", "sym"]).reset_index(drop=True)


def save(df: pd.DataFrame) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(PATH)


def load() -> pd.DataFrame | None:
    return pd.read_pickle(PATH) if PATH.exists() else None
