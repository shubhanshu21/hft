"""Paper trader for equity/results_gap (2026-10-24): buy results-day gap-ups at 09:30, sell at 15:20. Paper only -- no real orders.

    python3 -m engine.results_gap               # what the timer runs; the mode follows the clock (IST, Mon-Fri):
                                                #   before 09:25  -> refresh the NSE results calendar
                                                #   09:25-14:59   -> morning entry (once a day; 09:40 retries a failed quote fetch)
                                                #   from 15:15    -> sell everything still open (idempotent; 15:26 retries)
    python3 -m engine.results_gap --status [--no-telegram]

Stocks: every NSE stock with >= Rs10 cr median daily turnover over the previous 20 days (S.universe, from NSE's bhavcopy, refreshed at
08:50; 2026-10-29 -- the fixed 414-stock list was hindsight). Account RESULTS_GAP, Rs1L, notional = RESULTS_GAP_LEVERAGE (3x; it was 5x
for one day on 2026-10-29) x the account, split equally across the
morning's signals -- but never more than Upstox's own intraday leverage for that stock (stock_leverage: most get 5x, stocks on the
exchanges' watch lists only 1x), so a paper buy is always one Upstox would really accept.
Prices come from Upstox's live OHLC quote (09:15 open, previous close, last price). Rules and costs live in the strategy module.

2026-10-26: the 09:30 run used to mark the day as done BEFORE checking whether Upstox had returned usable quotes, so one failed quote
fetch lost the whole day even when prices were fine minutes later (it happened that morning: market_gap null, no trades). A run with no
usable quotes now leaves the day open for the 09:40 retry slot, and gives up -- loudly, and still alerted by engine_monitor -- only
once the window closes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time as dtime, timedelta, timezone

import engine.config  # noqa: F401  -- loads backend/.env FIRST: the strategy reads RESULTS_GAP_LEVERAGE when it is imported
from core.atomic_io import read_json, write_json
from core.paths import DB_DIR
from markets.equity.strategies.tf_intraday.results_gap import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT, NAME = "RESULTS_GAP", "results_gap"
INITIAL_CAPITAL = 100_000.0
STATE = DB_DIR / "results_gap_state.json"
RETRY_UNTIL = dtime(9, 45)             # no usable quotes by then -> give up on the day (09:40 is the retry slot in the timer)
MAX_ENTRY_ATTEMPTS = 3                 # or after this many tries, whichever comes first
LATE_AFTER = dtime(9, 35)
NIFTY_KEY = "NSE_INDEX|Nifty 50"         # recorded at each buy: the market's 09:15 -> now move (2026-10-29, see enter())              # an entry after this is a delayed entry, and the trade record says so


def _inr(v: float) -> str:
    return ("-" if v < 0 else "") + "Rs" + f"{abs(v):,.0f}"


def _state() -> dict:
    return read_json(STATE)


def _save_state(st: dict) -> None:
    st["updated_at"] = datetime.now(IST).isoformat()
    write_json(STATE, st, indent=1)        # atomic: a crash mid-write must not leave a truncated state file


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


def stock_leverage(broker, key: str, price: float) -> float | None:
    """Upstox's real intraday (MIS) leverage for this stock today: order value / margin from its margin calculator. None = unknown."""
    get = getattr(broker, "get_required_margin", None)
    if get is None or not price or price <= 0:
        return None
    qty = max(1, int(100_000 // price))
    try:
        margin = get(key, qty, "BUY", product="I")
    except Exception:
        return None
    return round(qty * price / margin, 2) if margin and margin > 0 else None


def _runup(sym: str, today: date) -> float | None:
    try:
        r = S.runup20(sym, today)
    except Exception:                                              # context only: a bad file must never block a buy
        return None
    return round(r, 4) if r is not None else None


def enter(db, broker, now: datetime, holidays: set[date], lookup, lines: list) -> None:
    st = _state()
    today = now.date()
    if st.get("entered_on") == str(today):
        return
    if st.get("entry_date") != str(today):                     # a new day: the attempt counter starts over
        st["entry_date"], st["entry_attempts"] = str(today), 0
    for p in db.get_open_positions(ACCOUNT):                       # a sale the engine missed (e.g. a failed 15:20 run): sell it now
        _close(db, broker, p, now, "missed_exit", lines)
    if not S.CALENDAR.exists() or datetime.fromtimestamp(S.CALENDAR.stat().st_mtime, IST).date() != today:
        try:
            S.refresh_calendar(today)
        except Exception as e:                                      # an old calendar still covers yesterday's results
            lines.append(f"results calendar refresh failed ({type(e).__name__}); using the saved one")
    cands = S.candidates(today, prev_trading_day(today, holidays))
    panel = set(S.market_panel())                                   # the market's gap is measured on the tested 414, as in the backtests
    keys = _keys(sorted(set(S.universe(today)) | panel), lookup)
    raw = broker.get_quote_batch(list(keys.values()) + [NIFTY_KEY]) or {}   # full quote: has the previous close (v3 OHLC does not, 2026-10-26)
    quotes = {s: raw.get(k) for s, k in keys.items()}
    priced = {s for s in panel if quotes.get(s)}
    buys, mkt = S.signals(quotes, cands, panel=priced if len(priced) >= 20 else None)
    if mkt is None:
        # NOT a finished day (2026-10-26): one failed quote fetch at 09:30 must not throw away the morning. Keep the day open so the
        # 09:40 slot can try again, and only mark it done -- out loud -- once the retry window has closed.
        st["entry_attempts"] = int(st.get("entry_attempts") or 0) + 1
        st["last_check"] = {"candidates": len(cands), "market_gap": None, "signals": [], "attempt": st["entry_attempts"]}
        if now.time() >= RETRY_UNTIL or st["entry_attempts"] >= MAX_ENTRY_ATTEMPTS:
            st["entered_on"] = str(today)
            lines.append(f"No usable 09:30 quotes from Upstox after {st['entry_attempts']} attempt(s) (login expired or market closed?) "
                         "-- giving up on today, no trades were checked.")
        else:
            lines.append(f"No usable 09:30 quotes from Upstox (login expired or market closed?) -- retrying at the next slot, today is "
                         f"not lost (attempt {st['entry_attempts']}/{MAX_ENTRY_ATTEMPTS}).")
        _save_state(st)
        return
    st["entered_on"] = str(today)
    st["last_check"] = {"candidates": len(cands), "market_gap": mkt, "signals": [b["symbol"] for b in buys]}
    if not buys:
        lines.append(f"No trade today: {len(cands)} stocks had results, none opened 7%+ above the market (market gap {mkt:+.2%}) and kept rising.")
        _save_state(st)
        return
    late = now.time() > LATE_AFTER                              # a retried entry: the gap is still 09:15 vs prev close, the price is later
    # Context recorded on every buy, NOT a filter (2026-10-29): in the fair 2022-26 test, buys on mornings when Nifty fell 09:15->09:30
    # averaged +2.0%/trade vs -0.5% when it rose (t 2.4, one of ~15 filters tried, unstable to the cut-off -> unproven). Live trades decide.
    nq = raw.get(NIFTY_KEY) or {}
    mkt15 = round(nq["ltp"] / nq["today_open"] - 1, 5) if nq.get("ltp") and nq.get("today_open") else None
    if mkt15 is not None:
        lines.append(f"Nifty 09:15->now {mkt15:+.2%} (buys on falling mornings did better in the backtest; recorded, not used)")
    cap = float(db.get_account(ACCOUNT)["current_capital"])
    share = cap / len(buys)                                     # each signal's slice of the account
    for b in buys:
        allowed = stock_leverage(broker, keys[b["symbol"]], b["ltp"])
        lev = S.LEVERAGE if allowed is None else min(S.LEVERAGE, allowed)
        if allowed is None:
            lines.append(f"{b['symbol']}: Upstox margin check unavailable -- sized at the configured {S.LEVERAGE:g}x")
        elif allowed < S.LEVERAGE:
            lines.append(f"{b['symbol']}: Upstox allows only {allowed:g}x on this stock -- sized at {lev:g}x, not {S.LEVERAGE:g}x")
        qty = int(share * lev // b["ltp"])
        if qty < 1:
            lines.append(f"{b['symbol']}: one share costs more than its share of the money -- skipped")
            continue
        pid = f"POS_RG_{now:%Y%m%d}_{b['symbol']}"
        db.open_position(pid, b["symbol"], "long", qty, b["ltp"], 0.0, 0.0, b["ltp"], account_id=ACCOUNT, instrument_key=keys[b["symbol"]],
                         strategy=NAME, state=json.dumps({"gap_vs_mkt": b["gap_vs_mkt"], "open": b["open"], "prev_close": b["prev_close"],
                                                          "entry_slot": f"{now:%H:%M}", "entry_date": str(today), "late": late,
                                                          "mkt_first15": mkt15, "runup20": _runup(b["symbol"], today),
                                                          "leverage": lev, "upstox_leverage": allowed}))
        lines.append(f"BOUGHT {b['symbol']} {qty} @ {b['ltp']:,.2f} = {_inr(qty * b['ltp'])} (opened {b['gap_vs_mkt']:+.1%} vs market after results, "
                     f"still rising at {now:%H:%M})"
                     + ("  [DELAYED: the 09:30 quote fetch failed, so this is the 09:30 rule applied at a later price]" if late else ""))
    _save_state(st)


def entry_candle_open(broker, key: str, slot: str) -> float | None:
    """Today's open of the 5-minute candle starting at `slot` (e.g. "09:30") -- the price the backtest assumes we bought at."""
    get = getattr(broker, "get_intraday_candles", None)
    if get is None or not key:
        return None
    try:
        rows = get(key, "minutes", 5) or []
    except Exception:
        return None
    for r in rows:
        if str(r.get("timestamp", ""))[11:16] == slot and r.get("open"):
            return float(r["open"])
    return None


def _record_fill(db, broker, p: dict, now: datetime, lines: list) -> None:
    """Fill tracking (2026-10-29): how far our buy was from the entry candle's open. The backtest's profit depends on buying close to
    that open (filling at the candle's high instead cut 2025-26 from Rs7.3L to Rs1.6L at 5x), so every sale says how we did."""
    try:
        st = json.loads(p.get("state") or "{}")
    except ValueError:
        return
    if st.get("entry_date", str(p.get("entry_time", ""))[:10]) != str(now.date()):   # a missed exit sold another day: today's candles say nothing
        return
    o = entry_candle_open(broker, p.get("instrument_key"), st.get("entry_slot", "09:30"))
    if not o:
        return
    pct = round((float(p["entry_price"]) / o - 1) * 100, 3)
    db.update_position_stop(p["position_id"], float(p.get("current_stop") or 0), float(p.get("best_price") or p["entry_price"]), False,
                            state_updates={"open_0930": o, "fill_vs_open_pct": pct})
    lines.append(f"{p['symbol']}: bought {pct:+.2f}% vs the {st.get('entry_slot', '09:30')} open ({o:,.2f})")


def _close(db, broker, p: dict, now: datetime, reason: str, lines: list) -> bool:
    px = (broker.get_ltp_batch([p["instrument_key"]]) or {}).get(p["instrument_key"]) if p.get("instrument_key") else None
    if not px:
        lines.append(f"{p['symbol']}: no live price -- kept open, will retry")
        return False
    _record_fill(db, broker, p, now, lines)
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
        try:                                                          # yesterday's turnover for the liquid-stock list
            b = S.refresh_bhavcopy(now.date())
        except Exception as e:
            b = f"failed ({type(e).__name__}; the list uses the files already held)"
        return [f"results calendar refreshed: {n} results meetings in the last/next 7 days; NSE daily files added: {b}; "
                f"{len(S.universe(now.date()))} liquid stocks today"]
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
