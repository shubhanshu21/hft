"""Paper trading for two Nifty weekly-option strategies (2026-10-15, user: "should u add it" -> yes). Its own paper account OPTIONS_PAPER
(OPTIONS_PAPER_CAPITAL, default Rs3 lakh), kept apart from the other accounts. No real orders: live Upstox quotes, fills recorded in
var/db/paper_trading.db.

    python3 -m engine.options_paper            # run for today's session (09:20-15:25 IST), then exit -- started by hft-options-paper.timer
    python3 -m engine.options_paper --status   # print the account

The rules are exactly the backtest's (scratchpad opt_family.py, real expired option prices Oct 2024 - Oct 2026):
  * expiry-day condor ("expiry_condor"): on an expiry day at 09:30 sell the calls / puts 1% above / below Nifty, buy protection 2% away; close
    when the loss reaches 1.5x the credit, else at 15:15. Lots = 90% of the account / Upstox's basket margin for one lot.
    Backtest 2026 at Rs3L: +Rs9,892 (27 trades, 18 won), worst fall -2.5%.
  * directional buying ("directional_buy"): on the first trading day after an expiry at 10:00, if Nifty is above the last 5 days' high buy the
    at-the-money call, below their low buy the put, else nothing; sell at +100% or -50%, else 15:15 the day before expiry. Premium up to 25% of
    the account (at least 1 lot if one lot costs <= 50%). Backtest 2026 at Rs3L: +Rs1,18,104 but only 11 trades (4 won) -- probably luck,
    which this paper account is here to find out.
Prices are checked every 5 minutes, like the backtest's 5-minute bars. Fills at the live price; slippage and Upstox charges (Rs30/order, STT
0.15% of sold premium, exchange 0.0354%, GST, stamp) are charged as in the backtest.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from core.paths import ARCHIVE_ROOT, CACHE_DIR, DB_DIR

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT = "OPTIONS_PAPER"
NIFTY = "NSE_INDEX|Nifty 50"
STATE = DB_DIR / "options_paper_state.json"
SL_X = 1.5
END = (15, 25)


def leg_costs(price: float, qty: int, side: str) -> dict:
    """Upstox charges + slippage of one option order (the backtest's cost())."""
    prem = price * qty
    exch = 0.000354 * prem
    gst = 0.18 * (30 + exch)
    stt = 0.0015 * prem if side == "SELL" else 0.0
    stamp = 0.00003 * prem if side == "BUY" else 0.0
    slip = (0.10 + 0.005 * price) * qty
    return {"brokerage": 30.0, "exchange_txn": exch, "gst": gst, "stt": stt, "stamp_duty": stamp, "sebi": 0.0, "slippage": slip,
            "total": 30 + exch + gst + stt + stamp + slip}


def _inr(v: float) -> str:
    from services.utils.telegram import inr
    return ("−" if v < 0 else "") + "₹" + inr(v)


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=1))


def options_master() -> pd.DataFrame:
    from services.broker.instruments import ensure_master
    ensure_master()
    m = pd.read_csv(sorted(glob.glob(str(CACHE_DIR / "upstox_instruments_2*.csv")))[-1])
    o = m[(m["name"] == "NIFTY") & (m["instrument_type"] == "OPTIDX")].copy()
    o["expiry"] = pd.to_datetime(o["expiry"]).dt.normalize()
    return o


def previous_expiry(today: date) -> pd.Timestamp | None:
    """The last expiry before today: the nightly option archive is named by expiry (engine/research_data.py)."""
    past = [pd.Timestamp(f.split("/")[-1][:10]) for f in glob.glob(str(ARCHIVE_ROOT / "options" / "NIFTY" / "*.csv*"))]
    past = [d for d in past if d.date() < today]
    st = _state().get("last_expiry")
    if st:
        past.append(pd.Timestamp(st))
    return max(past) if past else None


def trading_days_between(a: date, b: date, holidays: set) -> list[date]:
    """Trading days strictly after a and up to b."""
    out, d = [], a + timedelta(days=1)
    while d <= b:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


class Engine:
    def __init__(self, broker, db, send, now_fn=lambda: datetime.now(IST)):
        self.b, self.db, self.send, self.now = broker, db, send, now_fn
        self.capital0 = float(os.environ.get("OPTIONS_PAPER_CAPITAL", 300_000))
        db.init_account(ACCOUNT, name="Nifty weekly options (paper)", capital=self.capital0, leverage=1.0, risk_pct=0.0)
        self.master = options_master()
        try:
            from services.utils.market_holidays import get_trading_holidays
            self.holidays = set(get_trading_holidays())
        except Exception:
            self.holidays = set()

    @property
    def capital(self) -> float:
        return float(self.db.get_account(ACCOUNT)["current_capital"])

    def contract(self, expiry: pd.Timestamp, strike: float, typ: str):
        o = self.master[(self.master["expiry"] == expiry) & (self.master["option_type"] == typ)]
        if o.empty:
            return None
        return o.iloc[(o["strike"] - strike).abs().argsort()[:1]].iloc[0]

    def ltp(self, keys) -> dict:
        return {k: v for k, v in (self.b.get_ltp_batch(list(keys)) or {}).items() if v}

    # ---- open / close a group of legs -------------------------------------------------------------------------------------------
    def open_group(self, strategy: str, expiry, legs: list[tuple], lots: int, extra: dict) -> str | None:
        """legs: [(contract_row, side +1 buy / -1 sell)]. Returns the group id."""
        lot = int(legs[0][0]["lot_size"])
        px = self.ltp(c["instrument_key"] for c, _ in legs)
        if any(c["instrument_key"] not in px for c, _ in legs):
            return None
        now = self.now()
        gid = f"{strategy}_{now:%Y%m%d_%H%M}"
        q = lots * lot
        net_unit = sum(-side * px[c["instrument_key"]] for c, side in legs)
        for c, side in legs:
            p = px[c["instrument_key"]]
            state = {"group": gid, "strategy": strategy, "side": side, "lots": lots, "lot": lot, "expiry": str(expiry.date()),
                     "net_unit": net_unit, "entry_fee": leg_costs(p, q, "BUY" if side > 0 else "SELL")["total"], **extra}
            self.db.open_position(f"POS_{gid}_{c['tradingsymbol']}", c["tradingsymbol"], "long" if side > 0 else "short", q, p, 0.0, 0.0, 0.0,
                                  account_id=ACCOUNT, instrument_key=c["instrument_key"], strategy=strategy, state=json.dumps(state))
        legs_txt = "\n".join(f"• {'BUY' if s > 0 else 'SELL'} {c['tradingsymbol']} @ {px[c['instrument_key']]:,.2f}" for c, s in legs)
        what = (f"credit {net_unit:,.2f} pts × {q} = {_inr(net_unit * q)}" if net_unit > 0 else f"cost {-net_unit:,.2f} pts × {q} = {_inr(-net_unit * q)}")
        self.send(f"🎯 <b>OPTIONS PAPER: {strategy.replace('_', ' ').upper()}</b> opened ({lots} lot{'s' if lots > 1 else ''} × {lot})\n"
                  f"{legs_txt}\n{what}" + (f"\nMargin ≈ {_inr(extra['margin'])}" if extra.get("margin") else "")
                  + f"\nAccount {_inr(self.capital)}")
        return gid

    def groups(self) -> dict[str, list[dict]]:
        out = {}
        for p in self.db.get_open_positions(ACCOUNT):
            st = json.loads(p.get("state") or "{}")
            out.setdefault(st.get("group", p["position_id"]), []).append({**p, "st": st})
        return out

    def close_group(self, legs: list[dict], reason: str, px: dict, expiry_close: bool = False) -> float:
        now, cap, net_total = self.now(), self.capital, 0.0
        for p in legs:
            side, q = p["st"]["side"], int(p["qty"])
            x = px.get(p["instrument_key"], p["entry_price"])
            gross = side * (x - p["entry_price"]) * q
            fee_in = leg_costs(p["entry_price"], q, "BUY" if side > 0 else "SELL")
            fee_out = {k: 0.0 for k in fee_in} if (expiry_close and x < 0.5) else leg_costs(x, q, "SELL" if side > 0 else "BUY")
            fees = {k: fee_in[k] + fee_out[k] for k in fee_in}
            net = gross - fees["total"]
            net_total += net
            cap += net
            self.db.close_position(p["position_id"], x, reason, gross, net, fees["total"])
            hold = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
            self.db.record_trade(p["position_id"], p["symbol"], p["direction"], q, p["entry_price"], x, p["entry_time"], now.isoformat(), hold,
                                 reason, gross, fees, net, cap, account_id=ACCOUNT, strategy=p["strategy"])
        self.db.update_capital(ACCOUNT, cap)
        name = legs[0]["strategy"].replace("_", " ").upper()
        self.send(f"{'✅' if net_total >= 0 else '❌'} <b>OPTIONS PAPER: {name}</b> closed ({reason.replace('_', ' ')})\n"
                  + "\n".join(f"• {p['symbol']} {p['entry_price']:,.2f} → {px.get(p['instrument_key'], p['entry_price']):,.2f}" for p in legs)
                  + f"\nProfit after fees <b>{'+' if net_total >= 0 else ''}{_inr(net_total)}</b> · account {_inr(cap)}")
        return net_total

    # ---- the two strategies ------------------------------------------------------------------------------------------------------
    def try_condor(self, today, expiry, st):
        if st.get("condor_done") == str(expiry.date()):
            return
        spot = self.ltp([NIFTY]).get(NIFTY)
        if not spot:
            return
        want = [(spot * 1.02, "CE", 1), (spot * 0.98, "PE", 1), (spot * 1.01, "CE", -1), (spot * 0.99, "PE", -1)]   # buy legs first: hedge benefit
        legs = [(self.contract(expiry, k, t), s) for k, t, s in want]
        if any(c is None for c, _ in legs) or len({c["instrument_key"] for c, _ in legs}) < 4:
            return
        lot = int(legs[0][0]["lot_size"])
        margin = self.b.get_basket_required_margin([{"instrument_key": c["instrument_key"], "quantity": lot, "transaction_type": "BUY" if s > 0 else "SELL",
                                                     "product": "D"} for c, s in legs])
        lots = int(0.9 * self.capital // margin) if margin else 0
        st["condor_done"] = str(expiry.date())
        if lots < 1:
            self.send(f"🎯 OPTIONS PAPER: expiry-day condor skipped -- one lot needs {_inr(margin or 0)} margin, account {_inr(self.capital)}")
            return
        self.open_group("expiry_condor", expiry, legs, lots, {"margin": round(margin * lots), "spot": spot})

    def try_directional(self, today, expiry, st):
        if st.get("dir_done") == str(expiry.date()):
            return
        spot = self.ltp([NIFTY]).get(NIFTY)
        if not spot:
            return
        st["dir_done"] = str(expiry.date())
        days = self.b.get_historical_candles(NIFTY, "days", 1, (today - timedelta(days=1)).isoformat(), (today - timedelta(days=20)).isoformat()) or []
        d = pd.DataFrame(days)
        if len(d) < 5:
            return
        d = d.sort_values("timestamp").tail(5)
        if spot > d["high"].max():
            typ = "CE"
        elif spot < d["low"].min():
            typ = "PE"
        else:
            self.send(f"🎯 OPTIONS PAPER: directional -- Nifty {spot:,.0f} inside last week's range {d['low'].min():,.0f}-{d['high'].max():,.0f}, no trade this week")
            return
        c = self.contract(expiry, spot, typ)
        px = self.ltp([c["instrument_key"]]).get(c["instrument_key"]) if c is not None else None
        if not px:
            return
        lot = int(c["lot_size"])
        per_lot = px * lot
        if per_lot > 0.5 * self.capital:
            return
        lots = max(1, int(0.25 * self.capital // per_lot))
        self.open_group("directional_buy", expiry, [(c, 1)], lots, {"spot": spot})

    def day_before(self, d: date) -> date:
        """The trading day before d (directional trades close at 15:15 then, like the backtest)."""
        d -= timedelta(days=1)
        while d.weekday() >= 5 or d in self.holidays:
            d -= timedelta(days=1)
        return d

    def manage(self, today, expiry):
        now = self.now()
        for gid, legs in self.groups().items():
            px = self.ltp(p["instrument_key"] for p in legs)
            if any(p["instrument_key"] not in px for p in legs):
                continue
            st = legs[0]["st"]
            net_unit = st["net_unit"]
            cur = sum(-p["st"]["side"] * px[p["instrument_key"]] for p in legs)
            pnl = net_unit - cur
            exp = pd.Timestamp(st["expiry"])
            if st["strategy"] == "expiry_condor":
                if net_unit > 0 and pnl <= -SL_X * net_unit:
                    self.close_group(legs, "stop_1.5x_credit", px)
                elif now.date() >= exp.date() and (now.hour, now.minute) >= (15, 15):
                    self.close_group(legs, "expiry_close", px, expiry_close=True)
            else:
                gain = pnl / -net_unit if net_unit < 0 else 0
                if gain >= 1.0:
                    self.close_group(legs, "doubled", px)
                elif gain <= -0.5:
                    self.close_group(legs, "halved", px)
                elif now.date() >= self.day_before(exp.date()) and (now.hour, now.minute) >= (15, 15):
                    self.close_group(legs, "time_day_before_expiry", px)

    def tick(self):
        now = self.now()
        today = now.date()
        st = _state()
        future = sorted(e for e in self.master["expiry"].unique() if pd.Timestamp(e).date() >= today)
        if not future:
            return
        expiry = pd.Timestamp(future[0])
        prev = previous_expiry(today)
        if prev is not None:
            first_day = trading_days_between(prev.date(), today, self.holidays)
            if first_day and first_day[0] == today and (now.hour, now.minute) >= (10, 0) and expiry.date() != today:
                self.try_directional(today, expiry, st)
        if expiry.date() == today and (9, 30) <= (now.hour, now.minute) < (15, 10):
            self.try_condor(today, expiry, st)
        if expiry.date() == today and (now.hour, now.minute) >= (15, 15):
            st["last_expiry"] = str(expiry.date())
        _save(st)
        if now.minute % 5 == 0 or (now.hour, now.minute) >= (15, 15):
            self.manage(today, expiry)

    def summary(self):
        trades = [t for t in self.db.get_trades_for_date(self.now().strftime("%Y-%m-%d"), ACCOUNT)]
        open_n = len(self.groups())
        if trades or open_n:
            self.send(f"📊 <b>OPTIONS PAPER</b> day end: account {_inr(self.capital)} (started {_inr(self.capital0)}); "
                      f"closed today {_inr(sum(t['net_pnl'] for t in trades))}; open positions: {open_n}")


def main(argv=None) -> int:
    import engine.config  # noqa: F401
    from engine.database import TradingDB
    from markets.commodity.data import _get_broker
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args(argv)
    db = TradingDB()
    if a.status:
        print(db.get_account(ACCOUNT), *db.get_open_positions(ACCOUNT), sep="\n")
        return 0
    from services.utils import telegram
    eng = Engine(_get_broker(), db, print if a.no_telegram else telegram.send)
    now = eng.now()
    if now.weekday() >= 5 or now.date() in eng.holidays:
        print("market closed today")
        return 0
    while (eng.now().hour, eng.now().minute) < END:
        try:
            eng.tick()
        except Exception as exc:                        # one bad tick must not end the session
            print(f"tick failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(60 - eng.now().second)
    eng.summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
