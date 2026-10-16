"""Paper trading for equity/breakout_pair (two swing breakout sleeves on the 200 most-traded NSE stocks). Its own Rs1 lakh paper account,
kept apart from the equity/currency one. No real orders: prices come from Upstox quotes, fills are recorded in var/db/paper_trading.db
under account EQUITY_BREAKOUT. Rules and research: markets/equity/strategies/tf_daily/breakout_pair/strategy.py.

    python3 -m engine.equity_breakout             # one trading session, 09:10 -> 15:31 IST (run by hft-equity-breakout.timer)
    python3 -m engine.equity_breakout --signals   # print what it would do today from the last completed day, no trading
    python3 -m engine.equity_breakout --status    # print the account

A session, matching the backtest:
  * before the open: load the nightly daily archive (markets/equity/all_daily.py), bring every holding's trailing stop and day count up to
    the last completed day, list stocks that hit the dead-money rule, and compute the signals of the last completed day;
  * at the open (first quote with an opening price, ~09:15:30): sell dead-money holdings and holdings that opened at or below their stop at
    the open; buy d55 signals at the open (strongest first, while the sleeve has slots and cash, and only if the market was healthy);
  * every minute until 15:30, one batch quote: a holding whose day low reached its stop is sold at the stop (as a resting stop order would
    be); an nr7 candidate whose day high reached its trigger is bought at max(open, trigger) (a resting buy-stop order);
  * after the close: a snapshot and a Telegram summary.
Differences from the backtest: nr7 buys fill in the order their triggers are reached (the backtest takes them strongest first), and a
fill price is the order level, not a 1-minute replay. If the archive is not current (the nightly job failed), no new buys are made that day.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from markets.equity.strategies.tf_daily.breakout_pair import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT = "EQUITY_BREAKOUT"
POLL_S = 60
OPEN_AT, CLOSE_AT = (9, 15, 30), (15, 30, 30)
SLEEVE_OF = {cfg["name"]: k for k, cfg in S.SLEEVES.items()}


def _inr(v: float) -> str:
    from services.utils.telegram import inr
    return ("−" if v < 0 else "") + "₹" + inr(v)


def quotes(keys: dict[str, str]) -> dict[str, dict]:
    """symbol -> {ltp, open, high, low} from one Upstox batch quote (<= 500 keys)."""
    import requests
    from engine.config import _load_access_token
    if not keys:
        return {}
    r = requests.get("https://api.upstox.com/v2/market-quote/quotes", params={"instrument_key": ",".join(keys.values())},
                     headers={"Authorization": f"Bearer {_load_access_token()}", "Accept": "application/json"}, timeout=15)
    r.raise_for_status()
    by_key = {v: k for k, v in keys.items()}
    out = {}
    for v in (r.json().get("data") or {}).values():
        o = v.get("ohlc") or {}
        out[by_key.get(v.get("instrument_token"))] = {"ltp": v.get("last_price"), "open": o.get("open"), "high": o.get("high"), "low": o.get("low")}
    return out


class Session:
    def __init__(self, db, now: datetime, send=None, log=print):
        from services.utils import telegram
        self.db, self.now, self.send, self.log = db, now, send or telegram.send, log
        cap = float(os.environ.get("EQUITY_BREAKOUT_CAPITAL", 100_000))
        db.init_account(ACCOUNT, name="Equity breakout pair (swing)", capital=cap, leverage=1.0, risk_pct=S.RISK_PCT)
        self.acc = db.get_account(ACCOUNT)
        self.lines: list[str] = []

    # ------------------------------------------------------------------ book-keeping
    def positions(self, sleeve: str | None = None) -> list[dict]:
        ps = self.db.get_open_positions(ACCOUNT)
        for p in ps:
            p["st"] = json.loads(p.get("state") or "{}")
        return [p for p in ps if sleeve is None or p["st"].get("sleeve") == sleeve]

    def sleeve_value(self, sleeve: str, prices: dict) -> tuple[float, float]:
        """(equity, cash) of one sleeve: its share of the start capital + its booked profit, minus what its open holdings cost."""
        name = S.SLEEVES[sleeve]["name"]
        with self.db._get_conn() as con:
            booked = con.execute("SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE account_id = ? AND strategy = ?", (ACCOUNT, name)).fetchone()[0]
        cash = float(self.acc["initial_capital"]) * S.SLEEVES[sleeve]["share"] + float(booked)
        held = self.positions(sleeve)
        cash -= sum(p["qty"] * p["entry_price"] for p in held)
        return cash + sum(p["qty"] * (prices.get(p["symbol"]) or p["entry_price"]) for p in held), cash

    def buy(self, sleeve: str, sym: str, key: str, entry: float, atr: float, prices: dict, why: str) -> bool:
        K = S.SLEEVES[sleeve]["K"]
        if len(self.positions(sleeve)) >= K or any(p["symbol"] == sym for p in self.positions()):
            return False
        stop = entry - S.STOP_ATR * atr
        equity, cash = self.sleeve_value(sleeve, prices)
        q = S.size(equity, cash, K, entry, stop)
        if q < 1:
            return False
        pid = f"POS_EQBO_{self.now:%Y%m%d}_{sym}"
        st = {"sleeve": sleeve, "stop": stop, "hi": entry, "entry_day": str(self.now.date()), "n": 0, "done_through": None, "atr_at_entry": atr}
        self.db.open_position(pid, sym, "long", q, entry, stop, 0.0, 0.0, account_id=ACCOUNT, instrument_key=key,
                              strategy=S.SLEEVES[sleeve]["name"], state=json.dumps(st))
        msg = (f"🟢 <b>BREAKOUT BUY</b> {sym} ({why})\n{q} shares @ ₹{entry:,.2f} = {_inr(q * entry)}\n"
               f"stop ₹{stop:,.2f} (risk {_inr(q * (entry - stop))}) · sleeve {sleeve}, {len(self.positions(sleeve))}/{K} slots")
        self.lines.append(f"BUY {sym} {q} @ {entry:,.2f} ({why})"); self.send(msg); self.log(msg)
        return True

    def sell(self, p: dict, price: float, reason: str) -> None:
        c = S.costs(p["entry_price"], price, p["qty"], p["symbol"])
        capital = float(self.db.get_account(ACCOUNT)["current_capital"]) + c["net"]
        self.db.close_position(p["position_id"], price, reason, c["gross"], c["net"], c["total"])
        hold = (self.now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        self.db.record_trade(p["position_id"], p["symbol"], "long", p["qty"], p["entry_price"], price, p["entry_time"], self.now.isoformat(), hold,
                             reason, c["gross"], c, c["net"], capital, account_id=ACCOUNT, strategy=p["strategy"])
        self.db.update_capital(ACCOUNT, capital)
        msg = (f"{'✅' if c['net'] >= 0 else '🔻'} <b>BREAKOUT SELL</b> {p['symbol']} ({reason.replace('_', ' ')})\n"
               f"{p['qty']} shares ₹{p['entry_price']:,.2f} → ₹{price:,.2f}: <b>{'+' if c['net'] >= 0 else ''}{_inr(c['net'])}</b> after {_inr(c['total'])} fees")
        self.lines.append(f"SELL {p['symbol']} {reason} {'+' if c['net'] >= 0 else ''}{c['net']:,.0f}"); self.send(msg); self.log(msg)

    # ------------------------------------------------------------------ before the open
    def prepare(self, ind: dict) -> dict:
        """Bring holdings up to the last completed day; return today's plan."""
        C, H, L, A = ind["C"], ind["H"], ind["L"], ind["atr"]
        last = C.index[-1]
        dead = []
        for p in self.positions():
            st, s = p["st"], p["symbol"]
            if s not in C.columns:
                continue
            ref = st.get("ref")                                  # a split while held: the archive rescales history -> rescale the holding
            if ref and pd.Timestamp(ref[0]) in C.index and C.at[pd.Timestamp(ref[0]), s] > 0:
                f = C.at[pd.Timestamp(ref[0]), s] / ref[1]
                if abs(f - 1) > 0.02:
                    with self.db._get_conn() as con:
                        con.execute("UPDATE positions SET qty = ?, entry_price = ? WHERE position_id = ?", (round(p["qty"] / f), p["entry_price"] * f, p["position_id"]))
                    st["stop"] *= f; st["hi"] *= f
                    self.send(f"⚠️ BREAKOUT: {s} split/bonus detected, holding rescaled by {f:.3f}")
            days = C.index[(C.index >= pd.Timestamp(st["entry_day"])) & (C.index <= last)]
            if st.get("done_through"):
                days = days[days > pd.Timestamp(st["done_through"])]
            for d in days:
                if not C.at[d, s] == C.at[d, s]:
                    continue
                st["n"] += 1
                st["hi"] = max(st["hi"], float(H.at[d, s]))
                st["stop"] = max(st["stop"], st["hi"] - S.TRAIL_ATR * float(A.at[d, s]))
                if st["n"] >= S.DEAD_DAYS and C.at[d, s] < p["entry_price"]:
                    st["dead"] = True
                st["done_through"] = str(d.date())
            st["ref"] = [str(last.date()), float(C.at[last, s])]
            self.db.update_position_stop(p["position_id"], st["stop"], st["hi"], False, state_updates={k: v for k, v in st.items()})
            if st.get("dead"):
                dead.append(p["symbol"])
        cand = S.candidates(ind, last)
        atr = {s: float(A.at[last, s]) for s in set(cand["d55"]) | {x for x, _ in cand["nr7"]}}
        return {"last": last, "dead": dead, "cand": cand, "atr": atr}


def expected_last_day(today: date) -> date:
    from engine.nightly import last_trading_day
    from services.utils.market_holidays import get_trading_holidays
    return last_trading_day(today, get_trading_holidays())


def at(now: datetime, hms) -> datetime:
    return now.replace(hour=hms[0], minute=hms[1], second=hms[2], microsecond=0)


def session(db, send=None, log=print, sleep=time.sleep, clock=lambda: datetime.now(IST)) -> None:
    from markets.equity.all_daily import instruments
    ses = Session(db, clock(), send=send, log=log)
    ind = S.indicators(S.load())
    plan = ses.prepare(ind)
    fresh = plan["last"].date() >= expected_last_day(clock().date())
    keys_all = instruments()
    cand = plan["cand"]
    buying = fresh and cand["healthy"]
    log(f"last day {plan['last'].date()} (fresh={fresh}), breadth {cand['breadth']:.1f}%, d55 {cand['d55'][:8]}, nr7 {[s for s, _ in cand['nr7']][:8]}, dead {plan['dead']}")
    if not fresh:
        ses.send(f"⚠️ BREAKOUT: price archive ends {plan['last'].date()}, expected {expected_last_day(clock().date())} -- no new buys today, stops still watched")
    pending_nr7 = dict(cand["nr7"]) if buying else {}
    d55 = list(cand["d55"]) if buying else []
    opened = False
    while clock() < at(clock(), CLOSE_AT):
        ses.now = clock()
        if ses.now < at(ses.now, OPEN_AT):
            sleep(min(30, (at(ses.now, OPEN_AT) - ses.now).total_seconds())); continue
        held = ses.positions()
        watch = {p["symbol"] for p in held} | set(pending_nr7) | (set(d55) if not opened else set())
        keys = {s: keys_all[s] for s in watch if s in keys_all}
        try:
            q = quotes(keys)
        except Exception as exc:
            log(f"quote failed: {exc!s:.150}"); sleep(POLL_S); continue
        prices = {s: v["ltp"] for s, v in q.items() if v.get("ltp")}
        if not opened:
            if any(not (q.get(s) or {}).get("open") for s in keys) and ses.now < at(ses.now, (9, 20, 0)):
                sleep(10); continue                                  # opening prices not published yet
            opened = True
            for p in held:                                           # dead money and gap-through stops sell at the open
                op = (q.get(p["symbol"]) or {}).get("open")
                if not op:
                    continue
                if p["symbol"] in plan["dead"]:
                    ses.sell(p, op, "dead_money")
                elif op <= p["current_stop"]:
                    ses.sell(p, op, "stop")
            for s in d55:                                            # d55: buy at the open, strongest first
                op = (q.get(s) or {}).get("open")
                if op and plan["atr"].get(s, 0) > 0:
                    ses.buy("d55", s, keys_all[s], op, plan["atr"][s], prices, "55-day high")
        for p in ses.positions():                                    # resting stop orders
            v = q.get(p["symbol"]) or {}
            if v.get("low") and v["low"] <= p["current_stop"]:
                ses.sell(p, p["current_stop"] if (v.get("open") or 0) > p["current_stop"] else v["open"], "stop")
        for s, trig in list(pending_nr7.items()):                    # resting buy-stop orders
            v = q.get(s) or {}
            if len(ses.positions("nr7")) >= S.SLEEVES["nr7"]["K"]:
                break
            if v.get("high") and v["high"] >= trig:
                pending_nr7.pop(s)
                ses.buy("nr7", s, keys_all[s], max(v.get("open") or trig, trig), plan["atr"][s], prices, "quiet-day breakout")
        sleep(POLL_S)
    # after the close
    ses.now = clock()
    held = ses.positions()
    try:
        q = quotes({p["symbol"]: keys_all[p["symbol"]] for p in held if p["symbol"] in keys_all})
    except Exception:
        q = {}
    db.record_snapshot(ACCOUNT)
    acc = db.get_account(ACCOUNT)
    open_pnl = sum(((q.get(p["symbol"]) or {}).get("ltp") or p["entry_price"]) * p["qty"] - p["entry_price"] * p["qty"] for p in held)
    rows = [f"• {p['symbol']} {p['qty']} @ ₹{p['entry_price']:,.2f} → ₹{((q.get(p['symbol']) or {}).get('ltp') or p['entry_price']):,.2f}, stop ₹{p['current_stop']:,.2f}"
            for p in held]
    why = ("market healthy" if cand["healthy"] else f"market not healthy ({cand['breadth']:.0f}% of the 200 stocks above their 50-day average, needs {S.MIN_BREADTH_PCT:.0f}%): no new buys")
    ses.send(f"📈 <b>EQUITY BREAKOUT</b> ({ses.now:%d %b})\nAccount: <b>{_inr(acc['current_capital'] + open_pnl)}</b> (booked {_inr(acc['current_capital'])}, "
             f"open {'+' if open_pnl >= 0 else ''}{_inr(open_pnl)})\n{why}\n" + ("\n".join(ses.lines) + "\n" if ses.lines else "No trades today.\n")
             + ("\n".join(rows) if rows else "No open positions."))


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    from engine.database import TradingDB
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--signals", action="store_true", help="print today's plan from the last completed day; no trading")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    db = TradingDB()
    if a.status:
        print(db.get_account(ACCOUNT), *db.get_open_positions(ACCOUNT), sep="\n")
        return 0
    if a.signals:
        ind = S.indicators(S.load())
        c = S.candidates(ind, ind["C"].index[-1])
        print(f"last completed day {ind['C'].index[-1].date()}: breadth {c['breadth']:.1f}% (needs {S.MIN_BREADTH_PCT:.0f}) -> {'BUYING' if c['healthy'] else 'no new buys'}")
        print("d55 (buy at the open):", c["d55"]); print("nr7 (buy-stop at):", c["nr7"])
        return 0
    now = datetime.now(IST)
    from services.utils.market_holidays import get_trading_holidays
    if now.weekday() >= 5 or now.date() in get_trading_holidays():
        print("not an NSE trading day")
        return 0
    try:
        from services.auth.upstox_auto_login import ensure_fresh_upstox_token
        ensure_fresh_upstox_token()
    except Exception as exc:
        print(f"token refresh failed: {exc}")
    session(db, send=(print if a.no_telegram else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
