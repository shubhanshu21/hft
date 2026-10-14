"""Paper trading vs the backtest of the same days: did paper take the trades the strategy's own backtest takes?

    python3 -m engine.paper_vs_backtest [--strategy trend_ema_cross] [--since YYYY-MM-DD] [--no-telegram]

Why: a daily strategy trades a few times a month, so a live-runner bug (a missed entry, a wrong stop, an exit on the wrong day) could hide for
months. Each night this re-runs core/backtester over the paper account's own period, with the strategy's live settings, and compares trade by trade:

  * MISSED   the backtest bought a symbol that paper did not buy (within one trading day)
  * EXTRA    paper bought a symbol the backtest did not
  * PRICE    entry or exit more than PRICE_TOL away (paper fills near the open; the backtest at the open plus slippage)
  * STOP     the first stop differs by more than PRICE_TOL
  * EXIT     the position closed on a different day, or is open in one and closed in the other
Share counts are reported but not flagged: paper sizes from the account it shares with other strategies, the backtest from its own.
Only days up to the newest archived daily candle are compared (the nightly top-up writes yesterday's). Exit 1 and a Telegram alert on any flag.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta

import pandas as pd

from core import registry
from core.backtester import run_backtest
from core.paths import ARCHIVE_ROOT, DB_DIR

PRICE_TOL = 0.01
ACCOUNT = "DRYRUN_ACCOUNT"


def paper_positions(db_path, strategy: str, since: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query("SELECT symbol, qty, entry_price, current_stop, status, entry_time, exit_time, exit_price, exit_reason, state "
                               "FROM positions WHERE account_id = ? AND strategy = ? AND entry_time >= ?", con, params=(ACCOUNT, strategy, since))
    finally:
        con.close()
    df["entry_day"] = df["entry_time"].str[:10]
    df["exit_day"] = df["exit_time"].str[:10]
    return df


def first_stop(row) -> float | None:
    """The stop set at entry: positions.current_stop moves with the trail, so prefer the initial stop kept in the state, if any."""
    import json
    try:
        st = json.loads(row["state"] or "{}")
    except ValueError:
        st = {}
    return st.get("initial_stop")


def backtest_trades(strategy, symbols: list[str], since: str, until: str, capital: float) -> list[dict]:
    market = strategy.market
    risk = float(os.environ.get(f"{market.upper()}_{strategy.name.upper()}_RISK_PCT", strategy.default_risk_pct or 2.0))
    leverage = float(os.environ.get(f"{market.upper()}_LEVERAGE", 1.0)) if strategy.uses_leverage else 1.0
    r = run_backtest(strategy, symbols, since, until, capital=capital, slippage_bp=0 if market == "currency" else 5, risk_pct=risk,
                     max_positions=strategy.max_positions or 3, leverage=leverage)
    open_, out = {}, []
    for day, evs in zip(r["days"], r["events"]):
        for e in evs:
            if e["t"] == "buy" and not e.get("add"):
                open_[e["sym"]] = {"symbol": e["sym"], "entry_day": day, "entry_price": e["px"], "qty": e["q"], "stop": e.get("stop"), "exit_day": None, "exit_price": None}
            elif e["t"] == "sell" and e["sym"] in open_:
                t = open_.pop(e["sym"]); t.update(exit_day=day, exit_price=e["px"], exit_reason=e.get("why")); out.append(t)
    return out + list(open_.values())


def trading_days_apart(a: str, b: str) -> int:
    return abs(len(pd.bdate_range(min(a, b), max(a, b))) - 1)


def compare(paper: pd.DataFrame, bt: list[dict], last_day: str) -> list[str]:
    flags, used = [], set()
    for t in bt:
        cand = paper[(paper["symbol"] == t["symbol"])]
        cand = cand[[trading_days_apart(d, t["entry_day"]) <= 1 for d in cand["entry_day"]]] if len(cand) else cand
        if cand.empty:
            flags.append(f"MISSED  {t['symbol']}: the backtest bought on {t['entry_day']} at {t['entry_price']:.2f}; paper did not")
            continue
        p = cand.iloc[0]; used.add(p.name)
        if abs(p["entry_price"] / t["entry_price"] - 1) > PRICE_TOL:
            flags.append(f"PRICE   {t['symbol']}: bought at {p['entry_price']:.2f} in paper vs {t['entry_price']:.2f} in the backtest")
        fs = first_stop(p)
        if fs and t.get("stop") and abs(fs / t["stop"] - 1) > PRICE_TOL:
            flags.append(f"STOP    {t['symbol']}: first stop {fs:.2f} in paper vs {t['stop']:.2f} in the backtest")
        if t["exit_day"] and t["exit_day"] <= last_day:
            if p["status"] == "OPEN":
                flags.append(f"EXIT    {t['symbol']}: the backtest sold on {t['exit_day']} ({t.get('exit_reason')}); paper still holds it")
            elif trading_days_apart(p["exit_day"], t["exit_day"]) > 1:
                flags.append(f"EXIT    {t['symbol']}: sold {p['exit_day']} in paper vs {t['exit_day']} in the backtest")
            elif abs(p["exit_price"] / t["exit_price"] - 1) > PRICE_TOL:
                flags.append(f"PRICE   {t['symbol']}: sold at {p['exit_price']:.2f} in paper vs {t['exit_price']:.2f} in the backtest")
        elif p["status"] != "OPEN" and p["exit_day"] <= last_day:
            flags.append(f"EXIT    {t['symbol']}: paper sold on {p['exit_day']} ({p['exit_reason']}); the backtest still holds it")
    for i, p in paper.iterrows():
        if i not in used and p["entry_day"] <= last_day:
            flags.append(f"EXTRA   {p['symbol']}: paper bought on {p['entry_day']} at {p['entry_price']:.2f}; the backtest did not")
    return flags


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", nargs="*", default=None, help="default: the daily strategies named in EQUITY_STRATEGIES / CURRENCY_STRATEGIES")
    ap.add_argument("--since", default=None, help="default: the paper account's start")
    ap.add_argument("--db", default=str(DB_DIR / "paper_trading.db"))
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    created, capital = con.execute("SELECT created_at, initial_capital FROM accounts WHERE account_id = ?", (ACCOUNT,)).fetchone()
    con.close()
    since = a.since or (pd.Timestamp(created[:10]) + pd.offsets.BDay(1)).strftime("%Y-%m-%d")    # an account made in the evening trades from the next day
    names = a.strategy or [n.strip() for m in ("EQUITY", "CURRENCY") for n in os.environ.get(f"{m}_STRATEGIES", "").split(",") if n.strip()]
    lines, bad = [], False
    for name in names:
        strat = next((s for (m, n), s in registry.discover().items() if n == name), None)
        if strat is None or tuple(strat.timeframe) != ("days", 1):
            continue
        universe = list(getattr(strat, "universe", None) or [])
        if not universe:
            if strat.market == "equity":
                from markets.equity.universe import NIFTY50_SYMBOLS as universe
            else:
                from core.symbols import CURRENCY_SYMBOLS as universe
        symbols = [s for s in universe if strat.trades(s)]
        daily = "equity/{}_daily.csv" if strat.market == "equity" else "currency/{}_daily_adj.csv"
        last_day = max(pd.read_csv(ARCHIVE_ROOT / daily.format(s), usecols=["timestamp"])["timestamp"].iloc[-1][:10] for s in symbols)
        if last_day < since:
            lines.append(f"{name}: nothing to compare yet (paper since {since}, newest archived day {last_day})")
            continue
        bt = backtest_trades(strat, symbols, since, (pd.Timestamp(last_day) + timedelta(days=1)).strftime("%Y-%m-%d"), capital)
        paper = paper_positions(a.db, name, since)
        flags = compare(paper, bt, last_day)
        bad |= bool(flags)
        lines.append(f"{name}: {since} -> {last_day} | backtest trades {len(bt)} | paper trades {len(paper)} | "
                     + ("MATCH" if not flags else f"{len(flags)} difference(s)"))
        lines += ["   " + f for f in flags]
        for t in bt:
            p = paper[paper["symbol"] == t["symbol"]]
            if len(p) and int(p.iloc[0]["qty"]) != int(t["qty"]):
                lines.append(f"   (info) {t['symbol']}: {int(p.iloc[0]['qty'])} shares in paper vs {t['qty']} in the backtest")
    text = "\n".join(lines) or "no daily strategies to compare"
    print("paper vs backtest\n" + text)
    if bad and not a.no_telegram:
        from services.utils import telegram
        telegram.send("🔍 <b>PAPER vs BACKTEST</b> — paper trading took different trades than the backtest:\n" + text)
    return 1 if bad else 0


if __name__ == "__main__":
    import engine.config                                   # noqa: F401  (importing it loads backend/.env into os.environ)
    sys.exit(main())
