"""Paper trading for crypto/calm_btc (CoinDCX BTC futures while the crypto market is calm). Its own paper account CRYPTO_CALM.

    python3 -m engine.crypto_calm            # top up the data, act for today's weekday part, mark to market (Telegram)
    python3 -m engine.crypto_calm --status   # print the account, no trading

Run by hft-crypto-calm.timer every day at 05:45 IST (00:15 UTC, just after the daily candle closes; crypto trades every day). No real orders:
prices come from CoinDCX's public futures price feed, fills are recorded in var/db/paper_trading.db under account CRYPTO_CALM.
The money is split into 7 parts (as in the backtest). Each day the part for today's UTC weekday re-decides from yesterday's completed candles:
market calm -> hold BTC (buy if not held), wild -> cash (sell if held). Prices are in USDT; profit is turned into rupees at CoinDCX's
USDT/INR price when it is booked. Costs: 0.05% fee + 18% GST and 0.05% slippage per side, the real daily funding a long pays, and 30% tax
on each winning trade (losses not set off), exactly like the backtest. Settings (.env): CRYPTO_CALM_CAPITAL (default Rs1 lakh),
CRYPTO_CALM_LEVERAGE (default 1 = the sweet spot; 1.5x deepened the worst fall from -36% to -60% for almost no extra profit).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from core.paths import DB_DIR
from markets.crypto import data as cdata
from markets.crypto.strategies.tf_daily.calm_btc import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT, NAME = "CRYPTO_CALM", "calm_btc"
PAIR = f"B-{S.TRADE}_USDT"
STATE = DB_DIR / "crypto_calm_state.json"
FEE = 0.0005 * 1.18                     # CoinDCX futures taker fee + GST, per side
SLIP = 0.0005                           # slippage per side (S.FEE_SIDE = FEE + SLIP)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _inr(v: float) -> str:
    from services.utils.telegram import inr
    return ("−" if v < 0 else "") + "₹" + inr(v)


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def live_prices() -> dict:
    """BTC futures last price and funding rate (CoinDCX public feed) and USDT/INR (CoinDCX ticker). Empty values on failure."""
    out = {"price": None, "funding": None, "usdtinr": None, "at": None}
    try:
        p = requests.get("https://public.coindcx.com/market_data/v3/current_prices/futures/rt", timeout=15).json()
        q = (p.get("prices") or {}).get(PAIR) or {}
        out.update(price=float(q["ls"]) if q.get("ls") else None, funding=q.get("fr"),
                   at=datetime.fromtimestamp(int(p["ts"]) / 1000, IST).isoformat(timespec="seconds") if p.get("ts") else None)
    except Exception:
        pass
    try:
        t = next(x for x in requests.get("https://api.coindcx.com/exchange/ticker", timeout=15).json() if x.get("market") == "USDTINR")
        out["usdtinr"] = float(t["last_price"])
    except Exception:
        pass
    return out


def value_if_sold(p: dict, price: float, usdtinr: float) -> dict:
    """What selling an open position at `price` books, in rupees: gross, fees (both sides + slippage), funding paid, 30% tax on a gain."""
    st = json.loads(p.get("state") or "{}")
    q, e = float(p["qty"]), float(p["entry_price"])
    gross = q * (price - e)
    fee = q * (e + price) * FEE; slip = q * (e + price) * SLIP; fund = float(st.get("funding_usdt", 0.0))
    before = (gross - fee - slip - fund) * usdtinr
    tax = S.TAX * before if before > 0 else 0.0
    return {"gross": gross * usdtinr, "fees": fee * usdtinr, "slippage": slip * usdtinr, "funding": fund * usdtinr, "tax": tax, "net": before - tax,
            "total": (fee + slip + fund) * usdtinr + tax}


def run(db, now: datetime, report: bool = True, send=None) -> dict:
    from services.utils import telegram
    send = send or telegram.send
    capital0 = float(os.environ.get("CRYPTO_CALM_CAPITAL", 100_000))
    lev = float(os.environ.get("CRYPTO_CALM_LEVERAGE", 1.0))
    db.init_account(ACCOUNT, name="Crypto calm BTC (CoinDCX futures)", capital=capital0, leverage=lev, risk_pct=0.0)
    acc = db.get_account(ACCOUNT)
    st = _state()
    parts = st.get("parts") or [capital0 / S.PARTS] * S.PARTS             # each part's booked rupees
    lines, now_s = [], now.isoformat()
    top = cdata.topup()
    lines += [f"⚠️ data: {e}" for e in top["errors"]]
    d = S.load()
    today = pd.Timestamp(now.astimezone(timezone.utc).date())
    last = d["close"].index[-1]
    mv = S.market_vol(d["close"])
    live = live_prices()
    px, fx = live["price"], live["usdtinr"]

    open_pos = db.get_open_positions(ACCOUNT)
    through = pd.Timestamp(st.get("funding_through", "1970-01-01"))       # funding a long paid on completed days, added to each held part
    for p in open_pos:
        ps = json.loads(p.get("state") or "{}")
        since = max(through, pd.Timestamp(ps["entry_day"]) - pd.Timedelta(days=1))
        days = d["close"].index[(d["close"].index > since) & (d["close"].index <= last)]
        ps["funding_usdt"] = ps.get("funding_usdt", 0.0) + float(sum(p["qty"] * d["close"][S.TRADE].at[x] * d["funding"].at[x] for x in days))
        _set_state(db, p["position_id"], ps)
    st["funding_through"] = str(last.date())

    k = today.weekday(); did = None
    if last != today - pd.Timedelta(days=1):
        lines.append(f"⚠️ last completed candle is {last.date()}, expected {(today - pd.Timedelta(days=1)).date()}: no decision today")
    elif st.get("decided", {}).get(str(k)) == str(today.date()):
        pass                                                                # this part already decided today (a re-run)
    elif not (px and fx):
        lines.append("⚠️ no live BTC price or USDT/INR: no decision today")
    else:
        vol = float(mv.at[last]); calm = vol < S.CALM_THRESHOLD
        mine = [p for p in db.get_open_positions(ACCOUNT) if json.loads(p.get("state") or "{}").get("part") == k]
        why = f"market volatility {vol:.2%} {'below' if calm else 'at or above'} {S.CALM_THRESHOLD:.2%}"
        if mine and not calm:
            for p in mine:
                c = value_if_sold(p, px, fx)
                parts[k] += c["net"]
                db.close_position(p["position_id"], px, "market_wild", c["gross"], c["net"], c["total"])
                hold = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
                db.record_trade(p["position_id"], S.TRADE, "long", p["qty"], p["entry_price"], px, p["entry_time"], now_s, hold, "market_wild", c["gross"],
                                {"brokerage": c["fees"] / 1.18, "gst": c["fees"] - c["fees"] / 1.18, "slippage": c["slippage"], "total": c["total"]},
                                c["net"], sum(parts), account_id=ACCOUNT, strategy=NAME)
                lines.append(f"• part {k + 1}/7 ({DAYS[k]}) SOLD {p['qty']:.6f} BTC {p['entry_price']:,.1f} → {px:,.1f} USDT: "
                             f"{'+' if c['net'] >= 0 else ''}{_inr(c['net'])} after fees, funding {_inr(c['funding'])} and tax {_inr(c['tax'])} ({why})")
            did = "sell"
        elif not mine and calm:
            q = round(parts[k] * lev / (px * fx), 6)
            pid = f"POS_CRYPTO_{now.strftime('%Y%m%d_%H%M%S')}_P{k}"
            ps = {"part": k, "entry_day": str(today.date()), "usdtinr_entry": fx, "funding_usdt": 0.0, "margin_inr": round(parts[k], 2), "leverage": lev}
            db.open_position(pid, S.TRADE, "long", q, px, 0.0, 0.0, 0.0, account_id=ACCOUNT, instrument_key=PAIR, strategy=NAME, state=json.dumps(ps))
            lines.append(f"• part {k + 1}/7 ({DAYS[k]}) BOUGHT {q:.6f} BTC @ {px:,.1f} USDT = {_inr(q * px * fx)} ({why})")
            did = "buy"
        else:
            lines.append(f"• part {k + 1}/7 ({DAYS[k]}): {'keeps BTC' if mine else 'stays in cash'} ({why})")
        st.setdefault("decided", {})[str(k)] = str(today.date())
    st["parts"] = parts
    db.update_capital(ACCOUNT, sum(parts))
    STATE.write_text(json.dumps(st, indent=1))

    open_pos = db.get_open_positions(ACCOUNT)
    open_net, rows = 0.0, []
    for p in open_pos:
        part = json.loads(p.get("state") or "{}").get("part", 0)
        if px and fx:
            c = value_if_sold(p, px, fx); open_net += c["net"]
            rows.append(f"• part {part + 1} ({DAYS[part]}): {p['qty']:.6f} BTC @ {p['entry_price']:,.1f} → {px:,.1f}: if sold now <b>{'+' if c['net'] >= 0 else ''}{_inr(c['net'])}</b>")
    db.record_snapshot(ACCOUNT)
    value = sum(parts) + open_net
    msg = (f"₿ <b>CRYPTO CALM BTC</b> ({now:%d %b %H:%M} IST) · market volatility {float(mv.at[last]):.2%} (calm below {S.CALM_THRESHOLD:.2%})\n"
           + ("\n".join(lines) + "\n\n" if lines else "")
           + f"Account value: <b>{_inr(value)}</b> if all sold now, after fees and tax (started {_inr(float(acc['initial_capital']))}; booked {_inr(sum(parts))})\n"
           + f"{len(open_pos)} of 7 parts hold BTC" + (f" · BTC {px:,.0f} USDT · USDT/INR {fx:.2f}" if px and fx else "") + "\n" + "\n".join(rows))
    if report or did:
        send(msg)
    return {"did": did, "value": value, "lines": lines, "message": msg}


def _set_state(db, position_id: str, state: dict) -> None:
    with db._get_conn() as conn:
        conn.execute("UPDATE positions SET state = ? WHERE position_id = ?", (json.dumps(state), position_id))


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    from engine.database import TradingDB
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="print the account only")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    db = TradingDB()
    if a.status:
        print(db.get_account(ACCOUNT), *db.get_open_positions(ACCOUNT), _state(), sep="\n")
        return 0
    out = run(db, datetime.now(IST), report=True, send=(print if a.no_telegram else None))
    print(out["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
