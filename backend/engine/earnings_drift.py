"""Paper trader for equity/earnings_drift (2026-10-29), inside the RESULTS_GAP account next to results_gap. Paper only -- no real orders.

Driven by engine/results_gap.py's timer runs (no timer of its own):
  * 08:50  plan():  from last night's all-NSE daily archive and results dates, list the holdings due to be sold (40 trading days held) and
                    the stocks to buy (strong results reaction, healthy market). Saved to the state file. In real trading these orders go
                    into NSE's 09:00-09:08 pre-open auction and fill at the official open price -- the price the backtest uses.
  * 09:30  fill():  books those sells and buys at today's official open (the quote's today_open), sells first, BEFORE results_gap sizes its
                    intraday trades from the cash left over. 09:40 retries what had no price; buys still unfilled after 09:45 are dropped,
                    sells are retried at every run until they go through.
Money (since 2026-11-22, user: "Drift grows + momentum 50/50"): ACCOUNT_SHARE of the account's booked money (all of it while the momentum
sleeve, engine/momentum.py, is switched off), 1/K per stock, never more than the account's free cash. Before, it was SLEEVE_CAPITAL plus its
own closed trades, so results-gap profits sat as idle cash: joint backtest Rs1L Jan 2023-Nov 2026 Rs11.40L -> Rs15.65L with drift alone.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime, timedelta, timezone


from core.atomic_io import read_json, write_json
from services.utils import telegram as T
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


ACCOUNT_SHARE = 0.5               # of the account's booked money; momentum (engine/momentum.py) sizes from 75% (since 2026-11-23), drift buys first


def account_locked(db) -> float:
    """Cash ALL swing holdings in the account take out (earnings drift + momentum, bought outright); results gap's own trades excluded."""
    return sum(float(p["qty"]) * float(p["entry_price"]) for p in db.get_open_positions(ACCOUNT) if p.get("strategy") != "results_gap")


def sleeve_value(db) -> float:
    """The strategy's money (2026-11-22): its share of the account's booked money -- so results-gap profits and top-ups get invested too."""
    from core import switches
    return (ACCOUNT_SHARE if switches.on("RG_MOMENTUM") else 1.0) * float(db.get_account(ACCOUNT)["current_capital"])


def _entry_day(p: dict) -> date:
    try:
        return date.fromisoformat(json.loads(p.get("state") or "{}")["entry_date"])
    except (ValueError, KeyError, TypeError):
        return date.fromisoformat(str(p["entry_time"])[:10])


def plan(db, today: date, holidays: set[date]) -> list[str]:
    """08:50: what to sell and buy at today's open. Returns the message lines."""
    sell = {p["position_id"]: "held_40_days" for p in _mine(db) if S.trading_days_held(_entry_day(p), today, holidays) >= S.HOLD_DAYS}
    lines, buy, cands = [], [], []
    ind = _indicators()
    last = ind["C"].index[-1].date() if len(ind["C"].index) else None
    want = today - timedelta(days=1)
    while want.weekday() >= 5 or want in holidays:
        want -= timedelta(days=1)
    if last is None or last < want:
        lines.append(f"⚠️ Earnings drift: price data is not up to date (it ends {last}, needs {want}), so no new buys today")
    else:
        sig = S.signals(ind)
        held = {p["symbol"] for p in _mine(db)}
        # Usually just `want`. A weekend session (Diwali's Sunday Muhurat, 2026-11-08) adds its own day: the engine does not run on it, so
        # the signals of the day before it (bought at that session's open in the backtest) and of the session itself are both bought now.
        days = [d for d in ind["C"].index if d.date() >= want]
        cands = list(dict.fromkeys(s for d in days for s in S.candidates(ind, sig, d)))
        slots = S.K - len(held) + len(sell)
        # every candidate, strongest first: fill() buys down the list until the slots are full, so one it must skip (a share dearer
        # than 2 slots) is replaced by the next -- as the backtest does (2026-11-18; the slot used to stay empty)
        buy = [s for s in cands if s not in held] if slots > 0 else []
        from core import switches
        if not switches.on("EARNINGS_DRIFT"):                    # 2026-11-19: switched off in .env -- planned sells still happen
            if buy:
                lines.append(f"Earnings drift is switched off (STRATEGY_EARNINGS_DRIFT=off in backend/.env): not buying {', '.join(buy[:slots])}")
            buy = []
        if float(ind["breadth"].iloc[-1]) < S.MIN_BREADTH_PCT:
            lines.append(f"Earnings drift: no buys -- the market is weak ({float(ind['breadth'].iloc[-1]):.0f}% of the 200 most-traded stocks are above their 50-day average, needs 50%)")
        for s in buy[:slots]:
            r = sig[(sig.sym == s) & sig.signal_day.isin(days)].iloc[0]
            lines.append(f"BUY {s} at the open -- results {r.meeting:%d %b} beat the market by {r.reaction:+.1%} on {r.surge:.1f}x the usual volume")
        if buy[slots:]:
            lines.append(f"next in line if one is skipped: {', '.join(buy[slots:])}")
    for pid in sell:
        lines.append(f"SELL {db.get_position(pid)['symbol']} at the open -- held {S.HOLD_DAYS} trading days, as planned")
    breadth = round(float(ind["breadth"].iloc[-1]), 1) if len(ind["breadth"]) else None
    write_json(STATE, {"day": str(today), "sell": sell, "buy": buy, "breadth": breadth, "fresh": not (last is None or last < want),
                       "last_day": str(last), "candidates": cands[:20] if last is not None and last >= want else []})   # evidence if paper and backtest disagree
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
        lines.append(f"⚠️ Earnings drift: no opening price for {', '.join(buys)} by {BUY_UNTIL:%H:%M}, so not bought today")
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
        hold = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        db.record_trade(pid, p["symbol"], "long", qty, entry, x, p["entry_time"], now.isoformat(), hold, why, gross, c, net, cap,
                        account_id=ACCOUNT, strategy=S.NAME, close=True)
        lines.append("SOLD " + T.sold_block(p["symbol"], f"{qty} shares", entry, x, net, c["total"], f"swing trade, {T.why_text(why)}"))
        sells.pop(pid)
    held = {p["symbol"] for p in _mine(db)}
    for s in list(buys):
        if len(_mine(db)) >= S.K:                  # slots full: the rest were only reserves
            buys = []; break
        if s in held:
            buys.remove(s); continue
        if s not in px:
            continue
        free, slot = float(db.get_account(ACCOUNT)["current_capital"]) - account_locked(db), sleeve_value(db) / S.K
        qty = S.share_qty(px[s], slot, free)
        buys.remove(s)
        if qty < 1:
            why = (f"one share ({_inr(px[s])}) costs more than {S.ONE_SHARE_SLOTS} of its 1/{S.K} slots ({_inr(slot)} each)" if free >= px[s]
                   else f"not enough free cash ({_inr(free)})")
            lines.append(f"Earnings drift: {s} skipped -- {why}")
            continue
        db.open_position(f"POS_ED_{now:%Y%m%d}_{s}", s, "long", qty, px[s], 0.0, 0.0, px[s], account_id=ACCOUNT, instrument_key=keys[s],
                         strategy=S.NAME, state=json.dumps({"entry_date": str(now.date()), "fill": "official open (pre-open auction)"}))
        lines.append("BOUGHT " + T.bought_block(s, f"{qty} shares", px[s], qty * px[s], f"results beat the market · swing trade, sold after {S.HOLD_DAYS} trading days"))
    write_json(STATE, {**st, "sell": sells, "buy": buys})
