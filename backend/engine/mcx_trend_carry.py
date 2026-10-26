"""Paper trading for commodity/trend_carry (MCX trend + carry, re-checked every S.REBALANCE_DAYS trading days, ~2 weeks since 2026-10-25).

    python3 -m engine.mcx_trend_carry            # re-check if REBALANCE_DAYS trading days have passed (or after a reset), then mark to market
    python3 -m engine.mcx_trend_carry --status   # print the account, no trading

Run by hft-mcx-trend-carry.timer at 09:10 IST (after MCX opens: a pending re-check trades at the live price, signals from the last close) and 23:40 IST (after
the close: a daily mark-to-market line). No real orders: prices come from Upstox quotes, fills are recorded in var/db/paper_trading.db under
account MCX_TREND_CARRY. Settings (.env): MCX_TREND_CARRY_CAPITAL (default Rs2.5 lakh, since 2026-10-21 -- user: "make it 2.5 lakh account
and reset"; below ~Rs2-2.5L whole-lot minimums starve the strategy of trades, but above ~Rs2.5L the % return barely changes with more
capital, see markets/README.md), MCX_TREND_CARRY_EXPOSURE (default 1.5 = total contract value up to 1.5x the account; Upstox margin is
~9-35% of that, capped at 80% of the account). More than ~2x exposure took worst falls to -25..-44% (full Upstox leverage lost ~85% in
June 2026). See the strategy module's docstring.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from core.atomic_io import read_json, write_json
from core.paths import DB_DIR
from markets.commodity import futures_eod
from markets.commodity.costs import compute_mcx_costs, lot_units
from markets.commodity.strategies.tf_daily.trend_carry import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT, NAME = "MCX_TREND_CARRY", "trend_carry"
STATE = DB_DIR / "mcx_trend_carry_state.json"
ROLL_DAYS = 3                          # a held contract this close to expiry is swapped for the next one


def _inr(v: float) -> str:
    from services.utils.telegram import inr
    return ("−" if v < 0 else "") + "₹" + inr(v)


def _state() -> dict:
    return read_json(STATE)


def _save_state(st: dict) -> None:
    write_json(STATE, st, indent=1)        # atomic: a crash mid-write must not leave a truncated state file


def needs_rebalance(st: dict, signal_day: pd.Timestamp, days: list, open_pos: list, known_ids: set) -> bool:
    """True when a re-check is due: S.REBALANCE_DAYS trading days since the last one (S.rebalance_due) -- or the account was reset.

    Reset (2026-10-24 fix): a re-check records the position ids it holds; when none is open and none is even in the DB any more, the
    account was reset and the current signal is bought again. A re-check that legitimately bought nothing records [] and is not repeated."""
    if S.rebalance_due(st.get("rebalanced_on"), days, signal_day):
        return True
    if open_pos:
        return False
    opened = st.get("opened")
    return opened is None or (bool(opened) and not set(opened) & known_ids)


def contract_for(signal: str, today, contracts: pd.DataFrame):
    """The mini contract to hold: the nearest expiry more than ROLL_DAYS away (a later roll switches it before expiry)."""
    fam = S.TRADE[signal]
    c = contracts[(contracts["symbol"] == fam) & (contracts["expiry"] > pd.Timestamp(today) + timedelta(days=ROLL_DAYS))]
    return c.sort_values("expiry").iloc[0] if len(c) else None


def run(broker, db, now: datetime, report: bool = True, send=None) -> dict:
    from services.utils import telegram
    send = send or telegram.send
    capital0 = float(os.environ.get("MCX_TREND_CARRY_CAPITAL", 250_000))
    exposure = float(os.environ.get("MCX_TREND_CARRY_EXPOSURE", 1.5))
    db.init_account(ACCOUNT, name="MCX trend + carry", capital=capital0, leverage=exposure, risk_pct=0.0)
    acc = db.get_account(ACCOUNT)
    capital = float(acc["current_capital"])
    st = _state()
    table = futures_eod.load()
    sig_rows = table[table["symbol"].isin(S.SIGNALS)] if len(table) else table
    days = sorted(d for d in (sig_rows["date"].unique() if len(sig_rows) else []) if pd.Timestamp(d).date() < now.date())
    signal_day = pd.Timestamp(days[-1]) if days else pd.Timestamp(now.date())      # signals from the last completed session
    contracts = futures_eod.active_contracts()
    open_pos = db.get_open_positions(ACCOUNT)
    keys = set(contracts["instrument_key"]) | {p["instrument_key"] for p in open_pos if p.get("instrument_key")}
    ltp = {k: v for k, v in (broker.get_ltp_batch(sorted(keys)) or {}).items() if v}
    lines, now_s = [], now.isoformat()

    def close(p, reason):
        nonlocal capital
        px = ltp.get(p["instrument_key"])
        if not px:
            lines.append(f"• {p['symbol']}: no live price, kept open")
            return False
        stt = json.loads(p.get("state") or "{}")
        c = compute_mcx_costs(stt.get("family", p["symbol"]), "long", p["entry_price"], px, p["qty"])
        capital += c["net"]
        db.close_position(p["position_id"], px, reason, c["gross"], c["net"], c["total"])
        hold = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        db.record_trade(p["position_id"], p["symbol"], "long", p["qty"], p["entry_price"], px, p["entry_time"], now_s, hold, reason, c["gross"], c,
                        c["net"], capital, account_id=ACCOUNT, strategy=NAME)
        lines.append(f"• SOLD {p['symbol']} {p['qty']} lot(s) {p['entry_price']:,.2f} → {px:,.2f}: {'+' if c['net'] >= 0 else ''}{_inr(c['net'])} after fees")
        return True

    def buy(signal, weight, contract, lots=None):
        px = ltp.get(contract["instrument_key"])
        if not px:
            lines.append(f"• {S.TRADE[signal]}: no live price, skipped")
            return None
        lots = lots if lots is not None else S.lots_for(capital, exposure, weight, px, signal)
        lot_val = px * lot_units(S.TRADE[signal])
        if lots < 1:
            lines.append(f"• {S.TRADE[signal]} (weight {weight:.0%}): one lot is {_inr(lot_val)}, more than its share -- kept in cash")
            return None
        pid = f"POS_MCXTC_{now.strftime('%Y%m%d_%H%M%S')}_{contract['tradingsymbol']}"
        state = {"family": S.TRADE[signal], "signal": signal, "weight": round(weight, 4), "expiry": str(contract["expiry"].date()),
                 "lot_units": lot_units(S.TRADE[signal]), "margin_used": round(lots * lot_val * S.MARGIN_PCT[signal], 2), "signal_day": str(signal_day.date())}
        db.open_position(pid, contract["tradingsymbol"], "long", lots, px, 0.0, 0.0, 0.0, account_id=ACCOUNT,
                         instrument_key=contract["instrument_key"], strategy=NAME, state=json.dumps(state))
        lines.append(f"• BOUGHT {contract['tradingsymbol']} {lots} lot(s) @ {px:,.2f} = {_inr(lots * lot_val)} (weight {weight:.0%})")
        return pid

    did = None
    if needs_rebalance(st, signal_day, days, open_pos, db.get_position_ids(ACCOUNT)):
        R, C = S.daily_panel(table)
        w = S.targets(R, C, signal_day)
        if not w:
            lines.append("not enough history for a signal")
        else:
            held = {json.loads(p.get("state") or "{}").get("signal"): p for p in open_pos}
            ok = True
            for sig, p in held.items():                               # sell what dropped out of the top 3
                if sig not in w:
                    ok = close(p, "rebalance_dropped") and ok
            for sig, wt in sorted(w.items(), key=lambda x: -x[1]):
                c = contract_for(sig, now.date(), contracts)
                if c is None:
                    lines.append(f"• {S.TRADE[sig]}: no contract alive past the next few days, skipped")
                    continue
                px = ltp.get(c["instrument_key"])
                want = S.lots_for(capital, exposure, wt, px, sig) if px else None
                p = held.get(sig)
                if p is not None and want is not None and p["instrument_key"] == c["instrument_key"] and int(p["qty"]) == want:
                    lines.append(f"• KEPT {p['symbol']} {p['qty']} lot(s) (still in the top {S.TOP_N}, weight {wt:.0%})")
                    continue
                if p is not None:
                    ok = close(p, "rebalance_resize") and ok
                buy(sig, wt, c)
            if ok:
                st["rebalanced_on"] = str(signal_day.date())
                st["opened"] = [p["position_id"] for p in db.get_open_positions(ACCOUNT)]
                st.pop("rebalanced_for", None)
            did = "rebalance"
    else:
        for p in open_pos:                                           # roll a contract about to expire into the next one, same lots
            stt = json.loads(p.get("state") or "{}")
            if stt.get("expiry") and pd.Timestamp(stt["expiry"]) - pd.Timestamp(now.date()) <= pd.Timedelta(days=ROLL_DAYS):
                c = contract_for(stt["signal"], now.date() + timedelta(days=ROLL_DAYS + 1), contracts)
                if c is not None and close(p, "roll_before_expiry"):
                    buy(stt["signal"], stt.get("weight", 0.0), c, lots=p["qty"])
                    did = "roll"
    db.update_capital(ACCOUNT, capital)
    _save_state(st)

    # mark to market
    open_pos = db.get_open_positions(ACCOUNT)
    open_pnl, margin, rows = 0.0, 0.0, []
    for p in open_pos:
        stt = json.loads(p.get("state") or "{}")
        px = ltp.get(p["instrument_key"])
        u = stt.get("lot_units", 1)
        margin += float(stt.get("margin_used") or 0.0)
        if px:
            pnl = (px - p["entry_price"]) * p["qty"] * u
            open_pnl += pnl
            rows.append(f"• {p['symbol']} {p['qty']} lot(s) {p['entry_price']:,.2f} → {px:,.2f}: <b>{'+' if pnl >= 0 else ''}{_inr(pnl)}</b>")
        else:
            rows.append(f"• {p['symbol']} {p['qty']} lot(s) @ {p['entry_price']:,.2f}: no live price")
    db.record_snapshot(ACCOUNT)
    value = capital + open_pnl
    exposure_now = sum((ltp.get(p["instrument_key"]) or p["entry_price"]) * p["qty"] * json.loads(p.get("state") or "{}").get("lot_units", 1) for p in open_pos)
    head = {"rebalance": f"🛢 <b>MCX TREND+CARRY: 2-weekly re-check</b> (signals from the {signal_day.date():%d %b} close)",
            "roll": "🛢 <b>MCX TREND+CARRY: contract roll</b>"}.get(did, f"🛢 <b>MCX TREND+CARRY</b> ({now:%d %b %H:%M} IST)")
    msg = (head + "\n" + ("\n".join(lines) + "\n\n" if lines else "")
           + f"Account value: <b>{_inr(value)}</b> (started {_inr(float(acc['initial_capital']))}; booked {_inr(capital)} + open {'+' if open_pnl >= 0 else ''}{_inr(open_pnl)})\n"
           + f"Holding {_inr(exposure_now)} of contracts ({exposure_now / value:.1f}x the account) · margin ≈ {_inr(margin)}\n"
           + ("\n".join(rows) if rows else "No open positions."))
    if report or did:
        send(msg)
    return {"did": did, "value": value, "capital": capital, "open_pnl": open_pnl, "lines": lines, "message": msg}


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    from engine.database import TradingDB
    from markets.commodity.data import _get_broker
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="print the account only")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.now(IST)
    db = TradingDB()
    if a.status:
        acc = db.get_account(ACCOUNT)
        print(acc, *db.get_open_positions(ACCOUNT), sep="\n")
        return 0
    if now.weekday() >= 5:
        print("weekend: MCX closed")
        return 0
    try:
        from services.utils.market_holidays import get_trading_holidays
        if now.date() in get_trading_holidays():
            print("exchange holiday")
            return 0
    except Exception:
        pass
    from engine.account_reset import account_lock
    with account_lock(ACCOUNT):                        # a reset from the paper page waits for this run (and vice versa)
        out = run(_get_broker(), db, now, report=True, send=(print if a.no_telegram else None))
    print(out["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
