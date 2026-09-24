"""
markets/commodity/costs.py — Official MCX Commodity Futures Statutory Cost & Lot Sizing Model

Handles:
  1. Exact Indian Commodity Transaction Tax (CTT = 0.01% on sell-side turnover).
  2. Upstox Flat/Capped Brokerage (min(₹20, 0.05%) per order leg).
  3. Exchange Transaction Charges (MCX ~0.0021%).
  4. Stamp Duty (0.002% on buy-side).
  5. SEBI turnover fee (₹10 per crore = 0.0001%).
  6. 18% GST on (Brokerage + Exchange Charges + SEBI).
  7. Exact contract multipliers and integer lot sizing per trade.
"""
from __future__ import annotations

import math

from core.slippage import adaptive_slippage_per_leg

# Statutory rates drift -- e.g. the 2026-04-01 Union Budget hiked index-futures
# STT from 0.02% to 0.05% (found 2026-09-18 only via a manual web search while
# building the index-futures cost model; nothing in this codebase would have
# flagged the old rate as stale on its own). tests/test_rate_freshness.py fails
# once this goes more than ~180 days old, forcing a periodic human re-check
# against a current public rate source -- bump this date only after actually
# re-verifying every rate below, not just to silence the test.
RATES_LAST_VERIFIED = "2026-09-18"

COMMODITY_SPECS = {
    "CRUDEOIL": {
        "name": "Crude Oil (100 bbl)",
        "lot_size": 100,
        "tick_size": 1.0,
        "ctt_pct": 0.01,
        "margin_approx": 35000.0,
    },
    "CRUDEOILM": {
        "name": "Crude Oil Mini (10 bbl)",
        "lot_size": 10,
        "tick_size": 1.0,
        "ctt_pct": 0.01,
        "margin_approx": 3500.0,
    },
    "NATURALGAS": {
        "name": "Natural Gas (1250 mmBtu)",
        "lot_size": 1250,
        "tick_size": 0.10,
        "ctt_pct": 0.01,
        "margin_approx": 30000.0,
    },
    "NATGASMINI": {
        "name": "Natural Gas Mini (250 mmBtu)",
        "lot_size": 250,
        "tick_size": 0.10,
        "ctt_pct": 0.01,
        "margin_approx": 6000.0,
    },
    "GOLD": {
        "name": "Gold (1kg)",
        "lot_size": 100,
        "tick_size": 1.0,
        "ctt_pct": 0.01,
        "margin_approx": 50000.0,
    },
    "GOLDM": {
        "name": "Gold Mini (100g)",
        "lot_size": 10,
        "tick_size": 1.0,
        "ctt_pct": 0.01,
        "margin_approx": 5000.0,
    },
    "GOLDTEN": {
        "name": "Gold Ten (10g)",
        "lot_size": 1,                     # 10 g lot, priced per 10 g: one price point = Rs1 per lot (GOLDM: 100 g lot -> 10)
        "tick_size": 1.0,
        "ctt_pct": 0.01,
        "margin_approx": 14000.0,
    },
    "SILVER": {
        "name": "Silver (30kg)",
        "lot_size": 30,
        "tick_size": 1.0,
        "ctt_pct": 0.01,
        "margin_approx": 45000.0,
    },
    "SILVERM": {
        "name": "Silver Mini (5kg)",
        "lot_size": 5,
        "tick_size": 1.0,
        "ctt_pct": 0.01,
        "margin_approx": 7500.0,
    },
    "SILVERMIC": {
        "name": "Silver Micro (1kg)",
        "lot_size": 1,
        "tick_size": 1.0,
        "ctt_pct": 0.01,
        "margin_approx": 1500.0,
    },
    "COPPER": {
        "name": "Copper (2500kg)",
        "lot_size": 2500,
        "tick_size": 0.05,
        "ctt_pct": 0.01,
        "margin_approx": 40000.0,
    },
    # Added 2026-09-18 (base metals survey). Real lot sizes verified via web
    # search, NOT trusted blindly from Upstox's own instrument-master
    # "lot_size" field -- that field means "1 lot" (a trading-unit count) for
    # these three mini contracts, not the kg multiplier the way it happened
    # to coincide for CRUDEOILM/NICKEL. Real: Aluminium/Lead/Zinc Mini = 1 MT
    # (1000 kg) per lot, quoted per kg; Nickel = 250 kg per lot (this one DID
    # match Upstox's lot_size field directly).
    "ALUMINI": {
        "name": "Aluminium Mini (1 MT)",
        "lot_size": 1000,
        "tick_size": 0.05,
        "ctt_pct": 0.01,
        "margin_approx": 15000.0,
    },
    "LEADMINI": {
        "name": "Lead Mini (1 MT)",
        "lot_size": 1000,
        "tick_size": 0.05,
        "ctt_pct": 0.01,
        "margin_approx": 15000.0,
    },
    "ZINCMINI": {
        "name": "Zinc Mini (1 MT)",
        "lot_size": 1000,
        "tick_size": 0.05,
        "ctt_pct": 0.01,
        "margin_approx": 20000.0,
    },
    "NICKEL": {
        "name": "Nickel (250 kg)",
        "lot_size": 250,
        "tick_size": 0.10,
        "ctt_pct": 0.01,
        "margin_approx": 30000.0,
    },
}

MCX_CTT_PCT_SELL_SIDE = 0.010       # 0.01% on sell-side turnover
MCX_EXCHANGE_TXN_PCT  = 0.00210      # MCX turnover fee
MCX_SEBI_PCT          = 0.00010      # ₹10 per crore
MCX_STAMP_DUTY_PCT    = 0.00200      # 0.002% on buy-side turnover
GST_RATE              = 0.18         # 18% GST
# Upstox's own brokerage calculator (ChargeApi.get_brokerage), checked 2026-09-24 for equity MIS, MCX and NCD: min(0.06% of turnover, Rs30) per order.
# The model here used min(0.05%, Rs20) and understated every round trip by ~Rs23.6 (+GST) -- see tests/test_costs_vs_upstox.py.
UPSTOX_BROKERAGE_CAP  = 30.0         # Rs30 flat cap per executed order
UPSTOX_BROKERAGE_PCT  = 0.06         # 0.06% of turnover, whichever is lower


def get_contract_multiplier(symbol: str) -> int:
    """Lot size / P&L multiplier for a commodity symbol, scaled by any lot-size revision Upstox has made since the table below was built
    (engine/margin_rates.lot_scale reads Upstox's instrument master; 1.0 when unchanged)."""
    from engine import margin_rates
    return max(1, round(_static_contract_multiplier(symbol) * margin_rates.lot_scale(symbol)))


def _static_contract_multiplier(symbol: str) -> int:
    """The table value; see get_contract_multiplier for the live scaling."""
    sym_clean = symbol.upper().split("|")[-1].split("2")[0]  # Strip exchange prefix or expiry
    if sym_clean in COMMODITY_SPECS:
        return COMMODITY_SPECS[sym_clean]["lot_size"]
    fallbacks = {
        "CRUDEOIL": 100,
        "CRUDEOILM": 10,
        "NATURALGAS": 1250,
        "NATGASMINI": 250,
        "GOLD": 100,
        "GOLDM": 10,
        "GOLDTEN": 1,
        "SILVER": 30,
        "SILVERM": 5,
        "SILVERMIC": 1,
        "COPPER": 2500,
        "ALUMINI": 1000,
        "LEADMINI": 1000,
        "ZINCMINI": 1000,
        "NICKEL": 250,
    }
    return fallbacks.get(sym_clean, 10)


def compute_commodity_brokerage(trade_val: float) -> float:
    """Upstox brokerage for one order leg with GST."""
    base = min(trade_val * (UPSTOX_BROKERAGE_PCT / 100), UPSTOX_BROKERAGE_CAP)
    return base * (1 + GST_RATE)


def compute_mcx_commodity_costs(
    symbol: str,
    direction: str,
    entry: float,
    exit_p: float,
    lots: int
) -> dict:
    """
    Computes exact itemized costs for an MCX commodity trade matching statutory rules.
    `lots`: number of contracts.
    """
    mult = get_contract_multiplier(symbol)
    qty = lots * mult
    d = 1 if direction.lower() == "long" else -1

    ev = qty * entry
    xv = qty * exit_p
    gross = qty * (exit_p - entry) * d

    brok = compute_commodity_brokerage(ev) + compute_commodity_brokerage(xv)
    ctt = (xv if d == 1 else ev) * (MCX_CTT_PCT_SELL_SIDE / 100)
    stamp = (ev if d == 1 else xv) * (MCX_STAMP_DUTY_PCT / 100)
    exch = (ev + xv) * (MCX_EXCHANGE_TXN_PCT / 100)
    sebi = (ev + xv) * (MCX_SEBI_PCT / 100)
    gst_charges = (exch + sebi) * GST_RATE

    # Commodity slippage: use empirical median half-spread from spread_samples.csv
    # when >= 20 real observations exist for this symbol; fall back to ½ tick otherwise.
    from engine import margin_rates
    tick = margin_rates.tick_size(symbol) or COMMODITY_SPECS.get(symbol, {}).get("tick_size", 1.0)      # Upstox's own tick when known
    fallback_half_tick = 0.5 * tick
    slip_per_leg = adaptive_slippage_per_leg(symbol, fallback_half_tick)
    slip = slip_per_leg * qty * 2  # entry leg + exit leg

    total_friction = brok + ctt + stamp + exch + sebi + gst_charges + slip
    net = gross - total_friction

    return {
        "gross": round(gross, 2),
        "brokerage": round(brok, 2),
        "ctt": round(ctt, 2),
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


def size_commodity_lots(
    capital: float,
    entry_price: float,
    stop_distance: float,
    risk_pct: float,
    symbol: str = "CRUDEOIL",
    leverage: float = 5.0,
    size_mode: str = "margin"
) -> int:
    """
    Sizes integer lots dynamically based on account risk % and leverage margin constraint.
    """
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

    # Default: Always enforce user-specified leverage margin
    contract_notional_per_lot = entry_price * mult
    effective_leverage = max(1.0, float(leverage))
    margin_required_per_lot = contract_notional_per_lot / effective_leverage
    lots_margin = math.floor(capital / margin_required_per_lot) if margin_required_per_lot > 0 else 1

    # Margin is a hard limit, not a preference: if ONE lot needs more margin than the account has, Upstox rejects the order, so the
    # answer is 0 lots (the callers skip the trade). This used to be max(1, ...), which let GOLDM (Rs140k/lot) and SILVER (Rs901k/lot)
    # "trade" on Rs100k and produced backtest profits Upstox would never have allowed.
    return min(lots_risk, lots_margin)
