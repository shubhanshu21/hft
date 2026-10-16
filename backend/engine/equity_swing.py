"""Paper trading for the equity swing account (EQUITY_SWING, Rs3 lakh by default): three swing strategies sharing ONE account, the way
they would share one real Upstox account (user 2026-10-16: "why we are doing multiple paper trading account use same" -> "ok do it").
No real orders: prices come from Upstox quotes, fills are recorded in var/db/paper_trading.db.

  sleeve  strategy (module)                                         money     holdings  exits
  nr7     breakout_pair, quiet-day breakout (tf_daily/breakout_pair)   1/6       up to 5   2.5-ATR stop, 3.25-ATR trail, 20-day dead money
  d55     breakout_pair, 55-day-high breakout                          1/6       up to 3   same
  mom     momentum_portfolio (tf_quarterly/momentum_portfolio)         1/3       15        quarterly rebalance (keep while in the top 30)
  earn    earnings_drift (tf_daily/earnings_drift)                     1/3       up to 5   after 40 trading days
Each sleeve's money = its share of the start capital + its own booked profit (trades are tagged with the strategy name, so the Paper
trading page shows profit per strategy). A stock is held at most once across the account: if a sleeve wants a stock another sleeve holds,
it skips it (momentum takes the next-ranked stock).

    python3 -m engine.equity_swing             # one session 09:08 -> 15:31 IST (hft-equity-swing.timer, Mon-Fri)
    python3 -m engine.equity_swing --plan      # what today's session would do, from the last completed day; no trading
    python3 -m engine.equity_swing --status    # print the account

A session, matching the strategies' backtests:
  * before the open: load the nightly all-NSE daily archive (markets/equity/all_daily.py) and results dates (markets/equity/results_dates.py);
    bring breakout stops / day counts up to the last completed day; list dead-money, 40-day earnings exits, the momentum rebalance (first
    session, then the first session after each quarter end), and new signals;
  * at the open (first quote with an opening price): sell what is due at the open price, then buy d55 breakouts, earnings and momentum stocks
    at the open price;
  * every minute until 15:30, one batch quote: breakout stops (a day low at/below the stop sells at the stop, as a resting stop order) and
    nr7 buy-stops (a day high at/above the trigger buys at max(open, trigger));
  * after the close: a snapshot and a Telegram summary per strategy.
If the archive is not current (the nightly job failed), no new buys are made that day; stops are still watched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core.paths import DB_DIR
from markets.equity.strategies.tf_daily.breakout_pair import strategy as BP
from markets.equity.strategies.tf_daily.earnings_drift import strategy as ED
from markets.equity.strategies.tf_quarterly.momentum_portfolio import strategy as MP

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT = "EQUITY_SWING"
STATE = DB_DIR / "equity_swing_state.json"
POLL_S = 60
OPEN_AT, CLOSE_AT = (9, 15, 30), (15, 30, 30)
SLEEVES = {
    "nr7": dict(name="nr7_breakout", share=1 / 6, K=BP.SLEEVES["nr7"]["K"], label="quiet-day breakout"),
    "d55": dict(name="d55_breakout", share=1 / 6, K=BP.SLEEVES["d55"]["K"], label="55-day breakout"),
    "mom": dict(name="momentum_portfolio", share=1 / 3, K=MP.N_HOLD, label="momentum"),
    "earn": dict(name="earnings_drift", share=1 / 3, K=ED.K, label="earnings drift"),
}
SLEEVE_OF = {cfg["name"]: k for k, cfg in SLEEVES.items()}


def _inr(v: float) -> str:
    from services.utils.telegram import inr
    return ("−" if v < 0 else "") + "₹" + inr(v)


def quotes(keys: dict[str, str]) -> dict[str, dict]:
    """symbol -> {ltp, open, high, low} from Upstox batch quotes (<= 500 keys per call)."""
    import requests
    from engine.config import _load_access_token
    out, items = {}, list(keys.items())
    for k in range(0, len(items), 450):
        part = dict(items[k:k + 450])
        r = requests.get("https://api.upstox.com/v2/market-quote/quotes", params={"instrument_key": ",".join(part.values())},
                         headers={"Authorization": f"Bearer {_load_access_token()}", "Accept": "application/json"}, timeout=15)
        r.raise_for_status()
        by_key = {v: s for s, v in part.items()}
        for v in (r.json().get("data") or {}).values():
            o = v.get("ohlc") or {}
            out[by_key.get(v.get("instrument_token"))] = {"ltp": v.get("last_price"), "open": o.get("open"), "high": o.get("high"), "low": o.get("low")}
    return out


def expected_last_day(today: date) -> date:
    from engine.nightly import last_trading_day
    from services.utils.market_holidays import get_trading_holidays
    return last_trading_day(today, get_trading_holidays())


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def momentum_due(st: dict, held_mom: int, last_day: pd.Timestamp, qends: list) -> pd.Timestamp | None:
    """Signal day of a due momentum rebalance: the last completed day for the first build, then each new quarter end."""
    if not held_mom and not st.get("mom_rebalanced_for"):
        return last_day
    done = pd.Timestamp(st.get("mom_rebalanced_for") or "1900-01-01")
    new = [q for q in qends if q > done]
    return new[-1] if new else None


class Session:
    def __init__(self, db, now: datetime, send=None, log=print):
        from services.utils import telegram
        self.db, self.now, self.send, self.log = db, now, send or telegram.send, log
        cap = float(os.environ.get("EQUITY_SWING_CAPITAL", 300_000))
        db.init_account(ACCOUNT, name="Equity swing (breakout + momentum + earnings drift)", capital=cap, leverage=1.0, risk_pct=BP.RISK_PCT)
        self.acc = db.get_account(ACCOUNT)
        self.lines: list[str] = []

    def positions(self, sleeve: str | None = None) -> list[dict]:
        ps = self.db.get_open_positions(ACCOUNT)
        for p in ps:
            p["st"] = json.loads(p.get("state") or "{}")
        return [p for p in ps if sleeve is None or p["st"].get("sleeve") == sleeve]

    def held(self) -> set[str]:
        return {p["symbol"] for p in self.positions()}

    def sleeve_value(self, sleeve: str, prices: dict) -> tuple[float, float]:
        """(equity, cash) of one sleeve: its share of the start capital + its booked profit, minus what its holdings cost, plus their value."""
        with self.db._get_conn() as con:
            booked = con.execute("SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE account_id = ? AND strategy = ?",
                                 (ACCOUNT, SLEEVES[sleeve]["name"])).fetchone()[0]
        cash = float(self.acc["initial_capital"]) * SLEEVES[sleeve]["share"] + float(booked)
        mine = self.positions(sleeve)
        cash -= sum(p["qty"] * p["entry_price"] for p in mine)
        return cash + sum(p["qty"] * (prices.get(p["symbol"]) or p["entry_price"]) for p in mine), cash

    def buy(self, sleeve: str, sym: str, key: str, entry: float, prices: dict, why: str, atr: float | None = None, extra: dict | None = None) -> bool:
        K = SLEEVES[sleeve]["K"]
        if sym in self.held() or len(self.positions(sleeve)) >= K or not entry > 0:
            return False
        equity, cash = self.sleeve_value(sleeve, prices)
        if sleeve in ("nr7", "d55"):
            stop = entry - BP.STOP_ATR * (atr or 0)
            q = BP.size(equity, cash, K, entry, stop) if atr else 0
        else:
            stop = 0.0
            q = int(np.floor(min(equity / K, cash) / entry))
        if q < 1:
            self.lines.append(f"skip {sym} ({SLEEVES[sleeve]['label']}): one share ₹{entry:,.0f} is more than its slot or the cash left")
            return False
        st = {"sleeve": sleeve, "entry_day": str(self.now.date()), "n": 0, "done_through": None, **(extra or {})}
        if sleeve in ("nr7", "d55"):
            st.update(stop=stop, hi=entry, atr_at_entry=atr)
        self.db.open_position(f"POS_EQSW_{self.now:%Y%m%d}_{sym}", sym, "long", q, entry, stop, 0.0, 0.0, account_id=ACCOUNT, instrument_key=key,
                              strategy=SLEEVES[sleeve]["name"], state=json.dumps(st))
        msg = (f"🟢 <b>SWING BUY</b> {sym} · {SLEEVES[sleeve]['label']}\n{q} shares @ ₹{entry:,.2f} = {_inr(q * entry)} ({why})"
               + (f"\nstop ₹{stop:,.2f} (risk {_inr(q * (entry - stop))})" if stop else ""))
        self.lines.append(f"BUY {sym} {q} @ {entry:,.2f} [{sleeve}]"); self.send(msg); self.log(msg)
        return True

    def sell(self, p: dict, price: float, reason: str) -> None:
        c = BP.costs(p["entry_price"], price, p["qty"], p["symbol"])
        capital = float(self.db.get_account(ACCOUNT)["current_capital"]) + c["net"]
        self.db.close_position(p["position_id"], price, reason, c["gross"], c["net"], c["total"])
        hold = (self.now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        self.db.record_trade(p["position_id"], p["symbol"], "long", p["qty"], p["entry_price"], price, p["entry_time"], self.now.isoformat(), hold,
                             reason, c["gross"], c, c["net"], capital, account_id=ACCOUNT, strategy=p["strategy"])
        self.db.update_capital(ACCOUNT, capital)
        sl = SLEEVES.get(p["st"].get("sleeve"), {}).get("label", p["strategy"])
        msg = (f"{'✅' if c['net'] >= 0 else '🔻'} <b>SWING SELL</b> {p['symbol']} · {sl} ({reason.replace('_', ' ')})\n"
               f"{p['qty']} shares ₹{p['entry_price']:,.2f} → ₹{price:,.2f}: <b>{'+' if c['net'] >= 0 else ''}{_inr(c['net'])}</b> after {_inr(c['total'])} fees")
        self.lines.append(f"SELL {p['symbol']} {reason} {'+' if c['net'] >= 0 else ''}{c['net']:,.0f} [{p['st'].get('sleeve')}]"); self.send(msg); self.log(msg)

    def prepare(self, ind: dict, st: dict) -> dict:
        """Bring holdings up to the last completed day; return today's plan."""
        C, H, A = ind["C"], ind["H"], ind["atr"]
        last = C.index[-1]
        sell_at_open: dict[str, str] = {}
        for p in self.positions():
            s, ps = p["symbol"], p["st"]
            if s not in C.columns:
                continue
            ref = ps.get("ref")                                  # split / bonus while held: the archive rescales history -> rescale the holding
            if ref and pd.Timestamp(ref[0]) in C.index and C.at[pd.Timestamp(ref[0]), s] > 0:
                f = C.at[pd.Timestamp(ref[0]), s] / ref[1]
                if abs(f - 1) > 0.02:
                    with self.db._get_conn() as con:
                        con.execute("UPDATE positions SET qty = ?, entry_price = ? WHERE position_id = ?", (round(p["qty"] / f), p["entry_price"] * f, p["position_id"]))
                    if "stop" in ps:
                        ps["stop"] *= f; ps["hi"] *= f
                    self.send(f"⚠️ SWING: {s} split/bonus detected, holding rescaled by {f:.3f}")
            days = C.index[(C.index >= pd.Timestamp(ps["entry_day"])) & (C.index <= last)]
            if ps.get("done_through"):
                days = days[days > pd.Timestamp(ps["done_through"])]
            for d in days:
                if not C.at[d, s] == C.at[d, s]:
                    continue
                ps["n"] += 1
                if ps["sleeve"] in ("nr7", "d55"):
                    ps["hi"] = max(ps["hi"], float(H.at[d, s]))
                    ps["stop"] = max(ps["stop"], ps["hi"] - BP.TRAIL_ATR * float(A.at[d, s]))
                    if ps["n"] >= BP.DEAD_DAYS and C.at[d, s] < p["entry_price"]:
                        ps["dead"] = True
                ps["done_through"] = str(d.date())
            ps["ref"] = [str(last.date()), float(C.at[last, s])]
            self.db.update_position_stop(p["position_id"], ps.get("stop", 0.0), ps.get("hi", p["entry_price"]), False, state_updates=dict(ps))
            if ps.get("dead"):
                sell_at_open[s] = "dead_money"
            if ps["sleeve"] == "earn" and ps["n"] >= ED.HOLD_DAYS:
                sell_at_open[s] = "held_40_days"
        plan = {"last": last, "sell_at_open": sell_at_open, "bo": BP.candidates(ind, last), "atr": A.loc[last].to_dict(),
                "earn": ED.candidates(ind, ED.signals(ind), last)}
        sig_day = momentum_due(st, len(self.positions("mom")), last, MP.quarter_ends(C.index))
        plan["mom_day"] = sig_day
        if sig_day is not None:
            keep, sell, _ = MP.plan(MP.scores(ind).loc[sig_day], [p["symbol"] for p in self.positions("mom")])
            for s in sell:
                sell_at_open[s] = "quarterly_rebalance"
            ranked = MP.scores(ind).loc[sig_day].dropna().sort_values(ascending=False)
            plan["mom_keep"], plan["mom_ranked"] = keep, list(ranked.index)
            plan["mom_rank"] = {s: i + 1 for i, s in enumerate(ranked.index)}
        return plan


def at(now: datetime, hms) -> datetime:
    return now.replace(hour=hms[0], minute=hms[1], second=hms[2], microsecond=0)


def session(db, send=None, log=print, sleep=time.sleep, clock=lambda: datetime.now(IST)) -> None:
    from markets.equity.all_daily import instruments
    ses = Session(db, clock(), send=send, log=log)
    st = _state()
    ind = BP.indicators(BP.load())
    plan = ses.prepare(ind, st)
    fresh = plan["last"].date() >= expected_last_day(clock().date())
    keys = instruments()
    bo = plan["bo"]
    buying = fresh
    log(f"last day {plan['last'].date()} (fresh={fresh}), breadth {bo['breadth']:.1f}%, d55 {bo['d55'][:6]}, nr7 {[s for s, _ in bo['nr7']][:6]}, "
        f"earnings {plan['earn']}, momentum due {plan['mom_day']}, sell at open {plan['sell_at_open']}")
    if not fresh:
        ses.send(f"⚠️ SWING: price archive ends {plan['last'].date()}, expected {expected_last_day(clock().date())} -- no new buys today, stops still watched")
    pending_nr7 = dict(bo["nr7"]) if (buying and bo["healthy"]) else {}
    d55 = list(bo["d55"]) if (buying and bo["healthy"]) else []
    earn = list(plan["earn"]) if buying else []
    mom_ranked = plan.get("mom_ranked", []) if (buying and plan["mom_day"] is not None) else []
    opened = False
    while clock() < at(clock(), CLOSE_AT):
        ses.now = clock()
        if ses.now < at(ses.now, OPEN_AT):
            sleep(min(30, (at(ses.now, OPEN_AT) - ses.now).total_seconds())); continue
        held = ses.positions()
        watch = {p["symbol"] for p in held} | set(pending_nr7)
        if not opened:
            watch |= set(d55) | set(earn) | set(mom_ranked[: 3 * MP.N_HOLD])
        try:
            q = quotes({s: keys[s] for s in watch if s in keys})
        except Exception as exc:
            log(f"quote failed: {exc!s:.150}"); sleep(POLL_S); continue
        prices = {s: v["ltp"] for s, v in q.items() if v.get("ltp")}
        if not opened:
            missing = [s for s in watch if s in keys and not (q.get(s) or {}).get("open")]
            if missing and ses.now < at(ses.now, (9, 20, 0)):
                sleep(10); continue                                      # opening prices not published yet
            opened = True
            for p in held:                                               # sells at the open
                op = (q.get(p["symbol"]) or {}).get("open")
                if not op:
                    continue
                why = plan["sell_at_open"].get(p["symbol"])
                if why:
                    ses.sell(p, op, why)
                elif p["st"].get("sleeve") in ("nr7", "d55") and op <= p["current_stop"]:
                    ses.sell(p, op, "stop")
            for s in d55:                                                # buys at the open
                op = (q.get(s) or {}).get("open")
                if op and s in keys:
                    ses.buy("d55", s, keys[s], op, prices, "55-day high", atr=plan["atr"].get(s))
            for s in earn:
                op = (q.get(s) or {}).get("open")
                if op and s in keys:
                    ses.buy("earn", s, keys[s], op, prices, "strong results reaction")
            if mom_ranked:
                for s in mom_ranked:
                    if len(ses.positions("mom")) >= MP.N_HOLD:
                        break
                    if s in ses.held():
                        continue
                    op = (q.get(s) or {}).get("open")
                    if op and s in keys:
                        ses.buy("mom", s, keys[s], op, prices, f"momentum rank {plan['mom_rank'].get(s)}")
                st["mom_rebalanced_for"] = str(plan["mom_day"].date()); STATE.write_text(json.dumps(st, indent=1))
        for p in ses.positions():                                        # resting stop orders (breakout sleeves)
            if p["st"].get("sleeve") not in ("nr7", "d55"):
                continue
            v = q.get(p["symbol"]) or {}
            if v.get("low") and v["low"] <= p["current_stop"]:
                ses.sell(p, p["current_stop"] if (v.get("open") or 0) > p["current_stop"] else v["open"], "stop")
        for s, trig in list(pending_nr7.items()):                        # resting buy-stop orders (nr7)
            if len(ses.positions("nr7")) >= SLEEVES["nr7"]["K"]:
                break
            v = q.get(s) or {}
            if v.get("high") and v["high"] >= trig:
                pending_nr7.pop(s)
                ses.buy("nr7", s, keys[s], max(v.get("open") or trig, trig), prices, "quiet-day breakout", atr=plan["atr"].get(s))
        sleep(POLL_S)
    # after the close
    ses.now = clock()
    held = ses.positions()
    try:
        q = quotes({p["symbol"]: keys[p["symbol"]] for p in held if p["symbol"] in keys})
    except Exception:
        q = {}
    prices = {s: v["ltp"] for s, v in q.items() if v.get("ltp")}
    db.record_snapshot(ACCOUNT)
    acc = db.get_account(ACCOUNT)
    open_pnl = sum(((prices.get(p["symbol"]) or p["entry_price"]) - p["entry_price"]) * p["qty"] for p in held)
    rows = []
    for k, cfg in SLEEVES.items():
        eq, _ = ses.sleeve_value(k, prices)
        start = float(acc["initial_capital"]) * cfg["share"]
        rows.append(f"• {cfg['label']}: {_inr(eq)} ({'+' if eq >= start else ''}{(eq / start - 1) * 100:.1f}%), {len(ses.positions(k))} held")
    why = "market healthy" if bo["healthy"] else f"market not healthy ({bo['breadth']:.0f}% of the 200 stocks above their 50-day average, needs {BP.MIN_BREADTH_PCT:.0f}%): no new breakout or earnings buys"
    ses.send(f"📈 <b>EQUITY SWING</b> ({ses.now:%d %b})\nAccount: <b>{_inr(acc['current_capital'] + open_pnl)}</b> (started {_inr(acc['initial_capital'])}; "
             f"open {'+' if open_pnl >= 0 else ''}{_inr(open_pnl)})\n{why}\n" + "\n".join(rows) + "\n"
             + ("\n".join(ses.lines) if ses.lines else "No trades today."))


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    from engine.database import TradingDB
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--plan", action="store_true", help="print today's plan from the last completed day; no trading")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    db = TradingDB()
    if a.status:
        print(db.get_account(ACCOUNT), *db.get_open_positions(ACCOUNT), sep="\n")
        return 0
    if a.plan:
        ses = Session(db, datetime.now(IST), send=print, log=print)
        ind = BP.indicators(BP.load())
        p = ses.prepare(ind, _state())
        bo = p["bo"]
        print(f"last completed day {p['last'].date()}: breadth {bo['breadth']:.1f}% (needs {BP.MIN_BREADTH_PCT:.0f}) -> breakout/earnings {'BUYING' if bo['healthy'] else 'no new buys'}")
        print("d55 at the open:", bo["d55"]); print("nr7 buy-stops:", bo["nr7"]); print("earnings at the open:", p["earn"])
        print("momentum rebalance due:", p["mom_day"], "-> top 15:", p.get("mom_ranked", [])[:15]); print("sell at the open:", p["sell_at_open"])
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
