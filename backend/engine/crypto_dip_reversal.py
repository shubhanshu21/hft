"""Paper trading engine: crypto / dip_reversal -- 5-minute dip-buy reversal on 11 coins' perpetual futures (one shared capital pool, up
to markets.crypto.strategies.tf_5min.dip_reversal.strategy.MAX_CONCURRENT positions open together). Rules, costs and the backtest live
in that module; this file just runs the same rule live every 5 minutes against fresh Binance candles.

Fills, hybrid (user: "for delta exchange india we dont need paper account as it has demo account so we can directly use its apis",
2026-10-21): for the 6 coins Delta's demo/testnet environment actually lists (BTC, ETH, SOL, XRP, DOGE, ADA -- it has only 16
products total vs 237 on the real exchange, see services/broker/delta_broker.py), entries and exits are REAL orders placed on
Delta's own demo account via DeltaDemoBroker and filled by their real matching engine -- the fill price this engine records is
whatever Delta's engine actually gave it, not a simulated one. The other 6 coins (BNB, AVAX, LINK, DOT, LTC, TRX) aren't on Delta's
demo at all, so they keep the simulated fill logic this engine always used. Either way the money is one shared Rs1L ledger tracked
here (`state["trading_capital"]`) -- Delta's own demo wallet balance is a separate number (its own seeded demo funds) and is not
used as this account's capital.

Replaces engine/crypto_compounder_5lakh.py (removed 2026-10-21, user: "if its better and giving more profits then remove previous and
add this" -- the freqtrade screen found this rule more profitable on the same 2022-26 data; see markets/README.md).

Usage:
    python3 -m engine.crypto_dip_reversal            # process the latest finished 5-min candles & trade
    python3 -m engine.crypto_dip_reversal --status    # print account status
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from core.atomic_io import write_json
from core.paths import DB_DIR
from engine.database import TradingDB
from markets.crypto.strategies.tf_5min.dip_reversal import strategy as S
from services.broker.delta_broker import DeltaDemoBroker, PRODUCTS as DELTA_COINS

# Delta Exchange India live perpetuals trade in WHOLE contracts of this many coins each (api.india.delta.exchange/v2/products,
# checked 2026-10-23). The simulated fill buys a whole number of contracts, like a real order, instead of a fractional coin amount.
CONTRACT_VALUE = {"BTC": 0.001, "ETH": 0.01, "SOL": 1, "BNB": 0.1, "XRP": 1, "DOGE": 100, "ADA": 1, "AVAX": 1, "DOT": 1, "LTC": 0.1, "TRX": 1}

_log = logging.getLogger("crypto_dip_reversal")


def _delta() -> DeltaDemoBroker | None:
    """A broker instance if real demo orders are switched on (DELTA_PLACE_REAL_ORDERS=true in .env) and keys are configured, else
    None and every coin runs this engine's own simulated fill, fully sized by our own Rs1L ledger. BOTH the .env flag AND all three
    of engine/safety_gate.py's gates are required (2026-10-26) -- the flag alone no longer places an order anywhere.

    Off by default since 2026-10-22 (user: "try to place order in demo and get whatever we want and just create same paper trading
    system in our project") -- the demo account's seeded funds (~$138 margin) are far smaller than a RISK_PCT-sized position, so real
    orders were routinely clamped well below the size we actually mean to simulate (services/broker/delta_broker.py's
    place_market_order_fit_margin scales them down). A real fill on a WRONG size is less representative than a simulated fill at the
    RIGHT size, given Delta's own prices already track the real market closely (confirmed 2026-10-21). The order-placement code is
    kept, tested and working (see git history, commits 9e18f31/9cab62e) -- flip DELTA_PLACE_REAL_ORDERS=true (and pass all
    three safety gates) to use it again, e.g. once the demo account has more margin or this moves to a real account."""
    if str(os.environ.get("DELTA_PLACE_REAL_ORDERS", "false")).lower() not in ("1", "true", "yes"):
        return None
    # The .env flag alone is not enough (2026-10-26): the same three independent gates that protect the Upstox path
    # (engine/safety_gate.py -- kill-switch constant, ALLOW_LIVE_TRADING, armed-state file for component 'delta') must agree
    # before any code path is allowed to send an order to an exchange, demo or not. Without that, one careless .env edit put
    # real orders on a venue with no second check.
    from engine.safety_gate import live_trading_allowed
    if not live_trading_allowed("delta"):
        _log.warning("DELTA_PLACE_REAL_ORDERS is true but safety_gate's gates are not all satisfied for component 'delta' "
                     "(see engine/safety_gate.py) -- trading every coin in simulation.")
        return None
    try:
        return DeltaDemoBroker()
    except Exception as e:
        _log.warning("Delta demo broker unavailable (%s); trading all coins in simulation.", e)
        return None

IST = timezone(timedelta(hours=5, minutes=30))
ACCOUNT_ID = "CRYPTO_DIP_REVERSAL"
STRATEGY_NAME = "dip_reversal"
STATE_FILE = DB_DIR / "crypto_dip_reversal_state.json"
INITIAL_CAPITAL_INR = 100000.0
USDINR_FALLBACK = 92.5      # last resort only: used when the USD/INR reference feed is down, and every run that uses it says so
MAX_LEVERAGE = 3.0

# Portfolio-level ceilings (2026-10-26). MAX_LEVERAGE above is PER POSITION, and MAX_CONCURRENT (5) lets five of them run together, so
# the real book could reach ~4.3x the account in notional with roughly 30% of the capital lost if every stop hit at once -- the "3x
# leverage cap" only ever limited one trade. These two caps are what actually limit the book. Both are OFF (0) by default: switching them
# on would change how the live paper record would have traded, and that is a risk decision for the user, not a silent default change.
# Set them in .env to enable, e.g. CRYPTO_MAX_TOTAL_NOTIONAL_X=4.0 and/or CRYPTO_MAX_TOTAL_RISK_PCT=0.2.
def _cap_from_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        _log.warning("%s=%r is not a number; using %s (off).", name, os.environ.get(name), default)
        return default


MAX_TOTAL_NOTIONAL_X = _cap_from_env("CRYPTO_MAX_TOTAL_NOTIONAL_X", 0.0)     # sum of open notional, as a multiple of the account
MAX_TOTAL_RISK_PCT = _cap_from_env("CRYPTO_MAX_TOTAL_RISK_PCT", 0.0)         # loss if EVERY open position hit its stop, as a % of it


def _inr(v: float) -> str:
    s = f"{abs(v):,.2f}"
    return f"{'-' if v < 0 else ''}Rs{s}"


def portfolio_cap_block(cap: float, fx: float, open_pos: dict, pos_usd: float) -> str | None:
    """Why one more position of `pos_usd` USDT notional must not be opened, or None when both portfolio caps allow it.

    Both caps default to 0 (off), in which case this always returns None and trading is exactly as before. The stop is fixed at
    S.STOPLOSS below entry and never trailed, so the loss if a position's stop is hit is S.STOPLOSS of its notional -- the risk cap is
    therefore the honest all-stops-hit number, not a guess."""
    if MAX_TOTAL_NOTIONAL_X <= 0 and MAX_TOTAL_RISK_PCT <= 0:
        return None
    open_usd = sum(float(p.get("pos_usd") or 0.0) for p in open_pos.values())
    notional_inr = (open_usd + pos_usd) * fx
    if MAX_TOTAL_NOTIONAL_X > 0 and notional_inr > MAX_TOTAL_NOTIONAL_X * cap:
        return (f"the portfolio notional cap ({MAX_TOTAL_NOTIONAL_X:.1f}x the account = {_inr(MAX_TOTAL_NOTIONAL_X * cap)}) would be "
                f"breached: open {_inr(open_usd * fx)} + this {_inr(pos_usd * fx)}")
    risk_inr = S.STOPLOSS * notional_inr
    if MAX_TOTAL_RISK_PCT > 0 and risk_inr > MAX_TOTAL_RISK_PCT * cap:
        return (f"the portfolio risk cap ({MAX_TOTAL_RISK_PCT:.0%} of the account = {_inr(MAX_TOTAL_RISK_PCT * cap)} all stops hit) "
                f"would be breached: open + this = {_inr(risk_inr)}")
    return None


def load_state(db: TradingDB | None = None) -> dict:
    """The account ledger. A missing file (first run) starts at INITIAL_CAPITAL_INR; an UNREADABLE one must never silently reset
    the account -- that once wiped real history and then overwrote the database's capital to match (2026-10-26). In that case the
    capital is recovered from the database and `state_recovered` is set so the run says so out loud."""
    default = {"trading_capital": INITIAL_CAPITAL_INR, "open_positions": {}, "completed_trades_count": 0,
               "winning_trades_count": 0, "total_fees_paid": 0.0, "created_at": datetime.now(IST).isoformat()}
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("open_positions") or {}, dict):
            raise ValueError("not a state object")
        float(data.get("trading_capital"))
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        cap = INITIAL_CAPITAL_INR
        try:
            acc = db.get_account(ACCOUNT_ID) if db is not None else None
            if acc and acc.get("current_capital"):
                cap = float(acc["current_capital"])
        except Exception:
            pass
        _log.error("State file %s is unreadable (%s); recovered capital %s from the database. Open positions (if any) could not be "
                   "rebuilt -- check %s%s before trusting the next run.", STATE_FILE, e, _inr(cap), STATE_FILE, ".bak")
        default["trading_capital"] = cap
        default["state_recovered"] = True
        return default


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now(IST).isoformat()
    write_json(STATE_FILE, state)          # temp file + fsync + atomic rename, previous version kept as .bak


def init_account(db: TradingDB) -> None:
    db.init_account(account_id=ACCOUNT_ID, name="Crypto Dip-Reversal (11-coin 5m Perps)", capital=INITIAL_CAPITAL_INR,
                    leverage=MAX_LEVERAGE, risk_pct=S.RISK_PCT * 100)


def fetch_live_prices() -> dict:
    out = {"prices": {}, "usdtinr": USDINR_FALLBACK, "fx_fallback": True, "timestamp": datetime.now(IST).isoformat()}
    for c in S.COINS:                                 # Delta India quotes/settles in USD, but balances are effectively INR via a fixed rate
        try:
            r = requests.get(f"https://api.india.delta.exchange/v2/tickers/{c}USD", timeout=10)
            if r.status_code == 200:
                res = (r.json() or {}).get("result") or {}
                px = res.get("close") or res.get("mark_price")
                if px:
                    out["prices"][c] = float(px)
                    fr = res.get("funding_rate")
                    if fr is not None:
                        out.setdefault("funding_now", {})[c] = float(fr)
        except Exception as e:
            _log.warning("Delta India ticker fetch error for %s: %s", c, e)
    try:                                               # USD/INR reference rate -- an independent public source, not tied to any exchange
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        if r.status_code == 200:
            rate = (r.json() or {}).get("rates", {}).get("INR")
            if rate:
                out["usdtinr"] = float(rate)
                out["fx_fallback"] = False
    except Exception as e:
        _log.warning("USD/INR reference rate fetch error: %s", e)
    if out["fx_fallback"]:                             # never let the hardcoded rate pass as a live one
        _log.warning("USD/INR reference feed unavailable; using the fallback rate %.2f -- rupee P&L in this run is approximate.",
                     USDINR_FALLBACK)
    for c in S.COINS:                                 # fallback if Delta's own ticker failed for a coin
        if c not in out["prices"]:
            try:
                r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={c}USDT", timeout=5)
                if r.status_code == 200:
                    out["prices"][c] = float(r.json()["price"])
            except Exception:
                pass
    return out


def fetch_5m_candles(coin: str, limit: int = 300) -> pd.DataFrame:
    """Finished 5-minute candles from Binance USDT-M futures, with the entry/exit indicators (same math as the backtest module). 300 = 25h,
    so a position can be re-checked over its whole 24h life after missed runs (was 100 = ~8h)."""
    r = requests.get("https://fapi.binance.com/fapi/v1/klines", params={"symbol": f"{coin}USDT", "interval": "5m", "limit": limit}, timeout=15)
    r.raise_for_status()
    df = pd.DataFrame(r.json()).iloc[:, :7]
    df.columns = ["t", "open", "high", "low", "close", "v", "close_t"]
    df = df.astype({"t": "int64", "open": "float64", "high": "float64", "low": "float64", "close": "float64", "close_t": "int64"})
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    df = df[df["close_t"] < now_ms].sort_values("t").reset_index(drop=True)
    mid40 = df["close"].rolling(40).mean(); std40 = df["close"].rolling(40).std(); lower40 = mid40 - std40 * 2
    df["bbdelta"] = (mid40 - lower40).abs(); df["lower40"] = lower40
    df["closedelta"] = (df["close"] - df["close"].shift()).abs()
    df["tail"] = (df["close"] - df["low"]).abs()
    tp = (df["high"] + df["low"] + df["close"]) / 3
    mid20 = tp.rolling(20).mean(); std20 = tp.rolling(20).std()
    df["bb_mid20"] = mid20; df["bb_lower20"] = mid20 - std20 * 2
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    return df


BAR_MS = S.BAR_MIN * 60 * 1000
STOP_WHY, TARGET_WHY = f"STOP LOSS ({S.STOPLOSS:.0%})", f"TAKE PROFIT ({S.ROI:.0%})"
SIGNAL_WHY, TIME_WHY = "EXIT SIGNAL (close > 20-candle mid-band)", "TIME STOP (24h)"


def bar_exit(pos: dict, df: pd.DataFrame, live_px: float | None, coin: str) -> tuple:
    """(exit price, exit fee %, reason, exit bar time) or (None, 0, "", None) -- the backtest's exit rule (strategy.web_backtest), bar by bar.

    Walks EVERY finished candle since the position was last checked (from its entry candle on), so a run that failed or was skipped does
    not miss a stop or target hit in between. Stop, then target, then the close back above the mid-band (filled at the NEXT candle's
    open; for the newest candle that open is now, so the live price), then the 24h time stop at the close. On the entry candle itself the
    target fills at the target price and the mid-band exit is not allowed -- as in the backtest. 2026-10-24: before this the mid-band exit
    filled at the open of the candle that gave the signal (5 minutes stale), the entry candle counted as already past, and only the newest
    candle was looked at."""
    entry_t, done_t = pos["entry_bar_t"], pos.get("last_checked_t", -1)
    slip = S.TAKER + S.SLIP.get(coin, 0.00015)
    rows = df[(df["t"] >= entry_t) & (df["t"] > done_t)].reset_index(drop=True)
    for k, b in rows.iterrows():
        t, o, h, l, c = int(b["t"]), float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        if l <= pos["sl_price"]:
            return min(o, pos["sl_price"]), slip, STOP_WHY, t
        if h >= pos["tp_price"]:
            return (max(o, pos["tp_price"]) if t > entry_t else pos["tp_price"]), S.MAKER, TARGET_WHY, t
        if not pd.isna(b["bb_mid20"]) and c > float(b["bb_mid20"]) and t > entry_t:
            nxt = float(rows.loc[k + 1, "open"]) if k + 1 < len(rows) else (live_px or c)
            return nxt, slip, SIGNAL_WHY, t + BAR_MS
        if t - entry_t >= S.TIME_STOP_BARS * BAR_MS:
            return c, slip, TIME_WHY, t
    return None, 0.0, "", None


def process_exits(db: TradingDB, state: dict, candles_map: dict, live_info: dict, lines: list, broker: DeltaDemoBroker | None) -> None:
    fx = live_info["usdtinr"]
    for coin, pos in list(state["open_positions"].items()):
        df = candles_map.get(coin)
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-1]
        entry_bar_t, entry_px, pos_usd = pos["entry_bar_t"], pos["entry_price"], pos["pos_usd"]
        px = None; fee_pct = 0.0; why = ""; exit_t = int(last["t"])

        # A real position's resting target limit order may already have filled on Delta's own book -- check that FIRST, since it's
        # the actual source of truth for whether the target was hit (not our own candle's high crossing tp_px).
        tgt_id = pos.get("target_order_id")
        if pos.get("real") and broker is not None and tgt_id:
            try:
                tgt = broker.get_order(tgt_id)
                if tgt.get("state") in ("closed", "filled") and tgt.get("average_fill_price"):
                    px, fee_pct, why = float(tgt["average_fill_price"]), S.MAKER, TARGET_WHY + " (Delta demo limit order filled)"
                    exit_t = int(last["t"])
            except Exception as e:
                _log.warning("Delta demo target-order check failed for %s (falling back to the candle check): %s", coin, e)

        if px is None:
            px, fee_pct, why, exit_t = bar_exit(pos, df, live_info["prices"].get(coin), coin)
            pos["last_checked_t"] = int(last["t"])
            if px is None:
                continue
            if pos.get("real") and broker is not None and tgt_id and why == TARGET_WHY:
                # our own candle says the target was crossed, but the top-of-function check just found Delta's resting order still
                # open -- trust the resting order, not a redundant market close on top of it; it'll be picked up once it fills
                continue
            if pos.get("real") and broker is not None:
                if tgt_id:                                          # a non-target exit: cancel the resting target order first
                    try:
                        broker.cancel_order(coin, tgt_id)
                    except Exception as e:
                        _log.warning("Delta demo target-order cancel failed for %s (closing anyway): %s", coin, e)
                try:
                    order = broker.close_position(coin)
                    fill = order.get("average_fill_price") if order else None
                    if fill:
                        px = float(fill)                             # the real fill Delta's demo matching engine gave us
                except Exception as e:
                    _log.warning("Delta demo close failed for %s, using the simulated exit price instead: %s", coin, e)
        gross_usd = (px - entry_px) / entry_px * pos_usd
        hours = (exit_t - entry_bar_t) / 3_600_000
        try:
            fr = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": f"{coin}USDT",
                              "startTime": entry_bar_t, "endTime": exit_t, "limit": 100}, timeout=15).json()
            funding_usd = pos_usd * sum(float(x["fundingRate"]) for x in fr)
        except Exception:
            funding_usd = 0.0
        fee_usd = pos_usd * (S.TAKER + S.SLIP.get(coin, 0.00015) + fee_pct) + funding_usd
        net_usd = gross_usd - fee_usd
        net_inr, gross_inr, fee_inr = net_usd * fx, gross_usd * fx, fee_usd * fx
        pos_id = pos["position_id"]
        already = db.get_position(pos_id)
        if already is not None and already.get("status") == "CLOSED":
            # A previous run booked this exit in the database and then died before saving the state file, so the position is still
            # listed here. Booking it again would add its PnL to the ledger twice and write a duplicate trade row -- drop it
            # instead, before anything is credited.
            _log.warning("%s was already closed in the database (%s); dropping it from the state without booking it again.",
                         pos_id, already.get("exit_reason"))
            del state["open_positions"][coin]
            continue
        state["trading_capital"] += net_inr
        state["total_fees_paid"] += fee_inr
        state["completed_trades_count"] += 1
        if net_inr > 0:
            state["winning_trades_count"] += 1
        db.close_position(pos_id, px, why, gross_inr, net_inr, fee_inr)
        db.record_trade(position_id=pos_id, symbol=coin, direction="long", qty=pos["qty"], entry_price=entry_px, exit_price=px,
                         entry_dt=pos["entry_time"], exit_dt=datetime.now(IST).isoformat(), hold_minutes=hours * 60, exit_reason=why,
                         gross_pnl=gross_inr, costs_dict={"brokerage": fee_inr, "slippage": 0.0, "total": fee_inr}, net_pnl=net_inr,
                         capital_after=state["trading_capital"], account_id=ACCOUNT_ID, strategy=STRATEGY_NAME)
        sign = "+" if net_inr >= 0 else ""
        lines.append(f"CLOSED LONG {coin}: {why}\nEntry {entry_px:,.4f} -> Exit {px:,.4f} USDT\nNet PnL: {sign}{_inr(net_inr)} (fees {_inr(fee_inr)})\n"
                      f"Trading capital: {_inr(state['trading_capital'])}")
        del state["open_positions"][coin]


def process_entries(db: TradingDB, state: dict, candles_map: dict, live_info: dict, lines: list, broker: DeltaDemoBroker | None) -> None:
    cap = state["trading_capital"]
    if cap < 500.0:
        lines.append("Trading capital below Rs500. Pausing."); return
    open_pos = state["open_positions"]
    if len(open_pos) >= S.MAX_CONCURRENT:
        return
    fx = live_info["usdtinr"]
    signals = {}                                                  # every coin's signal on the latest bar, open or not (as the backtest counts)
    for coin in S.COINS:
        df = candles_map.get(coin)
        signals[coin] = df is not None and len(df) >= 41 and bool(S.entry_signal(df).iloc[-1])
    crowd = S.crowd_scale(sum(signals.values()))                  # many coins dipping at once = market-wide crash: shrink each new trade
    vol_scale = S.current_vol_scale()                             # ONE read per run (cached on disk; rebuilding it costs ~17 s)
    for coin in S.COINS:
        if len(open_pos) >= S.MAX_CONCURRENT or coin in open_pos:
            continue
        df = candles_map.get(coin)
        if not signals[coin]:
            continue
        last = df.iloc[-1]
        entry_px = live_info["prices"].get(coin, float(last["close"]))     # live price for a fresher fill than the closed candle
        sl_px = entry_px * (1 - S.STOPLOSS)
        risk_pct = S.RISK_PCT * vol_scale                   # dynamic: size up in a calmer-than-usual market, down in a wilder one
        risk_inr = cap * risk_pct
        pos_usd = min((risk_inr / fx) / S.STOPLOSS, (cap * MAX_LEVERAGE) / fx) * crowd
        risk_inr *= crowd
        cv = CONTRACT_VALUE.get(coin, 1)
        n_contracts = int(pos_usd / entry_px / cv)                 # whole contracts only, rounded down, as a real order would be
        if n_contracts < 1:
            lines.append(f"{coin}: signal, but one Delta contract ({cv} {coin}) is bigger than the position size -- skipped"); continue
        pos_usd = n_contracts * cv * entry_px
        blocked = portfolio_cap_block(cap, fx, open_pos, pos_usd)        # checked before any order goes out; off unless the .env knobs set
        if blocked:
            lines.append(f"{coin}: signal, but {blocked} -- skipped")
            continue
        intended_pos_usd = pos_usd
        qty = n_contracts * cv

        contracts, real, target_order_id = None, False, None
        if broker is not None and coin in DELTA_COINS:
            _, contract_value = DELTA_COINS[coin]
            contracts = round(qty / contract_value)
            if contracts >= 1:
                try:
                    order = broker.place_market_order_fit_margin(coin, "buy", contracts)
                    fill = order.get("average_fill_price")
                    filled_contracts = int(order.get("size") or contracts)   # may be smaller than asked -- the demo account's margin may not stretch to the full size
                    if fill:
                        entry_px = float(fill)                      # the real fill from Delta's demo matching engine
                        sl_px = entry_px * (1 - S.STOPLOSS)
                        qty = filled_contracts * contract_value
                        pos_usd = qty * entry_px
                        real = True
                        contracts = filled_contracts
                        try:                                        # resting GTC limit sell at the target -- a real maker fill if
                            tp_order = broker.place_limit_order(coin, "sell", filled_contracts, entry_px * (1 + S.ROI), reduce_only=True)
                            target_order_id = tp_order.get("id")     # it's reached, not a market/taker exit (matches the backtest's cost model)
                        except Exception as e:
                            _log.warning("Delta demo target limit order failed for %s (stop/exit-signal/time-stop still cover it): %s", coin, e)
                except Exception as e:
                    _log.warning("Delta demo order failed for %s, falling back to simulated fill: %s", coin, e)
                    contracts = None
            else:
                contracts = None                                    # too small for even 1 contract -- simulate instead

        pos_id = f"POS_DIPREV_{int(last['t'])}_{coin}"
        now_iso = datetime.now(IST).isoformat()
        open_pos[coin] = {"position_id": pos_id, "entry_price": entry_px, "sl_price": sl_px, "tp_price": entry_px * (1 + S.ROI),
                           "pos_usd": pos_usd, "qty": qty, "entry_bar_t": int(last["t"]) + BAR_MS, "entry_time": now_iso,
                           "real": real, "contracts": contracts, "target_order_id": target_order_id}
        db.open_position(position_id=pos_id, symbol=coin, direction="long", qty=qty, entry_price=entry_px, current_stop=sl_px,
                          target_price=entry_px * (1 + S.ROI), breakeven_price=entry_px, account_id=ACCOUNT_ID,
                          instrument_key=f"B-{coin}_USDT", strategy=STRATEGY_NAME, state=json.dumps({"risk_pct": risk_pct, "real": real}))
        tag = "Delta demo order" if real else "simulated fill"
        clamp_note = (f" -- demo account's margin only covered {_inr(pos_usd * fx)} of the intended {_inr(intended_pos_usd * fx)}"
                       if real and pos_usd < intended_pos_usd * 0.999 else "")
        lines.append(f"ENTERED LONG {coin}: dip-reversal signal ({tag}){clamp_note}\nEntry {entry_px:,.4f} | Stop {sl_px:,.4f} | Target {open_pos[coin]['tp_price']:,.4f}\n"
                      f"Size {_inr(pos_usd * fx)} | Risk {_inr(risk_inr)} ({risk_pct:.1%}, market vol scale {vol_scale:.2f}x)"
                      + (f"\nCRASH-AWARE: {sum(signals.values())} coins dipping at once (market-wide) -- trade shrunk to {crowd:.0%} of normal size"
                         if crowd < 1 else ""))


def run_iteration(db: TradingDB, send_fn=None) -> dict:
    state = load_state(db)
    live_info = fetch_live_prices()
    lines = []
    if live_info.get("fx_fallback"):                 # said out loud, in the Telegram message, not only in the log
        lines.append(f"⚠️ USD/INR reference feed unavailable -- using the fallback rate {USDINR_FALLBACK:.2f}; the rupee P&L of this "
                     "run is approximate.")
    if state.get("state_recovered"):
        lines.append(f"⚠️ The state file was unreadable: capital recovered from the database as {_inr(state['trading_capital'])}. "
                     f"Compare with {STATE_FILE.name}.bak (open positions were not rebuilt).")
    candles_map = {}
    coins_needed = set(S.COINS) | set(state.get("open_positions", {}))   # +any symbol with an open position, even if it's since
    for coin in coins_needed:                                            # been dropped from S.COINS (e.g. LINK, removed 2026-10-22)
        try:                                                              # -- an open trade must still get checked for its exit
            candles_map[coin] = fetch_5m_candles(coin)
        except Exception as e:
            _log.warning("Failed to fetch 5m candles for %s: %s", coin, e)
    if not candles_map:
        lines.append("Binance 5m candle feed unavailable -- no position was checked and no entry was taken this run.")
        if send_fn:                                   # a skipped run is news: without this the 5-minute engine could skip silently
            send_fn("⚠️ <b>CRYPTO DIP-REVERSAL</b> -- the Binance 5m candle feed failed, so this run checked nothing.")
        return {"lines": lines, "state": state}
    broker = _delta()
    process_exits(db, state, candles_map, live_info, lines, broker)
    process_entries(db, state, candles_map, live_info, lines, broker)
    save_state(state)
    db.update_capital(ACCOUNT_ID, state["trading_capital"])
    db.record_snapshot(ACCOUNT_ID)
    if lines and send_fn:
        now_dt = datetime.now(IST)
        wr = state["winning_trades_count"] / max(1, state["completed_trades_count"]) * 100
        header = f"CRYPTO DIP-REVERSAL ({now_dt:%d %b %H:%M} IST)\n"
        footer = (f"\nCapital: {_inr(state['trading_capital'])} | Open: {len(state['open_positions'])}/{S.MAX_CONCURRENT} | "
                  f"Trades: {state['completed_trades_count']} (win {wr:.1f}%)")
        send_fn(header + "\n".join(lines) + footer)
    return {"lines": lines, "state": state}


def print_status(db: TradingDB) -> None:
    state = load_state(db)
    wr = state["winning_trades_count"] / max(1, state["completed_trades_count"]) * 100
    print("=" * 70)
    print("CRYPTO DIP-REVERSAL STATUS (11-coin 5m perps)")
    print("=" * 70)
    print(f"Trading capital:   {_inr(state['trading_capital'])}")
    print(f"Completed trades:  {state['completed_trades_count']} (win rate {wr:.1f}%)")
    print(f"Total fees paid:   {_inr(state['total_fees_paid'])}")
    print(f"Open positions:    {len(state['open_positions'])}/{S.MAX_CONCURRENT}")
    for coin, pos in state["open_positions"].items():
        tag = "Delta demo order" if pos.get("real") else "simulated"
        print(f"  {coin} ({tag}): entry {pos['entry_price']:,.4f}  stop {pos['sl_price']:,.4f}  target {pos['tp_price']:,.4f}")
    print("=" * 70)


def main(argv=None) -> int:
    import engine.config  # noqa: F401 -- loads .env
    from services.utils import telegram

    parser = argparse.ArgumentParser(description="Crypto dip-reversal (5-min, 11-coin perps) paper trader")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args(argv)

    db = TradingDB()
    init_account(db)
    if args.status:
        print_status(db); return 0

    send_fn = print if args.no_telegram else telegram.send
    from engine.account_reset import account_lock
    with account_lock(ACCOUNT_ID):                     # a reset from the paper page waits for this run (and vice versa)
        out = run_iteration(db, send_fn=send_fn)
    print("\n".join(out["lines"]) if out["lines"] else "Cycle completed: no trade triggers or state changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
