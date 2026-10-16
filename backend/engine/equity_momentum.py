"""Paper trading for equity/momentum_portfolio (15 stocks, quarterly rebalance). Its own Rs1 lakh paper account (EQUITY_MOMENTUM), kept
apart from the other accounts. No real orders: fills at live Upstox prices, recorded in var/db/paper_trading.db.
Rules and research: markets/equity/strategies/tf_quarterly/momentum_portfolio/strategy.py.

    python3 -m engine.equity_momentum            # 09:20 IST: build / rebalance if due, at the live price; 15:40 IST: daily snapshot
    python3 -m engine.equity_momentum --plan     # print the current ranking and what a rebalance would do; no trading
    python3 -m engine.equity_momentum --status   # print the account

Run by hft-equity-momentum.timer (Mon-Fri 09:20 and 15:40 IST). A rebalance is due when the account is empty (first build) or a calendar
quarter has ended since the last one; it uses the ranking of the last completed day (the quarter's last trading day, from the nightly archive)
and trades at the live price shortly after the open (the backtest uses the open). If the archive is not current, the rebalance waits a day.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core.paths import DB_DIR
from markets.equity.strategies.tf_quarterly.momentum_portfolio import strategy as M

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT, NAME = "EQUITY_MOMENTUM", "momentum_portfolio"
STATE = DB_DIR / "equity_momentum_state.json"


def _inr(v: float) -> str:
    from services.utils.telegram import inr
    return ("−" if v < 0 else "") + "₹" + inr(v)


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def due(st: dict, held: int, last_day: pd.Timestamp, qends: list) -> pd.Timestamp | None:
    """The signal day a rebalance should use now, or None. First build: the last completed day. Then: each new quarter end."""
    if not held and not st.get("rebalanced_for"):
        return last_day
    done = pd.Timestamp(st.get("rebalanced_for") or "1900-01-01")
    new = [q for q in qends if q > done]
    return new[-1] if new else None


def run(db, now: datetime, send=None, log=print, rebalance: bool = True) -> dict:
    from engine.equity_breakout import expected_last_day, quotes
    from markets.equity.all_daily import instruments
    from markets.equity.strategies.tf_daily.breakout_pair import strategy as BP
    from services.utils import telegram
    send = send or telegram.send
    cap = float(os.environ.get("EQUITY_MOMENTUM_CAPITAL", 100_000))
    db.init_account(ACCOUNT, name="Equity momentum (15 stocks, quarterly)", capital=cap, leverage=1.0, risk_pct=0.0)
    st = _state(); lines = []
    held = db.get_open_positions(ACCOUNT)
    keys_all = instruments()
    did = None
    if rebalance:
        ind = BP.indicators(BP.load())
        last = ind["C"].index[-1]
        sig_day = due(st, len(held), last, M.quarter_ends(ind["C"].index))
        fresh = last.date() >= expected_last_day(now.date())
        if sig_day is not None and not fresh:
            send(f"⚠️ MOMENTUM: price archive ends {last.date()}, expected {expected_last_day(now.date())} -- rebalance postponed to tomorrow")
        elif sig_day is not None:
            row = M.scores(ind).loc[sig_day]
            keep, sell, buy = M.plan(row, [p["symbol"] for p in held])
            q = quotes({s: keys_all[s] for s in set(sell) | set(buy) | {p["symbol"] for p in held} if s in keys_all})
            px = {s: v.get("ltp") for s, v in q.items() if v.get("ltp")}
            capital = float(db.get_account(ACCOUNT)["current_capital"])
            for p in held:
                if p["symbol"] in sell:
                    x = px.get(p["symbol"])
                    if not x:
                        lines.append(f"• {p['symbol']}: no live price, kept"); keep.append(p["symbol"]); continue
                    c = BP.costs(p["entry_price"], x, p["qty"], p["symbol"])
                    capital += c["net"]
                    db.close_position(p["position_id"], x, "quarterly_rebalance", c["gross"], c["net"], c["total"])
                    hold_min = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
                    db.record_trade(p["position_id"], p["symbol"], "long", p["qty"], p["entry_price"], x, p["entry_time"], now.isoformat(), hold_min,
                                    "quarterly_rebalance", c["gross"], c, c["net"], capital, account_id=ACCOUNT, strategy=NAME)
                    lines.append(f"• SOLD {p['symbol']} {p['qty']} @ ₹{x:,.2f}: {'+' if c['net'] >= 0 else ''}{_inr(c['net'])} after fees")
            db.update_capital(ACCOUNT, capital)
            kept = [p for p in db.get_open_positions(ACCOUNT)]
            invested = sum(p["qty"] * p["entry_price"] for p in kept)
            value = capital + sum(p["qty"] * ((px.get(p["symbol"]) or p["entry_price"]) - p["entry_price"]) for p in kept)
            cash = capital - invested
            for s in buy[: M.N_HOLD - len(kept)]:
                x = px.get(s)
                if not x or s not in keys_all:
                    lines.append(f"• {s}: no live price, skipped"); continue
                qty = int(np.floor(min(value / M.N_HOLD, cash) / x))
                if qty < 1:
                    lines.append(f"• {s}: one share (₹{x:,.0f}) is more than its slot, skipped"); continue
                cash -= qty * x
                db.open_position(f"POS_EQMOM_{now:%Y%m%d}_{s}", s, "long", qty, x, 0.0, 0.0, 0.0, account_id=ACCOUNT, instrument_key=keys_all[s],
                                 strategy=NAME, state=json.dumps({"signal_day": str(sig_day.date()), "rank": int(row.rank(ascending=False)[s])}))
                lines.append(f"• BOUGHT {s} {qty} @ ₹{x:,.2f} = {_inr(qty * x)} (rank {int(row.rank(ascending=False)[s])})")
            st["rebalanced_for"] = str(sig_day.date()); STATE.write_text(json.dumps(st, indent=1))
            did = "rebalance"
            log(f"rebalance for {sig_day.date()}: keep {keep}, sell {sell}, buy {buy}")
    # mark to market
    held = db.get_open_positions(ACCOUNT)
    try:
        q = quotes({p["symbol"]: keys_all[p["symbol"]] for p in held if p["symbol"] in keys_all})
    except Exception:
        q = {}
    acc = db.get_account(ACCOUNT)
    open_pnl = sum((((q.get(p["symbol"]) or {}).get("ltp") or p["entry_price"]) - p["entry_price"]) * p["qty"] for p in held)
    value = float(acc["current_capital"]) + open_pnl
    db.record_snapshot(ACCOUNT)
    if did:
        send(f"🚀 <b>EQUITY MOMENTUM: {'first build' if not st.get('built_before') else 'quarterly rebalance'}</b>\n" + "\n".join(lines)
             + f"\n\nAccount value: <b>{_inr(value)}</b> (started {_inr(float(acc['initial_capital']))}) · {len(held)} stocks")
        st["built_before"] = True; STATE.write_text(json.dumps(st, indent=1))
    return {"did": did, "value": value, "lines": lines}


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    from engine.database import TradingDB
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--snapshot", action="store_true", help="mark to market and record the daily snapshot only (15:40 run)")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    db = TradingDB()
    if a.status:
        print(db.get_account(ACCOUNT), *db.get_open_positions(ACCOUNT), sep="\n")
        return 0
    if a.plan:
        from markets.equity.strategies.tf_daily.breakout_pair import strategy as BP
        ind = BP.indicators(BP.load()); d = ind["C"].index[-1]
        keep, sell, buy = M.plan(M.scores(ind).loc[d], [p["symbol"] for p in db.get_open_positions(ACCOUNT)])
        print(f"ranking of {d.date()}: keep {keep}\nsell {sell}\nbuy {buy}\nnext rebalance due: {due(_state(), len(keep) + len(sell), d, M.quarter_ends(ind['C'].index))}")
        return 0
    now = datetime.now(IST)
    from services.utils.market_holidays import get_trading_holidays
    if now.weekday() >= 5 or now.date() in get_trading_holidays():
        print("not an NSE trading day")
        return 0
    out = run(db, now, send=(print if a.no_telegram else None), rebalance=not a.snapshot and now.hour < 15)   # the 15:40 run: snapshot only
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
