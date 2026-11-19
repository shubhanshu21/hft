"""Paper trading for equity/breakout_pair (two swing breakout sleeves on the 200 most-traded NSE stocks). Its own Rs1 lakh paper account,
kept apart from RESULTS_GAP (sharing that Rs1L made both worse, 2026-10-29). Re-added 2026-11-17 (user's yes). No real orders: prices come from Upstox quotes, fills are recorded in var/db/paper_trading.db
under account EQUITY_BREAKOUT. Rules and research: markets/equity/strategies/tf_daily/breakout_pair/strategy.py.

    python3 -m engine.equity_breakout             # one trading session, 09:08 -> 15:31 IST (run by hft-equity-breakout.timer)
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
Since 2026-11-19 (user: "inside Breakout, 50/50") HALF the account is equity/momentum_12_1: at the open of the first session of each month
the holdings that left the top 20 by 12-month momentum are sold and the new top 20 bought, each 1/20 of that half (no stop, no time limit,
no market filter; fresh data needed). The two breakout sleeves have 25% each. Every buy is also capped by the account's real free cash.
Differences from the backtest: nr7 buys fill in the order their triggers are reached (the backtest takes them strongest first), and a
fill price is the order level, not a 1-minute replay. If the archive is not current (the nightly job failed), no new buys are made that day.
The account lock (engine/account_reset.py) is held only around each step that writes, never for the whole six-hour session, so the hourly
backup and a reset from the paper page are never blocked for long.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from core.atomic_io import read_json, write_json
from services.utils import telegram as T
from core.paths import DB_DIR
from markets.equity.strategies.tf_daily.breakout_pair import strategy as S
from markets.equity.strategies.tf_daily.momentum_12_1 import strategy as M

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT = "EQUITY_BREAKOUT"
STATE = DB_DIR / "equity_breakout_state.json"          # today's plan, for the account page's "Today" box (2026-11-18)
MOM_STATE = DB_DIR / "equity_breakout_momentum.json"   # {"month": "YYYY-MM"} of the last momentum refresh
BREAKOUT_SHARE = 0.5                                     # 2026-11-19: breakout sleeves 25% + 25%, momentum 50% (backtest Rs2.49L vs Rs2.17L)
SHARE = {k: cfg["share"] * BREAKOUT_SHARE for k, cfg in S.SLEEVES.items()} | {"mom": 1 - BREAKOUT_SHARE}
NAME = {k: cfg["name"] for k, cfg in S.SLEEVES.items()} | {"mom": M.NAME}
POLL_S = 60
OPEN_AT, CLOSE_AT = (9, 15, 30), (15, 30, 30)
SLEEVE_OF = {cfg["name"]: k for k, cfg in S.SLEEVES.items()}


def _inr(v: float) -> str:
    from services.utils.telegram import inr
    return ("−" if v < 0 else "") + "₹" + inr(v)


def quotes(broker, keys: dict[str, str]) -> dict[str, dict]:
    """symbol -> {ltp, open, high, low} from the broker's batch full quote (the same call results_gap / earnings_drift use)."""
    if not keys:
        return {}
    raw = broker.get_quote_batch(list(keys.values())) or {}
    return {s: {"ltp": q.get("ltp"), "open": q.get("today_open"), "high": q.get("today_high"), "low": q.get("today_low")}
            for s, k in keys.items() if (q := raw.get(k))}


def init_account(db) -> None:
    """Create the account on first use (Rs1L unless EQUITY_BREAKOUT_CAPITAL says otherwise); never touches an account that has traded."""
    cap = float(os.environ.get("EQUITY_BREAKOUT_CAPITAL", 100_000))
    db.init_account(ACCOUNT, name="Equity breakout pair (swing)", capital=cap, leverage=1.0, risk_pct=S.RISK_PCT)


class Session:
    def __init__(self, db, now: datetime, send=None, log=print):
        from services.utils import telegram
        self.db, self.now, self.send, self.log = db, now, send or telegram.send, log
        init_account(db)
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
        with self.db._get_conn() as con:
            booked = con.execute("SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE account_id = ? AND strategy = ?", (ACCOUNT, NAME[sleeve])).fetchone()[0]
        cash = float(self.acc["initial_capital"]) * SHARE[sleeve] + float(booked)
        held = self.positions(sleeve)
        cash -= sum(p["qty"] * p["entry_price"] for p in held)
        return cash + sum(p["qty"] * (prices.get(p["symbol"]) or p["entry_price"]) for p in held), cash

    def free_cash(self) -> float:
        """The account's real cash: booked money minus what every open holding cost (a sleeve can never spend more than this)."""
        return float(self.db.get_account(ACCOUNT)["current_capital"]) - sum(p["qty"] * p["entry_price"] for p in self.positions())

    def buy(self, sleeve: str, sym: str, key: str, entry: float, atr: float, prices: dict, why: str) -> bool:
        K = S.SLEEVES[sleeve]["K"]
        if len(self.positions(sleeve)) >= K or any(p["symbol"] == sym for p in self.positions()):
            return False
        stop = entry - S.STOP_ATR * atr
        equity, cash = self.sleeve_value(sleeve, prices)
        q = S.size(equity, min(cash, self.free_cash()), K, entry, stop)
        if q < 1:
            return False
        pid = f"POS_EQBO_{self.now:%Y%m%d%H%M%S}_{sym}"   # with the time: a stock stopped out and re-bought the same day gets a new row
        st = {"sleeve": sleeve, "stop": stop, "hi": entry, "entry_day": str(self.now.date()), "n": 0, "done_through": None, "atr_at_entry": atr}
        self.db.open_position(pid, sym, "long", q, entry, stop, 0.0, 0.0, account_id=ACCOUNT, instrument_key=key,
                              strategy=S.SLEEVES[sleeve]["name"], state=json.dumps(st))
        plain = {"55-day high": "closed at a new 55-day high", "quiet-day breakout": "broke out above a very quiet day's high"}.get(why, why)
        acc = self.db.get_account(ACCOUNT)
        msg = T.message("🟢", "BOUGHT · Breakout", [T.bought_block(sym, f"{q} shares", entry, q * entry, f"{plain} · swing trade, trailing stop",
                                                                   stop=stop, risk=q * (entry - stop))],
                        value=float(acc["current_capital"]), start=float(acc["initial_capital"]))
        self.lines.append(f"bought {sym}"); self.send(msg); self.log(msg)
        return True

    def sell(self, p: dict, price: float, reason: str, quiet: bool = False) -> str:
        c = S.costs(p["entry_price"], price, p["qty"])
        capital = float(self.db.get_account(ACCOUNT)["current_capital"]) + c["net"]
        self.db.close_position(p["position_id"], price, reason, c["gross"], c["net"], c["total"])
        hold = (self.now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        self.db.record_trade(p["position_id"], p["symbol"], "long", p["qty"], p["entry_price"], price, p["entry_time"], self.now.isoformat(), hold,
                             reason, c["gross"], c, c["net"], capital, account_id=ACCOUNT, strategy=p["strategy"])
        self.db.update_capital(ACCOUNT, capital)
        days = (self.now.date() - datetime.fromisoformat(p["entry_time"]).date()).days
        msg = T.message("✅" if c["net"] >= 0 else "❌", "SOLD · Breakout",
                        [T.sold_block(p["symbol"], f"{int(p['qty'])} shares", p["entry_price"], price, c["net"], c["total"], T.why_text(reason),
                                      f"{days} day{'s' if days != 1 else ''}" if days > 0 else "")],
                        value=capital, start=float(self.acc["initial_capital"]))
        self.lines.append(f"sold {p['symbol']} {T.signed_rupees(c['net'])}")
        if quiet:                                                # a momentum refresh sends one message for all its sales
            return T.sold_block(p["symbol"], f"{int(p['qty'])} shares", p["entry_price"], price, c["net"], c["total"], T.why_text(reason),
                                f"{days} day{'s' if days != 1 else ''}" if days > 0 else "")
        self.send(msg); self.log(msg)
        return msg

    def momentum_open(self, q: dict, plan: dict, keys_all: dict) -> None:
        """First session of the month, at the open: sell momentum holdings that left the top N, buy the new ones (1/N of the momentum
        half each, never more than its cash or the account's free cash), one SOLD and one BOUGHT message; then mark the month done."""
        m = plan["mom"]
        if not (m["due"] and m["buying"]):
            return
        sold = [self.sell(p, op, "momentum_out", quiet=True) for p in self.positions("mom")
                if p["symbol"] not in m["targets"] and (op := (q.get(p["symbol"]) or {}).get("open"))]
        prices = {s: v["ltp"] for s, v in q.items() if v.get("ltp")}
        equity, cash = self.sleeve_value("mom", prices)
        held = {p["symbol"] for p in self.positions()}
        bought = []
        for s in m["targets"]:
            op = (q.get(s) or {}).get("open")
            if s in held or not op or s not in keys_all:
                continue
            n = int(min(equity / M.N, cash, self.free_cash()) // op)
            if n < 1:
                continue
            st = {"sleeve": "mom", "entry_day": str(self.now.date()), "stop": 0.0}
            self.db.open_position(f"POS_EQBO_{self.now:%Y%m%d%H%M%S}_{s}", s, "long", n, op, 0.0, 0.0, 0.0, account_id=ACCOUNT,
                                  instrument_key=keys_all[s], strategy=M.NAME, state=json.dumps(st))
            cash -= n * op; held.add(s); self.lines.append(f"bought {s}")
            bought.append(T.bought_block(s, f"{n} shares", op, n * op, "top 20 by 12-month momentum · kept while it stays in the top 20"))
        acc = self.db.get_account(ACCOUNT)
        for icon, title, blocks in (("✅", "SOLD · Breakout momentum", sold), ("🟢", "BOUGHT · Breakout momentum", bought)):
            if blocks:
                msg = T.message(icon, title, ["Monthly refresh of the momentum half:"] + blocks,
                                value=float(acc["current_capital"]), start=float(acc["initial_capital"]))
                self.send(msg); self.log(msg)
        write_json(MOM_STATE, {"month": f"{self.now:%Y-%m}"})

    # ------------------------------------------------------------------ before the open
    def prepare(self, ind: dict) -> dict:
        """Bring holdings up to the last completed day; return today's plan."""
        C, H, A = ind["C"], ind["H"], ind["atr"]
        last = C.index[-1]
        dead = []
        for p in self.positions():
            st, s = p["st"], p["symbol"]
            if s not in C.columns:
                continue
            if st.get("sleeve") == "mom":                       # momentum: no stop, no dead-money rule; sold only at a monthly refresh
                continue
            ref = st.get("ref")                                  # a split while held: the archive rescales history -> rescale the holding
            if ref and pd.Timestamp(ref[0]) in C.index and C.at[pd.Timestamp(ref[0]), s] > 0:
                f = C.at[pd.Timestamp(ref[0]), s] / ref[1]
                if abs(f - 1) > 0.02:
                    with self.db._get_conn() as con:
                        con.execute("UPDATE positions SET qty = ?, entry_price = ? WHERE position_id = ?", (round(p["qty"] / f), p["entry_price"] * f, p["position_id"]))
                    st["stop"] *= f; st["hi"] *= f
                    self.send(T.problem("Breakout", f"{s}: a split or bonus was found, so the holding was adjusted (x{f:.3f}). Worth a look."))
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
        due = M.rebalance_due((read_json(MOM_STATE) or {}).get("month"), self.now.date())
        other = {p["symbol"] for p in self.positions() if p["st"].get("sleeve") != "mom"}
        targets = M.targets(M.scores(ind).iloc[-1], M.N, exclude=other) if due else []
        return {"last": last, "dead": dead, "cand": cand, "atr": atr, "mom": {"due": due, "targets": targets, "buying": False}}


RELOGIN_EVERY_S = 600
_last_relogin = [-1e9]


def _recover_login(broker, log=print, mono=time.monotonic) -> None:
    """2026-11-19: a login made by another process cancels this session's, and Upstox then answers 401 for the rest of the day (the
    session watched nothing 11:02-15:31). When the broker flags a dead login, adopt the newest saved one -- or log in again -- and hand it
    to the broker; at most once every RELOGIN_EVERY_S so a broken login is not retried every minute."""
    from services.broker.upstox_broker import token_invalid_event
    if not token_invalid_event.is_set() or mono() - _last_relogin[0] < RELOGIN_EVERY_S:
        return
    _last_relogin[0] = mono()
    try:
        from services.auth.upstox_auto_login import ensure_fresh_upstox_token
        ok = ensure_fresh_upstox_token(on_token_refreshed=broker.set_access_token)
    except Exception as exc:                                       # a failed refresh must never stop the session
        log(f"Upstox login recovery failed: {exc!s:.150}")
        return
    if ok:
        token_invalid_event.clear()
    log(f"Upstox login {'recovered' if ok else 'still failing'} after a 401")


def expected_last_day(today: date) -> date:
    from engine.nightly import last_trading_day
    from services.utils.market_holidays import get_trading_holidays
    return last_trading_day(today, get_trading_holidays())


def at(now: datetime, hms) -> datetime:
    return now.replace(hour=hms[0], minute=hms[1], second=hms[2], microsecond=0)


def session(db, broker, send=None, log=print, sleep=time.sleep, clock=lambda: datetime.now(IST), ind=None, keys_all=None, lock=None) -> None:
    from engine.account_reset import account_lock
    lock = lock or (lambda: account_lock(ACCOUNT))
    if keys_all is None:
        from markets.equity.all_daily import instruments
        keys_all = instruments()
    ind = ind if ind is not None else S.indicators(S.load())
    with lock():
        ses = Session(db, clock(), send=send, log=log)
        plan = ses.prepare(ind)
    fresh = plan["last"].date() >= expected_last_day(clock().date())
    cand = plan["cand"]
    buying = fresh and cand["healthy"]
    plan["mom"]["buying"] = fresh                                  # momentum has no market filter, but needs current prices
    log(f"last day {plan['last'].date()} (fresh={fresh}), breadth {cand['breadth']:.1f}%, d55 {cand['d55'][:8]}, nr7 {[s for s, _ in cand['nr7']][:8]}, dead {plan['dead']}")
    if not fresh:
        ses.send(T.problem("Breakout", f"Price data ends {plan['last'].date()}, it should reach {expected_last_day(clock().date())}. "
                                         "No new buys today; stops are still watched.", "journalctl --user -u hft-daily-data-topup.service -n 40"))
    write_json(STATE, {"day": str(clock().date()), "breadth": round(cand["breadth"], 1), "healthy": bool(cand["healthy"]), "fresh": bool(fresh),
                       "last_day": str(plan["last"].date()), "d55": cand["d55"][:8], "nr7": [[s, round(t, 2)] for s, t in cand["nr7"][:8]],
                       "dead": plan["dead"], "momentum": {"due": plan["mom"]["due"], "targets": plan["mom"]["targets"]}})
    pending_nr7 = dict(cand["nr7"]) if buying else {}
    d55 = list(cand["d55"]) if buying else []
    opened = False
    while clock() < at(clock(), CLOSE_AT):
        ses.now = clock()
        if ses.now < at(ses.now, OPEN_AT):
            sleep(min(30, (at(ses.now, OPEN_AT) - ses.now).total_seconds())); continue
        held = ses.positions()
        watch = {p["symbol"] for p in held} | set(pending_nr7) | (set(d55) | set(plan["mom"]["targets"]) if not opened else set())
        keys = {s: keys_all[s] for s in watch if s in keys_all}
        try:
            q = quotes(broker, keys)
        except Exception as exc:
            log(f"quote failed: {exc!s:.150}"); _recover_login(broker, log); sleep(POLL_S); continue
        _recover_login(broker, log)
        prices = {s: v["ltp"] for s, v in q.items() if v.get("ltp")}
        if not opened and any(not (q.get(s) or {}).get("open") for s in keys) and ses.now < at(ses.now, (9, 20, 0)):
            sleep(10); continue                                      # opening prices not published yet
        with lock():
            if not opened:
                ses.momentum_open(q, plan, keys_all)               # sales first: they free cash for the open's buys
            opened = _step(ses, q, prices, plan, d55, pending_nr7, keys_all, opened)
        sleep(POLL_S)
    # after the close
    ses.now = clock()
    held = ses.positions()
    try:
        q = quotes(broker, {p["symbol"]: keys_all[p["symbol"]] for p in held if p["symbol"] in keys_all})
    except Exception:
        q = {}
    with lock():
        db.record_snapshot(ACCOUNT)
    _summary(ses, db, held, q, cand)


def _step(ses, q, prices, plan, d55, pending_nr7, keys_all, opened) -> bool:
    """One poll: the opening actions (once), then resting stop and buy-stop orders. Returns opened."""
    if not opened:
        opened = True
        for p in ses.positions():                                    # dead money and gap-through stops sell at the open
            op = (q.get(p["symbol"]) or {}).get("open")
            if not op:
                continue
            if p["symbol"] in plan["dead"]:
                ses.sell(p, op, "dead_money")
            elif op <= p["current_stop"]:
                ses.sell(p, op, "stop")
        for s in d55:                                                # d55: buy at the open, strongest first
            op = (q.get(s) or {}).get("open")
            if op and plan["atr"].get(s, 0) > 0:
                ses.buy("d55", s, keys_all[s], op, plan["atr"][s], prices, "55-day high")
    for p in ses.positions():                                        # resting stop orders
        v = q.get(p["symbol"]) or {}
        if v.get("low") and v["low"] <= p["current_stop"]:
            ses.sell(p, p["current_stop"] if (v.get("open") or 0) > p["current_stop"] else v["open"], "stop")
    for s, trig in list(pending_nr7.items()):                        # resting buy-stop orders
        v = q.get(s) or {}
        if len(ses.positions("nr7")) >= S.SLEEVES["nr7"]["K"]:
            break
        if v.get("high") and v["high"] >= trig:
            pending_nr7.pop(s)
            ses.buy("nr7", s, keys_all[s], max(v.get("open") or trig, trig), plan["atr"][s], prices, "quiet-day breakout")
    return opened


def _summary(ses, db, held, q, cand) -> None:
    acc = db.get_account(ACCOUNT)
    open_pnl = sum(((q.get(p["symbol"]) or {}).get("ltp") or p["entry_price"]) * p["qty"] - p["entry_price"] * p["qty"] for p in held)
    rows = [f"{p['symbol']}: {T.signed_rupees((((q.get(p['symbol']) or {}).get('ltp') or p['entry_price']) - p['entry_price']) * p['qty'])}"
            for p in held]
    market = (f"Market healthy ({cand['breadth']:.0f}% of the 200 most-traded stocks above their 50-day average)" if cand["healthy"] else
              f"Market weak ({cand['breadth']:.0f}% above their 50-day average, needs {S.MIN_BREADTH_PCT:.0f}%), so no new buys")
    blocks = [market, "Today: " + (", ".join(ses.lines) if ses.lines else "no trades"), "Holding: " + (" · ".join(rows) if rows else "nothing")]
    ses.send(T.message("📊", f"Breakout · end of day {ses.now:%d %b}", blocks, value=float(acc["current_capital"]) + open_pnl,
                       start=float(acc["initial_capital"])))


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
        init_account(db)
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
    from markets.commodity.data import _get_broker
    session(db, _get_broker(), send=(print if a.no_telegram else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
