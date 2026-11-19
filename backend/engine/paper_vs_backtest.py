"""Paper vs backtest (2026-11-18): does each paper account trade the way its backtest says it should?

    python3 -m engine.paper_vs_backtest              # run every account, save var/cache/paper_vs_backtest.json, print the report
    python3 -m engine.paper_vs_backtest --telegram   # same, and send recent differences to Telegram (what the nightly job does)

For each account, the strategy's own website backtest (engine/backtest_api.run_strategy, the same code as the Backtest page) is re-run
over exactly the days the account has existed (since its created_at / last reset), with the account's money and the live settings.
Then every buy is matched by stock and day:
  matched        -- both bought it (the fill-price gap is reported, in %)
  only_backtest  -- the backtest bought it, paper did not (a missed trade, a skipped slot, a data gap...)
  only_paper     -- paper bought it, the backtest did not (a rule or data difference, or a trade from before a settings change)
The expected account value by day goes to the account page's chart next to the real one. Telegram only hears about differences from
the last RECENT_DAYS days, so an old, explained one (e.g. DOT/ADA bought before they were dropped on 2026-11-10) does not repeat nightly.
RESULTS_GAP runs two strategies on one Rs1L: each is backtested on its own money and their profits are added (an approximation -- in
the account they share the cash).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

from core.atomic_io import read_json, write_json
from core.paths import CACHE_DIR

OUT = CACHE_DIR / "paper_vs_backtest.json"
RECENT_DAYS = 5


def bodies(account: str, capital: float) -> list[dict]:
    """The website backtest(s) that ARE this account, with its live settings (validated by backtest_api.validate)."""
    if account == "CRYPTO_DIP_REVERSAL":
        from markets.crypto.strategies.tf_5min.dip_reversal import strategy as dr
        return [{"market": "crypto", "strategy": "dip_reversal", "capital": capital, "exposure": 3.0, "symbols": list(dr.COINS),
                 "tax": "business"}]
    if account == "MCX_TREND_CARRY":
        return [{"market": "commodity", "strategy": "trend_carry", "capital": capital,
                 "exposure": float(os.environ.get("MCX_TREND_CARRY_EXPOSURE", 1.5))}]   # validate reads "exposure" (a "leverage" key was ignored)
    if account == "RESULTS_GAP":
        from markets.equity.strategies.tf_daily.earnings_drift import strategy as ed
        from markets.equity.strategies.tf_intraday.results_gap import strategy as rg
        return [{"market": "equity", "strategy": "results_gap", "capital": capital, "exposure": rg.LEVERAGE},
                {"market": "equity", "strategy": "earnings_drift", "capital": ed.SLEEVE_CAPITAL, "slippage_bp": 5}]
    if account == "EQUITY_BREAKOUT":
        return [{"market": "equity", "strategy": "breakout_pair", "capital": capital, "risk_pct": 2, "slippage_bp": 5}]
    return []


def backtest_buys(result: dict) -> list[dict]:
    days, events = result.get("days") or [], result.get("events") or []
    return [{"sym": str(e["sym"]), "day": str(d)[:10], "px": e.get("px")} for d, evs in zip(days, events) for e in (evs or []) if e.get("t") == "buy"]


def daily_equity(result: dict) -> dict[str, float]:
    """Last value of each calendar day (crypto results are 5-minute)."""
    return {str(d)[:10]: float(v) for d, v in zip(result.get("days") or [], result.get("equity") or [])}


def paper_buys(con, account: str) -> list[dict]:
    out = []
    for sym, entry_time, px, state in con.execute("SELECT symbol, entry_time, entry_price, state FROM positions WHERE account_id = ?", (account,)):
        try:
            fam = json.loads(state or "{}").get("family")
        except ValueError:
            fam = None
        out.append({"sym": fam or sym, "day": str(entry_time)[:10], "px": px})   # MCX holds a contract; the backtest names its family
    return out


def match(paper: list[dict], bt: list[dict], tolerance_days: int = 0) -> dict:
    """Pair buys by stock and day (crypto: +-1 day, its 5-minute days cross midnight between IST and UTC)."""
    left = list(bt)
    matched, only_paper = [], []
    for p in sorted(paper, key=lambda x: x["day"]):
        pd_ = date.fromisoformat(p["day"])
        hit = next((b for b in left if b["sym"] == p["sym"] and abs((date.fromisoformat(b["day"]) - pd_).days) <= tolerance_days), None)
        if hit:
            left.remove(hit)
            gap = round((p["px"] / hit["px"] - 1) * 100, 2) if p.get("px") and hit.get("px") else None
            matched.append({"sym": p["sym"], "day": p["day"], "paper_px": p["px"], "backtest_px": hit["px"], "gap_pct": gap})
        else:
            only_paper.append(p)
    return {"matched": matched, "only_paper": only_paper, "only_backtest": sorted(left, key=lambda x: x["day"])}


def check_account(con, account: str, today: date, run=None) -> dict:
    from engine import backtest_api as api
    run = run or (lambda body: api.run_strategy(*(lambda v: (v["market"], v["strategy"], v["params"]))(api.validate(body))))
    row = con.execute("SELECT initial_capital, created_at FROM accounts WHERE account_id = ?", (account,)).fetchone()
    if not row:
        return {"account": account, "error": "no account yet"}
    capital, start = float(row[0]), str(row[1])[:10]
    if start >= str(today):
        return {"account": account, "start": start, "note": "started today -- nothing to compare yet"}
    buys, curves = [], []
    from core.backtester import BacktestError
    for body in bodies(account, capital):
        try:
            res = run(body | {"start": start, "end": str(today)})
        except BacktestError as e:
            if "No trading days" in str(e):                             # a brand-new account: nothing to compare yet
                return {"account": account, "start": start, "note": "too few trading days yet to compare"}
            if "No trade in the chosen dates" not in str(e):
                raise
            # 2026-11-19: MCX at 1x buys nothing since 24 Oct (Rs2.5L is below most lots) -- the backtest's answer is "no trades,
            # money unchanged", not an error; paper's own buys then show as differences
            res = {"days": [start, str(today)], "equity": [float(body["capital"])] * 2, "events": [[], []]}
        buys += backtest_buys(res)
        curves.append((float(body["capital"]), daily_equity(res)))
    expected: dict[str, float] = {}
    for d in sorted({d for _, c in curves for d in c}):                     # each strategy's profit added to the account's money
        total = capital
        for cap, c in curves:
            last = max((k for k in c if k <= d), default=None)
            total += (c[last] - cap) if last else 0.0
        expected[d] = round(total, 2)
    m = match(paper_buys(con, account), buys, 1 if account.startswith("CRYPTO") else 0)
    gaps = [x["gap_pct"] for x in m["matched"] if x["gap_pct"] is not None]
    return {"account": account, "start": start, "end": str(today), "expected": expected, **m,
            "avg_fill_gap_pct": round(sum(gaps) / len(gaps), 2) if gaps else None}


def recent_differences(r: dict, today: date) -> list[str]:
    since = str(today - timedelta(days=RECENT_DAYS))
    out = [f"{r['account']}: paper bought {x['sym']} on {x['day']}, the backtest did not" for x in r.get("only_paper", []) if x["day"] >= since]
    out += [f"{r['account']}: the backtest bought {x['sym']} on {x['day']}, paper did not" for x in r.get("only_backtest", []) if x["day"] >= since]
    return out


def run_all(db_path=None, today: date | None = None, run=None, accounts=None) -> dict:
    from engine import backtest_api as api
    from engine.database import TradingDB
    today = today or datetime.now(api.IST).date()
    con = sqlite3.connect(str(db_path or TradingDB().db_path), timeout=30)
    try:
        out = {}
        for account, _ in (accounts or api.PAPER_ACCOUNTS):
            try:
                out[account] = check_account(con, account, today, run)
            except Exception as e:                                     # one account failing must not hide the others
                out[account] = {"account": account, "error": f"{type(e).__name__}: {e}"}
    finally:
        con.close()
    return {"as_of": datetime.now(api.IST).isoformat(timespec="seconds"), "accounts": out}


def report(res: dict) -> list[str]:
    lines = []
    for r in res["accounts"].values():
        if "error" in r or "note" in r:
            lines.append(f"{r['account']}: {r.get('error') or r.get('note')}"); continue
        exp = list(r["expected"].values())
        lines.append(f"{r['account']} since {r['start']}: backtest value {exp[-1]:,.0f} | buys matched {len(r['matched'])}, only paper {len(r['only_paper'])}, "
                     f"only backtest {len(r['only_backtest'])}" + (f", avg fill gap {r['avg_fill_gap_pct']:+.2f}%" if r["avg_fill_gap_pct"] is not None else ""))
    return lines


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    ap = argparse.ArgumentParser()
    ap.add_argument("--telegram", action="store_true", help="send the last few days' differences to Telegram")
    a = ap.parse_args(argv)
    res = run_all()
    diffs = [d for r in res["accounts"].values() for d in recent_differences(r, date.fromisoformat(res["as_of"][:10]))]
    prev = read_json(OUT) or {}
    already = prev.get("sent_day") == res["as_of"][:10] and prev.get("sent") == diffs      # the nightly job runs twice: tell once
    res.update(sent_day=prev.get("sent_day"), sent=prev.get("sent"))
    if a.telegram and diffs and not already:
        from services.utils import telegram
        telegram.send(telegram.message("🔍", "CHECK · Paper vs backtest", ["These trades differ from what the strategy's backtest did "
                                                                              "in the last few days -- open the account page for why:",
                                                                              "\n".join(f"• {d}" for d in diffs[:15])]))
        res.update(sent_day=res["as_of"][:10], sent=diffs)
    write_json(OUT, res)
    print("\n".join(report(res)))
    print("\n".join(diffs) if diffs else "no recent differences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
