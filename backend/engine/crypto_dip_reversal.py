"""Paper trading engine: crypto / dip_reversal -- 5-minute dip-buy reversal on 12 coins' perpetual futures (one shared capital pool, up
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
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from core.paths import DB_DIR
from engine.database import TradingDB
from markets.crypto.strategies.tf_5min.dip_reversal import strategy as S
from services.broker.delta_broker import DeltaDemoBroker, PRODUCTS as DELTA_COINS

_log = logging.getLogger("crypto_dip_reversal")


def _delta() -> DeltaDemoBroker | None:
    """A broker instance if demo keys are configured, else None -- coins not on Delta's testnet (or any broker error) fall back to
    this engine's own simulated fill, exactly as before 2026-10-21."""
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
MAX_LEVERAGE = 3.0


def _inr(v: float) -> str:
    s = f"{abs(v):,.2f}"
    return f"{'-' if v < 0 else ''}Rs{s}"


def load_state() -> dict:
    default = {"trading_capital": INITIAL_CAPITAL_INR, "open_positions": {}, "completed_trades_count": 0,
               "winning_trades_count": 0, "total_fees_paid": 0.0, "created_at": datetime.now(IST).isoformat()}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            for k, v in default.items():
                data.setdefault(k, v)
            return data
        except Exception as e:
            _log.warning("Failed to parse %s (%s); recreating.", STATE_FILE, e)
    return default


def save_state(state: dict) -> None:
    state["updated_at"] = datetime.now(IST).isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def init_account(db: TradingDB) -> None:
    db.init_account(account_id=ACCOUNT_ID, name="Crypto Dip-Reversal (12-coin 5m Perps)", capital=INITIAL_CAPITAL_INR,
                    leverage=MAX_LEVERAGE, risk_pct=S.RISK_PCT * 100)


def fetch_live_prices() -> dict:
    out = {"prices": {}, "usdtinr": 92.5, "timestamp": datetime.now(IST).isoformat()}
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
    except Exception as e:
        _log.warning("USD/INR reference rate fetch error: %s", e)
    for c in S.COINS:                                 # fallback if Delta's own ticker failed for a coin
        if c not in out["prices"]:
            try:
                r = requests.get(f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={c}USDT", timeout=5)
                if r.status_code == 200:
                    out["prices"][c] = float(r.json()["price"])
            except Exception:
                pass
    return out


def fetch_5m_candles(coin: str, limit: int = 100) -> pd.DataFrame:
    """Finished 5-minute candles from Binance USDT-M futures, with the entry/exit indicators (same math as the backtest module)."""
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


def process_exits(db: TradingDB, state: dict, candles_map: dict, live_info: dict, lines: list, broker: DeltaDemoBroker | None) -> None:
    fx = live_info["usdtinr"]
    for coin, pos in list(state["open_positions"].items()):
        df = candles_map.get(coin)
        if df is None or len(df) < 2:
            continue
        last = df.iloc[-1]
        o, h, l, c = float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"])
        entry_bar_t, entry_px, sl_px, tp_px, pos_usd = pos["entry_bar_t"], pos["entry_price"], pos["sl_price"], pos["tp_price"], pos["pos_usd"]
        px = None; fee_pct = 0.0; why = ""

        # A real position's resting target limit order may already have filled on Delta's own book -- check that FIRST, since it's
        # the actual source of truth for whether the target was hit (not our own candle's high crossing tp_px).
        tgt_id = pos.get("target_order_id")
        if pos.get("real") and broker is not None and tgt_id:
            try:
                tgt = broker.get_order(tgt_id)
                if tgt.get("state") in ("closed", "filled") and tgt.get("average_fill_price"):
                    px, fee_pct, why = float(tgt["average_fill_price"]), S.MAKER, "TAKE PROFIT (15%, Delta demo limit order filled)"
            except Exception as e:
                _log.warning("Delta demo target-order check failed for %s (falling back to the candle check): %s", coin, e)

        if px is None:
            if l <= sl_px:
                px, fee_pct, why = min(o, sl_px), S.TAKER + S.SLIP.get(coin, 0.00015), "STOP LOSS (7%)"
            elif h >= tp_px:
                px, fee_pct, why = max(o, tp_px) if int(last["t"]) > entry_bar_t else tp_px, S.MAKER, "TAKE PROFIT (7%)"
            elif not pd.isna(last["bb_mid20"]) and c > float(last["bb_mid20"]) and int(last["t"]) > entry_bar_t:
                px, fee_pct, why = o, S.TAKER + S.SLIP.get(coin, 0.00015), "EXIT SIGNAL (close > 20-candle mid-band)"
            elif (int(last["t"]) - entry_bar_t) >= S.TIME_STOP_BARS * 5 * 60 * 1000:
                px, fee_pct, why = c, S.TAKER + S.SLIP.get(coin, 0.00015), "TIME STOP (24h)"
            if px is None:
                continue
            if pos.get("real") and broker is not None and tgt_id and why == "TAKE PROFIT (7%)":
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
        hours = (int(last["t"]) - entry_bar_t) / 3_600_000
        try:
            fr = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": f"{coin}USDT",
                              "startTime": entry_bar_t, "endTime": int(last["t"]), "limit": 100}, timeout=15).json()
            funding_usd = pos_usd * sum(float(x["fundingRate"]) for x in fr)
        except Exception:
            funding_usd = 0.0
        fee_usd = pos_usd * (S.TAKER + S.SLIP.get(coin, 0.00015) + fee_pct) + funding_usd
        net_usd = gross_usd - fee_usd
        net_inr, gross_inr, fee_inr = net_usd * fx, gross_usd * fx, fee_usd * fx
        state["trading_capital"] += net_inr
        state["total_fees_paid"] += fee_inr
        state["completed_trades_count"] += 1
        if net_inr > 0:
            state["winning_trades_count"] += 1
        pos_id = pos["position_id"]
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
    for coin in S.COINS:
        if len(open_pos) >= S.MAX_CONCURRENT or coin in open_pos:
            continue
        df = candles_map.get(coin)
        if df is None or len(df) < 41:
            continue
        sig = S.entry_signal(df)
        if not bool(sig.iloc[-1]):
            continue
        last = df.iloc[-1]
        entry_px = live_info["prices"].get(coin, float(last["close"]))     # live price for a fresher fill than the closed candle
        sl_px = entry_px * (1 - S.STOPLOSS)
        risk_pct = S.RISK_PCT * S.current_vol_scale()           # dynamic: size up in a calmer-than-usual market, down in a wilder one
        risk_inr = cap * risk_pct
        pos_usd = min((risk_inr / fx) / S.STOPLOSS, (cap * MAX_LEVERAGE) / fx)
        intended_pos_usd = pos_usd
        qty = pos_usd / entry_px

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
                           "pos_usd": pos_usd, "qty": qty, "entry_bar_t": int(last["t"]), "entry_time": now_iso,
                           "real": real, "contracts": contracts, "target_order_id": target_order_id}
        db.open_position(position_id=pos_id, symbol=coin, direction="long", qty=qty, entry_price=entry_px, current_stop=sl_px,
                          target_price=entry_px * (1 + S.ROI), breakeven_price=entry_px, account_id=ACCOUNT_ID,
                          instrument_key=f"B-{coin}_USDT", strategy=STRATEGY_NAME, state=json.dumps({"risk_pct": risk_pct, "real": real}))
        tag = "Delta demo order" if real else "simulated fill"
        clamp_note = (f" -- demo account's margin only covered {_inr(pos_usd * fx)} of the intended {_inr(intended_pos_usd * fx)}"
                       if real and pos_usd < intended_pos_usd * 0.999 else "")
        lines.append(f"ENTERED LONG {coin}: dip-reversal signal ({tag}){clamp_note}\nEntry {entry_px:,.4f} | Stop {sl_px:,.4f} | Target {open_pos[coin]['tp_price']:,.4f}\n"
                      f"Size {_inr(pos_usd * fx)} | Risk {_inr(risk_inr)} ({risk_pct:.1%}, market vol scale {S.current_vol_scale():.2f}x)")


def run_iteration(db: TradingDB, send_fn=None) -> dict:
    state = load_state()
    live_info = fetch_live_prices()
    lines = []
    candles_map = {}
    for coin in S.COINS:
        try:
            candles_map[coin] = fetch_5m_candles(coin)
        except Exception as e:
            _log.warning("Failed to fetch 5m candles for %s: %s", coin, e)
    if not candles_map:
        lines.append("Binance 5m candle feed unavailable. Skipping iteration.")
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
    state = load_state()
    wr = state["winning_trades_count"] / max(1, state["completed_trades_count"]) * 100
    print("=" * 70)
    print("CRYPTO DIP-REVERSAL STATUS (12-coin 5m perps)")
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

    parser = argparse.ArgumentParser(description="Crypto dip-reversal (5-min, 12-coin perps) paper trader")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args(argv)

    db = TradingDB()
    init_account(db)
    if args.status:
        print_status(db); return 0

    send_fn = print if args.no_telegram else telegram.send
    out = run_iteration(db, send_fn=send_fn)
    print("\n".join(out["lines"]) if out["lines"] else "Cycle completed: no trade triggers or state changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
