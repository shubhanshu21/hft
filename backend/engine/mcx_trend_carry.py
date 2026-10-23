"""Paper trading for commodity/trend_carry (monthly MCX trend + carry). Its own paper account, kept apart from the Rs1 lakh equity/currency one.

    python3 -m engine.mcx_trend_carry            # rebalance if a month has ended since the last one, then mark to market (Telegram)
    python3 -m engine.mcx_trend_carry --status   # print the account, no trading

Run by hft-mcx-trend-carry.timer at 09:10 IST (after MCX opens: a pending month-end rebalance trades at the live price) and 23:40 IST (after
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

from core.paths import DB_DIR
from markets.commodity import futures_eod
from markets.commodity.costs import compute_mcx_costs, lot_units
from markets.commodity.strategies.tf_monthly.trend_carry import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT, NAME = "MCX_TREND_CARRY", "trend_carry"
STATE = DB_DIR / "mcx_trend_carry_state.json"
ROLL_DAYS = 3                          # a held contract this close to expiry is swapped for the next one


def _inr(v: float) -> str:
    from services.utils.telegram import inr
    return ("−" if v < 0 else "") + "₹" + inr(v)


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=1))


def _month_last_day(d) -> pd.Timestamp:
    return (pd.Timestamp(d) + pd.offsets.MonthEnd(0)).normalize()


def contract_for(signal: str, today, contracts: pd.DataFrame):
    """The mini contract to hold this month: the nearest expiry after this month ends (and more than ROLL_DAYS away)."""
    fam = S.TRADE[signal]
    cutoff = max(_month_last_day(today), pd.Timestamp(today) + timedelta(days=ROLL_DAYS))
    c = contracts[(contracts["symbol"] == fam) & (contracts["expiry"] > cutoff)]
    return c.sort_values("expiry").iloc[0] if len(c) else None


def run(broker, db, now: datetime, report: bool = True, send=None) -> dict:
    from services.utils import telegram
    send = send or telegram.send
    capital0 = float(os.environ.get("MCX_TREND_CARRY_CAPITAL", 250_000))
    exposure = float(os.environ.get("MCX_TREND_CARRY_EXPOSURE", 1.5))
    db.init_account(ACCOUNT, name="MCX trend + carry (monthly)", capital=capital0, leverage=exposure, risk_pct=0.0)
    acc = db.get_account(ACCOUNT)
    capital = float(acc["current_capital"])
    st = _state()
    table = futures_eod.load()
    ends = S.month_ends(table, before=pd.Timestamp(now.date()))
    month_end = ends[-1]
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
            return
        lots = lots if lots is not None else S.lots_for(capital, exposure, weight, px, signal)
        lot_val = px * lot_units(S.TRADE[signal])
        if lots < 1:
            lines.append(f"• {S.TRADE[signal]} (weight {weight:.0%}): one lot is {_inr(lot_val)}, more than its share -- kept in cash")
            return
        pid = f"POS_MCXTC_{now.strftime('%Y%m%d_%H%M%S')}_{contract['tradingsymbol']}"
        state = {"family": S.TRADE[signal], "signal": signal, "weight": round(weight, 4), "expiry": str(contract["expiry"].date()),
                 "lot_units": lot_units(S.TRADE[signal]), "margin_used": round(lots * lot_val * S.MARGIN_PCT[signal], 2), "month_end": str(month_end.date())}
        db.open_position(pid, contract["tradingsymbol"], "long", lots, px, 0.0, 0.0, 0.0, account_id=ACCOUNT,
                         instrument_key=contract["instrument_key"], strategy=NAME, state=json.dumps(state))
        lines.append(f"• BOUGHT {contract['tradingsymbol']} {lots} lot(s) @ {px:,.2f} = {_inr(lots * lot_val)} (weight {weight:.0%})")

    did = None
    if st.get("rebalanced_for") != str(month_end.date()):
        R, C = S.panel(table, ends)
        w = S.targets(R, C, month_end)
        if not w:
            lines.append("not enough history for a signal")
        else:
            sold_ok = all(close(p, "monthly_rebalance") for p in open_pos)
            if sold_ok:
                for sig, wt in sorted(w.items(), key=lambda x: -x[1]):
                    c = contract_for(sig, now.date(), contracts)
                    if c is None:
                        lines.append(f"• {S.TRADE[sig]}: no contract alive past this month, skipped")
                        continue
                    buy(sig, wt, c)
                st["rebalanced_for"] = str(month_end.date())
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
    head = {"rebalance": f"🛢 <b>MCX TREND+CARRY: monthly rebalance</b> (signals from {month_end.date():%d %b})",
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
    out = run(_get_broker(), db, now, report=True, send=(print if a.no_telegram else None))
    print(out["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
