"""
strategy/costs.py — itemized NSE intraday equity transaction costs, for
realistic backtest P&L rather than a single flat guess.

Two different kinds of cost live here, and they must NOT be collapsed
into one flat percentage:

  1. Value-INDEPENDENT %-of-turnover charges (STT, exchange transaction
     charges, SEBI turnover fee, stamp duty, and GST on the applicable
     ones) — these genuinely scale with trade value with no cap, so a
     flat round-trip % (TOTAL_ROUND_TRIP_COST_PCT) is correct for them.
  2. Brokerage — Upstox (this project's actual broker) charges
     "0.1% of turnover OR ₹20 per executed order, WHICHEVER IS LOWER".
     For any trade above ₹20,000 notional (0.1% * ₹20,000 = ₹20), the
     ₹20 flat cap binds — meaning brokerage does NOT scale with trade
     size the way a percentage does. An earlier version of this file
     modeled brokerage as a flat 0.03% (a different broker's rate, with
     no cap applied at all) — for a ₹200,000 trade that overstates real
     brokerage by 3x (₹60 modeled vs. the real ₹20 capped charge).
     Because this depends on absolute trade value (quantity × price),
     it can only be computed once a position is actually sized — see
     brokerage_rupees() below, applied in backtest/portfolio.py, not
     folded into TOTAL_ROUND_TRIP_COST_PCT.

STT applies only to the SELL leg (whichever side that is — same total
either way for a long or a short intraday trade, since both close out
with one buy and one sell). GST applies to brokerage + exchange
transaction charges only (not STT/stamp duty/SEBI charges, per actual
contract-note treatment) — brokerage's GST is folded into
brokerage_rupees() itself, not into the %-based total below.

Slippage is separate from these regulatory/broker charges — it's an
assumption about execution quality (market impact + bid-ask spread on a
liquid large-cap NSE stock), not a fixed fee.
"""
from __future__ import annotations

STT_PCT_SELL_SIDE = 0.025  # Securities Transaction Tax, sell leg only, intraday equity
EXCHANGE_TXN_PCT_PER_SIDE = 0.00297  # NSE transaction charges
SEBI_PCT_PER_SIDE = 0.0001  # SEBI turnover fee (~₹10/crore)
STAMP_DUTY_PCT_BUY_SIDE = 0.003  # stamp duty, buy leg only (Maharashtra rate, common backtest default)
GST_RATE = 0.18  # applied to (brokerage + exchange transaction charges) only

SLIPPAGE_PCT_PER_LEG = 0.05  # conservative worst-case floor — used ONLY for the
# MIN_STOP_TO_COST_RATIO check in engine.py (ensures the stop is large enough
# relative to costs even for the cheapest stocks in the universe). Actual
# per-trade slippage in _simulate_trade uses slippage_pct_per_leg() below.

# Price-aware slippage model — two components:
#   1. Spread: ½ of a 1-tick (₹0.05) bid-ask on each side.  Scales as 1/price,
#      so cheaper stocks pay more slippage % (a ₹0.05 spread is 0.1% on a ₹50
#      stock but only 0.003% on a ₹1,500 stock — a meaningful real-world effect
#      that the old flat 0.05% completely ignored).
#   2. Market impact: flat % per leg — independent of price, reflects queue
#      latency and order-size impact on liquid NIFTY-universe stocks.
SLIPPAGE_HALF_SPREAD_RS = 0.05    # ₹ per share per leg (½ of 1-tick bid-ask)
SLIPPAGE_MARKET_IMPACT_PCT = 0.02  # % per leg, price-independent


def slippage_pct_per_leg(entry_price: float) -> float:
    """Price-aware per-leg execution slippage as % of trade value. Spread component
    (₹0.05/share) scales down for higher-priced stocks; market-impact component (0.02%)
    is flat. Never falls below the market-impact floor alone."""
    spread_pct = SLIPPAGE_HALF_SPREAD_RS / entry_price * 100 if entry_price > 0 else SLIPPAGE_PCT_PER_LEG
    return spread_pct + SLIPPAGE_MARKET_IMPACT_PCT

# Upstox intraday equity brokerage: min(BROKERAGE_PCT_PER_SIDE% of trade
# value, BROKERAGE_FLAT_CAP_RUPEES) per executed order (confirmed against
# Upstox's own published brokerage calculator).
BROKERAGE_PCT_PER_SIDE = 0.1
BROKERAGE_FLAT_CAP_RUPEES = 20.0


def brokerage_rupees(trade_value_rupees: float) -> float:
    """Real Upstox intraday brokerage for ONE leg (one executed order), including GST on the brokerage itself. `trade_value_rupees` = quantity × price for that leg."""
    base = min(BROKERAGE_PCT_PER_SIDE / 100 * trade_value_rupees, BROKERAGE_FLAT_CAP_RUPEES)
    return base * (1 + GST_RATE)


def _gst(base_pct: float) -> float:
    return base_pct * GST_RATE


# Exchange transaction charges also carry GST (brokerage's GST is handled
# separately, inside brokerage_rupees, since brokerage isn't part of this
# flat-%-of-turnover total).
BUY_SIDE_PCT = EXCHANGE_TXN_PCT_PER_SIDE + SEBI_PCT_PER_SIDE + STAMP_DUTY_PCT_BUY_SIDE + _gst(EXCHANGE_TXN_PCT_PER_SIDE)
SELL_SIDE_PCT = STT_PCT_SELL_SIDE + EXCHANGE_TXN_PCT_PER_SIDE + SEBI_PCT_PER_SIDE + _gst(EXCHANGE_TXN_PCT_PER_SIDE)

# A long trade pays BUY_SIDE_PCT on entry + SELL_SIDE_PCT on exit; a short
# trade pays SELL_SIDE_PCT on entry (selling first) + BUY_SIDE_PCT on exit
# (buying to cover) — same total either way. Brokerage is NOT included
# here — see module docstring; it's applied separately, in ₹, once
# quantity is known (backtest/portfolio.py).
#
# NOTE: TOTAL_ROUND_TRIP_COST_PCT no longer includes slippage — slippage is
# now computed per-trade in engine._simulate_trade() via slippage_pct_per_leg()
# so it correctly reflects each stock's actual price level. This constant is
# kept for the MIN_STOP_TO_COST_RATIO floor check only (using the conservative
# SLIPPAGE_PCT_PER_LEG worst-case figure).
REGULATORY_ROUND_TRIP_PCT = BUY_SIDE_PCT + SELL_SIDE_PCT
SLIPPAGE_ROUND_TRIP_PCT = 2 * SLIPPAGE_PCT_PER_LEG  # worst-case, for stop-floor check only
TOTAL_ROUND_TRIP_COST_PCT = REGULATORY_ROUND_TRIP_PCT + SLIPPAGE_ROUND_TRIP_PCT  # stop-floor check only


def compute_itemized_costs(direction: str, entry: float, exit_p: float, qty: int) -> dict:
    """Computes exact itemized costs matching Indian statutory & broker rules."""
    d = 1 if direction.lower() == "long" else -1
    ev = qty * entry
    xv = qty * exit_p
    gross = qty * (exit_p - entry) * d

    brok = brokerage_rupees(ev) + brokerage_rupees(xv)
    stt = (xv if d == 1 else ev) * (STT_PCT_SELL_SIDE / 100)
    stamp = (ev if d == 1 else xv) * (STAMP_DUTY_PCT_BUY_SIDE / 100)
    exch = (ev + xv) * (EXCHANGE_TXN_PCT_PER_SIDE / 100)
    sebi = (ev + xv) * (SEBI_PCT_PER_SIDE / 100)
    gst_charges = exch * GST_RATE
    slip = (ev + xv) * (slippage_pct_per_leg(entry) / 100)
    total_costs = brok + stt + stamp + exch + sebi + gst_charges + slip

    return {
        "gross": gross,
        "brokerage": brok,
        "stt": stt,
        "stamp_duty": stamp,
        "exchange_txn": exch,
        "sebi": sebi,
        "gst": gst_charges,
        "slippage": slip,
        "total": total_costs,
        "net": gross - total_costs
    }


def compute_realized_trade_costs(direction: str, entry: float, exit_p: float, qty: int) -> float:
    """Convenience helper returning total friction in Rupees."""
    return compute_itemized_costs(direction, entry, exit_p, qty)["total"]

