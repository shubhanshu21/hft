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
`python3 -m engine.live_trading`. Nothing else in this codebase imports or invokes
it:
  - cli.py has no subcommand for it (dryrun/backtest are unchanged).
  - No systemd unit references it (only hft-dryrun.service exists, and it
    still runs live_dryrun.py, behavior unchanged -- see below).
Grep for "live_trading" outside this file to confirm neither of the above
changes before ever touching that.

live_dryrun.py WAS touched, once, on 2026-09-18: its entry-decision logic
used to be duplicated here almost verbatim (the exact "keep two files in
sync by hand" drift risk ENTRY_THRESHOLDS' own extraction eliminated one
layer up). Both files now call the same shared markets.commodity.scalping.entry_signal.
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
    python3 -m engine.live_trading --capital 100000 --risk-pct 5.0 --leverage 4.0
"""
from __future__ import annotations

from core.paths import BACKEND_ROOT, DB_DIR, LOG_DIR
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(BACKEND_ROOT))

import engine.safety_gate as safety_gate
from services.auth.upstox_auto_login import ensure_fresh_upstox_token
from services.broker.feed_streamer import UpstoxFeedStreamer
from services.broker.order_manager import SmartOrderManager
from services.broker.upstox_broker import UpstoxBroker, token_invalid_event
from engine.config import UpstoxConfig
from engine.database import TradingDB
from markets.commodity.costs import COMMODITY_SPECS
from markets.currency.costs import CURRENCY_SPECS
from markets.commodity.scalping.entry_signal import is_currency as _is_currency
from markets.equity.scalping.entry_signal import is_equity as _is_equity
from core import registry
from core.risk import RiskGates
from core.strategy import EntryContext, ExitContext
from markets.equity.universe import NIFTY50_SYMBOLS
from core.sector_correlation import SectorCorrelationGate
from services.broker.instruments import build_mcx_commodity_map, build_currency_map, get_instrument_key
import pandas as pd
from services.utils.logger import get_logger, setup_logger
from services.utils import telegram

IST = ZoneInfo("Asia/Kolkata")
log = get_logger("live_trading")


# Same per-symbol risk override as live_dryrun.py -- see that file's comment
# (SILVER re-added 2026-09-18 at half risk pending more live experience;
# CRUDEOILM cut to 3.0 on 2026-09-19 after the full real archive showed the
# deployed thresholds are a net loser overall at full risk -- mirrored here
# 2026-09-19 to keep this file in sync, though it stays fully unwired
# regardless). The CRUDEOILM regime filter (_refresh_crude_regime) IS wired
# into both __init__ and scan()'s day-rollover block (added 2026-09-21),
# same as live_dryrun.py -- the reduced risk_pct + regime filter together
# reproduce the backtested +17.20% / -37.96% DD result.
_SYMBOL_RISK_PCT_OVERRIDE = {"SILVER": 5.0, "CRUDEOILM": 3.0}

# Same per-symbol leverage override as live_dryrun.py -- see that file's
# comment. Added 2026-09-19: GBPINR's under-proven-sample sizing fix has to
# go through leverage, not risk_pct, because margin sizing (not risk sizing)
# binds every one of its real trades -- a risk_pct override is a confirmed
# no-op for it (see live_dryrun.py's _SYMBOL_LEVERAGE_OVERRIDE comment for
# the full story).
_SYMBOL_LEVERAGE_OVERRIDE = {"GBPINR": 3.5}


def _get_market(sym: str) -> str:
    """Classify a trading symbol into its asset class: 'equity', 'currency', or 'commodity'."""
    if _is_equity(sym):
        return "equity"
    elif _is_currency(sym):
        return "currency"
    return "commodity"


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


class LiveTrader(RiskGates):
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
        self.max_daily_loss_pct = float(os.environ.get("MAX_DAILY_LOSS_PCT", "5.0"))

        self.symbol_map = _build_symbol_map()
        self.today = datetime.now(IST).strftime("%Y-%m-%d")
        self.trading_day = self.today
        self.day_start_capital = self.capital
        self.kill_switch_active = False
        self.symbol_daily_pnl: dict[str, float] = {s: 0.0 for s in self.symbols}
        self.symbol_kill_switch: dict[str, bool] = {s: False for s in self.symbols}

        # Market-specific daily loss limits & cooldown timers (added 2026-09-22):
        self.max_market_daily_loss_pct = float(os.environ.get("MAX_MARKET_DAILY_LOSS_PCT", "3.0"))
        self.market_cooldown_minutes = int(os.environ.get("MARKET_COOLDOWN_MINUTES", "60"))
        self.market_daily_pnl: dict[str, float] = {"commodity": 0.0, "currency": 0.0, "equity": 0.0}
        self.market_cooldown_until: dict[str, datetime | None] = {"commodity": None, "currency": None, "equity": None}

        # CRUDEOILM regime gate -- mirrors live_dryrun.py's DryRunner exactly
        # (see that file's __init__ and _refresh_crude_regime for the full
        # finding/rationale). Added 2026-09-19, the same day as this file's
        # _SYMBOL_RISK_PCT_OVERRIDE CRUDEOILM entry -- without this, that
        # risk_pct cut alone does NOT reproduce the validated backtest result
        # (net +17.20%/-37.96% DD requires BOTH the reduced risk_pct AND this
        # filter together; risk_pct alone leaves the pre-fix, net-losing-
        # overall entry logic in place). This module stays fully unwired
        # regardless of this fix -- see module docstring.
        self.use_commodity_regime_filter = os.environ.get("USE_COMMODITY_REGIME_FILTER",
            os.environ.get("USE_CRUDE_REGIME_FILTER", "true")).lower() in ("1", "true", "yes")
        self.commodity_regime_ok: dict[str, bool | None] = {}
        if self.use_commodity_regime_filter:
            self._refresh_commodity_regimes()

        self.use_equity_regime_filter = os.environ.get("USE_EQUITY_REGIME_FILTER", "false").lower() in ("1", "true", "yes")
        self.equity_regime_ok: bool | None = True
        if self.use_equity_regime_filter:
            self._refresh_equity_regime()

        # Market Segment Trading Toggles (added 2026-09-22)
        self.segment_enabled: dict[str, bool] = {
            "commodity": os.environ.get("ENABLE_COMMODITY_TRADING", "true").lower() in ("1", "true", "yes"),
            "currency": os.environ.get("ENABLE_CURRENCY_TRADING", "true").lower() in ("1", "true", "yes"),
            "equity": os.environ.get("ENABLE_EQUITY_TRADING", os.environ.get("DRYRUN_INCLUDE_EQUITY", "true")).lower() in ("1", "true", "yes"),
        }

        # Sector Correlation Risk Gate & Smart Order Manager
        self.sector_gate = SectorCorrelationGate(
            max_per_sector=int(os.environ.get("MAX_POSITIONS_PER_SECTOR", "1"))
        )
        self.order_manager = SmartOrderManager(self.broker)

        self.max_portfolio_heat_pct = float(os.environ.get("MAX_PORTFOLIO_HEAT_PCT", "8.0"))
        self._init_margin_limits()          # shared margin caps -- core/risk.py (were missing on this runner)
        self.strategies = {m: registry.active(m) for m in registry.MARKETS}
        self._midday_summary_sent = False

        log.info("Rule-based LiveTrader signal engine initialized across %d symbols.", len(self.symbols))

        self.db.init_account(account_id=self.account_id, capital=self.capital,
                              leverage=self.leverage, risk_pct=self.risk_pct)
        self.peak_capital = self.db.get_peak_capital(account_id=self.account_id) or self.capital
        acct = self.db.get_account(self.account_id)
        if acct:
            self.capital = acct["current_capital"]

        self.positions: dict[str, dict] = {}
        for p in self.db.get_open_positions(self.account_id):
            strat_name = p.get("strategy") or "scalping"
            strat = registry.get(_get_market(p["symbol"]), strat_name)
            pos = {
                "position_id": p["position_id"], "symbol": p["symbol"], "strategy": strat_name,
                "direction": p["direction"], "qty": p["qty"],
                "entry_price": p["entry_price"], "current_stop": p["current_stop"],
                "best_price": p.get("best_price", p["entry_price"]),
                "entry_time": datetime.fromisoformat(p["entry_time"]),
                "stop_dist": abs(p["entry_price"] - p["current_stop"]) or p["entry_price"] * 0.005,
                "entry_order_id": p.get("entry_order_id", ""),
                # Pinned to the instrument the position was opened on; the current map is only a fallback.
                "instrument_key": p.get("instrument_key") or self.symbol_map.get(p["symbol"]),
            }
            pos.update(strat.restore(p))
            if p.get("state"):
                pos.update(json.loads(p["state"]))
            self.positions[p["symbol"]] = pos
            log.warning("Restored OPEN real position from DB on startup: %s %s qty=%s -- "
                        "verify this matches the actual Upstox position book before trusting it.",
                        p["symbol"], p["direction"], p["qty"])

    def _is_market_on_cooldown(self, market: str, now: datetime) -> bool:
        """Checks whether the given market ('commodity', 'currency', 'equity')
        is currently on a loss-triggered cooldown. Automatically clears expired cooldowns."""
        expiry = self.market_cooldown_until.get(market)
        if expiry is None:
            return False
        if now < expiry:
            return True
        self.market_cooldown_until[market] = None
        log.info("%s cooldown expired (LIVE). Resuming %s entry evaluation.", market.upper(), market)
        telegram.send(f"▶ <b>{market.upper()} COOLDOWN EXPIRED (LIVE)</b>\n"
                      f"Cooldown has ended. New {market} setups will now be evaluated.")
        return False

    def _portfolio_heat_pct(self) -> float:
        if self.capital <= 0:
            return 0.0
        total_risk_rupees = 0.0
        for s, p in self.positions.items():
            sdist = abs(p["entry_price"] - p["current_stop"])
            if _is_equity(s):
                units = p.get("qty", 1)
            elif _is_currency(s):
                units = p.get("lots", 1) * CURRENCY_SPECS.get(s.upper(), {}).get("lot_size", 1000)
            else:
                units = p.get("lots", 1) * COMMODITY_SPECS.get(s, {}).get("lot_size", 1)
            total_risk_rupees += sdist * units
        return (total_risk_rupees / self.capital) * 100.0

    def _drawdown_risk_scale(self) -> float:
        if self.peak_capital <= 0:
            return 1.0
        dd_pct = max(0.0, (self.peak_capital - self.capital) / self.peak_capital * 100.0)
        if dd_pct >= 15.0:
            return 0.50
        elif dd_pct >= 10.0:
            return 0.75
        elif dd_pct >= 5.0:
            return 0.90
        return 1.0

    def _get_segment_risk_and_leverage(self, sym: str) -> tuple[float, float]:
        market = _get_market(sym)
        if market == "commodity":
            r = float(os.environ.get("COMMODITY_RISK_PCT", self.risk_pct))
            l = float(os.environ.get("COMMODITY_LEVERAGE", self.leverage))
        elif market == "currency":
            r = float(os.environ.get("CURRENCY_RISK_PCT", self.risk_pct))
            l = float(os.environ.get("CURRENCY_LEVERAGE", self.leverage))
        else:
            r = float(os.environ.get("EQUITY_RISK_PCT", self.risk_pct))
            l = float(os.environ.get("EQUITY_LEVERAGE", self.leverage))

        # Per-symbol values: an env var <SYMBOL>_RISK_PCT / <SYMBOL>_LEVERAGE wins, else the coded override above, else the segment value.
        r = float(os.environ.get(f"{sym.upper()}_RISK_PCT", _SYMBOL_RISK_PCT_OVERRIDE.get(sym.upper(), r)))
        l = float(os.environ.get(f"{sym.upper()}_LEVERAGE", _SYMBOL_LEVERAGE_OVERRIDE.get(sym.upper(), l)))
        from engine import margin_rates
        l = margin_rates.cap_leverage(sym, l)          # never more than Upstox really gives for this symbol
        return r, l

    def _send_midday_summary(self, now: datetime) -> None:
        self._midday_summary_sent = True
        open_syms = list(self.positions.keys())
        pnl = self.capital - self.day_start_capital
        pnl_pct = (pnl / self.day_start_capital * 100) if self.day_start_capital > 0 else 0.0
        mkt_pnl_lines = "\n".join([f"  • {m.capitalize()}: ₹{val:,.2f}" for m, val in self.market_daily_pnl.items()])
        msg = (
            f"📈 <b>LIVE MIDDAY P&L SUMMARY</b> ({now.strftime('%H:%M')} IST)\n"
            f"Capital: ₹{self.capital:,.2f} (Day P&L: ₹{pnl:+,.2f} / {pnl_pct:+.2f}%)\n"
            f"Market P&Ls:\n{mkt_pnl_lines}\n"
            f"Open Positions ({len(open_syms)}): {', '.join(open_syms) or 'none'}"
        )
        log.info(msg)
        telegram.send(msg)

    def _refresh_commodity_regimes(self) -> None:
        from core.regime import regime_ok as _regime_ok
        yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
        for sym in ["CRUDEOILM", "GOLDM", "GOLDTEN", "SILVER", "SILVERMIC", "NATGASMINI"]:
            ikey = self.symbol_map.get(sym)
            if not ikey:
                continue
            try:
                candles = self.broker.get_historical_candles(ikey, unit="days", interval=1, to_date=yesterday)
                if not candles:
                    continue
                closes = [float(c["close"]) for c in sorted(candles, key=lambda c: c["timestamp"])]
                res = _regime_ok(closes, window=15, min_autocorr=0.0)
                self.commodity_regime_ok[sym.upper()] = res
                if res is False:
                    log.warning("%s regime gate: BLOCKED for today (autocorr < 0).", sym)
            except Exception as exc:
                log.warning("Could not fetch daily candles for %s regime gate: %s", sym, exc)

    def _refresh_equity_regime(self) -> None:
        from core.regime import regime_ok as _regime_ok
        yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            candles = self.broker.get_historical_candles("NSE_INDEX|Nifty 50", unit="days", interval=1, to_date=yesterday)
            if not candles:
                return
            closes = [float(c["close"]) for c in sorted(candles, key=lambda c: c["timestamp"])]
            self.equity_regime_ok = _regime_ok(closes, window=15, min_autocorr=0.0)
            if self.equity_regime_ok is False:
                log.warning("Equity regime gate (NIFTY50): BLOCKED for today (autocorr < 0).")
        except Exception as exc:
            log.warning("Could not fetch daily candles for NIFTY50 equity regime gate: %s", exc)

    # ---- entry signal (mirrors DryRunner.scan()'s per-symbol logic) --------
    # ---- strategy-driven entry / exit ------------------------------------------------
    # Strategies (core/strategy.py) only decide. Everything below is the real-order execution layer
    # shared by all of them: funds backstop, order placement with the strategy's product type,
    # fill confirmation, re-anchoring the levels to the real fill, and the DB / alert bookkeeping.
    def _strategy_of(self, pos: dict, sym: str | None = None):
        return registry.get(_get_market(sym or pos["symbol"]), pos.get("strategy", "scalping"))

    def _candles(self, sym: str, ikey: str, strat) -> list[dict]:
        unit, interval = strat.timeframe
        if strat.lookback_days > 0:
            today = datetime.now(IST).date()
            raw = self.broker.get_historical_candles(ikey, unit=unit, interval=interval, to_date=today.isoformat(),
                                                     from_date=(today - timedelta(days=strat.lookback_days)).isoformat())
            return sorted(raw, key=lambda c: c["timestamp"]) if raw else []
        return _fetch_candles(self.broker, sym, ikey, self.today)

    def _try_enter(self, strat, sym: str, now: datetime) -> bool:
        market = _get_market(sym)
        if strat.blocked({"equity_regime_ok": self.equity_regime_ok if self.use_equity_regime_filter else True}):
            return False
        if strat.max_positions is not None:
            open_n = sum(1 for p in self.positions.values()
                         if p.get("strategy", "scalping") == strat.name and _get_market(p["symbol"]) == market)
            if open_n >= strat.max_positions:
                return False
        if strat.sector_cap:
            can_enter_sec, reason_sec = self.sector_gate.can_enter(sym, list(self.positions.keys()))
            if not can_enter_sec:
                log.info("%s: live %s entry skipped -- %s", sym, market, reason_sec)
                return False
        if not strat.due(now):
            return False

        ikey = self.symbol_map.get(sym)
        base_risk_pct, sym_leverage = self._get_segment_risk_and_leverage(sym)
        override = os.environ.get(f"{market.upper()}_{strat.name.upper()}_RISK_PCT")
        if override:
            base_risk_pct = float(override)
        leverage = sym_leverage if strat.uses_leverage else 1.0
        regime_ok = self.commodity_regime_ok.get(sym.upper(), None) if self.use_commodity_regime_filter else True
        sizing_capital = self._sizing_capital(market)
        if sizing_capital <= 0:
            return False                      # no margin left in the pool -- nothing to size an entry against
        entry_candles = self._candles(sym, ikey, strat)
        signal = strat.entry(EntryContext(
            symbol=sym, candles=entry_candles, now=now, instrument_key=ikey, capital=sizing_capital,
            risk_pct=base_risk_pct * self._drawdown_risk_scale(), leverage=leverage,
            direction_filter=self.direction_filter, full_session=self.full_session,
            regime_ok=regime_ok, equity_regime_ok=self.equity_regime_ok if self.use_equity_regime_filter else True,
        ))
        if not signal or (signal.direction == "short" and not strat.allow_short):
            return False
        if entry_candles:
            signal.exit_state["entry_bar_ts"] = str(entry_candles[-1]["timestamp"])   # see core/exits._side
        rejection = self._entry_gate_rejection(sym, market, signal, leverage)
        if rejection:
            log.info("%s: live %s entry skipped -- %s.", sym, market, rejection)
            return False
        return self._enter(strat, signal, leverage, now)

    def _enter(self, strat, signal, leverage: float, now: datetime) -> bool:
        """Place the real entry order. Returns True only if a position is now open."""
        sym = signal.symbol
        # `units` = shares / contract units (notional and margin maths); `quantity` = what Upstox expects in the order (engine/order_units.py):
        # LOTS for commodity, units for the rest. Confusing the two once would have sent a 5-lot crude order as 50 lots.
        units = signal.qty * signal.lot_size
        from engine.order_units import broker_quantity
        quantity = broker_quantity(strat.market, signal.qty, signal.lot_size)
        if quantity < 1:
            msg = f"{sym}: computed order quantity {quantity} is not positive; refusing to place it"
            log.error("live entry refused -- %s", msg)
            telegram.send(f"🔴 <b>LIVE ENTRY REFUSED</b> — {msg}")
            return False
        transaction_type = "BUY" if signal.direction == "long" else "SELL"
        ts_tag = now.strftime("%Y%m%d_%H%M%S")
        tag = f"LIVE_E_{sym}"[:16]

        # Real-funds check -- deliberately independent of the strategy's own margin-capped sizing, which
        # sizes against self.capital (this process's LOCAL ledger, seeded from the DB at startup) rather
        # than the account's actual balance. Those can drift -- a manual withdrawal, a trade placed
        # outside this process, a fee the ledger doesn't model exactly -- so this is the final,
        # authoritative check against the real broker balance right before an order that risks real
        # money. Fails safe: if the funds API can't be reached, treat that as insufficient.
        required_margin = (signal.entry_price * units) / max(leverage, 1.0)
        available_funds = self.broker.get_available_funds()
        if available_funds is None:
            msg = f"🔴 <b>LIVE ENTRY SKIPPED</b> — {sym}: could not fetch real available funds; refusing to size an order against an unknown balance."
            log.error(msg)
            telegram.send(msg)
            return False
        if available_funds < required_margin:
            msg = (f"🔴 <b>LIVE ENTRY SKIPPED — INSUFFICIENT FUNDS</b> — {sym}: needs ~₹{required_margin:,.2f} margin, "
                   f"only ₹{available_funds:,.2f} available. New entries for {sym} will keep being skipped until funds recover.")
            log.error(msg)
            telegram.send(msg)
            return False

        place = self.broker.place_buy_order if transaction_type == "BUY" else self.broker.place_sell_order
        order_id = place(signal.instrument_key, quantity, product=strat.product, tag=tag)
        if not order_id:
            log.error("%s: entry order placement returned no order_id (dry_run broker, or immediate rejection).", sym)
            return False

        outcome, fill_price = _wait_for_fill(self.broker, order_id, signal.entry_price)
        if outcome == "rejected":
            log.warning("%s: entry order %s cleanly rejected/cancelled by the broker -- nothing filled, no position opened.", sym, order_id)
            return False
        if outcome == "ambiguous":
            # NOT the same as a clean non-fill -- see _wait_for_fill's docstring. A partial fill means the
            # broker has real, unmanaged exposure this process doesn't know about: needs a human now.
            msg = (f"🔴🔴 <b>LIVE ENTRY ORDER STATUS UNKNOWN</b> — {sym} {order_id} never reached a confirmed "
                   f"'complete'/'rejected' state within {ORDER_FILL_TIMEOUT_SEC}s. It may be PARTIALLY FILLED "
                   f"at the broker with no position tracked here. Check the real Upstox order book IMMEDIATELY.")
            log.error(msg)
            telegram.send(msg)
            return False

        # The real fill rarely equals the price the signal assumed (the prior closed candle's close, from
        # before the order was placed). Every price level MUST be re-anchored to the real fill, not left
        # pointing at the stale assumed price -- otherwise a bad-slippage fill silently changes the
        # position's real risk. Each level shifts by exactly the offset the fill itself shifted, which
        # preserves the intended stop distance and R-multiples regardless of direction.
        assumed = signal.entry_price
        delta = fill_price - assumed
        slippage_pct = abs(delta) / assumed * 100 if assumed else 0.0
        dec = strat.price_decimals
        real_sl = round(signal.stop_loss + delta, dec)
        real_target = round(signal.target_price + delta, dec)
        real_be = round(signal.breakeven_price + delta, dec)
        exit_state = dict(signal.exit_state)
        for key in signal.price_levels:
            exit_state[key] = round(exit_state[key] + delta, dec)

        _MAX_ENTRY_SLIPPAGE_PCT = 0.5
        if slippage_pct > _MAX_ENTRY_SLIPPAGE_PCT:
            log.warning("%s: entry filled %.2f%% away from the assumed price (%.2f -> %.2f) -- "
                        "beyond the %.1f%% sanity bound.", sym, slippage_pct, assumed, fill_price, _MAX_ENTRY_SLIPPAGE_PCT)
            telegram.send(f"⚠️ <b>LIVE ENTRY LARGE SLIPPAGE</b> — {sym}: assumed ₹{assumed:.2f}, "
                          f"filled ₹{fill_price:.2f} ({slippage_pct:.2f}% away). Levels re-anchored to the real fill.")

        pos_id = f"POS_LIVE_{strat.id_prefix}_{ts_tag}_{sym}"
        margin_used = round(fill_price * units / max(leverage, 1.0), 2)
        self.db.place_order(
            order_id=order_id, symbol=sym, direction=transaction_type, intent="ENTRY",
            order_type="MARKET", qty=signal.qty, requested_price=assumed,
            fill_price=fill_price, status="FILLED", tag="LIVE_ENTRY", account_id=self.account_id,
        )
        state = {**exit_state, "margin_used": margin_used, "leverage": leverage, "lot_size": signal.lot_size}
        self.db.open_position(
            position_id=pos_id, symbol=sym, direction=signal.direction, qty=signal.qty,
            entry_price=fill_price, current_stop=real_sl, target_price=real_target, breakeven_price=real_be,
            account_id=self.account_id, instrument_key=signal.instrument_key, entry_order_id=order_id,
            strategy=strat.name, state=json.dumps(state),
        )
        self.positions[sym] = {
            "position_id": pos_id, "symbol": sym, "strategy": strat.name, "direction": signal.direction,
            "qty": signal.qty, "entry_price": fill_price, "current_stop": real_sl, "best_price": fill_price,
            "entry_time": now, "stop_dist": signal.stop_dist, "entry_order_id": order_id,
            # Pinned to the instrument this position was ACTUALLY opened on -- see the day-rollover comment
            # in scan(): re-looking-up self.symbol_map at exit time would be wrong if a contract rolled.
            "instrument_key": signal.instrument_key, "lot_size": signal.lot_size, "margin_used": margin_used,
            "leverage": leverage, **exit_state,
        }
        log.warning("LIVE ENTRY FILLED: %s %s %s qty=%s @ %.2f (order_id=%s, SL=%.2f)",
                    strat.name, sym, signal.direction, signal.qty, fill_price, order_id, real_sl)
        # "Balance" (self.capital) is realized equity -- it only changes when a position CLOSES, so it
        # would look unchanged here even though the order just consumed real margin. Fetch the REAL
        # post-order available funds so the actual margin impact is visible.
        remaining_funds = self.broker.get_available_funds()
        funds_line = f"\nReal Funds Remaining: ₹{remaining_funds:,.2f}" if remaining_funds is not None else \
            "\nReal Funds Remaining: (could not fetch)"
        levels = f"SL: ₹{real_sl:.2f}" + (f"  TP: ₹{exit_state['tp']:.2f}" if "tp" in exit_state else " (managed exit, no fixed TP)")
        telegram.send(f"📥 <b>LIVE ENTRY FILLED</b> [{strat.name}] {sym} {signal.direction.upper()} @ ₹{fill_price:.2f}  "
                      f"Qty: {signal.qty}\n{levels}{funds_line}\nBalance (realized equity): ₹{self.capital:,.2f}")
        return True

    def _maybe_exit(self, sym: str, now: datetime) -> None:
        pos = self.positions[sym]
        strat = self._strategy_of(pos, sym)
        ikey = pos["instrument_key"]  # pinned at entry -- NOT a fresh self.symbol_map lookup, see _enter()'s comment
        candles = self._candles(sym, ikey, strat)
        if not candles:
            return
        armed_key = "armed_be" if "armed_be" in pos else "armed_trail"
        before = (pos["current_stop"], pos.get(armed_key))
        decision = strat.manage(pos, ExitContext(symbol=sym, candles=candles, now=now))
        if decision is not None:
            self._exit(sym, pos, decision, now)
            return
        if (pos["current_stop"], pos.get(armed_key)) != before:
            self.db.update_position_stop(position_id=pos["position_id"], current_stop=pos["current_stop"],
                                          best_price=pos["best_price"], armed_be=bool(pos.get(armed_key, False)))

    def _exit(self, sym: str, pos: dict, decision, now: datetime) -> None:
        reason, expected_price = decision.reason, decision.price
        strat = self._strategy_of(pos, sym)
        from engine.order_units import broker_quantity
        quantity = broker_quantity(strat.market, pos["qty"], pos.get("lot_size") or strat.lot_size(sym))
        # Exit is always the opposite side of entry.
        exit_side_is_buy = pos["direction"] == "short"
        tag = f"LIVE_X_{sym}"[:16]
        ikey = pos["instrument_key"]  # pinned at entry -- see _maybe_exit's comment

        place = self.broker.place_buy_order if exit_side_is_buy else self.broker.place_sell_order
        order_id = place(ikey, quantity, product=strat.product, tag=tag)
        if not order_id:
            msg = f"🔴 <b>LIVE EXIT ORDER FAILED TO PLACE</b> — {sym} ({reason}). Position LEFT OPEN. Manual intervention required."
            log.error(msg)
            telegram.send(msg)
            return

        outcome, exit_price = _wait_for_fill(self.broker, order_id, expected_price)
        if outcome == "rejected":
            msg = f"🔴 <b>LIVE EXIT ORDER REJECTED</b> — {sym} {order_id} ({reason}). Position still fully OPEN (nothing filled) -- will retry on the next scan."
            log.error(msg)
            telegram.send(msg)
            return
        if outcome == "ambiguous":
            # Worse than the entry case: this position is still tracked as fully open here, but a partial
            # fill at the broker means the REAL remaining size could be smaller (or zero) -- a mismatch
            # that won't self-correct the way a clean rejection does. Needs a human to reconcile.
            msg = (f"🔴🔴 <b>LIVE EXIT ORDER STATUS UNKNOWN</b> — {sym} {order_id} ({reason}) never reached a "
                   f"confirmed 'complete'/'rejected' state within {ORDER_FILL_TIMEOUT_SEC}s. It may be PARTIALLY "
                   f"FILLED -- this process still thinks the full {pos['qty']} is open, which may now be "
                   f"WRONG. Check the real Upstox order/position book IMMEDIATELY and reconcile manually.")
            log.error(msg)
            telegram.send(msg)
            return

        cost_info = strat.costs(sym, pos["direction"], pos["entry_price"], exit_price, pos["qty"])
        net_pnl = cost_info["net"]
        self.capital += net_pnl
        self.peak_capital = max(self.peak_capital, self.capital)

        # Market-specific daily loss limit & cooldown timer
        market = _get_market(sym)
        self.market_daily_pnl[market] = self.market_daily_pnl.get(market, 0.0) + net_pnl
        if self.day_start_capital > 0:
            mkt_loss_pct = -self.market_daily_pnl[market] / self.day_start_capital * 100
            if mkt_loss_pct >= self.max_market_daily_loss_pct:
                if self.market_cooldown_until.get(market) is None or self.market_cooldown_until[market] < now:
                    overshoot = mkt_loss_pct / self.max_market_daily_loss_pct
                    cooldown_mins = (int(self.market_cooldown_minutes * 2) if overshoot >= 2.0
                                     else int(self.market_cooldown_minutes * 1.5) if overshoot >= 1.5
                                     else self.market_cooldown_minutes)
                    cooldown_expiry = now + timedelta(minutes=cooldown_mins)
                    self.market_cooldown_until[market] = cooldown_expiry
                    expiry_time_str = cooldown_expiry.strftime("%H:%M:%S")
                    telegram.send(
                        f"⏸ <b>{market.upper()} COOLDOWN ACTIVATED (LIVE)</b> — {mkt_loss_pct:.2f}% loss "
                        f"(limit {self.max_market_daily_loss_pct}%)\n"
                        f"New {market} entries paused for {cooldown_mins}m until <b>{expiry_time_str} IST</b>.\n"
                        f"Other markets continue normally."
                    )

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
            costs_dict=cost_info, net_pnl=net_pnl, capital_after=self.capital,
            adx=pos.get("adx"), account_id=self.account_id, strategy=strat.name,
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
        from services.utils.position_reconciliation import find_discrepancies
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
                cost_info = self._strategy_of(pos, sym).costs(sym, pos["direction"], pos["entry_price"], approx_exit, pos["qty"])
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
                    capital_after=self.capital, adx=pos.get("adx"), account_id=self.account_id,
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
            self.market_daily_pnl = {"commodity": 0.0, "currency": 0.0, "equity": 0.0}
            self.market_cooldown_until = {"commodity": None, "currency": None, "equity": None}

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

            # Pre-rollover contract expiry alert (MCX contracts expire ~20th of month)
            day_of_month = datetime.now(IST).day
            if 18 <= day_of_month <= 20:
                mcx_syms = [s for s in self.symbols if not _is_currency(s) and not _is_equity(s)]
                if mcx_syms:
                    telegram.send(
                        f"⚠️ <b>MCX CONTRACT EXPIRY WARNING (LIVE)</b> — Today is day {day_of_month} of month.\n"
                        f"MCX contracts for {', '.join(mcx_syms)} expire around 20th. Verify positions & instrument keys."
                    )

            if self.use_commodity_regime_filter:
                self._refresh_commodity_regimes()
            if self.use_equity_regime_filter:
                self._refresh_equity_regime()
            self._midday_summary_sent = False

        if now.hour >= 12 and now.minute >= 30 and not getattr(self, "_midday_summary_sent", False):
            self._send_midday_summary(now)

        daily_loss_pct = ((self.day_start_capital - self.capital) / self.day_start_capital * 100
                           if self.day_start_capital > 0 else 0.0)
        if not self.kill_switch_active and daily_loss_pct >= self.max_daily_loss_pct:
            self.kill_switch_active = True
            telegram.send(f"🛑 <b>DAILY LOSS LIMIT HIT (LIVE)</b> — {daily_loss_pct:.2f}% "
                          f"(limit {self.max_daily_loss_pct}%). New entries halted for the rest of today.")

        self._reconcile_positions(now)

        for sym in self.symbols:
            market = _get_market(sym)
            if sym in self.positions:
                self._maybe_exit(sym, now)       # open positions are always managed, even if the market is paused
                continue
            if not self.segment_enabled.get(market, True) or self._is_market_on_cooldown(market, now):
                continue
            if self.kill_switch_active or self.symbol_kill_switch.get(sym, False):
                continue
            for strat in self.strategies[market]:
                if self._try_enter(strat, sym, now):
                    break                        # one position per symbol across all strategies


def _acquire_process_lock(account_id: str):
    """Same pattern as live_dryrun.py's own _acquire_process_lock -- separate
    lock filename (LIVE_ prefix) so a paper-trading dryrun process and a
    real live_trading.py process for accounts that happen to share a name
    don't collide with each other, while two live_trading.py processes for
    the SAME account still correctly refuse to double-run (which, with real
    orders, could double-size or double-enter positions)."""
    import fcntl
    lock_dir = DB_DIR
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
    setup_logger("", log_file=str(LOG_DIR / "live_trading.log"))

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

    _lock_fh = _acquire_process_lock(args.account)  # noqa: F841 – held for process lifetime; fd must stay open to keep fcntl lock alive

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
            from services.utils.market_holidays import get_trading_holidays
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
        from services.utils.chart import generate_equity_curve
        chart_path = generate_equity_curve(
            db.get_snapshots(args.account), args.account,
            LOG_DIR / f"equity_{args.account}.png",
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
