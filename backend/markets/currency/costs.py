"""
markets/currency/costs.py — Official NSE Currency Derivatives (NCD_FO) Statutory Cost & Lot Sizing Model

Mirrors markets/commodity/costs.py's shape exactly, but with the genuinely
different NSE currency-derivatives fee schedule (verified 2026-09-18 against
public rate sources -- see conversation history):
  1. NO STT/CTT at all -- currency futures/options are exempt (a real cost
     advantage vs. MCX commodities' 0.01% CTT and NSE equity's 0.025% STT).
  2. Upstox Flat/Capped Brokerage (min(₹20, 0.05%) per order leg) -- same
     convention as the commodity/equity paths.
  3. Exchange Transaction Charge: NSE currency segment ~0.0009% of turnover.
  4. Stamp Duty: ₹10 per crore (0.0001%) on buy-side turnover -- reduced
     from ₹200/crore for currency & interest-rate derivatives specifically;
     do not reuse commodity's 0.002% or equity's 0.003% rate here, they are
     for different segments.
  5. SEBI turnover fee: ₹10 per crore (0.0001%), same as every other segment.
  6. 18% GST on (Brokerage + Exchange Charges + SEBI).
  7. Contract multipliers and integer lot sizing per trade.
"""
from __future__ import annotations

import math

from core.slippage import adaptive_slippage_per_leg

# See markets/commodity/costs.py's RATES_LAST_VERIFIED comment -- same
# freshness discipline, checked by tests/test_rate_freshness.py.
RATES_LAST_VERIFIED = "2026-09-18"

CURRENCY_SPECS = {
    "USDINR": {"name": "US Dollar-Rupee (1,000 USD)", "lot_size": 1000, "tick_size": 0.0025, "margin_approx": 2000.0},
    "EURINR": {"name": "Euro-Rupee (1,000 EUR)", "lot_size": 1000, "tick_size": 0.0025, "margin_approx": 2500.0},
    "GBPINR": {"name": "British Pound-Rupee (1,000 GBP)", "lot_size": 1000, "tick_size": 0.0025, "margin_approx": 3000.0},
    "JPYINR": {"name": "Japanese Yen-Rupee (100,000 JPY)", "lot_size": 100000, "tick_size": 0.0025, "margin_approx": 2000.0},
}

NCD_EXCHANGE_TXN_PCT = 0.00090   # NSE currency segment turnover fee
NCD_SEBI_PCT         = 0.00010   # ₹10 per crore
NCD_STAMP_DUTY_PCT   = 0.00010   # ₹10 per crore on buy-side turnover (reduced from ₹200/crore for currency & IRD)
GST_RATE             = 0.18      # 18% GST
# Upstox's own brokerage calculator (ChargeApi.get_brokerage), checked 2026-09-24 for equity MIS, MCX and NCD: min(0.06% of turnover, Rs30) per order.
# The model here used min(0.05%, Rs20) and understated every round trip by ~Rs23.6 (+GST) -- see tests/test_costs_vs_upstox.py.
UPSTOX_BROKERAGE_CAP = 30.0      # Rs30 flat cap per executed order
UPSTOX_BROKERAGE_PCT = 0.06      # 0.06% of turnover, whichever is lower


def get_contract_multiplier(symbol: str) -> int:
    """Lot size / multiplier (in units of the base currency) for a currency pair, scaled by any lot-size revision Upstox has made since the
    table was built (engine/margin_rates.lot_scale; 1.0 when unchanged)."""
    from engine import margin_rates
    sym_clean = symbol.upper().split("|")[-1].split("2")[0]  # strip exchange prefix or expiry, matches commodity_costs.py's convention
    base = CURRENCY_SPECS[sym_clean]["lot_size"] if sym_clean in CURRENCY_SPECS else 1000     # 3 of the 4 tracked pairs use 1000
    return max(1, round(base * margin_rates.lot_scale(symbol)))


def compute_currency_brokerage(trade_val: float) -> float:
    """Upstox brokerage for one order leg with GST -- identical convention to commodity/equity."""
    base = min(trade_val * (UPSTOX_BROKERAGE_PCT / 100), UPSTOX_BROKERAGE_CAP)
    return base * (1 + GST_RATE)


def compute_ncd_currency_costs(
    symbol: str,
    direction: str,
    entry: float,
    exit_p: float,
    lots: int,
) -> dict:
    """
    Computes exact itemized costs for an NSE currency-futures trade.
    `lots`: number of contracts. `entry`/`exit_p` are per-unit INR prices
    (e.g. ~95.94 for USDINR), matching how the instrument quotes.
    """
    mult = get_contract_multiplier(symbol)
    qty = lots * mult
    d = 1 if direction.lower() == "long" else -1

    ev = qty * entry
    xv = qty * exit_p
    gross = qty * (exit_p - entry) * d

    brok = compute_currency_brokerage(ev) + compute_currency_brokerage(xv)
    # No STT/CTT -- currency derivatives are exempt.
    stamp = (ev if d == 1 else xv) * (NCD_STAMP_DUTY_PCT / 100)
    exch = (ev + xv) * (NCD_EXCHANGE_TXN_PCT / 100)
    sebi = (ev + xv) * (NCD_SEBI_PCT / 100)
    gst_charges = (exch + sebi) * GST_RATE

    # Currency slippage: empirical median half-spread when available; fall back to ½ tick.
    from engine import margin_rates
    tick = margin_rates.tick_size(symbol) or CURRENCY_SPECS.get(symbol.upper(), {}).get("tick_size", 0.0025)      # Upstox's own tick when known
    slip_per_leg = adaptive_slippage_per_leg(symbol, 0.5 * tick)
    slip = slip_per_leg * qty * 2  # entry leg + exit leg

    total_friction = brok + stamp + exch + sebi + gst_charges + slip
    net = gross - total_friction

    return {
        "gross": round(gross, 2),
        "brokerage": round(brok, 2),
        "ctt": 0.0,   # kept for shape-parity with compute_mcx_commodity_costs's dict (always zero here)
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


def size_currency_lots(
    capital: float,
    entry_price: float,
    stop_distance: float,
    risk_pct: float,
    symbol: str = "USDINR",
    leverage: float = 5.0,
    size_mode: str = "margin",
) -> int:
    """Sizes integer lots dynamically based on account risk % and leverage margin constraint.
    Mirrors markets/commodity/costs.py's size_commodity_lots exactly."""
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
    lots_margin = math.floor(capital / margin_per_lot)            # 0 when one lot does not fit the account: see size_commodity_lots

    return min(lots_risk, lots_margin)
