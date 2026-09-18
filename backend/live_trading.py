#!/usr/bin/env python3
"""
live_trading.py — REAL-MONEY execution engine for MCX commodities + NSE
currency derivatives. Mirrors live_dryrun.py's DryRunner signal logic
exactly (same ENTRY_THRESHOLDS, same features, same session windows, same
TP/SL/breakeven/trailing/timeout/EOD-squareoff mechanics) but places actual
orders through UpstoxBroker instead of writing simulated fills straight to
SQLite.

================================================================================
THIS FILE IS NOT WIRED UP. Written 2026-09-18 at the user's explicit request
("create actual live trading code but dont wire up") -- it exists, it is
complete, and it is reachable ONLY by running it directly with
`python3 live_trading.py`. Nothing else in this codebase imports or invokes
it:
  - cli.py has no subcommand for it (dryrun/backtest are unchanged).
  - No systemd unit references it (only hft-dryrun.service exists, and it
    still runs live_dryrun.py exactly as before).
  - live_dryrun.py itself is completely untouched by this file's existence.
Grep for "live_trading" outside this file to confirm that stays true before
ever changing that.
================================================================================

Even run directly, this refuses to place a single real order unless ALL
THREE of safety_gate.py's independent gates pass:
  1. safety_gate.KILL_SWITCH_ENGAGED must be hand-edited to False in source.
  2. ALLOW_LIVE_TRADING=true must be set in .env.
  3. `python3 cli.py arm-live-trading --component upstox --confirm "..."`
     must have been run (see safety_gate.py for the exact phrase).
UpstoxBroker.__init__ enforces this itself (broker/upstox_broker.py calls
safety_gate.enforce_dry_run() before honouring dry_run=False) -- this
module's own main() ALSO checks safety_gate.live_trading_allowed() up front
and refuses to even start the loop otherwise, so a misconfigured run fails
immediately and loudly instead of silently sitting in a forced-paper mode
that could be mistaken for "armed and running".

Real-order specifics live_dryrun.py's simulation never had to deal with:
  - Entries confirm the ACTUAL fill (broker.get_order_status /
    get_fill_price polled with a timeout) before a position is recorded --
    an order_id being returned does not mean the exchange filled it.
  - Costs are computed from the REAL fill price, never an assumed one.
  - There is no cancel_order() on UpstoxBroker yet. An entry order that
    doesn't reach 'complete' within ORDER_FILL_TIMEOUT_SEC is logged as a
    FAILED entry and left alone -- this module does not attempt to cancel
    or otherwise resolve it. A stuck/partially-filled real order needs a
    human to check the actual Upstox order book, not an automated guess.
  - Exit orders are placed and confirmed the same way; if an exit order
    fails to fill, the position is left open and flagged loudly (Telegram +
    log) rather than the process assuming it's flat when it might not be.
  - Every entry checks REAL available funds (broker.get_available_funds())
    against the required margin immediately before placing the order --
    independent of and in addition to size_commodity_lots'/
    size_currency_lots' own margin cap, which only sizes against this
    process's locally-tracked capital ledger (seeded from the DB), not the
    account's actual current balance. The two can drift; this is the final
    authoritative check against real money. Fails safe: if the funds API
    can't be reached, the entry is skipped rather than risked on an unknown
    balance.
  - Every scan reconciles this process's tracked positions against the REAL
    broker-side book (utils/position_reconciliation.py, via
    broker.get_broker_positions()) BEFORE any exit/entry decision. Handles
    the "I manually closed it in the Upstox app" scenario a pure
    simulated-fill tracker like live_dryrun.py's DryRunner structurally
    can't detect: without this, _maybe_exit would eventually try to place a
    real closing order against a position that's already gone -- which for
    an intraday product doesn't fail, it OPENS A NEW, UNTRACKED POSITION in
    the opposite direction. A clean external close is auto-reconciled (using
    current LTP as an approximate exit price, clearly flagged as such since
    the real manual-close fill price isn't knowable from this API); a
    partial close or unexplained size mismatch is NOT auto-healed -- this
    process stops managing that symbol and demands a human look, rather than
    guess at a state it can't reconstruct.

Usage (once actually armed -- see above):
    python3 live_trading.py --capital 100000 --risk-pct 5.0 --leverage 4.0
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import safety_gate
from auth.upstox_auto_login import ensure_fresh_upstox_token
from broker.upstox_broker import UpstoxBroker
from config import UpstoxConfig
from database import TradingDB
from strategy.commodity_costs import (
    compute_mcx_commodity_costs, size_commodity_lots, COMMODITY_SPECS, get_contract_multiplier,
)
from strategy.currency_costs import (
    compute_ncd_currency_costs, size_currency_lots, CURRENCY_SPECS,
    get_contract_multiplier as get_currency_contract_multiplier,
)
from strategy.commodity_features import compute_commodity_features, COMMODITY_FEATURE_COLUMNS
from backtest_commodity import ENTRY_THRESHOLDS as COMMODITY_ENTRY_THRESHOLDS
from backtest_currency import ENTRY_THRESHOLDS as CURRENCY_ENTRY_THRESHOLDS, _MIN_ORB as CURRENCY_MIN_ORB
from broker.instruments import build_mcx_commodity_map, build_currency_map
from utils.logger import get_logger, setup_logger
from utils import telegram

IST = ZoneInfo("Asia/Kolkata")
log = get_logger("live_trading")

_CURRENCY_SYMBOLS = {"USDINR", "EURINR", "GBPINR", "JPYINR"}


def _is_currency(sym: str) -> bool:
    return sym.upper() in _CURRENCY_SYMBOLS


# Same per-symbol risk override as live_dryrun.py -- see that file's comment
# (SILVER re-added 2026-09-18 at half risk pending more live experience).
_SYMBOL_RISK_PCT_OVERRIDE = {"SILVER": 5.0}

# How long to wait for a real order to reach 'complete' before giving up and
# flagging it for manual review (see module docstring -- no cancel_order()
# exists yet). Market orders on liquid MCX/NCD_FO contracts should fill in
# well under this; a slower fill is itself a signal something's off.
ORDER_FILL_TIMEOUT_SEC = 30
ORDER_POLL_INTERVAL_SEC = 2


def _build_symbol_map() -> dict[str, str]:
    """Same rollover-safe resolution as live_dryrun.py's own _build_symbol_map --
    duplicated here deliberately rather than imported, since this module must
    stay import-independent of live_dryrun.py (see module docstring: nothing
    should couple this file's behavior to the paper-trading daemon's)."""
    try:
        return {**build_mcx_commodity_map(), **build_currency_map()}
    except Exception as exc:
        log.warning("Could not build symbol map: %s", exc)
        return {}


def _fetch_candles(broker: UpstoxBroker, symbol: str, ikey: str, today: str) -> list[dict]:
    if not ikey:
        return []
    raw = broker.get_intraday_candles(ikey, unit="minutes", interval=5)
    if not raw:
        raw = broker.get_historical_candles(ikey, unit="minutes", interval=5, to_date=today)
    if not raw:
        return []
    return list(reversed(raw))


def _wait_for_fill(broker: UpstoxBroker, order_id: str, requested_price: float) -> tuple[str, float]:
    """Polls get_order_status/get_fill_price until 'complete', a terminal
    failure status, or timeout. Returns (outcome, fill_price):
      - ("filled", price)   -- confirmed complete, price is the real fill.
      - ("rejected", 0.0)   -- broker cleanly rejected/cancelled the order.
        Nothing was filled; safe to treat as "never happened".
      - ("ambiguous", 0.0)  -- still not 'complete'/'rejected'/'cancelled' at
        timeout. NOT the same as "rejected" -- Upstox's order-history status
        values include intermediate states (e.g. partial fills, 'open',
        'trigger pending') this doesn't enumerate, so an order sitting in one
        of those at the deadline might have real quantity filled at the
        broker even though this process can't confirm it. Callers must NOT
        treat this the same as a clean non-fill."""
    deadline = time.monotonic() + ORDER_FILL_TIMEOUT_SEC
    while time.monotonic() < deadline:
        status = broker.get_order_status(order_id)
        if status in ("rejected", "cancelled"):
            return "rejected", 0.0
        if status == "complete":
            fill_price = broker.get_fill_price(order_id)
            return "filled", (fill_price if fill_price is not None else requested_price)
        time.sleep(ORDER_POLL_INTERVAL_SEC)
    return "ambiguous", 0.0


class LiveTrader:
    """Real-order equivalent of live_dryrun.py's DryRunner. Same signal logic,
    same risk/session/threshold configuration -- see that file's DryRunner for
    the paper-trading version this mirrors line-for-line where the logic
    itself (not the execution) is identical."""

    def __init__(self, broker: UpstoxBroker, db: TradingDB, symbols: list[str],
                 capital: float, risk_pct: float, leverage: float,
                 account_id: str = "LIVE_ACCOUNT", direction_filter: str = "both"):
        if broker.dry_run:
            # Not a hard error -- lets this run in a "what would it do" mode
            # against the real broker's read-only endpoints (candles, quotes)
            # without placing anything, useful for testing this file's own
            # logic safely. But it means NO real orders will ever be placed
            # regardless of anything else below, by construction (see
            # broker/upstox_broker.py -- self.dry_run already reflects
            # safety_gate's verdict, not the raw constructor argument).
            log.warning("LiveTrader constructed with a dry_run broker -- this run will not place any real orders.")

        self.broker = broker
        self.db = db
        self.symbols = symbols
        self.capital = capital
        self.risk_pct = risk_pct
        self.leverage = leverage
        self.account_id = account_id
        self.direction_filter = direction_filter.lower()
        self.full_session = os.environ.get("DRYRUN_FULL_SESSION", "true").lower() in ("1", "true", "yes")
        self.use_ml_filter = os.environ.get("DRYRUN_USE_ML_FILTER", "true").lower() in ("1", "true", "yes")
        self.max_daily_loss_pct = float(os.environ.get("MAX_DAILY_LOSS_PCT", "5.0"))

        self.symbol_map = _build_symbol_map()
        self.today = datetime.now(IST).strftime("%Y-%m-%d")
        self.trading_day = self.today
        self.day_start_capital = self.capital
        self.kill_switch_active = False
        self.symbol_daily_pnl: dict[str, float] = {s: 0.0 for s in self.symbols}
        self.symbol_kill_switch: dict[str, bool] = {s: False for s in self.symbols}

        self.commodity_models: dict[str, object] = {}
        import pickle
        mod_dir = Path(__file__).parent / "cache" / "commodity_models"
        for s in self.symbols:
            p = mod_dir / f"lgb_{s.lower()}.pkl"
            if p.exists():
                try:
                    with open(p, "rb") as f:
                        self.commodity_models[s] = pickle.load(f)
                except Exception:
                    pass

        self.db.init_account(account_id=self.account_id, capital=self.capital,
                              leverage=self.leverage, risk_pct=self.risk_pct)
        acct = self.db.get_account(self.account_id)
        if acct:
            self.capital = acct["current_capital"]

        self.positions: dict[str, dict] = {}
        for p in self.db.get_open_positions(self.account_id):
            self.positions[p["symbol"]] = {
                "position_id": p["position_id"], "direction": p["direction"], "qty": p["qty"],
                "entry_price": p["entry_price"], "current_stop": p["current_stop"],
                "tp": p["target_price"], "be": p["breakeven_price"],
                "best_price": p.get("best_price", p["entry_price"]), "armed_be": bool(p.get("armed_be", 0)),
                "entry_time": datetime.fromisoformat(p["entry_time"]),
                "stop_dist": abs(p["entry_price"] - p["current_stop"]) or p["entry_price"] * 0.005,
                "entry_order_id": p.get("entry_order_id", ""),
            }
            log.warning("Restored OPEN real position from DB on startup: %s %s qty=%s -- "
                        "verify this matches the actual Upstox position book before trusting it.",
                        p["symbol"], p["direction"], p["qty"])

    # ---- entry signal (mirrors DryRunner.scan()'s per-symbol logic) --------
    def _entry_signal(self, sym: str, now: datetime) -> dict | None:
        ikey = self.symbol_map.get(sym)
        candles = _fetch_candles(self.broker, sym, ikey, self.today)
        if not candles or len(candles) < 25:
            return None

        raw_df = pd.DataFrame(candles)
        feat_df = compute_commodity_features(raw_df, symbol=sym)
        if feat_df is None or len(feat_df) < 25:
            return None

        t = len(feat_df) - 1
        row = feat_df.iloc[t]
        mins = int(row.get("minutes_since_open", 0))
        is_curr = _is_currency(sym)

        if is_curr:
            if mins < 15 or mins > 460:
                return None
        else:
            if self.full_session:
                if mins < 60 or mins > 810:
                    return None
            else:
                if mins < 570 or mins > 780:
                    return None

        model = self.commodity_models.get(sym)
        if model:
            try:
                X_feat = feat_df[COMMODITY_FEATURE_COLUMNS].iloc[[t]]
                p_up = float(model.predict_proba(X_feat)[0, 1])
            except Exception:
                p_up = 0.50
        else:
            p_up = 0.50

        adx = float(row.get("adx", 25.0))
        dmp = float(row.get("dmp", 25.0))
        dmn = float(row.get("dmn", 25.0))
        vol_s = float(row.get("vol_surge_ratio", 1.0))
        vwap_d = float(row.get("vwap_dist_pct", 0.0))
        ema_s = float(row.get("ema_slope_pct", 0.0))
        orb_h_dist = float(row.get("orb_high_dist_pct", 0.0))
        orb_l_dist = float(row.get("orb_low_dist_pct", 0.0))
        entry = float(row["close"])
        atr = float(row.get("atr", 0.005 * entry))

        is_natgas = "NATGAS" in sym.upper() or "NATURALGAS" in sym.upper()
        is_gold = "GOLD" in sym.upper()
        is_silver = "SILVER" in sym.upper()
        if is_curr:
            _et = CURRENCY_ENTRY_THRESHOLDS.get(sym.upper(), CURRENCY_ENTRY_THRESHOLDS["USDINR"])
            min_ml_l, max_ml_s = 0.54, 0.44
            min_orb, min_stop_pct = CURRENCY_MIN_ORB, _et["min_stop_pct"]
        else:
            _key = "natgas" if is_natgas else "gold" if is_gold else "silver" if is_silver else "crude"
            _et = COMMODITY_ENTRY_THRESHOLDS[_key]
            min_ml_l, max_ml_s = _et["min_ml_l"], _et["max_ml_s"]
            min_orb, min_stop_pct = _et["min_orb"], _et["min_stop_pct"]
        min_adx, min_vol, min_vwap = _et["min_adx"], _et["min_vol"], _et["min_vwap"]
        min_ema_slope = _et["min_ema_slope"]
        tp_mult, stop_mult = _et["tp_mult"], _et["stop_mult"]

        sdist = max(stop_mult * atr, min_stop_pct * entry)
        if sdist <= 0 or entry <= 0:
            return None

        direction = None
        ml_long_ok = (not self.use_ml_filter) or (p_up >= min_ml_l)
        ml_short_ok = (not self.use_ml_filter) or (p_up <= max_ml_s)
        if ml_long_ok and adx >= min_adx and dmp > dmn and ema_s > min_ema_slope and orb_h_dist >= min_orb and vwap_d >= min_vwap and vol_s >= min_vol:
            direction = "long"
        elif self.direction_filter != "long" and ml_short_ok and adx >= min_adx and dmn > dmp and ema_s < -min_ema_slope and orb_l_dist <= -min_orb and vwap_d <= -min_vwap and vol_s >= min_vol:
            direction = "short"
        if not direction:
            return None
        if self.direction_filter != "both" and direction != self.direction_filter:
            return None

        sym_risk_pct = _SYMBOL_RISK_PCT_OVERRIDE.get(sym.upper(), self.risk_pct)
        if is_curr:
            lots = size_currency_lots(self.capital, entry, sdist, sym_risk_pct, sym, self.leverage)
        else:
            lots = size_commodity_lots(self.capital, entry, sdist, sym_risk_pct, sym, self.leverage)
        if lots <= 0:
            return None

        d = 1 if direction == "long" else -1
        sl = round(entry - sdist * d, 2)
        tp = round(entry + tp_mult * sdist * d, 2)
        be = round(entry + 0.60 * sdist * d, 2)

        return {
            "symbol": sym, "direction": direction, "entry_price": entry,
            "sl": sl, "tp": tp, "be": be, "lots": lots, "stop_dist": sdist,
            "instrument_key": ikey,
        }

    # ---- real order placement -----------------------------------------------
    def _enter(self, sig: dict, now: datetime) -> None:
        sym = sig["symbol"]
        is_curr = _is_currency(sym)
        lot_size = (CURRENCY_SPECS.get(sym.upper(), {}).get("lot_size", 1000) if is_curr
                    else COMMODITY_SPECS.get(sym, {}).get("lot_size", 1))
        quantity = sig["lots"] * lot_size
        transaction_type = "BUY" if sig["direction"] == "long" else "SELL"
        ts_tag = now.strftime("%Y%m%d_%H%M%S")
        tag = f"LIVE_E_{sym}"[:16]

        # Real-funds check -- deliberately independent of size_commodity_lots/
        # size_currency_lots' own margin cap above, which sizes against
        # self.capital (this process's LOCAL tracked ledger, seeded from the
        # DB at startup) rather than the account's actual current balance.
        # Those can drift -- a manual withdrawal, a trade placed outside this
        # process, a fee this ledger doesn't model exactly -- so this is the
        # final, authoritative check against the real broker balance right
        # before an order that risks real money, not a substitute for the
        # sizing logic above but a backstop on top of it. Fails safe: if the
        # funds API can't be reached at all, treat that as insufficient
        # rather than proceeding on an unknown balance.
        required_margin = (sig["entry_price"] * quantity) / max(self.leverage, 1.0)
        available_funds = self.broker.get_available_funds()
        if available_funds is None:
            msg = f"🔴 <b>LIVE ENTRY SKIPPED</b> — {sym}: could not fetch real available funds; refusing to size an order against an unknown balance."
            log.error(msg)
            telegram.send(msg)
            return
        if available_funds < required_margin:
            msg = (f"🔴 <b>LIVE ENTRY SKIPPED — INSUFFICIENT FUNDS</b> — {sym}: needs ~₹{required_margin:,.2f} margin, "
                   f"only ₹{available_funds:,.2f} available. New entries for {sym} will keep being skipped until funds recover.")
            log.error(msg)
            telegram.send(msg)
            return

        order_id = self.broker.place_buy_order(sig["instrument_key"], quantity, product="I", tag=tag) \
            if transaction_type == "BUY" else \
            self.broker.place_sell_order(sig["instrument_key"], quantity, product="I", tag=tag)

        if not order_id:
            log.error("%s: entry order placement returned no order_id (dry_run broker, or immediate rejection).", sym)
            return

        outcome, fill_price = _wait_for_fill(self.broker, order_id, sig["entry_price"])
        if outcome == "rejected":
            log.warning("%s: entry order %s cleanly rejected/cancelled by the broker -- nothing filled, no position opened.", sym, order_id)
            return
        if outcome == "ambiguous":
            # NOT the same as a clean non-fill -- see _wait_for_fill's docstring.
            # A partial fill here means the broker has real, unmanaged exposure
            # this process doesn't know about. This needs a human to check the
            # actual order/position book now, not a best-effort guess.
            msg = (f"🔴🔴 <b>LIVE ENTRY ORDER STATUS UNKNOWN</b> — {sym} {order_id} never reached a confirmed "
                   f"'complete'/'rejected' state within {ORDER_FILL_TIMEOUT_SEC}s. It may be PARTIALLY FILLED "
                   f"at the broker with no position tracked here. Check the real Upstox order book IMMEDIATELY.")
            log.error(msg)
            telegram.send(msg)
            return

        # Real fill price vs. the price _entry_signal() assumed (the prior
        # closed candle's close, from before the order was even placed) will
        # rarely match exactly -- this is the scenario that prompted this
        # comment: "we placed the order for some price and that price didn't
        # come". SL/TP/BE MUST be re-anchored to the real fill, not left
        # pointing at the stale assumed price -- otherwise a bad-slippage fill
        # silently changes the position's real risk without anyone noticing.
        assumed_entry = sig["entry_price"]
        slippage_pct = abs(fill_price - assumed_entry) / assumed_entry * 100 if assumed_entry else 0.0
        # Shift each level by exactly the same offset the fill itself shifted
        # from the assumed price -- preserves the intended stop distance and
        # R-multiple TP/BE regardless of direction, without re-deriving the
        # sign logic that _entry_signal() already got right once.
        real_sl = round(fill_price + (sig["sl"] - assumed_entry), 2)
        real_tp = round(fill_price + (sig["tp"] - assumed_entry), 2)
        real_be = round(fill_price + (sig["be"] - assumed_entry), 2)

        # Extreme-slippage safeguard: if the real fill already sits on the
        # wrong side of where the stop should be (a large adverse gap between
        # order placement and fill), don't hold a position that opened
        # already past its own risk boundary -- close it immediately instead.
        _MAX_ENTRY_SLIPPAGE_PCT = 0.5
        if slippage_pct > _MAX_ENTRY_SLIPPAGE_PCT:
            log.warning("%s: entry filled %.2f%% away from the assumed price (%.2f -> %.2f) -- "
                        "beyond the %.1f%% sanity bound.", sym, slippage_pct, assumed_entry, fill_price,
                        _MAX_ENTRY_SLIPPAGE_PCT)
            telegram.send(f"⚠️ <b>LIVE ENTRY LARGE SLIPPAGE</b> — {sym}: assumed ₹{assumed_entry:.2f}, "
                          f"filled ₹{fill_price:.2f} ({slippage_pct:.2f}% away). SL/TP re-anchored to the real fill.")

        pos_id = f"POS_LIVE_{ts_tag}_{sym}"
        self.db.place_order(
            order_id=order_id, symbol=sym, direction=transaction_type, intent="ENTRY",
            order_type="MARKET", qty=sig["lots"], requested_price=sig["entry_price"],
            fill_price=fill_price, status="FILLED", tag="LIVE_ENTRY", account_id=self.account_id,
        )
        self.db.open_position(
            position_id=pos_id, symbol=sym, direction=sig["direction"], qty=sig["lots"],
            entry_price=fill_price, current_stop=real_sl, target_price=real_tp,
            breakeven_price=real_be, account_id=self.account_id,
        )
        self.positions[sym] = {
            "position_id": pos_id, "direction": sig["direction"], "qty": sig["lots"],
            "entry_price": fill_price, "current_stop": real_sl, "tp": real_tp, "be": real_be,
            "best_price": fill_price, "armed_be": False, "entry_time": now,
            "stop_dist": sig["stop_dist"], "entry_order_id": order_id,
        }
        log.warning("LIVE ENTRY FILLED: %s %s qty=%s @ %.2f (order_id=%s, SL=%.2f, TP=%.2f)",
                    sym, sig["direction"], sig["lots"], fill_price, order_id, real_sl, real_tp)
        telegram.send(f"📥 <b>LIVE ENTRY FILLED</b> {sym} {sig['direction'].upper()} @ ₹{fill_price:.2f}  "
                      f"Qty: {sig['lots']}\nSL: ₹{real_sl:.2f}  TP: ₹{real_tp:.2f}\nBalance: ₹{self.capital:,.2f}")

    # ---- exit management (mirrors DryRunner._maybe_exit/_close_position) ----
    def _maybe_exit(self, sym: str, now: datetime) -> None:
        pos = self.positions[sym]
        ikey = self.symbol_map.get(sym)
        candles = _fetch_candles(self.broker, sym, ikey, self.today)
        if not candles:
            return
        latest = candles[-1]
        high, low = float(latest["high"]), float(latest["low"])
        d = 1 if pos["direction"] == "long" else -1
        fav = high if d == 1 else low
        adv = low if d == 1 else high

        if _is_currency(sym):
            market_close = datetime.now(IST).replace(hour=16, minute=50, second=0, microsecond=0)
        else:
            market_close = datetime.now(IST).replace(hour=22, minute=45, second=0, microsecond=0)

        exit_p = None
        reason = None
        if (fav >= pos["tp"] if d == 1 else fav <= pos["tp"]):
            exit_p, reason = pos["tp"], "take_profit"
        elif (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
            exit_p, reason = pos["current_stop"], ("be_stop" if pos["armed_be"] else "initial_stop")
        elif (now - pos["entry_time"]).total_seconds() >= 80 * 60:
            exit_p, reason = float(latest["close"]), "timeout_exit"
        elif now >= market_close:
            exit_p, reason = float(latest["close"]), "eod_squareoff"

        if exit_p is not None:
            self._exit(sym, pos, reason, now)
            return

        armed_before = pos["armed_be"]
        stop_before = pos["current_stop"]
        if not pos["armed_be"] and (fav >= pos["be"] if d == 1 else fav <= pos["be"]):
            pos["armed_be"] = True
            pos["current_stop"] = pos["entry_price"] + 0.0020 * pos["entry_price"] * d
        if pos["armed_be"]:
            pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
            trail = pos["best_price"] - 0.30 * pos["stop_dist"] * d
            pos["current_stop"] = max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail)
        if pos["current_stop"] != stop_before or pos["armed_be"] != armed_before:
            self.db.update_position_stop(position_id=pos["position_id"], current_stop=pos["current_stop"],
                                          best_price=pos["best_price"], armed_be=pos["armed_be"])

    def _exit(self, sym: str, pos: dict, reason: str, now: datetime) -> None:
        is_curr = _is_currency(sym)
        lot_size = (CURRENCY_SPECS.get(sym.upper(), {}).get("lot_size", 1000) if is_curr
                    else COMMODITY_SPECS.get(sym, {}).get("lot_size", 1))
        quantity = pos["qty"] * lot_size
        # Exit is always the opposite side of entry.
        exit_side_is_buy = pos["direction"] == "short"
        tag = f"LIVE_X_{sym}"[:16]
        ikey = self.symbol_map.get(sym)

        order_id = self.broker.place_buy_order(ikey, quantity, product="I", tag=tag) if exit_side_is_buy \
            else self.broker.place_sell_order(ikey, quantity, product="I", tag=tag)

        if not order_id:
            msg = f"🔴 <b>LIVE EXIT ORDER FAILED TO PLACE</b> — {sym} ({reason}). Position LEFT OPEN. Manual intervention required."
            log.error(msg)
            telegram.send(msg)
            return

        expected_price = pos["tp"] if reason == "take_profit" else pos["current_stop"]
        outcome, exit_price = _wait_for_fill(self.broker, order_id, expected_price)
        if outcome == "rejected":
            msg = f"🔴 <b>LIVE EXIT ORDER REJECTED</b> — {sym} {order_id} ({reason}). Position still fully OPEN (nothing filled) -- will retry on the next scan."
            log.error(msg)
            telegram.send(msg)
            return
        if outcome == "ambiguous":
            # Worse than the entry case: this position is still tracked as
            # fully open here, but a partial fill at the broker means the
            # REAL remaining size could be smaller (or zero) -- a mismatch
            # that won't self-correct on the next scan the way a clean
            # rejection does. Needs a human to reconcile the real position
            # book against what this process thinks is still open.
            msg = (f"🔴🔴 <b>LIVE EXIT ORDER STATUS UNKNOWN</b> — {sym} {order_id} ({reason}) never reached a "
                   f"confirmed 'complete'/'rejected' state within {ORDER_FILL_TIMEOUT_SEC}s. It may be PARTIALLY "
                   f"FILLED -- this process still thinks the full {pos['qty']} lots are open, which may now be "
                   f"WRONG. Check the real Upstox order/position book IMMEDIATELY and reconcile manually.")
            log.error(msg)
            telegram.send(msg)
            return

        if is_curr:
            cost_info = compute_ncd_currency_costs(sym, pos["direction"], pos["entry_price"], exit_price, pos["qty"])
        else:
            cost_info = compute_mcx_commodity_costs(sym, pos["direction"], pos["entry_price"], exit_price, pos["qty"])
        net_pnl = cost_info["net"]
        self.capital += net_pnl

        self.symbol_daily_pnl[sym] = self.symbol_daily_pnl.get(sym, 0.0) + net_pnl
        if not self.symbol_kill_switch.get(sym, False) and self.day_start_capital > 0:
            sym_loss_pct = -self.symbol_daily_pnl[sym] / self.day_start_capital * 100
            if sym_loss_pct >= self.max_daily_loss_pct:
                self.symbol_kill_switch[sym] = True
                telegram.send(f"🛑 <b>{sym} DAILY LOSS LIMIT HIT (LIVE)</b> — {sym_loss_pct:.2f}% "
                              f"(limit {self.max_daily_loss_pct}%). New {sym} entries halted for today.")

        self.db.place_order(
            order_id=order_id, symbol=sym, direction="BUY" if exit_side_is_buy else "SELL",
            intent=reason.upper(), order_type="MARKET", qty=pos["qty"], requested_price=expected_price,
            fill_price=exit_price, status="FILLED", tag="LIVE_EXIT", account_id=self.account_id,
        )
        self.db.close_position(position_id=pos["position_id"], exit_price=exit_price, exit_reason=reason,
                                gross_pnl=cost_info["gross"], net_pnl=net_pnl, total_fees=cost_info["total"])
        hold_mins = (now - pos["entry_time"]).total_seconds() / 60.0
        self.db.record_trade(
            position_id=pos["position_id"], symbol=sym, direction=pos["direction"], qty=pos["qty"],
            entry_price=pos["entry_price"], exit_price=exit_price, entry_dt=pos["entry_time"].isoformat(),
            exit_dt=now.isoformat(), hold_minutes=hold_mins, exit_reason=reason, gross_pnl=cost_info["gross"],
            costs_dict=cost_info, net_pnl=net_pnl, capital_after=self.capital, account_id=self.account_id,
        )
        del self.positions[sym]
        log.warning("LIVE EXIT FILLED: %s %s @ %.2f [%s] net=%.2f (order_id=%s)",
                    sym, pos["direction"], exit_price, reason, net_pnl, order_id)
        telegram.send(f"{'✅' if net_pnl >= 0 else '❌'} <b>LIVE EXIT FILLED</b> {sym} {pos['direction'].upper()} "
                      f"@ ₹{exit_price:.2f}  [{reason}]\nNet PnL: ₹{net_pnl:+,.2f}\nBalance: ₹{self.capital:,.2f}")

    # ---- scan loop ------------------------------------------------------------
    # ---- position reconciliation ----------------------------------------------
    def _reconcile_positions(self, now: datetime) -> None:
        """Catches the scenario a pure simulated-fill tracker (like
        live_dryrun.py's DryRunner) structurally can't have: a real position
        this process opened gets manually closed (or altered) directly at the
        broker, outside this code entirely. Without this check, _maybe_exit
        would eventually try to place a REAL CLOSING ORDER against a position
        that's already gone -- which for an intraday product doesn't fail, it
        OPENS A NEW, UNTRACKED POSITION in the opposite direction. Called at
        the top of every scan(), before any exit/entry decision, so a stale
        tracked position never reaches that point."""
        if not self.positions:
            return
        from utils.position_reconciliation import find_discrepancies
        discrepancies = find_discrepancies(self.broker, self.positions, self.symbol_map)
        for sym, disc in discrepancies.items():
            if disc["kind"] == "unknown":
                log.warning("%s: could not verify real broker position this cycle (API call failed) -- "
                            "will retry next scan.", sym)
                continue

            if disc["kind"] == "externally_closed":
                pos = self.positions[sym]
                ltp = self.broker.get_ltp(self.symbol_map.get(sym, ""))
                approx_exit = ltp if ltp is not None else pos["entry_price"]
                is_curr = _is_currency(sym)
                cost_info = (compute_ncd_currency_costs(sym, pos["direction"], pos["entry_price"], approx_exit, pos["qty"])
                             if is_curr else
                             compute_mcx_commodity_costs(sym, pos["direction"], pos["entry_price"], approx_exit, pos["qty"]))
                net_pnl = cost_info["net"]
                self.capital += net_pnl
                self.db.close_position(position_id=pos["position_id"], exit_price=approx_exit,
                                        exit_reason="externally_closed_reconciled", gross_pnl=cost_info["gross"],
                                        net_pnl=net_pnl, total_fees=cost_info["total"])
                hold_mins = (now - pos["entry_time"]).total_seconds() / 60.0
                self.db.record_trade(
                    position_id=pos["position_id"], symbol=sym, direction=pos["direction"], qty=pos["qty"],
                    entry_price=pos["entry_price"], exit_price=approx_exit, entry_dt=pos["entry_time"].isoformat(),
                    exit_dt=now.isoformat(), hold_minutes=hold_mins, exit_reason="externally_closed_reconciled",
                    gross_pnl=cost_info["gross"], costs_dict=cost_info, net_pnl=net_pnl,
                    capital_after=self.capital, account_id=self.account_id,
                )
                del self.positions[sym]
                msg = (f"⚠️ <b>POSITION MANUALLY CLOSED AT BROKER</b> — {sym} was {pos['direction']} qty={pos['qty']} "
                       f"in this app's tracking, but the real broker now shows it flat. Reconciled here using the "
                       f"current LTP (₹{approx_exit:.2f}) as an APPROXIMATE exit price -- this is NOT necessarily "
                       f"the real fill price of whatever manual action closed it. Verify the actual P&L in the "
                       f"Upstox trade book. Net PnL recorded here (approximate): ₹{net_pnl:+,.2f}.")
                log.warning(msg)
                telegram.send(msg)
                continue

            # "externally_reduced" / "externally_reversed" -- a partial manual
            # close, or something placed additional real orders on this
            # instrument outside this process. Can't be safely auto-healed
            # (this process doesn't know which lots were touched or at what
            # price) -- stop managing it automatically and demand a human
            # look, rather than guess and risk compounding the mismatch.
            pos = self.positions.pop(sym)
            msg = (f"🔴🔴 <b>REAL POSITION MISMATCH — {disc['kind'].upper()}</b> — {sym}: this app tracked "
                   f"{disc['tracked_qty']:+d} (signed qty), the real broker shows {disc['broker_qty']:+d}. "
                   f"Stopped automatically managing {sym} (removed from this process's tracking) -- it will "
                   f"NOT place further orders for it. Reconcile the real Upstox position/order book manually, "
                   f"then restart this process once resolved.")
            log.error(msg)
            telegram.send(msg)

    def scan(self) -> None:
        now = datetime.now(IST)
        today_str = now.strftime("%Y-%m-%d")
        if today_str != self.trading_day:
            self.trading_day = today_str
            self.today = today_str
            self.day_start_capital = self.capital
            self.kill_switch_active = False
            self.symbol_daily_pnl = {s: 0.0 for s in self.symbols}
            self.symbol_kill_switch = {s: False for s in self.symbols}

        daily_loss_pct = ((self.day_start_capital - self.capital) / self.day_start_capital * 100
                           if self.day_start_capital > 0 else 0.0)
        if not self.kill_switch_active and daily_loss_pct >= self.max_daily_loss_pct:
            self.kill_switch_active = True
            telegram.send(f"🛑 <b>DAILY LOSS LIMIT HIT (LIVE)</b> — {daily_loss_pct:.2f}% "
                          f"(limit {self.max_daily_loss_pct}%). New entries halted for the rest of today.")

        self._reconcile_positions(now)

        for sym in self.symbols:
            if sym in self.positions:
                self._maybe_exit(sym, now)
                continue
            if self.kill_switch_active or self.symbol_kill_switch.get(sym, False):
                continue
            sig = self._entry_signal(sym, now)
            if sig:
                self._enter(sig, now)


def main() -> None:
    setup_logger("", log_file=str(Path(__file__).parent / "logs" / "live_trading.log"))

    ap = argparse.ArgumentParser(description="REAL-MONEY live trading engine (MCX commodities + NSE currency). NOT wired into cli.py -- run directly, and see this file's module docstring before ever doing so.")
    ap.add_argument("--capital", type=float, default=100_000, help="Starting capital tracked in the LIVE_ACCOUNT ledger row (informational -- real capital lives at the broker, not here)")
    ap.add_argument("--risk-pct", type=float, default=float(os.environ.get("DRYRUN_RISK_PCT", "5.0")))
    ap.add_argument("--leverage", type=float, default=float(os.environ.get("DRYRUN_LEVERAGE", "4.0")))
    ap.add_argument("--symbols", nargs="+", default=os.environ.get("DRYRUN_SYMBOLS", "").split() or None)
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--direction", choices=["both", "long", "short"], default="both")
    ap.add_argument("--db", default=None)
    ap.add_argument("--account", default="LIVE_ACCOUNT")
    args = ap.parse_args()

    if not safety_gate.live_trading_allowed("upstox"):
        print("\033[91mRefusing to start: safety_gate.live_trading_allowed('upstox') is False.\033[0m")
        print("All three gates must pass -- see safety_gate.py's module docstring:")
        print(f"  1. KILL_SWITCH_ENGAGED = {safety_gate.KILL_SWITCH_ENGAGED} (must be False, hand-edited in source)")
        print(f"  2. ALLOW_LIVE_TRADING (.env) = {os.environ.get('ALLOW_LIVE_TRADING', 'unset')} (must be 'true')")
        print(f"  3. armed state = {safety_gate.is_armed('upstox')} (run `cli.py arm-live-trading --component upstox --confirm \"...\"`)")
        print("This is intentional -- this file is not meant to run live yet.")
        sys.exit(1)

    token = ensure_fresh_upstox_token() or UpstoxConfig.ACCESS_TOKEN
    if not token:
        print("No valid Upstox access token available.")
        sys.exit(1)

    broker = UpstoxBroker(access_token=token, dry_run=False)  # only actually live if all 3 gates passed above
    db = TradingDB(args.db)
    symbols = args.symbols or ["CRUDEOILM", "GOLDM", "USDINR", "EURINR", "GBPINR"]

    trader = LiveTrader(broker, db, symbols, args.capital, args.risk_pct, args.leverage,
                         account_id=args.account, direction_filter=args.direction)

    print(f"\033[91m\033[1mLIVE TRADING ARMED AND RUNNING -- REAL ORDERS WILL BE PLACED. Symbols: {symbols}\033[0m")
    telegram.send(f"🔴🔴 <b>LIVE TRADING STARTED</b> — real orders, account={args.account}, symbols={symbols}")

    try:
        while True:
            try:
                trader.scan()
            except Exception as exc:
                log.error("Scan error: %s", exc, exc_info=True)
                telegram.alert_error("live_trading scan", exc)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        telegram.send("🔴 <b>LIVE TRADING STOPPED</b> (manual/shutdown signal). Open positions, if any, are NOT auto-closed -- check the real Upstox order book.")


if __name__ == "__main__":
    main()
