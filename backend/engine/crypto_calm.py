"""Paper trading for crypto/calm_coins (CoinDCX futures on BTC ETH SOL DOGE TRX while the crypto market is calm). Its own paper account CRYPTO_CALM.

    python3 -m engine.crypto_calm            # top up the data, act for today's weekday part, mark to market (Telegram)
    python3 -m engine.crypto_calm --status   # print the account, no trading

Run by hft-crypto-calm.timer every day at 05:45 IST (00:15 UTC, just after the daily candle closes; crypto trades every day). No real orders:
prices come from CoinDCX's public futures price feed, fills are recorded in var/db/paper_trading.db under account CRYPTO_CALM.
Equal money per coin, each coin's money split into 7 parts (as in the backtest). Each day the part for today's UTC weekday re-decides from
yesterday's completed candles: market calm -> hold the coin (buy if not held), wild -> cash (sell if held). Prices are in USDT; profit is
turned into rupees at CoinDCX's USDT/INR price when it is booked. Costs: 0.05% fee + 18% GST and 0.05% slippage per side, the real daily
funding a long pays. Profit is booked BEFORE tax (India has not settled how crypto futures are taxed); the report shows the after-tax
value under both readings: strict crypto rule (31.2% of every winning trade, losses never set off) and business income (31.2% of each
financial year's net profit, losses carried 4 years). Settings (.env): CRYPTO_CALM_CAPITAL
(default Rs3 lakh: below that a BTC part is smaller than CoinDCX's 0.001 BTC minimum order, so BTC's share sits in cash), CRYPTO_CALM_LEVERAGE (default 1 = the sweet spot; 1.5x deepened BTC's worst fall from -36% to -60% for almost no extra profit).
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
from markets.crypto.strategies.tf_daily.calm_coins import strategy as S

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT, NAME = "CRYPTO_CALM", "calm_coins"
STATE = DB_DIR / "crypto_calm_state.json"
FEE = 0.0005 * 1.18                     # CoinDCX futures taker fee + GST, per side
SLIP = 0.0005                           # slippage per side (S.FEE_SIDE = FEE + SLIP)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def pair(coin: str) -> str:
    return f"B-{coin}_USDT"


def _inr(v: float) -> str:
    from services.utils.telegram import inr
    return ("−" if v < 0 else "") + "₹" + inr(v)


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def live_prices() -> dict:
    """Each traded coin's futures last price (CoinDCX public feed) and USDT/INR (CoinDCX ticker). Missing values on failure."""
    out = {"price": {}, "usdtinr": None, "at": None}
    try:
        p = requests.get("https://public.coindcx.com/market_data/v3/current_prices/futures/rt", timeout=15).json()
        for c in S.TRADED:
            q = (p.get("prices") or {}).get(pair(c)) or {}
            if q.get("ls"):
                out["price"][c] = float(q["ls"])
        out["at"] = datetime.fromtimestamp(int(p["ts"]) / 1000, IST).isoformat(timespec="seconds") if p.get("ts") else None
    except Exception:
        pass
    try:
        t = next(x for x in requests.get("https://api.coindcx.com/exchange/ticker", timeout=15).json() if x.get("market") == "USDTINR")
        out["usdtinr"] = float(t["last_price"])
    except Exception:
        pass
    return out


def value_if_sold(p: dict, price: float, usdtinr: float) -> dict:
    """What selling an open position at `price` books, in rupees: gross, fees (both sides + slippage), funding paid, 31.2% tax (S.TAX) on a gain."""
    st = json.loads(p.get("state") or "{}")
    q, e = float(p["qty"]), float(p["entry_price"])
    gross = q * (price - e) * (-1 if p.get("direction") == "short" else 1)     # trend_breakout also sells short
    fee = q * (e + price) * FEE; slip = q * (e + price) * SLIP; fund = float(st.get("funding_usdt", 0.0))
    before = (gross - fee - slip - fund) * usdtinr
    taxable = (gross - fee - slip) * usdtinr                         # funding is not deducted (as in the backtest: the cautious reading)
    tax = S.TAX * taxable if taxable > 0 else 0.0
    return {"gross": gross * usdtinr, "fees": fee * usdtinr, "slippage": slip * usdtinr, "funding": fund * usdtinr, "tax": tax, "net": before - tax,
            "total": (fee + slip + fund) * usdtinr + tax}


def _fy(d) -> int:
    """Indian financial year, named by the year it ends (April 2026 - March 2027 -> 2027)."""
    return d.year + (1 if d.month >= 4 else 0)


def business_value(st: dict, capital0: float, open_pretax: float, today) -> tuple[float, float]:
    """The account under the BUSINESS-income reading: before-tax booked profit of each financial year (kept in the state, 'pretax_fy'),
    the open positions' before-tax value counted in the current year, 31.2% on each year's net profit, losses carried 4 years.
    Returns (account value, tax)."""
    fy_net = {int(k): v for k, v in st.get("pretax_fy", {}).items()}
    cur = _fy(today); fy_net[cur] = fy_net.get(cur, 0.0) + open_pretax
    carry, tax = [], 0.0
    for y in sorted(fy_net):
        p = fy_net[y]
        if p > 0:
            use = 0.0
            for c in carry:
                take = min(c[1], p - use); c[1] -= take; use += take
            tax += S.TAX * (p - use)
        elif p < 0:
            carry.append([y, -p])
        carry = [c for c in carry if c[1] > 0 and y - c[0] < 4]
    return capital0 + sum(fy_net.values()) - tax, tax


def account_states() -> dict:
    """Both sleeves' tax records merged (calm_coins + trend_breakout share the CRYPTO_CALM account)."""
    from engine.crypto_breakout import _state as bstate
    a, b = _state(), bstate()
    fy = dict(a.get("pretax_fy", {}))
    for k, v in b.get("pretax_fy", {}).items():
        fy[k] = fy.get(k, 0.0) + v
    return {"pretax_fy": fy, "strict_tax": a.get("strict_tax", 0.0) + b.get("strict_tax", 0.0)}


def sleeve_capitals() -> tuple[float, float]:
    return float(os.environ.get("CRYPTO_CALM_CAPITAL", 100_000)), float(os.environ.get("CRYPTO_BREAKOUT_CAPITAL", 100_000))


def init_account(db) -> None:
    calm, brk = sleeve_capitals()
    db.init_account(ACCOUNT, name="Crypto (calm_coins + trend_breakout, CoinDCX futures)", capital=calm + brk, leverage=1.0, risk_pct=0.0)


def booked_total() -> float:
    """Booked money of both sleeves (before tax)."""
    from engine.crypto_breakout import _state as bstate
    calm, brk = sleeve_capitals()
    a, b = _state(), bstate()
    pa = sum(sum(v) for v in a["parts"].values()) if isinstance(a.get("parts"), dict) else calm
    pb = sum(b["slots"].values()) if isinstance(b.get("slots"), dict) else brk
    return pa + pb


def _other_booked() -> float:
    from engine.crypto_breakout import _state as bstate
    b = bstate()
    return sum(b["slots"].values()) if isinstance(b.get("slots"), dict) else sleeve_capitals()[1]


def _mine(db) -> list:
    return [p for p in db.get_open_positions(ACCOUNT) if p.get("strategy") == NAME]


def _set_state(db, position_id: str, state: dict) -> None:
    with db._get_conn() as conn:
        conn.execute("UPDATE positions SET state = ? WHERE position_id = ?", (json.dumps(state), position_id))


def run(db, now: datetime, report: bool = True, send=None) -> dict:
    from services.utils import telegram
    send = send or telegram.send
    capital0 = sleeve_capitals()[0]                                       # this sleeve's money (calm_coins); trend_breakout has its own
    lev = float(os.environ.get("CRYPTO_CALM_LEVERAGE", 1.0))
    init_account(db)
    acc = db.get_account(ACCOUNT)
    st = _state()
    parts = st.get("parts") if isinstance(st.get("parts"), dict) else {c: [capital0 / len(S.TRADED) / S.PARTS] * S.PARTS for c in S.TRADED}
    lines, now_s = [], now.isoformat()
    top = cdata.topup()
    lines += [f"⚠️ data: {e}" for e in top["errors"]]
    d = S.load()
    today = pd.Timestamp(now.astimezone(timezone.utc).date())
    last = d["close"].index[-1]
    mv = S.market_vol(d["close"])
    live = live_prices()
    fx = live["usdtinr"]

    through = pd.Timestamp(st.get("funding_through", "1970-01-01"))       # funding a long paid on completed days, added to each held part
    for p in _mine(db):
        ps = json.loads(p.get("state") or "{}")
        since = max(through, pd.Timestamp(ps["entry_day"]) - pd.Timedelta(days=1))
        days = d["close"].index[(d["close"].index > since) & (d["close"].index <= last)]
        ps["funding_usdt"] = ps.get("funding_usdt", 0.0) + float(sum(p["qty"] * d["close"][p["symbol"]].at[x] * d["funding"][p["symbol"]].at[x] for x in days))
        _set_state(db, p["position_id"], ps)
    st["funding_through"] = str(last.date())

    k = today.weekday(); did = None
    vol = float(mv.at[last]); calm = vol < S.CALM_THRESHOLD
    why = f"market volatility {vol:.2%} {'below' if calm else 'at or above'} {S.CALM_THRESHOLD:.2%}"
    if last != today - pd.Timedelta(days=1):
        lines.append(f"⚠️ last completed candle is {last.date()}, expected {(today - pd.Timedelta(days=1)).date()}: no decision today")
    elif st.get("decided", {}).get(str(k)) == str(today.date()):
        pass                                                                # today's parts already decided (a re-run)
    elif not fx:
        lines.append("⚠️ no USDT/INR price: no decision today")
    else:
        held = {}
        for p in _mine(db):
            ps = json.loads(p.get("state") or "{}")
            if ps.get("part") == k:
                held.setdefault(p["symbol"], []).append(p)
        acted = []
        for coin in S.TRADED:
            px = live["price"].get(coin)
            if not px:
                lines.append(f"⚠️ {coin}: no live price, part {k + 1} left as it is")
                continue
            mine = held.get(coin, [])
            if mine and not calm:
                for p in mine:
                    c = value_if_sold(p, px, fx)
                    pre = c["net"] + c["tax"]                                # booked BEFORE tax; tax is estimated separately (rule unsettled)
                    parts[coin][k] += pre
                    fyk = str(_fy(now.date())); fyd = st.setdefault("pretax_fy", {}); fyd[fyk] = fyd.get(fyk, 0.0) + pre
                    st["strict_tax"] = st.get("strict_tax", 0.0) + c["tax"]  # what the strict crypto rule would charge on this trade
                    db.close_position(p["position_id"], px, "market_wild", c["gross"], pre, c["total"] - c["tax"])
                    hold = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
                    db.record_trade(p["position_id"], coin, "long", p["qty"], p["entry_price"], px, p["entry_time"], now_s, hold, "market_wild", c["gross"],
                                    {"brokerage": c["fees"] / 1.18, "gst": c["fees"] - c["fees"] / 1.18, "slippage": c["slippage"], "total": c["total"] - c["tax"]},
                                    pre, sum(sum(v) for v in parts.values()) + _other_booked(), account_id=ACCOUNT, strategy=NAME)
                    acted.append(f"• SOLD {coin} {p['qty']:g} @ {p['entry_price']:,.4g} → {px:,.4g} USDT: {'+' if pre >= 0 else ''}{_inr(pre)} "
                                 f"after fees and funding {_inr(c['funding'])}, before tax (strict-rule tax on it: {_inr(c['tax'])})")
                did = "sell"
            elif not mine and calm:
                q = S.order_qty(coin, parts[coin][k] * lev, px, fx)        # CoinDCX's quantity step and smallest order
                if q <= 0:
                    step, min_usdt = S.LOT[coin]
                    acted.append(f"• {coin}: part {k + 1} has {_inr(parts[coin][k])}, below CoinDCX's smallest order "
                                 f"({_inr(max(step * px, min_usdt) * fx)}) -- kept in cash")
                    continue
                pid = f"POS_CRYPTO_{now.strftime('%Y%m%d_%H%M%S')}_{coin}_P{k}"
                ps = {"part": k, "entry_day": str(today.date()), "usdtinr_entry": fx, "funding_usdt": 0.0, "margin_inr": round(parts[coin][k], 2), "leverage": lev}
                db.open_position(pid, coin, "long", q, px, 0.0, 0.0, 0.0, account_id=ACCOUNT, instrument_key=pair(coin), strategy=NAME, state=json.dumps(ps))
                acted.append(f"• BOUGHT {coin} {q:g} @ {px:,.4g} USDT = {_inr(q * px * fx)}")
                did = "buy"
        lines.append(f"Part {k + 1}/7 ({DAYS[k]}) of each coin: {why} → " + ("hold" if calm else "cash") + ("" if acted else " (no change)"))
        lines += acted
        st.setdefault("decided", {})[str(k)] = str(today.date())
    st["parts"] = parts
    booked = sum(sum(v) for v in parts.values())
    STATE.write_text(json.dumps(st, indent=1))
    db.update_capital(ACCOUNT, booked_total())                           # both sleeves
    STATE.write_text(json.dumps(st, indent=1))

    open_pos = _mine(db)
    open_net, open_pre, per = 0.0, 0.0, {}
    for p in open_pos:
        px = live["price"].get(p["symbol"])
        if px and fx:
            c = value_if_sold(p, px, fx); open_net += c["net"]; open_pre += c["net"] + c["tax"]
            per.setdefault(p["symbol"], [0, 0.0]); per[p["symbol"]][0] += 1; per[p["symbol"]][1] += c["net"]
    rows = [f"• {c}: {n} of 7 parts held, if sold now <b>{'+' if v >= 0 else ''}{_inr(v)}</b> (strict-rule after tax)" for c, (n, v) in per.items()]
    db.record_snapshot(ACCOUNT)
    value = booked + open_pre                                             # before tax
    strict = booked - st.get("strict_tax", 0.0) + open_net              # strict crypto rule: 31.2% of every winning trade
    biz, _ = business_value(st, capital0, open_pre, now.date())        # this sleeve only
    msg = (f"₿ <b>CRYPTO CALM COINS</b> ({now:%d %b %H:%M} IST) · {why}\n"
           + ("\n".join(lines) + "\n\n" if lines else "")
           + f"Account value if all sold now, after fees and funding: <b>{_inr(value)}</b> before tax (calm_coins sleeve, started {_inr(capital0)}; booked {_inr(booked)})\n"
           + f"After tax: <b>{_inr(strict)}</b> strict crypto rule (31.2% of every winning trade) · <b>{_inr(biz)}</b> business-income rule (31.2% of the year's net)\n"
           + f"{len(open_pos)} of {len(S.TRADED) * S.PARTS} parts hold a coin" + (f" · USDT/INR {fx:.2f}" if fx else "") + "\n" + "\n".join(rows))
    if report or did:
        send(msg)
    return {"did": did, "value": value, "strict_value": strict, "business_value": biz, "lines": lines, "message": msg}


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    from engine.database import TradingDB
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="print the account only")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    db = TradingDB()
    if a.status:
        print(db.get_account(ACCOUNT), *_mine(db), _state(), sep="\n")
        return 0
    out = run(db, datetime.now(IST), report=True, send=(print if a.no_telegram else None))
    print(out["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
