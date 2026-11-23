"""Paper trader for equity/momentum_12_1 inside the ONE equity account (RESULTS_GAP), 2026-11-22 (user: "Drift grows + momentum 50/50").
Paper only -- no real orders.

Driven by engine/results_gap.py's timer runs, like engine/earnings_drift.py (no timer of its own):
  * 08:50  plan():  first session of a month (or the very first run): rank the 300 most-traded NSE stocks by 12-1 momentum on the last
                    completed day; holdings that left the top N are sold, the new ones bought, all at the open (NSE's pre-open auction
                    -> the official open price, as the backtest). Other sessions: buy any of the month's list still missing (cash freed up).
                    A stock earnings drift already holds is skipped (one holding per stock per account) and the next one taken.
  * 09:30  fill():  books those orders at today's official open, after earnings drift and before results gap sizes its intraday trades.
Money: SHARE of the account's booked money, 1/N per stock, whole shares, never more than the account's free cash (overnight = cash only;
results gap trades 3x the whole account intraday against these pledged holdings). No stop, no time limit.
Joint backtest (scratch joint.py / joint50.py), Rs1L Jan 2023-Nov 2026, results gap 3x + earnings drift 50% + momentum 50%, each sleeve
growing with the account: Rs17.13L / worst -28.0% (with Rs50k-sized sleeves), against Rs11.40L / -23.0% for results gap + drift alone.
2026-11-23 (user's yes): SHARE 0.5 -> 0.75 after the first live refresh bought only 10 of 20 (Rs1,791 slots) -- see SHARE.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta

from core import switches
from core.atomic_io import read_json, write_json
from core.paths import DB_DIR
from engine import earnings_drift as ED
from markets.equity.strategies.tf_daily.earnings_drift.strategy import delivery_costs
from markets.equity.strategies.tf_daily.momentum_12_1 import strategy as M
from services.utils import telegram as T

ACCOUNT = "RESULTS_GAP"
STATE = DB_DIR / "momentum_state.json"
SHARE = 0.75                      # of the booked money (2026-11-23, user's yes; was 0.5): drift keeps its half, so the two add to 125% --
                                  # idle cash gets used; free cash caps every buy and drift buys first (scratch shared_cash.py:
                                  # Rs71.6k 2023-Nov 2026 Rs11.1L / -26% -> Rs13.2L / -29%; whole account Rs14.5L / -30% starved drift)
BUY_UNTIL = dtime(9, 45)


def _mine(db) -> list[dict]:
    return [p for p in db.get_open_positions(ACCOUNT) if p.get("strategy") == M.NAME]


def sleeve_value(db) -> float:
    return SHARE * float(db.get_account(ACCOUNT)["current_capital"])


def _latest_scores():
    """(last completed day, its 12-1 momentum scores) from the nightly all-NSE daily archive."""
    sc = M.scores(M.load())
    return sc.index[-1], sc.iloc[-1]


def plan(db, today: date, holidays: set[date]) -> list[str]:
    """08:50: what to sell and buy at today's open. Returns the message lines."""
    st = read_json(STATE) or {}
    last, row = _latest_scores()
    want = today - timedelta(days=1)
    while want.weekday() >= 5 or want in holidays:
        want -= timedelta(days=1)
    mine = {p["symbol"]: p["position_id"] for p in _mine(db)}
    if last.date() < want:
        write_json(STATE, {**st, "day": str(today), "sell": {}, "buy": [], "fresh": False, "last_day": str(last.date())})
        return [f"⚠️ Momentum: price data is not up to date (it ends {last.date()}, needs {want}), so no orders today"]
    other = {p["symbol"] for p in db.get_open_positions(ACCOUNT) if p.get("strategy") != M.NAME}
    due = M.rebalance_due(st.get("month"), today)
    if due:
        targets, month = M.targets(row, M.N, exclude=other), f"{today:%Y-%m}"
        sell = {pid: "momentum_out" for s, pid in mine.items() if s not in targets}
    else:
        targets, month, sell = list(st.get("targets") or []), st["month"], {}
    buy = [s for s in targets if s not in mine and s not in other]
    lines = []
    if not switches.on("RG_MOMENTUM"):                            # switched off: no buys; the monthly refresh still sells
        if buy:
            lines.append(f"Momentum is switched off (STRATEGY_RG_MOMENTUM=off in backend/.env): not buying {', '.join(buy)}")
        buy = []
    if due:
        lines.append(f"Momentum: monthly refresh -- top {M.N} by 12-month return: {', '.join(targets)}")
    for pid in sell:
        lines.append(f"SELL {db.get_position(pid)['symbol']} at the open -- left the top {M.N} (monthly check)")
    if buy:
        lines.append(f"BUY at the open (momentum, 1/{M.N} of {SHARE:.0%} of the account each): {', '.join(buy)}")
    write_json(STATE, {"day": str(today), "month": month, "targets": targets, "sell": sell, "buy": buy, "due": due, "fresh": True,
                       "last_day": str(last.date())})
    return lines


def fill(db, broker, now: datetime, lookup, lines: list) -> None:
    """09:30 and later: book the planned sells and buys at today's official open."""
    st = read_json(STATE) or {}
    if st.get("day") != str(now.date()) or not (st.get("sell") or st.get("buy")):
        return
    sells = {pid: why for pid, why in st["sell"].items() if (p := db.get_position(pid)) and p.get("status") == "OPEN"}
    buys = list(st.get("buy") or [])
    if now.time() > BUY_UNTIL and buys:
        lines.append(f"⚠️ Momentum: no opening price for {', '.join(buys)} by {BUY_UNTIL:%H:%M}, so not bought today")
        buys = []
    keys = {db.get_position(pid)["symbol"]: db.get_position(pid)["instrument_key"] for pid in sells}
    keys.update({s: k for s in buys if (k := lookup(s))})
    px = ED._opens(broker, keys)
    for pid, why in list(sells.items()):
        p = db.get_position(pid)
        if p["symbol"] not in px:
            continue
        x, qty, entry = px[p["symbol"]], int(p["qty"]), float(p["entry_price"])
        c = delivery_costs(entry, x, qty)
        gross = (x - entry) * qty; net = gross - c["total"]
        cap = float(db.get_account(ACCOUNT)["current_capital"]) + net
        hold = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        db.record_trade(pid, p["symbol"], "long", qty, entry, x, p["entry_time"], now.isoformat(), hold, why, gross, c, net, cap,
                        account_id=ACCOUNT, strategy=M.NAME, close=True)
        lines.append("SOLD " + T.sold_block(p["symbol"], f"{qty} shares", entry, x, net, c["total"], f"momentum, {T.why_text(why)}"))
        sells.pop(pid)
    held = {p["symbol"] for p in db.get_open_positions(ACCOUNT)}
    for s in list(buys):
        if s in held:
            buys.remove(s); continue
        if s not in px:
            continue
        free = float(db.get_account(ACCOUNT)["current_capital"]) - ED.account_locked(db)
        slot = sleeve_value(db) / M.N
        qty = int(min(slot, free) // px[s])
        buys.remove(s)
        if qty < 1:
            lines.append(f"Momentum: {s} skipped -- " + (f"one share (Rs{px[s]:,.0f}) costs more than its slot (Rs{slot:,.0f})" if free >= px[s]
                                                         else f"not enough free cash (Rs{free:,.0f})"))
            continue
        db.open_position(f"POS_MO_{now:%Y%m%d}_{s}", s, "long", qty, px[s], 0.0, 0.0, px[s], account_id=ACCOUNT, instrument_key=keys[s],
                         strategy=M.NAME, state=json.dumps({"entry_date": str(now.date()), "fill": "official open (pre-open auction)"}))
        held.add(s)
        lines.append("BOUGHT " + T.bought_block(s, f"{qty} shares", px[s], qty * px[s], f"top {M.N} by 12-month momentum · kept while it stays in the top {M.N}"))
    write_json(STATE, {**st, "sell": sells, "buy": buys})
