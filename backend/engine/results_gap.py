"""Paper trader for equity/results_gap (2026-10-24): buy results-day gap-ups at 09:30, sell at 15:20. Paper only -- no real orders.

    python3 -m engine.results_gap               # what the timer runs; the mode follows the clock (IST, Mon-Fri):
                                                #   before 09:25  -> refresh the NSE results calendar
                                                #   09:25-14:59   -> morning entry (once a day)
                                                #   from 15:15    -> sell everything still open (idempotent; 15:26 retries)
    python3 -m engine.results_gap --status [--no-telegram]

Account RESULTS_GAP, Rs1L, notional = RESULTS_GAP_LEVERAGE (default 3x) x the account, split equally across the morning's signals.
Prices come from Upstox's live OHLC quote (09:15 open, previous close, last price). Rules and costs live in the strategy module.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone

from core.paths import DB_DIR
from markets.equity.strategies.tf_intraday.results_gap import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT, NAME = "RESULTS_GAP", "results_gap"
INITIAL_CAPITAL = 100_000.0
STATE = DB_DIR / "results_gap_state.json"


def _inr(v: float) -> str:
    return ("-" if v < 0 else "") + "Rs" + f"{abs(v):,.0f}"


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    st["updated_at"] = datetime.now(IST).isoformat()
    STATE.write_text(json.dumps(st, indent=1))


def prev_trading_day(d: date, holidays: set[date]) -> date:
    p = d - timedelta(days=1)
    while p.weekday() >= 5 or p in holidays:
        p -= timedelta(days=1)
    return p


def init_account(db) -> None:
    db.init_account(ACCOUNT, name="Results-day gap-up (intraday)", capital=INITIAL_CAPITAL, leverage=S.LEVERAGE, risk_pct=0.0)


def _keys(symbols: list[str], lookup) -> dict[str, str]:
    out = {}
    for s in symbols:
        k = lookup(s)
        if k:
            out[s] = k
    return out


def enter(db, broker, now: datetime, holidays: set[date], lookup, lines: list) -> None:
    st = _state()
    today = now.date()
    if st.get("entered_on") == str(today):
        return
    for p in db.get_open_positions(ACCOUNT):                       # a sale the engine missed (e.g. a failed 15:20 run): sell it now
        _close(db, broker, p, now, "missed_exit", lines)
    if not S.CALENDAR.exists() or datetime.fromtimestamp(S.CALENDAR.stat().st_mtime, IST).date() != today:
        try:
            S.refresh_calendar(today)
        except Exception as e:                                      # an old calendar still covers yesterday's results
            lines.append(f"results calendar refresh failed ({type(e).__name__}); using the saved one")
    cands = S.candidates(today, prev_trading_day(today, holidays))
    keys = _keys(S.universe(), lookup)
    raw = broker.get_ohlc_batch(list(keys.values())) or {}
    quotes = {s: raw.get(k) for s, k in keys.items()}
    buys, mkt = S.signals(quotes, cands)
    st["entered_on"] = str(today)
    st["last_check"] = {"candidates": len(cands), "market_gap": mkt, "signals": [b["symbol"] for b in buys]}
    if mkt is None:
        lines.append("No usable 09:30 quotes from Upstox (login expired or market closed?) -- no trades today.")
        _save_state(st)
        return
    if not buys:
        lines.append(f"No trade today: {len(cands)} stocks had results, none opened 7%+ above the market (market gap {mkt:+.2%}) and kept rising.")
        _save_state(st)
        return
    cap = float(db.get_account(ACCOUNT)["current_capital"])
    per = cap * S.LEVERAGE / len(buys)
    for b in buys:
        qty = int(per // b["ltp"])
        if qty < 1:
            lines.append(f"{b['symbol']}: one share costs more than its share of the money -- skipped")
            continue
        pid = f"POS_RG_{now:%Y%m%d}_{b['symbol']}"
        db.open_position(pid, b["symbol"], "long", qty, b["ltp"], 0.0, 0.0, b["ltp"], account_id=ACCOUNT, instrument_key=keys[b["symbol"]],
                         strategy=NAME, state=json.dumps({"gap_vs_mkt": b["gap_vs_mkt"], "open": b["open"], "prev_close": b["prev_close"]}))
        lines.append(f"BOUGHT {b['symbol']} {qty} @ {b['ltp']:,.2f} = {_inr(qty * b['ltp'])} (opened {b['gap_vs_mkt']:+.1%} vs market after results, "
                     f"still rising at 09:30)")
    _save_state(st)


def _close(db, broker, p: dict, now: datetime, reason: str, lines: list) -> bool:
    px = (broker.get_ltp_batch([p["instrument_key"]]) or {}).get(p["instrument_key"]) if p.get("instrument_key") else None
    if not px:
        lines.append(f"{p['symbol']}: no live price -- kept open, will retry")
        return False
    qty, entry = int(p["qty"]), float(p["entry_price"])
    c = S.costs(entry, px, qty)
    gross = (px - entry) * qty
    net = gross - c["total"]
    cap = float(db.get_account(ACCOUNT)["current_capital"]) + net
    db.close_position(p["position_id"], px, reason, gross, net, c["total"])
    hold = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
    db.record_trade(p["position_id"], p["symbol"], "long", qty, entry, px, p["entry_time"], now.isoformat(), hold, reason, gross, c, net, cap,
                    account_id=ACCOUNT, strategy=NAME)
    lines.append(f"SOLD {p['symbol']} {qty} {entry:,.2f} -> {px:,.2f}: {'+' if net >= 0 else ''}{_inr(net)} after fees")
    return True


def exit_all(db, broker, now: datetime, lines: list) -> None:
    for p in db.get_open_positions(ACCOUNT):
        _close(db, broker, p, now, "end_of_day", lines)
    st = _state()
    st["exited_on"] = str(now.date())
    _save_state(st)


def run(db, broker, now: datetime, holidays: set[date], lookup, send=print) -> list[str]:
    """One timer run; the clock picks what to do. Returns the message lines (also sent to Telegram when there is news)."""
    init_account(db)
    lines: list[str] = []
    t = now.time()
    if now.weekday() >= 5 or now.date() in holidays:
        return ["market closed today"]
    if t < dtime(9, 25):
        n = S.refresh_calendar(now.date())
        return [f"results calendar refreshed: {n} results meetings in the last/next 7 days"]
    if t < dtime(15, 0):
        enter(db, broker, now, holidays, lookup, lines)
    elif t >= dtime(15, 15):
        exit_all(db, broker, now, lines)
    if lines and send:
        acc = db.get_account(ACCOUNT)
        send("📈 <b>RESULTS GAP</b>\n" + "\n".join(lines) + f"\nAccount: {_inr(float(acc['current_capital']))}")
    _save_state(_state())                                          # heartbeat for the monitor / paper page
    return lines


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    from engine.account_reset import account_lock
    from engine.database import TradingDB
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    db = TradingDB()
    if a.status:
        init_account(db)
        print(db.get_account(ACCOUNT), *db.get_open_positions(ACCOUNT), _state(), sep="\n")
        return 0
    from markets.commodity.data import _get_broker
    from services.broker.instruments import get_instrument_key
    from services.utils import telegram
    from services.utils.market_holidays import get_trading_holidays
    try:
        holidays = get_trading_holidays()
    except Exception:
        holidays = set()
    with account_lock(ACCOUNT):
        out = run(db, _get_broker(), datetime.now(IST), holidays, get_instrument_key, send=print if a.no_telegram else telegram.send)
    print("\n".join(out) if out else "nothing to do at this time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
