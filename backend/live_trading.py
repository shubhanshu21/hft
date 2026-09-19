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
    still runs live_dryrun.py, behavior unchanged -- see below).
Grep for "live_trading" outside this file to confirm neither of the above
changes before ever touching that.

live_dryrun.py WAS touched, once, on 2026-09-18: its entry-decision logic
used to be duplicated here almost verbatim (the exact "keep two files in
sync by hand" drift risk ENTRY_THRESHOLDS' own extraction eliminated one
layer up). Both files now call the same shared strategy.entry_signal.
compute_entry_signal() instead. This changed live_dryrun.py's SOURCE, not
its BEHAVIOR -- verified by running its DryRunner.scan() directly before and
after and confirming identical output, plus the full test suite (23/23) and
a live service restart, before trusting the change. The paper daemon is
NOT wired to this file and never calls anything defined here.
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
  - An order that doesn't reach 'complete' within ORDER_FILL_TIMEOUT_SEC
    triggers an active broker.cancel_order() attempt (added 2026-09-18)
    before giving up, not just a flag-and-wait. Most timeouts resolve
    cleanly this way (cancelled with nothing filled, or it turns out to
    have filled anyway in the race). If it's STILL unresolved after that
    (Upstox's order-status values don't distinguish every intermediate/
    partial-fill state, and get_fill_price() only reports a price for a
    fully-'complete' order), it's flagged loudly for a human to check the
    real Upstox order book -- this module does not guess at a partial-fill
    quantity it can't actually see.
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

import safety_gate
from auth.upstox_auto_login import ensure_fresh_upstox_token
from broker.upstox_broker import UpstoxBroker, token_invalid_event
from config import UpstoxConfig
from database import TradingDB
from strategy.commodity_costs import compute_mcx_commodity_costs, COMMODITY_SPECS
from strategy.currency_costs import compute_ncd_currency_costs, CURRENCY_SPECS
from strategy.entry_signal import compute_entry_signal, is_currency as _is_currency
from strategy.equity_entry_signal import (
    compute_equity_entry_signal, is_equity as _is_equity,
    MAX_CONCURRENT_EQUITY_POSITIONS, BE_LOCK_BUFFER_PCT as EQUITY_BE_LOCK_BUFFER_PCT,
)
from strategy.equity_costs import compute_nse_equity_costs
from strategy.equity_features import compute_equity_features
from strategy.equity_universe import NIFTY50_SYMBOLS
from broker.instruments import build_mcx_commodity_map, build_currency_map, get_instrument_key
import pandas as pd
from utils.logger import get_logger, setup_logger
from utils import telegram

IST = ZoneInfo("Asia/Kolkata")
log = get_logger("live_trading")


# Same per-symbol risk override as live_dryrun.py -- see that file's comment
# (SILVER re-added 2026-09-18 at half risk pending more live experience;
# CRUDEOILM cut to 3.0 on 2026-09-19 after the full real archive showed the
# deployed thresholds are a net loser overall at full risk -- mirrored here
# 2026-09-19 to keep this file in sync, though it stays fully unwired
# regardless). NOTE: live_dryrun.py's crude fix also requires
# USE_CRUDE_REGIME_FILTER (a separate daily-regime check computed from real
# candle history) to reach the backtested +17.20%/-37.96% DD result -- this
# file has NO equivalent regime-filter wiring yet. Risk_pct alone without the
# regime filter does NOT reproduce the validated fix; treat CRUDEOILM here as
# still using the pre-fix (net-losing-overall) entry logic until that gap is
# closed, on top of this module being completely unwired regardless.
_SYMBOL_RISK_PCT_OVERRIDE = {"SILVER": 5.0, "CRUDEOILM": 3.0}

# Same per-symbol leverage override as live_dryrun.py -- see that file's
# comment. Added 2026-09-19: GBPINR's under-proven-sample sizing fix has to
# go through leverage, not risk_pct, because margin sizing (not risk sizing)
# binds every one of its real trades -- a risk_pct override is a confirmed
# no-op for it (see live_dryrun.py's _SYMBOL_LEVERAGE_OVERRIDE comment for
# the full story).
_SYMBOL_LEVERAGE_OVERRIDE = {"GBPINR": 3.5}

# How long to wait for a real order to reach 'complete' before attempting to
# cancel it (see _wait_for_fill). Market orders on liquid MCX/NCD_FO
# contracts should fill in well under this; a slower fill is itself a signal
# something's off.
ORDER_FILL_TIMEOUT_SEC = 30
ORDER_POLL_INTERVAL_SEC = 2
# How long to re-poll after attempting cancel_order() before giving up and
# reporting "ambiguous" -- gives the cancel a real chance to be reflected in
# order status, or lets a fill that raced the cancel show up as 'complete'.
ORDER_CANCEL_RECHECK_SEC = 10


def _build_symbol_map() -> dict[str, str]:
    """Same rollover-safe resolution as live_dryrun.py's own _build_symbol_map --
    duplicated here deliberately rather than imported, since this module must
    stay import-independent of live_dryrun.py (see module docstring: nothing
    should couple this file's behavior to the paper-trading daemon's).
    Equity resolution added 2026-09-19 alongside live_dryrun.py's own equity
    wiring -- NSE_EQ symbols have no monthly-expiry rollover concern, so this
    part of the map never actually changes across calls, but it's rebuilt
    unconditionally here anyway for the same simplicity live_dryrun.py uses."""
    equity_map = {}
    for sym in NIFTY50_SYMBOLS:
        key = get_instrument_key(sym)
        if key:
            equity_map[sym] = key
    try:
        return {**build_mcx_commodity_map(), **build_currency_map(), **equity_map}
    except Exception as exc:
        log.warning("Could not build symbol map: %s", exc)
        return equity_map


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
    failure status, or timeout -- and, added 2026-09-18 once
    UpstoxBroker.cancel_order() existed, attempts to actively resolve a
    timeout instead of just flagging it. Returns (outcome, fill_price):
      - ("filled", price)   -- confirmed complete, price is the real fill.
      - ("rejected", 0.0)   -- broker cleanly rejected/cancelled the order
        (either on its own, or via our own cancel attempt below) with
        nothing filled; safe to treat as "never happened".
      - ("ambiguous", 0.0)  -- STILL not resolved even after attempting to
        cancel it. Upstox's order-history status values include
        intermediate states (partial fills, 'open', 'trigger pending') this
        doesn't enumerate, and get_fill_price() only ever reports a price
        for a fully-'complete' order (see its own docstring) -- so a partial
        fill followed by our cancel of the remainder can still land here
        with real quantity filled at the broker that this process can't see.
        Callers must NOT treat this the same as a clean non-fill."""
    deadline = time.monotonic() + ORDER_FILL_TIMEOUT_SEC
    while time.monotonic() < deadline:
        status = broker.get_order_status(order_id)
        if status in ("rejected", "cancelled"):
            return "rejected", 0.0
        if status == "complete":
            fill_price = broker.get_fill_price(order_id)
            return "filled", (fill_price if fill_price is not None else requested_price)
        time.sleep(ORDER_POLL_INTERVAL_SEC)

    # Timed out still pending -- actively try to resolve it rather than just
    # reporting "unknown". A cancel request on an order that's already fully
    # filled by the time it reaches the exchange is simply rejected by
    # Upstox, so this is safe to attempt unconditionally.
    log.warning("Order %s still not resolved after %ds -- attempting cancel_order() before giving up.",
                order_id, ORDER_FILL_TIMEOUT_SEC)
    broker.cancel_order(order_id)
    recheck_deadline = time.monotonic() + ORDER_CANCEL_RECHECK_SEC
    while time.monotonic() < recheck_deadline:
        status = broker.get_order_status(order_id)
        if status in ("rejected", "cancelled"):
            return "rejected", 0.0
        if status == "complete":
            # Raced with our own cancel -- it filled before the cancel took
            # effect. Treat as a normal fill, same as the first loop above.
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

        # CRUDEOILM regime gate -- mirrors live_dryrun.py's DryRunner exactly
        # (see that file's __init__ and _refresh_crude_regime for the full
        # finding/rationale). Added 2026-09-19, the same day as this file's
        # _SYMBOL_RISK_PCT_OVERRIDE CRUDEOILM entry -- without this, that
        # risk_pct cut alone does NOT reproduce the validated backtest result
        # (net +17.20%/-37.96% DD requires BOTH the reduced risk_pct AND this
        # filter together; risk_pct alone leaves the pre-fix, net-losing-
        # overall entry logic in place). This module stays fully unwired
        # regardless of this fix -- see module docstring.
        self.use_crude_regime_filter = os.environ.get("USE_CRUDE_REGIME_FILTER", "false").lower() in ("1", "true", "yes")
        self.crude_regime_ok: bool | None = True
        if self.use_crude_regime_filter:
            self._refresh_crude_regime()

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
                # The positions table has no instrument_key column (this
                # process's own real-order additions came after that schema
                # was fixed) -- a position restored across a process restart
                # gets re-anchored to whatever's CURRENTLY correct at startup
                # (fine, since restarts happen between trading days, not
                # mid-position, in normal operation). See _enter()'s comment
                # for why this matters at all.
                "instrument_key": self.symbol_map.get(p["symbol"]),
            }
            log.warning("Restored OPEN real position from DB on startup: %s %s qty=%s -- "
                        "verify this matches the actual Upstox position book before trusting it.",
                        p["symbol"], p["direction"], p["qty"])

    # ---- CRUDEOILM regime gate (mirrors DryRunner._refresh_crude_regime exactly) ----
    def _refresh_crude_regime(self) -> None:
        """Fetches real daily candles through YESTERDAY (never today's own
        still-forming price -- causal by construction) and recomputes
        self.crude_regime_ok. Fails open (leaves the previous value in
        place, or True on the very first call) if the fetch fails -- a
        broker hiccup should never silently start blocking every crude
        entry for the day."""
        ikey = self.symbol_map.get("CRUDEOILM")
        if not ikey:
            return
        yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            candles = self.broker.get_historical_candles(ikey, unit="days", interval=1, to_date=yesterday)
        except Exception as exc:
            log.warning("Could not fetch daily candles for crude regime gate: %s", exc)
            return
        if not candles:
            return
        closes = [float(c["close"]) for c in sorted(candles, key=lambda c: c["timestamp"])]
        from strategy.regime import regime_ok as _regime_ok
        self.crude_regime_ok = _regime_ok(closes, window=15, min_autocorr=0.0)
        if self.crude_regime_ok is False:
            log.warning("Crude regime gate: BLOCKED for today (recent daily-return autocorrelation < 0).")

    # ---- entry signal (mirrors DryRunner.scan()'s per-symbol logic) --------
    def _entry_signal(self, sym: str, now: datetime) -> dict | None:
        """Delegates to strategy.entry_signal.compute_entry_signal -- the
        single shared decision function live_dryrun.py's DryRunner also
        calls, eliminating the duplicated-logic drift risk this method used
        to carry on its own (see that module's docstring)."""
        ikey = self.symbol_map.get(sym)
        candles = _fetch_candles(self.broker, sym, ikey, self.today)
        regime_ok = self.crude_regime_ok if (self.use_crude_regime_filter and sym.upper() == "CRUDEOILM") else True
        sig = compute_entry_signal(
            sym, candles, ikey, self.commodity_models, self.use_ml_filter,
            self.full_session, self.direction_filter, self.capital,
            _SYMBOL_RISK_PCT_OVERRIDE.get(sym.upper(), self.risk_pct),
            _SYMBOL_LEVERAGE_OVERRIDE.get(sym.upper(), self.leverage),
            regime_ok=regime_ok,
        )
        return sig

    # ---- NSE equity: separate entry/exit path, same reasoning as
    # live_dryrun.py's DryRunner._maybe_enter_equity/_maybe_exit_equity (see
    # strategy/equity_entry_signal.py's module docstring for why equity can't
    # share the commodity/currency shape -- no fixed take-profit, dynamic
    # ADX-scaled trailing instead). This module stays fully unwired regardless
    # (see module docstring) -- adding equity here keeps it in sync with the
    # paper-trading daemon in case it's ever armed, it does not make this file
    # any less inert on its own. ------------------------------------------
    def _entry_signal_equity(self, sym: str, now: datetime) -> dict | None:
        ikey = self.symbol_map.get(sym)
        candles = _fetch_candles(self.broker, sym, ikey, self.today)
        return compute_equity_entry_signal(
            sym, candles, ikey, self.capital, self.risk_pct, self.leverage,
            direction_filter=self.direction_filter,
        )

    def _enter_equity(self, sig: dict, now: datetime) -> None:
        sym = sig["symbol"]
        quantity = sig["qty"]
        transaction_type = "BUY" if sig["direction"] == "long" else "SELL"
        ts_tag = now.strftime("%Y%m%d_%H%M%S")
        tag = f"LIVE_E_{sym}"[:16]

        # Same real-funds backstop as _enter() above -- see that method's
        # comment for why this is independent of size_equity_shares' own cap.
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
            msg = (f"🔴🔴 <b>LIVE ENTRY ORDER STATUS UNKNOWN</b> — {sym} {order_id} never reached a confirmed "
                   f"'complete'/'rejected' state within {ORDER_FILL_TIMEOUT_SEC}s. It may be PARTIALLY FILLED "
                   f"at the broker with no position tracked here. Check the real Upstox order book IMMEDIATELY.")
            log.error(msg)
            telegram.send(msg)
            return

        # Re-anchor SL and activation_price to the real fill, same reasoning
        # as _enter() -- but no fixed TP to re-anchor here (equity has none,
        # see module docstring); the trailing exit naturally starts fresh
        # from whatever the real fill price actually was.
        assumed_entry = sig["entry_price"]
        slippage_pct = abs(fill_price - assumed_entry) / assumed_entry * 100 if assumed_entry else 0.0
        real_sl = round(fill_price + (sig["sl"] - assumed_entry), 4)
        real_activation = round(fill_price + (sig["activation_price"] - assumed_entry), 4)

        _MAX_ENTRY_SLIPPAGE_PCT = 0.5
        if slippage_pct > _MAX_ENTRY_SLIPPAGE_PCT:
            log.warning("%s: entry filled %.2f%% away from the assumed price (%.2f -> %.2f) -- "
                        "beyond the %.1f%% sanity bound.", sym, slippage_pct, assumed_entry, fill_price,
                        _MAX_ENTRY_SLIPPAGE_PCT)
            telegram.send(f"⚠️ <b>LIVE ENTRY LARGE SLIPPAGE</b> — {sym}: assumed ₹{assumed_entry:.2f}, "
                          f"filled ₹{fill_price:.2f} ({slippage_pct:.2f}% away). SL re-anchored to the real fill.")

        pos_id = f"POS_LIVE_EQ_{ts_tag}_{sym}"
        self.db.place_order(
            order_id=order_id, symbol=sym, direction=transaction_type, intent="ENTRY",
            order_type="MARKET", qty=quantity, requested_price=sig["entry_price"],
            fill_price=fill_price, status="FILLED", tag="LIVE_ENTRY", account_id=self.account_id,
        )
        # positions.target_price is NOT NULL and equity has no fixed profit
        # target -- store activation_price there instead, same repurposing
        # live_dryrun.py's DryRunner uses.
        self.db.open_position(
            position_id=pos_id, symbol=sym, direction=sig["direction"], qty=quantity,
            entry_price=fill_price, current_stop=real_sl, target_price=real_activation,
            breakeven_price=real_activation, account_id=self.account_id,
        )
        self.positions[sym] = {
            "position_id": pos_id, "direction": sig["direction"], "qty": quantity,
            "entry_price": fill_price, "current_stop": real_sl, "activation_price": real_activation,
            "trail_mult": sig["trail_mult"], "best_price": fill_price, "armed_trail": False,
            "entry_time": now, "stop_dist": sig["stop_dist"], "entry_order_id": order_id,
            "instrument_key": sig["instrument_key"],
        }
        log.warning("LIVE ENTRY FILLED (EQUITY): %s %s qty=%s @ %.2f (order_id=%s, SL=%.2f)",
                    sym, sig["direction"], quantity, fill_price, order_id, real_sl)
        remaining_funds = self.broker.get_available_funds()
        funds_line = f"\nReal Funds Remaining: ₹{remaining_funds:,.2f}" if remaining_funds is not None else \
            "\nReal Funds Remaining: (could not fetch)"
        telegram.send(f"📥 <b>LIVE ENTRY FILLED</b> {sym} {sig['direction'].upper()} @ ₹{fill_price:.2f}  "
                      f"Qty: {quantity}\nSL: ₹{real_sl:.2f} (dynamic trailing, no fixed TP){funds_line}\n"
                      f"Balance (realized equity): ₹{self.capital:,.2f}")

    def _maybe_exit_equity(self, sym: str, now: datetime) -> None:
        pos = self.positions[sym]
        ikey = pos["instrument_key"]
        candles = _fetch_candles(self.broker, sym, ikey, self.today)
        if not candles or len(candles) < 25:
            return
        raw_df = pd.DataFrame(candles)
        feat_df = compute_equity_features(raw_df)
        latest = candles[-1]
        high, low = float(latest["high"]), float(latest["low"])
        cur_atr = float(feat_df["atr"].iloc[-1]) if len(feat_df) else pos["stop_dist"]
        d = 1 if pos["direction"] == "long" else -1
        fav = high if d == 1 else low
        adv = low if d == 1 else high

        market_close = datetime.now(IST).replace(hour=15, minute=15, second=0, microsecond=0)

        exit_p = None; reason = None
        if (adv <= pos["current_stop"] if d == 1 else adv >= pos["current_stop"]):
            exit_p, reason = pos["current_stop"], ("trail_stop" if pos["armed_trail"] else "initial_stop")
        elif (now - pos["entry_time"]).total_seconds() >= 80 * 60:
            exit_p, reason = float(latest["close"]), "timeout_exit"
        elif now >= market_close:
            exit_p, reason = float(latest["close"]), "eod_squareoff"

        if exit_p is not None:
            self._exit_equity(sym, pos, reason, now)
            return

        armed_before = pos["armed_trail"]
        stop_before = pos["current_stop"]
        if not pos["armed_trail"] and (fav >= pos["activation_price"] if d == 1 else fav <= pos["activation_price"]):
            pos["armed_trail"] = True
            pos["current_stop"] = pos["entry_price"] + EQUITY_BE_LOCK_BUFFER_PCT * pos["entry_price"] * d
        if pos["armed_trail"]:
            pos["best_price"] = max(pos["best_price"], fav) if d == 1 else min(pos["best_price"], fav)
            trail_dist = pos["trail_mult"] * max(cur_atr, pos["stop_dist"] * 0.1)
            trail = pos["best_price"] - trail_dist * d
            pos["current_stop"] = max(pos["current_stop"], trail) if d == 1 else min(pos["current_stop"], trail)
        if pos["current_stop"] != stop_before or pos["armed_trail"] != armed_before:
            self.db.update_position_stop(position_id=pos["position_id"], current_stop=pos["current_stop"],
                                          best_price=pos["best_price"], armed_be=pos["armed_trail"])

    def _exit_equity(self, sym: str, pos: dict, reason: str, now: datetime) -> None:
        quantity = pos["qty"]
        exit_side_is_buy = pos["direction"] == "short"
        tag = f"LIVE_X_{sym}"[:16]
        ikey = pos["instrument_key"]

        order_id = self.broker.place_buy_order(ikey, quantity, product="I", tag=tag) if exit_side_is_buy \
            else self.broker.place_sell_order(ikey, quantity, product="I", tag=tag)
        if not order_id:
            msg = f"🔴 <b>LIVE EXIT ORDER FAILED TO PLACE</b> — {sym} ({reason}). Position LEFT OPEN. Manual intervention required."
            log.error(msg)
            telegram.send(msg)
            return

        outcome, exit_price = _wait_for_fill(self.broker, order_id, pos["current_stop"])
        if outcome == "rejected":
            msg = f"🔴 <b>LIVE EXIT ORDER REJECTED</b> — {sym} {order_id} ({reason}). Position still fully OPEN (nothing filled) -- will retry on the next scan."
            log.error(msg)
            telegram.send(msg)
            return
        if outcome == "ambiguous":
            msg = (f"🔴🔴 <b>LIVE EXIT ORDER STATUS UNKNOWN</b> — {sym} {order_id} ({reason}) never reached a "
                   f"confirmed 'complete'/'rejected' state within {ORDER_FILL_TIMEOUT_SEC}s. It may be PARTIALLY "
                   f"FILLED -- this process still thinks the full {quantity} shares are open, which may now be "
                   f"WRONG. Check the real Upstox order/position book IMMEDIATELY and reconcile manually.")
            log.error(msg)
            telegram.send(msg)
            return

        cost_info = compute_nse_equity_costs(pos["direction"], pos["entry_price"], exit_price, quantity)
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
            intent=reason.upper(), order_type="MARKET", qty=quantity, requested_price=pos["current_stop"],
            fill_price=exit_price, status="FILLED", tag="LIVE_EXIT", account_id=self.account_id,
        )
        self.db.close_position(position_id=pos["position_id"], exit_price=exit_price, exit_reason=reason,
                                gross_pnl=cost_info["gross"], net_pnl=net_pnl, total_fees=cost_info["total"])
        hold_mins = (now - pos["entry_time"]).total_seconds() / 60.0
        self.db.record_trade(
            position_id=pos["position_id"], symbol=sym, direction=pos["direction"], qty=quantity,
            entry_price=pos["entry_price"], exit_price=exit_price, entry_dt=pos["entry_time"].isoformat(),
            exit_dt=now.isoformat(), hold_minutes=hold_mins, exit_reason=reason, gross_pnl=cost_info["gross"],
            costs_dict=cost_info, net_pnl=net_pnl, capital_after=self.capital, account_id=self.account_id,
        )
        del self.positions[sym]
        log.warning("LIVE EXIT FILLED (EQUITY): %s %s @ %.2f [%s] net=%.2f (order_id=%s)",
                    sym, pos["direction"], exit_price, reason, net_pnl, order_id)
        telegram.send(f"{'✅' if net_pnl >= 0 else '❌'} <b>LIVE EXIT FILLED</b> {sym} {pos['direction'].upper()} "
                      f"@ ₹{exit_price:.2f}  [{reason}]\nNet PnL: ₹{net_pnl:+,.2f}\n"
                      f"Balance (realized equity): ₹{self.capital:,.2f}")

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
        sym_leverage = _SYMBOL_LEVERAGE_OVERRIDE.get(sym.upper(), self.leverage)
        required_margin = (sig["entry_price"] * quantity) / max(sym_leverage, 1.0)
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
            # Pinned to the instrument this position was ACTUALLY opened on --
            # see the day-rollover comment in scan() for why re-looking-up
            # self.symbol_map at exit time instead would be wrong if a
            # contract rolled over while this position was open.
            "instrument_key": sig["instrument_key"],
        }
        log.warning("LIVE ENTRY FILLED: %s %s qty=%s @ %.2f (order_id=%s, SL=%.2f, TP=%.2f)",
                    sym, sig["direction"], sig["lots"], fill_price, order_id, real_sl, real_tp)
        # "Balance" (self.capital) is realized equity -- it only changes when
        # a position CLOSES, never on entry (see _exit -- self.capital +=
        # net_pnl), so it would look unchanged here even though the order
        # just consumed real margin. Fetches the REAL post-order available
        # funds fresh (the pre-order check earlier in this method is now
        # stale) so the actual margin impact is visible instead of implied
        # by a "Balance" figure that was never going to move.
        remaining_funds = self.broker.get_available_funds()
        funds_line = f"\nReal Funds Remaining: ₹{remaining_funds:,.2f}" if remaining_funds is not None else \
            "\nReal Funds Remaining: (could not fetch)"
        telegram.send(f"📥 <b>LIVE ENTRY FILLED</b> {sym} {sig['direction'].upper()} @ ₹{fill_price:.2f}  "
                      f"Qty: {sig['lots']}\nSL: ₹{real_sl:.2f}  TP: ₹{real_tp:.2f}{funds_line}\n"
                      f"Balance (realized equity): ₹{self.capital:,.2f}")

    # ---- exit management (mirrors DryRunner._maybe_exit/_close_position) ----
    def _maybe_exit(self, sym: str, now: datetime) -> None:
        pos = self.positions[sym]
        ikey = pos["instrument_key"]  # pinned at entry -- NOT a fresh self.symbol_map lookup, see _enter()'s comment
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
        ikey = pos["instrument_key"]  # pinned at entry -- see _maybe_exit's comment

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
                      f"@ ₹{exit_price:.2f}  [{reason}]\nNet PnL: ₹{net_pnl:+,.2f}\n"
                      f"Balance (realized equity): ₹{self.capital:,.2f}")

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
                if _is_equity(sym):
                    cost_info = compute_nse_equity_costs(pos["direction"], pos["entry_price"], approx_exit, pos["qty"])
                elif _is_currency(sym):
                    cost_info = compute_ncd_currency_costs(sym, pos["direction"], pos["entry_price"], approx_exit, pos["qty"])
                else:
                    cost_info = compute_mcx_commodity_costs(sym, pos["direction"], pos["entry_price"], approx_exit, pos["qty"])
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

            # Refresh instrument_key resolution once per trading-day rollover
            # -- MCX/NCD_FO contracts are monthly-expiry, and self.symbol_map
            # was otherwise only ever built once at __init__ (fixed 2026-09-18,
            # same bug live_dryrun.py's SYMBOL_MAP had before its own
            # rollover fix). This only affects NEW entries going forward --
            # any position already open keeps using its own pinned
            # instrument_key (see _enter()'s comment), so a rollover can
            # never retarget an exit order at the wrong contract.
            fresh_map = _build_symbol_map()
            changed = {k: v for k, v in fresh_map.items() if self.symbol_map.get(k) != v}
            if changed:
                log.warning("Instrument key(s) rolled over: %s", changed)
                telegram.send(f"🔄 <b>CONTRACT ROLLOVER (LIVE)</b> — {len(changed)} instrument key(s) updated: "
                              f"{', '.join(changed.keys())}")
            self.symbol_map = fresh_map

            if self.use_crude_regime_filter:
                self._refresh_crude_regime()

        daily_loss_pct = ((self.day_start_capital - self.capital) / self.day_start_capital * 100
                           if self.day_start_capital > 0 else 0.0)
        if not self.kill_switch_active and daily_loss_pct >= self.max_daily_loss_pct:
            self.kill_switch_active = True
            telegram.send(f"🛑 <b>DAILY LOSS LIMIT HIT (LIVE)</b> — {daily_loss_pct:.2f}% "
                          f"(limit {self.max_daily_loss_pct}%). New entries halted for the rest of today.")

        self._reconcile_positions(now)

        for sym in self.symbols:
            if _is_equity(sym):
                if sym in self.positions:
                    self._maybe_exit_equity(sym, now)
                    continue
                if self.kill_switch_active or self.symbol_kill_switch.get(sym, False):
                    continue
                open_equity_count = sum(1 for s in self.positions if _is_equity(s))
                if open_equity_count >= MAX_CONCURRENT_EQUITY_POSITIONS:
                    continue
                sig = self._entry_signal_equity(sym, now)
                if sig:
                    self._enter_equity(sig, now)
                continue

            if sym in self.positions:
                self._maybe_exit(sym, now)
                continue
            if self.kill_switch_active or self.symbol_kill_switch.get(sym, False):
                continue
            sig = self._entry_signal(sym, now)
            if sig:
                self._enter(sig, now)


def _acquire_process_lock(account_id: str):
    """Same pattern as live_dryrun.py's own _acquire_process_lock -- separate
    lock filename (LIVE_ prefix) so a paper-trading dryrun process and a
    real live_trading.py process for accounts that happen to share a name
    don't collide with each other, while two live_trading.py processes for
    the SAME account still correctly refuse to double-run (which, with real
    orders, could double-size or double-enter positions)."""
    import fcntl
    lock_dir = Path(__file__).parent / "data"
    lock_dir.mkdir(exist_ok=True)
    lock_path = lock_dir / f".LIVE_{account_id}.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"\033[91mERROR: Another live_trading process is already running for account "
              f"'{account_id}' (lock: {lock_path}). Refusing to start a second one.\033[0m")
        telegram.send(f"🔴 <b>LIVE TRADING STARTUP REFUSED</b> — another live_trading process is already "
                      f"running for account '{account_id}'. This attempt was refused to prevent double-trading.")
        sys.exit(1)
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh  # caller must keep this referenced so the fd (and lock) stays alive


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

    _lock_fh = _acquire_process_lock(args.account)  # held for process lifetime

    token = ensure_fresh_upstox_token() or UpstoxConfig.ACCESS_TOKEN
    if not token:
        print("No valid Upstox access token available.")
        sys.exit(1)

    broker = UpstoxBroker(access_token=token, dry_run=False)  # only actually live if all 3 gates passed above
    db = TradingDB(args.db)
    symbols = args.symbols or ["CRUDEOILM", "GOLDM", "USDINR", "EURINR", "GBPINR"]
    # Same opt-in equity extension as live_dryrun.py -- see that file's main()
    # comment. Kept as an explicit opt-in here too (not on by default even
    # with the flag unset) given this module places REAL orders.
    if os.environ.get("DRYRUN_INCLUDE_EQUITY", "false").lower() in ("1", "true", "yes"):
        symbols = list(symbols) + [s for s in NIFTY50_SYMBOLS if s not in symbols]

    trader = LiveTrader(broker, db, symbols, args.capital, args.risk_pct, args.leverage,
                         account_id=args.account, direction_filter=args.direction)

    print(f"\033[91m\033[1mLIVE TRADING ARMED AND RUNNING -- REAL ORDERS WILL BE PLACED. Symbols: {symbols}\033[0m")
    telegram.send(f"🔴🔴 <b>LIVE TRADING STARTED</b> — real orders, account={args.account}, symbols={symbols}")

    # Same SIGTERM-as-clean-shutdown handling as live_dryrun.py's main() --
    # see that file's comment for why the handler re-arms to SIG_IGN
    # (prevents a second signal from re-raising KeyboardInterrupt mid-cleanup,
    # which can otherwise land inside the shutdown Telegram call itself).
    import signal
    def _handle_sigterm(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    token_check_interval_sec = int(os.environ.get("TOKEN_CHECK_INTERVAL_MIN", "15")) * 60
    last_token_check = time.monotonic()

    # 24/7 outer loop, same shape as live_dryrun.py's main(): sleeps through
    # nights/weekends/holidays and rolls into the next trading day rather
    # than exiting, so systemd (or a human) just needs to keep one
    # long-running process alive -- IF this is ever actually wired up to run
    # unattended, which it currently is not (see module docstring).
    stopped_by_user = False
    while not stopped_by_user:
        now = datetime.now(IST)
        mopen = now.replace(hour=9, minute=0, second=0, microsecond=0)
        mclose = now.replace(hour=23, minute=30, second=0, microsecond=0)

        if now > mclose:
            from utils.market_holidays import get_trading_holidays
            trading_holidays = get_trading_holidays()
            next_day = now + timedelta(days=1)
            while next_day.weekday() >= 5 or next_day.date() in trading_holidays:
                next_day += timedelta(days=1)
            mopen = mopen.replace(year=next_day.year, month=next_day.month, day=next_day.day)
            mclose = mclose.replace(year=next_day.year, month=next_day.month, day=next_day.day)

        trader.today = mopen.strftime("%Y-%m-%d")

        now = datetime.now(IST)
        if now < mopen:
            wait = int((mopen - now).total_seconds())
            print(f"  Market opens {mopen.strftime('%Y-%m-%d %H:%M')} IST "
                  f"(in {wait // 3600}h {(wait % 3600) // 60}m) — sleeping...", flush=True)
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                print("\nStopped by user (during overnight wait).", flush=True)
                stopped_by_user = True
                continue

        scan_n = 0
        while datetime.now(IST) <= mclose:
            scan_n += 1
            now = datetime.now(IST)
            print(f"\n-- LIVE Scan #{scan_n} @ {now.strftime('%Y-%m-%d %H:%M:%S')} IST "
                  f"| Balance: ₹{trader.capital:,.2f} --", flush=True)

            if token_invalid_event.is_set() or (time.monotonic() - last_token_check) >= token_check_interval_sec:
                last_token_check = time.monotonic()
                token_invalid_event.clear()
                if UpstoxConfig.auto_login_configured():
                    try:
                        old_token = UpstoxConfig.ACCESS_TOKEN
                        new_token = ensure_fresh_upstox_token(on_token_refreshed=broker.set_access_token)
                        if new_token is None:
                            log.error("Token refresh failed — trading may be blind until fixed.")
                            telegram.send("🔴 <b>LIVE TRADING TOKEN REFRESH FAILED</b> — auto-login attempt failed. "
                                          "Run `python3 -m auth.upstox_auth` manually or check credentials.")
                        elif new_token != old_token:
                            log.info("Token refreshed via scheduled check.")
                            telegram.send("🔑 <b>LIVE TRADING TOKEN REFRESHED</b> (scheduled check) — continuing normally.")
                    except Exception as exc:
                        log.error("Token refresh raised: %s", exc, exc_info=True)
                        telegram.alert_error("live_trading token refresh", exc)
                elif not UpstoxConfig.ACCESS_TOKEN:
                    telegram.send("🔴 <b>LIVE TRADING TOKEN INVALID</b> — auto-login not configured. "
                                   "Run `python3 -m auth.upstox_auth` manually.")

            try:
                trader.scan()
            except KeyboardInterrupt:
                print("\nStopped by user.", flush=True)
                stopped_by_user = True
                break
            except Exception as exc:
                log.error("Scan error: %s", exc, exc_info=True)
                telegram.alert_error(f"live_trading scan #{scan_n}", exc)

            nxt = datetime.now(IST) + timedelta(seconds=args.interval)
            sleep_until = min(nxt, mclose + timedelta(seconds=1))
            secs = max(1, (sleep_until - datetime.now(IST)).total_seconds())
            print(f"  Next scan in {secs:.0f}s...", flush=True)
            time.sleep(secs)

        # EOD summary for the day just finished
        db.print_dashboard(args.account)
        todays_trades = db.get_trades_for_date(trader.today, args.account)
        total_pnl = sum(t.get("net_pnl", 0) for t in todays_trades)
        telegram.send(
            f"🏁 <b>LIVE TRADING SESSION COMPLETE</b> ({trader.today})\n"
            f"Trades today: {len(todays_trades)}  Total PnL: ₹{total_pnl:+,.2f}\n"
            f"Balance: ₹{trader.capital:,.2f}"
        )
        from utils.chart import generate_equity_curve
        chart_path = generate_equity_curve(
            db.get_snapshots(args.account), args.account,
            Path(__file__).parent / "logs" / f"equity_{args.account}.png",
        )
        if chart_path:
            telegram.send_photo(chart_path, caption=f"📈 Live Equity Curve — {args.account} ({trader.today})")

    telegram.send("🔴 <b>LIVE TRADING STOPPED</b> (manual/shutdown signal). Open positions, if any, are NOT "
                  "auto-closed -- check the real Upstox order book.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Same top-level safety net as live_dryrun.py -- see that file's
        # comment: a SIGTERM-raised KeyboardInterrupt can land anywhere,
        # including inside a Telegram call that only catches Exception.
        print("\nShutting down.", flush=True)
        try:
            telegram.send("🔴 <b>LIVE TRADING STOPPED</b> (shutdown signal)")
        except BaseException:
            pass
        sys.exit(0)
