"""Paper trading for crypto/trend_breakout (4-hour trend breakout on BTC, ETH, SOL) -- the second sleeve of the CRYPTO_CALM account.

    python3 -m engine.crypto_breakout            # process the 4-hour candles finished since the last run, then report actions (Telegram)
    python3 -m engine.crypto_breakout --status   # print this sleeve, no trading

Run by hft-crypto-breakout.timer 2 minutes after every 4-hour candle closes (00/04/08/12/16/20 UTC = 05:32, 09:32 ... IST). No real orders.
The account (CRYPTO_CALM, Rs2L since 2026-10-17) holds two sleeves of money: calm_coins (engine/crypto_calm.py, CRYPTO_CALM_CAPITAL,
default Rs1L) and this one (CRYPTO_BREAKOUT_CAPITAL, default Rs1L), split equally over BTC, ETH and SOL, each coin's share compounding.
Rules = markets/crypto/strategies/tf_4hour/trend_breakout (same constants): on a finished 4-hour candle, close above the previous ENTRY_BARS
candles' high (by BREAKOUT_BUFFER) and above the 100 EMA -> buy at the live CoinDCX price (shorts mirrored); exit when a candle reaches the previous 10
candles' low (high for shorts) or the 8% stop -- treated as a resting stop order: filled at that level, or at the candle's open if it
opened past it -- or after 30 days. Candles: Binance USDT-M futures 4-hour klines (CoinDCX's futures follow Binance); entries priced at
CoinDCX's live futures price. Costs: CoinDCX taker 0.059% + slippage per side, real daily funding (longs pay, shorts receive). Profit is
booked before tax; the strict-rule tax and the business-income view are tracked like the calm sleeve.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from core.paths import DB_DIR
from engine import crypto_calm as CC
from markets.crypto.strategies.tf_4hour.trend_breakout import strategy as TB
from markets.crypto.strategies.tf_daily.calm_coins import strategy as CALM

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT, NAME = CC.ACCOUNT, "trend_breakout"
COINS = ("BTC", "ETH", "SOL")
STATE = DB_DIR / "crypto_breakout_state.json"
BAR_MS = 4 * 3600 * 1000


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def bars(coin: str, limit: int = 500) -> pd.DataFrame:
    """Finished 4-hour candles (Binance USDT-M futures), oldest first."""
    r = requests.get("https://fapi.binance.com/fapi/v1/klines", params={"symbol": f"{coin}USDT", "interval": "4h", "limit": limit}, timeout=30)
    r.raise_for_status()
    k = pd.DataFrame(r.json()).iloc[:, :7]
    k.columns = ["t", "o", "h", "l", "c", "v", "close_t"]
    k = k.astype({"o": float, "h": float, "l": float, "c": float})
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return k[k.close_t < now_ms].reset_index(drop=True)


def _levels(k: pd.DataFrame) -> pd.DataFrame:
    k = k.copy()
    k["hi"] = k.h.rolling(TB.ENTRY_BARS).max().shift(1); k["lo"] = k.l.rolling(TB.ENTRY_BARS).min().shift(1)
    k["xh"] = k.h.rolling(TB.EXIT_BARS).max().shift(1); k["xl"] = k.l.rolling(TB.EXIT_BARS).min().shift(1)
    k["ema"] = k.c.ewm(span=TB.EMA_LEN, adjust=False).mean()
    k["ema_slope"] = k["ema"].diff(3)
    return k


def _funding(coin: str, d0: pd.Timestamp, d1: pd.Timestamp) -> float:
    f = TB._funding(coin)
    return float(f.loc[d0:d1].sum()) if len(f) else 0.0


def _close(db, p: dict, px: float, why: str, now: datetime, st: dict, fx: float, lines: list) -> None:
    ps = json.loads(p.get("state") or "{}"); d = 1 if p["direction"] == "long" else -1
    d0 = pd.Timestamp(ps["entry_day"]); d1 = pd.Timestamp(now.astimezone(timezone.utc).date())
    ps["funding_usdt"] = d * _funding(p["symbol"], d0, d1) * p["qty"] * float(p["entry_price"])   # paid (+) or received (-)
    p = dict(p, state=json.dumps(ps))
    c = CC.value_if_sold(p, px, fx)
    pre = c["net"] + c["tax"]
    st["slots"][p["symbol"]] += pre
    fyk = str(CC._fy(now.date())); fyd = st.setdefault("pretax_fy", {}); fyd[fyk] = fyd.get(fyk, 0.0) + pre
    st["strict_tax"] = st.get("strict_tax", 0.0) + c["tax"]
    db.close_position(p["position_id"], px, why, c["gross"], pre, c["total"] - c["tax"])
    hold = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
    calm_parts = CC._state().get("parts")
    calm_booked = sum(sum(v) for v in calm_parts.values()) if isinstance(calm_parts, dict) else CC.sleeve_capitals()[0]
    db.record_trade(p["position_id"], p["symbol"], p["direction"], p["qty"], p["entry_price"], px, p["entry_time"], now.isoformat(), hold, why, c["gross"],
                    {"brokerage": c["fees"] / 1.18, "gst": c["fees"] - c["fees"] / 1.18, "slippage": c["slippage"], "total": c["total"] - c["tax"]},
                    pre, calm_booked + sum(st["slots"].values()), account_id=ACCOUNT, strategy=NAME)
    lines.append(f"• CLOSED {p['direction'].upper()} {p['symbol']} {p['qty']:g} {p['entry_price']:,.4g} → {px:,.4g} USDT: "
                 f"{'+' if pre >= 0 else ''}{CC._inr(pre)} before tax ({why})")


def run(db, now: datetime, send=None) -> dict:
    from services.utils import telegram
    send = send or telegram.send
    CC.init_account(db)
    capital0 = CC.sleeve_capitals()[1]
    st = _state()
    st.setdefault("slots", {c: capital0 / len(COINS) for c in COINS}); st.setdefault("last_bar", {})
    live = CC.live_prices(); fx = live["usdtinr"]; lines = []
    for coin in COINS:
        try:
            k = _levels(bars(coin))
        except Exception as e:
            lines.append(f"⚠️ {coin}: no 4-hour candles ({e})"); continue
        last_done = st["last_bar"].get(coin, int(k.t.iloc[-2]))                 # first run: act on the newest finished candle only
        new = k[k.t > last_done]
        if not fx:
            lines.append("⚠️ no USDT/INR price: no action"); break
        for idx in new.index:
            b = k.loc[idx]
            pos = [p for p in db.get_open_positions(ACCOUNT) if p.get("strategy") == NAME and p["symbol"] == coin]
            closed_now = False
            if pos:                                                               # 1) does this candle reach the exit or the stop?
                p = pos[0]; ps = json.loads(p.get("state") or "{}"); d = 1 if p["direction"] == "long" else -1; stop = ps["stop"]
                lvl = b.xl if d == 1 else b.xh; px, why = None, None
                if d == 1:
                    if b.l <= lvl and lvl > stop: px, why = min(b.o, lvl), f"{TB.EXIT_BARS}-candle low reached"
                    elif b.l <= stop: px, why = min(b.o, stop), f"{TB.STOP_LOSS_PCT:.0%} stop"
                else:
                    if b.h >= lvl and lvl < stop: px, why = max(b.o, lvl), f"{TB.EXIT_BARS}-candle high reached"
                    elif b.h >= stop: px, why = max(b.o, stop), f"{TB.STOP_LOSS_PCT:.0%} stop"
                if px is None and (b.t - ps["entry_bar"]) / BAR_MS >= 179:
                    px, why = b.c, "30 days held"
                if px is not None:
                    _close(db, p, float(px), why, now, st, fx, lines); closed_now = True
            if not pos and not closed_now and idx == k.index[-1]:                   # 2) a signal on the newest finished candle -> enter now
                buf = getattr(TB, "BREAKOUT_BUFFER", 0.0)                           # same 0.3% buffer as the backtest
                is_long = (b.c > b.hi * (1 + buf)) and (b.c > b.ema) and (b.ema_slope > 0)      # same rules as the backtest (no ADX)
                is_short = (b.c < b.lo * (1 - buf)) and (b.c < b.ema) and (b.ema_slope < 0)
                d = 1 if is_long else (-1 if is_short else 0)
                px = live["price"].get(coin)
                if d and px:
                    q = CALM.order_qty(coin, st["slots"][coin], px, fx)
                    if q <= 0:
                        lines.append(f"• {coin}: signal, but {CC._inr(st['slots'][coin])} is below CoinDCX's smallest order"); continue
                    stop = px * (1 - d * TB.STOP_LOSS_PCT)
                    ps = {"sleeve": NAME, "entry_bar": int(b.t) + BAR_MS, "entry_day": str(now.astimezone(timezone.utc).date()), "stop": stop,
                          "usdtinr_entry": fx, "funding_usdt": 0.0, "margin_inr": round(st["slots"][coin], 2)}
                    db.open_position(f"POS_CRYPTOBRK_{now.strftime('%Y%m%d_%H%M%S')}_{coin}", coin, "long" if d == 1 else "short", q, px, stop, 0.0, 0.0,
                                     account_id=ACCOUNT, instrument_key=CC.pair(coin), strategy=NAME, state=json.dumps(ps))
                    lines.append(f"• {'BOUGHT' if d == 1 else 'SHORTED'} {coin} {q:g} @ {px:,.4g} USDT = {CC._inr(q * px * fx)} "
                                 f"(4h close {b.c:,.4g} {f'above the {TB.ENTRY_BARS}-candle high' if d == 1 else f'below the {TB.ENTRY_BARS}-candle low'}; stop {stop:,.4g})")
        st["last_bar"][coin] = int(k.t.iloc[-1])
    STATE.write_text(json.dumps(st, indent=1))
    db.update_capital(ACCOUNT, CC.booked_total())
    db.record_snapshot(ACCOUNT)
    if lines:
        open_ = [p for p in db.get_open_positions(ACCOUNT) if p.get("strategy") == NAME]
        msg = (f"📈 <b>CRYPTO TREND BREAKOUT</b> ({now:%d %b %H:%M} IST)\n" + "\n".join(lines) +
               f"\nSleeve booked {CC._inr(sum(st['slots'].values()))} (started {CC._inr(capital0)}); open: "
               + (", ".join(f"{p['direction']} {p['symbol']}" for p in open_) or "none"))
        send(msg)
    return {"lines": lines, "slots": st["slots"]}


def main(argv=None) -> int:
    import engine.config  # noqa: F401  (loads backend/.env)
    from engine.database import TradingDB
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true"); ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    db = TradingDB()
    if a.status:
        print(_state(), *[p for p in db.get_open_positions(ACCOUNT) if p.get("strategy") == NAME], sep="\n"); return 0
    out = run(db, datetime.now(IST), send=(print if a.no_telegram else None))
    print("\n".join(out["lines"]) or "no change", out["slots"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
