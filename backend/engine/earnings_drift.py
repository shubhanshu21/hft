"""Paper trader for equity/earnings_drift (2026-10-29), inside the RESULTS_GAP account next to results_gap. Paper only -- no real orders.

Driven by engine/results_gap.py's timer runs (no timer of its own):
  * 08:50  plan():  from last night's all-NSE daily archive and results dates, list the holdings due to be sold (40 trading days held) and
                    the stocks to buy (strong results reaction, healthy market). Saved to the state file. In real trading these orders go
                    into NSE's 09:00-09:08 pre-open auction and fill at the official open price -- the price the backtest uses.
  * 09:30  fill():  books those sells and buys at today's official open (the quote's today_open), sells first, BEFORE results_gap sizes its
                    intraday trades from the cash left over. 09:40 retries what had no price; buys still unfilled after 09:45 are dropped,
                    sells are retried at every run until they go through.
Money: the strategy's own SLEEVE_CAPITAL plus its closed trades, 1/K per stock, never more than the account's free cash.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta, timezone

import pandas as pd

from core.atomic_io import read_json, write_json
from core.paths import DB_DIR
from markets.equity.strategies.tf_daily.earnings_drift import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT = "RESULTS_GAP"
STATE = DB_DIR / "earnings_drift_state.json"
BUY_UNTIL = dtime(9, 45)


def _inr(v: float) -> str:
    return f"Rs{v:,.0f}"


def _indicators() -> dict:
    return S.indicators(S.load())


def _mine(db) -> list[dict]:
    return [p for p in db.get_open_positions(ACCOUNT) if p.get("strategy") == S.NAME]


def locked_cash(db) -> float:
    """Cash these delivery holdings take out of the account (bought outright, no margin)."""
    return sum(float(p["qty"]) * float(p["entry_price"]) for p in _mine(db))


def sleeve_value(db) -> float:
    """The strategy's money: SLEEVE_CAPITAL plus everything its closed trades made or lost."""
    return S.SLEEVE_CAPITAL + sum(float(t["net_pnl"] or 0) for t in db.get_trades(limit=100_000, account_id=ACCOUNT) if t.get("strategy") == S.NAME)


def _entry_day(p: dict) -> date:
    try:
        return date.fromisoformat(json.loads(p.get("state") or "{}")["entry_date"])
    except (ValueError, KeyError, TypeError):
        return date.fromisoformat(str(p["entry_time"])[:10])


def plan(db, today: date, holidays: set[date]) -> list[str]:
    """08:50: what to sell and buy at today's open. Returns the message lines."""
    sell = {p["position_id"]: "held_40_days" for p in _mine(db) if S.trading_days_held(_entry_day(p), today, holidays) >= S.HOLD_DAYS}
    lines, buy = [], []
    ind = _indicators()
    last = ind["C"].index[-1].date() if len(ind["C"].index) else None
    want = today - timedelta(days=1)
    while want.weekday() >= 5 or want in holidays:
        want -= timedelta(days=1)
    if last != want:
        lines.append(f"earnings drift: daily data not up to date (last day {last}, needs {want}) -- no new buys today")
    else:
        sig = S.signals(ind)
        held = {p["symbol"] for p in _mine(db)}
        cands = S.candidates(ind, sig, pd.Timestamp(last))
        slots = S.K - len(held) + len(sell)
        buy = [s for s in cands if s not in held][:max(0, slots)]
        if float(ind["breadth"].iloc[-1]) < S.MIN_BREADTH_PCT:
            lines.append(f"earnings drift: market not healthy ({float(ind['breadth'].iloc[-1]):.0f}% of the 200 stocks above their 50-day average) -- no buys")
        for s in buy:
            r = sig[(sig.sym == s) & (sig.signal_day == pd.Timestamp(last))].iloc[0]
            lines.append(f"earnings drift: BUY {s} at the open (results {r.meeting:%d %b}: {r.reaction:+.1%} vs the market on {r.surge:.1f}x volume)")
    for pid in sell:
        lines.append(f"earnings drift: SELL {db.get_position(pid)['symbol']} at the open (held {S.HOLD_DAYS} trading days)")
    write_json(STATE, {"day": str(today), "sell": sell, "buy": buy})
    return lines


def _opens(broker, keys: dict[str, str]) -> dict[str, float]:
    if not keys:
        return {}
    raw = broker.get_quote_batch(list(keys.values())) or {}
    return {s: float(q["today_open"]) for s, k in keys.items() if (q := raw.get(k)) and q.get("today_open")}


def fill(db, broker, now: datetime, lookup, lines: list) -> None:
    """09:30 and later: book the planned sells and buys at today's official open."""
    st = read_json(STATE) or {}
    if st.get("day") != str(now.date()) or not (st.get("sell") or st.get("buy")):
        return
    sells = {pid: why for pid, why in st["sell"].items() if (p := db.get_position(pid)) and p.get("status") == "OPEN"}
    buys = list(st.get("buy") or [])
    if now.time() > BUY_UNTIL and buys:
        lines.append(f"earnings drift: no opening price for {', '.join(buys)} by {BUY_UNTIL:%H:%M} -- not bought today")
        buys = []
    keys = {db.get_position(pid)["symbol"]: db.get_position(pid)["instrument_key"] for pid in sells}
    keys.update({s: lookup(s) for s in buys if lookup(s)})
    px = _opens(broker, keys)
    for pid, why in list(sells.items()):
        p = db.get_position(pid)
        if p["symbol"] not in px:
            continue
        x, qty, entry = px[p["symbol"]], int(p["qty"]), float(p["entry_price"])
        c = S.delivery_costs(entry, x, qty)
        gross = (x - entry) * qty; net = gross - c["total"]
        cap = float(db.get_account(ACCOUNT)["current_capital"]) + net
        db.close_position(pid, x, why, gross, net, c["total"])
        hold = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        db.record_trade(pid, p["symbol"], "long", qty, entry, x, p["entry_time"], now.isoformat(), hold, why, gross, c, net, cap,
                        account_id=ACCOUNT, strategy=S.NAME)
        lines.append(f"earnings drift: SOLD {p['symbol']} {qty} {entry:,.2f} -> {x:,.2f} at the open: {'+' if net >= 0 else ''}{_inr(net)} after fees")
        sells.pop(pid)
    held = {p["symbol"] for p in _mine(db)}
    for s in list(buys):
        if s in held:
            buys.remove(s); continue
        if s not in px:
            continue
        free = float(db.get_account(ACCOUNT)["current_capital"]) - locked_cash(db)
        qty = int(min(sleeve_value(db) / S.K, free) // px[s])
        buys.remove(s)
        if qty < 1:
            lines.append(f"earnings drift: {s} skipped -- not enough free cash ({_inr(free)})")
            continue
        db.open_position(f"POS_ED_{now:%Y%m%d}_{s}", s, "long", qty, px[s], 0.0, 0.0, px[s], account_id=ACCOUNT, instrument_key=keys[s],
                         strategy=S.NAME, state=json.dumps({"entry_date": str(now.date()), "fill": "official open (pre-open auction)"}))
        lines.append(f"earnings drift: BOUGHT {s} {qty} @ {px[s]:,.2f} (today's open) = {_inr(qty * px[s])}, sell after {S.HOLD_DAYS} trading days")
    write_json(STATE, {**st, "sell": sells, "buy": buys})
