"""
strategy/index_futures_costs.py — Official NSE Index Futures (NIFTY/BANKNIFTY) Statutory Cost & Lot Sizing Model

Mirrors strategy/currency_costs.py's shape exactly, but with the genuinely
different (and, as of 2026-04-01, much higher) index-futures fee schedule
(verified 2026-09-18 against public rate sources -- see conversation history):
  1. STT on futures: 0.05% on SELL-side turnover only -- hiked from 0.02%
     in Union Budget 2026, effective 2026-04-01. 5x MCX's 0.01% CTT and a
     real cost the currency path (STT-exempt) doesn't carry at all.
  2. Stamp Duty: 0.002% on buy-side turnover (same rate as MCX commodities;
     do not reuse currency's much-lower 0.0001% rate here, that's a
     currency/interest-rate-derivative-specific concession).
  3. Exchange Transaction Charge: NSE F&O ~0.00183% of turnover, both sides.
  4. SEBI turnover fee: ₹10 per crore (0.0001%), same as every other segment.
  5. 18% GST on (Brokerage + Exchange Charges + SEBI).
  6. Lot sizes (NIFTY=65, BANKNIFTY=30) and tick sizes (0.1 / 0.2) pulled
     directly from Upstox's live instrument master 2026-09-18, not
     hand-typed -- NSE revises these periodically (most recently Jan 2026)
     and a stale hardcoded value silently mis-sizes every trade.
"""
from __future__ import annotations

import math

# See strategy/commodity_costs.py's RATES_LAST_VERIFIED comment -- same
# freshness discipline, checked by tests/test_rate_freshness.py. Also covers
# lot_size/tick_size above, which NSE revises periodically (most recently
# Jan 2026) -- these were pulled from Upstox's live instrument master, not
# hand-typed, but that master should be re-checked on the same cadence.
RATES_LAST_VERIFIED = "2026-09-18"

INDEX_FUTURES_SPECS = {
    "NIFTY": {"name": "NIFTY 50 Futures", "lot_size": 65, "tick_size": 0.1},
    "BANKNIFTY": {"name": "NIFTY Bank Futures", "lot_size": 30, "tick_size": 0.2},
}

FUT_STT_PCT_SELL_SIDE = 0.05     # hiked from 0.02% effective 2026-04-01 (Union Budget 2026)
FUT_STAMP_DUTY_PCT    = 0.002    # on buy-side turnover
FUT_EXCHANGE_TXN_PCT  = 0.00183  # NSE F&O, both sides
FUT_SEBI_PCT          = 0.00010  # ₹10 per crore
GST_RATE              = 0.18
UPSTOX_BROKERAGE_CAP  = 20.0
UPSTOX_BROKERAGE_PCT  = 0.05


def get_contract_multiplier(symbol: str) -> int:
    sym_clean = symbol.upper().split("|")[-1].split("2")[0]
    if sym_clean in INDEX_FUTURES_SPECS:
        return INDEX_FUTURES_SPECS[sym_clean]["lot_size"]
    return 65  # NIFTY's lot size as a sensible default


def compute_index_futures_brokerage(trade_val: float) -> float:
    base = min(trade_val * (UPSTOX_BROKERAGE_PCT / 100), UPSTOX_BROKERAGE_CAP)
    return base * (1 + GST_RATE)


def compute_index_futures_costs(
    symbol: str,
    direction: str,
    entry: float,
    exit_p: float,
    lots: int,
) -> dict:
    """Computes exact itemized costs for an NSE index-futures trade.
    `lots`: number of contracts. `entry`/`exit_p` are per-unit index points."""
    mult = get_contract_multiplier(symbol)
    qty = lots * mult
    d = 1 if direction.lower() == "long" else -1

    ev = qty * entry
    xv = qty * exit_p
    gross = qty * (exit_p - entry) * d

    brok = compute_index_futures_brokerage(ev) + compute_index_futures_brokerage(xv)
    stt = (xv if d == 1 else ev) * (FUT_STT_PCT_SELL_SIDE / 100)
    stamp = (ev if d == 1 else xv) * (FUT_STAMP_DUTY_PCT / 100)
    exch = (ev + xv) * (FUT_EXCHANGE_TXN_PCT / 100)
    sebi = (ev + xv) * (FUT_SEBI_PCT / 100)
    gst_charges = (exch + sebi) * GST_RATE

    tick = INDEX_FUTURES_SPECS.get(symbol.upper(), {}).get("tick_size", 0.1)
    slip = (0.5 * tick * qty) * 2

    total_friction = brok + stt + stamp + exch + sebi + gst_charges + slip
    net = gross - total_friction

    return {
        "gross": round(gross, 2),
        "brokerage": round(brok, 2),
        "stt": round(stt, 2),
        "stamp_duty": round(stamp, 2),
        "exchange_txn": round(exch, 2),
        "sebi": round(sebi, 2),
        "gst": round(gst_charges, 2),
        "slippage": round(slip, 2),
        "total": round(total_friction, 2),
        "net": round(net, 2),
        "lots": lots,
        "qty": qty,
    }


def size_index_futures_lots(
    capital: float,
    entry_price: float,
    stop_distance: float,
    risk_pct: float,
    symbol: str = "NIFTY",
    leverage: float = 5.0,
    size_mode: str = "margin",
) -> int:
    """Mirrors strategy/currency_costs.py's size_currency_lots exactly."""
    if capital <= 0 or entry_price <= 0 or stop_distance <= 0:
        return 0

    mult = get_contract_multiplier(symbol)
    risk_rupees = capital * (risk_pct / 100.0)
    loss_per_lot = stop_distance * mult
    if loss_per_lot <= 0:
        return 0

    lots_risk = max(1, math.floor(risk_rupees / loss_per_lot))
    if size_mode == "risk":
        return lots_risk

    margin_per_lot = (entry_price * mult) / max(leverage, 1.0)
    if margin_per_lot <= 0:
        return lots_risk
    lots_margin = max(1, math.floor(capital / margin_per_lot))

    return max(1, min(lots_risk, lots_margin))
