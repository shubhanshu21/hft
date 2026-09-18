"""
strategy/crypto_costs.py — Binance USDT-M Perpetual Futures Fee & Sizing Model

Handles:
  1. Binance USDT-M Futures taker fee (0.05% per side, standard/VIP0 tier --
     no BNB discount or maker-rebate assumed, i.e. the conservative default).
  2. Funding rate cost/rebate for the duration a position is held (funding
     settles every 8 hours on Binance Futures) -- uses real historical
     funding-rate events from archive_crypto/{symbol}_funding.csv when
     available, otherwise falls back to a flat average rate.
  3. Fractional contract sizing (Binance perpetuals trade in fractional
     BTC/ETH, not integer lots like MCX) with the same dual risk/margin cap
     used by strategy/commodity_costs.py's size_commodity_lots.

No STT/CTT/stamp-duty/SEBI/GST -- those are Indian-exchange-specific and
have no Binance equivalent.
"""
from __future__ import annotations

import math

BINANCE_SPECS = {
    "BTCUSDT": {"name": "Bitcoin USDT-M Perpetual", "qty_step": 0.001, "tick_size": 0.10},
    "ETHUSDT": {"name": "Ethereum USDT-M Perpetual", "qty_step": 0.01, "tick_size": 0.01},
}

BINANCE_TAKER_FEE_PCT = 0.05        # 0.05% per side, standard (VIP0) tier, no BNB discount
BINANCE_MAKER_FEE_PCT = 0.02        # 0.02% per side -- unused by the market-order-only backtest today, kept for completeness
DEFAULT_FUNDING_RATE_PCT = 0.01     # 0.01% per 8h funding event -- fallback when no real funding history is available
FUNDING_INTERVAL_HOURS = 8


def get_qty_step(symbol: str) -> float:
    return BINANCE_SPECS.get(symbol.upper(), {}).get("qty_step", 0.001)


def _funding_cost_from_history(symbol_notional: float, direction: str, entry_time, exit_time, funding_df) -> float:
    """Sums real funding events between entry and exit. Longs pay positive funding, receive negative funding (and vice-versa for shorts)."""
    sign = 1 if direction.lower() == "long" else -1
    mask = (funding_df["timestamp"] > entry_time) & (funding_df["timestamp"] <= exit_time)
    events = funding_df.loc[mask, "funding_rate"]
    if events.empty:
        return 0.0
    return float(events.sum()) * symbol_notional * sign


def _funding_cost_fallback(symbol_notional: float, direction: str, entry_time, exit_time) -> float:
    sign = 1 if direction.lower() == "long" else -1
    if entry_time is None or exit_time is None:
        return 0.0
    hours_held = max(0.0, (exit_time - entry_time).total_seconds() / 3600.0)
    periods = hours_held / FUNDING_INTERVAL_HOURS
    return symbol_notional * (DEFAULT_FUNDING_RATE_PCT / 100) * periods * sign


def compute_binance_futures_costs(
    symbol: str,
    direction: str,
    entry: float,
    exit_p: float,
    qty: float,
    entry_time=None,
    exit_time=None,
    funding_df=None,
    entry_fee_mode: str = "taker",
    exit_fee_mode: str = "taker",
) -> dict:
    """
    Computes itemized costs for one Binance USDT-M perpetual futures trade.
    `qty` is fractional contract size (e.g. BTC units). `entry_fee_mode`/
    `exit_fee_mode`: "taker" (market order, 0.05%) or "maker" (resting limit
    order that gets filled, 0.02% -- 60% cheaper, but not guaranteed to fill
    at a given price/time the way a market order is). Both default to
    "taker" -- the conservative, always-fills assumption; a strategy that
    actually rests limit orders at entry can pass entry_fee_mode="maker" to
    see the fee-drag impact of that execution style instead.
    """
    d = 1 if direction.lower() == "long" else -1

    entry_notional = qty * entry
    exit_notional = qty * exit_p
    gross = qty * (exit_p - entry) * d

    entry_fee_pct = BINANCE_MAKER_FEE_PCT if entry_fee_mode == "maker" else BINANCE_TAKER_FEE_PCT
    exit_fee_pct = BINANCE_MAKER_FEE_PCT if exit_fee_mode == "maker" else BINANCE_TAKER_FEE_PCT
    taker_fee_entry = entry_notional * (entry_fee_pct / 100)
    taker_fee_exit = exit_notional * (exit_fee_pct / 100)

    if funding_df is not None and not funding_df.empty and entry_time is not None and exit_time is not None:
        funding_cost = _funding_cost_from_history(entry_notional, direction, entry_time, exit_time, funding_df)
    else:
        funding_cost = _funding_cost_fallback(entry_notional, direction, entry_time, exit_time)

    total_friction = taker_fee_entry + taker_fee_exit + funding_cost
    net = gross - total_friction

    return {
        "gross": round(gross, 4),
        "taker_fee_entry": round(taker_fee_entry, 4),
        "taker_fee_exit": round(taker_fee_exit, 4),
        "funding_cost": round(funding_cost, 4),
        "total": round(total_friction, 4),
        "net": round(net, 4),
        "qty": qty,
    }


def size_crypto_position(
    capital: float,
    entry_price: float,
    stop_distance: float,
    risk_pct: float,
    symbol: str = "BTCUSDT",
    leverage: float = 5.0,
    size_mode: str = "margin",
) -> float:
    """Sizes a fractional contract quantity based on account risk % and leverage margin constraint (Binance perpetuals have no integer lot requirement). `size_mode`: "risk" = pure risk-budget sizing; anything else (default "margin") = capped by leverage margin too."""
    if capital <= 0 or entry_price <= 0 or stop_distance <= 0:
        return 0.0

    step = get_qty_step(symbol)
    risk_amount = capital * (risk_pct / 100.0)
    qty_risk = risk_amount / stop_distance

    if size_mode == "risk":
        qty = qty_risk
    else:
        effective_leverage = max(1.0, float(leverage))
        margin_budget = capital * effective_leverage
        qty_margin = margin_budget / entry_price
        qty = min(qty_risk, qty_margin)

    qty = math.floor(qty / step) * step
    return max(0.0, round(qty, 8))
